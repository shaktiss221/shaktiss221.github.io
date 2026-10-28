# The Daily Commit

Deep dives on how real systems work, plus AI news, served by GitHub Pages from `main`.
The HTML is generated and committed; run `python3 tools/build.py` after changing anything in `content/`.

- `content/reading/` – one Markdown file per deep dive (front matter: title, date, summary, tags, optional `daily: true`). The first tag is its main topic. The front page leads with the newest article dated today or earlier (India time), preferring the one marked `daily: true`; future-dated articles wait in the archive until their day.
- `content/diagrams/` – one SVG per diagram, inlined where an article has `<!--diagram:ID-->`; `src/` keeps the JSX each was rendered from
- `content/news/` – one AI news digest per day, named `YYYY-MM-DD.md` (see below). The AI News section only appears once this folder has a digest.
- `content/ai-first/` – the Becoming AI First path, one file per week named `week-NN.md` (see below). Days are numbered across weeks in order. The section, its nav item and the front-page strip only appear once this folder has a week.
- `style.css`, `site.js` – shared styles and the small script for the Aa menu, reading progress and copy buttons
- `ai-first.js` – marks AI First days done, picks today's session and counts the streak; progress is kept in the reader's browser (`localStorage`, key `dc-aif`)
- `auth.js`, `vendor/supabase.js`, `supabase/schema.sql` – optional Sign in with Google (Supabase) for read marks and resume. Off until `SUPABASE_URL` and `SUPABASE_ANON_KEY` are set in `tools/build.py`.
- Page views: set `GOATCOUNTER` in `tools/build.py` to the GoatCounter site code and rebuild; counts show at `https://<code>.goatcounter.com`.
- Generated: `index.html`, `reading/`, `topics/`, `news/`, `ai-first/`

## AI First week format

```markdown
---
week: 3
phase: Build with model APIs
title: Calling models from code
goal: One sentence on what this week builds.
---

## Structured output with a tool
learn: 10 | Claude docs: tool use overview | https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
do: 17 | What to build or try. `code` and [links](https://example.com) are allowed.
note: 3 | What to write in the log.
also: Optional extra resource | https://example.com
```

Each day starts with `## Title` and has `learn`, `do` and `note` steps (any of them can repeat or be left out) whose minutes add up to 30; the build warns when they don't. `also` lines are optional extras shown under the plan. Consecutive weeks with the same `phase` are grouped on the overview page.

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
