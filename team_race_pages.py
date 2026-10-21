"""Team race blocks for the site, from analysis/team_race (fit.py, upcoming.py).

forecast_html(site, regatta_name, school_href)  table for an upcoming team race page: each school's predicted line-up, strength,
                                                 expected match win share, chance of winning the regatta and of a top three.
sailor_block(league, name, school_name)          a sailor page section: team race rating, rank among active skippers, the offset
                                                 against what the fleet rating predicts, and a season-by-season record.
"""
from __future__ import annotations

import csv
import html as html_module
import json
import os
import re
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(BASE, 'analysis', 'team_race')
MIN_MATCHES = 30
ACTIVE_SEASONS = 3
_C: dict = {}


def _esc(s) -> str:
    return html_module.escape(str(s if s is not None else ''))


def _season_order(s: str) -> tuple:
    return (int(s[1:]), 1 if s[0] == 'f' else 0) if s and s[1:].isdigit() else (0, 0)


def _label(s: str) -> str:
    return ('Fall ' if s[0] == 'f' else 'Spring ') + str(2000 + int(s[1:])) if s and s[1:].isdigit() else ''


def _slug(name: str, school_slug: str) -> str:
    base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return f'{base}-{school_slug}' if school_slug else base


# ── upcoming regatta forecast ───────────────────────────────────────────────

def _upcoming() -> dict:
    path = os.path.join(DIR, 'upcoming_teamrace.json')
    stamp = os.path.getmtime(path) if os.path.exists(path) else 0
    hit = _C.get('up')
    if hit and hit[0] == stamp:
        return hit[1]
    data: dict = {}
    try:
        for r in json.load(open(path, encoding='utf-8')):
            data[(r['site'], r['regatta'].strip().lower())] = r
    except (OSError, ValueError, KeyError):
        pass
    _C['up'] = (stamp, data)
    return data


def forecast_html(site: str, regatta_name: str, school_href=None) -> str:
    r = _upcoming().get((site, (regatta_name or '').strip().lower()))
    if not r or not r.get('teams'):
        return ''
    rows = ''
    for i, t in enumerate(r['teams']):
        name = _esc(t['school'])
        if school_href:
            h = school_href(t['school'])
            name = f'<a href="{_esc(h)}">{name}</a>' if h else name
        if t.get('home'):
            name += ' <span style="font-size:.8em;color:#888" title="hosting: home-water edge included">(host)</span>'
        boats = []
        for b in t['lineup']:
            sk = _esc(b['skipper']); cr = _esc(b['crew'])
            sk = f'<a href="../../sailors/{_slug(b["skipper"], t["slug"])}/">{sk}</a>'
            boats.append(sk + (f' <span style="color:#777">/ {cr}</span>' if cr else ''))
        lineup = '<br>'.join(boats) if boats else '<span style="color:#999">no recent team racing</span>'
        rows += (f'      <tr class="row{i % 2}"><td class="schoolname" style="vertical-align:top">{name}</td>'
                 f'<td style="font-size:.9em;line-height:1.35">{lineup}</td>'
                 f'<td style="text-align:right;vertical-align:top">{t["win_share"] * 100:.0f}%</td>'
                 f'<td style="text-align:right;vertical-align:top">{t["win_pct"] * 100:.1f}%</td>'
                 f'<td style="text-align:right;vertical-align:top">{t["top3_pct"] * 100:.0f}%</td>'
                 f'<td style="text-align:right;vertical-align:top">{t["avg_rank"]:.1f}</td></tr>\n')
    return f'''    <h3 style="margin:0.6em 0 0.3em">Team race forecast</h3>
    <table class="results">
      <thead><tr><th style="text-align:left">School</th><th style="text-align:left">Predicted line-up (skipper / crew)</th>
        <th style="text-align:right" title="Expected share of matches won against this field">Match win %</th>
        <th style="text-align:right">Win</th><th style="text-align:right">Top 3</th><th style="text-align:right">Avg finish</th></tr></thead>
      <tbody>
{rows}      </tbody>
    </table>
    <p style="font-size:.85em;color:#666;max-width:70ch">Forecast from {int(r.get('n_sims', 0)):,} simulated round robins. Each side is the sum of
    its three boats (the skipper&#39;s fleet rating plus their team race record, and the crew&#39;s) plus the school&#39;s team race strength and a
    home-water edge for the host. Line-ups are each school&#39;s most-used skippers and crews from its last two seasons of team racing, graduates
    removed; they change when real line-ups are posted. Tested on held-out events the model calls 79% of matches.</p>'''


# ── finished regatta: pre-event forecast tab ───────────────────────────────

