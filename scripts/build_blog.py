#!/usr/bin/env python3
"""Build website/blog/: an index and one page per post.  Every number in the prose and the charts comes from analysis/blog/*.py, so a rebuild
after new data changes the text with it.   python3 scripts/build_blog.py     (not part of the nightly refresh: posts are dated observations)"""
import html
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'analysis' / 'blog'))
import json
import math
import shutil
from statistics import NormalDist
import elo_vs_pl, network_effects, skipper_ratings
import a_vs_b, calibration, division_effect, nationals_swap, rating_point, swap_decisions, swap_map, swap_threshold   # noqa: E402

OUT = ROOT / 'website' / 'blog'
ACCENT, INK, SUB, LINE = '#1a4f8a', '#20242b', '#6b7178', '#e6e2da'
C_A, C_B, C_BOTH, C_NEITHER = '#1a4f8a', '#7fa3cc', '#2f7d4f', '#b9bec4'
DATA_THROUGH = 'October 4, 2026'
esc = html.escape


def pct(x, d=0):
    return f'{x:.{d}f}%'


# ---- layout ---------------------------------------------------------------------------------------
EXTRA_CSS = '''
  .post h1.title{font-size:2rem;}
  .post .meta{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:0.8rem;color:var(--sub);margin-bottom:1.1rem;}
  .post h2{margin-top:2.2rem;}
  .fig{margin:1.4rem 0 1.8rem;}
  p.lede{font-size:1.35rem;line-height:1.45;margin:0 0 1rem;}
  p.eq{text-align:center;font-size:1.1rem;margin:1.2rem 0;padding:0.8rem;border-top:1px solid var(--line);border-bottom:1px solid var(--line);}
  .fig svg{width:100%;height:auto;display:block;}
  .fig .cap{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:0.78rem;color:var(--sub);margin-top:0.4rem;}
  table.t{border-collapse:collapse;width:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:0.85rem;margin:1rem 0 1.6rem;}
  table.t th{text-align:right;color:var(--sub);font-weight:600;font-size:0.72rem;text-transform:uppercase;letter-spacing:.05em;padding:0.4rem 0.5rem;border-bottom:2px solid var(--ink);}
  table.t td{text-align:right;padding:0.45rem 0.5rem;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums;}
  table.t th:first-child, table.t td:first-child{text-align:left;}
  table.t.cases th, table.t.cases td{text-align:left;}
  td.up{color:#b3412f;font-weight:700;} td.dn{color:#2f7d4f;font-weight:700;}
  table.t.cases th:last-child, table.t.cases td:last-child{text-align:right;}
  .note{font-size:0.92rem;color:var(--sub);border-left:3px solid var(--line);padding-left:1rem;margin:1.4rem 0;}
  .plist{list-style:none;padding:0;margin:1.5rem 0;}
  .plist li{padding:1.1rem 0;border-top:1px solid var(--line);}
  .plist li:last-child{border-bottom:1px solid var(--line);}
  .plist .d{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:0.76rem;color:var(--sub);text-transform:uppercase;letter-spacing:.05em;}
  .plist a.t1{font-size:1.25rem;font-weight:600;text-decoration:none;color:var(--ink);}
  .plist a.t1:hover{color:var(--accent);}
  .plist p{margin:0.35rem 0 0;color:var(--sub);}
'''


def write_css():
    css = re.search(r'<style>(.*?)</style>', (ROOT / 'website' / 'index.html').read_text(encoding='utf-8'), re.S).group(1)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'blog.css').write_text(css + EXTRA_CSS, encoding='utf-8')


def page(title, desc, body, slug=''):
    url = f'https://datadrivensailing.com/blog/{slug}' if slug else 'https://datadrivensailing.com/blog/'
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)} | Data-Driven Sailing</title>
<meta name="description" content="{esc(desc)}">
<meta property="og:type" content="article">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="https://datadrivensailing.com/og-preview.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/blog/blog.css">
</head>
<body>

<div class="topnav ui">
  <span class="who">Data-Driven Sailing</span>
  <span class="pages">
    <a href="/">Home</a>
    <a href="/engineering/">Under the hood</a>
    <a href="/icsa/">ICSA</a>
    <a href="/issa/">ISSA</a>
    <a href="/blog/" class="active">Notes</a>
  </span>
</div>

<div class="wrap">
{body}
</div>
</body>
</html>
'''


def _bust(body, assets):
    """append ?v=<content hash> to each copied image's src so browsers refetch after a rebuild (images are served with a 30-day cache)."""
    import hashlib
    for src in assets:
        name = Path(src).name; h = hashlib.md5(Path(src).read_bytes()).hexdigest()[:8]
        body = body.replace(f'src="{name}"', f'src="{name}?v={h}"')
    return body


def post_page(p):
    p = dict(p, body=_bust(p['body'], p.get('assets', [])))
    body = f'''<article class="post">
  <header class="hero" style="padding-bottom:1rem">
    <div class="meta">{esc(p['date_label'])} &middot; updated with data through {DATA_THROUGH} &middot; <a href="/blog/">All notes</a></div>
    <h1 class="title">{esc(p['title'])}</h1>
    <p class="tagline">{p['summary']}</p>
  </header>
{p['body']}
  <p class="note">Every number on this page is produced by <span class="mono">{esc(p['script'])}</span> in the project repository, from the same database that powers the rest of the site. Corrections welcome: <a href="mailto:quinnbrighton2005@gmail.com">quinnbrighton2005@gmail.com</a>.</p>
