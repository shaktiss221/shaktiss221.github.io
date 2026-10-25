---
title: "Kafka internals: the log, replication and consumer groups"
date: 2026-10-25
summary: Why Kafka is a log and not a queue, how partitions are stored and replicated, what the high watermark guarantees, how consumer groups rebalance, and what changed in Kafka 4.
tags: messaging, distributed systems
---

Kafka is easiest to understand if you stop thinking of it as a message queue: it is a replicated, append-only log that many readers can scan at their own pace. Almost every feature, and every surprising behavior, follows from that one design choice. This reading walks from a single partition on disk to replication, producers, consumer groups and the changes that arrived with Kafka 4.0 and 4.1.

## 1. A log, not a queue

A classic queue such as RabbitMQ or Amazon SQS hands each message to one consumer and forgets it once that consumer acknowledges it. Kafka does something different: it appends every record to a log and keeps it for a configured retention period, whether or not anyone has read it.

| | Queue (SQS, RabbitMQ) | Log (Kafka) |
| --- | --- | --- |
| After a message is consumed | It is deleted | It stays until retention expires |
| Tracking progress | The broker tracks each message's state | Each consumer group tracks one number per partition: its offset |
| Several independent readers | Needs fan-out (one queue per reader) | Free: each group reads the same log |
| Replay | Not possible once deleted | Rewind the offset and read again |
| Ordering | Usually best effort, or FIFO with limits | Strict within a partition |
| Parallelism | Add consumers freely | Limited by the number of partitions |

The log model is why Kafka is used as the backbone of event-driven systems. A new service can start reading from the beginning of a topic and build its own view of the data. A bug in a consumer can be fixed and the consumer replayed from yesterday's offset. The same stream can feed a search index, a data warehouse and a fraud detector without any of them knowing about each other.

The price is the last row of the table: parallelism and ordering are both tied to partitions, which section 5 comes back to.

## 2. A partition on disk

A **topic** is split into **partitions**, and each partition is an ordered log. Every record in a partition gets a sequential **offset**: 0, 1, 2 and so on. Offsets are per partition, so there is no global order across a topic.

On disk, a partition is a directory of **segment files**. Each segment holds a contiguous range of offsets, and a new segment is started when the active one reaches a size limit (1 GB by default) or an age limit. Only the newest segment is written to; older ones are immutable.

Next to each segment sit two small index files:

- An **offset index** maps offsets to byte positions in the segment. It is sparse, with an entry every few kilobytes, so it stays small enough to keep in memory. To find offset 1,234,567, Kafka binary-searches the segment file names, then the index, then scans forward a few kilobytes.
- A **time index** maps timestamps to offsets, so a consumer can ask to start "from 9:00 this morning".

Retention works on whole segments. When the oldest segment is entirely older than the retention time (seven days by default) or the partition exceeds its size limit, Kafka deletes the file. There is no per-record deletion, which is part of why it is cheap.

### Log compaction

For some topics you only care about the latest value per key, such as a customer's current address. A **compacted** topic keeps at least the last record for every key and lets a background cleaner remove older ones. A record with a null value, a **tombstone**, marks a key as deleted and is itself removed after a while. Compacted topics let Kafka act as a durable changelog: a new consumer can rebuild the full current state by reading the topic from the start. Kafka uses this itself for consumer offsets.

### Why it is fast

Kafka's throughput comes from doing very little per record:

- **Sequential I/O.** Appends go to the end of a file and reads usually scan forward. Disks and SSDs are far faster at sequential access than random access.
- **The OS page cache.** Kafka does not keep its own cache of records. Recently written data sits in the operating system's page cache, so consumers that keep up read from memory without Kafka managing it.
- **Zero copy.** When sending data to consumers, Kafka can use the `sendfile` system call to move bytes from the page cache to the network socket without copying them through the application.
- **Batching and compression.** Producers send records in batches, compressed as a unit, and the broker stores and serves those batches as they are. Compression works much better on a batch of similar records than on one.

## 3. Replication and the high watermark

Each partition has a **replication factor**, usually three. One replica is the **leader**; the others are **followers**. Producers write to the leader, and followers continuously **fetch** new records from it, the same way consumers do.

<!--diagram:kafka-hw-1025-->

### In-sync replicas

The leader tracks which followers are keeping up. Those form the **in-sync replica set (ISR)**. A follower that falls behind for longer than `replica.lag.time.max.ms` (30 seconds by default in recent versions) is dropped from the ISR, and rejoins once it catches up.

### The high watermark

