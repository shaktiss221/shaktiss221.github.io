---
title: Postgres MVCC, vacuum and isolation
date: 2026-10-24
summary: Row versions, snapshots, vacuum and transaction ID wraparound: how Postgres lets readers and writers never block each other.
tags: databases
---

Postgres never overwrites a row in place: every `UPDATE` writes a new copy, and every reader picks the copy its snapshot is allowed to see. That one design choice explains why readers never block writers, why tables bloat, why vacuum exists, and why a 32-bit transaction counter can take a database offline. Reading time is about 30 minutes.

## 1. How Postgres stores row versions

### Tuples, xmin and xmax

A table in Postgres is a **heap**: a file of 8 KB pages, each holding row versions called **tuples**. Every tuple has a 23-byte header. The two fields that matter most here are:

- **`xmin`**: the ID of the transaction that created this version.
- **`xmax`**: the ID of the transaction that deleted or replaced it (0 if none).

Every transaction that writes gets a **transaction ID (XID)**, a 32-bit counter that increases over time. You can see the hidden columns directly: `SELECT xmin, xmax, ctid, * FROM accounts;`.

The three write operations map onto these fields simply:

- **INSERT** writes a new tuple with `xmin` = my XID, `xmax` = 0.
- **DELETE** doesn't remove anything. It sets `xmax` = my XID on the current tuple.
- **UPDATE** is a delete plus an insert: set `xmax` on the old tuple, write a new tuple with `xmin` = my XID, and point the old tuple's **`ctid`** (its physical address, page and slot) at the new one, forming a version chain.

<!--diagram:bed7bb4b-2df6-->

Two updates leave three versions of one row in the heap; the reader's snapshot decides that version 2 is the one it sees.

### Snapshots

A **snapshot** records which transactions' work is visible. It is essentially three things:

- `xmin`: the oldest transaction still running when the snapshot was taken. Everything older is finished.
- `xmax`: the next XID to be assigned. Everything at or above it started after the snapshot and is invisible.
- `xip`: the list of XIDs between those two that were still in progress.

You can look at one with `SELECT pg_current_snapshot();`, which returns something like `100:104:100,102`.

### The visibility rule

A tuple is visible to a snapshot if, roughly:

1. Its `xmin` transaction **committed** and is not in the snapshot's in-progress set (the creator finished before I looked), **and**
2. Its `xmax` is 0, or the `xmax` transaction **aborted**, or is still in progress from my snapshot's point of view (the deleter hadn't finished when I looked).

A transaction always sees its own changes, which adds a few special cases involving command IDs (`cmin`/`cmax`) so a statement doesn't see rows it inserted earlier in the same statement.

To check whether an XID committed or aborted, Postgres looks it up in the **commit log** (`pg_xact`, formerly `pg_clog`): two bits per transaction. Looking that up for every tuple would be slow, so the first reader to learn a tuple's creator committed sets **hint bits** in the tuple header (`HEAP_XMIN_COMMITTED` and friends). This is why a plain `SELECT` on freshly loaded data can generate writes: it is setting hint bits and dirtying pages.

### Why this design

The payoff is concurrency. A long report reading a table doesn't block an `UPDATE` to it, and the update doesn't block the report: each sees its own consistent version. Writers still block other writers on the **same row** (the second updater waits on a row lock until the first commits or aborts), but there are no read locks at all.

Other databases do MVCC differently. **MySQL InnoDB** and **Oracle** update rows in place and keep old versions in a separate **undo log**; readers that need an old version reconstruct it from undo. That keeps the main table compact, but long-running reads make the undo grow, and Oracle's famous "snapshot too old" error is what happens when the undo was recycled first. Postgres keeps old versions in the table itself, so it pays the cleanup cost later, through vacuum.

That trade-off is the subject of the next three sections.

## 2. Isolation levels in practice

In Postgres, isolation levels differ mainly in **when the snapshot is taken** and **what happens when two transactions touch the same data**. Postgres implements three distinct levels; asking for `READ UNCOMMITTED` gives you `READ COMMITTED`, because MVCC has no reason to show uncommitted data.

