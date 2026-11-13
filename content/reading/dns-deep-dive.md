---
title: "DNS for backend engineers: resolution, caching, and why it's always DNS"
date: 2026-11-13
summary: How a name becomes an address and why DNS shows up in so many outages. Covers the hierarchy and delegation, stub vs recursive vs authoritative servers, TTLs and the many caches in between, negative caching and serve-stale, UDP/TCP/EDNS, record types that matter for services, DNS-based load balancing, Kubernetes DNS, cache poisoning and DNSSEC, encrypted DNS, and three big DNS outages (Facebook 2021, Slack 2021, AWS 2025).
tags: networking, infrastructure
daily: true
---

Almost every request your services make starts with a DNS lookup, and most engineers never think about it until something breaks. When it does, the failure is often strange: some users can connect and others can't, a fix takes an hour to "take effect", or a service that was healthy the whole time becomes unreachable because the name pointing at it disappeared. The joke "it's always DNS" exists because DNS is a globally distributed, heavily cached, eventually consistent database that sits in front of everything else. This reading covers how resolution actually works, where caching happens and how TTLs really behave, which record types matter for backend systems, how DNS is used for load balancing and service discovery, its security story (cache poisoning, DNSSEC, encrypted transports), and three large outages that were caused or amplified by DNS. It draws on the core RFCs, Cloudflare's and APNIC's writing on DNS, and published post-mortems from Meta, Slack and AWS.

## 1. What DNS is: a hierarchical, delegated, cached database

DNS (the Domain Name System, specified in RFC 1034 and RFC 1035 in 1987, building on Paul Mockapetris's 1983 design) maps names to data. The data is organised as **resource records**: a name, a type (A, AAAA, CNAME, MX, TXT...), a class (almost always IN), a TTL, and the value.

Three ideas make it scale to the whole internet:

- **Hierarchy.** Names form a tree read right to left: the root (an empty label, written as a trailing dot), then top-level domains like `com`, then `example.com`, then `api.example.com`.
- **Delegation.** No server holds the whole tree. The root servers know who is authoritative for `com`; the `com` servers know who is authoritative for `example.com`; and `example.com`'s own servers hold its records. Each delegation is expressed as NS records in the parent plus, where needed, "glue" A/AAAA records so the child's name servers can be found. A **zone** is the part of the tree one set of authoritative servers is responsible for.
- **Caching.** Every answer carries a TTL that says how long it may be reused. Caching is what makes DNS fast and cheap, and it's also where most of its operational surprises come from.

The result is a database with no central server for data, very high read availability, and only eventual consistency for writes. Changing a record is instant on the authoritative server, but the world sees it only as caches expire.

## 2. The cast: stub, recursive resolver, authoritative server

A DNS lookup involves several different kinds of software, and confusing them causes a lot of misdiagnosis.

- **Stub resolver.** The library in your process or OS that turns `getaddrinfo("api.example.com")` into a DNS query. It doesn't walk the tree; it sends one query to a configured recursive resolver (from `/etc/resolv.conf` on Linux) and waits. Some stubs cache (systemd-resolved, macOS's mDNSResponder, nscd, the JVM's own cache); glibc's resolver by itself does not.
- **Recursive resolver** (also called a full or caching resolver). Your ISP's resolver, a public one like 1.1.1.1 or 8.8.8.8, your corporate resolver, or the resolver inside a cloud VPC (such as the Route 53 Resolver at the VPC's `.2` address). It does the real work: walking the hierarchy, caching everything, and returning a final answer.
- **Authoritative server.** Holds the actual zone data for a domain and answers with the "authoritative answer" (AA) flag set. Root servers, TLD servers and your DNS provider's servers (Route 53, Cloudflare DNS, NS1...) are authoritative for their zones. They don't recurse on behalf of clients.
- **Forwarders** sit in between in many networks: a local resolver that forwards everything to an upstream recursive resolver, often with its own cache.

<!--diagram:dns-resolution-1113-->

### A cold lookup

Suppose nothing is cached and a client asks for `api.example.com`:

