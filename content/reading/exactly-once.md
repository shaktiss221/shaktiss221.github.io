---
title: "Exactly-once delivery: what it really means"
date: 2026-10-23
summary: Why no network delivers exactly once, and the patterns that make the effect happen once anyway: idempotent consumers, the outbox and Kafka transactions.
tags: distributed systems, messaging
---

No network can deliver a message exactly once, but a system can make the *effect* of processing happen exactly once. Every working "exactly-once" system, from Kafka transactions to Stripe's API, does the same thing underneath: at-least-once delivery plus deduplication or atomic commits. This piece explains why, then walks through the patterns you can use: idempotent consumers, the transactional outbox, and Kafka's transactional machinery. Reading time is about 30 minutes.

## 1. Why delivery can't be exactly once

### Three guarantees

| Guarantee | Sender behaviour | Failure mode |
| --- | --- | --- |
| At most once | Send, never retry | A lost message is gone for good |
| At least once | Retry until acknowledged | A lost *acknowledgement* causes a duplicate |
| Exactly once | ? | Requires telling "lost message" from "lost ack" |

Consider a producer sending a message to a broker. It sends, then waits for an ack. The ack doesn't arrive within the timeout. Two very different things could have happened:

1. The message never reached the broker.
2. The broker stored the message, but the ack was lost, or the broker crashed just after storing it, or the ack is simply slow.

From the producer's side these look identical. If it retries, case 2 yields a duplicate. If it doesn't, case 1 loses the message. No timeout value or protocol trick changes this, because the producer has no way to observe the broker's state except through messages that can themselves be lost. This is a close cousin of the **Two Generals' Problem**: two parties communicating over an unreliable channel can never both be certain they agree.

The same ambiguity repeats at every hop. A consumer reads a message, processes it, and then commits its offset (or deletes it from the queue, or acks it). If it crashes between processing and committing, the message is delivered again on restart. If it commits first and then crashes before processing, the message is lost. So the order of "do the work" and "record that I did it" decides whether you get at-least-once or at-most-once. There is no third order.

### What "exactly once" actually promises

When a vendor says exactly-once, read it as one of these, and ask which:

- **Exactly-once processing within one system.** Kafka Streams reading from Kafka and writing to Kafka can commit outputs and input offsets in one atomic transaction. Inside that closed loop, each input affects the output exactly once.
- **Effectively-once.** Messages may be delivered and even processed more than once, but the observable effect (a row in a database, a charge on a card) happens once, because the processing is idempotent or deduplicated.
- **Deduplication within a window.** SQS FIFO drops a repeat of the same deduplication ID within 5 minutes. A retry after 6 minutes is a new message.

None of these extend automatically to side effects outside the system. If your Kafka Streams app sends an email in the middle of processing, the email can go out twice, because Kafka's transaction cannot roll back an SMTP call. Tyler Treat's well-known 2015 post put it bluntly: you cannot have exactly-once delivery, and anyone claiming it is redefining terms.

### The practical rule

Design for **at-least-once delivery and idempotent processing**. Make retries safe, then retry freely. Every pattern in the rest of this piece is a way to implement that rule.

## 2. Idempotent consumers

An operation is **idempotent** if doing it twice has the same effect as doing it once. If every consumer is idempotent, duplicates stop mattering, and at-least-once delivery becomes effectively-once. There are four main ways to get there, from cheapest to most general.

### Option 1: make the operation naturally idempotent

Some operations already are. `SET balance = 100` is idempotent; `balance = balance + 10` is not. `PUT /users/42 {...}` is; `POST /users` usually is not. Deleting a row by primary key is; appending to a log is not.

So the first design question is whether the message can carry **state instead of a delta**. An event that says "order 981 status is now SHIPPED" can be applied any number of times. An event that says "increment shipped count" cannot. Upserts help here: `INSERT ... ON CONFLICT (order_id) DO UPDATE` in Postgres, or a `PutItem` in DynamoDB keyed by a business ID, turn a create into something safe to repeat.

State-carrying events have a catch: **ordering**. If "status = SHIPPED" is redelivered after a later "status = DELIVERED", replaying it moves the order backwards. Fix that with a version number.

### Option 2: version or sequence numbers per entity

Give each entity a monotonically increasing version, and have the producer stamp it on every event. The consumer applies an event only if its version is newer than what it has stored:

```sql
UPDATE orders
   SET status = :status, version = :version
 WHERE order_id = :id
   AND version < :version;
```

A duplicate or stale event matches zero rows and is silently dropped. This handles both duplicates and reordering in one conditional write. DynamoDB's `ConditionExpression`, Cassandra's lightweight transactions and Elasticsearch's external versioning all offer the same primitive. It works best when one producer owns each entity's history, so versions are easy to assign.

### Option 3: a deduplication table (the inbox pattern)

When the operation is not naturally idempotent, record which messages you have processed. Every message needs a unique, stable ID assigned by the *producer*, not by the broker, so a retry of the same logical message carries the same ID.

The consumer then does its work and records the ID **in the same database transaction**:

```sql
BEGIN;
INSERT INTO processed_messages (message_id, processed_at)
     VALUES (:msg_id, now());          -- fails on duplicate key
UPDATE accounts SET balance = balance + :amount
 WHERE account_id = :acct;
COMMIT;
```

If the insert hits a unique-key violation, the message was already processed; roll back and ack it. If the process crashes anywhere before `COMMIT`, neither the effect nor the record exists, and the redelivery runs cleanly. If it crashes after `COMMIT` but before acking the broker, the redelivery hits the unique key and is skipped. This is sometimes called the **idempotent receiver** or **inbox** pattern.

The essential property is that the dedup record and the business effect commit atomically. A common mistake is to keep the dedup set somewhere else, such as Redis:

1. `SETNX msg:123` succeeds.
2. The consumer updates Postgres.
3. The consumer crashes before acking.

On redelivery `SETNX` fails, so the message is skipped. That is fine. But reverse steps 1 and 2, or let step 2 fail after step 1, and you either double-apply or drop the message. Two stores cannot be updated atomically without a distributed transaction. Redis-based dedup is acceptable as a fast pre-filter, not as the source of truth.

The dedup table grows forever unless you prune it. Keep IDs at least as long as a duplicate could plausibly arrive: the broker's maximum redelivery window, plus producer retry time, plus margin. Many teams keep 7 to 30 days and delete older rows in batches.

### Option 4: push the key downstream

When the side effect lives in another service, you can't share its transaction. Instead, pass a deterministic **idempotency key** derived from the message ID, and let the downstream service dedupe. Stripe's API is the reference design: every `POST` accepts an `Idempotency-Key` header. Stripe stores the first response for that key (for 24 hours) and returns the same response to any retry with the same key, even if the first request is still in flight, in which case the retry gets a conflict error rather than a second charge.

The chain only holds if every hop keeps the same key. A consumer that generates a fresh UUID for each attempt defeats the whole scheme. Derive keys from the input: `charge-for-order-981` or `hash(message_id + step_name)`.

### Side effects you can't make idempotent

Some actions cannot be deduplicated by the receiver: sending an SMS through a provider that ignores idempotency keys, or calling a legacy system. The options are limited:

- Record intent first ("SMS for order 981 pending") and the result after, and have a reconciler check pending items rather than blindly resend.
- Accept rare duplicates where the cost is low (a second notification email) and say so in the design doc.
- Put a human or a manual review step in front of anything where a duplicate is expensive and can't be reconciled.

## 3. The dual-write problem and the transactional outbox

The hardest exactly-once bug is on the *producer* side: a service that must update its database **and** publish an event. Doing both reliably without a distributed transaction is the job of the transactional outbox.

### Why dual writes break

An order service handles `POST /orders`. It inserts a row into `orders` and publishes `OrderCreated` to Kafka. Whichever order you do them in, a crash in between leaves the system inconsistent:

- **Write DB, then publish.** If the process dies after the commit but before the publish, the order exists and nobody downstream ever hears about it. Inventory is never reserved; the customer is never emailed.
- **Publish, then write DB.** If the DB write fails (constraint violation, deadlock, crash), downstream services act on an order that does not exist.
- **Publish inside the DB transaction, before commit.** The publish can't be rolled back if the commit then fails, so this is the second case in disguise.

Retries don't help, because after a crash nothing remembers that the publish was still owed. Two-phase commit (XA) across the database and the broker would solve it in theory, but most brokers don't support XA, and where they do it is slow and couples the availability of both systems.

### The outbox pattern

The fix is to stop writing to two systems. Write to one, and derive the other from it.

<!--diagram:82d25974-35fc-->

The service writes the business row and an event row in one local transaction; a separate relay ships committed events to the broker.

