---
title: "OAuth 2.0 and OpenID Connect, done safely"
date: 2026-10-28
summary: What OAuth actually delegates, the authorization code flow with PKCE, access and refresh tokens, how OpenID Connect adds login, JWT validation pitfalls, sender-constrained tokens, and what RFC 9700 and OAuth 2.1 changed.
tags: security, APIs
daily: true
---

Almost every "Sign in with Google" button, every third-party app that reads your GitHub repos, and most of the access tokens flowing between microservices trace back to one framework: OAuth 2.0. It's also one of the most misunderstood pieces of infrastructure engineers touch. People use it for login when it wasn't designed for that, pick flows that the standards body now says never to use, and validate tokens in ways that have led to real breaches. This reading builds OAuth up from the problem it solves, then adds OpenID Connect, tokens, the attacks that shaped today's best practice, and the 2025 Security BCP (RFC 9700) that rewrote the defaults.

## 1. The problem: delegated access without passwords

Before OAuth, if a photo-printing site wanted your photos from another service, it asked for your password for that service. That's terrible in every direction: the printing site gets full access to your account, forever, and you can't revoke it without changing your password everywhere it's used. If the printing site is breached, so is your other account.

OAuth's answer is **delegation**. You (the *resource owner*) tell the photo service (which runs an *authorization server* and a *resource server*) that the printing site (the *client*) may read your photos, and nothing else, until you say otherwise. The client never sees your password. Instead it gets an **access token**: a credential that represents a specific, limited grant.

Those four roles appear in every OAuth discussion:

| Role | Who it is | Example |
|---|---|---|
| Resource owner | The user who can grant access | You |
| Client | The app that wants access | A printing site, a CLI, a mobile app |
| Authorization server (AS) | Authenticates the user and issues tokens | accounts.google.com, Okta, Auth0, Keycloak |
| Resource server (RS) | The API that accepts tokens | Google Photos API, your internal API |

OAuth 2.0 was published in 2012 as RFC 6749, with RFC 6750 defining how to send bearer tokens. It replaced OAuth 1.0, which required clients to sign every request with a shared secret, a scheme that was hard to implement correctly. OAuth 2.0 dropped request signing and relied on TLS instead, which made it far easier to adopt and, as we'll see, opened the door to token theft that later extensions had to close.

An important point that's easy to miss: OAuth is an **authorization** framework. An access token tells an API "the bearer may do X." It doesn't tell the client who the user is. Using OAuth alone for login is a known mistake, and section 6 covers the protocol built to fix that.

## 2. Scopes, consent and what a token means

A client asks for access in terms of **scopes**: strings such as `photos.read`, `repo`, or `https://www.googleapis.com/auth/calendar.readonly`. The authorization server shows the user a consent screen ("PrintCo wants to view your photos"), and if they agree, issues a token carrying those scopes.

Scopes are coarse by design. They name kinds of access, not specific objects. For finer control, RFC 9396 (Rich Authorization Requests, 2023) lets a client request a structured `authorization_details` object, such as "initiate one payment of 45 EUR to this account," which open-banking APIs use heavily.

Two more concepts matter for every design:

- **Audience.** Which resource server is the token for? A token minted for the calendar API shouldn't be accepted by the payments API. RFC 8707 (Resource Indicators) lets the client name the target API with a `resource` parameter, and the AS puts it in the token's `aud` claim.
- **Lifetime.** Access tokens are meant to be short-lived (minutes to an hour). Longer-lived access is handled with refresh tokens (section 5).

## 3. The grant types, and which ones survive

OAuth 2.0 defined several ways ("grants") for a client to get a token. Over the following decade, attacks and new platforms (single-page apps, mobile, IoT) changed which ones are considered safe.

| Grant | Use | Status today |
|---|---|---|
| Authorization code | User-facing apps of all kinds | **The default.** With PKCE, for every client |
| Implicit | Browser apps, token returned in the URL fragment | **Deprecated** by RFC 9700; removed in OAuth 2.1 |
| Resource owner password credentials | Client collects the user's password and trades it for a token | **Must not be used** (RFC 9700); removed in OAuth 2.1 |
| Client credentials | Service-to-service, no user involved | Fine, with strong client authentication |
| Device authorization (RFC 8628) | TVs, CLIs, devices without a browser | Fine; watch for phishing of user codes |
| Refresh token | Getting new access tokens without the user | Fine, with rotation or sender-constraining for public clients |