| Level | Snapshot taken | Prevents | Still allows |
| --- | --- | --- | --- |
| Read committed (default) | At the start of each statement | Dirty reads | Non-repeatable reads, phantoms, lost updates in read-modify-write code, write skew |
| Repeatable read | At the first statement of the transaction | The above, plus phantoms (stricter than the SQL standard requires) | Write skew |
| Serializable | Same as repeatable read, plus conflict tracking | All of the above | Nothing, at the cost of retries |

### Read committed: a fresh snapshot per statement

Each statement sees everything committed before it began. Two `SELECT`s in the same transaction can return different results if another transaction commits in between. That is usually fine for web requests that run one or two statements.

The subtle part is concurrent `UPDATE`s. Suppose two sessions both run `UPDATE accounts SET balance = balance - 10 WHERE id = 1`. The second blocks on the row lock until the first commits. Then, instead of failing, read committed **re-evaluates** the row: it fetches the newest committed version, re-checks the `WHERE` clause, and applies its update to that version. So both decrements land, and the result is correct.

The trap is application-side read-modify-write:

```sql
-- both sessions, concurrently
SELECT balance FROM accounts WHERE id = 1;   -- both read 100
-- application computes 100 - 10
UPDATE accounts SET balance = 90 WHERE id = 1;
```

Both write 90, and one withdrawal vanishes: a **lost update**. Fixes, in order of preference: do the arithmetic in SQL (`balance = balance - 10`), lock the row when reading (`SELECT ... FOR UPDATE`), or use optimistic concurrency with a version column (`UPDATE ... WHERE id = 1 AND version = 7`, and retry if zero rows matched).

### Repeatable read: one snapshot for the whole transaction

Under repeatable read, the snapshot is taken at the transaction's first statement and reused. Every query sees the database as of that moment, which is ideal for reports and consistent exports (`pg_dump` uses it).

If this transaction tries to update a row that another transaction changed and committed after the snapshot, Postgres can't apply the update to a version it can't see, so it raises `ERROR: could not serialize access due to concurrent update` (SQLSTATE `40001`). The application must retry the whole transaction. This turns the lost update from the previous example into an error, which is the right trade.

What repeatable read (snapshot isolation) still allows is **write skew**. The classic example: a hospital requires at least one doctor on call. Alice and Bob are both on call, and both feel ill at the same time.

1. Alice's transaction counts on-call doctors: 2. Fine to leave.
2. Bob's transaction counts on-call doctors: 2. Fine to leave.
3. Alice sets her own row to off-call. Bob sets his own row to off-call.
4. Both commit. Nobody is on call.

They updated **different rows**, so there was no write-write conflict to detect. Each transaction's decision was based on data the other changed.

### Serializable: snapshot isolation plus conflict detection

Since Postgres 9.1, `SERIALIZABLE` uses **Serializable Snapshot Isolation (SSI)**, based on research by Michael Cahill and colleagues (2008) and implemented by Dan Ports and Kevin Grittner (described in their VLDB 2012 paper). It runs exactly like repeatable read, but also tracks what each transaction *read* using lightweight **SIRead locks** (on tuples, pages or whole relations). These locks block nothing; they only record dependencies.

SSI watches for **read-write antidependencies**: transaction T1 read something that T2 later wrote. Theory shows that every non-serializable execution under snapshot isolation contains a "dangerous structure": two consecutive such edges, T1 → T2 → T3, where T2 is the pivot. When Postgres spots one, it aborts a transaction with `ERROR: could not serialize access due to read/write dependencies among transactions` (also `40001`). In the doctors example, one of the two commits fails, and its retry sees only one doctor on call.

Practical notes on serializable:

- **Retries are mandatory.** Wrap every serializable transaction in a retry loop on SQLSTATE `40001`. Without it, the level just turns bugs into errors.
- **False positives happen.** SSI is conservative, especially when locks are promoted from tuple to page to relation granularity. Good indexes keep reads (and therefore SIRead locks) narrow.
- **Mark read-only transactions** as `READ ONLY` (and `DEFERRABLE` for long reports), so Postgres can skip tracking or wait for a safe snapshot that can never fail.
- **Throughput cost** is usually modest, often reported in the single-digit to low-double-digit percent range, much cheaper than strict two-phase locking.

### Choosing

Most applications run read committed and handle the dangerous spots explicitly with `FOR UPDATE`, atomic SQL, or constraints. Teams with many invariants that span rows (balances, quotas, scheduling) increasingly choose serializable plus a generic retry wrapper, which is easier to reason about than auditing every query for write skew.

