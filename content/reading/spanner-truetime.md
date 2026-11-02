---
title: "Spanner, TrueTime and the problem of time in distributed databases"
date: 2026-11-02
summary: Why clocks are the hard part of globally distributed SQL, how Google Spanner uses TrueTime and commit wait to get external consistency, how CockroachDB and YugabyteDB manage without atomic clocks, and how cloud clock services like AWS ClockBound changed the trade-off.
tags: databases, distributed systems
daily: true
---

Ask a single-node database what happened first and it can always tell you: there's one clock and one log. Spread the database across continents and that question gets surprisingly hard. Each machine's clock drifts, network delays vary, and two transactions on opposite sides of the world can each believe they committed first. Most distributed databases either give up on real-time ordering or route everything through a single coordinator. Google's Spanner did something different: it measured clock uncertainty precisely and made the database wait it out. This reading explains the problem, how Spanner's TrueTime and commit wait solve it, how open-source systems like CockroachDB approximate it with ordinary clocks, and how cheap, precise cloud clocks are now bringing the Spanner approach to everyone.

## 1. Why time matters to a database

A database needs to order transactions for several reasons:

- **Snapshot reads.** A read at time T should see every transaction that committed before T and none after. MVCC (the Oct 24 Postgres reading) gives each version a timestamp, and a snapshot is just "versions with timestamp ≤ T."
- **Conflict detection.** Two transactions that touch the same data must be ordered, so one wins.
- **Consistent backups and analytics.** Reading the whole database "as of 10:00:00" only means something if timestamps are comparable across machines.

On a single machine, a counter or the local clock works. Across machines, you need timestamps that agree with each other, and ideally with **real time**.

### The anomaly to avoid

Consider this sequence:

1. Alice removes Bob from a shared photo album. The transaction commits on server A and Alice is told it succeeded.
2. Alice then uploads a private photo to the album. That transaction commits on server B.

Any reader should see either neither change, the first only, or both, never the photo without the removal. With timestamps from server clocks, that's not guaranteed: if server B's clock is 50 ms behind server A's, the upload can get an *earlier* timestamp than the removal. A snapshot read between those two timestamps would show Bob the private photo while he's still in the album.

The property that rules this out is what the Spanner paper calls **external consistency**: if transaction T1 commits before T2 starts (in real time, as observed by anyone outside the system), then T1's commit timestamp is smaller than T2's. It's equivalent to **strict serializability**, serializability plus agreement with real-time order. It's the strongest isolation level, and the one users intuitively expect.

## 2. Why clocks are hard

Computer clocks are quartz oscillators that drift, typically by tens of parts per million, depending on temperature and hardware. NTP corrects them by asking time servers over the network, but its accuracy depends on network delay, and asymmetric or congested paths can leave clocks off by milliseconds, sometimes far more. Virtual machines add pauses. Leap seconds have broken many systems. And worst of all, **a clock doesn't know how wrong it is**. A server that thinks it's 10:00:00.000 might really be at 09:59:59.950 or 10:00:00.100, and nothing tells it which.

Distributed systems theory has two classic responses:

- **Logical clocks** (Lamport, 1978): counters that increase with each event and travel with messages, so causally related events are ordered. They ignore real time entirely, so two transactions with no message between them (Alice's two transactions on different servers, linked only through Alice herself) can be ordered wrongly.
- **A central timestamp oracle**: one service hands out increasing timestamps. Google's earlier Percolator system did this, and TiDB uses a "placement driver" timestamp oracle today. It's simple and correct, but every transaction needs a round trip to one place, which is costly across continents and a single point to scale.

Spanner's insight was a third option: **use real clocks, but know exactly how uncertain they are**.

## 3. TrueTime

Spanner was described in the 2012 OSDI paper "Spanner: Google's Globally-Distributed Database" by James Corbett and many colleagues. Its foundation is an API called **TrueTime**.

TrueTime doesn't return a single time. `TT.now()` returns an **interval** `[earliest, latest]`, with a guarantee: the true absolute time is somewhere inside it. The interval's half-width is called **ε** (epsilon), the instantaneous uncertainty. Two helper calls follow from it: `TT.after(t)` is true if `t` has definitely passed, and `TT.before(t)` is true if `t` definitely hasn't arrived yet.

Making that guarantee hold took hardware. Each Google datacenter has **time masters**: most with GPS receivers, and some ("Armageddon masters") with atomic clocks, so the two kinds fail in different ways (GPS can suffer antenna or radio interference; atomic clocks drift slowly but independently of GPS). Every machine runs a daemon that polls several masters, across datacenters, rejects outliers, and synchronizes its local clock.

Between polls, the local clock drifts, so the daemon widens its uncertainty over time, assuming a conservative worst-case drift rate (200 microseconds per second in the paper). With polls every 30 seconds, uncertainty forms a sawtooth: it's small right after a poll and grows until the next one. The paper reports that ε typically varied between about 1 and 7 ms, averaging about 4 ms. If a machine's clock is found to drift worse than the bound, it's evicted.

The clever part isn't that Spanner's clocks are accurate; it's that their **error is bounded and known**. A system that knows "the true time is within ±4 ms of what I see" can reason about ordering safely.

## 4. Commit wait

<!--diagram:spanner-commit-wait-1102-->

Spanner uses TrueTime to assign commit timestamps that respect real time. For a read-write transaction, roughly:

1. The transaction acquires locks (Spanner uses two-phase locking for read-write transactions) and does its work.
2. The coordinating leader picks a commit timestamp `s` no smaller than `TT.now().latest`. Since true time is at most `latest`, `s` is at least the current true time.
3. **Commit wait**: the leader waits until `TT.after(s)` is true, meaning `TT.now().earliest > s`, so `s` has definitely passed in real time.
4. Only then does it release locks and tell the client the transaction committed.

Why does this give external consistency? Suppose T1 commits and its client sees the reply. Because of commit wait, the real time at that moment is already later than T1's timestamp `s1`. If T2 then starts, anything it does happens later still, and its own timestamp will be at least `TT.now().latest` at some point after that, which is greater than the real time, which is greater than `s1`. So `s2 > s1`, matching real time order. The uncertainty is absorbed by waiting, not by trusting clocks.

The cost is the wait itself: about 2ε on average, a few milliseconds in the 2012 paper. Spanner overlaps that wait with the Paxos replication it has to do anyway (the commit record must be replicated to a majority of replicas, which often takes longer than the wait), so in practice commit wait adds little latency. The paper makes the point directly: lowering ε matters, because it directly bounds commit latency. Google invested in better time infrastructure for exactly this reason.

## 5. The rest of Spanner's design

TrueTime is the famous part, but it sits inside a carefully layered system.

**Splits and Paxos groups.** Data is divided into ranges of keys (Spanner calls them splits). Each split is replicated, typically across three or five zones or regions, by its own **Paxos** group, with a long-lived leader holding a lease. This is the same multi-group design as the multi-Raft systems in the Oct 27 Raft reading. Spanner's leader leases are themselves based on TrueTime, so leaders know their leases are disjoint.

**Transactions across splits.** A transaction touching several splits uses **two-phase commit** across their Paxos leaders. One leader acts as coordinator. Each participant prepares, picks a prepare timestamp, and logs the prepare through Paxos. The coordinator chooses a commit timestamp at least as large as every participant's prepare timestamp and `TT.now().latest`, does commit wait, and then everyone commits. Because each participant is a replicated Paxos group, the classic 2PC weakness, a coordinator failure blocking everyone, is much less severe: the coordinator's state is replicated and a new leader takes over.

**Lock-free snapshot reads.** Read-only transactions don't take locks. They pick a timestamp (by default `TT.now().latest`, for a strong read) and read the MVCC versions at that timestamp. A replica can serve a read at time `t` if it knows it has seen every write up to `t`, which Spanner tracks as a "safe time" per replica. Followers can therefore serve consistent reads, which matters enormously for read-heavy global workloads. Clients can also ask for **stale reads** (for example, "as of 15 seconds ago"), which any replica can usually serve immediately without contacting the leader.

**Schema and data placement.** Spanner lets you interleave child tables in their parent (a customer's orders stored next to the customer), so related rows usually land in the same split and transactions stay single-group. Choosing primary keys that avoid hotspots matters too: a monotonically increasing key sends all inserts to one split, a problem the consistent hashing pack reading on Oct 27 discussed.

## 6. CAP and Spanner

Spanner is a CP system in CAP terms: during a partition, a minority side can't accept writes. Eric Brewer, who coined CAP and later joined Google, wrote a 2017 paper, "Spanner, TrueTime and the CAP Theorem," arguing that Spanner is "effectively CA": technically CP, but with partitions so rare in Google's private network, and availability so high (he reported Spanner exceeding five nines in practice), that users can treat it as both consistent and available.

The lesson isn't that CAP is wrong. It's that availability in practice depends at least as much on the network and operations as on the theoretical model. Spanner's availability comes from Google's private global network, redundant links and careful operations, and not everyone can say the same about theirs.

## 7. Doing without atomic clocks: CockroachDB

CockroachDB, inspired by Spanner, runs on ordinary hardware with NTP. Its clocks aren't bounded tightly or provably, so it can't use commit wait without waiting hundreds of milliseconds per transaction. Instead it uses a different combination.

**Hybrid logical clocks (HLC).** Each node keeps a timestamp combining physical time with a logical counter (from Kulkarni, Demirbas and colleagues' 2014 HLC paper). It stays close to physical time but, like a Lamport clock, always moves forward past any timestamp the node has seen in a message. Causally related events are ordered correctly even if clocks disagree.

**A configured maximum clock offset.** Operators promise that clocks are within a maximum offset of each other, 500 ms by default. Nodes continuously measure their offset from peers, and a node that finds itself too far out of sync with the rest of the cluster shuts itself down rather than risk violating consistency.

**Uncertainty intervals.** When a transaction with timestamp `t` reads a key and finds a value with a timestamp slightly above `t`, but within `t + max_offset`, it can't tell whether that write happened before or after it in real time. The clocks might simply disagree. So it treats the value as uncertain and **restarts** the read at a higher timestamp that includes it. This guarantees that a transaction sees any write that finished before it started, even with skewed clocks.

The result is **serializable isolation with strong guarantees for single-key and causally related operations**, but CockroachDB has historically been careful not to claim full strict serializability for all transactions. Its documentation and the 2016 blog post "Living without atomic clocks" explain that transactions touching disjoint keys with no causal link can, in a narrow window, be ordered differently from real time. In exchange, it avoids commit wait on every transaction and pays instead with occasional read restarts, which appear as `ReadWithinUncertaintyIntervalError` retries. High restart rates are a classic sign of clock trouble, and reducing clock skew directly reduces them.

YugabyteDB takes a similar approach, with HLC and a configured maximum clock skew, and also supports using AWS's precise clock service to tighten it.

## 8. The cloud clocks catch up

For years, the gap between Spanner and everyone else was hardware: GPS and atomic clocks in every datacenter, plus software that bounded the error. That gap has narrowed.

**Amazon Time Sync Service** provides time from satellite-connected and atomic reference clocks in each AWS region, and on supported EC2 instances it delivers time through the local Nitro hardware with **microsecond-range accuracy**. AWS also open-sourced **ClockBound**, a daemon and library that, like TrueTime, returns the current time as an interval with a bounded error, computed from the chrony time daemon's own uncertainty estimates.

That's exactly what's needed for Spanner-style commit wait, and AWS uses it. **Amazon Aurora DSQL**, announced in December 2024, is a distributed SQL database whose design posts ("Everything you don't need to know about Amazon Aurora DSQL") describe using these precise, bounded clocks to give transactions consistent timestamps across regions without a central timestamp service. Marc Brooker, one of its engineers, has written that cheap, accurate time is one of the key building blocks that changed what distributed databases can do.

Google Cloud exposes Spanner itself as a managed service, and Google has published improvements to TrueTime over the years that shrink uncertainty further. As error bounds fall from milliseconds to microseconds, commit wait becomes nearly free, and the trade-offs in section 7 matter less.

## 9. What this means in practice

You may never build a database, but these ideas show up whenever you choose or use one.

**Know your isolation level.** "Distributed SQL" products differ in what they actually guarantee. Spanner offers strict serializability. CockroachDB offers serializable with the clock caveats above. Many others default to snapshot isolation or read committed. Check the docs, and look for independent testing such as Jepsen analyses.

**Watch your clocks.** If you run CockroachDB, YugabyteDB or anything with a max-offset setting, monitor clock offset as a first-class metric. Use chrony with a good time source (in the cloud, the provider's time service), and treat clock alerts as seriously as disk alerts. A node that drifts past the limit will shut itself down, and if a shared time source misbehaves, many nodes can do so at once.

**Use stale reads where they're fine.** Both Spanner and CockroachDB let you read slightly in the past ("bounded staleness" or `AS OF SYSTEM TIME`). Those reads are served by nearby replicas without coordination, which is far faster in multi-region setups. Dashboards, search indexing and analytics rarely need the last second.

**Design keys and locality.** Commit latency in a global database is dominated by where replicas live and where the leaders are. Put leaders near the users who write, keep related rows together, and avoid sequential keys that create hotspots.

**Don't use wall clocks for ordering in your own code.** If you compare `updated_at` timestamps from different servers to decide which write wins, you've rebuilt the photo-album bug. Use version numbers, database-assigned timestamps from a single source, or the database's own transactional guarantees.

## Key takeaways

- Ordering transactions across machines is a clock problem. External consistency (strict serializability) requires timestamps that agree with real-time order.
- Clocks drift and don't know their own error; logical clocks ignore real time; a central oracle adds a global round trip.
- TrueTime returns an interval guaranteed to contain true time, backed by GPS and atomic clocks, with uncertainty of a few milliseconds in 2012.
- Commit wait picks a timestamp at the top of the interval and waits until it has surely passed, which costs about 2ε and gives external consistency.
- Spanner combines this with Paxos-replicated splits, two-phase commit across groups and lock-free snapshot reads.
- CockroachDB uses hybrid logical clocks, a maximum clock offset, and uncertainty restarts to get close without special hardware.
- Precise cloud clock services with error bounds, such as AWS ClockBound, now make Spanner-style designs practical outside Google.

## Discussion questions

1. Walk through the photo-album example with Spanner's commit wait. At what exact point would Bob's read be ordered after the removal?
2. If TrueTime's ε doubled overnight, what would happen to Spanner's write latency and throughput? What about read latency?
3. CockroachDB restarts reads that hit values in the uncertainty window. What workload pattern would cause the most restarts, and how would you reduce them?
4. Why do clock-based systems shut down nodes whose clocks are too far off, rather than just logging a warning?
5. Brewer calls Spanner "effectively CA." Would that argument hold for a Spanner-like system running across three public cloud regions connected over the internet?
6. TiDB uses a central timestamp oracle instead of clocks. When is that the better design?
7. Where in your own systems do you compare timestamps from different machines to decide ordering, and what could go wrong?

## Sources

- James C. Corbett et al., "Spanner: Google's Globally-Distributed Database," OSDI 2012, https://www.cs.princeton.edu/courses/archive/fall15/cos518/papers/spanner.pdf
- David F. Bacon et al., "Spanner: Becoming a SQL System," SIGMOD 2017.
- Eric Brewer, "Spanner, TrueTime and the CAP Theorem," Google, 2017.
- Leslie Lamport, "Time, Clocks, and the Ordering of Events in a Distributed System," CACM, 1978.
- Sandeep Kulkarni, Murat Demirbas et al., "Logical Physical Clocks and Consistent Snapshots in Globally Distributed Databases" (hybrid logical clocks), 2014.
- Cockroach Labs, "Living Without Atomic Clocks" (2016) and CockroachDB documentation on clock synchronization, `--max-offset` and transaction retries, https://www.cockroachlabs.com/docs/stable/architecture/transaction-layer
- Netdata, "CockroachDB ReadWithinUncertaintyInterval restarts" and "clock synchronization error" guides, https://www.netdata.cloud/guides/cockroachdb/cockroachdb-readwithinuncertainty-restarts/
- AWS Database Blog, "Everything you don't need to know about Amazon Aurora DSQL, Part 5: How the service uses clocks," https://aws.amazon.com/blogs/database/everything-you-dont-need-to-know-about-amazon-aurora-dsql-part-5-how-the-service-uses-clocks
- AWS, "It's About Time: Microsecond-Accurate Clocks on Amazon EC2 Instances," and the ClockBound project, https://github.com/aws/clock-bound
- Daniel Peng and Frank Dabek, "Large-scale Incremental Processing Using Distributed Transactions and Notifications" (Percolator), OSDI 2010.
- YugabyteDB documentation on hybrid time and clock skew.

The TrueTime figures (1 to 7 ms uncertainty, 200 μs/s drift assumption, 30-second polls) are from the 2012 paper. CockroachDB's 500 ms default offset was confirmed in search results on Nov 2, 2026, and the Aurora DSQL clocks post was located the same day. The "five nines" claim is Brewer's, as I recall it from his 2017 paper.
