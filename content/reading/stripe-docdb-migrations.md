---
title: "How Stripe moves petabytes between database shards without downtime"
date: 2026-11-17
summary: A case study of Stripe's DocDB and its Data Movement Platform, which migrates live data between thousands of MongoDB-based shards with a traffic switch of under two seconds. Covers why sharded databases need online data movement, the six-step migration (register, bulk import, oplog replication, verification, versioned traffic switch, cleanup), the design choices behind each step, how it compares to Vitess, MongoDB's balancer and Figma's approach, and lessons for any team migrating data while it's being written.
tags: case study, databases
daily: true
---

Stripe processed about $1 trillion in payments in 2023 and says its databases ran at 99.999% availability while doing it. Underneath is DocDB, an internal database service built on MongoDB Community, which serves over five million queries per second across more than 2,000 shards. A fleet that size never stands still: shards get too hot or too big, others are nearly idle, MongoDB versions need upgrading, and large customers sometimes need their own hardware. All of that means moving data between shards while it's being read and written by payment traffic that can't tolerate downtime. In June 2024, Stripe's database infrastructure team published how they do it: a system called the **Data Movement Platform**. This reading walks through that design step by step, explains why each step exists, compares it with other well-known approaches (Vitess, MongoDB's own balancer, Figma's Postgres sharding, which an earlier reading covered), and pulls out the general lessons for online data migration. The main source is Stripe's post by Jimmy Morzaria and Suraj Narkhede; related sources are listed at the end.

## 1. DocDB: what Stripe built on top of MongoDB

Stripe chose MongoDB early (around 2011) for its document model and developer productivity. Managed offerings like Atlas didn't exist yet, so Stripe ran its own clusters, and over the years built a database-as-a-service layer around MongoDB Community called **DocDB**. According to the post, it now serves:

- more than **five million queries per second** from product applications,
- more than **10,000 distinct query shapes**,
- **petabytes** of data in more than **5,000 collections**,
- spread across more than **2,000 shards**.

Applications don't talk to MongoDB directly. They go through a fleet of **database proxy servers** written in Go, which parse each query, route it to the right shard (or several), merge results, and enforce reliability features such as admission control and access control. A **chunk metadata service** records which **chunk** (a contiguous range of a collection's data) lives on which shard. **Change data capture (CDC)** pipelines ship each shard's write log (the MongoDB oplog) into Kafka and archive it in object storage, where other systems consume it.

This architecture matters for migration. Because every query goes through Stripe's own proxies, and because routing comes from Stripe's own metadata service, Stripe controls the exact moment traffic for a chunk moves from one shard to another. That's the lever the whole design depends on.

Stripe's sharding also changed over time. Early on, it ran a small number of large shards, each holding tens of terabytes. Today it runs thousands of smaller ones. Smaller shards are faster to back up, restore, rebuild and upgrade, and a problem affects less data, but they also mean many more shards to manage, and a much greater need for an automatic way to move data between them.

## 2. Why online data movement is a core capability

The post lists the uses Stripe has for moving data, and they're common to any sharded system:

- **Splitting hot or large shards.** A shard that grows too large or too busy needs part of its data moved elsewhere, sometimes urgently during a traffic surge.
- **Bin-packing underused shards.** The reverse problem: thousands of shards that are mostly idle waste money. In 2023, Stripe used the platform to consolidate thousands of under-utilised databases, moving **1.5 petabytes** of data and reducing the number of DocDB shards by about **three quarters**, without visible impact on applications.
- **Upgrades.** Instead of upgrading MongoDB in place, version by version, Stripe used data movement to migrate the fleet to a newer major version **in one step**, skipping intermediate versions: build new shards on the new version and move data into them. That makes an upgrade a migration that can be verified and rolled back, rather than an in-place operation that can't.
- **Tenant isolation.** Moving a very large user onto dedicated shards (single tenancy).

If data movement is slow, risky or manual, all of these become projects that teams avoid. If it's routine, the fleet can be continuously reshaped. That's the real payoff, and it's why Stripe invested in a general platform rather than one-off migration scripts.

## 3. Requirements: what "zero downtime" has to mean

The post frames the requirements roughly as follows, and they make a good checklist for any migration system:

- **Correctness and completeness.** Every document must end up on the target, identical to the source, including writes that happen during the migration.
- **Availability.** Applications must keep reading and writing throughout. Any pause must be shorter than events the applications already tolerate. Stripe's benchmark is a planned primary failover in a replica set, which typically takes a few seconds.
- **Granularity.** Move any chunk or set of chunks, not only whole databases, so splits and merges are possible.
- **No impact on the source.** A migration must not slow down the shard it's draining, which is often a shard that's already overloaded, which is the reason for moving data off it.
- **Reversibility.** If the target misbehaves after the switch, be able to switch back.