## 3. The cost of MVCC: bloat, HOT and vacuum

Every update and delete leaves a **dead tuple** behind. Until something reclaims it, it takes space in the heap and in every index that points to it. That something is `VACUUM`, and keeping it ahead of your write rate is the main ongoing job of running Postgres.

### What "dead" means

A tuple is dead when its `xmax` transaction committed **and** no running transaction could still need it. The second condition is the important one. Postgres computes an **xmin horizon**: the oldest XID that any open snapshot might still treat as in progress. Tuples deleted after that horizon must be kept, even if the deleting transaction committed hours ago.

Anything that holds the horizon back blocks cleanup across the whole database:

- a long-running query or report,
- a session sitting **idle in transaction** (an application that ran `BEGIN` and forgot to commit),
- an abandoned **prepared transaction** (`PREPARE TRANSACTION`),
- a **replication slot** whose consumer is gone or lagging,
- a standby with `hot_standby_feedback = on` running long queries.

The first check in any bloat investigation is `SELECT pid, state, xact_start, backend_xmin FROM pg_stat_activity ORDER BY age(backend_xmin) DESC;`, plus `pg_replication_slots` and `pg_prepared_xacts`. A single forgotten session can let a busy table grow from 10 GB to 100 GB over a weekend. Setting `idle_in_transaction_session_timeout` is cheap insurance.

### What VACUUM does

A plain `VACUUM` (no `FULL`):

1. Scans heap pages that might contain dead tuples, skipping pages the **visibility map** marks as all-visible.
2. Collects dead tuple IDs, then removes the index entries that point to them, one pass per index.
3. Marks the space in the heap as reusable and records it in the **free space map**, so future inserts and updates fill it.
4. Sets visibility-map bits for pages where every tuple is now visible to everyone. Those bits let later vacuums skip the page and let **index-only scans** avoid visiting the heap.
5. Freezes old tuples (section 4).

It runs alongside normal reads and writes. It does **not** return space to the operating system, except for empty pages at the very end of the file. A table that bloated to 100 GB stays 100 GB on disk, but new rows will reuse the free space inside it.

`VACUUM FULL` rewrites the table into a new compact file, but holds an `ACCESS EXCLUSIVE` lock for the duration, blocking even reads. On large production tables, the extension **pg\_repack** (or `pg_squeeze`) does the same rewrite online, taking only brief locks at the start and end. Index bloat can be fixed separately with `REINDEX CONCURRENTLY` (Postgres 12+).

### HOT updates: avoiding index churn

By default, an update creates a new tuple at a new location, so **every index** on the table needs a new entry pointing at it, even indexes on columns that didn't change. A table with 8 indexes pays 9 writes per updated row. This write amplification was one of the reasons Uber gave for moving from Postgres to MySQL in 2016.

**Heap-only tuple (HOT)** updates avoid this when two conditions hold:

1. The update changes **no indexed column**.
2. The new version **fits on the same page** as the old one.

Then the new tuple is linked from the old one inside the page, and indexes keep pointing at the original slot. Any page access can later **prune** the chain, reclaiming dead HOT versions without a full vacuum. Two practical consequences:

- Avoid indexing columns that change often (such as `updated_at`) unless you query by them.
- Lower the **fillfactor** on update-heavy tables (`ALTER TABLE t SET (fillfactor = 80)`) to leave room on each page for new versions. Check the effect with `n_tup_hot_upd` versus `n_tup_upd` in `pg_stat_user_tables`.

Postgres 14 added **bottom-up index deletion**, which removes index entries for dead versions when a B-tree page is about to split, cutting index bloat for workloads that can't use HOT.

### Autovacuum and its defaults

Autovacuum launches workers that vacuum and analyze tables based on how much has changed. A table is vacuumed when:

$$\text{dead tuples} > \texttt{autovacuum\_vacuum\_threshold} + \texttt{autovacuum\_vacuum\_scale\_factor} \times \text{rows}$$

With defaults of 50 and 0.2, that's 20% of the table. Fine for a 10,000-row table; terrible for a 500-million-row table, which collects 100 million dead tuples before cleanup begins. Since Postgres 13, inserts also trigger vacuum (`autovacuum_vacuum_insert_scale_factor`), so append-only tables get their visibility map set and get frozen gradually.

