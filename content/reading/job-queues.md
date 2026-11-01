---
title: "Job queues: leases, retries, dead letters and backpressure"
date: 2026-11-01
summary: How work queues like SQS, RabbitMQ and Postgres-backed job systems really behave, covering at-least-once delivery, visibility timeouts and acks, retries and dead-letter queues, ordering and fairness, backpressure, and what Slack learned when its job queue fell over.
tags: messaging, distributed systems
daily: true
---

Sending an email, resizing an image, charging a card after checkout, rebuilding a search index, notifying a webhook: most backends push this kind of work onto a queue and let workers process it in the background. Job queues look simple. A producer puts a message in, a worker takes it out and does the work. Nearly all the interesting behavior lives in the gaps: what happens when the worker crashes halfway, when a job fails forever, when producers outrun consumers, or when the queue itself becomes the bottleneck. This reading covers work queues as distinct from Kafka-style logs (the Oct 25 reading), using Amazon SQS, RabbitMQ and Postgres-backed job systems as concrete examples, and ends with how Slack rebuilt its job queue after it failed during an incident.

## 1. Queues versus logs

The Kafka reading described a **log**: an append-only sequence where messages stay after being read, each consumer tracks its own offset, and many consumer groups can read the same data independently. A **work queue** is different:

- Each message is meant to be processed by **one** worker.
- Once processed, the message is **deleted**.
- Workers compete for messages, and the queue hands out messages one at a time (or in small batches) rather than by partition.
- Failed messages can be **retried individually**, without blocking the messages behind them.

That last point is the practical difference. In a Kafka partition, a message that keeps failing blocks everything after it in that partition unless you build retry topics yourself. In a work queue, a failing message goes back for a retry while other workers keep processing the rest. That makes queues a natural fit for independent tasks, such as "send this email," where order rarely matters and per-message retries matter a lot.

The trade-off is that a queue usually can't be replayed (processed messages are gone) and doesn't give you strict ordering at scale. Systems that need both patterns often use both: a log as the durable record of events, and queues for the tasks those events trigger.

## 2. At-least-once is the default

Every practical queue faces the same problem the exactly-once reading on Oct 23 described: a worker takes a message, does the work, and crashes before telling the queue it's done. The queue can't tell "the worker crashed before doing anything" from "the worker finished but the acknowledgment was lost." It has two options:

- **Remove the message when it's handed out** (at-most-once). If the worker crashes, the work is lost.
- **Keep the message until the worker confirms** (at-least-once). If the worker crashes, the message is delivered again, and the work may happen twice.

Almost every job system chooses at-least-once, because lost work is usually worse than duplicated work, and duplicates can be made harmless. That leads to the most important rule in this reading: **job handlers must be idempotent**. Running a job twice must have the same effect as running it once. Common techniques:

