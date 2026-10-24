---
title: "Caching at scale: lessons from Facebook's memcache"
date: 2026-10-24
summary: Caching patterns and their failure modes, how Facebook scaled memcache, and how Meta pushed cache consistency to ten nines.
tags: caching, distributed systems
---

A cache is the easiest component to add and one of the hardest to get right: it makes reads cheap by keeping a second copy of the data, and every hard problem in caching is about that second copy being wrong, missing or overwhelmed. This reading covers the standard patterns and their failure modes, then follows how Facebook scaled memcache to billions of requests per second and how Meta later pushed cache consistency to ten nines.

## 1. What a cache buys, and what it costs

A cache trades **correctness risk** for **capacity**. That framing is more useful than "a cache makes things faster".

What you gain:

- **Throughput.** An in-memory key-value server can answer hundreds of thousands of gets per second on one machine; a relational database doing indexed lookups serves far fewer. The MIT 6.824 notes on Facebook's paper make the point that their cache was mainly about **throughput, not latency**: the databases simply could not serve Facebook's read load alone.
- **Latency**, especially for results that are expensive to compute (a rendered page fragment, an aggregate, a call to a slow third party).
- **Protection.** The cache absorbs the bulk of reads, so the database is sized for writes and misses.

What you pay:

- **Staleness.** Two copies of the data can disagree. Every caching design is a choice about how long and how often.
- **A new dependency with a dangerous failure mode.** When a cache that absorbs 90% of reads fails or is flushed, the database suddenly sees ten times its normal load. A cache you cannot run without is no longer an optimization; it is load-bearing infrastructure.
- **Complexity.** Invalidation logic spreads across every code path that writes data.

Phil Karlton's line about the two hard things in computer science (cache invalidation and naming things) survives because it keeps being true. The rest of this reading is about why.

## 2. The caching patterns

The patterns differ in one question: who is responsible for keeping the cache and the database in step, the application or the cache?

### Cache-aside (look-aside)

The application owns everything. To read: check the cache; on a miss, read the database and put the value in the cache. To write: update the database, then **delete** the cache key.

This is the most common pattern and the one Facebook used. The cache knows nothing about the database, so you can cache anything, including computed results. The cost is that every write path must remember to invalidate, and races between readers and writers can leave stale data (section 5).

Why delete instead of setting the new value on write? Deletes are **idempotent** and order-insensitive: two deletes in any order leave the same state. Two concurrent sets of different values can land in the wrong order and leave the older one in the cache. Deleting also avoids computing a value that might never be read. Facebook's design uses sets only to fill on a read miss.

### Read-through

The application only talks to the cache; on a miss, the cache itself loads from the database (through a loader function you register). It centralizes miss handling, which makes it a natural place to coalesce concurrent misses. Libraries such as Caffeine in Java and many CDN setups work this way.

### Write-through

Writes go to the cache, which synchronously writes to the database before acknowledging. The cache always has the latest value for anything written through it, at the cost of slower writes and caching data that may never be read.

### Write-behind (write-back)

Writes go to the cache and are acknowledged immediately; the cache flushes them to the database later, often in batches. Very fast writes, but a cache crash loses acknowledged data. Appropriate for counters and metrics where a small loss is acceptable, and dangerous almost everywhere else.

### TTLs: the safety net

Whatever the pattern, set a **time to live**. A TTL bounds how long any bug in invalidation can show stale data, and it lets unused keys age out. Two practical rules:

- Add random **jitter** to TTLs. If a batch job fills 1 million keys with exactly one hour of TTL, they all expire at the same second.
- Choose TTLs by how stale each kind of data may be, not one global number. A feature flag and a product description have very different tolerances.

| Pattern | Who fills the cache | Write path | Main risk |
| --- | --- | --- | --- |
| Cache-aside | Application on a miss | DB, then delete key | Races leave stale values; forgotten invalidations |
| Read-through | Cache's loader on a miss | Usually combined with delete or write-through | Loader becomes a hidden dependency |
| Write-through | Writes go through the cache | Cache, then DB, synchronously | Slower writes; cache full of unread data |
| Write-behind | Writes go to the cache | Cache now, DB later | Losing acknowledged writes |

