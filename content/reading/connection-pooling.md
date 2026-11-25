---
title: "Database connection pooling: why fewer connections make a faster database"
date: 2026-11-25
summary: Why database connections are expensive, why more of them usually makes things slower, and how to size and layer connection pools. Covers what a Postgres connection costs, contention and Little's law, the HikariCP sizing formula and the Oracle 2048-to-96 example, application pools vs external poolers, PgBouncer's session/transaction/statement modes and their caveats (including prepared statements), serverless and Kubernetes connection storms, timeouts and leaks, and a step-by-step sizing method.
tags: databases, distributed systems
daily: true
---

When a database gets slow under load, the instinct is to allow more connections so more work can happen at once. It usually makes things worse. A database server has a fixed number of CPU cores and disks; past a certain point, extra concurrent queries don't add throughput, they add contention for locks, CPU caches and memory, and every query takes longer. Meanwhile each connection has its own cost, especially in Postgres, where every connection is a separate operating system process. The well-known HikariCP write-up describes an Oracle performance demonstration where cutting a pool from 2,048 connections to 96 reduced response times from around 100 ms to around 2 ms. Getting connection management right means understanding why that happens, sizing pools from the database's capacity rather than the application's wishes, and adding the right pooling layer when there are many application instances. This reading covers all of it, using Postgres as the main example. It draws on the HikariCP pool-sizing guide, the PgBouncer documentation, Andres Freund's analysis of Postgres connection scalability, and the Postgres documentation. The earlier Figma case study showed pooling as one of the first scaling steps for a growing Postgres deployment.

## 1. What a connection costs

Opening a database connection isn't free, and keeping one open isn't free either.

**Opening a connection** involves a TCP handshake, usually a TLS handshake, authentication (SCRAM password exchange or certificate checks), and session setup. In Postgres, the postmaster process also **forks a new backend process** for every connection. Altogether that's typically a few milliseconds, sometimes tens of milliseconds with TLS across zones, which is often longer than the query itself. Opening a fresh connection per request is therefore a classic performance bug.

**Keeping a connection open** costs memory and CPU on the server:

- Each Postgres backend is a process with its own memory: catalog and relation caches, prepared statement plans, and per-query working memory (`work_mem` for each sort or hash operation, which can be allocated several times per query).
- Even idle connections had a real CPU cost in older versions. Postgres takes a **snapshot** at the start of each statement to decide which row versions are visible, and computing it used to scale with the number of connections. Andres Freund's 2020 analysis showed that throughput with a single active connection dropped sharply when thousands of *idle* connections were open. Postgres 14 reworked snapshot computation so this cost mostly disappeared; his workstation benchmark with 10,000 idle connections roughly doubled throughput (from about 16,000 to 33,000 transactions per second). Even so, many connections still cost memory, and many *active* connections still contend.

Postgres's default `max_connections` is 100. Raising it to thousands is possible but usually the wrong fix, for the reasons in the next section.

MySQL uses a thread per connection rather than a process, which is cheaper, and MySQL Enterprise and MariaDB offer thread pools, but the contention argument below applies to every database.

## 2. Why more connections can mean less throughput

Picture a database server with 16 cores. At any instant it can run 16 query threads on CPU (more if some are waiting on disk or locks). If 16 queries are running, each gets a full core. If 400 queries are running, the operating system time-slices them: each gets a fraction of a core, constantly context-switching, evicting each other's data from CPU caches. Total throughput doesn't rise; it often falls, because of:

- **Context switching and cache misses.** More runnable processes than cores means time spent switching instead of working.
- **Lock contention.** More concurrent transactions mean more waits on row locks, and on internal locks (buffer mappings, WAL insertion, lock manager partitions), which can collapse throughput at high concurrency.
- **Memory pressure.** Hundreds of active queries each using `work_mem` for sorts and hashes can push the server into swap or reduce the OS page cache, slowing everything.

**Little's law** helps reason about this: the number of requests in a system equals throughput times average time in the system (L = λW). If the database can complete 2,000 queries per second at 5 ms each, then about 10 queries are in progress at any moment. Allowing 500 concurrent queries doesn't make the database complete more than 2,000 per second; it just means the extra 490 are waiting somewhere. The only question is **where they wait**. Waiting inside the database (as contending processes) slows the work in progress. Waiting in a queue in front of the database (a pool) costs almost nothing and keeps the database at its efficient operating point.

