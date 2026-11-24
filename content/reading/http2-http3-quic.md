---
title: "HTTP/2, HTTP/3 and QUIC: why the web moved off TCP"
date: 2026-11-24
summary: How HTTP evolved from one request per connection to multiplexed streams over a new UDP-based transport. Covers HTTP/1.1's limits, HTTP/2 framing, multiplexing, HPACK and flow control, head-of-line blocking in TCP, QUIC's design (streams, built-in TLS 1.3, connection IDs and migration, 0-RTT, user-space congestion control), HTTP/3 and QPACK, what happened to server push, the Rapid Reset attack, and the operational trade-offs of running HTTP/3 in production.
tags: networking, APIs
daily: true
---

For its first twenty years, HTTP ran on a simple model: open a TCP connection, send a request, wait for the response. Web pages grew from one document to hundreds of resources, and that model became the main bottleneck. HTTP/2 (2015) fixed the application layer by multiplexing many requests over one connection, only to run into a limit in TCP itself. So the industry, led by Google, did something unusual: it built a new transport protocol, **QUIC**, on top of UDP, folded TLS 1.3 into it, and standardised **HTTP/3** on top (QUIC as RFC 9000 in May 2021, HTTP/3 as RFC 9114 in June 2022). Today most large sites and every major browser support HTTP/3. For backend engineers this matters beyond browsers: gRPC runs on HTTP/2, load balancers and proxies must understand all three versions, and problems like the 2023 HTTP/2 Rapid Reset attack show that multiplexing changes how servers must defend themselves. This reading explains what each version changed and why, the mechanics of QUIC, and what to consider when running these protocols. It draws on the RFCs, Cloudflare's, Google's and Chrome's engineering writing, and Ilya Grigorik's *High Performance Browser Networking*. The TLS 1.3 reading covers the handshake that QUIC builds on.

## 1. HTTP/1.1 and its workarounds

HTTP/1.1 (1997, revised as RFC 9112 in 2022) is a text protocol. On a connection, the client sends a request and must receive the whole response before the next one can be answered. Keep-alive lets a connection be reused, but requests are still processed one at a time. **Pipelining** (sending several requests without waiting) was in the spec but responses still had to come back in order, so one slow response blocked the rest (**head-of-line blocking at the HTTP layer**), and buggy intermediaries meant browsers never enabled it.

Browsers worked around this by opening **about six parallel connections per host**, and sites worked around *that* with tricks: **domain sharding** (spreading assets across `static1.`, `static2.` hostnames for more connections), **spriting** images into one file, **inlining** small resources, and **concatenating** scripts. Each connection paid for its own TCP handshake, TLS handshake and slow-start ramp-up, and headers (cookies, user agents) were resent in full, uncompressed, on every request.

## 2. HTTP/2: multiplexing over one connection

HTTP/2 grew out of Google's SPDY experiment and was published as RFC 7540 in 2015 (revised as RFC 9113 in 2022). It keeps HTTP's semantics (methods, status codes, headers) but changes how they're carried.

### Binary framing and streams

Everything is sent as **frames** with a fixed header: type, length, flags and a **stream ID**. A **stream** is one request/response exchange. Frames from many streams are **interleaved** on a single TCP connection, and the receiver reassembles them by stream ID. Key frame types include `HEADERS`, `DATA`, `SETTINGS`, `WINDOW_UPDATE`, `RST_STREAM` (cancel one stream), `PING` and `GOAWAY` (graceful connection shutdown).

The results:

- **One connection per origin** carries all requests concurrently. No more six-connection limit, no sharding.
- **No HTTP-level head-of-line blocking:** a slow response doesn't block others; their frames are interleaved.
- **Cancellation is cheap:** `RST_STREAM` cancels one request without closing the connection (which, as section 7 shows, became an attack vector).

Many HTTP/1.1 workarounds become counterproductive: sharding defeats the single connection, and concatenation hurts caching granularity.

### HPACK header compression

Headers are highly repetitive across requests on the same connection. **HPACK** (RFC 7541) compresses them using a static table of common headers, a **dynamic table** of headers seen earlier on the connection (so a repeated cookie becomes a small index), and Huffman coding for literals. HPACK was designed specifically to avoid the compression side-channel attacks (CRIME) that broke earlier attempts to compress headers with general-purpose compression.

### Flow control and priorities

HTTP/2 adds **flow control** per stream and per connection: the receiver advertises how many bytes it's willing to accept, using `WINDOW_UPDATE` frames, so a fast sender can't overwhelm a slow consumer of one stream. This matters for proxies and for gRPC streaming, where a misconfigured window can cap throughput on high-latency links.

The original **priority** scheme (a dependency tree with weights) proved complex and inconsistently implemented. RFC 9218 (2022) replaced it with a much simpler **Extensible Priorities** scheme (an urgency level and an incremental flag), used by both HTTP/2 and HTTP/3.

