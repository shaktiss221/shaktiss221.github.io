---
title: "Timeouts, retries, circuit breakers and the failures they cause"
date: 2026-11-11
summary: The resilience patterns every service-to-service call needs, and how each one goes wrong. Covers timeouts and deadline propagation, retries with backoff, jitter and budgets, retry amplification, hedged requests, circuit breakers, bulkheads, load shedding, and metastable failures where the fixes become the outage.
tags: distributed systems, APIs
daily: true
---

Every network call can fail, be slow, or succeed without you finding out. In a system of dozens of services, that's not an edge case; something is always degraded somewhere. The standard toolkit for living with it is well known: timeouts, retries, circuit breakers, bulkheads. What's less well known is how often these mechanisms cause the outages they're meant to prevent. Retries multiply load on a struggling service, timeouts set without thought leave threads waiting long after the caller gave up, and a system can get stuck in a degraded state long after the original trigger has gone. This reading covers each pattern, how to configure it, and the failure mode it can create, drawing on Amazon's Builders' Library, Google's SRE work, Michael Nygard's *Release It!*, and recent research on metastable failures.

## 1. Timeouts: the pattern everything else depends on

A call without a timeout can wait forever. If the downstream service hangs, the caller's thread, connection or goroutine is stuck, and when enough of them are stuck, the caller stops serving everyone. That's how one slow dependency takes down a service that calls it, and then the services that call that one. Nygard's *Release It!* (2007, second edition 2018) calls this a **cascading failure** and argues that unbounded waits are one of the most common causes.

So every remote call needs a timeout. Many libraries' defaults are infinite or very long, so you must set them explicitly, and there's usually more than one: connection timeout, TLS handshake timeout, time to first byte, total request time.

How long should a timeout be? Marc Brooker's Builders' Library article "Timeouts, retries, and backoff with jitter" describes Amazon's usual approach: pick an acceptable rate of false timeouts (say 0.1%) and set the timeout at the corresponding percentile of the downstream's latency (here, p99.9), with some padding. Too short, and you time out requests that would have succeeded, and if you retry them, you add load. Too long, and stuck calls tie up resources.

Two subtleties:

- **Latency distributions shift.** A timeout set from last month's p99.9 may be too tight after a dependency adds a feature. Revisit timeouts when latency changes, and alert on timeout rates.
- **The callee doesn't know you gave up.** When the caller times out, the downstream usually keeps working on the request, consuming resources for an answer nobody will read. That's wasted work, and under load it's a big part of why systems fall over.

### Deadlines, not just timeouts

The second subtlety is solved by **deadline propagation**. Instead of each layer having its own independent timeout, the original caller sets an absolute deadline ("this request must finish by 12:00:00.800"), and every downstream call carries the remaining time. gRPC does this natively: a client deadline is sent with the request, servers can check it, and outgoing calls inherit what's left. The Oct 25 gRPC pack reading covered the mechanics.

Deadlines give two benefits:

1. **No wasted work.** A server receiving a request with 5 ms left, for an operation that takes 50 ms, can reject it immediately. A queue worker can drop requests whose deadline has passed instead of processing them.
2. **Consistent budgets.** Without propagation, a service with a 1-second timeout might call one with a 2-second timeout, which is nonsense: the inner call can keep running after the outer one has given up.

## 2. Retries: useful and dangerous

Many failures are transient: a dropped connection, a server restarting, a brief overload. Retrying turns many of those into successes, and for a single client, retries almost always look like a good idea. At system scale, they're one of the most dangerous tools in the box.

### When to retry

Only retry when it's safe and likely to help:

- **The operation must be idempotent**, or made idempotent with an idempotency key (Oct 26 API design and Nov 9 webhooks readings). Retrying a non-idempotent "charge card" request after a timeout can charge twice.
- **The error must be retryable.** Connection failures, `503 Service Unavailable`, `429 Too Many Requests` (after `Retry-After`) and gRPC `UNAVAILABLE` usually are. `400 Bad Request`, `401`, `403`, `404` and validation errors aren't: retrying won't change the answer.
- **There must be time left** before the deadline.

### Backoff and jitter

Retrying immediately hits the struggling server again at the worst moment. **Exponential backoff** waits longer each time: 100 ms, 200 ms, 400 ms, and so on, up to a cap. But if many clients failed at the same moment (say, because the server restarted), they'll all retry at the same moments too, in synchronized waves. **Jitter** randomizes the waits. Brooker's 2015 AWS Architecture Blog post "Exponential Backoff And Jitter" compared strategies by simulation and found that "full jitter," sleeping a random time between zero and the exponential backoff value, greatly reduced both total work and completion time compared with no jitter. The Oct 22 rate-limiting reading covered this in more detail.

