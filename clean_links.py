#!/usr/bin/env python3
"""Links to a folder end in "/" rather than "/index.html".

    href="f26/danmark/index.html"   ->  href="f26/danmark/"
    href="../index.html"            ->  href="../"
    href="index.html"               ->  href="./"
    href="boat-speed/index.html?host=x" -> href="boat-speed/?host=x"

The web server serves index.html for a folder, so the page is the same and the address
bar stays clean.  Links to other sites are left alone.

Used two ways:
  * the site generators pass every page through clean() as they write it;
  * `python3 clean_links.py website/icsa website/issa ...` sweeps pages written by
    anything else.  It only rewrites files that change, so it is safe to run repeatedly.
"""
from __future__ import annotations

import os
import re
import sys
from multiprocessing import Pool

OWN_HOSTS = (b'https://datadrivensailing.com/', b'https://www.datadrivensailing.com/',
             b'http://datadrivensailing.com/', b'http://www.datadrivensailing.com/')

# The value may hold the other kind of quote (href="...?host=Queen's"), so each quote
# style is matched on its own.
_ATTR = re.compile(rb'''(\b(?:href|action)\s*=\s*)(?:"([^"<>]*?index\.html[^"<>]*)"|'([^'<>]*?index\.html[^'<>]*)')''')
_SCHEME = re.compile(rb'^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|//)')


def _clean_url(url: bytes) -> bytes:
    cut = len(url)
    for mark in (b'?', b'#'):
        i = url.find(mark)
        if i != -1:
            cut = min(cut, i)
    path, rest = url[:cut], url[cut:]
    if not path.endswith(b'index.html'):
        return url
    head = path[:-len(b'index.html')]
    if head and not head.endswith(b'/'):
        return url                      # e.g. "myindex.html"
    if _SCHEME.match(path) and not path.startswith(OWN_HOSTS):
        return url                      # someone else's site
    return (head or b'./') + rest


def clean_bytes(data: bytes) -> bytes:
    if b'index.html' not in data:
        return data
    def fix(m: re.Match) -> bytes:
        if m.group(2) is not None:
            return m.group(1) + b'"' + _clean_url(m.group(2)) + b'"'
        return m.group(1) + b"'" + _clean_url(m.group(3)) + b"'"
    return _ATTR.sub(fix, data)


def clean(html: str) -> str:
    return clean_bytes(html.encode('utf-8')).decode('utf-8')


def _sweep_file(path: str) -> int:
    try:
        with open(path, 'rb') as f:
            data = f.read()
        out = clean_bytes(data)
        if out == data:
            return 0
        tmp = path + '.tmp-clean'
        with open(tmp, 'wb') as f:
            f.write(out)
        os.replace(tmp, path)
        return 1
    except OSError as e:
        print(f'  skipped {path}: {e}', file=sys.stderr)
        return 0


def sweep(roots: list[str]) -> tuple[int, int]:
    files = []
    for root in roots:
        if os.path.isfile(root):
            files.append(root)
            continue
        for d, _, names in os.walk(root):
            files.extend(os.path.join(d, n) for n in names if n.endswith('.html'))
    with Pool() as pool:
        changed = sum(pool.imap_unordered(_sweep_file, files, chunksize=500))
    return len(files), changed


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('usage: clean_links.py <folder or file> [...]')
    total, changed = sweep(sys.argv[1:])
    print(f'clean_links: {total} pages checked, {changed} rewritten')
