---
title: "How Figma scaled Postgres: from one box to horizontal sharding"
date: 2026-11-10
summary: A case study in scaling a relational database in place. Covers Figma's path from a single Postgres instance through vertical partitioning to horizontal sharding, the choice of shard keys and colos, logical sharding with views before physical moves, the DBProxy query engine, what they gave up, and how it compares with buying a distributed database.
tags: case study, databases
daily: true
---

In 2020, Figma ran on a single Postgres database on AWS's largest physical instance. By the time its databases team wrote about it publicly, the database stack had grown almost 100 times, and the company had scaled it without switching databases, without a long migration freeze, and with only seconds of partial availability impact at each step. Figma's two engineering posts, "The growing pains of database architecture" (vertical partitioning) and "How Figma's databases team lived to tell the scale" (horizontal sharding), are among the clearest public accounts of scaling Postgres in place. This reading walks through that journey: the quick wins first, then splitting tables across databases, then splitting individual tables across many databases, and the design decisions that made each step safe and reversible.

## 1. The starting point

Figma is a collaborative design tool. Its backend (Ruby, using ActiveRecord) stores metadata about files, users, teams, organizations, comments and permissions in Postgres on **Amazon RDS**, with PgBouncer for connection pooling. (The design documents themselves are stored separately; this is the relational metadata around them.)

The second post describes the situation in 2020: traffic was growing roughly **three times a year**, and the single RDS database was running at more than **65% CPU** during peak traffic. At that level, latency gets worse in unpredictable ways and there's little headroom for a traffic spike or a bad query.

The team's first steps were the obvious, tactical ones:

- **Scale up**: move from an `r5.12xlarge` to an `r5.24xlarge`, the largest instance available.
- **Add read replicas** and route read-only queries to them.
- **Add PgBouncer** to manage the flood of connections from application servers (Postgres handles many connections poorly, since each one is a separate process).

These bought time, but scaling up has a hard ceiling. You can't buy a bigger machine than the biggest one, and write traffic can't go to read replicas. The team needed to spread data across multiple databases.

## 2. Why not just switch databases?

The obvious alternative was to move to a database built to scale horizontally: a distributed SQL ("NewSQL") system like CockroachDB, TiDB or Spanner (Nov 2 reading), or a NoSQL store like DynamoDB (Oct 20 reading).

Figma decided against it, and the posts explain why:

- **Migration risk and time.** Their application was built on Postgres semantics, ActiveRecord, and years of SQL. A database switch would be a long, risky migration of the company's most critical data.
- **Operational familiarity.** The team knew RDS Postgres well, including how it failed. A new system brings new failure modes that take years to learn.
- **Being a pioneer.** In the post's words, they "didn't want to bear the burden of being the first customer to hit certain scaling issues." At Figma's growth rate, they were worried about running into problems in a less mature system that nobody else had hit yet.

They chose to scale Postgres incrementally, building only the pieces they needed, and to reconsider managed or NewSQL options later. That framing is worth noting: it wasn't a claim that sharding Postgres yourself is better in general, but that for their timeline and risk tolerance, an incremental path on known technology was safer than a big switch.

## 3. Step one: vertical partitioning

**Vertical partitioning** moves whole tables (or groups of related tables) to their own database. Instead of one database holding everything, files-related tables go to one database, comments to another, and so on. The application routes each query to the database that holds the table.

How they chose what to move:

- **Impact**: tables responsible for a large share of database load (measured in average active sessions).
- **Isolation**: tables only weakly connected to others, so moving them wouldn't break joins and transactions the application relied on.

How they moved tables with minimal disruption:

1. Set up **logical replication** from the old database to the new one, so the new database receives a continuous copy.
2. **Drop indexes during the initial copy** and rebuild them afterward. This cut the initial data copy from days to a matter of hours.
3. Once caught up, briefly **pause writes** to the tables being moved, wait for replication to drain, then switch the application to the new database.
4. Set up **reverse replication** from new to old, so they could roll back to the old database without losing writes made after the switch.

