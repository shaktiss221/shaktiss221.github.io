---
title: "Secrets management: Vault, KMS and envelope encryption"
date: 2026-10-26
summary: From passwords in config files to dynamic credentials and workload identity, with envelope encryption underneath.
tags: security, infrastructure
---

The best secret is one that expires before anyone can misuse it, and the next best is one your code never sees. Modern secrets management moves step by step toward that: from passwords in config files, to a central vault, to credentials minted on demand, to workload identity where there is no long-lived secret at all. Underneath it sits one cryptographic pattern, envelope encryption, that every cloud KMS and Vault use. Reading time is about 30 minutes.

## 1. Why secrets leak, and the maturity ladder

### What counts as a secret

Database passwords, API keys for third parties (Stripe, Twilio, OpenAI), cloud access keys, TLS private keys, signing keys for JWTs or webhooks, SSH keys, OAuth client secrets, and encryption keys. What they share: possession equals authority. Whoever holds the string can act as you, usually without any further check.

### How they leak

The recurring patterns in public incident write-ups are mundane:

- **Committed to source control.** GitGuardian's annual "State of Secrets Sprawl" reports count millions of new secrets pushed to public GitHub each year, and the 2016 Uber breach began with AWS credentials found in a *private* repository. Deleting the file doesn't help; it stays in Git history and in every clone.
- **Copied into CI/CD.** Build systems hold credentials for every environment they deploy to. The 2021 Codecov incident modified a script that thousands of CI pipelines ran, exfiltrating their environment variables. After CircleCI's 2023 breach, the company told every customer to rotate every secret stored with it.
- **Logged.** A debug log line prints the full connection string, an exception handler dumps the request headers including `Authorization`, or an error tracker captures environment variables.
- **Over-shared.** One API key used by ten services means ten places it can leak, and nobody can rotate it without coordinating all ten.
- **Never rotated.** A key created in 2019 by an engineer who left in 2021 still works, and no one knows what depends on it.

The common thread is that a **static, long-lived, widely copied** secret has unbounded exposure. Every copy is a leak path, and every day it stays valid is a day a leaked copy works.

### Environment variables are not a secrets store

The Twelve-Factor App popularised configuration through environment variables, and they are much better than hard-coded values. But they're a delivery mechanism, not protection:

- They are readable by anything running as the same user (`/proc/<pid>/environ` on Linux) and are inherited by every child process.
- They appear in `docker inspect`, in Kubernetes pod specs, in crash dumps, and in many "debug info" pages.
- They're set once at start-up, so rotating means restarting.

Env vars are acceptable as the last hop for a secret fetched from a proper store, ideally a short-lived one. They shouldn't be the place the secret lives.

### The maturity ladder

