# The Daily Commit

Deep dives on how real systems work, plus AI news, served by GitHub Pages from `main`.
The HTML is generated and committed; run `python3 tools/build.py` after changing anything in `content/`.

- `content/reading/` – one Markdown file per deep dive (front matter: title, date, summary, tags). The first tag is its main topic.
- `content/diagrams/` – one SVG per diagram, inlined where an article has `<!--diagram:ID-->`; `src/` keeps the JSX each was rendered from
- `content/news/` – one AI news digest per day, named `YYYY-MM-DD.md` (see below). The AI News section only appears once this folder has a digest.
- `style.css`, `site.js` – shared styles and the small script for the Aa menu, reading progress and copy buttons
- `auth.js`, `vendor/supabase.js`, `supabase/schema.sql` – optional Sign in with Google (Supabase) for read marks and resume. Off until `SUPABASE_URL` and `SUPABASE_ANON_KEY` are set in `tools/build.py`.
- Generated: `index.html`, `reading/`, `topics/`, `news/`

## News digest format

```markdown
---
date: 2026-10-24
---

## Open-weights 70B model matches frontier systems on coding benchmarks
category: Models
source: Lab blog | https://example.com/post
why: Narrows the gap for teams that need to self-host.

One-paragraph summary. Links like [this](https://example.com) are allowed.
```

Each story starts with `## Title`; `category`, `source` (`Name | URL`, URL optional) and `why` are optional.
Stories are grouped on the page by `category` in the order they first appear (the daily brief uses Top stories, Built with AI, AI giants).
The AI News pages use the Modernist design via `news.css`, loaded on top of `style.css`.
