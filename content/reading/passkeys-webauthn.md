---
title: "Passkeys and WebAuthn: logins that can't be phished"
date: 2026-11-04
summary: Why passwords and one-time codes keep failing, how WebAuthn's public-key ceremonies work, what makes passkeys phishing-resistant, synced versus device-bound credentials, attestation, account recovery, and how to roll passkeys out in a real product.
tags: security, APIs
daily: true
---

Most account takeovers don't involve clever cryptography. They involve a reused password, a convincing fake login page, or a one-time code typed into the wrong site. Two decades of fixes (complexity rules, rotation policies, SMS codes, authenticator apps) have each helped a little and moved attackers to the next weak point. Passkeys, built on the W3C's WebAuthn standard and the FIDO Alliance's specifications, are the first mainstream login method designed so that a user *can't* hand their credential to the wrong site, even if they're fooled. This reading explains why that's true, how the protocol works, the trade-offs between kinds of passkeys, and what changes when you add them to a real product.

## 1. What's wrong with what we have

**Passwords** are shared secrets. The user knows it and the server stores something derived from it, so a password can be stolen from either side: phished from the user, guessed, or cracked from a breached database. Because people reuse passwords, one breach leads to **credential stuffing**: attackers try leaked username and password pairs on other sites, at scale. Verizon's annual Data Breach Investigations Report has consistently found stolen credentials among the most common ways attackers get in.

**One-time codes** (SMS, email, authenticator app TOTP codes) add a second factor, which stops credential stuffing. But they're still something the user reads and types, so a phishing page can simply ask for the code too and relay it to the real site in real time. Off-the-shelf "adversary in the middle" phishing kits do exactly this, proxying the whole login and stealing the session cookie afterward. SMS adds its own weaknesses, like SIM-swap attacks.

**Push approvals** ("Is this you signing in? Yes / No") are easy to use but vulnerable to **MFA fatigue**: attackers trigger prompts repeatedly until a tired user taps yes. Uber's 2022 breach reportedly involved exactly this, combined with social engineering over chat.

The common thread: in every case, the secret or approval is something **the user can be tricked into giving to the wrong party**. Fixing that requires taking the user's judgment out of the loop for the one question they're worst at answering: "Is this really the site I think it is?"

## 2. The core idea: public keys bound to origins

WebAuthn (Web Authentication) replaces shared secrets with **public-key cryptography**:

- When a user registers, their device (the **authenticator**) generates a new key pair just for that site. The **private key** stays on the authenticator. The **public key** goes to the site, which stores it.
- To sign in, the site sends a random **challenge**. The authenticator signs it with the private key, and the site verifies the signature with the stored public key.

That alone fixes the server-side problems. The server stores only public keys, so a database breach leaks nothing an attacker can log in with. Each site gets a different key pair, so there's nothing to reuse across sites. And there's no secret to guess.

The phishing resistance comes from a second property: **the browser, not the user, tells the authenticator which site is asking**. Every key pair is scoped to a **relying party ID** (RP ID), normally the site's domain, such as `example.com`. When a page calls the WebAuthn API, the browser checks that the page's origin matches the RP ID, and includes the real origin in the data that gets signed. A phishing page at `examp1e-login.com` can't ask for `example.com`'s credential, because the browser won't allow it, and the authenticator has no credential for the phishing domain. Even if the user is completely fooled by the page, there's nothing they can hand over.

That is the fundamental shift. Passwords and codes rely on the user to recognize the right site. WebAuthn relies on the browser, which checks the actual origin and never gets fooled by a lookalike domain.

## 3. The ceremonies

WebAuthn calls its two flows **ceremonies**, since they involve the user as well as machines.

### Registration

1. The server creates options: a random challenge, the RP ID and name, a user handle (an opaque, stable user ID, not an email address), the algorithms it accepts (for example ES256 and RS256), and preferences such as whether to create a discoverable credential and whether to require user verification.
2. The page calls `navigator.credentials.create({ publicKey: options })`.
3. The browser checks the RP ID against the page's origin and asks the authenticator to create a credential. The authenticator asks the user to verify, with a fingerprint, face, or device PIN.
4. The authenticator generates a key pair and returns the **credential ID**, the **public key**, and optionally an **attestation** statement (section 6), all wrapped with `clientDataJSON`, which records the challenge, the origin and the ceremony type.
5. The server checks that the challenge matches what it issued, that the origin is its own, and that the RP ID hash in the authenticator data matches, and then stores the credential ID and public key against the user account.