1. The stub sends a query (A or AAAA, often both in parallel) to the recursive resolver.
2. The resolver asks a **root server**. The root doesn't know the answer but returns a **referral**: the NS records for `com` and their addresses.
3. The resolver asks a `com` **TLD server**, which returns a referral to `example.com`'s name servers.
4. The resolver asks `example.com`'s **authoritative server**, which returns the A record (or a CNAME, which starts another lookup for the target name).
5. The resolver caches every piece, the referrals included, and answers the stub.

In practice, the root and TLD referrals are almost always cached already (the `com` NS records have a two-day TTL), so a typical "uncached" lookup is a single round trip from the resolver to your authoritative servers, and most lookups are answered straight from the resolver's cache in well under a millisecond plus the network hop.

### The root servers

There are 13 root server *identities*, `a.root-servers.net` through `m.root-servers.net`, run by 12 organisations. The number 13 is a historical consequence of fitting all their addresses into a 512-byte UDP response. Each identity is served by many machines in many places using **anycast** (the same IP address announced via BGP from many sites, with routing sending you to a nearby one), so in total there are well over a thousand root server instances worldwide. Anycast is also how large DNS providers and public resolvers achieve low latency and absorb attacks.

## 3. TTLs and the many caches

Every record has a TTL in seconds. A resolver that caches an answer counts the TTL down and returns the remaining time to its own clients, so a downstream cache never holds the answer longer than the original TTL in total. That's the theory. In practice, a lookup passes through several caches, and each may behave differently:

- **The application.** Long-lived processes may resolve a name once and keep the address forever. The JVM caches successful lookups (`networkaddress.cache.ttl`, 30 seconds by default in recent JDKs when no security manager is installed, but "forever" in some older configurations). HTTP client connection pools keep connections to an old IP alive no matter what DNS says.
- **The OS stub cache** (systemd-resolved, mDNSResponder, Windows DNS Client).
- **Node-local caches** (NodeLocal DNSCache in Kubernetes, dnsmasq).
- **The recursive resolver**, often a fleet of resolvers behind one anycast address, each with its own cache, so two queries a second apart can get different answers during a change.
- **Browsers**, which keep their own short-lived host caches.

Things to know about TTLs:

- **Lowering a TTL only helps after the old TTL has expired.** If a record has a TTL of 86,400 seconds and you change it to 60 just before a migration, resolvers that cached it an hour ago will hold it for 23 more hours. The standard playbook is to lower the TTL at least one old-TTL period *before* the change, make the change, then raise it again afterwards.
- **Some resolvers clamp TTLs.** Some enforce a minimum (ignoring very low TTLs), some a maximum. Don't assume a 5-second TTL produces 5-second failover everywhere.
- **Low TTLs cost something.** More queries to your authoritative servers (and more billable queries with managed DNS), more latency on cache misses, and more exposure if your DNS provider is down, because caches empty quickly. High TTLs make changes slow but make you resilient to authoritative outages. Many teams settle on 60 to 300 seconds for records that front failover-capable endpoints and hours for stable ones.

### Negative caching

