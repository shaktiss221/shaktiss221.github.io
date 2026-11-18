---
title: "GraphQL in production: N+1 queries, query cost, caching and federation"
date: 2026-11-18
summary: What it takes to run a GraphQL API at scale once the honeymoon is over. Covers why GraphQL exists, how execution and resolvers work, the N+1 problem and DataLoader batching, protecting the server with depth limits and cost analysis (GitHub's and Shopify's models), pagination with connections, why HTTP caching is hard and what persisted queries fix, errors and partial results, authorisation per field, schema evolution without versions, and federation across teams.
tags: APIs
daily: true
---

GraphQL lets clients ask for exactly the data they need in one request, shaped like the response they want. Facebook built it in 2012 for its mobile apps and open-sourced it in 2015, and it now powers public APIs at GitHub and Shopify and countless internal "backend for frontend" layers. The flexibility that makes it pleasant for client developers is exactly what makes it hard to operate. Any client can write a query that fans out into thousands of database calls, the same URL serves every request so HTTP caching stops working, and one schema has to evolve forever without version numbers. This reading covers the problems teams hit when running GraphQL in production and the techniques that have become standard: DataLoader batching, query cost analysis, persisted queries, cursor connections, field-level authorisation, deprecation-based evolution, and federation. It draws on the GraphQL specification and docs, GitHub's and Shopify's API documentation, the DataLoader project, and Apollo's and others' engineering writing. The earlier reading on API design covered REST and gRPC conventions; this one focuses on what's different about GraphQL.

## 1. Why GraphQL exists

Facebook's mobile apps in 2012 had a familiar REST problem. To render a news feed, a client had to call several endpoints (the feed, then each story's author, then comments, then likes) and stitch the results together, often over slow mobile networks. Endpoints returned either too much data (over-fetching) or too little (under-fetching, needing more round trips). Every new screen needed new endpoints or new parameters, and the app shipped on a slow release cycle, so old app versions with old data needs stayed in use for years.

GraphQL's answer has three parts:

- **A typed schema** describes everything the API can return: object types, their fields, and how they connect.
- **Client-specified queries** select exactly the fields needed, following relationships in one request.
- **A single endpoint** (usually `POST /graphql`) accepts all queries, and the server resolves them against the schema.

A query for the first 50 posts with each author's name looks like:

```graphql
query {
  posts(first: 50) {
    edges {
      node {
        title
        author { name }
      }
    }
  }
}
```

The response mirrors the query's shape. The client gets no unrequested fields and needs no extra round trips.

The benefits for client teams are real: fewer round trips, typed responses with generated client code, and independence from backend release schedules. The costs land on the server team, which is what the rest of this reading is about.

## 2. How execution works: resolvers

A GraphQL server executes a query by walking it field by field. Each field in the schema has a **resolver**, a function that returns that field's value given its parent object, its arguments, and a per-request context. In the query above:

1. The resolver for `Query.posts` runs once and returns a list of 50 post objects.
2. For each post, the resolver for `Post.title` runs (often trivial: read a property).
3. For each post, the resolver for `Post.author` runs and returns a user.
4. For each user, the resolver for `User.name` runs.

This design is what makes GraphQL composable. Each resolver only knows how to get its own field, so any combination of fields works without anyone writing code for that combination. It's also the root of GraphQL's main performance problem.

## 3. The N+1 problem and DataLoader

If `Post.author` is implemented the obvious way (look up the user by `post.authorId` in the database), step 3 runs 50 separate queries, one per post. Together with the query for the posts, that's 51 database queries for one GraphQL request. Nest another level (each author's latest three posts, each with comments) and it multiplies. This is the classic **N+1 problem**, and in GraphQL it's the default behaviour, not an occasional mistake, because resolvers are written independently.

<!--diagram:graphql-dataloader-1118-->

The standard fix is **DataLoader**, a small utility Facebook open-sourced alongside GraphQL (the JavaScript reference implementation, with ports in most languages). It works like this:

- A resolver doesn't fetch directly. It calls `userLoader.load(authorId)`, which returns a promise.
- DataLoader collects every key requested during the same tick of the event loop (in other languages, the same execution phase).
- It then calls a **batch function** once with all the keys, for example `SELECT * FROM users WHERE id IN (...)`, and resolves each promise with its matching result.
- It **caches** results by key for the duration of the request, so the same user requested in ten places is loaded once.

Now the query above makes two database calls: one for the posts and one for all 50 authors. Some details matter:

- **One loader instance per request.** The cache must not be shared across requests, or users would see each other's data and stale results. Create loaders in the per-request context.
- **The batch function must return results in the same order as the keys,** with a placeholder for missing ones. Getting this wrong silently assigns data to the wrong objects.
- **Batching depends on execution order.** It only batches keys requested at the same level at roughly the same time. Deeply nested queries still produce one batch per level, which is fine, but resolvers that `await` something unrelated before calling `load` can split batches.
- **DataLoader batches; it doesn't join.** For some shapes, a single SQL join or a purpose-built query is faster. Tools that compile a GraphQL query into one SQL query (Hasura, PostGraphile, Join Monster) take that approach, at the cost of coupling the API to the database schema.

A good habit is to log or count backend calls per GraphQL request in development and tests, so a new resolver that bypasses the loaders shows up immediately.

## 4. Protecting the server: depth, complexity and cost

With REST, the server decides what each endpoint does, so its cost is known. With GraphQL, the client decides, and a single request can ask for an enormous amount of work:

```graphql
query {
  users(first: 100) {
    followers(first: 100) {
      followers(first: 100) {
        followers(first: 100) { name }
      }
    }
  }
}
```

That's potentially 100 million nodes. Whether malicious or accidental, queries like this can take down a server. Defences, roughly from simplest to most precise:

- **Timeouts and resource limits** on each request, as a backstop.
- **Maximum query depth,** rejecting queries nested beyond, say, 10 levels. Simple, but depth isn't cost: a shallow query with huge page sizes can be worse than a deep one with small pages.
- **Required and capped page sizes.** Every list field must take a `first` or `last` argument with a maximum. GitHub requires `first` or `last` on every connection and caps it at 100.
- **Static cost analysis.** Before executing, compute the query's worst-case cost from the schema and the arguments, and reject it if it's too large.

### GitHub's model

GitHub's GraphQL API documents its approach clearly:

- A **node limit**: a single call can't request more than 500,000 total nodes, computed from the `first`/`last` arguments multiplied through the nesting.
- A **points-based rate limit**: 5,000 points per hour per user (higher for GitHub Apps owned by Enterprise Cloud organisations). A query's cost is roughly the number of requests needed to fulfil each connection (assuming every connection returns its maximum page), divided by 100 and rounded, with a minimum of 1.
- Clients can ask for the `rateLimit { cost remaining resetAt }` field to see what a query costs and how much budget is left.

### Shopify's model

Shopify's Admin GraphQL API uses a **calculated query cost** with a leaky bucket:

- Every field has a cost: scalars and enums cost 0, objects cost 1, connections cost in proportion to the requested page size, and mutations cost 10.
- The **requested cost** is calculated before execution and checked against the bucket; after execution, the difference between requested and **actual cost** is refunded.
- The bucket refills at a rate that depends on the plan (100 points per second on the standard plan, more on higher plans), and a single query can't cost more than 1,000 points.
- Responses include the costs and bucket state in an `extensions` field, so clients can throttle themselves.

The point of both models is to rate-limit **work, not requests**. A REST-style "1,000 requests per hour" limit makes no sense when one request can be a thousand times more expensive than another. The rate limiting reading covered token and leaky buckets; GraphQL simply uses the computed cost as the token count.

For internal APIs, where clients are trusted, many teams skip public-style cost limits and rely instead on **persisted queries** (section 7), which lets them review and approve every query that can run.

## 5. Pagination: connections

GraphQL doesn't mandate a pagination style, but the **Relay connection specification** has become the convention, used by GitHub, Shopify and most large APIs:

- A list field returns a **connection** with `edges` (each with a `node` and an opaque `cursor`) and `pageInfo { hasNextPage endCursor }`.
- Clients page with `first`/`after` (forwards) or `last`/`before` (backwards).
- Cursors are opaque strings, typically an encoded position (such as a sort key and ID), which lets the server use keyset pagination rather than offsets.

The earlier API design reading explained why cursor pagination beats offset pagination for large or changing datasets; connections are that idea in GraphQL form. The extra nesting (`edges { node { ... } }`) is verbose, which is why some APIs also offer a `nodes` shortcut alongside `edges`.

## 6. Errors and partial results

GraphQL responses always have a `data` key and may have an `errors` array. Unlike REST, a single response can be **partly successful**: if the resolver for one field fails, that field becomes `null` (if the schema allows it), an error describing the path to the field is added to `errors`, and the rest of the data is returned.

Some consequences:

- **HTTP status codes don't tell the story.** GraphQL-over-HTTP servers have traditionally returned `200 OK` even when there are errors in the body (the newer GraphQL over HTTP specification refines this for the `application/graphql-response+json` media type). Monitoring that only counts 5xx responses misses most failures. Instrument resolver errors per field and operation.
- **Nullability is a design decision.** If a field is declared non-null (`String!`) and its resolver fails, the null propagates up to the nearest nullable parent, which can wipe out a much larger part of the response. Making fields nullable where failure is possible (especially fields backed by other services) keeps errors contained. Many teams make most fields nullable for this reason.
- **Expected errors belong in the schema.** "Card declined" or "username taken" aren't exceptions; they're normal outcomes. A common pattern is for mutations to return a **payload type** with the result plus a `userErrors` list (as Shopify does), or a **union** of success and error types, so clients handle them with types rather than parsing error strings.

