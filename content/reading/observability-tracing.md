---
title: "Observability: metrics, logs, traces and SLOs that actually help"
date: 2026-10-31
summary: What observability means beyond dashboards, how metrics, logs and traces each answer different questions, how distributed tracing works from Dapper to OpenTelemetry and W3C Trace Context, sampling and cardinality trade-offs, and alerting on SLO burn rates.
tags: observability, distributed systems
daily: true
---

At 2 a.m. checkout latency doubles. The dashboards show it, but the question you actually need answered is "why?", and it's usually one you didn't anticipate when you built the dashboards: only users on one app version, only orders with a gift card, only requests that hit one database replica. Observability is the ability to answer new questions like that from the telemetry your system already emits, without shipping new code first. This reading covers the three classic signals (metrics, logs and traces) and what each is good for, how distributed tracing works from Google's Dapper to OpenTelemetry, the costs that dominate real deployments (cardinality and sampling), and how to alert on what users experience using SLOs and burn rates.

## 1. Monitoring versus observability

**Monitoring** asks known questions: Is CPU above 80%? Is the error rate above 1%? Is the queue backing up? You decide in advance what to measure and what thresholds matter. It's essential and it catches known failure modes.

**Observability** is borrowed from control theory, where a system is observable if you can infer its internal state from its outputs. For software it means: when something goes wrong in a way you didn't predict, can you work out what's happening from the data you have? Charity Majors, Liz Fong-Jones and George Miranda's book *Observability Engineering* (O'Reilly, 2022) frames the difference as known-unknowns versus unknown-unknowns. Distributed systems fail in novel combinations, so the second matters more and more as systems grow.

The practical test is simple: **can you slice your telemetry by any dimension you didn't plan for?** Version, customer, region, feature flag, endpoint, database shard. If answering "is it only customer X?" requires adding a new metric and redeploying, you have monitoring but not much observability.

## 2. Three signals, three jobs

**Metrics** are numbers aggregated over time: request counts, error counts, latency histograms, queue depths, CPU. They're cheap to store, because a counter costs the same whether it counts ten requests or ten million, and fast to query, which makes them ideal for dashboards and alerts. Their weakness is that aggregation throws detail away. A metric tells you the p99 latency went up. It can't tell you which requests were slow, unless you anticipated the dimension and added it as a label.

**Logs** are records of discrete events, ideally **structured** (JSON or key-value pairs rather than free text) so they can be queried. They carry the detail metrics drop: the error message, the user ID, the SQL that failed. Their weaknesses are cost (every event is stored) and isolation: a log line from one service doesn't tell you which user request it belonged to, unless you connect them.

**Traces** record the path of a single request through the system as a tree of timed operations. They answer "where did the time go?" and "which service failed?" for a specific request, across process boundaries. Their weaknesses are instrumentation effort and volume, which is why tracing usually involves sampling.

The signals are most useful **connected**. Put the trace ID in every log line, so you can jump from a slow trace to the logs of every service it touched. Attach **exemplars** (sample trace IDs) to metric data points, so a spike on a latency graph links straight to example traces from that spike. Prometheus and OpenTelemetry both support exemplars, and Grafana can show them on graphs.

### Wide events

A growing school of thought argues for one richer signal instead of three separate ones. Honeycomb, among others, advocates **wide structured events**: one event per request per service, with dozens or hundreds of fields (user, version, endpoint, cache hit, row counts, durations of sub-steps, feature flags). Metrics can then be computed from events at query time, and traces are just events that share a trace ID and have parent links. The benefit is that you never have to decide in advance which dimensions to aggregate on. The cost is storage and a columnar query engine that can handle it. Majors has called this approach "observability 2.0." Whether or not you adopt it fully, the lesson carries over: **add context to the telemetry you already emit**. A span or log line with twenty useful fields is worth far more than five with two.

## 3. Metrics that matter

With thousands of possible metrics, a few frameworks help decide what to look at first.

