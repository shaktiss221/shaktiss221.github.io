"""Build the static site from content/ into plain HTML at the repo root.

Each Markdown file in content/reading/ becomes reading/<slug>.html, and each
file in content/news/ (one AI news digest per day) becomes news/<date>.html.
The front page, archive, topic pages and news index are regenerated from the
front matter. Run from the repo root:  python3 tools/build.py
"""
import html
import math
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_TITLE = 'The Daily Commit'
SITE_DESC = 'Deep dives on how real systems work: databases, distributed systems, networking and security.'
DIAGRAMS = os.path.join(ROOT, 'content', 'diagrams')
# Supabase project for the optional Sign in with Google. Both values are public
# (the anon key only reaches rows the database rules allow); leave them empty
# to build the site without sign-in.
SUPABASE_URL = ''
SUPABASE_ANON_KEY = ''
FONTS = ('https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;'
         '0,6..72,600;1,6..72,400&family=Instrument+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap')
TOPIC_BLURBS = {
    'databases': 'Storage engines, indexing, transactions and the systems built on them.',
    'distributed systems': 'Replication, partitioning, failure and getting many machines to agree.',
    'caching': 'Keeping hot data close, and keeping it correct.',
    'networking': 'Protocols on the wire, from handshakes to streams.',
    'security': 'Cryptography in practice, secrets and trust.',
    'apis': 'The contracts between services, and how they hold up under load.',
    'analytics': 'Engines built to scan billions of rows fast.',
    'infrastructure': 'The platform pieces every service leans on.',
    'messaging': 'Queues, logs and delivery guarantees.',
    'case study': 'How one real company solved one real problem.',
}

# AI News pages wear the Modernist design (Archivo, 2px rules, one red accent) on top of style.css.
SKINS = {
    '': '',
    'mod': ('\n  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;800&display=swap">'
            '\n  <link rel="stylesheet" href="/news.css">'),
}

ICONS = {
    'right': '<path d="M5 12h14M13 6l6 6-6 6"/>',
    'left': '<path d="M19 12H5M11 6l-6 6 6 6"/>',
    'clock': '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    'check': '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
}


def icon(name):
    return (f'<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>')


def esc(s):
    return html.escape(s, quote=True)


def slugify(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def page(title, body, current, has_news, description=SITE_DESC, skin=''):
    nav = [('/', 'Today'), ('/reading/', 'Archive'), ('/topics/', 'Topics')]
    if has_news:
        nav.append(('/news/', 'AI News'))
    links = '\n        '.join(
        f'<a href="{href}"{" aria-current=\"page\"" if href == current else ""}>{label}</a>'
        for href, label in nav
    )
    full_title = title if title == SITE_TITLE else f'{title} · {SITE_TITLE}'
    tagline = 'Deep dives on how systems work, plus the AI news.' if has_news else 'Deep dives on how real systems work.'
    account = ('<button type="button" class="signin" data-auth hidden>Sign in</button><div class="pop" id="account" hidden></div>'
               if SUPABASE_URL else '')
    auth = (f'  <script>window.DC_AUTH={{url:"{SUPABASE_URL}",key:"{SUPABASE_ANON_KEY}"}}</script>\n'
            '  <script src="/vendor/supabase.js"></script>\n  <script src="/auth.js"></script>\n' if SUPABASE_URL else '')
    sizes = ''.join(f'<button type="button" data-size="{v}">{l}</button>' for l, v in [('S', 17), ('M', 19), ('L', 21), ('XL', 23)])
    themes = ''.join(
        f'<button type="button" class="swatch" data-theme-pick="{k}" style="background:{bg};color:{fg}">{l}</button>'
        for k, l, bg, fg in [('paper', 'Paper', '#f7f4ee', '#25211c'), ('sepia', 'Sepia', '#f1e7d6', '#3a2e21'), ('night', 'Night', '#191714', '#e9e3d8')]
    )
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(full_title)}</title>
  <meta name="description" content="{esc(description)}">
  <script>try{{var t=localStorage.getItem('dc-theme'),s=localStorage.getItem('dc-size');if(t)document.documentElement.dataset.theme=t;if(s)document.documentElement.style.setProperty('--read-size',s+'px')}}catch(e){{}}</script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="{FONTS}">
  <link rel="stylesheet" href="/style.css">{SKINS[skin]}