- Use natural idempotency: "set the user's plan to Pro" is safe to repeat; "add one month to the subscription" isn't.
- Record a job ID or idempotency key in the same database transaction as the job's effect, and skip jobs already recorded.
- Pass an idempotency key to external APIs that support one (Stripe's API does) so retries don't charge twice.
- Make side effects conditional on state: "send the receipt if `receipt_sent_at` is null," then set it.

## 3. Leases: visibility timeouts and acks

How does a queue know a worker is done? There are two main models.

<!--diagram:queue-lease-1101-->

### SQS: the visibility timeout

Amazon SQS, launched in 2006 and one of AWS's first services, uses a **lease**. When a worker calls `ReceiveMessage`, SQS doesn't remove the message. It hides it for the **visibility timeout** (30 seconds by default, configurable up to 12 hours). If the worker calls `DeleteMessage` with the message's receipt handle before the timeout expires, the message is gone. If not, because the worker crashed, hung, or is simply slow, the message becomes visible again and another worker receives it.

The visibility timeout is the most important SQS setting, and it's easy to get wrong:

- **Too short**, and a job that's still running becomes visible again, and a second worker starts it. Now two workers are doing the same work at once. Idempotency protects correctness, but you waste capacity, and slow jobs can snowball as each one is picked up again and again.
- **Too long**, and a crashed worker's message sits invisible for that whole time before anyone retries it.

The usual solution is a moderate timeout plus **heartbeating**: a long-running worker periodically calls `ChangeMessageVisibility` to extend its lease while it's still making progress. Most SQS client libraries can do this for you.

A few other SQS details shape how you use it:

- **Long polling.** `ReceiveMessage` with `WaitTimeSeconds` (up to 20 seconds) waits for messages to arrive instead of returning empty immediately. It reduces cost and empty responses dramatically.
- **Retention.** Messages are kept for 4 days by default, configurable up to 14 days. A message nobody processes in that time is silently deleted.
- **Size.** The maximum message payload was 256 KiB for many years. AWS raised it to 1 MiB in August 2025. For anything bigger, store the payload in S3 and put a pointer in the message.
- **Delay.** Messages can be delayed up to 15 minutes before becoming visible, which is handy for simple deferred work but too short for real scheduling.
- **Standard vs FIFO.** Standard queues offer at-least-once delivery with best-effort ordering and nearly unlimited throughput. FIFO queues give ordering within a message group and deduplication within a 5-minute window, at lower throughput limits.

### RabbitMQ: acknowledgments and prefetch

RabbitMQ (and other AMQP 0-9-1 brokers) push messages to consumers over a long-lived connection. A consumer with manual acknowledgments sends `basic.ack` when it's done. If the consumer's connection or channel closes before it acks, the broker **requeues** the message for another consumer. There's no timer by default: the lease lasts as long as the connection (RabbitMQ does enforce a delivery acknowledgment timeout, 30 minutes by default in recent versions, to catch consumers that never ack).

Consumers can also reject a message with `basic.nack` or `basic.reject`, either requeueing it or sending it to a dead-letter exchange.

The setting that matters most for performance is **prefetch** (`basic.qos`): how many unacknowledged messages the broker will push to one consumer at a time. With no limit, a fast producer can push thousands of messages into one consumer's memory, starving other consumers and losing all of them back to the queue if that consumer dies. With prefetch 1, every message waits for a round trip, and throughput suffers. RabbitMQ's documentation suggests values in the range of roughly 100 to 300 as a reasonable starting point for many workloads, then tuning from there. For slow jobs, a small prefetch keeps work spread evenly across consumers.

RabbitMQ's routing model is also worth knowing. Producers publish to **exchanges**, not queues. A **direct** exchange routes by exact key, a **topic** exchange by pattern (`orders.*.created`), a **fanout** exchange to every bound queue. This makes it easy to have one event feed several queues, each with its own consumers.

For durability, modern RabbitMQ uses **quorum queues**, introduced in version 3.8 in 2019 and replicated with Raft (the Oct 27 reading). Classic mirrored queues, the older replication mechanism, were removed in RabbitMQ 4.0 in 2024.

## 4. Retries, backoff and dead letters

Jobs fail for two very different reasons, and a good retry policy treats them differently.

**Transient failures**: a timeout, a 503 from a dependency, a deadlock, a rate limit. Retrying later will probably work.

**Permanent failures**: malformed data, a bug in the handler, a deleted record, a validation error. Retrying will never work.

The problem is that a handler usually can't tell which kind it's facing, so queues use a combination of policies.

**Exponential backoff with jitter.** Wait longer between each retry (for example 1s, 2s, 4s, 8s, plus randomness) so a struggling dependency gets breathing room and retries from many jobs don't arrive in lockstep. The rate-limiting reading on Oct 22 covered why jitter matters. SQS has no built-in per-message backoff, but you can approximate it by setting a longer visibility timeout on failure with `ChangeMessageVisibility`. Job frameworks such as Sidekiq, Celery, Oban and BullMQ build backoff in.

**Maximum attempts.** After a fixed number of tries, stop. In SQS this is the **redrive policy**: when a message's receive count exceeds `maxReceiveCount`, SQS moves it to a configured **dead-letter queue (DLQ)**. RabbitMQ uses dead-letter exchanges, and quorum queues can enforce a delivery limit.

**Dead-letter queues** are where poison messages go to stop hurting everything else. A message that crashes every worker that touches it would otherwise loop forever, consuming capacity and filling logs. A DLQ isolates it. But a DLQ only helps if someone looks at it:

- **Alert on DLQ depth.** A DLQ that silently fills up is lost work with extra steps.
- **Keep the failure reason** with the message (exception, attempt count, timestamps), or in logs linked by message ID.
- **Have a redrive process.** Once the bug is fixed, move messages back to the main queue. SQS supports redrive from a DLQ back to the source queue directly.
- **Set DLQ retention longer than the source queue's**, so failed messages don't expire before anyone investigates.

**Classify errors where you can.** If the handler knows an error is permanent, such as a validation failure, send the message straight to the DLQ or drop it with a log entry, rather than burning retries.

## 5. Ordering and fairness

### Ordering

Work queues generally don't guarantee order. SQS standard queues are best-effort. Even with a strictly ordered queue, parallel workers finish in different orders, and retries reorder things further. If ordering matters for some jobs (all updates to one account, say), you need to partition:

- **SQS FIFO** queues guarantee ordering within a **message group ID**. Only one message per group is in flight at a time, so different groups process in parallel while each group is serial.
- **Kafka**, by partition key, gives the same guarantee per key.
- **Application-level versioning** is often simpler: include a version or timestamp in each job and have the handler ignore jobs older than the state it already has.

A useful question to ask before requiring ordering: can the handler instead fetch the current state and act on it? "Sync user 42's profile to the CRM" is order-independent if the job reads the latest profile when it runs, rather than carrying a snapshot.

### Fairness and priorities

Shared queues have a noisy-neighbor problem. If one customer enqueues a million jobs, everyone else's jobs wait behind them. Common fixes:

- **Separate queues per priority** (critical, default, bulk), with workers polling higher priorities first or with dedicated worker pools per queue. Sidekiq supports weighted queue polling for this.
- **Per-tenant queues or rate limits**, so one tenant can't take more than a fair share. Some systems shard queues by tenant and round-robin across them.
- **Concurrency limits per job type**, so a flood of slow report jobs can't occupy every worker.

Don't put very different job types on one queue with one worker pool. A 50 ms email job and a 10-minute export job have different timeouts, different retry needs and different scaling, and the slow ones will block the fast ones.

## 6. Backpressure and capacity

A queue absorbs bursts: producers can briefly outrun consumers, and the backlog drains later. That's its main benefit. But if producers *consistently* outrun consumers, the queue grows without bound, latency for every job grows with it, and eventually something breaks: memory on the broker, retention limits, or the business tolerance for delay.

**Little's Law** gives the basic relationship: the average number of jobs in the system equals the arrival rate multiplied by the average time each job spends in it. If 200 jobs per second arrive and each takes 0.5 seconds, you need at least 100 concurrent workers just to keep up, and more to drain a backlog.

The metrics that matter for a queue:

- **Age of the oldest message**, not just queue depth. Depth tells you how much work is waiting; age tells you how late it is. SQS exposes `ApproximateAgeOfOldestMessage` in CloudWatch, and it's the best single alerting signal for most queues.
- **Arrival and completion rates**, so you can see whether the backlog is growing.
- **Failure and retry rates**, and DLQ depth.
- **Processing time per job type**, as a histogram (see the observability reading from Oct 31).

**Autoscaling workers** on queue age or depth is the standard response. Kubernetes KEDA and AWS's own guidance scale consumers on backlog per worker. But scaling consumers has limits: the downstream dependency the jobs call (a database, a third-party API) has its own capacity, and doubling workers can just double the load on a struggling database. A worker pool should respect the capacity of what it calls, with concurrency limits or rate limits.

**Backpressure** means pushing the problem back to producers when the system is overloaded, instead of letting queues grow forever:

- Bounded queues that reject new work when full (RabbitMQ supports `x-max-length` with a configurable overflow behavior that can reject publishes).
- Producers that check queue age and shed or defer low-priority work.
- Making the slowness visible to users ("your export will be ready in about an hour") rather than accepting work with an implied promise you can't keep.

## 7. Queues in your database

A popular alternative to a separate broker is to keep the queue in the database you already run. Postgres made this practical with `SELECT ... FOR UPDATE SKIP LOCKED`, added in version 9.5 (2016). A worker claims a job like this:

```sql
UPDATE jobs SET state = 'running', locked_at = now(), attempts = attempts + 1
WHERE id = (
  SELECT id FROM jobs
  WHERE state = 'available' AND run_at <= now()
  ORDER BY priority, run_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *;
```

`SKIP LOCKED` makes each worker skip rows that other workers have already locked, instead of waiting on them, so many workers can claim jobs concurrently without contention. Libraries built on this include Oban (Elixir), GoodJob and Solid Queue (Ruby; Solid Queue is the default in Rails 8), River (Go) and graphile-worker (Node).

The big advantage is **transactional enqueueing**. You can insert the job in the same transaction as the business change it relates to. If the transaction rolls back, the job never existed. If it commits, the job is guaranteed to be there. With a separate broker, "write to the database, then publish the message" can fail between the two steps, which is exactly the problem the transactional outbox pattern (Oct 23 reading) exists to solve. A database queue gives you the outbox for free.

The trade-offs:

- **Throughput.** Every claim and completion is a row update. Postgres handles thousands of jobs per second comfortably on good hardware, but not the hundreds of thousands per second a dedicated broker can.
- **Table bloat.** Constant updates and deletes create dead tuples that vacuum must clean (the Oct 24 Postgres MVCC reading explains why). Busy job tables need aggressive autovacuum settings, and long-running transactions elsewhere in the database can stop vacuum from cleaning up, slowing every claim query.
- **Shared fate.** A job backlog competes with your application for database CPU, I/O and connections.

For many applications, especially early on, a Postgres queue is the right default: one fewer system to run, and transactional guarantees that are hard to get otherwise. Move to a dedicated broker when throughput or isolation demands it.

## 8. Case study: Slack's job queue

Slack's 2017 post "Scaling Slack's Job Queue" describes a queue under real stress. Slack's original system used **Redis** as the queue: web servers enqueued jobs (message posting side effects, push notifications, link unfurling, billing tasks) into Redis lists, and worker machines dequeued and ran them.

The post describes an incident in which a slowdown in a database used by some jobs caused a large backlog to build in Redis. Redis ran out of memory, and once it was full, jobs couldn't be enqueued, and because dequeuing in their design also needed some free memory, workers couldn't make progress either. The queue that was supposed to absorb the problem became the outage.

The lessons Slack drew are general ones:

- **An in-memory queue has a hard ceiling.** A backlog is precisely when the queue needs the most room, and that's when an in-memory queue runs out.
- **Enqueue and dequeue need to be decoupled.** If a full queue blocks consumers, there's no way to drain it.
- **Scaling the queue shouldn't require downtime or painful reconfiguration.**

Slack's fix was to put **Kafka in front of Redis**. Web servers now write jobs to Kafka through a small service the post calls Kafkagate. A relay service, JQRelay, reads from Kafka and feeds jobs into Redis at a rate the workers can handle. Kafka, with its durable, disk-based log, absorbs backlogs of almost any size, while Redis remains a small working set for the workers. They migrated gradually, with traffic shifted queue by queue, without stopping the existing system.

It's a good example of combining patterns: a log for durable buffering, a queue for job distribution, and a buffer between them that provides backpressure.

## 9. Other things that bite in production

**Scheduled and delayed jobs.** "Send a reminder in 3 days" needs a scheduler, not just a queue. SQS delays cap at 15 minutes. Database queues handle this naturally with a `run_at` column. Redis-based systems (Sidekiq, BullMQ) use sorted sets keyed by time. Cron-style recurring jobs need a single leader, or a uniqueness constraint, so that ten workers don't each run the nightly report.

**Unique jobs.** Enqueueing "reindex product 42" ten times in a second should usually produce one job. Use a uniqueness key with a time window (many frameworks support this) or let idempotency absorb the duplicates.

**Large payloads.** Pass references, not data. Put the product ID in the job, not the whole product, so the job reads current data and the queue stays small.

**Deploys and in-flight jobs.** When workers restart, running jobs are interrupted. Handle `SIGTERM` by stopping new claims and finishing (or releasing) current jobs within a grace period. Long jobs should be resumable, with checkpoints, so a restart doesn't start from zero.

**Schema changes.** Jobs enqueued by the old code are processed by the new code. Treat job payloads like an API with versioning, as in the Oct 26 API design reading: add fields compatibly, and keep handlers able to process the old format until old jobs have drained.

**Observability.** Propagate trace context in message attributes (the Oct 31 reading), so a slow job links back to the request that created it, and log job ID, type, attempt number and queue latency with every job.

## 10. Choosing a queue

| Need | Good fit |
|---|---|
| Simple managed queue, unlimited scale, AWS | SQS standard, with a DLQ |
| Per-key ordering, deduplication, moderate rate | SQS FIFO with message group IDs |
| Flexible routing, fanout, self-hosted | RabbitMQ with quorum queues |
| Transactional enqueueing, modest throughput, already on Postgres | Postgres with `SKIP LOCKED` (Oban, River, GoodJob, Solid Queue) |
| Language-native jobs with scheduling and UI | Sidekiq or BullMQ on Redis (watch memory), Celery |
| Replay, multiple independent consumers, huge throughput | A log (Kafka), possibly feeding queues |

## Key takeaways

- Work queues give each message to one worker and retry messages individually; logs keep messages and let many consumers replay them.
- Delivery is at-least-once in practice, so every job handler must be idempotent.
- A received message is leased: SQS uses a visibility timeout, RabbitMQ uses acks tied to the connection. Tune the lease to job duration and heartbeat long jobs.
- Retries need exponential backoff with jitter, a maximum attempt count, and a dead-letter queue that someone monitors and can redrive.
- Ordering costs parallelism; use message groups or partitions only where needed, or design handlers to read current state.
- Alert on the age of the oldest message, scale workers on backlog, and respect downstream capacity.
- Postgres with `SKIP LOCKED` gives transactional enqueueing for free and is often the right default at moderate scale.
- Slack's outage shows that an in-memory queue fails exactly when a backlog builds, and that a durable log in front can absorb it.

## Discussion questions

1. A job calls a payment API, then updates a database row. The worker crashes between the two steps and the job is retried. What happens, and how would you make the job safe to retry?
2. Your SQS jobs usually take 10 seconds but occasionally take 5 minutes. How would you set the visibility timeout, and what would you add?
3. Your DLQ has 40,000 messages after a weekend. What process would you follow before redriving them?
4. When would you move a Postgres-backed queue to a dedicated broker? What metrics would tell you it's time?
5. One customer enqueues a million low-priority jobs. Design a setup so other customers' jobs still run within a minute.
6. Slack put Kafka in front of Redis rather than replacing Redis. What does each layer contribute, and what new failure modes does the extra layer add?
7. Which background jobs in your systems are not idempotent today, and what would break if one ran twice?

## Sources

- Amazon SQS Developer Guide: visibility timeout, dead-letter queues and redrive, long polling, message quotas, FIFO queues, https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/
- AWS, "Amazon SQS increases maximum message payload size to 1 MiB" (August 2025), https://aws.amazon.com/about-aws/whats-new/2025/08/amazon-sqs-max-payload-size-1mib
- RabbitMQ documentation: "Consumer Acknowledgements and Publisher Confirms," "Consumer Prefetch," "Quorum Queues," "Dead Letter Exchanges," "Queue Length Limit," https://www.rabbitmq.com/docs
- Slack Engineering, "Scaling Slack's Job Queue" (2017), https://slack.engineering/scaling-slacks-job-queue/
- PostgreSQL documentation, `SELECT ... FOR UPDATE SKIP LOCKED` (since 9.5), https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE
- Oban, River, GoodJob and Solid Queue project documentation.
- Sidekiq wiki, "Best Practices" (idempotency, small payloads) and "Error Handling," https://github.com/sidekiq/sidekiq/wiki
- Marc Brooker, "Exponential Backoff And Jitter," AWS Architecture Blog, 2015.
- John D. C. Little, "A Proof for the Queuing Formula: L = λW," Operations Research, 1961.

The SQS payload limit change was confirmed against AWS's August 2025 announcement on Nov 1, 2026, and the Slack post was located the same day. SQS's other limits and the RabbitMQ defaults come from their documentation as I know it, so check the current docs before relying on a specific number.