</article>'''
    return page(p['title'], re.sub(r'<[^>]+>', '', p['summary']), body, p['slug'] + '/')


# ---- charts (inline SVG) -------------------------------------------------------------------------
def stacked(items, width=640, bar_h=34, y=0, label=None, label_w=0):
    """One horizontal stacked bar. items: [(text, value, colour)] summing to ~100."""
    x0 = label_w; total = sum(v for _, v, _ in items); x = x0; out = []
    if label:
        out.append(f'<text x="0" y="{y + bar_h / 2 + 5}" font-size="13" fill="{INK}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">{esc(label)}</text>')
    for t, v, c in items:
        w = (width - x0) * v / total
        out.append(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="{bar_h}" fill="{c}"/>')
        if w > 34:
            out.append(f'<text x="{x + w / 2:.1f}" y="{y + bar_h / 2 + 5}" text-anchor="middle" font-size="13" font-weight="700" fill="#fff" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">{v:.0f}%</text>')
        x += w
    return ''.join(out)


def legend(items, y):
    out, x = [], 0
    for t, _, c in items:
        out.append(f'<rect x="{x}" y="{y}" width="12" height="12" rx="2" fill="{c}"/><text x="{x + 18}" y="{y + 11}" font-size="12" fill="{SUB}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">{esc(t)}</text>')
        x += 18 + 7.2 * len(t) + 22
    return ''.join(out)


def chart_ab(s):
    a = s['all']
    items = [('Won A only', a['a_only'], C_A), ('Won B only', a['b_only'], C_B), ('Won both', a['both'], C_BOTH), ('Won neither', a['neither'], C_NEITHER)]
    return f'<svg viewBox="0 0 640 96" role="img" aria-label="Share of regatta winners by the divisions they won">{stacked(items, 640, 38, 0)}{legend(items, 62)}</svg>'


def chart_size(s):
    rows = []; y = 0
    for lab, v in s['by_size'].items():
        items = [('A', v['a_only'], C_A), ('B', v['b_only'], C_B), ('both', v['both'], C_BOTH), ('neither', v['neither'], C_NEITHER)]
        rows.append(stacked(items, 640, 30, y, f"{lab} ({v['n']})", 140)); y += 42
    items = [('Won A only', 1, C_A), ('Won B only', 1, C_B), ('Won both', 1, C_BOTH), ('Won neither', 1, C_NEITHER)]
    rows.append(legend(items, y + 6))
    return f'<svg viewBox="0 0 640 {y + 30}" role="img" aria-label="Division wins by field size">{"".join(rows)}</svg>'


def chart_dumbbell(bins):
    """Predicted vs observed win rate per bin: a line between the two dots."""
    W_, rowh, left, right = 640, 44, 150, 30
    top = 28; H = top + rowh * len(bins) + 34
    xs = lambda v: left + (W_ - left - right) * v / 0.8
    out = [f'<text x="{left}" y="14" font-size="11" fill="{SUB}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">win rate</text>']
    for t in (0, .2, .4, .6, .8):
        out.append(f'<line x1="{xs(t):.1f}" y1="{top - 6}" x2="{xs(t):.1f}" y2="{top + rowh * len(bins) - 6}" stroke="{LINE}"/>'
                   f'<text x="{xs(t):.1f}" y="{top + rowh * len(bins) + 10}" text-anchor="middle" font-size="11" fill="{SUB}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">{int(t * 100)}%</text>')
    for i, b in enumerate(bins):
        cy = top + i * rowh + 14
        lab = f"{int(b['lo'] * 100)}–{int(b['hi'] * 100)}%  (n={b['n']})"
        out.append(f'<text x="0" y="{cy + 4}" font-size="12" fill="{INK}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">{lab}</text>')
        out.append(f'<line x1="{xs(b["pred"]):.1f}" y1="{cy}" x2="{xs(b["actual"]):.1f}" y2="{cy}" stroke="{SUB}" stroke-width="2"/>')
        out.append(f'<circle cx="{xs(b["pred"]):.1f}" cy="{cy}" r="6" fill="#fff" stroke="{ACCENT}" stroke-width="2.5"/>')
        out.append(f'<circle cx="{xs(b["actual"]):.1f}" cy="{cy}" r="6" fill="{ACCENT}"/>')
    out.append(f'<circle cx="{left}" cy="{H - 8}" r="5" fill="#fff" stroke="{ACCENT}" stroke-width="2.5"/><text x="{left + 12}" y="{H - 4}" font-size="12" fill="{SUB}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">predicted</text>'
               f'<circle cx="{left + 100}" cy="{H - 8}" r="5" fill="{ACCENT}"/><text x="{left + 112}" y="{H - 4}" font-size="12" fill="{SUB}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">actual</text>')
    return f'<svg viewBox="0 0 {W_} {H}" role="img" aria-label="Predicted against actual win rate by probability bin">{"".join(out)}</svg>'


def chart_curve():
    import math
    W_, H, l, b_, t_ = 640, 260, 46, 36, 14
    xs = lambda d: l + (W_ - l - 14) * d / 5; ys = lambda p: t_ + (H - t_ - b_) * (1 - (p - 0.5) / 0.5)
    pts = ' '.join(f'{xs(d / 20):.1f},{ys(1 / (1 + math.exp(-d / 20))):.1f}' for d in range(0, 101))
    out = []
    for p in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        out.append(f'<line x1="{l}" y1="{ys(p):.1f}" x2="{W_ - 14}" y2="{ys(p):.1f}" stroke="{LINE}"/><text x="{l - 8}" y="{ys(p) + 4:.1f}" text-anchor="end" font-size="11" fill="{SUB}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">{int(p * 100)}%</text>')
    for d in range(0, 6):
        out.append(f'<text x="{xs(d):.1f}" y="{H - 14}" text-anchor="middle" font-size="11" fill="{SUB}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">{d}</text>')
    out.append(f'<polyline points="{pts}" fill="none" stroke="{ACCENT}" stroke-width="3"/>')
    for d in (0.5, 1, 2, 3):
        p = 1 / (1 + math.exp(-d))
        out.append(f'<circle cx="{xs(d):.1f}" cy="{ys(p):.1f}" r="5" fill="{ACCENT}"/><text x="{xs(d) + 8:.1f}" y="{ys(p) + 16:.1f}" font-size="12" font-weight="700" fill="{INK}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">{p * 100:.0f}%</text>')
    out.append(f'<text x="{(l + W_) / 2:.0f}" y="{H}" text-anchor="middle" font-size="11" fill="{SUB}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">rating gap between two boats</text>')
    return f'<svg viewBox="0 0 {W_} {H + 4}" role="img" aria-label="Chance of beating a boat as a function of the rating gap">{"".join(out)}</svg>'



def _mix(c1, c2, t):
    return '#%02x%02x%02x' % tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def heat_svg(reg, xs, z, clip=6.0):
    """x: A-division skipper rating, y: B-division skipper rating, colour: change in predicted total if the two are swapped.
    Green = the swap lowers the predicted total (helps), red = raises it. The map is antisymmetric about the diagonal."""
    W_, l, r_, t_, b_ = 640, 62, 96, 14, 58
    pw = W_ - l - r_; ph = pw; n = len(xs); cw = pw / n
    f = 'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif"'
    white, green, red = (253, 251, 247), (47, 125, 79), (179, 65, 47)
    out = []
    for iy, y in enumerate(xs):
        for ix, x in enumerate(xs):
            v = z[iy][ix]; t = max(-1.0, min(1.0, v / clip))
            col = _mix(white, red, t) if t > 0 else _mix(white, green, -t)
            out.append(f'<rect x="{l + ix * cw:.1f}" y="{t_ + (n - 1 - iy) * cw:.1f}" width="{cw + 0.6:.1f}" height="{cw + 0.6:.1f}" fill="{col}"/>')
    X = lambda v: l + (v - xs[0] + 0.05) / (xs[-1] - xs[0] + 0.1) * pw
    Y = lambda v: t_ + ph - (v - xs[0] + 0.05) / (xs[-1] - xs[0] + 0.1) * ph
    out.append(f'<line x1="{X(xs[0]):.1f}" y1="{Y(xs[0]):.1f}" x2="{X(xs[-1]):.1f}" y2="{Y(xs[-1]):.1f}" stroke="{INK}" stroke-width="1.5" stroke-dasharray="5 4"/>')
    tick = 0.5; v = 2.0
    while v <= xs[-1] + 1e-9:
        out.append(f'<text x="{X(v):.1f}" y="{t_ + ph + 16}" text-anchor="middle" font-size="11" fill="{SUB}" {f}>{v:g}</text>'
                   f'<text x="{l - 8}" y="{Y(v) + 4:.1f}" text-anchor="end" font-size="11" fill="{SUB}" {f}>{v:g}</text>')
        v += tick
    for t_team in reg['teams']:
        if xs[0] <= t_team['a'] <= xs[-1] and xs[0] <= t_team['b'] <= xs[-1]:
            out.append(f'<circle cx="{X(t_team["a"]):.1f}" cy="{Y(t_team["b"]):.1f}" r="4.2" fill="#fff" stroke="{INK}" stroke-width="1.4"/>')
    out.append(f'<text x="{X(xs[-1]) - 6:.1f}" y="{Y(xs[0]) - 8:.1f}" text-anchor="end" font-size="12" font-weight="700" fill="{INK}" {f}>stronger skipper in A</text>')
    out.append(f'<text x="{X(xs[0]) + 6:.1f}" y="{Y(xs[-1]) + 16:.1f}" font-size="12" font-weight="700" fill="{INK}" {f}>stronger skipper in B</text>')
    out.append(f'<text x="{l + pw / 2:.0f}" y="{t_ + ph + 38}" text-anchor="middle" font-size="12" fill="{SUB}" {f}>A-division skipper rating</text>')
    out.append(f'<text x="14" y="{t_ + ph / 2:.0f}" text-anchor="middle" font-size="12" fill="{SUB}" transform="rotate(-90 14 {t_ + ph / 2:.0f})" {f}>B-division skipper rating</text>')
    bx = l + pw + 22; bh = ph - 40
    for i in range(60):
        v = clip - 2 * clip * i / 59
        tt = max(-1.0, min(1.0, v / clip)); col = _mix(white, red, tt) if tt > 0 else _mix(white, green, -tt)
        out.append(f'<rect x="{bx}" y="{t_ + 20 + bh * i / 60:.1f}" width="16" height="{bh / 60 + 0.6:.1f}" fill="{col}"/>')
    out.append(f'<text x="{bx + 22}" y="{t_ + 34}" font-size="11" fill="{SUB}" {f}>+{clip:g}</text><text x="{bx + 22}" y="{t_ + 24 + bh / 2:.0f}" font-size="11" fill="{SUB}" {f}>0</text>'
               f'<text x="{bx + 22}" y="{t_ + 20 + bh:.0f}" font-size="11" fill="{SUB}" {f}>&minus;{clip:g}</text>'
               f'<text x="{bx}" y="{t_ + 10}" font-size="11" font-weight="700" fill="{INK}" {f}>swap costs</text><text x="{bx}" y="{t_ + 32 + bh + 14:.0f}" font-size="11" font-weight="700" fill="{INK}" {f}>swap saves</text>')
    return f'<svg viewBox="0 0 {W_} {t_ + ph + b_}" role="img" aria-label="Change in predicted total points from swapping, for every pairing of A and B skipper ratings, {esc(reg["title"])}">{"".join(out)}</svg>'


def svg_lines(series, xlim, ylim, xlabel, ylabel, shades=(), marks=(), width=640, height=270, yticks=None, xticks=None, fmt='{:.1f}', texts=()):
    """series: [(xs, ys, colour, label)]; shades: [(xs, ys, colour)] filled between the curve and the axis; marks: [(x, y, colour, text, dx, dy)]."""
    l, r_, t_, b_ = 52, 16, 16, 44
    X = lambda v: l + (width - l - r_) * (v - xlim[0]) / (xlim[1] - xlim[0])
    Y = lambda v: t_ + (height - t_ - b_) * (1 - (v - ylim[0]) / (ylim[1] - ylim[0]))
    f = 'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif"'
    out = []
    for v in (yticks or []):
        out.append(f'<line x1="{l}" y1="{Y(v):.1f}" x2="{width - r_}" y2="{Y(v):.1f}" stroke="{LINE}"/><text x="{l - 8}" y="{Y(v) + 4:.1f}" text-anchor="end" font-size="11" fill="{SUB}" {f}>{fmt.format(v)}</text>')
    for v in (xticks or []):
        out.append(f'<text x="{X(v):.1f}" y="{height - b_ + 16}" text-anchor="middle" font-size="11" fill="{SUB}" {f}>{v:g}</text>')
    for xs, ys, col in shades:
        pts = ' '.join(f'{X(x):.1f},{Y(y):.1f}' for x, y in zip(xs, ys)) + f' {X(xs[-1]):.1f},{Y(ylim[0]):.1f} {X(xs[0]):.1f},{Y(ylim[0]):.1f}'
        out.append(f'<polygon points="{pts}" fill="{col}" fill-opacity="0.28"/>')
    for xs, ys, col, label in series:
        out.append(f'<polyline points="{" ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in zip(xs, ys))}" fill="none" stroke="{col}" stroke-width="3"/>')
    for x, y, col, text, dx, dy in marks:
        out.append(f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="5" fill="{col}"/>')
        if text:
            out.append(f'<text x="{X(x) + dx:.1f}" y="{Y(y) + dy:.1f}" font-size="12" font-weight="700" fill="{col}" {f}>{text}</text>')
    for x, y, col, text, anchor in texts:
        out.append(f'<text x="{X(x):.1f}" y="{Y(y):.1f}" text-anchor="{anchor}" font-size="13" font-weight="700" fill="{col}" {f}>{esc(text)}</text>')
    lx = l + 8
    for xs, ys, col, label in series:
        out.append(f'<rect x="{lx}" y="{t_ + 2}" width="14" height="4" fill="{col}"/><text x="{lx + 20}" y="{t_ + 8}" font-size="12" fill="{INK}" {f}>{esc(label)}</text>')
        lx += 20 + 7.0 * len(label) + 22
    out.append(f'<text x="{(l + width) / 2:.0f}" y="{height - 6}" text-anchor="middle" font-size="12" fill="{SUB}" {f}>{esc(xlabel)}</text>')
    out.append(f'<text x="14" y="{(t_ + height - b_) / 2:.0f}" text-anchor="middle" font-size="12" fill="{SUB}" transform="rotate(-90 14 {(t_ + height - b_) / 2:.0f})" {f}>{esc(ylabel)}</text>')
    return f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{esc(ylabel)} against {esc(xlabel)}">{"".join(out)}</svg>'


def fig_points_curve(ex):
    g = ex['grid']; lo, hi = ex['lo'], ex['hi']
    ia = lambda v: min(range(len(g)), key=lambda i: abs(g[i] - v))
    Ea, Eb = ex['EA'], ex['EB']
    marks = [(lo, Ea[ia(lo)], C_A, f'{Ea[ia(lo)]:.1f}', 8, -8), (hi, Ea[ia(hi)], C_A, f'{Ea[ia(hi)]:.1f}', 8, -8),
             (lo, Eb[ia(lo)], C_BOTH, f'{Eb[ia(lo)]:.1f}', 8, 16), (hi, Eb[ia(hi)], C_BOTH, f'{Eb[ia(hi)]:.1f}', 8, 16)]
    return svg_lines([(g, Ea, C_A, 'A-division field'), (g, Eb, C_BOTH, 'B-division field')], (g[0], g[-1]), (0, max(max(Ea), max(Eb)) + 1),
                     'skipper rating', 'expected points per race (lower is better)', marks=marks, yticks=[0, 4, 8, 12, 16], xticks=[3, 3.5, 4, 4.5, 5, 5.5])


def fig_density(ex):
    g = ex['grid']; lo, hi = ex['lo'], ex['hi']
    sel = [i for i, x in enumerate(g) if lo - 1e-9 <= x <= hi + 1e-9]
    xs = [g[i] for i in sel]
    shades = [(xs, [ex['gA'][i] for i in sel], C_A), (xs, [ex['gB'][i] for i in sel], C_BOTH)]
    top = max(max(ex['gA']), max(ex['gB'])) * 1.15
    return svg_lines([(g, ex['gA'], C_A, 'A-division field'), (g, ex['gB'], C_BOTH, 'B-division field')], (g[0], g[-1]), (0, top),
                     'skipper rating', 'boats passed per rating point', shades=shades, texts=[((lo + hi) / 2, top * 0.30, '#12415f', f"A field: {ex['SA']:.2f} boats passed per race", 'middle'), ((lo + hi) / 2, top * 0.18, '#1f5c3b', f"B field: {ex['SB']:.2f} boats passed per race", 'middle')], yticks=[0, 0.5, 1.0, 1.5, 2.0, 2.5][: int(top / 0.5) + 1], xticks=[3, 3.5, 4, 4.5, 5, 5.5], fmt='{:.1f}')


def fig_hump(reg):
    """H(r) = E_A(r) - E_B(r): the places a skipper gives up per race by sailing A instead of B, with the hump's centre marked."""
    xs, h = swap_threshold.hump(reg['rivA'], reg['rivB'], lo=0.0, hi=8.0)
    k = max(range(len(h)), key=h.__getitem__)
    return svg_lines([(xs, h, C_A, 'places lost by sailing A instead of B')], (xs[0], xs[-1]), (0, max(h) * 1.2), 'skipper rating', 'places per race', shades=[(xs, h, C_A)],
                     marks=[(xs[k], h[k], C_BOTH, 'centre m', 8, -8)], yticks=[0, 0.5, 1.0, 1.5, 2.0], xticks=[0, 2, 4, 6, 8], fmt='{:.1f}')


def fig_forest(rows):
    """Crew rating relative to the roster average with a 90% range, best first; the dot is the posterior mean."""
    W_, l, r_, t_, rowh = 640, 150, 16, 28, 22
    H = t_ + rowh * len(rows) + 40
    lo_, hi_ = -0.40, 0.45
    X = lambda v: l + (W_ - l - r_) * (v - lo_) / (hi_ - lo_)
    f = 'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif"'
    out = [f'<line x1="{X(0):.1f}" y1="{t_ - 8}" x2="{X(0):.1f}" y2="{H - 34}" stroke="{INK}" stroke-dasharray="3 3"/>']
    for v in (-0.4, -0.2, 0.0, 0.2, 0.4):
        out.append(f'<text x="{X(v):.1f}" y="{H - 18}" text-anchor="middle" font-size="11" fill="{SUB}" {f}>{v:+.1f}</text>' if v else f'<text x="{X(v):.1f}" y="{H - 18}" text-anchor="middle" font-size="11" fill="{SUB}" {f}>0</text>')
    for i, r in enumerate(rows):
        y = t_ + rowh * i + rowh / 2
        a, b = r['rel'] - 1.645 * r['sd'], r['rel'] + 1.645 * r['sd']
        col = C_BOTH if r['rel'] > 0 else '#b5473a'
        out.append(f'<text x="{l - 10}" y="{y + 4:.1f}" text-anchor="end" font-size="12" fill="{INK}" {f}>{esc(r["name"])}</text>')
        out.append(f'<line x1="{X(a):.1f}" y1="{y:.1f}" x2="{X(b):.1f}" y2="{y:.1f}" stroke="{col}" stroke-opacity="0.45" stroke-width="3"/>')
        out.append(f'<circle cx="{X(r["rel"]):.1f}" cy="{y:.1f}" r="4.5" fill="{col}"/>')
    out.append(f'<text x="{(l + W_ - r_) / 2:.1f}" y="{H - 2}" text-anchor="middle" font-size="12" fill="{SUB}" {f}>crew rating relative to the roster average, rating points (dot = estimate, bar = 90% range)</text>')
    return f'<svg viewBox="0 0 {W_} {H}" width="100%" role="img" aria-label="Brown fall 2025 crew ratings with 90% ranges">{"".join(out)}</svg>'


def fig_shrink(rows, label):
    """Raw lift (with the crew minus the same skippers without) against the final rating: the cloud is flatter than the diagonal."""
    W_, H, l, b_, t_, r_ = 640, 330, 52, 44, 16, 16
    pts = [r for r in rows if r['lift'] is not None]
    lo_, hi_, ylo, yhi = -0.6, 0.6, -0.25, 0.30
    X = lambda v: l + (W_ - l - r_) * (v - lo_) / (hi_ - lo_)
    Y = lambda v: t_ + (H - t_ - b_) * (1 - (v - ylo) / (yhi - ylo))
    f = 'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif"'
    out = []
    for v in (-0.2, -0.1, 0, 0.1, 0.2, 0.3):
        out.append(f'<line x1="{l}" y1="{Y(v):.1f}" x2="{W_ - r_}" y2="{Y(v):.1f}" stroke="{LINE}"/><text x="{l - 8}" y="{Y(v) + 4:.1f}" text-anchor="end" font-size="11" fill="{SUB}" {f}>{v:+.1f}</text>')
    for v in (-0.6, -0.4, -0.2, 0, 0.2, 0.4, 0.6):
        out.append(f'<text x="{X(v):.1f}" y="{H - b_ + 16}" text-anchor="middle" font-size="11" fill="{SUB}" {f}>{v:+.1f}</text>')
    out.append(f'<line x1="{X(lo_):.1f}" y1="{Y(lo_):.1f}" x2="{X(0.3):.1f}" y2="{Y(0.3):.1f}" stroke="{SUB}" stroke-dasharray="4 4"/>')
    out.append(f'<text x="{X(0.31):.1f}" y="{Y(0.3) + 14:.1f}" font-size="11" fill="{SUB}" {f}>rating = raw lift</text>')
    for r in pts:
        y = Y(r['rel'])
        out.append(f'<line x1="{X(max(lo_, r["lift"] - r["lift_se"])):.1f}" y1="{y:.1f}" x2="{X(min(hi_, r["lift"] + r["lift_se"])):.1f}" y2="{y:.1f}" stroke="{C_A}" stroke-opacity="0.25" stroke-width="2"/>')
    for r in pts:
        out.append(f'<circle cx="{X(r["lift"]):.1f}" cy="{Y(r["rel"]):.1f}" r="4.5" fill="{C_A}"/>')
        if r['name'] in label:
            dx, dy, anc = label[r['name']]
            out.append(f'<text x="{X(r["lift"]) + dx:.1f}" y="{Y(r["rel"]) + dy:.1f}" text-anchor="{anc}" font-size="12" font-weight="700" fill="{INK}" {f}>{esc(r["name"])}</text>')
    out.append(f'<text x="{(l + W_ - r_) / 2:.1f}" y="{H - 4}" text-anchor="middle" font-size="12" fill="{SUB}" {f}>raw lift: boats with the crew minus the same skippers without (rating points; bar = one standard error)</text>')
    out.append(f'<text transform="translate(13 {(t_ + H - b_) / 2:.1f}) rotate(-90)" text-anchor="middle" font-size="12" fill="{SUB}" {f}>crew rating, relative</text>')
    return f'<svg viewBox="0 0 {W_} {H}" width="100%" role="img" aria-label="Raw lift against shrunken crew rating, Brown fall 2025">{"".join(out)}</svg>'


_SANS = 'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif"'


def _last(name):
    return name.replace('Crew ', '')


def fig_withwithout(rows):
    """Per crew: the same skippers without them (grey) and with them (colour), joined; the gap is the raw lift."""
    rs = sorted([r for r in rows if r['lift'] is not None], key=lambda r: -r['lift'])
    W_, l, r_, t_, rowh = 640, 150, 16, 26, 22
    H = t_ + rowh * len(rs) + 42
    lo_, hi_ = -0.5, 0.55
    X = lambda v: l + (W_ - l - r_) * (v - lo_) / (hi_ - lo_)
    f = _SANS
    out = [f'<line x1="{X(0):.1f}" y1="{t_ - 8}" x2="{X(0):.1f}" y2="{H - 36}" stroke="{INK}" stroke-dasharray="3 3"/>']
    for v in (-0.4, -0.2, 0, 0.2, 0.4):
        out.append(f'<text x="{X(v):.1f}" y="{H - 20}" text-anchor="middle" font-size="11" fill="{SUB}" {f}>{v:+.1f}</text>' if v else f'<text x="{X(v):.1f}" y="{H - 20}" text-anchor="middle" font-size="11" fill="{SUB}" {f}>0</text>')
    out.append(f'<circle cx="{l + 6}" cy="10" r="4.5" fill="{C_NEITHER}"/><text x="{l + 16}" y="14" font-size="12" fill="{INK}" {f}>same skippers, other races</text><circle cx="{l + 190}" cy="10" r="4.5" fill="{C_A}"/><text x="{l + 200}" y="14" font-size="12" fill="{INK}" {f}>boats with this crew</text>')
    for i, r in enumerate(rs):
        y = t_ + rowh * i + rowh / 2
        col = C_A if r['lift'] > 0 else '#b5473a'
        out.append(f'<text x="{l - 10}" y="{y + 4:.1f}" text-anchor="end" font-size="12" fill="{INK}" {f}>{esc(r["name"])}</text>')
        out.append(f'<line x1="{X(r["without_crew"]):.1f}" y1="{y:.1f}" x2="{X(r["with_crew"]):.1f}" y2="{y:.1f}" stroke="{col}" stroke-opacity="0.45" stroke-width="3"/>')
        out.append(f'<circle cx="{X(r["without_crew"]):.1f}" cy="{y:.1f}" r="4.5" fill="{C_NEITHER}"/><circle cx="{X(r["with_crew"]):.1f}" cy="{y:.1f}" r="4.5" fill="{col}"/>')
    out.append(f'<text x="{(l + W_ - r_) / 2:.1f}" y="{H - 3}" text-anchor="middle" font-size="12" fill="{SUB}" {f}>surprisal in rating points: how much better the boats went than their skippers\' ratings predicted</text>')
    return f'<svg viewBox="0 0 {W_} {H}" width="100%" role="img" aria-label="Boats with each Brown crew against the same skippers without them">{"".join(out)}</svg>'


def fig_network(g, rows):
    """The comparison network behind the roster: crews (large), the skippers they sailed with, and other crews of those skippers."""
    W_, H = 900.0, 620.0
    rate = {n['id']: n for n in g['nodes']}
    f = _SANS
    out = []
    for e in g['edges']:
        a, b = rate[e['a']], rate[e['b']]
        if e['push'] is None:
            out.append(f'<line x1="{a["x"]}" y1="{a["y"]}" x2="{b["x"]}" y2="{b["y"]}" stroke="{C_NEITHER}" stroke-opacity="0.5" stroke-width="0.8"/>')
    for e in g['edges']:
        if e['push'] is not None:
            a, b = rate[e['a']], rate[e['b']]
            out.append(f'<line x1="{a["x"]}" y1="{a["y"]}" x2="{b["x"]}" y2="{b["y"]}" stroke="{C_A}" stroke-opacity="0.45" stroke-width="{0.6 + 0.22 * min(e["w"], 30) ** 0.5 * 2:.1f}"/>')
    for n in g['nodes']:
        if n['type'] == 'other':
            out.append(f'<circle cx="{n["x"]}" cy="{n["y"]}" r="2.6" fill="{C_NEITHER}"/>')
    for n in g['nodes']:
        if n['type'] == 'skipper':
            if n.get('hub'):
                out.append(f'<rect x="{n["x"] - 6}" y="{n["y"] - 6}" width="12" height="12" fill="{INK}" transform="rotate(45 {n["x"]} {n["y"]})"/>')
            else:
                out.append(f'<rect x="{n["x"] - 3.5}" y="{n["y"] - 3.5}" width="7" height="7" fill="{SUB}" transform="rotate(45 {n["x"]} {n["y"]})"/>')
    for n in g['nodes']:
        if n['type'] == 'crew':
            col = C_BOTH if n['e'] > 0.02 else ('#b5473a' if n['e'] < 0.0 else C_B)
            out.append(f'<circle cx="{n["x"]}" cy="{n["y"]}" r="10" fill="{col}" stroke="#fff" stroke-width="2"/>')
            out.append(f'<text x="{n["x"]}" y="{n["y"] - 15}" text-anchor="middle" font-size="12" font-weight="700" fill="{INK}" stroke="#fbf9f5" stroke-width="3" paint-order="stroke" {f}>{esc(_last(n["name"]))}</text>')
    return f'<svg viewBox="0 0 {W_:.0f} {H:.0f}" width="100%" role="img" aria-label="Comparison network of the Brown fall 2025 roster">{"".join(out)}</svg>'


def fig_push(det, factor):
    """One crew's rating, skipper by skipper: each skipper's push on the crew's effect; the pushes add up to the effect."""
    rows = det['rows'][:9]
    W_, l, r_, t_, rowh = 640, 200, 70, 18, 24
    tot = det['total'] / factor
    H = t_ + rowh * (len(rows) + 1) + 46
    lo_, hi_ = -0.02, 0.26
    X = lambda v: l + (W_ - l - r_) * (v - lo_) / (hi_ - lo_)
    f = _SANS
    out = [f'<line x1="{X(0):.1f}" y1="{t_ - 6}" x2="{X(0):.1f}" y2="{H - 40}" stroke="{INK}"/>']
    for v in (0, 0.1, 0.2):
        out.append(f'<text x="{X(v):.1f}" y="{H - 24}" text-anchor="middle" font-size="11" fill="{SUB}" {f}>{v:+.2f}</text>' if v else f'<text x="{X(v):.1f}" y="{H - 24}" text-anchor="middle" font-size="11" fill="{SUB}" {f}>0</text>')
    for i, r in enumerate(rows):
        y = t_ + rowh * i + rowh / 2; v = r['push'] / factor
        col = C_A if v >= 0 else '#b5473a'
        out.append(f'<text x="{l - 10}" y="{y + 4:.1f}" text-anchor="end" font-size="12" fill="{INK}" {f}>{esc(r["skipper"])} <tspan fill="{SUB}">({r["races"]} races)</tspan></text>')
        out.append(f'<rect x="{min(X(0), X(v)):.1f}" y="{y - 7:.1f}" width="{abs(X(v) - X(0)):.1f}" height="14" fill="{col}" fill-opacity="0.8"/>')
        out.append(f'<text x="{(X(v) + 6) if v >= 0 else (X(0) + 6):.1f}" y="{y + 4:.1f}" text-anchor="start" font-size="11" fill="{SUB}" {f}>{v:+.3f}</text>')
    y = t_ + rowh * len(rows) + rowh / 2 + 4
    out.append(f'<line x1="{l - 190}" y1="{y - 14:.1f}" x2="{W_ - r_ + 40}" y2="{y - 14:.1f}" stroke="{LINE}"/>')
    out.append(f'<text x="{l - 10}" y="{y + 4:.1f}" text-anchor="end" font-size="12" font-weight="700" fill="{INK}" {f}>crew effect (sum)</text><rect x="{X(0):.1f}" y="{y - 7:.1f}" width="{abs(X(tot) - X(0)):.1f}" height="14" fill="{C_BOTH}"/><text x="{X(tot) + 6:.1f}" y="{y + 4:.1f}" font-size="12" font-weight="700" fill="{INK}" {f}>{tot:+.3f}</text>')
    out.append(f'<text x="{(l + W_ - r_) / 2:.1f}" y="{H - 4}" text-anchor="middle" font-size="12" fill="{SUB}" {f}>push on the crew effect, rating points</text>')
    return f'<svg viewBox="0 0 {W_} {H}" width="100%" role="img" aria-label="Skipper-by-skipper pushes on one crew rating">{"".join(out)}</svg>'


def fig_h2h(d):
    """Head-to-head grid: the chance the row crew is stronger than the column crew, rows and columns best first."""
    n = len(d['order_names']); P = d['p_matrix']
    cell, l, t_ = 30, 100, 92
    W_, H = l + cell * n + 10, t_ + cell * n + 10
    f = _SANS
    out = []
    for i in range(n):
        out.append(f'<text x="{l - 6}" y="{t_ + cell * i + cell / 2 + 4}" text-anchor="end" font-size="11" fill="{INK}" {f}>{esc(_last(d["order_names"][i]))}</text>')
        out.append(f'<text transform="translate({l + cell * i + cell / 2 + 4} {t_ - 6}) rotate(-60)" font-size="11" fill="{INK}" {f}>{esc(_last(d["order_names"][i]))}</text>')
        for j in range(n):
            v = P[i * n + j]
            if v is None:
                out.append(f'<rect x="{l + cell * j}" y="{t_ + cell * i}" width="{cell - 1}" height="{cell - 1}" fill="#eee9df"/>')
                continue
            a = abs(v - 0.5) * 2
            col = f'rgba(47,125,79,{0.12 + 0.88 * a:.2f})' if v >= 0.5 else f'rgba(181,71,58,{0.12 + 0.88 * a:.2f})'
            out.append(f'<rect x="{l + cell * j}" y="{t_ + cell * i}" width="{cell - 1}" height="{cell - 1}" fill="{col}"/>')
            if i < j and max(v, 1 - v) >= 0.8:
                out.append(f'<text x="{l + cell * j + cell / 2 - 0.5}" y="{t_ + cell * i + cell / 2 + 4}" text-anchor="middle" font-size="10" font-weight="700" fill="#fff" {f}>{v * 100:.0f}</text>')
    return f'<svg viewBox="0 0 {W_} {H}" width="100%" role="img" aria-label="Head-to-head probabilities between Brown crews">{"".join(out)}</svg>'


_PALETTE = ['#1a4f8a', '#2f7d4f', '#c58a1c', '#8a4f8a', '#b5473a']


def fig_peel(toy):
    """A race as a sequence of choices: at each pick, the remaining boats share the probability in proportion to e^rating."""
    W_, l, r_, t_, rowh = 640, 78, 90, 24, 40
    stages = toy['stages']; H = t_ + rowh * len(stages) + 56
    f = _SANS
    out = []
    X = lambda p: l + (W_ - l - r_) * p
    for k, st_ in enumerate(stages):
        y = t_ + rowh * k
        out.append(f'<text x="{l - 10}" y="{y + 22}" text-anchor="end" font-size="12" fill="{INK}" {f}>pick {k + 1}</text>')
        x = 0.0
        for b in sorted(st_['probs'], key=lambda i: toy['order'].index(i)):
            w = st_['probs'][b]
            chosen = b == st_['chosen']
            out.append(f'<rect x="{X(x):.1f}" y="{y + 6}" width="{(X(x + w) - X(x)):.1f}" height="26" fill="{_PALETTE[b]}" fill-opacity="{0.95 if chosen else 0.3}" stroke="#fff" stroke-width="1.5"/>')
            if w > 0.07:
                out.append(f'<text x="{(X(x) + X(x + w)) / 2:.1f}" y="{y + 24}" text-anchor="middle" font-size="11" font-weight="700" fill="{"#fff" if chosen else INK}" {f}>{chr(65 + b)}</text>')
            x += w
        out.append(f'<text x="{W_ - r_ + 8}" y="{y + 24}" font-size="12" fill="{INK}" {f}>{st_["probs"][st_["chosen"]] * 100:.0f}% for {chr(65 + st_["chosen"])}</text>')
    y = t_ + rowh * len(stages) + 14
    out.append(f'<text x="{l}" y="{y + 12}" font-size="12" fill="{SUB}" {f}>boats (rating): ' + ', '.join(f'{chr(65 + i)} ({r:+.1f})' for i, r in enumerate(toy['ratings'])) + '</text>')
    out.append(f'<text x="{l}" y="{y + 34}" font-size="13" font-weight="700" fill="{INK}" {f}>chance of this exact finishing order: ' + ' &#215; '.join(f'{st_["probs"][st_["chosen"]]:.2f}' for st_ in stages) + f' = {toy["p_order"] * 100:.1f}%</text>')
    return f'<svg viewBox="0 0 {W_} {H}" width="100%" role="img" aria-label="A race as a sequence of choices">{"".join(out)}</svg>'


def fig_penalty():
    xs = [-0.6 + 0.01 * i for i in range(121)]
    ys = [skipper_ratings.move_penalty(x) for x in xs]
    return svg_lines([(xs, ys, C_A, 'penalty on a one-week change in rating')], (-0.6, 0.6), (0, max(ys) * 1.05), 'change in rating from one regatta week to the next', 'penalty (log-likelihood units)',
                     marks=[(-0.3, skipper_ratings.move_penalty(-0.3), C_BOTH, 'a 0.3 drop', -10, -10), (0.3, skipper_ratings.move_penalty(0.3), C_BOTH, 'a 0.3 gain', -50, -10)],
                     yticks=[0, 40, 80, 120], xticks=[-0.6, -0.3, 0, 0.3, 0.6], fmt='{:.0f}')


def fig_path(path):
    ys = path['y']; lo_, hi_ = min(ys) - 0.3, max(ys) + 0.3
    return svg_lines([(path['x'], ys, C_A, 'one skipper\'s weekly rating')], (min(path['x']) - 0.1, max(path['x']) + 0.1), (lo_, hi_), 'date', 'rating',
                     marks=[(x, y, C_A, '', 0, 0) for x, y in zip(path['x'], ys)], yticks=[v / 2 for v in range(math.ceil(lo_ * 2), math.floor(hi_ * 2) + 1)],
                     xticks=list(range(int(min(path['x'])) + 1, int(max(path['x'])) + 1)), fmt='{:.1f}')


def fig_ladder(rows):
    """Out-of-sample log-loss by model (lower is better): season ahead (grey) and championships with a 95% interval (blue), numbers in a column on the right."""
    W_, l, pr, t_, rowh = 640, 236, 118, 34, 34
    H = t_ + rowh * len(rows) + 50
    lo_, hi_ = 0.52, 0.63; R = W_ - pr
    X = lambda v: l + (R - l) * (v - lo_) / (hi_ - lo_)
    f = _SANS
    out = []
    for k in range(6):
        v = lo_ + 0.02 * k
        out.append(f'<line x1="{X(v):.1f}" y1="{t_ - 8}" x2="{X(v):.1f}" y2="{H - 42}" stroke="{LINE}"/><text x="{X(v):.1f}" y="{H - 26}" text-anchor="middle" font-size="11" fill="{SUB}" {f}>{v:.2f}</text>')
    out.append(f'<circle cx="{l + 4}" cy="11" r="5" fill="{C_NEITHER}"/><text x="{l + 14}" y="15" font-size="12" fill="{INK}" {f}>season ahead</text><circle cx="{l + 118}" cy="11" r="5" fill="{C_A}"/><text x="{l + 128}" y="15" font-size="12" fill="{INK}" {f}>championships, 95% interval</text>')
    out.append(f'<text x="{R + 12}" y="15" font-size="11" fill="{SUB}" {f}>season / champs</text>')
    for i, r in enumerate(rows):
        y = t_ + rowh * i + rowh / 2
        site = r['model'] == 'weekly_pl'
        out.append(f'<text x="{l - 12}" y="{y + 4:.1f}" text-anchor="end" font-size="12" font-weight="{700 if site else 400}" fill="{INK}" {f}>{esc(r["label"])}</text>')
        a_, b_ = r['champ_ll_ci']
        out.append(f'<line x1="{X(a_):.1f}" y1="{y:.1f}" x2="{X(b_):.1f}" y2="{y:.1f}" stroke="{C_A}" stroke-opacity="0.4" stroke-width="3"/>')
        out.append(f'<circle cx="{X(r["season_ll"]):.1f}" cy="{y:.1f}" r="5" fill="{C_NEITHER}"/><circle cx="{X(r["champ_ll"]):.1f}" cy="{y:.1f}" r="5" fill="{C_A}"/>')
        out.append(f'<text x="{R + 12}" y="{y + 4:.1f}" font-size="11" fill="{SUB}" {f}>{r["season_ll"]:.4f} / {r["champ_ll"]:.4f}</text>')
    out.append(f'<text x="{(l + R) / 2:.1f}" y="{H - 6}" text-anchor="middle" font-size="12" fill="{SUB}" {f}>pairwise log-loss on races never seen in fitting (lower is better)</text>')
    return f'<svg viewBox="0 0 {W_} {H}" width="100%" role="img" aria-label="Out-of-sample log-loss of Elo and Plackett-Luce models">{"".join(out)}</svg>'


def fig_kcurve(curves):
    series = []; cols = [C_A, C_BOTH, '#c58a1c', '#8a4f8a']
    for c, col in zip(curves, cols):
        ks = sorted(c['curve']); series.append(([math.log2(k) for k in ks], [c['curve'][k] for k in ks], col, f"scored on {c['season']}"))
    ys = [v for s_ in series for v in s_[1]]
    marks = [(math.log2(c['k']), c['curve'][c['k']], col, '', 0, 0) for c, col in zip(curves, cols)]
    return svg_lines(series, (3, 8), (min(ys) - 0.01, max(ys) + 0.01), 'K (log scale)', 'Elo log-loss on the tuning season', marks=marks, yticks=[0.58, 0.60, 0.62], xticks=[3, 4, 5, 6, 7, 8], fmt='{:.2f}', width=640, height=290)


def fig_reliability(cal):
    ser = [([0.5, 1.0], [0.5, 1.0], C_NEITHER, 'perfect'), ([p for p, o, n in cal['elo']], [o for p, o, n in cal['elo']], C_BOTH, 'Elo'), ([p for p, o, n in cal['weekly_pl']], [o for p, o, n in cal['weekly_pl']], C_A, 'Plackett–Luce, weekly')]
    return svg_lines(ser, (0.5, 1.0), (0.5, 1.0), 'predicted chance the favourite finishes ahead', 'how often it did', marks=[(p, o, C_BOTH, '', 0, 0) for p, o, n in cal['elo']] + [(p, o, C_A, '', 0, 0) for p, o, n in cal['weekly_pl']],
                     yticks=[0.5, 0.6, 0.7, 0.8, 0.9, 1.0], xticks=[0.5, 0.6, 0.7, 0.8, 0.9, 1.0], fmt='{:.1f}', width=640, height=320)


def fig_one_race(rows):
    """The same finishing order seen by Elo (boats beaten minus expected, rating points moved) and by Plackett-Luce (the score of the order)."""
    rows = sorted(rows, key=lambda r: r['place'])
    W_, t_, rowh = 640, 30, 28
    H = t_ + rowh * len(rows) + 44
    f = _SANS
    def panel(x0, wid, key, scale, title, fmt):
        out = [f'<text x="{x0 + wid / 2}" y="16" text-anchor="middle" font-size="12" font-weight="700" fill="{INK}" {f}>{title}</text>', f'<line x1="{x0 + wid / 2}" y1="{t_ - 6}" x2="{x0 + wid / 2}" y2="{H - 36}" stroke="{INK}"/>']
        for i, r in enumerate(rows):
            y = t_ + rowh * i + rowh / 2; v = r[key]; w = v / scale * wid / 2
            col = C_BOTH if v >= 0 else '#b5473a'
            out.append(f'<rect x="{min(x0 + wid / 2, x0 + wid / 2 + w):.1f}" y="{y - 8:.1f}" width="{abs(w):.1f}" height="16" fill="{col}" fill-opacity="0.85"/>')
            out.append(f'<text x="{(x0 + wid / 2 + w + 5) if v >= 0 else (x0 + wid / 2 + w - 5):.1f}" y="{y + 4:.1f}" text-anchor="{"start" if v >= 0 else "end"}" font-size="11" fill="{SUB}" {f}>{fmt.format(v)}</text>')
        return out
    out = []
    for i, r in enumerate(rows):
        y = t_ + rowh * i + rowh / 2
        out.append(f'<text x="142" y="{y + 4:.1f}" text-anchor="end" font-size="12" fill="{INK}" {f}>{r["place"]}{"st" if r["place"] == 1 else ("nd" if r["place"] == 2 else ("rd" if r["place"] == 3 else "th"))}: boat {chr(65 + r["boat"])} ({r["theta"]:+.1f})</text>')
    out += panel(152, 214, 'elo_driver', 3.2, 'Elo: boats beaten minus expected', '{:+.2f}')
    out += panel(404, 214, 'pl_score', 1.4, 'Plackett–Luce: score of the order', '{:+.2f}')
    return f'<svg viewBox="0 0 {W_} {H}" width="100%" role="img" aria-label="One race through Elo and Plackett-Luce">{"".join(out)}</svg>'


def fig_ripple(rp):
    """Two regions of sailors who race among themselves, linked by a few travellers.  One new race among four travellers is added; dots grow by how much each rating changed."""
    W_, H = 640, 250; f = _SANS
    n_per = rp['n_per']; trav = set(rp['trav']); new = set(rp['new'])
    def positions(x0):
        pos = {}
        for reg, cx in ((0, x0 + 78), (1, x0 + 232)):
            ids = [i for i in range(reg * n_per, (reg + 1) * n_per)]
            tv = [i for i in ids if i in trav]; nt = [i for i in ids if i not in trav]
            face = 0.0 if reg == 0 else math.pi
            for k, i in enumerate(tv):
                a = face + (k - (len(tv) - 1) / 2) * 0.28; pos[i] = (cx + 62 * math.cos(a), 125 + 62 * math.sin(a))
            for k, i in enumerate(nt):
                a = face + math.pi + (k - (len(nt) - 1) / 2) * (0.95 * math.pi / len(nt)); pos[i] = (cx + 62 * math.cos(a), 125 + 62 * math.sin(a))
        return pos
    out = []
    for x0, key, title in ((0, 'd_elo', 'Elo'), (320, 'd_pl', 'joint Plackett–Luce fit')):
        d = rp[key]; pos = positions(x0)
        moved = sum(1 for v in d if abs(v) > 0.01)
        out.append(f'<text x="{x0 + 160}" y="18" text-anchor="middle" font-size="13" font-weight="700" fill="{INK}" {f}>{title}: {moved} of {rp["n"]} ratings move</text>')
        for cx in (x0 + 78, x0 + 232):
            out.append(f'<circle cx="{cx}" cy="125" r="62" fill="{C_NEITHER}" fill-opacity="0.18"/>')
        out.append(f'<text x="{x0 + 78}" y="215" text-anchor="middle" font-size="11" fill="{SUB}" {f}>region A</text><text x="{x0 + 232}" y="215" text-anchor="middle" font-size="11" fill="{SUB}" {f}>region B</text>')
        for i, j, c in rp['edges']:
            if (i < n_per) != (j < n_per):
                out.append(f'<line x1="{pos[i][0]:.1f}" y1="{pos[i][1]:.1f}" x2="{pos[j][0]:.1f}" y2="{pos[j][1]:.1f}" stroke="{C_NEITHER}" stroke-width="0.8"/>')
        for i in range(rp['n']):
            v = d[i]; r = 3.0 + 55 * min(abs(v), 0.2)
            col = (C_BOTH if v > 0 else '#b5473a') if abs(v) > 0.01 else C_NEITHER
            out.append(f'<circle cx="{pos[i][0]:.1f}" cy="{pos[i][1]:.1f}" r="{r:.1f}" fill="{col}" fill-opacity="{0.9 if abs(v) > 0.01 else 0.6}" stroke="{"#20242b" if i in new else "none"}" stroke-width="1.5"/>')
    out.append(f'<text x="320" y="{H - 6}" text-anchor="middle" font-size="12" fill="{SUB}" {f}>one new race among the four outlined boats; dot size is the change (green up, red down, grey none)</text>')
    return f'<svg viewBox="0 0 {W_} {H}" width="100%" role="img" aria-label="Which ratings change after one new race, under Elo and under a joint fit">{"".join(out)}</svg>'


def fig_bars(groups, ylabel, ymax, fmt='{:.2f}', width=640, height=260):
    """groups: [(label, [(series_label, value, colour)])]."""
    l, r_, t_, b_ = 52, 16, 28, 48; f = _SANS
    gw = (width - l - r_) / len(groups)
    Y = lambda v: t_ + (height - t_ - b_) * (1 - v / ymax)
    out = []
    step = ymax / 4
    for k in range(5):
        v = step * k
        out.append(f'<line x1="{l}" y1="{Y(v):.1f}" x2="{width - r_}" y2="{Y(v):.1f}" stroke="{LINE}"/><text x="{l - 8}" y="{Y(v) + 4:.1f}" text-anchor="end" font-size="11" fill="{SUB}" {f}>{fmt.format(v)}</text>')
    legend = {}
    for gi, (lab, bars) in enumerate(groups):
        bw = gw * 0.7 / len(bars); x0 = l + gw * gi + gw * 0.15
        for bi, (sl, v, col) in enumerate(bars):
            x = x0 + bw * bi
            out.append(f'<rect x="{x:.1f}" y="{Y(v):.1f}" width="{bw - 3:.1f}" height="{Y(0) - Y(v):.1f}" fill="{col}"/>')
            out.append(f'<text x="{x + (bw - 3) / 2:.1f}" y="{Y(v) - 5:.1f}" text-anchor="middle" font-size="11" fill="{INK}" {f}>{fmt.format(v)}</text>')
            legend[sl] = col
        out.append(f'<text x="{l + gw * gi + gw / 2:.1f}" y="{height - b_ + 18}" text-anchor="middle" font-size="12" fill="{INK}" {f}>{esc(lab)}</text>')
    lx = l + 8
    for sl, col in legend.items():
        out.append(f'<rect x="{lx}" y="{t_ - 20}" width="12" height="12" fill="{col}"/><text x="{lx + 17}" y="{t_ - 10}" font-size="12" fill="{INK}" {f}>{esc(sl)}</text>')
        lx += 24 + 6.8 * len(sl)
    out.append(f'<text transform="translate(13 {(t_ + height - b_) / 2:.1f}) rotate(-90)" text-anchor="middle" font-size="12" fill="{SUB}" {f}>{esc(ylabel)}</text>')
    return f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{esc(ylabel)}">{"".join(out)}</svg>'


def fig_league_margin(rows):
    """Elo against the site model by league and test: pairwise accuracy, with the bootstrap interval of the site model's margin."""
    W_, l, r_, t_, rowh = 640, 200, 30, 30, 34; f = _SANS
    H = t_ + rowh * len(rows) + 44
    lo_, hi_ = 0.66, 0.74
    X = lambda v: l + (W_ - l - r_) * (v - lo_) / (hi_ - lo_)
    out = []
    for v in (0.66, 0.68, 0.70, 0.72, 0.74):
        out.append(f'<line x1="{X(v):.1f}" y1="{t_ - 6}" x2="{X(v):.1f}" y2="{H - 36}" stroke="{LINE}"/><text x="{X(v):.1f}" y="{H - 20}" text-anchor="middle" font-size="11" fill="{SUB}" {f}>{v * 100:.0f}%</text>')
    out.append(f'<circle cx="{l + 4}" cy="10" r="5" fill="{C_NEITHER}"/><text x="{l + 14}" y="14" font-size="12" fill="{INK}" {f}>Elo</text><circle cx="{l + 60}" cy="10" r="5" fill="{C_A}"/><text x="{l + 70}" y="14" font-size="12" fill="{INK}" {f}>site model</text>')
    for i, r in enumerate(rows):
        y = t_ + rowh * i + rowh / 2
        out.append(f'<text x="{l - 12}" y="{y + 4:.1f}" text-anchor="end" font-size="12" fill="{INK}" {f}>{esc(r["label"])}</text>')
        out.append(f'<line x1="{X(r["elo"]):.1f}" y1="{y:.1f}" x2="{X(r["site"]):.1f}" y2="{y:.1f}" stroke="{C_A}" stroke-opacity="0.45" stroke-width="3"/>')
        out.append(f'<circle cx="{X(r["elo"]):.1f}" cy="{y:.1f}" r="5" fill="{C_NEITHER}"/><circle cx="{X(r["site"]):.1f}" cy="{y:.1f}" r="5" fill="{C_A}"/>')
        out.append(f'<text x="{X(r["site"]) + 9:.1f}" y="{y + 4:.1f}" font-size="11" fill="{SUB}" {f}>+{r["lo"] * 100:.1f} to +{r["hi"] * 100:.1f} pts</text>')
    out.append(f'<text x="{(l + W_ - r_) / 2:.1f}" y="{H - 3}" text-anchor="middle" font-size="12" fill="{SUB}" {f}>share of boat pairs ordered correctly (95% bootstrap interval of the margin, by regatta)</text>')
    return f'<svg viewBox="0 0 {W_} {H}" width="100%" role="img" aria-label="Elo against the site model by league">{"".join(out)}</svg>'


def fig_bars_signed(groups, ylabel, lo_, hi_, fmt='{:+.2f}', width=640, height=260, legend=True):
    """Like fig_bars, with a zero baseline and negative bars; groups: [(label, [(series_label, value, colour)])]."""
    l, r_, t_, b_ = 52, 16, 30, 48; f = _SANS
    gw = (width - l - r_) / len(groups)
    Y = lambda v: t_ + (height - t_ - b_) * (1 - (v - lo_) / (hi_ - lo_))
    out = []
    step = (hi_ - lo_) / 4
    for k in range(5):
        v = lo_ + step * k
        out.append(f'<line x1="{l}" y1="{Y(v):.1f}" x2="{width - r_}" y2="{Y(v):.1f}" stroke="{LINE}"/><text x="{l - 8}" y="{Y(v) + 4:.1f}" text-anchor="end" font-size="11" fill="{SUB}" {f}>{fmt.format(v)}</text>')
    out.append(f'<line x1="{l}" y1="{Y(0):.1f}" x2="{width - r_}" y2="{Y(0):.1f}" stroke="{INK}"/>')
    leg = {}
    for gi, (lab, bars) in enumerate(groups):
        bw = gw * 0.72 / len(bars); x0 = l + gw * gi + gw * 0.14
        for bi, (sl, v, col) in enumerate(bars):
            x = x0 + bw * bi; top, bot = (Y(v), Y(0)) if v >= 0 else (Y(0), Y(v))
            out.append(f'<rect x="{x:.1f}" y="{top:.1f}" width="{max(bw - 3, 2):.1f}" height="{bot - top:.1f}" fill="{col}"/>')
            if len(bars) == 1: out.append(f'<text x="{x + (bw - 3) / 2:.1f}" y="{(Y(v) - 5) if v >= 0 else (Y(v) + 13):.1f}" text-anchor="middle" font-size="11" fill="{INK}" {f}>{fmt.format(v)}</text>')
            leg[sl] = col
        out.append(f'<text x="{l + gw * gi + gw / 2:.1f}" y="{height - b_ + 18}" text-anchor="middle" font-size="12" fill="{INK}" {f}>{esc(lab)}</text>')
    if legend:
        lx = l + 8; ly = t_ - 20
        for sl, col in leg.items():
            wdt = 24 + 6.6 * len(sl)
            if lx + wdt > width - r_: lx = l + 8; ly += 15
            out.append(f'<rect x="{lx}" y="{ly}" width="12" height="12" fill="{col}"/><text x="{lx + 17}" y="{ly + 10}" font-size="12" fill="{INK}" {f}>{esc(sl)}</text>'); lx += wdt
    out.append(f'<text transform="translate(13 {(t_ + height - b_) / 2:.1f}) rotate(-90)" text-anchor="middle" font-size="12" fill="{SUB}" {f}>{esc(ylabel)}</text>')
    return f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{esc(ylabel)}">{"".join(out)}</svg>'


def fig_venue_means(vm, width=640):
    """Horizontal dot-and-interval chart of venue mean residual orderliness (fleet removed), sorted; +-2 empirical se."""
    items = sorted(vm.items(), key=lambda kv: -kv[1]['mean']); rowh = 17; l, r_, t_, b_ = 170, 20, 26, 40; H = t_ + rowh * len(items) + b_
    lo_, hi_ = -0.18, 0.22; X = lambda v: l + (width - l - r_) * (v - lo_) / (hi_ - lo_); f = _SANS; out = []
    for v in (-0.1, 0, 0.1, 0.2):
        out.append(f'<line x1="{X(v):.1f}" y1="{t_ - 6}" x2="{X(v):.1f}" y2="{H - b_ + 4}" stroke="{INK if v == 0 else LINE}"/><text x="{X(v):.1f}" y="{H - b_ + 18}" text-anchor="middle" font-size="11" fill="{SUB}" {f}>{v:+.1f}</text>')
    for i, (name, d) in enumerate(items):
        y = t_ + rowh * i + rowh / 2; col = C_BOTH if d['mean'] > 0 else '#b5473a'
        nm = name if len(name) <= 22 else name.replace('Yacht and Country Club', 'YC').replace('Hobart & William Sm', 'Hobart & Wm Smith')[:22]
        out.append(f'<text x="{l - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="{INK}" {f}>{esc(nm)} <tspan fill="{SUB}">({d["n"]})</tspan></text>')
        out.append(f'<line x1="{X(d["mean"] - 2 * d["se"]):.1f}" y1="{y:.1f}" x2="{X(d["mean"] + 2 * d["se"]):.1f}" y2="{y:.1f}" stroke="{col}" stroke-opacity="0.45" stroke-width="3"/><circle cx="{X(d["mean"]):.1f}" cy="{y:.1f}" r="4" fill="{col}"/>')
    out.append(f'<text x="{(l + width - r_) / 2:.1f}" y="{H - 4}" text-anchor="middle" font-size="12" fill="{SUB}" {f}>right = favourites\' venue, left = underdogs\' (bar = &plusmn;2 se)</text>')
    return f'<svg viewBox="0 0 {width} {H}" width="100%" role="img" aria-label="Venue mean residual orderliness">{"".join(out)}</svg>'


def chart_gap(bins):
    """Per rating-gap bin: share of teams that put the stronger skipper in B (blue) next to the share where the model prefers it (green)."""
    W_, H, l, b_, t_ = 640, 270, 44, 64, 16
    bw = (W_ - l - 16) / len(bins)
    ys = lambda v: t_ + (H - t_ - b_) * (1 - v / 0.8)
    f = 'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif"'
    out = []
    for v in (0, .2, .4, .6, .8):
        out.append(f'<line x1="{l}" y1="{ys(v):.1f}" x2="{W_ - 10}" y2="{ys(v):.1f}" stroke="{LINE}"/><text x="{l - 8}" y="{ys(v) + 4:.1f}" text-anchor="end" font-size="11" fill="{SUB}" {f}>{int(v * 100)}%</text>')
    for i, b in enumerate(bins):
        x0 = l + i * bw + bw * 0.12; w = bw * 0.36
        for j, (val, col) in enumerate(((b['chose_b'], C_A), (b['prefers_b'], C_BOTH))):
            x = x0 + j * (w + 6); h = (H - t_ - b_) * val / 0.8
            out.append(f'<rect x="{x:.1f}" y="{ys(val):.1f}" width="{w:.1f}" height="{h:.1f}" fill="{col}"/>'
                       f'<text x="{x + w / 2:.1f}" y="{ys(val) - 6:.1f}" text-anchor="middle" font-size="13" font-weight="700" fill="{INK}" {f}>{val * 100:.0f}%</text>')
        out.append(f'<text x="{x0 + w + 3:.1f}" y="{H - 42}" text-anchor="middle" font-size="12" fill="{INK}" {f}>{esc(b["label"])}</text>'
                   f'<text x="{x0 + w + 3:.1f}" y="{H - 27}" text-anchor="middle" font-size="11" fill="{SUB}" {f}>n={b["n"]}</text>')
    out.append(f'<rect x="{l}" y="{H - 14}" width="12" height="12" fill="{C_A}"/><text x="{l + 18}" y="{H - 4}" font-size="12" fill="{INK}" {f}>teams that put the stronger skipper in B</text>'
               f'<rect x="{l + 270}" y="{H - 14}" width="12" height="12" fill="{C_BOTH}"/><text x="{l + 288}" y="{H - 4}" font-size="12" fill="{INK}" {f}>model prefers the stronger skipper in B</text>')
    out.append(f'<text x="{(l + W_) / 2:.0f}" y="{H - 52}" text-anchor="middle" font-size="11" fill="{SUB}" {f}></text>')
    return f'<svg viewBox="0 0 {W_} {H + 2}" role="img" aria-label="How often teams put the stronger skipper in B against how often the model prefers it, by rating gap">{"".join(out)}</svg>'


def grids_svg(grids, ncols=2, pw=310, ph=176):
    """One 2x2 per program: rows = what it did (stronger skipper in A / in B), columns = what the model prefers. Green = they agree, red = they disagree."""
    f = 'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif"'
    rows_n = (len(grids) + ncols - 1) // ncols; W_ = 640; H = rows_n * ph + 6
    mx = max((max(c for row in g['grid'] for c in row) for g in grids), default=1) or 1
    white, green, red = (253, 251, 247), (47, 125, 79), (179, 65, 47)
    out = []
    for k, g in enumerate(grids):
        x0 = (k % ncols) * (pw + 20); y0 = (k // ncols) * ph
        name = short_school(g['school'])
        out.append(f'<text x="{x0}" y="{y0 + 16}" font-size="15" font-weight="700" fill="{INK}" {f}>{esc(name)}</text>'
                   f'<text x="{x0}" y="{y0 + 33}" font-size="11" fill="{SUB}" {f}>{g["clear"]} clear entries of {g["n"]} ({g["toss_ups"]} toss-ups)</text>')
        for j, lab in enumerate(('model: A', 'model: B')):
            out.append(f'<text x="{x0 + 178 + j * 78}" y="{y0 + 54}" text-anchor="middle" font-size="11" fill="{SUB}" {f}>{lab}</text>')
        for i, lab in enumerate(('stronger skipper in A', 'stronger skipper in B')):
            out.append(f'<text x="{x0 + 132}" y="{y0 + 82 + i * 44}" text-anchor="end" font-size="11" fill="{SUB}" {f}>{lab}</text>')
            for j in range(2):
                c = g['grid'][i][j]; agree = (i == j)
                t = 0 if c == 0 else 0.28 + 0.72 * c / mx
                col = _mix(white, green if agree else red, t) if c else '#f2efe9'
                tc = '#fff' if t > 0.55 else (INK if c else '#b9bec4')
                cx = x0 + 140 + j * 78
                out.append(f'<rect x="{cx}" y="{y0 + 62 + i * 44}" width="74" height="40" rx="3" fill="{col}"/>'
                           f'<text x="{cx + 37}" y="{y0 + 88 + i * 44}" text-anchor="middle" font-size="19" font-weight="700" fill="{tc}" {f}>{c}</text>')
        out.append(f'<text x="{x0}" y="{y0 + 158}" font-size="11" fill="{SUB}" {f}>{g["cost_per"]:.1f} points given up per entry vs the model\'s best</text>')
    return f'<svg viewBox="0 0 {W_} {H}" role="img" aria-label="Per program: stronger skipper in A or B against the model preference">{"".join(out)}</svg>'

# ---- posts -----------------------------------------------------------------------------------------
def post_a_vs_b():
    s = a_vs_b.compute(); a = s['all']
    big = s['by_size']['16+ teams']; small = s['by_size']['8-11 teams']
    gap = a['b_only'] - a['a_only']
    body = f'''
  <h2>The question</h2>
  <p>Most college regattas are scored in two divisions. A team's total is its A-division points plus its B-division points, so both count the same. But people talk about "the A boat" as if it were the one that decides things. Does it?</p>

  <h2>What I looked at</h2>
  <p>Every two-division fleet regatta with at least eight teams from fall 2019 to spring 2026 (2020 left out): <strong>{a['n']:,} regattas</strong>. For the team that won overall, I checked whether it also won the A division, the B division, both, or neither.</p>
  <div class="fig">{chart_ab(s)}<div class="cap">Share of regatta winners by the division(s) they also won.</div></div>

  <h2>What I found</h2>
  <p><strong>Neither division is the decider.</strong> Winners took B only in {pct(a['b_only'])} of regattas and A only in {pct(a['a_only'])}. That {gap:.1f}-point gap is about {gap / s['se_gap']:.1f} standard errors, so I would read it as a tie, not as B mattering more. The winner's average finish was {s['mean_rank_a']:.2f} in A and {s['mean_rank_b']:.2f} in B, and the spread of points across teams is about the same in both divisions ({s['spread_a']:.0f} and {s['spread_b']:.0f}).</p>
  <p><strong>Being good in both is the usual way to win.</strong> {pct(a['both'])} of winners won both divisions, and {pct(100 - a['neither'])} won at least one. Only {pct(a['neither'])} won the regatta without winning either division, by being consistent in both.</p>
  <p><strong>Bigger fields make it harder to sweep.</strong> At 8 to 11 teams, {pct(small['both'])} of winners won both divisions. At 16 or more teams that falls to {pct(big['both'])}, and winning neither division rises from {pct(small['neither'])} to {pct(big['neither'])}.</p>
  <div class="fig">{chart_size(s)}<div class="cap">The same split, by number of teams (regatta count in parentheses).</div></div>

  <h2>Caveats</h2>
  <ul class="plain">
    <li>Scoring weights A and B equally by construction, so this says nothing about which skipper is better. It says the result is decided by both divisions together.</li>
    <li>Division points include penalty scores. I treated a team as having won a division if no team scored fewer points there; ties for first overall were skipped.</li>
    <li>"A" and "B" are assigned by each school's lineup, not by skill. A school's strongest skipper usually sails A, but not always.</li>
  </ul>'''
    return dict(slug='a-vs-b', title='A or B: which division decides the regatta?', date=date(2026, 5, 7), date_label='May 7, 2026', script='analysis/blog/a_vs_b.py',
                summary=f'Across {a["n"]:,} two-division regattas, the winner took A only {pct(a["a_only"])} of the time and B only {pct(a["b_only"])}. {pct(a["both"])} won both. Neither division is the one that decides.', body=body)


def post_calibration():
    s = calibration.compute(); bins = s['bins']
    rows = ''.join(f"<tr><td>{int(b['lo'] * 100)}&ndash;{int(b['hi'] * 100)}%</td><td>{b['n']}</td><td>{b['pred'] * 100:.1f}%</td><td>{b['actual'] * 100:.1f}%</td><td>{b['expected']:.1f}</td><td>{b['wins']}</td></tr>" for b in bins)
    top = bins[-1]
    body = f'''
  <h2>The question</h2>
  <p>The site shows a win probability for every team before a regatta. If a team is listed at 30%, it should win about three times in ten. This checks whether that is true.</p>

  <h2>What I looked at</h2>
  <p>The {s['events']} fall 2026 regattas with eight or more teams, from {s['first']:%B %-d} to {s['last']:%B %-d}. For each one the model was refit using only races from earlier weeks, then asked for every team's chance of winning, given the lineups that actually sailed. That is <strong>{s['entries']} team entries</strong>. I grouped them by the stated chance and counted how often each group actually won.</p>
  <div class="fig">{chart_dumbbell(bins)}<div class="cap">Hollow dot: average predicted chance of winning in the group. Solid dot: how often those teams won.</div></div>
  <table class="t"><thead><tr><th>Stated chance</th><th>Entries</th><th>Predicted</th><th>Actual</th><th>Wins expected</th><th>Wins</th></tr></thead><tbody>{rows}</tbody></table>

  <h2>What I found</h2>
  <p><strong>In total the model got the number of winners right.</strong> It expected {s['expected']:.1f} winners across the {s['events']} regattas and there were {s['wins']} (one regatta finished tied for first).</p>
  <p><strong>The groups track the stated chances, within what luck allows.</strong> Teams listed under 2% won {bins[0]['wins']} of {bins[0]['n']}. Teams listed above 55% won {top['wins']} of {top['n']} where the model expected {top['expected']:.1f}. With only {s['wins']} winners to spread over seven groups, any single group can be a win or two off by chance, so I would not read much into one row.</p>

  <h2>The harder test</h2>
  <p>That check gives the model the real lineups. A forecast made before an event has to guess them. I also scored the forecasts the site saved at least a day before an event, with predicted lineups: in {s['live_n']} regattas the top pick won {s['live_wins']} times, where the model's own stated odds expected about {s['live_expected']:.0f}.</p>
  <p>That is the number I care about most, and it is worse. My working explanation is that guessing who sails is where the error comes from, because the same model is well calibrated once the lineups are known. I have not proved that, and {s['live_n']} regattas is a small sample. Improving the lineup forecasts is what I am working on next.</p>

  <h2>Caveats</h2>
  <ul class="plain">
    <li>Forty regattas is a small sample. These are checks, not proof.</li>
    <li>The first check uses the lineups that actually sailed, which a forecast before the event does not have.</li>
    <li>Win probabilities come from 10,000 simulated regattas per event, so each has a small simulation error.</li>
  </ul>'''
    return dict(slug='odds-calibration', title='Do the odds mean what they say?', date=date(2026, 5, 14), date_label='May 14, 2026', script='analysis/blog/calibration.py',
                summary=f'Across {s["events"]} fall regattas the model expected {s["expected"]:.0f} winners and saw {s["wins"]}. With the real lineups the odds hold up; forecasting before the event, with guessed lineups, is where they do not yet.', body=body)


def post_rating_point():
    s = rating_point.compute()
    pc = s['percentiles']
    prow = ''.join(f"<tr><td>{pc[k]:.1f}</td><td>{label}</td></tr>" for k, label in (('50', '50th percentile (the median rated skipper)'), ('75', '75th'), ('90', '90th'), ('95', '95th'), ('99', '99th'), ('99.9', '99.9th')))
    trow = ''.join(f"<tr><td>{d:g}</td><td>{p * 100:.0f}%</td></tr>" for d, p in s['table'])
    cal = s['calibration']
    crow = ''.join('<tr><td>' + format(c['pred'] * 100, '.1f') + '%</td><td>' + format(c['obs'] * 100, '.1f') + '%</td><td>' + format(c['n'], ',') + '</td></tr>' for c in cal)
    body = f'''
  <h2>The short version</h2>
  <p>Ratings here are on a logistic scale. If one boat is rated <strong>1 point higher</strong> than another, it beats it in a given race about <strong>73%</strong> of the time. Two points is 88%. The model's own out-of-sample check says those chances are about right.</p>

  <h2>How a rating gap turns into a chance</h2>
  <p>The model gives every sailor a rating each week. The chance that boat <em>i</em> finishes ahead of boat <em>j</em> in a race is 1 / (1 + e<sup>&minus;(r<sub>i</sub> &minus; r<sub>j</sub>)</sup>). Only the gap matters.</p>
  <div class="fig">{chart_curve()}<div class="cap">Chance of beating another boat in one race, by rating gap.</div></div>
  <table class="t"><thead><tr><th>Rating gap</th><th>Chance the higher-rated boat finishes ahead</th></tr></thead><tbody>{trow}</tbody></table>

  <h2>What a rating looks like</h2>
  <p>Ratings are not capped at anything, so the useful way to read one is as a percentile among rated ICSA skippers.</p>
  <table class="t"><thead><tr><th>Rating</th><th>Where it sits</th></tr></thead><tbody>{prow}</tbody></table>
  <p>The top-rated skipper sailing this fall is at {s['top_now']:.2f}. Against the median rated skipper that is a gap of about {s['top_now'] - pc['50']:.1f}, which is a {rating_point.p_beats(s['top_now'] - pc['50']) * 100:.0f}% chance to finish ahead in any one race. Against a 90th-percentile skipper it is {rating_point.p_beats(s['top_now'] - pc['90']) * 100:.0f}%.</p>

  <h2>Does the model's chance match reality?</h2>
  <p>Fit the ratings on earlier seasons, then check them against later races: for every pair of boats that raced, take the model's chance that the favorite finishes ahead and see how often it did. The model is close throughout, a little over-confident at the high end.</p>
  <table class="t"><thead><tr><th>Predicted chance</th><th>Favorite actually finished ahead</th><th>Pairs</th></tr></thead><tbody>{crow}</tbody></table>

  <h2>Caveats</h2>
  <ul class="plain">
    <li>This is per race, between two boats. A whole regatta has many races and many boats, so a rating edge shows up in the standings more than in any one result.</li>
    <li>The percentiles are among ICSA skippers with a rating, which includes graduated sailors.</li>
    <li>Singlehanded, match-race and keelboat events have their own ratings and are not covered here.</li>
  </ul>'''
    return dict(slug='what-a-rating-point-is-worth', title='What a rating point is worth', date=date(2026, 3, 12), date_label='March 12, 2026', script='analysis/blog/rating_point.py',
                summary='A one-point rating edge means a 73% chance to beat another boat in a race; two points is 88%. How to read the numbers on the site, and a check that those chances match what happened.', body=body)



def post_swap():
    nat = nationals_swap.compute(); op = nat['open']
    ex = nat['examples']['Jacksonville University']
    de = division_effect.compute()
    reg = swap_map.regatta('nationals'); xs, z = swap_map.grid(reg); sm = swap_map.summary(reg, xs, z)
    hx, hh = swap_threshold.hump(reg['rivA'], reg['rivB']); m = hx[max(range(len(hh)), key=hh.__getitem__)]
    thr_out, thr_flat = swap_threshold.compute(); thr = swap_threshold.summary(thr_out, thr_flat)
    c_by = {o['event']: o for o in thr_out}
    c_nat = c_by['Open Fleet Race National Championships 2026']; c_low = c_by["Women's Western National Semis 2026"]
    wrong = [f for f in thr_flat if not f['rule_mean'] and f['did_a']]
    w_cost = sum(max(0, -f['change']) for f in wrong) / len(wrong); w_max = max(-f['change'] for f in wrong)
    thr_rows = ''; thr_m = []; thr_t = []
    for lo_, hi_, lab in ((-9, -1.5, 'more than 1.5 below'), (-1.5, -0.75, '0.75 to 1.5 below'), (-0.75, 0, 'up to 0.75 below'), (0, 0.75, 'up to 0.75 above'), (0.75, 1.5, '0.75 to 1.5 above'), (1.5, 9, 'more than 1.5 above')):
        b_ = [f for f in thr_flat if lo_ < f['total'] - f['mean_sum'] <= hi_]
        if b_:
            thr_m.append(sum(f['exact_a'] for f in b_) / len(b_) * 100); thr_t.append(sum(f['did_a'] for f in b_) / len(b_) * 100)
            thr_rows += f"<tr><td>{lab}</td><td>{len(b_)}</td><td>{thr_m[-1]:.0f}%</td><td>{thr_t[-1]:.0f}%</td></tr>"
    bucket_rows = ''.join(
        '<tr><td>' + esc(c['label']) + '</td>' + ''.join(
            ('<td>' + format(c[d]['n'], ',') + '</td><td>' + format(c[d]['mean'], '+.2f') + ' (' + format(c[d]['lo'], '+.2f') + ' to ' + format(c[d]['hi'], '+.2f') + ')</td>') if d in c else '<td></td><td></td>'
            for d in ('A', 'B')) + '</tr>' for c in de['curve'])
    body = f'''
  <p class="lede">This is a sensitivity question, and it has a one-line answer: <strong>add the two skippers' ratings and compare the total with the field. Above the line, the better skipper belongs in A. Below it, in B.</strong></p>
  <p>Almost nobody follows the second half. Across {thr['n']} clear cases at {thr['events']} events, teams below the line still started their better skipper in A {thr['below_did_a'] * 100:.0f}% of the time, where the model wanted that in {thr['below_exact_a'] * 100:.0f}%. They gave up {w_cost:.1f} points on average against their best arrangement, and up to {w_max:.0f}. The rest of this note derives the line, shows it on a map, and tests it.</p>

  <h2>Points are a curve in your rating</h2>
  <p>Score in a division is places, so a boat's expected points per race are a function of its rating <em>r</em> and of the rivals it races. With the Plackett&ndash;Luce model, the chance of beating rival <em>j</em> is p<sub>j</sub>(r) = 1 / (1 + e<sup>r<sub>j</sub> &minus; r</sup>) in rating units, and</p>
  <p class="eq">E(r) = 1 + &Sigma;<sub>j</sub> (1 &minus; p<sub>j</sub>(r))</p>
  <p>one point for yourself, plus one for every rival who beats you. E falls as rating rises, and it falls fastest where many rivals sit close to you. Here are the A-division and B-division fields at the {op['name'].replace('Championships', 'Championship')} ({op['teams']} teams, {op['races']} races), with {ex['school'].replace(' University', '')}'s two skippers ({ex['hi']:.2f} and {ex['lo']:.2f}) marked on each.</p>
  <div class="fig">{fig_points_curve(ex)}<div class="cap">Expected points per race against skipper rating, in the A field and the B field, rivals only.</div></div>

  <h2>Sensitivity, and the area under it</h2>
  <p>The derivative is the sensitivity: g(r) = &minus;dE/dr = &Sigma;<sub>j</sub> p<sub>j</sub>(1 &minus; p<sub>j</sub>). Each rival contributes a bell centered on its own rating, so g is a smoothed density of the field. It is large where many rivals are within a rating point or two of you, and near zero for a boat far above or far below everyone. That is what fleet skill means here: where the field is packed.</p>
  <div class="fig">{fig_density(ex)}<div class="cap">Sensitivity &minus;dE/dr for the same two fields. The shaded area between the team's two ratings is the boats a skipper passes per race by being the stronger one in that division.</div></div>
  <p>Integrating, upgrading a division from the weaker skipper <em>w</em> to the stronger <em>s</em> saves S = &int;<sub>w</sub><sup>s</sup> g(r) dr = E(w) &minus; E(s) places per race. Here that is {ex['SA']:.2f} boats in the A field and {ex['SB']:.2f} in the B field. A team's total is E<sub>A</sub> + E<sub>B</sub>, so with the stronger skipper in A, swapping the two changes the predicted total by races &times; (S<sub>A</sub> &minus; S<sub>B</sub>). For {ex['school'].replace(' University', '')} that is {ex['change']:+.1f} points over {ex['races']:.0f} races. <strong>The sum is maximized by putting the stronger skipper in the division with the larger area</strong>, and the areas depend only on where the two fields are packed.</p>

  <h2>A map of every pairing</h2>
  <p>Do that for every pair of ratings. Each cell below is a hypothetical team with an A skipper rated as on the horizontal axis and a B skipper as on the vertical axis, in this one fleet. Color is the change in predicted points if the two swap: green helps, red hurts. The map is antisymmetric across the dashed diagonal (swapping twice returns you), so it is zero on the diagonal. Below the diagonal the stronger skipper is in A, as usual; circles are the teams that sailed.</p>
  <div class="fig">{heat_svg(reg, xs, z)}<div class="cap">{esc(reg['title'])}: change in predicted points if A and B are swapped, {reg['races']:.0f} races per division. Clipped at &plusmn;6.</div></div>
  <p>Inside this field's rating range, with the stronger skipper in A, a swap lowers the predicted total by more than half a point in {sm['share_helps'] * 100:.0f}% of pairings and raises it in {sm['share_hurts'] * 100:.0f}%. The best case gains {abs(sm['max_help']):.1f} points and the worst costs {sm['max_hurt']:.1f}. The green zone is a strong A skipper over a B skipper well below the rest of the B field, where the B field is dense and a better boat passes many. The red zone is two strong skippers, where there are few boats left to pass. The green and red regions are separated by bands that run along lines of constant rating sum.</p>

  <h2>Why it comes down to a sum</h2>
  <p>Let H(r) = E<sub>A</sub>(r) &minus; E<sub>B</sub>(r), the places a skipper gives up per race by sailing A instead of B. The swap change is exactly races &times; (H(w) &minus; H(s)), so the stronger skipper belongs in A when A duty costs them less than it costs the weaker one. Differentiate: H&prime;(r) = &minus;(g<sub>A</sub>(r) &minus; g<sub>B</sub>(r)). The A field is packed higher in the ratings than the B field, so g<sub>B</sub> is larger on the left and g<sub>A</sub> on the right, and they cross at a rating <em>m</em>. H rises until <em>m</em> and falls after it.</p>
  <div class="fig">{fig_hump(reg)}<div class="cap">H(r) at the 2026 Open Nationals, peaking at m = {m:.1f}. A weak skipper finishes near last in either division and a strong one near first, so A duty costs them little. It costs most mid-fleet.</div></div>
  <p>Below <em>m</em>, being the stronger skipper is worth more in B. Above it, more in A. If H is roughly symmetric about <em>m</em>, then H(w) &gt; H(s) just means <em>w</em> is closer to <em>m</em> than <em>s</em> is, and (s &minus; w)(s + w &minus; 2m) &gt; 0 gives the rule:</p>
  <p class="eq"><strong>stronger skipper in A &nbsp;&hArr;&nbsp; r<sub>A</sub> + r<sub>B</sub> &gt; c</strong> &nbsp; with c &asymp; 2m set by the fleet</p>
  <p>The two skippers' ratings enter only through their sum, because the areas on each side of <em>m</em> trade off one for one. The fleet decides where the line sits. At the Open Nationals, a deep field, the line is a total of {c_nat['c']:.1f}; at the Women's Western Semifinals it is {c_low['c']:.1f}. The same pair of skippers can belong in A in one fleet and B in another, which is why comparing teams by "better skipper in A or B" without the fleet mixes cells of the map that point in opposite directions.</p>

  <h2>Does the line hold?</h2>
  <p>H is not perfectly symmetric, so I did not take c = 2m on faith. For each of {thr['events']} events (the ICSA championships since fall 2024 and this fall's regattas) I computed every team's swap change from pre-event ratings, and found the one threshold on the sum that best separates the entries where the model wants A from those where it wants B. I counted only entries where the two skippers are at least a quarter of a rating point apart and the swap matters by at least half a point.</p>
  <p><strong>One cut on the sum is enough.</strong> With a threshold fitted to each event, the sum rule matches the full model's verdict in {thr['agree_best'] * 100:.1f}% of {thr['n']} clear cases. The threshold is fitted, so this shows the sum is the right statistic, not that the line is known in advance. Using the field's average A+B total as the line, with no fitting, it matches {thr['agree_mean'] * 100:.0f}%.</p>
  <table class="t cases"><thead><tr><th>Your two ratings vs the field's average</th><th>Entries</th><th>Model wants the better skipper in A</th><th>Teams put the better skipper in A</th></tr></thead><tbody>{thr_rows}</tbody></table>
  <p>The model's column swings from {thr_m[0]:.0f}% to {thr_m[-1]:.0f}% across the line. The teams' column stays between {min(thr_t):.0f}% and {max(thr_t):.0f}%. Teams respond to rating gaps, not to where those ratings sit in the fleet, which is the whole point of a sensitivity analysis.</p>

  <h2>Are the curves right?</h2>
  <p>The map is only as good as E(r). I tested it on real races at the fall 2026 regattas ({de['races']} races, {de['n']:,} boat-races at {de['events']} events), conditioning on the fleet: for each race I computed every boat's expected place from its pre-event rating against the other boats in that race, and compared with the place it earned.</p>
  <p>Regressing earned place on expected place within each race gives a slope of {de['A']['slope']:.2f} in A (95% interval {de['A']['lo']:.2f} to {de['A']['hi']:.2f}) and {de['B']['slope']:.2f} in B ({de['B']['lo']:.2f} to {de['B']['hi']:.2f}); 1 means the curve is as steep as reality. Grouped by rating relative to the field, the gap between earned and expected place is small everywhere, and every interval includes zero:</p>
  <table class="t cases"><thead><tr><th>Rating vs the field</th><th>A boats</th><th>Earned minus expected place (95% interval)</th><th>B boats</th><th>Earned minus expected place (95% interval)</th></tr></thead><tbody>{bucket_rows}</tbody></table>
  <p>A direct before-and-after test is not possible: teams almost never move a skipper between divisions within a regatta, and comparing teams that put their better skipper in B with those that did not pools cells of the map that point in opposite directions. Testing the two ingredients, the curve's steepness and the same curve in A and B, is what the evidence can support.</p>

  <h2>Caveats</h2>
  <ul class="plain">
    <li>The effect is a few points over a regatta, against typical misses of about 30, so it matters in a close finish.</li>
    <li>Ratings are estimates, and a gap of half a point is within their error. The model ignores conditions, crews, and coaches developing sailors.</li>
    <li>Skippers rotate; for team entries I used each division's most frequent skipper.</li>
    <li>The real-races check covers fall 2026 only, where pre-event ratings exist for every race.</li>
  </ul>'''
    return dict(slug='best-sailor-in-a', title='Should the best sailor sail A?', date=date(2026, 4, 30), date_label='April 30, 2026', script='analysis/blog/swap_map.py, nationals_swap.py, swap_threshold.py and division_effect.py',
                summary=f"A sensitivity analysis. Add your two skippers' ratings and compare the total with the field's average. Above it, the better skipper belongs in A. Below it, in B. Teams below the line still start the better skipper in A {thr['below_did_a'] * 100:.0f}% of the time.", body=body)


def post_crew():
    d = json.loads((ROOT / 'analysis' / 'blog' / 'crew_example.json').read_text())
    # Crews and skippers are anonymized: crews are numbered by rating, skippers by how much racing they share with the crew shown.
    orig = {r['name']: i + 1 for i, r in enumerate(d['rows'])}
    k_top, k_soder, k_ham = orig['Dominic Ciccimaro'], orig['Marin Soderberg'], orig['Laura Hamilton']
    lab = {nm: f'Crew {i}' for nm, i in orig.items()}
    for r in d['rows']:
        r['name'] = lab[r['name']]; r['year'] = ''
    d['order_names'] = [lab[x] for x in d['order_names']]
    for nd in d['graph']['nodes']:
        nd['name'] = lab.get(nd['name'], '') if nd['type'] == 'crew' else ''
    for v in d['graph']['detail'].values():
        v['name'] = lab[v['name']]
        for j, r in enumerate(v['rows']):
            r['skipper'] = f'Skipper {chr(65 + j)}'
    rows = d['rows']
    top, ham, soder = rows[k_top - 1], rows[k_ham - 1], rows[k_soder - 1]
    n = len(rows)
    ppl = [NormalDist(0, 1.4).inv_cdf((i + 0.5) / 12) for i in range(12)]
    me = ppl[6]; per_point = sum(1 / (1 + math.exp(me - x)) * (1 - 1 / (1 + math.exp(me - x))) for i, x in enumerate(ppl) if i != 6)
    P = [(x if x is not None else float('nan')) for x in d['p_matrix']]
    pair = [max(P[i * n + j], 1 - P[i * n + j]) for i in range(n) for j in range(i + 1, n)]
    clear80 = sum(1 for v in pair if v >= 0.8); clear90 = sum(1 for v in pair if v >= 0.9)
    ev = json.loads((ROOT / 'analysis' / 'crew_ratings' / 'crew_seasons_eval.json').read_text())['static crew effect x1']
    tbl = ''.join(f"<tr><td>{esc(r['name'])}</td><td>{r['career']}</td><td>{r['partners']}</td><td>{r['with_crew']:+.2f}</td><td>{r['without_crew']:+.2f}</td><td>{r['lift']:+.2f} &plusmn; {r['lift_se']:.2f}</td><td><strong>{r['rel']:+.2f} &plusmn; {r['sd']:.2f}</strong></td></tr>" for r in (top, ham, soder))
    body = f"""
  <p class="lede">A crew rating says how much faster a boat goes, in skipper-rating points, when a given crew is aboard. <strong>The honest answer is that it is small and uncertain, and the useful part is the error bar.</strong> The best crew on <a href="/icsa/schools/brown/f25/">Brown's fall 2025 roster</a> rates +{top['rel']:.2f} &plusmn; {top['sd']:.2f} against a roster average of zero, about {top['rel'] * per_point:.1f} places a race, and still has only a {top['p_best'] * 100:.0f}% chance of being the best crew on the roster.</p>
  <p>This note builds that number from scratch on the {n} ranked crews of <a href="/icsa/schools/brown/f25/">Brown's fall 2025 roster</a>, as of the end of fall 2025: only races through that season, with skipper ratings as they stood then.</p>

  <h2>Step 1: what a race says about a crew</h2>
  <p>A skipper's rating already predicts how their boat should finish. Hold it fixed and look at what happened. In the Plackett&ndash;Luce model, each boat has a surprisal: <strong>g</strong>, how much better the boat finished than its rating predicted (the score of the finishing order with respect to the boat's strength), and <strong>h</strong>, how much information that race carries about the boat's strength. Divide the totals and you get a reading in rating units: a crew with total G and H has reading G / H, with standard error about 1/&radic;H. One race is mostly noise.</p>

  <h2>Step 2: compare with the same skippers without the crew</h2>
  <p>You cannot just average a crew's readings. A skipper's rating is fitted to all of their racing, <em>including</em> what their crews added, so a skipper who always sails with great crews is rated a little high, and every crew of theirs then looks worse than it is. The fix is to compare a crew only against the same skippers in their other races. The skipper, and whatever their rating absorbed, appears on both sides and cancels. The difference is the <strong>lift</strong>.</p>
  <p>Take {esc(top['name'])}, who has crewed {top['career']} races for {top['partners']} different skippers. The boats they crewed went {top['with_crew']:+.2f} rating points better than their skippers' ratings predicted. The same skippers in all their other races went {top['without_crew']:+.2f}. That is a lift of {top['lift']:+.2f} &plusmn; {top['lift_se']:.2f}, three standard errors from zero.</p>

  <div class="fig">{fig_withwithout(rows)}<div class="cap">Brown fall 2025: for each crew, how the boats did with them aboard and how the same skippers did the rest of the time, sorted by raw lift.</div></div>

  <h2>Step 3: shrink what you cannot trust</h2>
  <p>Raw lifts are noisy, and the extremes are mostly luck. Across all rated crews, true crew effects are small: the prior is a normal with standard deviation &tau; = 0.12 rating points (fitted from the data), against 1.4 for skippers. So a crew with no evidence sits at 0, and each race pulls it toward its reading by its share of the evidence. For one reading at a time (Step 4 does it jointly):</p>
  <p class="eq">rating = reading &times; I / (I + 1/&tau;&sup2;), &nbsp; sd = 1/&radic;(I + 1/&tau;&sup2;)</p>
  <p>where I is the information, the sum of h over the crew's races, scaled by 0.6 because races within a regatta are not independent (calibrated on forecasts, see below). The table shows what this does to three Brown crews.</p>
  <table class="t cases"><thead><tr><th>Crew</th><th>Career races</th><th>Skippers</th><th>With crew</th><th>Same skippers without</th><th>Raw lift</th><th>Rating (vs roster)</th></tr></thead><tbody>{tbl}</tbody></table>
  <p><strong>{esc(ham['name'])} had a raw lift of {ham['lift']:+.2f}, almost as big as {esc(top['name'])}'s, and rates {ham['rel']:+.2f}.</strong> It rests on {ham['career']} races with {ham['partners']} skippers, a standard error of {ham['lift_se']:.2f}, so the data cannot tell them from average. {esc(top['name'])}'s rests on {top['career']} races with {top['partners']} skippers, so most of it survives. {esc(soder['name'])} is the opposite case: {soder['career']} races with {soder['partners']} skippers, plenty of evidence, and the answer is that they are about average ({soder['rel']:+.2f} &plusmn; {soder['sd']:.2f}).</p>
  <div class="fig">{fig_shrink(rows, {top['name']: (-8, -10, 'end'), ham['name']: (8, 4, 'start'), soder['name']: (8, 16, 'start')})}<div class="cap">Brown fall 2025: raw lift against final rating (relative to the roster average). The ratings are spread far less than the raw lifts, and the crews with the least evidence (long bars) move the most.</div></div>

  <h2>Step 4: fit the whole roster at once</h2>
  <p>Crews and skippers form a network: two crews are linked when they sailed with the same skipper. The final rating solves all the comparisons together, with each (crew, skipper) pair's reading modeled as the crew's effect plus a free offset for the skipper. That chains comparisons through skippers who crewed with several people, so a crew can be compared with a teammate they never sailed with, with an error that grows with the length and thinness of the path. Ratings are reported relative to the roster average, because that is what is identified.</p>
  <div class="fig">{fig_network(d['graph'], rows)}<div class="cap">The comparison network behind Brown's fall 2025 roster. Circles are Brown crews (green above the roster average, red below), dark diamonds are skippers who sailed with two or more of them, small grey diamonds are skippers with one, and the grey dots are those skippers' other crews. Line width is how much racing the link rests on.</div></div>
  <p>The fit has a tidy decomposition. A crew's effect is exactly the sum over its skippers of that skipper's push: how much better the boats did with the crew than that skipper's usual crews (the reading minus the skipper's offset), weighted by how much racing it rests on. Here it is for {esc(top['name'])}.</p>
  <div class="fig">{fig_push(d['graph']['detail'][next(k for k, v in d['graph']['detail'].items() if v['name'] == top['name'])], d['match_factor'])}<div class="cap">{esc(top['name'])}'s crew effect, skipper by skipper. Most of it comes from two skippers with a lot of racing together. About {d['graph']['detail'][next(k for k, v in d['graph']['detail'].items() if v['name'] == top['name'])]['prior_share'] * 100:.0f}% of the weight in the fit sits on the prior at zero, which is the shrinkage. The effect is measured against zero, an average crew; the rating in the plot below subtracts the Brown roster average.</div></div>

  <div class="fig">{fig_forest(rows)}<div class="cap">Brown fall 2025 crews, relative rating with a 90% range. Almost every bar covers zero.</div></div>

  <h2>What it means for a race</h2>
  <div class="fig">{fig_h2h(d)}<div class="cap">Chance that the row crew is stronger than the column crew, crews ordered best first. Green means the row crew is likely higher, red the column crew; numbers mark 80% or more, shown once above the diagonal.</div></div>
  <p>With {n} crews and bars this wide, the ranking is a rough guide. Of the {len(pair)} possible pairs of Brown crews, {clear80} can be separated with 80% confidence or more, and {clear90} with 90%. The clearest is the top crew over the bottom crew at {max(pair) * 100:.0f}%. Everything else is close to a coin flip.</p>
  <p>Sizes: skipper ratings spread with a standard deviation of about 1.4. In a 12-boat fleet one rating point is worth about {per_point:.1f} places a race for a mid-fleet boat, so {top['rel']:+.2f} is about {top['rel'] * per_point:.1f} of a place a race, or roughly {top['rel'] * per_point * 14:.0f} points over 14 races, against Brown's average crew. For scale, one standard deviation of skippers, 1.4 rating points, is about {per_point * 1.4:.1f} places a race; the best crew on the roster is worth a fraction of one.</p>

  <h2>Does it predict anything?</h2>
  <p>Yes, a little, and I checked that it is not an artifact. Fit before each of four cutoffs (fall 2024 to spring 2026), then predict every race of the next season ({1413876:,} boat pairs): adding the crew rating to the skipper rating lowers log-loss by {abs(ev['ll']):.5f} (95% interval {abs(ev['ll_ci'][1]):.5f} to {abs(ev['ll_ci'][0]):.5f}), and the improvement holds in every bootstrap resample. When I shuffle crews between boats, the gain vanishes. The calibration also holds: the stated error bars cover the realized differences about as often as they should.</p>
  <p>It is a small gain. A crew's rating is estimated from a few hundred noisy races, and split-half correlations (rate crews on half their races, check against the other half) run only 0.1 to 0.2. That is the right reading of the Brown table: a real, persistent, small effect that mostly cannot be separated from zero for any one crew.</p>

  <h2>Caveats</h2>
  <ul class="plain">
    <li>A crew effect is anything that goes with the crew: tactics calls, boat handling, weight, or chemistry with particular skippers. It does not isolate the sailor.</li>
    <li>Crews rotate. A crew's partners are usually not the skippers they will sail with next, so the rating is a career effect, not a pairing effect.</li>
    <li>On sailor pages the crew rating is shown on the skipper scale, the roster's average skipper rating plus about 19 times the relative effect, so crews can be compared across teams. That display scale makes small effects look large; it is for comparison, not for forecasting.</li>
    <li>A rating with a wide bar is not evidence of a weak crew. It is evidence we do not know.</li>
  </ul>"""
    return dict(slug='what-a-crew-rating-does', title='How does a crew rating work?', date=date(2026, 6, 18), date_label='June 18, 2026', script='analysis/blog/crew_example.py and analysis/crew_ratings/',
                summary=f"A worked example on Brown's fall 2025 roster. A crew rating is small and uncertain: the best crew rates +{top['rel']:.2f} (about {top['rel'] * per_point:.1f} places a race) with a {top['p_best'] * 100:.0f}% chance of being best on the roster.", body=body)


def post_skipper():
    toy = skipper_ratings.toy_race(); path = skipper_ratings.sailor_path(); hd = skipper_ratings.headline()
    d = elo_vs_pl.load(); cal = elo_vs_pl.calibration(d)
    top_pred, top_obs = cal['weekly_pl'][-1][0], cal['weekly_pl'][-1][1]
    champ_ci = d['championships']['bootstrap']['champ']['models']['weekly_pl']['accuracy_ci']
    body = f"""
  <p class="lede">A skipper rating is a number on a log scale: the chance one boat finishes ahead of another is 1 / (1 + e<sup>&minus;gap</sup>). It is fitted to all {hd['data']['races']:,} fleet races in the database at once, by reading each race as a sequence of choices, and then smoothed from week to week so it follows a sailor's form.</p>
  <p>For what a point of rating means in finishing chances, and how ratings map to percentiles, see <a href="/blog/what-a-rating-point-is-worth/">What a rating point is worth</a>. This note is about where the numbers come from.</p>

  <h2>A race is a sequence of choices</h2>
  <p>Give every boat a strength e<sup>rating</sup>. To decide who wins, pick one boat from the fleet with probability proportional to its strength. Remove it, and pick the second-place boat from those left, again in proportion to strength. Continue to the end. The probability of a finishing order is the product of the probabilities of the picks. Here is a five-boat race where the middle-rated boat wins, then the top two follow.</p>
  <div class="fig">{fig_peel(toy)}<div class="cap">One race as four picks. Each bar splits the chance among the boats still in the race; the bright segment is the boat that actually finished there. The order is a real possibility, not a likely one: about {toy['p_order'] * 100:.0f}%.</div></div>
  <p>This is why only gaps matter: multiply every strength by the same number and nothing changes. It is also why strength of schedule takes care of itself. Finishing ahead of strong boats is a less likely event than finishing ahead of weak ones, so it moves a rating more.</p>

  <h2>Fit everyone at once</h2>
  <p>The model's likelihood is the product of that probability over every race in the data. The ratings are the numbers that make the observed results as likely as possible. Each sailor's rating is pushed up when they finished better than their rating and their rivals' ratings predicted, and down when worse, until the pushes balance. Everyone's rating depends on everyone else's, which is how a sailor who only raced in one region ends up on the same scale as one who raced everywhere: the sailors who raced both link them. There is no K-factor to set and no order in which races are processed.</p>

  <h2>Ratings move through time</h2>
  <p>A sailor is not the same skipper in their first fall as in their fourth. Each sailor gets a rating for every regatta week, written as a starting level plus the week-to-week changes. Left alone, those changes would chase every bad race, so the fit pays a price for them: a log-cosh penalty on each change, which is gentle for small moves and only grows linearly for large ones, plus a small penalty on how quickly the changes themselves change. There is an extra cost on drops, so ratings fall more reluctantly than they rise. The weights were retuned against week-ahead forecasts when I fixed the trainer in September.</p>
  <div class="fig">{fig_penalty()}<div class="cap">The price of a one-week change in rating at the trainer's weights. Small moves are cheap, big moves are expensive, and drops cost more than equal gains.</div></div>
  <div class="fig">{fig_path(path)}<div class="cap">One ICSA skipper's rating over a career, a typical one: {path['weeks']} rated weeks, the median among the {path['n_candidates']:,} sailors with at least 20. Each dot is a regatta week; the line joins them.</div></div>

  <h2>Starting a career</h2>
  <p>A sailor's first rating is anchored toward zero with a weak prior, so a newcomer with two races does not get an extreme number. On top of that there is team pooling: each sailor's career-average rating is pulled toward the average of their teammates that season, with a fixed strength, so the pull fades as their own results accumulate. That one change lowered season-ahead log-loss in every held-out season. For forecasts, a sailor with no race history at all starts from the typical debut rating of their school rather than from zero.</p>

  <h2>What is not in a rating</h2>
  <ul class="plain">
    <li>Boats that did not finish are not in the finishing order. They are handled by a separate model, so a DNF does not count as last place.</li>
    <li>Singlehanded and match-race (keelboat) events are rated separately, since the skills are different.</li>
    <li>Crews are not in the skipper rating. Their contribution is estimated separately and is small; see <a href="/blog/what-a-crew-rating-does/">How does a crew rating work?</a></li>
  </ul>

  <h2>How good is it?</h2>
  <p>I score the model only on races it never saw. For each of four seasons I refit using races before the season began, freeze the ratings, and predict every race. Pooled, it orders {hd['season_acc'] * 100:.2f}% of boat pairs correctly (pairwise log-loss {hd['season_ll']:.4f}; a coin flip is 50% and 0.6931). At the {hd['champ_regattas']} championship regattas, refit the week before each one, it gets {hd['champ_acc'] * 100:.1f}% (95% interval {champ_ci[0] * 100:.1f} to {champ_ci[1] * 100:.1f}).</p>
  <p>One honest weakness: it is overconfident at the top. When it says a favourite has a {top_pred * 100:.0f}% chance of finishing ahead in a race a season later, it happens {top_obs * 100:.0f}% of the time. That is the price of frozen ratings and a model that cannot know who will improve over the summer.</p>

  <h2>Caveats</h2>
  <ul class="plain">
    <li>A rating is relative to the sailors in the data. It says nothing about conditions, boats or tactics beyond what shows up in results.</li>
    <li>Ratings are estimates with error, largest for sailors with few races.</li>
    <li>Why this model rather than Elo: see <a href="/blog/why-not-elo/">Why not Elo?</a></li>
  </ul>"""
    return dict(slug='how-skipper-ratings-work', title='How do skipper ratings work?', date=date(2026, 3, 26), date_label='March 26, 2026', script='analysis/blog/skipper_ratings.py and analysis/plackett_luce/',
                summary=f"A rating is a log-strength fitted to every race at once, smoothed week to week. Frozen at the start of a season, it orders {hd['season_acc'] * 100:.1f}% of boat pairs correctly.", body=body)


def post_elo():
    d = elo_vs_pl.load(); lad = elo_vs_pl.ladder(d); by = {r['model']: r for r in lad}
    curves = elo_vs_pl.elo_k_curves(d); cal = elo_vs_pl.calibration(d); race = elo_vs_pl.one_race()
    el, sp, wk = by['elo'], by['static_pl'], by['weekly_pl']
    rp = network_effects.ripple(); pts, isl = network_effects.islands(); ret = network_effects.retired()
    spread = max(max(c['curve'].values()) - min(c['curve'].values()) for c in curves)
    margin = d['championships']['bootstrap']['champ']['models']['elo']['site_margin_ci']
    top_p, top_o = cal['weekly_pl'][-1][0], cal['weekly_pl'][-1][1]; etop_p, etop_o = cal['elo'][-1][0], cal['elo'][-1][1]
    r1 = ret['rows'][0]; r4 = ret['rows'][-1]
    lm = []
    for lg, name in (('ICSA', 'college'), ('ISSA', 'high school')):
        sb = d['forecast']['bootstrap'][lg]['models']['elo']['site_margin_ci']; cb = d['championships']['bootstrap']['champ_' + lg]['models']['elo']['site_margin_ci']
        lm.append(dict(label=f'{name}, season ahead', elo=d['forecast']['pooled']['elo'][lg]['accuracy'], site=d['forecast']['pooled']['weekly_pl'][lg]['accuracy'], lo=sb[0], hi=sb[1]))
        lm.append(dict(label=f'{name}, championships', elo=d['championships']['metrics']['elo'][lg]['accuracy'], site=d['championships']['metrics']['weekly_pl'][lg]['accuracy'], lo=cb[0], hi=cb[1]))
    types = d['championships']['regattas_by_type']
    n_icsa = sum(t['regattas'] for t in types if t['league'] == 'ICSA'); n_issa = sum(t['regattas'] for t in types if t['league'] == 'ISSA')
    nat = sum(t['regattas'] for t in types if 'National' in t['type'])
    ret_bars = [(r['label'], [('share revised by more than 0.1', r['share_big'], C_A)]) for r in ret['rows']]
    body = f"""
  <p class="lede">Elo tracks form; so does the site model. On the same forward test, the weekly Plackett&ndash;Luce model beats a tuned Elo by {el['season_ll'] - wk['season_ll']:.4f} of pairwise log-loss a season ahead and {el['champ_ll'] - wk['champ_ll']:.4f} at championships. <strong>The difference is not the logistic curve, which they share. It is that a joint fit lets every result inform every rating it is connected to, in both directions, where Elo lets a race touch only the boats in it, once, and never again.</strong></p>

  <h2>They are close relatives</h2>
  <p>Elo's update after a race, for a boat that beat <em>S</em> of its rivals when its rating said to expect <em>E</em>, is a step of size K/(m&minus;1) &times; (S &minus; E). That is one stochastic-gradient step on the pairwise logistic log-likelihood, taken once, in race order. Plackett&ndash;Luce fits a listwise likelihood, the whole finishing order, to convergence over all the data at once. Same logistic form for a pair of boats, a different way of getting to the numbers.</p>
  <div class="fig">{fig_one_race(race)}<div class="cap">One six-boat race where the fourth-rated boat wins, seen by each method (ratings in logits, shown in brackets). Both push the winner up and the last boat down. They weigh the middle differently: Elo charges a boat for every pairwise loss; Plackett&ndash;Luce charges it only its share of each pick it was passed over in, which is small for a weak boat, and credits it fully for edging the one boat left behind.</div></div>

  <h2>Elo moves information one way</h2>
  <p>In Elo a race changes the ratings of the boats in it and nothing else. Once applied, the update is final. If a boat you beat last month turns out to be much stronger than its rating said, your rating does not get the credit; the information arrived too late and has nowhere to go. In a joint fit the same race is a constraint that holds at the solution together with every other race. When new results move your rival's rating, every race you sailed against them re-weighs, and your rating moves with it, and so on outward through the sailors you have shared a start line with.</p>
  <p>Here is that in a constructed network: two regions of twenty sailors who race only among themselves, linked by six travellers from each who met once. Then one more race is added among four of the travellers.</p>
  <div class="fig">{fig_ripple(rp)}<div class="cap">After one new race, Elo changes {rp['moved_elo']} ratings. The joint fit changes {rp['moved_pl']}: the four boats most, then everyone who has raced them, in proportion to how much racing they share.</div></div>

  <h2>It shows up in real ratings</h2>
  <p>The clearest test is sailors whose own results can no longer change. Take everyone in the fit made before spring 2026 who has not raced since, and compare their rating at their last race in that fit with the same rating in today's fit, which has seen a further season of other people's results. Their own races are identical in both. Any revision is information that flowed back to them through rivals.</p>
  <div class="fig">{fig_bars(ret_bars, 'share of retired sailors', 0.5, fmt='{:.0%}')}<div class="cap">{ret['n']:,} sailors with no race since the {ret['cut']} cutoff, by time since their last race. Share whose rating at that last race was revised by more than 0.1 once a further season of other sailors' results was fitted.</div></div>
  <p>Among sailors whose last race was within a year of the cutoff, {r1['share_big'] * 100:.0f}% were revised by more than a tenth of a rating point, with a typical revision (standard deviation) of {r1['sd']:.2f}. Four or more years out it falls to {r4['share_big'] * 100:.0f}% and {r4['sd']:.2f}, because their rivals have also stopped racing. Under Elo every one of those revisions is zero.</p>

  <h2>Why it matters for sailing</h2>
  <p>College sailing is regional. Most regattas are within a conference, and the regions meet a few times a year at interconference events and the championships. To compare a New England sailor with one from the Pacific coast you need a path between them: the travellers who raced both. Simulated below, with the bridging regatta coming at the end of the season, as a national championship does.</p>
  <div class="fig">{fig_bars([('gap between the regions', [('truth', isl['true_gap'], C_NEITHER), ('Elo', isl['elo_gap'], C_BOTH), ('joint fit', isl['pl_gap'], C_A)]), ('error, sailors who never travelled', [('Elo', isl['elo_rmse_nontrav'], C_BOTH), ('joint fit', isl['pl_rmse_nontrav'], C_A)])], 'rating points', 1.2)}<div class="cap">Two regions whose true average ratings differ by one point, linked by one late regatta of travellers. Mean over {isl['reps']} simulations. Elo recovers {isl['elo_gap']:.2f} of the gap; the joint fit {isl['pl_gap']:.2f}. For sailors who never left their region, the joint fit's error is {isl['pl_rmse_nontrav']:.2f} against Elo's {isl['elo_rmse_nontrav']:.2f}.</div></div>
  <p>Elo sees the bridge regatta as a few updates to the travellers. The regions' other sailors never raced an outsider, so their ratings stay where their home races put them, on two scales that happen to share a number. The joint fit pushes the whole region through the travellers, because every home race connects them.</p>

  <h2>The test</h2>
  <p>For four seasons (fall 2024 to spring 2026) every model is fit on races before the season, then predicts every race in it with ratings frozen. Elo's K and probability scale are tuned on the previous season, never the one being scored. A separate test refits the week before each of {d['championships']['regattas']} championship regattas. Lower log-loss is better.</p>
  <div class="fig">{fig_ladder(lad)}<div class="cap">Out-of-sample pairwise log-loss. The site model beats Elo in every bootstrap resample of championship regattas, by {margin[0] * 100:.1f} to {margin[1] * 100:.1f} points of accuracy.</div></div>
  <p>One row deserves a word. A Plackett&ndash;Luce fit with a single rating per sailor and no notion of time ties Elo ({sp['season_ll']:.4f} against {el['season_ll']:.4f}). That is not the comparison to make, since Elo's K lets it follow form and the static model cannot; it is in the table because it separates the two ingredients. Propagation alone roughly pays for the loss of form-tracking. Add a weekly path with smoothing, and team pooling, and the joint fit pulls ahead.</p>

  <h2>By league, and at the championships</h2>
  <p>The championship test is the one that matters for the site, since its win chances are made the week before. It covers {d['championships']['regattas']} regattas: {n_icsa} college (conference championships, showcases, and the {nat - sum(t['regattas'] for t in types if t['league'] == 'ISSA' and 'National' in t['type'])} national semifinal and final rounds) and {n_issa} high school (district qualifiers, district championships and the nationals). Elo and the site model, by league:</p>
  <div class="fig">{fig_league_margin(lm)}<div class="cap">Pairwise accuracy of Elo and the site model, by league, season ahead and at championships. The site model wins in every bootstrap resample of every row.</div></div>
  <p>The margin is larger in high school than in college on both tests: {lm[3]['lo'] * 100:.1f} to {lm[3]['hi'] * 100:.1f} points at high-school championships against {lm[1]['lo'] * 100:.1f} to {lm[1]['hi'] * 100:.1f} at college ones.</p>

  <h2>Caveats</h2>
  <ul class="plain">
    <li>This is the standard multi-boat Elo with a tuned K; Glicko and TrueSkill were not tested. The two-region and one-race examples are simulations; the retired-sailor check and the championship test are real data.</li>
  </ul>"""
    return dict(slug='why-not-elo', title='Why not Elo?', date=date(2026, 4, 9), date_label='April 9, 2026', script='analysis/blog/elo_vs_pl.py, network_effects.py and analysis/plackett_luce/evaluate_models.py',
                summary=f"Both track form. The difference is propagation: in a joint fit every result informs every connected rating, both ways; in Elo a race touches only its boats, once.", body=body)



def post_venues():
    V = json.loads((ROOT / 'analysis' / 'blog' / 'venue_post.json').read_text())
    rt = (ROOT / 'analysis' / 'blog' / 'race_temperature.log').read_text()
    het = float(re.search(r'mean s\^2/I = ([\d.]+)', rt).group(1)); ev_sd = float(re.search(r'implied true sd ([\d.]+)\n', rt.split('events with 8+')[1]).group(1)); oe = float(re.search(r'odd vs even races within an event: r = ([+-][\d.]+)', rt).group(1))
    fe = V['field_effects']; cov = V['coverage']; val = V['validate']; cl = V['clusters']; cb = V['clusters_badj']; ps = V['pooled_sectors']; vm = V['venue_means']
    SEC = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
    sector_bars = [(k, [('residual orderliness, venue-demeaned', ps[k][0], C_A if ps[k][0] >= 0 else '#b5473a')]) for k in SEC]
    resid_bars = [(lab, [('residual orderliness', v, C_A if v >= 0 else '#b5473a')]) for lab, v, n in V['resid_bins']]
    BANDS = ['<6kn', '6-10', '10-14', '14+']; names = {1: 'breeze brings order', 2: 'breeze brings chaos', 3: 'wind barely matters', 4: 'light air orderly, W-NW messy', 5: 'light air orderly, southerly clean'}
    # map clusters to narrative names by their speed slope and S/NW signature
    def label(c):
        if c['speed'] > 0.12: base = 'breeze helps'
        elif c['speed'] < -0.12: base = 'breeze hurts'
        elif c['dirs'].get('S', 0) >= 0.05: base = 'light air orderly, S clean'
        elif abs(c['speed']) < 0.05 and abs(c['dirs'].get('S', 0)) < 0.05: base = 'wind barely matters'
        else: base = 'in between'
        return base if c['n'] > 1 else f"{base} ({c['members'][0].split(' (')[0]})"
    cl_rows = ''.join(f"<tr><td><strong>{esc(label(c))}</strong></td><td>{c['n']}</td><td>{c['speed']:+.2f}</td><td>" + ', '.join(f"{b}: {v:+.2f}" for b, v in c['bands']) + f"</td><td>{esc(', '.join(c['members']))}</td></tr>" for c in sorted(cl['clusters'], key=lambda c: -c['speed']))
    vt_rows = ''.join(f"<tr><td>{esc(t['host'])}</td><td>{t['n']}</td><td>{t['p']:.3f}</td><td>{t['best']} {t['best_v']:+.2f}</td><td>{t['worst']} {t['worst_v']:+.2f}</td></tr>" for t in V['venue_tests'][:8])
    band_series = []
    for c in sorted(cl['clusters'], key=lambda c: -c['speed']):
        bd = dict(c['bands']); band_series.append((label(c), [bd.get(b, 0.0) for b in BANDS]))
    band_bars = [(b, [(lab_, vals[i], col) for (lab_, vals), col in zip(band_series, [C_A, C_BOTH, '#c58a1c', '#8a4f8a', '#b5473a'])]) for i, b in enumerate(BANDS)]
    fe_names = {'nb': 'fleet size', 'teams': 'teams', 'mean_r': 'mean rating', 'sd_r': 'spread', 'top_r': 'top boat', 'unr': 'newcomers', 'exp': 'experience', 'champ': 'champ.'}
    fe_bars = [(fe_names[k], [('effect on orderliness per sd', fe[k], C_A if fe[k] >= 0 else '#b5473a')]) for k in ('mean_r', 'top_r', 'unr', 'exp', 'nb', 'sd_r', 'teams', 'champ')]
    body = f"""
  <p class="lede">Some venues get more orderly when it blows, a few get less, most barely care &mdash; and at a handful of venues the favourites are reliably over- or under-rated. Fleet first, then wind, then venue; small effects, carefully isolated.</p>

  <h2>Surprisal and temperature</h2>
  <p>A boat's surprisal is the derivative of the finishing order's log-likelihood with respect to its rating (score g over information h): how much better it did than its rating predicted. An event's <strong>temperature</strong> &beta; is the same thing for the whole fleet &mdash; refit the ratings' scale to the event; &beta; &lt; 1 is a surprising event, &gt; 1 an orderly one. Temperatures vary more than noise allows (mean s&sup2;/I = {het:.2f}; true spread {ev_sd:.2f}) and persist within a day (odd vs even races r = {oe:+.2f}).</p>

  <h2>The wind labels</h2>
  <p>Scorers' daily summaries, scraped and parsed by a language model: {cov['regattas']:,} regattas, {cov['speed']:,} regatta-days with a stated wind speed and {cov['direction']:,} with a direction. Reanalysis wind fills the gaps after calibration (r = {val['corr']:.2f}; stated &asymp; {val['slope']:.2f} &times; reanalysis + {val['icpt']:.1f}).</p>

  <h2>The fleet first</h2>
  <div class="fig">{fig_bars_signed(fe_bars, 'effect per sd', -0.12, 0.04, fmt='{:+.2f}', height=230, legend=False)}<div class="cap">What a day's entry list says about its orderliness, before any wind (CV R&sup2; = {V['field_r2']:.2f}). Stronger fleets are more surprising; a standout top boat makes an event orderly; newcomers make it unpredictable.</div></div>
  <p>Everything below is the <strong>residual</strong>: temperature minus what the fleet predicted. Once the fleet is out, a venue-only model has no held-out skill (R&sup2; = {V['wind_cv']['venue only']:+.3f}), and removing skippers' individual breeze preferences changes the residuals by nothing (r = {V['badj_corr']:.3f}; no out-of-sample gain, log-loss {V['oos']['ll']:+.5f}).</p>

  <h2>Then the wind</h2>
  <div class="fig">{fig_bars_signed(resid_bars, 'residual orderliness', -0.04, 0.06, fmt='{:+.2f}', height=220, legend=False)}<div class="cap">By stated wind speed, {V['days']:,} labelled days: breeze helps, about {V['resid_slope'] * 10:+.2f} per 10 knots.</div></div>
  <div class="fig">{fig_bars_signed(sector_bars, 'residual orderliness', -0.04, 0.04, fmt='{:+.3f}', height=220, legend=False)}<div class="cap">By direction, venue-demeaned (se &asymp; &plusmn;0.007): onshore sea-breeze directions orderly, the post-frontal north-westerly surprising.</div></div>
  <table class="t cases"><thead><tr><th>Venue</th><th>Days</th><th>p</th><th>Most orderly sector</th><th>Least</th></tr></thead><tbody>{vt_rows}</tbody></table>
  <p>Direction effects per venue, permutation-tested: {V['n_sig']} of {len(V['venue_tests'])} venues beyond p &lt; 0.05 where one would be by chance. Navy (114 days) is the robust case.</p>

  <h2>Favourites' venues and underdogs' venues</h2>
  <div class="fig">{fig_venue_means(vm)}<div class="cap">Mean residual orderliness per venue (30+ labelled days). Yale and Wisconsin reward the favourites beyond what their fleets explain; at Connecticut College and Cornell the fleet compresses. Six of 30 venues sit beyond two standard errors (1.5 expected).</div></div>

  <h2>Clustering the venues</h2>
  <div class="fig"><img src="venue_clusters_field.png" alt="Venues on the first two principal components of their wind-response profiles, coloured by cluster" style="width:100%"><div class="cap">Each venue's profile (overall residual, eight direction sectors, four speed bands, shrunk) on its first two principal components; Ward clusters. PC1 ({cl['pc1']}%) is the speed response, left "breeze brings order" to right "breeze brings chaos"; direction is PC2 ({cl['pc2']}%).</div></div>
  <div class="fig">{fig_bars_signed(band_bars, 'residual orderliness', -0.24, 0.08, fmt='{:+.2f}', height=300)}<div class="cap">Cluster profiles by wind-speed band.</div></div>
  <table class="t cases"><thead><tr><th>Group</th><th>Venues</th><th>Slope / 10 kn</th><th>By speed band</th><th>Members</th></tr></thead><tbody>{cl_rows}</tbody></table>
  <p>The two ends are dependable; the middle reshuffles when inputs change and should not be read as assigned.</p>
  <p class="eq"><a href="/blog/venues-and-wind/maps/">Is it a good day to be an underdog? Explore every venue's map, two at a time &rarr;</a></p>

  <h2>Navy, Brown, Yale, Tufts</h2>
  <div class="fig"><img src="resid_navy_pair.png" alt="Navy: residual orderliness by wind direction and speed" style="width:100%"><div class="cap">Navy, 114 days, fleet removed: a southerly up the Chesapeake is the favourites' breeze, a north-westerly off the Academy the underdogs'. Left: nearby-day average; right: fitted surface.</div></div>
  <div class="fig"><img src="resid_brown_pair.png" alt="Brown: residual orderliness by wind direction and speed" style="width:100%"><div class="cap">Brown, 78 days: the breezy westerly corner is the messy one.</div></div>
  <div class="fig"><img src="resid_yale_tufts_pair.png" alt="Yale and Tufts residual surfaces" style="width:100%"><div class="cap">Yale ({vm['Yale']['mean']:+.3f}, {vm['Yale']['n']} days, {vm['Yale']['above'] * 100:.0f}% above zero) and Tufts ({vm['Tufts']['mean']:+.3f}, {vm['Tufts']['n']} days), 150 km apart, same conferences, opposite ends of the scale. <a href="/blog/venues-and-wind/maps/#Yale|Tufts">Compare them interactively &rarr;</a></div></div>

  <h2>Validation, in one list</h2>
  <ul class="plain">
    <li>Every model cross-validated with whole events held out; venue direction tested by within-venue permutation; the clustering rerun on breeze-adjusted residuals (ends survive, middle reshuffles); stated winds checked against reanalysis ({val['corr']:.2f}).</li>
    <li>Caveats: venue cells hold 3&ndash;20 days; race times are approximate; the wind effects are a few hundredths of temperature against a daily spread of {ev_sd:.2f}, and none improve race forecasts.</li>
  </ul>"""
    A = ROOT / 'analysis' / 'blog'
    return dict(slug='venues-and-wind', title='Which venues get orderly in a breeze?', date=date(2026, 7, 23), date_label='July 23, 2026', script='analysis/blog/venue_post_data.py, orderliness_wind.py, venue_clusters.py and the wind pipeline',
                summary=f"Sort {cl['n']} venues by how results respond to wind after the fleet is taken out: river venues get orderly in a breeze, a few open-water ones get messier, the Chesapeake likes a southerly. Small effects, carefully isolated.",
                body=body, assets=[str(A / 'venue_clusters_field.png'), str(A / 'resid_navy_pair.png'), str(A / 'resid_brown_pair.png'), str(A / 'resid_yale_tufts_pair.png')])


def post_boats():
    B = json.loads((ROOT / 'analysis' / 'blog' / 'boat_post.json').read_text())
    w = B['within']; s = B['season']; cl = {c['cls']: c for c in B['classes']}; zf = B['zfleets']
    RED = '#b5473a'
    null_bars = [('rotation-preserving null', [('boat-effect variance', w['null'], SUB)]), ('observed', [('boat-effect variance', w['obs'], C_A)])]
    sig_bars = [('p < 0.05', [('chance', 0.05, SUB), ('observed', w['sig05'], C_A)]), ('p < 0.01', [('chance', 0.01, SUB), ('observed', w['sig01'], C_A)])]
    hand_names = {'prev skipper 1+ below fleet': 'previous skipper 1+ below the fleet', 'within 1': 'within 1 point', '1+ above': '1+ above'}
    hand_bars = [(hand_names[k], [('next team, in that boat', v, C_A if v >= 0 else RED)]) for k, v, se in w['hand_bins']]
    carry = dict(B['carry'])
    carry_bars = [('weekend', [('r', s['weekend'], C_A)]), ('season', [('r', s['same'], C_A)])] + [(f"{g / 2:g} yr" + ('+' if g == 6 else ''), [('r', carry[g], C_A if carry[g] > 0.015 else SUB)]) for g in (1, 2, 3, 4, 5, 6)] + [('other sail', [('r', s['null'], SUB)])]
    cls_rows = ''.join(f"<tr><td><strong>{esc(c['cls'])}</strong></td><td>{c['fleets']}</td><td>{c['q']:.4f} &plusmn; {c['q_se']:.4f}</td><td>{c['half_life']:.1f} yrs</td><td>{c['tau']:.3f} &plusmn; {c['tau_se']:.3f}</td><td>{c['tau'] / 0.83 * 1.9:.2f}</td></tr>" for c in B['classes'] if c['fleets'] >= 7)
    age_bars = [(lab.replace('age ', 'age '), [(cls_, v, col) for cls_, col in (('FJ', C_A), ('420', RED), ('Z420', '#c58a1c')) for l2, v in B['age'].get(cls_, []) if l2 == lab]) for lab in ('age 1-2', 'age 3-4', 'age 5-8', 'age 9+')]
    age_bars = [(lab, bars) for lab, bars in age_bars if bars]
    z_rows = ''.join(f"<tr><td>{esc(k)}</td><td>{v['n']}</td><td>{v['sd']:.2f}</td><td>{v['over2']}</td><td>{'&lt; 0.001' if v['p'] < 0.001 else f'{v[chr(112)]:.2f}'}</td></tr>" for k, v in zf.items())
    cal_rows = ''.join(f"<tr><td>{esc(a)}</td><td>{esc(b)}</td><td>{esc(c)}</td></tr>" for a, b, c in B['calibration'])
    ot = B['over_time']; NEXT_LBL = "next team's surprisal"; I = B['inherit']
    G = B['signal']; H = json.loads((ROOT / 'analysis' / 'blog' / 'boat_pl_heldout.json').read_text())
    q_bars = [(f"{'worst' if k == 0 else 'best' if k == 4 else str(k + 1) + ('nd' if k == 1 else 'rd' if k == 2 else 'th')} fifth", [('realised, held-out', v, C_A if v >= 0 else RED)]) for k, (pr, v, se_, n_) in enumerate(H['quintiles'])]
    sc_bars = [(f"&times;{sc:g}", [('held-out gain', v, C_A if v >= 0 else RED)]) for sc, v, se_ in H['scales']]
    sc40 = next(t for t in H['scales'] if abs(t[0] - 0.4) < 1e-9)
    need_bars = [(f"{pl:.2f} pl/race", [('regattas to call it at 2σ', n_, C_A)]) for pl, n_ in G['need']]
    rel_bars = [(f"{n_} reg.", [('correlation with the truth', r_, C_A)]) for n_, rel_, r_ in G['rel']]
    prior_rows = ''.join(f"<tr><td><strong>{esc(r['fleet'])}</strong></td><td>{esc(r['era'])}</td><td>{esc(r['reset'])}</td><td>{r['tau_pl']:.2f}</td><td>{esc(r['best'][0])} ({r['best'][1]:+.2f})</td><td>{esc(r['worst'][0])} ({r['worst'][1]:+.2f})</td><td>{r['spread']:.2f} / {r['pts']}</td></tr>" for r in B['prior'])
    inh_bars = [(f"{lo:+.1f}", [('next team, demeaned', v, C_A if v >= 0 else RED)]) for lo, hi, n_, v, se_ in I['bins'] if n_ >= 3000]
    wb = I['well']; well_bars = [(lab, [('previous team sailed it badly', wb[f'{k}|badly'][0], RED), ('sailed it well', wb[f'{k}|well'][0], C_A)]) for k, lab in (('prev occupant 1+ below fleet', 'previous occupant 1+ below fleet'), ('within 1', 'within 1 point'), ('1+ above', '1+ above'))]
    body = f"""
  <p class="lede">Yes, some boats in a fleet are slow &mdash; but less than sailors think, a good sailor doesn't leave a boat faster for the next crew, and a fleet's unevenness is a property it arrives with, not one it wears into. Here is the chain of tests, in order, with the trap at each step.</p>

  <h2>1. The data, and the trap in it</h2>
  <p>Techscore's rotation sheets say which <em>sail number</em> each team had in each race: 964,000 team-races across 3,800 regattas, matched to 97% of our scored ICSA boat-races. Each team's result is converted to a <strong>surprisal</strong> &mdash; how much better or worse it finished than its pre-regatta rating predicted, in standard-error units &mdash; and demeaned within the team's regatta, so the team's form that weekend is gone. What remains is race-to-race movement; a boat's effect is its average over everyone who sailed it.</p>
  <p>The trap: teams keep a boat for two races, then it passes to another team. A naive test treats those two races as independent evidence about the boat, when they are mostly evidence about the team in it. So the null hypothesis has to keep that structure: within each rotation set the sail numbers are relabelled at random, consistently for every team, and the boat-effect variance is recomputed. Only variance beyond that is boat.</p>
  <div class="fig">{fig_bars(null_bars, 'variance of boat effects (z²)', 0.16, fmt='{:.3f}', height=220)}<div class="cap">{w['n_ev']:,} regatta-divisions with a full rotation. The excess, {w['obs'] - w['null']:.3f} z&sup2;, is a boat standard deviation of {w['boat_sd']:.2f} z &mdash; about <strong>{w['places']:.1f} places a race</strong> in a 12-boat fleet for a one-sd boat.</div></div>
  <div class="fig">{fig_bars(sig_bars, 'share of regatta-divisions', 0.16, fmt='{:.0%}', height=220)}<div class="cap">Regattas with a significant boat effect against the rotation-preserving null, versus what chance would give. Roughly one fleet in fifteen has a boat worth complaining about on the day.</div></div>

  <h2>2. Two artefacts to clear before believing anything</h2>
  <p><strong>Same team, same boat again.</strong> The raw correlation between a team's two stints in the same boat is &minus;0.16 &mdash; which looks like "a boat that went well goes badly next time". It is arithmetic: demeaning a team's k stints forces them to sum to zero, so any two correlate about &minus;1/(k&minus;1). The same team's stints in <em>different</em> boats correlate &minus;0.18. Same boat is 0.02 less negative: the small boat signal again, nothing more.</p>
  <p><strong>Does a good sailor tune the boat for the next crew?</strong> {w['hand_n']:,} handovers where a different team takes the boat in the next set:</p>
  <div class="fig">{fig_bars_signed(hand_bars, NEXT_LBL, -0.02, 0.02, fmt='{:+.3f}', height=220, legend=False)}<div class="cap">By the previous skipper's rating relative to the fleet (&plusmn;0.005). Slope {w['hand_slope']:+.4f} per rating point: nothing. The next team's result does correlate with the previous team's result in that boat ({w['hand_corr']:+.3f}) &mdash; that is the boat's own fixed quality, and it does not grow when the previous skipper was strong.</div></div>

  <h2>Does inheriting a strong team's boat help?</h2>
  <p>The same question from the other side: for {I['n']} handovers starting at race 3 or later, the next team's surprisal against the previous occupant's rating relative to the fleet.</p>
  <div class="fig">{fig_bars_signed(inh_bars, NEXT_LBL, -0.02, 0.02, fmt='{:+.3f}', height=230, legend=False)}<div class="cap">Previous occupant's rating minus the fleet mean (bins of half a point, lower edge; &plusmn;0.004&ndash;0.013). Slope {I['slope']:+.4f} &plusmn; {I['slope_se']:.4f} per rating point. A team's <em>own</em> rating moves its result by {I['own']:+.3f} per point &mdash; the inheritance effect is at most a sixtieth of that, and not distinguishable from zero.</div></div>
  <div class="fig">{fig_bars_signed(well_bars, NEXT_LBL, -0.08, 0.08, fmt='{:+.3f}', height=250)}<div class="cap">What does transfer is the boat: when the previous team went well in it the next team gains about +0.04, badly &minus;0.04 &mdash; and by the same amount whether that team was strong or weak. A fast boat is handed on; a good crew's tuning is not.</div></div>
  <p>One sub-slice, the second rotation set, reads +0.009 &plusmn; 0.004 per point; it is one of five set-level slopes (set 4 is &minus;0.007 the other way), splits into +0.017 and +0.001 on random halves of the regattas, and would be worth four hundredths of a place a race even if real. Not news.</p>
  <p><strong>Is there a team whose boat you want next?</strong> Per previous-occupant school ({I['schools']} with 300+ handovers): the spread of school effects is <em>narrower</em> than chance (sd of z {I['sch_sd']:.2f} against 1.00), none beyond two sigma ({I['sch_exp']:.0f} expected), and the school means do not replicate between halves of the data (r = {I['split']:+.2f}). Per skipper ({I['skippers']} with 100+): sd {I['sk_sd']:.2f}, {I['sk_over2']} beyond two sigma against {I['sk_exp']:.0f} expected. Nobody rigs a boat for the next crew, and nobody wrecks one.</p>

  <h2>3. Does a boat carry its effect to the next regatta?</h2>
  <p>A boat here is a sail number at one host, in one class, in one season (A and B divisions share a fleet unless the class is listed as "420/FJ"). Correlate a sail's effect in one regatta with the same sail at the same host later:</p>
  <div class="fig">{fig_bars_signed(carry_bars, 'correlation', -0.02, 0.10, fmt='{:+.2f}', height=240, legend=False)}<div class="cap">Same boat on the same weekend (A and B swapping fleets) {s['weekend']:+.2f}; same season {s['same']:+.2f}; about {carry[2]:+.2f} for up to two and a half years; <strong>{carry[6]:+.3f} beyond three</strong> &mdash; the lifetime of a fleet as the data sees it. A random other sail at the same host: {s['null']:+.2f}.</div></div>
  <p>So pooling helps, out to a horizon. Out of sample, predicting a regatta's boat effects from all earlier regattas at the host is best with a decay of 0.75&ndash;0.85 per half-season on older evidence (r &asymp; 0.065); never decaying is worse than same-season-only, because three-year-old evidence describes a different boat.</p>

  <h2>4. Fleets, season by season</h2>
  <div class="fig"><img src="boat_heatmap_cornell_420.png" alt="Cornell 420 fleet: boat effect by sail and season" style="width:100%"><div class="cap">Cornell's 420s. Rows are sails, columns seasons, green fast, the number is regattas with a rotation. Sail 4 is dark green for five straight seasons from 2014 and 15, 16, 10 are a red band across the same era; before and after it the same numbers are unremarkable. Best-to-worst in that era was 1.4 places a race &mdash; 20 points over a 16-race regatta.</div></div>
  <div class="fig"><img src="boat_changepoints_tufts_lark.png" alt="Tufts Lark fleet: boat effect by sail and season with the detected fleet change" style="width:100%"><div class="cap">Tufts' Larks with the detected fleet change. Sail 1 is the slow boat for six seasons, then a good one; sail 3 goes from best to worst. Correlation of boat effects across the line: &minus;0.46.</div></div>

  <h2>5. Finding the fleet changes</h2>
  <p>Within an era each sail has a constant effect; a boundary is placed where splitting the seasons reduces the weighted squared error by more than a penalty, and the penalty is calibrated per fleet by shuffling the season order until shuffled orders produce no boundary 95% of the time. Where that is too strict, the weakest link in the chain of pooled season-to-season correlations does the same job with less certainty. Scorers' daily summaries ("brand new Zim FJs", "first regatta in the new Larks") give the answer key:</p>
  <table class="t cases"><thead><tr><th>Fleet</th><th>Scorer / owner says</th><th>The data said</th></tr></thead><tbody>{cal_rows}</tbody></table>
  <div class="fig"><img src="boat_brown_eras.png" alt="Brown FJ fleet: three eras, and old fleet vs new fleet" style="width:100%"><div class="cap">Brown's FJs: three fleets. The programme first dated the second change to 2021; the chain of correlations ran unbroken through 2021 (+0.44) and broke between fall 2023 and spring 2024 (&minus;0.03) &mdash; which turned out to be when the fleet was replaced. Old fast boats (6, 17, 14) are not the new fast boats (18, 16, 8).</div></div>

  <h2>6. Uneven fleets and even ones</h2>
  <p>Within an era, each sail's pooled effect divided by its standard error is a z-score. An even fleet gives a standard normal; an uneven one gives a wider distribution.</p>
  <div class="fig"><img src="boat_z_fleets.png" alt="z-score distributions of boats in uneven and even fleets" style="width:100%"><div class="cap">Three uneven fleets and three even ones, against N(0,1).</div></div>
  <table class="t cases"><thead><tr><th>Fleet, era</th><th>Sails</th><th>sd of z (1 = even)</th><th>Boats beyond 2&sigma;</th><th>Fleet &chi;&sup2; p</th></tr></thead><tbody>{z_rows}</tbody></table>
  <p>Six to eight boats per uneven fleet are individually beyond two sigma; in the even fleets one is, which is what 18 draws give by chance.</p>

  <h2>7. Wear: by class, by age</h2>
  <p>Model each sail's true effect as a random walk with variance q per half-season, estimated from how the squared difference between two seasons' effects grows with the gap between them, within eras:</p>
  <table class="t cases"><thead><tr><th>Class</th><th>Fleets</th><th>Drift q (z&sup2; / half-season)</th><th>Half-life of the fast/slow ranking</th><th>Unevenness &tau; (z)</th><th>&tau; in places / race</th></tr></thead><tbody>{cls_rows}</tbody></table>
  <p><strong>FJ fleets reshuffle faster than 420 fleets</strong> &mdash; but hold the ratio loosely. Both drifts are small next to the measurement noise (season-to-season changes, scaled by their noise, have variance 1.10 for FJs and 1.03 for 420s against 1.00 for no change at all), and a half-life is &tau;&sup2; divided by that small number, so "1.3 years vs 13" is really "a year or two vs several". The changes show no jump signature either (excess kurtosis &asymp; 0, large moves at the Gaussian rate, drops and gains equally common): single-boat events of the size that matters, a tenth of a place a race, are below what four regattas a season can resolve, so a physically sensible damage-and-repair model (boats only degrade; repairs and others' damage are what make a boat faster <em>relative</em> to the fleet) is indistinguishable here from a small symmetric walk. How <em>uneven</em> a fleet is does not depend on class &mdash; and it does not depend on age either:</p>
  <div class="fig">{fig_bars(age_bars, 'unevenness τ (z)', 0.2, fmt='{:.2f}', height=240)}<div class="cap">Unevenness by age since a scorer-dated purchase. Flat. Fleets arrive as uneven as they will ever be; a reversion test (boats that stick out drift back, slope {B['reversion']:+.3f} &plusmn; {B['reversion_se']:.3f}) says maintenance keeps it that way. "New fleet = even" is false: Cornell's and Tufts' were uneven from the first season.</div></div>
  <div class="fig"><img src="boat_variability_time.png" alt="fleet unevenness and share of significant fleets by class over time" style="width:100%"><div class="cap">By class over time, two-year windows. Left: unevenness across fleets, {ot['FJ'][0][1]:.2f}&ndash;{max(v[1] for v in ot['FJ']):.2f} z for FJs, {min(v[1] for v in ot['420']):.2f}&ndash;{max(v[1] for v in ot['420']):.2f} for 420s, no trend. Right: the share of fleets whose boats differ significantly, 15&ndash;40% against 5% by chance.</div></div>

  <h2>8. The prior, applied</h2>
  <p>Put together: the fleet's current era (a scorer-dated purchase, else a multi-year gap in hosting, else all its data), its own unevenness &tau; as the prior spread, the class drift q as the random walk, and a Kalman pass over the era's seasons. Each sail gets an expected finish change for next season with an uncertainty.</p>
  <div class="fig"><img src="boat_fleet_prior.png" alt="expected finish change next season by sail for eight example fleets" style="width:100%"><div class="cap">Eight fleets, sails sorted by expected effect in places per race (&plusmn; one standard error). The 420 fleets at Cornell and Kings Point are the ones where the draw matters; Yale's and Navy's 420s and Brown's new FJs barely do.</div></div>
  <table class="t cases"><thead><tr><th>Fleet</th><th>Era</th><th>Era from</th><th>&tau; (places/race)</th><th>Best sail</th><th>Worst sail</th><th>Spread, places/race / pts per 16</th></tr></thead><tbody>{prior_rows}</tbody></table>
  <p>Tufts' new FJs have too few regattas yet for any prior. The Boston University row runs the full 2012&ndash;26 era with FJ drift, so its older seasons contribute almost nothing; a confirmed later purchase would shorten it.</p>

  <h2>9. Predicting boat quality, tested on held-out regattas</h2>
  <p>The averaging above treats a boat's effect as a residual. The cleaner way is to put the boat inside the rating model itself: in the Plackett&ndash;Luce likelihood each team's strength in a race is its skipper's and crew's rating that week, plus a team-at-regatta term for form, plus a term for the boat it is in. The boat terms of a fleet-era are shrunk toward zero with the era's own unevenness &tau;, which is estimated at the same time, and everything is fitted jointly on the GPU ({H['races']:,} races, {H['boats']:,} boats in {H['fleet_eras']} fleet-eras). Then the honest test: hold out <strong>every third regatta</strong> in every fleet ({H['regattas_heldout']:,} regattas, {H['races_heldout']:,} races), fit on the rest, and ask what the boat term knows about the regattas it has never seen.</p>
  <div class="fig">{fig_bars_signed(q_bars, 'realised boat effect, places/race', -0.08, 0.08, fmt='{:+.3f}', height=240, legend=False)}<div class="cap">Held-out regattas: each sail's realised effect on the day (team-demeaned surprisal, as in section 1) by fifth of its predicted effect from the other regattas. {H['n_sail_reg']:,} sail-regattas, &plusmn;{H['quintiles'][0][2]:.3f}. Monotone end to end: correlation with the realised effect {H['r_boat']:+.3f} (se {H['r_se']:.3f}).</div></div>
  <p>The fitted term is over-confident on its own: used at full size it makes the held-out finishing orders slightly <em>less</em> likely than the ratings alone. Scaled down it helps, and the best scale says how much of a boat's fitted effect is a property of the boat that travels to the next regatta:</p>
  <div class="fig">{fig_bars_signed(sc_bars, 'held-out likelihood gain, nats/race', -0.002, 0.004, fmt='{:+.4f}', height=240, legend=False)}<div class="cap">Gain in held-out log-likelihood per race over the ratings alone, by the factor applied to the boat term. The peak is at 0.4&ndash;0.5: {sc40[1]:+.4f} &plusmn; {sc40[2]:.4f} nats a race at 0.4, about {sc40[1] / sc40[2]:.0f} sigma. For scale, a whole place a race in a 12-boat fleet is worth {1 / H['places_per_nat']:.3f} nats.</div></div>
  <p>So the prediction method is: fit the boat term on the fleet's current era, and expect <strong>0.4 of it</strong> in the next regatta. At that scale the prediction is nearly calibrated (realised effects run {H['calib']:.2f} of predicted), and the boats it rates in the top fifth of a fleet beat the boats it rates in the bottom fifth by <strong>{H['gap']:+.2f} &plusmn; {H['gap_se']:.2f} places a race</strong> in regattas it has not seen &mdash; about {H['gap'] * 16:.0f} points over a 16-race regatta. That is the reproducible edge in knowing the boats: the size of a good crew swap, three to four times larger in the uneven 420 fleets at Cornell and Kings Point, and essentially nil in Brown's or Yale's.</p>
  <p>Why 0.4, and why not more? Within a single fleet-season the true spread of boats is &tau; = {G['tau']['FJ'][0]:.3f} z for FJs and {G['tau']['420'][0]:.3f} for 420s (about {G['tau']['FJ'][2]:.2f} places a race), against a per-regatta measurement noise of 0.37 z, so a sail's effect in one regatta is mostly noise and the term that fits it has to be discounted. Pooling the era is what makes the 40% recoverable:</p>
  <div class="fig">{fig_bars(need_bars, 'regattas needed', 50, fmt='{:.0f}', height=220)}<div class="cap">Regattas needed to identify a boat of a given true size at two sigma. A typical programme hosts four to six a year; stable 420 fleets can be pooled for three years, which is where the sharpest per-boat calls come from.</div></div>
  <div class="fig">{fig_bars(rel_bars, 'correlation with the truth', 1.0, fmt='{:.2f}', height=220)}<div class="cap">How well a season's estimate of a boat tracks its true effect, by regattas per sail. At the usual four, a fifth of what you see is boat; the joint fit with the era pooled behind it is what lifts that to a usable prediction.</div></div>

  <h2>What it is worth</h2>
  <ul class="plain">
    <li>A one-sd boat is {w['places']:.1f} places a race; in an uneven fleet the worst boat costs about a place a race, 15&ndash;20 points over a regatta. In Harvard's, Navy's or Brown's current fleets the boat you draw is worth under a quarter of a place.</li>
    <li>The boat can be predicted: fit it as a term in the rating model on the fleet's current era (confirmed purchase, detected break, or a multi-year gap in hosting) and take 0.4 of it. On regattas the fit never saw, the predicted-best fifth of a fleet beats the predicted-worst fifth by {H['gap']:.2f} places a race, {H['gap'] * 16:.0f} points over a regatta.</li>
    <li>Nothing here says a well-sailed boat is handed over fast, that any team's or skipper's boat is worth inheriting, or that a rotation can be gamed beyond knowing which sail is the slow one this season.</li>
  </ul>"""
    A = ROOT / 'analysis' / 'blog'
    return dict(slug='are-the-boats-even', title='Are the boats even?', date=date(2026, 8, 20), date_label='August 20, 2026', script='analysis/blog/boat_effects.py, boat_inherit.py, boat_fleet_prior.py, boat_pl.py, boat_pl_wind.py, boat_season_signal.py, boat_decay_shape.py, boat_season.py, boat_carry.py, boat_changepoints.py, boat_wear*.py, boat_post_data.py',
                summary=f"Rotation sheets, a null that keeps the rotation, and a chain of tests: boats differ by about {w['places']:.1f} places a race, a good sailor doesn't leave a boat faster, fleets are uneven from day one, and a boat term in the rating model predicts {H['gap']:.2f} places a race between a fleet's best and worst fifth on held-out regattas.",
                body=body, assets=[str(A / 'boat_heatmap_cornell_420.png'), str(A / 'boat_changepoints_tufts_lark.png'), str(A / 'boat_brown_eras.png'), str(A / 'boat_z_fleets.png'), str(A / 'boat_variability_time.png'), str(A / 'boat_fleet_prior.png')])


def short_school(n):
    if n in ('Boston College', 'Boston University'):
        return n
    return n.replace('University of ', '').replace(' University', '').replace(' College', '').replace('U. S. ', '')


# ---- team racing --------------------------------------------------------------------------------
def _tr():
    return json.loads((ROOT / 'analysis' / 'blog' / 'team_race_post.json').read_text())


def _school_name(slug):
    return ' '.join(w.capitalize() for w in slug.replace('st-mary', "st-mary's").split('-')).replace("St-mary's", "St. Mary's").replace('Uc ', 'UC ').replace('Mit', 'MIT')


def post_tr_forecasts():
    F = _tr()['forecasts']; m = F['model']; V = {v['key']: v for v in F['variants']}
    ll_bars = [(v['name'].split(' (')[0].replace('Site model', 'Site model'), [('log-loss, 6,335 held-out matches', v['logloss'], C_A if v['key'] == 'best-lam30' else C_B)]) for v in F['variants']]
    ll_bars = [(lab.replace('Fleet ratings + school', 'Ratings + school').replace('School strength only', 'School only').replace('Fleet ratings only', 'Ratings only'), b) for lab, b in ll_bars]
    cal_rows = ''.join(f"<tr><td>{c['lo'] * 100:.0f}&ndash;{c['hi'] * 100:.0f}%</td><td>{c['n']:,}</td><td>{c['p'] * 100:.1f}%</td><td>{c['won'] * 100:.1f}%</td></tr>" for c in F['calibration'])
    sch_rows = ''.join(f"<tr><td><a href=\"/icsa/schools/{s['slug']}/\">{esc(_school_name(s['slug']))}</a></td><td>{s['offset']:+.2f} &plusmn; {s['se']:.2f}</td><td>{s['matches']:,}</td><td>{s['win'] * 100:.0f}%</td></tr>" for s in F['schools_top'])
    best, main, school = V['best-lam30'], V['main'], V['school']
    I = F['issa']
    body = f"""
  <p class="lede">A team race forecast adds up what three boats bring, then asks how often that beats what the other three bring. Fleet ratings do most of the work; the school does the rest. Held out on two spring seasons, the forecast calls {best['acc'] * 100:.1f}% of matches and its probabilities are honest.</p>

  <h2>The model</h2>
  <p>Every ICSA team race match since 2013 with a full line-up: <strong>{m['matches']:,} matches</strong>, {m['sailors']:,} sailors, {m['schools']} schools. A side's strength is the sum over its three boats of the skipper's fleet rating (scaled by a fitted {m['k']:.2f}), a small per-sailor team race offset for the skipper and the crew, the school's team race term, and a home-water edge when the school hosts. The probability that side A beats side B is the logistic of the difference. Offsets are shrunk toward zero so a sailor with ten matches cannot acquire a large one; the fit uses a two-year half-life so old matches count less.</p>
  <p class="eq">P(A beats B) = &sigma;( strength(A) &minus; strength(B) ),&nbsp;&nbsp; strength = &Sigma;<sub>3 boats</sub> [ {m['k']:.2f}&middot;rating + skipper offset + crew offset ] + school + home</p>

  <h2>What each piece is worth</h2>
  <div class="fig">{fig_bars(ll_bars, 'log-loss (lower is better)', 0.75, fmt='{:.2f}', height=250)}<div class="cap">Held out: fit before every regatta date in spring 2025 and 2026 with fleet ratings frozen at the week before, then score the {best['matches']:,} matches. A coin flip scores 0.693.</div></div>
  <p>Knowing only which schools are racing gets to {school['logloss']:.3f} and {school['acc'] * 100:.1f}% of matches called. The three skippers' fleet ratings, nothing else, get to {main['logloss']:.3f} and {main['acc'] * 100:.1f}%. Adding the school term, the sailor offsets and the home edge brings it to <strong>{best['logloss']:.3f}</strong> and <strong>{best['acc'] * 100:.1f}%</strong>. The sailor offsets themselves are tiny: their fitted spread is {m['offset_sd']:.3f} for skippers and {m['crew_offset_sd']:.3f} for crews, against a school spread of {m['school_sd']:.2f}. Individual team race skill beyond the fleet rating is barely identifiable from results; what persists is the programme.</p>

  <h2>The school term</h2>
  <table class="t"><thead><tr><th>School</th><th>Team race term</th><th>Matches</th><th>Win share</th></tr></thead><tbody>{sch_rows}</tbody></table>
  <p>Schools with at least 500 matches, top of {F['n_schools']}. The term is what the school adds to the sum of its three boats, in the same units as a rating: St. Mary's, Stanford, Roger Williams and Charleston win more team races than their fleet ratings alone would predict, season after season. A per-season version of the term overfits and hurts the forecast, so the site uses one persistent number per school.</p>

  <h2>Are the probabilities honest?</h2>
  <table class="t"><thead><tr><th>Stated chance for the favourite</th><th>Matches</th><th>Mean stated</th><th>Favourite won</th></tr></thead><tbody>{cal_rows}</tbody></table>
  <p>Held-out matches grouped by the stated chance. The best temperature rescaling is 1.00: the numbers can be read as they are.</p>

  <h2>What did not help</h2>
  <ul class="plain">
    <li><strong>Finishing positions as extra training signal.</strong> Fitting the six places as a ranking, alongside the win, made the win forecast clearly worse. Team race finishes are tactical; a 2-3-4 says nothing about who was fastest.</li>
    <li><strong>Refitting between rounds of the same regatta.</strong> Form within an event adds almost nothing once the pre-event numbers are in.</li>
    <li><strong>Pair and trio familiarity.</strong> Line-ups are stable (the median pair has sailed 13 matches together), so there is nothing to learn from.</li>
    <li><strong>High school.</strong> The same model on ISSA data reaches {I['best-lam30']['logloss']:.3f} on {I['best-lam30']['matches']:,} held-out matches ({I['best-lam30']['acc'] * 100:.1f}% called), with crews often missing from the line-ups.</li>
  </ul>
  <p>On the site: every team race regatta page has a <em>Ratings &amp; Forecast</em> tab with the pre-event boat values, the forecast for each match and who beat it; sailor pages show a team race rating and a regatta-by-regatta surprisal; the <a href="/icsa/rankings/#teamrace">power rankings</a> have a Team Race selector.</p>"""
    return dict(slug='how-team-race-forecasts-work', title='How team race forecasts work', date=date(2026, 5, 21), date_label='May 21, 2026', script='analysis/team_race/model.py, evaluate.py and fit.py; numbers via analysis/blog/team_race_post_data.py',
                summary=f"Add up three boats' ratings, a school term and a home edge, then take the logistic of the difference. Held out on two spring seasons the forecast calls {best['acc'] * 100:.1f}% of matches, and the stated chances match what happened.",
                body=body)


def post_tr_orderliness():
    O = _tr()['orderliness']; d = O['ICSA']; h = O['ISSA']
    def ev_link(e):
        key = e['name']; url = d['url_by_name'].get(key)
        name = key.rsplit(' ', 1)[0]; season = key.rsplit(' ', 1)[1]
        lab = f"{esc(name)} (spring 20{season[1:]})"
        return f'<a href="/icsa{url}/">{lab}</a>' if url else lab
    up_rows = ''.join(f"<tr><td>{ev_link(e)}</td><td class=\"up\">{e['value']:+.2f} &plusmn; {e['se']:.2f}</td></tr>" for e in sorted(d['upsets'], key=lambda e: e['value']))
    or_rows = ''.join(f"<tr><td>{ev_link(e)}</td><td class=\"dn\">{e['value']:+.2f} &plusmn; {e['se']:.2f}</td></tr>" for e in sorted(d['orderly'], key=lambda e: -e['value']))
    wb = [(b['label'].replace('  ', ' ').replace('12-99', '12+'), [('residual orderliness', b['resid'], C_A if b['resid'] >= 0 else '#b5473a')]) for b in d['stated']['bins']]
    rb = [(b['label'].replace('  ', ' ').replace('12-99', '12+'), [('residual orderliness', b['resid'], C_A if b['resid'] >= 0 else '#b5473a')]) for b in d['reanalysis']['bins']]
    body = f"""
  <p class="lede">Some team race regattas go to form and some are a lottery, and the difference is real: it shows up in the first half of a round robin and again in the second. But nothing known before the first start predicts it. Not the field, not the host, not the wind.</p>

  <h2>Measuring it</h2>
  <p>For every match the <a href="/blog/how-team-race-forecasts-work/">forecast</a> gives a log-odds that the stronger side wins. An event's <strong>orderliness</strong> is the scale that best fits those log-odds to what happened at that event: 0 means the forecasts were right-sized, above 0 the favourites won even more than stated, below 0 the event was upset-heavy. It is the same quantity as the temperature on the Weather tab of fleet regattas, computed from matches instead of races. {d['regattas']} ICSA team race regattas since 2013, {d['matches']:,} matches; the typical standard error on one event is {d['median_se']:.2f}.</p>

  <h2>It is a property of the event</h2>
  <p>If every event had the same orderliness, the spread of the estimates would be explained by their standard errors; it is {d['het']:.2f} times that, which implies a true event-to-event spread of about <strong>{d['true_sd']:.2f}</strong>. Split each event's matches into odd and even and measure orderliness twice: the two halves agree at r = {d['split_half']:+.2f}; first half against second half, r = {d['halves']:+.2f}. {d['beyond2se']} of {d['n_csv']} events sit more than two standard errors from zero where about {d['n_csv'] * 0.046:.0f} would by chance.</p>
  <table class="t cases"><thead><tr><th>Most upset-heavy</th><th>Orderliness</th></tr></thead><tbody>{up_rows}</tbody></table>
  <table class="t cases"><thead><tr><th>Most orderly</th><th>Orderliness</th></tr></thead><tbody>{or_rows}</tbody></table>

  <h2>Nothing predicts it</h2>
  <ul class="plain">
    <li><strong>The field.</strong> Number of teams, matches, mean and spread of strength, newcomers, championship status, women's event, how much history the forecast had: a ridge model on all of them, cross-validated by event, explains R&sup2; = {d['field_r2']:+.2f}.</li>
    <li><strong>The host.</strong> A per-host effect on what is left over makes held-out prediction worse at every shrinkage tried (best R&sup2; {d['host_best_r2']:+.2f}, {d['n_hosts']} hosts). The host's fleet race orderliness, which is a real and persistent venue property, correlates with its team race orderliness at r = {d['fleet_offset_r']:+.2f} over {d['fleet_offset_n']} events.</li>
    <li><strong>The same regatta next year.</strong> r = {d['next_year']:+.2f} over {d['next_year_n']} consecutive editions.</li>
    <li><strong>A fleet regatta at the same venue the same weekend.</strong> {d['same_weekend_n']} cases, r = {d['same_weekend_r']:+.2f}.</li>
  </ul>

  <h2>Not the wind either</h2>
  <div class="fig">{fig_bars_signed(wb, 'residual orderliness', -0.08, 0.12, fmt='{:+.2f}', height=220, legend=False)}<div class="cap">By the scorer's stated wind, {d['stated']['n']} events with a wind in the daily summary. Slope {d['stated']['slope'] * 10:+.2f} per 10 knots, r = {d['stated']['r']:+.2f}.</div></div>
  <div class="fig">{fig_bars_signed(rb, 'residual orderliness', -0.08, 0.12, fmt='{:+.2f}', height=220, legend=False)}<div class="cap">By reanalysis wind at the venue, 9 to 5, {d['reanalysis']['n']} events. Slope {d['reanalysis']['slope'] * 10:+.2f} per 10 knots, r = {d['reanalysis']['r']:+.2f}.</div></div>
  <p>In fleet racing, <a href="/blog/venues-and-wind/">some venues get orderly in a breeze</a>. Apply each host's fitted fleet surface at the team race event's wind and the slope is {d['stated']['surface_slope']:.2f} of the fleet effect, not distinguishable from zero. A two-boat-per-side format with mark traps and pass-backs apparently erases what the wind does to a fleet.</p>

  <h2>High school</h2>
  <p>ISSA: {h['regattas']} regattas, true spread {h['true_sd']:.2f}, split-half r = {h['split_half']:+.2f}, the same regatta next year r = {h['next_year']:+.2f}. One thing does predict a little: championships are more orderly (R&sup2; = {h['champ_r2']:+.2f} on its own), which fits a format where the field has been qualified down to teams whose strength is well measured.</p>

  <h2>What this means on the site</h2>
  <p>Each team race regatta's <em>Ratings &amp; Forecast</em> tab shows its measured orderliness and the share of events it was more orderly than, as a description of what happened. There is no prediction, because there is nothing to predict it with. If you think a particular venue or a particular breeze makes team racing a lottery, this is the test that says otherwise, at least so far.</p>"""
    return dict(slug='which-team-races-go-to-form', title='Which team races go to form?', date=date(2026, 7, 9), date_label='July 9, 2026', script='analysis/team_race/orderliness.py; numbers via analysis/blog/team_race_post_data.py',
                summary=f"Orderliness is a real property of a team race regatta (split-half r = {d['split_half']:+.2f}, true spread {d['true_sd']:.2f}) and nothing known in advance predicts it: field R&sup2; {d['field_r2']:+.02f}, host effects negative, wind r = {d['stated']['r']:+.2f}.",
                body=body)


def post_tr_plays():
    P = _tr()['plays']; L = P['leagues']['ICSA']; H = P['leagues']['ISSA']
    GROUPS = [('Play 1 (1-2-x)', ['1-2-3', '1-2-4', '1-2-5', '1-2-6'], '#164b8c'), ('1-3 sets', ['1-3-4', '1-3-5', '1-3-6'], '#1e8f8a'), ('Play 4 (1-4-5)', ['1-4-5'], '#86c45f'), ('Play 2 (2-3-4/5)', ['2-3-4', '2-3-5'], '#d9c93c')]
    def mix_svg(rows, width=640):
        out = []; y = 0
        for lab, mix in rows:
            items = [(g, 100 * sum(mix.get(c, 0) for c in cs), col) for g, cs, col in GROUPS]
            out.append(stacked(items, width, 30, y, label=lab, label_w=150)); y += 36
        items = [(g, 1, col) for g, _, col in GROUPS]
        out.append(legend(items, y + 6)); return f'<svg viewBox="0 0 {width} {y + 24}" role="img" aria-label="winning combinations by play">{"".join(out)}</svg>'
    breeze_rows = [(f"{b} ({L['by_breeze'][b]['n']:,})", L['by_breeze'][b]) for b in ('drifter', 'light', 'moderate', 'fresh', 'heavy') if b in L['by_breeze']]
    hs_rows = [(f"{b} ({H['by_breeze'][b]['n']:,})", H['by_breeze'][b]) for b in ('drifter', 'light', 'moderate', 'fresh', 'heavy') if b in H['by_breeze']]
    lvl = L['by_level']; lv_rows = [(f"{lab.replace(' third', '')} · {g}", lvl[lab][g]) for lab in ('weakest third', 'middle third', 'strongest third') for g in ('light', 'moderate', 'fresh/heavy') if g in lvl[lab]]
    sp = L['by_spread']; sp_rows = [(f"{lab.replace(' third', '')} · {k}", sp[lab][k]) for lab in ('weakest third', 'middle third', 'strongest third') for k in ('balanced', 'lopsided')]
    first_bars = [(b, [('winner holds 1st', 100 * L['by_breeze'][b]['first'], C_A)]) for b in ('drifter', 'light', 'moderate', 'fresh', 'heavy') if b in L['by_breeze']]
    M = L['models']; mrows = ''.join(f"<tr><td>{esc(k)}</td><td>{v['nll']:.3f}</td><td>{v['win_ll']:.4f}</td><td>{v['shape_nll']:.3f}</td></tr>" if 'win_ll' in v else f"<tr><td>{esc(k)}</td><td>{v['nll']:.3f}</td><td>&mdash;</td><td>&mdash;</td></tr>" for k, v in M.items())
    heavy, light = L['by_breeze']['heavy'], L['by_breeze']['light']
    g234 = lambda m: 100 * (m['2-3-4'] + m['2-3-5']); g145 = lambda m: 100 * m['1-4-5']
    hs_fresh = H['by_breeze']['fresh']; hs_light = H['by_breeze']['light']
    body = f"""
  <p class="lede">Team racers name their winning combinations: Play 1 is 1-2-anything, Play 2 is 2-3-4, Play 4 is 1-4-5. Here is how often each actually happens, how that shifts with the breeze and the level, whether a star with two weak boats wins differently (no), and whether a model can predict the whole finish set from six ratings (somewhat).</p>

  <h2>The twenty sets</h2>
  <p>A side holds three of six places; there are twenty ways to do that and ten of them win (places summing to ten or less). Team racers group the winning ones: <strong>Play 1</strong>, 1-2-x, where the two lead boats control the race; <strong>Play 2</strong>, 2-3-4, with 2-3-5 the same play held one place looser; <strong>Play 4</strong>, 1-4-5, where the leader holds and the fourth and fifth boats keep the last opponent back; and the <strong>1-3 sets</strong>, which win but are unstable, the usual aim being to pass back the opponent in second and convert them into Play 1. {L['n_all']:,} ICSA matches with full line-ups and a pre-event value for each of the six boats.</p>
  <div class="fig">{mix_svg(breeze_rows)}<div class="cap">Winning combinations grouped by play, as a share of all wins, by the scorer's word for the day's breeze (one word per event, matches in brackets).</div></div>
  <p>Play 1 is most of team racing: the winner holds first place in {100 * light['first']:.0f}% of light-air wins and {100 * heavy['first']:.0f}% of heavy-air wins. As the breeze builds, college winners hold first less and win through the back-heavy sets more: Play 2 goes from {g234(light):.0f}% of wins to {g234(heavy):.0f}%, Play 4 from {g145(light):.1f}% to {g145(heavy):.1f}%. Only {heavy['n']} matches were sailed in what the scorer called heavy air, so the heavy column is the noisy one, but the gradient runs through fresh as well.</p>
  <div class="fig">{fig_bars(first_bars, 'winner holds 1st, % of wins', 100, fmt='{:.0f}', height=220)}<div class="cap">Share of wins in which the winning side took first place, by breeze.</div></div>

  <h2>By level and breeze</h2>
  <div class="fig">{mix_svg(lv_rows)}<div class="cap">Thirds by the mean strength of the two sides, then breeze grouped into light (drifter and light), moderate, and fresh/heavy.</div></div>
  <p>The breeze effect lives in the middle and top thirds. Weak fleets win by 1-2-3 sweeps in any wind; in the middle third, Play 2 climbs from {100 * (lvl['middle third']['light']['2-3-4'] + lvl['middle third']['light']['2-3-5']):.0f}% of wins in light air to {100 * (lvl['middle third']['fresh/heavy']['2-3-4'] + lvl['middle third']['fresh/heavy']['2-3-5']):.0f}% in fresh air. Strong, even fleets in a breeze produce the fewest Play 1 wins.</p>
  <div class="fig">{mix_svg(hs_rows)}<div class="cap">High school (ISSA), the same cut. The opposite gradient: 1-2-3 sweeps go from {100 * hs_light['1-2-3']:.0f}% of wins in light air to {100 * hs_fresh['1-2-3']:.0f}% in fresh air.</div></div>
  <p>High school goes the other way. In a breeze, high school winners sweep 1-2-3 more and the winner holds first in {100 * hs_fresh['first']:.0f}% of fresh-air wins. One reading: college teams in a breeze are even enough that the race is decided by mark traps in the pack; high school fleets are uneven enough that breeze simply stretches them out.</p>

  <h2>Does a star with two weak boats win differently?</h2>
  <div class="fig">{mix_svg(sp_rows)}<div class="cap">Winners only, split into thirds by the sum of their three boat values, then into balanced and lopsided halves by the spread of the three.</div></div>
  <p>No. Within each strength third, the balanced and lopsided halves win through the same mix of plays, to within a point. The spread also adds nothing to the win forecast: held-out log-loss {P['spread_ll']['sum only']:.4f} on the sum alone, {P['spread_ll']['sum + sd']:.4f} with the spread added. Among {P['lopsided_n']} held-out matches where the two sides had nearly equal totals and clearly different spreads, the more lopsided side won {P['lopsided_win']:.0f}%. Three boats of 3.0 and one of 4.0 with two of 2.5 are, as far as results can tell, the same side.</p>

  <h2>Predicting the whole finish set</h2>
  <p>Can six ratings predict not only who wins but how? A twenty-way multinomial on the six sorted boat values, the gap, each side's spread, best against best, worst against worst, the school term and the home flags, trained on seasons before spring 2025 and scored on spring 2025 and 2026 ({L['n_test']:,} matches, each seen from both sides).</p>
  <table class="t"><thead><tr><th>Model</th><th>Finish-set NLL</th><th>Implied win log-loss</th><th>Shape given the winner</th></tr></thead><tbody>{mrows}</tbody></table>
  <p>Negative log-likelihood of the actual set per match, in nats, lower is better. The multinomial beats the obvious two-stage baseline, the production win probability times the overall frequency of each set, by about a tenth of a nat; the gain is in the shape of the win, not in who wins (its implied win log-loss is a touch worse than the production forecast's). A small network does no better than the linear model, so there is no hidden non-linearity in six numbers. What the model has learned is simple: the stronger the side, the more its wins are Play 1 and the fewer are 1-3 sets and Play 2.</p>
  <p class="eq"><a href="/icsa/lab/team-race-combinations/">Explore it: the mix of finish sets as a side gets stronger, as one boat is raised, as a star is built, by play &rarr;</a></p>

  <h2>Caveats</h2>
  <ul class="plain">
    <li>Breeze is one word per event, from the scorer's daily summary; a two-day regatta with one light day and one fresh day is "moderate". Heavy-air events are few and cluster at a handful of venues.</li>
    <li>Boat values are the pre-event forecast values, so nothing looks ahead; regattas forecast from fewer than 1,000 prior matches are left out.</li>
    <li>These are finish sets, not the plays as sailed. A race that ends 1-2-6 may have been Play 4 until the last beat.</li>
  </ul>"""
    return dict(slug='play-1-play-2-play-4', title='Play 1, Play 2, Play 4: how team races are won', date=date(2026, 8, 27), date_label='August 27, 2026', script='analysis/team_race/sequence_model.py and export_combinations.py; numbers via analysis/blog/team_race_post_data.py',
                summary=f"Winning combinations named the way team racers name them, across {L['n_all']:,} college matches: Play 1 is most of team racing; college winners hold first less in a breeze and high school winners sweep more; a star with two weak boats wins the same way as three even boats; and six ratings predict the finish set a tenth of a nat better than the win probability alone.",
                body=body)


POSTS = [post_rating_point, post_skipper, post_elo, post_swap, post_tr_forecasts, post_crew, post_tr_orderliness, post_venues, post_boats, post_tr_plays]   # oldest first; the index shows newest first


def index_page(posts):
    items = ''.join(f'<li><div class="d">{esc(p["date_label"])}</div><a class="t1" href="/blog/{p["slug"]}/">{esc(p["title"])}</a><p>{p["summary"]}</p></li>' for p in posts)
    body = f'''<header class="hero" style="padding-bottom:0.6rem">
    <div class="kicker">Observations from the models and data</div>
    <h1 class="title">Notes</h1>
    <p class="tagline">Short write-ups of what the ratings and results show about college sailing. Each says how its numbers were computed and where they could be wrong.</p>
  </header>
  <ul class="plist">{items}</ul>'''
    return page('Notes', 'Observations from a college sailing ratings model: what the data shows, how it was computed, and where it could be wrong.', body)


def main():
    write_css()
    posts = [dict(f(), order=i) for i, f in enumerate(POSTS)]
    posts.sort(key=lambda p: (p['date'], p['order']), reverse=True)
    for p in posts:
        d = OUT / p['slug']; d.mkdir(parents=True, exist_ok=True)
        (d / 'index.html').write_text(post_page(p), encoding='utf-8')
        for src in p.get('assets', []): shutil.copy(src, d / Path(src).name)
        print('wrote', d.relative_to(ROOT))
    (OUT / 'index.html').write_text(index_page(posts), encoding='utf-8')
    sys.path.insert(0, str(ROOT / 'scripts')); import build_venue_maps; build_venue_maps.build()
    print('wrote website/blog/index.html')


if __name__ == '__main__':
    main()