The implicit grant existed because in 2012, JavaScript apps couldn't easily make cross-origin POST requests to a token endpoint. The AS returned the access token directly in the redirect URL fragment. That put tokens in browser history, in referrer headers, and within reach of any script on the page, and there was no way to bind the token to the client that started the flow. Once CORS made POSTs to the token endpoint easy, the reason for implicit disappeared, and the code flow with PKCE replaced it.

The password grant defeats the whole point of OAuth (the client sees the password) and can't support multi-factor authentication or federated login. RFC 9700 says it must not be used.

## 4. The authorization code flow with PKCE

This is the flow to learn, because it's the one you should almost always use.

<!--diagram:oauth-pkce-1028-->

Step by step:

1. **The client generates a secret.** Before redirecting, the client creates a random `code_verifier` (43 to 128 characters) and computes `code_challenge = BASE64URL(SHA-256(code_verifier))`. It also creates a random `state` value. Both stay in the client.
2. **Redirect to the AS.** The client sends the user's browser to the authorization endpoint with `response_type=code`, its `client_id`, the `redirect_uri`, the requested `scope`, the `state`, the `code_challenge`, and `code_challenge_method=S256`.
3. **The user authenticates and consents.** This happens entirely at the AS. The client never sees the password, the MFA prompt, or which identity provider the user picked.
4. **Redirect back with a code.** The AS redirects to the registered `redirect_uri` with a short-lived, single-use **authorization code** and the same `state`. The client checks that `state` matches what it sent, which stops an attacker from injecting their own authorization response (a CSRF attack on the login flow).
5. **Exchange the code.** The client POSTs the code and the original `code_verifier` to the token endpoint, over a direct HTTPS connection (the "back channel"), plus client authentication if it's a confidential client.
6. **The AS checks PKCE.** It hashes the verifier and compares it to the challenge from step 2. Only the party that started the flow knows the verifier. If they match, the AS returns an access token (and usually a refresh token).

### Why the indirection?

The code passes through the browser (the "front channel"), where it could leak through logs, referrers, malicious browser extensions, or on mobile, another app registered for the same URL scheme. But a code alone is useless: redeeming it requires the verifier (and, for confidential clients, the client secret), which never touched the browser. The token itself only ever travels on the back channel.

### What PKCE fixes

PKCE (Proof Key for Code Exchange, pronounced "pixy") was published as RFC 7636 in 2015 to fix **authorization code interception** on mobile. On a phone, a malicious app could register the same custom URL scheme (`com.example.app://callback`) as a legitimate app, receive the redirect, and redeem the code itself, because mobile apps can't keep a client secret. PKCE closes that gap: the malicious app has the code but not the verifier.

It later turned out that PKCE also defends against **code injection**, where an attacker takes a code issued for *their own* account (or one stolen from a victim) and gets it redeemed in someone else's session. Because the code is bound to a challenge from a specific flow, it can't be redeemed in a different one. That's why RFC 9700 now recommends PKCE for **all** clients, including confidential server-side apps that have secrets, and why OAuth 2.1 makes it mandatory for the code flow.

### Redirect URIs must match exactly

Many real OAuth breaches came from loose redirect URI matching: an AS that accepted any URL under `https://example.com/` could be tricked into sending codes to an attacker-controlled path, an open redirector, or a subdomain the attacker took over. RFC 9700 requires exact string matching of redirect URIs (with a narrow exception for localhost ports used by native apps, per RFC 8252).

### Mix-up attacks and the `iss` parameter

A client that works with several authorization servers can be tricked into sending a code from an honest AS to an attacker's AS. RFC 9207 (2022) has the AS include an `iss` parameter in the authorization response so the client can check which AS actually answered. If your client talks to more than one identity provider, check `iss`.

## 5. Access tokens and refresh tokens

### Two formats

An access token is opaque to the client, but the resource server has to validate it. There are two common approaches:

- **Reference tokens.** A random string that means nothing on its own. The RS asks the AS what it means through **token introspection** (RFC 7662), getting back JSON with `active`, `scope`, `sub`, `exp` and so on. Revocation takes effect immediately, but every API call (or every cache miss) costs a round trip to the AS.
- **Self-contained tokens**, almost always **JWTs** (RFC 7519). The token carries its claims and a signature. The RS validates it locally with the AS's public key, fetched from a JWKS endpoint and cached. Validation is fast and works even if the AS is down, but a token can't be revoked before it expires, short of the RS checking a denylist. RFC 9068 (2021) defines a standard JWT profile for access tokens with required claims (`iss`, `exp`, `aud`, `sub`, `client_id`, `iat`, `jti`) and a `typ` header of `at+jwt`.

