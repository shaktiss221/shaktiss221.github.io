---
title: "Real-time at scale: WebSockets, fan-out and millions of open connections"
date: 2026-11-26
summary: How chat apps, live dashboards and collaborative tools push updates to millions of clients. Covers polling, long polling, Server-Sent Events and WebSockets, the cost of holding connections, gateway and pub/sub architectures, Slack's channel and gateway servers, Discord's guild processes and fan-out tricks, WhatsApp's 2 million connections per server, presence, ordering and missed messages, reconnect storms, load balancing long-lived connections, and backpressure.
tags: networking, messaging
daily: true
---

Most of the web is request and response: the client asks, the server answers, the connection goes quiet. Chat, notifications, live sports scores, trading screens, multiplayer cursors in a design tool, and agent progress updates all need the opposite: the **server** decides when to send, and it needs to reach the right users within a fraction of a second. That means keeping a connection open to every online client, often millions of them, and routing each event to exactly the connections that care. This reading covers the transport options (polling, long polling, Server-Sent Events, WebSockets), what it costs to hold connections, how systems like Slack, Discord and WhatsApp structure their real-time layers, and the hard problems that show up at scale: fan-out to huge groups, presence, catching up after disconnects, reconnect storms after deploys, and backpressure on slow clients. It draws on Slack's and Discord's engineering blogs, WhatsApp's early writing, RFC 6455, and the HTML specification for Server-Sent Events. The HTTP/2 and HTTP/3 reading and the load balancing reading are useful background.

## 1. Getting data from server to client

There are four common ways for a server to get new data to a browser or app.

**Polling.** The client asks every few seconds: "anything new?" It's simple, works through every proxy, and is stateless on the server. But most requests return nothing, latency is up to the polling interval, and at scale the empty requests dominate load. Polling is fine for slowly changing data (a build status every 30 seconds) and terrible for chat.

**Long polling.** The client asks, and the server **holds the request open** until there's something to send or a timeout (often 30 to 60 seconds) passes, then the client immediately asks again. Latency drops to near zero, and it works anywhere HTTP works, which is why early real-time web apps (and Facebook's original chat) used it. The costs: every message ends a request, so there's a gap and a new request per delivery, headers are resent each time, and ordering across reconnections must be handled with cursors.

**Server-Sent Events (SSE).** Defined in the HTML standard, SSE is a long-lived HTTP response with `Content-Type: text/event-stream`, over which the server writes a stream of text events. The browser's `EventSource` API handles parsing and **automatic reconnection**, sending a `Last-Event-ID` header so the server can resume from where the client left off. SSE is **one-way** (server to client), text-only, and works through ordinary HTTP infrastructure. Over HTTP/1.1, browsers limit connections per host (about six), which hurts when users open many tabs; over HTTP/2 and HTTP/3, many SSE streams share one connection, removing that limit. SSE has become popular again as the standard way to stream tokens from LLM APIs.

**WebSockets.** Standardised in RFC 6455 (December 2011), a WebSocket starts as an HTTP request with an `Upgrade: websocket` header; after the server agrees, the TCP connection switches to a lightweight **bidirectional, message-framed** protocol. Either side can send text or binary messages at any time, with a 2 to 14 byte frame header. WebSockets suit interactive applications where the client also sends frequent messages (typing indicators, game input, cursor positions). The trade-offs: you lose HTTP semantics on the connection (no status codes, caching or standard retries per message), some corporate proxies interfere, and you must design your own message protocol, heartbeats and resumption.

Newer options exist (WebSockets over HTTP/2 via RFC 8441, and **WebTransport** over HTTP/3, which offers multiple streams and unreliable datagrams), but WebSockets and SSE remain the workhorses.

A good default: **SSE for server-to-client streams** (notifications, feeds, LLM output), **WebSockets when the client sends a lot too**, and long polling as a fallback for hostile networks.

## 2. What holding a connection costs

A connection that's open but idle uses little CPU. Its cost is mostly memory and bookkeeping:

- **Kernel socket buffers** for each TCP connection (send and receive), which can be tuned down for mostly-idle connections.
- **TLS state** for each connection.
- **Application state**: the user's identity, subscriptions, and a send buffer.
- **File descriptors**: operating system limits must be raised well above defaults.

