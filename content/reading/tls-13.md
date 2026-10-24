---
title: TLS 1.3 from the handshake up
date: 2026-10-21
summary: The protocol from the first byte to the certificates behind it, plus ECH, post-quantum key exchange and 47-day certificates.
tags: security, networking
---

TLS 1.3 is mostly a deletion: it removed almost every option that had led to a real attack on TLS 1.2, and in doing so made the handshake one round trip shorter. This reading follows the protocol from the first byte of a connection to the certificates behind it, and ends with what changed in 2025 and 2026: Encrypted Client Hello became an RFC, browsers turned on post-quantum key exchange, and certificate lifetimes started shrinking toward 47 days.

## 1. Why TLS 1.3 exists

TLS 1.2 (2008) was not broken, but it was a menu, and attackers kept ordering the bad dishes. Over ten years, almost every famous TLS attack used a feature that was optional, legacy or negotiable:

| TLS 1.2 feature | What went wrong | What TLS 1.3 did |
| --- | --- | --- |
| RSA key exchange (client encrypts the secret to the server's RSA key) | No forward secrecy: steal the server key later and you can decrypt every recorded session. Also Bleichenbacher-style oracles (ROBOT, 2017). | Removed. Every handshake uses ephemeral Diffie-Hellman. |
| CBC mode with MAC-then-encrypt | Padding oracles: BEAST, Lucky Thirteen, POODLE. | Removed. Only AEAD ciphers. |
| RC4, 3DES, export ciphers | Weak or deliberately weakened (FREAK, Logjam, Sweet32). | Removed. Five cipher suites remain. |
| Compression | CRIME: compressed length leaks secrets. | Removed. |
| Renegotiation | Injection attacks and complex state machines. | Removed; replaced by a simple key update. |
| Plaintext handshake after Hello | Certificates and extensions visible to anyone on the path. | Everything after ServerHello is encrypted. |

The other problem was speed. A TLS 1.2 handshake took **two round trips** before the client could send its first request. On a 100 ms mobile link that is 200 ms of nothing, on top of the TCP handshake.

TLS 1.3 was published as [RFC 8446](https://www.rfc-editor.org/rfc/rfc8446) in August 2018, after 28 drafts and four years of work. It does two things at once: **one round trip** for a full handshake (zero for a resumed one), and a much smaller design that cryptographers could actually analyze. It was the first TLS version formally verified during design, with tools like Tamarin and ProVerif finding real bugs in drafts before they shipped.

The guiding idea is worth naming: **every option is attack surface.** A secure protocol is not one that supports good choices, it is one that has no bad ones to fall back to.

## 2. The five building blocks

The whole protocol is assembled from five primitives, and once you know what each one is for, the handshake reads like a sentence.

**1. Ephemeral Diffie-Hellman (ECDHE).** Client and server each make a fresh key pair, swap public keys, and each compute the same shared secret without ever sending it. In practice this is X25519 or P-256. "Ephemeral" means the private keys are thrown away after the handshake. That is what gives **forward secrecy**: even if the server's long-term key leaks next year, today's traffic stays private, because the key that protected it no longer exists anywhere.

**2. Signatures.** Diffie-Hellman alone proves nothing about *who* you are talking to; a man in the middle can do DH with both sides. So the server signs the handshake with the private key behind its certificate (ECDSA, RSA-PSS or Ed25519). The signature is the only job the certificate's key does in TLS 1.3. It never encrypts anything.

**3. AEAD encryption.** Authenticated Encryption with Associated Data encrypts and authenticates in one operation, so there is no separate MAC to get in the wrong order. TLS 1.3 allows only AEADs. The common suites are:

- `TLS_AES_128_GCM_SHA256` (the default almost everywhere)
- `TLS_AES_256_GCM_SHA384`
- `TLS_CHACHA20_POLY1305_SHA256` (fast on phones without AES hardware)

Notice what a 1.3 suite name no longer contains: the key exchange and signature algorithm. Those are negotiated separately through extensions, which cut the old list of hundreds of combined suites to a handful.

**4. HKDF.** A key derivation function that turns one secret into many independent keys. It has two steps: *Extract* squeezes possibly uneven input (like a DH output) into a uniform secret, and *Expand* stretches that into as many labelled keys as needed. Section 4 is entirely HKDF.

**5. The transcript hash.** A running hash of every handshake message sent so far. Signatures and the final MACs cover this hash, so if an attacker changed any byte of any earlier message, such as removing a strong cipher from the client's list, the hashes on the two sides would differ and the handshake would fail. This is the protocol's defense against downgrade and tampering, in one mechanism.

## 3. The 1-RTT handshake, message by message

The trick that saves a round trip is that the client **guesses** which key exchange the server will pick and sends its Diffie-Hellman public key in the very first message. The server can then compute the shared secret immediately and encrypt everything it sends back.

<!--diagram:61bea739-4f13-->

Only the two Hello messages travel in plaintext. Everything the server says after ServerHello is already encrypted, and the request goes out with the client's Finished.

### Flight 1: ClientHello (plaintext)

The client sends:

- **random**: 32 fresh random bytes.
- **cipher_suites**: the AEAD + hash pairs it supports.
- **supported_versions** extension: lists TLS 1.3. (The old `version` field is frozen at TLS 1.2 for compatibility; see section 7.)
- **supported_groups**: the DH groups it supports, such as X25519 and P-256.
- **key_share**: the actual public key for one or two of those groups. This is the guess.
- **signature_algorithms**: which signatures it can verify.
- **server_name (SNI)**: the hostname, so a server hosting many sites knows which certificate to use. Unencrypted unless ECH is used (section 7).
- Optionally **pre_shared_key** and **early_data** for resumption (section 5).

### Flight 2: the server's reply (mostly encrypted)

1. **ServerHello** (plaintext): picks the cipher suite and returns its own **key_share** in the same group. Both sides can now compute the DH shared secret and derive **handshake traffic keys**.
2. **EncryptedExtensions**: everything that is not needed to set up the keys, such as ALPN (`h2` or `http/1.1`). From here on, all messages are encrypted.
3. **CertificateRequest** (optional): only for mutual TLS.
4. **Certificate**: the server's certificate chain.
5. **CertificateVerify**: a signature, with the certificate's private key, over the transcript hash so far. This proves the server owns the certificate *and* that it saw the same ClientHello the client sent.
6. **Finished**: an HMAC over the whole transcript, keyed from the handshake secret. It confirms the key exchange was not tampered with.

The server can start sending application data right after its Finished, before hearing back. This is sometimes called "0.5-RTT" data. It is useful for things like server push, but the server does not yet know who the client is.

### Flight 3: client Finished

The client checks the certificate chain and the CertificateVerify signature, verifies the server's Finished, and sends its own **Finished** (plus Certificate and CertificateVerify if mutual TLS was requested). It can send its HTTP request in the same flight. Total cost: **one round trip** before the request goes out.

### When the guess is wrong: HelloRetryRequest

If the client sent a key share for a group the server does not want, the server replies with a **HelloRetryRequest** naming the group it prefers. The client sends a new ClientHello with the right key share. This costs an extra round trip, which is why clients pick their key shares carefully. It is also relevant to post-quantum rollout: large post-quantum key shares make the guess more expensive to get wrong.

### After the handshake

Two post-handshake messages matter:

- **NewSessionTicket**: the server gives the client a ticket for resuming later (section 5).
- **KeyUpdate**: either side can rotate the traffic keys on a long-lived connection. This replaces renegotiation with a much smaller mechanism.

## 4. The key schedule

TLS 1.3 never uses one secret for two jobs. It runs a chain of three HKDF-Extract steps, mixing in a new input at each stage, and expands each stage into labelled keys for exactly one purpose.

1. **Early Secret** = Extract(salt 0, PSK). With no resumption, the PSK is a string of zeros. This stage produces the keys for 0-RTT early data and the binder that proves the client holds the PSK.
2. **Handshake Secret** = Extract(Derive(Early Secret), ECDHE shared secret). This stage produces `c hs traffic` and `s hs traffic`, the client and server **handshake traffic secrets** that encrypt everything from EncryptedExtensions to Finished.
3. **Master Secret** = Extract(Derive(Handshake Secret), zeros). This stage produces `c ap traffic` and `s ap traffic`, the **application traffic secrets** for the actual data, plus the `res master` secret used to mint resumption tickets and an `exp master` secret for exporters.

Each `Derive-Secret` call also mixes in the **transcript hash** at that point. So the application keys depend not only on the DH output but on every byte of the handshake that came before. Two sides that saw different handshakes cannot end up with the same keys.

Why have separate keys per direction and per phase?

- **Separation of concerns.** A bug that leaks handshake keys does not leak application keys.
- **Clean proofs.** Each key is used for exactly one thing, which is what made the formal analysis tractable.
- **Key updates.** A KeyUpdate replaces a traffic secret with `HKDF-Expand-Label(secret, "traffic upd", "", Hash.length)`. The old key is deleted, so compromising the current key does not reveal earlier traffic on the same connection.

The Finished message is computed with a `finished_key` derived from each side's handshake traffic secret. It is an HMAC over the transcript hash. That one MAC is what binds the key exchange, the certificate and every negotiated parameter together.

## 5. Resumption and 0-RTT

Resumption skips the certificate work on a repeat visit, and 0-RTT goes further by sending data in the very first packet, at the cost of letting an attacker replay that data.

### Pre-shared keys and tickets

After a handshake, the server sends one or more **NewSessionTicket** messages. Each holds an opaque ticket (usually the resumption secret encrypted under a key only the server knows) and a lifetime, which RFC 8446 caps at seven days. The client stores the ticket together with a **PSK** derived from `res master`.

Next time, the client includes the ticket in its ClientHello's `pre_shared_key` extension, plus a **binder**: an HMAC proving it actually knows the PSK, not just the ticket. There are two modes:

- **psk_dhe_ke**: PSK plus a fresh ECDHE exchange. Keeps forward secrecy. This is what browsers use.
- **psk_ke**: PSK only, no DH. Cheaper but loses forward secrecy if the ticket key leaks. Rarely appropriate.

Resumption skips the Certificate and CertificateVerify messages and the signature check, which is the expensive part for both sides.

### 0-RTT early data

With a PSK, the client can derive keys from the **Early Secret** before hearing from the server at all. It then sends its HTTP request alongside the ClientHello. The server can answer in its first flight. For a returning user this removes a whole round trip.

The catch is **replay**. Early data is encrypted only under the PSK, with no fresh input from the server. An attacker who records the first packet can send it again, and a server cannot always tell. If the early data was `POST /transfer?amount=100`, that is a real problem.

RFC 8446 is blunt about it: TLS does not protect 0-RTT data against replay, so the application must decide what is safe. In practice:

- Servers accept 0-RTT only for **idempotent** requests, typically GET without side effects.
- Servers can keep a cache of recently seen ClientHellos or use single-use tickets, which limits replay within one server or cluster but is hard to do globally.
- HTTP has a status code for this, **425 Too Early** ([RFC 8470](https://www.rfc-editor.org/rfc/rfc8470)), so a backend can tell a TLS-terminating proxy "retry this after the handshake completes".
- Early data also lacks forward secrecy with respect to the ticket key: whoever holds the server's ticket encryption key can decrypt it.

The general lesson goes beyond TLS: **latency gains often come from skipping a step that provided a guarantee.** 0-RTT is safe exactly when the request did not need that guarantee.

## 6. Certificates and trust

The handshake proves the server holds a private key; the certificate is what ties that key to a name, and the hard problems of the web PKI are all about that tie: who may vouch for it, how a client checks it, and what happens when it goes wrong.

### The chain

A server sends its **leaf certificate** (for `example.com`) and one or more **intermediate** certificates. The client already has a set of **root** certificates in its trust store, shipped by the browser or OS. Validation walks the chain upward:

1. Each certificate's signature verifies with the public key of the one above it, ending at a trusted root.
2. Each certificate is within its validity dates.
3. The leaf's **Subject Alternative Name** matches the hostname the client asked for. (The old Common Name field is ignored by modern clients.)
4. Key usage and constraints allow the certificate to be used for a TLS server.

Roots are kept offline and sign intermediates; intermediates sign leaves. If an intermediate is compromised it can be revoked without replacing a root that is baked into billions of devices.

A classic production bug: a server that sends only its leaf and not the intermediate. Browsers often recover by fetching or caching the intermediate, so the site looks fine, while curl, Java clients or mobile apps fail. Always serve the full chain (minus the root).

### Revocation, and why it mostly failed

When a private key leaks, the certificate needs to be revoked before it expires. The two classic mechanisms were:

- **CRLs**: CAs publish lists of revoked serial numbers. They grew large and were slow to fetch.
- **OCSP**: the client asks the CA "is this certificate still good?" during the handshake. It added latency, leaked browsing history to the CA, and when the responder was down, clients **soft-failed** and accepted the certificate anyway, which defeats the point.

**OCSP stapling** let the server fetch the OCSP response itself and attach it to the handshake, fixing privacy and latency, but adoption stayed patchy. The industry has largely given up: browsers ship their own compressed revocation sets (Chrome's CRLSets, Firefox's CRLite), and Let's Encrypt, the largest CA, [shut down its OCSP service in 2025](https://isrg.org/post/ending-ocsp/) in favor of CRLs.

### Short lifetimes instead of revocation

If revocation is unreliable, the next best thing is certificates that expire quickly. In April 2025 the CA/Browser Forum approved ballot SC-081, which [shrinks the maximum certificate lifetime](https://www.globalsign.com/en/blog/47-day-tls-certificate-lifespan-what-it-means-for-security-teams) from 398 days to 200 days from March 15, 2026, and in steps down to **47 days** by March 15, 2029. The period a CA can reuse a domain-validation check drops to 10 days. Let's Encrypt also began offering six-day certificates.

The practical consequence for backend engineers: certificate renewal must be fully automated (ACME, cert-manager, a cloud certificate service). A manual renewal calendar that worked with yearly certificates breaks at 47 days.

### Certificate Transparency

The other answer to "what if a CA misbehaves" is visibility. With **Certificate Transparency (CT)**, every publicly trusted certificate must be logged in public, append-only, Merkle-tree logs. The certificate carries **signed certificate timestamps (SCTs)** proving it was logged, and Chrome and Safari reject certificates without them. Domain owners can monitor the logs (for example via crt.sh) and see any certificate issued for their names. Several CA distrust decisions started with something spotted in CT logs.

## 7. Deploying it in the real world

The cryptography of TLS 1.3 was settled early; most of the remaining years of work went into getting it past the boxes that sit between clients and servers.

### Middleboxes and ossification

When browsers first tried early TLS 1.3 drafts on real networks, a noticeable share of connections failed. Firewalls, antivirus proxies and corporate inspection boxes had hard-coded what a TLS handshake "should" look like, and dropped anything new. This is **protocol ossification**: a field that was meant to be flexible becomes frozen because deployed software depends on its old values.

The fix was to make TLS 1.3 *look* like TLS 1.2 resumption on the wire:

- The `legacy_version` field in ClientHello and ServerHello stays at `0x0303` (TLS 1.2). The real version is negotiated in the **supported_versions** extension.
- The client sends a fake `legacy_session_id`, and both sides send a meaningless **ChangeCipherSpec** message, because middleboxes expected one.

To stop the new extension points from ossifying too, Google introduced **GREASE** ([RFC 8701](https://www.rfc-editor.org/rfc/rfc8701)): clients randomly include reserved, meaningless values in version, cipher and extension lists. Any server or middlebox that chokes on unknown values breaks early and visibly, instead of years later when a real new value arrives.

### Downgrade protection

If an attacker can make the client and server fall back to TLS 1.2, they bypass everything 1.3 fixed. The transcript hash protects a 1.3 handshake, but a 1.2 handshake does not cover the 1.3 signal. So a TLS 1.3 server that is forced to negotiate TLS 1.2 puts a **sentinel** in the last eight bytes of its ServerHello random: the ASCII string `DOWNGRD` followed by `0x01`. That random value *is* signed in TLS 1.2. A 1.3-capable client that sees the sentinel knows someone tampered with the negotiation and aborts.

### Encrypted Client Hello (ECH)

TLS 1.3 encrypts the certificate, but the ClientHello is still plaintext, and its **SNI** field reveals which site you are visiting to anyone on the path. That is how many networks filter and censor.

**ECH** fixes this by splitting the ClientHello in two. An **outer** ClientHello carries a harmless public name (for example the CDN's own domain). An **inner** ClientHello, with the real SNI and other sensitive extensions, is encrypted with HPKE to a public key the server publishes in DNS (in the HTTPS resource record). The server, typically a large CDN fronting many sites, decrypts the inner hello and continues with it. Observers see only that you connected to the CDN.

ECH was published as [RFC 9849](https://rfc-editor.org/rfc/rfc9849.html) in March 2026, after years of deployment as a draft by Cloudflare, Chrome and Firefox. It only gives real privacy when many sites share one outer name, so it is mainly a CDN-scale feature. It also needs encrypted DNS, since a plaintext DNS lookup would leak the name anyway.

### Post-quantum key exchange

A large enough quantum computer would break both X25519 and RSA. Nobody has one yet, but traffic recorded today could be decrypted later: **harvest now, decrypt later**. Key exchange is the urgent part, since signatures only need to resist forgery at the moment of the handshake.

NIST standardized **ML-KEM** (formerly Kyber) as FIPS 203 in August 2024. TLS deploys it as a **hybrid**: the key share combines X25519 and ML-KEM-768, and the shared secret mixes both, so the connection is safe as long as either one holds. Chrome first enabled a hybrid with the earlier Kyber draft in 2024 and [switched to the standardized X25519MLKEM768](https://thehackernews.com/2024/09/google-chrome-switches-to-ml-kem-for.html) in Chrome 131. Firefox, Cloudflare and major TLS libraries followed.

The main cost is size. An ML-KEM-768 public key is 1,184 bytes, against 32 bytes for X25519. The ClientHello no longer fits in one packet, and some middleboxes that assumed it would (the ossification story again) broke. This is also why the key-share guess from section 3 matters more now: guessing wrong costs a HelloRetryRequest with large messages.

Post-quantum **signatures** and certificates are harder still, because the signatures are larger and appear many times in a certificate chain. That migration is still being designed.

## 8. Ideas to carry away, and questions for our discussion

Five ideas from TLS 1.3 apply well beyond cryptography:

1. **Every option is attack surface.** Removing choices made the protocol both faster and easier to prove secure.
2. **Bind everything to a transcript.** One hash over the whole conversation, covered by a signature and a MAC, defeats a whole family of tampering and downgrade attacks.
3. **One key, one job.** The key schedule separates keys by direction and phase, so a leak in one place stays contained.
4. **Latency often costs a guarantee.** 0-RTT is fast because it skips the server's fresh input, and that is exactly why it can be replayed.
5. **Design for the network that exists.** Middlebox compatibility mode and GREASE exist because deployed software freezes whatever it can see.

Think about these before we talk:

- Why does sending the key share in the first message save a round trip, and what does it cost when the guess is wrong?
- In TLS 1.3, what exactly does the certificate's private key sign, and why is that enough to stop a man in the middle?
- Which of your service's endpoints would be safe to accept as 0-RTT early data, and which would not?
- If revocation does not really work, what is the actual exposure window for a leaked key today, and how does a 47-day lifetime change it?
- Where in your own infrastructure is TLS terminated (load balancer, sidecar, app)? What travels in plaintext after that point?
- Why is post-quantum key exchange being deployed years before post-quantum signatures?

## Sources

- IETF, [RFC 8446: The Transport Layer Security (TLS) Protocol Version 1.3](https://www.rfc-editor.org/rfc/rfc8446), August 2018
- Cloudflare, [A Detailed Look at RFC 8446 (a.k.a. TLS 1.3)](https://blog.cloudflare.com/rfc-8446-aka-tls-1-3/), 2018
- Michael Driscoll, [The Illustrated TLS 1.3 Connection](https://tls13.xargs.org/), every byte of a real handshake explained
- IETF, [RFC 9849: TLS Encrypted Client Hello](https://rfc-editor.org/rfc/rfc9849.html), March 2026
- IETF, [RFC 8470: Using Early Data in HTTP](https://www.rfc-editor.org/rfc/rfc8470) and [RFC 8701: GREASE](https://www.rfc-editor.org/rfc/rfc8701)
- ISRG, [Ending OCSP Support in 2025](https://isrg.org/post/ending-ocsp/)
- GlobalSign, [47-day TLS certificate lifespan](https://www.globalsign.com/en/blog/47-day-tls-certificate-lifespan-what-it-means-for-security-teams)
- The Hacker News, [Google Chrome switches to ML-KEM](https://thehackernews.com/2024/09/google-chrome-switches-to-ml-kem-for.html), September 2024
- Book for going deeper: Ivan Ristić, *Bulletproof TLS and PKI* (2nd edition, Feisty Duck)

Some details, such as the TLS 1.2 attack history and ML-KEM key sizes, are from my own knowledge of the specifications rather than a page opened today.
