#!/usr/bin/env python3
"""Render website/engineering/index.html: a terse, chart-first engineering summary.

All numbers are constants taken from the evaluation pages and JSON (methodology page, analysis/disciplines/squads_stage2_eval*.json);
charts are inline SVG so the page has no script and no external assets.

    python3 scripts/render_engineering_page.py
"""
from __future__ import annotations

import html
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "website" / "engineering" / "index.html"

# Season-ahead pairwise accuracy, four held-out seasons (methodology page).
ACCURACY = [("Coin flip", 50.0), ("Avg finish %", 65.3), ("Elo", 68.7), ("PL static", 68.8), ("PL weekly, pre-fix", 69.6), ("PL weekly (site)", 70.4)]
# Calibration: (model said %, favourite finished ahead %), 10 bins.
CALIBRATION = [(53, 52), (58, 57), (62, 61), (68, 66), (72, 70), (77, 75), (82, 80), (87, 86), (92, 90), (97, 94)]
# Entry-rating error (rating points, lower is better), forward by season.
SQUAD = {
    "Singlehanded": [("Calibrated (site)", 0.43), ("Best-rated sails", 0.49), ("Most likely squad", 0.55), ("Pool average", 0.60)],
    "Match race": [("Calibrated (site)", 0.44), ("Best-rated sails", 0.62), ("Most likely squad", 0.55), ("Pool average", 0.57)],
}

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"


def bar_chart(rows, lo, hi, unit="", fmt="{:.1f}", tick_fmt=None, highlight=-1, width=520, label_w=130, row_h=26) -> str:
    """Horizontal bars on one linear axis from lo to hi, value printed at the bar end, the highlighted row in the accent colour."""
    h = row_h * len(rows) + 28
    plot_w = width - label_w - 56
    x = lambda v: label_w + (v - lo) / (hi - lo) * plot_w
    out = [f'<svg viewBox="0 0 {width} {h}" role="img" class="chart">']
    ticks = [lo + i * (hi - lo) / 4 for i in range(5)]
    for t in ticks:
        out.append(f'<line x1="{x(t):.1f}" y1="0" x2="{x(t):.1f}" y2="{h - 20}" class="grid"/>'
                   f'<text x="{x(t):.1f}" y="{h - 6}" class="tick" text-anchor="middle">{(tick_fmt or fmt).format(t)}{unit}</text>')
    for i, (name, v) in enumerate(rows):
        y = i * row_h + 4
        cls = "bar hl" if i == (highlight % len(rows)) else "bar"
        out.append(f'<text x="{label_w - 8}" y="{y + 13}" class="lab" text-anchor="end">{html.escape(name)}</text>'
                   f'<rect x="{label_w}" y="{y}" width="{max(1.0, x(v) - label_w):.1f}" height="{row_h - 10}" rx="2" class="{cls}"/>'
                   f'<text x="{x(v) + 5:.1f}" y="{y + 13}" class="val">{fmt.format(v)}{unit}</text>')
    out.append("</svg>")
    return "".join(out)


def reliability_chart(points, size=300) -> str:
    """Predicted vs observed win frequency with the y = x diagonal; points under the line are overconfident."""
    m, lo, hi = 38, 50.0, 100.0
    s = lambda v: m + (v - lo) / (hi - lo) * (size - m - 12)
    sy = lambda v: size - 24 - (v - lo) / (hi - lo) * (size - m - 12)
    out = [f'<svg viewBox="0 0 {size} {size}" role="img" class="chart square">']
    for t in (50, 60, 70, 80, 90, 100):
        out.append(f'<line x1="{s(t):.1f}" y1="{sy(lo):.1f}" x2="{s(t):.1f}" y2="{sy(hi):.1f}" class="grid"/>'
                   f'<line x1="{s(lo):.1f}" y1="{sy(t):.1f}" x2="{s(hi):.1f}" y2="{sy(t):.1f}" class="grid"/>'
                   f'<text x="{s(t):.1f}" y="{size - 8}" class="tick" text-anchor="middle">{t}</text>'
                   f'<text x="{m - 6}" y="{sy(t) + 3:.1f}" class="tick" text-anchor="end">{t}</text>')
    out.append(f'<line x1="{s(lo):.1f}" y1="{sy(lo):.1f}" x2="{s(hi):.1f}" y2="{sy(hi):.1f}" class="diag"/>')
    pts = " ".join(f"{s(p):.1f},{sy(o):.1f}" for p, o in points)
    out.append(f'<polyline points="{pts}" class="line"/>')
    for p, o in points:
        out.append(f'<circle cx="{s(p):.1f}" cy="{sy(o):.1f}" r="3.2" class="dot"><title>said {p}%, happened {o}%</title></circle>')
    out.append(f'<text x="{size / 2:.0f}" y="{size - 0}" class="axt" text-anchor="middle"></text></svg>')
    return "".join(out)