| Level | Where the secret lives | Typical tools | Remaining risk |
| --- | --- | --- | --- |
| 0. Hard-coded | Source code | none | Anyone with repo access, forever |
| 1. Out-of-repo config | Env vars, config files on hosts | `.env`, Ansible vars | Copies on every host and CI runner; no audit; manual rotation |
| 2. Encrypted in Git | Ciphertext in the repo, decrypted at deploy | SOPS, Sealed Secrets, git-crypt | Better history hygiene; still static and long-lived |
| 3. Central store | Fetched at runtime from one service | AWS Secrets Manager, GCP Secret Manager, Azure Key Vault, Vault KV | Audit and access control; secret is still long-lived unless rotated automatically |
| 4. Dynamic secrets | Minted per workload, expiring in minutes or hours | Vault database / AWS / PKI engines | A leak is time-boxed and traceable to one workload |
| 5. No shared secret | Identity from the platform, exchanged for short-lived tokens | IAM roles, GKE/EKS workload identity, OIDC federation, SPIFFE | The trust anchor (the platform's identity) is what you must protect |

Most organisations live at levels 2 to 3 for most things, with level 5 for cloud APIs and level 4 for a few high-value databases. The rest of this piece explains the mechanics behind levels 3 to 5, starting with how the stores themselves keep secrets encrypted.

## 2. KMS and envelope encryption

Envelope encryption means encrypting data with a fresh **data encryption key (DEK)**, then encrypting that DEK with a **key encryption key (KEK)** that never leaves a hardened key service. It's how AWS KMS, Google Cloud KMS, Azure Key Vault, Vault's transit engine, S3 server-side encryption, and encrypted databases all work.

### Why not just encrypt everything with the master key?

Three reasons:

1. **Throughput and size limits.** A KMS is a remote, rate-limited service backed by hardware security modules (HSMs). AWS KMS's `Encrypt` API accepts at most 4 KB of plaintext. You can't stream a 10 GB backup through it.
2. **Blast radius.** If one key encrypts everything directly, compromising it exposes everything. With per-object or per-file DEKs, each piece of data has its own key.
3. **Rotation cost.** Rotating a KEK only requires re-wrapping the small DEKs, not re-encrypting terabytes of data.

### The flow

<!--diagram:307f9c6a-bf2f-->

The application asks KMS for a data key, uses it locally, and stores only the wrapped copy next to the data.

**Encrypt:**

1. The app calls KMS `GenerateDataKey` with the ID of a KEK it's allowed to use.
2. KMS generates a random 256-bit DEK and returns two copies: the **plaintext DEK** and the **DEK encrypted under the KEK**.
3. The app encrypts the data locally with the plaintext DEK using an authenticated cipher such as AES-256-GCM, stores the ciphertext together with the encrypted DEK (the "envelope"), and wipes the plaintext DEK from memory.

**Decrypt:**

1. The app reads the envelope and sends the encrypted DEK to KMS `Decrypt`.
2. KMS checks the caller's permission on the KEK, logs the call, and returns the plaintext DEK.
3. The app decrypts locally.

The security property is that **anyone who steals the stored data gets only ciphertext and a wrapped key**. To read it they also need permission to call `Decrypt` on that KEK, which is governed by IAM, audited in CloudTrail (or the equivalent), and can be revoked instantly. Disabling a KEK makes all data under it unreadable, which is the basis of **crypto-shredding**: delete a customer's KEK, and all their data, including copies in backups, is effectively destroyed.

### Encryption context and AAD

AWS KMS and similar services accept an **encryption context**: key-value pairs (for example `{"tenant": "acme", "table": "invoices"}`) that are cryptographically bound to the ciphertext as additional authenticated data. Decrypt must supply the same context, or it fails. This stops an attacker from swapping one tenant's encrypted DEK into another tenant's record, and the context appears in audit logs, so you can see *what* was decrypted, not just that something was. Use the same idea with AES-GCM's AAD when you encrypt the data itself.

### Caching data keys

Calling KMS on every read adds latency (typically single-digit to tens of milliseconds) and costs money per request, and KMS has per-account request quotas. Libraries such as the **AWS Encryption SDK** can cache DEKs for a bounded time or number of messages. That trades a little blast radius for a lot of performance. Bound the cache by age and by count, and never cache across tenants.

### Key hierarchy and rotation

Real systems usually have more than two levels: an HSM-protected root, per-service or per-tenant KEKs under it, and per-object DEKs at the bottom. Rotation works level by level:

- **KMS automatic rotation** creates new key material for a KEK on a schedule (yearly by default in AWS KMS, configurable). Old material is kept so old envelopes still decrypt; new `GenerateDataKey` calls use the new material. Nothing has to be re-encrypted.
- **Re-wrapping** decrypts each stored DEK with the old KEK and re-encrypts it under a new one. That's cheap, because DEKs are 32 bytes.
- **Re-encrypting data** is only needed if a DEK itself may be compromised.

### Bring and hold your own key

Cloud KMS offerings have variants for stricter requirements. **BYOK** imports key material you generated. **Customer-managed HSMs** (AWS CloudHSM, Azure Dedicated HSM) give you single-tenant devices. **External key stores** (AWS XKS, Google Cloud EKM) keep the KEK in an HSM outside the cloud provider, so the provider must call you for every unwrap. Each step adds control and operational burden; most teams are well served by a standard managed KEK with tight IAM and audit.

## 3. Inside HashiCorp Vault

Vault is a secrets broker: clients prove who they are, get a short-lived token with a policy attached, and use it to read static secrets, mint dynamic credentials, or ask Vault to encrypt data for them. Every request is audited. It is the most widely deployed self-hosted option, and its open-source fork **OpenBao** (a Linux Foundation project started after Vault moved to the Business Source License in 2023) shares the same architecture.

### The barrier, the keyring and sealing

Vault stores everything (secrets, tokens, policies, configuration) in a storage backend, today usually its built-in **Integrated Storage**, a Raft-replicated store across 3 or 5 nodes. The storage backend is untrusted: everything Vault writes passes through a **cryptographic barrier** and is encrypted with AES-256-GCM.

The key chain looks like this:

1. Data is encrypted with keys from the **keyring**, which supports rotation (`vault operator rotate`) so old data stays readable.
2. The keyring is encrypted with the **root key** (historically called the master key).
3. The root key is encrypted with the **unseal key**.

When Vault starts, it is **sealed**: it knows where its data is but can't decrypt any of it. It can't even read its own configuration. Unsealing means supplying the unseal key so Vault can decrypt the root key, then the keyring, and start serving.

### Shamir's secret sharing and auto-unseal

With the classic setup, the unseal key is never stored whole. At initialisation Vault splits it using **Shamir's secret sharing** into, by default, 5 shares, any 3 of which can reconstruct it. Each share goes to a different operator, often PGP-encrypted. After a restart, three operators each submit their share. No single person can unseal Vault, and losing two shares is survivable.

The mathematics is neat: the secret is the constant term of a random polynomial of degree 2 (threshold minus one). Each share is one point on the curve. Any 3 points determine a degree-2 polynomial exactly; 2 points reveal nothing about the constant term.

In practice, manual unsealing after every restart is operationally painful, so most production clusters use **auto-unseal**: the root key is encrypted by a cloud KMS key or an HSM, and Vault calls that service at start-up. Shamir shares are replaced by **recovery keys**, which are needed for sensitive operations like generating a new root token but can't unseal Vault. The trust has moved, not disappeared: whoever controls that KMS key can unseal your Vault.

Sealing is also an emergency brake. `vault operator seal` throws away the in-memory root key, and every secret becomes unreadable until a quorum unseals again.

### Authentication: who are you?

Clients log in through an **auth method**, which verifies an identity and issues a Vault **token**. Common ones:

| Auth method | Proof of identity | Typical user |
| --- | --- | --- |
| Kubernetes | The pod's service account JWT, verified via the Kubernetes TokenReview API | Pods |
| AWS / GCP / Azure | A signed cloud identity document or IAM request | VMs, Lambdas, cloud workloads |
| JWT / OIDC | A token from an identity provider (Okta, GitHub Actions, GitLab CI) | Humans via SSO, CI pipelines |
| AppRole | A role ID plus a secret ID, usually delivered by a trusted orchestrator | Legacy apps, on-premises |
| TLS certificates | A client certificate | Machines with existing PKI |

The best methods use an identity the platform already provides, so there's no **secret zero** to distribute. AppRole still needs its secret ID delivered somehow, which is why Vault offers **response wrapping**: the orchestrator receives a single-use wrapping token, and the app unwraps it to get the real secret ID. If an attacker intercepted and used it first, the app's unwrap fails, which raises an alarm.

### Authorisation: policies

Tokens carry **policies**, written in HCL as path-based rules:

```hcl
path "secret/data/payments/*" {
  capabilities = ["read"]
}
path "database/creds/payments-readonly" {
  capabilities = ["read"]
}
path "transit/encrypt/payments" {
  capabilities = ["update"]
}
```

Everything is deny by default. Because every secret, credential and crypto operation is a path, one model governs all of them.

### Leases and dynamic secrets

Almost everything Vault hands out has a **lease**: a TTL after which it is revoked, plus the option to renew or revoke early. That is what makes **dynamic secrets** possible.

With the database secrets engine, Vault holds an admin connection to Postgres. When the payments service reads `database/creds/payments-readonly`, Vault:

1. Runs the role's creation SQL, for example `CREATE ROLE "v-k8s-payments-x7Fq" WITH LOGIN PASSWORD '...' VALID UNTIL '...'; GRANT SELECT ON ALL TABLES IN SCHEMA public TO ...;`.
2. Returns the username and password with a lease, for example 1 hour, renewable up to 24.
3. When the lease expires or is revoked, runs the revocation SQL to drop the role.

Every pod gets its own credentials. If a password leaks, it works only for its remaining TTL; the database logs show exactly which workload it belonged to; and you can revoke every credential under a path with one call (`vault lease revoke -prefix database/creds/payments-readonly`) during an incident. The same pattern exists for AWS (temporary STS credentials), PKI (short-lived X.509 certificates), SSH (signed certificates) and many SaaS systems.

### Transit: encryption as a service

The **transit** engine is Vault acting as a KMS. Applications send plaintext to `transit/encrypt/<key>` and get back ciphertext like `vault:v3:AbC...`, where `v3` is the key version. Keys never leave Vault. Rotating the key creates `v4`; old ciphertexts still decrypt, and `transit/rewrap` upgrades them to the new version without exposing plaintext to the caller. Transit can also generate data keys for envelope encryption, sign and verify, and compute HMACs.

### Audit

Audit devices (file, syslog, socket) log every request and response. Sensitive values are HMAC-ed rather than logged in plain text, so you can check whether a specific token or secret appeared without the log becoming a secrets store. Vault refuses to service requests if it can't write to at least one enabled audit device: no audit trail, no secrets.

## 4. Workload identity, Kubernetes and rotation

### Workload identity: removing secret zero

Every secrets store has a bootstrapping problem. To fetch secrets, the app must authenticate, and if that needs a secret, where does *that* one come from? **Workload identity** answers it by having the platform vouch for the workload, usually with a short-lived signed token, which the workload exchanges for short-lived credentials.

The common implementations follow the same shape:

- **Kubernetes on AWS (IRSA, and the newer EKS Pod Identity).** The kubelet mounts a projected service-account token: a JWT signed by the cluster, scoped to an audience, expiring in about an hour and rotated automatically. The pod's AWS SDK calls STS `AssumeRoleWithWebIdentity` with that JWT. AWS verifies the signature against the cluster's OIDC issuer and returns temporary credentials for the IAM role mapped to that service account.
- **GKE and Azure** offer equivalent workload identity federation, mapping Kubernetes service accounts to cloud identities.
- **CI/CD with OIDC.** GitHub Actions, GitLab CI and others can issue an OIDC token per job, with claims like the repository, branch and environment. A cloud trust policy that says "only `repo:acme/api` on `refs/heads/main` may assume the deploy role" replaces long-lived cloud keys stored as CI secrets. This single change removes one of the most common breach paths in section 1.
- **SPIFFE and SPIRE.** A vendor-neutral standard for service identity. SPIRE agents attest workloads (by Kubernetes metadata, process attributes and node identity) and issue short-lived X.509 certificates (SVIDs) with IDs like `spiffe://acme.com/payments`. Services use them for mutual TLS, and service meshes such as Istio use the same identity format.

The pattern to aim for: platform-attested identity → short-lived token → short-lived credential → audited access. No human ever sees or copies the secret.

### Kubernetes Secrets: what they do and don't protect

A Kubernetes `Secret` is a convenience object, not a vault:

- Values are **base64-encoded, not encrypted**. Anyone who can `get` or `list` Secrets in a namespace can read them all.
- They are stored in etcd **unencrypted by default**. Enable encryption at rest with an `EncryptionConfiguration`, ideally using the **KMS provider** (KMS v2 has been stable since Kubernetes 1.29), which is envelope encryption from section 2 applied to etcd.
- Anyone who can **create pods** in a namespace can mount any Secret in that namespace, so pod-creation rights are effectively secret-read rights.
- Secrets show up in places people forget: Helm release history, GitOps repos, and backups of etcd.

The common patterns for connecting Kubernetes to a real secrets store:

| Pattern | How it works | Trade-off |
| --- | --- | --- |
| External Secrets Operator | Syncs values from AWS/GCP/Azure/Vault into native Secrets | Apps stay unchanged; secrets still land in etcd |
| Secrets Store CSI Driver | Mounts secrets as files in a volume at pod start, fetched by the pod's identity | Avoids etcd unless sync is enabled; files must be re-read on rotation |
| Vault Agent Injector / Vault Secrets Operator | A sidecar or operator logs in with the pod's service account and renders templated files, renewing leases | Supports dynamic secrets well; more moving parts |
| Direct SDK calls | The app fetches secrets itself with workload identity | Most control and freshest values; ties code to the store |

Prefer files over environment variables for anything that rotates, because a file can be re-read without a restart.

### Rotation without downtime

Rotation fails in practice when the old credential stops working before every consumer has the new one. The standard fix is to make two credentials valid at once:

1. Create the new credential (a second API key, or a second database user) while the old one still works.
2. Distribute it: update the secrets store, and let consumers pick it up by re-reading files, refreshing caches, or rolling restarts.
3. Verify that nothing uses the old one, using provider usage logs or database connection logs.
4. Revoke the old credential.

AWS Secrets Manager's managed rotation implements this as an "alternating users" strategy for databases: two users, and each rotation changes the password of the one not currently in use, then switches. Dynamic secrets make the whole problem disappear, since every credential is born with an expiry.

Rotation also has to be **rehearsed**. A secret that has never been rotated usually can't be rotated quickly during an incident, because nobody knows every place it is used.

### Detecting leaks

- **Pre-commit and push protection.** Tools such as gitleaks and TruffleHog, and GitHub's push protection, block commits that contain known secret formats.
- **Provider scanning.** GitHub's secret scanning partner program notifies providers like AWS, Stripe and Slack when their tokens appear in public repos, and many revoke them automatically.
- **Canary tokens.** Plant fake credentials in places attackers look (a `.env` in an old repo, a config on a jump host); any use of them raises an alert.
- **Anomaly alerts** on audit logs: a credential used from a new network or at an unusual volume.

When a leak is found, **rotate first, investigate second**. Revoking the credential stops the bleeding; working out whether it was used can come after.

### A practical checklist

- No secrets in source code or container images; scanning enforced in CI and on push.
- Cloud access from CI and workloads uses OIDC or workload identity, not static keys.
- One central store per environment, with least-privilege policies per service and audit logging on.
- Databases and cloud APIs use dynamic or automatically rotated credentials where supported.
- Application data that needs encryption uses envelope encryption with a managed KEK and encryption context.
- Kubernetes etcd encryption at rest enabled with a KMS provider; Secret and pod-creation RBAC reviewed.
- Every secret has an owner and a rotation runbook, and rotation has been exercised at least once.
- Break-glass access (Vault recovery keys, root tokens, KMS admin) is documented, split between people, and alerted on.

## Discussion questions

1. Place three systems you work with on the maturity ladder. For each, what is the cheapest step up, and what would it take?
2. With auto-unseal, whoever controls the cloud KMS key can unseal Vault. Is that better or worse than Shamir shares held by five people? Under what threat model does each win?
3. An attacker gets read access to your S3 bucket of envelope-encrypted customer files but no IAM permissions. What can they learn? What changes if they also steal a pod's projected service-account token?
4. Design zero-downtime rotation for a third-party API key that the provider lets you have only one of at a time. What's the least-bad option?
5. A developer argues that Kubernetes Secrets with etcd encryption are "good enough" and Vault is overkill. Make the strongest case for each side.
6. Your incident runbook says "rotate first, investigate second". When could rotating immediately make the investigation harder, and how would you mitigate that?

## Sources

Written from the references below. Links were not re-opened while writing this piece.

- [AWS KMS concepts: envelope encryption](https://docs.aws.amazon.com/kms/latest/developerguide/kms-cryptography.html#enveloping) and [encryption context](https://docs.aws.amazon.com/kms/latest/developerguide/encrypt_context.html), AWS docs
- [Envelope encryption](https://cloud.google.com/kms/docs/envelope-encryption), Google Cloud KMS docs
- [Vault architecture](https://developer.hashicorp.com/vault/docs/internals/architecture) and [Seal/Unseal](https://developer.hashicorp.com/vault/docs/concepts/seal), HashiCorp docs
- [Database secrets engine](https://developer.hashicorp.com/vault/docs/secrets/databases) and [Transit secrets engine](https://developer.hashicorp.com/vault/docs/secrets/transit), HashiCorp docs
- [Response wrapping](https://developer.hashicorp.com/vault/docs/concepts/response-wrapping), HashiCorp docs
- [OpenBao](https://openbao.org), Linux Foundation fork of Vault
- [Good practices for Kubernetes Secrets](https://kubernetes.io/docs/concepts/security/secrets-good-practices/) and [Encrypting confidential data at rest](https://kubernetes.io/docs/tasks/administer-cluster/encrypt-data/), Kubernetes docs
- [IAM roles for service accounts](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html), Amazon EKS docs
- [Security hardening with OpenID Connect](https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/about-security-hardening-with-openid-connect), GitHub docs
- [SPIFFE concepts](https://spiffe.io/docs/latest/spiffe-about/spiffe-concepts/), spiffe.io
- [State of Secrets Sprawl report](https://www.gitguardian.com/state-of-secrets-sprawl-report-2025), GitGuardian
- Adi Shamir, "How to Share a Secret", Communications of the ACM, 1979