### Retry amplification

<!--diagram:retry-amplification-1111-->

The deeper problem is that retries **multiply across layers**. Suppose a request passes through four layers before reaching a database, and each layer makes up to 3 retries (4 attempts in total) when the call below fails. If the database starts failing, the gateway makes 4 attempts to service A, each of which makes 4 attempts to service B, each of which makes 4 attempts to the database. One user request becomes up to 4⁴ = 256 database calls, at exactly the moment the database is struggling. A small overload becomes a total one.

Defenses:

- **Retry at one layer only**, usually the one closest to the user or the one with the most context. Inner layers fail fast and let the outer layer decide. Google's SRE book recommends this in its chapter on cascading failures.
- **Retry budgets.** Limit retries to a fraction of normal traffic, for example 10% extra across a client. When failures are rare, every failed request can be retried. When many requests fail, the budget runs out and retries stop, so retries can't multiply load during an outage. gRPC's retry policy includes retry throttling based on a token bucket for exactly this, and Envoy and Finagle support retry budgets.
- **Per-request retry caps** as well (for example, at most 2 retries), so one request can't monopolize the budget.
- **Respect the server's signals.** Honor `Retry-After`, and have overloaded servers say explicitly that a request shouldn't be retried (some systems use a response header or a specific error code for this).

## 3. Hedged requests: retries for latency

Retries handle failures. **Hedged requests** handle slowness. Jeff Dean and Luiz André Barroso's 2013 paper "The Tail at Scale" (which also appeared in the Oct 31 observability reading) describes the idea: send a request to one replica, and if it hasn't answered within, say, the 95th-percentile expected latency, send the same request to a second replica and use whichever answers first.

Because the second request is only sent for the slowest few percent, the extra load is small, but it cuts tail latency dramatically, since the chance of both replicas being slow is much lower. The paper gives a Google benchmark: reading 1,000 keys spread across 100 BigTable servers, sending a hedge after a 10 ms delay cut the 99.9th-percentile latency from 1,800 ms to 74 ms while sending only about 2% more requests.

Refinements and cautions:

- **Tied requests.** Send to two servers at once, each told about the other. When one starts processing, it tells the other to cancel. This avoids even the small delay before hedging, at the cost of more coordination.
- **Only hedge idempotent reads.** Hedging writes is how you get duplicates.
- **Hedge only when the system has spare capacity.** Under overload, hedging adds load just like retries do. Tie hedges to the same retry budget.

gRPC supports hedging as a configurable policy, alongside its retry policy.

## 4. Circuit breakers

Even with backoff and budgets, a caller that keeps calling a dependency that's clearly down wastes time on every request: each call waits for its timeout before failing. A **circuit breaker**, popularized by Nygard's *Release It!*, wraps calls to a dependency and tracks failures:

- **Closed**: calls go through normally. If failures exceed a threshold (for example, 50% of calls in the last 10 seconds, with a minimum number of calls), the breaker **opens**.
- **Open**: calls fail immediately, without touching the dependency. After a cool-down period, the breaker becomes **half-open**.
- **Half-open**: a few trial calls are allowed through. If they succeed, the breaker closes; if they fail, it opens again.

Circuit breakers help in two ways. Callers fail fast instead of waiting for timeouts, which keeps their own threads and connections free. And the struggling dependency gets breathing room to recover instead of being hammered.

Netflix's **Hystrix** library made circuit breakers mainstream in the 2010s. Netflix moved it to maintenance mode in 2018, recommending adaptive approaches and libraries like **resilience4j** for the JVM. Service meshes and proxies (Envoy's circuit breaking and outlier detection, Oct 30 reading) now provide similar protection outside application code.

Pitfalls:

- **What does the caller do when the breaker is open?** Failing fast is only useful if there's a sensible **fallback**: cached data, a default, a degraded feature ("recommendations unavailable"), or a clear error. A breaker that just turns slow errors into fast errors helps the caller's resources, but not the user.
- **Granularity matters.** One breaker for a whole dependency can open because one endpoint or one shard is failing, cutting off healthy parts too. Per-endpoint or per-host breakers are more precise.
- **Thresholds need tuning** for low traffic, where a couple of failures can look like 100%. Use a minimum request count.

## 5. Bulkheads

Ships are divided into watertight compartments, called bulkheads, so a hull breach floods one compartment instead of sinking the ship. In software, a **bulkhead** isolates resources so one failing part can't consume everything.

