---
title: Consistent hashing and partitioning
date: 2026-10-27
summary: The hash ring and its successors (rendezvous, jump and Maglev hashing), range partitioning, and how real systems combine them.
tags: distributed systems, databases
---

When you add a fifth server to four, `hash(key) mod N` moves about 80% of your keys, while consistent hashing moves only the 20% that the new server should own. That single property, minimal movement when membership changes, is why consistent hashing shows up in caches, load balancers and databases. This piece covers the ring, its successors (rendezvous, jump and Maglev hashing), the alternative of range partitioning, and how real systems combine them. Reading time is about 30 minutes.

## 1. Why partition, and why mod N breaks

### Partitioning in one paragraph

When data or traffic outgrows one machine, you split it into **partitions** (also called shards, or vnodes, or tablets, or regions, depending on the system) and spread them across nodes. Every request then needs a fast answer to one question: *which node owns this key?* A good partitioning scheme answers it with:

- **Balance**: each node gets roughly its fair share of keys and load.
- **Minimal disruption**: when a node joins or leaves, only a small fraction of keys change owner.
- **Cheap lookup**: no central directory on the hot path, or at most a small one every client can cache.
- **Locality, when you need it**: range scans over adjacent keys stay on one or a few nodes.

No single scheme maximises all four. Hash-based schemes are great at balance and terrible at locality; range-based schemes are the reverse. Consistent hashing is the hash-based family's answer to the second requirement.

### The obvious approach: hash mod N

Hash the key and take the remainder modulo the number of nodes:

$$\text{node}(k) = h(k) \bmod N$$

It balances perfectly, needs no directory, and is one CPU instruction. It's what most people write first, and it's fine as long as N never changes.

Now add a node. A key stays where it is only if `h(k) mod 4` equals `h(k) mod 5`. For uniformly distributed hashes that happens for 1 key in 5. The other **80% move**. Going from 10 to 11 nodes moves about 91%. In general, nearly every key moves, when the minimum necessary is only the new node's fair share, 1/(N+1).

For a cache, that means a near-total miss storm the moment you scale: every client suddenly looks for its keys on the wrong server, the database behind the cache gets hammered, and the outage you added capacity to prevent arrives anyway. For a database, it means copying almost the entire dataset across the network to add one machine.

### The goal, stated precisely

We want a function from keys to nodes such that when a node is added, the only keys that move are those that move **to the new node**, and when a node is removed, the only keys that move are the ones it **owned**. The expected fraction moved is then 1/N, the theoretical minimum. Karger and colleagues at MIT defined exactly this in their 1997 paper "Consistent Hashing and Random Trees", written to spread web-cache load without hot spots. The work became the foundation of Akamai, which several of the authors co-founded.

The next two sections are different ways to achieve that goal, with different trade-offs in balance, memory and lookup speed.

## 2. The hash ring and virtual nodes

### The ring

Picture the output space of a hash function, say 0 to 2⁶⁴ − 1, bent into a circle. Hash each **node's** identifier onto the circle. Hash each **key** onto the same circle. A key belongs to the first node you reach walking clockwise from it.

<!--diagram:c44a8ada-5237-->

When a node joins, it takes over only the arc between itself and its counter-clockwise neighbour; everything else is untouched.

That gives exactly the property we wanted:

- **Adding a node** splits one arc. Only keys on the part of that arc now in front of the new node move, and they all move *to* the new node.
- **Removing a node** merges its arc into its clockwise neighbour's. Only its keys move, and they all go to that neighbour.

Lookup is a binary search over the sorted list of node positions: O(log N) with N nodes, usually microseconds even for thousands of entries. Every client can hold the full ring, because it's just a sorted array of (position, node) pairs.

### The balance problem

With only a few nodes placed by random hashes, arcs come out very uneven. With N nodes at random positions, the largest arc is typically around ln(N) times the average. With 10 nodes, one may own two or three times its fair share, while another owns a sliver. Heterogeneous hardware makes it worse: a big machine should take more keys, but its position on the ring doesn't know that.

