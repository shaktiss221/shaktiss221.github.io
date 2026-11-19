---
title: "Bloom filters, HyperLogLog and Count-Min: trading exactness for memory"
date: 2026-11-19
summary: The small, approximate data structures behind caches, databases and analytics at scale. Covers Bloom filters and the false-positive maths, where they show up (LSM trees, CDN cache admission, joins), cuckoo, quotient and ribbon filters, HyperLogLog for counting distinct values in 12 KB, Count-Min sketch for frequencies and heavy hitters, TinyLFU cache admission, quantile sketches for latency percentiles, mergeability, and how to choose and size them.
tags: caching, analytics
daily: true
---

Some questions are expensive to answer exactly and cheap to answer approximately. "Have I seen this key before?", "How many unique users visited today?", "Which URLs are the most popular right now?", "What's the p99 latency across 2,000 servers?" Answering these exactly needs memory proportional to the data: a set of every key, a hash table of every user, a counter per URL, every latency sample. Answering them approximately can take a few kilobytes, with an error you can choose and bound in advance. That trade is behind a surprising amount of infrastructure: Bloom filters in every LSM-tree database and in CDN caches, HyperLogLog in Redis and analytics warehouses, Count-Min sketches in cache admission and network monitoring, and quantile sketches in metrics systems. This reading explains how the main structures work, the maths you need to size them, where they're used, and how to decide when approximate is good enough. It draws on the original papers (Bloom 1970, Flajolet et al. 2007, Cormode and Muthukrishnan 2005, Fan et al. 2014), Google's HyperLogLog++ paper, the Redis and Caffeine documentation, and Apache DataSketches.

## 1. The trade: bounded error for tiny, mergeable state

All the structures in this reading share a few properties:

- **Fixed or sub-linear memory.** The size depends on the accuracy you choose, not (or only weakly) on how much data you put in.
- **Errors in a known direction with a known bound.** A Bloom filter can say "maybe" when the answer is "no", but never "no" when the answer is "yes". A Count-Min sketch can overestimate a count but never underestimates it. You can reason about these errors.
- **Hashing does the work.** Each item is reduced to one or more hash values, and the structure stores something about those hashes. Good, independent hash functions are essential; a weak hash quietly breaks the guarantees.
- **Mergeability.** Many sketches can be combined: the union of two Bloom filters is a bitwise OR, two HyperLogLogs merge by taking per-register maximums, two Count-Min sketches merge by adding counters. That's what makes them so useful in distributed systems: each server or partition builds its own sketch, and you merge them centrally, without moving the raw data.

The last property often matters more than the memory savings. Counting distinct users exactly across 1,000 partitions means shipping or shuffling every user ID; with HyperLogLog, each partition sends a few kilobytes.

## 2. Bloom filters: "definitely not" or "probably yes"

Burton Bloom described the structure in 1970 ("Space/Time Trade-offs in Hash Coding with Allowable Errors"). It answers set membership with no false negatives and a tunable rate of false positives.

**Structure:** an array of m bits, all initially 0, and k independent hash functions, each mapping an item to a bit position.

- **Insert:** hash the item k times and set those k bits to 1.
- **Query:** hash the item k times. If any of those bits is 0, the item was **definitely never inserted**. If all are 1, it was **probably** inserted, or those bits were all set by other items: a false positive.

<!--diagram:bloom-filter-1119-->

### The maths you need

With n items inserted into m bits using k hash functions, the probability that a particular bit is still 0 is about e^(−kn/m), so the false-positive rate is approximately:

> p ≈ (1 − e^(−kn/m))^k

Two useful consequences:

- The best number of hash functions is **k = (m/n) · ln 2**, which leaves about half the bits set.
- At that optimum, the bits needed per item depend only on the target false-positive rate: **m/n ≈ −log₂(p) / ln 2 ≈ 1.44 · log₂(1/p)**. That works out to about **9.6 bits per item for a 1% false-positive rate**, and each further 10× reduction in the rate costs about 4.8 more bits per item.

So a filter for 100 million keys at 1% false positives needs about 120 MB, no matter how long the keys are. A hash set of the same keys would be many gigabytes. In practice, implementations don't compute k separate hashes; the Kirsch–Mitzenmacher technique derives all k positions from two hash values (h₁ + i·h₂) with no loss of accuracy in practice.