The usual compromise: JWT access tokens with short lifetimes (5 to 60 minutes) plus refresh tokens for long sessions, so revocation takes effect within one access-token lifetime.

### Refresh tokens

A refresh token lets the client get new access tokens without sending the user through the flow again. It's sent only to the AS, never to resource servers, which shrinks its exposure. But it's long-lived and powerful, so RFC 9700 requires that refresh tokens issued to **public clients** (apps without a secret, such as SPAs and mobile apps) be either **sender-constrained** (section 8) or protected by **refresh token rotation**.

With rotation, every use of a refresh token returns a new one and invalidates the old. If an attacker steals a refresh token and uses it, then the legitimate client later uses the same (now stale) token, the AS sees a refresh token used twice. It can't tell which party is the attacker, so it revokes the whole token family and forces the user to log in again. That turns silent long-term theft into a detectable, bounded event.

### Bearer means bearer

RFC 6750 tokens are **bearer tokens**: whoever holds the token can use it, like cash. That's the reason for nearly every rule in this reading: short lifetimes, narrow scopes and audiences, never putting tokens in URLs, never logging them, and sender-constraining where the risk justifies it.

A real example of what happens when long-lived bearer tokens leak: in April 2022, GitHub disclosed that attackers had used stolen OAuth tokens issued to Heroku and Travis CI (two integrations many organizations had authorized) to download private repositories from dozens of organizations, including npm. The tokens were valid, the scopes were broad, and nothing about them was tied to the integrations' own infrastructure.

## 6. OpenID Connect: login on top of OAuth

OAuth tells an API what a client may do. It doesn't tell the client who the user is. Before OpenID Connect, many apps "logged in" users by getting an access token and calling a profile API. That's dangerous: a malicious app that obtained a token for a user (perhaps legitimately, for its own purposes) could present it to *your* app's login endpoint, and if your app just checks "does this token work against the profile API?", the attacker logs in as that user. The token wasn't issued to your app, but your app can't tell.

**OpenID Connect** (OIDC Core 1.0, finalized by the OpenID Foundation in 2014) adds an authentication layer to OAuth. Request the `openid` scope, and the token response includes an **ID token**: a signed JWT *about the authentication event, addressed to your client*.

Key ID token claims:

| Claim | Meaning | Client must check |
|---|---|---|
| `iss` | Who issued it | Matches the expected provider exactly |
| `aud` | Who it's for | Contains your `client_id` |
| `sub` | Stable user ID at this issuer | Use `iss` + `sub` as the user key, not email |
| `exp`, `iat` | Validity window | Not expired, with small clock skew |
| `nonce` | Echo of a value you sent | Matches, which prevents replay |
| `auth_time`, `acr`, `amr` | When and how the user authenticated | If you need recent or MFA login |

The `aud` check is what fixes the profile-API problem: an ID token issued to another app has a different audience, so your app rejects it.

The **`iss` plus `sub`** rule deserves emphasis. Emails change, can be reassigned, and at some providers aren't verified. Several account-takeover bugs came from apps that linked accounts by email across providers, so an attacker who could register an unverified email at one provider got into the victim's account at the app. Key users by issuer and subject, and treat email as a profile attribute (check `email_verified` if you use it at all).

OIDC adds a few more standard pieces that make integration easier:

- **Discovery.** `/.well-known/openid-configuration` lists the provider's endpoints, supported algorithms and JWKS URL, so clients configure themselves from one URL.
- **UserInfo endpoint.** Returns profile claims for an access token.
- **Dynamic registration** and **logout** specs (front-channel, back-channel and RP-initiated logout), with varying adoption.

A useful rule of thumb: **ID tokens are for the client, access tokens are for APIs.** Never send an ID token to an API as a credential, and never treat an access token as proof of who logged in.

## 7. Validating JWTs without getting burned

JWTs are simple to describe and easy to validate badly. A JWT is three Base64url parts: a header (`{"alg":"RS256","kid":"abc"}`), a payload of claims, and a signature over the first two. Most JWT vulnerabilities come from trusting the header to tell you how to verify the token.

