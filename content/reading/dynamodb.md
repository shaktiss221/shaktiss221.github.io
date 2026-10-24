---
title: DynamoDB under the hood
date: 2026-10-20
summary: How AWS keeps single-digit millisecond latency at any scale, from Paxos partitions to global admission control.
tags: databases, distributed systems
---

DynamoDB is a key-value store that is less interesting for its API than for one promise: the same single-digit millisecond latency at 10 requests per second or 10 million. This reading follows how AWS keeps that promise. It is drawn mainly from the 2022 USENIX ATC paper by the DynamoDB team, Marc Brooker's commentary on it, and Alex DeBrie's writing on data modeling.

## 1. Why DynamoDB exists

DynamoDB was built because Amazon's earlier store, Dynamo, scaled well but every team had to run it themselves. The 2007 Dynamo paper (SOSP) described a leaderless, eventually consistent store with consistent hashing, vector clocks and sloppy quorums. It powered the shopping cart, but each team installed and operated its own cluster. That meant on-call, capacity planning and upgrades for every team.

Around the same time AWS offered SimpleDB, a fully managed store. Teams loved not operating it, but it had hard limits: a domain held at most 10 GB, and latency was unpredictable because every attribute was indexed. Engineers kept picking Dynamo for scale and wishing for SimpleDB's operations story.

DynamoDB, launched in January 2012, is the merge of the two. It keeps Dynamo's ideas about partitioning and replication but changes the replication model (more on that in section 3) and adds the managed service. The paper lists six properties the team holds itself to:

| Property | What it means in practice |
| --- | --- |
| Fully managed | No servers, patching, backups or upgrades for the customer |
| Multi-tenant | Many customers share the same storage nodes, so isolation is a core problem |
| Boundless scale | No fixed upper limit on table size or throughput |
| Predictable performance | A 1 KB get or put stays in single-digit milliseconds as the table grows |
| Highly available | Replicas across three Availability Zones; a 99.99% SLA, 99.999% for global tables |
| Flexible use cases | Key-value and document data, with a choice of eventual or strong consistency |

The phrase to keep in mind is **predictable performance**. Average speed is easy. The hard part is that p99 latency does not drift when the table grows a thousandfold or when a noisy neighbor shares your hardware. During Amazon Prime Day 2021, DynamoDB peaked at 89.2 million requests per second with that latency held. Most of the design decisions below trade raw efficiency for predictability.

## 2. The data model in five minutes

Every DynamoDB request is shaped by the primary key, so the internals only make sense once you know what that key is. A **table** holds **items**, and each item is a set of attributes up to 400 KB. Apart from the key, items in the same table do not need the same attributes.

The primary key comes in two forms:

- **Partition key only.** One attribute, such as `user_id`. DynamoDB hashes it to choose where the item lives. You can get, put, update or delete one item by exact key.
- **Partition key plus sort key.** For example `customer_id` + `order_date`. All items with the same partition key form an **item collection**, stored together and sorted by the sort key. You can now `Query` a range inside one collection: "orders for customer 42 since October 1".

Two kinds of secondary index give you other access paths:

- A **local secondary index (LSI)** keeps the same partition key with a different sort key. It lives in the same partition as the base data, so it can be strongly consistent, but it caps an item collection at 10 GB.
- A **global secondary index (GSI)** has a completely different key. Under the hood it is a separate table that DynamoDB keeps in sync asynchronously, so GSI reads are eventually consistent.

There is no join and no query planner. `Scan` reads the whole table and is rarely what you want in production. The design pushes you to know your access patterns first and shape the keys to answer each one with a single `GetItem` or `Query`. Section 6 returns to what that means for modeling.

Capacity is measured in units that map directly onto the work a partition does:

- One **read capacity unit (RCU)** is one strongly consistent read per second of an item up to 4 KB. An eventually consistent read costs half.
- One **write capacity unit (WCU)** is one write per second of an item up to 1 KB.

These units matter because they are what the system rations. Section 4 is essentially the story of how DynamoDB learned to ration them fairly.

## 3. Architecture: partitions, leaders and routers

A DynamoDB table is split into partitions, each partition is a small Paxos group of three replicas in three Availability Zones, and a stateless fleet of request routers decides which group gets each request.

<!--diagram:354ba25a-122c-->

The router asks MemDS where the partition lives and asks GAC whether the table has capacity, then sends the request to the leader. The leader replicates each write to its two followers and the log is archived to S3.

### Partitions

The partition key is hashed, and each partition owns a contiguous range of the hash space. A small table may have one partition; a large one has thousands. Partitions split when they grow too large in size or when they need more throughput than one partition can serve. The customer never sees this; it is the main lever DynamoDB pulls to scale.

