---
title: "Distributed locks, leases and leader election: why 'only one at a time' is so hard"
date: 2026-11-16
summary: Why mutual exclusion across machines is fundamentally different from a mutex, and how to get it right. Covers efficiency vs correctness locks, leases and why they need bounded clocks, process pauses, fencing tokens, the Redlock debate, Chubby and ZooKeeper recipes, etcd leases and revisions, Kubernetes leader election, database locks and advisory locks, and how to design so that a lock failure can't corrupt data.
tags: distributed systems
daily: true
---

Inside one process, "only one thread at a time" is a solved problem: take a mutex, and the operating system guarantees that nobody else holds it until you release it. Across machines, nothing provides that guarantee. A process that holds a lock can be paused for seconds by garbage collection, cut off by a network partition, or simply crash without releasing anything, and the rest of the system can't tell which of these happened. Every distributed lock is therefore really a **lease**: a lock that expires. And a lock that expires can be held by two processes at once, from their own points of view. This reading explains why, what that means for leader election and job scheduling, how systems like Chubby, ZooKeeper and etcd approach the problem, what the famous argument about Redis's Redlock was about, and the one technique (fencing) that makes locks safe when the storage cooperates. It draws on Martin Kleppmann's writing and *Designing Data-Intensive Applications*, the Chubby and ZooKeeper papers, Gray and Cheriton's original paper on leases, and the etcd and Kubernetes documentation.

## 1. Why you'd want a distributed lock

The need comes up all over backend systems:

- **Exactly one worker should run a scheduled job** (send the nightly invoices, rotate a key, compact a table).
- **One leader** should coordinate a cluster: assign partitions, run a controller loop, or accept writes for a shard.
- **Serialise access to an external resource** that can't handle concurrent changes, such as a file in object storage, a third-party API account, or a legacy system.
- **Avoid duplicate work**, such as two servers regenerating the same expensive cache entry at the same time.

Kleppmann's 2016 post "How to do distributed locking" makes the most useful distinction for all of these. Ask what happens if the lock fails and two processes both think they hold it:

- **Efficiency locks.** The lock just saves work. If it fails, two workers do the same computation, or a user gets two identical emails. Annoying but not harmful. A simple, fast, occasionally wrong lock is fine.
- **Correctness locks.** The lock protects an invariant. If it fails, data gets corrupted, money moves twice, or a file is overwritten with stale content. Here, "occasionally wrong" isn't acceptable, and as the rest of this reading shows, the lock alone can't give you the guarantee; the protected resource has to help.

Knowing which kind you need decides almost everything else.

## 2. From locks to leases

Suppose a lock service hands out locks that last until released. If the holder crashes, the lock is never released, and the system deadlocks forever. So every practical distributed lock has a timeout: the holder gets the lock for a period, must renew it before it runs out, and loses it if it doesn't.

This is a **lease**, a term from Cary Gray and David Cheriton's 1989 paper "Leases: An Efficient Fault-Tolerant Mechanism for Distributed File Cache Consistency." Their insight was that a time-limited grant lets the server recover from a client's failure without having to know whether the client failed: just wait out the lease. The price is that correctness now depends on **time**: on clocks running at roughly the same rate on both sides, and on the holder noticing that its lease has expired before it acts.

That price is where the trouble starts. A lease holder reasons like this:

1. I acquired the lease at time T with a duration of 10 seconds.
2. It's now T+3, so I still hold it.
3. Therefore I can safely write to the resource.

Every step can go wrong. The holder's clock can jump, the check in step 2 and the action in step 3 aren't atomic, and between them the process can stop for an arbitrary amount of time.

## 3. Process pauses: the problem nobody can code around

The most important fact about distributed locks is that **a process can pause for any length of time at any point in its execution, without knowing it**. Causes include:

- **Garbage collection.** Stop-the-world GC pauses can last seconds and, in pathological cases, minutes. Kleppmann's post cites HBase, which used to have exactly this problem: a region server paused in GC long enough for its ZooKeeper session to expire, another server took over its regions, and the first one woke up and kept writing.
- **Virtual machine suspension or live migration**, which can freeze a guest for a noticeable period.
- **Operating system scheduling and swapping**, page faults on a memory-mapped file, or a slow disk on a synchronous log write.
- **`SIGSTOP`**, a debugger, or a laptop lid closing (for clients on laptops).
- **Network delays**: a request that the client sent while it held the lease can arrive at the storage service long after the lease expired.

Now the scenario that breaks every lease-only design:

<!--diagram:fencing-token-1116-->

1. Client 1 acquires the lease.
2. Client 1 checks that its lease is still valid, then pauses (GC) before writing.
3. The lease expires. Client 2 acquires it and writes to storage.
4. Client 1 resumes. From its perspective, it checked the lease a moment ago. It writes, overwriting client 2's newer data.

No amount of checking inside client 1 can fix this, because the pause can happen *after* the last check and *before* the effect lands. Checking the lease right before the write only shrinks the window. It doesn't close it. This is the core reason a lock service, however well built, can't guarantee mutual exclusion on its own.

## 4. Fencing tokens: making the resource enforce it

The fix is to have the **resource** reject operations from stale lock holders. Every time the lock service grants the lock, it also returns a **fencing token**: a number that increases with every grant. The holder includes the token with every write. The storage service remembers the highest token it has seen and rejects any write with a lower one.

In the scenario above, client 1 got token 33 and client 2 got token 34. Client 2's write with 34 succeeds and raises the storage's high-water mark to 34. Client 1's delayed write arrives with 33 and is rejected. Client 1's pause no longer matters.

Some properties of fencing:

- **It moves the guarantee to where the data is.** The resource is the only component that sees operations in their real order, so it's the only place mutual exclusion can be enforced.
- **The token has to be monotonic and come from a reliable source.** A consensus-based lock service can provide one easily: ZooKeeper's `zxid` or a znode's version, etcd's revision numbers, or a database sequence.
- **The resource has to support it.** That's the catch. A database can enforce it with a conditional write (`UPDATE ... WHERE fence_token < $new`) or a compare-and-set on a version column. Object stores with conditional writes (`If-Match` on an ETag, or preconditions on a generation number) can do it too. A third-party email API or a payment provider usually can't, so for those you need **idempotency keys** instead (covered in the earlier reading on exactly-once delivery), which serve a similar purpose: making the second attempt harmless.
- **Once you have fencing, the lock becomes an optimisation.** The resource rejects stale writers anyway; the lock just keeps them from wasting effort. That's a much more comfortable place to be.

Chubby, Google's lock service, had this idea built in: its locks could produce a **sequencer** (lock name, mode and generation number) that clients passed to servers, which checked it was still current. For servers that didn't support sequencers, Chubby used a **lock-delay**: after a holder failed without releasing the lock, nobody else could acquire it for a configurable period (up to a minute), to give in-flight requests from the old holder time to drain. Lock-delay reduces the risk but doesn't eliminate it.

## 5. The Redlock debate

Redis is often used for locks, and the debate about it is the clearest illustration of these ideas.

**Single-instance Redis lock.** The common pattern is `SET resource_key random_value NX PX 30000`: set the key only if it doesn't exist, with a 30-second expiry. To release, a Lua script deletes the key only if it still holds your random value, so you don't delete someone else's lock. This is fast and simple. Its weakness is that it's one Redis server: if it fails over to a replica that hadn't received the key yet (Redis replication is asynchronous), two clients can hold the lock.

**Redlock.** To avoid the single point of failure, Redis's creator Salvatore Sanfilippo (antirez) proposed Redlock: use N independent Redis masters (five in the description), try to acquire the key on all of them with the same random value and a short timeout, and consider the lock held only if a majority (at least three) granted it within less than the lock's validity time. The remaining validity is the TTL minus the time spent acquiring, minus an allowance for clock drift.

**Kleppmann's critique** (February 2016) made two main points:

1. **Redlock doesn't produce fencing tokens.** The random value is unique but not monotonic, so a storage service can't use it to reject stale writers. Without fencing, the process-pause problem from section 3 applies regardless of how many Redis nodes agree.
2. **Redlock depends on timing assumptions.** Its safety relies on bounded network delay, bounded process pauses and clocks that don't jump. If a Redis node's clock jumps forward (for example, after an NTP correction), keys there can expire early, and a second client can get a majority while the first still believes it holds the lock. Algorithms used for correctness, such as Raft or Paxos-based systems, are designed to stay *safe* under arbitrary delays and pauses and only lose *liveness* (progress), whereas Redlock loses safety.

His conclusions: if the lock is for **efficiency**, Redlock's cost and complexity aren't needed and a single Redis instance is fine. If it's for **correctness**, don't use Redlock; use a consensus-based system like ZooKeeper (or etcd), and enforce fencing tokens at the resource.

**Antirez's reply** ("Is Redlock safe?") argued that the timing assumptions are reasonable in practice if clocks are configured to slew rather than jump, that Redlock's check of elapsed time after acquiring reduces the impact of pauses during acquisition, and that if you have a resource that can enforce fencing tokens (essentially a linearisable compare-and-set), you might not need a distributed lock at all. That last point is partly a concession: it agrees that the protected resource is where the guarantee really lives.

The practical takeaway most engineers draw: Redis locks are fine for efficiency, with the expiry and safe-release pattern done correctly. For correctness, use fencing or idempotency at the resource, and a lock service built on consensus if you need the lock at all.

## 6. Locks on consensus systems: Chubby, ZooKeeper, etcd

Systems built on consensus (Paxos, ZAB, Raft) give you a linearisable store, which provides the two things a correct lock needs: a single agreed order of lock grants, and a monotonic number to use as a fencing token. They're still lease-based, because they still need to detect dead clients, but their state doesn't get lost or forked on failover.

### Chubby

Mike Burrows's "The Chubby lock service for loosely-coupled distributed systems" (OSDI 2006) describes Google's lock service: a small cell of five replicas running Paxos, exposing a file-system-like namespace where files can be locked. Notable lessons from the paper:

- It's intended for **coarse-grained** locks held for hours or days (such as leader election), not fine-grained locks taken per request.
- Most of its load turned out to be **name service and configuration storage**, not locking. Teams used it as a highly available place to store small amounts of metadata, such as who the current leader is.
- Clients hold **sessions** kept alive with KeepAlive RPCs; if the session lease expires, the client's locks are released. The client library has a **grace period** to ride out a master failover without losing the session.
- It introduced the sequencer and lock-delay mechanisms described in section 4.

### ZooKeeper

ZooKeeper (Hunt, Konar, Junqueira and Reed, USENIX ATC 2010) took a different approach: instead of providing locks, it provides primitives from which clients build locks, leader election, barriers and queues. The key primitives:

- **Ephemeral znodes**, which are deleted automatically when the client's session ends (by expiring or closing).
- **Sequential znodes**, which get a monotonically increasing suffix assigned by ZooKeeper.
- **Watches**, one-time notifications when a znode changes.

The standard lock recipe: each client creates an ephemeral sequential znode under `/locks/my-resource/`. The client whose znode has the lowest sequence number holds the lock. Every other client watches the znode **just before its own** (not the lock holder), so when a holder leaves only one client is woken. This avoids the **herd effect** where every waiter rushes to check at once. Leader election uses the same recipe: the lowest sequence number is the leader.

The sequence number or the znode's `zxid` (ZooKeeper's transaction ID) can serve as a fencing token. The ZooKeeper session timeout plays the role of the lease, and the same pause problem applies: if a leader's session expires during a GC pause, a new leader is elected while the old one may still be acting.

Apache Curator packages these recipes for Java clients and is the safer way to use them, because the recipes have many edge cases (connection loss during znode creation is a notable one).

### etcd

etcd (the Raft-based store behind Kubernetes) uses an explicit **lease** object: a client creates a lease with a TTL, attaches keys to it, and sends keep-alives. If the lease expires, all attached keys are deleted. Every change to the store gets a cluster-wide **revision** number, which increases monotonically.

