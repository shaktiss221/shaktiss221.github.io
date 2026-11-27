---
title: "Authorization at scale: from roles to Google's Zanzibar"
date: 2026-11-27
summary: How applications decide who can do what, and why that gets hard as products grow. Covers authentication vs authorization, ACLs, RBAC, ABAC and policy engines, relationship-based access control, Google's Zanzibar (tuples, userset rewrites, the new enemy problem, zookies, Leopard, caching and hedging at 10M+ QPS), open-source descendants such as OpenFGA and SpiceDB, the hard problems of list filtering and consistency, and how to choose and migrate.
tags: security, distributed systems
daily: true
---

Every request to a product answers two questions. **Authentication**: who is this? **Authorization**: are they allowed to do this, to this thing? The earlier readings on OAuth, passkeys and workload identity covered the first. The second starts as a few `if user.is_admin` checks and quietly becomes one of the most complex parts of a growing product: sharing, teams, nested folders, organisations, guest access, per-feature permissions, enterprise customers who want custom roles, and security reviews asking "who can see this document, and why?" Google faced this across Drive, Calendar, YouTube, Photos and Cloud, and built **Zanzibar**, a single global authorization service described in a 2019 USENIX ATC paper. It stores more than two trillion access control entries, serves millions of checks per second with 95th-percentile latency under 10 ms, and inspired a family of open-source systems. This reading walks through the main authorization models, how Zanzibar works and the consistency problem it was designed around, what its open-source descendants offer, and the practical problems (filtering lists, keeping data in sync, auditing) that decide whether an authorization system works in production. It draws on the Zanzibar paper, the OpenFGA and SpiceDB documentation, NIST's ABAC guidance, and the OWASP authorization guidance.

## 1. Why authorization gets hard

Authorization failures are consistently among the most serious web vulnerabilities: **Broken Access Control** has been first in the OWASP Top 10 since 2021, and **Broken Object Level Authorization** is first in the OWASP API Security Top 10. The typical bug isn't exotic: an API endpoint that loads `/invoices/123` without checking that invoice 123 belongs to the caller's organisation.

Authorization becomes hard for structural reasons:

- **Permissions depend on relationships.** "You can edit this document because you're in a team that owns the folder that contains it" involves a chain of facts stored in different places.
- **Checks are everywhere.** Every endpoint, every list view, every search result, every export and background job. One missed check is a data leak.
- **Rules change.** New features, new roles, enterprise custom roles, regulatory requirements.
- **Multiple services.** In a microservice architecture, each service needs answers about objects that other services own.
- **Lists are harder than checks.** "Can Bob see document 7?" is one question. "Show Bob every document he can see" means filtering millions of objects, which is where most authorization systems struggle.
- **Auditing.** Customers and auditors ask "who has access to this, and why?", which needs the logic to be explicit and queryable, not scattered through code.

## 2. The classic models

### Access control lists (ACLs)

Each object has a list of who can do what to it. Simple and precise, which is why file systems use them. On its own it doesn't scale to organisational structure: when someone joins a team, you'd have to add them to every object the team can access.

### Role-based access control (RBAC)

Users are assigned **roles** (viewer, editor, admin, billing manager), and roles grant **permissions**. RBAC is easy to understand and audit, and it's enough for many internal tools and B2B apps with a few fixed roles per organisation.

Its limits show up quickly: roles tend to be global or per organisation, but users want permissions per object ("editor on this project, viewer on that one"). Teams end up with **role explosion**: `project-42-editor`, `project-43-viewer`, and so on, or with object-specific exceptions hard-coded around the role system.

### Attribute-based access control (ABAC) and policy engines

ABAC (described in NIST SP 800-162) decides access by evaluating **rules over attributes** of the user, the resource, the action and the environment: "allow if user.department == resource.department and time is within business hours and the device is managed". Policy engines make this explicit:

- **Open Policy Agent (OPA)** with its Rego language, widely used for infrastructure (Kubernetes admission, API gateways) and increasingly for applications.
- **AWS Cedar**, a policy language designed for application authorization with formal analysis tooling, used in Amazon Verified Permissions.

ABAC is powerful and flexible, and policies live in one place where they can be reviewed and tested. Its weakness is that it needs the relevant attributes at decision time. For "is Bob in a team that owns the parent folder?", the engine needs the relationship data, which brings us to the next model.

### Relationship-based access control (ReBAC)