There's also a recovery problem. When node A fails, its whole arc goes to one neighbour, which suddenly carries double load, exactly when the system is already stressed.

### Virtual nodes

The fix is to place each physical node at **many** points on the ring, by hashing `A#0`, `A#1`, ..., `A#255`. Each point is a **virtual node** (vnode), and each owns a small arc.

- **Balance improves** with more points per node: with V vnodes per node, load imbalance shrinks roughly in proportion to 1/√V. Cassandra historically used 256 vnodes per node; with a smarter token allocation algorithm, newer versions default to 16 and still balance well.
- **Weighting is easy.** Give a machine with twice the disk twice as many vnodes.
- **Failure spreads out.** When a node dies, its hundreds of small arcs go to hundreds of different neighbours, so the extra load (and the work of re-replicating data) is shared by the whole cluster rather than dumped on one machine.

The cost is memory and lookup size: 100 nodes × 256 vnodes is 25,600 ring entries, still small, and a larger number of token ranges to track during repair and streaming.

### Replication on the ring

The ring also gives a natural replica placement rule, introduced by Amazon's Dynamo paper (2007): store each key on the first node clockwise **and the next N−1 distinct physical nodes** after it. That list is the key's **preference list**. With vnodes you skip positions belonging to a physical node already in the list, and with rack or zone awareness you also skip nodes in a zone already used, so the three replicas land in three zones.

Dynamo-style systems then use quorums: with N = 3 replicas, writes wait for W acknowledgements and reads for R, and choosing R + W > N (for example 2 + 2) means every read quorum overlaps the latest write quorum. Cassandra's `QUORUM` and `LOCAL_QUORUM` consistency levels are this idea.

### Ring metadata

