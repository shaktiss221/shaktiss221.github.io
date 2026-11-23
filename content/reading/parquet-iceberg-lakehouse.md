---
title: "Parquet, Iceberg and the lakehouse: how tables are built from files"
date: 2026-11-23
summary: How modern analytics stores data as files in object storage and still gets fast queries and ACID transactions. Covers row vs columnar layout, Parquet's row groups, column chunks, pages, encodings and statistics, why object storage changes the rules, the Hive table problem, how Apache Iceberg and Delta Lake build tables from immutable files with snapshot metadata, commits with optimistic concurrency, row-level deletes, time travel, compaction and maintenance, and choosing between the formats.
tags: analytics, databases
daily: true
---

A growing share of the world's analytical data doesn't live inside a database at all. It sits as Parquet files in Amazon S3, Google Cloud Storage or Azure Blob Storage, and many different engines (Spark, Trino, Snowflake, BigQuery, Databricks, StarRocks, DuckDB) read and write it directly. For years that meant giving up what databases provide: transactions, consistent snapshots, safe concurrent writers, schema changes, and row-level updates. **Open table formats**, chiefly Apache Iceberg and Delta Lake (with Apache Hudi as a third), put those back by adding a layer of metadata files on top of the data files. The result is often called a **lakehouse**: data-lake storage with data-warehouse behaviour. This reading works up from the bottom: why columnar storage is fast for analytics, how Parquet lays out bytes, why object storage breaks the old "table is a directory" model, and how Iceberg and Delta build reliable tables from immutable files. It draws on the Parquet documentation, the Dremel paper, the Iceberg specification, the Delta Lake paper (VLDB 2020), and engineering writing from Netflix and Databricks. The earlier StarRocks reading covered an OLAP engine; this one covers the storage those engines increasingly read.

## 1. Row vs columnar storage

A transactional database stores rows together: all of order 1's fields, then all of order 2's. That's ideal for "fetch or update this one order". Analytical queries look different: "total revenue by country for last quarter" reads two or three columns out of perhaps fifty, across billions of rows.

**Columnar storage** stores each column's values together. For that query, the engine reads only the `country`, `amount` and `date` columns and skips the other 47 entirely. Columnar layout also brings:

- **Better compression.** Values in one column are the same type and often similar (many repeated countries, sorted dates), so they compress far better than mixed rows, often several times better.
- **Vectorised execution.** Engines process a batch of values from one column with tight loops and SIMD instructions, which modern CPUs execute very efficiently.
- **Skipping.** Per-column statistics (min, max, null count) let the engine skip whole blocks that can't match a filter.

The costs are that writing a single row touches every column's storage, and updating in place is expensive. Columnar formats are therefore written in large batches and treated as immutable, which turns out to fit object storage well.

## 2. Inside a Parquet file

Apache Parquet was created in 2013 by engineers at Twitter and Cloudera, modelled on the record shredding approach in Google's Dremel paper (Melnik et al., VLDB 2010). It's now the default file format for analytical data. A Parquet file is organised in three levels:

- **Row groups.** The file is split horizontally into row groups, each holding a slice of rows (for example a few hundred thousand to millions of rows). The Parquet documentation recommends large row groups, 512 MB to 1 GB, so that each column chunk is big enough for efficient sequential reads. In practice many writers default to around 128 MB, a balance between scan efficiency and memory needed during writes.
- **Column chunks.** Within each row group, each column's values are stored contiguously as a column chunk. A query that needs three columns reads three column chunks per row group.
- **Pages.** Each column chunk is divided into pages, the unit of encoding and compression. The documentation suggests 8 KB pages; many writers use larger defaults (around 1 MB). Smaller pages allow finer-grained skipping; larger pages compress better.

At the end of the file is the **footer**, containing the schema and, for every row group and column chunk, its location and **statistics**: min, max and null count. A reader fetches the footer first (one small read from the end of the file), then decides which row groups to read at all.

### Encodings and compression

Before general-purpose compression, Parquet applies **encodings** suited to columnar data:

- **Dictionary encoding:** a column with few distinct values (country, status) stores a dictionary of the distinct values and replaces each value with a small integer index. This is the most important encoding for typical business data.
- **Run-length and bit-packing (RLE):** runs of repeated values or small integers are stored compactly, which is how dictionary indexes and nulls are stored.
- **Delta encodings** for sorted integers and timestamps, storing differences between consecutive values.

Then pages are compressed with a codec such as Snappy, Zstandard or Gzip. Zstd has become a common choice for its ratio-to-speed balance.

### Nested data

Parquet handles nested structures (structs, lists, maps) using Dremel's **repetition and definition levels**: two small integers per value that record where in the nesting each value sits and whether optional parents were null. This lets nested JSON-like data be stored column by column and read back exactly.

### Skipping work

A query engine reading Parquet uses several layers of pruning:

1. **Column projection:** read only the needed columns.
2. **Row group pruning:** skip row groups whose min/max statistics show they can't match the filter (for `date >= '2026-10-01'`, skip any row group whose max date is earlier).
3. **Page-level indexes:** Parquet's optional column and offset indexes record min/max per page, allowing even finer skipping.
4. **Bloom filters:** optional per-column Bloom filters (covered in the probabilistic data structures reading) help with equality filters on high-cardinality columns, where min/max ranges are useless.

Statistics only help if data is **clustered**: if dates are scattered randomly across row groups, every row group's range spans everything and nothing gets skipped. Sorting or clustering data on common filter columns when writing is one of the biggest performance levers.

## 3. Object storage changes the rules

Object stores like S3 are cheap, durable and effectively unlimited, which is why analytics moved there. But they aren't file systems:

- **No real directories or renames.** A "directory" is just a key prefix, and "renaming" a directory means copying and deleting every object under it, which isn't atomic.
- **Listing is slow and paginated.** Listing millions of objects under a prefix takes many API calls.
- **Objects are immutable.** You can't append to or modify part of an object; you rewrite it.
- **High per-request latency** (tens of milliseconds) but huge aggregate throughput, so engines read large ranges in parallel.

S3 has offered strong read-after-write consistency since December 2020, which removed one class of problems, and newer conditional-write support (put-if-absent) helps coordination. But the core limitation remains: there's no atomic multi-object operation.

## 4. The Hive table problem

Before table formats, the common way to define a table on a data lake was the **Hive** convention: a table is a directory, partitions are subdirectories (`/orders/date=2026-11-22/`), and the table's contents are whatever files happen to be in those directories. A metastore records the table's schema and its list of partitions.

This breaks down at scale and under concurrency:

- **No atomic commits.** A job writing 500 files is visible to readers while it's half done. A failed job leaves partial files behind.
- **No isolation between writers.** Two jobs writing the same partition can clobber each other.
- **Listing is the query plan.** To find files, the engine lists directories, which is slow with many partitions and files.
- **Partitioning leaks into queries.** Users must filter on the partition column exactly as stored (`date_str = '2026-11-22'`), or the engine scans everything.
- **Schema changes are fragile.** Renaming or reordering columns can silently misread old files that identify columns by name or position.
- **Updates and deletes** mean rewriting whole partitions.

Netflix hit these limits with tables of tens of petabytes, which is why Ryan Blue and Daniel Weeks started Iceberg there in 2017.

## 5. Apache Iceberg: tables as trees of immutable files

Iceberg's central idea is to stop treating a directory listing as the table. Instead, the table is defined by **metadata files that list exactly which data files belong to it**, organised as a tree, plus a single pointer to the current root. Iceberg was open-sourced and donated to the Apache Software Foundation in November 2018 and became a top-level Apache project in May 2020.

<!--diagram:iceberg-tree-1123-->

The layers, from the top:

- **Catalog.** Stores, for each table, a pointer to its current metadata file. This could be a Hive metastore, a database (via JDBC), AWS Glue, Nessie, or a service implementing Iceberg's REST catalog API (such as Apache Polaris or Unity Catalog). The catalog is the only mutable piece, and it must support an atomic compare-and-swap of the pointer.
- **Metadata file** (`vN.metadata.json`): the table's schema (with column IDs), partition spec, sort order, properties, and a list of **snapshots**, each representing the table's state after a commit.
- **Manifest list:** one per snapshot, listing the manifest files that make up that snapshot, with summary statistics (such as partition value ranges) for each manifest.
- **Manifest files:** each lists a set of data files (or delete files) with per-file details: partition values, record counts, and **column-level statistics** (min, max, null counts).
- **Data files:** Parquet (or ORC/Avro) files containing the rows.

### How a read works

The engine asks the catalog for the current metadata file, picks the current snapshot (or an older one, for time travel), reads its manifest list, uses manifest-level partition ranges to skip irrelevant manifests, reads the remaining manifests, uses file-level statistics to skip irrelevant data files, and then reads only the needed columns and row groups from the surviving files. **No directory listing is involved at any point.** The query plan comes entirely from metadata, which is why Iceberg scales to tables with millions of files.

### How a commit works

A writer:

1. Writes new data files (Parquet) to object storage. Nobody can see them yet.
2. Writes new manifests listing them, a new manifest list (reusing existing manifests for unchanged data), and a new metadata file with the new snapshot appended.
3. Asks the catalog to **atomically swap** the table pointer from the metadata file it started from to the new one.

If another writer committed first, the swap fails. The writer then re-reads the new current state, checks whether its change conflicts (for example, whether both changed the same files), and if not, rewrites its metadata on top of the new state and retries. This is **optimistic concurrency control**, the same idea as compare-and-set in the distributed locks reading, with the catalog pointer acting as the version. Readers always see a complete snapshot: either before the commit or after, never half of it. That gives **serialisable isolation for commits and snapshot isolation for reads**, on top of a storage system with no transactions at all.

### Features that fall out of the design

- **Time travel and rollback.** Old snapshots remain until expired, so queries can read the table "as of" a snapshot ID or timestamp, and a bad write can be rolled back by pointing at the previous snapshot.
- **Schema evolution by ID.** Columns are tracked by unique IDs, not names or positions, so adding, dropping, renaming and reordering columns are metadata-only changes that never misread old files.
- **Hidden partitioning.** The partition spec is defined as a transform of a column (`day(event_time)`, `bucket(16, user_id)`), and Iceberg derives partition values itself. Users filter on `event_time` directly and the engine prunes partitions automatically. The spec can even change over time (**partition evolution**): old data keeps its old layout and new data uses the new one.
- **Branches and tags** on snapshots, enabling write-audit-publish workflows: write to a branch, validate, then fast-forward main.

### Row-level deletes and updates

Rewriting whole files for every delete is expensive, so the Iceberg v2 format added **delete files**:

- **Position deletes** record (data file path, row position) pairs to remove.
- **Equality deletes** record column values (for example `id = 5`) that should be treated as deleted in older data files. These are cheap to write, which suits streaming upserts, but expensive to apply at read time.

This gives a choice per table: **copy-on-write** (rewrite affected files on every update; fast reads, slow writes) or **merge-on-read** (write small delete files; fast writes, readers merge deletes at query time until compaction rewrites the files). The **v3** format adds **binary deletion vectors**, a compact per-file bitmap of deleted rows (the same approach Delta uses), along with row lineage tracking, default column values, and new types such as variant (semi-structured data) and geospatial types.

## 6. Delta Lake: a transaction log in the object store

Databricks' Delta Lake, open-sourced in 2019 and described in "Delta Lake: High-Performance ACID Table Storage over Cloud Object Stores" (Armbrust et al., VLDB 2020), reaches the same goals with a different structure: an ordered **transaction log** stored alongside the data.