FLOW = [
    ("Collect", "scraper.py", "Techscore: results, rosters, entries"),
    ("Store", "build_db.py", "SQLite x2, women's tags, HS-to-college links"),
    ("Rate", "train_pl_weekly.py", "Plackett-Luce, team pooling; discipline + crew layers"),
    ("Forecast", "predict_upcoming_lineups.py", "lineups, squads, Monte Carlo odds"),
    ("Publish", "generate_site.py", "120K+ static pages, Flask API"),
    ("Ship", "refresh_f26.sh", "checks, rsync, restart, health checks"),
]


def flow_svg() -> str:
    w, h, gap = 138, 76, 12
    total = len(FLOW) * w + (len(FLOW) - 1) * gap
    out = [f'<svg viewBox="0 0 {total} {h + 8}" role="img" class="chart flow">']
    for i, (a, f, d) in enumerate(FLOW):
        x = i * (w + gap)
        out.append(f'<rect x="{x}" y="4" width="{w}" height="{h}" rx="5" class="box"/>'
                   f'<text x="{x + 8}" y="22" class="fa">{i + 1} {a}</text>'
                   f'<text x="{x + 8}" y="38" class="ff">{html.escape(f)}</text>')
        words, line, ly = d.split(), "", 53
        for wd in words:
            if len(line) + len(wd) > 22:
                out.append(f'<text x="{x + 8}" y="{ly}" class="fd">{html.escape(line.strip())}</text>')
                line, ly = "", ly + 11
            line += wd + " "
        out.append(f'<text x="{x + 8}" y="{ly}" class="fd">{html.escape(line.strip())}</text>')
        if i < len(FLOW) - 1:
            out.append(f'<path d="M{x + w + 2},{h / 2 + 4} l{gap - 4},0 m-4,-3 l4,3 l-4,3" class="arrow"/>')
    out.append("</svg>")
    return "".join(out)


DECISIONS = [
    ("Trainer stopped before converging", "rating increments + first-race prior, retuned on week-ahead forecasts", "69.6 → 70.4% pairwise"),
    ("Newcomers have no history", "start at school debut median, not league mean", "+3.5 pts on newcomer pairs"),
    ("Races in a regatta are correlated", "simulate with regatta effect, drift, same-boat effect", "team totals checked at every championship"),
    ("Most likely squad is a poor rating forecast", "price a calibrated, probability-weighted rating", "error 0.43 vs 0.55 (SH), 0.44 vs 0.55 (MR)"),
    ("Strongest-event-first sent specialists to fleet", "staff singlehanded / match race first", "0.9% of such drivers also sail fleet that week"),
    ("Availability is uncertain", "Monte Carlo weekend, one sailor per boat, 2,000 draws", "80% interval shown per entry"),
    ("Collected data vs source", "re-pull, compare boat by boat with Techscore", "0 dropped boats, all numeric fields match"),
    ("Page and server must quote the same price", "one snapping rule in Python and JS", "unit test compares both"),
]