This is the key insight behind connection pooling: **a pool is a concurrency limiter with a queue in front of the database.**

## 3. How big should a pool be?

The HikariCP project's "About Pool Sizing" page popularised a starting formula, originally from the Postgres community:

> connections = (core_count × 2) + effective_spindle_count

`core_count` is the number of CPU cores on the **database server** (not the application), and `effective_spindle_count` approximates how many concurrent disk I/O operations the storage can usefully serve (zero if the working set is fully cached; on SSDs, a modest number). For a 16-core database with fast storage, that suggests on the order of 32 to 40 connections actively running queries.

The same page describes an Oracle Real-World Performance demonstration in which reducing a connection pool from **2,048 to 96** connections cut response times from around **100 ms to around 2 ms**, more than 50 times faster, with no other change.

Treat the formula as a starting point, then test:

- **Long waits outside the CPU** (slow disks, network storage, lock waits, queries that call out to extensions) justify more connections, because each one spends time not using a core.
- **Short, CPU-bound queries** need fewer.
- **The limit is the total across all clients.** If you have 20 application instances, each with a pool of 20, the database sees 400 connections. The formula applies to the total the database sees, which is where things usually go wrong.

A pool that's "too small" shows up as requests waiting for a connection while the database has spare CPU. A pool that's too large shows up as a busy database with rising query latency and little gain in throughput. Measure both sides.

## 4. Application-side pools

Most applications use an in-process pool: HikariCP for Java, the pool in `database/sql` for Go, SQLAlchemy's pool for Python, `pg-pool` for Node.js, and so on. They all work similarly:

- Keep a set of open connections (bounded by a **maximum** size, often with a **minimum idle** count).
- Hand a connection to code that needs one; if none is free, make the caller **wait**, up to a **timeout**.
- Validate connections (a quick check or relying on driver-level keepalives) and replace broken ones.
- Close connections after a **maximum lifetime**, so they're periodically refreshed. This matters behind load balancers and proxies that silently drop long-lived idle connections, and after database failovers.

Settings worth getting right:

- **Maximum pool size:** derived from the database's capacity divided by the number of application instances, not from the application's thread count.
- **Connection acquisition timeout:** how long a request waits for a connection before failing. Keep it short (hundreds of milliseconds to a few seconds) so that overload produces fast errors instead of piled-up requests, which the resilience reading covered as load shedding.
- **Maximum lifetime:** shorter than any network idle timeout between the application and the database, and slightly randomised so connections don't all expire at once.
- **Minimum idle:** often best set equal to the maximum (a fixed-size pool), avoiding the latency of opening connections under sudden load.
- **Leak detection:** log a warning if code holds a connection longer than a threshold. Leaked connections (borrowed and never returned, often in error paths) are a common cause of pool exhaustion.

Holding a connection for a long time without using it, such as while making an HTTP call to another service in the middle of a transaction, is another common anti-pattern. It ties up a scarce resource and holds database locks while waiting on something else.

## 5. When application pools aren't enough: external poolers

Application pools work well with a handful of long-lived instances. They struggle when:

- **There are many instances.** Fifty Kubernetes pods with a pool of 20 each means 1,000 connections, even if each pod is mostly idle. Autoscaling makes it worse: scaling out adds connections exactly when the database is busiest.
- **Instances are short-lived.** Serverless functions (AWS Lambda, Cloud Functions) and per-request processes can't keep a meaningful pool, and each cold start opens new connections. A traffic spike can open thousands of connections in seconds, a **connection storm**.
- **Many applications share one database**, each sizing its pool independently.

The answer is an **external pooler** between the applications and the database. It accepts a large number of cheap client connections and multiplexes them over a small number of real database connections.

<!--diagram:conn-pool-1125-->

Options for Postgres include **PgBouncer** (the long-standing, lightweight standard), **PgCat** and **Supavisor** (newer, with sharding or multi-tenant features), **Odyssey**, and managed proxies such as **Amazon RDS Proxy**, plus poolers built into managed Postgres services. Many teams use both layers: a small application pool per instance, connected to an external pooler, which holds the database-side limit.

## 6. PgBouncer's pooling modes