- The table directory contains a `_delta_log/` folder of numbered JSON files (`000...0001.json`, `000...0002.json`, ...). Each one is a commit, listing actions: **add** these files, **remove** those files, change metadata or schema.
- The table's state is the result of replaying the log. To avoid replaying thousands of JSON files, Delta periodically writes **checkpoints**, Parquet files summarising the full state at a given version.
- **A commit is the creation of the next log file.** Writers use optimistic concurrency: they try to create version N+1, and if it already exists, they check for conflicts and retry. Creating a file only if it doesn't exist requires a put-if-absent primitive, which some object stores historically lacked (Delta on S3 used a coordination service such as DynamoDB for this, though S3 now supports conditional writes).
- Because no server holds table state, any engine that understands the log can read the table.

Delta's features mirror Iceberg's: time travel by version or timestamp, schema enforcement and evolution, deletion vectors for merge-on-read, Z-ordering and liquid clustering for data layout, and change data feed for incremental processing.

The structural difference: Iceberg's metadata is a **tree** of snapshots and manifests with a catalog pointer, while Delta's is a **linear log** with checkpoints. Both work well; Iceberg's design doesn't depend on the storage layer for atomicity (the catalog provides it) and was built from the start to be engine-neutral, while Delta was tightly integrated with Spark and Databricks before broadening.

**Apache Hudi**, started at Uber in 2016, is the third major format, with a strong focus on record-level upserts and incremental pulls for streaming ingestion. Projects like Apache XTable and Delta UniForm generate metadata for more than one format over the same data files, so readers can choose.

## 7. Keeping tables healthy: maintenance

Immutable files and snapshots mean a table accumulates debris. Production lakehouses need routine maintenance, and neglecting it is the most common source of slow queries and high costs.