Event-driven servers (epoll on Linux, kqueue on BSD) let one thread handle huge numbers of mostly-idle connections. WhatsApp's early engineering blog post "1 million is so 2011" (January 2012) reported pushing a single FreeBSD server running their Erlang stack to **over 2 million simultaneous TCP connections**. Modern Go, Rust, Java (Netty), Node.js and Elixir servers comfortably hold hundreds of thousands of connections per machine.

The harder limits are elsewhere:

- **Heartbeats.** Idle connections get silently dropped by NATs, load balancers and mobile networks. Clients and servers exchange **pings** every 20 to 60 seconds so both sides detect dead connections and intermediaries see traffic. With millions of connections, heartbeats alone are a large steady load.
- **Mobile battery and radio.** Frequent pings keep a phone's radio awake. Mobile apps typically keep a socket open only while in the foreground and rely on the platform's push notification services (APNs, FCM) in the background.
- **Deploys.** Restarting a server disconnects every client on it. With long-lived connections, deployments become a major operational concern (section 7).

## 3. The basic architecture: gateways and a pub/sub backbone

Almost every large real-time system separates two concerns:

- **Gateway (edge) servers** hold the client connections. They authenticate the client, keep track of what each connection is subscribed to, and write messages to sockets. They know nothing about business logic.
- **A routing or pub/sub layer** decides which gateways need each event, so that an event is delivered to the right connections no matter which gateway they're on.

The write path is usually separate from the delivery path: clients **send** through ordinary HTTP APIs (with authentication, validation, rate limiting and persistence) and **receive** over the real-time connection. That keeps the hard guarantees (durable storage, idempotency) in the normal request path, and lets the real-time layer focus on fast delivery.

For the routing layer, options range from Redis pub/sub (simple, but at-most-once and with every subscriber receiving every message on a channel) to Kafka or NATS, to purpose-built in-memory services that own topics, which is what Slack and Discord describe.

## 4. Slack: channel servers and gateway servers

Slack's engineering blog post "Real-time Messaging" (April 2023) describes its architecture:

<!--diagram:realtime-fanout-1126-->

- **Channel servers** are stateful, in-memory servers that each own a subset of channels, assigned with **consistent hashing**. Every message for a channel goes through that channel's server. Slack runs a service it calls CHARMs (consistent hash ring managers) to manage the ring, and says a replacement channel server can be ready to serve traffic in under 20 seconds. At peak, about 16 million channels are served per host.
- **Gateway servers** are stateful, deployed in multiple regions, and hold users' WebSocket connections and their **subscriptions** to channels.
- **Admin servers** are stateless and sit between Slack's main web application and the channel servers.
- **Presence servers** track who's online, also distributed by hashing (on user ID).

When a user sends a message, the client calls Slack's web API, which persists the message and passes it to an admin server. The admin server finds the right channel server via the hash ring, and the channel server sends the message to **every gateway server around the world that's subscribed to that channel**. Each gateway then fans it out to its own connected clients. Slack describes delivering messages to tens of millions of connected clients across the world within about 500 ms.

Two design choices are worth noticing. **One owner per channel** gives a natural ordering point for each channel's events. And **sending once per gateway** rather than once per user means a message to a 10,000-person channel crosses the backbone only as many times as there are gateways with subscribers, with the final fan-out done locally.

## 5. Discord: a process per guild and the fan-out problem

Discord's 2017 post "How Discord Scaled Elixir to 5,000,000 Concurrent Users" describes a different structure built on Erlang/Elixir's lightweight processes:

- Each Discord server ("guild") is represented by a **guild process** on some Erlang node. Each connected client's WebSocket has a **session process**.
- When something happens in a guild (a message, a member joining, a presence change), the guild process sends it to every connected session in that guild.

That fan-out became the bottleneck as guilds grew to tens of thousands of concurrently connected users. Sending a message between Erlang processes on different nodes took 30 to 70 µs, so a single event in a large guild took between 900 ms and 2.1 seconds just to dispatch, blocking the guild process. Discord's fixes included:

- **Manifold**, a library that groups recipient processes by the node they run on, sends **one message per node**, and lets a process on each node do the local fan-out, partitioned across CPU cores. This is the same "send once per gateway" principle as Slack's design.
- **FastGlobal** for read-heavy shared data: replacing lookups in ETS (Erlang's in-memory table, about 7 µs per read) with a compiled-module trick costing about 0.3 µs, which cut a hash ring lookup workload from 17.5 seconds to 750 ms.
- **Semaphores** to stop stampedes of session processes all hitting a guild process at once (for example, after a node restarts and many sessions reconnect).

The post reported nearly five million concurrent users and millions of events per second at the time, with up to 500,000 live sessions per Erlang VM. The general lesson: in real-time systems the hard part usually isn't holding connections; it's **fan-out to large groups**, and the fixes are batching by destination, doing the last hop locally, and protecting hot objects from thundering herds.

## 6. Presence, ordering and missed messages

### Presence

"Who's online" sounds simple and is surprisingly expensive. Every connect, disconnect and idle change is an event, and in a naive design each one fans out to everyone who can see that user. In a 100,000-member community, one user's status change could be 100,000 deliveries. Common techniques:

- **Only send presence for people the viewer can actually see** (the visible member list, recent conversation partners), subscribing and unsubscribing as the view changes.
- **Batch and debounce** changes, and treat presence as best-effort: being a few seconds stale is fine.
- **Don't flap.** Use grace periods before marking someone offline after a disconnect, so a brief network blip doesn't generate two events.
- **Large groups** often show only counts or omit presence entirely.

### Ordering and gaps

Real-time delivery is usually **best-effort**: a client that's disconnected for ten seconds misses whatever was sent in that time. Reliable systems handle this with **sequence numbers and catch-up**:

- Each event in a stream (a channel, a document, a user's feed) gets a monotonically increasing sequence number or cursor from the owner (the channel server, guild process or database).
- The client tracks the last sequence number it processed. On reconnect it sends it (SSE's `Last-Event-ID` does exactly this), and the server either replays missed events from a buffer or tells the client to **refetch** state via the normal API if the gap is too large.
- If a client sees a **gap** in sequence numbers while connected, it fetches the missing range.

The durable copy of the data lives in the database, written through the normal API; the real-time channel is an optimisation that tells clients what changed. Designing it that way means a lost real-time message causes a short delay, not lost data.

### Delivery guarantees and deduplication

Because clients can receive the same event both over the socket and in a refetch, events should carry **IDs** so clients can deduplicate, and updates should be idempotent ("message 123 now has this content") rather than relative ("append this").

## 7. Operating long-lived connections

### Load balancing

Load balancers distribute **connections**, not messages. A WebSocket connected to a server stays there for hours. Consequences:

- **New servers don't get load** until clients reconnect. After scaling out, the old servers remain hot. Some systems periodically close a small fraction of connections (with a "please reconnect" message) to rebalance.
- **Uneven connection weight:** one connection might be an idle mobile client and another an admin viewing a 50,000-member channel. Balance on connection count *and* measured load.
- **Sticky routing** is needed if a connection's state lives on one server (as with Socket.IO's long-polling fallback), which complicates failover.

### Deploys and reconnect storms

Restarting a gateway server disconnects all its clients at once, and they all try to reconnect at the same moment, to the remaining servers and to every backend they need to rebuild their session (authentication, subscriptions, initial state). This **reconnect storm** can overload the system, and if reconnects fail and retry, it can sustain itself, a metastable failure in the resilience reading's terms. Defences:

- **Drain gradually:** stop accepting new connections on a server, then close existing ones over several minutes rather than all at once, telling clients to reconnect elsewhere.
- **Jittered exponential backoff** in clients, with server-provided hints ("reconnect in 0 to 30 seconds").
- **Cheap resumption:** let a reconnecting client resume a session with a token and a sequence number, so it receives only missed events instead of rebuilding everything. Discord's gateway protocol has an explicit "resume" operation for this.
- **Protect hot objects** (large channels, guilds) with semaphores or rate limits on resubscription, as Discord did.

### Backpressure and slow clients

A client on a poor connection can't receive messages as fast as they're produced. If the server queues messages for it without limit, memory grows until the server falls over. Every gateway needs a **bounded send buffer per connection** and a policy for when it fills:

- **Drop and resync:** discard queued messages and tell the client to refetch state.
- **Coalesce:** for state updates (cursor positions, scores), keep only the latest value per key.
- **Disconnect:** close the connection and let the client reconnect and catch up.

The same applies inside the backend: if the fan-out layer can't keep up, it needs to shed or coalesce work rather than build unbounded queues.

### Security

Authenticate during the handshake (cookies or a short-lived token), **check the `Origin` header** on WebSocket upgrades to prevent cross-site WebSocket hijacking, re-check authorisation when subscribing to each channel (not just at connect time), handle token expiry on long-lived connections, and rate-limit messages per connection, since one socket can send a lot of traffic.

## 8. Build or buy

Running a real-time layer at scale is a significant operational commitment. Managed services (Ably, Pusher, PubNub, AWS AppSync and API Gateway WebSockets, Azure Web PubSub, Cloudflare Durable Objects with WebSockets) handle connections, fan-out and reconnection for you, at a per-message or per-connection price. Building your own makes sense when real-time is central to the product, volumes make managed pricing expensive, or you need custom semantics (as Slack and Discord do). A middle path is to run gateways yourself on top of an existing pub/sub system, keeping the durable write path in your normal APIs and database.

## Key takeaways

- **Polling** is simple but wasteful; **long polling** gives low latency over plain HTTP; **SSE** is the simplest one-way stream with built-in resume; **WebSockets** (RFC 6455, 2011) give bidirectional messaging at the cost of building your own protocol.
- Holding connections is cheap per connection (WhatsApp pushed **over 2 million** on one server in 2012); heartbeats, deploys and mobile constraints are the real costs.
- Separate **gateways** (hold connections) from **routing/pub-sub** (decide where events go), and keep **writes on the normal API path**.
- **Slack**: channels hashed onto in-memory **channel servers**, which send each message once to every subscribed **gateway**, which fans out locally.
- **Discord**: one process per guild; fan-out to huge guilds was the bottleneck, fixed by **sending once per node** (Manifold), faster shared lookups, and semaphores against stampedes.
- Treat real-time delivery as **best-effort plus catch-up** with sequence numbers and refetch; deduplicate by event ID.
- Plan for **reconnect storms**, **uneven load on long-lived connections**, and **bounded per-connection buffers** for slow clients.

## Discussion questions

1. You're adding live notifications to a web app with 200,000 daily users. Would you choose SSE or WebSockets, and why? What would make you change your mind?
2. Design catch-up for a chat client that was offline for 30 seconds, and for one offline for three days. Where's the boundary between replay and refetch?
3. A channel with 50,000 members gets a burst of 100 messages per second. Estimate the fan-out work in a naive design and in a Slack-style "once per gateway" design.
4. You need to deploy a new gateway version to servers holding 2 million connections. Write the rollout plan, including how clients reconnect.
5. How would you implement presence for a workspace with 100,000 members without generating a storm of events every morning when people log on?
6. What happens to your real-time layer if one client's network is very slow? Where would memory grow, and what policy would you apply?

## Sources

- I. Fette, A. Melnikov, [RFC 6455: The WebSocket Protocol](https://www.rfc-editor.org/rfc/rfc6455) (December 2011); RFC 8441 (WebSockets over HTTP/2)
- WHATWG HTML Living Standard, "Server-sent events" (EventSource, Last-Event-ID)
- Slack Engineering, ["Real-time Messaging"](https://slack.engineering/real-time-messaging/) (April 2023)
- Discord, ["How Discord Scaled Elixir to 5,000,000 Concurrent Users"](https://discord.com/blog/how-discord-scaled-elixir-to-5-000-000-concurrent-users) (July 2017); Discord developer documentation on the Gateway (heartbeats, identify and resume)
- WhatsApp, ["1 million is so 2011"](https://blog.whatsapp.com/1-million-is-so-2011) (January 2012)
- W3C WebTransport draft and IETF WebTransport over HTTP/3 draft
- OWASP guidance on WebSocket security (Origin checks, cross-site WebSocket hijacking)
- Martin Kleppmann, *Designing Data-Intensive Applications*, ch. 11 on event streams and fan-out

*Note on verification: the Slack, Discord and WhatsApp figures and RFC 6455's date were read from the linked pages today. Details on SSE behaviour, browser connection limits, heartbeat intervals, Discord's resume operation, WebTransport and managed services come from specifications and documentation as I know them and weren't re-fetched.*
