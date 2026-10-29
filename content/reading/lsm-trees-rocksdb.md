---
title: "LSM trees and RocksDB: trading reads for writes"
date: 2026-10-29
summary: Why log-structured merge trees beat B-trees for write-heavy workloads, how memtables, SSTables, Bloom filters and compaction work in RocksDB, the read/write/space amplification trade-off, and what MyRocks, WiscKey and newer research changed.
tags: databases, distributed systems
daily: true
---

If you've used Cassandra, ScyllaDB, CockroachDB, TiKV, Kafka Streams state stores, MyRocks, or the storage behind many queues and time-series systems, you've used a log-structured merge tree. The LSM tree is the main alternative to the B-tree, and for write-heavy workloads on modern storage it usually wins. The idea fits in one sentence: never update data in place; buffer writes in memory, write them out as sorted immutable files, and merge those files in the background. Everything interesting about LSM trees, from their speed to their failure modes, comes from that background merging. This reading builds an LSM tree from scratch, using RocksDB as the concrete example, and ends with how real systems tune and extend it.

## 1. Why not just use a B-tree?

A B-tree (more precisely a B+tree, as used in InnoDB, Postgres indexes, and SQLite) keeps data sorted in fixed-size pages, typically 4 to 16 KB, arranged as a shallow tree. To update a row, you find its page, change it, and write the page back. Reads are excellent: three or four page lookups find any key, and the upper levels are almost always cached.

Writes are where it hurts. Changing a 100-byte row means rewriting an entire page (say 16 KB), so the **write amplification** (bytes written to storage divided by bytes the application wrote) can be in the hundreds for small random updates. Those writes are also random, scattered across the file. On spinning disks, random writes were painfully slow. On SSDs they're fast, but SSDs have their own reasons to dislike them: flash can't overwrite in place, so the drive's firmware does its own garbage collection, and random small writes increase it, wearing out the drive faster.

Pages also waste space. B-tree pages split when they fill, so they typically sit partly empty, and compression works page by page, so a 16 KB page that compresses to 5 KB may still occupy an 8 KB slot on disk.

Patrick O'Neil and colleagues proposed the **log-structured merge tree** in 1996 to fix the write side: turn random writes into sequential ones by batching them in memory and merging sorted runs on disk. Google's Bigtable paper (2006) brought the design into large-scale production, LevelDB (2011, by Jeff Dean and Sanjay Ghemawat) made a compact open-source version, and Facebook forked LevelDB in 2012 to create **RocksDB**, which is now the most widely embedded LSM engine.

## 2. The write path: WAL and memtable

<!--diagram:lsm-levels-1029-->

A write in RocksDB (a `Put`, `Delete` or `Merge`) does two things:

1. **Append to the write-ahead log (WAL).** The record goes to the end of a log file. This is purely for durability: if the process crashes, the WAL is replayed on restart to rebuild what was in memory. Appends are sequential and can be batched across many concurrent writers ("group commit"). Whether each write waits for `fsync` is configurable, which is the usual durability versus latency trade.
2. **Insert into the memtable.** The memtable is an in-memory sorted structure, a skip list by default, holding the most recent writes. Sorted order matters because it lets the memtable be written out later as a sorted file without a separate sort.

That's it. The write is acknowledged without reading anything from disk, and without touching any existing data. This is why LSM write throughput is so high, and why writes have predictable latency as long as the background work keeps up (that "as long as" is section 7).

When the memtable reaches its size limit (`write_buffer_size`, 64 MB by default in RocksDB), it becomes **immutable**, a new empty memtable takes over new writes, and a background thread **flushes** the immutable one to disk as a sorted file. Once flushed, the corresponding WAL segment can be deleted.

### Deletes are writes too

You can't delete from an immutable file. A delete writes a **tombstone**: a marker saying "this key is deleted as of this sequence number." Reads that find a tombstone treat the key as absent. The old value and the tombstone both stay on disk until compaction merges them and can prove no older version remains below, at which point both are dropped. Tombstones cause one of the classic LSM pathologies, covered in section 7.

Every write gets a monotonically increasing **sequence number**, and each stored entry is really `(user key, sequence number, type)`. That's how the engine knows which version is newest, and it gives you cheap snapshots: a snapshot is just a sequence number, and reads at that snapshot ignore anything newer.

## 3. SSTables: the files on disk