The classic example: a service with one shared pool of 200 threads calls three dependencies. Dependency C becomes slow, every thread ends up waiting on C, and calls to the perfectly healthy A and B can't get a thread. With bulkheads, each dependency gets its own limited pool (or a concurrency limit, such as a semaphore), so C can tie up at most its own share.

Bulkheads appear at many levels:

- **Per-dependency connection pools and concurrency limits** inside a service.
- **Separate worker pools or queues per job type**, as in the Nov 1 job queue reading.
- **Separate deployments for critical and non-critical traffic**: a dedicated cluster for checkout, isolated from the cluster serving recommendations.
- **Cell-based architecture**, where a service is split into independent copies ("cells"), each serving a subset of customers, so a bad deploy or a poison request takes down one cell, not all. AWS has written extensively about using cells to limit blast radius, and it's the same principle as the progressive rollouts in the Nov 7 reading.

## 6. Load shedding and adaptive concurrency

All the patterns above protect callers. Servers need to protect themselves too, because the most reliable way to handle more load than you can serve is to reject the excess quickly, rather than accept everything and serve nobody well.

The Builders' Library article "Using load shedding to avoid overload" describes the core problem: as a server becomes overloaded, latency rises, clients time out, and the server ends up spending its capacity on requests whose callers have already given up. Throughput of *useful* work drops toward zero even though the server is at 100% CPU. Rejecting excess requests early, with a cheap `503`, keeps the accepted requests fast.

Good load shedding:

- **Rejects early and cheaply**, before doing expensive work like parsing a large body or querying a database.
- **Drops requests whose deadline has already passed**, and serves queues in an order that favors fresh requests (some systems switch to LIFO under overload, since the newest request is most likely to still be wanted).
- **Prioritizes**: shed batch and background traffic before user-facing traffic, as the Google SRE book's criticality levels describe.

Fixed concurrency limits are hard to tune, because the right limit changes with request mix and dependency health. **Adaptive concurrency** algorithms adjust the limit automatically using latency, similar to TCP congestion control. Netflix's **concurrency-limits** library (using algorithms like Vegas and gradient-based limits) and Envoy's adaptive concurrency filter both work this way: if latency rises above a measured baseline, they lower the limit; if it's healthy, they raise it.

## 7. Metastable failures: when the fix becomes the outage

Some outages don't end when their trigger ends. A brief spike, a short network blip or a cache flush pushes the system into a bad state, and then the system stays there, even after the trigger is gone, because the system's own behavior keeps it overloaded. Nathan Bronson, Aleksey Charapko, Abutalib Aghayev and Timothy Zhu named this **metastable failure** in a 2021 HotOS paper, and a 2022 OSDI paper by Lexiang Huang and colleagues, "Metastable Failures in the Wild," studied real incidents at many companies and found them to be a recurring cause of major outages.

A metastable failure has three parts:

1. A system running in a **vulnerable state**: efficient and stable, but with little slack.
2. A **trigger**: a load spike, a deploy, a cache flush, a dependency hiccup.
3. A **sustaining effect**: some feedback loop that keeps the system overloaded after the trigger ends.

Retries are the most common sustaining effect. A brief slowdown causes timeouts; timeouts cause retries; retries add load; more load causes more timeouts. Even after the original trigger is gone, the retry traffic alone keeps the system above capacity. Other sustaining effects include **cache misses** (a cold cache sends more traffic to the database, which slows down, so cache fills are slower), **connection storms** after restarts, and garbage collection pressure from queued work.

Marc Brooker's blog post on the paper sums up the practical implication: systems often run most efficiently close to the edge of a cliff, and the techniques that help most are those that weaken the feedback loops: retry budgets rather than unlimited retries, load shedding, deadline-aware queues, and the ability to drop traffic sharply to let the system recover.

Recovering from a metastable state usually requires **reducing load well below the original level**, not just removing the trigger: temporarily shedding a large fraction of traffic, disabling retries, or warming caches before letting traffic back in. Teams that know this can recover in minutes; teams that wait for the trigger to pass can stay down for hours.

## 8. Putting it together: a call policy

For each service-to-service call, decide and write down:

| Question | Typical answer |
|---|---|
| Timeout? | Around the callee's p99.9 plus padding, and never more than the remaining deadline |
| Deadline propagation? | Yes, pass remaining time downstream |
| Retries? | Only at one layer, only for idempotent operations and retryable errors, with exponential backoff, full jitter and a retry budget |
| Hedging? | For latency-critical idempotent reads, after p95, within the retry budget |
| Circuit breaker? | Per dependency or endpoint, with a fallback defined |
| Bulkhead? | A concurrency limit per dependency |
| Fallback? | Cached, default, degraded, or a clear error |