### Server push and its removal

HTTP/2 allowed a server to **push** responses the client hadn't asked for yet, such as CSS the page would need. In practice it was hard to use well: servers pushed resources the browser already had cached, competed with more important responses, and rarely improved performance. Chrome's team reported that only about 1.25% of HTTP/2 sites used push, falling to around 0.7%, and that many HTTP/3 implementations never built it. Chrome disabled push by default in **Chrome 106** (2022). The recommended replacement is the **103 Early Hints** status code, which tells the browser early which resources to fetch, leaving the browser (which knows its cache) in control, plus `<link rel=preload>`.

## 3. TCP's head-of-line blocking

HTTP/2 solved head-of-line blocking at the HTTP layer but moved it down a layer. TCP guarantees an **ordered byte stream**. If one packet is lost, every byte after it waits in the receiver's buffer until the lost packet is retransmitted, even if those bytes belong to completely different HTTP/2 streams.

<!--diagram:hol-blocking-1124-->

On a clean network this rarely matters. On lossy networks (mobile, congested Wi-Fi), a single loss stalls every request on the connection for at least a round trip. With HTTP/1.1's six connections, a loss stalls only one of them, so on lossy links HTTP/2's single connection can perform **worse** than HTTP/1.1.

Fixing this inside TCP isn't practical. TCP is implemented in operating system kernels and inspected or modified by countless middleboxes (firewalls, NATs, load balancers), which drop or mangle packets with unfamiliar options. Changes to TCP take a decade or more to deploy widely, a problem known as **ossification**. Google's answer was to build a new transport in user space, over UDP, which middleboxes already pass.

## 4. QUIC: a new transport over UDP

QUIC started at Google around 2012, was deployed between Chrome and Google's servers, and was then redesigned and standardised by the IETF (RFC 9000 for the transport, RFC 9001 for its use of TLS, RFC 9002 for loss detection and congestion control). Its main design points:

### Streams are native to the transport

QUIC carries many independent **streams** within one connection, each with its own ordering. A lost packet only delays the streams whose data it carried; other streams continue to be delivered to the application. That eliminates transport-level head-of-line blocking between streams.

### TLS 1.3 is built in

Instead of layering TLS on top of a transport, QUIC **integrates the TLS 1.3 handshake** into its own: the TLS handshake messages travel in QUIC's CRYPTO frames, and the keys TLS produces encrypt QUIC packets. Consequences:

- **Fewer round trips.** A new connection completes the transport and cryptographic handshake in **one round trip**, versus TCP's handshake plus TLS 1.3's (two round trips before the first request). With a previous session, **0-RTT** lets the client send its first request in the very first flight, with the same replay caveats as TLS 1.3 0-RTT (only safe for idempotent requests).
- **Encryption is mandatory** and covers almost the whole packet, including most of the transport headers (packet numbers, acknowledgements). Only a few fields, such as the connection ID, remain visible. This prevents middleboxes from depending on transport internals, which is exactly what ossified TCP, but it also reduces what network operators can observe.

### Connection IDs and migration

A TCP connection is identified by its four-tuple (source IP, source port, destination IP, destination port). If any of these changes, such as a phone moving from Wi-Fi to cellular or a NAT rebinding the port, the connection dies. QUIC connections are identified by **connection IDs** chosen by each endpoint, so a connection can **migrate** to a new network path and keep going, after the endpoints validate the new path. Endpoints can switch between several connection IDs so that an observer can't link the old and new paths easily.

Connection IDs also matter for load balancers: because the four-tuple can change, a QUIC-aware load balancer routes by connection ID, typically by encoding a server identifier into the ID the server chooses (the IETF QUIC-LB draft standardises a scheme for this).

### Loss recovery and congestion control in user space

QUIC improves on TCP's loss detection: every packet gets a new, never-reused **packet number**, even for retransmitted data, which removes TCP's ambiguity about whether an ACK refers to the original or the retransmission, and ACKs carry more precise information (including ranges and ACK delay). Congestion control (commonly CUBIC or BBR) runs in the application's QUIC library rather than the kernel, so it can be updated with an app or server release rather than an OS upgrade. The flip side is that every implementation must get these hard algorithms right.

### Costs

- **CPU.** QUIC runs in user space, encrypts each packet individually, and can't use the decades of kernel and NIC optimisations for TCP (segmentation offload, kernel bypass for TCP). Early deployments used noticeably more CPU per byte than TCP+TLS. Techniques like UDP generic segmentation offload (GSO), batching system calls, and hardware offload have narrowed the gap, but it's still a real cost for high-throughput servers.
- **UDP is sometimes blocked or throttled** by enterprise firewalls and some networks. Clients therefore race or fall back to TCP: browsers discover HTTP/3 support via the `Alt-Svc` header or DNS **HTTPS records** (covered in the DNS reading) and fall back to HTTP/2 if QUIC fails.
- **Amplification protection.** Because UDP source addresses can be spoofed, a QUIC server may send at most three times as many bytes as it has received from an unverified client address, and clients pad their first packet to at least 1,200 bytes. Servers can use Retry packets to validate addresses under attack.