A flushed memtable becomes a **Sorted String Table** (SST), an immutable file of key-value pairs sorted by key. The term comes from Bigtable. A RocksDB SST in the default block-based format contains:

- **Data blocks**, about 4 KB each by default (`block_size`), holding sorted entries. Keys within a block are prefix-compressed: each key stores only the bytes that differ from the previous key, with periodic "restart points" so a lookup can binary-search within the block.
- **Index block**, with one entry per data block giving the last key in that block and its offset. To find a key, binary-search the index, then read one data block.
- **Filter block**, holding a **Bloom filter** (or a newer Ribbon filter) over the file's keys.
- **Properties and metadata**, such as key ranges, entry counts and compression type.

Because files are immutable and sorted, they compress well. Compression is per block, with LZ4 or Snappy for speed and Zstandard for the higher levels where most data lives. Immutability also makes many things easier: backups and checkpoints can hard-link files, caches never need invalidation, and replication or snapshot shipping can copy whole files (CockroachDB and TiKV send Raft snapshots this way, as the Oct 27 Raft reading mentioned).

## 4. The read path and Bloom filters

A point lookup has to find the newest version of a key, which could be anywhere: in the active memtable, an immutable memtable, or any SST. RocksDB checks them in order from newest to oldest and stops at the first hit:

1. The active memtable.
2. Any immutable memtables waiting to flush.
3. Each file in **L0**, newest first. L0 files are raw memtable flushes, so their key ranges overlap and every one must be checked.
4. Then one file per level in L1, L2 and so on. Below L0 each level is a single sorted run with non-overlapping files, so a binary search over the file key ranges picks the only file that could contain the key.

Without help, a lookup for a key that doesn't exist would read a block from every level. **Bloom filters** prevent that. A Bloom filter is a compact bit array that can answer "definitely not here" or "maybe here." With about 10 bits per key, the false-positive rate is about 1%, so a lookup skips 99% of the files that don't hold the key with no disk read at all. Filters and index blocks are usually pinned in the block cache, so a point read typically costs one data block read from storage, or zero if that block is cached.

**Range scans** are the harder case. A scan like "all keys from `user:100` to `user:200`" can't use Bloom filters (they only answer exact-key questions), so it has to open an iterator on every memtable, every L0 file and one file per level, and merge them with a heap. The cost grows with the number of sorted runs. RocksDB offers **prefix Bloom filters** for scans within a known prefix, and research systems have proposed range filters, but in general range-heavy read workloads are where B-trees keep an edge.

## 5. Compaction: where the real work happens

If flushes were the only thing happening, L0 would grow forever and reads would check thousands of files. **Compaction** merges files to keep the number of sorted runs small, throws away overwritten values and resolved tombstones, and moves data toward the bottom of the tree. How you compact defines the LSM's trade-offs.

### Leveled compaction (RocksDB's default)

Data is organized into levels L0, L1, ... Ln. Each level from L1 down has a target size, and each is **10 times** larger than the one above (`max_bytes_for_level_multiplier`, default 10). L1's target is set by `max_bytes_for_level_base` (256 MB by default in recent RocksDB versions).

- When L0 accumulates enough files (`level0_file_num_compaction_trigger`, default 4), they're merged together with the overlapping files in L1.
- When any level exceeds its target size, RocksDB picks a file in that level and merges it with the files it overlaps in the next level down, writing new files into the lower level.

Because each level is one sorted run, a read touches at most one file per level, and space overhead is small: most data sits in the last level, and at most around 10% of data can be stale copies sitting in upper levels. The cost is **write amplification**. Every byte gets rewritten each time it moves down a level, and merging a file into a level ten times bigger can rewrite roughly ten times its size in overlapping data. With 5 to 7 levels, leveled compaction commonly writes each byte 10 to 30 times over its lifetime, and RocksDB's own tuning guide describes write amplification in this range.

RocksDB's `level_compaction_dynamic_level_bytes` option (on by default since RocksDB 8.4) sizes levels from the bottom up, setting the last level from the actual data size and dividing by 10 upward. That keeps the 10x ratio true even when the database is small and avoids wasting space when levels are mostly empty.

### Tiered (universal) compaction

Instead of keeping one sorted run per level, tiered compaction lets several similar-sized runs accumulate and merges them all at once into a bigger run. RocksDB calls its version **universal compaction**; Cassandra's default for years was the similar **size-tiered** strategy.

