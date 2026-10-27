---
title: "Raft: how a cluster agrees on one log"
date: 2026-10-27
summary: Leader election, log replication, the commit rule that keeps Raft safe, membership changes, snapshots, linearizable reads, and the production fixes (PreVote, CheckQuorum, leases) that etcd, Consul, CockroachDB and Kafka rely on.
tags: distributed systems, databases
daily: true
---

Much modern infrastructure runs on one quiet idea: a few servers keep identical copies of an ordered log, and every server applies the log's entries in the same order. If the log is the same everywhere, the state built from it is the same everywhere, and the cluster survives losing a minority of its machines. Kubernetes stores its state this way in etcd. CockroachDB and TiKV run thousands of these logs per cluster. Kafka 4 uses one to store its own metadata. The algorithm most of them use to agree on that log is Raft.

Raft was published in 2014 by Diego Ongaro and John Ousterhout at Stanford, under the title "In Search of an Understandable Consensus Algorithm." It won Best Paper at USENIX ATC that year. It doesn't solve a new problem: Paxos had solved consensus decades earlier. Its contribution is that people can actually understand and implement it. This reading walks through how Raft works, why each rule exists, and what real systems had to add to make it behave well in production.

## 1. The problem: replicated state machines

Imagine a key-value store on one server. Every write is a command (`set x=3`, `delete y`), and the store's state is whatever you get by applying every command in order. That's a *state machine*: deterministic, so the same commands in the same order always give the same state.

Now put copies on five servers. If every copy applies the same commands in the same order, all five end in the same state, and any of them can answer reads. The hard part is getting five servers to agree on the order when messages are delayed, lost or reordered, servers crash and restart, and nobody can tell a slow server from a dead one.

That agreement is *consensus*. A consensus algorithm for a replicated log must guarantee:

- **Safety.** No two servers ever apply different commands at the same log position, whatever the network does. Raft (like Paxos) assumes no Byzantine behavior: servers may crash or be slow, but they don't lie.
- **Liveness.** As long as a majority of servers are up and can talk to each other reasonably promptly, the cluster keeps making progress.

The FLP result (Fischer, Lynch and Paterson, 1985) shows that no deterministic algorithm can guarantee both in a fully asynchronous network where even one process may crash. Raft does what practical algorithms do: it never gives up safety, and it gets liveness by relying on timing (randomized timeouts) that works almost all the time.

The majority rule is the foundation. With 5 servers, any two majorities of 3 share at least one server. If every decision needs a majority, any later decision must involve someone who saw the earlier one. A 5-node cluster survives 2 failures, a 3-node cluster survives 1, and adding a 4th node to a 3-node cluster buys nothing, because a majority of 4 is 3. That's why clusters almost always have an odd number of members.

## 2. Why not Paxos?

Leslie Lamport's Paxos is the classic consensus algorithm and it's correct. The trouble is the gap between Paxos and a working system. Basic Paxos agrees on a single value. A replicated log needs a sequence of values, and "Multi-Paxos" was never fully specified in the original papers, so every team filled in the gaps differently. Google's 2007 paper "Paxos Made Live," about building Chubby, says there were "significant gaps between the description of the Paxos algorithm and the needs of a real-world system," and that the final system was based on an unproven protocol.

Ongaro and Ousterhout set *understandability* as the main design goal. They made two choices to get there:

1. **Decompose the problem** into nearly independent parts: leader election, log replication and safety, plus membership changes.
2. **Reduce the state space.** Logs aren't allowed to have holes, there's always at most one leader per term, and data flows only from leader to followers.

They tested the claim. In a user study with 43 students at Stanford and Berkeley, students watched video lectures on both algorithms and took a quiz on each. 33 of the 43 scored higher on the Raft quiz. That's a small study, but it's a rare example of a systems paper measuring the thing it claims.

## 3. Terms, roles and the two RPCs

A Raft server is always in one of three roles:

- **Follower.** Passive. It responds to requests from leaders and candidates. All servers start here.
- **Candidate.** A follower that hasn't heard from a leader for a while and is trying to become one.
- **Leader.** Handles all client writes and replicates them. There is at most one leader per term.