### Authentication

<!--diagram:passkey-signin-1104-->

1. The server creates a fresh random challenge (and, if it already knows the user, optionally a list of their credential IDs).
2. The page calls `navigator.credentials.get({ publicKey: options })`.
3. The authenticator finds a credential for this RP ID, verifies the user, and signs the authenticator data plus a hash of `clientDataJSON` (which contains the challenge and origin).
4. The server looks up the public key by credential ID, verifies the signature, and checks the challenge, origin, RP ID hash and flags.

Two flags in the authenticator data matter for security decisions:

- **User Present (UP)**: someone interacted with the authenticator, such as a touch.
- **User Verified (UV)**: the authenticator verified *who* that someone is, with biometrics or a PIN. With UV, a single passkey is effectively **multi-factor**: something you have (the device holding the key) plus something you are or know (the biometric or PIN that unlocks it). That's why passkeys can replace both the password and the second factor.

Authenticators also return a **signature counter** that increases with use, which can help detect cloned keys. Synced passkeys (next section) typically report zero, so servers shouldn't rely on it.

### Libraries do the heavy lifting

The server side involves parsing CBOR, COSE keys and several attestation formats. Don't write this by hand. Use a maintained library: SimpleWebAuthn (TypeScript), `py_webauthn` (Python), `webauthn4j` or Yubico's `java-webauthn-server` (Java), `go-webauthn` (Go), `webauthn-rs` (Rust). They handle the verification steps the spec requires, which are easy to get subtly wrong.

## 4. From security keys to passkeys

WebAuthn became a W3C Recommendation in March 2019 (Level 1), followed by Level 2 in 2021. W3C has since advanced Level 3, which adds features that matter for passkeys, such as hints and better support for related origins. Together with the FIDO Alliance's **CTAP** protocol (how browsers talk to external authenticators over USB, NFC or Bluetooth), WebAuthn makes up **FIDO2**.

In the early years, WebAuthn was mostly used with **hardware security keys** such as YubiKeys, typically as a second factor. Google reported in 2018 that after requiring security keys for its employees, it had no confirmed account takeovers via phishing. But security keys have a usability problem: lose the key and you lose the credential, so users need backups, and few consumers will buy and manage two keys.

**Passkeys** are the consumer-friendly version. In May 2022, Apple, Google and Microsoft jointly committed to support passkeys, and each shipped them over the following year. A passkey is simply a WebAuthn credential that's:

- **Discoverable** (also called a resident key): stored on the authenticator together with the user handle, so the user doesn't need to type a username first. The site can call `get()` without listing credential IDs, and the authenticator offers the matching accounts.
- **Usually synced**: backed up and synchronized across the user's devices by their platform or password manager, such as iCloud Keychain, Google Password Manager, or third-party managers like 1Password and Bitwarden. Lose your phone, sign in on the new one, and your passkeys come with you.

"Passkey" is now used for both synced credentials and **device-bound** ones (like those on a hardware security key), but the distinction matters for some decisions.

### Synced versus device-bound

| | Synced passkeys | Device-bound passkeys |
|---|---|---|
| Where the key lives | Encrypted in a cloud keychain, on all the user's devices | On one device or security key only |
| Survives device loss | Yes | No, so you need backups |
| Phishing-resistant | Yes | Yes |
| Security depends on | The sync provider's account security and end-to-end encryption | Physical possession of the device |
| Attestation | Usually none | Often available |
| Best for | Consumer accounts | High-assurance workforce and admin access |

The authenticator data includes **backup eligibility (BE)** and **backup state (BS)** flags, so a server can tell whether a credential is a synced passkey. Some high-security applications require device-bound credentials for privileged accounts and accept synced passkeys elsewhere.

