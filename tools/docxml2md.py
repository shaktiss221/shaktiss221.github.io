"""Convert a Claude Docs XML export into a Markdown file with front matter.

Usage: docxml2md.py <tool-result.json | doc.xml> <out.md> --summary "..." --tags "a, b"
"""
import argparse
import json
import re
import xml.etree.ElementTree as ET

MD_SPECIAL = re.compile(r'([\\`*_\[\]<>$])')


def esc(s):
    return MD_SPECIAL.sub(r'\\\1', s)


def inline(el):
    """Render the inline children of a paragraph."""
    out = []
    for t in el:
        if t.tag != 'text':
            continue
        out.append(run(t))
    return ''.join(out)


def run(t):
    if t.text and len(t) == 0:
        return esc(t.text)
    parts = [esc(t.text or '')]
    for c in t:
        parts.append(mark(c))
        parts.append(esc(c.tail or ''))
    return ''.join(parts)


def mark(c):
    if c.tag == 'code':
        txt = ''.join(c.itertext())
        tick = '``' if '`' in txt else '`'
        return f'{tick}{txt}{tick}'
    inner = run(c)
    if c.tag == 'bold':
        return f'**{inner}**'
    if c.tag == 'italic':
        return f'*{inner}*'
    if c.tag == 'link':
        return f'[{inner}]({c.get("href")})'
    return inner


def cell_text(cell):
    return ' '.join(inline(p) for p in cell if p.tag == 'paragraph').replace('|', '\\|')


def block(el, depth=0):
    tag = el.tag
    if tag == 'paragraph':
        txt = inline(el)
        h = el.get('heading')
        if h:
            return '#' * int(h) + ' ' + txt
        return txt
    if tag == 'list':
        lines = []
        ordered = el.get('kind') == 'ordered'
        for i, item in enumerate(el, 1):
            marker = f'{i}. ' if ordered else '- '
            pad = ' ' * len(marker)
            sub = [block(c, depth + 1) for c in item]
            first, rest = sub[0], sub[1:]
            lines.append(marker + first.replace('\n', '\n' + pad))
            for r in rest:
                lines.append(pad + r.replace('\n', '\n' + pad))
        return '\n'.join(lines)
    if tag == 'table':
        rows = [[cell_text(c) for c in r] for r in el]
        out = ['| ' + ' | '.join(rows[0]) + ' |', '| ' + ' | '.join(['---'] * len(rows[0])) + ' |']
        out += ['| ' + ' | '.join(r) + ' |' for r in rows[1:]]
        return '\n'.join(out)
    if tag == 'codeBlock':
        code = ''.join(el.itertext())
        lang = el.get('language') or ''
        if lang == 'latex':
            return '$$' + code + '$$'
        return f'```{lang}\n{code}\n```'
    if tag == 'embed':
        ref = el.get('ref', '').split('/')[-1]
        return f'<!--diagram:{ref}-->'
    return ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('out')
    ap.add_argument('--summary', required=True)
    ap.add_argument('--tags', required=True)
    a = ap.parse_args()

    raw = open(a.src, encoding='utf-8').read()
    if a.src.endswith('.json'):
        raw = json.loads(json.loads(raw)[0]['text'])['data']['xml']
    doc = ET.fromstring(raw)
    blocks = list(doc)

    title = inline(blocks[0]).replace('\\', '')
    date_el = blocks[1].find('date')
    date = date_el.get('value') if date_el is not None else ''
    body = '\n\n'.join(b for b in (block(e) for e in blocks[2:]) if b)

    q = '"' if ':' in title else ''
    fm = f'---\ntitle: {q}{title}{q}\ndate: {date}\nsummary: {a.summary}\ntags: {a.tags}\n---\n\n'
    open(a.out, 'w', encoding='utf-8').write(fm + body + '\n')


if __name__ == '__main__':
    main()