Someone has to agree on where the nodes sit. Dynamo and Cassandra spread ring membership by **gossip**: every second, each node exchanges state with a few random peers, and changes reach the whole cluster in O(log N) rounds. Clients either learn the ring (Cassandra's token-aware drivers send each request straight to a replica) or talk to any node, which forwards the request as coordinator. Newer Cassandra versions are moving membership to a strongly consistent transactional metadata log, because gossip-based ring changes were a historical source of subtle bugs during concurrent topology changes.

## 3. Beyond the ring: rendezvous, jump, Maglev, bounded loads and slots

The ring with vnodes is the classic answer, but it isn't the only one, and for several common situations it isn't the best one.

### Rendezvous (highest random weight) hashing

Proposed by Thaler and Ravishankar in the late 1990s, rendezvous hashing has no ring at all. For each key, compute a score for **every** node, `score = h(key, node)`, and pick the node with the highest score.

```python
def owner(key, nodes):
    return max(nodes, key=lambda n: hash64(f"{n}:{key}"))
```

Why it works: when a node is added, a key moves only if the new node's score beats the old winner, which happens for 1/(N+1) of keys, and those keys all move to the new node. When a node is removed, only the keys it won move, each to its own second-highest scorer, so its load spreads evenly across all remaining nodes without any vnodes.

- **Replicas come free.** The top k scores give a k-node preference list.
- **Weights** are supported with a simple transform of the score (`-w / ln(h)` with h normalised to (0,1)).
- **The cost is O(N) per lookup.** Fine for tens or a few hundred nodes, too slow for thousands without a hierarchy (hash to a group, then within it).

It's a good default when the node list is small and changes occasionally, such as choosing a cache server or a shard owner among a few dozen machines.

### Jump consistent hash

Lamping and Veach at Google published jump consistent hash in 2014. It maps a key to a bucket number in `[0, N)` with no memory and a few lines of code:

```c
int32_t jump_hash(uint64_t key, int32_t num_buckets) {
    int64_t b = -1, j = 0;
    while (j < num_buckets) {
        b = j;
        key = key * 2862933555777941757ULL + 1;
        j = (b + 1) * ((double)(1LL << 31) / (double)((key >> 33) + 1));
    }
    return b;
}
```

The idea: as the number of buckets grows from 1 to N, a key "jumps" to the newly added bucket with probability 1/n at step n, and the loop computes where those jumps land without visiting every step, in O(ln N) time. Balance is essentially perfect, and growing from N to N+1 moves exactly the expected 1/(N+1) of keys.

The catch: buckets are **numbered**, and you can only add or remove at the end. Removing bucket 3 of 10 isn't supported. That makes jump hash ideal for mapping keys to a fixed set of **shards** (which you then replicate and place on machines by other means), and a poor fit for mapping directly to servers that fail at random.

### Maglev hashing

Google's Maglev network load balancer (NSDI 2016) needed something else: lookups at line rate for millions of packets per second, near-perfect balance across backends, and *mostly* stable assignment so existing TCP connections keep hitting the same backend.

Maglev builds a **lookup table** of fixed prime size M (for example 65,537). Each backend derives its own permutation of table slots from two hashes of its name (an offset and a skip). The table is filled round-robin: each backend in turn claims its next preferred empty slot, until the table is full. Lookup is `table[h(flow) mod M]`, a single array access.

Every backend ends up with almost exactly M/N slots, so balance is excellent. When a backend changes, most slots keep their owner, though slightly more than the theoretical minimum move. Maglev accepts that trade-off and also keeps a connection-tracking table so in-flight connections survive. Envoy's `MAGLEV` load-balancing policy and several eBPF load balancers use the same algorithm.

### Consistent hashing with bounded loads

Even with perfect key balance, *request* load is not balanced if some keys are much hotter. Mirrokni, Thorup and Zadimoghaddam (Google, 2016) proposed a simple fix: give every node a capacity of `(1 + ε)` times the average load. Hash as usual, but if the chosen node is at capacity, keep walking clockwise to the next node with room. With ε = 0.25, no node ever exceeds 125% of average, and keys still move only a bounded amount when nodes change.

Vimeo implemented this in HAProxy in 2016 to route video requests to caches; their write-up reported a large drop in cache bandwidth imbalance, and the option (`hash-balance-factor`) is now part of HAProxy.

### Fixed slots plus a directory

Many databases avoid ring arithmetic entirely: hash keys into a **large, fixed number of slots**, and keep a small, explicit **slot-to-node map**. Redis Cluster uses 16,384 slots; Couchbase uses 1,024 vBuckets; Elasticsearch fixes the number of primary shards when an index is created.

Rebalancing then means moving whole slots from one node to another, chosen deliberately by an operator or a balancer, not by hash luck. The map is small enough for every client to cache. The trade-off is that the slot count is fixed up front: too few and you can't spread across many nodes; Elasticsearch's answer is the split and shrink APIs, which create a new index with a multiple or factor of the shard count.

### Comparison

| Scheme | Lookup cost | Memory | Balance | Arbitrary node removal | Typical use |
| --- | --- | --- | --- | --- | --- |
| Ring + vnodes | O(log NV) | N × V entries | Good with enough vnodes | Yes | Dynamo, Cassandra, Riak, memcached clients |
| Rendezvous | O(N) | Node list | Very good | Yes | Small pools, replica choice |
| Jump hash | O(ln N) | None | Near perfect | No, append/remove at end only | Key to shard number |
| Maglev | O(1) | Table of M entries | Near perfect | Yes, slightly more churn | L4/L7 load balancers |
| Bounded loads | Ring + walk | Ring + load counts | Capped at (1+ε)× average | Yes | Caches with hot keys |
| Fixed slots + map | O(1) | Slot map | Controlled by balancer | Yes, by moving slots | Redis Cluster, Couchbase, Elasticsearch |

## 4. Range partitioning, hot keys and real systems

### Range partitioning: the other family

Hashing destroys key order: `user:1000` and `user:1001` land on unrelated nodes, so a range scan has to ask every partition. When queries need order (time ranges, prefix scans, "next page after X"), systems partition by **key range** instead: partition 1 holds `a` to `f`, partition 2 holds `f` to `m`, and so on.

This is how Bigtable (tablets), HBase (regions), Spanner (splits), CockroachDB (ranges) and TiKV (regions) work. A small, strongly consistent metadata table maps ranges to nodes, and clients cache it. Ranges are not fixed in advance; they **split** when they grow past a size threshold (CockroachDB's default is 512 MiB) or when they get too much load, and adjacent small ranges **merge**. A background balancer moves ranges, or their Raft leaders, between nodes to even out disk and traffic.

Range partitioning brings back the problem hashing solved: **skew from key order**. If keys increase monotonically (timestamps, auto-increment IDs, time-ordered UUIDs such as UUIDv7 or ULIDs), every new write lands in the last range, so one node takes all inserts while the rest sit idle. Splitting doesn't help, because the new right-hand range immediately becomes the hot one. Spanner's schema guidance explicitly warns against monotonically increasing primary keys for this reason. Common fixes:

- **Prefix with a hash or shard number**: `(hash(user_id) % 16, timestamp)` spreads writes over 16 ranges, at the cost of fanning out range reads across 16.
- **Bit-reverse sequential IDs** so consecutive values land far apart.
- **Use random IDs** (UUIDv4) where scans by ID aren't needed, accepting worse index locality in B-tree storage.

MongoDB lets you choose per collection: a ranged shard key for locality or a **hashed shard key** for even writes, which is a nice illustration that the choice is about your query pattern, not about which algorithm is better.

### Hot keys: when balance by key isn't balance by load

All the schemes above balance **keys**. Real traffic follows something like a Zipf distribution, so one key (a celebrity's profile, a flash-sale product, a global counter) can receive more traffic than an entire node can serve. No partitioning function can split a single key. The options:

- **Cache it.** Put a short-TTL cache, or even an in-process cache, in front of hot reads. Many "hot partition" problems disappear here.
- **Replicate reads.** Serve reads for hot keys from all replicas, not just the leader, if staleness is acceptable.
- **Split the key.** Write to `counter#0` through `counter#9` chosen at random and sum them on read. Writes spread across ten partitions; reads fan out to ten. Only worth it for keys you know are hot.
- **Let the platform adapt.** DynamoDB's adaptive capacity and automatic splitting for heat (covered in the DynamoDB piece earlier this month) do a version of this automatically, though they can't split one item.
- **Bound the load** with consistent hashing with bounded loads (section 3) for caches, so a hot range spills to neighbours.

### Rebalancing without hurting yourself

Martin Kleppmann's *Designing Data-Intensive Applications* groups rebalancing approaches into three:

1. **A fixed number of partitions**, many more than nodes, moved whole (Redis Cluster, Couchbase, Elasticsearch, Riak). Simple and predictable; pick the count carefully up front.
2. **Dynamic partitioning** by splitting and merging ranges (HBase, CockroachDB, Spanner, MongoDB). Adapts to data volume; needs a metadata service.
3. **Partitions proportional to nodes**, each new node taking a share of existing ranges (Cassandra with vnodes).

Whichever you use, be careful with fully automatic rebalancing triggered by failure detection. A node that is slow rather than dead gets declared failed, its data starts streaming elsewhere, the extra streaming load slows other nodes, and they get declared failed too. Many operators prefer automation that proposes moves and a human or a rate-limited controller that applies them, and every system that moves data throttles the streaming rate.

### How real systems choose

| System | Partitioning | Notes |
| --- | --- | --- |
| Amazon Dynamo (2007 paper) | Ring with vnodes, preference lists, gossip | The paper that popularised the approach for databases |
| Amazon DynamoDB | Hash of partition key to partitions, split by size and heat, metadata-driven | Not a gossip ring; partitions managed by a central control plane |
| Apache Cassandra / ScyllaDB | Ring of tokens (Murmur3), vnodes; ScyllaDB moving to tablets | Token-aware drivers route directly to replicas |
| Redis Cluster / Valkey | CRC16 to 16,384 fixed slots, slot map | Slots moved explicitly; hash tags co-locate keys |
| Memcached clients | Ketama-style ring in the client library | Servers know nothing about each other |
| Apache Kafka | `murmur2(key) mod partitions` | Adding partitions remaps keys and breaks per-key ordering: the mod N problem in the wild |
| Spanner, CockroachDB, TiKV | Range partitioning with automatic split and merge | Each range is a Raft or Paxos group |
| MongoDB | Ranged or hashed shard key, chunks moved by a balancer | The choice is per collection |
| Envoy, Google Maglev | Ring hash or Maglev for request routing | Session affinity for stateful backends |

The Kafka row deserves a second look, because it catches teams regularly. Kafka's default partitioner is plain hash mod N, so increasing a topic from 12 to 16 partitions sends most keys to a different partition. Consumers that relied on all events for one key arriving in order on one partition may see them interleaved during the transition. The usual advice is to over-provision partitions at creation, or create a new topic and migrate.

### Summary

Use hash partitioning when access is by key and you need even load; reach for a ring with vnodes or rendezvous when nodes come and go, jump hash when mapping to numbered shards, and Maglev when lookups must be O(1). Use range partitioning when queries need order, and design keys so writes don't all land at the end. In every case, plan separately for hot keys, because no hash function can split one.

## Discussion questions

1. Work out by hand what fraction of keys move when a mod-N cache cluster grows from 8 to 9 nodes, and what fraction should move. What would that difference mean for your database during a traffic spike?
2. You have 6 cache servers that rarely change and a client library you control. Would you choose a ring with vnodes, rendezvous hashing or jump hash? What would change your answer if servers failed several times a day?
3. Your Kafka topic has 12 partitions and you need 48. Consumers rely on per-key ordering. Design the migration.
4. A time-series table in a range-partitioned database uses `(timestamp, device_id)` as its primary key, and one node is at 100% CPU. Explain why, and compare two key designs that fix it, including what each costs at read time.
5. One product page receives 30% of all reads during a sale. Which of the hot-key techniques would you apply, in what order, and why doesn't better partitioning help?
6. Automatic rebalancing triggered by failure detection can cascade. Sketch the guardrails you would put around a rebalancer for a 50-node cluster.

## Sources

Written from the references below. Links were not re-opened while writing this piece.

- David Karger et al., ["Consistent Hashing and Random Trees"](https://dl.acm.org/doi/10.1145/258533.258660), STOC 1997
- Giuseppe DeCandia et al., ["Dynamo: Amazon's Highly Available Key-value Store"](https://www.allthingsdistributed.com/files/amazon-dynamo-sosp2007.pdf), SOSP 2007
- David Thaler and Chinya Ravishankar, "Using Name-Based Mappings to Increase Hit Rates", IEEE/ACM Transactions on Networking, 1998 (rendezvous hashing)
- John Lamping and Eric Veach, ["A Fast, Minimal Memory, Consistent Hash Algorithm"](https://arxiv.org/abs/1406.2294), 2014 (jump hash)
- Daniel Eisenbud et al., ["Maglev: A Fast and Reliable Software Network Load Balancer"](https://research.google/pubs/maglev-a-fast-and-reliable-software-network-load-balancer/), NSDI 2016
- Vahab Mirrokni, Mikkel Thorup and Morteza Zadimoghaddam, ["Consistent Hashing with Bounded Loads"](https://arxiv.org/abs/1608.01350), 2016
- [Improving load balancing with a new consistent-hashing algorithm](https://medium.com/vimeo-engineering-blog/improving-load-balancing-with-a-new-consistent-hashing-algorithm-9f1bd75709ed), Vimeo Engineering, 2016
- [Consistent Hashing: Algorithmic Tradeoffs](https://dgryski.medium.com/consistent-hashing-algorithmic-tradeoffs-ef6b8e2fcae8), Damian Gryski, 2018
- [Redis cluster specification](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/), redis.io
- [Schema design best practices](https://cloud.google.com/spanner/docs/schema-design), Google Cloud Spanner docs (hotspots and key choice)
- Martin Kleppmann, *Designing Data-Intensive Applications*, chapter 6 (Partitioning)