The trade-off with synced passkeys is real but usually favorable. The private key is no longer confined to one piece of hardware, so the security of the sync account (say, the user's Apple or Google account) matters. In return, account recovery is far easier, which removes the biggest reason people fall back to weaker methods. For nearly all consumer accounts, a synced passkey is dramatically better than a password plus SMS code.

## 5. Signing in across devices

What if your passkey is on your phone but you're signing in on a borrowed laptop? WebAuthn's **hybrid transport** (formerly caBLE) handles this. The laptop's browser shows a QR code. You scan it with your phone, which uses the QR data plus a **Bluetooth Low Energy proximity check** to confirm it's physically near the laptop, and then completes the ceremony through an encrypted tunnel relayed over the internet. The phone signs; the laptop never sees the private key.

The Bluetooth check is what keeps this phishing-resistant: an attacker can't send you a QR code from across the world and have your phone sign in for them, because your phone has to detect the laptop nearby. After a hybrid sign-in, many sites offer to create a new passkey on the laptop itself so next time is direct.

## 6. Attestation: do you need to know which authenticator it is?

During registration, an authenticator can include an **attestation statement**: a signature by a key embedded by the manufacturer, which proves the credential was created by, say, a particular model of YubiKey. Servers can check it against the FIDO Metadata Service to learn the authenticator's certification level.

For consumer sites, the usual advice is to **not require attestation** (request `attestation: "none"`). Synced passkey providers generally don't provide it, requiring it would block most users, and it adds privacy concerns. Attestation matters in regulated or enterprise settings where policy says "only these approved hardware keys may be used for admin accounts." There, you request attestation and enforce an allowlist.

## 7. The user experience is the hard part

The cryptography is solved. Adoption depends on UX, and a few patterns have emerged.

**Autofill (conditional UI).** Instead of a separate "Sign in with a passkey" button, the site calls `navigator.credentials.get()` with `mediation: "conditional"` when the login page loads, and adds `autocomplete="username webauthn"` to the username field. The browser then shows available passkeys in the normal autofill dropdown alongside saved passwords. Users who have a passkey pick it; others type as usual. It makes passkeys feel like the familiar autofill rather than a new concept.

**Offer creation at the right moment.** Prompt users to create a passkey right after a successful sign-in, after a password reset, or in account security settings, not in the middle of checkout. Newer platform features let a password manager automatically upgrade an existing password account to a passkey when the user signs in, which removes the prompt entirely for some users.

**Name things clearly.** Show users which passkeys they have ("iCloud Keychain, created Oct 3"), using the authenticator's AAGUID (an identifier for the authenticator type) to display a provider name and icon, and let them delete ones they no longer use.

**Keep the user handle opaque.** The user handle is stored on the authenticator and returned on sign-in. Use a random ID, not an email address, since it may be visible to the authenticator and shouldn't leak personal data.

Large deployments have reported encouraging results. Google, for example, has published that passkey sign-ins succeed more often and faster than passwords, and companies like Amazon, PayPal and others have reported similar improvements in their own announcements. Results vary by product, so measure your own success and fallback rates.

## 8. Account recovery is where security leaks back out

The strongest login method is only as strong as the weakest way to get into the account. If a user who "lost their passkey" can recover the account with an SMS code or a security question, an attacker can too, and phishing resistance disappears.

Recovery design is a policy choice with no perfect answer:

- **Encourage multiple passkeys**, for example a synced passkey plus a second one on a different platform or a security key. Synced passkeys already survive device loss, which handles most cases.
- **Treat recovery as a high-risk event**: send notifications to all known channels, add delays before sensitive changes take effect, and require stronger proof for high-value accounts.
- **Be honest about fallbacks.** Many consumer services keep a password or email-link fallback during the transition. That's reasonable, but understand it means accounts are only as phishing-resistant as the fallback. For high-risk users, offer an option to disable weaker methods entirely (Google's Advanced Protection Program works this way).
- **For workforce accounts**, use the identity provider's verified onboarding and recovery processes, ideally in person or with a manager's verification, and issue device-bound keys for privileged roles.

## 9. Portability and lock-in

An early worry about synced passkeys was lock-in: if your passkeys live in Apple's keychain, can you move them to Google's or to a third-party manager? In October 2024, the FIDO Alliance published drafts of the **Credential Exchange Protocol (CXP)** and **Credential Exchange Format (CXF)**, which define a secure way to move passkeys and other credentials between providers, encrypted end to end during transfer, with several major password managers participating. Support has since started rolling out in some providers. It's an important piece for long-term trust, since users shouldn't have to choose between security and the freedom to switch tools.

Separately, platforms now let third-party password managers act as passkey providers on iOS, macOS, Android and Windows, so users aren't forced into the platform's own keychain in the first place.

## 10. Rolling out passkeys: a practical plan

For a typical web product:

1. **Add passkeys alongside passwords**, not instead of them, at first. Store multiple credentials per user.
2. **Use a maintained library** and follow its verification steps: challenge, origin, RP ID hash, UV flag, signature.
3. **Choose the RP ID carefully.** It's usually your registrable domain (`example.com`) so credentials work across subdomains. Changing it later invalidates existing passkeys. If you need to share credentials across different domains you own, look at related origin requests (a Level 3 feature) rather than working around it.
4. **Require user verification** (`userVerification: "required"`) if you want passkeys to count as multi-factor.
5. **Turn on conditional UI** on your login page.
6. **Prompt for creation** after sign-ins and recoveries, and support platform auto-upgrade where available.
7. **Build a credential management page**: list, name and delete passkeys.
8. **Redesign recovery** so it doesn't silently undo the security gain.
9. **Measure**: passkey creation rate, sign-in success rate, time to sign in, fallback rate, and account takeover rate compared with password users.
10. **Over time, nudge toward passwordless**: let users remove their password, and for high-risk or admin accounts, require phishing-resistant authentication.

For **APIs and services**, passkeys aren't directly relevant: machines should authenticate with keys, mTLS or workload identity, as the Oct 26 secrets and Oct 28 OAuth readings describe. But passkeys fit well into OAuth and OIDC flows: the user signs in to the identity provider with a passkey, and the rest of the token machinery stays the same. That's how many organizations adopt them, by enabling passkeys at their identity provider (Okta, Entra ID, Google Workspace and others support them) and getting phishing-resistant login for every app behind it.

## Key takeaways

- Passwords and one-time codes fail because users can be tricked into giving them to the wrong site.
- WebAuthn uses a separate key pair per site; the server stores only public keys, so breaches and reuse stop mattering.
- Phishing resistance comes from the browser binding every credential and signature to the real origin, not from the user's judgment.
- With user verification, a passkey combines possession and biometrics or a PIN, so it can replace both password and second factor.
- Synced passkeys survive device loss and suit consumers; device-bound keys with attestation suit high-assurance access.
- Hybrid sign-in uses a QR code plus a Bluetooth proximity check, which keeps cross-device login phishing-resistant.
- UX (autofill, timely prompts) drives adoption, and account recovery decides whether the security gain survives.
- The FIDO Credential Exchange specs address portability between passkey providers.

## Discussion questions

1. Explain to a non-technical colleague why a perfect fake login page can't steal a passkey, but can steal a password plus an authenticator app code.
2. Your product's account recovery uses an email link. After adding passkeys, how much phishing resistance have you actually gained, and what would you change?
3. Would you accept synced passkeys for an internal admin console? What would make you require device-bound keys instead?
4. Why should the user handle be a random ID rather than the user's email address?
5. What would happen to your users' passkeys if you changed your RP ID from `app.example.com` to `example.com`? How would you plan that migration?
6. Passkeys move trust to the sync provider's account. Is that better or worse than today's situation, where email accounts are already the recovery path for most services?
7. Which metrics would tell you that a passkey rollout is working, and which would warn you it's being bypassed?

## Sources

- W3C, "Web Authentication: An API for accessing Public Key Credentials," Level 1 (Recommendation, March 2019), Level 2 (2021) and Level 3, https://www.w3.org/TR/webauthn/
- Reports on the W3C advancing WebAuthn Level 3 (2026), for example https://ssojet.com/blog/webauthn-level-3-changes-for-passkey-implementers
- FIDO Alliance, "Client to Authenticator Protocol (CTAP)" specifications and passkeys overview, https://fidoalliance.org/passkeys/
- FIDO Alliance, Credential Exchange Protocol and Format working drafts (October 2024); TechTarget, "FIDO unveils new specifications to transfer passkeys," https://www.techtarget.com/searchsecurity/news/366613642/FIDO-unveils-new-specifications-to-transfer-passkeys
- Apple, Google and Microsoft joint announcement on expanding FIDO sign-in support (May 2022).
- passkeys.dev (W3C WebAuthn Community Adoption Group and FIDO), implementation guidance on conditional UI, AAGUIDs and device support, https://passkeys.dev
- web.dev and Google Identity documentation on passkeys and form autofill, https://web.dev/articles/passkey-form-autofill
- Yubico developer documentation on WebAuthn, attestation and user verification, https://developers.yubico.com/WebAuthn/
- Brian Krebs, "Google: Security Keys Neutralized Employee Phishing," KrebsOnSecurity, July 2018.
- Verizon, Data Breach Investigations Report (annual).
- SimpleWebAuthn documentation, https://simplewebauthn.dev

The WebAuthn Level 3 status and the FIDO credential exchange specs were found in searches on Nov 4, 2026. Other details come from the specifications and vendor documentation as I know them. Adoption figures vary by company and I haven't quoted specific numbers, so check the companies' own posts for those.