1. In the same transaction as the business change, insert a row into an `outbox` table: event ID, aggregate type and ID, event type, payload, created time.
2. Commit. The order and its event now either both exist or both don't. This is plain single-database ACID.
3. A separate **relay** process reads unsent outbox rows, publishes them to the broker, and marks them sent (or deletes them).

The relay is at-least-once by construction. If it publishes and then crashes before marking the row, it publishes again on restart. That's why consumers still need the idempotency patterns from section 2, keyed on the outbox event ID. The outbox guarantees *no lost events*; the inbox guarantees *no double effects*. Together they give effectively-once end to end.

### Polling relay vs change data capture

There are two ways to build the relay.

**Polling.** A loop runs `SELECT ... FROM outbox WHERE sent_at IS NULL ORDER BY id LIMIT 500`, publishes, then updates `sent_at`. It is simple and works on any database. The costs are polling latency (often 100 ms to 1 s), load on the database, and care with concurrency: two relay instances must not grab the same rows, which `SELECT ... FOR UPDATE SKIP LOCKED` in Postgres and MySQL 8 handles neatly.

**Change data capture (CDC).** Instead of querying the table, tail the database's replication log (the Postgres WAL via logical decoding, or the MySQL binlog). **Debezium** is the common tool and ships an "outbox event router" transform that turns outbox inserts into Kafka messages routed by aggregate type. CDC has lower latency, no polling load, and preserves commit order. The cost is operational: a Kafka Connect cluster, replication slots to monitor (an abandoned Postgres slot retains WAL until the disk fills), and schema handling.

### Ordering and the outbox

If downstream consumers need events for one entity in order, publish with the aggregate ID as the partition key so all events for order 981 land in one partition. A single-threaded relay that reads in commit order (CDC does this naturally) then preserves per-entity order. A polling relay ordered by an auto-increment ID can still misorder events, because IDs are assigned at insert time, not commit time: transaction A takes ID 10, transaction B takes ID 11 and commits first, and a poll in between sees 11 without 10. Per-aggregate version numbers in the payload let consumers detect and handle this.

### Related patterns

- **Listen to yourself.** The service publishes the event first and updates its own database only when it consumes its own event. The log becomes the source of truth. This works, but read-your-writes becomes eventual for the service itself.
- **Event sourcing.** The event log *is* the database, so there is no dual write at all. Powerful, but a much larger architectural commitment.
- **Sagas** chain local transactions across services, each publishing through an outbox, with compensating actions to undo earlier steps if a later one fails.

## 4. How Kafka does exactly-once

Kafka added exactly-once semantics (EOS) in version 0.11 in 2017, through KIP-98. It is the best-known exactly-once implementation, and it is built from three pieces: an idempotent producer, transactions across partitions, and consumers that can skip uncommitted data. Each piece solves one failure from section 1.

### Piece 1: the idempotent producer

Problem: the producer sends a batch, the broker writes it, the ack is lost, the producer retries, and the partition now holds the batch twice.

Fix: on startup the producer gets a **producer ID (PID)** from the broker. Every batch it sends carries the PID, a **producer epoch**, and a **sequence number** per partition, starting at 0 and increasing by one per record. The broker remembers the last sequence numbers it accepted from each PID for each partition (enough to cover the last 5 in-flight batches). A retry with a sequence it has already written is acknowledged but not appended. A gap in sequence numbers means something was lost, and the broker rejects the batch with an out-of-order error.

This costs almost nothing, so since Kafka 3.0 `enable.idempotence=true` and `acks=all` are the producer defaults. It also preserves ordering with up to 5 batches in flight, which previously required `max.in.flight.requests.per.connection=1`.

The limit: dedup is per producer session and per partition. If the producer process restarts without a stable identity, it gets a new PID, and an application-level retry of the same message is a new message to Kafka.

### Piece 2: transactions

Problem: a stream processor reads from topic A, writes results to topics B and C, and commits its offset on A. A crash in the middle leaves some outputs written and the offset uncommitted, so the input is reprocessed and the outputs duplicated.

Fix: make all of it one atomic transaction.

```java
producer.initTransactions();          // registers transactional.id, fences zombies
while (true) {
  ConsumerRecords<K, V> in = consumer.poll(Duration.ofMillis(100));
  producer.beginTransaction();
  for (var r : in) producer.send(transform(r));
  producer.sendOffsetsToTransaction(offsets(in), consumer.groupMetadata());
  producer.commitTransaction();         // or abortTransaction() on error
}
```

