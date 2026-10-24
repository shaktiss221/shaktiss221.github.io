---
title: gRPC and Protobuf in production
date: 2026-10-25
summary: Why field numbers and HTTP/2 streams explain Protobuf's schema rules, gRPC streaming and its load-balancing quirks.
tags: networking, APIs
---

Protobuf identifies fields by number instead of by name, and gRPC carries those bytes over HTTP/2 streams. Those two facts explain almost everything about the pair: why messages are small, why the schema rules are what they are, why streaming is cheap, and why load balancing needs special care. Reading time is about 30 minutes.

## 1. The Protobuf wire format

### A message is a list of (field number, value) pairs

Take this schema:

```protobuf
syntax = "proto3";

message User {
  int32  id    = 1;
  string name  = 2;
  repeated int32 roles = 3;
}
```

The names `id`, `name` and `roles` never appear on the wire. Only the numbers 1, 2 and 3 do. A serialized message is a flat sequence of **records**, each made of a **tag** followed by a value. The tag packs the field number and a 3-bit **wire type** into one varint:

$$\text{tag} = (\text{field\_number} \ll 3) \;|\; \text{wire\_type}$$

There are only a handful of wire types, because the wire only needs to know how to *skip* a field, not what it means:

| Wire type | ID | Used for |
| --- | --- | --- |
| VARINT | 0 | int32, int64, uint32, uint64, sint32, sint64, bool, enum |
| I64 | 1 | fixed64, sfixed64, double |
| LEN | 2 | string, bytes, nested messages, packed repeated fields |
| I32 | 5 | fixed32, sfixed32, float |

(Types 3 and 4 were start-group and end-group, deprecated long ago.)

### Varints

Integers are encoded as **base-128 varints**: 7 bits of payload per byte, with the high bit set on every byte except the last, least significant group first. Small numbers take one byte; a full 64-bit value takes up to ten.

Worked example: `User { id: 150 }`.

- Tag: field 1, wire type 0 → `(1 << 3) | 0 = 8` → byte `08`.
- Value 150 in binary is `1001 0110`. Split into 7-bit groups from the low end: `0010110` and `0000001`. The first group gets the continuation bit: `1001 0110` = `96`. The last doesn't: `0000 0001` = `01`.
- Result: `08 96 01`. Three bytes. The JSON `{"id":150}` is ten.

Adding `name: "ann"` appends tag `(2 << 3) | 2 = 18` = `12`, a length `03`, then the UTF-8 bytes `61 6e 6e`. Six more bytes.

### Consequences worth remembering