- **Compaction.** Streaming and frequent small batches create many small files, and merge-on-read creates delete files. Many small files mean many object-store requests, large metadata and poor compression. Periodic compaction (Iceberg's `rewrite_data_files`, Delta's `OPTIMIZE`) rewrites them into right-sized files and applies pending deletes.
- **Clustering and sorting.** Rewriting data sorted or clustered on common filter columns (sort orders, Z-order, Hilbert curves) makes min/max statistics selective, which can cut scanned data by orders of magnitude.
- **Snapshot expiry.** Old snapshots keep old files alive for time travel. Expiring snapshots older than a retention window (`expire_snapshots`, Delta's `VACUUM`) lets their unreferenced files be deleted. Retention is a trade-off between time-travel and rollback ability and storage cost, and it has compliance implications: a "deleted" row still exists in old files until those snapshots are expired and the files removed.
- **Orphan file cleanup.** Failed writes leave files that no snapshot references; a periodic job finds and deletes them.
- **Manifest rewriting.** Many small commits create many small manifests, slowing planning; rewriting manifests keeps metadata compact.

Managed services increasingly run these automatically, but someone still needs to choose the policies.

## 8. Why this matters for backend engineers

Even if you never run Spark, table formats are showing up in backend architectures:

- **CDC into the lake.** Change streams from operational databases (Debezium, database-native CDC) land in Iceberg or Delta tables with upserts, giving analytics an up-to-date copy without loading the production database.
- **Streaming ingestion.** Kafka topics are written to tables continuously by Flink, Spark Structured Streaming or the platforms' built-in connectors; some streaming platforms can now expose topics directly as Iceberg tables.
- **One copy, many engines.** Instead of copying data into each warehouse, teams keep one Iceberg table and query it from Snowflake, BigQuery, Trino, Spark or DuckDB, avoiding lock-in and duplication.
- **Data deletion and compliance.** Handling GDPR deletion requests on a lakehouse means row-level deletes plus snapshot expiry plus file cleanup, which needs to be designed in, not discovered during an audit.

## 9. Choosing and using a format

- **Iceberg** if you want the broadest engine support and catalog flexibility, large tables with many partitions, or a vendor-neutral standard. It has become the common denominator that most warehouses support for external tables.
- **Delta Lake** if you're primarily on Databricks or Spark and want the tightest integration there.
- **Hudi** if your workload is dominated by high-frequency upserts with incremental consumption and your stack supports it well.

Whatever the format:

- **Choose a catalog deliberately.** It's the source of truth for which metadata is current and the atomic commit point. Multiple writers must go through the same catalog.
- **Design partitioning for pruning, not for directories.** With hidden partitioning, partition on what queries filter by, at a granularity that yields reasonably large files (daily rather than hourly for modest volumes).
- **Watch file sizes.** Aim for files in the hundreds of megabytes; alert when average file size drops.
- **Schedule maintenance** from day one: compaction, snapshot expiry, orphan cleanup.

## Key takeaways

- **Columnar** layout reads only needed columns, compresses well, and enables vectorised execution and min/max skipping.
- **Parquet** organises data as row groups → column chunks → pages, with dictionary/RLE/delta encodings, a footer of statistics, and optional page indexes and Bloom filters. Clustering data on filter columns is what makes statistics useful.
- **Object storage** has no atomic rename or multi-object transactions, which made **Hive-style directory tables** unsafe and slow at scale.
- **Iceberg** defines a table as a tree (catalog pointer → metadata file → manifest list → manifests → data files) and commits by **atomically swapping the pointer** with optimistic concurrency. It provides snapshot isolation, time travel, ID-based schema evolution, hidden partitioning, and row-level deletes (position/equality deletes in v2, deletion vectors in v3).
- **Delta Lake** uses an ordered **transaction log** in the object store, with Parquet checkpoints and put-if-absent commits.
- Lakehouse tables need **maintenance**: compaction, clustering, snapshot expiry and orphan cleanup.

## Discussion questions

1. A query filtering on `customer_id = 123` scans the whole table despite Parquet statistics. Why might that be, and what would you change in how the data is written?
2. A streaming job writes to an Iceberg table every 10 seconds. What problems will appear after a week, and what maintenance would you schedule?
3. Compare copy-on-write and merge-on-read for a table receiving 1,000 updates per second and queried by dashboards every minute. Which would you pick?
4. A user asks for their data to be deleted under GDPR. Walk through what has to happen in an Iceberg or Delta table for the data to be truly gone.
5. Why does Iceberg need a catalog with atomic compare-and-swap, and what could go wrong if two writers used different catalogs for the same table?
6. Would you move an analytics workload from a managed warehouse's native storage to Iceberg tables? What would you gain and what would you have to operate yourself?

## Sources

- [Apache Parquet documentation: file format and configurations](https://parquet.apache.org/docs/file-format/configurations/) (row group and page size recommendations), encodings, page index and Bloom filter specs
- S. Melnik et al., "Dremel: Interactive Analysis of Web-Scale Datasets," VLDB 2010
- [Apache Iceberg table specification](https://iceberg.apache.org/spec/) (metadata files, manifest lists, manifests, v2 delete files, v3 additions); Iceberg documentation on hidden partitioning, schema evolution, branching and maintenance procedures
- [Wikipedia: Apache Iceberg](https://en.wikipedia.org/wiki/Apache_Iceberg) (Netflix origin 2017, ASF donation 2018, top-level project 2020)
- M. Armbrust et al., ["Delta Lake: High-Performance ACID Table Storage over Cloud Object Stores"](https://www.vldb.org/pvldb/vol13/p3411-armbrust.pdf), PVLDB 13(12), 2020; Delta Lake protocol specification
- Apache Hudi documentation; Apache XTable and Delta UniForm documentation
- AWS, "Amazon S3 now delivers strong read-after-write consistency" (December 2020) and S3 conditional writes documentation
- Ryan Blue's talks and Netflix Tech Blog posts on Iceberg's motivation

*Note on verification: the Parquet configuration recommendations, Iceberg spec details (some from a summary of the long spec page), Iceberg's history and the Delta Lake paper were read today. Writer default sizes (128 MB row groups, 1 MB pages), Parquet's 2013 Twitter/Cloudera origin, Hudi's Uber origin, S3 consistency and conditional writes, and catalog implementations come from documentation as I know it and weren't re-fetched.*
