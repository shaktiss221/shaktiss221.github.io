---
title: "Stream processing: event time, watermarks and state"
date: 2026-11-08
summary: How stream processors like Flink compute correct results over unbounded, out-of-order data, covering event time versus processing time, windows, watermarks and late data, keyed state, checkpoints with asynchronous barrier snapshots, end-to-end exactly-once, backpressure, and where streaming SQL fits.
tags: analytics, messaging
daily: true
---

Batch jobs have a comfortable property: when they start, all the input already exists. A daily report runs over yesterday's data, finishes, and is correct. Streaming systems give that up. The input never ends, events arrive late and out of order, machines fail mid-computation, and answers are needed in seconds, not tomorrow. Yet modern stream processors can produce results as correct as a batch job's, continuously. This reading explains how: the distinction between event time and processing time, windows and the watermarks that decide when a window is complete, how processors keep large amounts of state and recover it after failures, and what "exactly-once" really means in a streaming pipeline. Apache Flink is the main example, with nods to Kafka Streams and Google Dataflow.

## 1. Bounded and unbounded data

Tyler Akidau, one of the engineers behind Google's MillWheel and Dataflow, argued in his widely read 2015 O'Reilly essays "Streaming 101" and "Streaming 102" that the useful distinction isn't batch versus streaming systems but **bounded versus unbounded data**. A bounded dataset has an end. An unbounded one, like clicks, payments, sensor readings or log lines, keeps growing forever.

