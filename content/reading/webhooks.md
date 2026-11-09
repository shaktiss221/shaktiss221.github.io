---
title: "Webhooks done right: delivery, signatures, retries and receivers"
date: 2026-11-09
summary: Webhooks look like a simple HTTP POST but behave like a distributed system with two owners. This covers sending from an outbox, signing with HMAC and timestamps, retries and endpoint health, idempotent receivers, ordering, thin versus fat payloads, SSRF and other security risks, and the Standard Webhooks and CloudEvents specs.
tags: APIs, security
daily: true
---

Webhooks are how most of the internet's APIs talk back. Stripe tells you a payment succeeded, GitHub tells you a pull request was opened, Twilio tells you an SMS was delivered, Shopify tells you an order was placed. The mechanism couldn't be simpler: when something happens, the provider sends an HTTP POST to a URL you registered. But that simplicity hides a distributed system with two independent owners, unreliable networks in between, and attackers who'd love to send you fake events. Most webhook bugs (lost events, double-processed payments, forged requests, retry storms) come from treating webhooks as simpler than they are. This reading covers both sides: how to build a reliable webhook sender, and how to build a receiver that's safe and correct.

## 1. Why webhooks, and why they're hard

The alternative to webhooks is **polling**: the client asks "anything new?" every few seconds. Polling is simple and robust, but wasteful (most responses are empty) and slow (latency equals the polling interval). Webhooks invert the flow: the provider pushes events as they happen, so clients get near-real-time updates without constant requests.

The cost is that the provider now makes outbound calls to thousands of endpoints it doesn't control:

- Endpoints are slow, down, misconfigured, or behind firewalls.
- Endpoints return errors for reasons that have nothing to do with the provider: a bug in the customer's handler, an expired certificate, a deploy in progress.
- Networks drop connections after the receiver processed a request but before the sender saw the response, the same ambiguity the Oct 23 exactly-once reading started with.
- Anyone on the internet can POST to a receiver's URL and pretend to be the provider.

So webhooks need the same building blocks as any distributed messaging system: durable queues, retries with backoff, idempotency, authentication and monitoring, split across two organizations that can't see each other's systems.

## 2. The sender: never lose an event

<!--diagram:webhook-pipeline-1109-->

The first rule for senders: **an event must not be lost because a webhook delivery failed, or because the sender crashed at the wrong moment.**

The classic mistake is to send the webhook inline, inside the request that caused the event:

```python
charge = db.create_charge(...)
requests.post(customer.webhook_url, json=event)   # don't
```

If the POST times out, the request either fails (and the charge creation looks failed even though it succeeded) or the event is silently lost. If the process crashes between the database write and the POST, the event is lost.

The robust pattern is the **transactional outbox** (Oct 23 reading): write the event to an `events` table in the same database transaction as the change that caused it. A separate process reads new events from the outbox and enqueues one delivery job per subscribed endpoint. Dispatcher workers process those jobs: build the request, sign it, POST it with a timeout, and record the result. Failed deliveries are retried later.

This structure gives several properties at once:

- **No lost events**: if the transaction commits, the event exists; if it rolls back, it never did.
- **Isolation between customers**: each endpoint's deliveries are separate jobs, so one slow endpoint doesn't delay everyone. Use per-endpoint concurrency limits so a single customer with a thousand events in a burst doesn't occupy every worker.
- **Short timeouts**: the dispatcher can use a firm timeout (GitHub's documentation, for example, asks receivers to respond within 10 seconds) without blocking user requests.
- **An event log**: stored events can back a "list events" API and redelivery tools (section 7).

The job queue reading from Nov 1 covers the queue mechanics: leases, retries, dead-letter queues and fairness all apply directly.

## 3. Signing: proving the request is genuine

A receiver's URL is effectively public. Without authentication, anyone who learns or guesses it can send a fake "payment succeeded" event. The standard defense is an **HMAC signature**:

1. When the customer registers an endpoint, the provider generates a random **signing secret** and shows it to the customer once.
2. For each delivery, the provider computes `HMAC-SHA256(secret, signed_content)` and sends it in a header.
3. The receiver computes the same HMAC over the raw request body with its copy of the secret and compares.

Details that matter:

**Sign the raw bytes.** The receiver must verify against the exact bytes received, before any JSON parsing and re-serialization, which can reorder keys or change whitespace. Many frameworks parse the body automatically; you have to grab the raw body first. This is the most common reason signature verification "mysteriously" fails.

**Include a timestamp in the signed content.** A signature over just the body proves authenticity but not freshness. An attacker who captures one valid request can **replay** it later. Stripe's scheme signs `timestamp.body` and sends `Stripe-Signature: t=<timestamp>,v1=<signature>`. Receivers reject requests whose timestamp is too old; Stripe's libraries default to a five-minute tolerance. Combined with deduplication by event ID (section 5), this makes replays harmless.

**Compare in constant time.** Use `hmac.compare_digest` or the equivalent, not `==`, so attackers can't learn the signature byte by byte from timing differences.

**Support secret rotation.** Customers need to rotate secrets without downtime. Providers support this by signing with both the old and new secrets for an overlap period and sending multiple signatures, and receivers accept the request if any signature matches. The Oct 26 secrets-management pack reading covered rotation patterns in general.

GitHub uses a similar scheme with the `X-Hub-Signature-256` header (HMAC-SHA256 of the body, without a timestamp), and many other providers follow the same pattern with their own header names. That inconsistency is what the Standard Webhooks spec (section 9) tries to fix.

Some providers go further and sign with **asymmetric keys** (the receiver verifies with the provider's public key, so no shared secret needs protecting), or support **mutual TLS** for high-security receivers. HMAC remains the most common because it's simple and fast.

## 4. Retries and endpoint health

Deliveries fail all the time, mostly for transient reasons, so retries are essential. The design questions:

**What counts as success?** Typically any `2xx` response within the timeout. Everything else, including timeouts, connection errors, `3xx`, `4xx` and `5xx`, is a failure. Some providers treat certain `4xx` codes (like `410 Gone`) as a signal to stop retrying.

**How long to keep trying?** Long enough to survive a customer's outage or a bad weekend deploy. Stripe documents retrying live-mode deliveries for up to three days with exponential backoff. A schedule like "immediately, then 5 minutes, 30 minutes, 2 hours, 5 hours, 10 hours, then every 12 hours" spreads attempts out while still giving quick recovery from short blips. Add jitter so retries from many events don't arrive in synchronized waves (Oct 22 reading).

**What happens to a persistently failing endpoint?** If an endpoint has failed every delivery for days, keeping on hammering it wastes resources and can look like an attack. Providers typically mark the endpoint as failing, email the account owner, and eventually **disable** it, while keeping the events so they can be replayed once the customer fixes it.

**Protect the receiver from yourself.** When a customer's endpoint comes back after an outage, the sender may have thousands of queued deliveries. Sending them all at once can knock the endpoint over again. Rate limit per endpoint, and ramp up gradually after recovery, like the slow start in the Oct 30 load balancing reading.

## 5. The receiver: verify, dedupe, acknowledge, then work

On the receiving side, a robust handler does four things in order:

1. **Verify the signature and timestamp** using the raw body. Reject anything that fails with `400` or `401`.
2. **Deduplicate by event ID.** Delivery is at-least-once: the provider will resend if it didn't see your `2xx`, even if you processed the event. Store processed event IDs (with a unique constraint) and skip duplicates. Doing the insert in the same transaction as the event's effects makes this exact.
3. **Acknowledge quickly.** Return `2xx` as soon as the event is durably recorded, ideally within a second or two. Don't do slow work, such as calling other APIs, sending emails or generating PDFs, inside the webhook request. If that work takes longer than the sender's timeout, the sender retries, and now you're processing duplicates in parallel.
4. **Do the work asynchronously**, from your own queue or database table, with your own retries.

The pattern is exactly the job queue from Nov 1, with the webhook endpoint as the producer. It makes the receiver fast, idempotent and resilient to its own downstream failures.

A frequent mistake is returning an error to the sender when your own downstream processing fails ("we couldn't update the CRM, so return 500"). That makes the provider retry, but the problem is on your side and retries from the provider won't fix it. Once you've durably stored the event, return `2xx` and handle your own retries.

## 6. Ordering and the "fetch the latest" pattern

Webhooks generally **aren't delivered in order**. Retries reorder events: `invoice.paid` might succeed on the first try while an earlier `invoice.updated` is still being retried. Parallel dispatching reorders them too. Stripe's documentation explicitly says it doesn't guarantee delivery in the order events were generated.

Receivers that apply each event's payload as the new state can go wrong: an old `subscription.updated` arriving late can overwrite a newer one. Three defenses:

- **Compare versions or timestamps.** Ignore an event if its object version (or `created` timestamp) is older than what you already have.
- **Fetch the latest state.** Treat the webhook as a notification that "something changed for object X," and call the provider's API to get X's current state. Ordering stops mattering, because you always apply the newest state. It costs an extra API call per event, but it's the most robust approach, and it's the same idea as the "read current state" advice in the Nov 1 queue reading.
- **Design for commutativity** where possible, so applying events in any order gives the same result.

### Thin versus fat payloads

The fetch-the-latest pattern connects to a design choice for providers: what to put in the payload.

- **Fat (snapshot) payloads** include the full object as of the event. Receivers can often act without another API call, but the payload may be stale by the time it's processed, it may expose sensitive data to anyone who misconfigures an endpoint, and payload shape is tied to API versions (Stripe renders webhook payloads in the endpoint's pinned API version, as the Oct 26 API design reading described).
- **Thin payloads** include only the event type and the object's ID. Receivers fetch the current object through the API. Stripe introduced "thin events" for its newer APIs in 2024 for these reasons: smaller payloads, no version coupling, and always-fresh data.

Thin payloads put more load on the provider's API and need the receiver to have API credentials, but they eliminate whole classes of ordering and staleness bugs.

## 7. Observability and replay

Webhooks fail silently from the receiver's point of view: if an event never arrives, nothing tells you. Good providers give customers visibility and control:

- **A delivery log** per endpoint, showing each attempt, response code, latency and response body excerpt.
- **Manual redelivery** of individual events and **bulk replay** for a time range, after the customer fixes a bug.
- **An events API**, so receivers can list recent events and reconcile anything they missed, which turns webhooks from the only path into a fast path backed by polling. Stripe recommends this kind of reconciliation for critical flows.
- **Test events** and a CLI or tunnel (like Stripe CLI's `listen` command) for local development.

Receivers should monitor their side too: signature failure counts (a spike might mean a rotated secret or an attack), processing lag in their own queue, and duplicate rates. For critical events such as payments, run a periodic **reconciliation job** that compares the provider's records with yours via the API, so a missed webhook gets caught within hours instead of when a customer complains.

## 8. Security beyond signatures

**SSRF on the sender.** A webhook provider lets customers enter arbitrary URLs, and its servers then make requests to them. An attacker can register `http://169.254.169.254/latest/meta-data/` (the cloud metadata service), `http://localhost:6379`, or an internal hostname, and use the provider's dispatchers to probe or attack its internal network. This **server-side request forgery** risk is the most serious security issue for webhook senders. Defenses:

- Resolve the hostname and **block private, loopback, link-local and metadata addresses**, checking the IP actually connected to (not just the one validated earlier), so DNS rebinding can't swap addresses between check and use.
- Send webhooks from **isolated egress infrastructure** that has no access to internal networks, ideally through a dedicated proxy.
- Don't follow redirects, or re-validate each redirect target.
- Require HTTPS for production endpoints.

**Data exposure.** Fat payloads can carry personal data to an endpoint the customer misconfigured or that an employee set up for testing. Let customers choose which event types they subscribe to, keep payloads minimal, and consider thin events for sensitive objects.

**Receiver endpoint hygiene.** Don't put secrets in the webhook URL as query parameters (they end up in logs). Allowlisting the provider's IP ranges can be an extra layer, but providers change ranges, so treat it as defense in depth, not a replacement for signatures.

**Verify endpoint ownership.** Some systems verify that the registering party controls the URL by sending a challenge first, which prevents customers from pointing webhooks at third parties to use the provider as a traffic source.

## 9. Standards: Standard Webhooks and CloudEvents

Every provider inventing its own headers and signing scheme makes receivers write custom verification code for each one. Two efforts address this.

**Standard Webhooks** is an open specification started in 2023 by Svix with contributors from several companies. It defines three headers:

- `webhook-id`: a unique message ID, for deduplication.
- `webhook-timestamp`: Unix seconds, for replay protection.
- `webhook-signature`: one or more signatures, such as `v1,<base64 HMAC-SHA256>`, over `id.timestamp.body`.

It also gives guidance on retries, secret formats, payload structure, and asymmetric signatures, with reference libraries in many languages. A receiver that supports the spec can verify any compliant provider with one library.

**CloudEvents** is a CNCF specification (graduated in January 2024) for describing event metadata in a common way: `id`, `source`, `type`, `specversion`, `time`, `subject` and `data`, with bindings for HTTP, Kafka, AMQP and others. It's about event *format* rather than webhook *delivery*, and it's used widely in cloud eventing systems such as Azure Event Grid, Knative and Google Eventarc. The two can be combined: a CloudEvents-formatted payload delivered with Standard Webhooks signing.

There are also managed services (Svix, Hookdeck and others) that run the sender side, the receiver side, or both, handling retries, signing, logs and replay, for teams that would rather not build this themselves.

## 10. Checklists

**Sending webhooks:**

1. Write events to an outbox in the same transaction as the change.
2. One delivery job per endpoint, with per-endpoint concurrency limits.
3. Sign with HMAC over `id.timestamp.body` (or follow Standard Webhooks), and support secret rotation.
4. Short timeouts; retry with exponential backoff and jitter for days.
5. Mark failing endpoints, notify owners, disable after a long failure period, and allow replay.
6. Block SSRF: private IPs, metadata endpoints, redirects; send from isolated egress.
7. Provide delivery logs, redelivery and an events API for reconciliation.
8. Version payloads, and consider thin events.

**Receiving webhooks:**

1. Verify signatures on the raw body, with timestamp tolerance and constant-time comparison.
2. Deduplicate by event ID with a unique constraint.
3. Store and acknowledge quickly; process asynchronously.
4. Handle out-of-order events by checking versions or fetching current state.
5. Return `2xx` once stored, even if your downstream work fails; retry on your side.
6. Monitor signature failures and processing lag; reconcile critical data periodically through the API.

## Key takeaways

- Webhooks are at-least-once, unordered messages across two organizations, so treat them like any distributed messaging system.
- Senders should use an outbox and per-endpoint delivery jobs so events are never lost and one bad endpoint doesn't hurt others.
- HMAC signatures over the raw body plus a timestamp prove authenticity and freshness; deduplication by event ID makes replays harmless.
- Retry for days with backoff and jitter, disable chronically failing endpoints, and offer replay.
- Receivers should verify, dedupe, store, acknowledge in under a few seconds, and do real work asynchronously.
- Handle ordering by version checks or by fetching the latest state; thin payloads make this the default.
- SSRF is the biggest risk for senders; isolate egress and block internal addresses.
- Standard Webhooks and CloudEvents reduce per-provider custom code.

## Discussion questions

1. Your webhook handler calls a CRM API that sometimes takes 15 seconds. The provider's timeout is 10 seconds. What happens, and how would you redesign it?
2. Why does verifying a signature after your framework has parsed and re-serialized the JSON fail, and how do you get the raw body in a framework you use?
3. A `subscription.updated` event from two hours ago arrives after a newer one. How would each of the three ordering defenses handle it?
4. If you're building a webhook sender, how would you protect against a customer registering `http://169.254.169.254/` as their endpoint, including through DNS rebinding?
5. When would you choose fat payloads over thin events, as a provider?
6. A customer's endpoint was down for 18 hours and comes back. You have 400,000 queued deliveries for it. What do you do?
7. Which webhooks does your system consume today, and would you notice if one provider's events stopped arriving for a day?

## Sources

- Stripe documentation: "Receive Stripe events in your webhook endpoint," "Manage webhook endpoints," signature verification, retry behavior and event ordering, https://docs.stripe.com/webhooks and https://docs.stripe.com/events/manage-webhook-endpoints
- Hookdeck, "Guide to Stripe Webhooks: Features and Best Practices," https://hookdeck.com/webhooks/platforms/guide-to-stripe-webhooks-features-and-best-practices
- GitHub Docs, "Validating webhook deliveries" and "Best practices for using webhooks," https://docs.github.com/en/webhooks
- Standard Webhooks specification, https://www.standardwebhooks.com and https://github.com/standard-webhooks/standard-webhooks ; Webhook Relay, "What is Standard Webhooks?", https://webhookrelay.com/glossary/standard-webhooks/
- Svix, "Webhook signature" and webhook best-practice resources, https://www.svix.com/resources/glossary/webhook-signature/
- CloudEvents specification (CNCF), https://cloudevents.io
- OWASP, "Server-Side Request Forgery Prevention Cheat Sheet," https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
- Chris Richardson, "Pattern: Transactional outbox," microservices.io.

The Standard Webhooks header names and Stripe's retry and tolerance behavior were checked against search results on Nov 9, 2026. Other provider details come from their documentation as I know it, and they change, so check the current docs before relying on a specific timeout or schedule.
