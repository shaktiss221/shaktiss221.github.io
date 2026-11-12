---
title: "Write-ahead logging and crash recovery: how databases survive pulling the plug"
date: 2026-11-12
summary: How a database keeps committed data and discards half-finished work when the power goes out mid-write. Covers the write-ahead rule, steal/no-force buffer policies, LSNs, checkpoints, the ARIES analysis/redo/undo passes, torn pages and full-page writes, the InnoDB doublewrite buffer, group commit, durability knobs, and the 2018 fsyncgate incident.
tags: databases
daily: true
---

A database promises that once it says "committed", the data survives a crash, and that work which never committed leaves no trace. That sounds simple until you notice how storage actually behaves: pages are cached in memory for speed, the operating system adds another cache underneath, disks reorder writes, an 8 KB page can be half-written when power fails, and `fsync` can fail in ways that quietly lose data. The answer that almost every serious database converged on is the write-ahead log (WAL): an append-only record of every change, forced to disk before the change itself, and replayed after a crash. This reading explains why the log works, how the classic ARIES recovery algorithm uses it, how Postgres, InnoDB and SQLite implement the ideas, and where the guarantees leak. It draws on the ARIES paper (Mohan et al., 1992), Haerder and Reuter's 1983 recovery taxonomy, Alex Petrov's *Database Internals*, the Postgres and MySQL documentation, and the 2018 "fsyncgate" discussion.

## 1. The problem: memory is fast, disks are durable, and crashes happen in between

A database keeps its data in fixed-size pages (8 KB in Postgres, 16 KB by default in InnoDB). Reading and writing pages on disk for every change would be far too slow, so the database caches pages in a buffer pool in memory. A transaction that updates a row changes the cached page and marks it dirty. At some later time a background writer flushes the dirty page to disk.

That creates two failure cases when the machine crashes:

- **A committed change exists only in memory.** The client was told "committed", but the dirty page never reached disk. Without some other record, that change is lost. This breaks **durability**.
- **An uncommitted change is already on disk.** The buffer pool needed space and flushed a dirty page that contained a change from a transaction still in progress. After the crash, that half-done work is sitting in the data file. This breaks **atomicity**.

A recovery design has to handle both. The cleanest way to see the options is Haerder and Reuter's 1983 classification (the same paper that coined "ACID") along two axes:

- **Steal or no-steal:** may the buffer pool write a dirty page containing *uncommitted* changes to disk? "Steal" means yes, the pool can steal the frame from a running transaction.
- **Force or no-force:** must all pages a transaction changed be written to disk *at commit*? "Force" means yes.

No-steal plus force is the easy combination: nothing uncommitted ever reaches disk, and everything committed is already there at commit time. No recovery work is needed. It's also terrible: a large transaction must hold all its dirty pages in memory, and every commit waits for random writes scattered across the data files.

Steal plus no-force is the fast combination: the buffer pool can evict whatever it wants, and commit doesn't wait for data pages at all. It's also the hardest to recover from, because both failure cases above can happen. Nearly every production database chooses steal/no-force and pays for it with a log. To survive "committed but not on disk" it needs **redo** information. To survive "on disk but not committed" it needs **undo** information.

## 2. The write-ahead rule

The log is a sequential, append-only file of records describing changes. Each record gets a **log sequence number (LSN)**, a monotonically increasing identifier that in practice is often the byte offset in the log stream. A typical update record contains the transaction ID, the page ID, and enough information to redo the change and, if the system uses undo logging, to reverse it.

Two rules make the log sufficient for recovery:

1. **Write-ahead rule (for undo):** before a dirty page is written to the data file, every log record describing changes to that page must already be durable in the log. The buffer manager enforces this by storing the LSN of the latest change in the page header (the **pageLSN**) and refusing to flush a page until the log has been flushed at least up to that LSN.
2. **Commit rule (for redo):** before a transaction is reported committed, all its log records, including the commit record, must be durable in the log.