Each move caused about **30 seconds of partial availability impact, with about 2% of requests dropped**. The first operation moved two tables; the last, in October 2022, moved 50. By the end of 2022, Figma had caching, read replicas and around a dozen vertically partitioned databases, and the largest partition was running at about 10% CPU.

This is the right first step for most growing systems, and worth emphasizing: vertical partitioning needs no changes to how queries are written, only to where they're sent. It also matches the natural boundaries of the product.

## 4. Why vertical partitioning ran out

Vertical partitioning has a built-in limit: **the smallest unit you can move is one table**. Some of Figma's tables had grown to **several terabytes and billions of rows**, and they kept growing. Two problems were coming:

- **Vacuum.** Postgres vacuum (the Oct 24 MVCC pack reading) must periodically scan huge tables to clean up dead row versions and prevent transaction ID wraparound. On multi-terabyte tables, vacuum took long enough to affect reliability.
- **Write IOPS.** The highest-write tables were on track to exceed the maximum IOPS that RDS supports for a single instance.

No amount of moving tables around fixes a single table that's too big for one machine. The only option left was **horizontal sharding**: splitting the rows of one table across many databases.

## 5. Choosing shard keys and colos

Horizontal sharding starts with the **shard key**: the column used to decide which shard a row lives on. Every query that includes the shard key can go to a single shard. Queries without it must ask every shard.

Figma's data model naturally clusters around a few entities, so they picked a small set of shard keys: **`UserID`, `FileID` and `OrgID`**. Almost every table could be sharded by one of these.

Tables that share a shard key are grouped into a **colo** (colocation). All rows for a given `FileID`, across all tables in the file colo, live on the same shard. Within a colo, the application can still do **joins and transactions**, as long as they're scoped to one shard key value, because all the data involved is on one database. This keeps the most common access patterns, "everything about this file" or "everything about this user," working like plain Postgres.

### Hashing the key

Figma's IDs were generated in a roughly increasing order (auto-increment and Snowflake-style timestamp-prefixed IDs). If shards were assigned by **ranges** of ID, all new rows, which are also the most active, would land on the same shard, creating a hotspot. So Figma routes by a **hash of the shard key**, which spreads new and old rows evenly. The cost, which the post acknowledges, is that range scans over the key become less efficient, since consecutive IDs end up on different shards. The consistent hashing pack reading (Oct 27) covers this trade-off between range and hash partitioning in general.

## 6. Logical sharding before physical sharding

<!--diagram:figma-sharding-1110-->

The most distinctive part of Figma's design is splitting the rollout into two separate steps, so the risky parts could be tested and reversed independently.

**Logical sharding.** First, without moving any data, they made the application *behave* as if the table were sharded. They created Postgres **views** on the existing, unsharded table, one per future shard, each selecting the rows whose hashed shard key falls in that shard's range:

```sql
CREATE VIEW table_shard1 AS
SELECT * FROM table
WHERE hash(shard_key) >= min_shard_range
  AND hash(shard_key) < max_shard_range;
```

Reads and writes went through these views, routed by the new proxy layer (section 7), behind **feature flags** that rolled the change out to a percentage of traffic. If something broke, such as a query the router couldn't handle or a performance problem, they could turn the flag off in seconds and go back to the unsharded path. All the data was still in one database, so there was nothing to migrate back. They measured the overhead of querying through views and found it was under 10% in the worst case.

**Physical sharding.** Only after logical sharding had run successfully did they actually move data to separate databases. Because the application was already sending every query through the shard-aware path, this step became a database failover: copy data to the new shard databases with replication, then switch the mapping so each logical shard points at its new physical home. The step that's hard to undo was reduced to a well-understood infrastructure operation.

This separation is the same idea as the progressive delivery reading from Nov 7, applied to a database: separate the risky behavioral change (the application now treats data as sharded) from the risky infrastructure change (data physically moves), and make the first one gradual and reversible with feature flags.

