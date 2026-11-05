---
title: "HTTP and CDN caching: Cache-Control, keys, purges and poisoning"
date: 2026-11-05
summary: How HTTP caching actually decides what to store and serve, covering freshness and validation, Cache-Control directives, stale-while-revalidate, cache keys and Vary, CDN tiers, targeted headers, purging with surrogate keys, and the cache poisoning and deception attacks that come from getting keys wrong.
tags: caching, networking
daily: true
---

The fastest request is one that never reaches your servers. HTTP caching, in browsers, reverse proxies and CDNs, is the most effective performance and cost tool most web systems have, and it's also one of the most misunderstood. A single header can decide whether a page is served from a cache a few kilometers from the user or from your origin on another continent, whether a deploy shows up instantly or an hour later, and whether one user's private data ends up served to everyone. The Oct 24 reading covered application caches such as memcache. This one covers the HTTP layer: the rules in RFC 9111, the directives that matter, how CDNs extend them, and the security bugs that come from getting cache keys wrong.

## 1. The model: shared and private caches

HTTP caching, standardized today in **RFC 9111** (June 2022, replacing the caching part of RFC 7234), distinguishes two kinds of cache:

- **Private caches** belong to one user, most importantly the browser's HTTP cache. They can store personalized responses.
- **Shared caches** serve many users: CDNs, reverse proxies like Varnish or NGINX, corporate proxies. They must never store a response meant for one user and serve it to another.

A cache stores a response under a **cache key**, by default the request method and URL. When a later request has the same key, the cache can answer from storage, if the stored response is **fresh**, or after checking with the origin that it's still valid. Nearly everything in HTTP caching is about those two questions: how long is a response fresh, and how do you cheaply check it once it isn't?

## 2. Freshness

A stored response is fresh for its **freshness lifetime**, and its **age** is how long it has been since the origin generated it. While age is below lifetime, a cache can serve the response without contacting the origin at all.

The lifetime comes from, in priority order:

1. `Cache-Control: s-maxage=N` (shared caches only).
2. `Cache-Control: max-age=N`.
3. The `Expires` header (an absolute date; older and clock-dependent, so prefer `max-age`).
4. A **heuristic**, if none of the above is present and the status is cacheable by default. Caches commonly use 10% of the time since `Last-Modified`. This is a frequent surprise: a response with a `Last-Modified` header and no `Cache-Control` can be cached for hours, simply because the file is old.

The `Age` response header tells downstream caches how long the response has already spent in caches upstream, so a response cached for 50 seconds at a CDN arrives at the browser with `Age: 50`, and the browser knows it has only `max-age` minus 50 seconds of freshness left.

## 3. Validation: cheap re-checks

When a response goes stale, a cache doesn't have to download it again. It can **revalidate** with a conditional request:

- If the response had an `ETag` (an opaque version identifier), the cache sends `If-None-Match: "<etag>"`.
- If it had a `Last-Modified` date, it sends `If-Modified-Since: <date>`.

If nothing changed, the origin replies **`304 Not Modified`** with headers but no body, and the cache refreshes its stored copy's freshness. For a large page or asset, that's the difference between megabytes and a few hundred bytes, and the origin often doesn't have to re-render anything if it can compute the ETag cheaply (for example from a version number or content hash).

ETags come in two strengths. A **strong** ETag means byte-for-byte identical. A **weak** ETag (`W/"abc"`) means semantically equivalent, so it works for things like content that varies only in insignificant whitespace or compression. The Oct 26 API design reading showed ETags used with `If-Match` for concurrency control. That's the same header, used for writes instead of reads.

## 4. Cache-Control, directive by directive

`Cache-Control` is where most caching decisions live. The response directives that matter most:

| Directive | Meaning |
|---|---|
| `max-age=N` | Fresh for N seconds in any cache |
| `s-maxage=N` | Overrides `max-age` for shared caches only |
| `public` | May be stored by shared caches even if normally not (for example, with `Authorization`) |
| `private` | Only the user's own (browser) cache may store it |
| `no-cache` | May be stored, but **must be revalidated** before every use |
| `no-store` | Must not be stored anywhere |
| `must-revalidate` | Once stale, never serve without successful revalidation |
| `immutable` | Won't change while fresh, so don't revalidate even on reload (RFC 8246) |
| `stale-while-revalidate=N` | May serve stale for N seconds while refreshing in the background (RFC 5861) |
| `stale-if-error=N` | May serve stale for N seconds if the origin errors (RFC 5861) |

