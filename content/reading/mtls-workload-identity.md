---
title: "Mutual TLS and workload identity: how services prove who they are"
date: 2026-11-15
summary: How service-to-service authentication works when the network can't be trusted. Covers why network location isn't identity, how mutual TLS differs from ordinary TLS, certificate issuance and rotation, SPIFFE IDs and SVIDs, SPIRE's node and workload attestation, service meshes (sidecar and ambient), authorisation on identities, cloud workload identity, migrating to mTLS without an outage, and the failure modes, from expired certificates to confused deputies.
tags: security, networking
daily: true
---

Inside most companies, services call each other millions of times a second, and for years the main security control on those calls was the network: if a request came from inside the VPC or from an allowed IP range, it was trusted. That model fails the moment an attacker gets a foothold anywhere inside, and it gets harder to maintain as workloads move between pods, nodes, regions and clouds with changing addresses. The modern answer is to give every workload a cryptographic identity and make every connection prove it, which in practice means **mutual TLS (mTLS)** with short-lived certificates issued automatically. This reading explains how mTLS works, how workloads get identities without a human-managed secret (SPIFFE and SPIRE), how service meshes make it transparent, how authorisation builds on it, and how to roll it out and operate it without causing outages. It draws on Google's BeyondCorp and ALTS papers, NIST's zero trust architecture guidance, the SPIFFE specifications, and the Istio and Linkerd documentation.

## 1. Why network location isn't identity

The traditional model has a hard perimeter (firewalls, VPNs, security groups) and a soft interior. Inside, a request from `10.2.3.4` is assumed to be from the billing service because that's where billing runs.

That assumption breaks in several ways:

- **Lateral movement.** One compromised host, container or laptop on the network can reach everything that trusts the network. Most serious breaches involve an attacker moving sideways from an initial foothold.
- **Addresses aren't stable.** In Kubernetes, pod IPs are recycled constantly; with autoscaling and multiple clusters, an IP-based allow list is out of date almost as soon as it's written.
- **Shared infrastructure.** Load balancers, NAT gateways and proxies hide the original caller, so the IP you see often belongs to infrastructure, not the service.
- **No encryption by default.** Traffic inside the data centre was often plaintext. Leaked documents published in 2013 described intelligence agencies tapping the private links between large companies' data centres, which pushed Google and others to encrypt internal traffic.

Google's response was **BeyondCorp** (described in a series of ;login: articles starting in 2014), which removed the privileged corporate network for employees: every access decision is based on the user's and device's identity and state, not on where the request comes from. For service-to-service traffic, Google built **ALTS** (Application Layer Transport Security), which authenticates and encrypts RPCs between workloads using identities tied to the service, not the machine. NIST later generalised these ideas in **SP 800-207, Zero Trust Architecture** (August 2020): no implicit trust based on network location, authenticate and authorise every request, and use the most specific identity available.

For services, "authenticate every request" needs two things: a strong identity for every workload, and a protocol that proves it on every connection. mTLS provides the second; systems like SPIFFE provide the first.

## 2. TLS versus mutual TLS

In ordinary TLS (the earlier reading on TLS 1.3 covers the handshake in depth), only the **server** proves its identity. It presents a certificate chain; the client checks the chain against trusted root CAs and checks that the certificate's name matches the host it wanted. The client stays anonymous at the TLS layer and usually authenticates later with something in the request, such as a password, cookie or bearer token.

In **mutual TLS**, the server also asks the client for a certificate (a `CertificateRequest` message in the handshake). The client sends its chain and proves it holds the private key by signing the handshake transcript (`CertificateVerify`). Both sides now know, cryptographically, who is on the other end, and the channel is encrypted.

Compared with bearer tokens or API keys sent in each request, mTLS has some useful properties:

- **The secret never crosses the wire.** The private key signs the handshake; it isn't sent. A captured connection or a logged header doesn't leak a reusable credential.
- **Identity is bound to the connection.** Every request on that connection is from the authenticated peer.
- **It works for any protocol over TCP:** HTTP, gRPC, database protocols, Kafka.

It also has costs:

- **Certificate management.** Every workload needs a key pair and a certificate, and they must be rotated. Done by hand, this is the main reason mTLS projects fail.
- **Termination points break the chain.** If an L7 load balancer terminates TLS, the backend sees the load balancer's identity, not the original client's, unless the proxy forwards the client identity in a header (which the backend must then trust) or re-establishes mTLS with its own identity.
- **It identifies workloads, not users.** mTLS tells you the caller is the orders service; it says nothing about which customer the request is for. You still need end-user context, usually a token, for that.

## 3. What's in a workload certificate

A workload certificate is an ordinary X.509 certificate, but what matters is what goes in it and how long it lives.

- **Identity.** Classic web certificates identify DNS names in the Subject Alternative Name (SAN). For workloads, DNS names are a poor fit (many replicas, internal names, services without DNS). The SPIFFE convention puts a **URI SAN** like `spiffe://acme.com/ns/prod/sa/orders` in the certificate, which names the workload itself.
- **Issuer.** An internal CA, not a public one. Public CAs can't issue for internal identities and shouldn't be trusted for them. The internal CA's root or intermediate is distributed to every workload as its **trust bundle**.
- **Lifetime.** Short. Linkerd's proxy certificates expire after 24 hours and are rotated automatically; Istio's workload certificates are also short-lived (a day by default, rotated before expiry); SPIRE commonly issues certificates that live an hour. Short lifetimes mean revocation matters much less. Certificate revocation lists and OCSP are notoriously unreliable, so instead of revoking a compromised certificate you stop renewing it and wait at most its lifetime.
- **Key storage.** The private key should be generated where it's used and never leave that process or node. Ideally it lives only in memory.

Short lifetimes only work if rotation is fully automatic and continuously exercised. The upside is that a rotation system that runs every hour gets tested every hour, unlike a yearly manual renewal that everyone has forgotten how to do.

## 4. SPIFFE: a standard for workload identity

**SPIFFE** (Secure Production Identity Framework For Everyone) is a set of open specifications for workload identity. It was accepted into the CNCF in 2018 and graduated in August 2022. It defines three things.

**SPIFFE ID.** A URI of the form `spiffe://<trust-domain>/<path>`. The trust domain is a security boundary, usually an organisation or environment (`acme.com`, `prod.acme.com`). The path is whatever naming scheme you choose, such as Kubernetes namespace and service account, or a service name and region.

**SVID** (SPIFFE Verifiable Identity Document). The proof of a SPIFFE ID. There are two kinds:

- **X.509-SVID**: a certificate with the SPIFFE ID as the URI SAN, used for mTLS.
- **JWT-SVID**: a signed JWT with the SPIFFE ID as the subject and a specific audience, used where TLS can't carry identity end-to-end (for example through an L7 proxy). It's a bearer token, so it can be replayed if leaked, which is why the X.509 form is preferred.

**Workload API.** A local API (a Unix domain socket on the node) that a workload calls to get its SVIDs and trust bundles, and to receive updates when they rotate. The key design choice is that the workload **doesn't present any secret** to call it. The API identifies the caller from the outside.

SPIFFE also defines **federation**: trust domains can exchange trust bundles so that workloads in `acme.com` can verify SVIDs from `partner.com` or from another cluster with its own CA, without merging the two into one PKI.

## 5. SPIRE: solving the "secret zero" problem

Every credential system has a bootstrapping problem: to get a certificate, a workload has to authenticate to the CA, but with what? If the answer is "a token we baked into the image" or "an API key in an environment variable", you're back to managing static secrets, which is exactly what you were trying to avoid. This is often called the **secret zero** problem.

**SPIRE**, the reference implementation of SPIFFE, solves it with **attestation**: proving what something is by checking facts that an attacker can't easily forge.

<!--diagram:spiffe-mtls-1115-->

**Node attestation.** When a SPIRE agent starts on a node, it proves the node's identity to the SPIRE server using evidence from the platform: an AWS instance identity document signed by AWS, a GCP instance identity token, an Azure managed identity, a Kubernetes projected service account token, or a TPM. The server verifies the evidence with the platform and gives the agent an identity for the node.

**Workload attestation.** When a process calls the Workload API on the node's socket, the agent asks the kernel who is calling (the process ID via the socket's peer credentials) and then collects facts about that process: its Unix user and group, its binary path or hash, or, on Kubernetes, the pod, namespace, service account, labels and container image, by asking the kubelet. These facts are called **selectors**.