With those two rules, a crash at any moment leaves the log as the authority. If a committed change didn't make it into the data file, the log has the redo information. If an uncommitted change did make it into the data file, the log has the record that says so, and the undo information to reverse it.

Why is this faster than forcing data pages? Because the log is written sequentially, and a commit only has to flush the tail of one file. Data page writes are random and can be batched lazily, merged (a page updated a thousand times is written once), and done in the background. The log turns random synchronous writes into sequential synchronous writes plus random asynchronous ones.

## 3. What goes in a log record: physical, logical, physiological

There's a design choice in how a log record describes a change.

- **Physical logging** records bytes: "at page 812, offset 340, the old bytes were X, the new bytes are Y." It's simple and replay is idempotent in an obvious way, but records are large, and a change that shifts many bytes (inserting into the middle of a sorted page) generates a lot of log.
- **Logical logging** records operations: "insert row R into table T." Records are small, but replay is hard. The operation might touch several pages (the heap, three indexes), and if some of those pages were flushed and some weren't, re-running the operation can't easily be made consistent.
- **Physiological logging**, which ARIES and most real systems use, is a hybrid: physical to a page, logical within the page. "On page 812, insert this tuple into slot 7." Each record affects exactly one page, so recovery can reason page by page, but the record doesn't depend on exact byte offsets inside the page, which lets the page reorganise itself.

The "one record, one page" property matters a lot. It means the pageLSN on each page tells recovery exactly which log records that page already contains. If the pageLSN is at least as new as a record's LSN, that change is on the page; otherwise it isn't.

## 4. Checkpoints: bounding how far back recovery must read

Without checkpoints, recovery would have to replay the log from the beginning of time. A **checkpoint** records enough state that recovery can start from a recent point.

The naive version stops all transactions, flushes every dirty page, and writes a "checkpoint" record. Recovery then starts at that record. Stopping the world is unacceptable in production, so real systems use **fuzzy checkpoints**:

- Write a "begin checkpoint" record.
- Record the **dirty page table** (each dirty page and its **recLSN**, the LSN of the first change that dirtied it since it was last flushed) and the **active transaction table** (each running transaction and its latest LSN).
- Write an "end checkpoint" record containing those tables, and store the checkpoint's location somewhere well known (Postgres uses the `pg_control` file; ARIES calls it the master record).
- Meanwhile, keep flushing dirty pages in the background so that the oldest recLSN keeps moving forward.

Transactions keep running throughout. The checkpoint doesn't make the disk consistent; it just tells recovery where to start reading and gives it a head start on what state was in flight.

Checkpoints also decide when old log can be deleted or recycled. The log can't be discarded until every change it describes is safely in the data files, which means the minimum recLSN across all dirty pages (and anything replicas or backups still need) sets the truncation point.

In Postgres, `checkpoint_timeout` (default 5 minutes) and `max_wal_size` (default 1 GB) trigger checkpoints, whichever comes first, and `checkpoint_completion_target` spreads the flush I/O over most of the interval to avoid a burst. The trade-off is direct: more frequent checkpoints mean less WAL to replay after a crash (faster recovery), but more data page writes and, as section 7 shows, more full-page images in the WAL.

## 5. ARIES: analysis, redo, undo

ARIES ("Algorithms for Recovery and Isolation Exploiting Semantics", Mohan, Haderle, Lindsay, Pirahesh and Schwarz, ACM TODS 1992) came out of IBM's work on DB2 and is the template for recovery in DB2, SQL Server and many others. Its core idea is easy to state: **repeat history, then undo the losers.** Recovery runs in three passes.

<!--diagram:aries-recovery-1112-->

### Pass 1: analysis

Start at the last checkpoint and scan forward to the end of the log. Begin with the dirty page table and active transaction table from the checkpoint and update them as records go by: a record touching a page not in the table adds it with that record's LSN as recLSN; a commit or abort-complete record removes the transaction from the active table. At the end:

- The active transaction table lists the **losers**: transactions that were running at the crash and never committed.
- The dirty page table lists pages that *might* be missing changes, and the smallest recLSN among them is where redo must start.