<!--diagram:raft-roles-1027-->

Time is divided into **terms**, numbered with increasing integers. Each term begins with an election. If a candidate wins, it leads for the rest of the term. If the vote splits, the term ends with no leader and a new term (and election) starts.

Terms act as a logical clock. Every message carries the sender's current term, and two rules apply everywhere:

- If a server sees a higher term than its own, it adopts that term and becomes a follower.
- If a server gets a request with a lower term than its own, it rejects it.

These two rules handle stale leaders cleanly. A leader that was partitioned away and comes back still thinks it's in term 4. The first reply it gets carries term 5, so it steps down. It can't do damage in the meantime, because it can't get a majority to accept its writes.

The core algorithm needs only two RPCs:

- **RequestVote**, sent by candidates during elections.
- **AppendEntries**, sent by the leader to replicate log entries. An AppendEntries with no entries is a heartbeat.

(A third RPC, InstallSnapshot, comes in with log compaction in section 8.)

Each server persists three things to stable storage before answering any RPC: its `currentTerm`, `votedFor` (who it voted for in this term), and its log. Everything else, including which entries are committed, can be rebuilt after a restart.

## 4. Leader election

Followers expect regular heartbeats. If a follower goes an **election timeout** without hearing from a valid leader, it assumes there isn't one and starts an election:

1. It increments its term and becomes a candidate.
2. It votes for itself.
3. It sends RequestVote to every other server.

Each server grants at most one vote per term, first come first served (with one restriction, covered in section 6). The candidate then waits for one of three outcomes:

- **It wins** with votes from a majority. It becomes leader and sends heartbeats immediately to stop other elections.
- **Another server becomes leader.** The candidate gets an AppendEntries from a leader whose term is at least its own, so it accepts that leader and becomes a follower.
- **Nobody wins.** The votes split, for example three candidates in a five-node cluster each get some. Each candidate times out and starts a new election in a new term.

The danger in the third case is repeated split votes forever. Raft's fix is simple: **randomized election timeouts**. Each server picks its timeout at random from a range; the paper suggests 150 to 300 ms. Usually one server times out well before the others, wins the election, and sends heartbeats before anyone else gets started. After a split vote, everyone picks new random timeouts, so a repeat is unlikely.

The paper gives a timing requirement that the system needs to hold for elections to work:

> broadcastTime ≪ electionTimeout ≪ MTBF

