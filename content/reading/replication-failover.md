---
title: "Database replication and failover: what you lose, and when"
date: 2026-11-21
summary: How single-leader databases like Postgres and MySQL replicate and fail over, and the data-loss and split-brain risks behind each choice. Covers physical vs logical replication, asynchronous, synchronous and quorum commit, replication lag and read-your-writes, failure detection, automated failover with Patroni and Orchestrator, fencing and split brain, rejoining an old primary with pg_rewind, GitHub's 2018 cross-region failover incident, and how managed services like Aurora change the picture.
tags: databases, distributed systems
daily: true
---

Almost every production relational database runs as a primary with one or more replicas. Replicas serve reads, provide a hot spare, and feed analytics and backups. The setup looks routine until the primary fails, and then a set of questions that were easy to ignore become urgent. Was every acknowledged write on a replica when the primary died? Which replica should take over, and who decides? What if the old primary isn't dead, just unreachable, and keeps accepting writes? How do you bring it back without corrupting data? Getting these answers wrong leads to lost transactions, split brain, or a day-long incident like GitHub's in 2018. This reading explains how replication works in Postgres and MySQL, the durability trade-off between asynchronous, synchronous and quorum commit, how automated failover tools decide when and where to fail over, why fencing matters, and what managed services do differently. It draws on the PostgreSQL and Patroni documentation, GitHub's post-incident analysis, *Designing Data-Intensive Applications*, and AWS's Aurora paper. The earlier readings on WAL and crash recovery, Raft, and distributed locks are useful background.

## 1. What replication copies

A single-leader database replicates by shipping its changes from the primary to replicas. There are two broad ways to describe those changes.

**Physical replication** ships the write-ahead log itself: byte-level changes to data pages. In Postgres this is **streaming replication**: each standby connects to the primary, receives WAL records as they're generated, writes them to its own WAL, and replays them, so the standby is a block-for-block copy of the primary. It replicates everything (all databases, indexes, schema changes) with very little overhead, and a standby can serve read-only queries while it replays (**hot standby**). The limitations are that replicas must run the same major version and architecture, you can't replicate a subset of tables, and you can't write to the replica.

**Logical replication** ships row-level changes: "insert this row into table T", "update the row with primary key 42". Postgres's built-in logical replication (publications and subscriptions) and MySQL's row-based binary log both work this way. Logical replication can go across major versions (which makes it a common upgrade path), replicate selected tables, and feed other systems through change data capture (Debezium and similar). It's more expensive, doesn't replicate schema changes automatically in Postgres, and needs care with sequences and conflicts.

MySQL's traditional replication is logical (binary log events, now usually row-based) rather than physical, and MySQL with GTIDs (global transaction identifiers) makes it easier to know exactly which transactions each replica has applied, which matters a lot during failover.

For high availability, the important question isn't the format; it's **when the primary considers a write committed relative to when replicas have it.**

## 2. Asynchronous, synchronous and quorum commit

<!--diagram:replication-commit-1121-->

### Asynchronous replication

By default, both Postgres and MySQL replicate **asynchronously**. The primary flushes the commit to its own WAL (or binlog), acknowledges the client, and ships the change to replicas in the background. Replicas usually lag by milliseconds, but the lag can grow to seconds or minutes under heavy write load, long-running queries on the replica, or network trouble.

The consequence: if the primary fails permanently, any writes it acknowledged but hadn't yet shipped are **lost** when a replica is promoted. The amount you can lose is the replication lag at the moment of failure. This is the **recovery point objective (RPO)** of asynchronous replication: usually small, never guaranteed to be zero.

### Synchronous replication

With **synchronous replication**, the primary waits for at least one standby to confirm it has the commit before acknowledging the client. In Postgres this is controlled by `synchronous_standby_names` together with `synchronous_commit`:

- `synchronous_commit = on` (with standbys named): wait until the standby has **flushed** the WAL to disk.
- `remote_write`: wait until the standby has written it to its OS (survives a standby Postgres crash, not a standby OS crash).
- `remote_apply`: wait until the standby has **replayed** it, so a read on that standby immediately after commit sees the write.