**`alg: none`.** The JWT spec allows unsigned tokens with `"alg":"none"`. In 2015, Tim McLean (writing on the Auth0 blog) showed that several popular libraries would accept a token with `alg: none` and no signature as valid, if the application called a generic `verify(token, key)`.

**Algorithm confusion.** The same research showed a worse bug. A server expects RS256 (RSA, verified with a public key). An attacker sends a token with `"alg":"HS256"` (HMAC, verified with a shared secret) and signs it using the server's *public* key as the HMAC secret. A library that picks the algorithm from the header then verifies the HMAC with the public key and accepts it. Public keys are public, so anyone can forge tokens.

The defense for both is the same: **the verifier decides the algorithm, not the token.** Configure an allowlist of expected algorithms per issuer and reject anything else. RFC 8725 (JWT Best Current Practices, 2020) makes this explicit.

A complete validation checklist for an access token at a resource server:

1. Parse the header, but use it only to select a key by `kid` from the issuer's JWKS, and reject unexpected `alg` values.
2. Verify the signature with that key.
3. Check `iss` equals the expected issuer exactly.
4. Check `aud` includes this API.
5. Check `exp` (and `nbf` if present) with a small clock-skew allowance.
6. Check `typ` is `at+jwt` if you follow RFC 9068, so an ID token can't be passed off as an access token.
7. Check scopes or other authorization claims for the specific operation.
8. Cache the JWKS but refresh it on an unknown `kid`, rate-limited, so key rotation works without letting attackers force endless fetches.

**Signing keys are the crown jewels.** In 2023, Microsoft disclosed that a threat actor it calls Storm-0558 had acquired a Microsoft account (consumer) signing key and used it to forge tokens that were accepted by Exchange Online for enterprise mailboxes, including US government accounts. The US Cyber Safety Review Board's 2024 report on the incident found, among other failures, that the token validation logic didn't properly check that a consumer key wasn't being used for enterprise tokens. Two lessons apply to any system: protect and rotate signing keys like the most sensitive secrets you have (the Oct 26 secrets-management reading covers KMS and HSMs), and validate the issuer and key scope strictly, not just the signature.

## 8. Sender-constrained tokens: mTLS and DPoP

Bearer tokens are only as safe as every place they pass through. **Sender-constrained** tokens fix this by binding the token to a key the client holds, so a stolen token is useless without the private key.

**Mutual TLS (RFC 8705, 2020).** The client authenticates to the AS with a TLS client certificate, and the AS puts the certificate's thumbprint in the token (`cnf.x5t#S256`). The resource server only accepts the token over a TLS connection authenticated with the same certificate. It's strong and widely used in open banking (FAPI profiles), but it needs client certificates and TLS termination that passes certificate details through, which is awkward for browsers and many load-balancer setups.

**DPoP (RFC 9449, 2023).** Demonstrating Proof of Possession works at the application layer. The client generates a key pair (in a browser, ideally a non-extractable WebCrypto key). On each request, it sends a `DPoP` header containing a small JWT signed with its private key, covering the HTTP method, the URL, a timestamp, a unique ID, and (when sending to a resource server) a hash of the access token. The AS binds the issued token to the public key's thumbprint (`cnf.jkt`). The RS checks that each request's proof is signed by that key and matches the request. Servers can also supply a nonce to limit how long a pre-generated proof stays useful.

DPoP is practical where mTLS isn't: SPAs, mobile apps, and anything behind TLS-terminating proxies. It's also the recommended way to protect refresh tokens for public clients without rotation. The cost is a signature per request and some complexity in clients and servers.

## 9. Where tokens live in a browser

Single-page apps are the hardest case. Wherever JavaScript can read a token, a cross-site scripting bug can steal it. The IETF's "OAuth 2.0 for Browser-Based Applications" document (an OAuth working group BCP draft) lays out the options, from most to least secure:

1. **Backend for Frontend (BFF).** A small server-side component on the same site runs the OAuth flow as a confidential client and keeps all tokens on the server. The browser gets only an `HttpOnly`, `Secure`, `SameSite` session cookie, and the BFF proxies API calls, attaching tokens. XSS can still make requests while the page is open, but it can't steal tokens to use elsewhere or later.
2. **Token-mediating backend.** The backend runs the flow but hands short-lived access tokens to the browser, keeping the refresh token server-side.
3. **Browser-only client.** The SPA runs the code flow with PKCE itself and holds tokens in memory (or a service worker or web worker to isolate them). Pair it with DPoP and short lifetimes. Storing tokens in `localStorage` makes them readable by any script on the origin, which is the setup the draft most strongly discourages.