### Limitations

- **No deletes.** Clearing a bit might remove other items that share it. A **counting Bloom filter** replaces bits with small counters (typically 4 bits) to allow deletes, at 4× the space.
- **Fixed capacity.** If you insert many more items than planned, the false-positive rate climbs steeply. Size for the expected count with headroom, or use **scalable Bloom filters** that add new, tighter filters as they fill.
- **No enumeration.** You can't list what's in the filter.
- **Random memory access.** k bit lookups touch k random cache lines. **Blocked Bloom filters** put all k bits for an item in one cache line (or SIMD word), trading a slightly higher false-positive rate for much faster lookups, which is what most high-performance implementations do.

## 3. Where Bloom filters earn their keep

The pattern is always the same: a cheap in-memory check that avoids an expensive operation for items that aren't there.

**LSM-tree databases.** RocksDB, Cassandra, HBase, LevelDB and ScyllaDB keep a Bloom filter for each SSTable file (the LSM-trees reading covered the structure). A point lookup for a key would otherwise have to search every level of the tree; with filters, the database skips any file whose filter says "not present" and only reads files that might contain the key. A 1% filter means 99% of unnecessary disk reads are avoided at about 10 bits per key. RocksDB has also adopted **Ribbon filters**, which reach the same false-positive rate in roughly 30% less memory at some extra CPU cost, an example of how much memory these filters consume at scale.

**CDN cache admission.** A large share of objects requested from a CDN are requested only once, ever. Caching them wastes disk writes and evicts useful content. Akamai's engineers (Maggs and Sitaraman, "Algorithmic Nuggets in Content Delivery", 2015) described keeping a Bloom filter of recently requested URLs and caching an object only on its **second** request: if the URL isn't in the filter, record it and serve without caching; if it is, cache it. They reported that around three quarters of objects were these "one-hit wonders", and that cache-on-second-hit substantially reduced disk writes while improving hit rates, because space was no longer wasted on objects that would never be requested again.

**Distributed joins and query engines.** When joining a large table with a small filtered one, the engine can build a Bloom filter of the small side's join keys and send it to the scanners of the large side, which drop rows that can't match before shipping them over the network. Spark, Trino, StarRocks and most MPP engines do this ("runtime filters" or "Bloom filter joins"), which is often a big win for star-schema queries.

**Avoiding expensive lookups.** Checking whether a username is taken or a password is in a breach list before hitting a database or remote API; Chrome's former Safe Browsing design used a local probabilistic set of malicious URL prefixes before asking the server; web crawlers use them to avoid revisiting URLs.

**Postgres** has a `bloom` index extension that builds a per-row signature over many columns, useful for queries that filter on arbitrary combinations of columns where B-tree indexes on every combination would be impractical.

## 4. Better filters: cuckoo, quotient, ribbon, xor

Bloom filters aren't the most space-efficient option, and they can't delete. Newer designs improve on them:

- **Cuckoo filters** (Fan, Andersen, Kaminsky and Mitzenmacher, CoNEXT 2014) store a short **fingerprint** of each item in a cuckoo hash table, where each item has two candidate buckets. They support **deletion** (remove the fingerprint), have better lookup locality (two buckets to check), and use less space than Bloom filters for false-positive rates below about 3%. Inserts can fail when the table is nearly full, so they need headroom.
- **Quotient filters** store fingerprints in a compact, cache-friendly table using linear probing and can be resized and merged.
- **Xor and binary fuse filters** and **Ribbon filters** are static: built once from a known set, they get close to the theoretical minimum space (around 1.08 to 1.23 times the minimum, versus 1.44 for Bloom). They're ideal for immutable files like SSTables, which is why RocksDB uses Ribbon.

The rule of thumb: Bloom for simplicity and incremental inserts; cuckoo if you need deletes; xor/ribbon for build-once sets where memory matters.

## 5. HyperLogLog: counting distinct values in kilobytes

Counting **distinct** items (unique visitors, unique IPs, distinct search queries) is harder than counting items, because you have to remember what you've already seen. Exactly, that's a set of every distinct value. **HyperLogLog** (Flajolet, Fusy, Gandouet and Meunier, 2007) estimates the count with about 1–2% error using a few kilobytes, for cardinalities into the billions.

### The intuition