etcd's concurrency package implements locks and elections on top of this: a client creates a key under a prefix attached to its lease, and the key with the lowest **create revision** holds the lock. That revision is a natural fencing token. etcd also supports transactions with comparisons on revisions, so a client can make a write conditional on still holding the lock key ("only if my lock key exists with this create revision"), which closes the gap for writes *to etcd itself*. For writes to other systems, fencing at those systems is still needed.

## 7. Leader election in practice

Leader election is a lock that's held for a long time and whose holder does continuous work. Kubernetes is a useful concrete example. Controllers such as the kube-controller-manager and kube-scheduler run several replicas, and only the leader acts. They coordinate through a **Lease** object in the API server (backed by etcd):

- The leader records its identity and a renew time in the Lease, and renews it periodically.
- Followers watch the Lease and try to take over if it hasn't been renewed within the lease duration.
- The leader steps down if it can't renew within a renew deadline, which is shorter than the lease duration, so it should stop acting before anyone else can start. The defaults for the kube-controller-manager are a 15-second lease duration, a 10-second renew deadline and a 2-second retry period.

Look at the timing: the gap between the leader's renew deadline and the lease duration is the safety margin. It works when the leader's process is running and checking. It fails in exactly the scenario from section 3: a leader that pauses for 20 seconds can't notice that it's past its deadline until it resumes, by which time a new leader is active. Kubernetes controllers tolerate this mostly because their actions go through the API server with **optimistic concurrency** (each object has a `resourceVersion`, and updates that are based on a stale version are rejected). That's fencing again, in a different form.

General lessons for leader election:

- **Make the leader's actions conditional** on state that a new leader would change: a term or epoch number, a version, a token. Raft does this internally with terms; your application should do it externally.
- **Step down early and stop all work**, including in-flight background tasks, when the lease can't be renewed. A leader that keeps a background thread running after losing leadership is a common bug.
- **Prefer designs where two leaders can't do damage**, for example by partitioning work so each partition has a single writer enforced by the storage layer, or by making operations idempotent.

## 8. Using the database you already have

Often the simplest correct lock is in the database that holds the data you're protecting, because then the lock and the data share one consistency model.