**The four golden signals** (Google SRE book, chapter 6, "Monitoring Distributed Systems"): **latency**, **traffic**, **errors** and **saturation**. If you can measure only four things about a user-facing service, measure these. The book adds an important detail about latency: track the latency of successful and failed requests separately, because a fast error can make average latency look better while things are getting worse.

**RED** (Tom Wilkie, for request-driven services): **R**ate, **E**rrors, **D**uration. Essentially the golden signals minus saturation, applied per service and endpoint.

**USE** (Brendan Gregg, for resources): for every resource such as CPU, memory, disk and network, check **U**tilization, **S**aturation and **E**rrors. USE is how you find out *why* a service is slow once RED tells you *that* it is.

### Histograms, not averages

Latency should almost never be summarized as an average. Distributions are skewed: most requests are fast, a few are very slow, and those few are the ones users complain about. Use **percentiles** (p50, p95, p99, p99.9). And at scale, tail latency affects nearly everyone: Jeff Dean and Luiz André Barroso's "The Tail at Scale" (CACM, 2013) shows that when a user request fans out to many servers, even a rare slow response on one server becomes common at the request level. If a page makes 100 backend calls, each with a 1% chance of being slow, about 63% of page loads hit at least one slow call.

Two technical points trip people up:

- **You can't average percentiles.** The p99 of the whole fleet isn't the average of each server's p99. You need the underlying distribution. That's why metric systems store **histograms** (counts per latency bucket) that can be summed across servers and then turned into percentiles. Prometheus classic histograms use fixed buckets, so choose them to bracket your SLO thresholds. Newer formats (Prometheus native histograms, OpenTelemetry exponential histograms) use automatically scaled buckets with much better accuracy and no hand-tuning.
- **Summaries computed on each client** (pre-calculated quantiles) can't be aggregated at all. Prefer histograms.

### Cardinality: the cost that bites

Each unique combination of metric name and label values is a separate time series. `http_requests_total{service, endpoint, status}` with 20 endpoints and 5 status codes is 100 series per service instance. Add a `user_id` label with a million users and it becomes 100 million series, and your metrics system falls over or your bill explodes.

The rule of thumb: **metric labels must have bounded, small sets of values** (status code, endpoint template, region, version). High-cardinality context like user IDs, request IDs and order IDs belongs in logs, traces or wide events, which store per-event detail without creating a time series per value. Also watch for accidental cardinality: an endpoint label built from the raw URL (`/users/8123/orders`) instead of the route template (`/users/{id}/orders`) creates one series per user.

## 4. Distributed tracing: from Dapper to OpenTelemetry

### Dapper

Google described its tracing system, **Dapper**, in a 2010 technical report by Benjamin Sigelman and colleagues. Dapper set the model nearly every tracing system still uses:

- A **trace** is a tree of **spans**. A span is one unit of work (an RPC, a database query, a function) with a name, start and end timestamps, a span ID, its parent's span ID, and annotations.
- All spans in a trace share a **trace ID**, which is passed along with every RPC so downstream services can attach their spans to the same trace.
- Instrumentation lives mostly in shared libraries (the RPC framework, thread pools, control-flow libraries), so application developers get tracing almost for free. That was a key design goal: tracing that requires every team to instrument by hand never reaches full coverage.
- Tracing everything is too expensive, so Dapper **sampled**. The first production version traced 1 out of every 1,024 requests, and the paper reports that even at that rate there was enough data for the common analyses at Google's traffic volumes. Lower-traffic services later used adaptive sampling so they still got enough traces.

Dapper inspired Twitter's **Zipkin** (open-sourced in 2012) and Uber's **Jaeger** (2017). The industry then went through two competing standards for instrumentation APIs, OpenTracing and OpenCensus, which merged in 2019 into **OpenTelemetry**.

<!--diagram:trace-waterfall-1031-->

### OpenTelemetry

OpenTelemetry (OTel), a CNCF project, standardizes how telemetry is produced and shipped, without being a storage backend itself. It has several parts:

- **APIs and SDKs** in many languages for creating spans, metrics and logs.
- **Auto-instrumentation** for common frameworks and libraries (HTTP servers and clients, gRPC, database drivers, message queues), continuing Dapper's idea of instrumenting shared code.
- **Semantic conventions**: standard attribute names such as `http.request.method`, `http.response.status_code`, `db.system` and `server.address`, so every service and every backend agrees on what fields mean.
- **OTLP**, the wire protocol for sending telemetry.
- The **Collector**, a separately deployed pipeline that receives, processes (batching, filtering, sampling, redacting) and exports telemetry to one or more backends.

The main benefit is vendor neutrality: instrument once, then send the data to Jaeger, Grafana Tempo, Honeycomb, Datadog or anything else that accepts OTLP, and change backends without touching application code.

### Context propagation and W3C Trace Context

Tracing only works if every hop passes the trace ID along. Different systems used different headers (`X-B3-TraceId` for Zipkin, `uber-trace-id` for Jaeger, vendor-specific ones for others), so traces broke whenever a request crossed between them. The **W3C Trace Context** specification, a W3C Recommendation since February 2020, standardizes two HTTP headers:

- `traceparent`: version, a 16-byte trace ID, an 8-byte parent span ID, and flags (including whether this trace is sampled). For example, `00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01`.
- `tracestate`: optional vendor-specific data that rides along.

OpenTelemetry uses Trace Context by default. A companion W3C spec, **Baggage**, carries arbitrary key-value pairs (such as a tenant ID) along the same path, so downstream services can add them to their own telemetry. Be careful what you put in baggage: it's sent with every downstream request, including to third parties if you don't strip it.

The hard parts of propagation are the asynchronous ones: message queues, background jobs, batch processing. A consumer processing a message from Kafka has to read the trace context from the message headers. If a job processes a batch of 100 messages from 100 different requests, it can't have one parent. OpenTelemetry uses **span links** for this: the batch span links to each message's originating span instead of having a single parent.

## 5. Sampling

Tracing every request in a high-traffic system is expensive to collect, ship and store. Sampling decides which traces to keep.

**Head-based sampling** decides at the start of a request, typically at random (keep 1%), and the decision propagates in the `traceparent` sampled flag so every service keeps or drops its spans consistently. It's cheap and simple. Its weakness is that the decision is made before anything interesting happens: it keeps 1% of errors and 1% of slow requests, which are exactly the traces you want most.

**Tail-based sampling** decides after the trace is complete: keep every trace with an error, every trace slower than the SLO threshold, and a small random fraction of the rest. That's far more useful, but it requires buffering all spans of a trace in one place until the trace finishes. The OpenTelemetry Collector's tail sampling processor does this, but you need to route all spans of a trace to the same collector instance (usually by load-balancing on trace ID) and size memory for the buffer window.

Common refinements:

- **Rate-limited or per-endpoint sampling**, so a high-traffic health check doesn't use the whole budget and rare endpoints still get traced.
- **Always sample** specific requests, such as those with a debug header or from internal test accounts.
- **Record the sampling rate** with each kept trace, so you can still estimate true counts. If a trace was kept at 1-in-100, it represents 100 requests.

## 6. Logs that work with everything else

Logging advice is often treated as basic, but a few practices make logs dramatically more useful in a distributed system:

- **Structured, always.** Key-value or JSON, with consistent field names. Free-text logs can't be filtered by field and need fragile regexes.
- **Include trace and span IDs** in every log line. OpenTelemetry SDKs and many logging libraries do this automatically when a span is active.
- **Log at the right level of detail.** One rich log line (or wide event) at the end of a request, with everything known about it, is often more useful than twenty lines scattered through the code.
- **Control volume.** Debug logging in a hot path at production traffic can cost more than the service itself. Sample high-volume, low-value logs, and make verbose logging switchable at runtime for specific requests or users.
- **Never log secrets or sensitive personal data.** Tokens, passwords, card numbers and session cookies end up in logs more often than anyone admits. Redact at the source, and add a redaction step in the Collector as a second line of defense. The Oct 28 OAuth reading noted that leaked bearer tokens are just as good as the original.