The trend in practice is toward BFF for anything sensitive. It turns the problem back into classic web session security, which browsers are good at.

## 10. Service-to-service and workload identity

Not every token has a user behind it. For a backend calling another backend, the **client credentials** grant is the OAuth answer: the service authenticates to the AS with its own credentials and gets a token scoped to what it may call.

The weak point is how the service authenticates. A static `client_secret` in an environment variable is a long-lived bearer secret with all the problems above. Better options:

- **Private key JWT** (`private_key_jwt`, from OIDC and RFC 7523). The client signs a short assertion with its private key, and the AS verifies it with the registered public key. No shared secret ever leaves the client.
- **mTLS client authentication** (RFC 8705).
- **Workload identity federation.** The service presents a token its platform already gives it (a Kubernetes service-account token, a cloud instance identity, a SPIFFE ID) and exchanges it for an access token. Cloud providers' "workload identity federation" features (for example, letting GitHub Actions jobs get AWS or GCP credentials with no stored secret) are built on OIDC tokens and **token exchange** (RFC 8693). The Oct 26 secrets-management reading goes deeper on this.

Token exchange also handles **delegation chains**: service A received a user's token and needs to call service B on that user's behalf. Rather than forwarding the user's token (which has the wrong audience and probably too much scope), A exchanges it at the AS for a new token, audience-restricted to B, possibly with an `act` (actor) claim recording that A is acting for the user.

## 11. RFC 9700 and OAuth 2.1

After years of attacks and patches spread across many documents, the OAuth working group published two consolidating efforts.

**RFC 9700, "Best Current Practice for OAuth 2.0 Security"** (January 2025, also BCP 240) updates RFC 6749 and the related specs with what a decade of deployment taught. Its main requirements:

- Use the authorization code grant with **PKCE** for all clients; authorization servers must support it.
- Don't use the **implicit** grant or other response types that return access tokens in the authorization response.
- **Must not** use the resource owner password credentials grant.
- Register and match **redirect URIs exactly**.
- Prevent CSRF with PKCE, the OIDC `nonce`, or a one-time `state`.
- Defend against **mix-up attacks** when a client uses multiple authorization servers (for example with the `iss` response parameter).
- **Sender-constrain** or **rotate** refresh tokens for public clients, and preferably sender-constrain access tokens too.
- Restrict access token **privileges and audience** to the minimum needed.
- Don't send access tokens in query strings.
- Prefer asymmetric client authentication (mTLS or `private_key_jwt`) over shared secrets.

**OAuth 2.1** (draft-ietf-oauth-v2-1, still an Internet-Draft at the time of writing) is a rewrite of the core spec that folds in these lessons so newcomers don't need to read a dozen RFCs. It removes the implicit and password grants, requires PKCE for the code flow, requires exact redirect URI matching, forbids bearer tokens in query strings, and constrains refresh tokens for public clients. If you follow RFC 9700 today, you're already effectively doing OAuth 2.1.

One more extension worth knowing: **Pushed Authorization Requests** (PAR, RFC 9126, 2021). Instead of putting all the authorization parameters in the browser redirect, where they can be tampered with or leaked and URLs get long, the client POSTs them to the AS directly and gets back a short `request_uri` to put in the redirect. Financial-grade profiles (FAPI 2.0) require PAR, PKCE and sender-constrained tokens together.

## 12. A practical checklist

When reviewing an OAuth or OIDC integration:

1. Code flow with PKCE (`S256`) everywhere, including server-side apps.
2. No implicit or password grants anywhere, even "just for internal tools."
3. Exact redirect URI registration; `state` or `nonce` checked on every response.
4. For login, validate the **ID token** (`iss`, `aud`, `exp`, `nonce`) and key users by `iss` + `sub`.
5. For APIs, validate access tokens with an algorithm allowlist, correct `iss` and `aud`, and scopes per operation.
6. Short access token lifetimes; refresh tokens rotated or sender-constrained.
7. Tokens never in URLs or logs; BFF for browser apps that handle anything sensitive.
8. Services authenticate with keys or workload identity, not long-lived secrets.
9. Signing keys in a KMS or HSM, rotated, with JWKS caching that tolerates rotation.
10. An inventory of third-party OAuth grants your organization has approved, reviewed regularly (the 2022 GitHub incident started with integrations nobody was watching).