Each byte is rewritten far fewer times, since it's merged only when its tier fills up, so write amplification is much lower. But there are more sorted runs to check on reads, and space amplification can be large: during a full merge of the biggest runs, the old and new copies exist side by side, so you may temporarily need up to twice the data size free on disk.

### FIFO and others

**FIFO compaction** just deletes the oldest files once a size or age limit is hit. It's for time-series and cache-like data where old data simply expires. Cassandra's **time-window compaction** groups files by write time for the same reason. Many systems mix strategies, for example tiering in upper levels and leveling at the bottom, which is where much of the research in section 9 points.

## 6. The amplification triangle

Every LSM design choice moves you around three costs:

- **Write amplification**: bytes written to storage per byte written by the application. Costs SSD endurance and background I/O and CPU.
- **Read amplification**: storage reads (or files and blocks checked) per lookup. Costs latency and IOPS.
- **Space amplification**: storage used divided by live data size. Costs money.

Manos Athanassoulis and colleagues formalized this in 2016 as the **RUM conjecture** (Read, Update, Memory): an access method can optimize for at most two of the three at once, and improving one costs the others. A B-tree has low read amplification but high write amplification and wasted page space. Leveled LSM has low space and decent read amplification but high write amplification. Tiered LSM has low write amplification but higher read and space amplification.

| Design | Write amp | Read amp (point) | Space amp |
|---|---|---|---|
| B+tree (InnoDB-like) | High for small random updates | Low (about 1 cached path plus 1 page) | Medium (partly empty pages) |
| Leveled LSM | Medium-high (roughly 10 to 30) | Low with Bloom filters | Low (about 1.1x) |
| Tiered LSM | Low | Higher (more runs) | High during merges (up to about 2x) |

Mark Callaghan, who worked on MySQL and RocksDB at Google and Facebook, has written extensively on his "Small Datum" blog about measuring these three factors. One of his recurring points is that you can't compare engines on a single benchmark number: you need to know which of the three costs the workload actually pays for.

## 7. When LSM trees go wrong

LSM trees are fast as long as compaction keeps up. Most production incidents involve it falling behind.

**Write stalls.** If writes arrive faster than flush and compaction can process them, L0 fills with files and pending compaction bytes pile up. Reads get slower (every L0 file must be checked), so RocksDB protects itself by throttling writes. When L0 reaches `level0_slowdown_writes_trigger` files (default 20) it slows writes, and at `level0_stop_writes_trigger` (default 36) it stops them entirely until compaction catches up. Similar limits apply to the number of unflushed memtables and to estimated pending compaction bytes. To an application, this looks like sudden latency spikes or timeouts under heavy write load. The fixes are more background threads (`max_background_jobs`), larger or more memtables, faster disks, or reducing write amplification.

**Tombstone pileups.** Deleting many keys writes many tombstones, and they can't be dropped until compaction reaches the bottom level. Meanwhile range scans must step over every tombstone. A queue implemented as "insert at the tail, delete from the head" is the classic victim: each dequeue scans past an ever-growing run of tombstones at the head. Cassandra's warning about scanning too many tombstones exists for this reason, and the Discord reading on Oct 28 described how tombstones and compaction contributed to Discord's Cassandra latency problems. RocksDB mitigations include **range deletions** (one tombstone covering a whole key range), `SingleDelete` for keys written once, and compaction triggers that prioritize files with many deletions.

**Space amplification surprises.** With tiered compaction, a major merge can need free space equal to the data it's merging. Teams run out of disk not because data grew, but because compaction needed room to work.

**Compaction interfering with foreground work.** Compaction competes with queries for disk bandwidth and CPU. RocksDB's rate limiter caps background write bandwidth, and newer versions can adjust that cap automatically, but there's a real trade-off: limit compaction too much and you hit write stalls instead.

**Large values.** If values are large (kilobytes or more), rewriting them during every compaction is pure waste, since the keys need to stay sorted but the values don't. That's the problem WiscKey addresses (section 9).

## 8. RocksDB in production