## 7. SLOs: deciding what "good enough" means

Telemetry tells you what's happening. **Service level objectives** tell you whether it matters.

- A **service level indicator (SLI)** is a measurement of user experience, usually the ratio of good events to total events. For example: the fraction of checkout requests that succeed in under 500 ms.
- A **service level objective (SLO)** is a target for that SLI over a window: 99.9% of checkout requests succeed in under 500 ms over 30 days.
- The **error budget** is what the SLO allows to fail: 0.1% of requests. Over 30 days, 99.9% availability allows about 43 minutes of full downtime, or the equivalent spread over partial failures.

SLOs change how teams make decisions. When there's budget left, ship features and take risks. When the budget is spent, prioritize reliability work. The Google SRE book (chapter 4) and *The Site Reliability Workbook* (chapter 2) describe this policy in detail.

Good SLIs are measured as close to the user as possible (at the load balancer or in the client, not in the database) and focus on what users notice: availability, latency, and for data pipelines, freshness and correctness.

## 8. Alerting on burn rate

A common alerting mistake is to page on symptoms that don't matter ("CPU at 85%") or on thresholds that fire constantly ("error rate above 0.1% for 1 minute"). SLOs give a better basis: **alert when you're consuming the error budget too fast**.

**Burn rate** is how fast you're using the budget relative to the SLO's pace. A burn rate of 1 uses exactly the whole budget over the SLO window. A burn rate of 10 would exhaust a 30-day budget in 3 days.

*The Site Reliability Workbook*, chapter 5 ("Alerting on SLOs"), works through alerting strategies and recommends **multiwindow, multi-burn-rate alerts**. A typical starting configuration for a 30-day, 99.9% SLO:

| Severity | Burn rate | Long window | Short window | Budget consumed when it fires |
|---|---|---|---|---|
| Page | 14.4 | 1 hour | 5 minutes | 2% |
| Page | 6 | 6 hours | 30 minutes | 5% |
| Ticket | 1 | 3 days | 6 hours | 10% |

The **long window** makes the alert significant: a burn rate of 14.4 sustained for an hour has used 2% of the month's budget. The **short window** makes it reset quickly: the alert only fires if the burn is still happening right now, so it stops paging soon after the problem is fixed. Fast burns page someone; slow burns become tickets.

The result is fewer, more meaningful pages. Every page means users are being hurt at a rate that threatens the SLO, and every page has a clear first question: what's burning the budget? Traces and logs from the burning window answer it.

## 9. Debugging with the signals together

A typical investigation with well-connected telemetry goes like this:

1. **Alert fires** on a checkout latency SLO burn rate.
2. **Metrics** confirm p99 latency for `POST /checkout` jumped at 02:10, while traffic is flat. Error rate is normal. Saturation metrics (USE) show the payments service's database connection pool at its limit.
3. **Exemplars** on the latency graph link to example slow traces.
4. **Traces** show the time going into `payments-svc` and, within it, a database insert waiting on a connection, like the waterfall above.
5. **Slicing** the traces or wide events by attributes shows that slow requests all have `payment.method = gift_card`, a code path that started making an extra query in a deploy at 02:05.
6. **Logs** for those trace IDs show the new query and its timing.

The key step is 5: comparing slow requests against fast ones across many dimensions to find what they have in common. Some tools automate this (Honeycomb calls it BubbleUp). It only works if the telemetry carries enough attributes.

## 10. Costs and practical advice

Observability spending can grow faster than the infrastructure it watches. Some guidelines:

- **Instrument shared libraries first.** Auto-instrumentation for HTTP, gRPC, databases and queues gives broad coverage cheaply, as Dapper showed.
- **Standardize on OpenTelemetry and semantic conventions**, so data is consistent and backends are swappable.
- **Keep metric labels low-cardinality**, and put high-cardinality context in traces, logs or events.
- **Sample traces deliberately**: tail-based if you can afford the Collector setup, otherwise head-based with rate limits per endpoint, and always keep errors.
- **Use the Collector as a control point** for filtering, redaction, sampling and routing, rather than changing every service.
- **Define a few SLOs per user journey** and alert on burn rate, not on resource thresholds.
- **Review dashboards and alerts** periodically. Delete what nobody looks at and alerts nobody acts on.
- **Watch retention.** Most high-detail data loses value within days. Keep aggregates longer and raw events shorter.