Answers that say "this doesn't exist" are cached too. RFC 2308 defines **negative caching**: an NXDOMAIN (name doesn't exist) or NODATA (name exists, but not with that type) response includes the zone's SOA record, and the negative answer is cached for the smaller of the SOA record's TTL and its MINIMUM field. This matters operationally:

- If a client looks up a name *before* you create it (for example, a health checker probing a new service before the record is published), resolvers cache the NXDOMAIN, and the name stays "missing" for them until the negative TTL runs out, even after you create it.
- A misconfiguration that briefly deletes a record gets cached as NXDOMAIN and keeps causing failures after you fix it.

### Serve-stale

RFC 8767 (2020) standardises **serve-stale**: if a resolver can't reach any authoritative server to refresh an expired record, it may keep serving the stale answer (with a short TTL, such as 30 seconds) for a limited period, often up to a day or a few days, while it retries in the background. Several major public resolvers do this. It turns "authoritative DNS is down" from an immediate outage into a slow degradation for names that were recently queried, which is a big resilience win, but it also means a record you deliberately removed can linger in some places when your servers are unreachable.

## 4. On the wire: UDP, TCP, EDNS and size limits

Classic DNS is a single UDP request and response on port 53, which is why it's so fast. Some details that cause real problems:

- **The 512-byte limit.** The original spec limited UDP responses to 512 bytes. A response that doesn't fit sets the **TC** (truncated) flag, and the client retries over TCP. **EDNS(0)** (RFC 6891) lets a client advertise a larger UDP buffer, which DNSSEC signatures and large TXT records need.
- **Fragmentation.** Large UDP responses get split into IP fragments, which are often dropped by firewalls and are a vector for spoofing attacks. DNS Flag Day 2020 coordinated major resolver and server operators on a default EDNS buffer size of 1232 bytes, chosen to avoid fragmentation on typical paths, with TCP fallback for anything larger.
- **TCP must work.** RFC 7766 makes TCP support a requirement for DNS, not an optional fallback. A firewall that blocks TCP port 53 "because DNS is UDP" causes intermittent failures for exactly the names that have large responses: DNSSEC-signed zones, names with many A records, big TXT records for SPF and domain verification.
- **Query IDs and timeouts.** Each query has a 16-bit ID, and a stub typically retries after a timeout (glibc's default is 5 seconds per attempt, with 2 attempts per server). A lost UDP packet can therefore add seconds of latency to a request. That's why a mysterious, very regular 5-second latency spike in a service is a classic DNS symptom.

## 5. Record types a backend engineer actually uses

- **A / AAAA:** IPv4 / IPv6 addresses. Clients that support both will query both (and use "Happy Eyeballs", RFC 8305, to race connections). A broken AAAA record can slow down or break IPv6-capable clients while IPv4-only clients are fine, a common "only some users" failure.
- **CNAME:** an alias to another name. A CNAME must be the only record at its name, which is why you can't put a CNAME at the zone apex (`example.com` itself), since the apex must also have SOA and NS records. Managed DNS providers work around this with proprietary flattening (Route 53 "alias" records, Cloudflare "CNAME flattening", "ANAME" or "ALIAS" elsewhere): the provider resolves the target itself and returns A/AAAA records.
- **NS / SOA:** delegation and zone metadata. The SOA's fields control negative caching and secondary-server refresh.
- **MX, TXT:** mail routing; free text used heavily for SPF, DKIM, DMARC and domain-ownership verification.
- **SRV:** service location with port, priority and weight (`_service._proto.name`). Used by many service-discovery systems, SIP, XMPP, and Kubernetes headless services.
- **CAA:** which certificate authorities may issue certificates for the domain. CAs are required to check it, so it's a cheap defence against mis-issuance.
- **SVCB / HTTPS** (RFC 9460, 2023): lets a domain advertise how to connect, such as supported ALPN protocols (h2, h3), alternative endpoints, IP hints and Encrypted Client Hello keys, in the initial lookup. It also solves the apex problem for HTTPS services. Browsers already query HTTPS records, which is one way HTTP/3 gets discovered without a first connection over TCP.
- **PTR:** reverse DNS (IP to name). Mostly matters for mail servers and logs.

## 6. DNS as a load balancer and failover mechanism

Because DNS is the first step of every connection, it's an attractive place to steer traffic. The earlier reading on load balancing covered where DNS sits among the layers; here are the mechanics and their limits.

- **Round-robin and multi-value answers.** Return several A records; clients usually try them in the returned order or randomise. There's no health awareness unless the authoritative server provides it, and clients that cache one address stick to it.
- **Health-checked records.** Managed DNS services can health-check endpoints and drop unhealthy ones from answers (Route 53 health checks, failover routing). Failover speed is roughly health-check detection time plus TTL plus however long clients hold onto addresses or connections.
- **Weighted, latency-based and geo routing.** The authoritative server picks an answer based on configured weights, measured latency, or the client's location. The location is usually the *resolver's* location, not the user's, which is wrong for users on distant public resolvers. **EDNS Client Subnet** (ECS, RFC 7871) lets a resolver pass a truncated client subnet to the authoritative server to fix this, at a cost to privacy and to cache efficiency (answers are cached per subnet). Some public resolvers, including Cloudflare's 1.1.1.1, deliberately don't send ECS for privacy reasons.
- **Anycast** often replaces DNS steering for large providers: one IP address everywhere, and BGP routes users to the nearest site. DNS then only has to return a stable address.

The common limitation is that **DNS can't drain connections**. It only affects new lookups. Long-lived connections (HTTP/2, gRPC, database pools, WebSockets) stay on the old target until something closes them. Reliable failover needs DNS changes *plus* connection lifetimes or server-side draining.

## 7. DNS inside your infrastructure

Internal DNS is where many backend engineers actually meet DNS problems.

### Kubernetes

Kubernetes clusters run CoreDNS (or kube-dns), and every pod's `/etc/resolv.conf` points at it with a list of search domains and, by default, `options ndots:5`. That setting means any name with fewer than five dots is first tried with each search domain appended. A lookup for `api.example.com` from a pod in namespace `prod` can turn into queries for `api.example.com.prod.svc.cluster.local`, `api.example.com.svc.cluster.local`, `api.example.com.cluster.local`, maybe a cloud-provider domain, and only then `api.example.com`, each for both A and AAAA. Most come back NXDOMAIN. This multiplies DNS load and latency for external names. Fixes include using fully qualified names with a trailing dot (`api.example.com.`), lowering `ndots` in the pod's `dnsConfig`, and running NodeLocal DNSCache so queries don't cross the network to CoreDNS.

A well-known source of intermittent 5-second DNS delays in Kubernetes was a Linux conntrack race when A and AAAA queries were sent in parallel from the same socket over UDP, causing one of them to be dropped and retried after the timeout. Workarounds included the `single-request-reopen` resolver option and node-local caching.

### Cloud VPCs and split horizon

Cloud VPCs provide a resolver that answers both public names and private zones (for example, Route 53 private hosted zones), so the same name can resolve to a private address inside the VPC and a public one outside. This **split-horizon** DNS is convenient but confusing when debugging: "it resolves fine on my laptop" proves nothing about what a pod sees. Always test from where the failing client runs, and check which resolver it's using.

### Service discovery

Consul, Kubernetes services and cloud service registries all expose service discovery through DNS because every language and tool already speaks it. The trade-off is the caching story above: DNS-based discovery is only as fresh as its TTLs and its clients' caches. Systems that need fast membership changes (gRPC's xDS, client-side load balancers that watch an API) skip DNS for that reason.

## 8. Security: spoofing, cache poisoning and DNSSEC

Classic DNS has no authentication. A resolver accepts a UDP response if it arrives from the right address and port with the right 16-bit query ID and question. That makes **cache poisoning** possible: an attacker who can get a forged response in before the real one gets it cached for the TTL, redirecting every user of that resolver.

In 2008 Dan Kaminsky showed that this was much more practical than people thought. By querying many random non-existent subdomains (`a1.example.com`, `a2.example.com`...), an attacker gets unlimited attempts, and a forged response can include a malicious *referral* that hijacks the whole domain. The coordinated fix was **source port randomisation**: making resolvers send each query from a random UDP port, multiplying the guessing space from 16 bits of query ID to roughly 32 bits. Further hardening includes "0x20" randomisation of letter case in the query name (which the response must echo), and since 2020 the "SAD DNS" research showed side channels that could still recover the source port, leading to more fixes in operating systems and resolvers.

### DNSSEC

**DNSSEC** adds cryptographic signatures to DNS data so that a validating resolver can verify an answer really came from the zone owner, unmodified.

- Each zone has signing keys published as **DNSKEY** records, and every record set is signed with an **RRSIG** record.
- The parent zone publishes a **DS** record, a hash of the child's key-signing key, which builds a **chain of trust** from the root (whose key resolvers ship with as a trust anchor) down through `com` to `example.com`.
- **NSEC** or **NSEC3** records prove that a name doesn't exist, so "NXDOMAIN" can be authenticated too.

The root zone was signed in 2010, and the root key-signing key was rolled for the first time in October 2018 after years of preparation, a measure of how carefully this has to be done.

DNSSEC's protection is real but narrow: it authenticates data between the zone and a validating resolver. It does nothing for privacy and nothing for the hop between your stub and the resolver unless the stub validates too. Its operational risk is also real: a signing mistake, an expired signature, or a key mismatch makes the domain fail for every validating resolver, which is a large share of internet users today. Adoption among large commercial domains remains low partly for this reason, and the Slack incident in section 10 shows why.

### Encrypted DNS

Classic DNS queries are plaintext, so anyone on the path can see and alter which names you look up. Several protocols encrypt the stub-to-resolver hop:

- **DNS over TLS (DoT)**, RFC 7858 (2016), on port 853.
- **DNS over HTTPS (DoH)**, RFC 8484 (2018), which looks like ordinary HTTPS traffic and is what browsers adopted.
- **DNS over QUIC (DoQ)**, RFC 9250 (2022).

These protect privacy and integrity on that hop but move trust to the resolver operator. Enterprises sometimes find that browsers using DoH bypass internal resolvers and split-horizon names, so corporate policies often disable or redirect it. Separately, **QNAME minimisation** (RFC 7816, updated by RFC 9156) has resolvers send only the needed label to each server (ask the root about `com`, not `api.example.com`), reducing what root and TLD operators learn.

## 9. Debugging DNS

A short toolkit:

- **`dig`** shows the full response: flags (AA, TC, RD, RA, AD for "authenticated data"), TTLs, sections. `dig +trace name` walks the hierarchy from the root yourself and shows where delegation breaks. `dig @server name` queries a specific resolver or authoritative server, which is how you tell "my resolver's cache is stale" from "the authoritative data is wrong".
- **Compare authoritative vs recursive.** Query each of the zone's NS servers directly. If they disagree, a zone update hasn't propagated to all of them. If they agree but your resolver doesn't, it's caching.
- **Check the TTL counting down** in repeated `dig` output from a resolver to see when a cached record will expire.
- **Watch for NXDOMAIN vs SERVFAIL.** NXDOMAIN means the name doesn't exist (and gets negatively cached). SERVFAIL means the resolver couldn't get a good answer, which often points at unreachable authoritative servers, a lame delegation, or a DNSSEC validation failure. `dig +cd` (checking disabled) that succeeds where a normal query fails is a strong DNSSEC hint.
- **Measure from the client's environment.** Inside the container, with its `/etc/resolv.conf`, its search domains and its resolver. Many "DNS is fine" conclusions come from testing on a different machine.
- **Instrument resolution in applications.** Track lookup latency and failures as their own metrics. A slow DNS lookup otherwise shows up as a vague increase in "connect time".

## 10. Three outages worth knowing

### Facebook, October 4, 2021

According to Meta's engineering write-up, a command issued during routine maintenance to assess backbone capacity unintentionally took down all the connections in Meta's backbone network, disconnecting its data centres from each other and the internet. A bug in the audit tool meant to catch such commands didn't stop it. Meta's authoritative DNS servers were designed to withdraw their BGP route announcements when they couldn't reach the data centres, treating that as a sign of an unhealthy network location. With the backbone gone, *every* DNS site withdrew its routes. Meta's DNS servers were still running but unreachable, so `facebook.com`, `instagram.com` and `whatsapp.com` stopped resolving worldwide. The outage lasted about six hours. Recovery was slow partly because the same failure took down internal tools, and engineers had to get physical access to data centres. A side effect: resolvers around the world were hammered with retries for names that wouldn't resolve.

The lesson for system design: a health mechanism that removes an endpoint when it looks unhealthy can remove *every* endpoint during a correlated failure. DNS health checks and anycast withdrawals need a floor ("never withdraw the last N sites") or a fail-open mode.

### Slack, September 30, 2021

Slack was rolling out DNSSEC for `slack.com`. According to Slack's engineering post-mortem ("What happened during Slack's DNSSEC rollout"), after the DS record was published at the `.com` parent, some users behind validating resolvers couldn't reach Slack. The root cause turned out to be how their DNS provider answered certain queries under DNSSEC (involving a wildcard record and the "no data" proof returned for AAAA queries), which validating resolvers rejected or cached as negative answers. Rolling back took a long time, because the DS record in the parent zone and the negative answers were cached by resolvers for their TTLs, so removing the DS record didn't fix users immediately. The outage affected a subset of users for many hours.

Lesson: DNSSEC changes involve records in a parent zone you don't control and caches you can't flush, so the rollback can be much slower than the rollout. Test with many validating resolvers, use short TTLs during the rollout, and plan the rollback timeline before you start.

### AWS us-east-1, October 19-20, 2025

AWS's post-event summary describes a latent race condition in the automation that manages DNS for DynamoDB. A **DNS Planner** component monitors the health and capacity of load balancers and periodically produces a new DNS plan for each endpoint; three independent **DNS Enactor** instances, in different Availability Zones, apply plans to Route 53. One Enactor was delayed while applying an older plan; meanwhile another applied a newer plan and then ran clean-up that deletes plans significantly older than the one just applied. The delayed Enactor's check that its plan was newer than what was active had gone stale, so it overwrote the newer plan with the old one, and the clean-up then deleted that old plan. The result was that all IP addresses for `dynamodb.us-east-1.amazonaws.com` were removed, leaving an empty record, and the automation was left in a state it couldn't repair, requiring manual operator intervention.

The failure began at 11:48 PM PDT on October 19. AWS restored the DNS records by 2:25 AM PDT on October 20, and customers recovered between 2:25 and 2:40 AM as cached DNS entries expired. Because many AWS services depend on DynamoDB internally, the impact spread well beyond DynamoDB and lasted much longer for some services.

Lessons: DNS automation is a distributed system with its own consistency bugs (here, a stale check and concurrent writers), and an empty answer is the worst possible output. Guardrails that refuse to publish a plan with zero healthy targets, and comparisons based on versions checked at commit time rather than at the start of a long operation, are the kind of fixes that generalise.

### The 2016 Dyn attack

Worth a sentence for context: on October 21, 2016, the Mirai botnet of compromised IoT devices hit the managed DNS provider Dyn with a large DDoS, and many big sites that depended on Dyn alone became unreachable for parts of the US for hours. Many companies then started using **two independent authoritative DNS providers**, which is now a common practice for critical domains, at the cost of keeping zones in sync.

## 11. A checklist for production DNS

- Use at least two authoritative providers, or one provider with a strong anycast footprint and a tested plan for the other.
- Choose TTLs per record by how fast you need to change it versus how long you want to survive an authoritative outage. Lower TTLs one full old-TTL period before planned changes.
- Never let automation publish an empty or all-unhealthy answer. Fail open to the last known good set.
- Remember that DNS failover doesn't move existing connections; cap connection lifetimes or drain at the server.
- Keep TCP port 53 open and EDNS sizes sane; test DNSSEC-signed and large responses.
- In Kubernetes, watch `ndots` and search-domain amplification, and run node-local caching.
- Monitor resolution latency and error rates from the client side, separately from connect time.
- Treat DNSSEC and registrar changes like database migrations: staged, reversible, with the rollback time written down.
- Lock down your registrar account (MFA, registry lock) and use CAA records; hijacking the domain at the registrar bypasses everything else.

## Key takeaways

- DNS is a hierarchical, delegated, **heavily cached** database. Writes are instant at the source and **eventually consistent** everywhere else.
- **Stub, recursive and authoritative** servers do different jobs. Most lookups are answered from a resolver cache; a cold lookup walks root → TLD → authoritative.
- **TTLs** bound cache lifetimes, but applications, connection pools and resolver clamping all stretch them. **Negative answers are cached too**, and **serve-stale** keeps old answers alive when authoritative servers are unreachable.
- DNS works over **UDP and TCP**; block TCP or mishandle EDNS and large responses fail intermittently. Regular 5-second delays usually mean a lost UDP query and a stub retry.
- DNS-based load balancing steers **new lookups**, not existing connections, and geo-steering sees the resolver, not the user, unless ECS is used.
- **Cache poisoning** is mitigated by randomisation and fully addressed for data integrity by **DNSSEC**, which brings its own operational risk. **DoT/DoH/DoQ** encrypt the client hop.
- The big DNS outages share a pattern: automation or health logic that can remove **every** answer, plus caches that make rollback slow.

## Discussion questions

1. You're moving an API to a new load balancer. The record currently has a one-hour TTL. Write out the timeline, including what happens to clients with open HTTP/2 connections.
2. Your health-checked DNS removes unhealthy endpoints. What should it do when all endpoints fail their health checks at once, and why did that question matter in the Facebook outage?
3. A pod in Kubernetes takes 20 ms longer to call an external API than a VM in the same region does. How would you find out whether DNS is responsible, and what would you change?
4. Would you enable DNSSEC on your company's main domain? Weigh the threat it addresses against the Slack experience, and describe how you'd roll it out safely.
5. In the AWS incident, what single guardrail would have turned the bug into a non-event? Where else in your systems does an automated planner write configuration that every client depends on?
6. Your service caches DNS results forever in-process because lookups were once slow. What breaks, and what's a safer design?

## Sources

- P. Mockapetris, RFC 1034 "Domain Names: Concepts and Facilities" and RFC 1035 "Domain Names: Implementation and Specification" (1987)
- RFC 2308 (negative caching), RFC 6891 (EDNS(0)), RFC 7766 (DNS over TCP), RFC 7858 (DoT), RFC 8484 (DoH), RFC 9250 (DoQ), RFC 7816/9156 (QNAME minimisation), RFC 7871 (EDNS Client Subnet), RFC 8767 (serve-stale), RFC 8305 (Happy Eyeballs v2), RFC 9460 (SVCB/HTTPS records)
- DNS Flag Day 2020 (dnsflagday.net) on EDNS buffer sizes; root-servers.org on root server operators and instances
- Meta Engineering, ["More details about the October 4 outage"](https://engineering.fb.com/2021/10/05/networking-traffic/outage/) (2021); ThousandEyes, ["Facebook outage analysis"](https://www.thousandeyes.com/blog/facebook-outage-analysis); APNIC Labs, ["Learning from Facebook's mistakes"](https://labs.apnic.net/?p=1496)
- Slack Engineering, ["What happened during Slack's DNSSEC rollout"](https://slack.engineering/what-happened-during-slacks-dnssec-rollout/) (2021); [ianix DNSSEC outage log entry](https://ianix.com/pub/dnssec-outages/20210930-slack.com)
- AWS, ["Summary of the Amazon DynamoDB Service Disruption in the Northern Virginia (US-EAST-1) Region"](https://aws.amazon.com/message/101925) (October 2025); InfoQ, ["AWS DynamoDB outage postmortem"](https://www.infoq.com/news/2025/11/aws-dynamodb-outage-postmortem)
- Dan Kaminsky's 2008 cache poisoning disclosure and coordinated patch; K. Man et al., "DNS Cache Poisoning Attack Reloaded" (SAD DNS), ACM CCS 2020
- Kubernetes documentation, "DNS for Services and Pods" and "Using NodeLocal DNSCache"; Weaveworks/XING engineering posts on the conntrack race causing 5-second DNS delays
- Cloudflare Learning Center and blog posts on DNS, 1.1.1.1 and DNSSEC; Amazon Route 53 developer guide (alias records, health checks, routing policies)
- Cricket Liu and Paul Albitz, *DNS and BIND*, 5th ed. (O'Reilly)

*Note on verification: the AWS timeline and DNS Planner/Enactor description were read from the AWS summary today, and the Facebook and Slack incidents were checked against the linked write-ups via search. RFC numbers, the root server facts, TTL and timeout defaults (glibc 5 s, JVM 30 s, `ndots:5`, DNS Flag Day 1232 bytes) and the Kaminsky/SAD DNS details come from the specifications and documentation as I know them and weren't re-fetched today; check your own resolver and runtime versions before relying on a specific default.*