These requirements rule out the simplest approaches. Stop-copy-restart violates availability. Having applications dual-write to both locations puts correctness in the hands of every application team and is hard to make atomic. Reading the source heavily to copy it violates "no impact on the source."

## 4. The six steps

A **coordinator** service orchestrates each migration through six steps.

<!--diagram:stripe-dmp-1117-->

### Step 1: Register the migration

The coordinator records the intent to move specific chunks from a source shard to a target shard in the chunk metadata service, and builds the needed **indexes on the target** before any data arrives. Registering first means every other component (proxies, CDC consumers, other migrations) can see that the chunk is in motion. Building indexes up front is cheaper than building them after a large import and means the target is ready to serve queries the moment traffic arrives.

### Step 2: Bulk import a snapshot

The coordinator takes a **point-in-time snapshot** of the chunk's data as of some time T and loads it into the target. Two details from the post are worth noting:

- **Filtering:** the import includes only documents in the chunks being moved, so a shard can be split.
- **Insertion order matters a lot.** Initially, imports were slow. The team sorted documents by the most common index attributes and inserted them in that order, which they report increased write throughput by about **10×**. The reason is that MongoDB's storage engine (WiredTiger) keeps data and indexes in B-trees. Inserting in index order appends to the right edge of the B-tree, touching the same few pages repeatedly and keeping them in cache; inserting in random order touches random leaf pages all over the tree, causing page splits, cache misses and much more I/O. It's the same reason bulk loads into any B-tree database (Postgres, MySQL, SQL Server) run faster with pre-sorted input, and the same effect the earlier reading on database indexing described for random UUID primary keys.

Using a snapshot rather than live reads from the primary keeps the import from competing with production traffic on the source.

### Step 3: Replicate changes asynchronously