The **high watermark** is the offset up to which every replica in the ISR has the data. Records below it are **committed**: they will survive the loss of the leader, because every in-sync follower already has them. Consumers are only allowed to read up to the high watermark. The leader may hold newer records beyond it, but those are not yet safe, so no consumer sees them.

This is the key guarantee: a consumer never reads a record that could later disappear because of a leader failure.

### acks and min.insync.replicas

The producer chooses how long to wait:

- `acks=0`: do not wait at all. Fast, and records can be lost silently.
- `acks=1`: wait for the leader to write it. A leader crash before followers copy it loses the record.
- `acks=all`: wait until every replica in the ISR has it. This has been the default since Kafka 3.0.

`acks=all` has a trap. If the ISR shrinks to just the leader, "all in-sync replicas" means one copy. The broker setting `min.insync.replicas` closes the gap: with a replication factor of 3 and `min.insync.replicas=2`, a write with `acks=all` is rejected unless at least two replicas have it. That combination is the standard durable setup, and it tolerates one broker failure without losing acknowledged data or availability.

### Leader failover

When a leader fails, the controller picks a new leader from the ISR, so it already has every committed record. Followers that had records beyond the old high watermark **truncate** them to match the new leader, using **leader epochs** (a counter that increases with every leader change) to find exactly where their logs diverged.

If every ISR member is gone, Kafka faces a choice: wait for one to come back, or elect an out-of-sync replica and lose data. `unclean.leader.election.enable` decides, and it is off by default, choosing consistency over availability. Kafka 4.0 also added a preview of **Eligible Leader Replicas** (KIP-966), which tracks more precisely which replicas are safe to elect.

## 4. Producers: partitioning, batching and idempotence

A producer decides which partition each record goes to:

- If the record has a **key**, it is hashed and the hash picks the partition. All records with the same key land in the same partition, which is how you get per-key ordering, for example all events for one order in sequence.
- If there is no key, the default partitioner sticks to one partition until the current batch is full, then moves on. This "sticky" behavior makes larger batches than spreading each record around would.

A consequence worth remembering: adding partitions to a topic changes which partition a key hashes to. Per-key ordering only holds while the partition count stays the same, so choose it with growth in mind.

Batching is controlled by `batch.size` and `linger.ms`: the producer waits up to `linger.ms` milliseconds to fill a batch before sending. A few milliseconds of linger can multiply throughput.

### Idempotent producers

Retries can create duplicates. If a broker writes a batch but the acknowledgment is lost, the producer retries and the batch is written twice. The **idempotent producer** fixes this: each producer gets an ID, and every batch carries a sequence number per partition. The broker discards a batch whose sequence it has already seen. This is on by default since Kafka 3.0. Transactions build on it to write atomically across partitions; this site's reading on exactly-once delivery covers that part.

## 5. Consumers, groups and rebalancing

Consumers that share a `group.id` form a **consumer group**. Kafka assigns each partition of the subscribed topics to exactly one consumer in the group. That gives parallelism and per-partition ordering at once: each partition is processed in order by one consumer.

The rule cuts both ways. A topic with 12 partitions can be processed by at most 12 consumers in one group; a 13th sits idle. If one slow record blocks a partition, everything behind it waits. Partition count is effectively the maximum parallelism, which is why teams often over-partition up front.

### Offsets

Each group records its progress by **committing** the offset of the next record to read, per partition, to an internal compacted topic called `__consumer_offsets`. When a consumer restarts or a partition moves, the new owner continues from the last committed offset.

When you commit decides your delivery guarantee. Commit after processing and a crash means some records are processed twice (at-least-once). Commit before processing and a crash means some are skipped (at-most-once). Most systems choose at-least-once and make processing idempotent.

### Rebalancing, and why it was painful

When a consumer joins, leaves or crashes, partitions must be reassigned. In the original protocol, every member stopped consuming, gave up all its partitions, and waited while one member (the group leader) computed a new assignment. With hundreds of consumers, or a deployment that restarts them one by one, this **stop-the-world rebalance** could pause a whole group again and again.

Kafka improved this in steps:

1. **Static membership** (KIP-345): a consumer with a stable `group.instance.id` can restart without triggering a rebalance, as long as it returns within the session timeout.
2. **Cooperative incremental rebalancing** (KIP-429): members only give up the partitions that actually move, and keep processing the rest.
3. **The new consumer group protocol** (KIP-848): assignment moves from the client to the broker-side **group coordinator**, which reconciles each member incrementally without a global pause. It is generally available in Kafka 4.0, enabled on the broker by default, with clients opting in through `group.protocol=consumer`.