Hash each item to a uniformly random bit string. Look at the position of the first 1 bit. Half of all hashes start with 1, a quarter with 01, an eighth with 001, and so on. If the longest run of leading zeros you've seen is 20, you've probably seen on the order of 2^20 distinct values, because that pattern occurs once in about a million random hashes. Duplicates don't matter, because the same item always produces the same hash.

One observation is a very noisy estimate. HyperLogLog reduces the noise by **splitting the stream into m buckets** (registers) using the first few bits of the hash, tracking the maximum leading-zero count separately in each register, and combining them with a **harmonic mean**, which limits the influence of outlier registers. The standard error is about **1.04/√m**.

### Real numbers

Redis's implementation uses 16,384 registers of 6 bits each, so a HyperLogLog key takes **up to 12 KB** and has a **standard error of 0.81%**, whether it has counted a thousand items or a billion. Commands are `PFADD` to add items, `PFCOUNT` to estimate, and `PFMERGE` to union several. Small counts use a sparse encoding that takes much less memory.

Google's **HyperLogLog++** (Heule, Nunkesser and Hall, EDBT 2013) is the version most analytics systems use. It uses 64-bit hashes (avoiding collisions at very large cardinalities), applies an empirically measured **bias correction** for small and medium counts where the original estimator is skewed, and uses a sparse representation for low cardinalities. BigQuery's `APPROX_COUNT_DISTINCT` and `HLL_COUNT` functions, Druid, Presto/Trino's `approx_distinct`, ClickHouse's `uniq` family and many others are based on HyperLogLog variants.

### Why it's so useful: merging

HyperLogLog registers merge by taking the maximum of each register. That lets you:

- Store one HLL per day and get weekly or monthly unique counts by merging, without re-reading raw events. Distinct counts don't add up across days, so this would otherwise be impossible without the raw data.
- Compute distinct counts in parallel across partitions and merge.
- Pre-aggregate in a rollup table (one sketch per dimension combination) and answer distinct-count queries over arbitrary slices at query time.

### Limits

- **Intersections are weak.** HLL supports union exactly but estimates intersections (users who visited both A and B) only via inclusion–exclusion, whose error explodes when the intersection is small relative to the sets. **Theta sketches** (Apache DataSketches) support set intersection and difference with better error behaviour.
- **No membership or listing.** You can't ask whether a specific user is counted.
- **Error is relative,** so 0.81% of 10 million is ±81,000. For billing or anything audited, use exact counts.

## 6. Count-Min sketch: approximate frequencies

The **Count-Min sketch** (Cormode and Muthukrishnan, 2005) estimates how many times each item has appeared in a stream, using memory that doesn't grow with the number of distinct items.

**Structure:** a table of d rows and w columns of counters, with an independent hash function for each row.

- **Update:** for each of the d rows, hash the item to a column and increment that counter.
- **Query:** hash the item into each row and return the **minimum** of the d counters.

Each counter holds the item's true count plus the counts of every other item that collided with it in that row, so each counter is an overestimate, and the minimum across rows is the least polluted. Estimates are **never below** the true count. The guarantee: with width w = ⌈e/ε⌉ and depth d = ⌈ln(1/δ)⌉, the estimate is at most the true count plus ε·N (where N is the total of all counts) with probability at least 1 − δ. For example, ε = 0.001 and δ = 0.01 gives a sketch of 2,719 × 5 counters, about 54 KB with 32-bit counters, for any number of distinct items.

The error is relative to the **total** stream size, so Count-Min is accurate for frequent items and useless for rare ones: a URL seen 5 times in a stream of 100 million could be estimated at 100,000. That's fine for its main uses:

- **Heavy hitters / top-k.** Keep a Count-Min sketch plus a small heap of the current top-k candidates. Each update checks whether the item's estimate qualifies for the heap. This finds the most popular URLs, the noisiest tenants, the IPs sending the most traffic (DDoS detection) or the hottest keys in a cache, in a fixed amount of memory.
- **Hot key detection** in databases and caches, where the goal is to notice a few keys getting a disproportionate share of traffic.
- **Approximate counting** in streaming systems where exact per-key state would be too large.

**Conservative update** (only increment the counters that are currently at the minimum) reduces overestimation considerably in practice, at the cost of making the sketch harder to merge correctly.

## 7. TinyLFU: Count-Min as a cache admission policy

