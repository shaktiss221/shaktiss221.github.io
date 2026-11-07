---
title: "Shipping safely: feature flags, canaries and progressive delivery"
date: 2026-11-07
summary: Why most outages start with a change, how separating deploy from release with feature flags reduces risk, canary analysis and progressive rollouts, blue-green and rollback strategies, database changes that can't be rolled back, flag debt, and the lessons of Knight Capital and CrowdStrike.
tags: infrastructure, distributed systems
daily: true
---

Ask any team running a large service what causes most of their incidents, and the answer is almost always the same: changes. New code, new configuration, new infrastructure. Systems that are left alone tend to keep working; systems that change break. But a product that doesn't change is dead, so the real question isn't how to avoid change but how to make each change small, observable and reversible. This reading covers the toolkit modern teams use for that: feature flags that separate deploying code from releasing it, canaries and progressive rollouts that expose changes gradually while watching for trouble, rollback strategies, the special problem of database changes, and two expensive lessons, Knight Capital in 2012 and CrowdStrike in 2024, about what happens when changes go everywhere at once.

## 1. Change is the main source of outages

Google's SRE books have long made the point directly: a large share of outages are caused by changes to a live system, and the best way to reduce them is to make changes safer rather than rarer. *The Site Reliability Workbook*'s chapter on canarying starts from exactly this premise.

The reasoning is simple. A running system has been exercised by real traffic, so most of its bugs that real traffic can trigger have already shown up and been fixed. A change introduces new behavior that has only been tested against test traffic and test data, which never fully match production. Some fraction of changes will be bad, no matter how good the tests are.

So reliability engineering for change has three goals:

1. **Detect bad changes quickly**, with good signals.
2. **Limit the blast radius**, so a bad change affects a small fraction of users before it's detected.
3. **Recover quickly**, ideally automatically, by rolling back or turning off the change.

Everything in this reading serves one of those three goals.

## 2. Deploy is not release

The most important conceptual shift is separating two things that used to happen together:

- **Deploying** puts new code onto servers.
- **Releasing** exposes new behavior to users.

If they're the same event, every deploy is a big-bang release: when the new binary starts, every user gets the new behavior at once, and the only way back is to redeploy the old binary, which can take as long as the deploy itself.

**Feature flags** (also called feature toggles) separate them. New code ships to production turned off, behind a condition:

```python
if flags.enabled("new-checkout", user=user):
    return new_checkout(cart)
return old_checkout(cart)
```

The deploy is now low-risk, because nothing user-visible changes. The release becomes a separate, fast, reversible step: flip the flag for internal users, then 1% of customers, then everyone. If something goes wrong, flip it off in seconds, without a deploy.

Pete Hodgson's article "Feature Toggles (aka Feature Flags)" on Martin Fowler's site is the standard reference here, and it distinguishes four kinds of flags with very different lifetimes:

| Kind | Purpose | Lifetime | Example |
|---|---|---|---|
| **Release toggles** | Hide unfinished or unreleased features | Days to weeks | New checkout flow |
| **Experiment toggles** | A/B tests | Weeks | Two button layouts |
| **Ops toggles** | Kill switches and degradation controls | Long-lived | Disable recommendations under load |
| **Permission toggles** | Features for some users only | Long-lived | Beta program, premium tier |

Mixing these up causes trouble. A release toggle should be deleted soon after rollout. An ops toggle should stay, be documented, and be tested, because it's what on-call engineers reach for during an incident.

### Trunk-based development

Flags also change how teams write code. Instead of long-lived feature branches merged in one big risky step, developers merge small changes to the main branch daily, with unfinished work hidden behind flags. Every merge is deployable. The *Accelerate* research by Nicole Forsgren, Jez Humble and Gene Kim, based on the State of DevOps surveys, found trunk-based development to be one of the practices associated with high-performing teams, which deploy more often *and* have lower change failure rates. Speed and stability aren't a trade-off when changes are small.

### Flag systems at scale

Large companies built their own flag systems early: Facebook's **Gatekeeper** has been described in its engineering posts as controlling which users see which features, evaluated billions of times. Commercial and open-source systems now fill this role for everyone else: LaunchDarkly, Statsig, Unleash, Flagsmith, Flipt, and cloud offerings like AWS AppConfig. **OpenFeature**, a CNCF project, defines a vendor-neutral API so application code doesn't depend on one flag vendor, much as OpenTelemetry did for telemetry (the Oct 31 reading).

Design points that matter for a flag system:

- **Evaluate locally.** Flag checks happen on hot paths, so SDKs download rules and evaluate in memory rather than calling a server per check.
- **Fail safe.** If the flag service is unreachable, the SDK should use cached rules or a safe default. A flag service outage must not become your outage.
- **Consistent bucketing.** Rolling out to 10% of users should mean the same 10% each time, usually by hashing the user ID with the flag name, so users don't flip between experiences.
- **Audit trail.** Every flag change is a production change. Record who changed what, when, and ideally require review for important flags.

## 3. Canaries

A **canary** is a small slice of production that gets the new version first, named after the canaries miners carried to detect gas. If the canary looks healthy compared with the rest of the fleet, the change proceeds. If not, it stops.

The key word is *compared*. A canary isn't just "watch the dashboards after deploying to one server." A good canary compares the new version with a **baseline** running the old version, at the same time, under similar traffic:

- **Same time**, so time-of-day patterns, traffic spikes and dependency hiccups affect both equally.
- **Similar size**, ideally. Netflix's approach, described in its posts on automated canary analysis and the open-source **Kayenta** tool (built with Google for Spinnaker), deploys a fresh baseline cluster of the old version alongside the canary, rather than comparing against the large existing fleet, so that differences like cache warmth and instance age don't skew the comparison.
- **Statistical comparison.** Kayenta compares metric distributions (error rates, latency percentiles, CPU, business metrics) between canary and baseline with statistical tests, and produces a score. Below a threshold, the canary fails.

*The Site Reliability Workbook*'s canarying chapter adds practical guidance:

- **Pick metrics that reflect user impact**: error rate and latency for the canary's own requests, not just host health. A canary that crashes on startup is easy to catch; one that returns subtly wrong results is not.
- **Run long enough.** Some bugs only appear under particular traffic, at particular times, or after caches expire or memory leaks grow. A canary that runs for five minutes misses those.
- **Make the canary representative.** If the canary only gets traffic from one region or one type of user, it can't catch bugs that affect others.
- **Watch the effect on other services.** A new version that sends ten times more requests to a dependency can look healthy itself while hurting everyone else.

## 4. Progressive rollouts

<!--diagram:progressive-rollout-1107-->

A canary is the first step of a **progressive rollout**: exposure grows in stages, with checks between them.

A typical sequence: internal users or employees ("dogfooding"), then a 1% canary, then 5%, 25%, 50% and 100%, with a **soak time** at each stage and automated analysis gating the next. The stages can be by traffic share, by user cohort, by region, or by availability zone. Rolling out one zone or region at a time limits a bad change to one failure domain and matches how cloud providers design their own deployments. AWS's Builders' Library article "Automating safe, hands-off deployments" describes deployments that go to one box, then one availability zone, then one region, then progressively more regions, waiting and checking alarms between each, with automatic rollback.

Tools such as **Argo Rollouts** and **Flagger** do this for Kubernetes, shifting traffic between versions through a service mesh or ingress and querying metrics at each step. Flag systems do the same for features inside a single deployment.

The trade-off is speed: a careful global rollout can take hours or days. For most changes, that's acceptable, because the cost of a slow rollout is small and the cost of a global outage is large. For urgent security fixes, teams sometimes compress stages, but they should do so knowingly, not by default.

## 5. Blue-green, rolling and rollback

Several deployment strategies trade off speed, cost and recovery:

**Rolling deploys** replace instances a few at a time. They need no extra capacity, but during the rollout both versions serve traffic, and rolling back means rolling forward again with the old version, which takes as long as the original deploy.

**Blue-green deploys** run two complete environments. "Blue" serves traffic; "green" gets the new version and is tested. Switching traffic (at the load balancer or DNS) releases it, and switching back is near-instant rollback, as long as blue is still running. The cost is double capacity during the switch, and the switch itself is all-or-nothing unless you combine it with gradual traffic shifting.

**Canary plus gradual traffic shifting** combines the two: run both versions, move traffic over in steps, and keep the old version warm until the new one is proven.

Whichever you use, a few rollback principles hold:

- **Rollback should be the default response**, not a debate. If a change correlates with a problem, roll it back first and investigate later. Google's SRE guidance is that rolling back is usually the fastest way to mitigate, even before you know the cause.
- **Rollback must be tested.** A rollback path that hasn't been exercised often fails when needed.
- **Configuration is code.** Configuration changes cause as many incidents as code changes, and deserve the same staged rollout, review and rollback. Many large outages, including several at major cloud providers, came from configuration pushed globally at once.

## 6. Database changes: the part you can't roll back