Autovacuum is deliberately throttled so it doesn't hurt foreground traffic: each worker accumulates "cost" for pages read and dirtied and sleeps when it hits a limit (`autovacuum_vacuum_cost_limit`, which inherits 200 from `vacuum_cost_limit`, with a 2 ms delay since Postgres 12). On modern SSDs those defaults are often too gentle. Common tuning:

| Setting | Default | Typical change for busy systems |
| --- | --- | --- |
| `autovacuum_vacuum_scale_factor` (per big table) | 0.2 | 0.01 to 0.05, or 0 with a fixed threshold |
| `autovacuum_vacuum_cost_limit` | 200 (inherited) | 1000 to 2000 or higher on fast storage |
| `autovacuum_max_workers` | 3 | 5 to 10 with many large tables (the cost limit is shared, so raise it too) |
| `maintenance_work_mem` / `autovacuum_work_mem` | 64 MB | 1 GB or more, so dead-TID lists fit and indexes are scanned once |

Postgres 17 replaced the dead-tuple array with a compact radix-tree store and removed the old 1 GB cap on that memory, so a single vacuum pass now handles far more dead tuples before it must rescan indexes.

Watch `n_dead_tup`, `last_autovacuum` and `autovacuum_count` in `pg_stat_user_tables`, and look for tables where vacuum runs constantly yet dead tuples keep rising: that usually means the xmin horizon is stuck, not that autovacuum is too slow.

## 4. Transaction ID wraparound and freezing

Transaction IDs are 32 bits, so there are about 4.2 billion of them. A busy system consuming 10,000 write transactions per second uses that up in under 5 days. Postgres handles this by treating XIDs as a circle and by **freezing** old rows, and if freezing falls behind, Postgres stops accepting writes to protect your data.

### The circle

Postgres compares XIDs using modulo-2³² arithmetic: for any XID, the 2.1 billion IDs "behind" it are in the past and the 2.1 billion "ahead" are in the future. That works as long as no live tuple is more than 2.1 billion transactions old. If one were, its `xmin` would suddenly look like it was in the future, and the row would vanish from every query. That is **wraparound**, and it would be silent data loss.

### Freezing

The fix is to mark tuples that are old enough as **frozen**: visible to every transaction, past and future, regardless of `xmin`. Since Postgres 9.4, freezing sets a flag combination in the tuple header rather than overwriting `xmin`, which keeps the original XID for forensics.

Each table tracks `relfrozenxid`: every tuple older than this is frozen. Each database tracks `datfrozenxid`, the minimum over its tables. The **age** of these values, how many XIDs have been used since, is what you monitor:

```sql
SELECT datname, age(datfrozenxid) FROM pg_database ORDER BY 2 DESC;

SELECT c.oid::regclass AS table_name, age(c.relfrozenxid) AS xid_age,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS size
  FROM pg_class c
 WHERE c.relkind IN ('r', 'm', 't')
 ORDER BY 2 DESC LIMIT 20;
```

The thresholds that drive freezing:

| Setting | Default | What it does |
| --- | --- | --- |
| `vacuum_freeze_min_age` | 50 million | A normal vacuum freezes tuples older than this on pages it visits |
| `vacuum_freeze_table_age` | 150 million | Above this table age, vacuum runs in **aggressive** mode, visiting every page not already marked all-frozen |
| `autovacuum_freeze_max_age` | 200 million | Above this, autovacuum forces an **anti-wraparound** vacuum on the table, even if autovacuum is disabled |
| `vacuum_failsafe_age` (PG 14+) | 1.6 billion | Emergency mode: skip index cleanup and ignore cost throttling to finish freezing as fast as possible |

Since Postgres 9.6, the visibility map has an **all-frozen** bit per page, so aggressive vacuums skip pages already frozen. Before that, every anti-wraparound vacuum read the entire table, which on multi-terabyte tables could take days.

### The danger zone

If freezing can't keep up, Postgres escalates:

1. At about 40 million XIDs before the limit, it logs warnings on every transaction: `WARNING: database "app" must be vacuumed within 39985967 transactions`.
2. At about 3 million before the limit, it **refuses to assign new XIDs**. Reads still work, but every write fails with an error telling you to vacuum. The database is effectively down for writes.
3. Recovery means running `VACUUM` (in recent versions, in normal multi-user mode; older guidance said single-user mode) on the oldest tables, which may take hours on large ones.