CSS = f"""
:root{{--ink:#1c2128;--sub:#667085;--line:#e4e7ec;--bg:#fcfcfd;--panel:#fff;--accent:#1a4f8a;--soft:#e8f0fa;--bar:#b7c4d6;--mono:'SF Mono',Consolas,Menlo,'Liberation Mono',monospace;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 {FONT};}}
a{{color:var(--accent)}}
.nav{{display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap;max-width:880px;margin:0 auto;padding:1rem 1.25rem;font-size:13px;font-weight:600}}
.nav a{{color:var(--sub);text-decoration:none;margin-left:1.1rem}} .nav a.on,.nav a:hover{{color:var(--accent)}} .nav span{{color:var(--sub)}}
main{{max-width:880px;margin:0 auto;padding:0 1.25rem 4rem}}
h1{{font-size:28px;line-height:1.2;margin:1.4rem 0 .3rem;letter-spacing:-.01em}}
.sub{{color:var(--sub);margin:0 0 1.2rem}}
h2{{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--sub);margin:2.2rem 0 .6rem;padding-bottom:.35rem;border-bottom:1px solid var(--line)}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:.75rem;margin:1rem 0 0}}
.stats div{{border-top:2px solid var(--ink);padding-top:.5rem}} .stats b{{display:block;font-size:26px;letter-spacing:-.02em;color:var(--accent);font-variant-numeric:tabular-nums}}
.stats span{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--sub)}}
.grid2{{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:1.5rem;align-items:start}}
@media(max-width:700px){{.grid2{{grid-template-columns:1fr}}}}
.fig{{margin:0}} .fig figcaption{{font-size:12px;color:var(--sub);margin-top:.3rem}}
svg.chart{{width:100%;height:auto;display:block}} svg.square{{max-width:300px}}
.grid{{stroke:var(--line);stroke-width:1}} .tick{{font:10px {FONT};fill:var(--sub)}} .lab{{font:12px {FONT};fill:var(--ink)}} .val{{font:600 11px {FONT};fill:var(--ink);font-variant-numeric:tabular-nums}}
.bar{{fill:var(--bar)}} .bar.hl{{fill:var(--accent)}}
.diag{{stroke:var(--sub);stroke-dasharray:4 4;stroke-width:1}} .line{{fill:none;stroke:var(--accent);stroke-width:1.6}} .dot{{fill:var(--accent)}}
.box{{fill:var(--panel);stroke:var(--line)}} .fa{{font:700 12px {FONT};fill:var(--accent)}} .ff{{font:8.6px var(--mono);fill:var(--ink)}} .fd{{font:9.5px {FONT};fill:var(--sub)}} .arrow{{stroke:var(--sub);fill:none;stroke-width:1.2}}
.flowwrap{{overflow-x:auto}} .flowwrap svg{{min-width:760px}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{text-align:left;padding:.4rem .5rem;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--sub)}} td.m{{font-family:var(--mono);font-size:12px;color:var(--accent);white-space:nowrap}}
.tblwrap{{overflow-x:auto}}
.kv{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:.5rem 1.5rem;font-size:13px}} .kv div{{border-bottom:1px solid var(--line);padding:.25rem 0;display:flex;justify-content:space-between;gap:.5rem}} .kv b{{font-variant-numeric:tabular-nums}}
ul.l{{margin:.2rem 0;padding-left:1.1rem;font-size:13px;color:var(--sub)}}
.links a{{margin-right:1rem;font-size:13px;font-weight:600}}
footer{{margin-top:2.5rem;font-size:12px;color:var(--sub);border-top:1px solid var(--line);padding-top:.8rem}}
"""


def page() -> str:
    acc = bar_chart(ACCURACY, 50, 72, unit="%", tick_fmt="{:.0f}", highlight=-1, width=520, label_w=120)
    cal = reliability_chart(CALIBRATION)
    sq = {k: bar_chart(v, 0.0, 0.8, fmt="{:.2f}", tick_fmt="{:.1f}", highlight=0, width=420, label_w=110) for k, v in SQUAD.items()}
    rows = "".join(f"<tr><td>{html.escape(a)}</td><td>{html.escape(b)}</td><td class='m'>{html.escape(c)}</td></tr>" for a, b, c in DECISIONS)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Under the Hood — Data-Driven Sailing</title>