**Registration entries** on the server map selectors to SPIFFE IDs: "a workload on a node in cluster prod, in namespace `payments`, with service account `payments-api`, gets `spiffe://acme.com/ns/payments/sa/payments-api`." If the attested selectors match an entry, the agent issues the SVID (signing it via the server or with an intermediate it holds) and streams renewals to the workload before expiry.

The result is a credential system with no stored secret anywhere in the workload. An attacker who copies the container image or reads its environment gets nothing usable. They would have to run code inside that pod on an attested node to get its identity, and even then the certificate expires within an hour or so.

## 6. Service meshes: mTLS without changing the code

Asking every team to add mTLS, rotation and policy to every service, in every language, rarely works. **Service meshes** move it into infrastructure.

### Sidecar meshes

In the classic model (Istio with Envoy, Linkerd with its Rust micro-proxy), every pod gets a **sidecar proxy**. The pod's traffic is transparently redirected through it (usually with iptables rules). When service A calls service B:

1. A's application sends plaintext to what it thinks is B.
2. A's sidecar intercepts it, opens an mTLS connection to B's sidecar using A's workload certificate, and verifies B's certificate.
3. B's sidecar terminates mTLS, checks authorisation policy against A's identity, and forwards plaintext to B's application over localhost.

The mesh control plane (istiod, or Linkerd's identity controller) acts as the CA, issues certificates based on Kubernetes service account tokens, and pushes configuration. Linkerd turns on mTLS for all TCP traffic between meshed pods by default. Istio offers a **PERMISSIVE** mode that accepts both plaintext and mTLS (for migration) and a **STRICT** mode that requires mTLS.

The benefits are big: uniform encryption and identity, plus retries, timeouts, traffic splitting and telemetry, in every language without code changes. The costs are real too: a proxy per pod uses memory and CPU, adds a little latency per hop (two proxies on every call), complicates pod start-up and shutdown ordering, and adds a whole distributed system to operate and upgrade.

### Sidecar-less meshes

To cut those costs, Istio introduced **ambient mode**, which reached general availability in late 2024. A per-node proxy (**ztunnel**) handles mTLS and L4 policy for all pods on the node; optional **waypoint proxies**, deployed per namespace or service, handle L7 features only where needed. Cilium offers a similar split using eBPF in the kernel for L3/L4 and a per-node Envoy for L7. The trade-off is that a shared node proxy is a bigger blast radius than a per-pod sidecar, and it has to keep the identities of many workloads apart correctly.

### Library-based alternatives

Some organisations skip proxies entirely and build mTLS into RPC libraries, as Google did with ALTS in Stubby/gRPC. gRPC's xDS support lets proxyless gRPC clients receive certificates and policy from a mesh control plane. This avoids the extra hop but requires a supported library in every language you use.

## 7. Authorisation: identity is only half the job

Authentication answers "who is calling?". The point of having it is to answer "is this caller allowed to do this?". Encrypting everything and then allowing every authenticated workload to call every other one is a common half-finished state.

Authorisation on workload identity usually looks like:

- **Service-level allow lists.** "Only `spiffe://acme.com/ns/checkout/sa/checkout` may call the payments service." Istio's `AuthorizationPolicy`, Linkerd's `AuthorizationPolicy` and `MeshTLSAuthentication`, and Cilium network policies all express this. Start with default-deny in sensitive namespaces and allow specific callers.
- **Method-level rules.** "The reporting service may call `GET /payments/*` but not `POST /refunds`." This needs an L7-aware enforcement point.
- **Policy engines.** For complex rules, an external engine such as Open Policy Agent can be consulted by the proxy or the application, combining workload identity, the end user's token, and request attributes.

Two subtleties matter:

- **Workload identity plus user identity.** When the API gateway calls the orders service on behalf of customer 42, the orders service should check both that the caller is the gateway (mTLS) and that the request is for customer 42 (a signed token from the gateway or identity provider, passed along). Relying only on workload identity creates a **confused deputy**: any service allowed to call orders can read any customer's orders, so compromising the least important caller exposes everything. Relying only on the user token lets anything that steals a token call internal services directly. Using both is the robust design.
- **Where the check happens.** If the mesh enforces policy at the sidecar, the application must not be reachable by any path that skips the sidecar (for example, a pod port exposed on the host network). Defence in depth means having the application verify identity for critical operations too.

## 8. Workload identity in the cloud

Cloud providers solve the same problem for calls to their own APIs, and the ideas match SPIFFE closely:

- **AWS IAM roles** for EC2 instances and IAM Roles for Service Accounts (IRSA) or EKS Pod Identity on Kubernetes give workloads short-lived credentials obtained by proving where they run, not by storing access keys.
- **GCP service accounts with Workload Identity** on GKE map Kubernetes service accounts to Google service accounts.
- **Azure managed identities** and workload identity federation do the same on Azure.
- **Workload identity federation** across clouds and CI systems usually works by exchanging a signed OIDC token from one platform (a Kubernetes service account token, a GitHub Actions OIDC token) for short-lived credentials on another, so no long-lived cloud keys live in CI.

The common principle: **credentials come from attestation, are short-lived, and are never stored**. Static access keys in environment variables or config files are the thing to eliminate. The earlier reading on secrets management covered the storage side; workload identity is how you make many of those secrets unnecessary.

## 9. Rolling out mTLS without breaking production

Turning on strict mTLS in a running system is a migration with real outage risk. A sequence that tends to work:

1. **Inventory traffic first.** Use mesh telemetry or flow logs to find every caller of every service, including the surprising ones: batch jobs, cron jobs, health checkers, monitoring scrapers, services outside the cluster, legacy VMs.
2. **Issue identities everywhere** before requiring them. Get every workload onto the mesh or SPIRE so it has an SVID.
3. **Permissive mode.** Servers accept both mTLS and plaintext; clients that can use mTLS do. Watch the metrics for plaintext connections per service until they reach zero for each one.
4. **Strict mode per namespace or service**, starting with the most sensitive and best understood. Keep a fast way to revert.
5. **Then authorisation**, in audit mode first (log what would be denied), then enforce default-deny with explicit allow rules.
6. **Handle the edges:** ingress gateways (external clients use ordinary TLS to the gateway, which then uses mTLS inside), egress to third parties, and non-meshed systems such as managed databases, where you might use the platform's own IAM-based authentication.

The most common failure during rollout is a caller nobody knew about, typically a health checker or a metrics scraper that can't do mTLS, which starts failing the moment strict mode is enabled. Audit and permissive phases exist to find them first.

## 10. Failure modes in operation

mTLS replaces one class of risk (network-based trust) with operational risks that need their own care.

- **Expired certificates.** The classic certificate outage. Short-lived certificates make this *more* likely if rotation breaks, because a rotation failure becomes an outage within hours instead of months. Monitor time-to-expiry of the certificates actually being served (not just issued), and alert well before expiry, for example when less than half the lifetime remains.
- **CA and trust bundle rotation.** Rotating a workload certificate is routine; rotating the root or intermediate CA is not. It has to be done in overlapping phases: distribute the new root alongside the old one, start issuing from the new CA, wait until every old certificate has expired, then remove the old root. Skipping a phase splits the fleet into two groups that can't talk to each other. Several widely reported outages have been caused by CA certificate expiry (for example, the September 2021 expiry of Let's Encrypt's old "DST Root CA X3" cross-signed root broke many older clients, which shows how hard root transitions are even on the public web).
- **Control plane dependency.** If the CA (istiod, SPIRE server) is down, existing certificates keep working until they expire, so the CA's availability requirement is tied to certificate lifetime. One-hour certificates with a single CA in one zone is a recipe for a self-inflicted outage. Run the CA highly available, and make certificate lifetime long enough to survive a realistic CA outage.
- **Clock skew.** Certificate validity is time-based. A node whose clock is minutes off can reject fresh certificates as "not yet valid". Keep NTP healthy and issue certificates with a small backdated `notBefore`.
- **Latency and CPU.** TLS handshakes are relatively expensive; connection reuse (HTTP/2, connection pools) makes the per-request cost negligible. Watch for clients that open a new connection per request, since mTLS makes that pattern much more costly.
- **Debuggability.** Encrypted traffic can't be read with tcpdump. Use the mesh's telemetry, proxy access logs, and tools like `istioctl proxy-config secret` or `linkerd viz` to see which identities are in use. Errors such as "certificate signed by unknown authority" almost always mean a trust bundle mismatch between trust domains or after a CA rotation.

## 11. Is it worth it?

mTLS with automated workload identity is now standard practice in large organisations and is increasingly expected by compliance frameworks that require encryption in transit and strong service authentication. Whether to adopt a full service mesh is a separate question:

- **If you're on Kubernetes with many services in several languages**, a mesh is usually the cheapest path to universal mTLS, and sidecar-less modes have reduced the overhead.
- **If you have a few services in one language**, library-level mTLS with SPIFFE (the `go-spiffe` library, or gRPC's built-in support) may be simpler than a mesh.
- **If most of your calls go to cloud-managed services**, cloud workload identity and IAM do most of the work, and mTLS matters mainly between your own services.

Whatever the mechanism, the goals are the same: every workload has an identity it didn't have to be given as a secret, every connection proves it, credentials are short-lived and rotated automatically, and authorisation uses those identities together with the end user's.

## Key takeaways

- **Network location is not identity.** Zero trust (BeyondCorp, NIST SP 800-207) means authenticating and authorising every request, based on the workload's identity, not its address.
- **Mutual TLS** authenticates both ends of a connection and encrypts it, without sending a reusable secret over the wire. It identifies workloads, not end users.
- **SPIFFE** standardises workload identity: SPIFFE IDs (`spiffe://trust-domain/path`), X.509 and JWT SVIDs, and a local Workload API. **SPIRE** issues them by **attesting** nodes and workloads, solving the secret zero problem.
- **Short-lived certificates** (an hour to a day) remove the need for revocation but make rotation and CA availability critical.
- **Service meshes** (sidecar or ambient) deliver mTLS and policy without code changes, at a cost in resources and operational complexity.
- **Authorisation** should combine workload identity with end-user context to avoid confused deputies, and should be default-deny for sensitive services.
- Roll out in phases: inventory, issue identities, permissive, strict, then authorisation. Monitor certificate expiry and plan CA rotations as overlapping phases.

## Discussion questions

1. Your platform uses security groups to restrict which services can reach the payments database. What attacks does that stop, and what does it miss that workload identity would catch?
2. An L7 load balancer terminates TLS in front of a service. How can the backend still learn and trust the caller's identity? What are the risks of forwarding identity in a header?
3. You're choosing a workload certificate lifetime. What argues for one hour, and what for seven days? How does your CA's availability affect the answer?
4. The API gateway calls the orders service over mTLS for every customer. Design an authorisation scheme that prevents a compromised internal analytics service from reading arbitrary customers' orders.
5. During a move to strict mTLS, a batch job that runs once a month breaks two weeks after the cut-over. How could your rollout plan have found it earlier?
6. Would you adopt a service mesh for 15 services, all written in Go? What would you do instead, if anything?

## Sources

- R. Ward, B. Beyer, "BeyondCorp: A New Approach to Enterprise Security," ;login: (USENIX), 2014, and later BeyondCorp papers
- Google, "Application Layer Transport Security" (ALTS) whitepaper; Google Cloud, "BeyondProd: A new approach to cloud-native security"
- NIST, [SP 800-207, Zero Trust Architecture](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-207.pdf), August 2020
- RFC 8446 (TLS 1.3) on client authentication; RFC 5280 (X.509 certificates and SANs)
- SPIFFE specifications (SPIFFE ID, X.509-SVID, JWT-SVID, Workload API, Trust Domain and Bundle federation) at spiffe.io; [CNCF SPIFFE project page](https://www.cncf.io/projects/spiffe/) (graduated August 2022)
- SPIRE documentation: architecture, node attestors, workload attestors and registration entries
- Linkerd, ["Automatic mTLS"](https://linkerd.io/2-edge/features/automatic-mtls/) (on by default, 24-hour certificates)
- Istio documentation: security concepts, PeerAuthentication (PERMISSIVE/STRICT), AuthorizationPolicy, ambient mode (ztunnel and waypoint proxies)
- Cilium documentation on mutual authentication and service mesh; Open Policy Agent documentation
- AWS IAM Roles for Service Accounts and EKS Pod Identity docs; GKE Workload Identity docs; Azure workload identity federation docs
- Let's Encrypt, "DST Root CA X3 Expiration (September 2021)"

*Note on verification: the SPIFFE CNCF timeline, Linkerd's defaults and the NIST publication date were read from the linked pages today. Istio's default certificate lifetime and the timing of ambient mode's general availability, SPIRE's typical certificate lifetimes, and the cloud identity mechanisms come from documentation as I know it and weren't re-fetched; check the current docs for your versions.*