## 5. HTTP/3: HTTP over QUIC

HTTP/3 (RFC 9114) maps HTTP semantics onto QUIC. It looks a lot like HTTP/2 with the transport's job removed:

- Each request/response uses its own **QUIC stream**, so there are no stream IDs in HTTP/3 frames and no HTTP-level flow control; QUIC handles both.
- **QPACK** (RFC 9204) replaces HPACK. HPACK assumed in-order delivery of all headers on the connection; with independent streams, a header block could reference a dynamic table entry that hasn't arrived yet. QPACK sends dynamic table updates on dedicated streams and lets the encoder control how much it risks blocking, so header compression doesn't reintroduce head-of-line blocking.
- Control information (settings, `GOAWAY`) goes on a dedicated control stream.

HTTP semantics (RFC 9110) are shared by all versions, so applications, frameworks and caches don't need to know which version carried a request.

### Adoption

All major browsers support HTTP/3, as do large CDNs and cloud load balancers. Cloudflare's 2024 Radar Year in Review reported that about 20.5% of requests it saw used HTTP/3, with HTTP/2 at nearly half; its 2025 review said both shares grew slightly. HTTP/3 is mostly used on the edge, between clients and CDNs or load balancers. Inside data centres, where loss is rare and latency is low, TCP-based HTTP/2 (often gRPC) and HTTP/1.1 remain the norm, and many origins still speak HTTP/1.1 to their CDN.

## 6. What this means for APIs and backends

- **gRPC depends on HTTP/2.** It uses HTTP/2 streams for each call, trailers for status, and flow control for streaming. That's why gRPC needs proxies and load balancers that support HTTP/2 end to end, and why connection-level load balancing (L4) can pin all of a client's calls to one backend: one long-lived HTTP/2 connection carries everything. The load balancing reading covered the fix (L7 balancing per request, or client-side balancing). gRPC over HTTP/3 exists but isn't yet widely used.
- **Long-lived connections need explicit lifetimes.** One HTTP/2 or HTTP/3 connection can stay open for hours, so deployments and scaling events don't rebalance traffic unless servers send `GOAWAY` periodically or after a maximum connection age.
- **Connection coalescing.** Browsers reuse an HTTP/2 or HTTP/3 connection for different hostnames if they resolve to the same IP and the certificate covers both, which changes how traffic is distributed and can surprise operators with misdirected requests (the `421 Misdirected Request` status exists for this).
- **Concurrency limits move.** Instead of limiting connections, servers limit **concurrent streams per connection** (`SETTINGS_MAX_CONCURRENT_STREAMS` in HTTP/2, stream credit in QUIC). A single client connection can now generate a lot of parallel work, which leads directly to the next section.

## 7. Rapid Reset: when cancellation becomes an attack

In October 2023, Google, Cloudflare and AWS jointly disclosed **CVE-2023-44487**, the HTTP/2 **Rapid Reset** attack. An attacker opens a stream with a request and immediately cancels it with `RST_STREAM`, then repeats, as fast as possible. Cancelled streams don't count toward the concurrent-stream limit, so the client is never throttled by it, but the server still does work to parse each request, and often starts forwarding it to a backend before noticing the cancellation.

The scale was extraordinary. Google reported a peak of **398 million requests per second** in a two-minute attack. Cloudflare reported a peak of just over **201 million requests per second**, from a botnet of only about **20,000 machines**, showing how much leverage the technique gave each attacking machine.

Mitigations, now in most HTTP/2 servers and proxies:

- **Track resets per connection** and close connections (with `GOAWAY`) that cancel too many streams too quickly.
- **Count recently reset streams** against concurrency limits, or limit the rate of new streams.
- **Delay expensive work** (backend dispatch) until a request has proven it isn't immediately cancelled, where possible.

The broader lesson connects to the GraphQL reading: when a protocol lets one client ask for many units of work cheaply, rate limiting must count the work, not the connections or requests that look cheap.

## 8. Running these protocols in production

A practical checklist:

- **At the edge,** enable HTTP/3 alongside HTTP/2 and advertise it with `Alt-Svc` and DNS HTTPS records. Open UDP 443 in firewalls and security groups, and make sure DDoS protection covers UDP.
- **Measure real-user performance** by protocol, especially at the tail and on mobile networks, where HTTP/3's benefits concentrate. For users on low-loss, low-latency connections, the difference may be small.
- **Watch CPU** on HTTP/3 terminators, and use implementations that support GSO and batching.
- **Load balancers:** if you terminate QUIC yourself behind an L4 balancer, it needs to route by connection ID or rely on consistent hashing that tolerates migration.
- **Keep internal traffic simple:** HTTP/2 (or gRPC) internally, with sane `MAX_CONCURRENT_STREAMS`, flow-control windows sized for your bandwidth-delay product, and maximum connection ages.
- **Patch and configure HTTP/2 servers against abuse:** reset-rate limits, header size limits, stream limits, and idle timeouts.
- **Retire HTTP/1.1-era tricks:** domain sharding, spriting and aggressive concatenation hurt more than they help today.
- **Use 103 Early Hints and preload** instead of server push.

## Key takeaways

- **HTTP/1.1** handles one request at a time per connection, which led to six-connection limits and workarounds like sharding and concatenation.
- **HTTP/2** multiplexes many streams over one TCP connection with binary frames, **HPACK** header compression and flow control. Server push was removed from Chrome in version 106; **103 Early Hints** replaces it.
- **TCP's ordered byte stream** reintroduces head-of-line blocking: one lost packet stalls every stream, and TCP is too ossified to change.
- **QUIC** (RFC 9000, 2021) runs over UDP with native independent streams, integrated **TLS 1.3** (1-RTT, optional 0-RTT), encrypted headers, **connection IDs** that survive network changes, and user-space congestion control, at a cost in CPU and occasional UDP blocking.
- **HTTP/3** (RFC 9114, 2022) maps HTTP onto QUIC streams with **QPACK**. It's widely used at the edge (about 20% of Cloudflare's requests in 2024), while HTTP/2 dominates inside data centres.
- Multiplexing shifts limits from connections to **streams**; the **Rapid Reset** attack (CVE-2023-44487, up to 398M rps) exploited cheap stream cancellation.

## Discussion questions

1. Your API's mobile clients see poor p99 latency on cellular networks. How would you determine whether HTTP/3 would help, and what would you measure before and after?
2. Why can HTTP/2 perform worse than HTTP/1.1 on a lossy network? Which of your traffic would be affected?
3. Your gRPC service behind an L4 load balancer shows one backend at 90% CPU and others idle. Explain why, and list two fixes.
4. QUIC encrypts nearly all transport headers. What does that make harder for your network or security team, and is the trade-off worth it?
5. How does your HTTP/2 server or proxy defend against Rapid Reset today? How would you test it?
6. Should you enable HTTP/3 between your CDN and your origin servers, or only between clients and the CDN? What would change your answer?

## Sources

- RFC 9110 (HTTP Semantics), RFC 9112 (HTTP/1.1), RFC 7540 and RFC 9113 (HTTP/2), RFC 7541 (HPACK), RFC 9218 (Extensible Priorities)
- [RFC 9000](https://www.rfc-editor.org/rfc/rfc9000) (QUIC, May 2021), RFC 9001 (Using TLS to Secure QUIC), RFC 9002 (QUIC Loss Detection and Congestion Control), [RFC 9114](https://www.rfc-editor.org/rfc/rfc9114) (HTTP/3, June 2022), RFC 9204 (QPACK); IETF QUIC-LB draft
- Chrome for Developers, ["Remove HTTP/2 Server Push from Chrome"](https://developer.chrome.com/blog/removing-push) (Chrome 106, Early Hints)
- Cloudflare, ["HTTP/2 Rapid Reset: deconstructing the record-breaking attack"](https://blog.cloudflare.com/technical-breakdown-http2-rapid-reset-ddos-attack/); Google Cloud, ["Google mitigated the largest DDoS attack to date, peaking above 398 million rps"](https://cloud.google.com/blog/products/identity-security/google-cloud-mitigated-largest-ddos-attack-peaking-above-398-million-rps) (October 2023)
- Cloudflare, [Radar 2024 Year in Review](https://blog.cloudflare.com/radar-2024-year-in-review/) and [2025 Year in Review](https://blog.cloudflare.com/radar-2025-year-in-review/)
- A. Langley et al., "The QUIC Transport Protocol: Design and Internet-Scale Deployment," SIGCOMM 2017
- Ilya Grigorik, *High Performance Browser Networking* (O'Reilly), chapters on HTTP/1.x, HTTP/2 and TLS
- gRPC documentation on HTTP/2 transport, keepalive and max connection age

*Note on verification: the RFC dates, Chrome's server push removal, the Rapid Reset figures (Cloudflare's partly from a summary of the page) and Cloudflare's HTTP/3 share were read today. Details such as QUIC's 3× amplification limit and 1,200-byte minimum, HPACK/QPACK mechanics, priorities, connection coalescing and CPU-cost observations come from the RFCs and engineering literature as I know them and weren't re-fetched.*