ReBAC models permissions as a **graph of relationships** between users and objects: Bob is a member of the PM group, the PM group is a viewer of the product folder, the roadmap document's parent is the product folder. A permission is a **path** through that graph, defined by rules like "viewers of a document include viewers of its parent folder". Sharing, nesting, teams, organisations and inheritance all become edges. This is the model Zanzibar popularised, and it fits collaborative products naturally.

In practice, mature systems combine models: relationships for who-is-connected-to-what, roles as named bundles of permissions, and attribute conditions ("only from the corporate network", "only before the link expires") layered on top.

## 3. Zanzibar: Google's global authorization service

Before Zanzibar, Google's products each had their own ACL implementations. Zanzibar ("Zanzibar: Google's Consistent, Global Authorization System", Pang et al., USENIX ATC 2019) provides one service for **hundreds of client services**, including Calendar, Cloud, Drive, Maps, Photos and YouTube. The paper reports more than **two trillion** access control entries, more than **10 million client queries per second** at peak across all APIs, 95th-percentile check latency **under 10 ms**, and availability **above 99.999%** over three years of production use, running on more than 10,000 servers in over 30 locations.

### The data model: relation tuples

All authorization data is stored as **relation tuples** of the form:

> `object#relation@user`

where `user` is either a user ID or a **userset**, another `object#relation`. For example:

- `doc:roadmap#owner@user:alice`: Alice owns the roadmap.
- `group:pm#member@user:bob`: Bob is a member of the PM group.
- `folder:product#viewer@group:pm#member`: members of the PM group are viewers of the product folder.
- `doc:roadmap#parent@folder:product`: the roadmap is in the product folder.

Usersets are what make groups and nesting possible without copying memberships onto every object.

### Namespace configuration: userset rewrites

Each object type (namespace) has a configuration that defines its relations and how they derive from each other, using **userset rewrite rules**:

- **`this`**: users directly listed in tuples for this relation.
- **`computed_userset`**: another relation on the same object. "Every editor is also a viewer."
- **`tuple_to_userset`**: follow a relation to another object and use one of *its* relations. "Viewers of the parent folder are viewers of this document."
- Combined with **union**, **intersection** and **exclusion**.

A document's `viewer` relation might be defined as: direct viewers ∪ editors ∪ viewers of the parent folder. That one rule makes folder sharing and inheritance work for every document, everywhere.

<!--diagram:zanzibar-check-1127-->

### The API

Zanzibar exposes a small set of operations:

- **Check**: is user U in relation R to object O? (Can Bob view the roadmap?)
- **Read**: list the stored tuples matching a filter.
- **Write**: add or remove tuples, with optimistic concurrency control on the object's tuples.
- **Expand**: return the full tree of usersets that make up a relation, useful for debugging and building indexes.
- **Watch**: stream tuple changes, so clients can maintain their own indexes or caches.

### The new enemy problem and zookies

The paper's central design concern is consistency. Authorization data and content change independently, and getting their order wrong leaks data. The paper calls this the **new enemy problem**, which can arise when updates to ACLs aren't applied in order, or when old ACLs are applied to new content. Its examples:

1. Alice removes Bob from a folder's ACL, then asks Charlie to put new documents in it. If the check for Bob's access uses a stale view that still includes him, he can see documents that were added after he was removed.
2. Alice removes Bob from a document's ACL and then adds new content to the document. A stale check would let Bob see the new content.

Zanzibar solves this with an opaque consistency token called a **zookie**. When a client changes content (for example, saves a new version of a document), it asks Zanzibar for a **content-change check** and receives a zookie encoding a global timestamp, which it stores alongside that content version. Later checks for that content pass the zookie, and Zanzibar evaluates the check at a snapshot **at least as fresh** as that timestamp. Because ACL writes are committed through Spanner with TrueTime-based timestamps (the Spanner reading covered external consistency), any ACL change made before the content change has a lower timestamp and is guaranteed to be included.

This is a clever trade-off. Requiring every check to read the very latest data would destroy cache hit rates and add latency everywhere. Zookies let Zanzibar use a slightly stale, heavily cached snapshot for most checks, while guaranteeing that a check is never older than the content it protects.

### Making it fast

Serving millions of checks per second over deeply nested groups needs serious engineering. The paper describes:

- **Distributed caching** with consistent hashing, so each server caches results for a slice of the keyspace, with cache keys that include the snapshot timestamp. Evaluation timestamps are **quantised** (rounded to 1 or 10 seconds) so that many requests share cache entries.
- **Lock tables** so that when many requests need the same uncached result, one computes it and the others wait, avoiding thundering herds on hot objects, together with prefetching for very hot groups.
- **Request hedging** for calls to Spanner and Leopard: if a request is slower than an estimated percentile, a second copy is sent to another replica and the first response wins (the resilience reading covered hedging).
- **Leopard**, an indexing system for large and deeply nested groups. Rather than recursively walking group-within-group membership at check time, Leopard maintains flattened membership sets that can be intersected directly, with an incremental layer that keeps them consistent with the latest snapshot.

## 4. Zanzibar's descendants

Several open-source and commercial systems implement the Zanzibar model:

- **OpenFGA**, accepted into the CNCF in September 2022 and promoted to incubating status in October 2025. It uses a readable modelling language (type definitions with relations like `define viewer: [user, group#member] or editor or viewer from parent`), supports conditions on relationships, and runs on Postgres, MySQL or SQLite.
- **SpiceDB** (AuthZed), which closely follows the paper, including **ZedTokens** (the zookie equivalent) and caveats for attribute-like conditions, on top of CockroachDB, Spanner, Postgres or MySQL.
- **Ory Keto**, **Permify** and others, plus managed services from several vendors.
- Commercial platforms that mix models: **AWS Verified Permissions** with Cedar (policy-based, with entity hierarchies), **Oso**, and **Cerbos** (policy-based, stateless decision points).

Some well-known companies have described building their own Zanzibar-style systems, and many more have adopted the open-source ones. Without Google's Spanner, open-source implementations usually offer a choice of consistency per request: **fully consistent** (read the latest data, slower), **at least as fresh as a token** (the zookie approach), or **minimise latency** (use whatever's cached).

## 5. The hard problems in practice

### Listing and filtering

The most common question in a real app isn't "can Bob see doc 7?" but "which of these 10,000 documents can Bob see?" for a list page, a search, or an export. Options:

- **Check each candidate** in a batch. Fine for small pages, terrible for large filtered lists, where most candidates fail.
- **Ask the authorization service which objects the user can access** (OpenFGA's ListObjects, SpiceDB's LookupResources). This walks the graph in reverse and can be expensive for users with access to huge numbers of objects.
- **Pre-filter in the database** using a denormalised index of accessible objects (or the organisation/workspace IDs the user belongs to), maintained from the authorization system's change stream (Zanzibar's Watch API), then check the final page. This is how search systems typically do it: store allowed group IDs on each document in the search index.
- **Coarse partition, fine check:** most B2B products can filter by tenant or workspace in SQL, then apply fine-grained checks to the much smaller remaining set.

Designing for list filtering early is the single biggest factor in whether an authorization system performs well.

### Dual writes and data synchronisation

With a separate authorization service, creating a document means writing the document to your database **and** writing tuples (owner, parent) to the authorization service. If one succeeds and the other fails, you get orphaned permissions or inaccessible objects. The patterns from the exactly-once reading apply: write the tuple change to an **outbox** in the same transaction as the document and deliver it reliably, make tuple writes idempotent, and reconcile periodically. Some teams keep coarse relationships (tenant membership) in their own database and only put fine-grained sharing in the authorization service, reducing the surface.

### Consistency in practice

Decide per operation how fresh the answer must be. Revoking access and then adding sensitive content is the classic case where you need a zookie-style token or a fully consistent check. Most reads can use cached results. Make the choice explicit rather than accidental.

### Latency and availability

Authorization is on the critical path of nearly every request. The service needs to be highly available and fast, usually deployed close to callers, with caching and with a clear policy for failure (almost always **fail closed** for sensitive operations, which means its availability directly limits yours).

### Explainability and auditing