**MyRocks.** Facebook's biggest RocksDB deployment is MyRocks, a MySQL storage engine built on RocksDB, which replaced InnoDB for the user database (UDB) that stores Facebook's social graph. Facebook's 2016 announcement and its 2017 migration post describe the motivation: compressed InnoDB used far more space on flash than the same data in MyRocks, which needed roughly half the storage, while also writing much less to flash. The VLDB 2020 paper by Yoshinori Matsunobu, Siying Dong and Herman Lee ("MyRocks: LSM-Tree Database Storage Engine Serving Facebook's Social Graph") documents the migration, the engineering needed to make an LSM perform for a general-purpose SQL workload, and the trade-offs that remained, such as range scans and CPU spent on compaction.

**Embedded everywhere.** RocksDB is a library, not a server, so it shows up inside many distributed systems as the local storage on each node: TiKV (TiDB), YugabyteDB's DocDB (a heavily modified fork), Kafka Streams state stores, Flink's RocksDB state backend, and many others. CockroachDB used RocksDB until 2020, then replaced it with **Pebble**, a Go LSM engine compatible with RocksDB's file format, citing the cost of crossing between Go and C++ and the desire to control the engine's code.

**Column families.** One RocksDB instance can hold several **column families**, each with its own memtables, SSTs and compaction settings, but sharing one WAL so a write across families is atomic. Systems use this to keep, say, a data family on leveled compaction and a short-lived metadata family on FIFO.

**Merge operator.** For read-modify-write patterns like counters, RocksDB's **merge operator** lets you write an operand ("add 1") instead of reading the value first. Operands stack up and are combined lazily, during reads or compaction. It turns a read-then-write into a blind write, which an LSM does cheaply.

## 9. Ideas that changed the trade-offs

LSM research has been very active in the last decade. A few ideas have made it into production engines.

**Key-value separation (WiscKey).** Lu and colleagues' WiscKey paper (FAST 2016) keeps keys and small pointers in the LSM tree, and stores values in a separate append-only **value log**. Compaction now moves only keys and pointers, which slashes write amplification for large values. The cost is that range scans become random reads into the value log, and the log needs its own garbage collection. RocksDB's **BlobDB** (integrated BlobDB, rewritten around 2021) implements this idea, with values above a size threshold going into blob files. TiKV's Titan and Badger (the Go engine behind Dgraph) use the same approach.

**Smarter Bloom filters (Monkey).** Giving every level the same bits per key is not optimal. Dayan, Athanassoulis and Idreos's Monkey paper (SIGMOD 2017) shows that a lookup's cost is dominated by the false positives at the larger, deeper levels, so allocating filter memory unevenly across levels reduces lookup cost for the same memory. Their later Dostoevsky paper (SIGMOD 2018) proposed "lazy leveling," tiering at upper levels and leveling only at the last, which gets most of tiering's write savings while keeping leveling's space and point-read benefits for the bulk of the data.

**Ribbon filters.** RocksDB added the Ribbon filter in 2021 (based on work by Peter Dillinger and Stefan Walzer), which uses about 30% less memory than a Bloom filter for the same false-positive rate, at somewhat higher CPU cost to build. RocksDB lets you use Ribbon for lower levels, where most keys live and memory matters most, and Bloom at the top, where filters are built frequently.

**Tiered storage and disaggregation.** Because SSTs are immutable, they're natural to put on cheaper or remote storage. RocksDB supports placing the last levels on a different, cheaper device. Cloud-native databases go further, keeping SSTs in object storage with local caches, the same direction you saw in StarRocks' shared-data mode on Oct 23.

## 10. Choosing and tuning

Some rules of thumb for deciding whether an LSM fits and how to configure it:

- **Write-heavy, point-read workloads** (event ingestion, messaging metadata, time series, counters, the storage layer under a distributed KV store) are the LSM's home ground.
- **Read-heavy, range-scan-heavy workloads with few updates** often do as well or better on a B-tree. Many OLTP systems still choose B-trees for predictability.
- **Watch the three amplifications.** Measure bytes written to disk versus bytes written by the app (write amp), disk used versus live data (space amp), and block cache hit rates and read IOPS (read amp). RocksDB exposes all of these in its statistics.
- **Size the block cache and pin filters and indexes** so that point reads cost at most one storage read.
- **Pick compaction by workload.** Leveled for read and space efficiency, universal for write-heavy workloads that can spare disk, FIFO for expiring data.
- **Leave headroom.** Compaction needs disk bandwidth and free space. A disk at 85% full or an I/O budget at 90% can turn into write stalls under a traffic spike.
- **Design keys for the engine.** Keys that share prefixes compress well and enable prefix filters. Avoid queue-like delete patterns, or use range deletions.