Batch processing handles unbounded data by chopping it into bounded pieces (an hourly or daily job) and processing each piece separately. That works, but it has two costs: latency (a result can't be ready until its piece is complete and the job runs), and awkward boundaries (a user session that spans midnight gets split between two jobs).

A **stream processor** runs continuously over unbounded data and updates results as data arrives. Akidau's claim, backed up by the systems built since, is that a well-designed streaming system can give the same correct answers as batch, plus lower latency, so batch becomes a special case of streaming rather than the other way around. The Dataflow Model paper (Akidau et al., VLDB 2015) laid out the concepts, which became Apache Beam's programming model and strongly influenced Flink.

## 2. Event time versus processing time

Every event has two times:

- **Event time**: when it actually happened, recorded in the event itself (the moment the user tapped "buy").
- **Processing time**: when the pipeline sees it.

In an ideal world they'd be equal. In reality, the gap varies constantly: a phone is offline in a tunnel and uploads events ten minutes later, a Kafka partition falls behind, a server's batch uploader runs every minute, a network hiccup delays one region. Events arrive late, and out of order.

This matters because almost every interesting question is about event time. "How many purchases happened between 12:00 and 12:01?" is about when purchases happened, not when the pipeline noticed them. If you group by processing time, a delay in one data source shifts its events into the wrong minute, and replaying old data gives completely different answers from the original run. Results that depend on processing time aren't reproducible.

So correct stream processing usually means computing over **event time**, despite events arriving in processing-time order. That raises the central question of streaming: if events can arrive late, when do you know you have all the events for 12:00 to 12:01?

## 3. Windows

**Windows** divide an unbounded stream into finite chunks for aggregation. The common kinds:

- **Tumbling windows**: fixed size, non-overlapping. Every minute, every hour.
- **Sliding (hopping) windows**: fixed size, overlapping. "The last 10 minutes, updated every minute." Each event belongs to several windows.
- **Session windows**: dynamic, per key, defined by gaps in activity. A user's session ends after 30 minutes of inactivity. Sessions can merge when a late event fills a gap between two of them.
- **Global windows** with custom triggers, for things like "every 100 events."

Windows are usually **keyed**: tumbling one-minute windows of purchases *per merchant*, sessions *per user*. Each key and window pair is an independent piece of state.

Windows are easy to define. Deciding when each window's result is final is the hard part.

## 4. Watermarks: deciding when a window is complete

<!--diagram:watermark-1108-->

A **watermark** is the stream processor's estimate of progress in event time: a watermark of 12:01 is a claim that "no more events with event time before 12:01 are expected." When the watermark passes the end of a window, the processor considers the window complete and emits its result.

Watermarks are generated near the sources, based on what the source knows:

- If a source is perfectly ordered (say, a log written by one process and read in order), the watermark can simply follow the latest event time. That's a **perfect watermark**.
- Usually, sources are only roughly ordered, so the watermark is a **heuristic**. A common strategy in Flink is **bounded out-of-orderness**: the watermark trails the largest event time seen by a fixed delay, say 5 seconds, betting that events are never more than 5 seconds late.

Watermarks flow through the pipeline with the data. An operator that reads from several inputs (several Kafka partitions, or two joined streams) takes the **minimum** of its inputs' watermarks, because it can only be as sure as its slowest input. This has a well-known consequence: **one idle or slow partition holds back the watermark for everything downstream**, and windows stop firing. Flink lets you mark sources idle after a timeout so they don't block progress, and newer versions can align watermarks across sources so a fast source doesn't race far ahead of a slow one.

Watermark choice is a direct trade-off between **latency and completeness**:

- An aggressive watermark (small delay) emits results quickly but declares windows complete before some late events have arrived.
- A conservative watermark (large delay) waits longer for stragglers, so results are more complete but later.

No setting is perfect, because lateness is unbounded in principle. A phone can come back online days later.

## 5. Late data and triggers

So what happens to an event that arrives after the watermark has passed its window? The Dataflow Model's answer is to make it an explicit choice, with three tools:

**Allowed lateness.** Keep a window's state for some time after it fires (say, an hour), and if late events arrive in that period, update the result. After that, discard the state and treat further events as too late.

**Triggers** decide when to emit results for a window. A typical combination: an **early** result every minute of processing time while the window is open (so dashboards update quickly), an **on-time** result when the watermark passes the end, and **late** results each time late data arrives within allowed lateness.

**Accumulation mode** decides what each emitted result means:
- **Discarding**: each emission contains only the new data since the last one. Downstream must add them up.
- **Accumulating**: each emission is the full current result, replacing the previous one. Downstream must overwrite.
- **Accumulating and retracting**: each emission includes the new result and a retraction of the old one, which matters when downstream aggregates further (for example, sessions that merged).

Events that are too late can be dropped, or, better, routed to a **side output** so they're logged, counted and possibly fixed by a batch correction job. Silently dropping them means you never learn your watermark is too aggressive.

The key mindset shift: **a streaming result is a sequence of refinements**, not a single answer. Downstream systems should be designed to accept updates, for example by upserting into a database keyed by window, rather than assuming each result is final.

## 6. State

Anything beyond simple filtering needs **state**: counts per window, the contents of a session, the last known value for a key, the buffered side of a join. A stream processor is mostly a system for managing very large, partitioned state correctly.

Flink partitions state by key, just like the data: events for merchant 42 always go to the same parallel task, which owns merchant 42's state. That means no locking across tasks and natural scaling, since adding parallelism redistributes key ranges.

Where state lives:

- **On the heap** (Flink's HashMap state backend): fast, but limited by memory and subject to garbage collection pauses.
- **In an embedded RocksDB** (the LSM engine from the Oct 29 reading): state lives on local disk with a memory cache, so it can be far larger than RAM, at the cost of serialization on every access. This is how Flink jobs keep terabytes of state.
- **Disaggregated state** in remote storage. Flink 2.0 (2025) introduced a disaggregated state backend that keeps state primarily in object storage, with local disks as a cache. The goal is faster rescaling and recovery, since tasks no longer need to download all their state to local disk first, and decoupling compute from storage, as many cloud databases have done.

State needs a lifetime. Windows clean up after themselves, but state for things like "last seen value per user" grows forever unless you set a **time-to-live**. Unbounded state growth is one of the most common causes of streaming jobs slowly degrading and eventually failing.

**Kafka Streams** takes a different approach to the same problem. It's a library, not a cluster. State lives in local RocksDB stores, and every update is also written to a compacted Kafka **changelog topic**. If an instance dies, another rebuilds its state by replaying the changelog. The Oct 25 Kafka reading covered compacted topics.

## 7. Checkpoints: consistent snapshots of a running stream

If a machine fails, the processor must recover its state and resume without losing or double-counting events. Flink does this with periodic **checkpoints**, consistent snapshots of all operator state plus the input positions (such as Kafka offsets) that correspond to it.

The challenge is taking a consistent snapshot of a distributed system that never stops. Flink uses **asynchronous barrier snapshotting**, described by Paris Carbone and colleagues in 2015 ("Lightweight Asynchronous Snapshots for Distributed Dataflows") and based on the classic Chandy-Lamport algorithm for distributed snapshots:

1. The coordinator injects a **barrier** into each source's stream, marked with a checkpoint number. Sources record their current positions.
2. Barriers flow downstream with the data, never overtaking records.
3. When an operator has received the barrier on all its inputs, it snapshots its state (asynchronously, so processing continues) and forwards the barrier.
4. When every operator has acknowledged, the checkpoint is complete.

On failure, Flink restarts the job from the last completed checkpoint: every operator restores its state, and sources rewind to the recorded positions. Every event after the checkpoint is processed again, but the state was restored to exactly the point before those events, so the **state** reflects each event exactly once.

**Alignment** is the subtle part. An operator with two inputs that receives the barrier on input A must wait for the barrier on input B before snapshotting, buffering A's records in the meantime. Under backpressure, alignment can take a long time and stall checkpoints. **Unaligned checkpoints** (Flink 1.11+) let barriers overtake buffered records and include the in-flight buffers in the snapshot, which keeps checkpoints fast under load at the cost of larger snapshots.

**Savepoints** use the same mechanism, triggered manually, for upgrades: stop the job with a savepoint, deploy a new version, and start from the savepoint. Like the database migrations in the Nov 7 reading, this requires the new code's state schema to be compatible with the old.

## 8. Exactly-once, end to end

Checkpoints give exactly-once **state**. But results leave the pipeline: they're written to a database, a Kafka topic, or an API. If the job writes a result and then fails before the next checkpoint, recovery reprocesses those events and writes the result again. Exactly-once inside the processor doesn't guarantee exactly-once effects outside it.

There are two standard ways to close the gap, both covered in more depth in the Oct 23 exactly-once reading:

**Idempotent sinks.** Make writes safe to repeat, for example upserts keyed by window and key. Writing "merchant 42, 12:00–12:01, total 1,830" twice has the same effect as writing it once. This is the simplest approach and works well with the refinement model in section 5.

**Transactional sinks with two-phase commit.** Flink's two-phase commit sink framework opens a transaction in the sink for each checkpoint period. Records are written inside the transaction. When the checkpoint completes, the transactions are committed; on failure, they're aborted. Kafka's transactions (KIP-98) make this work for Kafka-to-Kafka pipelines: downstream consumers using `read_committed` only see results from completed checkpoints. The cost is latency: output becomes visible only at checkpoint intervals.

Sources need to be **replayable** for any of this to work: Kafka, Kinesis or files, where you can rewind to an offset. A source that can't replay, like a plain HTTP webhook receiver, has to put data in a durable log first.

## 9. Backpressure and operations

When a downstream operator can't keep up, a streaming system must slow the upstream rather than buffer without limit. Flink uses **credit-based flow control** between tasks: a receiver tells a sender how many buffers it can accept, and the sender stops when credit runs out. Pressure propagates back to the sources, which simply read from Kafka more slowly, and lag grows in the log rather than in memory. The Nov 1 job queue reading made the same point: a durable log is the right place for a backlog to accumulate.

Operational signals that matter for streaming jobs:

- **Consumer lag** at the sources, in records and in time. Growing lag means the job can't keep up.
- **Watermark lag**: how far the watermark trails real time. If it stops advancing, look for an idle or stuck partition.
- **Backpressure** per operator, to find the bottleneck. It's usually the operator just downstream of the last one showing backpressure.
- **Checkpoint duration and size.** Checkpoints that take longer and longer usually mean state growth or backpressure.
- **Late event counts** from side outputs.
- **Data skew**: one hot key (a huge merchant, a bot user) can overload one task while others idle. The fixes resemble those in the Oct 27 partitioning pack: salt the key and aggregate in two stages.

## 10. Streaming SQL and where things are heading

Writing jobs as code is powerful but heavy. **Streaming SQL** lets you express many pipelines declaratively:

```sql
SELECT merchant_id,
       window_start,
       SUM(amount) AS total
FROM TABLE(
  TUMBLE(TABLE payments, DESCRIPTOR(event_time), INTERVAL '1' MINUTE))
GROUP BY merchant_id, window_start, window_end;
```

Flink SQL, ksqlDB, RisingWave, Materialize and others treat a stream as a table that keeps changing and a query as a **continuously maintained result**, a view that updates incrementally as input arrives. The same ideas of event time, watermarks and retractions sit underneath. Materialize and RisingWave in particular present themselves as databases with incrementally maintained materialized views, blurring the line between stream processor and database. Real-time OLAP systems like StarRocks (Oct 23 reading) attack the same need from the database side, by making ingestion fast enough to query fresh data directly.

Choosing between them comes down to the shape of the problem:

- **Simple transformations and routing**: Kafka Streams or a lightweight consumer may be enough.
- **Large state, complex event-time logic, exactly-once across systems**: Flink is the usual choice.
- **Continuously updated query results that people or services read**: a streaming database or real-time OLAP store.
- **Results needed hourly or daily**: a batch job is still simpler and perfectly fine.

## Key takeaways

- Unbounded data needs processing over event time, not processing time, for correct and reproducible results.
- Windows group events into finite chunks; tumbling, sliding and session windows suit different questions.
- Watermarks estimate event-time progress and decide when windows fire. They trade latency against completeness, and the slowest input holds them back.
- Late data needs an explicit policy: allowed lateness, triggers, accumulation mode, and side outputs rather than silent drops.
- Streaming results are refinements; design sinks to accept updates.
- Flink partitions state by key and stores it on the heap, in RocksDB, or (since 2.0) in disaggregated remote storage. Set TTLs on state.
- Asynchronous barrier snapshots give consistent checkpoints, so state reflects each event exactly once; idempotent or transactional sinks extend that to outputs.
- Backpressure should push backlog into the durable log, and lag, watermark lag and checkpoint health are the signals to watch.

## Discussion questions

1. A dashboard shows purchases per minute. A mobile client batches events and uploads every five minutes. What goes wrong if you window by processing time, and what changes if you use event time?
2. Your watermark uses a 10-second out-of-orderness bound, but 2% of events arrive more than a minute late. What would you change, and what would it cost?
3. One Kafka partition stops receiving data. Why do all your windows stop firing, and how would you fix it?
4. Why is "exactly-once state" not the same as "exactly-once results in my database"? Which sink strategy would you use for a payments ledger, and which for a metrics dashboard?
5. A job's checkpoints grow from 2 GB to 200 GB over a month. What are the likely causes, and how would you find which one?
6. When would you choose Kafka Streams over Flink, or a streaming database over both?
7. Which of your current batch jobs would benefit from becoming streaming jobs, and which wouldn't?

## Sources

- Tyler Akidau et al., "The Dataflow Model: A Practical Approach to Balancing Correctness, Latency, and Cost in Massive-Scale, Unbounded, Out-of-Order Data Processing," VLDB 2015.
- Tyler Akidau, "Streaming 101" and "Streaming 102," O'Reilly Radar, 2015 and 2016; Tyler Akidau, Slava Chernyak and Reuven Lax, *Streaming Systems* (O'Reilly, 2018).
- Paris Carbone et al., "Lightweight Asynchronous Snapshots for Distributed Dataflows," 2015 (arXiv:1506.08603); K. Mani Chandy and Leslie Lamport, "Distributed Snapshots," ACM TOCS, 1985.
- Apache Flink documentation: "Timely Stream Processing," "Generating Watermarks," "Windows," "State Backends," "Checkpointing," "Unaligned checkpoints," "Fault Tolerance Guarantees," https://nightlies.apache.org/flink/flink-docs-stable/
- Yuan Mei et al., "Disaggregated State Management in Apache Flink 2.0," PVLDB 18, https://www.vldb.org/pvldb/vol18/p4846-mei.pdf
- Apache Flink blog, "An Overview of End-to-End Exactly-Once Processing in Apache Flink" (2018).
- Apache Kafka documentation, "Kafka Streams: Architecture" (state stores and changelogs); KIP-98, "Exactly Once Delivery and Transactional Messaging."
- Martin Kleppmann, *Designing Data-Intensive Applications* (O'Reilly, 2017), chapter 11 "Stream Processing."
- Materialize and RisingWave documentation on incrementally maintained views.

The Flink 2.0 disaggregated-state paper was located in a search on Nov 8, 2026. The other details come from the cited papers and Flink documentation as I know them. Flink option names and defaults change between versions, so check the docs for the version you run.