A relationship model can answer "why can Bob see this?" by returning the path (Zanzibar's Expand), and "who can see this?" by expanding the relation. That's one of its biggest advantages over permission logic scattered in code. Log decisions for sensitive operations, and give administrators a way to see effective permissions.

## 6. Choosing an approach

A rough guide:

- **A few fixed roles per tenant, little sharing** (many internal tools and simple B2B apps): RBAC in your own database, enforced in a single, well-tested authorization layer in the application. Always scope queries by tenant.
- **Complex conditions on attributes** (regulated access, time, location, device posture, data classification): a policy engine such as OPA or Cedar, possibly alongside RBAC.
- **Sharing, nested resources, teams, inheritance** (documents, projects, design files, code repositories): ReBAC with a Zanzibar-style system, or a carefully designed relationship model in your own database if scale is moderate.
- **Many services needing consistent answers:** a central authorization service with a shared model, rather than each service reimplementing rules.

Regardless of model:

- **Centralise the decision logic** behind one interface (`can(user, action, resource)`), even if the first implementation is simple. Migrating later is much easier if checks don't contain the rules inline.
- **Deny by default**, and check on every request on the server side, never relying on the UI hiding a button.
- **Check object ownership** on every object-level endpoint (the OWASP BOLA problem).
- **Test authorization explicitly**: matrix tests of roles × actions × resources, and tests that each endpoint rejects unauthorised access.
- **Model the domain first**: write out the object types, relations and permission rules on paper before choosing a tool.

## 7. Migrating to a relationship-based system

A typical path for an existing product:

1. **Inventory** every place permissions are checked, and the rules each implements.
2. **Write the model** (types, relations, rewrite rules) and test it against the existing behaviour with real examples.
3. **Backfill tuples** from existing data (memberships, ownership, shares).
4. **Shadow mode**: run the new system's checks alongside the old logic, log disagreements, and fix the model or data until they match.
5. **Switch reads** endpoint by endpoint, keeping writes synchronised via an outbox.
6. **Make the new system the source of truth** for sharing data and remove the old logic.

The shadow phase almost always reveals surprising existing behaviour, some of it bugs, some of it features customers rely on.

## Key takeaways

- **Broken access control** is the top web and API risk in OWASP's lists; most bugs are missing object-level checks.
- **RBAC** is simple but leads to role explosion for per-object permissions; **ABAC**/policy engines (OPA, Cedar) evaluate rules over attributes; **ReBAC** models permissions as paths in a relationship graph.
- **Zanzibar** stores `object#relation@user` tuples and derives permissions with **userset rewrites** (`this`, `computed_userset`, `tuple_to_userset`, combined with union, intersection, exclusion). It serves 10M+ QPS over two trillion ACLs with p95 under 10 ms and 99.999% availability.
- The **new enemy problem** (applying old ACLs to new content) is solved with **zookies**: tokens that force checks to use a snapshot at least as fresh as the content.
- Speed comes from **snapshot-keyed caching with quantised timestamps**, **lock tables**, **hedging** and the **Leopard** index for nested groups.
- **OpenFGA** (CNCF incubating since October 2025) and **SpiceDB** bring the model to everyone.
- In practice, **list filtering**, **dual-write synchronisation**, **consistency choices** and **explainability** decide success.

## Discussion questions

1. Find an endpoint in your product that loads an object by ID. Where exactly is the check that the caller may access it? How would you test that every such endpoint has one?
2. Model your product's permissions as Zanzibar-style types and relations. Which rules are awkward to express, and why?
3. Your list page shows documents a user can access across 2 million documents. Which filtering strategy from section 5 would you use, and how would you keep it fresh?
4. Describe a new enemy scenario in your product. Would a zookie-style token prevent it, and where would you store the token?
5. Your authorization service is down. Which operations should fail closed, and are there any that could safely fail open?
6. When is a central authorization service worth its operational cost compared with authorization logic in each service's own database?

## Sources

- R. Pang et al., ["Zanzibar: Google's Consistent, Global Authorization System"](https://www.usenix.org/system/files/atc19-pang.pdf), USENIX ATC 2019
- [OpenFGA documentation](https://openfga.dev/) and [CNCF project page](https://www.cncf.io/projects/openfga/) (accepted September 2022, incubating October 2025)
- SpiceDB / AuthZed documentation (schema language, ZedTokens, consistency options, LookupResources)
- NIST SP 800-162, "Guide to Attribute Based Access Control (ABAC) Definition and Considerations" (2014)
- Open Policy Agent documentation; AWS Cedar language documentation and Amazon Verified Permissions
- OWASP Top 10 (2021: A01 Broken Access Control); OWASP API Security Top 10 (API1: Broken Object Level Authorization); OWASP Authorization Cheat Sheet
- Earlier readings on this site: OAuth and OIDC, Spanner and TrueTime, exactly-once delivery and the outbox pattern, resilience patterns (hedging)

*Note on verification: the Zanzibar figures and mechanisms were read from the paper today (part of it via a summary of the later sections), and OpenFGA's CNCF dates from the CNCF page. SpiceDB, Cedar, OPA and NIST ABAC details, the OWASP rankings, and the descriptions of list-filtering strategies come from documentation as I know it and weren't re-fetched today.*