### Replication groups and Multi-Paxos

This is the biggest break from the original Dynamo. Dynamo was leaderless: any node could take a write, and conflicts were reconciled later with vector clocks. DynamoDB instead gives each partition a **leader**, elected with **Multi-Paxos**.

- Only the leader accepts writes. It writes the change to its **write-ahead log (WAL)**, sends the log record to its peers, and acknowledges the client once a **quorum** (two of three) has persisted it.
- Strongly consistent reads also go to the leader, because only it is guaranteed to have every acknowledged write.
- Eventually consistent reads can go to any replica, which is why they cost half an RCU.
- The leader holds a **lease** that it keeps renewing. If a follower stops hearing from the leader, it can call an election. A newly elected leader does not serve traffic until the old leader's lease has expired, so two leaders never serve at once.

Each replica stores two things: the WAL and a **B-tree** of the actual items. The paper also describes **log replicas**, which store only the WAL. They are cheap to create, so when a replica dies the group can restore its write quorum in seconds instead of waiting minutes to copy a full B-tree. Section 5 comes back to this.

The trade-off is clear. A leader gives you strong consistency, simple conflict handling and an obvious place for the write path to go. It costs a little availability during leader failover, which the team then worked hard to shrink.

### The services around the storage nodes

The paper names a handful of core services:

| Service | Job |
| --- | --- |
| Request routers | Authenticate and authorize each request, look up which storage node leads the target partition, and forward the request. Stateless, so they scale horizontally. |
| Metadata service | Stores the routing table: tables, indexes, partitions and which nodes host each replica group. |
| Storage nodes | Host replicas of many partitions from many customers. Run the WAL, B-tree and Paxos. |
| Autoadmin | The "central nervous system": watches fleet and partition health, replaces failed replicas, moves partitions off hot nodes, and executes control-plane requests like creating a table. |
| Global admission control | Tracks how much capacity each table is consuming across all routers (section 4). |

Other features such as backups, streams, global tables, GSIs and transactions are built as additional services around this core. The key insight is that the data path (router to storage node) is short and has very few dependencies, while everything slower lives off to the side.

## 4. Capacity: from per-partition quotas to global admission control

This is the heart of the paper. DynamoDB's first capacity model was simple and fair on paper, and it kept throttling customers who were nowhere near their limits. Each fix along the way moved the unit of fairness from the partition toward the whole table.

### Version 1: split the table's capacity evenly

At launch, a customer provisioned capacity for the table, for example 1,000 WCU. DynamoDB divided it evenly across partitions. With four partitions, each got 250 WCU, enforced locally on the storage node. This gave clean isolation: the storage node knew exactly how much each replica could use, and could pack replicas from many tables onto one machine without overcommitting it.

Two problems showed up in production.

1. **Hot partitions.** Real traffic is not uniform. If one partition gets 60% of the writes, it is throttled at its 250 WCU while the other three sit mostly idle. The customer sees throttling at well under 1,000 WCU and cannot understand why.
2. **Throughput dilution.** When a partition split for size, its capacity was split too. A table that grew from 4 to 8 partitions without changing its provisioned throughput saw each partition drop from 250 to 125 WCU. Growth alone made hot spots worse.

Customers' workaround was to over-provision, paying for capacity they could not use. That is exactly the kind of leak a managed service should not have.

### Fix 1: bursting

The first patch observed that partitions often have short spikes. DynamoDB began letting a partition bank its unused capacity for up to 300 seconds and spend it in a burst. Bursting was only allowed if the storage node itself had spare capacity, enforced with a second, node-level token bucket. So a partition has its own allocated bucket plus a burst bucket, and the node has a bucket that protects all its tenants.

Bursting helps with short spikes. It does nothing for a partition that is hot for an hour.

### Fix 2: adaptive capacity

Next, DynamoDB watched both provisioned and consumed capacity per table. When a table was being throttled while its total consumption was still under its provisioned total, adaptive capacity raised the limit on the hot partitions using a proportional control algorithm. If the node got too hot, autoadmin moved partitions elsewhere.

This fixed most of the complaints, but it was **reactive**. It only acted after throttling had already happened, so customers still saw a short window of errors at the start of each spike.

### Fix 3: global admission control (GAC)

The lasting fix was to stop enforcing the table's limit at the partition at all. With **global admission control**, the table's capacity lives in a central token bucket tracked by the GAC service.

- Each request router keeps a small local token bucket for each table it is serving.
- Routers draw from their local bucket for each request and ask GAC for more tokens every few seconds.
- GAC sees consumption across every router, so it can hand out tokens wherever the traffic actually is.