## 7. DBProxy: a query engine in front of Postgres

Sharding needs something that knows where data lives and routes queries accordingly. Figma built **DBProxy**, a Go service that sits between the application and PgBouncer.

DBProxy includes a small **query engine**:

- A **parser** that turns SQL into an abstract syntax tree.
- A **logical planner** that works out which tables, shard keys and conditions the query involves.
- A **physical planner** that decides which shard or shards to send it to and how to combine the results.

Queries that include the shard key go straight to one shard. Queries that don't are **scatter-gathered**: sent to every shard, with the results merged. Scatter-gather works, but it costs load on every shard, so the team tried to keep it rare for hot paths.

The **shard topology**, meaning which logical shards map to which physical databases, is stored in S3 and etcd, and changes propagate to DBProxy in under a second. That's what makes the physical-sharding failover quick.

### Supporting only what's needed

Writing a SQL engine that supports all of Postgres would be a huge project. Instead, the team decided to support a deliberately limited subset. To choose it, they ran **shadow planning**: DBProxy logic processed real production queries without acting on them, and the results were logged to Snowflake for analysis. That showed which query shapes the application actually used. The supported language covers **the most common 90% of queries**, and the remaining queries were rewritten in the application.

Key restrictions:

- **Joins are allowed only within a colo, on the shard key.** A query joining a file's tables by `FileID` works; joining across colos or on other columns doesn't.
- **No cross-shard transactions.** A transaction must touch a single shard. Product code that writes to several shards must tolerate partial failures, where one write commits and another doesn't.
- **Unique constraints must include the shard key**, because each shard can only enforce uniqueness on its own rows.

These are the same restrictions most sharded systems end up with, from Vitess to Citus. What's notable is how they chose them: from measured production traffic rather than up-front design.

## 8. The result

The first horizontally sharded table went live in **September 2023**, about **nine months** after the project started. The physical failover caused **ten seconds of partial availability on database primaries, and no availability impact on replicas**, and the team reported no regressions in latency or availability after sharding.

The post is honest about what remained unsolved at the time:

- **Sharded schema changes**, which must be coordinated across all shards.
- **Globally unique ID generation** across shards.
- **Atomic cross-shard transactions.**
- **Distributed globally unique indexes.**
- **An ORM** layer that understands sharding, to make developer experience smoother.
- **One-click resharding**, to split shards further as they grow.

They also said they'd reassess their in-house approach against NewSQL and managed options over time, now that they'd bought themselves room.

## 9. How this compares with other approaches

Figma's path is one of several common ways to scale a relational database. It helps to see the options side by side.

| Approach | Examples | Trade-off |
|---|---|---|
| Scale up + read replicas | RDS, Aurora | Simple; hard ceiling on writes |
| Vertical partitioning | Figma 2020 to 2022 | No query changes; limited by largest table |
| Application-level sharding | Many early startups | Full control; logic spread through the app |
| Proxy-based sharding | Figma's DBProxy, Vitess for MySQL, PgDog/PgCat for Postgres | Central routing; must support a SQL subset |
| Extension-based sharding | Citus for Postgres | Distributed queries inside Postgres; a different operational model |
| Distributed SQL | CockroachDB, TiDB, YugabyteDB, Spanner, Aurora DSQL | Sharding built in; migration and new failure modes |

**Vitess** is the closest analogy. Built at YouTube to shard MySQL, it uses a proxy layer (VTGate) with a query planner, keyspaces and shard keys (called vindexes) to route queries, and it supports resharding online. Slack, GitHub and others have written about running it. Figma's DBProxy is a smaller, purpose-built version of the same idea for Postgres.

**Notion** described a similar journey in its 2021 post on sharding Postgres, splitting its block table across 480 logical shards on 32 physical databases. It used application-level routing and a long double-write migration rather than a proxy, a useful contrast with Figma's views-first rollout.