## 7. Caching: why it's hard and what helps

REST gets caching almost for free: `GET /users/42` has a URL, so browsers, CDNs and reverse proxies can cache it with standard headers (the HTTP caching reading covered this in depth). GraphQL usually sends every query as a `POST` to one URL with the query in the body, so none of that works out of the box. Teams use several layers instead.

**Client-side normalised caches.** Apollo Client and Relay store results in a normalised cache keyed by type and ID (`User:42`), so data fetched by one query can satisfy part of another, and a mutation's response updates every view that shows that object. This is why GraphQL types usually expose a globally unique `id`.

**Persisted queries.** Instead of sending the query text, the client sends a hash or ID of a query registered ahead of time, plus variables. This has several benefits:

- Requests can be **GETs with short URLs** (`/graphql?id=abc123&variables=...`), so CDNs and HTTP caches work again.
- The server **only runs known queries.** For first-party clients, this turns a free-form query language into a reviewed set of operations, which is a strong security and performance control: you can analyse every query's cost before deploying it.
- Smaller requests, which matters on mobile.

Automatic persisted queries (APQ) are a softer version where the client sends the hash first and the full query only if the server doesn't recognise it; that saves bandwidth but doesn't restrict which queries can run.

**Server-side caching.** Resolver-level caches (memoising expensive fields in Redis with a TTL), DataLoader's per-request cache, and response caches keyed by the operation and variables, with cache hints per field to compute a safe overall max-age. Whole-response caching is only practical for public, non-personalised data, because one personalised field makes the whole response private.

## 8. Authorisation per field

In REST, authorisation is often checked per endpoint. In GraphQL there are no endpoints to hang checks on: any field can be reached by many paths. A user's `email` might be reachable through `viewer.email`, `post.author.email`, `comment.author.email` and so on. If the check lives in only one of those resolvers, the others leak it.

Practices that work:

- **Put authorisation in the business logic layer, not in resolvers.** The GraphQL docs recommend this explicitly: resolvers should call into a domain layer that enforces "who can see what", so every path goes through the same check. The same layer then also protects REST and other entry points.
- **Check per object as well as per field.** Loading a `Post` by ID through `node(id:)` must check that the viewer can see that post, not just that they're logged in.
- **Be careful with introspection and error messages** in public APIs. Introspection reveals the full schema, which is fine for public APIs designed to be explored, but some teams disable it for internal ones. Error messages like "not authorised to see field X on User 42" confirm that user 42 exists.
- **Watch batching attacks.** GraphQL lets a client send many operations or aliases in one request, for example 1,000 aliased `login` mutations to brute-force passwords while appearing to be a single request to a naive rate limiter. Count operations and aliases, not HTTP requests.

## 9. Evolving the schema without versions

GraphQL's philosophy, stated in its docs, is to avoid versioning and evolve one schema continuously. Because clients request fields explicitly, **adding** fields and types never breaks anyone. Removing or changing fields does, so the process is:

1. Add the new field alongside the old one.
2. Mark the old field with the `@deprecated(reason: "...")` directive, which shows up in tooling and generated clients.
3. **Measure usage.** Because every query names its fields, the server can record exactly which clients still use a deprecated field. This field-level usage data is one of GraphQL's biggest operational advantages over REST.
4. Remove the field once usage reaches zero (or after a published deprecation period for public APIs).

Some changes are breaking in subtle ways: making a nullable argument non-null, making a non-null field nullable (clients that assumed it was always present may crash), adding a value to an enum that clients switch on exhaustively, or changing a field's type. Schema linting and **breaking-change detection in CI** (comparing the new schema with the old one and with recorded client operations) catches most of these.

Public APIs sometimes still add coarse versions for big changes. Shopify, for example, releases dated API versions quarterly and supports each for a period, while GitHub evolves one schema with announced breaking-change windows. Both rely on deprecations and usage data in between.

## 10. Federation: one graph, many teams

As an organisation grows, a single GraphQL server that every team contributes to becomes a bottleneck: one codebase, one deploy, one on-call. **Federation** splits the graph across services while presenting a single schema to clients.

In Apollo Federation (and compatible implementations), each team runs a **subgraph** that owns part of the schema. Types can be extended across subgraphs: the users service defines `User` with `@key(fields: "id")`, and the reviews service adds a `reviews` field to `User`. A **router** (or gateway) composes the subgraph schemas into one **supergraph**, plans each incoming query (which subgraphs to call, in what order, and how to pass entity keys between them), executes the plan, and merges results.

Federation solves the organisational problem, but it moves the distributed systems problems into the router:

- **Query planning and fan-out.** One client query may call several subgraphs in sequence, each adding latency. The `_entities` calls between subgraphs are effectively the N+1 problem at the service level, which the router batches by entity type.
- **Schema composition as a deploy gate.** A subgraph change that conflicts with another subgraph must be caught before deployment. Teams run composition checks in CI against a schema registry.
- **Failure isolation.** If the reviews subgraph is down, queries that only touch users should still work, and queries that touch both should return partial data with nullable fields, which ties back to the nullability point in section 6.
- **Ownership clarity.** Every type and field needs a clear owner, or the "one graph" becomes a shared space nobody maintains.

Alternatives include **schema stitching** (an older, more manual approach) and simply keeping a single **backend-for-frontend** GraphQL layer per client application that calls REST or gRPC services underneath. For many companies, the BFF approach is the simplest: GraphQL serves the client teams, and service-to-service communication stays on gRPC or REST.

## 11. When GraphQL is (and isn't) the right choice

GraphQL fits well when:

- **Many client types with different data needs** (web, iOS, Android, partners) use the same data, and client teams want to move without backend changes.
- **The data is a graph** with many relationships that clients navigate in different ways.
- **You can invest in the platform:** DataLoader discipline, cost analysis, persisted queries, schema governance and observability.

It fits less well when:

- **The API is mostly simple resource CRUD or file transfer,** where REST's simplicity and HTTP caching are hard to beat.
- **Service-to-service calls** between backend services, where gRPC's performance, streaming and strict contracts are usually better.
- **Heavily cacheable public data** served through CDNs, unless you commit to persisted GET queries.
- **A small team** where the operational overhead outweighs the flexibility.

## Key takeaways

- GraphQL trades server-side predictability for client flexibility: a **typed schema**, **client-chosen fields**, and **one endpoint**.
- Execution runs a **resolver per field**, which makes **N+1 queries** the default. **DataLoader** batches and de-duplicates loads per request; create one per request and keep result order correct.
- Protect the server by limiting **work, not requests**: required page sizes, depth limits, and **static cost analysis** like GitHub's (5,000 points per hour, 500,000-node limit) and Shopify's (calculated cost and a leaky bucket, 1,000-point query cap).
- **Partial results** and `200 OK` responses with errors mean monitoring must look at per-field errors. Choose **nullability** deliberately; model expected errors in the schema.
- **Persisted queries** restore HTTP caching via GET and let you allow only known queries. Client-side **normalised caches** rely on stable global IDs.
- Enforce **authorisation in the domain layer** so every path to a field is checked; count aliases and operations for rate limiting.
- Evolve with **additive changes, deprecations and field-level usage data** instead of versions. **Federation** scales the graph across teams but moves fan-out and failure isolation into the router.

## Discussion questions

1. A product page query takes 900 ms. Tracing shows 400 database queries. How would you find which resolvers are responsible, and what would you change first?
2. Design a cost model for your own API. Which fields are expensive, and how would you estimate a query's cost before running it?
3. Your mobile app uses GraphQL for a mostly public product catalogue. How would you get CDN caching to work, and what would you do about the few personalised fields?
4. A field is reachable through five different paths in your schema. Where should the authorisation check live, and how would you test that every path enforces it?
5. You want to remove a deprecated field from a public API. What data would you collect first, and what would you do about the last few clients still using it?
6. Would you choose federation or a single BFF GraphQL layer over gRPC services for an organisation of 10 teams? What would change your mind?

## Sources

- GraphQL specification and [GraphQL Foundation](https://graphql.org/foundation/) (created in 2012, open-sourced in 2015, foundation formed in 2019 under the Linux Foundation); graphql.org Learn pages on execution, pagination, authorisation, schema design and best practices; GraphQL over HTTP specification draft
- DataLoader (github.com/graphql/dataloader) README
- GitHub Docs, ["Rate limits and query limits for the GraphQL API"](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)
- Shopify Dev Docs, ["GraphQL Admin API rate limits"](https://shopify.dev/docs/apps/build/apis/graphql-admin/rate-limits) and API versioning docs
- Relay Cursor Connections Specification
- Apollo documentation: Federation (subgraphs, `@key`, router and query planning), persisted queries and automatic persisted queries, Apollo Client normalised cache
- Marc-André Giroux, *Production Ready GraphQL* (2020)
- OWASP GraphQL Cheat Sheet (batching attacks, introspection, depth and cost limits)
- Lee Byron, "GraphQL: A data query language" (Facebook engineering blog, 2015)

*Note on verification: the GitHub and Shopify limits and the GraphQL history were read from the linked pages today. DataLoader behaviour, Relay connections, Apollo Federation details, the GraphQL over HTTP status-code guidance and the OWASP recommendations come from those docs as I know them and weren't re-fetched; check current versions before relying on specifics.*