The single most common mistake is reading `no-cache` as "don't cache." It actually means "cache, but check with the origin every time," which is often what you want for HTML. If something truly must not be stored, such as a page with bank account details, use `no-store`.

### Patterns that work

**Fingerprinted static assets** (`app.3f9a2c.js`, where the hash changes whenever the content does):

```
Cache-Control: public, max-age=31536000, immutable
```

The URL changes when the content changes, so the old URL can be cached for a year. This is the highest-leverage caching pattern on the web, and build tools produce fingerprinted filenames by default.

**HTML pages** that reference those assets:

```
Cache-Control: no-cache
ETag: "v42"
```

Every load checks with the origin, but usually gets a cheap `304`. Deploys show up immediately, because the HTML is always validated and points at the new asset URLs.

**Shared content that can be a bit stale**, such as a product page or API response for anonymous users:

```
Cache-Control: public, max-age=60, stale-while-revalidate=30, stale-if-error=600
```

<!--diagram:http-cache-life-1105-->

**Personalized content**:

```
Cache-Control: private, no-cache
```

or `no-store` if it's sensitive.

### stale-while-revalidate and stale-if-error

These two directives, from RFC 5861, are among the most valuable and underused tools in HTTP caching.

**`stale-while-revalidate`** lets a cache serve a slightly stale response immediately and refresh it in the background. Users never wait for the origin on a popular resource, and the origin sees one refresh request instead of a burst. It's the HTTP equivalent of the stampede protection discussed in the Oct 24 caching reading.

**`stale-if-error`** lets a cache keep serving the last good response if the origin returns errors or is unreachable. A CDN with a ten-minute `stale-if-error` keeps a site's cached pages up through a short origin outage. Many CDNs offer an equivalent "serve stale" setting even without the header.

## 5. Cache keys and Vary

By default, the key is the method plus URL. But responses often depend on request headers too: the `Accept-Encoding` (gzip, Brotli or none), `Accept-Language`, or a cookie. The **`Vary`** response header tells caches which request headers were used to choose the response, so they become part of the key:

```
Vary: Accept-Encoding
```

Now a gzip response and a Brotli response for the same URL are stored separately, and a client that doesn't support Brotli never gets it.

`Vary` is powerful and easy to misuse:

- **`Vary: User-Agent`** effectively disables shared caching, since there are thousands of distinct user agents. If you need device-specific responses, normalize first (CDNs can compute a `device-type` value from the user agent) and vary on that.
- **`Vary: Cookie`** splits the cache per session, which for logged-in traffic means no sharing at all. Usually better to strip irrelevant cookies at the edge, or not cache personalized pages in shared caches.
- **`Vary: *`** means the response can't be reused at all.
- **Forgetting `Vary`** is worse than overusing it. If a response depends on a header that isn't in the key, a cache may serve one user's variant to another: the wrong language, the wrong format, or someone else's data. Section 9 shows how attackers exploit this.

