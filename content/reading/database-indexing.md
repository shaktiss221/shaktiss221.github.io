---
title: "Database indexes in practice: how queries find rows"
date: 2026-11-06
summary: How B-tree indexes really work, the left-prefix rule for composite indexes, covering and partial indexes, when the planner ignores your index and why, specialized index types (GIN, BRIN, hash), reading EXPLAIN, and the write-side cost every index adds, with Postgres as the running example.
tags: databases
daily: true
---

Most slow database queries aren't slow because the database is slow. They're slow because the database has to read far more data than the answer needs, and the usual fix is an index. But indexes are often added by guesswork: one per column, or whatever an ORM suggested, and then a query still does a full table scan and nobody knows why. This reading builds a working mental model of indexes from the data structure up: how a B-tree finds rows, why column order in a composite index matters so much, how covering and partial indexes cut work further, how the query planner decides whether to use an index at all, and what every index costs on the write side. The examples use PostgreSQL, but the ideas carry over to MySQL, SQL Server and most other relational databases.

## 1. Why tables need indexes

A table in Postgres is a **heap**: rows stored in pages (8 KB each by default) in no particular order. Finding all orders for customer 42 without an index means a **sequential scan**: read every page, check every row. For a table with a hundred million rows, that's gigabytes of reading to return maybe a dozen rows.

An **index** is a separate data structure that keeps some columns in sorted order, with a pointer back to each row's location in the heap (in Postgres, a tuple ID: page number plus position). Finding customer 42's orders becomes: search the index for 42, read the few matching entries, then fetch just those rows. The work now depends on the size of the answer, not the size of the table.

Markus Winand's book and website *Use The Index, Luke* is still the best practical guide to this topic, and his core message shapes this whole reading: **indexing is a developer's job**, because only the person writing the query knows which access paths the application needs.

## 2. The B-tree

The default index in nearly every relational database is the **B-tree** (strictly, a B+tree). Its structure:

- **Leaf pages** hold index entries (key plus row pointer) in sorted order, and each leaf links to its neighbors, forming a doubly linked list across the whole index.
- **Internal pages** above them hold separator keys that say which child page covers which key range.
- A single **root** page sits on top.

Each page holds hundreds of entries, so the tree is very shallow. Three or four levels index hundreds of millions of rows, and the upper levels are almost always in memory. Finding a key means walking from the root to one leaf (a few page reads), and then reading neighbors along the leaf chain for a range.

This gives B-trees their two fundamental operations:

1. **Seek**: jump directly to the first entry matching a value or range start. Cost: tree depth, a few pages.
2. **Scan**: read entries in order from there. Cost: proportional to the number of matching entries.

Everything about using indexes well comes down to arranging for queries to need **one seek and a short scan**, rather than many seeks or a long scan.

### The hidden cost: fetching rows

The index gives row pointers, but the query usually needs columns that aren't in the index, so the database must also visit the heap. If 10,000 matching rows are scattered across 10,000 different heap pages, that's 10,000 random page reads, which can be slower than just scanning the table sequentially. This is why the planner sometimes ignores an index that technically matches the query (section 6), and why **covering indexes** (section 4) are so effective.

Postgres softens the scattered-fetch problem with a **bitmap scan**: it first collects all matching row pointers from the index, sorts them by page, and then reads each needed heap page once, in physical order. You'll often see `Bitmap Index Scan` feeding a `Bitmap Heap Scan` in query plans for medium-selectivity queries.

## 3. Composite indexes and the left-prefix rule

<!--diagram:composite-index-1106-->

A **composite** (multi-column) index sorts by the first column, then by the second within equal first-column values, and so on, like a phone book sorted by last name, then first name. That ordering determines which queries can seek into it.

For an index on `(tenant_id, created_at)`:

- `WHERE tenant_id = 9`: seek to tenant 9, scan its entries. Works.
- `WHERE tenant_id = 9 AND created_at >= '2026-10-15'`: seek to `(9, 2026-10-15)`, scan to the end of tenant 9. Ideal: one seek, short scan.
- `WHERE tenant_id = 9 ORDER BY created_at DESC LIMIT 20`: seek to the end of tenant 9 and read backward 20 entries. No sort, no reading of older rows. This is the pattern behind fast feeds, inboxes and the cursor pagination in the Oct 26 API design reading.
- `WHERE created_at >= '2026-10-15'` alone: matches are spread across every tenant in the index. There's no single place to seek to.

That last case is the **left-prefix rule**: a B-tree index can be used efficiently for conditions on a **leading prefix** of its columns. You can phone-book search by last name, or last name plus first name, but not by first name alone.

PostgreSQL 18 (released September 2025) added **skip scan** for B-trees, which softens this rule. When the leading column has few distinct values, the planner can do a separate seek for each value of the first column (tenant 7, tenant 9, tenant 12...), each one jumping straight to the matching dates. Oracle and MySQL 8.0 have had similar features. It helps when the leading column has low cardinality, but it's no substitute for the right column order when the query is common.

### Choosing column order

The traditional advice, "most selective column first," is incomplete. A better rule of thumb:

1. **Equality columns first**: columns compared with `=` (or `IN`), since every value of them narrows to one contiguous range.
2. **Then the range or sort column**: the column used with `<`, `>`, `BETWEEN` or `ORDER BY`.
3. **Range conditions end the useful prefix.** In an index on `(a, b, c)` with `WHERE a = 1 AND b > 5 AND c = 3`, the seek uses `a` and `b`. The `c = 3` condition can still filter entries during the scan, but it doesn't narrow the seek, because within `b > 5` the `c` values aren't sorted. Putting `c` before `b` would make the seek exact.

Design composite indexes around your most important queries, not around columns. One well-ordered index often serves several queries that share a prefix: an index on `(tenant_id, status, created_at)` supports filtering by tenant, by tenant and status, and listing by date within both.

## 4. Covering indexes and index-only scans

If every column a query needs is in the index, the database can answer from the index alone and skip the heap entirely. That's an **index-only scan**, and for queries that return many rows it can be dramatically faster.

Postgres (since version 11) supports `INCLUDE` columns, which are stored in leaf entries but not part of the sort key:

```sql
CREATE INDEX orders_by_customer
  ON orders (customer_id, created_at)
  INCLUDE (status, total_cents);
```

Now `SELECT created_at, status, total_cents FROM orders WHERE customer_id = 42 ORDER BY created_at DESC` can be served without touching the table. `INCLUDE` columns don't affect ordering, so they can be added without changing which queries can seek.

There's a Postgres-specific catch. Because of MVCC (the Oct 24 pack reading), an index entry doesn't say whether its row version is visible to your transaction. Postgres checks the **visibility map**, a per-page bit saying "every row on this page is visible to everyone." For pages marked all-visible, the index-only scan really skips the heap. For other pages it must still check the heap. Vacuum sets those bits, so on a table with heavy updates and lagging vacuum, an "index-only scan" quietly turns into a regular index scan. `EXPLAIN (ANALYZE)` shows this as `Heap Fetches`.

The trade-off: covering indexes are bigger, so they take more memory and make writes more expensive. Use them for hot, read-heavy queries, not everywhere.

## 5. Partial and expression indexes

**Partial indexes** index only the rows matching a condition:

```sql
CREATE INDEX jobs_pending ON jobs (run_at) WHERE state = 'pending';
```

If 99% of jobs are completed and only a few thousand are pending, this index stays tiny and hot in memory, while still serving the query that workers run constantly (`WHERE state = 'pending' ORDER BY run_at`). The Postgres job queues from the Nov 1 reading benefit enormously from exactly this. Partial indexes are also useful for enforcing conditional uniqueness:

```sql
CREATE UNIQUE INDEX one_active_subscription
  ON subscriptions (user_id) WHERE status = 'active';
```

