---
title: "Load balancing: from DNS to the power of two choices"
date: 2026-10-30
summary: The layers a request passes through, L4 versus L7 balancing, why round robin and least-connections fail at scale, the power of two random choices, health checks and outlier ejection, subsetting, and lessons from Google, Netflix, Twitter and Envoy.
tags: networking, distributed systems
daily: true
---

Load balancing sounds like the simplest problem in backend systems: you have N servers and some requests, so spread the requests evenly. In practice it decides whether a slow server degrades one request or a whole fleet, whether a deploy is smooth or a retry storm, and how many machines you need to buy. The hard parts aren't the algorithms in textbooks. They're stale information, many independent balancers acting at once, servers that are alive but sick, and connections that last much longer than requests. This reading goes from the layers of balancing a request passes through to the algorithms, health checking and the production lessons that companies like Google, Netflix and Twitter have written about.

## 1. What "balanced" actually means

The goal isn't equal request counts. It's that **no server is overloaded while others sit idle**, measured in whatever resource actually runs out: CPU, memory, connections, or queue depth. Requests vary wildly in cost (a cached lookup versus a report), and servers vary too (different hardware generations, noisy neighbors, a JIT that hasn't warmed up, a garbage collection pause). An algorithm that gives each server the same number of requests can still leave one at 95% CPU and another at 40%.

Capacity planning makes this concrete. If your hottest server runs at 90% when the average is 60%, you have to provision for the hottest one. Google's SRE book makes this point in its chapter on datacenter load balancing: imbalance is wasted capacity, because the fleet's useful capacity is limited by its most loaded members. Better balancing directly translates into fewer machines.

A second goal is often more important than the first: **keep failures contained**. A balancer that keeps sending traffic to a broken server, or that piles all retries onto the remaining healthy servers until they fall over too, turns a single failure into an outage.

## 2. The layers

<!--diagram:lb-layers-1030-->

A request to a large service typically passes through several layers of balancing, each with a different view and granularity:

**DNS and anycast (global).** DNS resolution can return different IPs depending on the client's location and the health of each region. It's coarse: answers are cached by resolvers for the TTL and often longer, so shifting traffic away from a failed region can take minutes. **Anycast** advertises the same IP address from many locations via BGP, so the network itself routes each client to a nearby site. CDNs and DNS providers rely on it heavily, and it reacts as fast as routing converges.

**L4 load balancers (connections).** At layer 4, the balancer sees IP addresses and ports, not HTTP. It picks a backend for each TCP connection (or UDP flow) and forwards packets. Because it doesn't parse or terminate the protocol, it can be extremely fast. Google's **Maglev** (NSDI 2016) runs on ordinary servers, uses consistent hashing so that every Maglev machine picks the same backend for a given connection, and returns responses directly from backends to clients (direct server return). Facebook's **Katran** does the same with eBPF and XDP in the Linux kernel, GitHub built **GLB**, and Cloudflare built **Unimog**. In the cloud, AWS's Network Load Balancer plays this role. The consistent hashing pack reading on Oct 27 covered Maglev's hashing in detail.

**L7 proxies (requests).** At layer 7, the balancer terminates the connection, parses HTTP or gRPC, and picks a backend for each **request**. It can route by path or header, retry, add timeouts, terminate TLS, and see response codes and latencies. Envoy, NGINX, HAProxy, AWS ALB and Google's front ends work here. The price is more CPU per request and another hop.

**Client-side balancing (requests, inside the caller).** In a service mesh or with gRPC, the calling service (or a sidecar proxy next to it) holds a list of backend addresses from service discovery and picks one per request. No central balancer sits in the path. Twitter's Finagle, Netflix's Ribbon, gRPC's built-in policies and Envoy as a sidecar all work this way.

Why so many layers? Each solves a different problem. DNS and anycast handle geography and whole-site failures. L4 handles enormous packet rates cheaply and gives a stable virtual IP. L7 and client-side balancing make fine-grained, per-request decisions using information only they can see.

## 3. L4 versus L7, and the long-lived connection problem

The difference between per-connection and per-request balancing matters more than it seems.

HTTP/1.1 clients open several connections and send one request at a time on each, so balancing connections roughly balances requests. **HTTP/2 and gRPC multiplex many requests over one long-lived connection.** An L4 balancer only sees the connection, so if a client opens one connection, all of its requests go to one backend for hours. Add new backends during a scale-up and they get no traffic, because existing connections are pinned to the old ones.

This is the most common load-balancing surprise in gRPC deployments, and the fixes are all about moving to request-level balancing:

- Put an **L7 proxy** that understands HTTP/2 in front, so it balances each request across its own connections to backends.
- Use **client-side balancing**: gRPC clients resolve all backend addresses and keep a connection to each (or to a subset), then balance requests across them. The Oct 25 gRPC pack reading touched on this.
- Set a **maximum connection age** on servers, so clients periodically reconnect and get rebalanced. gRPC servers support `MAX_CONNECTION_AGE` for exactly this reason.

## 4. The algorithms

### Round robin

Send each request to the next server in order. It's simple, stateless apart from a counter, and works well when requests cost the same and servers are identical. It fails when they're not: a slow server gets the same share as a fast one, so its queue grows without bound. **Weighted round robin** gives servers shares proportional to their capacity, which helps with mixed hardware but needs the weights to be right.

### Random

Pick a server uniformly at random. Over many requests it's as even as round robin, but over short windows some servers get noticeably more than others. With n servers and n requests, the busiest server gets about log n / log log n requests instead of 1, a classic balls-into-bins result. Random needs no coordination, which is its main attraction.

### Least connections / least outstanding requests

Send each request to the server with the fewest requests currently in flight. This adapts automatically: a slow server accumulates in-flight requests and stops receiving new ones. With a single balancer, it's one of the best simple algorithms.

The problem is **multiple balancers**. Each one sees only its own in-flight counts. If 100 client-side balancers all see server 7 with zero outstanding requests (because it just came up, or just finished a burst), all 100 send their next request there at once. This **herd behavior** turns a newly started server into the most overloaded one. The same happens with any "pick the global best" rule based on information that's even slightly stale.

### The power of two random choices

The fix is a remarkable result from Michael Mitzenmacher's work in the 1990s (his 1996 PhD thesis and the paper "The Power of Two Choices in Randomized Load Balancing," published in 2001). Instead of picking one server at random or the best of all, **pick two at random and send the request to the less loaded of the two**.

With pure random assignment of n requests to n servers, the busiest server ends up with about log n / log log n. With two random choices, it drops to about log log n / log 2, which is an exponential improvement. Going from two choices to three helps only by a constant factor. Almost all the benefit comes from the second choice.

Two choices also fix the herd problem. Because each balancer picks its two candidates at random, different balancers compare different pairs, so they don't all converge on the same "best" server. A server that's momentarily least loaded will win some comparisons, not all of them. Least-loaded-of-two with stale information performs far better than least-loaded-of-all with the same stale information.

This is why "P2C" is the default in many modern balancers. Envoy's `LEAST_REQUEST` policy samples two hosts by default (`choice_count` is configurable) and picks the one with fewer active requests. Linkerd and Finagle use P2C with latency-aware load metrics. Netflix's 2018 post "Rethinking Netflix's Edge Load Balancing" describes moving Zuul from round robin to a choice-of-2 algorithm that combined the client's own in-flight count with load data reported by servers, plus penalties for recently failed or newly started servers.

### Latency-aware: EWMA

Outstanding requests measure queue length, not speed. A server can have few requests in flight but take a long time on each. Twitter's Finagle and Linkerd use **peak EWMA**: an exponentially weighted moving average of response latency that jumps up immediately when a slow response arrives and decays slowly, multiplied by the number of outstanding requests. Combined with P2C, it steers traffic away from slow servers quickly and returns it gradually.

### Consistent hashing

Sometimes you want the same key to go to the same server: a cache shard, a session, a user's partition. That's **consistent hashing** (ring, rendezvous, jump hash, Maglev). It trades balance for affinity, and it needs extensions like bounded loads to avoid hot servers. The Oct 27 pack reading covered it in depth, so here it's enough to say: use hashing when locality matters, and load-aware balancing otherwise.

## 5. Health checking

A balancer must know which backends can take traffic. There are two complementary approaches.

**Active health checks.** The balancer periodically sends a probe (a TCP connect, an HTTP `GET /healthz`, a gRPC health check) and marks a backend unhealthy after a few consecutive failures, healthy again after a few successes. They're simple and catch dead servers, but they have blind spots:

- A health endpoint that just returns 200 says the process is alive, not that it can serve real requests. If the database it depends on is down, `/healthz` still passes.
- A health endpoint that checks *every* dependency has the opposite problem. If a shared dependency fails, every backend fails its check at once, and the balancer removes the entire fleet. A degraded service becomes a completely unavailable one. Many balancers have a "panic" mode for this: Envoy, for example, stops honoring health status and spreads traffic over all hosts when the healthy fraction drops below a threshold (50% by default), on the theory that some service is better than none.
- Probes from many balancers to many backends add up. Ten thousand clients each checking a hundred backends every few seconds is real load.

A good compromise: the health check tests that the process can do useful local work, while dependency failures show up through real traffic (below) and are handled with circuit breakers.

**Passive health checks (outlier detection).** The balancer watches real responses. If a backend returns several consecutive errors, or its error rate or latency is far worse than its peers, it's **ejected** temporarily. Envoy's outlier detection defaults are a good reference: eject after 5 consecutive 5xx responses, evaluate every 10 seconds, eject for a base time of 30 seconds (multiplied by how many times the host has been ejected before), and never eject more than 10% of hosts at once. That last limit is the important one. It stops outlier detection from removing most of the fleet during a widespread problem, where the backends aren't the outliers at all.

Passive checks catch what active probes miss, the server that answers `/healthz` but fails real requests, and they cost nothing extra. Use both.

## 6. Graceful changes: draining, slow start and lame ducks

Most backend churn is planned: deploys, autoscaling, maintenance. Handling it well prevents a lot of errors.

**Draining.** When a server is about to stop, it should stop receiving new requests but finish the ones in flight. Google's SRE book describes a **lame duck** state: the backend is still running and answering in-flight requests, but tells clients (through health checks or response metadata) that it's going away, so they stop sending new work. Kubernetes models this with readiness probes (fail readiness first, so endpoints are removed) plus a termination grace period. A common bug is the race between a pod shutting down and the endpoint removal propagating to every client and proxy. The usual fix is a short sleep in a `preStop` hook so the server keeps serving while the update spreads.

**Slow start.** A freshly started server often can't handle a full share right away: caches are cold, JIT compilation hasn't happened, connection pools are empty. And least-request algorithms send it *more* traffic, because it has no outstanding requests. Slow start ramps a new backend's weight up over a window (Envoy and HAProxy both support this). Netflix's edge balancer applied a similar penalty to new servers.

**Connection draining on L4.** For long-lived connections, removing a backend from an L4 balancer doesn't move existing connections. Either wait for them to close, or have the server close them gracefully (HTTP/2 `GOAWAY`), so clients reconnect elsewhere.

## 7. Subsetting: not everyone connects to everyone

With client-side balancing, every client connecting to every backend gets expensive. A thousand clients and a thousand backends means a million connections, each with TLS state, health checks and memory. Google's SRE book describes the solution: **subsetting**, where each client connects to only a subset of backends.

The naive approach, each client picking a random subset, leads to uneven load: some backends end up in many subsets and others in few. The SRE book describes **deterministic subsetting**: clients are grouped into rounds, each round shuffles the backend list with a seed shared by that round, and each client in the round takes a different slice of the shuffled list. That way every backend gets roughly the same number of clients, and the assignment stays stable as long as membership doesn't change. Twitter's Finagle has a related scheme called **aperture** balancing, where each client balances over a small window of backends sized dynamically to its own load, with a deterministic layout so windows spread evenly across the backends.

Subset size is a trade-off. Too small, and a few slow or failing backends affect a large share of one client's capacity. Too large, and you're back to too many connections.

## 8. Overload: when balancing isn't enough

Load balancing spreads load. It can't create capacity. When total demand exceeds total capacity, a good balancer helps the system fail gracefully instead of collapsing.

- **Load shedding on the server.** Backends should reject excess work quickly, with a 503 or gRPC `RESOURCE_EXHAUSTED`, instead of queueing until everything times out. A fast rejection lets the client try elsewhere or back off. The Oct 22 rate-limiting reading covers this from the server side.
- **Retry budgets.** Retries multiply load exactly when the system is weakest. Limit retries to a fraction of normal traffic (for example, 10% extra) and never retry at more than one layer without coordination. Finagle and Envoy both support retry budgets.
- **Circuit breakers.** Envoy's circuit breakers cap pending requests, active requests and connections per backend cluster, so a slow dependency can't consume all of the caller's resources.
- **Criticality.** The SRE book describes tagging requests by criticality (for example, user-facing versus batch) so that under overload the system sheds the least important work first.

The interaction between balancing and overload is worth stating directly: least-request and EWMA policies move traffic *away* from struggling servers, which is great when one server is sick and dangerous when all of them are near capacity. Then shifting load just overloads the next server. That's why limits on ejection, retries and panic thresholds matter as much as the algorithm.

## 9. Lessons from production

**Google (SRE book, chapters 19 and 20).** Google's front-end balancing uses DNS and Maglev to choose datacenters and spread connections, then L7 front ends. Inside the datacenter, clients use subsetting and a **weighted round robin** where backends report their own utilization and error rates, and clients adjust weights accordingly. The key idea is that the backend knows its own load better than any client, so feeding that signal back beats guessing from outside.

**Netflix.** The 2018 Zuul post describes how round robin worked poorly at the edge when servers were unevenly loaded, especially during deploys and with new instances. The team moved to choice-of-2 with a combination of client-side in-flight counts and server-reported utilization, with penalties for servers that were new or had recently errored, and reported a reduction in load-related errors.

**Twitter (Finagle).** Finagle's balancers moved from round robin to P2C with least-loaded and later peak-EWMA load metrics, and then to aperture for very large clusters. Twitter's engineering posts emphasize that local decisions with good random sampling outperform attempts at global coordination.

**Envoy.** Envoy packages most of these ideas as configuration: round robin, least request (P2C), random, ring hash and Maglev policies; active health checks; outlier detection with ejection limits; panic thresholds; slow start; zone-aware routing; and circuit breakers. Its documentation's "Load balancing" section is one of the best practical overviews of the trade-offs.

**Zone awareness.** Cloud regions have several availability zones, and cross-zone traffic costs money and latency. Zone-aware routing prefers backends in the caller's own zone, spilling over only when local capacity is insufficient. It's an easy win, but it needs care: if zones have unequal capacity, strict locality overloads the smallest one.

## 10. A practical playbook

When designing or reviewing load balancing for a service:

1. **Know your layers.** Which balancers does a request pass through, and does each balance connections or requests?
2. **For HTTP/2 and gRPC, balance requests**, with an L7 proxy or client-side balancing, and set a maximum connection age.
3. **Default to P2C least-request** (or EWMA) for request balancing, not round robin, unless requests are uniform.
4. **Use both active and passive health checks**, keep the health endpoint local, and cap ejection at a fraction of the fleet.
5. **Configure a panic threshold** so a widespread failure doesn't remove every backend.
6. **Drain before stopping** and **slow-start new instances**.
7. **Subset** when client and server counts are large.
8. **Shed load on servers, budget retries on clients**, and add circuit breakers for slow dependencies.
9. **Prefer local zones**, with spillover.
10. **Measure imbalance**: compare the hottest backend's utilization to the average. That gap is the capacity you're paying for and not using.

## Key takeaways

- Balance the resource that runs out, not request counts. Imbalance means you have to provision for the hottest server.
- Requests pass through several layers (DNS or anycast, L4, L7, client-side), each with a different granularity and failure scope.
- L4 balances connections. With HTTP/2 and gRPC, that can pin all of a client's traffic to one backend.
- Least-loaded-of-all fails with many balancers and stale data. The power of two random choices gets most of the benefit with no herding.
- Health checks should be local; passive outlier detection catches what probes miss; ejection limits and panic thresholds stop health logic from causing outages.
- Draining, slow start, subsetting, retry budgets and load shedding matter as much as the selection algorithm.

## Discussion questions

1. Why does picking the least loaded of *two* random servers do so much better than picking one at random, and why doesn't picking the best of *all* servers do better still when there are many balancers?
2. Your gRPC service scaled from 10 to 20 pods, but the new pods get almost no traffic. What's happening, and what are three ways to fix it?
3. A health endpoint checks the database. The database has a 30-second blip. Walk through what happens with and without a panic threshold.
4. Envoy caps outlier ejection at 10% of hosts by default. What failure would happen without that cap?
5. Google feeds backend-reported utilization into client weights. What could go wrong if a buggy backend reports 0% utilization?
6. When would you choose consistent hashing over P2C for a service, and how would you handle a hot key?
7. How would you measure load imbalance in your current services, and what number would make you change the algorithm?

## Sources

- Michael Mitzenmacher, "The Power of Two Choices in Randomized Load Balancing," IEEE Transactions on Parallel and Distributed Systems, 2001 (based on his 1996 PhD thesis); Azar, Broder, Karlin and Upfal, "Balanced Allocations," STOC 1994.
- Betsy Beyer et al. (eds.), *Site Reliability Engineering* (O'Reilly, 2016), chapter 19 "Load Balancing at the Frontend," chapter 20 "Load Balancing in the Datacenter," chapter 21 "Handling Overload," https://sre.google/sre-book/load-balancing-datacenter/
- Daniel E. Eisenbud et al., "Maglev: A Fast and Reliable Software Network Load Balancer," NSDI 2016.
- Envoy documentation: "Load balancing," "Outlier detection," "Health checking," "Circuit breaking," https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/load_balancing/overview and https://envoyproxy.io/docs/envoy/latest/api-v3/config/cluster/v3/outlier_detection.proto
- Netflix Technology Blog, "Rethinking Netflix's Edge Load Balancing" (2018), and the Zuul wiki, https://github.com/Netflix/zuul/wiki
- Twitter Finagle documentation, "Load Balancing" (P2C, EWMA, aperture), https://twitter.github.io/finagle/guide/Clients.html
- Facebook Engineering, "Open-sourcing Katran, a scalable network load balancer" (2018); GitHub Engineering, "GLB: GitHub's open source load balancer" (2018); Cloudflare blog, "Unimog: Cloudflare's edge load balancer" (2020).
- gRPC documentation, "Load Balancing" and server keepalive options (`MAX_CONNECTION_AGE`), https://grpc.io/blog/grpc-load-balancing/
- Kubernetes documentation, "Pod Lifecycle: termination of Pods" and readiness probes.

The Envoy outlier detection settings come from Envoy's proto documentation, located on Oct 30, 2026. The Netflix, Twitter and Google descriptions summarize those organizations' own write-ups as I know them, without exact figures. Check the posts for numbers.