CDNs also let you customize the key directly: ignore certain query parameters (marketing `utm_*` tags shouldn't fragment the cache), sort query parameters, or include a header or cookie value. Key design is one of the most important caching decisions, and it's mostly done in CDN configuration.

## 6. CDN architecture

A **CDN** is a large, geographically distributed shared cache. Requests reach a nearby **edge** location, typically via anycast or DNS, as described in the Oct 30 load-balancing reading. If the edge has a fresh copy, it answers. If not, it fetches from the origin, stores the response, and serves it.

A few architectural features matter for how you use one.

**Tiered caching (origin shield).** With hundreds of edge locations, a cache miss at each edge means hundreds of requests to the origin for the same object. Tiered caching puts a few regional or central cache layers between the edges and the origin: edges miss to an upper tier, and only the upper tier talks to the origin. Hit ratio at the origin goes up dramatically, and origin load goes down. Cloudflare's Tiered Cache, Fastly's shielding and CloudFront's Regional Edge Caches and Origin Shield all work this way.

**Request collapsing (coalescing).** If a thousand users request the same uncached object at once, a good CDN sends one request to the origin and makes the other 999 wait for it, instead of forwarding all of them. This protects origins from stampedes on popular new content. Varnish and most CDNs do this by default for cacheable responses, but it requires the response to be cacheable. A popular uncacheable response gets no protection.

**Hit ratio isn't one number.** A CDN may report a 95% hit ratio by requests while the origin still serves most of the bytes, because large files miss more often. Or the edge hit ratio is high but the origin sees a lot of revalidation requests. Look at hit ratio by requests and by bytes, at the edge and at the origin.

**Long tail.** CDN caches are large but finite, and they evict unpopular content (usually with some form of LRU). An object requested once a day may be evicted between requests at a small edge location, even with a long `max-age`. Tiered caching helps here too.

## 7. Targeted headers: telling the CDN and the browser different things

You often want a CDN to cache a response for a long time (because you can purge it instantly) while browsers cache it only briefly (because you can't purge a browser). `s-maxage` helps, but it applies to every shared cache, including corporate proxies you don't control.

**RFC 9213** ("Targeted HTTP Cache Control," June 2022) adds headers that target specific caches. **`CDN-Cache-Control`** applies only to CDNs, and individual CDNs can define their own (such as `Cloudflare-CDN-Cache-Control`). For example:

```
Cache-Control: public, max-age=60
CDN-Cache-Control: max-age=86400
```

Browsers cache for a minute; the CDN caches for a day; when content changes, you purge the CDN. Many CDNs also strip their targeted header before forwarding the response, so it doesn't confuse other caches.

**RFC 9211** ("The Cache-Status HTTP Response Header Field," 2022) standardizes how each cache reports what it did: hit or miss, why it forwarded the request, the remaining TTL. Before it, every CDN had its own `X-Cache` header format. Cache-Status makes debugging caching across layers much easier.

## 8. Invalidation: purges and surrogate keys

Phil Karlton's line that cache invalidation is one of the two hard things in computer science applies fully to CDNs. Options, from simplest to most powerful:

- **Don't invalidate; change the URL.** Fingerprinted assets never need purging. Use this wherever you can.
- **Short TTLs.** Accept up to N seconds of staleness. Simple and robust, combined with `stale-while-revalidate`.
- **Purge by URL.** Tell the CDN to drop a specific URL. Fine for a few objects, impractical when one change affects thousands of pages.
- **Purge by tag (surrogate keys).** The origin labels each response with tags in a header, such as `Surrogate-Key: product-123 category-shoes` (Fastly's convention) or `Cache-Tag` (Cloudflare's). When product 123 changes, one API call purges every cached page that included it: the product page, category listings, search results. Fastly built much of its reputation on fast, tag-based purging, and it's the key enabler for caching dynamic content for long periods.
- **Soft purge.** Mark content stale rather than deleting it, so `stale-while-revalidate` and `stale-if-error` still apply. This avoids a sudden flood of misses to the origin right after a large purge.

Combining these gives a powerful pattern for dynamic sites: cache pages at the CDN with long TTLs (via `CDN-Cache-Control`), tag every response with the entities it contains, purge by tag when data changes, and keep browser TTLs short. The origin renders each page roughly once per change instead of once per visitor.

## 9. When caching becomes a security bug

Shared caches serve one stored response to many users. If an attacker can control what gets stored, or trick the cache into storing something private, the cache amplifies the attack to everyone.

### Web cache poisoning

James Kettle of PortSwigger popularized this class of attack with his 2018 research "Practical Web Cache Poisoning." The core problem is an **unkeyed input**: a part of the request that changes the response but isn't part of the cache key.

A classic example: an application builds absolute URLs using the `X-Forwarded-Host` header, so a page includes `<script src="https://{X-Forwarded-Host}/app.js">`. The CDN's cache key is just the URL. An attacker sends a request for the home page with `X-Forwarded-Host: evil.example`. The response, with a script tag pointing at the attacker's server, gets cached under the normal home page key. Every visitor for the next N minutes receives the poisoned page and runs the attacker's script.

Kettle's follow-up research, "Web Cache Entanglement" (2020), found more ways to create such mismatches: caches and applications parsing URLs differently, query parameters the cache ignores but the application uses, and fat GET requests with bodies.

Defenses:

- **Don't use request headers to build responses unless they're in the cache key.** Configure the CDN to drop or overwrite headers like `X-Forwarded-Host` before they reach the application.
- **Make keys match what the application actually uses**, including any query parameters it reads.
- **Don't cache error pages or redirects for long** if they might reflect attacker input.

### Web cache deception

Omer Gil described the reverse in 2017: tricking a cache into storing a **private** page. Suppose a CDN caches anything ending in `.css`, and the application ignores trailing path segments, so `https://bank.example/account/settings/nonexistent.css` returns the user's settings page. An attacker sends a victim that link. The victim's browser loads it with their session cookie, the application returns their private page, and the CDN, seeing `.css`, caches it publicly. The attacker then requests the same URL and gets the victim's page from the cache.

Defenses:

- **Base caching on the origin's `Cache-Control` headers**, not on file extensions or path patterns. Make sure private pages send `Cache-Control: private` or `no-store`.
- **Return 404 for unknown paths** rather than silently serving a parent route.
- **Check content types**: a CDN rule that caches `.css` should require the response to actually be `text/css`. Several CDNs added this kind of check after the research.

The common lesson: **the cache and the origin must agree on what a request means**. Any mismatch in how they parse the URL, which headers matter, or what's private is a potential vulnerability.

## 10. A caching checklist

1. **Fingerprint static assets** and serve them with `max-age=31536000, immutable`.
2. **Send `no-cache` plus an ETag for HTML** so deploys appear immediately and revalidation is cheap.
3. **Set `Cache-Control` explicitly on every response.** Don't rely on heuristics. Use `private` or `no-store` for anything user-specific.
4. **Use `stale-while-revalidate` and `stale-if-error`** for shared content.
5. **Design cache keys deliberately**: normalize query strings, strip irrelevant cookies, set `Vary` correctly, and avoid `Vary: User-Agent`.
6. **Use tiered caching and request collapsing** to protect the origin.
7. **Separate CDN and browser lifetimes** with `CDN-Cache-Control` or `s-maxage`, and **purge by surrogate key** when data changes.
8. **Audit for unkeyed inputs** and path confusion; never let headers like `X-Forwarded-Host` reach the app unchecked.
9. **Monitor** hit ratio by requests and bytes at the edge and the origin, plus `Cache-Status` in debugging.

## Key takeaways

- A cache serves a stored response while it's fresh, then revalidates it cheaply with ETags and `304 Not Modified`.
- `no-cache` means "revalidate before use," not "don't store"; `no-store` means don't store.
- Fingerprinted URLs with `immutable` are the most effective caching pattern, and HTML should revalidate on every load.
- `stale-while-revalidate` hides origin latency and stampedes; `stale-if-error` keeps a site up through origin outages.
- The cache key (URL plus `Vary` headers plus CDN rules) decides who shares a response. Too broad leaks data; too narrow wastes the cache.
- CDNs add tiered caching, request collapsing, targeted headers (RFC 9213) and tag-based purging.
- Cache poisoning exploits inputs that change a response but aren't in the key; cache deception tricks a cache into storing private pages. Both come from the cache and origin disagreeing about what a request means.

## Discussion questions

1. A teammate set `Cache-Control: no-cache` on a page with account details, thinking it prevents caching. What actually happens, and what should it be?
2. Your CDN reports a 92% hit ratio, but origin bandwidth hasn't dropped. What might explain that, and what would you measure?
3. You deploy a new version and some users see a broken page for an hour: new HTML with old JavaScript. What caching setup causes this, and how do you fix it?
4. When would you choose short TTLs over surrogate-key purging, and vice versa?
5. Find an unkeyed input in a typical setup: which request headers does your application read that your CDN doesn't include in the cache key?
6. Why does serving stale content on errors sometimes make outages worse instead of better?
7. Should API responses for logged-in users ever go through a shared cache? Under what conditions?

## Sources

- RFC 9111, "HTTP Caching" (June 2022), https://www.rfc-editor.org/rfc/rfc9111.html ; RFC 9110, "HTTP Semantics" (conditional requests).
- RFC 5861, "HTTP Cache-Control Extensions for Stale Content" (2010); RFC 8246, "HTTP Immutable Responses" (2017).
- RFC 9213, "Targeted HTTP Cache Control" (June 2022), https://www.rfc-editor.org/rfc/rfc9213.html ; RFC 9211, "The Cache-Status HTTP Response Header Field" (June 2022).
- MDN Web Docs, "HTTP caching" and "Cache-Control," https://developer.mozilla.org/en-US/docs/Web/HTTP/Caching
- Mark Nottingham, "Caching Tutorial for Web Authors and Webmasters," https://www.mnot.net/cache_docs/
- James Kettle, "Practical Web Cache Poisoning," PortSwigger Research, 2018, https://portswigger.net/research/practical-web-cache-poisoning ; "Web Cache Entanglement: Novel Pathways to Poisoning," 2020, https://portswigger.net/research/web-cache-entanglement
- Omer Gil, "Web Cache Deception Attack," Black Hat USA 2017.
- Fastly documentation, "Purging" and "Surrogate keys"; Cloudflare documentation, "Tiered Cache" and "Purge cache by cache-tags"; Amazon CloudFront documentation, "Origin Shield."
- Varnish Cache documentation, "Request coalescing" and grace mode.

The RFC 9213 publication date was confirmed against the RFC Editor page on Nov 5, 2026, and the PortSwigger research pages were located the same day. CDN product names and features are from vendor documentation as I know it, so check the current docs for exact behavior.
