---
title: "Why Cloudflare replaced NGINX with Pingora"
date: 2026-11-03
summary: A case study in rebuilding core infrastructure. Covers how proxies like NGINX are structured, why its process-per-core model hurt Cloudflare's connection reuse and tail latency, how Pingora's multithreaded Rust design fixed it, the results, and when a rewrite like this is worth it.
tags: case study, networking
daily: true
---

For most of its history, a large share of Cloudflare's traffic to origin servers went through NGINX, the most widely deployed web server and reverse proxy on the internet. In 2022, Cloudflare announced it had replaced that layer with **Pingora**, a proxy it wrote from scratch in Rust, serving over a trillion requests a day while using about a third of the CPU and memory. In 2024 it open-sourced Pingora as a framework. It's a rare, well-documented example of a company rebuilding a piece of infrastructure that almost everyone else treats as a given. This reading looks at how proxies like NGINX work, what specifically went wrong at Cloudflare's scale, what Pingora did differently, and what the story teaches about when a rewrite is actually justified.

## 1. What a reverse proxy does

A **reverse proxy** sits in front of servers and handles client connections on their behalf. Cloudflare's network is a giant one: a client's request arrives at a Cloudflare data center near them, Cloudflare checks its cache and applies security rules, and if it needs to, it forwards the request to the customer's **origin** server and relays the response back.

For each request, a proxy does work like this:

1. Accept the client's TCP connection and complete the TLS handshake (the Oct 21 TLS reading covered this).
2. Parse the HTTP request.
3. Run logic: routing, header changes, authentication, caching decisions, rate limits.
4. Pick an origin server and get a connection to it, ideally an existing one.
5. Forward the request, stream the response back, and log what happened.

Steps 1 and 4 are where much of the cost hides. Opening a new connection to an origin means a TCP handshake and usually a TLS handshake, which cost round trips and CPU on both sides. If the origin is on another continent, a new connection can add hundreds of milliseconds before the first byte of the request is even sent. So a proxy wants to **keep connections to origins open and reuse them** across many requests. That one goal, connection reuse, ends up at the center of the Pingora story.

## 2. How NGINX is built

NGINX, first released by Igor Sysoev in 2004, was designed to solve the "C10K problem": handling ten thousand simultaneous connections on one machine, at a time when servers like Apache typically used a process or thread per connection. NGINX's answer was an **event-driven** design:

- A **master process** reads configuration and manages workers.
- Several **worker processes**, typically one per CPU core, each run a single-threaded event loop. A worker uses `epoll` (on Linux) to wait for activity on thousands of sockets at once and handles each event without blocking.
- Each worker is independent. It accepts connections, processes their requests from start to finish, and keeps its own state, including its own **pool of upstream connections**.

This design is excellent for the problem it was built for. There's no locking between workers, a crash in one worker doesn't take down the others, and a single machine handles enormous numbers of mostly idle connections with little memory. NGINX became the default front door for a large share of the web.

Extending NGINX means writing C modules compiled into it, or using **OpenResty**, which embeds LuaJIT so logic can be written in Lua scripts that run inside the request phases. Cloudflare used both heavily.

## 3. Where it broke down for Cloudflare

Cloudflare's 2022 post "How we built Pingora, the proxy that connects Cloudflare to the Internet," lays out the problems it ran into. None of them is a bug in NGINX. They're consequences of its architecture meeting Cloudflare's particular scale and workload.

### Connection reuse was fragmented

<!--diagram:pingora-pools-1103-->

Because each NGINX worker has its own connection pool, a request handled by worker 3 can only reuse idle origin connections that worker 3 happens to hold. If worker 1 has an idle connection to the same origin, it doesn't help. As machines got more cores, Cloudflare ran more workers, and each pool got a smaller slice of traffic, so reuse got worse. Every miss meant a new TCP and TLS handshake to the origin: slower for the user, more expensive for Cloudflare, and more load on the customer's server. The post calls this the most critical problem for its use cases.

### Load was unbalanced across cores

In NGINX, the worker that accepts a connection handles every request on it, start to finish. If one request is expensive (a big response to compress, heavy Lua logic, a slow disk read for cache), it occupies that worker's event loop, and every other request on that worker waits, even if other cores are idle. Cloudflare saw unbalanced load across CPU cores and latency spikes for requests that happened to share a worker with a slow one. In an event loop, one blocking operation stalls everything on that loop.