## Key takeaways

- OAuth delegates limited access to a client without sharing passwords. It's authorization, not authentication.
- The authorization code flow with PKCE is the one flow to use for user-facing apps. The code goes through the browser; tokens never do.
- PKCE defends against code interception and code injection, so use it even when you have a client secret.
- Access tokens are bearer credentials: keep them short-lived, narrowly scoped, audience-restricted and out of URLs.
- OpenID Connect adds the ID token, which is addressed to your client. Check `aud` and `nonce`, and key users by `iss` + `sub`.
- Never let a JWT's header choose the verification algorithm.
- DPoP and mTLS bind tokens to keys so stolen tokens are useless.
- RFC 9700 (2025) is the current security baseline; OAuth 2.1 packages the same rules into one spec.

## Discussion questions

1. Your team runs a confidential server-side web app with a client secret. Someone asks why it needs PKCE. Walk through the code injection attack that PKCE prevents even here.
2. JWT access tokens can't be revoked before they expire. How short would you make them for an internal admin API versus a public read API, and what would you add if you needed instant revocation?
3. Refresh token rotation detects reuse but can't tell attacker from victim. What user experience does that produce after a theft, and is it acceptable for a banking app? For a note-taking app?
4. Why is "Sign in with X" by calling a profile API with an access token unsafe, when the token is perfectly valid?
5. The BFF pattern puts tokens on a server and gives the browser a cookie. Which attacks does it stop, and which ones (think CSRF and XSS) does it not?
6. Storm-0558 forged tokens with a stolen key. Beyond key protection, which validation checks would have limited the damage, and where in your own systems might the same gap exist?
7. When would you choose mTLS over DPoP for sender-constraining, and vice versa?

## Sources

- RFC 6749, "The OAuth 2.0 Authorization Framework" (2012), and RFC 6750, "Bearer Token Usage" (2012).
- RFC 9700, "Best Current Practice for OAuth 2.0 Security" (BCP 240, January 2025), https://www.rfc-editor.org/rfc/rfc9700.html
- draft-ietf-oauth-v2-1, "The OAuth 2.1 Authorization Framework," and the OAuth WG spec list at https://oauth.net/specs
- RFC 7636, "Proof Key for Code Exchange" (2015); RFC 8252, "OAuth 2.0 for Native Apps" (2017); RFC 8628, "Device Authorization Grant" (2019).
- RFC 9449, "OAuth 2.0 Demonstrating Proof of Possession (DPoP)" (September 2023), https://www.rfc-editor.org/rfc/rfc9449.html ; RFC 8705, "OAuth 2.0 Mutual-TLS Client Authentication and Certificate-Bound Access Tokens" (2020).
- RFC 7519 (JWT), RFC 8725 (JWT Best Current Practices), RFC 9068 (JWT Profile for Access Tokens), RFC 7662 (Token Introspection), RFC 8693 (Token Exchange), RFC 8707 (Resource Indicators), RFC 9126 (PAR), RFC 9207 (Authorization Server Issuer Identification), RFC 9396 (Rich Authorization Requests).
- OpenID Connect Core 1.0 and Discovery 1.0, OpenID Foundation, https://openid.net/specs/openid-connect-core-1_0.html
- IETF OAuth WG, "OAuth 2.0 for Browser-Based Applications" (draft-ietf-oauth-browser-based-apps).
- Tim McLean, "Critical vulnerabilities in JSON Web Token libraries," Auth0 blog, 2015.
- GitHub blog, "Security alert: Attack campaign involving stolen OAuth user tokens issued to two third-party integrators," April 2022.
- Microsoft Security Response Center, "Results of major technical investigations for Storm-0558 key acquisition" (2023), and the US Cyber Safety Review Board report on the Microsoft Exchange Online intrusion (2024).
- WorkOS, "DPoP (RFC 9449) explained," https://workos.com/blog/dpop-rfc-9449-explained

The RFC 9700 and RFC 9449 publication details were confirmed against the RFC Editor pages on Oct 28, 2026. The other RFC numbers, the 2015 JWT library research and the 2022 and 2023 incidents come from the cited documents as I know them; check the originals before quoting specifics.