Under the hood:

- The producer is configured with a stable **`transactional.id`**. A broker acting as **transaction coordinator** for that ID keeps state in an internal `__transaction_state` topic.
- `initTransactions()` bumps the producer epoch for that `transactional.id`. Any older instance still running with the same ID (a "zombie", say one paused by a long GC that the group already replaced) now has a stale epoch, and its writes are rejected. This **fencing** is what stops two copies of the same worker from both committing.
- As the transaction writes to partitions, the coordinator records which partitions are involved. Consumer offsets are just writes to the `__consumer_offsets` topic, so they join the transaction like any other partition.
- On commit, the coordinator runs a two-phase commit: it writes PREPARE_COMMIT to its log, then writes a **commit marker** (a control record) into every involved partition, then writes COMPLETE_COMMIT. Once PREPARE_COMMIT is durable, the commit will finish even if the coordinator fails over.

### Piece 3: read_committed consumers

Transactional records are written to partitions *before* the commit decision, interleaved with other data. A consumer with the default `isolation.level=read_uncommitted` sees them immediately, including ones that will later be aborted.

With `isolation.level=read_committed`, a consumer reads only up to the **last stable offset (LSO)**: the earliest offset of any still-open transaction in that partition. Records from aborted transactions are filtered out using an index of aborted transactions that the broker keeps alongside each log segment. The cost is latency: a downstream consumer can't see anything past an open transaction, so long transactions delay everyone reading that partition. A "hanging" transaction that never completes (historically possible after some client bugs) stalls the LSO until it times out, which is what later work such as KIP-890 hardened against on the server side.

### Kafka Streams and EOS v2

Kafka Streams wraps all of this behind one setting, `processing.guarantee=exactly_once_v2`. The first version created one transactional producer per input partition (task), which was expensive with thousands of partitions. KIP-447 (Kafka 2.6) changed fencing to use consumer group generation, allowing one producer per stream thread. Confluent's measurements put the throughput cost of EOS at a few percent with reasonable commit intervals (the default is 100 ms under EOS); very short transactions cost more.

### Where Kafka's guarantee ends

Kafka's EOS covers **read from Kafka, process, write to Kafka**. It does not cover:

- **External side effects** during processing: HTTP calls, emails, writes to Postgres. An aborted and retried transaction repeats them.
- **Sinks outside Kafka.** Kafka Connect sink connectors get exactly-once only if the sink is idempotent (upsert by key) or stores offsets atomically with the data, for example writing the Kafka offset into the same Postgres transaction as the rows.
- **Producers that are not Kafka-aware.** An upstream service that retries an HTTP request to your producer creates a new message, unless you dedupe with a business key.

Source connectors gained exactly-once support in Kafka 3.3 (KIP-618), by writing source offsets transactionally with the records. The general lesson stands: Kafka gives you an atomic commit within its own log, and you extend it outward with idempotency.

## 5. Other systems, and choosing an approach

### How other brokers and engines frame it

| System | What it offers | The fine print |
| --- | --- | --- |
| Amazon SQS FIFO | Drops a message whose `MessageDeduplicationId` (or content hash) was seen in the last 5 minutes | A retry after 5 minutes is a new message. Consumers can still get a message twice if processing outlasts the visibility timeout. |
| Amazon SQS Standard | At-least-once, best-effort ordering | Duplicates are expected; consumers must be idempotent |
| RabbitMQ | At-least-once with publisher confirms and manual consumer acks | No built-in broker-side dedup in the core; redelivered messages carry a `redelivered` flag as a hint |
| NATS JetStream | Dedup on a `Nats-Msg-Id` header within a configurable window (2 minutes by default), plus double-ack for consumers | Window-bound, like SQS FIFO |
| Apache Pulsar | Broker-side dedup by producer name and sequence ID; transactions across topics since 2.8 | Similar shape to Kafka's idempotent producer and transactions |
| Apache Flink | Exactly-once *state* via checkpoints; exactly-once *sinks* via two-phase commit | The sink must support transactions or be idempotent |

Flink's approach is worth a closer look, because it shows the same idea in a different shape. Flink periodically injects **checkpoint barriers** into the input streams. When an operator has seen the barrier on all inputs, it snapshots its state. Once every operator has checkpointed, the checkpoint is complete. After a failure, Flink rewinds all sources to the offsets saved in the last checkpoint and restores all operator state to match, so internal state reflects each input exactly once even though records are replayed.