### It was hard to extend safely

Cloudflare needed behavior NGINX didn't support well. The post gives an example: when retrying a failed request to a different origin, it wanted to change the request headers for the new attempt, which NGINX didn't allow. More generally, adding features meant either patching NGINX's C code or writing Lua. C changes risked memory-safety bugs in a component that handles untrusted input from the whole internet. Cloudflare knew this risk firsthand: in 2017 a bug in an HTML parser running inside its NGINX-based stack, nicknamed **Cloudbleed**, leaked memory contents into responses. Lua was safer but slower, and large dynamically typed codebases are hard to maintain.

### Fixing it inside NGINX wasn't practical

Cloudflare considered three options: keep investing in NGINX, adopt another proxy (Envoy was the obvious candidate), or build its own. Changing NGINX's process model would mean rewriting its core, at which point it would no longer really be NGINX. Other proxies would also need deep changes to fit Cloudflare's scale and features. Building in-house was the most expensive option up front, but the post argues it was the one with the best long-term payoff, given how central this proxy was to the business.

## 4. How Pingora is designed

Pingora's design choices map directly onto the problems above.

**Rust, for memory safety without giving up performance.** Rust gives C-like control over memory and performance, while its ownership system rules out whole classes of bugs, such as use-after-free and buffer overflows, at compile time. For a proxy that parses untrusted traffic all day, that's the most important property. The post states that in the time Pingora had been running, serving a few hundred trillion requests, it had yet to crash because of Pingora's own code. (Crashes from other causes, like hardware or kernel bugs, still happen.)

**Threads instead of processes, so connection pools can be shared.** Pingora runs as one multithreaded process. All threads share one connection pool per origin, so any idle connection can serve any request, regardless of which thread accepted the client. Sharing memory between threads needs synchronization, and Rust's type system helps make that safe.

**Async with work stealing, so one slow task doesn't stall others.** Pingora is built on **Tokio**, Rust's most widely used async runtime. Each request is a lightweight task, and Tokio's scheduler uses **work stealing**: if one thread is busy, idle threads take tasks from its queue. Load spreads across cores automatically instead of being tied to whichever worker accepted the connection.

**Its own HTTP implementation.** Cloudflare sees every kind of HTTP traffic on the internet, including plenty that doesn't follow the RFCs exactly. Rather than use an existing Rust HTTP library like hyper, Pingora has its own HTTP/1 and HTTP/2 implementation, built to tolerate odd but real-world traffic in ways Cloudflare controls. Pingora also supports HTTP/2 to origins, which made gRPC proxying possible.

**A programmable request lifecycle.** Pingora exposes the request's life as a series of phases, each with a callback (a "filter") that application code can implement: when the request headers arrive, when an upstream is chosen, when the response headers come back, when the request finishes. This is similar in spirit to NGINX's phases and OpenResty's Lua hooks, but written in typed Rust, compiled with the rest of the proxy. Features like changing headers on retry become simple code in the right callback.

**Graceful upgrades.** A proxy restart can't drop millions of in-flight connections. Pingora supports upgrading to a new binary without dropping traffic, handing listening sockets from the old process to the new one, which then takes new connections while the old one finishes its existing work.

## 5. Results

The 2022 post reported these outcomes, measured against the NGINX-based service it replaced:

- **Resources.** For the same traffic, Pingora used about 70% less CPU and 67% less memory, which the post summarizes as about a third of the resources.
- **Connection reuse.** Pingora created only a third as many new connections per second. For one major customer, the reuse ratio went from 87.1% to 99.92%, which cut new connections to their origins by 160 times.
- **Latency.** Median time to first byte improved by 5 ms and the 95th percentile by 80 ms. The post says this isn't because the code runs faster, but because reused connections skip handshakes. Tail latency benefits most, because the slowest requests were often the ones that had to open new connections to far-away origins.
- **Handshake time saved.** Cloudflare estimated the improved reuse saves its customers and users a combined 434 years of handshake time every day.
- **Scale.** Pingora was serving over a trillion requests a day.