## Key takeaways

- Observability means answering questions you didn't plan for. The test is whether you can slice by new dimensions without shipping code.
- Metrics are cheap aggregates for dashboards and alerts; logs carry per-event detail; traces show a request's path and timing. Connect them with trace IDs and exemplars.
- Use histograms for latency, never averages, and remember you can't average percentiles.
- Metric labels must be low-cardinality. Put user and request IDs in traces, logs or wide events.
- Dapper's model of traces made of spans, propagated IDs, library instrumentation and sampling is still the model. OpenTelemetry and W3C Trace Context standardize it.
- Head sampling is cheap; tail sampling keeps the interesting traces.
- SLOs and error budgets turn telemetry into decisions, and multiwindow burn-rate alerts make pages rare and meaningful.

## Discussion questions

1. Pick a service you run. Which question about a recent incident could you not answer from your telemetry without adding new instrumentation?
2. Your team wants to add `customer_id` as a label on request metrics so support can see per-customer error rates. What would you suggest instead, and why?
3. Head-based sampling at 1% keeps only 1% of errors. Under what conditions is that still acceptable, and when would you insist on tail sampling?
4. A batch job processes messages from many different user requests. How would you represent that in a trace, and what would you want to see when debugging one user's request?
5. Why does a burn-rate alert need both a long and a short window? What happens with only the long window? Only the short one?
6. Your SLO is 99.9% of requests under 500 ms. Where should you measure it: in the service, at the load balancer, or in the client? What does each miss?
7. Observability costs are growing faster than traffic. Which three changes from section 10 would you try first, and what would you measure to see if they worked?

## Sources

- Benjamin H. Sigelman et al., "Dapper, a Large-Scale Distributed Systems Tracing Infrastructure," Google Technical Report, 2010, https://research.google/pubs/dapper-a-large-scale-distributed-systems-tracing-infrastructure/ ; Adrian Colyer's summary, The Morning Paper, 2015, https://blog.acolyer.org/2015/10/06/dapper-a-large-scale-distributed-systems-tracing-infrastructure/
- W3C, "Trace Context" Recommendation (February 2020; revised edition November 2021), https://www.w3.org/TR/trace-context/ ; W3C Baggage specification.
- OpenTelemetry documentation: concepts, semantic conventions, sampling, Collector tail sampling processor, https://opentelemetry.io/docs/
- Betsy Beyer et al. (eds.), *Site Reliability Engineering* (O'Reilly, 2016), chapter 4 "Service Level Objectives" and chapter 6 "Monitoring Distributed Systems," https://sre.google/sre-book/
- Betsy Beyer et al. (eds.), *The Site Reliability Workbook* (O'Reilly, 2018), chapter 2 "Implementing SLOs" and chapter 5 "Alerting on SLOs," https://sre.google/workbook/alerting-on-slos/
- Charity Majors, Liz Fong-Jones and George Miranda, *Observability Engineering* (O'Reilly, 2022).
- Jeffrey Dean and Luiz André Barroso, "The Tail at Scale," Communications of the ACM, February 2013.
- Brendan Gregg, "The USE Method," https://www.brendangregg.com/usemethod.html ; Tom Wilkie, "The RED Method" (Grafana Labs blog, 2018).
- Prometheus documentation, "Histograms and summaries" and native histograms, https://prometheus.io/docs/practices/histograms/
- Jaeger and Zipkin project documentation.

The Dapper sampling rate and the W3C Trace Context dates were checked against the Google Research page and the W3C pages on Oct 31, 2026. The burn-rate table follows the Site Reliability Workbook's example configuration. The 63% figure is 1 − 0.99^100.