The caching readings covered eviction (what to throw out). **Admission** is the other half: should a new item even enter the cache? An LRU cache admits everything, so a burst of one-off requests (a scan, a crawler) can flush out items that are genuinely popular.

**TinyLFU** (Einziger, Friedman and Manes, ACM Transactions on Storage, 2017) uses a compact frequency sketch to decide: when a new item would evict a victim, admit it only if its estimated recent frequency is higher than the victim's. Frequencies are aged by periodically halving all counters, so the sketch reflects recent popularity rather than all-time counts.

**W-TinyLFU**, used by Java's Caffeine library (and its ports, such as Ristretto in Go), adds a small LRU **window** in front so that new items get a short chance to build up frequency, plus a segmented LRU main area. Caffeine's documentation describes its frequency sketch as a 4-bit Count-Min sketch costing about 8 bytes per cache entry, and it adjusts the window size automatically with hill climbing. Across a wide range of published traces, W-TinyLFU gets hit rates close to the theoretical optimum and well above LRU, for little memory. It's a good example of a sketch improving a system's core behaviour, not just its analytics.

## 8. Quantile sketches: percentiles without keeping every sample

Latency percentiles are one of the most important operational metrics and one of the easiest to compute wrongly. You **cannot average percentiles**: the average of 100 servers' p99 latencies is not the fleet's p99. Computing it exactly needs every sample, which is too much data. **Quantile sketches** keep a compact summary that answers "what's the value at quantile q?" with bounded error and **merges** across servers and time windows.

- **t-digest** (Ted Dunning) clusters samples into centroids, keeping clusters small near the tails, so it's very accurate for extreme percentiles like p99 and p99.9. It's widely used (Elasticsearch percentiles aggregation, many databases).
- **DDSketch** (Datadog, VLDB 2019) uses logarithmically sized buckets so that every quantile estimate is within a fixed **relative** error (for example 1%) of the true value. That's a natural guarantee for latency: being off by 1% of 2 ms or of 2 s is equally acceptable. It's fully mergeable.
- **KLL** (Karnin, Lang, Liberty) has strong worst-case guarantees on **rank** error and is in Apache DataSketches.
- **HDR Histogram** (Gil Tene) is a related fixed-precision histogram popular in benchmarking, designed to record latency without losing the tail.

Prometheus histograms are a simple version of the same idea: fixed buckets you choose up front, merged by summing counts, with quantiles interpolated within buckets. Their accuracy depends on how well the buckets match the data, which is the problem that native (exponential) histograms address.

## 9. Choosing and sizing

A practical decision guide:

| Question | Structure | Error type | Typical size |
|---|---|---|---|
| Is X in the set? | Bloom / cuckoo / ribbon filter | false positives only | ~10 bits per item for 1% |
| How many distinct X? | HyperLogLog (++), theta sketch | ±relative error | 12 KB for 0.81% (Redis) |
| How often does X occur? Top k? | Count-Min + heap | overestimate by ≤ εN | tens of KB |
| What's the p99? | t-digest, DDSketch, KLL | rank or relative value error | a few KB |

Some guidance for using them in real systems:

- **Decide what error is acceptable before choosing,** and in which direction. A false positive in a Bloom filter in front of a database costs one wasted lookup; a false positive in a filter used to *skip* a security check is a vulnerability.
- **Approximate is for decisions and dashboards, not for money.** Billing, quotas that customers see, and anything audited should use exact counts, possibly with a sketch as a fast pre-check.
- **Version the hash function and parameters.** Sketches built with different hash seeds or register counts can't be merged. Store the parameters with the sketch.
- **Watch capacity.** Bloom filters degrade sharply when overfilled. Monitor the fill ratio (fraction of bits set) and rebuild before it passes the design point.
- **Use libraries.** Apache DataSketches (Java, C++, Python), Redis's built-in HyperLogLog and its probabilistic data types (Bloom, cuckoo, Count-Min, top-k, t-digest), and database functions like `approx_count_distinct` are tested and interoperable. Subtle bugs in hand-written sketches (biased hashes, wrong bias correction) produce numbers that look plausible and are wrong.

## Key takeaways