## 3. Stampedes: when everyone misses at once

The most common way a cache takes down a database is not by being wrong but by being empty for one popular key at the wrong moment.

Picture a key read 10,000 times per second, such as the home page's trending list. Its entry expires, or a write deletes it. For the next few hundred milliseconds, while one request recomputes the value, every other request also misses and also goes to the database. Thousands of identical expensive queries hit at once; the database slows down; the recomputation takes longer; more requests pile up. This is a **cache stampede** (or thundering herd, or dog-pile).

There are four standard defenses, and large systems use several.

### 1. Leases (Facebook's answer)

In Facebook's memcache, a client that misses receives a **lease**: a token giving it the right to fill that key. Memcache hands out at most one lease per key every 10 seconds. Other clients that miss during that window are told to wait briefly and retry, by which time the leaseholder has usually filled the value. One database query instead of thousands.

Leases also solve a correctness problem. If the key is deleted while the leaseholder is still computing, memcache invalidates the lease, and the late `set` with the stale value is rejected. Section 5 explains why that race matters.

A refinement: for data where slight staleness is fine, memcache can return the **just-deleted value**, marked stale, to the waiting clients instead of making them wait at all.

### 2. Request coalescing

Inside one process, collapse concurrent misses for the same key into one call and share the result. Go's `singleflight` package and NGINX's `proxy_cache_lock` do exactly this. It only deduplicates within a process or proxy, so with 500 app servers you still get up to 500 database queries, which is far better than 10,000.

### 3. Serve stale while revalidating

Keep the old value past its soft expiry and serve it while one background refresh runs. HTTP standardizes this as the `stale-while-revalidate` Cache-Control extension (RFC 5861), and CDNs support it. Users never see the miss at all.

### 4. Probabilistic early expiration

Instead of every client refreshing at the exact expiry time, let each request refresh a little **early with a small probability** that rises as expiry approaches. The paper *Optimal Probabilistic Cache Stampede Prevention* (Vattani, Chierichetti and Lowenstein, VLDB 2015) gives the XFetch rule. A request recomputes early if:

$$\text{now} - \Delta \cdot \beta \cdot \ln(\text{rand}()) \ge \text{expiry}$$

where Δ is how long the recomputation takes, rand() is uniform on (0, 1] (so its logarithm is negative), and β (default 1) tunes how eagerly to refresh. Slow-to-compute values start refreshing earlier. Usually exactly one request refreshes shortly before expiry, with no locks or coordination at all.

### The cold cache

The largest stampede is a whole cold cache: a new cluster, a restart, or a flush. Facebook's paper describes **cold cluster warmup**: a new cluster's clients read misses from a warm cluster's cache rather than the database, until hit rates recover. The MIT notes give the arithmetic of why: a cluster that normally misses 1% of reads but starts at a 33% miss rate means roughly 30 times the usual database load.

## 4. How Facebook scaled memcache

*Scaling Memcache at Facebook* (Nishtala et al., NSDI 2013) describes taking a simple open-source cache to billions of requests per second across several regions. Its lasting value is that each scaling step uncovered a new problem, and each fix is a reusable idea.

<!--diagram:cf8b2df2-d610-->

Web servers fill caches on misses; committed writes flow from MySQL through mcsqueal as deletes to every cluster, and replica regions learn about writes through MySQL replication.

### Step 1: one cluster

Web servers use memcache as a look-aside cache with delete-on-write, as in section 2. Keys are spread over many memcache servers by consistent hashing. A single page view can fetch hundreds of keys in parallel, so two problems appear quickly:

- **Latency of many small requests.** Clients batch requests and fetch independent data in parallel, building a dependency graph of what can be fetched at once. Gets go over **UDP** (a lost packet is just treated as a miss, which is rare and harmless), while sets and deletes go over TCP through **mcrouter**, a proxy that pools connections so each web server does not hold a connection to every cache server.
- **Incast.** When a request fans out to hundreds of servers, all the replies arrive at once and overflow switch buffers. Clients limit how many requests they have outstanding at a time, a sliding window similar to TCP congestion control.

