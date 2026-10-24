"""Build the static site from content/ into plain HTML at the repo root.

Each Markdown file in content/reading/ becomes reading/<slug>.html. The home
page and reading index are regenerated from the front matter. Run from the
repo root:  python3 tools/build.py
"""
import html
import math
import os
import re
import subprocess
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_TITLE = "Shakti's Knowledge Base"
SITE_DESC = 'Long-form notes on how real systems work: databases, distributed systems, networking and security.'
DIAGRAMS = os.path.join(ROOT, 'content', 'diagrams')


def page(title, body, current, description=SITE_DESC):
    nav = [('/', 'Home'), ('/reading/', 'Reading')]
    links = '\n      '.join(
        f'<a href="{href}"{" aria-current=\"page\"" if href == current else ""}>{label}</a>'
        for href, label in nav
    )
    full_title = title if title == SITE_TITLE else f'{title} · {SITE_TITLE}'
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(full_title)}</title>
  <meta name="description" content="{html.escape(description)}">
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <header class="site"><div class="wrap">
    <a class="brand" href="/">{html.escape(SITE_TITLE)}</a>
    <nav>
      {links}
    </nav>
  </div></header>

  <main><div class="wrap">
{body}
  </div></main>

  <footer class="site"><div class="wrap">© {date.today().year} Shakti</div></footer>
</body>
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


def fmt_date(iso):
    d = date.fromisoformat(iso)
    return d.strftime('%B %-d, %Y')


def diagram(match):
    ref = match.group(1)
    path = os.path.join(DIAGRAMS, ref + '.svg')
    if not os.path.exists(path):
        return ''
    svg = open(path, encoding='utf-8').read().strip()
    return f'<figure class="diagram">{svg}</figure>'


def render_markdown(md):
    out = subprocess.run(
        ['pandoc', '--from=markdown+tex_math_dollars-raw_tex', '--to=html5', '--mathml',
         '--toc', '--toc-depth=2', '--template', os.path.join(ROOT, 'tools', 'article.tpl'),
         '--no-highlight'],
        input=md, capture_output=True, text=True, check=True,
    ).stdout
    return re.sub(r'<!--diagram:([\w-]+)-->', diagram, out)


def tag_list(tags):
    return ''.join(f'<span class="tag">{html.escape(t)}</span>' for t in tags)


def list_item(a):
    return (f'<li><a class="title" href="/reading/{a["slug"]}.html">{html.escape(a["title"])}</a>'
            f'<span class="desc">{html.escape(a["summary"])}</span>'
            f'<span class="meta-line">{fmt_date(a["date"])} · {a["minutes"]} min read{tag_list(a["tags"])}</span></li>')


def build():
    src = os.path.join(ROOT, 'content', 'reading')
    out_dir = os.path.join(ROOT, 'reading')
    os.makedirs(out_dir, exist_ok=True)

    articles = []
    for name in sorted(os.listdir(src)):
        if not name.endswith('.md'):
            continue
        meta, body = read_front_matter(open(os.path.join(src, name), encoding='utf-8').read())
        meta['slug'] = name[:-3]
        meta['body'] = body
        meta['minutes'] = max(1, math.ceil(len(re.findall(r'\w+', body)) / 230))
        articles.append(meta)
    articles.sort(key=lambda a: (a['date'], a['title']), reverse=True)

    for i, a in enumerate(articles):
        newer = articles[i - 1] if i > 0 else None
        older = articles[i + 1] if i + 1 < len(articles) else None
        content = render_markdown(a['body'])
        pager = '<nav class="pager">'
        pager += (f'<a class="prev" href="/reading/{older["slug"]}.html"><span>Older</span>{html.escape(older["title"])}</a>'
                  if older else '<span></span>')
        pager += (f'<a class="next" href="/reading/{newer["slug"]}.html"><span>Newer</span>{html.escape(newer["title"])}</a>'
                  if newer else '<span></span>')
        pager += '</nav>'
        body = f'''    <article class="post">
      <p class="crumb"><a href="/reading/">Reading</a></p>
      <h1>{html.escape(a["title"])}</h1>
      <p class="meta">{fmt_date(a["date"])} · {a["minutes"]} min read{tag_list(a["tags"])}</p>
{content}
    </article>
    {pager}'''
        with open(os.path.join(out_dir, a['slug'] + '.html'), 'w', encoding='utf-8') as f:
            f.write(page(a['title'], body, '/reading/', a['summary']))

    # Reading index, grouped by tag order of first appearance.
    items = '\n      '.join(list_item(a) for a in articles)
    body = f'''    <h1>Reading</h1>
    <p class="lede">{len(articles)} long reads on how real systems work, newest first. Each one ends with ideas to carry away and questions to think about.</p>
    <ul class="list">
      {items}
    </ul>'''
    with open(os.path.join(out_dir, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(page('Reading', body, '/reading/'))

    # Home page.
    tags = {}
    for a in articles:
        for t in a['tags']:
            tags[t] = tags.get(t, 0) + 1
    topics = ''.join(f'<span class="tag">{html.escape(t)} · {n}</span>'
                     for t, n in sorted(tags.items(), key=lambda kv: (-kv[1], kv[0])))
    latest = '\n      '.join(list_item(a) for a in articles[:5])
    body = f'''    <h1>Notes on how systems really work</h1>
    <p class="lede">A public notebook of long reads on databases, distributed systems, networking and security, each built around real papers and engineering write-ups.</p>
    <p class="topics">{topics}</p>

    <h2>Latest reads</h2>
    <ul class="list">
      {latest}
    </ul>
    <p class="more"><a href="/reading/">All {len(articles)} reads →</a></p>'''
    with open(os.path.join(ROOT, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(page(SITE_TITLE, body, '/'))

    print(f'Built {len(articles)} articles.')


if __name__ == '__main__':
    build()