Code can be rolled back in minutes. A database migration that drops a column cannot. Schema changes need a different discipline, usually called **expand and contract** (or parallel change):

1. **Expand**: add the new structure alongside the old. Add the new column (nullable, or with a default that doesn't rewrite the table), add the new table, add the new index concurrently (see the Nov 6 indexing reading).
2. **Migrate**: deploy code that writes to both old and new, backfill existing data in batches, then switch reads to the new structure, ideally behind a flag.
3. **Contract**: once nothing reads or writes the old structure, and you're confident you won't roll back, remove it.

At every step, both the current code and the previous code version must work with the current schema. That's what makes code rollbacks safe during the migration. Renaming a column in one step breaks this rule; adding the new name, dual-writing, switching reads, and dropping the old name later doesn't.

The same idea applies to message formats in queues (the Nov 1 reading) and to API changes (the Oct 26 reading): make every intermediate state compatible in both directions, so any single step can be reversed.

## 7. Flag debt and the Knight Capital lesson

Flags have a cost. Every flag doubles the number of code paths through the code it guards. Ten independent flags mean over a thousand combinations, most of them never tested. Old flags that are always on, or always off, make code harder to read and hide dead paths that can come back to life.

The most famous example is **Knight Capital** in August 2012. According to the SEC's 2013 order, Knight was deploying new code for a new NYSE program to its trading servers. The new code reused a flag that had previously activated an old, long-unused function called "Power Peg." A technician failed to deploy the new code to one of the eight servers. When the market opened and the flag was turned on, the seven updated servers ran the new logic, but the eighth ran the old Power Peg code, which started sending enormous numbers of orders. In about 45 minutes, Knight accumulated huge unwanted positions and lost roughly $460 million, which effectively ended the company as an independent firm.

The incident combined several failures this reading warns about: dead code left in production, a flag reused for a new purpose, a manual deploy without verification that every server had the same version, and no automated kill switch that stopped trading when behavior looked abnormal.

Practical flag hygiene:

- **Give every release flag an owner and an expiry date**, and remove the flag and the dead code path soon after rollout. Some teams make the build fail or file tickets automatically when flags pass their expiry.
- **Never reuse a flag name for a new purpose.**
- **Track which flags exist and their state** in one place, and alert on stale ones.
- **Test the important combinations**, especially kill switches, which must actually work during an incident.
- **Verify deploys**: confirm every instance runs the expected version before enabling a flag that depends on it.

## 8. CrowdStrike: when a change goes everywhere at once

On July 19, 2024, CrowdStrike pushed a content update, a configuration file called Channel File 291, to its Falcon sensor, which runs inside the Windows kernel on customers' machines. The update triggered an out-of-bounds memory read in the sensor, and Windows machines that received it crashed and couldn't boot normally. Microsoft estimated that about 8.5 million Windows devices were affected. Airlines, hospitals, banks and broadcasters were disrupted, and many machines had to be fixed by hand.

CrowdStrike's root cause analysis described a mismatch: the content update supplied 21 input fields where the sensor code expected 20, and the content validator didn't catch it. But the broader lesson from the incident, which CrowdStrike itself acknowledged by promising changes, was about rollout. **The content update went to all customers at essentially the same time.** Sensor *code* updates had staged rollouts and customer controls; these rapid content updates were treated as data, not code, and were deployed globally. CrowdStrike committed to staged deployment of content updates, with canaries and increasing rings, and to giving customers more control over when they receive updates.

The lessons generalize to any system:

- **Configuration and data that change behavior are code** and deserve staged rollouts.
- **The more privileged and widespread the component, the more careful the rollout must be.** A kernel driver on millions of machines has no room for big-bang changes.
- **Validate inputs at the consumer**, not just at the producer. The sensor trusted its content files.
- **Plan for recovery when rollback is impossible.** Machines that crash before they can receive a fix need a recovery path that doesn't depend on the broken component.

## 9. Measuring delivery performance

The DORA research program (DevOps Research and Assessment, now at Google Cloud) tracks software delivery with four key metrics, popularized by *Accelerate*:

- **Deployment frequency**: how often you deploy to production.
- **Lead time for changes**: from commit to running in production.
- **Change failure rate**: the share of deployments that cause a failure needing remediation.
- **Time to restore service** (now often called failed deployment recovery time): how long recovery takes.

The first two measure speed; the last two measure stability. The consistent finding across years of State of DevOps reports is that the best-performing teams do well on all four at once. Small, frequent, reversible changes are how they do it. Teams that deploy rarely tend to ship large, risky batches that are harder to test, harder to diagnose and harder to roll back.

## 10. A practical checklist

1. **Separate deploy from release** with feature flags for user-facing changes.
2. **Classify flags** (release, experiment, ops, permission), give release flags owners and expiry dates, and delete them after rollout.
3. **Canary every change** against a concurrent baseline, with metrics that reflect user impact and long enough soak times.
4. **Roll out progressively** by cohort, zone and region, with automated gates and automatic rollback.
5. **Treat configuration and data as code**: review, stage and roll back the same way.
6. **Make rollback the default** response to a correlated problem, and test it regularly.
7. **Use expand and contract for schema changes**, so every step is compatible in both directions.
8. **Verify deploys**: every instance should run the expected version before dependent flags flip.
9. **Keep kill switches** for risky features and dependencies, documented and tested.
10. **Track DORA metrics** to see whether changes are getting smaller, faster and safer.

## Key takeaways

- Most outages start with a change, so the goal is to make changes small, observable and reversible.
- Feature flags separate deploying code from releasing behavior, making release and rollback fast.
- Different kinds of flags have different lifetimes; release flags must be removed promptly.
- Good canaries compare the new version with a concurrent baseline using user-impact metrics and enough soak time.
- Progressive rollouts limit the blast radius by exposing changes in stages and failure domains, with automated gates.
- Database changes can't be rolled back like code; use expand and contract so every step is reversible.
- Knight Capital shows the danger of dead code, reused flags and inconsistent deploys; CrowdStrike shows the danger of treating behavior-changing data as something that can skip staged rollout.
- High-performing teams deploy more often and fail less, because their changes are small.

## Discussion questions

1. Which changes in your systems skip the staged rollout that code gets: configuration, feature flags, data files, infrastructure? What could go wrong?
2. A canary shows a 0.3% error rate against a baseline of 0.2%. Is that a failure? What else would you want to know?
3. You need to rename a heavily used column. Write the sequence of deploys and migrations, and say at which point rollback stops being possible.
4. How many feature flags does your main codebase have, and how many are older than three months? What would it take to clean them up?
5. Some changes are urgent, like a security fix. How would you shorten a progressive rollout without losing its main protection?
6. What would a kill switch for your riskiest dependency look like, and how would you know it works?
7. CrowdStrike's update ran in the kernel and could crash machines before they could get a fix. What recovery paths would you design for software that can't be rolled back remotely?

## Sources

- Betsy Beyer et al. (eds.), *The Site Reliability Workbook* (O'Reilly, 2018), chapter 16 "Canarying Releases," https://sre.google/workbook/canarying-releases/
- Pete Hodgson, "Feature Toggles (aka Feature Flags)," martinfowler.com, 2017, https://martinfowler.com/articles/feature-toggles.html
- Nicole Forsgren, Jez Humble and Gene Kim, *Accelerate: The Science of Lean Software and DevOps* (IT Revolution, 2018); DORA State of DevOps reports, https://dora.dev
- Netflix Technology Blog and Google Cloud, "Introducing Kayenta: An open automated canary analysis tool from Google and Netflix" (2018).
- Clare Liguori, "Automating safe, hands-off deployments," Amazon Builders' Library, https://aws.amazon.com/builders-library/automating-safe-hands-off-deployments/
- OpenFeature project, https://openfeature.dev ; Argo Rollouts and Flagger documentation.
- U.S. Securities and Exchange Commission, order in the matter of Knight Capital Americas LLC (October 2013); summary at https://www.jrvarma.in/blog/Y2013/Knight-Capital.html and https://en.wikipedia.org/wiki/Knight_Capital_Group
- CrowdStrike, "Channel File 291 Incident: Root Cause Analysis" (August 2024); coverage in TechTarget, "CrowdStrike details errors that led to mass IT outage," https://www.techtarget.com/searchsecurity/news/366602392/CrowdStrike-details-errors-that-led-to-mass-IT-outage and Help Net Security, https://www.helpnetsecurity.com/2024/08/07/crowdstrike-outage-causes/
- Microsoft, "Helping our customers through the CrowdStrike outage" (July 2024), the source of the 8.5 million device estimate.
- Martin Fowler, "ParallelChange" (expand and contract), https://martinfowler.com/bliki/ParallelChange.html

The Knight Capital and CrowdStrike details were checked against the search results on Nov 7, 2026. The Facebook Gatekeeper and Netflix canary descriptions summarize those companies' public posts as I know them.
