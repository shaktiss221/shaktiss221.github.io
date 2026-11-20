---
title: "Software supply chain security: from xz to Shai-Hulud, and what actually helps"
date: 2026-11-20
summary: How attackers compromise software before it reaches you, and the controls that make it harder. Covers the stages of the supply chain, landmark attacks (SolarWinds, Codecov, dependency confusion, event-stream, xz utils, tj-actions, the Shai-Hulud npm worm), dependency hygiene, CI/CD hardening, SBOMs, signing with Sigstore, SLSA provenance levels, reproducible builds, and a prioritised plan for a typical engineering team.
tags: security, infrastructure
daily: true
---

Most of the code running in your production systems wasn't written by your company. A typical service pulls in hundreds or thousands of open-source packages, is built by a CI system that runs third-party actions and scripts, and is shipped as a container image built on someone else's base image. Each of those is a place where an attacker can insert code that you'll then run with your own credentials. Over the past six years, that's exactly what has happened, repeatedly and at scale: a build system compromised at SolarWinds, a backdoor patiently planted in the compression library xz, a popular GitHub Action rewritten to dump secrets, and in 2025 a self-propagating worm that stole npm tokens and republished hundreds of packages. This reading walks through the stages of the software supply chain, the attacks that defined each one, and the controls that work against them (dependency pinning, CI hardening, SBOMs, artifact signing with Sigstore, SLSA provenance and reproducible builds), ending with a practical order in which to adopt them. It draws on CISA advisories, the SLSA specification, the Sigstore project, Andres Freund's xz disclosure, and incident write-ups from security firms. The earlier readings on secrets management and workload identity cover related ground on credentials.

## 1. What "supply chain" means for software

A useful model, close to the one SLSA uses, follows code from its author to production:

1. **Source:** the code in a repository, and the people and accounts that can change it.
2. **Dependencies:** third-party packages pulled from registries (npm, PyPI, Maven Central, crates.io, Go modules), plus base images and system packages.
3. **Build:** the CI system and build scripts that turn source and dependencies into artifacts, including any third-party CI plugins or actions.
4. **Artifacts:** the resulting packages, binaries and container images.
5. **Distribution and deployment:** registries and package mirrors, download scripts, and the deployment system that pulls and runs artifacts.

An attacker who controls any stage controls what you run. Two properties make these attacks especially effective. They're **one-to-many**: compromise one popular package or build system and you reach every downstream user. And they **arrive through trusted channels**: the malicious code is signed by the right vendor, published by the right maintainer account, or built by your own CI, so the defences aimed at outside attackers don't trigger.

<!--diagram:supply-chain-1120-->

## 2. Attacks that defined the field

### SolarWinds (disclosed December 2020): the build system

Attackers compromised the build environment for SolarWinds' Orion network-monitoring product and inserted a backdoor (known as SUNBURST) into the build process itself. The source repository looked clean; the malicious code was injected while the product was compiled, and the resulting updates were signed with SolarWinds' legitimate certificate. SolarWinds said that up to about 18,000 customers installed the affected updates, and the attackers then selectively exploited a small number of high-value targets, including several US government agencies. It showed that reviewing source code isn't enough if the build can be tampered with, and it directly led to the US Executive Order 14028 (May 2021) on improving the nation's cybersecurity, which called for SBOMs and secure development practices for software sold to the government.

### Codecov (2021): the download script

Codecov's Bash uploader, a script that thousands of CI pipelines downloaded and ran with `curl | bash`, was modified by an attacker who obtained a credential from Codecov's Docker image build process. The altered script sent the environment variables of every CI job that ran it (including secrets and tokens) to an attacker's server, and the change went unnoticed for about two months. The lesson: anything your CI downloads and executes at build time is part of your supply chain, and CI environments are full of secrets.

### Dependency confusion (2021): the package resolver

Security researcher Alex Birsan showed that many companies' build tools would install a **public** package in place of an **internal** one with the same name if the public one had a higher version number. By publishing harmless proof-of-concept packages with names taken from leaked internal manifests, he got code execution inside dozens of large companies, including Apple, Microsoft and PayPal, and collected substantial bug bounties. The fix is configuration: scope internal packages (npm `@company/` scopes), point resolvers at a single private registry that proxies the public one, and never let the resolver choose between sources by version.