## 10. Lessons

Several lessons from Figma's experience apply beyond Postgres:

- **Exhaust the simple options first, but plan the next step before you need it.** Scaling up, read replicas and vertical partitioning bought Figma two years. They started horizontal sharding work well before the hard limits arrived.
- **Pick shard keys from your data model's natural ownership.** Users, files and organizations made good keys because nearly all access patterns are scoped to one of them.
- **Hash monotonically increasing keys** to avoid hot shards, and accept slower range scans.
- **Make the risky change reversible.** Logical sharding behind feature flags meant the behavioral change could be rolled back in seconds, and the physical move became a routine failover.
- **Measure real queries before deciding what to support.** Shadow planning let them build exactly the SQL subset they needed instead of a general distributed database.
- **Be explicit about what you give up.** No cross-shard transactions, no global unique constraints, and scatter-gather costs are real constraints that product engineers must design around.
- **Build versus buy is a moving decision.** Figma built in-house because of its timeline and risk tolerance, while explicitly planning to revisit the choice.

## Key takeaways

- Figma scaled RDS Postgres about 100x from 2020 without switching databases, in stages: bigger instances, read replicas and PgBouncer, then vertical partitioning, then horizontal sharding.
- Vertical partitioning moved groups of tables using logical replication with reverse replication for rollback, at about 30 seconds of partial impact per move. It stopped working when single tables reached terabytes, with vacuum and IOPS limits.
- Horizontal sharding used shard keys `UserID`, `FileID` and `OrgID`, grouped tables into colos that allow joins and transactions on one key, and hashed keys to avoid hotspots.
- Logical sharding with Postgres views behind feature flags came first; physical sharding became a failover with about ten seconds of partial impact on primaries.
- DBProxy, a Go service with its own parser and planners, routes single-shard queries and scatter-gathers the rest, supporting the 90% of query shapes found through shadow planning.
- Cross-shard transactions, global unique IDs and indexes, and resharding were left as future work.

## Discussion questions

1. Figma rejected NewSQL databases partly to avoid being "the first customer to hit certain scaling issues." When would you make the opposite choice?
2. What makes `UserID`, `FileID` and `OrgID` good shard keys for Figma? Think of an app where choosing a shard key would be much harder.
3. Logical sharding through views let Figma test sharding without moving data. What kinds of problems would it catch, and what problems would only appear after physical sharding?
4. A product feature needs to update a file and its owner's usage counter atomically, but they're in different colos. How would you design around the lack of cross-shard transactions?
5. Hashing the shard key prevents hotspots but makes range scans expensive. Which of Figma's likely queries suffer from that, and how would you mitigate it?
6. Shadow planning measured real query shapes before building DBProxy. How could you apply the same technique to another migration you've seen?
7. If your main database hit its limits next year, which step in Figma's sequence would you be at now, and what would you start on today?

## Sources

- Figma Engineering, "The growing pains of database architecture" (2023), https://www.figma.com/blog/how-figma-scaled-to-multiple-databases/
- Figma Engineering, "How Figma's databases team lived to tell the scale" (2024), https://www.figma.com/blog/how-figmas-databases-team-lived-to-tell-the-scale/
- Vitess documentation, "Architecture," "VTGate," "Vindexes" and "Resharding," https://vitess.io/docs/
- Notion Engineering, "Herding elephants: Lessons learned from sharding Postgres at Notion" (2021).
- Citus documentation, "Distributed tables and colocation," https://docs.citusdata.com
- PostgreSQL documentation, "Logical Replication," https://www.postgresql.org/docs/current/logical-replication.html
- Martin Kleppmann, *Designing Data-Intensive Applications* (O'Reilly, 2017), chapter 6 "Partitioning."

The Figma figures and quotes come from the two Figma posts, read on Nov 10, 2026. The fetched pages didn't show publication dates, so the years above are as I recall them. The Notion and Vitess comparisons summarize those projects' public write-ups as I know them.