PgBouncer's most important setting is `pool_mode`, which decides when a server connection is returned to the pool for another client:

- **Session mode** (the default): a server connection is assigned to a client for the whole client connection and returned when the client disconnects. It's fully compatible with all Postgres features, but it only helps if clients connect and disconnect frequently; long-lived clients each hold a server connection, just as without a pooler.
- **Transaction mode:** a server connection is assigned only for the duration of a **transaction** and returned when it commits or rolls back. Between transactions, the client holds nothing. This is the mode that delivers large multiplexing ratios (thousands of clients over dozens of server connections), and it's what most deployments use.
- **Statement mode:** returned after every statement; multi-statement transactions aren't allowed. Rarely used, mostly for simple autocommit workloads.

### The transaction-mode caveats

Because consecutive transactions from one client may run on different server connections, anything that relies on **session state** breaks or behaves unexpectedly in transaction mode:

- **`SET` of session parameters** (search path, time zone, statement timeout) applies to whichever server connection ran it, then leaks to the next client that gets that connection. Use `SET LOCAL` inside a transaction, or set parameters per role or per database instead.
- **Session-level advisory locks** (covered in the distributed locks reading) are tied to a server connection, so a lock taken in one transaction may be "held" by a connection the client no longer has. Use transaction-level advisory locks.
- **`LISTEN`/`NOTIFY`** needs a persistent session and doesn't work through transaction pooling.
- **Temporary tables** and **cursors held across transactions** don't survive.
- **Protocol-level prepared statements** historically broke, because a statement prepared on one server connection didn't exist on the next. Many drivers prepare statements automatically, so this was the most common surprise. **PgBouncer 1.21 (October 2023)** added support for prepared statements in transaction and statement modes, tracking them with `max_prepared_statements` and re-preparing them transparently on whichever server connection a client gets; newer releases enable this by default (200 statements). With older PgBouncer versions, drivers had to be configured to disable server-side prepared statements.

Other useful settings: `default_pool_size` (server connections per user/database pair, 20 by default), `max_client_conn` (client connections allowed, 100 by default, usually raised to thousands), `reserve_pool_size` for bursts, and `server_lifetime` / `server_idle_timeout` to recycle server connections.

A single PgBouncer process is single-threaded, so very high-throughput deployments run several instances (or use `so_reuseport` to share a port), or use multi-threaded alternatives. The pooler also becomes a component in the critical path: it needs health checks, redundancy and monitoring like any other.

## 7. Connection storms, failovers and serverless

Pools behave interestingly at the edges:

- **Deployments and restarts.** Every new application instance opens its pool at start-up. Rolling out 100 pods at once can open thousands of connections in a few seconds. Staggered rollouts, lazy pool initialisation, or an external pooler absorb this.
- **Database failover.** When the primary changes (the replication and failover reading covered this), every pooled connection to the old primary breaks at once. Pools must detect dead connections (validation, TCP keepalives, maximum lifetime) and reconnect, ideally with backoff and jitter so they don't stampede the new primary. DNS-based failover also requires that pools and drivers re-resolve the hostname rather than caching the old address.
- **Serverless.** Functions should connect through a pooler or proxy (RDS Proxy, Supavisor, a managed Postgres's built-in pooler), or use an HTTP-based database API where the provider offers one, rather than opening direct connections from each invocation.
- **Retry amplification.** If the pool is exhausted and callers retry immediately, demand rises exactly when capacity is exhausted. Fail fast on acquisition timeout, return errors upstream, and let higher layers shed load.

## 8. Observing pools

Useful metrics, from both sides:

- **Application pool:** active connections, idle connections, **threads waiting for a connection**, acquisition time (p50/p99), timeouts, connections created and closed per minute. Rising wait time with a busy database means the database is the bottleneck; rising wait time with an idle database means the pool is too small or connections are leaking or held too long.
- **PgBouncer:** `SHOW POOLS` and `SHOW STATS` report waiting clients (`cl_waiting`), active and idle server connections, and the longest wait (`maxwait`).
- **Postgres:** `pg_stat_activity` shows each connection's state. Many connections **"idle in transaction"** point to code that opens a transaction and then does slow work (or forgets to commit); these hold locks and block vacuum. `idle_in_transaction_session_timeout` limits the damage.

## 9. A sizing method

Putting it together:

1. **Start from the database.** Estimate how many concurrently *active* queries the server handles efficiently: around two per core, adjusted for I/O waits. Load-test to find where throughput stops rising and latency starts to climb.
2. **Reserve headroom** for administration, migrations, replication, monitoring and background jobs.
3. **Divide the remainder** among the clients that need it. With many application instances, put an external pooler in transaction mode between them and the database, and set its server-side pool to the number from step 1.
4. **Keep application pools small** (often 5 to 20 per instance) with short acquisition timeouts, so overload produces fast, visible failures instead of queues inside the database.
5. **Set lifetimes and timeouts:** maximum connection lifetime below network idle timeouts, `statement_timeout` for runaway queries, and `idle_in_transaction_session_timeout`.
6. **Re-check after changes** in instance counts, autoscaling limits, or database size. A pool configuration that was fine at 10 pods can overload the database at 100.

## Key takeaways

- A database connection is expensive to open (handshakes, auth, a forked process in Postgres) and costs memory and CPU to keep. Postgres 14 greatly reduced the cost of many *idle* connections, but not of many *active* ones.
- Beyond roughly a few active queries per core, more concurrency adds **contention, not throughput**. By **Little's law**, the extra requests just wait somewhere; it's far better that they wait in a pool than inside the database.
- A starting pool size is **(cores × 2) + effective spindles**, for the total the database sees. Oracle's demo showed **2,048 → 96 connections** improving response times from ~100 ms to ~2 ms.
- Application pools need small maximums, **short acquisition timeouts**, maximum lifetimes and leak detection.
- With many instances or serverless functions, use an **external pooler**. **PgBouncer transaction mode** gives large multiplexing ratios but breaks session state (`SET`, session advisory locks, `LISTEN`, temp tables); prepared statements are supported since **PgBouncer 1.21**.
- Watch for **connection storms** on deploys and failovers, and for **idle-in-transaction** sessions holding locks.

## Discussion questions

1. Your service runs 30 pods with a pool of 25 each against a 16-core Postgres server. Calculate the potential connections and propose a better configuration.
2. During a traffic spike, request latency jumps while database CPU is only at 40%. What would you check first: the pool, the database, or something else?
3. Which features of your applications would break if you put PgBouncer in transaction mode in front of the database tomorrow? How would you find out before trying?
4. An engineer proposes raising `max_connections` from 200 to 2,000 to fix "too many connections" errors. What would you say, and what would you do instead?
5. How would you handle database connections from a serverless function that can scale to 1,000 concurrent invocations?
6. After a database failover, your application takes 10 minutes to recover. Which pool and driver settings might be responsible?

## Sources

- Brett Wooldridge, [HikariCP wiki: "About Pool Sizing"](https://github.com/brettwooldridge/HikariCP/wiki/About-Pool-Sizing) (sizing formula, Oracle Real-World Performance demonstration)
- [PgBouncer configuration](https://www.pgbouncer.org/config.html) (pool modes, default_pool_size, max_client_conn, max_prepared_statements) and [changelog](https://www.pgbouncer.org/changelog.html) (1.21.0, October 2023, prepared statement support)
- Andres Freund, ["Improving Postgres Connection Scalability: Snapshots"](https://techcommunity.microsoft.com/blog/adforpostgresql/improving-postgres-connection-scalability-snapshots/1806462), October 2020 (shipped in Postgres 14)
- [PostgreSQL documentation: Connections and Authentication](https://www.postgresql.org/docs/current/runtime-config-connection.html) (max_connections), `pg_stat_activity`, `idle_in_transaction_session_timeout`, `statement_timeout`
- J. D. C. Little, "A Proof for the Queuing Formula: L = λW," Operations Research, 1961
- Amazon RDS Proxy documentation; Supavisor and PgCat project documentation
- Figma engineering, "The growing pains of database architecture" (2023), on PgBouncer in Figma's scaling journey

*Note on verification: HikariCP's formula and Oracle example, PgBouncer's modes, defaults and prepared-statement history, Andres Freund's results and Postgres's default max_connections were read from the linked pages today. Connection-cost figures (milliseconds to open), MySQL threading details, driver and pool setting names, and serverless guidance come from documentation as I know it and weren't re-fetched.*