### event-stream (2018) and the maintainer handover

A widely used npm package, `event-stream`, was handed over by its tired maintainer to a volunteer who soon added a dependency containing obfuscated code targeting a specific Bitcoin wallet application. It was a reminder that open-source maintenance often rests on one unpaid person, and that ownership transfers are a supply chain event.

### xz utils (March 2024): the patient insider

The most sophisticated case so far. Over about two years, a contributor using the name "Jia Tan" built trust in the xz utils compression project, became a co-maintainer, and then shipped releases **5.6.0 and 5.6.1** containing a backdoor (CVE-2024-3094). The malicious code wasn't in the visible source; it was hidden in binary test files and activated by changes to the build scripts included in the release tarballs, not in the git repository. On some Linux distributions, the OpenSSH server is linked (through systemd) with liblzma, so the backdoor could intercept SSH authentication and allow remote code execution for someone holding the attacker's private key.

It was caught on 29 March 2024, before reaching most stable distributions, largely by luck. Andres Freund, a Postgres developer at Microsoft, noticed that SSH logins on a Debian testing machine were using more CPU and taking about half a second longer than they should, investigated, and reported it to the oss-security mailing list. The incident showed that attackers will invest years in social engineering, that release tarballs can differ from the repository in dangerous ways, and that the health of critical but under-resourced projects is a security issue.

### tj-actions/changed-files (March 2025): third-party CI actions

The GitHub Action `tj-actions/changed-files`, used by tens of thousands of repositories (reports put it at around 23,000), was compromised: attackers modified the action's code and moved its existing version tags to point at a malicious commit. Any workflow referencing the action by tag (`@v35`, `@v44`) ran the new code, which dumped the CI runner's memory, including secrets, into the workflow logs. For public repositories, those logs are publicly readable. The investigation traced the compromise back to a token stolen through another action (`reviewdog/action-setup`), a chain of supply chain compromises. The lesson: a version tag in Git is a mutable pointer, and referencing third-party actions by tag means running whatever the tag points at today.

### Shai-Hulud (September 2025): a self-propagating worm

In September 2025 a worm spread through npm. According to CISA's alert, it **compromised over 500 packages**. When a compromised package was installed, its code scanned the environment for credentials, especially GitHub personal access tokens and cloud keys for AWS, GCP and Azure, exfiltrated them, and published them to public GitHub repositories named "Shai-Hulud". Then it used stolen npm credentials to **inject itself into other packages the victim maintained and publish new versions**, which spread it further without any human attacker involved. CISA advised pinning dependencies to versions published before 16 September 2025, rotating credentials, and reviewing for malicious repositories and workflows.

Shai-Hulud combined several of the earlier patterns: install-time scripts executing on developer and CI machines, long-lived tokens available in the environment, and publishing rights that let one compromised account reach many packages.

## 3. Dependencies: the largest attack surface

Most teams' biggest exposure is the dependency tree. Controls that work, roughly in order of value for effort:

- **Lockfiles everywhere, committed and enforced.** `package-lock.json`, `pnpm-lock.yaml`, `poetry.lock`, `go.sum`, `Cargo.lock`. Install with the lockfile-respecting command (`npm ci`, `pip install --require-hashes`) so CI installs exactly what was reviewed, with integrity hashes. A lockfile turns "whatever the latest version is right now" into a specific, reviewable change.
- **Don't take new versions immediately.** Most malicious releases are detected within hours or days. Configuring update tools (Dependabot, Renovate) to wait a few days before proposing new versions (Renovate's `minimumReleaseAge`, for example) avoids a lot of risk at little cost. Several recent package-manager releases also add options to refuse versions younger than a set age.
- **Limit install-time scripts.** npm `preinstall`/`postinstall` scripts run arbitrary code on every machine that installs the package; that's how many npm attacks execute. Disable them by default (`npm config set ignore-scripts true`, or pnpm, which blocks dependency lifecycle scripts by default in recent versions) and allow them only for packages that need them.
- **Use a private registry proxy** (Artifactory, Nexus, cloud artifact registries) as the single source for all packages. It prevents dependency confusion, gives you a place to block known-bad versions quickly, and keeps builds working if the public registry is down.
- **Scan continuously** for known vulnerabilities (OSV, GitHub Dependabot alerts, Snyk and others) **and** for known-malicious packages, which vulnerability databases sometimes treat separately. Tools like OpenSSF Scorecard help assess the health of a dependency before adopting it (maintainers, branch protection, signed releases, active fixes).
- **Prefer fewer, healthier dependencies.** Every dependency is code you run and a set of maintainers you trust. A small utility function you can write in ten lines may not be worth a package with its own transitive tree.