- **Row locks with `SELECT ... FOR UPDATE`** within a transaction serialise updates to specific rows. The lock is released when the transaction ends, including when the connection dies, so there's no stale holder.
- **`FOR UPDATE SKIP LOCKED`** lets many workers claim different rows from a jobs table without blocking each other (the job queues reading covered this pattern).
- **Postgres advisory locks** (`pg_advisory_lock`, `pg_try_advisory_xact_lock`) are application-defined locks keyed by a number. Session-level advisory locks are held until released or the session ends; transaction-level ones are released at commit or rollback. They're a good fit for "only one instance runs this migration or cron job", with the caveat that connection poolers in transaction mode (such as PgBouncer) can break session-level locks because consecutive statements may run on different server connections.
- **Conditional writes** (compare-and-set on a version column, DynamoDB condition expressions, `If-Match` headers on object stores) give you fencing directly. Often you can skip the lock entirely and just do optimistic concurrency: read the version, compute, write if the version is unchanged, retry otherwise.
- **DynamoDB-based lock libraries** (such as AWS's DynamoDB Lock Client) implement leases on a table with conditional writes and heartbeats. Since they live in the same store as the protected items, you can combine the lock check and the data write in one conditional transaction.

The pattern to aim for: **put the guard and the protected data in the same atomic operation.** A lock in one system protecting data in another always has the gap from section 3.

## 9. A design checklist

When someone proposes a distributed lock:

1. **Efficiency or correctness?** If two holders only cause duplicate work, a simple lease (single Redis key with expiry, or a database advisory lock) is enough. Stop there.
2. **Can the resource enforce ordering?** If yes, use fencing tokens or conditional writes, and treat the lock as an optimisation. If no, can you make the operation idempotent with a key the resource honours?
3. **Can you avoid the lock?** Partition the work so each item has one natural owner (consistent hashing, Kafka partitions assigned by the consumer group protocol), use optimistic concurrency, or use a single-writer queue.
4. **If you need a lock service, use one built on consensus** (etcd, ZooKeeper, Consul, or your cloud's equivalent) and get a monotonic token from it.
5. **Choose lease durations deliberately.** Long leases mean slow failover when a holder dies; short leases mean more false expiries during GC pauses or network blips. Make the renew deadline well short of the lease duration, and monitor GC pause times against it.
6. **Stop work on lease loss** and test it: inject a pause (`kill -STOP`), a partition, and a clock jump, and confirm that no stale writes land.
7. **Don't trust wall clocks** for lease decisions on the holder side. Use a monotonic clock to measure elapsed time since acquisition, and remember that the lock service's clock and yours may differ.

## Key takeaways

- Every distributed lock is a **lease**, because holders can crash. Leases bring in **time**, and with it the possibility that two processes both believe they hold the lock.
- **Process pauses** (GC, VM migration, swapping) and network delays can happen between checking the lease and acting on it. No client-side check closes that gap.
- **Fencing tokens**, monotonic numbers checked by the protected resource, are what actually provide mutual exclusion. Conditional writes, versions and idempotency keys are the same idea in other forms.
- Decide whether a lock is for **efficiency** (simple and occasionally wrong is fine) or **correctness** (the resource must enforce it).
- The **Redlock** debate comes down to fencing and timing assumptions. For correctness, use a consensus-based service and fencing; for efficiency, a single Redis key is enough.
- **Chubby, ZooKeeper and etcd** provide sessions or leases plus monotonic identifiers (sequencers, zxids, revisions) that serve as fencing tokens.
- The best lock is often **no separate lock**: put the guard in the same atomic operation as the data, or partition work so there's a single writer.

## Discussion questions

1. Your nightly billing job uses a Redis lock so only one instance runs. Is that an efficiency lock or a correctness lock? What would you change if a double run charged customers twice?
2. A leader holds a 15-second lease with a 10-second renew deadline and then pauses for 30 seconds. Walk through what happens. Which writes can it make after it resumes, and what would stop them?
3. You need to protect writes to a file in object storage. What conditional-write features would let you use fencing there, and what would you do if the store didn't have any?
4. Antirez argued that if a resource can check fencing tokens, you may not need a distributed lock at all. When is that true, and when does the lock still earn its place?
5. Where in your systems do two components both believe they're "the leader" or "the owner" during a failover? How would you detect it if it happened?
6. When would you choose a Postgres advisory lock over etcd, and what breaks if PgBouncer runs in transaction pooling mode?

## Sources

- Martin Kleppmann, ["How to do distributed locking"](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html), February 2016; *Designing Data-Intensive Applications* (O'Reilly, 2017), ch. 8 "The Trouble with Distributed Systems" and ch. 9
- Salvatore Sanfilippo (antirez), "Is Redlock safe?" (2016); Redis documentation, ["Distributed Locks with Redis"](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/)
- C. Gray, D. Cheriton, "Leases: An Efficient Fault-Tolerant Mechanism for Distributed File Cache Consistency," SOSP 1989
- M. Burrows, "The Chubby lock service for loosely-coupled distributed systems," OSDI 2006
- P. Hunt, M. Konar, F. Junqueira, B. Reed, "ZooKeeper: Wait-free coordination for Internet-scale systems," USENIX ATC 2010; ZooKeeper Recipes and Solutions documentation; Apache Curator recipes
- etcd documentation: leases, revisions, transactions, and the `concurrency` package (mutex and election)
- Kubernetes documentation: Leases, leader election in client-go, and kube-controller-manager `--leader-elect-*` flags
- PostgreSQL documentation: explicit locking, `SELECT ... FOR UPDATE`/`SKIP LOCKED`, advisory lock functions; PgBouncer documentation on pooling modes
- AWS, Amazon DynamoDB Lock Client (awslabs) and DynamoDB condition expressions

*Note on verification: Kleppmann's post (date, HBase GC example, fencing tokens and conclusions) was read today. The Redlock algorithm details (five masters, validity time), antirez's reply, the Kubernetes leader-election defaults (15 s, 10 s, 2 s), the Chubby and ZooKeeper details and the Postgres/PgBouncer behaviour come from the papers and docs as I know them and weren't re-fetched; check the current docs before relying on a specific default.*