For output, Flink's two-phase commit sinks open a transaction per checkpoint (for Kafka, a Kafka transaction), pre-commit when the barrier arrives, and commit only when the checkpoint is confirmed complete. Downstream `read_committed` readers therefore see output in checkpoint-sized steps, which adds latency equal to the checkpoint interval.

### A decision guide

Work through these in order, and stop at the first that fits:

1. **Can the message carry state, not a delta?** Then use upserts, plus per-entity version numbers if order matters. Cheapest and most robust.
2. **Does the consumer write to a transactional database?** Then use an inbox table: record the message ID in the same transaction as the effect.
3. **Does the producer update a database and publish an event?** Then use the transactional outbox, with CDC if you need low latency or strict per-entity ordering, polling if you want minimal infrastructure.
4. **Is the whole pipeline Kafka in, Kafka out?** Then turn on Kafka transactions or Kafka Streams `exactly_once_v2`, and make sure downstream consumers use `read_committed`.
5. **Does the effect live in another service?** Then pass a deterministic idempotency key and require the service to honour it.
6. **None of the above?** Accept at-least-once, make duplicates detectable, and reconcile.

Across all six, three habits pay off:

- **Assign IDs at the source.** The message ID must exist before the first send attempt and survive every retry. IDs generated by the broker or per attempt can't deduplicate anything.
- **Commit the dedup record with the effect.** If they live in different stores, you have reintroduced the dual-write problem.
- **Test the crash points.** Kill the consumer between processing and acking, and the producer between commit and publish, in an integration test. Duplicate handling that has never been exercised usually doesn't work.

## Discussion questions

1. A teammate proposes deduplicating payment events using Redis `SETNX` with a 24-hour TTL before writing to Postgres. Walk through the crash scenarios. What would you change?
2. Your outbox relay uses polling ordered by an auto-increment ID. Describe how two concurrent transactions could produce out-of-order events, and how a consumer could detect it.
3. Kafka fences "zombie" producers using the epoch tied to a `transactional.id`. What could go wrong if two different workers were accidentally configured with the same `transactional.id`?
4. SQS FIFO deduplicates within 5 minutes. Design a scenario where a duplicate still gets through, and say which layer should catch it.
5. Why does `read_committed` increase end-to-end latency, and how would you size transaction or checkpoint intervals for a pipeline with a 2-second freshness target?
6. Pick a workflow you know (sign-up, checkout, refunds). Which steps are naturally idempotent, which need keys, and which side effect would be hardest to make safe?

## Sources

Written from the references below. Links were not re-opened while writing this piece.

- [KIP-98: Exactly Once Delivery and Transactional Messaging](https://cwiki.apache.org/confluence/display/KAFKA/KIP-98+-+Exactly+Once+Delivery+and+Transactional+Messaging), Apache Kafka
- [KIP-447: Producer scalability for exactly once semantics](https://cwiki.apache.org/confluence/display/KAFKA/KIP-447%3A+Producer+scalability+for+exactly+once+semantics), Apache Kafka
- [Exactly-once Semantics are Possible: Here's How Kafka Does it](https://www.confluent.io/blog/exactly-once-semantics-are-possible-heres-how-apache-kafka-does-it/), Neha Narkhede, Confluent, 2017
- [Transactions in Apache Kafka](https://www.confluent.io/blog/transactions-apache-kafka/), Apurva Mehta and Jason Gustafson, Confluent
- [You Cannot Have Exactly-Once Delivery](https://bravenewgeek.com/you-cannot-have-exactly-once-delivery/), Tyler Treat, 2015
- [Pattern: Transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html), Chris Richardson
- [Outbox Event Router](https://debezium.io/documentation/reference/stable/transformations/outbox-event-router.html), Debezium docs
- [Designing robust and predictable APIs with idempotency](https://stripe.com/blog/idempotency), Brandur Leach, Stripe, 2017
- [Using the message deduplication ID in Amazon SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/using-messagededuplicationid-property.html), AWS docs
- [An overview of end-to-end exactly-once processing in Apache Flink](https://flink.apache.org/2018/02/28/an-overview-of-end-to-end-exactly-once-processing-in-apache-flink-with-apache-kafka-too/), Apache Flink blog, 2018
