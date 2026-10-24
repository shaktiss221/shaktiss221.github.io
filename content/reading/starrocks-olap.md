---
title: StarRocks and how fast OLAP engines work
date: 2026-10-23
summary: Columnar storage, vectorized execution, cost-based joins and real-time upserts, learned through one open-source engine.
tags: databases, analytics
---

A fast analytics database is mostly a machine for not reading data: it stores columns instead of rows, skips blocks it can prove are irrelevant, and processes what is left in batches that fit the CPU. StarRocks is a good engine to learn this from because it combines nearly every modern trick in one open-source system: columnar storage, a vectorized MPP engine, a cost-based optimizer, real-time updates, and the option to run on object storage or directly over a data lake.

## 1. Why analytics engines look different

A transactional database and an analytical database are optimized for opposite query shapes, and almost every design choice below follows from that.

| | OLTP (Postgres, MySQL, DynamoDB) | OLAP (StarRocks, ClickHouse, Druid, Snowflake) |
| --- | --- | --- |
| Typical query | Fetch or update one order by ID | Sum revenue by country and week over two years |
| Rows touched | A handful | Millions to billions |
| Columns touched | Most of the row | 3 to 10 of perhaps 200 |
| Writes | Many small, concurrent transactions | Batches or streams of appends, fewer updates |
| Latency goal | Milliseconds per request | Sub-second to seconds per query |
| Storage layout | Rows together (B-tree pages) | Columns together |

The key line is "columns touched". If a table has 200 columns and a query reads 4, a row store must still pull all 200 off disk, because each page holds whole rows. A column store reads only the 4 columns it needs, which can be 50 times less I/O before any other trick.

Columns also **compress** far better than rows, because a column holds values of one type that often repeat (country codes, status flags, timestamps that increase). Less data on disk means less to read and more fits in memory.

The third idea is **parallelism**. An analytical query is easy to split: each node scans and partially aggregates its own slice of the data, then the partial results are merged. Systems that do this across many machines are called **MPP** (massively parallel processing) databases.

StarRocks sits in a family of engines built on these ideas. Its particular aim is to handle three workloads in one system: low-latency dashboards with many concurrent users, complex multi-table joins (which some fast engines handle poorly), and data that changes in real time.

## 2. Architecture: frontends, backends and where the data lives

StarRocks has only two kinds of process: frontends that think and backends that work. Its biggest architectural change, from shared-nothing to shared-data, is about where the backends keep their data.

<!--diagram:e2888a5c-7fc9-->

In shared-nothing mode the bottom two layers merge: backends store tablets on their own disks, three copies each, and scaling means moving data.

### A little history

