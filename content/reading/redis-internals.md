---
title: "Redis internals: one thread, many tricks"
date: 2026-10-22
summary: One thread, compact encodings, persistence, eviction and clustering: how Redis gets so much from a single core.
tags: databases, caching
---

Redis serves well over 100,000 operations per second on one core because it keeps everything in RAM, runs commands on a single thread, and picks a compact encoding for every value. This piece walks through each of those decisions, then covers how Redis survives restarts, forgets data on purpose, and scales past one machine. Reading time is about 30 minutes.

## 1. Why one thread is fast enough

The core of Redis is a single-threaded event loop called `ae` (short for "a simple event library"). One thread waits on `epoll` (Linux) or `kqueue` (BSD/macOS), wakes up when sockets are readable or writable, parses commands, executes them against in-memory data structures, and queues replies. There are no locks around the data, because only one thread ever touches it.

That sounds like a bottleneck, so it is worth asking where time actually goes in a cache server. A `GET` on a small key costs well under a microsecond of CPU for the lookup itself. The expensive parts are everything around it: the system calls to read the request and write the reply, and the network round trip. Antirez (Salvatore Sanfilippo, Redis's creator) argued for years that Redis is usually bound by memory or network long before CPU, and that a lock-free single thread is simpler and faster than a multi-threaded design full of contention.

### What the loop does each turn

1. `beforeSleep` runs housekeeping: flushes pending replies to clients, handles active expiry in a fast cycle, flushes the AOF buffer if configured.
2. The loop calls `epoll_wait` with a timeout equal to the time until the next timer event.
3. For each ready file descriptor, it runs the registered handler: accept a new client, read from a client, or write to a client.
4. It processes timer events. The main one is `serverCron`, which runs `hz` times per second (default 10) and does background chores: incremental rehashing, expiry sampling, checking child processes, replication heartbeats, statistics.

Because every command runs to completion before the next one starts, each command is atomic for free. `INCR` cannot interleave with another `INCR`. `MULTI/EXEC` transactions and Lua scripts are atomic for the same reason: nothing else runs while they execute. The flip side is the main operational rule of Redis: **one slow command blocks every client.** `KEYS *` on a 50 million key database, `SMEMBERS` on a set with millions of members, or a Lua script that loops for two seconds will freeze the server for that long. This is why `SCAN`, `SSCAN`, `HSCAN` and `ZSCAN` exist: they walk the keyspace in small batches with a cursor, so no single call holds the thread for long.

### Where threads do appear

Redis has never been purely single-threaded as a process. It is single-threaded for command execution.

- **Background I/O threads (****`bio`****)** handle work that would block: closing files, `fsync` of the AOF, and lazy freeing. Since Redis 4, `UNLINK` (and `FLUSHALL ASYNC`, plus the `lazyfree-lazy-*` options) removes a key from the keyspace immediately but hands the actual memory freeing to a background thread. Freeing a hash with 10 million fields can take seconds; doing it in the background keeps the main loop responsive.
- **I/O threads** (Redis 6 and later, `io-threads`) parallelize reading requests from sockets and writing replies. Command execution still happens on the main thread. With many clients and small commands, syscall overhead dominates, so offloading socket reads and writes can roughly double throughput on a multi-core box. Redis 8 reworked this further so I/O threads handle more of the client lifecycle.
- **Forked children** do RDB snapshots and AOF rewrites, covered in section 3.

This split is the key idea: keep the part that touches shared data single-threaded and lock-free, and move everything that waits on the kernel elsewhere.

### Pipelining and the real bottleneck

If a client sends one command and waits for the reply, throughput is capped by round-trip time. At 0.2 ms RTT inside a data centre, that is 5,000 requests per second per connection, no matter how fast the server is. Pipelining sends many commands without waiting, then reads all replies. It cuts both round trips and syscalls, because one `read()` can pick up dozens of commands. Benchmarks with `redis-benchmark -P 16` routinely show several times the throughput of unpipelined runs. In practice, most "Redis is slow" reports trace back to round trips, not to Redis.

### Why not just use many threads?

Other systems made different choices. **Memcached** is multi-threaded, with fine-grained locks on its hash table, and scales better on one big box for simple get/set. **KeyDB** forked Redis to add multi-threaded execution. **Dragonfly** uses a shared-nothing design: the keyspace is split into shards, each owned by one thread, so threads never share data. The Redis answer to many cores has been to run many Redis processes, one per core, and shard across them with Redis Cluster. Each process stays simple, and the operational model is the same whether you run 1 or 100 instances.

## 2. Data structures and their hidden encodings

Every Redis type has two or more internal encodings, and Redis switches between them automatically based on size. Small collections use flat, cache-friendly byte arrays; large ones switch to pointer-based structures with better big-O. You can see the choice with `OBJECT ENCODING key`.

### The redisObject and SDS

Every value in the keyspace is wrapped in a `redisObject`: a 16-byte header holding the type (string, list, hash, set, zset, stream), the encoding, LRU/LFU bits for eviction, a reference count, and a pointer to the actual data.

Strings are stored as **SDS** (Simple Dynamic Strings), not C strings. An SDS has a small header with the used length and the allocated capacity, followed by the bytes. That buys three things:

- `STRLEN` is O(1), because the length is stored.
- Strings are binary safe. A value can contain zero bytes, so you can store images or Protobuf blobs.
- Appends are amortized cheap, because SDS over-allocates (doubling up to 1 MB, then adding 1 MB at a time).

The header comes in several sizes (`sdshdr8`, `sdshdr16`, `sdshdr32`, `sdshdr64`) so a 10-byte string does not pay for a 64-bit length field.

String values themselves have three encodings:

| Encoding | When | Why |
| --- | --- | --- |
| `int` | The value is an integer that fits in 64 bits | Stored directly in the pointer field; no allocation. Small integers 0 to 9999 are shared objects. |
| `embstr` | String of 44 bytes or less | Object header and SDS are allocated in one 64-byte block, one `malloc`, one cache line |
| `raw` | Longer strings | Header and SDS allocated separately |

### Listpack: the small-collection workhorse

For small lists, hashes, sets and sorted sets, Redis uses a **listpack**: one contiguous byte array holding entries back to back. Each entry stores its encoding, its data, and its own length at the end, which allows walking backwards. Integers are packed into as few bytes as possible.

A listpack of 100 short entries might use 1 to 2 KB, where the equivalent hash table would use several times that in pointers, bucket arrays and per-entry allocations. Lookups are O(n) scans, but for n under about 128 a linear scan over one contiguous block is faster than hashing and chasing pointers, because it stays in CPU cache.

Listpack replaced the older **ziplist** in Redis 7. A ziplist entry stored the length of the *previous* entry, using 1 byte if it was under 254 bytes and 5 bytes otherwise. Growing one entry past 253 bytes could force its neighbour's header to grow, which could push that neighbour past the threshold, and so on: a "cascade update" that made some inserts O(n²) in the worst case. Listpack stores each entry's own length instead, so changes stay local.

The thresholds are configurable. Defaults in recent versions:

| Type | Small encoding | Switches when | Large encoding |
| --- | --- | --- | --- |
| Hash | listpack | more than 128 fields or any value over 64 bytes | hashtable |
| Sorted set | listpack | more than 128 members or any member over 64 bytes | skiplist + hashtable |
| Set (all integers) | intset | more than 512 members | hashtable |
| Set (strings, 7.2+) | listpack | more than 128 members or any member over 64 bytes | hashtable |
| List | listpack | grows past one node's size limit | quicklist |

Conversion is one way. Once a hash becomes a hashtable it stays one even if you delete fields. This is the basis of a classic memory trick from Instagram's 2011 engineering blog: instead of 300 million top-level keys, bucket them into hashes of about 1,000 fields each (`HSET media:1155 1155315 939`), keep each hash under the listpack threshold, and memory drops several-fold.

### Quicklist

A list that outgrows a single listpack becomes a **quicklist**: a doubly linked list whose nodes are listpacks. Each node holds a bounded chunk (by default up to 8 KB, set by `list-max-listpack-size -2`). This gets O(1) push and pop at both ends from the linked list, plus the memory density of listpacks. With `list-compress-depth`, interior nodes can be LZF-compressed, since queues mostly touch the head and tail.

### The dict and incremental rehashing

The main keyspace, and large hashes and sets, use Redis's `dict`: a chained hash table with power-of-two sizing. The interesting part is resizing. A naive hash table doubles by allocating a new array and moving every entry at once. With 100 million keys that would stall the single thread for seconds.

Redis instead keeps **two tables** during a resize. New entries go into the new table. Every lookup, insert or delete also moves one bucket from the old table to the new one, and `serverCron` spends about 1 ms per call moving more buckets. Lookups check both tables until the old one is empty. The cost of rehashing is spread across millions of operations, so no single command pays it. Redis also avoids resizing while a fork child is alive, because rehashing would touch many pages and defeat copy-on-write (section 3).

### Sorted sets: skiplist plus hashtable

A large sorted set is two structures pointing at the same members:

- A **hashtable** from member to score gives O(1) `ZSCORE` and membership.
- A **skiplist** ordered by (score, member) gives O(log n) insert, delete and rank, and cheap range scans for `ZRANGE` and `ZRANGEBYSCORE`.

A skiplist is a sorted linked list with express lanes. Each node gets a random height; Redis uses probability 1/4 of going up a level and caps it at 32 levels. Searching starts at the top lane and drops down when it would overshoot. Redis augments each forward pointer with a **span** (how many nodes it skips), which makes `ZRANK` O(log n). Antirez chose skiplists over balanced trees because they are simpler to implement, easy to range-scan, and the span trick is straightforward to add.

### The specialised types

- **Streams** store entries in a radix tree keyed by entry ID, with each tree node holding a listpack of several entries. Consumer groups track pending entries per consumer.
- **HyperLogLog** estimates distinct counts in at most 12 KB with a standard error of 0.81%, using a sparse encoding while small.
- **Bitmaps** are just strings addressed bit by bit (`SETBIT`, `BITCOUNT`).
- **Geo** is a sorted set whose scores are 52-bit geohashes.

The pattern is the same throughout: compact flat encodings while small, pointer structures when large, and the switch is invisible to the client.

## 3. Persistence: surviving a restart

Redis is an in-memory store that can optionally write to disk in two ways: periodic **RDB snapshots** and a continuous **append-only file (AOF)**. Most production setups use both, and since Redis 7 the AOF itself embeds an RDB-format base file.

<!--diagram:6eea278c-27e9-->

The snapshot path forks a child that writes in the background; the AOF path logs every write command on the main thread and lets the kernel or a background thread flush it.

### RDB and the fork trick

An RDB file is a compact binary dump of the whole dataset at one moment. `BGSAVE` (or a `save 3600 1 300 100 60 10000` schedule) calls `fork()`. The child process gets a copy of the parent's address space and walks the entire dataset, writing it to a temporary file, then renames it over `dump.rdb`. The parent keeps serving clients the whole time.

The trick is that `fork()` does not copy memory. The kernel marks every page of the parent as shared and read-only, and only copies a 4 KB page when one side writes to it. This is **copy-on-write (COW)**. The child sees a frozen snapshot for free, and the parent pays only for the pages it modifies while the child runs.

That has three operational consequences:

- **Memory headroom.** In the worst case, a write-heavy workload touches every page during the snapshot, and memory use doubles. Plan for well over the dataset size, and watch `rdb_last_cow_size` in `INFO persistence`.
- **Fork latency.** `fork()` itself must copy the page tables. For a 25 GB process that is about 50 MB of page tables, which can block the main thread for tens to hundreds of milliseconds. `latest_fork_usec` in `INFO stats` shows it.
- **Transparent huge pages hurt.** With THP enabled, a single-byte write copies a 2 MB page instead of 4 KB, so COW cost explodes. Redis warns at startup if THP is on, and the standard advice is to disable it.

RDB files are fast to load (often several times faster than replaying an AOF) and ideal for backups and for bootstrapping replicas. Their weakness is the window of loss: crash five minutes after the last snapshot and you lose five minutes of writes.

### AOF: logging every write

With `appendonly yes`, every write command is appended to a buffer in the Redis wire format (RESP), then written to the AOF. The `appendfsync` setting decides when the data is forced to disk:

| Setting | Durability | Cost |
| --- | --- | --- |
| `always` | fsync after every write batch; lose at most the in-flight batch | Slowest; throughput limited by disk fsync latency |
| `everysec` (default) | fsync once per second on a background thread; lose about 1 second, up to 2 if the disk stalls | Small; the usual choice |
| `no` | Leave it to the OS (often around 30 seconds on Linux) | Fastest, weakest |

With `everysec`, if the background fsync falls more than two seconds behind, Redis delays the main thread's writes rather than let the gap grow without limit. Slow disks can therefore show up as latency spikes on writes.

### AOF rewrite and multi-part AOF

The log only grows. A counter incremented a million times leaves a million `INCR` lines where one `SET` would do. **AOF rewrite** (`BGREWRITEAOF`, triggered automatically by `auto-aof-rewrite-percentage` and `auto-aof-rewrite-min-size`) forks a child, which writes the current dataset as a minimal file.

Before Redis 7, the parent had to buffer every write made during the rewrite and append that buffer to the child's output at the end, which cost memory and a pause. Redis 7 introduced **multi-part AOF**: an `appendonlydir` holding a base file (RDB or AOF format), one or more incremental AOF files, and a manifest that lists them. During a rewrite, the parent simply opens a new incremental file. When the child finishes its new base, the manifest is swapped atomically and old files are deleted. No rewrite buffer, no merge step.

With `aof-use-rdb-preamble yes` (the default), the base file is in RDB format, so a restart loads the compact snapshot quickly and then replays only the short incremental tail. That is the "hybrid" persistence most deployments now run.

### Which should you use?

- **Pure cache**, data can be rebuilt from the source of truth: often no persistence at all, or RDB only for a warm restart.
- **Data you would miss**: AOF `everysec` plus periodic RDB for backups.
- **Data you cannot lose**: Redis replication is asynchronous and AOF `always` is slow, so consider whether Redis should be your system of record at all. Managed variants such as AWS MemoryDB put a durable multi-AZ transaction log in front of Redis for exactly this reason.

## 4. Forgetting on purpose, copying, and splitting

### Expiry: lazy plus sampled

Keys with a TTL are also recorded in a second dict, `expires`, mapping key to expiry timestamp. Redis deletes expired keys in two ways:

- **Lazy expiry.** Every command that touches a key first checks whether it has expired, and deletes it if so. A client never sees an expired value.
- **Active expiry.** Lazy expiry alone would leak memory for keys nobody reads again. So, several times a second, Redis samples 20 random keys from `expires`, deletes the expired ones, and repeats immediately if more than 25% of the sample was expired. The loop is time-bounded so it cannot monopolise the thread. `active-expire-effort` (1 to 10) trades CPU for how aggressively stale keys are reclaimed.

The sampling is probabilistic by design: Redis aims to keep the share of expired-but-not-yet-deleted keys below about 25% without scanning everything. Two practical notes follow. If you set millions of keys with the same TTL, they all expire in the same second, and active expiry can show up as a CPU and latency spike; add jitter to TTLs. And replicas do not expire keys on their own: the primary sends explicit `DEL` commands, so the dataset stays consistent across the replication stream (replicas still hide logically expired keys from reads). Redis 7.4 added per-field TTLs on hashes (`HEXPIRE`), using the same lazy-plus-active approach inside the hash.

### Eviction: approximate LRU and LFU

When `maxmemory` is reached, the `maxmemory-policy` decides what to do:

| Policy | Evicts |
| --- | --- |
| `noeviction` (default) | Nothing; writes return an OOM error |
| `allkeys-lru` / `volatile-lru` | Least recently used, from all keys / only keys with a TTL |
| `allkeys-lfu` / `volatile-lfu` | Least frequently used |
| `allkeys-random` / `volatile-random` | Random keys |
| `volatile-ttl` | Keys with the nearest expiry |

True LRU needs a linked list of every key, reordered on every access: two pointers per key and constant pointer churn. Redis instead stores a 24-bit timestamp of last access in each object header and **samples**. On eviction it picks `maxmemory-samples` keys at random (default 5), and feeds them into a 16-entry "eviction pool" that keeps the best candidates seen across rounds. It evicts the oldest in the pool. Antirez's 2016 post on this showed that with 10 samples the result is very close to true LRU, at no memory cost.

LFU (Redis 4+) reuses the same 24 bits: 16 bits for a coarse "last decrement" time in minutes and an 8-bit **logarithmic counter**. An 8-bit counter maxes at 255, so increments are probabilistic: the higher the counter, the less likely an access bumps it (`lfu-log-factor`, default 10, puts a million hits near the top). The counter also decays over time (`lfu-decay-time`, default 1 minute per step), so yesterday's hot key can cool off. LFU tends to beat LRU for caches with a stable hot set and a long tail of one-off reads, because one scan of cold keys does not flush the hot ones.

### Replication

Redis replication is **asynchronous** and primary-driven:

1. A replica connects and sends `PSYNC <replication-id> <offset>`.
2. If the primary still has those bytes in its **replication backlog** (a circular buffer, `repl-backlog-size`, default 1 MB), it sends just the missing tail: a partial resync.
3. Otherwise it does a full resync: fork, produce an RDB (to disk, or streamed straight to the socket with `repl-diskless-sync`), send it, then stream every write since.

The replication ID plus offset uniquely names a point in history. After a failover, the promoted replica keeps the old ID as a secondary ID, so other replicas can partially resync against it instead of copying everything again. Sizing the backlog large enough to cover a typical network blip saves expensive full syncs.

Because replication is asynchronous, an acknowledged write can be lost if the primary dies before replicas receive it. `WAIT numreplicas timeout` blocks until that many replicas confirm, which narrows but does not close the gap, since Redis does not roll back the primary if `WAIT` times out. `min-replicas-to-write` makes a primary refuse writes when it cannot see enough replicas, which limits damage in a network partition. **Redis Sentinel** watches primaries, agrees by quorum that one is down, and promotes a replica.

### Redis Cluster

Redis Cluster shards the keyspace across primaries using **16,384 hash slots**. A key's slot is `CRC16(key) mod 16384`. Each primary owns a range of slots and has replicas for failover.

- **Clients route directly.** Smart clients cache the slot map. Send a command to the wrong node and it replies `MOVED 3999 10.0.0.5:6379`, telling the client to update its map.
- **Resharding is live.** When slots migrate, keys move one at a time. During migration, a node replies `ASK` for keys it has already handed over, a one-off redirect that does not update the client's map.
- **Multi-key commands need one slot.** `MGET a b` fails across slots. **Hash tags** fix this: only the part inside braces is hashed, so `{user:42}:cart` and `{user:42}:profile` land in the same slot.
- **Gossip.** Nodes talk on a cluster bus (data port + 10000), exchanging heartbeats that include a bitmap of the slots each node owns. Antirez chose 16,384 slots partly because that bitmap is 2 KB, cheap to send in every heartbeat, and clusters beyond about 1,000 primaries were never the goal.
- **Failover.** When a majority of primaries flag a primary as failed, its replicas hold an election and the winner takes over its slots with a higher configuration epoch.

Redis Cluster favours availability and latency over strong consistency: with asynchronous replication, a failover can lose a small window of acknowledged writes.

### A note on the ecosystem

In March 2024 Redis moved from the BSD licence to a source-available dual licence. The Linux Foundation forked the last BSD version as **Valkey**, now backed by AWS, Google Cloud and others, and Redis 8 later added an AGPLv3 option. The internals described here are shared by both, since they diverged only recently.

## Discussion questions

1. Redis made a single thread fast by keeping the data path lock-free and moving blocking work elsewhere. Where in your own services is a lock or shared structure the real bottleneck, and could a "one owner per shard" design remove it?
2. Your team stores 200 million small user-session records as top-level keys. Using what you know about listpack thresholds, how would you restructure them, and what would you lose (TTL per session, for example)?
3. A Redis primary with 40 GB of data and a write-heavy workload runs `BGSAVE` every 15 minutes, and p99 latency spikes every 15 minutes. List three causes from this piece and how you would confirm each.
4. When is `allkeys-lfu` a better fit than `allkeys-lru`? Describe a traffic pattern where LRU would evict the wrong keys.
5. Redis replication is asynchronous, and `WAIT` does not roll back. If a product requirement says "never lose an acknowledged payment", where should that data live, and what role, if any, should Redis play?
6. Why does Redis Cluster refuse cross-slot `MGET`, while a single instance allows it? What design choices would you make up front so a future move to Cluster is painless?

## Sources

Written from the references below. Links were not re-opened while writing this piece, so check the official docs for current defaults.

- [Redis persistence](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/), redis.io docs
- [Key eviction](https://redis.io/docs/latest/develop/reference/eviction/), redis.io docs
- [Redis replication](https://redis.io/docs/latest/operate/oss_and_stack/management/replication/), redis.io docs
- [Redis cluster specification](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/), redis.io docs
- [Diagnosing latency issues](https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/latency/), redis.io docs
- [Random notes on improving the Redis LRU algorithm](http://antirez.com/news/109), Salvatore Sanfilippo, 2016
- [Storing hundreds of millions of simple key-value pairs in Redis](https://instagram-engineering.com/storing-hundreds-of-millions-of-simple-key-value-pairs-in-redis-1091ae80f74c), Instagram Engineering, 2011
- [Redis source code](https://github.com/redis/redis): `ae.c`, `dict.c`, `listpack.c`, `t_zset.c`, `evict.c`
- [Valkey](https://valkey.io), the Linux Foundation fork