And for each service as a server:

- Reject excess load early, ideally with adaptive concurrency limits.
- Drop requests whose deadlines have passed.
- Prioritize by criticality.
- Expose metrics for timeouts, retries, breaker state and shed requests.

## 9. Testing resilience

These mechanisms only run during failures, so they're rarely exercised and often broken. Ways to find out before an incident:

- **Fault injection in tests**: add latency, errors and dropped connections to dependencies in integration tests, and check that timeouts, retries and fallbacks behave as designed.
- **Chaos engineering in production**, carefully: Netflix's Chaos Monkey (randomly terminating instances) started the practice, and later tools inject latency and failures at the network or service-mesh level. Start small, with clear abort conditions.
- **Load tests past capacity**, to see whether the system degrades gracefully (sheds load, keeps serving accepted requests) or collapses. Run the overload, then reduce load back to normal and check that the system actually recovers, which is the test for metastability.
- **Game days**, where teams rehearse a dependency outage and practice the recovery steps, including shedding load and disabling retries.

## Key takeaways

- Every remote call needs a timeout. Set it from measured latency and propagate absolute deadlines so downstream work stops when the caller gives up.
- Retry only idempotent operations on retryable errors, with exponential backoff and full jitter.
- Retries multiply across layers; retry at one layer, cap per-request retries, and use retry budgets.
- Hedged requests cut tail latency for a small amount of extra load: in Google's example, p99.9 fell from 1,800 ms to 74 ms with about 2% more requests.
- Circuit breakers fail fast and give dependencies room to recover, but need a real fallback.
- Bulkheads and cells keep one failing dependency or customer from consuming everything.
- Servers must shed load early; adaptive concurrency limits do this automatically.
- Metastable failures persist after the trigger because of feedback loops like retries and cold caches; recovery often requires cutting load far below normal.

## Discussion questions

1. Your service has a 2-second timeout and calls a dependency with a 5-second timeout. What goes wrong, and how does deadline propagation fix it?
2. Count the layers in one of your request paths and the retries at each. What's the worst-case amplification at the bottom?
3. A retry budget of 10% means that during an outage, 90% of failed requests aren't retried. Is that acceptable for your users? What would you do instead for a payment flow?
4. When does a circuit breaker make things worse? Think about a breaker that opens because one shard is failing.
5. Describe a metastable failure you've seen or can imagine in your systems. What was the sustaining effect, and how would you break the loop?
6. How would you test that your system recovers after an overload, not just that it survives one?
7. Where would hedged requests help in your systems, and where would they be dangerous?

## Sources

- Marc Brooker, "Timeouts, retries, and backoff with jitter," Amazon Builders' Library, https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/
- Marc Brooker, "Exponential Backoff And Jitter," AWS Architecture Blog, 2015.
- David Yanacek, "Using load shedding to avoid overload," Amazon Builders' Library; Amazon Builders' Library articles on avoiding insurmountable queue backlogs and on cell-based architecture.
- Jeffrey Dean and Luiz André Barroso, "The Tail at Scale," Communications of the ACM, February 2013; summary at https://warrenqi.github.io/2020/03/28/tail-at-scale.html
- Michael T. Nygard, *Release It! Design and Deploy Production-Ready Software*, 2nd edition (Pragmatic Bookshelf, 2018).
- Betsy Beyer et al. (eds.), *Site Reliability Engineering* (O'Reilly, 2016), chapter 21 "Handling Overload" and chapter 22 "Addressing Cascading Failures," https://sre.google/sre-book/addressing-cascading-failures/
- Nathan Bronson, Aleksey Charapko, Abutalib Aghayev and Timothy Zhu, "Metastable Failures in Distributed Systems," HotOS 2021.
- Lexiang Huang et al., "Metastable Failures in the Wild," OSDI 2022, https://www.usenix.org/system/files/osdi22-huang-lexiang.pdf
- Marc Brooker, "Metastability and Distributed Systems," 2021, https://brooker.co.za/blog/2021/05/24/metastable/
- gRPC documentation, "Deadlines," "Retry" and "Request Hedging," https://grpc.io/docs/guides/
- Netflix Hystrix README (maintenance-mode notice, 2018); resilience4j documentation; Netflix concurrency-limits, https://github.com/Netflix/concurrency-limits
- Envoy documentation, "Circuit breaking," "Retry budgets," "Adaptive concurrency."

The metastable-failure papers and Brooker's post were located in searches on Nov 11, 2026. The Tail at Scale hedging numbers are from the paper as I know it and match commonly cited summaries.