StarRocks started in 2020 as a fork of Apache Doris (itself born at Baidu as Palo), first called DorisDB. The team rewrote large parts of the execution engine and optimizer, renamed it StarRocks and open-sourced it in 2021. In February 2023 CelerData, the company behind it, [contributed the project to the Linux Foundation](https://bigdatawire.com/2023/02/14/celerdata-contributes-starrocks-to-the-linux-foundation). CelerData sells a managed cloud version; StarRocks 4.0 [was released in late 2025](https://www.starrocks.io/blog/starrocks-4.0-now-available).

### Frontend (FE)

FE nodes are written in Java and handle everything except touching data:

- **Metadata**: databases, tables, partitions, tablets and which nodes hold them.
- **Query planning**: parse SQL, run the cost-based optimizer, and split the plan into fragments for the backends.
- **Coordination**: schedule fragments, track load jobs and transactions.

One FE is the **leader**; others are **followers** that replicate the metadata journal and can take over through an election, or **observers** that only replicate and serve reads to scale out query planning. The metadata journal is replicated with an embedded Berkeley DB Java Edition.

### Backend (BE) and compute node (CN)

BE nodes are written in C++ and do the real work: storing data and executing query fragments. A query typically runs on all BEs at once, each scanning and pre-aggregating its own tablets, before results are exchanged and merged.

A **compute node (CN)** is a backend with no permanent data, only execution and a cache. CNs are the unit of elastic scaling: you can add 20 for a busy hour and remove them afterwards, because nothing needs to be moved.

### Shared-nothing vs shared-data

In the original **shared-nothing** mode, each table is split into **tablets** and each tablet is stored on the local disks of usually three BEs. This is fast and simple, but storage and compute scale together: to add disk you add machines, and adding or removing a node means moving tablets around.

Starting with version 3.0 (2023), StarRocks added a **shared-data** mode. Data lives in object storage (S3, GCS, Azure Blob, MinIO or HDFS) as the source of truth, and CNs keep a local **cache** of hot data on disk and in memory. Object storage provides durability and cheap capacity, so the engine does not need its own three-way replication; the cache keeps repeated queries fast. Compute becomes stateless and elastic.

The trade-off is the cold read. A query that misses the cache pays object-storage latency, which is tens of milliseconds per request rather than well under one millisecond for local NVMe. Much of the engineering in shared-data mode goes into prefetching, cache warm-up and making sure small writes are batched before they reach object storage. This is the same direction Snowflake took a decade earlier and most analytics systems have followed since.

## 3. Storage: how a column store avoids reading data

Every layer of StarRocks storage exists to let a query skip work: partitions skip whole time ranges, indexes skip blocks inside a file, and encodings shrink what remains.

### Partitions, buckets and tablets

A table is first **partitioned**, usually by date (one partition per day or month). A query with `WHERE dt >= '2026-10-01'` never opens older partitions. This is **partition pruning**, and it is the single biggest lever on query speed.

Each partition is then split into **buckets** by hashing one or more columns (or randomly). Each bucket of each partition is a **tablet**, the unit that is stored, replicated, moved and scanned in parallel. Choosing the bucket key well matters for joins, as section 4 shows.

### Inside a tablet

Data is written as immutable **segment files**. Inside a segment, each column is stored separately and broken into **pages** of a few tens of kilobytes. Each column picks an encoding suited to its data:

- **Dictionary encoding** for low-cardinality strings: store "Germany" once and use a small integer everywhere else.
- **Run-length encoding** for long runs of repeated values.
- **Frame-of-reference and bit-packing** for integers that sit close together, such as timestamps.

Then a general compressor such as LZ4 or Zstandard runs on top.

Rows in a segment are sorted by the table's **sort key**. That enables a set of lightweight indexes:

- A sparse **prefix index** on the sort key, so a query filtering on the leading key columns can jump straight to the right range.
- **Zone maps**: the min and max of each column per page and per segment. A filter `amount > 1000` skips every page whose max is below 1,000 without decompressing it.
- Optional **Bloom filter** and **bitmap** indexes for high-selectivity filters on columns that are not in the sort key.

An engine that is smart about these indexes can answer "revenue for one customer last week" by reading a tiny fraction of a terabyte table.

### The four table types

Column stores are naturally append-only, since rewriting a value in a compressed, sorted column file is expensive. StarRocks offers four table types that differ in how they handle rows with the same key:

| Table type | Behavior | Typical use |
| --- | --- | --- |
| Duplicate Key | Keeps every row; the key only defines the sort order | Logs, events, append-only facts |
| Aggregate | Rows with the same key are merged with a function per column (SUM, MAX, REPLACE and so on) | Pre-aggregated metrics |
| Unique Key | Keeps only the latest row per key, merged at **read** time | Older way to do upserts |
| Primary Key | Keeps only the latest row per key, resolved at **write** time | Real-time upserts and deletes, CDC from OLTP databases |

### Merge-on-read vs delete-and-insert

The difference between the last two is the most instructive design choice in the system.

With **merge-on-read** (Unique Key), an update simply writes a new version of the row. At query time, the engine must merge all versions of each key and keep the newest. That merge has to happen *before* filters can be trusted: a filter on an old version could wrongly include a row whose latest version no longer matches. So filters cannot be pushed down into the scan, and reads get slower as updates pile up until compaction cleans them.

The **Primary Key** table uses **delete-and-insert** (also called merge-on-write). It keeps a **primary key index** mapping each key to the location of its current row. On each write, the engine looks up the old row, marks it deleted in a per-segment **delete vector** (a bitmap of dead row positions), and writes the new row. Reads then simply skip rows marked dead. No merging is needed, filters and indexes work normally, and query speed stays close to an append-only table even under heavy updates.

The price is paid on writes: every upsert needs an index lookup. StarRocks keeps the index in memory, or as a persistent on-disk index to cap memory use on very large tables. This is the classic trade-off between optimizing for writes (LSM-style, merge later) and for reads (do the work up front), and for dashboards that are read far more often than written, paying at write time wins.

## 4. Query execution: making the CPU the bottleneck

Once I/O is minimized, the CPU becomes the limit, and StarRocks' execution engine is built around keeping every core busy on tight loops over columns.

### Vectorized execution

A classic database engine (the Volcano model) processes **one row at a time**: each operator calls `next()` on its child, gets a row, and passes it up. For analytics this wastes most of the CPU on function calls, branches and type checks per row.

A **vectorized** engine passes **batches of a few thousand values per column** between operators. A filter runs one tight loop over an array of integers; an aggregation adds up an array. These loops are friendly to CPU caches and branch predictors, and compilers can turn them into **SIMD** instructions that process 8 or 16 values in one instruction. The difference between row-at-a-time and vectorized execution on analytic queries is often several times, sometimes an order of magnitude. StarRocks has been fully vectorized in C++ since its early versions; this was one of the main changes from its Doris roots.

### MPP and the pipeline engine

The FE turns a query plan into **fragments**, and each fragment runs as many parallel instances across BEs, connected by **exchange** operators that ship data over the network. Partial aggregation happens near the data, final aggregation after an exchange.

Inside each node, the **pipeline execution engine** breaks fragments into pipelines of operators that are scheduled on a fixed pool of threads, roughly one per core. Instead of each query grabbing its own threads and blocking on I/O, a pipeline that is waiting for data yields its thread to one that has work. This matters most under **concurrency**: hundreds of dashboard queries share the cores fairly instead of fighting through thread context switches.

### The cost-based optimizer

Fast operators do not help if the plan is bad, and for multi-table joins the plan dominates everything. StarRocks has a **cost-based optimizer (CBO)** in the Cascades style. It explores equivalent plans by applying transformation rules (reordering joins, pushing predicates and aggregations down, rewriting subqueries) and picks the cheapest using **statistics**: row counts, distinct counts, null counts and histograms per column.

The optimizer also chooses how to execute each distributed join:

| Join strategy | How it works | When it is chosen |
| --- | --- | --- |
| Broadcast | Send the whole small table to every node | One side is small |
| Shuffle | Hash both sides on the join key and redistribute | Both sides are large |
| Bucket shuffle | Shuffle only one side, to match the other's existing bucketing | One side is already bucketed by the join key |
| Colocate | No network at all: both tables are bucketed identically, so matching rows are already on the same node | Tables designed together, same bucket key and count |

This table is why bucket keys matter. Two large fact tables that are always joined on `user_id` and are both bucketed by `user_id` can be joined with no data movement.

### Runtime filters

In a join between a large fact table and a filtered dimension table ("sales in stores in Germany"), the engine builds a hash table on the small side first. StarRocks then turns the set of join keys into a **runtime filter** (a Bloom filter, an IN-list or a min-max range) and pushes it **into the scan of the large table**, even on other nodes. Rows that cannot possibly join are discarded before they are decoded, sent over the network or probed. For star-schema queries, runtime filters often remove most of the fact table rows before the join even starts.

## 5. Real-time data, materialized views and the data lake

Three features turn a fast query engine into a platform: loading data continuously, precomputing expensive results, and querying data that lives somewhere else.

### Loading data

StarRocks offers several loading paths, each with transaction semantics so that a batch becomes visible all at once:

- **Stream Load**: push a file or batch over HTTP. The usual building block for connectors such as Flink and Kafka.
- **Routine Load**: StarRocks itself consumes a Kafka topic continuously.
- **Broker Load and INSERT INTO ... SELECT FROM FILES()**: bulk loads from object storage.

Combined with Primary Key tables, this supports **change data capture**: replicate inserts, updates and deletes from MySQL or Postgres through Flink CDC or Debezium into StarRocks, and dashboards see changes within seconds.

The usual warning applies: column stores dislike many tiny writes. Each load creates a new data version that later needs compaction. Batch writes into loads of at least a few seconds or thousands of rows rather than one row per load.

### Materialized views

A **materialized view** stores the result of a query and keeps it updated. StarRocks supports **synchronous** views (single-table rollups maintained on every load) and **asynchronous** views that can join several tables and refresh on a schedule or when base tables change, refreshing only changed partitions.

The powerful part is **transparent query rewrite**. Analysts keep querying the base tables; the optimizer notices that a materialized view can answer the query (or part of it) more cheaply and rewrites the plan to use it. You get pre-aggregation without changing a single dashboard query. The cost is storage, refresh compute and some staleness.

### Querying the lake

Through **external catalogs**, StarRocks can query tables in Hive, Apache Iceberg, Apache Hudi, Delta Lake and Apache Paimon directly in object storage, using the same vectorized engine and optimizer. A common pattern now is a **lakehouse**: keep the data once in open table formats such as Iceberg, query it in place from StarRocks for interactive dashboards, and only copy the hottest tables or materialized views into native StarRocks storage when they need lower latency. Engineering blogs from companies such as Pinterest and Coinbase have described this pattern.

## 6. How it compares, and when to pick it

StarRocks competes in a crowded field, and the honest summary is that each engine is strongest on a different query shape.

| Engine | Strongest at | Weaker at |
| --- | --- | --- |
| StarRocks | Multi-table joins with a real CBO, high concurrency, real-time upserts, querying lake tables | Operational maturity and ecosystem are smaller than ClickHouse's |
| Apache Doris | Very similar design (shared ancestry); strong in the same niches | Same trade-offs; the two projects now differ mainly in details and community |
| ClickHouse | Raw scan and aggregation speed on a single wide table, huge community | Complex joins have historically needed careful manual tuning; updates are expensive |
| Apache Druid / Pinot | Sub-second slice-and-dice on event streams with many concurrent users | Joins and ad-hoc SQL are limited |
| Trino | Federated SQL over many sources and lakes, no storage of its own | Less suited to many concurrent low-latency dashboard queries |
| Snowflake, BigQuery | Fully managed, elastic, broad SQL | Cost and latency for high-concurrency customer-facing analytics |

Good reasons to choose StarRocks:

- Your data model is a **star or snowflake schema** with several large joins, and you do not want to denormalize everything into one wide table.
- You need **customer-facing analytics**: hundreds of concurrent queries with sub-second targets.
- The data **changes**, for example CDC from an OLTP database, and you need upserts and deletes to be cheap.
- You already keep data in **Iceberg or another lake format** and want fast queries without copying everything.

Reasons to hesitate: you need a mature, widely understood operational story above all; your workload is one denormalized table where ClickHouse is excellent; or a managed warehouse already meets your latency and cost needs.

Vendor benchmarks are worth reading skeptically. Every engine in this table publishes a benchmark it wins. The useful exercise is to run your own top 20 queries, at your own concurrency, on your own data shape.

## 7. Ideas to carry away, and questions for our discussion

Five ideas that apply to any analytics system:

1. **The fastest data is data you never read.** Columnar layout, partition pruning, zone maps and runtime filters are all ways of skipping work.
2. **Batch the CPU's work.** Vectorized execution turns per-row overhead into tight loops the hardware is built for.
3. **The plan matters more than the operators.** For joins, a cost-based optimizer with good statistics beats any amount of raw speed.
4. **Pay at write time when reads dominate.** Delete-and-insert makes upserts slower so that every query is faster.
5. **Separate storage from compute.** Object storage plus a local cache makes compute elastic, at the price of cold-read latency.

Think about these before we talk:

- Why can't a merge-on-read table push a filter like `status = 'paid'` into the scan, while a delete-and-insert table can?
- You have `orders` and `order_items`, both large and always joined on `order_id`. How would you partition and bucket them, and which join strategy would you expect?
- A dashboard query is fast at 10 users and slow at 300. Which parts of the architecture would you look at first?
- When would a materialized view with query rewrite be a bad idea?
- In shared-data mode, what happens to the first query after you scale from 4 to 20 compute nodes, and how could you soften it?
- For your own analytics needs, which row of the comparison table fits best, and why?

## Sources

- StarRocks documentation: [Architecture](https://docs.starrocks.io/docs/introduction/Architecture/), [Primary Key table](https://docs.starrocks.io/docs/table_design/table_types/primary_key_table/), [Features](https://docs.starrocks.io/docs/introduction/Features/), [Deploy shared-data StarRocks (3.0)](https://docs.starrocks.io/docs/3.0/deployment/deploy_shared_data/)
- StarRocks blog, [The answer to the data upsert challenge in real-time analytics](https://starrocks.io/blog/the-answer-to-the-data-upsert-challenge-in-real-time-analytics)
- StarRocks blog, [StarRocks 4.0 now available](https://www.starrocks.io/blog/starrocks-4.0-now-available), and [StarRocks 2025 year in review](https://www.starrocks.io/blog/starrocks-2025-year-in-review)
- BigDATAwire, [CelerData contributes StarRocks to the Linux Foundation](https://bigdatawire.com/2023/02/14/celerdata-contributes-starrocks-to-the-linux-foundation), February 2023
- Rill Data, [Why Coinbase and Pinterest chose StarRocks](https://www.rilldata.com/blog/why-coinbase-and-pinterest-chose-starrocks-lakehouse-native-design-and-fast-joins-at-terabyte-scale)
- Background: Boncz, Zukowski and Nes, *MonetDB/X100: Hyper-Pipelining Query Execution* (CIDR 2005), the paper that popularized vectorized execution; Graefe, *The Cascades Framework for Query Optimization* (1995)

A caveat on today's sources: I could open search results but not the StarRocks documentation pages themselves, so the internal details (FE roles, encodings, index types, join strategies, primary key index) come from my knowledge of StarRocks and are not re-checked against the current docs. The Linux Foundation move, shared-data arriving in 3.0, and the 4.0 release were confirmed through search results; the 2020 and 2021 origin dates are from memory.