Now a single partition can use most of the table's capacity if that is where the traffic goes, as long as the table as a whole stays within its limit. Partition-level token buckets were kept as an upper safeguard, so one partition cannot overrun its storage node.

This is a recurring pattern in large systems: local enforcement is cheap and isolating, global enforcement is accurate, and the practical answer is a global budget handed out in small local leases.

### Balancing the nodes and splitting for traffic

Once partitions can take more than their "share", storage nodes can get hot. DynamoDB monitors each node, and when the replicas on it exceed a threshold of the node's capacity, it moves replicas to colder nodes.

It also learned to **split for consumption**. If a partition stays hot, DynamoDB splits it, but not at the midpoint of its key range. It picks the split point from the observed key distribution, so the hot keys land on separate partitions. The paper is honest about the limits: this does not help if the heat is a single item, or if keys are written in sequential order such as timestamps, because every new key lands at the end of the range.

### On-demand tables

In 2018, DynamoDB added **on-demand** mode, where you do not provision anything. The table accommodates up to double its previous peak instantly, and grows by splitting partitions for consumption. On-demand is only possible because of everything above: GAC for table-wide accounting and consumption-based splits for scaling. The irony is that the simplest feature for customers to understand, "just pay per request", needed the most machinery underneath.

## 5. Durability and availability

DynamoDB treats "never lose data" and "keep answering" as separate problems, and solves each with layers of checks rather than one clever mechanism.

### Not losing data

- **Replicated WAL.** A write is acknowledged only after two of three replicas, in different Availability Zones, have persisted its log record.
- **Archive to S3.** Each partition's WAL is continuously archived to Amazon S3, which is designed for eleven nines of durability. At any moment, only a small tail of recent log (the paper says typically a few hundred megabytes per node) exists only on the replicas.
- **Checksums everywhere.** Every log entry, message and log file carries a checksum. Silent corruption from disks, memory or networks is assumed to happen, not hoped against.
- **Continuous verification (scrub).** A background process checks that all replicas of a partition hold identical data. It also rebuilds a copy offline from the archived WAL and snapshots in S3, and verifies that it matches the live replicas. This catches bugs that would slowly diverge the copies.
- **Formal methods.** The replication protocol was specified and model-checked in TLA+. Failure-injection and stress testing run on top.

Because the WAL is already in S3, backups and point-in-time restore (to any second in the last 35 days) do not touch the live partitions at all. They are built from the archive.

### Staying up when a replica dies

A partition can take writes as long as it has a healthy leader and a write quorum. When one replica fails, the group has only two left, so one more failure would stop writes. Rebuilding a full storage replica means copying the whole B-tree, which takes minutes.

This is where **log replicas** pay off. The leader adds a log replica that only needs to store the WAL, which takes seconds. The quorum is healthy again almost immediately, and a full replica is rebuilt in the background.

### Gray failures

The trickier problem is a node that is not dead but is not quite healthy either, for example one with a flaky network path. A follower that cannot reach the leader might call an election that was not needed, causing a short outage for no reason.

DynamoDB's fix is simple: before triggering an election, a follower asks the other replicas whether they can still reach the leader. If they can, the follower assumes the problem is on its own side and stays quiet.

### Not depending on dependencies

DynamoDB depends on AWS IAM for authorization and AWS KMS for encryption keys. If either has an outage, DynamoDB should keep working. Request routers and storage nodes cache those results, so the data plane continues serving with what it last knew. This idea is called **static stability**: a system keeps doing what it was doing when something it relies on fails.

### The metadata cache and constant work

This is the paper's best story. Routers originally cached partition metadata locally, with a hit rate of about 99.75%. That sounds great until a cache is cold: a fleet of fresh routers, or a deployment, sends a sudden flood of requests to the metadata service. A system whose load depends on the cache hit rate has two very different modes, and the bad mode arrives precisely when things are already disturbed.

The fix was **MemDS**, a distributed in-memory metadata store built on a structure the team calls a Perkle tree (a Patricia trie combined with a Merkle tree). The important part is how routers use it. On a cache hit, the router still answers from its cache, but it also sends an **asynchronous** request to MemDS to refresh the entry. So MemDS receives roughly the same load whether the cache is warm or cold. Routers may briefly use a stale entry; if a storage node no longer owns a partition, it tells the router, which then refreshes.

This is the **constant work** principle that appears across AWS: a system that does the same amount of work in good times and bad has no hidden cliff to fall off.

## 6. What the internals mean for your data model

Everything above explains why DynamoDB modeling advice looks so different from relational advice: a request that touches one partition is fast and predictable, and anything else is your problem.

### Single-table design

