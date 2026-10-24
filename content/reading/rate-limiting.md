---
title: Rate limiting, from token buckets to global limits
date: 2026-10-22
summary: The classic algorithms, how Stripe and Cloudflare run limits in production, and the contract between a limited server and a well-behaved client.
tags: distributed systems, APIs
---

A rate limiter is a counter with an opinion, and almost every interesting question about it is about the counter: how precise it is, where it lives, and what happens when it is wrong. This reading goes from the classic algorithms to how Stripe and Cloudflare run limits in production, and ends with the API contract between a limited server and a well-behaved client.

## 1. Why rate limit at all

Rate limiting exists to keep one caller from spending capacity that belongs to everyone else. That breaks down into four jobs:

- **Protection.** A buggy client in a retry loop, a scraper or a credential-stuffing attack can send far more traffic than a service was sized for. A limit turns "the database fell over" into "one client got 429s".
- **Fairness.** In a multi-tenant system, one large customer's batch job should not raise latency for everyone else. Per-tenant limits are a form of isolation.
- **Cost control.** When each request costs money downstream (an LLM call, an SMS, a paid third-party API), limits cap the bill.
- **Product tiers.** Free plans get 60 requests a minute, paid plans get 6,000. Here the limit is part of the product, not just a safety device.

It helps to separate two ideas that are often blurred together. Stripe's engineering blog puts the distinction well: a **rate limiter** decides based on *who* is calling ("this user has used their quota"), while a **load shedder** decides based on *the state of the whole system* ("we are at 95% CPU, drop low-priority work"). A user well under their quota can still be shed during an incident, and a user over their quota is limited even when the system is idle. A robust service needs both.

The other distinction is **what you count**:

| Limit on | Example | Protects against |
| --- | --- | --- |
| Request rate | 100 requests per second per API key | Floods of cheap requests |
| Concurrency | 20 requests in flight per API key | Slow, expensive requests that pile up |
| Cost or weight | 1,000 "points" per minute, where a search costs 10 and a get costs 1 | Requests with very different costs |
| Volume | 1 million tokens per day | Long-term quota and billing |

Request rate is the most common, but concurrency limits are underrated. A rate of 100 per second is harmless if each request takes 5 ms and dangerous if each takes 30 seconds. Limiting in-flight requests ties the limit to the resource that actually runs out.

## 2. The algorithms

There are really only two families: count requests in a time window, or model a bucket that fills and drains. The rest are clever ways to get the accuracy of one with the cost of the other.

### Fixed window counter

Keep one counter per key per window, for example `user42:2026-10-22T10:15` for the 10:15 minute. Increment on each request; reject when it exceeds the limit; let the key expire.

It is one integer and one `INCR` per request, which is why it is everywhere. The flaw is the **boundary burst**. With a limit of 100 per minute, a client can send 100 requests at 10:15:59 and another 100 at 10:16:00. That is 200 requests in two seconds, and both windows are within their limit.

### Sliding log

Store a timestamp for every request (in Redis, a sorted set per key). On each request, drop timestamps older than the window, count what remains, and allow if under the limit.

This is exact, with no boundary problem. It is also expensive: memory grows with the limit, so a limit of 10,000 per hour means up to 10,000 timestamps per key.

### Sliding window counter

This is the practical compromise, and the one Cloudflare described in its 2017 post on rate limiting millions of domains. Keep the fixed-window counters for the current and previous window, and estimate the sliding count by weighting the previous window by how much of it still overlaps:

$$\text{rate} = \text{prev} \times \frac{\text{window} - \text{elapsed}}{\text{window}} + \text{current}$$

Cloudflare's worked example: 42 requests last minute, 18 so far this minute, and 15 seconds into the current minute. The estimate is 42 × 45/60 + 18 = 49.5. The assumption is that the previous window's traffic was evenly spread. Cloudflare reported that on 400 million real requests from 270,000 sources, only 0.003% were wrongly allowed or limited. Two integers per key buys almost all the accuracy of a full log.

### Token bucket

Each key has a bucket that holds up to **B** tokens and refills at **R** tokens per second. A request takes one token; if the bucket is empty, it is rejected. Two parameters, two separate meanings:

- **R** is the sustained rate you allow over the long run.
- **B** is the burst you tolerate. A client who has been idle can send B requests at once.