The interesting point is *where* the gains came from. They didn't come from Rust being faster than C. They came from architecture: sharing connection pools across threads, and scheduling work across cores. A C rewrite with the same architecture could, in principle, have achieved similar efficiency. What Rust added was the confidence to write a large, concurrent, shared-memory program handling untrusted input without the memory-safety bugs that usually come with one.

## 6. Open-sourcing Pingora

In February 2024, Cloudflare open-sourced Pingora under the Apache 2.0 license, in the post "Open sourcing Pingora: our Rust framework for building programmable network services." What it released isn't a ready-made proxy server like NGINX with a configuration file. It's a **framework**: a set of Rust libraries for building your own proxies, load balancers and other network services. You write a Rust program that implements the lifecycle callbacks (for example, choosing an upstream in `upstream_peer`), and Pingora handles connections, HTTP, TLS, pooling, load balancing between upstreams and graceful restarts. The repository includes examples such as a simple load balancer across two upstream servers.

By then, the post said, Pingora had handled nearly a quadrillion requests across Cloudflare. It also noted that Pingora was being used beyond the original origin proxy, as the basis for other Cloudflare services.

The framework approach reflects how Cloudflare uses it: not as one proxy with lots of configuration, but as a toolkit for building many specialized services. It's a different philosophy from NGINX's configuration-driven model, and it suits teams that want to write their proxy logic in code.

## 7. How this compares to Envoy and others

It's worth comparing Pingora to Envoy, the other modern proxy discussed in the load-balancing reading on Oct 30.

**Envoy** (C++, from Lyft, 2016) is multithreaded, with one event loop per worker thread. Connections are assigned to a worker thread for their lifetime, and each worker keeps **its own** upstream connection pools, mostly to avoid locking. So Envoy shares some of NGINX's pool fragmentation at the thread level, though it's configurable, supports per-request load balancing well, and is mostly used in environments like service meshes where origins are nearby and new connections are cheap. Envoy is configured through a rich API (xDS) and extended with C++ filters, Lua, or WebAssembly.

**HAProxy** moved from a process model to multithreading in version 1.8 and 2.0, partly to share state and connections across threads, a similar journey to the one Cloudflare took, done inside an existing project.

**Other Rust proxies** have appeared since Pingora, and Rust has become a common choice for new network infrastructure for the same memory-safety reasons. Linkerd's data-plane proxy, for example, has been written in Rust since 2018.

The general trend is clear: shared-nothing process models were the right answer for C10K-era hardware, but on many-core machines talking to far-away origins, sharing state across cores matters again, and memory-safe languages make it practical to do safely.

## 8. When is a rewrite worth it?

Rewrites of core infrastructure have a bad reputation, deservedly. Joel Spolsky's 2000 essay "Things You Should Never Do" argued that rewriting working software from scratch throws away years of accumulated bug fixes and knowledge, and many teams have learned that the hard way. So what made Pingora a reasonable bet?

**The problem was architectural, not incidental.** Per-worker connection pools are inherent to NGINX's design. No amount of tuning or patching would give a process-per-core proxy a shared pool. When the limitation is in the foundation, incremental fixes run out.

**The scale made the payoff huge.** At over a trillion requests a day, a two-thirds cut in CPU and memory means a lot of servers, and better connection reuse helps every customer. At a smaller scale, the same rewrite would never pay for itself.

**The component was core to the business.** The proxy is Cloudflare's product, not a side system. Owning it means owning the roadmap: features like modifying requests on retry no longer depend on someone else's priorities.

**There was a strong team and a migration path.** Cloudflare had engineers who knew the old system's behavior in detail and could replace it gradually, traffic slice by traffic slice, comparing results. A rewrite without a way to migrate incrementally and verify behavior is much riskier.

**Safety was a requirement, not a preference.** After Cloudbleed, memory safety in code that parses untrusted traffic was a business risk, not just an engineering nicety.

A fair counterpoint: most organizations have none of these conditions. For them, NGINX, Envoy, HAProxy or a managed load balancer is the right choice, and the lesson to take from Pingora is not "rewrite your proxy." It's the reasoning: measure where the cost actually comes from (here, handshakes from poor reuse, not slow code), find whether the limit is architectural, and only then decide between tuning, replacing or building.