The planner only uses a partial index when it can prove the query's `WHERE` clause implies the index's condition, so the query must include a matching predicate.

**Expression indexes** index a computed value:

```sql
CREATE INDEX users_email_lower ON users (lower(email));
```

A query with `WHERE lower(email) = 'a@b.com'` can use it. Without it, wrapping an indexed column in a function hides it from the index: `WHERE lower(email) = ...` can't use a plain index on `email`. The same happens with type casts and arithmetic (`WHERE created_at::date = '2026-11-06'`, or `WHERE price * 1.18 > 100`). Rewrite the condition to keep the column bare (`created_at >= '2026-11-06' AND created_at < '2026-11-07'`) or index the expression.

## 6. How the planner chooses

Having an index doesn't mean it gets used. Postgres's **cost-based planner** estimates the cost of each possible plan (sequential scan, index scan, bitmap scan, index-only scan, and join strategies for multi-table queries) and picks the cheapest.

The estimates depend on **statistics** gathered by `ANALYZE` (run automatically by autovacuum): per column, the fraction of nulls, the number of distinct values, a list of the most common values with their frequencies, and a histogram of the rest. From these, the planner estimates **selectivity**: what fraction of rows a condition will match.

The planner then weighs I/O using settings like `seq_page_cost` (1.0 by default) and `random_page_cost` (4.0 by default). The default assumes a random page read costs four times a sequential one, which reflects spinning disks more than modern SSDs. Many teams on SSDs lower `random_page_cost` to around 1.1 to 2, which makes the planner more willing to use indexes.

Common reasons an index isn't used:

- **The condition matches a large fraction of rows.** If `WHERE status = 'active'` matches 80% of the table, a sequential scan really is cheaper. This is the planner being right.
- **The statistics are wrong.** After bulk loads or big deletes, before autovacuum has run `ANALYZE`, estimates can be wildly off. Run `ANALYZE` and check again.
- **Correlated columns.** The planner assumes conditions are independent by default. `WHERE city = 'Mumbai' AND country = 'India'` gets underestimated, because it multiplies the two selectivities. Postgres's extended statistics (`CREATE STATISTICS`, since version 10) can capture such dependencies.
- **The column is wrapped in a function or cast** (section 5), or compared with a mismatched type.
- **Leading wildcards**: `LIKE '%term'` can't use a B-tree. Use a trigram index (`pg_trgm` with GIN) or full-text search.
- **The index doesn't match the left prefix** (section 3).
- **Small tables.** For a few pages, a sequential scan is cheapest and that's fine.

### Reading EXPLAIN

`EXPLAIN` shows the chosen plan; `EXPLAIN (ANALYZE, BUFFERS)` runs the query and shows what actually happened. Things to look for:

- **Estimated vs actual rows.** `rows=10` estimated versus `actual rows=50000` means bad statistics, and the plan was chosen for the wrong situation. This is the single most useful thing to check.
- **The scan type**: `Seq Scan`, `Index Scan`, `Index Only Scan` (check `Heap Fetches`), `Bitmap Heap Scan`.
- **Rows Removed by Filter**: many removed rows means the index narrowed poorly, often a sign that column order is wrong or a condition isn't in the index.
- **Buffers**: `shared hit` (from memory) and `read` (from disk) show how much data the query really touched.
- **Sort and its method**: an external sort (`Sort Method: external merge`) spills to disk. An index that matches the `ORDER BY` can remove the sort entirely.

`pg_stat_statements` tells you which queries cost the most in total across all calls. Start there, not with whichever query someone complained about.

## 7. Beyond B-trees

Postgres ships several other index types for data that B-trees can't handle well:

| Type | Good for | Example |
|---|---|---|
| **Hash** | Equality only; rarely better than B-tree in practice | `WHERE token = ...` |
| **GIN** (generalized inverted index) | Values containing many elements: arrays, `jsonb`, full-text, trigrams | `WHERE tags @> '{urgent}'`, `WHERE doc @> '{"type":"invoice"}'` |
| **GiST** | Overlap and nearness: geometry, ranges, exclusion constraints | PostGIS, "no two bookings overlap" |
| **SP-GiST** | Space-partitioned data: IP ranges, some geometric data | `inet` lookups |
| **BRIN** (block range index) | Huge tables whose physical order follows a column | Append-only events by timestamp |

**GIN** works like a search engine's inverted index: for each element (a word, a JSON key-value pair, an array item), it stores the list of rows containing it. Lookups are fast, updates are expensive, which is why GIN has a "pending list" to batch insertions.

**BRIN** is the opposite of a B-tree in size. It stores, for each range of table pages (128 pages by default), just the minimum and maximum value of a column. For an append-only events table where `created_at` increases with physical position, a BRIN index might be a few hundred kilobytes where a B-tree would be gigabytes. Queries skip every block range whose min-max doesn't overlap the condition. It only works if the physical order correlates with the column; on randomly ordered data it's useless.

Vector similarity search (`pgvector` with HNSW or IVFFlat indexes) has become another major index type in recent years, for embedding search. It's approximate by design, a different trade-off from everything above.

## 8. The cost side: every index taxes writes

Indexes speed up reads and slow down everything else.

- **Every insert** must add an entry to every index on the table.
- **Every update** that changes an indexed column must update that index. In Postgres, an update usually creates a new row version, and unless the update qualifies as a **HOT** (heap-only tuple) update, which requires that no indexed column changed and that the page has free space, *every* index gets a new entry, even indexes on unchanged columns. More indexes means fewer HOT updates and more write amplification. Uber's 2016 post on moving from Postgres to MySQL cited exactly this write amplification as one of its reasons.
- **Every delete** leaves index entries for vacuum to clean up.
- **Storage and memory**: indexes compete with table data for the buffer cache. An index that doesn't fit in memory makes every lookup slower.
- **Vacuum and maintenance** work grows with the number and size of indexes.

So indexes should be deliberate. Practical habits:

- **Find unused indexes.** `pg_stat_user_indexes.idx_scan` counts how often each index is used. An index with zero scans over weeks (on every replica, since replicas have their own counters) is a candidate to drop.
- **Find redundant indexes.** An index on `(a)` is usually redundant if one exists on `(a, b)`, since the composite can serve the same queries (the single-column one is smaller and may still be slightly faster).
- **Create and drop without blocking.** `CREATE INDEX CONCURRENTLY` builds an index without blocking writes (it takes longer and can fail, leaving an invalid index to drop). `DROP INDEX CONCURRENTLY` likewise. On a busy production table, a plain `CREATE INDEX` blocks writes for the whole build.
- **Watch for bloat.** Indexes on heavily updated columns can bloat. Postgres 13's B-tree deduplication and bottom-up deletion in version 14 reduced this a lot, and `REINDEX CONCURRENTLY` (version 12+) rebuilds without downtime.

## 9. Patterns from real systems

**Multi-tenant SaaS.** Almost every query includes `tenant_id`, so most indexes should start with it: `(tenant_id, ...)`. This keeps each tenant's data contiguous in the index and makes per-tenant queries a single seek. It also makes later sharding by tenant easier.

**Feeds and timelines.** `(user_id, created_at DESC)` plus keyset pagination (`WHERE user_id = ? AND created_at < ? ORDER BY created_at DESC LIMIT 20`) gives constant-time pages regardless of depth.

**Soft deletes.** If every query says `WHERE deleted_at IS NULL`, make the important indexes partial on that condition so deleted rows don't bloat them.

**Uniqueness and foreign keys.** Unique constraints are indexes. Foreign keys in Postgres are *not* automatically indexed on the referencing side. Without an index on `orders.customer_id`, deleting a customer scans the whole orders table to check for references, and joins on that column can't use an index either. A missing foreign key index is one of the most common performance bugs.