In a relational database you would have `customers` and `orders` tables and join them. DynamoDB has no joins, and two separate `GetItem` calls means two network round trips. Alex DeBrie's well-known answer is **single-table design**: put different entity types in one table and arrange the keys so that related items share a partition key.

| PK | SK | Attributes |
| --- | --- | --- |
| `CUSTOMER#42` | `PROFILE` | name, email, tier |
| `CUSTOMER#42` | `ORDER#2026-10-01#981` | total, status |
| `CUSTOMER#42` | `ORDER#2026-10-18#1204` | total, status |

Now one `Query` on `PK = CUSTOMER#42` returns the customer profile and all their orders, sorted by date, from a single partition. A `begins_with(SK, "ORDER#2026-10")` condition narrows it to October. The item collection is doing the job a join would do, and it can do it because section 3 guarantees those items live together on one partition, sorted by the sort key.

Common techniques that follow from this:

- **Generic key names** like `PK` and `SK`, because the same attribute holds different kinds of identifiers.
- **GSI overloading**: one GSI with generic keys (`GSI1PK`, `GSI1SK`) serves several access patterns for different entity types.
- **Prefixes** like `ORDER#` so that range conditions select one entity type.

### When not to bother

DeBrie is clear that single-table design has costs. It is harder to learn and to read. Adding a new access pattern later can mean a data migration. Exporting to analytics tools is awkward because one table holds many shapes. AWS's own guidance has since softened: multiple tables are fine when entities are not fetched together, and for many apps (for example GraphQL APIs that resolve each type separately) there is little to gain. The real rule is **store together what you read together**, not "one table no matter what".

### Avoiding hot keys

Global admission control lets a hot partition borrow the table's capacity, but it cannot make one storage node infinitely fast. AWS documents a per-partition ceiling of about 3,000 RCU and 1,000 WCU. So:

- Choose partition keys with many distinct values that are accessed fairly evenly, such as user ID rather than country.
- For a key that is genuinely hot on writes, such as a global counter, use **write sharding**: append a suffix (`COUNTER#0` to `COUNTER#9`) and sum across the shards when reading.
- Avoid partition keys that are timestamps or sequential IDs. As section 4 noted, split-for-consumption cannot help when every new write lands at the end of the range.
- For a key that is hot on reads, put a cache such as DAX or ElastiCache in front of it.

## 7. Ideas to carry away, and questions for our discussion

Five ideas from DynamoDB show up in almost every large backend system:

1. **Predictability over peak efficiency.** DynamoDB accepts extra cost (three replicas, log replicas, background scrubbing) to keep tail latency flat.
2. **Move fairness to the right scope.** Per-partition limits were easy to enforce and wrong for customers; a global budget handed out as local tokens fixed it.
3. **Static stability.** Cache what you depend on so that a dependency outage does not become your outage.
4. **Constant work.** If load on a component depends on a cache hit rate, it has a hidden failure mode. Make the work the same in good and bad times.
5. **Leaders simplify.** Moving from Dynamo's leaderless design to a Paxos leader per partition traded a little failover time for much simpler consistency.

Think about these before we talk:

- Why did splitting a partition for size make hot-partition throttling worse under the original model?
- GAC hands out tokens to routers every few seconds. What could go wrong if a router crashes holding tokens, or if traffic shifts faster than the refresh interval?
- Why does a new leader wait for the old leader's lease to expire? What would break if it did not?
- MemDS makes routers call it even on a cache hit. That is wasted work most of the time. Why is it still the right choice?
- In your own systems, where is there a cache whose cold-start behavior could take down the thing behind it?
- When would you choose a multi-table design over single-table design?

## Sources

- Elhemali et al., [Amazon DynamoDB: A Scalable, Predictably Performant, and Fully Managed NoSQL Database Service](https://usenix.org/system/files/atc22-elhemali.pdf), USENIX ATC 2022 ([conference page](https://www.usenix.org/conference/atc22/presentation/vig))
- Marc Brooker, [The DynamoDB paper](https://brooker.co.za/blog/2022/07/12/dynamodb.html), July 2022
- Alex DeBrie, [The What, Why, and When of Single-Table Design with DynamoDB](https://alexdebrie.com/posts/dynamodb-single-table)
- AWS Database Blog, [Single-table vs. multi-table design in Amazon DynamoDB](https://aws.amazon.com/blogs/database/single-table-vs-multi-table-design-in-amazon-dynamodb/)
- DeCandia et al., Dynamo: Amazon's Highly Available Key-value Store, SOSP 2007 (background for section 1)

Some numbers, such as the per-partition limits and LSI size cap, come from AWS documentation as I remember it and may have changed. Check the current DynamoDB service quotas before relying on them.