**Leases** (section 3) handle stampedes and stale sets within the cluster.

**Gutter pools** handle failures. If a memcache server stops responding, its keys would all miss and land on the database. Instead, clients retry against a small pool of otherwise idle **gutter** servers, about 1% of the cluster according to reviews of the paper, whose entries expire quickly. The gutter pool absorbs the misses for a few minutes until the failed server is replaced. Rehashing the dead server's keys onto the remaining servers was rejected, because a hot key could then overload a second server and cause a cascade.

### Step 2: many clusters in a region

One cluster cannot grow forever: more servers means more fan-out per request and worse incast. So a region runs several **front-end clusters** of web and memcache servers, all sharing one storage cluster of MySQL databases.

Now a write must invalidate the key in every cluster, not only the writer's. Facebook moved invalidation to the database side: a daemon called **mcsqueal** tails the MySQL commit log, extracts the keys each committed transaction affects, and broadcasts deletes to every cluster through mcrouter. Because invalidations come from the commit log, they are only sent for committed data, are replayable after failures, and can be batched. The writing web server still deletes the key in its own cluster immediately, so the user who wrote sees their own write.

To save memory, rarely read keys live in a shared **regional pool** instead of being copied into every cluster's cache; popular keys are replicated in each cluster.

### Step 3: many regions

Across regions, one region holds the master MySQL databases and the others hold read replicas fed by MySQL replication. Each region has its own memcache. Reads are served locally; writes go to the master region.

The tricky case is a user in a replica region who writes and then reads. The write goes to the master, but the local replica database may not have it yet, so a cache miss would refill the cache with old data. Facebook's fix is a **remote marker**: when writing from a replica region, the client sets a marker for the key in its region. A later miss that finds the marker reads from the master region instead of the local replica. The marker is removed once replication catches up. It trades a slower read (a cross-region round trip, on the order of 100 ms) for read-your-writes consistency, only for keys that were just written.

## 5. Making caches consistent

Even with delete-on-write, a cache-aside system can end up permanently stale, because a read and a write can interleave in exactly the wrong order.

### The classic race

Meta's 2022 post [Cache made consistent](https://engineering.fb.com/2022/06/08/core-infra/cache-made-consistent/) gives the canonical example:

1. A cache miss on `x` sends a fill request to the database, which returns `x = 42`.
2. Before that reply reaches the cache, a write changes `x` to 43, and the invalidation for 43 arrives at the cache first.
3. The delayed fill with 42 then lands and overwrites it.

The database says 43, the cache says 42, and nothing will fix it until the key expires or is written again. With a long TTL that can be days.

### The fixes

- **Leases or fill tokens.** The cache only accepts a fill from the client holding a valid lease, and any delete in between cancels the lease. This is Facebook's memcache answer from section 3.
- **Versions.** Store a version number (or a commit timestamp) with each value and refuse to overwrite a newer version with an older one. TAO, Meta's graph cache, uses versioned entries.
- **Delete again later.** Some teams issue a second, delayed delete a second or two after each write to catch fills that were in flight. It narrows the window rather than closing it.
- **Short TTLs** as the last line of defense, so any inconsistency heals by itself.

### Measuring it: Polaris

The most important idea in Meta's post is that you cannot improve what you cannot measure. Meta built **Polaris**, a service that receives the same invalidation events as the caches and then checks whether cache replicas actually reflect them. It rechecks suspected inconsistencies at 1, 5 and 10 minute timescales to avoid false alarms, and reports results as "N nines of cache writes are consistent in M minutes".

With Polaris and a targeted **consistency tracing** library that logs only the narrow window where fills and invalidations race, Meta reports that TAO's cache consistency went from 99.9999% (six nines) to 99.99999999% (ten nines) at the five-minute timescale: fewer than one in 10 billion cache writes still inconsistent after five minutes.

The bug story in the post is a good lesson in why this is hard. A cache entry held `metadata=0 @version 4` while the database held `metadata=1 @version 4`. A rare transaction interleaved with a cache fill, the invalidation then hit a transient error, and the error handler called a "drop this entry if its version is older" function. The stale entry already had the newest version number, so nothing was dropped. Polaris raised the alarm, and with tracing the on-call engineers found the bug in under 30 minutes.