def _forecasts(league: str) -> dict:
    path = os.path.join(DIR, f'regatta_forecasts_{league}.json')
    stamp = os.path.getmtime(path) if os.path.exists(path) else 0
    hit = _C.get(('fc', league))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        data = json.load(open(path, encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    _C[('fc', league)] = (stamp, data)
    return data


def _tr_orderliness(league: str) -> dict:
    """url -> (actual temperature relative to the league's team race average, se, percentile among the league's events) from
    data/orderliness_teamrace.csv (analysis/team_race/orderliness.py).  Nothing predicts it (field, venue and wind all failed), so
    only the measured value is shown."""
    key = ('tr_order', league)
    if key in _C:
        return _C[key]
    rows = []
    try:
        with open(os.path.join(BASE, 'data', 'orderliness_teamrace.csv'), newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                if r['league'] == league:
                    rows.append((r['url'], float(r['actual']), float(r['se'])))
    except (OSError, ValueError):
        pass
    vals = sorted(a for _, a, _ in rows)
    out = {}
    for url, a, se in rows:
        below = sum(1 for v in vals if v < a)
        out[url] = (a, se, below / len(vals) if vals else 0.5)
    _C[key] = out
    return out


def _pbar(p: float, upset: bool = False) -> str:
    col = '#c0392b' if upset else '#2e7d32'
    return (f'<span style="display:inline-block;width:60px;height:9px;background:#eee;vertical-align:middle;margin-right:4px">'
            f'<span style="display:block;width:{p * 60:.0f}px;height:9px;background:{col}"></span></span>{p * 100:.0f}%')


def _lineups(league: str) -> dict:
    """regatta_url -> {match_num: ((winner skippers), (loser skippers))} from matches.csv, for the surprisal table."""
    key = ('lineups', league)
    if key in _C:
        return _C[key]
    out: dict = defaultdict(dict)
    try:
        with open(os.path.join(DIR, 'matches.csv'), newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                if r['league'] == league:
                    out[r['regatta_url']][int(r['match_num'])] = (tuple(r['w_skippers'].split(';')), tuple(r['l_skippers'].split(';')),
                                                                  tuple(x for x in r['w_crews'].split(';') if x), tuple(x for x in r['l_crews'].split(';') if x))
    except OSError:
        pass
    _C[key] = out
    return out


def _perf_shift(logits: list, won: list, temp: float) -> tuple[float, str]:
    """The shift d (on the strength scale) that makes sum sigmoid(logit_i + temp*d) equal the wins: the least-surprisal value of
    the side (or boat) over these matches, everything else fixed.  Flag '>=' / '<=' when every match was won / lost."""
    import math
    w = sum(won)
    if w == len(won):
        return 3.0, '>='
    if w == 0:
        return -3.0, '<='
    lo, hi = -6.0, 6.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if sum(1 / (1 + math.exp(-(l + temp * mid))) for l in logits) < w:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2, ''


def _surprisal(logits: list, won: list, temp: float, shift: float = 0.0) -> float:
    """Bits of surprise of the actual outcomes at the given shift."""
    import math
    tot = 0.0
    for l, y in zip(logits, won):
        p = 1 / (1 + math.exp(-(l + temp * shift)))
        p = min(max(p if y else 1 - p, 1e-9), 1 - 1e-9)
        tot -= math.log2(p)
    return tot


def _p_at_least(ps: list, k: int) -> float:
    """P(at least k wins) when match i is won with probability ps[i], independently (Poisson-binomial)."""
    dist = [1.0]
    for p in ps:
        nxt = [0.0] * (len(dist) + 1)
        for i, v in enumerate(dist):
            nxt[i] += v * (1 - p); nxt[i + 1] += v * p
        dist = nxt
    return sum(dist[k:])


def surprisal_section(league: str, regatta_url: str, fc: dict, school_link, sailor_href, order: list) -> str:
    """Team race surprisal: for each side and each skipper, the strength that makes their record least surprising, the extra
    surprise of the record at the pre-event strength, and the chance of this many wins or more.  `order`: (place, school, slug, team)."""
    import math
    temp = float(fc.get('model', {}).get('temperature', 1.0) or 1.0)
    lineups = _lineups(league).get(regatta_url.rstrip('/'), {})
    teams = {(t['school'], t['team']): t for t in fc['teams']}
    by_team: dict = defaultdict(lambda: ([], []))            # (slug, team) -> (logits, won)
    by_sk: dict = defaultdict(lambda: ([], [], set()))       # sid -> (logits, won, teams)
    for m in fc['matches']:
        p = min(max(m['p'], 1e-6), 1 - 1e-6)
        lw = math.log(p / (1 - p))                           # logit for the winner; the loser sees -lw
        by_team[(m['w'], m['wt'])][0].append(lw); by_team[(m['w'], m['wt'])][1].append(1)
        by_team[(m['l'], m['lt'])][0].append(-lw); by_team[(m['l'], m['lt'])][1].append(0)
        lu = lineups.get(m['match'])
        if lu:
            for sid in lu[0]:
                by_sk[sid][0].append(lw); by_sk[sid][1].append(1); by_sk[sid][2].add((m['w'], m['wt']))
            for sid in lu[1]:
                by_sk[sid][0].append(-lw); by_sk[sid][1].append(0); by_sk[sid][2].add((m['l'], m['lt']))
    if not by_team:
        return ''

    def cells(logits, won):
        n = len(won); w = sum(won)
        d, flag = _perf_shift(logits, won, temp)
        s0 = _surprisal(logits, won, temp); s1 = _surprisal(logits, won, temp, d)
        ps = [1 / (1 + math.exp(-l)) for l in logits]
        p_ge = _p_at_least(ps, w)
        dtxt = ('&ge;' if flag == '>=' else '&le;' if flag == '<=' else '') + f'{d:+.2f}'.replace('-', '&minus;')
        dcol = '#1a8a3a' if d >= 0.3 else ('#c0392b' if d <= -0.3 else '#555')
        pcol = '#1a8a3a' if p_ge <= 0.2 else ('#c0392b' if p_ge >= 0.8 else '#333')
        ptxt = '&lt;1%' if p_ge < 0.005 else ('&gt;99%' if p_ge > 0.995 else f'{p_ge * 100:.0f}%')
        return (f'<td style="text-align:right">{w}&ndash;{n - w}</td><td style="text-align:right">{sum(ps):.1f}</td>'
                f'<td style="text-align:right;color:{dcol};font-weight:600">{dtxt}</td><td style="text-align:right">{max(0.0, s0 - s1):.1f}</td>'
                f'<td style="text-align:right;color:{pcol}">{ptxt}</td>'), d

    school_name = {sl: s for _, s, sl, _ in order}
    place_of = {(sl, tm): pl for pl, _, sl, tm in order}
    multi = len({sl for _, _, sl, _ in order}) < len(order)
    trows = ''
    team_rows = []
    for key, (logits, won) in by_team.items():
        c, d = cells(logits, won)
        team_rows.append((d, key, c))
    team_rows.sort(key=lambda x: -x[0])
    for i, (d, (slug, team), c) in enumerate(team_rows):
        t = teams.get((slug, team), {})
        name = school_link(school_name.get(slug, slug), slug) if school_link else _esc(school_name.get(slug, slug))
        if multi:
            name += f' <span style="color:#777;font-size:.85em">{_esc(team)}</span>'
        pl = place_of.get((slug, team))
        st = t.get('strength')
        trows += (f'      <tr class="row{i % 2}"><td style="text-align:right">{pl if pl is not None else "&mdash;"}</td><td class="schoolname" style="text-align:left">{name}</td>'
                  f'<td style="text-align:right">{st:.2f}</td><td style="text-align:right;font-weight:600">{st + d:.2f}</td>{c}</tr>\n'
                  if st is not None else '')
    # skippers: boat value from the forecast's team boats
    boat_val = {}
    for t in fc['teams']:
        for b in t.get('boats', []):
            if b.get('skipper_id'):
                boat_val.setdefault(b['skipper_id'], (b['skipper'], t['school'], b.get('strength')))
    srows = ''
    sk_rows = []
    for sid, (logits, won, tms) in by_sk.items():
        if sid not in boat_val or len(won) < 3:
            continue
        c, d = cells(logits, won)
        sk_rows.append((d, sid, c, tms))
    sk_rows.sort(key=lambda x: -x[0])
    for i, (d, sid, c, tms) in enumerate(sk_rows):
        nm, slug, val = boat_val[sid]
        h = _esc(nm)
        if sailor_href and nm:
            href = sailor_href(nm, slug)
            if href:
                h = f'<a href="{_esc(href)}">{h}</a>'
        school = school_link(school_name.get(slug, slug), slug) if school_link else _esc(school_name.get(slug, slug))
        srows += (f'      <tr class="row{i % 2}"><td style="text-align:left;white-space:nowrap">{h}</td><td class="schoolname" style="text-align:left">{school}</td>'
                  f'<td style="text-align:right">{val:.2f}</td><td style="text-align:right;font-weight:600">{val + d:.2f}</td>{c}</tr>\n' if val is not None else '')
    head = ('<th style="text-align:right">Record</th><th style="text-align:right" title="wins the pre-event forecast expected from these matches">Exp.<br>wins</th>'
            '<th style="text-align:right" title="performance minus pre-event strength">&Delta;</th>'
            '<th style="text-align:right" title="extra bits of surprise of the record at the pre-event strength, versus at the performance strength">Surprise<br>(bits)</th>'
            '<th style="text-align:right" title="chance, at the pre-event strengths, of this many wins or more in these matches">P(this good<br>or better)</th>')
    try:
        import help_tip
        tip = help_tip.tip('For each side, the performance strength is the value that would have made its record least surprising: the strength at which the '
                           'model&#39;s expected wins over exactly these opponents equal the actual wins, with every opponent fixed at its pre-event strength. '
                           'For a skipper, the same shift is applied only to their boat in the matches they sailed, with team-mates and opponents fixed; team-mates who '
                           'sailed every match together therefore get the same number. Surprise is in bits: -log2 of the probability of the record at the pre-event '
                           'strength minus the same at the performance strength. P(this good or better) treats matches as independent coin flips at the model&#39;s '
                           'pre-event probabilities, so it ignores form within the day; read it as a rough guide.', None, 'Team race surprisal', 'Surprisal')
    except Exception:
        tip = ''
    return f'''    <h3 style="margin:1em 0 0.3em">Surprisal: who beat their rating{tip}</h3>
    <p style="font-size:.9em;color:#444;max-width:80ch;margin:0 0 .6em">Pre-event strength against the strength that would have made each record least surprising,
    everything else fixed. &Delta; is in the model&#39;s logit units, the same scale as the boat values above (a side is three boats plus the school term);
    green is a clear over-performance, red a clear under-performance. Skippers need three or more matches.</p>
    <table class="results">
      <thead><tr><th style="text-align:right">Place</th><th style="text-align:left">School</th><th style="text-align:right">Strength</th>
        <th style="text-align:right" title="strength at which the record is least surprising">Perf.</th>{head}</tr></thead>
      <tbody>
{trows}      </tbody>
    </table>
    <h3 style="margin:1em 0 0.3em">By skipper</h3>
    <table class="results">
      <thead><tr><th style="text-align:left">Skipper</th><th style="text-align:left">School</th><th style="text-align:right" title="boat value before the event">Value</th>
        <th style="text-align:right" title="boat value at which the skipper&#39;s record is least surprising">Perf.</th>{head}</tr></thead>
      <tbody>
{srows}      </tbody>
    </table>'''


def regatta_panel(league: str, regatta_url: str, standings: list, school_link, sailor_href) -> str:
    """Ratings & Forecast tab for a sailed team race regatta.  `standings` rows: (place, school, school_slug, team, wins, losses).
    school_link(school, slug) -> html; sailor_href(name, school_slug) -> url."""
    u = regatta_url.rstrip('/')
    u = u.split('.org', 1)[-1] if '.org' in u else u
    fc = _forecasts(league).get(u)
    if not fc or not fc.get('teams'):
        return ''
    teams = {(t['school'], t['team']): t for t in fc['teams']}
    pred_rank = {k: i + 1 for i, k in enumerate(sorted(teams, key=lambda k: -teams[k]['exp_wins'] / max(teams[k]['n'], 1)))}
    rows = ''
    shown = set()
    order = [(place, school, slug, team) for place, school, slug, team, *_ in standings]
    order += [(None, t['school'], t['school'], t['team']) for t in fc['teams'] if (t['school'], t['team']) not in {(s, tm) for _, _, s, tm in order}]
    i = 0
    for place, school, slug, team in order:
        t = teams.get((slug, team)) or next((v for (s, tm), v in teams.items() if s == slug and (slug, tm) not in shown), None)
        if not t:
            continue
        shown.add((t['school'], t['team']))
        boats = []
        for b in t['boats']:
            sk = _esc(b['skipper'])
            if sailor_href and b['skipper']:
                sk = f'<a href="{_esc(sailor_href(b["skipper"], slug))}">{sk}</a>'
            cr = _esc(b['crew'])
            if sailor_href and b['crew']:
                cr = f'<a href="{_esc(sailor_href(b["crew"], slug))}">{cr}</a>'
            val = f'<span style="color:#555" title="what this boat brings: {b["strength"]:.2f} (skipper fleet rating {b["main"] if b["main"] is not None else "unrated"}, team race offset {b["offset"]:+.2f})">{b["strength"]:.2f}</span>' if b.get('offset') is not None else ''
            rec = f' <span style="color:#999;font-size:.85em">{b["wins"]}&ndash;{b["n"] - b["wins"]}</span>' if b['n'] != t['n'] else ''
            boats.append(f'{sk}' + (f' <span style="color:#777">/ {cr}</span>' if cr else '') + (f' {val}' if val else '') + rec)
        n = t['n']; wins = t['wins']; ew = t['exp_wins']
        pw = ew / n if n else 0
        diff = wins - ew
        dtxt = f'{diff:+.1f}'.replace('-', '&minus;')
        dcol = '#2e7d32' if diff > 1.5 else ('#c0392b' if diff < -1.5 else '#555')
        extra = []
        if abs(t.get('school_offset', 0)) >= 0.05:
            extra.append(f'school {t["school_offset"]:+.2f}'.replace('-', '&minus;'))
        if t.get('home'):
            extra.append(f'home +{fc["model"]["g_home"]:.2f}')
        name = school_link(school, slug) if school_link else _esc(school)
        rows += (f'      <tr class="row{i % 2}"><td style="text-align:right">{place if place is not None else "&mdash;"}</td>'
                 f'<td class="schoolname" style="text-align:left;vertical-align:top">{name}<br><span style="color:#777;font-size:.85em">{_esc(team)}</span></td>'
                 f'<td style="text-align:right;vertical-align:top">{pred_rank[(t["school"], t["team"])]}</td>'
                 f'<td style="text-align:right;vertical-align:top" title="sum of the three boats plus the school term (model logit scale)">{t["strength"]:.2f}'
                 + (f'<br><span style="color:#777;font-size:.8em">{", ".join(extra)}</span>' if extra else '') + '</td>'
                 f'<td style="font-size:.9em;line-height:1.4;text-align:left;vertical-align:top">{"<br>".join(boats)}</td>'
                 f'<td style="text-align:right;vertical-align:top">{pw * 100:.0f}%</td>'
                 f'<td style="text-align:right;vertical-align:top">{wins}&ndash;{n - wins} ({wins / n * 100:.0f}%)</td>'
                 f'<td style="text-align:right;vertical-align:top;color:{dcol}" title="wins minus the forecast&#39;s expected wins over the matches actually sailed">{dtxt}</td></tr>\n')
        i += 1
    # match list
    mrows = ''
    school_name = {sl: s for _, s, sl, _ in order}
    multi = len({sl for _, _, sl, _ in order}) < len(order)      # a school with several teams: show the team name too

    def side(slug, team):
        h = school_link(school_name.get(slug, slug), slug) if school_link else _esc(school_name.get(slug, slug))
        return h + (f' <span style="color:#777;font-size:.85em">{_esc(team)}</span>' if multi else '')

    for j, m in enumerate(sorted(fc['matches'], key=lambda m: (m['round'] or 0, m['match']))):
        upset = m['p'] < 0.5
        w = side(m['w'], m['wt']); l = side(m['l'], m['lt'])
        mrows += (f'      <tr class="row{j % 2}"><td style="text-align:right">{m["round"] if m["round"] else ""}</td>'
                  f'<td style="text-align:right">{m["match"]}</td><td style="text-align:left">{w}</td><td style="text-align:left">{l}</td>'
                  f'<td style="text-align:left;white-space:nowrap">{_pbar(m["p"], upset)}</td>'
                  f'<td style="text-align:left;color:#c0392b">{"upset" if upset else ""}</td></tr>\n')
    n_m = fc['n']; acc = fc['acc']; ll = fc['logloss']
    called = round(acc * n_m)
    date = fc.get('date', '')
    try:
        import help_tip
        tip = help_tip.tip('Each side is the sum of its three boats (the skipper&#39;s fleet rating times 0.72 plus a team race offset learned from their '
                           'past matches, plus the crew&#39;s offset and a small experience term) plus the school&#39;s team race strength and a home-water '
                           'edge for the host. The win probability is a logistic function of the difference. The forecast was made before the event: the '
                           'model was refitted on every match sailed before the first day, with fleet ratings as of the week before.',
                           None, 'How the forecast works', 'Team race forecast')
    except Exception:
        tip = ''
    verdict = ('well' if ll < 0.45 else 'about as expected' if ll < 0.6 else 'poorly')
    try:
        surprisal = surprisal_section(league, u, fc, school_link, sailor_href, order)
    except Exception as e:
        print(f'  team race surprisal failed for {u}: {e}')
        surprisal = ''
    order_txt = ''
    od = _tr_orderliness(league).get(u)
    if od:
        a, se, pct = od
        word = ('an orderly event' if a > se else 'an upset-heavy event' if a < -se else 'a typical event')
        col = '#1a8a3a' if a > se else '#c0392b' if a < -se else '#555'
        order_txt = (f' <b style="color:{col}">Orderliness {a:+.2f} &plusmn; {se:.2f}</b>: {word}, more orderly than {pct * 100:.0f}% of '
                     f'{"college" if league == "ICSA" else "high school"} team race events (temperature of the forecast log-odds that best fits the results, '
                     f'relative to the league average; nothing about the field, venue or wind predicts it, see the Weather tab).').replace('+-', '&minus;').replace(' -', ' &minus;')
    return f'''    <h3 style="margin:0.2em 0 0.3em">Pre-event forecast{tip}</h3>
    <p style="font-size:.9em;color:#444;max-width:80ch;margin:0 0 .6em">Fitted on the {fc['n_train']:,} team race matches sailed before {_esc(date)}, using the
    line-ups that actually raced. It called <b>{called} of {n_m}</b> matches ({acc * 100:.0f}%) with a log-loss of <b>{ll:.3f}</b>
    (a coin flip scores 0.693), so the event went {verdict} for the forecast.{order_txt} <b>vs forecast</b> is a team&#39;s wins minus the wins the model expected from the
    matches it sailed: positive means it beat its rating.</p>
    <table class="results">
      <thead><tr><th style="text-align:right">Place</th><th style="text-align:left">School</th>
        <th style="text-align:right" title="rank by predicted match win share">Pred.</th>
        <th style="text-align:right" title="side strength on the model&#39;s scale">Strength</th>
        <th style="text-align:left">Boats (skipper / crew, boat value)</th>
        <th style="text-align:right" title="expected share of its matches won">Pred. win %</th>
        <th style="text-align:right">Record</th><th style="text-align:right">vs forecast</th></tr></thead>
      <tbody>
{rows}      </tbody>
    </table>
{surprisal}
    <h3 style="margin:1em 0 0.3em">Match by match</h3>
    <p style="font-size:.85em;color:#666;margin:0 0 .4em">Probability the model gave the side that went on to win. Red bars are upsets (the winner was given under 50%).</p>
    <div style="max-height:520px;overflow-y:auto">
    <table class="results">
      <thead><tr><th style="text-align:right">Rd</th><th style="text-align:right">#</th><th style="text-align:left">Winner</th><th style="text-align:left">Loser</th>
        <th style="text-align:left">P(winner)</th><th></th></tr></thead>
      <tbody>
{mrows}      </tbody>
    </table>
    </div>'''


# ── sailor page block ───────────────────────────────────────────────────────

def _load() -> dict:
    if 'data' in _C:
        return _C['data']
    aliases: dict = {}
    try:
        with open(os.path.join(BASE, 'analysis', 'crew_ratings', 'id_aliases.csv'), newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                aliases[f'{r["league"]}|{r["name"].lower().strip()}|{r["school"].lower().strip()}'] = r['sailor_id']
    except OSError:
        pass
    by_sid: dict = {}; by_key: dict = {}
    for suffix in ('', '_issa'):
        try:
            with open(os.path.join(DIR, f'ratings_teamrace{suffix}.csv'), newline='', encoding='utf-8') as f:
                for r in csv.DictReader(f):
                    by_sid[(r['league'], r['sailor_id'])] = r          # a cluster id can span HS and college: key by league too
                    by_key[f'{r["league"]}|{r["name"].lower().strip()}|{r["school"].lower().strip()}'] = r
        except OSError:
            pass
    # season records and last season from the match list
    rec: dict = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0, set()]))   # sid -> season -> [sk wins, sk matches, cr wins, cr matches, regattas]
    try:
        with open(os.path.join(DIR, 'matches.csv'), newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                lg = r['league']
                for pre, won in (('w_', 1), ('l_', 0)):
                    for x in r[pre + 'skippers'].split(';'):
                        a = rec[(lg, x)][r['season']]; a[0] += won; a[1] += 1; a[4].add(r['regatta_url'])
                    for x in r[pre + 'crews'].split(';'):
                        if x:
                            a = rec[(lg, x)][r['season']]; a[2] += won; a[3] += 1; a[4].add(r['regatta_url'])
    except OSError:
        pass
    last = {sid: max(s, key=_season_order) for sid, s in rec.items()}
    seasons = sorted({s for s in last.values()}, key=_season_order)
    floor = _season_order(seasons[-ACTIVE_SEASONS]) if len(seasons) >= ACTIVE_SEASONS else (0, 0)
    ranks: dict = {}
    for lg in ('ICSA', 'ISSA'):
        pool = [r for r in by_sid.values() if r['league'] == lg and int(r['matches_skipper'] or 0) >= MIN_MATCHES
                and _season_order(last.get((lg, r['sailor_id']), '')) >= floor and r['tr_rating']]
        pool.sort(key=lambda r: -float(r['tr_rating']))
        for i, r in enumerate(pool):
            ranks[(lg, r['sailor_id'])] = (i + 1, len(pool))
    _C['data'] = dict(aliases=aliases, by_sid=by_sid, by_key=by_key, rec=rec, last=last, ranks=ranks)
    return _C['data']


def _history(league: str) -> dict:
    """sid -> [(date, season, regatta name, url, boat value, value from the fleet rating alone, offset, wins, n)] from the pre-event
    forecasts (regatta_forecasts.py): what the model made of the skipper the morning of each regatta they drove in."""
    key = ('hist', league)
    if key in _C:
        return _C[key]
    out: dict = defaultdict(list)
    for url, v in _forecasts(league).items():
        m = v.get('model', {})
        k = float(m.get('k', 0.72)); b0 = float(m.get('b0', 0.0))
        for t in v.get('teams', []):
            for b in t.get('boats', []):
                if b.get('offset') is None or not b.get('skipper_id'):
                    continue
                fleet = k * b['main'] if b.get('main') is not None else b0     # the same k x fleet rating + offset as the headline rating
                # keyed by name + school, not id: cluster ids are renumbered at every refresh and older forecasts keep the old ones
                out[(b['skipper'].lower().strip(), t['school'])].append((v['date'], v['season'], v['name'], url, fleet + b['offset'], fleet, b['offset'], b['wins'], b['n']))
    for sid in out:
        out[sid].sort()
    _C[key] = out
    return out


def _by_name(index: dict, league: str, sid: str) -> list:
    """Rows of a (name, school)-keyed index for this sailor id: the id's name and school from the ratings file, else any school."""
    r = _load()['by_sid'].get((league, sid))
    if not r:
        return []
    nm = r['name'].lower().strip()
    rows = [x for (n, _sch), v in index.items() if n == nm for x in v]
    return sorted(rows, key=lambda x: x[0])


def history_svg(league: str, sid: str, up: str = '../../') -> str:
    """Team race rating over time: the boat value before each regatta (line + dots, coloured by that regatta's record) against what the
    fleet rating alone would give (dashed), so the gap is the team race offset as it was learned."""
    import datetime as _dt
    pts = _by_name(_history(league), league, sid)
    if len(pts) < 3:
        return ''
    W, H, L, R, T, B = 640, 250, 58, 16, 14, 40
    d0 = _dt.date.fromisoformat(pts[0][0]); d1 = _dt.date.fromisoformat(pts[-1][0])
    span = max((d1 - d0).days, 120)
    pad = span * 0.03
    vals = [p[4] for p in pts] + [p[5] for p in pts]
    lo, hi = min(vals), max(vals)
    lo, hi = lo - 0.15, hi + 0.15

    def X(d: str) -> float:
        return L + ((_dt.date.fromisoformat(d) - d0).days + pad) / (span + 2 * pad) * (W - L - R)

    def Y(v: float) -> float:
        return H - B - (v - lo) / (hi - lo) * (H - T - B)

    g = []
    step = 0.5 if hi - lo > 1.6 else 0.25 if hi - lo > 0.8 else 0.1
    t = (int(lo / step) + 1) * step
    while t < hi:
        g.append(f'<line x1="{L}" y1="{Y(t):.1f}" x2="{W - R}" y2="{Y(t):.1f}" stroke="#e4e2da"/>'
                 f'<text x="{L - 6}" y="{Y(t) + 4:.1f}" text-anchor="end" style="font-size:11px;fill:#444">{t:g}</text>')
        t = round(t + step, 10)
    for yr in range(d0.year, d1.year + 2):
        d = _dt.date(yr, 1, 1)
        if d0 <= d <= d1:
            x = X(d.isoformat())
            g.append(f'<line x1="{x:.1f}" y1="{T}" x2="{x:.1f}" y2="{H - B}" stroke="#e4e2da" stroke-dasharray="2 3"/>')
    seen_s = set()
    for p in pts:
        if p[1] not in seen_s:
            seen_s.add(p[1])
            xs = [X(q[0]) for q in pts if q[1] == p[1]]
            g.append(f'<text x="{sum(xs) / len(xs):.1f}" y="{H - B + 16}" text-anchor="middle" style="font-size:11px;fill:#555">{_esc(_label(p[1]))}</text>')
    base = ' '.join(f'{"M" if i == 0 else "L"}{X(p[0]):.1f},{Y(p[5]):.1f}' for i, p in enumerate(pts))
    line = ' '.join(f'{"M" if i == 0 else "L"}{X(p[0]):.1f},{Y(p[4]):.1f}' for i, p in enumerate(pts))
    g.append(f'<path d="{base}" style="fill:none;stroke:#9a9890;stroke-width:1.5;stroke-dasharray:5 4"/>')
    g.append(f'<path d="{line}" style="fill:none;stroke:#2a78d6;stroke-width:2;stroke-linejoin:round"/>')
    for p in pts:
        date, season, rname, url, val, fleet, off, wins, n = p
        share = wins / n if n else 0.5
        col = '#2e7d32' if share >= 0.6 else '#c0392b' if share < 0.4 else '#2a78d6'
        tip = (f'{rname} ({date}): rating {val:.2f}, fleet rating alone {fleet:.2f}, offset {off:+.2f}; record {wins}-{n - wins} in this boat')
        g.append(f'<a href="{_esc(up)}{_esc(season)}/{_esc(url.rstrip("/").split("/")[-1])}/"><title>{_esc(tip)}</title>'
                 f'<circle cx="{X(date):.1f}" cy="{Y(val):.1f}" r="4.5" fill="{col}" stroke="#fff" stroke-width="1.2"/></a>')
    g.append(f'<text transform="translate(12,{(T + H - B) / 2:.1f}) rotate(-90)" text-anchor="middle" style="font-size:11px;fill:#555">Team race rating</text>')
    svg = (f'<svg viewBox="0 0 {W} {H}" style="width:100%;max-width:{W}px;height:auto;display:block" role="img" '
           f'aria-label="Team race rating before each regatta">' + ''.join(g) + '</svg>')
    return (f'<div style="margin-top:.6em">{svg}'
            '<p style="font-size:.8em;color:#666;max-width:80ch;margin:.3em 0 0">Rating before each regatta driven (solid; dots green when the boat won '
            '60% or more of its matches there, red under 40%) and what the fleet rating alone would give (dashed). The gap is the team race offset '
            'as it stood at the time. Hover a dot for the regatta; click to open it.</p></div>')


MIN_TRAIN_SURPRISAL = 1000     # forecasts fitted on fewer matches (the first season) are too rough to grade a sailor against


def _sailor_surprisal(league: str) -> dict:
    """sid -> [(date, season, name, url, role, n, wins, exp wins, shift d, flag, bits, P(>= wins), boat value)] per regatta: the same
    least-surprisal shift as surprisal_section, for the boat the sailor was in, in the matches they sailed."""
    import math
    key = ('ssurp', league)
    if key in _C:
        return _C[key]
    lineups = _lineups(league)
    names = {sid: r['name'].lower().strip() for (lg, sid), r in _load()['by_sid'].items() if lg == league}
    out: dict = defaultdict(list)
    for url, fc in _forecasts(league).items():
        if int(fc.get('n_train', 0) or 0) < MIN_TRAIN_SURPRISAL or not fc.get('matches'):
            continue
        temp = float(fc.get('model', {}).get('temperature', 1.0) or 1.0)
        lu_all = lineups.get(url, {})
        boat_val: dict = {}                                    # name -> boat value (ids in old forecasts are stale, see _history)
        for t in fc.get('teams', []):
            for b in t.get('boats', []):
                for who in ('skipper', 'crew'):
                    if b.get(who) and b.get('strength') is not None:
                        boat_val.setdefault(b[who].lower().strip(), b['strength'])
        acc: dict = defaultdict(lambda: ([], []))      # (sid, role) -> (logits, won)
        for m in fc['matches']:
            lu = lu_all.get(m['match'])
            if not lu:
                continue
            p = min(max(m['p'], 1e-6), 1 - 1e-6)
            lw = math.log(p / (1 - p))
            for sid in lu[0]:
                acc[(sid, 'skipper')][0].append(lw); acc[(sid, 'skipper')][1].append(1)
            for sid in lu[1]:
                acc[(sid, 'skipper')][0].append(-lw); acc[(sid, 'skipper')][1].append(0)
            for sid in lu[2]:
                acc[(sid, 'crew')][0].append(lw); acc[(sid, 'crew')][1].append(1)
            for sid in lu[3]:
                acc[(sid, 'crew')][0].append(-lw); acc[(sid, 'crew')][1].append(0)
        for (sid, role), (logits, won) in acc.items():
            if len(won) < 3:
                continue
            nm = names.get(sid, '')
            d, flag = _perf_shift(logits, won, temp)
            bits = max(0.0, _surprisal(logits, won, temp) - _surprisal(logits, won, temp, d))
            ps = [1 / (1 + math.exp(-l)) for l in logits]
            out[sid].append((fc['date'], fc['season'], fc['name'], url, role, len(won), sum(won), sum(ps), d, flag, bits,
                             _p_at_least(ps, sum(won)), boat_val.get(nm)))
    for sid in out:
        out[sid].sort(reverse=True)
    _C[key] = out
    return out


def surprisal_table(league: str, sid: str, up: str = '../../') -> str:
    """Sailor page: regatta-by-regatta team race surprisal with a career line."""
    import math
    rows = _sailor_surprisal(league).get(sid, [])
    if not rows:
        return ''
    n_all = sum(r[5] for r in rows); w_all = sum(r[6] for r in rows); e_all = sum(r[7] for r in rows)
    bits_all = sum(r[10] for r in rows)
    over = sum(1 for r in rows if r[11] <= 0.2); under = sum(1 for r in rows if r[11] >= 0.8)
    trs = ''
    for i, (date, season, rname, url, role, n, w, ew, d, flag, bits, p_ge, val) in enumerate(rows):
        dtxt = ('&ge;' if flag == '>=' else '&le;' if flag == '<=' else '') + f'{d:+.2f}'.replace('-', '&minus;')
        dcol = '#1a8a3a' if d >= 0.3 else ('#c0392b' if d <= -0.3 else '#555')
        pcol = '#1a8a3a' if p_ge <= 0.2 else ('#c0392b' if p_ge >= 0.8 else '#333')
        ptxt = '&lt;1%' if p_ge < 0.005 else ('&gt;99%' if p_ge > 0.995 else f'{p_ge * 100:.0f}%')
        slug = url.rstrip('/').split('/')[-1]
        link = f'<a href="{_esc(up)}{_esc(season)}/{_esc(slug)}/">{_esc(rname)}</a>'
        vtxt = f'{val:.2f}' if val is not None else '&mdash;'
        ptx = f'{val + d:.2f}' if val is not None else '&mdash;'
        trs += (f'      <tr class="row{i % 2}"><td style="text-align:left;white-space:nowrap">{_esc(date)}</td><td style="text-align:left">{link}</td>'
                f'<td style="text-align:left">{role}</td><td style="text-align:right">{vtxt}</td><td style="text-align:right;font-weight:600">{ptx}</td>'
                f'<td style="text-align:right">{w}&ndash;{n - w}</td><td style="text-align:right">{ew:.1f}</td>'
                f'<td style="text-align:right;color:{dcol};font-weight:600">{dtxt}</td><td style="text-align:right">{bits:.1f}</td>'
                f'<td style="text-align:right;color:{pcol}">{ptxt}</td></tr>\n')
    diff = f'{w_all - e_all:+.1f}'.replace('-', '&minus;')
    summary = (f'Over {len(rows)} graded regattas ({n_all} matches) the record is <b>{w_all}&ndash;{n_all - w_all}</b> against {e_all:.1f} wins the pre-event '
               f'forecasts expected (<b>{diff}</b>). {over} regatta{"s" if over != 1 else ""} clearly above the rating (P &le; 20%), {under} clearly below '
               f'(P &ge; 80%); {bits_all:.0f} bits of surprise in all.')
    try:
        import help_tip
        tip = help_tip.tip('For each regatta, the boat value that would have made this sailor&#39;s record least surprising: the shift applied to their boat alone, in '
                           'the matches they sailed, with team-mates and opponents fixed at the pre-event forecast. A boat&#39;s skipper and crew share the boat, so '
                           'they get the same number when they sailed every match together. Surprise is in bits at the pre-event value minus at the performance value; '
                           'P(this good or better) treats the matches as independent at the forecast probabilities. Regattas forecast from fewer than '
                           f'{MIN_TRAIN_SURPRISAL:,} training matches are left out.', None, 'Team race surprisal', 'Surprisal')
    except Exception:
        tip = ''
    return f'''
    <h3 style="margin:1em 0 0.3em">Team race surprisal: regatta by regatta{tip}</h3>
    <p style="font-size:.9em;color:#444;max-width:80ch;margin:0 0 .5em">{summary}</p>
    <div style="max-height:420px;overflow-y:auto">
    <table class="results">
      <thead><tr><th style="text-align:left">Date</th><th style="text-align:left">Regatta</th><th style="text-align:left">Role</th>
        <th style="text-align:right" title="boat value before the event">Value</th>
        <th style="text-align:right" title="boat value at which the record is least surprising">Perf.</th>
        <th style="text-align:right">Record</th><th style="text-align:right" title="wins the pre-event forecast expected">Exp.<br>wins</th>
        <th style="text-align:right" title="performance minus pre-event value">&Delta;</th>
        <th style="text-align:right" title="extra bits of surprise at the pre-event value">Surprise<br>(bits)</th>
        <th style="text-align:right" title="chance of this many wins or more at the pre-event probabilities">P(this good<br>or better)</th></tr></thead>
      <tbody>
{trs}      </tbody>
    </table>
    </div>'''


def sailor_block(league: str, name: str, school_name: str, school_slug: str = '') -> str:
    d = _load()
    key = f'{league}|{name.lower().strip()}|{school_name.lower().strip()}'
    sid = d['aliases'].get(key)
    r = d['by_sid'].get((league, sid)) if sid else None
    r = r or d['by_key'].get(key) or (d['by_key'].get(f'{league}|{name.lower().strip()}|{school_slug.lower()}') if school_slug else None)
    if not r:
        return ''
    sid = (r['league'], r['sailor_id'])
    n_sk, w_sk, n_cr, w_cr = (int(r[k] or 0) for k in ('matches_skipper', 'wins_skipper', 'matches_crew', 'wins_crew'))
    if n_sk + n_cr < 10:
        return ''
    rank = d['ranks'].get(sid)
    rank_txt = f'{rank[0]} of {rank[1]}' if rank else f'unranked (fewer than {MIN_MATCHES} matches as skipper, or not active)'
    tr = float(r['tr_rating']) if r['tr_rating'] else None
    off = float(r['offset']) if r['offset'] else 0.0
    se = float(r['offset_se']) if r['offset_se'] else None
    mr = float(r['main_rating']) if r['main_rating'] else None
    if se and abs(off) >= se:
        verdict = ('better' if off > 0 else 'worse') + ' at team racing than the fleet rating predicts'
    else:
        verdict = 'about what the fleet rating predicts'
    season_rows = ''
    for se_, a in sorted(d['rec'][sid].items(), key=lambda kv: _season_order(kv[0]), reverse=True):
        season_rows += (f'      <tr><td style="text-align:left">{_esc(_label(se_))}</td><td style="text-align:right">{len(a[4])}</td>'
                        f'<td style="text-align:right">{f"{a[0]}&ndash;{a[1] - a[0]}" if a[1] else "&mdash;"}</td>'
                        f'<td style="text-align:right">{f"{a[2]}&ndash;{a[3] - a[2]}" if a[3] else "&mdash;"}</td></tr>\n')
    try:
        import help_tip
        tip = help_tip.tip('A team race rating is what the sailor brings to one of the three boats: 0.72 x their fleet rating plus a team-race offset '
                           'learned from every match they sailed, shrunk toward zero so a few matches cannot move it far. Sides are compared by the sum '
                           'of their three boats plus the school&#39;s team race strength.', None, 'How this rating works', 'Team race ratings')
    except Exception:
        tip = ''
    parts = []
    if n_sk:
        parts.append(f'as skipper {w_sk}&ndash;{n_sk - w_sk} ({w_sk / n_sk:.0%})')
    if n_cr:
        parts.append(f'as crew {w_cr}&ndash;{n_cr - w_cr} ({w_cr / n_cr:.0%})')
    off_txt = (f'{off:+.2f} &plusmn; {se:.2f}' if se is not None else f'{off:+.2f}').replace('-', '&minus;')
    return f'''
  <div class="port" id="team-race-rating">
    <h3 style="margin-bottom:0.3em">Team Race Rating{tip}</h3>
    <table class="results">
      <thead><tr><th style="text-align:right">Rating</th>
        <th style="text-align:right" title="Among skippers with {MIN_MATCHES}+ matches who team raced in the last three seasons">Rank</th>
        <th style="text-align:right" title="Team race offset: the sailor&#39;s effect beyond what the fleet rating predicts, with its uncertainty">vs fleet rating</th>
        <th style="text-align:right">Record</th></tr></thead>
      <tbody>
      <tr><td style="text-align:right"><strong>{f"{tr:.2f}" if tr is not None else "&mdash;"}</strong>{f' <span style="color:#888;font-size:.85em">(fleet {mr:.2f})</span>' if mr is not None else ''}</td>
        <td style="text-align:right">{_esc(rank_txt)}</td>
        <td style="text-align:right">{off_txt}<br><span style="font-size:.85em;color:#666">{verdict}</span></td>
        <td style="text-align:right">{"<br>".join(parts)}</td></tr>
      </tbody>
    </table>
    <table class="results" style="margin-top:0.5em">
      <thead><tr><th style="text-align:left">Season</th><th style="text-align:right">Regattas</th>
        <th style="text-align:right">Skipper W&ndash;L</th><th style="text-align:right">Crew W&ndash;L</th></tr></thead>
      <tbody>
{season_rows}      </tbody>
    </table>{history_svg(league, sid[1])}{surprisal_table(league, sid[1])}
  </div>'''


# ── Team racing hub page (icsa/team-racing/, issa/team-racing/) ─────────────────────────────────────
HUB_CSS = '''
  <style>
    .mv{max-width:62em;margin:0 auto;padding:0 12px;font-size:0.95em;line-height:1.5;color:#0b0b0b;box-sizing:border-box}
    .mv p,.mv li{max-width:48em}
    .mv h2{font-family:Arial,Helvetica,sans-serif;font-size:1.1em;margin:1.9em 0 0.4em;color:#2f2f2e;border-bottom:1px solid #e1e0d9;padding-bottom:0.25em}
    .mv table.results{margin:0.6em 0}
    .mv table.results td,.mv table.results th{padding:0.3em 0.55em;font-variant-numeric:tabular-nums}
    .mv table.results th{text-align:right;vertical-align:bottom}
  </style>'''


def hub_html(league: str, P: str, site_dir: str, schools_csv: str, methodology_href: str = '') -> dict | None:
    """Content for the team racing hub: {'info': <li>s for #page-info, 'body': the .mv div}.  Numbers from
    analysis/blog/team_race_post.json (held-out evaluation, model parameters, plays), regatta_forecasts_<league>.json
    (recent regattas), data/orderliness_teamrace.csv and school_effects_teamrace[_issa].csv."""
    esc = html_module.escape
    try:
        with open(os.path.join(BASE, 'analysis', 'blog', 'team_race_post.json'), encoding='utf-8') as f:
            T = json.load(f)
    except (OSError, ValueError):
        return None
    hs = league == 'ISSA'
    if hs:
        V = T['forecasts']['issa']; m = T['forecasts']['model_issa']
    else:
        V = {v['key']: v for v in T['forecasts']['variants']}; m = T['forecasts']['model']
    best, main = V['best-lam30'], V['main']
    try:
        with open(os.path.join(BASE, 'analysis', 'team_race', f'regatta_forecasts_{league}.json'), encoding='utf-8') as f:
            fc = json.load(f)
    except (OSError, ValueError):
        fc = {}
    orderl = {}
    try:
        with open(os.path.join(BASE, 'data', 'orderliness_teamrace.csv'), newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                if r['league'] == league:
                    orderl[r['url'].rstrip('/')] = (float(r['actual']), float(r['se']))
    except OSError:
        pass
    names = {}
    try:
        with open(schools_csv, newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                names[r['slug']] = r['school']
    except OSError:
        pass
    min_matches = 300 if hs else 500
    schools = []
    try:
        with open(os.path.join(BASE, 'analysis', 'team_race', 'school_effects_teamrace' + ('_issa' if hs else '') + '.csv'), newline='', encoding='utf-8') as f:
            schools = [r for r in csv.DictReader(f) if r['league'] == league and int(r['matches']) >= min_matches]
    except OSError:
        pass
    schools.sort(key=lambda r: -float(r['offset']))
    seasons = sorted({v['season'] for v in fc.values()}, key=lambda s: (s[1:], s[0] == 'f'))
    recent = seasons[-2:]
    rows = ''
    for url, v in sorted(fc.items(), key=lambda kv: kv[1]['date'], reverse=True):
        if v['season'] not in recent or v.get('n', 0) < 10 or not os.path.exists(os.path.join(site_dir, url.strip('/'), 'index.html')):
            continue
        o = orderl.get(url.rstrip('/'))
        if o:
            col = '#2f7d4f' if o[0] > o[1] else ('#b3412f' if o[0] < -o[1] else '#333')
            o_txt = f'<span style="color:{col}">{o[0]:+.2f}</span> &plusmn; {o[1]:.2f}'
        else:
            o_txt = '&mdash;'
        rows += (f'<tr><td>{esc(v["date"])}</td><td style="text-align:left"><a href="{P}{url.strip("/")}/">{esc(v["name"])}</a></td>'
                 f'<td>{v["n"]}</td><td>{v["acc"] * 100:.0f}%</td><td>{v["logloss"]:.3f}</td><td>{o_txt}</td></tr>')
    sch = ''.join(f'<tr><td style="text-align:left"><a href="{P}schools/{esc(s["school_slug"])}/">{esc(names.get(s["school_slug"], s["school_slug"]))}</a></td>'
                  f'<td>{float(s["offset"]):+.2f} &plusmn; {float(s["offset_se"]):.2f}</td><td>{int(s["matches"]):,}</td><td>{float(s["win_share"]) * 100:.0f}%</td></tr>'
                  for s in schools[:10])
    O = T['orderliness'][league]; PL = T['plays']['leagues'][league]
    first_light = PL['by_breeze']['light']['first'] * 100
    heavy_key = 'heavy' if 'heavy' in PL['by_breeze'] else 'fresh'
    first_heavy = PL['by_breeze'][heavy_key]['first'] * 100
    plays_txt = (f'high school winners sweep 1-2-3 more as the breeze builds, the opposite of college, where winners hold first less and win through Play 2 and Play 4 more'
                 if hs else 'college winners win through Play 2 and Play 4 more as the breeze builds, high school winners sweep 1-2-3 more')
    meth = (f' Details and what did not help are in <a href="{methodology_href}">the methodology</a>.' if methodology_href else
            ' The model was developed and tested on college data; the high school fit uses the same structure, with crews often missing from the line-ups.')
    champ = (f' One thing does predict a little in high school: championships are more orderly (R&sup2; {O["champ_r2"]:+.2f}).' if hs and 'champ_r2' in O else '')
    info = (f'      <li><span class="page-info-key">Matches</span><span class="page-info-value">{m["matches"]:,} with full line-ups</span></li>\n'
            f'      <li><span class="page-info-key">Held-out forecast</span><span class="page-info-value">{best["acc"] * 100:.1f}% of {best["matches"]:,} matches called, log-loss {best["logloss"]:.3f}</span></li>')
    body = f'''  <div class="mv">
    <h2>What the site does with team racing</h2>
    <ul>
      <li><strong>Forecasts.</strong> Every team race regatta page has a <em>Ratings &amp; Forecast</em> tab: each side&#39;s pre-event strength and boat values, the forecast for every match, who beat it, and the event&#39;s measured orderliness.{'' if hs else ' Upcoming team races get a forecast of the round robin from each school&#39;s likely line-up.'}</li>
      <li><strong>Ratings.</strong> The <a href="{P}rankings/#teamrace">power rankings</a> have a Team Race selector: what each skipper brings to one of the three boats, and a school table that adds the three best skippers and the school&#39;s own team race term.</li>
      <li><strong>Sailor pages</strong> show a team race rating, its history before each regatta, and a regatta-by-regatta surprisal: the record against the expected record, and how surprising it was.</li>
      <li><strong>Plays.</strong> <a href="/icsa/lab/team-race-combinations/">An explorer</a> for how team races are won: the mix of finish sets, by play, as a side gets stronger, as one boat is raised, as a star is built, by breeze and level, with a high school toggle.</li>
    </ul>
    <p>Three notes explain the work: <a href="/blog/how-team-race-forecasts-work/">how team race forecasts work</a>,
    <a href="/blog/which-team-races-go-to-form/">which team races go to form</a> and
    <a href="/blog/play-1-play-2-play-4/">Play 1, Play 2, Play 4: how team races are won</a>.</p>

    <h2>How the forecast works, in one paragraph</h2>
    <p>A side&#39;s strength is the sum over its three boats of {m["k"]:.2f} times the skipper&#39;s fleet rating, plus a small
    team race offset for the skipper and the crew (shrunk toward zero; their fitted spread is only {m["offset_sd"]:.3f}
    and {m["crew_offset_sd"]:.3f}), plus the school&#39;s team race term (spread {m["school_sd"]:.2f}) and a home-water
    edge of {m["g_home"]:.2f} when the school hosts. The chance that one side beats the other is the logistic of the
    difference. Fleet ratings alone call {main["acc"] * 100:.1f}% of held-out matches; the full model calls
    {best["acc"] * 100:.1f}%.{meth}</p>

    <h2>Recent team race regattas</h2>
    <p>Forecast made before the event; <em>called</em> is the share of matches where the favourite won; orderliness
    is how much more (green) or less (red) the favourites won than the forecast said, with its standard error.
    Nothing known in advance predicts an event&#39;s orderliness (field, host and wind all fail), so it is a description, not a prediction.{champ}</p>
    <div style="overflow-x:auto"><table class="results" style="max-width:56em"><thead><tr><th>Date</th><th style="text-align:left">Regatta</th><th>Matches</th><th>Called</th><th>Log-loss</th><th>Orderliness</th></tr></thead>
    <tbody>{rows}</tbody></table></div>

    <h2>The school term</h2>
    <p>What a school adds to the sum of its three boats, in rating units, for schools with at least {min_matches} matches
    (top {min(10, len(schools))} of {len(schools)}). These programmes win more team races than their fleet ratings alone would predict, season after season.</p>
    <table class="results" style="max-width:40em"><thead><tr><th style="text-align:left">School</th><th>Team race term</th><th>Matches</th><th>Win share</th></tr></thead>
    <tbody>{sch}</tbody></table>

    <h2>How team races are won</h2>
    <p>Across {PL["n_all"]:,} {'high school' if hs else 'college'} matches, the winner held first place in
    {first_light:.0f}% of light-air wins and {first_heavy:.0f}% of {heavy_key}-air wins; {plays_txt}. A star with two weak boats wins
    the same way as three even boats. <a href="/icsa/lab/team-race-combinations/">Explore the mix of finish sets</a> or
    read <a href="/blog/play-1-play-2-play-4/">the note</a>.</p>
    <p style="font-size:0.9em;color:#555">Orderliness over {O["regattas"]} {league} team race regattas: true event-to-event spread {O["true_sd"]:.2f},
    split-half agreement r = {O["split_half"]:+.2f}, field R&sup2; {O["field_r2"]:+.2f}{f', wind r = {O["stated"]["r"]:+.2f}' if 'stated' in O else ''}.</p>
  </div>'''
    return {'info': info, 'body': body}