This has happened to well-known companies. Sentry wrote up a 2015 outage where a large table hit the limit, and Mailchimp's Mandrill had a multi-day outage in 2019 traced to XID wraparound on a sharded Postgres setup. The common causes are the same ones that cause bloat: a stuck xmin horizon (anti-wraparound vacuum can't freeze tuples that an old snapshot might still need), anti-wraparound vacuums being throttled too hard, and very large tables where nobody watched `age(relfrozenxid)` climb.

A related limit applies to **multixact IDs**, used when several transactions lock the same row at once (for example, many `SELECT ... FOR SHARE` or foreign-key checks). They have their own counter, their own `relminmxid`, and their own wraparound, governed by `autovacuum_multixact_freeze_max_age`.

### A production checklist

- Alert on `age(datfrozenxid)` crossing about 500 million, well before forced shutdown at about 2.1 billion. Alert on the oldest `backend_xmin` and on inactive replication slots.
- Set `idle_in_transaction_session_timeout` and a sensible `statement_timeout` for application roles.
- Tune autovacuum per table for your largest, busiest tables; don't rely on the 20% scale factor.
- Let anti-wraparound autovacuums run. They are not auto-cancelled by conflicting locks the way normal autovacuums are, and killing them by hand just restarts the clock.
- Schedule a manual `VACUUM (FREEZE)` during quiet hours on huge, mostly static tables, so the forced one doesn't arrive at peak.
- Partition very large append-heavy tables, so old partitions get frozen once and then skipped.

There has been long-running community work toward 64-bit transaction IDs, which would make wraparound practically impossible, but as of the latest major releases Postgres still uses 32-bit XIDs on disk, so freezing remains part of every operator's job.

## Discussion questions

1. Postgres keeps old versions in the heap; InnoDB and Oracle keep them in undo. For a workload of frequent small updates plus occasional hour-long analytical queries, which design hurts less, and where does each one feel the pain?
2. Your `orders` table has 9 indexes, one of them on `updated_at`, and `n_tup_hot_upd` is near zero. What would you change, and how would you measure the effect?
3. Rewrite the doctors-on-call write-skew example so it is safe under read committed without using serializable. Then compare that fix with simply switching to serializable plus a retry loop.
4. A table's dead tuples keep rising even though autovacuum runs every few minutes. List the four most likely causes and the query you would run to confirm each.
5. Your busiest database burns about 300 million XIDs a day. With default settings, how often will anti-wraparound vacuums hit your largest tables, and what would you change before that becomes a problem?
6. Why can a plain `SELECT` on a freshly bulk-loaded table cause a burst of disk writes, and what could you do right after a bulk load to avoid it?

## Sources

Written from the references below. Links were not re-opened while writing this piece, so check the current docs for defaults in your Postgres version.

- [Concurrency Control](https://www.postgresql.org/docs/current/mvcc.html), PostgreSQL documentation (chapter on MVCC and transaction isolation)
- [Routine Vacuuming](https://www.postgresql.org/docs/current/routine-vacuuming.html), PostgreSQL documentation (includes preventing XID wraparound)
- [Heap-Only Tuples (HOT)](https://www.postgresql.org/docs/current/storage-hot.html), PostgreSQL documentation
- [Serializable Snapshot Isolation in PostgreSQL](https://drkp.net/papers/ssi-vldb12.pdf), Dan R. K. Ports and Kevin Grittner, VLDB 2012
- Michael J. Cahill, Uwe Röhm, Alan D. Fekete, "Serializable Isolation for Snapshot Databases", SIGMOD 2008
- [The Internals of PostgreSQL](https://www.interdb.jp/pg/), Hironobu Suzuki (chapters 5 and 6 on concurrency control and vacuum)
- [Why Uber Engineering Switched from Postgres to MySQL](https://www.uber.com/blog/postgres-to-mysql-migration/), Uber Engineering, 2016
- [Transaction ID Wraparound in Postgres](https://blog.sentry.io/transaction-id-wraparound-in-postgres/), Sentry, 2015
- Martin Kleppmann, *Designing Data-Intensive Applications*, chapter 7 (Transactions)