Here broadcastTime is how long it takes to send RPCs to every server and get replies, and MTBF is the average time between failures of a single server. If heartbeats take longer than the election timeout to arrive, followers keep starting elections against a healthy leader. If the timeout is close to how often servers fail, the cluster spends a large fraction of its time without a leader. With broadcast times of 0.5 to 20 ms (the paper's range, depending on how storage is configured) and servers that run for months, a timeout of a few hundred milliseconds fits comfortably. Production systems usually run longer: etcd defaults to a 100 ms heartbeat and a 1,000 ms election timeout, and its tuning guide recommends setting heartbeat close to round-trip time and election timeout around ten times that.

## 5. Log replication

Once elected, the leader serves clients. Each write goes like this:

1. The leader appends the command to its own log as a new entry. Each entry records the command, the term in which the leader received it, and its index (position).
2. The leader sends the entry to followers in AppendEntries, in parallel.
3. When a **majority** of servers (counting the leader) have stored the entry, the leader marks it **committed**.
4. The leader applies the entry to its state machine and replies to the client.
5. Later AppendEntries (including heartbeats) carry the leader's **commit index**, so followers learn which entries are committed and apply them too.

Committed is the key word. An entry is committed once it's on a majority, and Raft guarantees that a committed entry will never be lost or overwritten, and will eventually be applied by every server that stays up.

### Keeping logs consistent

How does the leader know a follower's log matches its own? Each AppendEntries includes `prevLogIndex` and `prevLogTerm`: the index and term of the entry just before the new ones. The follower only accepts the new entries if it has an entry at `prevLogIndex` with the matching term. Otherwise it rejects the request.

That consistency check gives Raft the **Log Matching Property**:

- If two logs have an entry with the same index and term, those entries hold the same command.
- If two logs have an entry with the same index and term, the logs are identical in every entry up to that point.

The first part holds because a leader creates at most one entry per index in a given term and never moves entries. The second holds by induction: every append checked the entry before it.

### Repairing a follower

After crashes and leader changes, a follower's log can be behind the leader's (missing entries), or it can have extra entries from an old term that never committed, or both. Raft's rule is blunt: **the leader's log wins**. The leader keeps a `nextIndex` for each follower, the next entry it plans to send. When a follower rejects an AppendEntries because the consistency check fails, the leader decrements `nextIndex` and retries. Eventually it finds the last point where both logs agree. The follower deletes everything after that point and takes the leader's entries.

Backing up one entry per round trip is slow for a follower far behind. The paper mentions an optimization that most implementations use: the follower's rejection includes the conflicting term and the first index it has for that term, so the leader can skip a whole term in one step.

A leader never deletes or overwrites entries in its own log. Data only flows outward from the leader. This one-directional flow is a big part of why Raft is easier to reason about than designs where any replica can contribute entries.

## 6. Safety: who is allowed to lead

Everything so far would be unsafe without one more restriction. Suppose a follower was offline while several entries committed, then came back and won an election. As leader, it would force its shorter log onto everyone and erase committed entries. Raft prevents this with the **election restriction**.

A RequestVote includes the candidate's last log index and last log term. A voter refuses to vote for a candidate whose log is less up to date than its own. "Up to date" is defined by comparing the last entries:

- If the last terms differ, the log with the later term is more up to date.
- If the last terms are the same, the longer log is more up to date.

Since a committed entry is on a majority, and a candidate needs votes from a majority, at least one voter has that entry. That voter will refuse a candidate missing it. So **only a server with every committed entry can become leader**. The paper calls this the **Leader Completeness Property**, and it's the heart of Raft's safety argument.

### The subtle commit rule

There's one more trap, shown in Figure 8 of the paper, and it's the part of Raft most often got wrong.

A leader in term 2 replicates an entry at index 2 to a minority, then crashes. A new leader in term 3 writes a different entry at index 2 on itself only, then crashes. The term 2 leader comes back, wins term 4, and keeps replicating its old index 2 entry until it's on a majority. Is it committed?

It looks committed: it's on a majority. But if this leader crashes before committing anything from term 4, the server holding the term 3 entry can win an election (its last term, 3, is higher than the others' last term, 2) and overwrite index 2 everywhere. An entry that was on a majority gets erased.

Raft's fix: **a leader only commits entries from its current term by counting replicas**. Once an entry from the current term is committed, every entry before it is committed too, by the Log Matching Property. Old-term entries are committed indirectly, never by counting.

This is why a new leader commits a **no-op entry** at the start of its term. Until it has committed something from its own term, it doesn't know which earlier entries are committed. The no-op settles that question quickly, which matters for reads (section 9) and membership changes (section 7).

Put together, these rules give **State Machine Safety**: if one server has applied an entry at some index, no other server will ever apply a different entry at that index. The paper includes an informal proof, and Ongaro's PhD dissertation includes a TLA+ specification and a formal proof of safety. Separately, the Verdi project at the University of Washington produced a machine-checked proof of a Raft implementation in Coq in 2016.

## 7. Changing the cluster's membership

Real clusters replace machines, so the set of servers has to change while the system runs. The danger: during the switch from the old configuration to the new one, different servers may use different configurations, and the old and new majorities might not overlap. Then two leaders could be elected for the same term, one by each majority.

The original paper solves this with **joint consensus**. The leader first commits a special entry for a combined configuration, C(old,new). While it's in effect, every decision (elections and commits) needs a majority of the old configuration *and* a majority of the new one, separately. Once C(old,new) is committed, the leader commits C(new). At no point can either configuration decide on its own.