</head>
<body{f' class="{skin}"' if skin else ''}>
  <header class="hdr"><div class="wrap hdr-in">
      <a class="brand" href="/">{SITE_TITLE}<i>.</i></a>
      <nav class="nav" aria-label="Sections">
        {links}
      </nav>
      <div class="tools">
        {account}<button type="button" class="icon-btn" data-settings aria-label="Reading settings" aria-expanded="false" aria-controls="settings">Aa</button>
        <div class="pop" id="settings" hidden>
          <div><div class="kicker" style="margin-bottom:8px">Text size</div><div class="seg">{sizes}</div></div>
          <div><div class="kicker" style="margin-bottom:8px">Theme</div><div class="swatches">{themes}</div></div>
          <button type="button" class="mono muted" data-theme-pick="" style="text-align:left">Match my system</button>
        </div>
      </div>
  </div></header>

  <main>
{body}
  </main>

  <footer class="ftr"><div class="wrap">
    <span class="name">{SITE_TITLE}<span style="color:var(--accent)">.</span></span>
    <span class="mono muted">{tagline}</span>
  </div></footer>
  <script src="/site.js"></script>
{auth}</body>
</html>
'''


def read_front_matter(text):
    m = re.match(r'---\n(.*?)\n---\n', text, re.S)
    meta = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(':')
        meta[k.strip()] = v.strip().strip('"')
    meta['tags'] = [t.strip() for t in meta.get('tags', '').split(',') if t.strip()]
    return meta, text[m.end():]


def fmt_date(iso, style='long'):
    d = date.fromisoformat(iso)
    return {
        'long': d.strftime('%A, %B %-d'),
        'full': d.strftime('%B %-d, %Y'),
        'short': d.strftime('%b %-d'),
        'row': d.strftime('%a, %b %-d'),
        'month': d.strftime('%B %Y'),
    }[style]


def diagram(match):
    ref = match.group(1)
    path = os.path.join(DIAGRAMS, ref + '.svg')
    if not os.path.exists(path):
        return ''
    svg = open(path, encoding='utf-8').read().strip()
    return f'<figure class="diagram">{svg}</figure>'


def render_markdown(md):
    out = subprocess.run(
        ['pandoc', '--from=markdown+tex_math_dollars-raw_tex', '--to=html5', '--mathml', '--no-highlight'],
        input=md, capture_output=True, text=True, check=True,
    ).stdout
    return re.sub(r'<!--diagram:([\w-]+)-->', diagram, out)


def sections(body_html):
    """The article's h2 headings as (id, label), without their "1." numbering."""
    out = []
    for hid, inner in re.findall(r'<h2 id="([^"]+)"[^>]*>(.*?)</h2>', body_html, re.S):
        label = html.unescape(re.sub(r'<[^>]+>', '', inner)).strip()
        out.append((hid, re.sub(r'^\d+\.\s*', '', label)))
    return out


def inline_md(s):
    """Just enough Markdown for news summaries: links, code and bold."""
    s = esc(s)
    s = re.sub(r'\[([^\]]+)\]\((https?://[^)\s]+)\)', r'<a href="\2">\1</a>', s)
    s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
    return re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', s)


def read_news(path):
    """One digest: front matter, then a "## Title" per story with key: value
    lines (category, source as "Name | URL", why) and a summary paragraph."""
    meta, body = read_front_matter(open(path, encoding='utf-8').read())
    items = []
    for chunk in re.split(r'^## ', body, flags=re.M)[1:]:
        lines = chunk.strip().splitlines()
        item = {'title': lines[0].strip(), 'category': '', 'source': '', 'url': '', 'why': ''}
        rest = lines[1:]
        while rest and re.match(r'^(category|source|why):', rest[0], re.I):
            k, _, v = rest.pop(0).partition(':')
            item[k.strip().lower()] = v.strip()
        if '|' in item['source']:
            item['source'], item['url'] = [x.strip() for x in item['source'].split('|', 1)]
        item['summary'] = ' '.join(x.strip() for x in rest if x.strip())
        items.append(item)
    return {'date': meta['date'], 'items': items}


def row(a, date_style='short', show_summary=False):
    summary = f'<p>{esc(a["summary"])}</p>' if show_summary else ''
    return (f'<a class="row-item" data-slug="{a["slug"]}" href="/reading/{a["slug"]}.html">'
            f'<span class="mono muted">{fmt_date(a["date"], date_style)}</span>'
            f'<div><h3>{esc(a["title"])}</h3>{summary}'
            f'<div class="mono muted sub">{esc(a["topic"])} · {a["minutes"]} min<span class="read-mark"> · {icon("check")}Read</span></div></div></a>')


def write(rel, text):
    path = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def build():
    src = os.path.join(ROOT, 'content', 'reading')
    articles = []
    for name in sorted(os.listdir(src)):
        if not name.endswith('.md'):
            continue
        meta, body = read_front_matter(open(os.path.join(src, name), encoding='utf-8').read())
        meta['slug'] = name[:-3]
        meta['minutes'] = max(1, math.ceil(len(re.findall(r'\w+', body)) / 230))
        meta['html'] = render_markdown(body)
        meta['sections'] = sections(meta['html'])
        meta['topic'] = (meta['tags'] or ['notes'])[0].title().replace('Apis', 'APIs')
        articles.append(meta)
    articles.sort(key=lambda a: (a['date'], a['title']), reverse=True)

    news_dir = os.path.join(ROOT, 'content', 'news')
    digests = sorted((read_news(os.path.join(news_dir, n)) for n in os.listdir(news_dir) if n.endswith('.md')),
                     key=lambda d: d['date'], reverse=True) if os.path.isdir(news_dir) else []
    has_news = bool(digests)

    topics = {}
    for a in articles:
        for t in a['tags']:
            topics.setdefault(t, []).append(a)
    topic_name = lambda t: t.title().replace('Apis', 'APIs')
    topic_order = sorted(topics, key=lambda t: (-len(topics[t]), t))

    # Article pages.
    for i, a in enumerate(articles):
        newer = articles[i - 1] if i > 0 else None
        older = articles[i + 1] if i + 1 < len(articles) else None
        toc = ''.join(f'<a href="#{hid}">{esc(label)}</a>' for hid, label in a['sections'])
        nav = (f'<a href="/reading/{older["slug"]}.html"><div class="kicker">← Older · {fmt_date(older["date"], "short")}</div>'
               f'<div class="t">{esc(older["title"])}</div></a>') if older else '<span></span>'
        if newer:
            nav += (f'<a href="/reading/{newer["slug"]}.html"><div class="kicker">Newer · {fmt_date(newer["date"], "short")} →</div>'
                    f'<div class="t">{esc(newer["title"])}</div></a>')
        elif has_news:
            nav += (f'<a href="/news/"><div class="kicker">Also today →</div>'
                    f'<div class="t">AI News: {len(digests[0]["items"])} stories</div></a>')
        chips = ''.join(f'<a class="chip" href="/topics/{slugify(t)}.html">{esc(topic_name(t))}</a>' for t in a['tags'])
        body = f'''    <div class="progress"></div>
    <div class="rbar"><div class="wrap rbar-in">
      <a class="link-btn mono" href="/reading/" style="font-size:12px">{icon("left")}Archive</a>
      <span class="mono muted now" data-now>{esc(a["title"])}</span>
      <button type="button" class="mono resume" data-resume hidden></button>
      <span class="mono left">{icon("clock")}<span data-left>{a["minutes"]} min left</span></span>
    </div></div>
    <div class="wrap reader-grid">
      <nav class="toc" aria-label="Contents">
        <div class="kicker" style="margin-bottom:12px">Contents</div>
        {toc}
        <div class="mono muted" style="margin-top:18px" data-pct>0% · {a["minutes"]} min left</div>
      </nav>
      <article class="article" data-slug="{a["slug"]}" data-minutes="{a["minutes"]}">
        <div class="kicker acc" style="margin-bottom:18px"><a href="/topics/{slugify(a["tags"][0])}.html">{esc(a["topic"])}</a> · {fmt_date(a["date"], "full")} · {a["minutes"]} min</div>
        <h1>{esc(a["title"])}</h1>
        <p class="dek">{esc(a["summary"])}</p>
{a["html"]}
        <div class="end-card">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap"><div class="big">You reached the end.</div>
          <button type="button" class="btn btn-solid" data-mark hidden>{icon("check")}<span>Mark as read</span></button></div>
          <div class="chips" style="margin-top:14px"><span class="kicker" style="align-self:center;margin-right:4px">Filed under</span>{chips}</div>
          <div class="daynav">{nav}</div>
        </div>
      </article>
      <aside class="reader-aside"></aside>
    </div>'''
        write(f'reading/{a["slug"]}.html', page(a['title'], body, '/reading/', has_news, a['summary']))

    # Archive, grouped by month.
    groups, seen = [], {}
    for a in articles:
        m = fmt_date(a['date'], 'month')
        if m not in seen:
            seen[m] = []
            groups.append((m, seen[m]))
        seen[m].append(a)
    total = sum(a['minutes'] for a in articles)
    chips = ''.join(f'<a class="chip" href="/topics/{slugify(t)}.html">{esc(topic_name(t))}</a>' for t in topic_order)
    blocks = '\n'.join(
        f'    <section class="group"><div class="section-hd"><span class="kicker">{m}</span>'
        f'<span class="mono muted">{len(items)} deep dives</span></div>\n      '
        + '\n      '.join(row(a, 'row', True) for a in items) + '\n    </section>'
        for m, items in groups)
    body = f'''    <div class="wrap page">
    <div class="kicker">Archive</div>
    <h1 class="page-title">Every deep dive</h1>
    <p class="muted" style="margin-top:12px">{len(articles)} deep dives · about {round(total / 60)} hours of reading</p>
    <div class="chips" style="margin-top:24px">{chips}</div>
{blocks}
    </div>'''
    write('reading/index.html', page('Archive', body, '/reading/', has_news))

    # Topics.
    shutil.rmtree(os.path.join(ROOT, 'topics'), ignore_errors=True)
    cards = '\n      '.join(
        f'<a class="tcard" href="/topics/{slugify(t)}.html"><span class="mono muted">{len(topics[t])} deep dive{"s" if len(topics[t]) > 1 else ""}</span>'
        f'<h3>{esc(topic_name(t))}</h3><span>{esc(TOPIC_BLURBS.get(t.lower(), ""))}</span></a>'
        for t in topic_order)
    body = f'''    <div class="wrap page">
    <div class="kicker">Topics</div>
    <h1 class="page-title">Browse by topic</h1>
    <div class="tcards">
      {cards}
    </div>
    </div>'''
    write('topics/index.html', page('Topics', body, '/topics/', has_news))
    for t in topic_order:
        items = topics[t]
        others = ''.join(f'<a class="chip" href="/topics/{slugify(o)}.html">{esc(topic_name(o))}</a>' for o in topic_order if o != t)
        rows = '\n      '.join(row(a, 'short', True) for a in items)
        body = f'''    <div class="wrap page">
    <a class="link-btn mono" href="/topics/" style="font-size:12px">{icon("left")}All topics</a>
    <h1 class="page-title" style="margin-top:20px">{esc(topic_name(t))}</h1>
    <p class="muted" style="margin-top:12px;font-size:17px;max-width:620px">{esc(TOPIC_BLURBS.get(t.lower(), ""))}</p>
    <p class="mono muted" style="margin-top:16px">{len(items)} deep dive{"s" if len(items) > 1 else ""} · {sum(a["minutes"] for a in items)} min of reading</p>
    <section class="group"><div class="section-hd"><span class="kicker">Newest first</span></div>
      {rows}
    </section>
    <div class="chips" style="margin-top:40px"><span class="kicker" style="align-self:center;margin-right:6px">Other topics</span>{others}</div>
    </div>'''
        write(f'topics/{slugify(t)}.html', page(topic_name(t), body, '/topics/', has_news, TOPIC_BLURBS.get(t.lower(), SITE_DESC)))

    # AI news digests: stories grouped by category, each group a ruled grid of cells.
    shutil.rmtree(os.path.join(ROOT, 'news'), ignore_errors=True)
    for i, d in enumerate(digests):
        newer = digests[i - 1] if i > 0 else None
        older = digests[i + 1] if i + 1 < len(digests) else None
        groups = {}
        for it in d['items']:
            groups.setdefault(it['category'] or 'Stories', []).append(it)

        def cell(n, it):
            src = ''
            if it['source']:
                src = (f'<a class="src" href="{esc(it["url"])}" rel="noopener">{esc(it["source"])} ↗</a>' if it['url']
                       else f'<span class="src">{esc(it["source"])}</span>')
            why = f'<p class="why"><b>Why it matters</b> {inline_md(it["why"])}</p>' if it['why'] else ''
            return (f'<article class="news-cell"><span class="n">{n:02d}</span><h3>{esc(it["title"])}</h3>'
                    f'<p class="sum">{inline_md(it["summary"])}</p>{why}{src}</article>')

        secs = ''
        stats = ''
        for g, (name, its) in enumerate(groups.items()):
            slug = slugify(name)
            stats += f'<a class="stat" href="#{slug}"><span class="num">{len(its):02d}</span><span class="lbl">{esc(name)}</span></a>'
            secs += (f'<section class="news-sec" id="{slug}"><div class="sec-hd"><span class="sec-n">{g + 1:02d}</span>'
                         f'<h2>{esc(name)}</h2><span class="sec-c">{len(its)} stories</span></div>'
                         f'<div class="cells">{"".join(cell(n + 1, it) for n, it in enumerate(its))}</div></section>')
        prev_btn = (f'<a class="btn btn-line" href="/news/{older["date"]}.html">{icon("left")}{fmt_date(older["date"], "short")}</a>'
                    if older else f'<span class="btn btn-line off">{icon("left")}Oldest</span>')
        next_btn = (f'<a class="btn btn-line" href="/news/{"" if i == 1 else newer["date"] + ".html"}">{fmt_date(newer["date"], "short")}{icon("right")}</a>'
                    if newer else f'<span class="btn btn-line off">Latest{icon("right")}</span>')
        earlier = ''
        if i == 0 and len(digests) > 1:
            earlier = ('<section class="news-sec"><div class="sec-hd"><span class="sec-n">—</span><h2>Earlier digests</h2>'
                       f'<span class="sec-c">{len(digests) - 1} days</span></div><div class="earlier">'
                       + ''.join(f'<a class="row-item" href="/news/{e["date"]}.html"><span class="mono muted">{fmt_date(e["date"], "row")}</span>'
                                 f'<div><h3>{esc(e["items"][0]["title"]) if e["items"] else ""}</h3>'
                                 f'<div class="mono muted sub">{len(e["items"])} stories</div></div></a>' for e in digests[1:30])
                       + '</div></section>')
        body = f'''    <div class="wrap news-page">
    <div class="news-hd">
      <div><div class="kicker acc">AI News · daily digest</div>
      <h1 class="page-title">{fmt_date(d["date"], "long")}</h1></div>
      <div class="pager-btns">{prev_btn}{next_btn}</div>
    </div>
    <div class="stats">{stats}<div class="stat"><span class="num">{math.ceil(len(d["items"]) * 0.6)}</span><span class="lbl">Minutes to read</span></div></div>
    {secs}
    {earlier}
    </div>'''
        title = f'AI News, {fmt_date(d["date"], "full")}'
        write(f'news/{d["date"]}.html', page(title, body, '/news/', has_news, skin='mod'))
        if i == 0:
            write('news/index.html', page('AI News', body, '/news/', has_news, skin='mod'))

    # Front page: today's deep dive, the latest news (or topics), then earlier reads.
    # Articles dated after today (India time) wait in the archive until their day;
    # on a day with two articles, the one marked `daily: true` leads.
    today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date().isoformat()
    current = [a for a in articles if a['date'] <= today] or articles
    current.sort(key=lambda a: (a['date'], a.get('daily') == 'true'), reverse=True)
    lead = current[0]
    preview = ''.join(f'<li><span class="n">{n + 1:02d}</span><a href="/reading/{lead["slug"]}.html#{hid}">{esc(label)}</a></li>'
                      for n, (hid, label) in enumerate(lead['sections']))
    if has_news:
        d = digests[0]
        rail = (f'<div class="section-hd" style="border-bottom:0;padding-bottom:0"><span class="kicker acc">AI News</span>'
                f'<span class="mono muted">{len(d["items"])} stories</span></div>'
                + ''.join(f'<a class="rail-item" href="/news/"><div class="kicker" style="font-size:10.5px">{esc(it["category"])}</div>'
                          f'<div class="t">{esc(it["title"])}</div></a>' for it in d['items'][:6])
                + f'<a class="link-btn" href="/news/" style="margin-top:18px">Read the full digest {icon("right")}</a>')
    else:
        rail = ('<div class="section-hd" style="border-bottom:0;padding-bottom:0"><span class="kicker acc">Browse by topic</span>'
                f'<span class="mono muted">{len(articles)} deep dives</span></div>'
                + ''.join(f'<a class="rail-item" href="/topics/{slugify(t)}.html"><div class="kicker" style="font-size:10.5px">'
                          f'{len(topics[t])} deep dive{"s" if len(topics[t]) > 1 else ""}</div>'
                          f'<div class="t">{esc(topic_name(t))}</div><div class="d">{esc(TOPIC_BLURBS.get(t.lower(), ""))}</div></a>'
                          for t in topic_order[:6])
                + f'<a class="link-btn" href="/topics/" style="margin-top:18px">All topics {icon("right")}</a>')
    earlier = '\n      '.join(row(a) for a in current[1:7])
    body = f'''    <div class="wrap page">
    <div class="kicker" style="margin-bottom:28px">{fmt_date(lead["date"], "long")}</div>
    <div class="front-grid">
      <article class="lead">
        <div class="kicker acc" style="margin-bottom:16px">Latest deep dive · {esc(lead["topic"])} · {lead["minutes"]} min read</div>
        <h1><a href="/reading/{lead["slug"]}.html">{esc(lead["title"])}</a></h1>
        <p class="dek">{esc(lead["summary"])}</p>
        <div style="margin-top:28px"><a class="btn btn-solid" data-start data-slug="{lead["slug"]}" data-minutes="{lead["minutes"]}" href="/reading/{lead["slug"]}.html">Start reading {icon("right")}</a></div>
        <div class="in-doc"><div class="kicker" style="margin-bottom:14px">In this deep dive</div><ol>{preview}</ol></div>
      </article>
      <aside class="rail">{rail}</aside>
    </div>
    <section class="rows">
      <div class="section-hd"><span class="kicker">Earlier</span><a class="link-btn" href="/reading/">Full archive {icon("right")}</a></div>
      {earlier}
    </section>
    </div>'''
    write('index.html', page(SITE_TITLE, body, '/', has_news))

    print(f'Built {len(articles)} articles, {len(topics)} topics, {len(digests)} news digests.')


if __name__ == '__main__':
    build()