## 6. Hot keys, big keys and other sharp edges

Consistent hashing spreads keys evenly, but it cannot spread **traffic** evenly when traffic itself is skewed.

**Hot keys.** A celebrity's profile or a viral post can receive far more reads than one cache server can serve. Common fixes:

- **Replicate the hot key**: store copies under `key#1` to `key#N` on different servers and have readers pick one at random. Writes must invalidate every copy.
- **A small local cache** in each application process for the hottest keys, with a TTL of a second or two. This removes most network calls for the very top keys and is how many systems survive viral spikes.
- **Detect hotness dynamically.** Track per-key request rates (client-side sampling or server-side stats) so hot keys can be promoted to replication or local caching automatically.

**Big keys.** A 5 MB value (a huge list or a serialized page) is slow to fetch, blocks the network, and in single-threaded caches like Redis can stall every other request on that server. Split large values into chunks, compress them, or cache only the part actually needed.

**Negative caching.** If a key does not exist in the database, cache that fact too (with a short TTL). Otherwise lookups for missing keys, including from attackers probing random IDs, bypass the cache and all hit the database.

**Eviction is not invalidation.** An LRU cache that evicts a key under memory pressure gives a miss, which is safe. A cache that silently keeps a stale value is not. Never rely on eviction to remove data that must not be served.

**Watch the hit rate and the miss cost, not just the hit rate.** A 99% hit rate sounds great until you notice the 1% of misses are the most expensive queries. Monitor database load from misses, and alert when the hit rate drops, because a falling hit rate is the early warning before a stampede.

## 7. Ideas to carry away, and questions for our discussion

Five ideas to keep:

1. **A cache trades correctness risk for capacity.** Once it absorbs most reads, it is part of your availability story, not an optimization.
2. **Delete, don't set, on write.** Deletes are idempotent and order-insensitive; sets can land out of order.
3. **Protect the database from the cache's failures.** Leases, coalescing, stale-while-revalidate, gutter pools and warm-up all exist to stop a miss storm.
4. **Invalidate from the source of truth.** Driving invalidations from the database commit log, as mcsqueal does, means no write path can forget.
5. **Measure consistency.** Meta's jump from six to ten nines started with a system that could tell them how inconsistent the cache actually was.

Think about these before we talk:

- Walk through the 42/43 race in section 5. How exactly does a lease prevent it?
- Why did Facebook prefer a gutter pool over rehashing a failed server's keys onto the remaining servers?
- Your cache cluster restarts during peak traffic. What protects the database in your current design?
- When is write-through a better choice than cache-aside?
- How would you find out how stale your own caches are today?
- What is the hottest key in a system you work on, and what would happen if it went viral?

## Sources

- Nishtala et al., *Scaling Memcache at Facebook*, NSDI 2013 ([USENIX page](https://www.usenix.org/node/172909))
- MIT 6.824, [Lecture notes on Memcache at Facebook](https://people.csail.mit.edu/alinush/6.824-spring-2015/l16-memcached.html), 2015
- UW-Madison CS739, [student reviews of Scaling Memcache at Facebook](https://pages.cs.wisc.edu/~swift/classes/cs739-fa14/blog/2014/09/scaling_memcache_at_facebook.html) (source for the ~1% gutter pool and the 10-second lease limit)
- Meta Engineering, [Cache made consistent](https://engineering.fb.com/2022/06/08/core-infra/cache-made-consistent/), June 2022
- Vattani, Chierichetti and Lowenstein, [Optimal Probabilistic Cache Stampede Prevention](https://mail.vldb.org/pvldb/volumes/8/paper/Optimal%20Probabilistic%20Cache%20Stampede%20Prevention), VLDB 2015
- IETF, RFC 5861, HTTP Cache-Control extensions for stale content

The Meta and MIT pages were read today. Paper details I could not open directly (the original NSDI PDF) are taken from the lecture notes and reviews above; general patterns in sections 2 and 6 are standard practice rather than from one source.