Ongaro's dissertation introduced a simpler alternative that most implementations use: **add or remove one server at a time**. When you change membership by one server, any majority of the old configuration overlaps any majority of the new one, so no joint phase is needed. Bigger changes become a sequence of single-server changes.

The single-server approach turned out to have a bug. In 2015 Ongaro posted to the raft-dev mailing list that, with leader changes in the middle of overlapping single-server changes, two leaders could be elected in the same term. The fix is the rule from section 6: a new leader must commit an entry in its own term (the no-op) before it starts any configuration change. It's a good example of how subtle consensus is, even in an algorithm designed to be simple, and of why the no-op matters beyond reads.

Two practical refinements show up in nearly every production implementation:

- **Learners (non-voting members).** A new server starts with an empty log. If it joined as a voter right away, it would count toward the majority while it was still copying data, which can stall commits. So it joins as a learner first, catches up, and is promoted to voter once it has caught up. etcd and Kafka's KRaft both work this way.
- **Leadership transfer.** Before removing or restarting the leader, you can ask it to hand leadership to a chosen follower that's caught up: the leader stops taking writes, makes sure the target is current, and tells it to start an election right away. This avoids waiting a full election timeout with no leader during planned maintenance.

## 8. Snapshots and log compaction

A log that grows forever eventually fills the disk, and a restarted server would have to replay all of it. Raft handles this with **snapshots**. Each server independently snapshots its state machine at some committed index, records the last included index and term in the snapshot (so the consistency check still works for the next entry), and throws away the log up to that point.

When a follower is so far behind that the leader has already discarded the entries it needs, the leader sends its snapshot with **InstallSnapshot**. The follower replaces its state with the snapshot and continues from there.

Snapshotting is where much of the engineering effort goes in practice. A snapshot of a large state machine is expensive, so implementations take them in the background, often with copy-on-write tricks (fork the process, or use an MVCC storage engine where a consistent snapshot is cheap). CockroachDB and TiKV, which keep their data in RocksDB-style LSM engines, send snapshots as files from the storage engine rather than serializing the whole state through Raft.

## 9. Reads: the part people forget

Writes go through the log, so they're linearizable: once a write is acknowledged, every later read sees it. Reads are trickier. The obvious approach, letting the leader answer from its local state, is unsafe. A leader that's been partitioned from the majority might not know it has been replaced, and it would serve stale data while a new leader accepts writes.

There are three common ways to serve linearizable reads, with different costs:

1. **Read through the log.** Append the read as a log entry and answer once it's committed. Correct, but every read costs a disk write and a round trip to a majority.
2. **ReadIndex.** From Ongaro's dissertation. The leader records its current commit index, confirms it's still leader by exchanging heartbeats with a majority, waits until its state machine has applied up to the recorded index, and then answers locally. This costs one network round trip but no disk write. (A new leader must have committed its no-op first, since until then its commit index may be stale.) Followers can serve reads this way too, by asking the leader for the read index and waiting until they've applied that far.
3. **Leader leases.** The leader assumes no other leader can be elected for some time after its last successful heartbeat round, and serves reads locally during that window with no round trip at all. This is fastest but depends on bounded clock drift between servers: if a server's clock runs fast or a process pauses for a long GC, the assumption can break and stale reads become possible.

etcd exposes this choice directly: its reads are linearizable by default (using ReadIndex), and clients can ask for "serializable" reads that any member answers from local state, faster but possibly stale. CockroachDB uses leases at the level of each data range, with careful clock bounds, because ranges serve so many reads.

The lesson generalizes beyond Raft: "it's replicated with consensus" says nothing about whether reads are consistent. Always check which read path a system uses.

## 10. What production Raft added

The paper's core algorithm is correct, but running it on real networks exposed some liveness problems. Most serious implementations add the following.