<meta name="description" content="Pipeline, forward-tested accuracy, calibration and engineering decisions behind Data-Driven Sailing.">
<style>{CSS}</style></head><body>
<nav class="nav"><span>Data-Driven Sailing</span><span><a href="../">Home</a><a class="on" href="./">Under the hood</a><a href="../icsa/methodology/">Methodology</a><a href="../icsa/">ICSA</a><a href="../issa/">ISSA</a></span></nav>
<main>
<h1>Under the hood</h1>
<p class="sub">Scrape, rate, forecast, publish. Every figure below is out-of-sample.</p>
<div class="stats">
<div><b>1.58M</b><span>boat-race rows</span></div><div><b>7,693</b><span>regattas, 37 seasons</span></div>
<div><b>23,824</b><span>sailors rated</span></div><div><b>212</b><span>tests, CI on clean checkout</span></div>
</div>

<h2>Pipeline</h2>
<div class="flowwrap">{flow_svg()}</div>

<h2>Accuracy</h2>
<div class="grid2">
<figure class="fig">{acc}<figcaption>Pairs of boats ordered correctly, 4 held-out seasons (~16,000 races). Championships: 72.4% (95% CI 71.6&ndash;73.3), 166 regattas.</figcaption></figure>
<figure class="fig">{cal}<figcaption>Calibration. x: model said, y: happened (%). Under the dashed line = overconfident; honest to ~85%.</figcaption></figure>
</div>

<h2>Squad model error (rating points, lower is better)</h2>
<div class="grid2" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">
<figure class="fig"><div class="lab" style="font-weight:600;margin-bottom:.2rem">Singlehanded</div>{sq['Singlehanded']}</figure>
<figure class="fig"><div class="lab" style="font-weight:600;margin-bottom:.2rem">Match race</div>{sq['Match race']}</figure>
</div>
<p class="sub" style="font-size:12px;margin-top:.4rem">Fitted before each season, tested on it. Exact driver named: 63% / 46%. Ratings are today's, so slightly optimistic.</p>

<h2>Decisions</h2>
<div class="tblwrap"><table><thead><tr><th>Problem</th><th>Change</th><th>Evidence</th></tr></thead><tbody>{rows}</tbody></table></div>

<h2>Operations</h2>
<div class="kv">
<div><span>Nightly pipeline</span><b>gated deploy</b></div><div><span>Optional stages</span><b>fail soft</b></div>
<div><span>Page generators</span><b>idempotent</b></div><div><span>Worker restart</span><b>pkill lswsgi</b></div>
<div><span>Import-time threads</span><b>none</b></div><div><span>Prices (Py = JS)</span><b>tested</b></div>
</div>

<h2>Limits</h2>
<ul class="l">
<li>Match-race results on Techscore are one final ranking: no round-robin data.</li>
<li>~5,400 singlehanded rows lack a class year, ~1,300 keelboat boats lack a skipper; neither feeds ratings.</li>
<li>Discipline accuracy uses today's ratings. The fantasy market is pretend: Mollicoins are not real money, and it is not gambling.</li>
</ul>

<p class="links" style="margin-top:1.5rem"><a href="../icsa/methodology/">Methodology</a><a href="../icsa/rankings/">Rankings</a><a href="../icsa/f26/predicted-lineups/">Predicted lineups</a><a href="../icsa/fantasy/">Fantasy</a></p>
<footer>Quinn Brighton &middot; <a href="mailto:quinnbrighton2005@gmail.com">quinnbrighton2005@gmail.com</a> &middot; data: scores.collegesailing.org, scores.hssailing.org</footer>
</main></body></html>
"""


if __name__ == "__main__":
    OUT.write_text(page(), encoding="utf-8")
    print(f"wrote {OUT}")