## 9. Lessons that apply everywhere

Even if you never build a proxy, Pingora's story has practical lessons:

- **Connection reuse is a performance feature.** Check that your HTTP clients, database drivers and service-to-service calls actually reuse connections. A misconfigured client that opens a new TLS connection per request can dominate latency. Metrics like new connections per second, compared with requests per second, show it immediately.
- **Per-worker or per-process state fragments.** Caches, pools and rate-limit counters held per process get less effective as you add processes. That applies to Gunicorn or Puma workers in Python and Ruby, Node.js cluster workers, and any app server with a process model.
- **Event loops hate blocking.** One CPU-heavy or blocking call stalls everything sharing its loop. Move heavy work off the loop, or use a runtime with work stealing.
- **Look at the tail.** Pingora's biggest latency improvement was at p95, not the median, because the expensive cases (new connections to far origins) were rare but slow. The Oct 31 observability reading explains why tails matter.
- **Memory safety matters most at the boundary.** Code that parses untrusted input is where memory-safe languages pay off first.

## Key takeaways

- NGINX's process-per-core, shared-nothing design was ideal for the C10K era, but it fragments connection pools and ties requests to one core's event loop.
- At Cloudflare's scale, poor connection reuse to origins meant many extra TCP and TLS handshakes, which cost latency, CPU and origin load.
- Pingora uses Rust for memory safety, threads sharing one connection pool, and Tokio's work-stealing scheduler to balance load across cores.
- Results reported: about 70% less CPU and 67% less memory, a third as many new connections, reuse for one customer from 87.1% to 99.92%, and 5 ms (median) and 80 ms (p95) faster time to first byte.
- The gains came mainly from architecture, not from Rust being faster; Rust made the shared-memory design safe to write.
- Pingora was open-sourced in 2024 under Apache 2.0 as a framework for building proxies, not a drop-in NGINX replacement.
- Rewrites pay off when the limitation is architectural, the scale is large, the component is core, and there's a safe migration path.

## Discussion questions

1. Why does adding CPU cores make connection reuse worse with NGINX's model? Would running fewer, larger workers fix it, and what would it cost?
2. Pingora's median latency improved by only 5 ms, but p95 improved by 80 ms. Explain why, using what you know about connection setup.
3. Envoy keeps per-thread connection pools, like NGINX's per-worker pools. Why might that be a sensible choice for a service mesh but not for Cloudflare's edge?
4. If your company ran into NGINX's limits, what would you measure before considering a rewrite, and what alternatives would you try first?
5. Cloudflare wrote its own HTTP parser instead of using an existing library. What are the risks and benefits of that decision?
6. Where in your own systems do you have per-process state (pools, caches, counters) that gets less effective as you scale out?
7. Pingora was released as a framework rather than a configurable server. Which would you rather operate, and why?

## Sources

- Yuchen Wu and Andrew Hauck, "How we built Pingora, the proxy that connects Cloudflare to the Internet," Cloudflare blog, September 14, 2022, https://blog.cloudflare.com/how-we-built-pingora-the-proxy-that-connects-cloudflare-to-the-internet/
- Cloudflare, "Open sourcing Pingora: our Rust framework for building programmable network services," February 28, 2024, https://blog.cloudflare.com/pingora-open-source ; repository https://github.com/cloudflare/pingora
- InfoQ, "Cloudflare Open Sources Pingora," March 2024, https://www.infoq.com/news/2024/03/cloudflare-open-sources-pingora
- Cloudflare, "Incident report on memory leak caused by Cloudflare parser bug" (Cloudbleed), February 2017.
- NGINX documentation, "Inside NGINX: How We Designed for Performance & Scale" (2015), and Dan Kegel, "The C10K problem."
- Envoy documentation, "Threading model" and "Connection pooling," https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/intro/threading_model
- Tokio documentation, "Scheduler" (work stealing), https://tokio.rs
- Joel Spolsky, "Things You Should Never Do, Part I," Joel on Software, 2000.

The Pingora figures were read from Cloudflare's two posts on Nov 3, 2026. Some of them came from a summary of the posts' later sections, not the verbatim text, so check the original if you plan to quote a number. The Envoy and HAProxy comparisons summarize their documentation as I know it.