- Probabilistic structures trade a **bounded, one-directional error** for memory that's **tiny and independent of data size**, and most are **mergeable**, which is what makes them ideal for distributed systems.
- **Bloom filters** answer "definitely not" or "probably yes". At the optimum, about **9.6 bits per item gives 1% false positives**. They power LSM-tree reads, CDN **cache-on-second-hit**, and runtime join filters. **Cuckoo** filters add deletes; **xor/ribbon** filters save space for static sets.
- **HyperLogLog** counts distinct items with ~1/√m relative error; Redis uses **12 KB for 0.81%**. It unions perfectly and handles intersections poorly.
- **Count-Min** estimates frequencies with an overestimate bounded by **εN**: great for heavy hitters and hot keys, useless for rare items.
- **TinyLFU** uses a frequency sketch for **cache admission**; Caffeine's W-TinyLFU gets near-optimal hit rates.
- **Quantile sketches** (t-digest, DDSketch, KLL) give mergeable percentiles. Never average percentiles.

## Discussion questions

1. Your service checks a database for whether an email is already registered on every signup attempt, and bots are hammering it with random emails. Would a Bloom filter help? Where would the filter live, and how would you keep it up to date?
2. You need daily, weekly and monthly unique users per country and per platform. Design the storage using HyperLogLog. What can't you answer with this design?
3. A cache uses LRU and its hit rate collapses every night when a batch job scans the whole key space. How would TinyLFU-style admission change that?
4. Your dashboard shows the "average p99 latency" across 50 pods. What's wrong with that number, and how would you compute the real fleet p99?
5. A product manager wants a "trending searches" list updated every minute from 50,000 queries per second. Sketch a design with Count-Min and a heap. What error would users notice?
6. Where in your stack would a false positive be harmless, and where would it be dangerous?

## Sources

- B. H. Bloom, "Space/Time Trade-offs in Hash Coding with Allowable Errors," Communications of the ACM, 1970
- A. Kirsch, M. Mitzenmacher, "Less Hashing, Same Performance: Building a Better Bloom Filter," ESA 2006
- B. Fan, D. Andersen, M. Kaminsky, M. Mitzenmacher, "Cuckoo Filter: Practically Better Than Bloom," CoNEXT 2014
- P. Dillinger, S. Walzer, "Ribbon filter: practically smaller than Bloom and Xor," 2021, and the RocksDB wiki on Ribbon filters
- B. Maggs, R. Sitaraman, "Algorithmic Nuggets in Content Delivery," ACM SIGCOMM CCR, 2015
- P. Flajolet, É. Fusy, O. Gandouet, F. Meunier, "HyperLogLog: the analysis of a near-optimal cardinality estimation algorithm," AofA 2007
- S. Heule, M. Nunkesser, A. Hall, "HyperLogLog in Practice: Algorithmic Engineering of a State of The Art Cardinality Estimation Algorithm," EDBT 2013
- [Redis documentation: HyperLogLog](https://redis.io/docs/latest/develop/data-types/probabilistic/hyperloglogs/) (12 KB, 0.81% standard error) and probabilistic data types
- G. Cormode, S. Muthukrishnan, "An Improved Data Stream Summary: The Count-Min Sketch and its Applications," Journal of Algorithms, 2005
- G. Einziger, R. Friedman, B. Manes, "TinyLFU: A Highly Efficient Cache Admission Policy," ACM Transactions on Storage, 2017; [Caffeine wiki: Efficiency](https://github.com/ben-manes/caffeine/wiki/Efficiency)
- T. Dunning, "Computing Extremely Accurate Quantiles Using t-Digests"; C. Masson, J. Rim, H. Lee, "DDSketch: A Fast and Fully-Mergeable Quantile Sketch with Relative-Error Guarantees," VLDB 2019; Z. Karnin, K. Lang, E. Liberty, "Optimal Quantile Approximation in Streams," FOCS 2016
- Apache DataSketches documentation (theta sketches, KLL, HLL)

*Note on verification: the Redis HyperLogLog figures and Caffeine's W-TinyLFU and 4-bit Count-Min description were read from the linked pages today. The Akamai paper couldn't be fetched, so its "around three quarters one-hit wonders" figure is from memory and stated loosely. Bloom, HyperLogLog and Count-Min formulas are standard results from the cited papers; the Ribbon (~30% smaller) and cuckoo (better below ~3%) comparisons come from those papers as I know them and weren't re-checked today.*
