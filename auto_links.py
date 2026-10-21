#!/usr/bin/env python3
"""Turn plain-text mentions of schools, seasons and conferences into links.

A text node whose whole text is exactly

    a school name      ("Boston University")      -> that school's page
    a season label     ("Fall 2026")              -> that season's page
    a conference code  ("NEISA")                  -> that conference's page

and that is not already inside a link (or a heading, button, menu, script, ...) becomes a link, relative to the page it
is on.  Only whole-cell matches are linked, so nothing inside a sentence changes, and a link is only made when the target
page exists.  The same page always produces the same output, so a re-run changes nothing.

Used two ways:
  * the site generators pass every page through link_html() as they write it (before the unchanged-file comparison, so
    unchanged pages keep their modification time);
  * `python3 auto_links.py website/icsa website/issa` sweeps pages written by anything else (the home-page podiums are
    injected after the build).
"""
from __future__ import annotations

import html as html_module
import os
import re
import sqlite3
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, 'website')
LEAGUES = ('icsa', 'issa')
SKIP_TAGS = {'a', 'script', 'style', 'title', 'option', 'button', 'select', 'textarea', 'svg', 'head', 'noscript', 'h1', 'label'}
VOID = {'img', 'br', 'hr', 'input', 'meta', 'link', 'source', 'area', 'col', 'embed', 'param', 'track', 'wbr'}
CONFERENCE_CODES = {'NEISA', 'MAISA', 'SAISA', 'SEISA', 'MCSA', 'PCCSC', 'NWICSA', 'NWISA', 'PCISA', 'NJISA', 'MASSA', 'NESSA'}

_TOKEN = re.compile(r'<!--.*?-->|<[^>]+>|[^<]+', re.S)
_TAGNAME = re.compile(r'^<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9:-]*)')
_SEASON = re.compile(r'^(Fall|Spring) (20\d\d)$')

_schools = None          # {league: {name: slug}}
_exists = {}             # path -> bool


def _school_maps() -> dict:
    global _schools
    if _schools is None:
        _schools = {}
        for lg in LEAGUES:
            m = {}
            try:
                con = sqlite3.connect(os.path.join(BASE, 'db', f'{lg}.db'))
                for school, slug in con.execute('SELECT school, slug FROM schools'):
                    if school and slug:
                        m[school.strip()] = slug
                con.close()
            except sqlite3.Error:
                pass
            _schools[lg] = m
    return _schools


def _has(path: str) -> bool:
    if path not in _exists:
        _exists[path] = os.path.exists(path)
    return _exists[path]


def _page_info(path: str):
    """(league, depth) for a page under website/<league>/..., else None."""
    rel = os.path.relpath(os.path.abspath(path), WEB).split(os.sep)
    if len(rel) < 2 or rel[0] not in LEAGUES:
        return None
    return rel[0], len(rel) - 2          # directories below website/<league>/ (index.html itself not counted)


def _target(lg: str, depth: int, tlg: str, sub: str) -> str:
    up = '../' * depth
    return f'{up}{sub}' if tlg == lg else f'{up}../{tlg}/{sub}'


# Plain HTML links only: the page scripts build links by string concatenation ('<a href="../schools/'+slug+'/">'), and those must be left alone.
_DEAD = re.compile(r'<a href="((?:\.\./)+(?:(?:icsa|issa)/)?schools/[^"/#?\'+]+/)">([^<>\']*)</a>')


def _drop_dead_school_links(html: str, path: str) -> str:
    """A link to a school page that does not exist (a school the site never built a page for) becomes plain text, not a 404."""
    base = os.path.dirname(os.path.abspath(path))

    def fix(m):
        if _has(os.path.normpath(os.path.join(base, m.group(1), 'index.html'))):
            return m.group(0)
        return m.group(2)
    return _DEAD.sub(fix, html)