**Status queues.** Partial indexes on the small "active" subset (pending jobs, open tickets, unpaid invoices) keep the hot path tiny.

## 10. A workflow for slow queries

1. **Find the queries that matter** with `pg_stat_statements`, by total time, not just by mean.
2. **Run `EXPLAIN (ANALYZE, BUFFERS)`** on a realistic parameter set, on production-like data.
3. **Compare estimated and actual rows.** If they're far apart, fix statistics first (`ANALYZE`, extended statistics, higher statistics targets).
4. **Look for the access path you'd want:** one seek, a short scan, ideally no heap fetches and no sort.
5. **Design the index for that path:** equality columns, then range or sort column, then `INCLUDE` columns if it's hot; partial if it targets a small subset.
6. **Build it concurrently** and re-check the plan and the timings.
7. **Measure the write cost**, and remove indexes that no longer earn their keep.

## Key takeaways

- An index trades write cost and space for reading only the rows the answer needs.
- B-trees are shallow sorted trees: queries are fast when they need one seek and a short ordered scan.
- Composite indexes follow the left-prefix rule. Put equality columns first, then the range or sort column. Postgres 18's skip scan helps when the leading column has few values.
- Covering indexes with `INCLUDE` enable index-only scans, which depend on the visibility map in Postgres.
- Partial and expression indexes keep hot paths small and let functions use indexes.
- The planner uses statistics and cost settings. Wrong row estimates are the most common reason for bad plans.
- GIN, GiST, BRIN and vector indexes handle data B-trees can't.
- Every index slows writes and can block HOT updates. Drop unused ones and build new ones concurrently.

## Discussion questions

1. You have an index on `(created_at, tenant_id)` and the main query is `WHERE tenant_id = ? AND created_at > ?`. What happens, and what would you change?
2. A query's plan shows `rows=12` estimated and `actual rows=480000`. What are three possible causes, and how would you check each?
3. When is a sequential scan the right choice even though an index exists?
4. Your `orders` table has 14 indexes and write latency is climbing. How would you decide which to remove?
5. Why does an index-only scan still show heap fetches on a frequently updated table, and what would reduce them?
6. Would a BRIN index help on a `users` table indexed by `signup_date`? What about an `events` table that's only appended to?
7. Pick one slow query in a system you work on. Sketch the index you'd want, in column order, and explain why.

## Sources

- Markus Winand, *SQL Performance Explained* and *Use The Index, Luke*, https://use-the-index-luke.com
- PostgreSQL documentation: "Indexes" (types, multicolumn, index-only scans and covering indexes, partial indexes, expression indexes), "Using EXPLAIN," "Statistics Used by the Planner," "Planner Cost Constants," https://www.postgresql.org/docs/current/indexes.html
- PostgreSQL 18 release notes (September 25, 2025), B-tree skip scan, https://www.postgresql.org/docs/release/18/
- PostgreSQL release notes for versions 11 (`INCLUDE`), 12 (`REINDEX CONCURRENTLY`), 13 (B-tree deduplication) and 14 (bottom-up index deletion).
- Peter Geoghegan's pgsql-hackers posts on PostgreSQL 18 skip scan, https://www.postgresql.org/message-id/CAH2-Wzk1aYZpsXRP1iz7iBweg55dZPicPBUL50SmtzkF-Rm9ig%40mail.gmail.com
- Egor Rogov, *PostgreSQL 14 Internals* (Postgres Professional, 2023), chapters on index access methods and the planner.
- Uber Engineering, "Why Uber Engineering Switched from Postgres to MySQL" (2016).
- Rudolf Bayer and Edward McCreight, "Organization and Maintenance of Large Ordered Indices" (1970), the original B-tree paper.
- pgvector documentation, https://github.com/pgvector/pgvector

The PostgreSQL 18 release date was confirmed from the release notes on Nov 6, 2026. The default cost settings and version numbers for each feature come from the PostgreSQL docs as I know them.