## Key takeaways

- An LSM tree turns random writes into sequential ones: WAL plus memtable in front, immutable sorted files behind.
- Deletes and updates are just new writes; old versions and tombstones disappear only when compaction merges them away.
- Reads check newest to oldest. Bloom filters (about 1% false positives at 10 bits per key) make point lookups cheap; range scans pay for every sorted run.
- Compaction strategy is the main tuning knob. Leveled favors reads and space, tiered favors writes.
- Read, write and space amplification trade off against each other (the RUM conjecture). Know which one your workload pays for.
- Most LSM incidents are compaction falling behind: write stalls, tombstone pileups and surprise disk usage.
- Key-value separation, better filters and hybrid compaction policies have shifted the trade-offs, and are now in production engines.

## Discussion questions

1. Leveled compaction with a multiplier of 10 rewrites each byte many times. Would a multiplier of 4 or 20 be better for a write-heavy workload, and what would you give up?
2. A team implements a job queue on RocksDB with keys `job:<timestamp>` and deletes jobs after processing. Latency climbs over hours. What's happening, and how would you redesign it?
3. Why does key-value separation help writes but hurt range scans? For which value sizes would you turn on BlobDB?
4. CockroachDB moved from RocksDB to Pebble. What are the risks and benefits of owning your own storage engine?
5. Facebook found that MyRocks used much less space than compressed InnoDB. Using the B-tree weaknesses in section 1, explain where those savings come from.
6. If you ran RocksDB on object storage with a local SSD cache, which levels would you keep local, and how would compaction change?
7. Where in your current systems is an LSM tree hiding underneath, and do you know which compaction strategy it uses?

## Sources

- Patrick O'Neil, Edward Cheng, Dieter Gawlick and Elizabeth O'Neil, "The Log-Structured Merge-Tree (LSM-Tree)," Acta Informatica, 1996.
- Fay Chang et al., "Bigtable: A Distributed Storage System for Structured Data," OSDI 2006.
- RocksDB wiki and docs: "RocksDB Overview," "Leveled Compaction," "Universal Compaction," "Write Stalls," "RocksDB Tuning Guide," "BlobDB," https://github.com/facebook/rocksdb/wiki and https://rocksdb.org
- Yoshinori Matsunobu, Siying Dong and Herman Lee, "MyRocks: LSM-Tree Database Storage Engine Serving Facebook's Social Graph," PVLDB 13(12), 2020, https://www.vldb.org/pvldb/vol13/p3217-matsunobu.pdf
- Facebook Engineering, "MyRocks: A space- and write-optimized MySQL database" (2016), https://engineering.fb.com/core-data/myrocks-a-space-and-write-optimized-mysql-database/ and "Migrating a database from InnoDB to MyRocks" (2017), https://engineering.fb.com/2017/09/25/core-data/migrating-a-database-from-innodb-to-myrocks/
- Manos Athanassoulis et al., "Designing Access Methods: The RUM Conjecture," EDBT 2016.
- Lanyue Lu et al., "WiscKey: Separating Keys from Values in SSD-conscious Storage," FAST 2016.
- Niv Dayan, Manos Athanassoulis and Stratos Idreos, "Monkey: Optimal Navigable Key-Value Store," SIGMOD 2017; Niv Dayan and Stratos Idreos, "Dostoevsky," SIGMOD 2018.
- Peter C. Dillinger and Stefan Walzer, "Ribbon filter: practically smaller than Bloom and Xor" (2021), and the RocksDB blog post on Ribbon filters.
- Cockroach Labs, "Introducing Pebble: A RocksDB-inspired key-value store written in Go" (2020).
- Mark Callaghan, Small Datum blog, posts on read, write and space amplification, https://smalldatum.blogspot.com
- Martin Kleppmann, *Designing Data-Intensive Applications* (O'Reilly, 2017), chapter 3.

The MyRocks paper and Facebook posts were located on Oct 29, 2026. The RocksDB option names and defaults are from the RocksDB wiki and options headers as I know them; defaults change between releases, so check `include/rocksdb/options.h` for the version you run. The 10 bits per key and 1% figure is the standard Bloom filter result.