**PreVote.** Suppose a follower is cut off from the cluster by a flaky link. It times out, increments its term and starts an election it can't win. It keeps timing out and incrementing. When it reconnects, its term is far higher than everyone else's, so the healthy leader steps down on seeing it, and the cluster goes through a needless election. PreVote (from Ongaro's dissertation, section 9.6) adds a trial round: before incrementing its term, a would-be candidate asks the others "would you vote for me?" They say yes only if they haven't heard from a leader recently and the candidate's log is up to date. Only if a majority says yes does it start a real election. A partitioned node never gets that majority, so it never inflates its term.

**CheckQuorum.** The opposite problem: a leader that can't reach a majority keeps thinking it's leader and keeps accepting client requests that will never commit. With CheckQuorum, a leader steps down if it hasn't heard from a majority within an election timeout. etcd implements both PreVote and CheckQuorum as options in its Raft library, and CockroachDB and TiKV use them as well.

**Partial partitions.** Real networks fail in odd ways: A can talk to B, B can talk to C, but A can't talk to C. In some of these cases, Raft clusters have been seen to flip leaders repeatedly or lose availability even though every node is up. PreVote and CheckQuorum help, but these cases are still a common source of incidents and a common thing for testing tools such as Jepsen to probe.

**Batching and pipelining.** The paper's AppendEntries sends one batch and waits. Production systems batch many client writes into one entry or one RPC, keep several AppendEntries in flight to each follower, and write to the leader's disk in parallel with sending to followers (it's safe because the leader still needs a majority, which can include itself only once its own write is done). These changes turn a few thousand writes per second into hundreds of thousands.

**Multi-Raft.** One Raft group has one leader, and all writes go through it, so a single group doesn't scale writes past one machine. Systems like CockroachDB and TiKV split data into ranges or regions (tens of megabytes to a few hundred megabytes each) and run a separate Raft group for each, with leaders spread across the cluster. A node may host thousands of groups, which brings its own problems: thousands of heartbeat streams, so implementations coalesce heartbeats between pairs of nodes and let idle groups go quiet. CockroachDB calls idle groups "quiesced."

## 11. Where Raft runs

- **etcd** is a key-value store built on Raft and the storage behind Kubernetes. Its Go Raft library (go.etcd.io/raft) is also used by other projects. The library implements only the algorithm. The application supplies storage, networking and the state machine, which made it easy to embed.
- **Consul** and **Nomad** use HashiCorp's own Raft library for their server clusters.
- **CockroachDB**, **TiKV** (under TiDB) and **YugabyteDB** use multi-Raft for replicating data ranges.
- **Kafka** replaced ZooKeeper with KRaft, a Raft-based quorum of controllers that stores cluster metadata as a Kafka log. Kafka 4.0 (2025) removed ZooKeeper support entirely. The Oct 25 Kafka reading covers how it fits into the rest of Kafka. KRaft is pull-based: followers fetch from the leader, as Kafka's data replication always has, rather than the leader pushing AppendEntries. It shows that Raft's ideas survive changes to the RPC style.
- **Redpanda** runs a Raft group per partition for data replication, instead of Kafka's ISR scheme.
- **MongoDB**'s replica set protocol (since version 3.2) is Raft-like, with terms and an election restriction, adapted to its pull-based oplog replication.

## 12. Raft and Paxos: closer than they look

Raft is often presented as an alternative to Paxos, but the two are close relatives. Heidi Howard and Richard Mortier's 2020 paper "Paxos vs Raft: Have we reached consensus on distributed consensus?" describes Raft in Paxos terms and finds the differences are mostly in leader election. In Raft, only a server with an up-to-date log can win, so a new leader never has to fetch missing entries. In Multi-Paxos, any server can become leader and then must learn missing entries from the others. Raft's choice makes the leader simpler and makes elections slightly more restrictive. They conclude that Raft's understandability comes largely from how it is presented, with its clear decomposition and its concrete, complete specification of every step, and less from fundamental algorithmic differences.

That's a useful frame. The lasting contribution of Raft isn't a new impossibility result or a new protocol trick. It's showing that a consensus algorithm can be specified completely enough, in a single paper with one page of rules (Figure 2), that hundreds of teams can implement it independently and mostly get it right.

## Key takeaways

- A replicated state machine works because every server applies the same log in the same order. Consensus is just agreement on that log.
- Majorities overlap, which is why every decision in Raft needs one, and why odd cluster sizes are the norm.
- Terms are a logical clock that lets stale leaders discover they're stale and step down.
- Randomized election timeouts keep split votes rare. Timing affects liveness only, never safety.
- The election restriction ensures only servers with every committed entry can lead.
- A leader commits only current-term entries by counting. That's why a new leader commits a no-op first.
- Membership changes must keep old and new majorities overlapping, through joint consensus or one server at a time (plus the no-op fix).
- Reads aren't automatically linearizable. You need a log read, ReadIndex or a lease, and leases rely on clocks.
- Production Raft needs PreVote, CheckQuorum, learners, batching and pipelining, and multi-Raft to scale writes.

## Discussion questions

1. Walk through the Figure 8 scenario yourself with three servers. At which exact step would a naive "on a majority means committed" rule lose data?
2. Why does adding a fourth server to a three-server cluster make it *less* available, not more? When might you still run an even-sized cluster?
3. A lease-based read is fast but relies on clocks. Under what deployment conditions would you be comfortable turning leases on? What failure would you watch for?
4. etcd's default election timeout is ten times its heartbeat interval. What happens to availability and to false elections if you run etcd across regions with 80 ms round trips and keep the defaults?
5. In multi-Raft, thousands of groups share the same machines. What new failure modes appear when one slow disk hosts replicas for hundreds of groups?
6. Kafka's KRaft uses fetch (pull) instead of AppendEntries (push). What does the leader lose and gain by not driving replication itself?
7. If Raft and Multi-Paxos are this similar, why do you think nearly every new system since 2014 chose Raft?

## Sources

- Diego Ongaro and John Ousterhout, "In Search of an Understandable Consensus Algorithm (Extended Version)," USENIX ATC 2014, https://raft.github.io/raft.pdf (also at https://www.scs.stanford.edu/~zyedidia/docs/papers/raft.pdf)
- Diego Ongaro, "Consensus: Bridging Theory and Practice," PhD dissertation, Stanford, 2014 (single-server membership changes, PreVote, ReadIndex, leases, TLA+ spec), https://github.com/ongardie/dissertation
- Diego Ongaro, "bug in single-server membership changes," raft-dev mailing list, July 2015.
- The Raft site, with visualizations and a list of implementations: https://raft.github.io
- Tushar Chandra, Robert Griesemer and Joshua Redstone, "Paxos Made Live: An Engineering Perspective," PODC 2007.
- Fischer, Lynch and Paterson, "Impossibility of Distributed Consensus with One Faulty Process," JACM 1985.
- Heidi Howard and Richard Mortier, "Paxos vs Raft: Have we reached consensus on distributed consensus?" PaPoC 2020, https://arxiv.org/abs/2004.05074
- Wilcox et al., "Verdi: A Framework for Implementing and Formally Verifying Distributed Systems," PLDI 2015, and Woos et al., "Planning for Change in a Formal Verification of the Raft Consensus Protocol," CPP 2016.
- etcd documentation, "Tuning" (heartbeat and election timeouts) and the go.etcd.io/raft README (PreVote, CheckQuorum, ReadIndex, learners), https://etcd.io/docs/ and https://github.com/etcd-io/raft
- CockroachDB documentation and blog on multi-Raft, range leases and quiescence, https://www.cockroachlabs.com/docs/stable/architecture/replication-layer
- Apache Kafka KIP-595, "A Raft Protocol for the Metadata Quorum," https://cwiki.apache.org/confluence/display/KAFKA/KIP-595

The paper's core details (the user study, the 150 to 300 ms timeouts, the timing inequality, Figure 8) come from the ATC 2014 paper, which I located on Oct 27, 2026. The etcd defaults, the 2015 membership bug, and the production behavior of CockroachDB, TiKV, Redpanda and MongoDB are from those projects' docs as I know them; the search results that morning were thin, so check the linked docs before relying on a specific number.