### Pass 2: redo ("repeat history")

Scan forward from the smallest recLSN. For each update record, reapply it unless there's proof the page already has it:

- If the page isn't in the dirty page table, or the record's LSN is older than that page's recLSN, skip it: the page was flushed after this change.
- Otherwise read the page and compare its pageLSN. If pageLSN ≥ record LSN, the change is already there; skip it. If not, apply the change and set pageLSN to the record's LSN.

The surprising part is that redo reapplies changes from **loser transactions too**. ARIES rebuilds the exact state of the database at the moment of the crash, including uncommitted work, and only then rolls the losers back. That sounds wasteful, but it's what makes the design simple and robust: after redo, the database is in a state that actually existed, and the undo logic is exactly the same code that runs for a normal transaction abort. It's also what lets ARIES support fine-grained (row-level) locking. Two transactions can modify the same page, one commits and one doesn't; repeating history and then undoing the loser at the record level works, whereas trying to selectively redo only winners would leave pages inconsistent.

### Pass 3: undo

Roll back every loser by walking its log records backwards (each record carries a prevLSN pointer to the same transaction's previous record). ARIES processes all losers together, always undoing the record with the largest LSN next, so the log is read backwards once.

Every undo action is itself logged as a **compensation log record (CLR)**. A CLR describes the reversal and carries an **undoNextLSN** that points to the next record to undo for that transaction. CLRs are redo-only: they are never themselves undone. This solves the problem of crashing *during* recovery. If the system crashes halfway through undo, the next recovery redoes the CLRs (repeating history again) and then picks up undo where the undoNextLSN says, so no change is ever undone twice and recovery work is bounded even across repeated crashes.

The same machinery gives ARIES **partial rollbacks**: savepoints are just LSNs, and rolling back to a savepoint is undo with CLRs back to that LSN.

### What the passes guarantee

After the three passes, every committed transaction's effects are present (redo), no loser's effects remain (undo), and the log describes exactly what happened, including the recovery itself. The database can start accepting connections. Many systems actually open for business after redo and run undo in the background, locking the loser's rows until they're rolled back, because redo is usually the long pass.

## 6. Postgres: redo-only recovery

Postgres uses a WAL with the write-ahead and commit rules from section 2, but its recovery is simpler than ARIES because of how it handles concurrency. Postgres uses MVCC with no in-place updates: an UPDATE writes a new tuple version and marks the old one with the transaction's ID as its deleter. Whether a tuple version is visible is decided at read time by checking the commit status of the transactions that created and deleted it in the commit log (`pg_xact`).

That design means Postgres doesn't need an undo pass. After a crash, it replays WAL from the last checkpoint's redo point (redo only). A transaction that never wrote a commit record is treated as aborted, so its tuple versions are simply invisible to everyone, and VACUUM eventually removes them. The cost is moved elsewhere: dead tuples have to be cleaned up, and table bloat and transaction ID wraparound become operational concerns instead of a recovery-time concern.

Some details worth knowing:

- WAL is stored in **segment files**, 16 MB each by default, under `pg_wal`. Old segments are recycled or removed after checkpoints, unless something still needs them (a replication slot, `wal_keep_size`, or archiving that hasn't finished).
- The same WAL stream is the basis for **physical replication** (standbys replay it continuously) and **point-in-time recovery** (restore a base backup and replay archived WAL up to a target time). Crash recovery, a hot standby and PITR are the same replay loop pointed at different sources.
- **Logical decoding** reads WAL and turns it into row-level change events, which is how logical replication and CDC tools like Debezium work. The log written for crash recovery ends up as the source of truth for downstream systems.

## 7. Torn pages and full-page writes

There's a gap in the story so far. Redo assumes a page on disk is either the old version or the new version, so that pageLSN can be trusted. But a database page is bigger than the unit the storage stack writes atomically. A Postgres 8 KB page is two 4 KB OS pages, which may be eight 512-byte sectors on disk. If power fails in the middle of writing it, the page on disk can be part old and part new: a **torn page**. Its header might say LSN 900 while half its contents are from LSN 600. A physiological redo record ("insert into slot 7") can't be applied safely to a page that is internally inconsistent.

Databases take different approaches to fixing this.

**Postgres: full-page writes.** With `full_page_writes = on` (the default), the first time a page is modified after a checkpoint, Postgres writes the *entire page image* into the WAL, not just the change. During recovery, redo starts from that full image and so never depends on whatever torn state is in the data file. Later changes to the same page before the next checkpoint log only the delta.

The cost is WAL volume. Right after a checkpoint, almost every modification logs a full 8 KB page, so WAL generation spikes and then decays until the next checkpoint. This is a big reason why tuning checkpoint intervals longer can noticeably reduce WAL volume, and why `wal_compression` exists. Turning `full_page_writes` off is only safe if the storage really guarantees atomic 8 KB writes (some filesystems such as ZFS with copy-on-write can).

**InnoDB: the doublewrite buffer.** InnoDB solves the same problem on the data file side. Before writing dirty pages to their real locations, InnoDB writes them to a separate **doublewrite** area and flushes it, then writes them to their final places. On recovery, if a page in the tablespace is torn (detected by checksum), InnoDB restores the intact copy from the doublewrite area and then applies redo. Since MySQL 8.0.20, the doublewrite area lives in its own files rather than in the system tablespace, and it can be disabled when the storage guarantees atomic writes. Pages are written twice, but the doublewrite writes are large and sequential, so the overhead is usually much less than 2×.

**SQLite: rollback journal or WAL.** SQLite's original approach is a **rollback journal**: before modifying a page in the database file, copy the original page to a journal and fsync it, then modify in place; on crash, copy the originals back. That's undo logging with physical page images. **WAL mode** inverts it: changes are appended to a `-wal` file, readers check the WAL for newer page versions before reading the main file, and a checkpoint copies pages back (automatically, by default once the WAL reaches 1000 pages). WAL mode allows readers and a writer to proceed concurrently, which the rollback journal does not.

## 8. Group commit and the cost of fsync

The commit rule says the commit record must be durable before reporting success. On most systems "durable" means calling `fsync` (or `fdatasync`, or opening with `O_DSYNC`) and waiting for the device to acknowledge. On a consumer SSD or a cloud volume, that's commonly hundreds of microseconds to a few milliseconds; on spinning disks it was milliseconds. If every transaction issued its own fsync serially, throughput would be capped at roughly one commit per fsync latency.

**Group commit** fixes this. When one transaction is waiting for the log flush, others that commit in the meantime add their records to the log buffer and wait on the *same* flush. One fsync then makes dozens or hundreds of commits durable at once. Throughput rises with concurrency while individual commit latency stays roughly one flush. Postgres does this naturally (a backend that flushes the log flushes everything up to its position, covering others) and offers `commit_delay` to deliberately wait a few microseconds for more company. InnoDB and MySQL's binary log coordinate group commit across both logs.

Group commit is also why benchmark numbers with one client look so different from numbers with 64 clients. A single-threaded loop of small transactions measures fsync latency; a concurrent workload measures how well commits are batched.

## 9. Durability knobs: trading the last few milliseconds of data

Because the log flush is the price of durability, databases let you pay less of it, and it's important to know exactly what you give up.

**Postgres `synchronous_commit`:**

- `on` (default): wait for the local WAL flush (and, with synchronous replicas configured, for the replica as well).
- `off`: report commit before the WAL is flushed. The WAL writer flushes in the background (within roughly three times `wal_writer_delay`, which defaults to 200 ms). A crash can lose the last fraction of a second of *committed* transactions, but it **cannot corrupt** the database: the write-ahead rule for data pages still holds, so the database recovers to a consistent earlier point. It can be set per transaction, which makes it a good option for low-value writes such as analytics events.
- `remote_write`, `remote_apply`: variants for synchronous replication, deciding whether the standby must have written, flushed or applied the WAL before the primary acknowledges.

**`fsync = off`** is a different thing entirely and much more dangerous. It stops Postgres from flushing at all, including for data pages and checkpoints, so after an OS crash or power loss the database can be **corrupt**, not just behind. It's acceptable only for throwaway data, such as loading a database you could rebuild from scratch.

**InnoDB `innodb_flush_log_at_trx_commit`:**

- `1` (default): write and flush the redo log at every commit. Full durability.
- `2`: write to the OS at every commit, flush about once per second. A *mysqld* crash loses nothing (the OS has the data); an OS crash or power loss can lose about a second.
- `0`: write and flush about once per second. Either kind of crash can lose about a second.

The general lesson: "asynchronous commit" options trade a bounded window of recent committed data for latency, while turning off flushing trades away consistency. Know which one a setting does before you reach for it.

## 10. When fsync lies: the 2018 fsyncgate incident

All of the above assumes that when `fsync` returns success, the data is on stable storage, and when it returns an error, the application can retry. In March 2018 Craig Ringer reported on the pgsql-hackers mailing list that the second assumption was false on Linux, and that Postgres's handling of fsync errors could lose data.

The mechanism, as described in that thread and on the Postgres wiki's "Fsync Errors" page:

- Postgres writes data pages into the OS page cache with ordinary `write` calls and later calls `fsync` during a checkpoint to make them durable.
- If writeback of a page failed (say, a transient I/O error on a thin-provisioned or network volume), Linux reported the error from `fsync` but could then **mark the failed pages clean** in the page cache. The data in memory no longer matched disk, and the kernel no longer considered it dirty.
- Postgres's response to a failed checkpoint fsync was to report an error and retry the checkpoint later. The retry's `fsync` would **succeed**, because there were no dirty pages left to write, and Postgres would treat the data as durable and remove the WAL it no longer needed. The changes that failed to write were gone, with no error.
- Error reporting was also tied to file descriptors in ways that made it possible to miss errors entirely if the file was closed and reopened, or if another process consumed the error first. Kernel 4.13 improved this with a per-file error sequence counter, but did not change the "pages marked clean" behaviour.

The fix in Postgres, committed by Thomas Munro in late 2018 and back-patched to supported releases, was to stop trusting a retry: if `fsync` on a data file fails, Postgres **PANICs** and restarts, which forces crash recovery from the WAL, the one copy of the changes that's known to be durable. A new setting, `data_sync_retry` (default `off`), keeps that behaviour; turning it on restores retrying for platforms where retry is known to be safe.

Research published afterwards showed Postgres wasn't alone. Rebello and colleagues' USENIX ATC 2020 paper "Can Applications Recover from fsync Failures?" injected fsync failures under several filesystems and found that a range of widely used storage applications, including Redis, SQLite, LMDB, LevelDB and PostgreSQL (before the fix), could lose or corrupt data or behave inconsistently, because the filesystems' behaviour after a failed fsync differs and isn't what applications assume. Earlier, Pillai and colleagues' OSDI 2014 paper "All File Systems Are Not Created Equal" showed how many applications make unsafe assumptions about the ordering and atomicity of file operations around crashes, such as assuming a rename is persisted, or that appends are atomic, without the right fsync calls on the file and its directory.

The practical lessons:

- A failed fsync is not an ordinary retryable error. The safest response is usually to crash and recover from a log you know is durable.
- Durability depends on every layer: the database's flushes, the filesystem's semantics, the volume (and its write cache), and the device's honesty about flush. A drive or controller with a volatile write cache that acknowledges flushes it hasn't persisted defeats all the logic in this reading. Enterprise SSDs with power-loss protection, or caches configured for write-through, close that gap.
- Test with tools that actually simulate crashes and I/O errors, rather than `kill -9`, which only tests process crashes while the OS page cache stays intact.

## 11. The log as the database

Once you see that the log is the authority and the data files are a cache of the log's effects, a family of designs falls out.

- **Replication by log shipping.** Postgres streaming replication and MySQL's binlog-based replication send the log to replicas, which replay it. Crash recovery and replication are the same code.
- **Amazon Aurora.** Aurora's SIGMOD 2017 paper (Verbitski et al.) describes pushing this idea into the storage layer: the database instance sends only redo log records to a distributed storage service, which keeps six copies across three availability zones and acknowledges a write when four of the six have it. Storage nodes materialise pages from the log in the background. The instance never writes data pages over the network, and crash recovery is mostly done by storage continuously applying the log, so a restarted instance doesn't need a long redo pass. The paper sums it up as "the log is the database."
- **LSM trees.** RocksDB and similar engines have a WAL in front of an in-memory memtable; the memtable is flushed to immutable sorted files, and the WAL segment is dropped once its contents are in a flushed file. It's the same write-ahead rule with a different on-disk structure.
- **Event sourcing and Kafka.** At the application level, keeping an append-only log of events as the source of truth and deriving state from it is the same idea, with the same questions about snapshots (checkpoints), compaction (log truncation) and replay.

## 12. Operating a WAL-based database: what to watch

A few signals turn this theory into day-to-day practice:

- **Recovery time.** After a crash, the database must replay WAL from the last checkpoint's redo point. With `max_wal_size` at 1 GB and a fast disk, that's usually seconds to a minute or two; with very large `max_wal_size` and long checkpoint intervals (tuned to reduce write load), recovery can take much longer. That's a direct RTO trade-off.
- **WAL volume.** WAL generation rate affects disk, replication bandwidth, archive storage and backup size. Spikes right after checkpoints point to full-page writes. Bulk operations such as index builds, `UPDATE` of a whole table, or `VACUUM FULL` can generate gigabytes.
- **WAL retention.** An inactive replication slot or a failing archive command prevents WAL removal, and `pg_wal` grows until the disk fills and the database stops. This is one of the most common Postgres outages. Postgres 13 added `max_slot_wal_keep_size` to cap how much WAL a slot may hold.
- **Commit latency.** If p99 commit latency rises with no change in query plans, look at fsync latency on the WAL device. Putting WAL on a separate, low-latency device (or a volume with provisioned IOPS) is a classic optimisation.
- **Checkpoint behaviour.** Postgres logs whether checkpoints were triggered by time or by WAL size (`log_checkpoints`, on by default since Postgres 15). Frequent size-triggered checkpoints mean `max_wal_size` is too small for the write rate.

## Key takeaways

- Fast databases use a **steal/no-force** buffer policy and pay for it with a **write-ahead log** that carries redo (and often undo) information.
- The **write-ahead rule** (log before data page) protects atomicity; the **commit rule** (log flushed before acknowledging) protects durability. pageLSN is how the buffer manager enforces the first and how recovery knows what each page contains.
- **Fuzzy checkpoints** bound how much log recovery has to read without stopping the world, and set when old log can be discarded.
- **ARIES** recovers in three passes: analysis finds losers and dirty pages, redo **repeats history** including losers, and undo rolls losers back while writing **CLRs** so a crash during recovery is safe.
- Postgres needs only **redo** because MVCC makes uncommitted versions invisible; the cleanup cost moves to VACUUM.
- **Torn pages** break redo's assumptions. Postgres logs **full-page images** after each checkpoint; InnoDB uses a **doublewrite buffer**; both are unnecessary only if storage writes pages atomically.
- **Group commit** amortises fsync across concurrent transactions. Asynchronous commit loses a bounded window of committed data; turning off fsync risks corruption.
- **fsync failures** can't be safely retried on Linux; since 2018 Postgres PANICs and recovers from WAL instead. Durability is only as good as the weakest layer down to the disk's write cache.

## Discussion questions

1. Why does ARIES redo the changes of transactions it's about to undo? What would go wrong with row-level locking if it only redid winners?
2. Postgres avoids an undo pass but needs VACUUM. Where else could the cost of MVCC cleanup land, and what does InnoDB's undo-log-based MVCC do differently?
3. Your Postgres primary generates WAL at 50 MB/s right after each checkpoint and 5 MB/s just before the next one. What's going on, and which two settings would you consider changing? What do you give up?
4. A team wants `synchronous_commit = off` for an event-ingestion table. What exactly can they lose, and in what kinds of failure? Would `fsync = off` be acceptable instead?
5. You run a database on a cloud block volume. List every layer between `fsync` returning and the bits being on non-volatile media. Which ones could acknowledge too early?
6. In your own systems, where else does "append to a log first, apply later" appear (queues, outbox, event sourcing)? Which of them have an equivalent of checkpoints and truncation, and who owns them?

## Sources

- C. Mohan, D. Haderle, B. Lindsay, H. Pirahesh, P. Schwarz, "ARIES: A Transaction Recovery Method Supporting Fine-Granularity Locking and Partial Rollbacks Using Write-Ahead Logging," ACM TODS 17(1), 1992 ([PDF via Berkeley](https://dsf.berkeley.edu/cs262/Aries.pdf)); Adrian Colyer's summary on [the morning paper](https://blog.acolyer.org/2016/01/08/aries/); [Wikipedia overview](https://en.wikipedia.org/wiki/Algorithms_for_Recovery_and_Isolation_Exploiting_Semantics)
- T. Haerder, A. Reuter, "Principles of Transaction-Oriented Database Recovery," ACM Computing Surveys 15(4), 1983
- Alex Petrov, *Database Internals* (O'Reilly, 2019), ch. 5 "Transaction Processing and Recovery"
- Martin Kleppmann, *Designing Data-Intensive Applications* (O'Reilly, 2017), ch. 3 and ch. 7
- PostgreSQL documentation: "Reliability and the Write-Ahead Log" (WAL configuration, asynchronous commit, `full_page_writes`, checkpoints)
- PostgreSQL wiki, [Fsync Errors](https://wiki.postgresql.org/wiki/Fsync_Errors); the pgsql-hackers thread ["PostgreSQL's handling of fsync() errors is unsafe and risks data loss at least on XFS"](https://postgrespro.com/list/thread-id/2379543) (2018); [Dan Luu's archive of the thread](https://danluu.com/fsyncgate/); the [PANIC on fsync failure](https://commitfest.postgresql.org/patch/1639) commitfest entry; Christophe Pettus, ["All your GUCs in a row: data_sync_retry"](https://thebuild.com/blog/all-your-gucs-in-a-row-datasyncretry/)
- A. Rebello et al., "Can Applications Recover from fsync Failures?", USENIX ATC 2020
- T. S. Pillai et al., "All File Systems Are Not Created Equal: On the Complexity of Crafting Crash-Consistent Applications," OSDI 2014
- MySQL 8.0 Reference Manual, [Doublewrite Buffer](https://dev.mysql.com/doc/refman/8.0/en/innodb-doublewrite-buffer.html) and `innodb_flush_log_at_trx_commit`; MySQL worklog [WL#5655](https://dev.mysql.com/worklog/task/?id=5655); Percona, ["Surprise with innodb_doublewrite_pages in MySQL 8.0.20+"](https://www.percona.com/blog/surprise-with-innodb_doublewrite_pages-in-mysql-8-0-20/)
- SQLite documentation, "Write-Ahead Logging" and "Atomic Commit in SQLite"
- A. Verbitski et al., "Amazon Aurora: Design Considerations for High Throughput Cloud-Native Relational Databases," SIGMOD 2017

*Note on verification: the fsyncgate discussion, the Postgres PANIC fix and `data_sync_retry`, the ARIES paper and the MySQL 8.0.20 doublewrite change were checked against the linked pages for this reading. Default values (16 MB WAL segments, 5 min `checkpoint_timeout`, 1 GB `max_wal_size`, 200 ms `wal_writer_delay`, SQLite's 1000-page autocheckpoint, InnoDB flush modes), the Aurora quorum numbers and the paper findings come from the documentation and papers as I know them and weren't re-fetched today; check current docs for your version before tuning.*