## 4. CI/CD: the most privileged machine you own

CI systems have access to source code, signing keys, cloud deployment credentials and package publishing tokens, and they execute code from many sources (your repository, pull requests, third-party actions, dependencies). That makes them prime targets. Hardening steps:

- **Pin third-party actions and plugins to a full commit SHA**, not a tag: `uses: some/action@8f4b7f84...` rather than `@v4`. A SHA can't be moved; a tag can, as tj-actions showed. Update tools can keep the SHAs current with readable version comments.
- **Least-privilege tokens.** In GitHub Actions, set the default `GITHUB_TOKEN` permissions to read-only at the organisation level and grant write permissions per job only where needed.
- **No long-lived cloud credentials in CI.** Use OIDC federation (GitHub Actions, GitLab and others issue a signed OIDC token per job) to exchange for short-lived cloud credentials scoped to that repository and branch. A stolen token then expires in minutes and only works for that context. The workload identity reading covered the mechanism.
- **Separate untrusted from trusted workflows.** Workflows triggered by pull requests from forks must not have access to secrets. GitHub's `pull_request_target` trigger runs with secrets in the context of the base repository and has been the source of many "pwn request" vulnerabilities when combined with checking out the PR's code.
- **Isolate the build.** Ephemeral runners that are destroyed after each job prevent one job from tampering with the next. Hermetic builds, which can only use declared inputs and have no network access during compilation, prevent a build from quietly fetching something it shouldn't.
- **Protect publishing.** Require two-factor authentication on registry accounts, use **trusted publishing** where registries support it (PyPI, npm and crates.io now let CI publish via OIDC without stored API tokens), and restrict who and what can publish.
- **Protect the source.** Branch protection, required reviews, signed commits or at least verified committer identity, and 2FA for everyone with write access. Watch for changes to CI configuration files themselves, which are as sensitive as code.

## 5. SBOMs: knowing what you run

When Log4Shell (CVE-2021-44228) was disclosed in December 2021, the first question every organisation had to answer was "where do we use log4j?", and many couldn't answer it quickly, because log4j was buried in transitive dependencies and vendored into third-party products. A **software bill of materials (SBOM)** is a machine-readable list of the components in an artifact: names, versions, suppliers, hashes and relationships, typically in the **SPDX** or **CycloneDX** formats.

What SBOMs are good for:

- **Answering "are we affected?"** in minutes when the next big vulnerability lands, by querying SBOMs for every deployed artifact.
- **Licence compliance**, which is where the SPDX format originated.
- **Procurement and regulation.** EO 14028 drove SBOM requirements for US federal suppliers, and the EU's Cyber Resilience Act brings documentation requirements for products sold in the EU.

What they aren't: an SBOM is an inventory, not a security control. It doesn't stop a malicious package; it helps you respond. Its value depends on generating it automatically from the actual build (tools like Syft, Trivy or the build system itself), storing it with the artifact, and having a way to query across all of them. A **VEX** (Vulnerability Exploitability eXchange) document can accompany an SBOM to say which known vulnerabilities actually affect the product, which cuts down scanner noise.

## 6. Signing and provenance: Sigstore and SLSA

Knowing *what* is in an artifact is one question. Knowing **where it came from and how it was built** is another, and it's the one that would have helped with SolarWinds and Codecov.

### Signing with Sigstore

Traditional code signing requires managing long-lived private keys, which are hard to protect, and most open-source projects never did it. **Sigstore**, a Linux Foundation / OpenSSF project launched in 2021, makes signing nearly free:

- **Fulcio** is a certificate authority that issues **short-lived signing certificates** (valid for about ten minutes) bound to an identity proven via OIDC, such as a developer's email account or a CI workflow's identity ("this GitHub Actions workflow in this repository on this branch").
- **Rekor** is a public, append-only **transparency log** that records every signature, so signatures can be audited and a compromised identity's misuse can be detected.
- **Cosign** and language-specific clients sign and verify artifacts and container images.

This is "keyless" signing: there's no long-lived key to steal, and the signature says *which identity* signed it, which a verifier can check against policy ("only accept images signed by our release workflow"). npm, PyPI, Maven Central, Homebrew and Kubernetes itself use Sigstore for signatures or provenance.

### SLSA: levels for build integrity

**SLSA** (Supply-chain Levels for Software Artifacts, pronounced "salsa") is an OpenSSF framework, originally from Google's internal practices, that defines incremental levels of assurance. In the v1.0 specification, the **Build track** has four levels:

- **Build L0:** no guarantees.
- **Build L1: provenance exists.** The build produces a provenance document describing how the artifact was built (source repository and commit, build platform, build command, inputs).
- **Build L2: hosted build platform, signed provenance.** The build runs on a hosted CI service, which signs the provenance, so it can't be forged by a developer's machine.
- **Build L3: hardened builds.** The build platform prevents builds from influencing each other and keeps signing material out of reach of the user-defined build steps, so even a malicious build script can't forge provenance.

Provenance is typically expressed as an **in-toto attestation** and signed (often with Sigstore). npm's **provenance** feature (`npm publish --provenance` from a supported CI) links each published package version to the exact source commit and workflow that built it, viewable on the package page. GitHub's artifact attestations offer the same for any build artifact.

The value of provenance is verification at the consumer side: a deployment system or admission controller can refuse images that lack provenance from your release workflow, or that were built from a branch other than `main`. In the Codecov case, verification against the expected source would have flagged a script that didn't match its repository. Against SolarWinds-style build compromise, SLSA L3's isolation requirements are what matter, because provenance from a compromised build system would otherwise attest to the tampered artifact.

### Reproducible builds

A build is **reproducible** if anyone can rebuild the same source and get a bit-for-bit identical artifact. Then independent parties can verify that a published binary matches its source, and a compromised build server would be caught by the mismatch. Debian, Arch Linux, NixOS and others have done a lot of work toward this (the Reproducible Builds project). It requires removing timestamps, file ordering differences and other nondeterminism, which is hard for complex software, but it's the strongest defence against build tampering. The xz incident was also a reminder that release tarballs should be reproducible from the tagged repository, since that's where its malicious build script was hidden.

## 7. Enforcing at deploy time

Signing and provenance only help if something checks them. In Kubernetes, admission controllers such as **Sigstore's policy-controller** or **Kyverno** can enforce rules like: images must come from our registry, must be signed by our release workflow identity, and must have SLSA provenance showing they were built from our repository's protected branch. Similar checks can run in deployment pipelines for non-Kubernetes targets. Start in audit mode, measure how many images would fail, fix the pipelines, then enforce.

## 8. A practical plan for a typical team

Not everything above is equally urgent. A reasonable order, by risk reduced per unit of effort:

1. **Lock and pin.** Commit lockfiles and install from them in CI. Pin third-party CI actions to SHAs. This alone would have blunted tj-actions and most "malicious new version" attacks.
2. **Remove long-lived secrets from CI.** Switch to OIDC federation for cloud access and trusted publishing for package registries. Set default CI token permissions to read-only. This limits the damage of almost every attack in section 2, since most of them were credential theft.
3. **Turn on 2FA and branch protection** for source repositories and registry accounts.
4. **Delay and gate updates.** Use automated update tools with a minimum release age, and block install scripts by default.
5. **Use a private registry proxy** for all dependencies.
6. **Generate SBOMs** for every artifact and store them where you can query them.
7. **Sign artifacts and generate provenance** from CI (Sigstore, SLSA Build L2 via your CI's attestation features).
8. **Verify at deploy** with admission policies, first in audit mode.
9. **Invest upstream.** Identify the small, critical open-source projects you depend on and consider funding or contributing to them; xz showed that their health is part of your security.

## Key takeaways

- The supply chain runs from **source → dependencies → build → artifacts → deployment**, and attacks at any stage deliver code you'll run with your own credentials, through trusted channels.
- **SolarWinds** (build tampering), **Codecov** (download script), **dependency confusion** (resolver), **xz utils** (years-long maintainer infiltration, CVE-2024-3094 in 5.6.0/5.6.1), **tj-actions** (moved tags, secrets in logs) and **Shai-Hulud** (a worm that compromised 500+ npm packages) each exposed a different weak link.
- **Lockfiles, SHA pinning, delayed updates and blocked install scripts** are cheap and stop the most common attacks.
- **CI is your most privileged system:** use OIDC instead of stored secrets, least-privilege tokens, ephemeral runners, and trusted publishing.
- **SBOMs** answer "are we affected?" fast but don't prevent anything on their own.
- **Sigstore** makes keyless, identity-bound signing practical; **SLSA** levels (L1 provenance, L2 signed provenance from a hosted builder, L3 hardened builds) and **reproducible builds** make build integrity verifiable.
- Controls only matter if **something verifies them at deploy time.**

## Discussion questions

1. If a package in your dependency tree published a malicious version an hour ago, would your next CI run pick it up? Trace exactly what would happen in your setup.
2. Which secrets does your CI have access to today, and which of them are long-lived? What would it take to replace them with OIDC federation?
3. The xz backdoor lived in release tarballs, not the git repository. Where else in your stack do you consume artifacts that might differ from their source?
4. Your organisation wants to require SLSA Build L3 for everything. What would that mean for your current CI, and which artifacts would you start with?
5. A new critical CVE drops in a widely used library. How long would it take you to list every affected service and version today? What would make it faster?
6. How would you decide which open-source dependencies are "critical" enough to fund or help maintain?

## Sources

- CISA, ["Widespread Supply Chain Compromise Impacting npm Ecosystem"](https://www.cisa.gov/news-events/alerts/2025/09/23/widespread-supply-chain-compromise-impacting-npm-ecosystem), September 2025 (Shai-Hulud)
- Andres Freund, ["backdoor in upstream xz/liblzma leading to ssh server compromise"](https://openwall.com/lists/oss-security/2024/03/29/4), oss-security, 29 March 2024; [Wikipedia: XZ Utils backdoor](https://en.wikipedia.org/wiki/XZ_Utils_backdoor); [Red Hat CVE-2024-3094](https://access.redhat.com/security/cve/cve-2024-3094)
- Palo Alto Networks Unit 42, ["GitHub Actions supply chain attack"](https://unit42.paloaltonetworks.com/github-actions-supply-chain-attack/) (tj-actions/changed-files and reviewdog); [GHSA-mrrh-fwg8-r2c3](https://osv.dev/vulnerability/GHSA-mrrh-fwg8-r2c3); [Socket analysis](https://socket.dev/blog/github-actions-supply-chain-attack-puts-thousands-of-projects-at-risk)
- SolarWinds security advisory and FireEye/Mandiant SUNBURST analysis (December 2020); US Executive Order 14028, "Improving the Nation's Cybersecurity" (May 2021)
- Codecov, "Bash Uploader Security Update" (April 2021)
- Alex Birsan, "Dependency Confusion: How I Hacked Into Apple, Microsoft and Dozens of Other Companies" (February 2021)
- [SLSA specification v1.0: Security levels](https://slsa.dev/spec/v1.0/levels) (Build L0–L3; v1.2 is now current)
- Sigstore documentation (Fulcio, Rekor, Cosign) and the Sigstore policy-controller; in-toto attestation framework
- npm documentation on provenance statements and trusted publishing; PyPI trusted publishers documentation
- SPDX and CycloneDX specifications; CISA SBOM and VEX resources; Reproducible Builds project (reproducible-builds.org)
- OpenSSF Scorecard; GitHub Actions security hardening guide (pinning to SHAs, `GITHUB_TOKEN` permissions, `pull_request_target`)

*Note on verification: the Shai-Hulud details and SLSA build levels were read from CISA and slsa.dev today, and the xz and tj-actions facts were checked against the search results linked above. Details of SolarWinds (about 18,000 customers), Codecov, dependency confusion and event-stream, Fulcio's ten-minute certificate lifetime, and the specific package-manager options for release-age delays and install scripts come from the original disclosures and docs as I know them and weren't re-fetched today.*