The cost is latency (every commit waits at least one network round trip, which matters a lot across regions) and availability: if the synchronous standby is down or unreachable, commits **block**. A two-node setup with one synchronous standby therefore has *worse* write availability than a single node, unless something changes the rule when the standby fails.

### Priority and quorum

Postgres lets you name several standbys and choose how many must confirm:

- `FIRST 1 (s1, s2, s3)` is **priority-based**: wait for the highest-priority available standby (s1, or s2 if s1 is down, and so on).
- `ANY 2 (s1, s2, s3)` is **quorum-based**: wait for any two of the three to confirm.

Quorum commit is the sweet spot for many setups: with three standbys and `ANY 1`, every commit is on at least two machines, and any single standby can fail without blocking writes. MySQL's equivalent is **semi-synchronous replication**, where the primary waits for at least a configured number of replicas to acknowledge receipt of the transaction before returning to the client, and falls back to asynchronous after a timeout unless configured otherwise. That fallback is a durability gap worth knowing about: under a network problem, a semi-sync setup can quietly become async.

### What "zero data loss" really requires

Synchronous replication only guarantees zero loss if failover promotes a replica that **has** the synchronous writes. Promote the wrong replica (one that was behind) and the guarantee is gone. The replication mode and the failover logic have to agree, which is why tools like Patroni manage both.

## 3. Replication lag and reading from replicas

Reading from replicas spreads load, but with asynchronous replication, reads can be stale. The classic anomalies (DDIA chapter 5 describes them well):

- **Read-your-writes violations.** A user updates their profile, the next page load reads from a lagging replica, and their change appears to be gone.
- **Monotonic read violations.** Two successive reads go to different replicas with different lag, so data appears to move backwards in time.
- **Consistent prefix violations.** Related writes (a question and its answer) appear in the wrong order when they come from different partitions or replicas.

Common mitigations:

- **Read your own writes from the primary** for a short window after a user writes (for example, route that user's reads to the primary for a few seconds, or for data they own).
- **Track positions.** After a write, record the primary's WAL position (LSN in Postgres, GTID in MySQL) in the session; route reads only to replicas that have replayed past it, or wait until they have. Postgres's `remote_apply` does this at commit time for synchronous standbys.
- **Sticky routing**: send all of a user's reads to the same replica to avoid going backwards.
- **Monitor lag** (`pg_stat_replication` on the primary shows each standby's sent, written, flushed and replayed LSN; `replay_lag` gives a time estimate) and take replicas out of rotation when they fall behind a threshold.

A replica running long analytical queries can also slow replication: Postgres must either pause replay (delaying the replica) or cancel the query when replay conflicts with rows it's reading (`max_standby_streaming_delay`), or prevent the conflict by having the primary keep old rows (`hot_standby_feedback`), which can cause table bloat on the primary. Separate replicas for OLTP reads and for analytics avoid fighting over these settings.

## 4. Failover: the hard part

Failover means promoting a replica to primary and redirecting writes to it. Doing it manually is slow (minutes to hours, often at 3 a.m.); doing it automatically requires answering three questions correctly.

### Is the primary actually dead?

A primary that doesn't respond to health checks may be crashed, overloaded, paused, or simply unreachable from the monitor while working perfectly for clients. Failure detection in distributed systems is fundamentally imperfect (the timeouts discussion in the resilience reading applies). Practical rules:

- **Check from several vantage points.** A monitor in one location can be partitioned from a healthy primary. Tools like Orchestrator (for MySQL) check whether the **replicas** can still see the primary, not only whether the monitor can: if replicas are still receiving replication traffic, the primary is alive.
- **Tune timeouts to your tolerance.** Fast detection means more false failovers; a failover on a healthy primary is itself an incident.

### Which replica should take over?

The best candidate is the one with the most data: the highest replayed (or received) position. Promoting a lagging replica loses everything it hadn't received. With synchronous or quorum replication, the candidate must be a replica known to hold all acknowledged commits. Tools also consider configured priorities, data centre placement, and replica health. Patroni has `maximum_lag_on_failover`, the largest replication lag (in bytes) a replica may have and still be eligible.

### How do you stop the old primary?

This is the question that turns failover from routine into catastrophic. If the old primary is still running and some clients can still reach it, you now have **two primaries accepting writes**: split brain. Their data diverges, and reconciling it later is manual, slow and error-prone.

The defences, which echo the distributed locks reading:

- **Fencing.** Make sure the old primary can't accept writes before the new one does. Options include shutting it down or cutting its power (STONITH, "shoot the other node in the head"), revoking its network access, or having it demote itself.
- **Leases.** The primary holds a leader lease in a consensus store and demotes itself to read-only if it can't renew in time. As the locks reading explained, a paused process can't notice that its lease expired, so this works best combined with fencing at the routing layer.
- **Single routing authority.** Clients find the primary through one mechanism (a proxy, a DNS name, a virtual IP, or a service discovery entry) that's updated as part of failover, so the old primary stops receiving traffic even if it's alive.

## 5. Patroni: failover with a consensus store

Patroni is the most widely used automated failover manager for Postgres. Its design is a good template for any leader-based system.

- Each Postgres node runs a **Patroni agent** that manages the local Postgres (start, stop, promote, demote, reconfigure).
- The agents coordinate through a **distributed configuration store (DCS)**: etcd, Consul, ZooKeeper or the Kubernetes API, all backed by consensus.
- The primary's agent holds a **leader key** in the DCS with a time to live (`ttl`, 30 seconds by default) and renews it every `loop_wait` (10 seconds by default). Patroni requires `loop_wait + 2 × retry_timeout ≤ ttl`, so the leader gives up before its key can expire while it's still retrying.
- If the primary's agent **can't renew** the key (it lost contact with the DCS), it **demotes** its own Postgres to a read-only replica. That's self-fencing: a primary that's cut off from the consensus store stops accepting writes.
- If the leader key **expires**, the healthy replicas race to acquire it. Only eligible replicas may try: ones within `maximum_lag_on_failover`, and in synchronous mode only the last known leader or a synchronous replica. The winner promotes itself.
- Clients find the primary through HAProxy or PgBouncer checking Patroni's REST API, through a Kubernetes service pointed at the leader pod, or through a callback that updates DNS or a virtual IP.

Patroni's **`synchronous_mode`** ties replication to failover. Patroni manages `synchronous_standby_names` itself, choosing healthy replicas as synchronous standbys, and only a synchronous replica may be promoted. That gives zero loss of acknowledged commits at failover, at the cost of write availability when no synchronous replica is healthy (Patroni can be configured either to keep blocking or to fall back). Newer versions also support a **quorum** mode based on `ANY k`.

The DCS is what makes split brain unlikely: leadership is a single key in a linearisable store, and a primary that can't see the store demotes itself. The remaining risk is the lease timing window, and the old primary still needs to be kept away from clients by the routing layer.

## 6. GitHub, October 2018: when failover is correct and still wrong

GitHub's post-incident analysis of 21 October 2018 is one of the most instructive published failover incidents.

During routine maintenance replacing failing optical equipment, the connection between GitHub's US East Coast network hub and its primary East Coast data centre was lost. Connectivity was restored in **43 seconds**. In that window, GitHub's MySQL failover tool, Orchestrator (which itself uses Raft for its own leadership), behaved exactly as designed: the Orchestrator nodes in the US West Coast data centre and in a public cloud region formed a quorum, judged the East Coast primaries unreachable, and **failed over database clusters to primaries on the West Coast**.

When connectivity returned:

- The **West Coast primaries** were now taking writes, while the East Coast primaries held **a few seconds of writes** that had never replicated west (one of the busiest clusters had 954 writes in the affected window).
- The application tier, which was mostly on the East Coast, now had to cross the continent for every database write. The added latency made many applications unable to function.
- **Failing back** wasn't safe: both sides had writes the other lacked.

GitHub chose to prioritise data integrity: they deliberately degraded the site, restored the East Coast databases from backups, replayed the West Coast writes to catch up, and only then failed back, reconciling the orphaned East Coast writes manually afterwards. The site was degraded for **24 hours and 11 minutes**.

Lessons GitHub and others drew:

- **A correct failover can be the wrong decision.** Failing over across regions for a 43-second blip turned a brief partition into a day-long incident. Many teams now require cross-region failover to be a deliberate, human-approved action, or allow automated failover only within a region.
- **Topology must match application placement.** A database primary far from its applications can be as unavailable as a dead one.
- **Asynchronous replication plus automatic failover implies possible data loss.** If you choose that combination, plan how to find and reconcile orphaned writes.
- **Restores take time at scale.** Restoring multi-terabyte databases took hours, which dominated the incident's length. Backup restore speed is part of your failover plan.

## 7. Bringing the old primary back

After a failover, the old primary may have **diverged**: it applied writes (or at least WAL) that the new primary never received. It can't simply start replicating from the new primary, because its history forked.

Options in Postgres:

- **Rebuild it from scratch** with a fresh base backup (`pg_basebackup`) from the new primary. Simple and safe, but slow for large databases.
- **`pg_rewind`** compares the old primary's data directory with the new primary's, finds the point where their timelines diverged, and copies back only the blocks that changed since then, making the old primary a valid replica of the new one much faster. It requires `wal_log_hints` or data checksums to be enabled, which is worth turning on in advance.
- **Timelines** help here: each promotion starts a new Postgres timeline ID, and WAL files record which timeline they belong to, so a replica can follow the history across failovers and the system can detect forks.

Patroni can run `pg_rewind` automatically when a former primary rejoins. Whatever the method, the writes that existed only on the old primary are discarded from the database. If they mattered, they need to be extracted and reconciled before the rewind, which is why capturing them (from the old WAL or logs) is part of a careful runbook.

## 8. How managed and cloud-native databases change things

Managed services hide much of this, but the trade-offs don't disappear.

- **Amazon RDS Multi-AZ** (classic) keeps a synchronous standby in another Availability Zone using storage-level replication, and fails over automatically, typically in one to two minutes, by updating the instance's DNS name. The standby isn't readable; it exists only for failover.
- **Amazon Aurora** separates compute from storage. Its SIGMOD 2017 paper describes a storage layer that keeps six copies of the data across three Availability Zones and acknowledges a write once four of the six have it. Replicas share the same storage volume, so they don't replay a separate copy of the data, and replication lag is typically short. Failover promotes a replica that attaches to the same volume, with no data loss for acknowledged writes, and is usually faster than with separate storage.
- **Distributed SQL databases** (Spanner, CockroachDB, YugabyteDB) replicate every range with consensus (the Raft and Spanner readings covered this), so a leader failure is handled per range automatically, without a separate failover tool, at the cost of higher write latency.

Even with these, application-level concerns remain: clients must reconnect and retry (connection pools must notice the DNS change, which many cache for too long), in-flight transactions are lost and must be retried idempotently, and read-your-writes on readable replicas is still the application's problem.

## 9. A checklist for your own setup

- **Know your RPO and RTO** for each database, in numbers, and pick the replication mode that matches. "Zero data loss" means synchronous or quorum commit and failover rules that respect it.
- **Prefer quorum commit** (`ANY k`) over a single synchronous standby, so one replica failure doesn't block writes.
- **Automate failover within a region** with a consensus-backed tool (Patroni, Orchestrator, a managed service). Make **cross-region failover** a deliberate decision unless you've tested the application's behaviour on the other side.
- **Fence the old primary** through a single routing authority and self-demotion; test that a partitioned primary stops accepting writes.
- **Monitor replication lag** and alert on it; take lagging replicas out of read rotation.
- **Handle stale reads** where users would notice: read-your-writes routing or LSN/GTID waits.
- **Enable what rejoining needs** (`wal_log_hints` or checksums for `pg_rewind`) and keep runbooks for reconciling orphaned writes.
- **Practise.** Run failovers regularly in staging and, carefully, in production. Measure how long clients take to reconnect, including DNS and connection pool caching.
- **Test restores.** Backup restore speed sets a floor on your worst-case recovery time.

## Key takeaways

- Replication ships changes **physically** (WAL blocks: whole-cluster copies, same version) or **logically** (row changes: selective, cross-version, CDC).
- **Asynchronous** commit is fast but can lose the writes in flight at failure. **Synchronous** commit waits for a standby and blocks if it's down. **Quorum** commit (`ANY k`) gets zero loss without a single point of blocking.
- **Replication lag** causes read-your-writes and monotonic-read anomalies; route by LSN/GTID or read recent writes from the primary.
- Automated failover must answer **is it dead, who's most up to date, and how do we stop the old primary**. Getting the last one wrong causes **split brain**.
- **Patroni** uses a leader key with a TTL in a consensus store, self-demotion on lost contact, lag limits, and synchronous mode to tie durability to promotion.
- **GitHub 2018**: a 43-second partition triggered a correct cross-region failover that led to 24 hours and 11 minutes of degradation. Topology, latency and orphaned writes matter as much as the failover mechanism.
- After failover, a diverged old primary must be **rewound or rebuilt**, discarding writes that only it had.

## Discussion questions

1. For your main database, what's the RPO today in practice? What would you have to change to make it zero, and what would it cost in latency and availability?
2. You run a primary and two replicas with `synchronous_standby_names = 'FIRST 1 (r1, r2)'`. Walk through what happens when r1 fails, then when both fail. How would `ANY 1 (r1, r2)` differ?
3. A user updates their settings and immediately sees the old values. Design a fix that doesn't send all reads to the primary.
4. In the GitHub incident, should Orchestrator have been allowed to fail over across regions automatically? What rule would you write instead?
5. Your monitoring host loses network access to a healthy primary for 20 seconds. What prevents an unnecessary failover in your setup? What prevents split brain if one happens anyway?
6. After a failover, the old primary had 300 writes the new one never received. How would you find them, and how would you decide what to do with them?

## Sources

- GitHub, ["October 21 post-incident analysis"](https://github.blog/news-insights/company-news/oct21-post-incident-analysis/), October 2018
- [PostgreSQL documentation: Replication settings](https://www.postgresql.org/docs/current/runtime-config-replication.html) (`synchronous_standby_names` with FIRST and ANY), High Availability, Load Balancing and Replication chapter, `pg_stat_replication`, `pg_rewind`, logical replication
- [Patroni documentation: dynamic configuration](https://patroni.readthedocs.io/en/latest/dynamic_configuration.html) (ttl, loop_wait, retry_timeout, maximum_lag_on_failover, synchronous_mode) and replication modes
- MySQL documentation: GTIDs, semi-synchronous replication; Orchestrator documentation (failure detection via replicas, Raft mode)
- Martin Kleppmann, *Designing Data-Intensive Applications* (O'Reilly, 2017), ch. 5 "Replication"
- A. Verbitski et al., "Amazon Aurora: Design Considerations for High Throughput Cloud-Native Relational Databases," SIGMOD 2017; Amazon RDS Multi-AZ documentation
- Earlier readings on WAL and crash recovery, Raft consensus, distributed locks and leases, and resilience patterns on this site

*Note on verification: the GitHub incident details, Patroni's defaults and synchronous mode, and Postgres's FIRST/ANY syntax were read from the linked pages today. MySQL semi-sync fallback behaviour, Orchestrator's detection approach, `pg_rewind` prerequisites, RDS Multi-AZ failover times and the Aurora quorum numbers come from documentation and the Aurora paper as I know them and weren't re-fetched today.*