You never need a timer to refill. Store `tokens` and `last_refill_time`; on each request, add `R × (now − last_refill_time)` tokens (capped at B), then try to take one. Stripe uses a token bucket per user, stored in Redis, for its main request limiter. AWS API Gateway also describes its throttling in terms of a rate and a burst, which is the same model.

### Leaky bucket

The leaky bucket is the token bucket viewed from the other side. Requests pour into a bucket that drains at a constant rate; if it overflows, requests are dropped. As a **queue** (requests actually wait in the bucket), it smooths bursty input into a steady output, which is what traffic shaping in routers and NGINX's `limit_req` with a queue do. As a **meter** (no queue, just accept or reject), it is mathematically equivalent to a token bucket.

### GCRA: a token bucket in one number

The **Generic Cell Rate Algorithm** comes from ATM networks and is a favorite for Redis-based limiters (Brandur Leach's post on it is the classic explanation). Instead of storing tokens and a timestamp, it stores one value: the **theoretical arrival time (TAT)**, when the next request would be due if the client sent at exactly the allowed rate.

- The emission interval is T = period / limit. For 10 per second, T = 100 ms.
- When a request arrives at time `now`, the new TAT would be max(TAT, now) + T.
- If that new TAT is further ahead of `now` than the allowed burst, reject. Otherwise accept and store the new TAT.

It behaves like a token bucket, but it is one atomic compare-and-set on one value, and it tells you exactly when the client may retry: that is just the moment when TAT minus the burst allowance reaches the current time.

### Which to use

| Algorithm | State per key | Bursts | Good for |
| --- | --- | --- | --- |
| Fixed window | 1 counter | Up to 2× at boundaries | Simple quotas, daily limits |
| Sliding log | 1 entry per request | Exact | Low limits needing precision, such as logins |
| Sliding window counter | 2 counters | Close to exact | High-volume edge limits |
| Token bucket | Tokens + timestamp | Explicit burst B | API rate limits |
| GCRA | 1 timestamp | Explicit burst | Same as token bucket, minimal storage |
| Concurrency limit | In-flight count | Not applicable | Slow or expensive endpoints |

## 3. Making it distributed

On one server, any of these algorithms is a few lines of code. Across a fleet of 50 API servers, the hard question becomes where the counter lives, and every answer trades accuracy against latency and availability.

### Option 1: a shared store (usually Redis)

Every API server checks and updates the same counter in Redis. This is what Stripe describes, and it gives accurate global limits. Three details decide whether it works:

- **Atomicity.** "Read the count, compare, then write" from the application races with other servers: two servers both read 99 and both allow request 100. Use an atomic command (`INCR` returns the new value) or put the whole token-bucket or GCRA logic in a **Lua script**, which Redis runs atomically.
- **One clock.** If each API server uses its own clock for refill timestamps, skew between servers makes the limit drift. Taking time from the Redis server inside the script removes that problem.
- **Failure mode.** If Redis is slow or down, does every API request fail? Almost always the answer should be **fail open**: allow the request and log it. Stripe's post is explicit that errors in its limiters fail open. A rate limiter that causes an outage has made things worse than the traffic it was protecting against.

The costs are one network round trip per request (usually under a millisecond in the same zone) and a new hot spot: one very busy key, such as a huge tenant's limit, all lands on one Redis shard.

### Option 2: local limits only

Each server enforces limit / N on its own, in memory. No network call, no shared failure. It is accurate only if the load balancer spreads each client's traffic evenly, which it often does not: with sticky connections or HTTP/2, one client can land entirely on one server and be limited at 1/N of their real allowance. N also changes during autoscaling.

Local limits are still very useful as a **coarse first layer**. Envoy, for example, has a local rate limit filter that runs a token bucket inside each proxy instance, purely to protect that instance.

### Option 3: local counting, periodic sync

Each server counts locally and periodically pushes its counts to a central store (or gossips them to peers), pulling back the global total. Decisions are made locally against the last known global count. The limit can overshoot by whatever arrives between syncs, but there is no per-request round trip and a central outage only makes the limit staler.

This is the same pattern as DynamoDB's global admission control from an earlier reading: a global budget handed out as local allowances.

### Option 4: limit where the traffic lands

Cloudflare's 2017 design is a variant that sidesteps the global problem. Its rate limiting counters live **inside each point of presence (PoP)**, in a memcache-protocol cluster, because a given attacker's traffic tends to reach the same nearby PoP through anycast. Its post states that sending every counter to one central point was not realistic at its scale. The limit is therefore per-PoP rather than truly global, which is the right trade for blocking abusive sources at the edge.

### A dedicated rate limit service

Large service meshes often pull the logic into its own service. Envoy's **global rate limit** filter calls an external gRPC service (the reference implementation, `envoyproxy/ratelimit`, came from Lyft and stores counters in Redis). Each request is described by **descriptors**, such as `(api_key=abc, path=/search)`, and the service holds the configured limit for each descriptor. Application code no longer contains any rate limit logic; it is configuration at the proxy.

A common production setup combines layers: a generous local limit per proxy to absorb floods cheaply, a precise global limit per tenant in a central service, and load shedding behind both.

## 4. A real system: Stripe's four limiters

Stripe's 2017 post by Paul Tarjan is still the best short description of layered rate limiting in production: four checks, each catching what the one before it cannot, ordered from per-user to whole-system.

<!--diagram:909a526c-1734-->

The first two checks reject a caller for their own behavior with 429. The last two reject for the system's sake with 503, dropping the least important traffic first.

1. **Request rate limiter.** A token bucket per user: N requests per second, with room for short bursts (Stripe mentions flash sales). Stripe calls this "by far the most important one".
2. **Concurrent requests limiter.** A cap on requests in flight per user, with the post's example of 20 at a time. It exists for expensive endpoints, where a client that retries aggressively against a slow endpoint makes it slower for everyone. It triggered about 12,000 times in the month Stripe cited.
3. **Fleet usage load shedder.** Traffic is split into **critical** methods (creating a charge) and **non-critical** ones (listing charges). A share of the fleet is reserved for critical traffic. With a 20% reservation, non-critical requests beyond their 80% share get a 503. Counts live in Redis.
4. **Worker utilization load shedder.** The last line of defense. When an individual server's workers are saturated, it sheds traffic by category, lowest priority first: test-mode traffic, then GETs, then POSTs, and critical methods last. It sheds and recovers slowly, to avoid flapping between "shed everything" and "shed nothing". It fired about 100 times in the cited month, only during major incidents.

The status codes are deliberate. The first two answer **429 Too Many Requests**, which tells the client "this is about you". The load shedders answer **503 Service Unavailable**, which says "this is about us".

The operating advice in the post is as valuable as the design:

- **Dark launch** every limiter first: log what it would have blocked, and only enforce once the numbers look right.
- **Fail open** at every level, so a bug or a Redis outage never blocks legitimate traffic.
- Put each limiter behind a **feature flag**, so a human can turn it off quickly during an incident.
- Start with the request rate limiter. It gives most of the benefit, and the others can come later.

The priority ordering is the idea to keep. Under overload, a system will drop something. Deciding in advance *what* to drop (test traffic before reads, reads before writes, writes before payments) turns an outage into a degradation.

## 5. The API contract: telling clients to slow down

A rate limit is only half a system. The other half is the client, and a server that rejects well but gives clients no guidance gets a retry storm instead of less traffic.

### What the server should send

- **Status 429 Too Many Requests** (RFC 6585) when the caller has exceeded its limit. Use 503 when the server itself is overloaded.
- **Retry-After** with a number of seconds (or an HTTP date). With a token bucket or GCRA you can compute exactly when the next request would succeed, so send that instead of a guess.
- **Remaining quota on every response**, not only on rejections, so well-behaved clients can slow down *before* they hit the wall.

For years every API invented its own headers for that last part. GitHub, for example, sends `x-ratelimit-limit`, `x-ratelimit-remaining`, `x-ratelimit-used` and `x-ratelimit-reset`. The IETF HTTPAPI working group has been standardizing this as two fields, described in [draft-ietf-httpapi-ratelimit-headers](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/) (version 11, May 2026, still a draft):

```
RateLimit-Policy: "default";q=100;w=60
RateLimit: "default";r=37;t=21
```

The policy says the quota is 100 per 60-second window. The second header says 37 remain and the window resets in 21 seconds. Several policies can be listed at once, for example a per-second burst limit and a daily quota.

One security note: limits applied to unauthenticated traffic, such as login attempts, should be keyed by more than IP address (also by account name, for example). Otherwise an attacker spreads across many IPs, or many users behind one corporate NAT share one limit.

### What the client should do

A client that retries immediately on every 429 turns a limit into a denial of service against itself. The well-known recipe has three parts.

**1. Exponential backoff with jitter.** Wait longer after each failure, and randomize the wait. Marc Brooker's AWS Architecture Blog post on backoff showed with simulations that plain exponential backoff still makes clients retry in synchronized waves; adding randomness spreads them out. The simplest good version is "full jitter":

$$\text{sleep} = \text{random}(0,\ \min(\text{cap},\ \text{base} \times 2^{\text{attempt}}))$$

If the server sent Retry-After, honor it instead.

**2. Retry budgets.** Retries multiply through layers. If a front end, a service and its database client each make up to 3 attempts, one user request can become 3 × 3 × 3 = 27 database calls at exactly the moment the database is struggling. Google's SRE book recommends capping retries per request (around three attempts) and also per client, so that retries are at most about 10% of a client's traffic. Once the budget is spent, fail fast instead of retrying.

**3. Client-side throttling.** The same SRE chapter describes **adaptive throttling**: each client tracks how many requests it sent and how many the backend accepted over the last couple of minutes. When the backend starts rejecting, the client starts dropping a matching share of its own requests locally, before they ever leave the process. The book's formula:

$$P(\text{reject locally}) = \max\left(0,\ \frac{\text{requests} - K \times \text{accepts}}{\text{requests} + 1}\right)$$

With K = 2, the client keeps sending until requests reach twice the accepted count, then sheds the excess itself. Rejected requests that never reach the server cost the server nothing.

Finally, only retry what is safe to retry. GETs are idempotent; a retried POST that creates a payment is not, unless the API supports **idempotency keys** so the server can recognize a repeat.

## 6. Ideas to carry away, and questions for our discussion

Five ideas to keep:

1. **Rate limiting is about the caller; load shedding is about the system.** You need both, and they should return different status codes.
2. **A token bucket has two dials.** R is the sustained rate, B is the burst. Most limit complaints are really about choosing B badly.
3. **Precision costs coordination.** Exact global limits need a shared store on every request; approximate limits (sliding window counters, local counting with periodic sync, per-PoP limits) are cheaper and usually good enough.
4. **Fail open.** A rate limiter whose outage blocks all traffic is a bigger risk than the traffic it was meant to stop.
5. **The client is half the system.** Retry-After, backoff with jitter, retry budgets and client-side throttling decide whether a limit reduces load or multiplies it.

Think about these before we talk:

- A partner says your 100-per-minute limit blocked them after only 60 requests in a minute. Which algorithm would explain that, and how would you check?
- How would you choose R and B for an endpoint that a mobile app calls in a burst of 10 when it opens?
- Your rate limiter uses Redis and Redis goes down. What should happen, and what would you monitor to notice?
- Where in your own systems would a concurrency limit protect better than a rate limit?
- What would a priority order for load shedding look like in your product: what would you drop first, and what last?
- How many layers of retries sit between your users and your database today, and what is the worst-case amplification?

## Sources

- Paul Tarjan, Stripe, [Scaling your API with rate limiters](https://stripe.com/blog/rate-limiters), March 2017
- Julien Desgats, Cloudflare, [How we built rate limiting capable of scaling to millions of domains](https://blog.cloudflare.com/counting-things-a-lot-of-different-things/), June 2017
- Brandur Leach, [Rate Limiting, Cells, and GCRA](https://brandur.org/rate-limiting)
- IETF HTTPAPI, [RateLimit header fields for HTTP, draft 11](https://www.ietf.org/archive/id/draft-ietf-httpapi-ratelimit-headers-11.html), May 2026
- Marc Brooker, AWS Architecture Blog, [Exponential Backoff And Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/), 2015
- Google, *Site Reliability Engineering*, chapter 21, [Handling Overload](https://sre.google/sre-book/handling-overload/)
- Envoy documentation on local and global rate limiting, and the [envoyproxy/ratelimit](https://github.com/envoyproxy/ratelimit) reference service

The Stripe, Cloudflare and IETF details were checked against those pages today. The Google SRE figures (K = 2, about three attempts per request, about 10% retries per client) and the AWS backoff summary are from my knowledge of those texts; I could not open the SRE chapter today.