## 6. The control plane: from ZooKeeper to KRaft

Every Kafka cluster needs a **controller** that knows which brokers are alive, which replica leads each partition, and what the ISR is. For most of Kafka's history that metadata lived in Apache ZooKeeper, a separate system to deploy and operate, and a single controller broker loaded it from ZooKeeper on failover. With many partitions, controller failover could take a long time.

**KRaft** replaces ZooKeeper with a small quorum of controller nodes that agree on metadata using a Raft-based protocol. The metadata itself is stored as a Kafka log (`__cluster_metadata`), and brokers simply replicate that log to stay up to date. The controller that takes over already has the full metadata in memory, so failover is quick.

Kafka 4.0, released in March 2025, is [the first major release to run entirely without ZooKeeper](https://kafka.apache.org/blog/2025/03/18/apache-kafka-4.0.0-release-announcement/). Clusters must migrate to KRaft on 3.x before upgrading. 4.0 also added a **pre-vote** step (KIP-996) so a briefly disconnected controller does not force unnecessary elections, and later releases added dynamic changes to the controller quorum (KIP-853).

## 7. Queues for Kafka: share groups

The partition-per-consumer rule is a poor fit for work queues, where you want 100 workers pulling jobs and do not care about order. **KIP-932, Queues for Kafka**, adds **share groups** for exactly this.

In a share group, many consumers can read from the same partition at once. Each record is handed to one consumer under a time-limited **acquisition lock**, and the consumer acknowledges records individually: accept, release (give it back for redelivery) or reject. Kafka counts delivery attempts per record, so a record that keeps failing can be dropped or set aside after a limit. This brings SQS-style semantics (visibility timeouts, per-message acknowledgment, redelivery) to ordinary Kafka topics, without giving up retention and replay for other groups reading the same topic.

It arrived as early access in Kafka 4.0, and [Kafka 4.1 moved it to preview](https://thenewstack.io/apache-kafka-4-1-the-3-big-things-developers-need-to-know/) in September 2025. Check the current release notes before relying on it in production.

## 8. Ideas to carry away, and questions for our discussion

Five ideas to keep:

1. **Kafka is a log.** Retention, replay, independent consumer groups and compaction all follow from keeping records instead of deleting them on read.
2. **The partition is the unit of everything.** Ordering, parallelism, replication and storage are all per partition, so partition count and key choice are the most important design decisions.
3. **Committed means on every in-sync replica.** The high watermark is what makes "acknowledged" and "readable" safe across leader failures.
4. **acks=all needs min.insync.replicas.** Without it, a shrunken ISR silently reduces you to one copy.
5. **Coordination is the hard part.** Rebalancing and the controller were Kafka's biggest operational pain, and KIP-848 and KRaft are the fixes.

Think about these before we talk:

- Why can't a consumer read records above the high watermark, even though the leader has them?
- With replication factor 3, `min.insync.replicas=2` and `acks=all`, what happens to producers when two brokers go down? What would you rather happen?
- You need per-customer ordering and expect traffic to grow tenfold. How many partitions would you create, and why does that number matter more than it does in a queue?
- When would you pick SQS or RabbitMQ over Kafka, and do share groups change that answer?
- A consumer group keeps rebalancing during every deploy. What would you check first?
- Which topics in a system you know would benefit from log compaction?

## Sources

- Apache Kafka, [Apache Kafka 4.0.0 release announcement](https://kafka.apache.org/blog/2025/03/18/apache-kafka-4.0.0-release-announcement/), March 2025
- The New Stack, [Apache Kafka 4.1: the 3 big things developers need to know](https://thenewstack.io/apache-kafka-4-1-the-3-big-things-developers-need-to-know/), September 2025
- Apache Kafka, [Queues for Kafka (KIP-932) preview release notes](https://cwiki.apache.org/confluence/display/KAFKA/Queues+for+Kafka+%28KIP-932%29+-+Preview+Release+Notes)
- Jay Kreps, *The Log: What every software engineer should know about real-time data's unifying abstraction* (LinkedIn Engineering, 2013)
- Kreps, Narkhede and Rao, *Kafka: a Distributed Messaging System for Log Processing* (NetDB 2011)
- Apache Kafka documentation: design, replication and consumer configuration sections

The Kafka 4.0 and 4.1 details were checked against the pages above today. Storage, replication and configuration defaults (segment size, retention, replica lag time, idempotence and acks defaults) are from my knowledge of the Kafka documentation and were not re-checked today.