def link_html(html: str, path: str) -> str:
    info = _page_info(path)
    if info is None:
        return html
    html = _drop_dead_school_links(html, path)
    lg, depth = info
    schools = _school_maps()
    here = os.path.normpath(os.path.dirname(os.path.abspath(path)))
    out = []
    stack = []                 # (tag name, skipping)
    skip = 0
    for tok in _TOKEN.findall(html):
        if tok[0] != '<' or tok.startswith('<!--'):
            if skip == 0 and not tok.startswith('<!--'):
                core = tok.strip()
                if core and len(core) <= 240:
                    linked = _link_text(core, lg, depth, schools, here)
                    if linked != core:
                        i = tok.index(core)
                        tok = f'{tok[:i]}{linked}{tok[i + len(core):]}'
            out.append(tok)
            continue
        m = _TAGNAME.match(tok)
        if m:
            closing, name = m.group(1) == '/', m.group(2).lower()
            if closing:
                for j in range(len(stack) - 1, -1, -1):
                    if stack[j][0] == name:
                        for _n, sk in stack[j:]:
                            if sk:
                                skip -= 1
                        del stack[j:]
                        break
            elif name not in VOID and not tok.endswith('/>'):
                sk = name in SKIP_TAGS or 'totalrow' in tok or 'tab-btn' in tok or 'onclick' in tok
                stack.append((name, sk))
                if sk:
                    skip += 1
        out.append(tok)
    return ''.join(out)


def _link_text(core: str, lg: str, depth: int, schools: dict, here: str) -> str:
    """core with every whole-name part linked: the text itself, or each part of an "A/B/C" list of schools."""
    href = _href_for(html_module.unescape(core), lg, depth, schools, here)
    if href:
        return f'<a href="{href}">{core}</a>'
    m = re.match(r'^(.+?)(\s*(?:&mdash;|\u2014)\s*\d+% win)$', core)           # "Boston College &mdash; 82% win" (the home-page podium cards)
    if m:
        h2 = _href_for(html_module.unescape(m.group(1)), lg, depth, schools, here)
        if h2:
            return f'<a href="{h2}">{m.group(1)}</a>{m.group(2)}'
    if '/' in core and len(core) <= 240:
        parts = re.split(r'(\s*/\s*)', core)
        out, hit = [], False
        for i, part in enumerate(parts):
            h = None if i % 2 else _href_for(html_module.unescape(part.strip()), lg, depth, schools, here) if part.strip() else None
            if h and _SEASON.match(html_module.unescape(part.strip())) is None:
                out.append(f'<a href="{h}">{part.strip()}</a>'); hit = True
            else:
                out.append(part)
        if hit:
            return ''.join(out)
    return core


def _href_for(text: str, lg: str, depth: int, schools: dict, here: str) -> str | None:
    s = _SEASON.match(text)
    if s:
        code = ('f' if s.group(1) == 'Fall' else 's') + s.group(2)[2:]
        sub = f'{code}/'
        if _has(os.path.join(WEB, lg, code, 'index.html')) and os.path.normpath(os.path.join(WEB, lg, code)) != here:
            return _target(lg, depth, lg, sub)
        return None
    if text in CONFERENCE_CODES:
        c = text.lower()
        if _has(os.path.join(WEB, lg, 'conferences', c, 'index.html')) and os.path.normpath(os.path.join(WEB, lg, 'conferences', c)) != here:
            return _target(lg, depth, lg, f'conferences/{c}/')
        return None
    for tlg in (lg, 'issa' if lg == 'icsa' else 'icsa'):
        slug = schools.get(tlg, {}).get(text)
        if slug and _has(os.path.join(WEB, tlg, 'schools', slug, 'index.html')):
            if os.path.normpath(os.path.join(WEB, tlg, 'schools', slug)) == here:
                return None
            return _target(lg, depth, tlg, f'schools/{slug}/')
    return None


def _sweep(path: str) -> int:
    with open(path, encoding='utf-8', errors='replace') as f:
        old = f.read()
    new = link_html(old, path)
    if new != old:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new)
        return 1
    return 0


def main() -> None:
    roots = sys.argv[1:] or [os.path.join(WEB, 'icsa'), os.path.join(WEB, 'issa')]
    changed = total = 0
    for root in roots:
        if os.path.isfile(root):
            total += 1
            changed += _sweep(root)
            continue
        for d, _dirs, files in os.walk(root):
            for fn in files:
                if fn.endswith('.html'):
                    total += 1
                    changed += _sweep(os.path.join(d, fn))
    print(f'auto_links: {changed:,} of {total:,} pages changed')


if __name__ == '__main__':
    main()