- **Field numbers 1 to 15 fit in a one-byte tag**; 16 to 2047 need two bytes. Put your most frequent fields in 1 to 15.
- **Negative numbers are expensive in ****`int32`****/****`int64`****.** They are sign-extended to 64 bits, so `-1` takes ten bytes. If a field is often negative, use `sint32`/`sint64`, which apply **ZigZag encoding** (0 → 0, -1 → 1, 1 → 2, -2 → 3) so small magnitudes stay small.
- **Fixed types win for large or random values.** A hash or a random 64-bit ID is usually above 2⁵⁶, so a varint would use 9 or 10 bytes; `fixed64` always uses 8.
- **Repeated scalars are packed** in proto3 by default: one tag, one length, then the values back to back. `roles: [1, 2, 3]` is `1a 03 01 02 03`.
- **Default values are not sent.** In proto3, a field equal to its zero value (0, empty string, false) is simply omitted. That's compact, but it means the receiver can't tell "set to 0" from "not set" unless the field has explicit presence (section 2).
- **Field order is not guaranteed and duplicates are legal.** For a scalar, the last value wins; for a nested message, duplicates are merged. This is what makes concatenating two serialized messages equivalent to merging them.
- **The wire is not self-describing.** Without the schema, a decoder sees `field 1: varint 150` and can't know whether that's an `int32`, an `enum` or a `bool`. `protoc --decode_raw` shows exactly this view and is handy for debugging.

### Unknown fields

When a parser meets a field number not in its schema, it uses the wire type to skip the right number of bytes and, in current implementations, **keeps the raw bytes** as unknown fields. If the message is re-serialized, those bytes are written back out. So an old service that reads a message, changes one field and forwards it doesn't silently strip fields added by a newer producer. (Proto3 briefly dropped unknown fields in its early releases; preservation was restored in 3.5.)

This skip-and-preserve behaviour is the mechanical basis of forward and backward compatibility, which the next section turns into rules.

### Is it actually faster than JSON?

Usually smaller (often 3 to 10 times for numeric-heavy payloads, less for text-heavy ones) and cheaper to parse, because there's no tokenizing, no quoting or escaping, and no field-name string matching. The gap narrows if JSON is compressed and the payload is mostly long strings. The bigger practical win is often not raw speed but having one schema that generates typed clients in a dozen languages.

## 2. Schema evolution: the rules that keep you compatible

In a real system, producers and consumers deploy at different times. For a while, new code reads old messages (**backward compatibility**) and old code reads new messages (**forward compatibility**). Protobuf supports both, as long as you follow rules that all come from one principle: **a field number is a permanent contract for one meaning and one wire type.**

### Safe changes

- **Add a field** with a new, never-used number. Old readers skip it as unknown (and preserve it); new readers see the zero value when reading old messages.
- **Remove a field**, provided you **reserve** its number and name so nobody reuses them:

```protobuf
message User {
  reserved 4, 7 to 9;
  reserved "email", "legacy_flags";
  int32  id   = 1;
  string name = 2;
}
```

- **Rename a field.** The name isn't on the wire, so binary compatibility is unaffected. But renames *do* break the JSON mapping (which uses names), generated code that references the old name, and any field masks stored as strings. Treat a rename as an API change for anything other than binary Protobuf.
- **Add enum values**, with care: an old reader that receives an unknown value keeps the number (proto3 enums are "open"), but its code probably has no branch for it. Always define a zero value like `ROLE_UNSPECIFIED = 0`, and write code that handles unknown values.
- **Some type changes** where the wire type and encoding match: `int32`, `uint32`, `int64`, `uint64` and `bool` are all varints and interconvert, with truncation if values don't fit. `string` and `bytes` are compatible if the bytes are valid UTF-8. An embedded message and `bytes` holding its serialized form are compatible.
- **Move a single field into a new ****`oneof`** is safe; moving several existing fields into one is not, because old writers could set more than one.

### Breaking changes

| Change | Why it breaks |
| --- | --- |
| Reusing a deleted field number | Old messages in queues, logs and caches get decoded as the new field with the wrong meaning. This has caused real data corruption. |
| Changing a field's number | Equivalent to delete plus add; existing data stops mapping to it |
| Changing wire-incompatible types (`int32` to `string`, `sint32` to `int32`) | Different encoding; values are misread or rejected |
| Changing `repeated` to singular for messages, or similar shape changes | Parsers merge or take the last value; data silently lost |
| Changing a field's default meaning ("0 now means unlimited") | Not detectable by any tool, but old and new code disagree |

The first row is the important one. Messages live far longer than code: in Kafka topics, in databases as blobs, in caches, in backups. Google's own guidance treats reusing a field number as one of the few true sins of Protobuf.

### Presence: "zero" versus "not set"

In proto2, every singular field had **explicit presence**: generated code had `has_name()`. Proto3 initially dropped it for scalars, so `age: 0` and "age not provided" were indistinguishable. That turned out to be painful, especially for partial updates ("set this field to 0" versus "don't touch it").

The fixes, in order of history:

1. **Wrapper types** such as `google.protobuf.Int32Value`. A message field always has presence, so a null wrapper means unset. Verbose but works everywhere.
2. **`optional`**** in proto3** (stable since protoc 3.15, in 2021). `optional int32 age = 5;` gives you `has_age()` again.
3. **Editions** (Edition 2023 onward), the successor to `syntax = "proto2"` and `"proto3"`. Instead of two dialects, each behaviour (field presence, enum openness, packed encoding, UTF-8 validation) becomes a **feature** with a file-wide default that can be overridden per field. Edition 2023 defaults to explicit presence.

For partial updates, the standard Google API pattern is a **`FieldMask`**: the request carries the new resource plus a list of paths to change (`update_mask: { paths: ["display_name", "address.city"] }`). Only listed fields are written, which separates "set to empty" from "leave alone" without relying on presence at all.

### Enforcing the rules: Buf

Rules that rely on people remembering them get broken. **Buf** is the common tool for enforcing them in CI:

- `buf lint` checks style and structure (naming, package layout, every enum has a zero `UNSPECIFIED` value, request and response messages are unique per RPC).
- `buf breaking --against '.git#branch=main'` compares the schema to a previous version and fails the build on changes that break wire, JSON or generated-code compatibility, at a level you choose (`FILE`, `PACKAGE`, `WIRE_JSON`, `WIRE`).
- The **Buf Schema Registry** hosts versioned modules and generates client SDKs, so consumers pull a package rather than copying `.proto` files around.

A simple, effective policy: every `.proto` change goes through `buf breaking` against `main`, deleted fields must be reserved in the same change, and any intentional break requires a new package version (`acme.user.v2`) instead of modifying `v1`.

### Versioning packages

Google's API design guide recommends putting the major version in the package (`google.pubsub.v1`) and keeping each major version compatible forever. New, incompatible versions get new packages and can be served side by side, with the server translating between them. That mirrors REST URL versioning, but the compiler and `buf breaking` enforce it.

## 3. gRPC on top of HTTP/2

gRPC is a thin convention layered on HTTP/2: each RPC is one HTTP/2 **stream**, the method name is the URL path, messages travel in DATA frames, and the result comes back in **trailers**. Google open-sourced it in 2015 as the public successor to its internal RPC system, Stubby.

### Defining a service

```protobuf
service Users {
  rpc GetUser(GetUserRequest) returns (User);                       // unary
  rpc ListUsers(ListUsersRequest) returns (stream User);            // server streaming
  rpc ImportUsers(stream User) returns (ImportSummary);             // client streaming
  rpc Chat(stream ChatMessage) returns (stream ChatMessage);        // bidirectional
}
```

`protoc` (or `buf generate`) produces a client **stub** and a server interface in each target language. Calling `client.GetUser(ctx, req)` looks like a local function call, which is both the appeal and, as section 4 explains, a trap.

### What a call looks like on the wire

<!--diagram:c4219f49-6b09-->

A unary call is five frames on a single stream: request headers and body, then response headers, body and trailers.

The pieces:

- **Request headers.** `:method POST`, `:path /acme.user.v1.Users/GetUser` (package, service, method), `content-type: application/grpc` (or `application/grpc+proto`), `te: trailers`, an optional `grpc-timeout` (the deadline, for example `2S` or `150m` for milliseconds), `grpc-encoding` for compression, and any custom **metadata** such as `authorization` or a trace ID. Binary metadata keys end in `-bin` and are base64-encoded.
- **Length-prefixed messages.** Each message in a DATA frame is preceded by 5 bytes: a 1-byte compressed flag and a 4-byte big-endian length. That framing is what lets a stream carry many messages, independent of how HTTP/2 splits DATA frames.
- **Trailers.** The final HEADERS frame carries `grpc-status` (0 means OK), an optional `grpc-message`, and optionally `grpc-status-details-bin` with a serialized `google.rpc.Status` holding rich error details. The HTTP status is 200 even for application errors, because the HTTP layer succeeded. A call that fails before any response can send a "trailers-only" response with everything in one HEADERS frame.

Trailers are why gRPC needs end-to-end HTTP/2 support. Many proxies, CDNs and browsers historically dropped or ignored HTTP trailers, which is one reason gRPC doesn't work directly from a browser (section 4).

### Why HTTP/2 matters

- **Multiplexing.** Many concurrent RPCs share one TCP connection as independent streams, so there's no connection-per-request cost and no head-of-line blocking at the HTTP layer. (TCP-level head-of-line blocking remains; one lost packet stalls every stream on that connection.)
- **Header compression (HPACK).** Repeated headers like `:path`, `content-type` and auth tokens shrink to a few bytes after the first request.
- **Flow control** per stream and per connection, using WINDOW\_UPDATE frames. A slow consumer of a streaming RPC pushes back on the sender instead of buffering without limit. Most implementations use dynamic window sizing based on the measured bandwidth-delay product.
- **Long-lived connections** with PING frames for keepalive. That's efficient, but it complicates load balancing, as section 4 explains.

### The four call types in practice

| Type | Good for | Watch out for |
| --- | --- | --- |
| Unary | Most request/response APIs | Nothing special; it's the default |
| Server streaming | Large result sets, change feeds, progress updates, LLM token streams | Consumers must handle a stream that ends with an error after partial results |
| Client streaming | Uploads, batching telemetry from a device | The server only replies at the end; plan for partial failure |
| Bidirectional | Chat, collaborative sessions, interactive protocols | Long-lived streams pin a client to one backend; reconnect logic and backpressure are your job |

One practical rule: a stream is not a durable queue. If a connection drops mid-stream, nothing is redelivered automatically. Design streaming APIs so clients can resume, for example by passing the last sequence number or a resume token on reconnect.

### Interceptors

Most gRPC libraries offer **interceptors** (middleware) on both client and server, for unary and streaming calls. They're where cross-cutting concerns live: authentication from metadata, deadline propagation, logging, metrics, OpenTelemetry tracing, retries on the client, and panic recovery on the server.

## 4. Running gRPC in production

### Deadlines, not timeouts

gRPC's guidance is blunt: **always set a deadline.** A deadline is an absolute point in time by which the whole call must finish. The client sends it as `grpc-timeout`, and servers in most languages expose it through the request context (`ctx.Deadline()` in Go, `context.time_remaining()` in Python).

The key property is **propagation**. If service A has 2 seconds left and calls B, B should pass on its *remaining* budget, minus a little, to C. When the deadline passes, every hop can stop work immediately instead of computing an answer nobody is waiting for. Without deadlines, one slow dependency leaves threads and connections hanging at every layer above it, which is how small slowdowns become cascading outages. A call without a deadline effectively has an infinite one.

Cancellation propagates the same way: if a client cancels or disconnects, the server's context is cancelled, and well-written handlers check it and stop.

### Status codes

gRPC defines 17 status codes (0 to 16). The ones you'll handle most:

| Code | Name | Meaning | Retry? |
| --- | --- | --- | --- |
| 0 | OK | Success | n/a |
| 3 | INVALID\_ARGUMENT | The request is bad regardless of system state | No |
| 4 | DEADLINE\_EXCEEDED | Ran out of time; the operation may or may not have happened | Only if idempotent |
| 5 | NOT\_FOUND | Resource doesn't exist | No |
| 7 | PERMISSION\_DENIED | Caller identified but not allowed | No |
| 8 | RESOURCE\_EXHAUSTED | Quota or rate limit hit, or a message too large | Later, with backoff |
| 9 | FAILED\_PRECONDITION | System not in the required state (e.g. directory not empty) | Not until state changes |
| 10 | ABORTED | Concurrency conflict, such as a transaction abort | Yes, at a higher level (re-read, then retry) |
| 13 | INTERNAL | A server-side invariant broke | Usually no |
| 14 | UNAVAILABLE | Transient: server down, connection reset, overloaded | Yes, with backoff |
| 16 | UNAUTHENTICATED | Missing or invalid credentials | After refreshing credentials |

Pick codes carefully on the server, because clients and proxies make retry decisions from them. Returning `INTERNAL` for a validation error, or `UNAVAILABLE` for a bug, will either hide problems or trigger useless retry storms. For richer errors, Google's `google.rpc` error model attaches typed details (`BadRequest` with field violations, `RetryInfo` with a suggested delay, `QuotaFailure`) in the trailers.

### Retries and hedging

gRPC clients support declarative retries through the **service config**, which can be set in code or delivered by the name resolver:

```json
{
  "methodConfig": [{
    "name": [{ "service": "acme.user.v1.Users" }],
    "timeout": "2s",
    "retryPolicy": {
      "maxAttempts": 4,
      "initialBackoff": "0.1s",
      "maxBackoff": "1s",
      "backoffMultiplier": 2,
      "retryableStatusCodes": ["UNAVAILABLE"]
    }
  }],
  "retryThrottling": { "maxTokens": 10, "tokenRatio": 0.1 }
}
```

Retries use jittered exponential backoff and stay within the overall deadline. **Retry throttling** is a client-side token bucket: each failure costs a token, each success earns back a fraction, and retries stop when tokens fall below half. This keeps a struggling backend from being hit with three or four times its normal load exactly when it can least handle it.

**Hedging** is the alternative policy for latency-sensitive, idempotent reads: send the request, and if no reply arrives within a short delay (say the p95 latency), send a second copy to another backend and take whichever answers first. It trades a few percent of extra load for a much better tail latency, the idea popularised by Jeff Dean and Luiz Barroso's "The Tail at Scale".

### Load balancing: the classic gRPC trap

Because gRPC multiplexes every call over one long-lived HTTP/2 connection, a traditional **L4 (TCP) load balancer** balances *connections*, not *requests*. A client opens one connection, gets routed to one backend, and sends all its traffic there for hours. Add three new pods during a traffic spike and they get almost nothing, because existing connections never move. In Kubernetes, a plain `ClusterIP` Service shows exactly this behaviour.

Three standard fixes:

1. **An L7 proxy that understands HTTP/2**, such as Envoy, Linkerd, Istio's sidecars or a cloud L7 load balancer. The proxy terminates the client's connection and balances individual streams across backends.
2. **Client-side load balancing.** The client resolves all backend addresses (for example, a Kubernetes headless Service returns every pod IP via DNS), opens a connection to each, and spreads calls with a policy like `round_robin`. Modern gRPC can also get endpoints and policies from an **xDS** control plane, the same API Envoy uses, giving "proxyless service mesh" behaviour without a sidecar.
3. **Force reconnection** with the server option `MAX_CONNECTION_AGE` (plus a grace period). Clients periodically reconnect and get rebalanced. Crude, but it works with any L4 balancer.

### Browsers, REST and gRPC-Web

Browsers don't expose HTTP/2 framing or trailers to JavaScript, so they can't speak native gRPC. Options:

- **gRPC-Web**: a variant that moves trailers into the response body, requiring a translating proxy (Envoy has a filter for it). It supports unary and server streaming only.
- **The Connect protocol** from Buf: a simpler protocol that works over HTTP/1.1 or HTTP/2, supports plain JSON or Protobuf bodies, lets you call a method with `curl`, and is served by the same handlers that serve gRPC and gRPC-Web.
- **HTTP/JSON transcoding**: annotate RPCs with `google.api.http` options (`get: "/v1/users/{id}"`), and a gateway (grpc-gateway, Envoy, or Google Cloud Endpoints) exposes a REST API backed by the gRPC service.

### When not to choose gRPC

gRPC shines for internal service-to-service calls in a polyglot fleet, streaming, and high request rates. It is a weaker fit for public APIs consumed from browsers or by third parties who expect `curl` and JSON, for APIs that benefit from HTTP caching (gRPC calls are POSTs, so CDNs won't cache them), and for teams without tooling for debugging binary traffic. If you do adopt it, enable **server reflection** and use `grpcurl` or a GUI client so engineers can still poke at services by hand. Finally, remember that a stub that looks like a local function hides a network call: every gRPC call still needs a deadline, an error-handling path, and a retry decision.

## Discussion questions

1. Encode `User { id: 300, name: "bo" }` by hand, byte by byte. Then explain why a field holding `-1` would be ten bytes as `int32` and one byte as `sint32`.
2. A teammate deletes field 4 (`email`) from a message stored in Kafka with 30-day retention, and a month later someone adds `phone = 4`. Describe exactly what consumers will see, and which tool or rule should have stopped it.
3. You need a PATCH-style `UpdateUser` RPC where clients can set `age` to 0 or leave it unchanged. Compare `optional`, wrapper types and `FieldMask`. Which would you choose, and why?
4. Your gRPC service runs on Kubernetes behind a `ClusterIP` Service, and after scaling from 3 to 10 pods, CPU stays pinned on the original 3. Explain why, and rank three fixes by effort and side effects.
5. Service A has a 1-second deadline and calls B, which calls C with its own hard-coded 5-second timeout. What goes wrong under load, and how should the code change?
6. For a new public API used by third-party developers and a web front end, would you expose gRPC, Connect, REST via transcoding, or plain REST? What would tip the decision?

## Sources

Written from the references below. Links were not re-opened while writing this piece.

- [Encoding](https://protobuf.dev/programming-guides/encoding/), Protocol Buffers documentation
- [Language Guide (proto 3)](https://protobuf.dev/programming-guides/proto3/), including "Updating a Message Type"
- [Proto Best Practices](https://protobuf.dev/best-practices/dos-donts/) and [Field Presence](https://protobuf.dev/programming-guides/field_presence/), Protocol Buffers documentation
- [Protobuf Editions overview](https://protobuf.dev/editions/overview/), Protocol Buffers documentation
- [gRPC over HTTP2](https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md), gRPC protocol specification
- [Status codes and their use in gRPC](https://grpc.io/docs/guides/status-codes/), [Deadlines](https://grpc.io/docs/guides/deadlines/) and [Retry](https://grpc.io/docs/guides/retry/), grpc.io guides
- [gRPC Load Balancing](https://grpc.io/blog/grpc-load-balancing/), grpc.io blog
- [Breaking change detection](https://buf.build/docs/breaking/), Buf documentation
- [Connect protocol reference](https://connectrpc.com/docs/protocol/), connectrpc.com
- [Google API Improvement Proposals](https://google.aip.dev/), especially AIP-134 (update methods and field masks) and AIP-193 (errors)
- Jeffrey Dean and Luiz André Barroso, "The Tail at Scale", Communications of the ACM, 2013