While the import runs and afterwards, the source keeps taking writes. The coordinator replays every write since time T from the source to the target, using the **oplog** (MongoDB's replication log). Stripe doesn't read the oplog from the source shard directly; it uses the CDC stream that already ships oplog entries to **Kafka** and archives them in object storage. That keeps the migration from adding read load to the source, and it means the replication can catch up from the archive if it falls behind.

Two design points:

- **Replication starts from T,** the snapshot time, so snapshot plus replay is complete. Replaying operations that the snapshot already reflects must be harmless, which works because applying the same oplog entry twice produces the same state (oplog entries are written to be idempotent).
- **Replication is bidirectional.** After the traffic switch, writes to the target are replicated back to the source. Writes are tagged so that they don't loop back and forth endlessly. Bidirectional replication is what makes the switch **reversible**: until the migration is finished, the source is kept current, and traffic can be moved back.

### Step 4: Verify correctness

Before switching traffic, the coordinator compares **point-in-time snapshots** of the source and target to confirm they hold the same data. Verification is a separate, explicit step rather than an assumption that the import and replication worked. Bugs in migration tooling are usually subtle: a document type that serialises differently, a filter that's off by one chunk boundary, an oplog entry type the replayer doesn't handle. Comparing real data catches what tests don't.

### Step 5: Switch traffic with versioned gating

This is the delicate part. At some instant, the authoritative copy of the chunk has to move from source to target, with no write lost, no write applied in the wrong place, and as little disruption as possible. Stripe calls its protocol **versioned gating**:

- Every request the proxies send carries a **version token** for the chunk's routing.
- Stripe **patched MongoDB** so that a shard rejects any request whose version token isn't at least as new as the version it knows for that chunk.

The switch then goes:

1. **Bump the version on the source shard.** From this moment, the source rejects all reads and writes for the chunk that carry the old token. No new writes can land on the source.
2. **Wait for replication to drain.** The writes that landed before the bump are replicated to the target. Once replication has caught up, the target has every write.
3. **Update the routing.** The coordinator updates the chunk metadata service with the new shard and the new version token. Proxies pick up the new route and send traffic to the target.

Requests that arrive during the switch are rejected by the source and succeed when retried against the new route. The post says the entire switch takes **less than two seconds**, shorter than a planned primary failover, which applications already handle.

The version token is, in effect, a **fencing token**, the same idea from yesterday's reading on distributed locks. A proxy that hasn't yet learned the new route, or a delayed request from before the switch, can't write to the old location, because the shard itself enforces the newer version. The guarantee is enforced at the storage layer, not by hoping every proxy updates in time. This is why Stripe needed to patch MongoDB: the shard is the only component that sees requests in their real order.

### Step 6: Deregister and clean up

The coordinator marks the migration complete in the metadata service and deletes the chunk's data from the source. Deleting last keeps the rollback path open until the very end.

## 5. Why this design works: the principles behind it

Stepping back, several principles make the design robust.

**Copy, then catch up, then cut over.** Nearly every online migration system has this shape: a bulk copy of a consistent snapshot, a change-stream replay to catch up, and a short, controlled cut-over. The bulk copy does the heavy lifting off the critical path, replay makes the target converge, and the cut-over is short because only the last few writes need to drain.

**Make the cut-over shorter than events you already tolerate.** Stripe didn't aim for literally zero unavailability; it aimed for a pause shorter than a routine failover, which every client is already built to retry through. That's a much more achievable and testable target.

**Enforce ownership at the storage layer.** Routing metadata can be stale in any proxy at any time. Versioned gating means stale routes produce rejected requests (safe, retryable) rather than misplaced writes (data corruption).

**Keep the old copy until you're sure.** Bidirectional replication and deferred deletion make every migration reversible until the last step. Reversibility is what lets a team run thousands of migrations without treating each one as a high-stakes event.

**Reuse existing data pipelines.** Replicating from the CDC stream in Kafka and object storage avoids putting load on the source, and it means the migration system relies on infrastructure that is already heavily exercised.

**Verify explicitly.** Treat "the data is identical" as something to check with real comparisons, not something to infer from the absence of errors.

**Centralise orchestration.** One coordinator owns the state machine of each migration, with every step recorded in the metadata service. Migrations can be paused, resumed and audited, and multiple migrations can run without conflicting.

## 6. How others solve the same problem

The same pattern shows up in other systems, with different trade-offs.

### MongoDB's own balancer

MongoDB's sharded clusters include a **balancer** that moves chunks between shards automatically, using a similar clone-then-catch-up-then-commit process coordinated by config servers, and a final critical section where writes to the chunk are briefly blocked. Stripe's post doesn't say exactly why it built its own, but its architecture explains some of it: Stripe uses its own proxies and metadata service rather than MongoDB's `mongos` routers, so it needs its own data movement that fits that routing layer. Building its own also gave it control over pacing, verification, reversibility, cross-version moves (needed for the one-step upgrades), and reading from the CDC archive rather than the source.

### Vitess

Vitess, the sharding layer for MySQL created at YouTube and used by Slack, GitHub and others, has **VReplication**, which powers workflows like `MoveTables` and `Reshard`. It copies data, then streams binlog events to keep the target in sync, lets you verify with `VDiff`, and then switches reads and writes with `SwitchTraffic`, with a reverse replication stream so you can switch back. The shape is almost identical to Stripe's: copy, catch up, verify, switch, keep a reverse path. Vitess's routing layer (VTGate) and its topology service play the roles of Stripe's proxies and chunk metadata service.

### Figma's Postgres sharding

The earlier reading on Figma covered how it horizontally sharded Postgres with its own proxy (DBProxy) and logical replication. Figma's approach also relied on a routing layer it controlled, logical replication to keep new shards in sync, and a short, carefully choreographed cut-over, with a lot of emphasis on "shadow" reads and planning to catch problems before switching. Both companies converged on owning the proxy and the routing metadata, because that's what makes a controlled cut-over possible.

### Application-level migrations

Stripe itself described a different pattern in its 2017 post "Online migrations at scale," for moving data between *data models* (such as splitting a field into a new table) rather than between shards. That pattern has four phases: **dual-write** to old and new, **change reads** to the new location, **change writes** so only the new location is written, and **remove** the old data, with verification and backfills along the way. It's slower and requires application changes, but it can change the data's shape, which infrastructure-level movement can't. The two approaches complement each other: infrastructure moves data between machines; application migrations change what the data looks like.

## 7. Lessons for teams that aren't Stripe

Most teams don't need a platform that moves petabytes. The lessons still apply to smaller problems such as moving a table to a new database, splitting a monolith's database, or migrating to a managed service.

- **Own your routing.** If you might ever need to move data without downtime, put a layer between applications and the database that controls where queries go: a proxy, a data-access library, or at minimum a configuration switch for connection targets. Without it, every cut-over needs a coordinated deploy of every client.
- **Get a change stream.** Postgres logical replication, MySQL binlog, MongoDB change streams and DynamoDB Streams all let you follow writes. CDC tools such as Debezium package this. The snapshot-plus-replay pattern only works if you can replay changes from the snapshot point.
- **Design a cut-over that fails safe.** The worst outcome of a migration isn't a few seconds of errors; it's writes split between two databases with neither one complete. Block writes on the old location (a read-only flag, revoked permissions, or a version check) before routing writes to the new one.
- **Keep a way back.** Reverse replication, or at least keeping the source intact and a way to replay writes back, turns a one-way door into a two-way one.
- **Verify with data.** Row counts are a start; checksums of key ranges or sampled document comparisons are better.
- **Measure the pause and compare it with what clients already handle.** If clients retry through a few-second failover, design the cut-over to fit inside that.
- **Sort your bulk loads.** The 10× from inserting in index order is the kind of improvement that's easy to miss and applies to any B-tree store.

## 8. Open questions and limitations

The post leaves some things out, which are worth considering when you adapt the design:

- **Multi-chunk transactions.** If a transaction touches documents in a chunk being moved and another that isn't, the brief rejection window affects it as a whole. The post doesn't go into how cross-chunk operations are handled during the switch.
- **Replication lag under load.** The cut-over waits for replication to drain. If the source is very busy, lag could stretch the pause. Presumably the coordinator only starts the switch when lag is small, but the post doesn't spell out the thresholds.
- **Operational load of thousands of shards.** Smaller shards make each migration easier but add many more moving parts. Stripe mentions future work on a **heat management** system and **shard autoscaling**, which suggests the next step is deciding automatically *when* and *where* to move data, not just how.
- **Patching the database.** Versioned gating needed a custom MongoDB patch. That's a maintenance cost every upgrade has to carry, and teams without database engineers on staff may need to find the equivalent in their proxy layer instead, at the cost of a weaker guarantee.

## Key takeaways

- Stripe's **DocDB** serves 5M+ QPS from 2,000+ MongoDB-based shards through its own **proxies** and **chunk metadata service**, which gives it full control of routing.
- The **Data Movement Platform** migrates chunks in six steps: **register, bulk import a snapshot, replicate the oplog via Kafka, verify, switch traffic, deregister**.
- The **versioned gating** switch uses a version token enforced by a patched MongoDB as a **fencing token**: stale routes get rejected, not misapplied. The switch takes **under two seconds**, less than a routine failover.
- **Bidirectional replication** and deferred deletion keep every migration **reversible** until the end.
- **Inserting in index order** sped up bulk imports about **10×**, a general B-tree lesson.
- Data movement as a routine capability enabled **consolidating shards by about three quarters (1.5 PB moved)** and **one-step major-version upgrades**.
- Vitess, MongoDB's balancer and Figma converged on the same shape: **copy, catch up, verify, short fenced cut-over, keep a way back**.

## Discussion questions

1. Stripe aimed for a cut-over shorter than a planned failover rather than literally zero errors. What is the equivalent "already tolerated" event in your systems, and how long is it?
2. Why does versioned gating need to be enforced by the shard rather than by the proxies? What could go wrong if only proxies checked the version?
3. You need to move one large Postgres table to a new database with no downtime, and you don't have a proxy layer. Sketch a plan using logical replication. Where's the riskiest moment, and how would you make it fail safe?
4. When would you choose the application-level dual-write pattern (Stripe's 2017 post) over infrastructure-level replication, and vice versa?
5. Stripe upgraded MongoDB versions by migrating data to new shards instead of upgrading in place. What does that approach cost, and what does it buy?
6. Thousands of small shards versus a few large ones: list the operational trade-offs. At what point would you need automated heat management?

## Sources

- Jimmy Morzaria, Suraj Narkhede, ["How Stripe's document databases supported 99.999% uptime with zero-downtime data migrations"](https://stripe.dev/blog/how-stripes-document-databases-supported-99.999-uptime-with-zero-downtime-data-migrations), Stripe, June 2024
- Jacqueline Xu, "Online migrations at scale," Stripe blog, 2017
- MongoDB documentation: sharded cluster balancer, chunk migration, oplog and change streams, WiredTiger storage engine
- Vitess documentation: VReplication, MoveTables, Reshard, VDiff and SwitchTraffic
- Figma engineering, "How Figma's databases team lived to tell the scale" (2024), and the earlier reading on Figma's Postgres sharding
- Martin Kleppmann, *Designing Data-Intensive Applications*, ch. 6 "Partitioning" and ch. 11 on change data capture
- Debezium documentation on change data capture

*Note on verification: Stripe's figures and the description of DocDB, the six steps, versioned gating, the 10× import speed-up, the under-two-second switch and the 2023 consolidation were read from Stripe's post today. The comparisons with MongoDB's balancer, Vitess, Figma and Stripe's 2017 post come from those projects' documentation and posts as I know them and weren't re-fetched; the reasons Stripe built its own platform instead of using MongoDB's balancer are my inference, not stated in the post.*
