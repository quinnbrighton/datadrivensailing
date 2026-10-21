#!/usr/bin/env python3
"""
generate_site.py — Build the entire static site into icsa/.

Reads exclusively from db/icsa.db (built by build_db.py from local CSVs).
No network requests.

Outputs:
  icsa/index.html             — homepage
  icsa/seasons/index.html     — all-seasons list
  icsa/{season}/index.html    — each season page

Usage:
    python3 generate_site.py           # full build
    python3 generate_site.py s26 f25   # specific seasons only (+ homepage)
"""

import csv as _csv
import re
import os, sys, shutil, sqlite3, html as html_module
from datetime import datetime as _dt, timedelta as _td
from website.weather_tab import weather_tab_html, WEATHER_CSS

BASE            = os.path.dirname(os.path.abspath(__file__))
DB_PATH         = os.path.join(BASE, 'db', 'icsa.db')
SITE            = os.path.join(BASE, 'website', 'icsa')
CSV_DIR         = os.path.join(BASE, 'data')
UPCOMING_CSV    = os.path.join(CSV_DIR, 'icsa_upcoming_regattas.csv')


# ── helpers ───────────────────────────────────────────────────────────────────

def esc(s):
    return html_module.escape(str(s) if s is not None else '')

def sailor_slug(name, school_slug):
    """Generate a URL slug for a sailor: name-slugified + school_slug."""
    name_part = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return f'{name_part}-{school_slug}'

def _parse_name(raw):
    """Strip asterisks and extract a trailing grad year like '29 from a name.
    Returns (clean_name, grade_or_empty)."""
    s = re.sub(r'\s+', ' ', (raw or '').replace('*', '').strip())
    m = re.search(r"\s+'(\d{2})\s*$", s)
    if m:
        return s[:m.start()].strip(), f"'{m.group(1)}"
    return s, ''

def season_label(code):
    prefix, yr = code[0], 2000 + int(code[1:])
    return ('Spring' if prefix == 's' else 'Fall') + f' {yr}'

def season_sort_key(code):
    yr = int(code[1:])
    return (yr, 0 if code[0] == 's' else 1)

def db():
    return sqlite3.connect(DB_PATH)


# ── queries ───────────────────────────────────────────────────────────────────

def all_seasons():
    """All season codes, newest first."""
    seasons = set()
    with db() as con:
        for (s,) in con.execute("SELECT DISTINCT season FROM fleet_results"):
            seasons.add(s)
        for (s,) in con.execute("SELECT DISTINCT season FROM tr_standings"):
            seasons.add(s)
        for (s,) in con.execute("SELECT DISTINCT season FROM keelboat_results"):
            seasons.add(s)
    return sorted(seasons, key=season_sort_key, reverse=True)


def _load_leading_schools(season_code):
    """regatta name (lower case) -> {url, school, slug} of the boat with the lowest total (see leading.py)."""
    import leading
    with db() as con:
        return leading.leaders(con, season_code)


def _week_sort_key(week_label):
    """Sort week labels numerically; Preweek sorts below Week 1 (value -N)."""
    import re
    label = (week_label or '').lower()
    m = re.search(r'(\d+)', label)
    n = int(m.group(1)) if m else 0
    return -n if 'preweek' in label else n


def season_regattas_by_week(season_code):
    """
    Load regattas from CSV, merge in leading school from DB.
    Returns ordered list of (week_label, [regatta_dicts]), most recent week first.
    Each regatta_dict has: name, host, type, scoring, start_date, status, url, school, slug
    """
    csv_path = os.path.join(CSV_DIR, 'icsa_regattas.csv')
    if not os.path.exists(csv_path):
        return []

    # Load CSV rows for this season, preserving order
    week_order = []   # week labels in first-seen order
    by_week = {}      # week_label -> [row, ...]
    with open(csv_path, newline='', encoding='utf-8') as f:
        for row in _csv.DictReader(f):
            if row['season'] != season_code:
                continue
            w = row['leading_week'] or ''
            if w not in by_week:
                week_order.append(w)
                by_week[w] = []
            by_week[w].append(row)

    # The outside championship is listed with its season's regattas, in the week it was sailed
    _ev = _away_event()
    if _ev.get('slug') and _ev.get('season') == season_code and by_week:
        _y, _m, _d = _ev['start'].split('-')
        _wk = next((w for w, rows_ in by_week.items() if any(r['start_date'] == f'{_m}/{_d}/{_y}' for r in rows_)), None)
        if _wk is not None:
            by_week[_wk].insert(0, {'season': season_code, 'name': _ev['short_title'], 'host': _ev.get('host', ''),
                                    'type': 'National Championship', 'scoring': 'Team Race',
                                    'start_date': f'{_m}/{_d}/{_y}', 'end_date': _ev['end'], 'status': 'Official',
                                    'leading_week': _wk, 'url': f'/{season_code}/{_ev["slug"]}/'})

    if not by_week:
        return []

    # Merge leading school from DB
    leads = _load_leading_schools(season_code)

    # Sort weeks descending (Week 18 first)
    week_order_sorted = sorted(week_order, key=_week_sort_key, reverse=True)

    result = []
    for week in week_order_sorted:
        regattas = []
        for row in by_week[week]:
            db_info = leads.get(row['name'].lower(), {})
            csv_url = row.get('url', '')
            # Strip domain from full URLs (e.g. https://scores.collegesailing.org/s26/foo -> /s26/foo)
            if csv_url.startswith('http'):
                from urllib.parse import urlparse as _urlparse
                csv_url = _urlparse(csv_url).path
            regattas.append({
                'name':       row['name'],
                'host':       row['host'],
                'type':       row['type'],
                'scoring':    row['scoring'],
                'start_date': row['start_date'],
                'status':     row['status'],
                'url':        db_info.get('url', '') or csv_url,
                'school':     db_info.get('school', ''),
                'slug':       db_info.get('slug', ''),
            })
        result.append((week, regattas))

    return result


def season_regattas(season_code):
    """Flat list of all regattas for a season (used for counts etc.)."""
    groups = season_regattas_by_week(season_code)
    return [r for _, regs in groups for r in regs]


def season_counts(season_code):
    """(regatta_count, entry_count) for a season."""
    with db() as con:
        urls = set()
        entries = set()

        for (url,) in con.execute(
            "SELECT DISTINCT regatta_url FROM fleet_results WHERE season=?", (season_code,)
        ):
            urls.add(url)

        for (url,) in con.execute(
            "SELECT DISTINCT regatta_url FROM tr_standings WHERE season=?", (season_code,)
        ):
            urls.add(url)

        for (url,) in con.execute(
            "SELECT DISTINCT regatta_url FROM keelboat_results WHERE season=?", (season_code,)
        ):
            urls.add(url)

        # Entries = distinct (regatta, school_slug) pairs across all tables
        for (url, slug) in con.execute(
            "SELECT DISTINCT regatta_url, school_slug FROM fleet_results WHERE season=?", (season_code,)
        ):
            entries.add((url, slug))

        for (url, slug) in con.execute(
            "SELECT DISTINCT regatta_url, school_slug FROM tr_standings WHERE season=?", (season_code,)
        ):
            entries.add((url, slug))

        for (url, slug) in con.execute(
            "SELECT DISTINCT regatta_url, school_slug FROM keelboat_results WHERE season=?", (season_code,)
        ):
            entries.add((url, slug))

    return len(urls), len(entries)



# ── HTML components ───────────────────────────────────────────────────────────

_BUILD_TIMESTAMP = _dt.now().strftime('%-I:%M %p, %B %-d, %Y')

NAV_TEMPLATE = """
<div class="nav">
  <h3>Navigate</h3>
  <ul>
    <li><a href="#menu">Menu</a></li>
    <li><a href="#page-content">Content</a></li>
  </ul>
</div>
<hr class="nav">

<div id="page-header">
  <div id="top-wrapper">
    <div id="last_updated"></div>
    <script src="/last_updated.js" defer></script>
  </div>
  <div id="menu-wrapper">
    <h3 class="nav">Menu</h3>
    <ul id="menu">
      <li><a href="{home}">Home</a></li>
      <li><a href="{schools}"{schools_style}>Schools</a></li>
      <li id="logo">
        <a href="{logo_href}"{logo_extra}>
          <img src="{logo}" alt="ICSA" width="145" height="92">
        </a>
      </li>
{current_li}      <li><a href="{sailors}"{sailors_style}>Sailors</a></li>
    </ul>
  </div>
</div>

<hr class="nav">
"""

def nav(home, schools, sailors, logo, current_href, current_label, logo_href=None, fantasy=None):
    if logo_href is None:
        logo_href = 'https://collegesailing.org'
        logo_extra = ' target="_blank" rel="noopener"'
    else:
        logo_extra = ''
    # Schools/Sailors sections: bold the fixed item instead of duplicating it
    schools_style, sailors_style, current_li = _nav_current(current_href, current_label)
    return NAV_TEMPLATE.format(
        home=home, schools=schools, sailors=sailors, logo=logo,
        logo_href=logo_href, logo_extra=logo_extra,
        schools_style=schools_style, sailors_style=sailors_style, current_li=current_li,
        build_timestamp=_BUILD_TIMESTAMP,
    )


def _nav_current(current_href, current_label):
    """Return (schools_style, sailors_style, current_li) for the menu.

    When the current section is Schools or Sailors, the permanent menu item is
    shown bold and no extra (duplicate) item is emitted.
    """
    label = (current_label or '').strip().lower()
    bold = ' style="font-weight:bold;"'
    if label == 'schools':
        return bold, '', ''
    if label == 'sailors':
        return '', bold, ''
    li = f'      <li><a href="{current_href}" style="font-weight:bold;">{esc(current_label)}</a></li>\n'
    return '', '', li


def other_seasons_ul(seasons_list, current_code):
    items = ''
    for s in seasons_list:
        href = 'index.html' if s == current_code else f'../{s}/index.html'
        items += f'      <li><a href="{href}">{esc(season_label(s))}</a></li>\n'
    return f'''  <div id="submenu-wrapper">
    <h3 class="nav">Other seasons</h3>
    <ul id="other-seasons">
{items}    </ul>
  </div>'''

def school_link(school, slug, prefix='../../'):
    """Wrap a school name in a link to its school page."""
    if not school or not slug:
        return esc(school) if school else '&mdash;'
    href = f'{prefix}schools/{esc(slug)}/'
    return f'<a href="{href}">{esc(school)}</a>'


def lead_cell(school, slug):
    if not school:
        return '&mdash;'
    logo = f'../icsa_logos/{slug}.png'
    return (
        f'<img src="{esc(logo)}" alt="{esc(school)}" '
        f'style="height:30px;width:auto;vertical-align:middle;" '
        f'onerror="this.style.display=\'none\'">'
    )


# ── page builders ─────────────────────────────────────────────────────────────

def build_season_page(season_code, seasons_list):
    label  = season_label(season_code)
    groups = season_regattas_by_week(season_code)
    nregs, nentries = season_counts(season_code)

    rows = ''
    row_idx = 0
    for week_label, regattas in groups:
        if week_label:
            rows += f'      <tr><th colspan="7">{esc(week_label)}</th></tr>\n'
        for r in regattas:
            slug = r['url'].strip('/').split('/')[-1] if r['url'] else ''
            href = f'{slug}/index.html' if slug else ''
            name_cell = (f'<a href="{esc(href)}">{esc(r["name"])}</a>'
                         if href else esc(r['name']))
            _k = _regatta_kind(r['url'] and ('/' + '/'.join(r['url'].rstrip('/').split('/')[-2:])), r.get('scoring', ''))
            if _k in ('singlehanded', 'keelboat'):
                name_cell += (f' <span class="badge badge-kind" style="font-size:.7em;background:#e8eefc;color:#1a4f8a;padding:1px 6px;'
                              f'border-radius:8px;white-space:nowrap">{"Singlehanded" if _k == "singlehanded" else "Keelboat"}</span>')
            rows += (
                f'      <tr class="row{row_idx % 2}">\n'
                f'        <td class="schoolname">{name_cell}</td>\n'
                f'        <td>{esc(r["host"])}</td>\n'
                f'        <td>{esc(r["type"])}</td>\n'
                f'        <td>{esc(r["scoring"])}</td>\n'
                f'        <td>{esc(r["start_date"])}</td>\n'
                f'        <td>{"<strong>Official</strong>" if r["status"].strip().lower() == "official" else esc(r["status"])}</td>\n'
                f'        <td class="schoolname">{lead_cell(r["school"], r["slug"])}</td>\n'
                f'      </tr>\n'
            )
            row_idx += 1

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(label)} | ICSA Real-Time Regatta Results</title>
  <link rel="stylesheet" href="../styles.css" media="screen,print">
</head>
<body>
{nav('../index.html', '../schools/index.html', '../sailors/index.html', '../img/icsa-logo-real.png',
     '../seasons/index.html', 'All Seasons', logo_href='../index.html')}
<div id="page-content">

  <div id="content-header">
    <h1>
      <span id="left-fill"></span>
      <span>{esc(label)}</span>
      <span id="right-fill"></span>
    </h1>
    <ul id="page-info">
      <li><span class="page-info-key">Number of Regattas</span><span class="page-info-value">{nregs}</span></li>
      <li><span class="page-info-key">Number of Entries</span><span class="page-info-value">{nentries}</span></li>
    </ul>
  </div>

  <div class="port">
    <h3>Season regattas</h3>
    <table class="season-summary">
      <thead>
        <tr>
          <th>Name</th>
          <th>Host</th>
          <th>Type</th>
          <th>Scoring</th>
          <th>Start Date</th>
          <th>Status</th>
          <th>Leading</th>
        </tr>
      </thead>
      <tbody>
{rows}      </tbody>
    </table>
  </div>

{other_seasons_ul(seasons_list, season_code)}

</div>
</body>
</html>
'''


def build_seasons_page(seasons_list):
    items = ''
    for s in seasons_list:
        nregs, _ = season_counts(s)
        items += (
            f'      <li>'
            f'<span class="page-info-key">{esc(season_label(s))}</span>'
            f'<span class="page-info-value"><a href="../{s}/index.html">{nregs} regattas</a></span>'
            f'</li>\n'
        )

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>All seasons | ICSA Real-Time Regatta Results</title>
  <link rel="stylesheet" href="../styles.css" media="screen,print">
</head>
<body>
{nav('../index.html', '../schools/index.html', '../sailors/index.html', '../img/icsa-logo-real.png',
     'index.html', 'All Seasons', logo_href='../index.html')}
<div id="page-content">

  <div id="content-header">
    <h1>
      <span id="left-fill"></span>
      <span>All Seasons</span>
      <span id="right-fill"></span>
    </h1>
    <ul id="page-info">
{items}    </ul>
  </div>

</div>
</body>
</html>
'''


def _today_et():
    """Today's date in US Eastern time, where the regattas are sailed."""
    try:
        from zoneinfo import ZoneInfo
        return _dt.now(ZoneInfo('America/New_York')).date()
    except Exception:
        return _dt.now().date()


def load_upcoming_regattas(latest_season):
    """Regattas that have not started yet and start within the next two weeks (an event under way is a recent regatta)."""
    today = _today_et()
    hi = today + _td(days=14)
    try:
        with open(UPCOMING_CSV, newline='', encoding='utf-8') as f:
            reader = _csv.DictReader(f)
            rows = []
            for r in reader:
                if r.get('season', '') != latest_season:
                    continue
                status = r.get('status', '').strip()
                if status.lower() == 'official' or status.lower().startswith('in progress'):
                    continue
                date_str = (r.get('start_date') or r.get('date', '')).strip()
                try:
                    reg_date = _dt.strptime(date_str, '%m/%d/%Y').date()
                except ValueError:
                    continue
                if today < reg_date <= hi:
                    rows.append(r)
            return rows
    except FileNotFoundError:
        return []


_ENTRY_NAMES = None


def _events_with_entries():
    """Lower-cased names of upcoming regattas that have at least one entry; only those get a page with anything on it."""
    global _ENTRY_NAMES
    if _ENTRY_NAMES is None:
        names = set()
        try:
            with open(os.path.join(CSV_DIR, 'icsa_upcoming_entries.csv'), newline='', encoding='utf-8') as f:
                for r in _csv.DictReader(f):
                    if r.get('school_slug'):
                        names.add((r.get('regatta') or '').strip().lower())
        except OSError:
            pass
        _ENTRY_NAMES = names
    return _ENTRY_NAMES


_PCT_TABLE = None


def _rating_percentile(rating, league='ICSA'):
    """Where a rating sits among rated skippers, from analysis/plackett_luce/rating_percentiles.json."""
    global _PCT_TABLE
    if _PCT_TABLE is None:
        import json as _json
        try:
            with open(os.path.join(BASE, 'analysis', 'plackett_luce', 'rating_percentiles.json'), encoding='utf-8') as f:
                _PCT_TABLE = _json.load(f)
        except (OSError, ValueError):
            _PCT_TABLE = {}
    tab = (_PCT_TABLE.get(league) or _PCT_TABLE.get('all') or {})
    if rating is None or not tab:
        return None
    pts = sorted((float(pc), float(v)) for pc, v in tab.items())
    if rating <= pts[0][1]:
        return pts[0][0]
    for (p0, v0), (p1, v1) in zip(pts, pts[1:]):
        if rating <= v1:
            f = (rating - v0) / (v1 - v0) if v1 > v0 else 0.0
            return p0 + f * (p1 - p0)
    return pts[-1][0]


def _regatta_skill(regatta_url, season, name, _cache={}):
    """(mean rating of the regatta's skippers going in, how many were rated) — how strong the fleet was."""
    if regatta_url in _cache:
        return _cache[regatta_url]
    from analysis.regatta_analysis import sailor_id, rating_before_after, regatta_week
    wn = regatta_week('ICSA', season, name) or 0
    with db() as con:
        rows = con.execute("SELECT DISTINCT skipper, skipper_grade, school_slug FROM fleet_results "
                           "WHERE regatta_url=? AND skipper IS NOT NULL AND skipper<>''", (regatta_url,)).fetchall()
        if not rows:
            rows = con.execute("SELECT DISTINCT skipper, skipper_grade, school_slug FROM tr_sailors "
                               "WHERE regatta_url=? AND skipper IS NOT NULL AND skipper<>''", (regatta_url,)).fetchall()
    vals = []
    for sk, gr, slug in rows:
        if not (sk and gr):
            continue
        prior, at = rating_before_after(sailor_id('ICSA', sk.strip(), slug or '', gr.strip()), wn)
        v = prior if prior is not None else at
        if v is not None:
            vals.append(v)
    out = (sum(vals) / len(vals), len(vals)) if vals else (None, 0)
    _cache[regatta_url] = out
    return out


_SCHOOL_TOP_RATINGS = None


def _school_top_ratings(league='ICSA'):
    """school slug -> that school's sailor ratings, best first.

    Keyed by slug on purpose.  The ratings file spells schools out in full ("Brown
    University") while the entry lists use the short form ("Brown"), so joining on the
    name matches almost nothing — the schools table is what reconciles the two.
    """
    global _SCHOOL_TOP_RATINGS
    if _SCHOOL_TOP_RATINGS is None:
        name_to_slug = {}
        try:
            with db() as con:
                for slug, school in con.execute('SELECT slug, school FROM schools'):
                    if slug and school:
                        name_to_slug[school.lower()] = slug
        except Exception:                      # noqa: BLE001 — no schools table, no skill column
            name_to_slug = {}
        table = {}
        try:
            with open(os.path.join('analysis', 'plackett_luce', 'pl_weekly_latest.csv'),
                      encoding='utf-8') as f:
                for row in _csv.DictReader(f):
                    if (row.get('league') or '').upper() != league:
                        continue
                    slug = name_to_slug.get((row.get('school') or '').lower())
                    if not slug:
                        continue
                    try:
                        table.setdefault(slug, []).append(float(row['rating']))
                    except (ValueError, KeyError):
                        pass
        except OSError:
            pass
        for vals in table.values():
            vals.sort(reverse=True)
        _SCHOOL_TOP_RATINGS = table
    return _SCHOOL_TOP_RATINGS


_UPCOMING_SKILL_TABLE = None


def _upcoming_skill_table():
    """regatta key -> (fleet skill, how many sailors it rests on, where it came from).

    The recent table measures a regatta by the mean rating of the skippers who sailed it.
    An upcoming regatta has no results, so the same quantity is estimated three ways, best
    first:

      "predicted lineups"  the sailors the selection model expects each school to bring.
                           Individual skippers, averaged — the same shape as the recent
                           number, so the two columns finally mean the same thing.
      "last year"          the mean rating of whoever sailed the previous edition.  Weaker,
                           since the field turns over, but grounded in what actually happened.
      "entered rosters"    each entered school's two best sailors, averaged.  A last resort:
                           schools enter B and C teams, so their top two are far stronger
                           than the crew they actually field.  This reads about two rating
                           points high, which pinned every good regatta at 99-100 when it
                           was the only method — Mystic Lake showed 100 against the recent
                           table's 86 for a comparable fleet.

    Keyed by results-site path where one exists and by normalised name otherwise, since
    regattas imported from a conference schedule have no URL yet.
    """
    global _UPCOMING_SKILL_TABLE
    if _UPCOMING_SKILL_TABLE is not None:
        return _UPCOMING_SKILL_TABLE

    import json as _json
    out = {}

    def _put(key, value, n, source):
        """First source wins: lineups are loaded before history, history before rosters."""
        if key and value is not None and key not in out:
            out[key] = (value, n, source)

    # ── 1. The sailors we expect to turn up ──────────────────────────────────────
    lineups = []
    try:
        with open(os.path.join(BASE, 'analysis', 'upcoming_lineups_icsa.json'),
                  encoding='utf-8') as f:
            lineups = _json.load(f) or []
    except (OSError, ValueError):
        lineups = []
    for rec in lineups:
        key = _norm_reg_key(rec.get('regatta', ''))
        vals = [p['rating'] for s in (rec.get('schools') or [])
                for p in (s.get('lineup') or []) if p.get('rating') is not None and p.get('role') in (None, 'driver')]
        if vals:
            _put(key, sum(vals) / len(vals), len(vals), 'predicted lineups')

    # ── 2. Failing that, the previous edition of the same regatta ────────────────
    for rec in lineups:
        key = _norm_reg_key(rec.get('regatta', ''))
        ly = rec.get('last_year') or {}
        if ly.get('mean') is not None:
            _put(key, ly['mean'], ly.get('n_sailors') or 0, 'last year')

    # ── 3. Failing that, the entered schools' rosters ────────────────────────────
    ratings = _school_top_ratings()
    per_reg = {}
    try:
        with open(os.path.join(CSV_DIR, 'icsa_upcoming_entries.csv'), encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                url = (row.get('regatta_url') or '').strip()
                if url.startswith('http'):
                    url = url.split('://')[-1].split('/', 1)[-1]
                path = '/' + url.strip('/') if url else ''
                nk = _norm_reg_key(row.get('regatta', ''))
                for key in {path, nk}:
                    if key:
                        per_reg.setdefault(key, []).append(row.get('school_slug', ''))
    except OSError:
        pass
    for key, slugs in per_reg.items():
        vals = []
        for slug in set(slugs):
            top2 = ratings.get(slug, [])[:2]
            if top2:
                vals.append(sum(top2) / len(top2))
        if vals:
            _put(key, sum(vals) / len(vals), len(vals), 'entered rosters')

    # A results-site path and a name are two ways of naming one regatta; make the better
    # estimate reachable from either.
    try:
        with open(os.path.join(CSV_DIR, 'icsa_upcoming_entries.csv'), encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                url = (row.get('regatta_url') or '').strip()
                if url.startswith('http'):
                    url = url.split('://')[-1].split('/', 1)[-1]
                path = '/' + url.strip('/') if url else ''
                nk = _norm_reg_key(row.get('regatta', ''))
                if not (path and nk):
                    continue
                rank = {'predicted lineups': 0, 'last year': 1, 'entered rosters': 2}
                here, there = out.get(path), out.get(nk)
                # Both keys name one regatta, so both should carry the best estimate we
                # have.  Absence is not the only case to fix: the roster pass fills in the
                # path key, so without comparing sources a regatta with a results-site URL
                # would keep the roster number even though its lineup estimate exists —
                # and the lookup tries the path first.
                if there and (not here or rank.get(there[2], 9) < rank.get(here[2], 9)):
                    out[path] = there
                elif here and (not there or rank.get(here[2], 9) < rank.get(there[2], 9)):
                    out[nk] = here
    except OSError:
        pass

    _UPCOMING_SKILL_TABLE = out
    return out


_LAST_YEAR_TABLE = None


def _last_year_table():
    """regatta key -> (mean rating of last year's fleet, how many sailors, which edition).

    The lineup predictor already matches each upcoming regatta to its previous edition and
    records the ratings of whoever sailed it.  That is a different thing from the skill
    estimate — it is what the regatta actually drew last time, not what we expect this
    time — so it is shown alongside rather than folded in.  Useful for spotting an event
    that historically pulls a strong fleet, and for sanity-checking a prediction that looks
    out of line with the regatta's history.
    """
    global _LAST_YEAR_TABLE
    if _LAST_YEAR_TABLE is None:
        import json as _json
        table = {}
        try:
            with open(os.path.join(BASE, 'analysis', 'upcoming_lineups_icsa.json'),
                      encoding='utf-8') as f:
                for rec in _json.load(f) or []:
                    ly = rec.get('last_year') or {}
                    if ly.get('mean') is None:
                        continue
                    name = rec.get('regatta', '')
                    matched = ly.get('matched_regatta') or ''
                    # The matcher pairs each regatta with its previous edition by name, and
                    # mostly gets it right (Conn College Invite -> Connecticut College
                    # Invite, ILCA North -> Laser North).  It does slip: the SEISA Men's
                    # Singlehanded matched the SEISA Fall Women's Fleet Race Championship.
                    # Showing a women's fleet race as a men's singlehanded event's history
                    # is worse than showing nothing, so drop a match whose gender disagrees.
                    if ('women' in name.lower()) != ('women' in matched.lower()):
                        continue
                    table[_norm_reg_key(name)] = (
                        ly['mean'], ly.get('n_sailors') or 0, matched)
        except (OSError, ValueError):
            pass
        _LAST_YEAR_TABLE = table
    return _LAST_YEAR_TABLE


def _norm_reg_key(name):
    """Loose regatta-name key, for entries that carry no URL to join on."""
    import re as _re
    s = (name or '').lower()
    s = _re.sub(r"\b(20\d\d|'\d\d|trophy|regatta|invite|invitational|championships?|"
                r"champs?|the|open)\b", ' ', s)
    return _re.sub(r'[^a-z0-9]', '', s)


_REGATTA_TIER_TABLE = None


def _regatta_tier_table():
    """normalised regatta name -> its published rank, from the conference schedule.

    NEISA ranks every event on its schedule: A / A- / B / B- / C for the open circuit,
    WA / WA- / WSA for the women's, SA for the open singlehanded championships.  The rank
    is the conference's own statement of how strong an event is meant to be, and schools
    send their first team to an A and their third to a C — so it says something the entry
    list alone does not.  Regattas outside that schedule (every other conference) have no
    published rank and are shown as UNR rather than left blank, so the column reads as
    "we have no rank for this" instead of looking like missing data.
    """
    global _REGATTA_TIER_TABLE
    if _REGATTA_TIER_TABLE is None:
        import json as _json
        table = {}
        try:
            with open(os.path.join(CSV_DIR, 'neisa_schedule_meta.json'), encoding='utf-8') as f:
                for key, meta in (_json.load(f) or {}).items():
                    tier = (meta.get('pr_tier') or '').strip()
                    if tier:
                        table[key] = tier
        except (OSError, ValueError):
            pass

        # The schedule keys events under the conference's name for them, but a regatta we
        # already had keeps OUR name — "Toni Deutsch" there against "Toni Deutsch '58
        # Trophy 2026" here — so those would all read UNR despite being ranked.  The
        # reviewed mapping file pairs the two spellings; index each tier under our name too.
        try:
            with open(os.path.join(CSV_DIR, 'neisa_schedule_map.json'), encoding='utf-8') as f:
                for row in (_json.load(f) or {}).get('regattas') or []:
                    ours = (row.get('our_name') or '').strip()
                    tier = (row.get('pr') or '').strip()
                    if not ours:
                        continue
                    sheet_key = _norm_reg_key(row.get('sheet_name', ''))
                    tier = tier or table.get(sheet_key, '')
                    if tier:
                        table.setdefault(_norm_reg_key(ours), tier)
        except (OSError, ValueError):
            pass
        _REGATTA_TIER_TABLE = table
    return _REGATTA_TIER_TABLE


def _upcoming_path(url, latest):
    """Normalise a regatta URL (absolute or relative) to a leading-slash path."""
    url = (url or '').strip()
    if not url:
        return ''
    if url.startswith('http'):
        url = url.split('://')[-1].split('/', 1)[-1]
    return '/' + url.strip('/')



def _schedule_sources_html():
    """Where the preliminary race schedules come from: every sheet in data/schedule_sources.csv marked expose_home=1.
    Add a row there (and run analysis/import_sheet_schedules.py) and it appears on the ICSA homepage at the next build."""
    path = os.path.join(BASE, 'data', 'schedule_sources.csv')
    try:
        with open(path, newline='', encoding='utf-8') as f:
            rows = [r for r in _csv.DictReader(f) if (r.get('expose_home') or '0').strip() == '1']
    except OSError:
        return ''
    if not rows:
        return ''
    links = ' &middot; '.join(
        f'<a href="{html_module.escape(r["url"], quote=True)}" target="_blank" rel="noopener" '
        f'title="{html_module.escape(r["label"], quote=True)}">{html_module.escape(r["conference"])}</a>'
        for r in rows)
    return ('<p class="schedule-sources" style="font-size:0.9em;color:#555;">Preliminary race schedules and entry lists '
            'come from the conferences&#39; published schedule sheets: ' + links + '.</p>')


def build_homepage(seasons_list):
    latest       = seasons_list[0]
    latest_label = season_label(latest)

    # ── Upcoming regattas (shown first if any exist) ──────────────────────────
    upcoming = load_upcoming_regattas(latest)
    upcoming_paths = set()
    skill_table = _upcoming_skill_table()
    MIN_RATED_SCHOOLS = 3

    for r in upcoming:
        path = _upcoming_path(r.get('url', ''), latest)
        if path:
            upcoming_paths.add(path)
        avg, n_rated, source = skill_table.get(path) or \
                               skill_table.get(_norm_reg_key(r.get('name', '')), (None, 0, ''))
        # A roster estimate counts schools, the other two count sailors, so the floor
        # differs: three rated schools, or four sailors, before the number means anything.
        floor = MIN_RATED_SCHOOLS if source == 'entered rosters' else 4
        r['_skill']   = avg if (avg is not None and n_rated >= floor) else None
        r['_skill_n'] = n_rated
        r['_skill_src'] = source
        r['_lastyr'] = _last_year_table().get(_norm_reg_key(r.get('name', '')))

    def _upcoming_start_key(r):
        try:
            return _dt.strptime((r.get('start_date') or r.get('date', '')).strip(),
                                '%m/%d/%Y').date()
        except (ValueError, TypeError):
            return _dt.max.date()

    # Strongest field first, matching the recent table.  Regattas whose entries are not
    # rated yet keep their place at the end, ordered by start date so the list still
    # reads as a schedule down there.
    upcoming.sort(key=lambda r: (r['_skill'] is not None,
                                 r['_skill'] if r['_skill'] is not None else 0.0),
                  reverse=True)
    unrated = [r for r in upcoming if r['_skill'] is None]
    unrated.sort(key=_upcoming_start_key)
    upcoming = [r for r in upcoming if r['_skill'] is not None] + unrated

    upcoming_rows = ''
    for idx, r in enumerate(upcoming):
        url    = r.get('url', '').strip()
        date   = r.get('start_date') or r.get('date', '')
        name   = r.get('name', '')
        # Every upcoming regatta links now: scheduled ones get a page of their own built
        # from the entry list and the predicted line-ups (build_scheduled_regatta_page),
        # which the real results overwrite once published.
        # Linked only when there is something on the page (an entry list); an event with no entries yet is plain text, not a dead link.
        if url and (name.strip().lower() in _events_with_entries() or _upcoming_path(url, latest) in _upcoming_entries_by_url()):
            slug = url.rstrip('/').split('/')[-1]
            season_code = url.rstrip('/').split('/')[-2] if '/' in url.rstrip('/') else latest
            href = f'{season_code}/{slug}/index.html'
            name_cell = f'<a href="{esc(href)}">{esc(name)}</a>'
        else:
            name_cell = esc(name)
        pct = _rating_percentile(r['_skill']) if r['_skill'] is not None else None
        _src = r.get('_skill_src') or ''
        _unit = 'rated schools' if _src == 'entered rosters' else 'sailors'
        skill_cell = ('&mdash;' if pct is None else
                      f'<span title="How strong the fleet is, as a percentile among rated '
                      f'ICSA skippers &mdash; mean rating over {r["_skill_n"]} {_unit}, '
                      f'from {_src}">{pct:.0f}</span>')
        tier = _regatta_tier_table().get(_norm_reg_key(r.get('name', '')), '')
        tier_cell = (f'<span title="Rank published on the conference schedule">{esc(tier)}</span>'
                     if tier else
                     '<span title="Unranked: this regatta is not on a schedule we have" '
                     'style="opacity:.5">UNR</span>')
        _ly = r.get('_lastyr')
        if _ly and _ly[0] is not None:
            _ly_pct = _rating_percentile(_ly[0])
            ly_cell = (f'<span title="How strong the fleet was the last time this regatta '
                       f'sailed: {esc(_ly[2])}, {_ly[1]} sailors, mean rating '
                       f'{_ly[0]:.2f}">{_ly_pct:.0f}</span>')
        else:
            ly_cell = '<span title="No previous edition matched in our data" style="opacity:.5">&mdash;</span>'
        upcoming_rows += (
            f'        <tr class="row{idx % 2}">\n'
            f'          <td class="schoolname">{name_cell}</td>\n'
            f'          <td>{esc(r.get("host",""))}</td>\n'
            f'          <td>{esc(r.get("type",""))}</td>\n'
            f'          <td>{esc(r.get("scoring",""))}</td>\n'
            f'          <td>{esc(date)}</td>\n'
            f'          <td>{tier_cell}</td>\n'
            f'          <td>{skill_cell}</td>\n'
            f'          <td>{ly_cell}</td>\n'
            f'        </tr>\n'
        )

    # ── Recent regattas: everything that has started (in progress, finished but not yet official, official) ──────────
    today = _today_et()

    def _started(r):
        try:
            return _dt.strptime(r['start_date'], '%m/%d/%Y').date() <= today
        except (ValueError, TypeError, KeyError):
            return (r.get('status') or '').strip().lower() == 'official'

    MIN_LINKED = 12
    groups = season_regattas_by_week(latest)
    recent_groups = []
    linked_count = 0
    for week_label, regattas in groups:
        shown = [r for r in regattas if r['url'] and _started(r)]
        if not shown:
            continue                      # a week of events still to come
        recent_groups.append((week_label, regattas))
        linked_count += len(shown)
        if linked_count >= MIN_LINKED:
            break
    if linked_count < MIN_LINKED and len(seasons_list) > 1:
        prior_groups = season_regattas_by_week(seasons_list[1])
        if prior_groups:
            recent_groups.append(prior_groups[0])

    # Flatten and sort by how strong the fleet was (mean skipper rating going in), best first
    MIN_RATED = 3
    recent_list = [r for _wk, regattas in recent_groups for r in regattas
                   if r['url'] and ('/' + r['url'].strip('/')) not in upcoming_paths and (_started(r) or not r['start_date'])]
    for r in recent_list:
        season_code = r['url'].strip('/').split('/')[0]
        avg, n_rated = _regatta_skill(r['url'], season_code, r['name'])
        r['_skill'] = avg if n_rated >= MIN_RATED else None
        r['_skill_n'] = n_rated

    def _start_key(r):
        try:
            return _dt.strptime(r['start_date'], '%m/%d/%Y').date()
        except (ValueError, TypeError):
            return _dt.min.date()

    recent_list.sort(key=lambda r: (r['_skill'] is not None, r['_skill'] if r['_skill'] is not None else 0.0,
                                    _start_key(r)), reverse=True)

    recent_rows = ''
    row_idx = 0
    if True:
        for r in recent_list:
            season_code = r['url'].strip('/').split('/')[0]
            reg_slug    = r['url'].strip('/').split('/')[-1]
            href = f'{season_code}/{reg_slug}/index.html'
            logo = f'icsa_logos/{r["slug"]}.png'
            pct = _rating_percentile(r['_skill']) if r['_skill'] is not None else None
            skill_cell = ('&mdash;' if pct is None else
                          f'<span title="Mean rating of the {r["_skill_n"]} rated skippers entered, '
                          f'as a percentile among rated ICSA skippers">{pct:.0f}</span>')
            recent_rows += (
                f'        <tr class="row{row_idx % 2}">\n'
                f'          <td class="schoolname"><a href="{esc(href)}">{esc(r["name"])}</a></td>\n'
                f'          <td>{esc(r["type"])}</td>\n'
                f'          <td>{esc(r["scoring"])}</td>\n'
                f'          <td>{"<strong>Official</strong>" if r["status"].strip().lower() == "official" else esc(r["status"].strip() or "In progress")}</td>\n'
                f'          <td>{skill_cell}</td>\n'
                f'          <td class="schoolname">'
                f'<img src="{esc(logo)}" alt="{esc(r["school"])}" '
                f'style="height:30px;width:auto;vertical-align:middle;" '
                f'onerror="this.style.display=\'none\'">'
                f'</td>\n'
                f'        </tr>\n'
            )
            row_idx += 1

    skill_th = ('            <th><abbr title="How strong the fleet was: the mean rating of the skippers entered, '
                'as a percentile among rated ICSA skippers">Fleet skill</abbr></th>\n')
    recent_panel = f'''    <div id="in-progress">
      <h1>
        <span class="left-fill"></span>
        <span>Recent regattas</span>
        <span class="right-fill"></span>
      </h1>
      <table class="season-summary">
        <thead>
          <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Scoring</th>
            <th>Status</th>
{skill_th}            <th>Leading</th>
          </tr>
        </thead>
        <tbody>
{recent_rows}        </tbody>
      </table>
    </div>
''' if recent_rows else ''

    upcoming_panel = f'''  <div class="port">
    <h3 class="nav">Upcoming schedule</h3>
    <table class="coming-regattas">
      <thead>
        <tr>
          <th>Name</th>
          <th>Host</th>
          <th>Type</th>
          <th>Scoring</th>
          <th>Start date</th>
          <th><abbr title="Rank published on the conference schedule: A/A-/B/B-/C on the open circuit, WA/WA-/WSA for women's, SA for singlehanded championships. UNR = no published rank.">Rank</abbr></th>
          <th><abbr title="How strong the fleet is expected to be, as a percentile among rated ICSA skippers. From the sailors we predict each school will bring, falling back to last year's fleet, then to entered rosters.">Fleet skill</abbr></th>
          <th><abbr title="How strong the fleet was the last time this regatta sailed, on the same percentile scale. A dash means we found no previous edition.">Last year</abbr></th>
        </tr>
      </thead>
      <tbody>
{upcoming_rows}      </tbody>
    </table>
  </div>
''' if upcoming_rows else ''

    season_links = ''
    for s in seasons_list:
        season_links += f'        <li><a href="{s}/index.html">{esc(season_label(s))}</a></li>\n'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Scores | ICSA Real-Time Regatta Results</title>
  <link rel="stylesheet" href="styles.css" media="screen,print">
</head>
<body>

{nav('../index.html', 'schools/index.html', 'sailors/index.html', 'img/icsa-logo-real.png', f'{latest}/index.html', latest_label, fantasy='fantasy/index.html')}

<div id="page-content">

  <div id="message-container">
    <div id="welcome">
      <h1>
        <span class="left-fill"></span>
        <span>Welcome</span>
        <span class="right-fill"></span>
      </h1>
      <p>
        Welcome to Data Driven Sailing, home of real-time results of College Sailing regattas.
        This site contains scores and participation records for all fleet-racing and
        team-racing events within ICSA (Intercollegiate Sailing Association).
        An archive of all previous seasons is also available.
      </p>
      <p style="font-size:0.9em;color:#555;">
        Data Driven Sailing mirrors the Techscore layout for ease of navigation, but it is an
        independent, unofficial site with no affiliation to Techscore, ICSA or the host schools. Results here are pulled from Techscore
        and may lag or differ from it &mdash; use
        <a href="https://scores.collegesailing.org" target="_blank" rel="noopener">Techscore</a> for official results.
      </p>
      {_schedule_sources_html()}
      <div class="issa-cta">
        <a href="../issa/index.html" class="issa-button">
          <img src="img/issa-logo.png" alt="ISSA" style="height:36px;width:auto;vertical-align:middle;margin-right:0.5em;">
          <span>ISSA High School Sailing</span>
        </a>
      </div>
      <div class="issa-cta" style="margin-top:0.5em;">
        <a href="rankings/index.html" class="issa-button">
          <img src="img/icsa-logo-real.png" alt="ICSA" style="height:36px;width:auto;vertical-align:middle;margin-right:0.5em;">
          <span>ICSA Power Rankings</span>
        </a>
      </div>
      <div class="issa-cta" style="margin-top:0.5em;">
        <a href="boat-speed/index.html" class="issa-button">
          <span>Fleet Race - Boat Speed Analyzer</span>
        </a>
      </div>
      <div class="issa-cta" style="margin-top:0.5em;">
        <a href="team-racing/index.html" class="issa-button">
          <span>Team Racing - Forecasts, Ratings &amp; Plays</span>
        </a>
      </div>
      <div class="issa-cta" style="margin-top:0.5em;">
        <a href="../blog/" class="issa-button">
          <span>Notes - What the Data Shows</span>
        </a>
      </div>
      <div class="issa-cta" style="margin-top:0.5em;">
        <a href="fantasy/index.html" class="issa-button">
          <span>⛵ Fantasy Sailing</span>
        </a>
      </div>
    </div>

{recent_panel}  </div>

{upcoming_panel}
  <div id="submenu-wrapper">
    <h3 class="nav">Other seasons</h3>
    <ul id="other-seasons">
{season_links}    </ul>
  </div>

</div>
</body>
</html>
'''


def _methodology_ready():
    """True once evaluate_models.py has written final results (including the
    championship test).  Gates the methodology page and every link to it, so a
    partial evaluation never gets published or linked."""
    import json as _json
    path = os.path.join(BASE, 'analysis', 'plackett_luce', 'eval', 'results.json')
    try:
        with open(path, encoding='utf-8') as f:
            return bool(_json.load(f).get('championships'))
    except (OSError, ValueError):
        return False


def _methodology_li(href):
    return f'      <li><a href="{href}">How accurate are these ratings?</a></li>\n' if _methodology_ready() else ''


def _methodology_note(prefix):
    return f' (<a href="{prefix}methodology/">how accurate it is</a>)' if _methodology_ready() else ''


METHODOLOGY_CSS = '''
  <style>
    .mv{max-width:62em;margin:0 auto;padding:0 12px;font-size:0.95em;line-height:1.5;color:#0b0b0b;box-sizing:border-box}
    .mv p,.mv li{max-width:48em}
    .mv h2{font-family:Arial,Helvetica,sans-serif;font-size:1.1em;margin:1.9em 0 0.4em;color:#2f2f2e;
      border-bottom:1px solid #e1e0d9;padding-bottom:0.25em}
    .mv-lede{font-size:1.05em}
    .mv-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(10em,1fr));gap:0.6em;margin:1.1em 0 0.4em}
    .mv-tile{border:1px solid rgba(11,11,11,0.10);border-radius:6px;padding:0.7em 0.9em;background:#fcfcfb}
    .mv-tile-label{font-size:0.85em;color:#52514e}
    .mv-tile-value{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;font-size:1.95em;font-weight:600;
      line-height:1.2;margin:0.12em 0}
    .mv-tile-delta{font-size:0.85em;color:#52514e}
    .mv-figure{margin:0.6em 0 1.3em}
    .mv-figure figcaption{font-size:0.9em;color:#52514e;margin-bottom:0.35em}
    .mv-svg{width:100%;height:auto;max-width:600px;display:block}
    .mv-grid{stroke:#e1e0d9;stroke-width:1}
    .mv-axis{stroke:#c3c2b7;stroke-width:1}
    .mv-ref{stroke:#898781;stroke-width:1}
    .mv-tick{font-size:11px;fill:#898781;font-variant-numeric:tabular-nums}
    .mv-label{font-size:13px;fill:#52514e}
    .mv-val{font-size:12px;fill:#52514e;font-variant-numeric:tabular-nums}
    .mv-hl{fill:#0b0b0b;font-weight:600}
    .mv-dot{fill:#a3a29c;stroke:#ffffff;stroke-width:2}
    .mv-dot-hl{fill:#2a78d6}
    .mv-ci{stroke:#a3a29c;stroke-width:2;stroke-linecap:round}
    .mv-ci-hl{stroke:#2a78d6}
    .mv-line{fill:none;stroke:#2a78d6;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
    .mv-hit{fill:transparent}
    .mv-row:hover .mv-hit{fill:#f3f2ee}
    .mv-pt:hover .mv-dot-hl{stroke:#2a78d6;stroke-opacity:0.25;stroke-width:6}
    .mv-axis-title{font-size:12px;fill:#52514e}
    .mv-note{font-size:11px;fill:#898781}
    .mv table.results{margin:0.6em 0}
    .mv table.results td,.mv table.results th{padding:0.3em 0.55em;font-variant-numeric:tabular-nums}
    .mv table.results th{text-align:right;vertical-align:bottom}
    .mv table.results th:first-child,.mv table.results td:first-child{text-align:left}
    .mv tr.mv-site td{font-weight:bold;background:#eef4fc}
    .mv td.mv-muted{color:#898781}
    .mv details{margin:0.2em 0 1.2em}
    .mv summary{cursor:pointer;color:#3465A4}
    .mv code,.mv pre{background:#f3f2ee;border-radius:3px;font-size:0.9em}
    .mv code{padding:0.1em 0.35em}
    .mv pre{padding:0.6em 0.8em;overflow-x:auto}
    .mv .mv-tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
    .mv-narrow{display:none}
    @media (max-width:640px){.mv-wide{display:none}.mv-narrow{display:block}.mv-tile-value{font-size:1.6em}.mv-tile{padding:0.6em 0.7em}}
  </style>
'''


def _heat_rgb(v, vmax=0.35):
    """Sequential blue ramp for correlations 0..vmax (one hue, light to dark)."""
    stops = [(0.0, (247, 249, 252)), (0.4, (180, 208, 240)), (0.7, (84, 144, 214)), (1.0, (22, 72, 140))]
    t = max(0.0, min(1.0, (v or 0.0) / vmax))
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return '#%02x%02x%02x' % tuple(round(a + (b - a) * f) for a, b in zip(c0, c1))
    return '#%02x%02x%02x' % stops[-1][1]


def _regatta_form_data(path=None):
    """regatta_form.json if it has the race-by-race correlation, else None."""
    import json as _json
    path = path or os.path.join(BASE, 'analysis', 'plackett_luce', 'eval', 'regatta_form.json')
    try:
        with open(path, encoding='utf-8') as f:
            d = _json.load(f)
    except (OSError, ValueError):
        return None
    return d if d.get('race_correlation') else None


def _regatta_form_section(path=None, champ_tau=None, champ=None):
    """Accuracy-page section: races within a regatta are correlated beyond what ratings explain."""
    import math
    d = _regatta_form_data(path)
    if not d:
        return ''
    rc = d['race_correlation']
    K = rc['max_race']
    heat = rc['heatmap']['real']
    pairs = rc['pairs']
    lag = rc['by_lag']

    def heat_svg(W, show_vals):
        L, T = 30, 30
        cell = (W - L - 6) / K
        H = T + cell * K + 50
        g = []
        for j in range(K):
            g.append(f'<text x="{L + cell * j + cell / 2:.1f}" y="{T - 9}" text-anchor="middle" class="mv-tick">{j + 1}</text>')
        for i in range(K):
            g.append(f'<text x="{L - 7}" y="{T + cell * i + cell / 2 + 4:.1f}" text-anchor="end" class="mv-tick">{i + 1}</text>')
            for j in range(K):
                x, y = L + cell * j, T + cell * i
                v = heat[i][j]
                if i == j or v is None:
                    g.append(f'<rect x="{x + 1:.1f}" y="{y + 1:.1f}" width="{cell - 2:.1f}" height="{cell - 2:.1f}" rx="2" fill="#ffffff" stroke="#e1e0d9"/>')
                    continue
                n = pairs[i][j]
                g.append(f'<g><title>Race {i + 1} and race {j + 1}: correlation {v:+.2f} ({n:,} sailors)</title>'
                         f'<rect x="{x + 1:.1f}" y="{y + 1:.1f}" width="{cell - 2:.1f}" height="{cell - 2:.1f}" rx="2" fill="{_heat_rgb(v)}"/>')
                if show_vals:
                    ink = '#ffffff' if v >= 0.2 else '#0b0b0b'
                    g.append(f'<text x="{x + cell / 2:.1f}" y="{y + cell / 2 + 4:.1f}" text-anchor="middle" '
                             f'style="font-size:{min(11, cell * 0.3):.0f}px;fill:{ink};font-variant-numeric:tabular-nums">{v:.2f}</text>')
                g.append('</g>')
        # legend ramp
        ly = T + cell * K + 18
        lw = min(220, W - L - 10)
        steps = 22
        for k in range(steps):
            g.append(f'<rect x="{L + lw * k / steps:.1f}" y="{ly}" width="{lw / steps + 0.5:.1f}" height="10" fill="{_heat_rgb(0.35 * (k + 0.5) / steps)}"/>')
        for tv in (0.0, 0.1, 0.2, 0.3):
            g.append(f'<text x="{L + lw * tv / 0.35:.1f}" y="{ly + 24}" text-anchor="middle" class="mv-tick">{tv:.1f}</text>')
        g.append(f'<text x="{L + lw + 10:.1f}" y="{ly + 9}" class="mv-note">correlation</text>')
        return (f'<svg viewBox="0 0 {W} {H:.0f}" class="mv-svg" style="max-width:{W}px" role="img" '
                f'aria-label="Heatmap of correlation between results in race i and race j">' + ''.join(g) + '</svg>')

    series = [('real', 'Real results', 'mv-line', True),
              ('regatta_form', 'Regatta offset', None, False),
              ('independent', 'Independent', None, False)]

    def lag_svg(W, H, L=46, R=120, T=12, B=46, label_px=12):
        xs = [p['lag'] for p in lag['real']]
        hi = max(0.3, max(p['r'] for p in lag['real']) + 0.02)
        hi = math.ceil(hi * 20) / 20
        lo = -0.05

        def X(v):
            return L + (v - xs[0]) / (xs[-1] - xs[0]) * (W - L - R)

        def Y(v):
            return H - B - (v - lo) / (hi - lo) * (H - T - B)

        g = []
        t = 0.0
        while t <= hi + 1e-9:
            g.append(f'<line x1="{L}" y1="{Y(t):.1f}" x2="{W - R}" y2="{Y(t):.1f}" class="{"mv-axis" if abs(t) < 1e-9 else "mv-grid"}"/>')
            g.append(f'<text x="{L - 8}" y="{Y(t) + 4:.1f}" text-anchor="end" class="mv-tick">{t:.1f}</text>')
            t = round(t + 0.1, 10)
        for x in xs:
            g.append(f'<text x="{X(x):.1f}" y="{H - B + 17}" text-anchor="middle" class="mv-tick">{x}</text>')
        styles = {'real': 'stroke:#2a78d6;stroke-width:2',
                  'regatta_form': 'stroke:#898781;stroke-width:2;stroke-dasharray:5 4',
                  'independent': 'stroke:#c3c2b7;stroke-width:2'}
        label_y = []
        for key, name, _cls, hl in series:
            pts = lag.get(key)
            if not pts:
                continue
            g.append(f'<path d="' + ' '.join(f'{"M" if k == 0 else "L"}{X(p["lag"]):.1f},{Y(p["r"]):.1f}' for k, p in enumerate(pts))
                     + f'" style="fill:none;{styles[key]};stroke-linejoin:round;stroke-linecap:round"/>')
            if hl:
                for p in pts:
                    g.append(f'<g class="mv-pt"><title>{p["lag"]} race{"s" if p["lag"] > 1 else ""} apart: correlation {p["r"]:+.3f} '
                             f'({p["pairs"]:,} pairs)</title><circle cx="{X(p["lag"]):.1f}" cy="{Y(p["r"]):.1f}" r="12" class="mv-hit"/>'
                             f'<circle cx="{X(p["lag"]):.1f}" cy="{Y(p["r"]):.1f}" r="4.5" class="mv-dot mv-dot-hl"/></g>')
            label_y.append([Y(pts[-1]['r']), name, hl])
        label_y.sort()
        for k in range(1, len(label_y)):
            label_y[k][0] = max(label_y[k][0], label_y[k - 1][0] + 15)
        for y, name, hl in label_y:
            g.append(f'<text x="{W - R + 8}" y="{y + 4:.1f}" class="mv-label{" mv-hl" if hl else ""}" style="font-size:{label_px}px">{esc(name)}</text>')
        g.append(f'<text x="{(L + W - R) / 2:.1f}" y="{H - 10}" text-anchor="middle" class="mv-axis-title">Races apart</text>')
        g.append(f'<text transform="translate(13,{(T + H - B) / 2:.1f}) rotate(-90)" text-anchor="middle" class="mv-axis-title">Correlation</text>')
        return (f'<svg viewBox="0 0 {W} {H}" class="mv-svg" style="max-width:{W}px" role="img" '
                f'aria-label="Correlation of results against rating by how many races apart">' + ''.join(g) + '</svg>')

    real_lag = {p['lag']: p for p in lag['real']}
    within = [heat[i][i + 1] for i in range(0, K - 1, 2) if heat[i][i + 1] is not None]
    across = [heat[i][i + 1] for i in range(1, K - 1, 2) if heat[i][i + 1] is not None]
    offdiag = [heat[i][j] for i in range(K) for j in range(K) if i != j and heat[i][j] is not None]
    far = real_lag.get(K - 1) or lag['real'][-1]
    form_flat = sum(p['r'] for p in lag.get('regatta_form', [])) / max(len(lag.get('regatta_form', [])), 1)

    upd = d.get('updating', {})
    grid = upd.get('grid', [])
    tt = upd.get('tau_tuned')
    upd_txt = ''
    if grid and tt in grid and upd.get('test'):
        j = grid.index(tt)
        a0, a1 = upd['test']['acc'][0], upd['test']['acc'][j]
        l0, l1 = upd['test']['log_loss'][0], upd['test']['log_loss'][j]
        upd_txt = (f'<p><strong>Using it.</strong> If each sailor&#39;s strength is nudged by how they have sailed so far in the '
                   f'regatta, the order of later races is easier to predict. Tuned on 2024&ndash;25 championships and tested '
                   f'on 2025&ndash;26, pairs ordered correctly rose from {a0 * 100:.1f}% to {a1 * 100:.1f}% and log-loss fell '
                   f'from {l0:.3f} to {l1:.3f}. That is a bigger gain than the site model&#39;s whole edge over Elo.</p>')
    r1 = (d.get('race1') or {}).get('won race 1')
    r1_txt = ''
    if r1:
        r1_txt = (f'<p><strong>So yes, winning race 1 means something.</strong> Across {r1["sailors"]} championship divisions, the '
                  f'race-1 winner went on to average {r1["actual"]:.1f} in later races. Their rating alone predicted '
                  f'{r1["rating_only"]:.1f}; updating on race 1 predicts {r1["updated"]:.1f}.</p>')
    boat = d.get('boat') or {}
    b2b = boat.get('back_to_back') or {}
    boat_txt = ''
    sb, db_ = (b2b.get('same_block_same_boat') or {}), (b2b.get('across_block_diff_boat') or {})
    bl = {p['lag']: p for p in boat.get('by_lag', [])}
    if sb.get('r') is not None and db_.get('r') is not None:
        far_same = [p for p in boat.get('by_lag', []) if p['lag'] >= 2 and p['same_r'] is not None and p['same_pairs'] >= 500]
        boat_txt = (f' Rotations show why. Back-to-back races sailed in the same boat correlate at {sb["r"]:.2f}, back-to-back '
                    f'races in different boats at {db_["r"]:.2f}.')
        if far_same:
            p = max(far_same, key=lambda q: q['same_pairs'])
            if p.get('diff_r') is not None:
                boat_txt += (f' The boat matters even when races are far apart: {p["lag"]} races apart, the same boat '
                             f'gives {p["same_r"]:.2f} and a different boat {p["diff_r"]:.2f}.')
    sim_txt = ''
    comp = (champ or {}).get('form_components')
    sim_txt = ''
    exp = d.get('experience') or {}
    table = exp.get('table') or []
    csp = (d.get('champ_spread') or {}).get('k')
    osp = (d.get('other_spread') or {}).get('k')
    if comp:
        sizes = ''
        if table:
            by_lab = {r['label']: r['multiplier'] for r in table}
            sizes = (f' How big the swings should be depends on how well a sailor&#39;s rating is known, because they also '
                     f'cover the rating being wrong. For skippers with more than 200 races before the event they are '
                     f'{by_lab.get("200+ races", 1):.1f} times the fitted size; for 61&ndash;200 races '
                     f'{by_lab.get("61-200 races", 1):.1f}; for 1&ndash;60 races {by_lab.get("1-60 races", 1):.1f}; for newcomers '
                     f'{by_lab.get("newcomer", 1):.1f}. Those sizes were chosen on the 2024&ndash;25 championships and checked on '
                     f'2025&ndash;26 (the newcomers still finish worse than their starting rating suggests, which bigger swings '
                     f'cannot fix).')
        stretch = ''
        if csp and csp > 1.0:
            stretch = (f' Ratings frozen a week ahead are also a little too bunched together: results at championships fit best '
                       f'with the gaps between ratings {(csp - 1) * 100:.0f}% wider'
                       + (f' ({(osp - 1) * 100:.0f}% at other regattas the same weeks)' if osp else '')
                       + ', so the championship simulations stretch them.')
        why = ''
        if table and csp:
            why = (f' Before both changes, team totals needed much smaller swings than individual sailors did: championship '
                   f'winners are mostly experienced skippers with well-known ratings, and extra swings were also flattening '
                   f'favourites whose ratings were already too bunched. With both, team totals chose '
                   f'{comp.get("scale", 1) * 100:.0f}% of the fitted size.')
        sim_txt = (f'<p><strong>In the simulations.</strong> The championship simulations and the regatta analysis pages now '
                   f'build these correlations into every simulated regatta instead of treating each race as a fresh draw. '
                   f'Each sailor gets a good or bad regatta, form that drifts from race to race, and the speed of whichever '
                   f'boat the rotation puts them in, shared with every team that sails that boat. The three sizes were fitted '
                   f'to the correlations above.{sizes}{stretch}{why} See <a href="championships/">how likely was each '
                   f'result</a>.</p>')
    elif champ_tau:
        sim_txt = (f'<p><strong>In the simulations.</strong> The championship simulations and the regatta analysis pages '
                   f'now give every sailor one form offset per simulated regatta, shared by all of their races, instead of '
                   f'treating each race as a fresh draw. The offset is typically within &plusmn;{champ_tau * 100:.0f} rating points, '
                   f'a size chosen on the 2024&ndash;25 championships and checked on 2025&ndash;26: see '
                   f'<a href="championships/">how likely was each result</a>.</p>')

    heat_fig = (f'<div class="mv-wide">{heat_svg(600, True)}</div><div class="mv-narrow">{heat_svg(360, False)}</div>')
    lag_fig = (f'<div class="mv-wide">{lag_svg(600, 300)}</div><div class="mv-narrow">{lag_svg(360, 260, L=40, R=94, label_px=11)}</div>')
    return f'''
    <h2 id="races-not-independent">Races within a regatta are not independent</h2>
    <p>The model predicts each race from ratings alone, as if every race were a fresh draw. Real regattas are not like
    that: a sailor who beats their rating in one race tends to beat it again. To measure it, each championship
    skipper&#39;s result in every race was scored against their rating going in (places better or worse than expected,
    as a share of the fleet), then compared race by race, with up to {max(max(r_) for r_ in pairs):,} sailors behind
    each pair of races. If races were independent every cell below would be about zero.</p>
    <figure class="mv-figure"><figcaption>Correlation between a sailor&#39;s result against their rating in race i
      (rows) and race j (columns), championship divisions with ratings frozen the week before.
      Darker means more alike.</figcaption>{heat_fig}</figure>
    <p>Every pair of races is positively correlated, {sum(offdiag) / len(offdiag):.2f} on average. Back-to-back races are
    the most alike ({real_lag[1]["r"]:.2f}) and the link fades with distance, to {far["r"]:.2f} for races {far["lag"]} apart,
    so part of it is form or conditions that change through the regatta. Races also come in pairs: back-to-back races
    within the same pair (1&ndash;2, 3&ndash;4, &hellip;) average {sum(within) / max(len(within), 1):.2f}, while back-to-back
    races across a pair (2&ndash;3, 4&ndash;5, &hellip;) average {sum(across) / max(len(across), 1):.2f}.{boat_txt}</p>
    <figure class="mv-figure"><figcaption>Average correlation by how many races apart, with what two simple models
      would produce on the same fleets: independent races, and one form offset per sailor that lasts the whole
      regatta (a flat {form_flat:.2f}).</figcaption>{lag_fig}</figure>
    {r1_txt}
    {upd_txt}
    {sim_txt}
'''


def build_methodology_page(results_path=None, points_path=None):
    """Model evaluation page (icsa/methodology/) rendered from
    analysis/plackett_luce/eval/results.json (written by evaluate_models.py)."""
    import json as _json
    import re as _re
    import math as _math

    path = results_path or os.path.join(BASE, 'analysis', 'plackett_luce', 'eval', 'results.json')
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        res = _json.load(f)

    P = '../'
    order = res['order']
    labels = res['labels']
    short = {'uninformed': 'Coin flip', 'avg_finish': 'Average finish', 'elo': 'Elo',
             'static_pl': 'Static PL', 'weekly_pl_v1': 'Previous site model', 'weekly_pl': 'Weekly PL (site)'}
    site = 'weekly_pl'
    prev_model = res.get('previous_model') or {}
    prev_change = prev_model.get('change', 'penalties')
    fc = res['forecast']
    pooled = fc['pooled']
    folds = fc['folds']
    baselines = [m for m in order if m not in ('uninformed', site, 'weekly_pl_v1')]

    def m_(model, key, sl='all'):
        return pooled[model][sl][key]

    def pct(x, d=1):
        return '&mdash;' if x is None else f'{x * 100:.{d}f}%'

    best_acc_b = max(baselines, key=lambda m: m_(m, 'accuracy'))
    best_ll_b = min(baselines, key=lambda m: m_(m, 'log_loss'))
    d_acc = m_(site, 'accuracy') - m_(best_acc_b, 'accuracy')
    d_ll = (m_(site, 'log_loss') - m_(best_ll_b, 'log_loss')) / m_(best_ll_b, 'log_loss')
    n_pairs = m_(site, 'pairs')
    n_races = m_(site, 'races')
    season_names = [season_label(f['season']) for f in folds]
    seasons_txt = ', '.join(season_names[:-1]) + (' and ' if len(season_names) > 1 else '') + season_names[-1]

    good, bad = '#006300', '#b42318'

    def delta_span(text, is_good):
        if is_good is None:
            return f'<div class="mv-tile-delta">{text}</div>'
        col = good if is_good else bad
        return f'<div class="mv-tile-delta" style="color:{col}">{text}</div>'

    tiles = [
        ('Boat pairs ordered correctly', pct(m_(site, 'accuracy')),
         delta_span(f'{d_acc * 100:+.1f} pts vs {short[best_acc_b]}', d_acc >= 0)),
        ('Race winner picked', pct(m_(site, 'winner_accuracy'), 0),
         delta_span(f'coin flip picks {pct(m_("uninformed", "winner_accuracy"), 0)}', None)),
        ('Pairwise log-loss', f'{m_(site, "log_loss"):.3f}',
         delta_span(f'{d_ll * 100:+.1f}% vs {short[best_ll_b]} &middot; lower is better', d_ll <= 0)),
        ('Returning sailors only', pct(m_(site, 'accuracy', 'seen')),
         delta_span(f'pairs with a newcomer: {pct(m_(site, "accuracy", "unseen"))}', None)),
    ]
    tiles_html = '<div class="mv-tiles">' + ''.join(
        f'<div class="mv-tile"><div class="mv-tile-label">{lab}</div>'
        f'<div class="mv-tile-value">{val}</div>{dl}</div>' for lab, val, dl in tiles) + '</div>'

    # ── Dot plot (one metric, one row per model, site model emphasised) ─────────
    def dot_plot(key, lo, hi, step, val_fmt, tick_fmt, aria, W=600, LW=150, RW=64):
        RH, TOP, AX = 34, 6, 30
        x0, x1 = LW, W - RW
        n = len(order)
        H = TOP + RH * n + AX

        def X(v):
            return x0 + (v - lo) / (hi - lo) * (x1 - x0)

        g = []
        t = lo
        while t <= hi + 1e-9:
            x = X(t)
            g.append(f'<line x1="{x:.1f}" y1="{TOP}" x2="{x:.1f}" y2="{TOP + RH * n}" class="mv-grid"/>')
            g.append(f'<text x="{x:.1f}" y="{TOP + RH * n + 18}" text-anchor="middle" class="mv-tick">{tick_fmt(t)}</text>')
            t += step
        for i, model in enumerate(order):
            v = m_(model, key)
            cy = TOP + RH * i + RH / 2
            hl = model == site
            cls_hl = ' mv-hl' if hl else ''
            g.append(
                f'<g class="mv-row"><title>{esc(labels[model])}: {val_fmt(v)}</title>'
                f'<rect x="0" y="{TOP + RH * i}" width="{W}" height="{RH}" class="mv-hit"/>'
                f'<text x="{LW - 14}" y="{cy + 4.5:.1f}" text-anchor="end" class="mv-label{cls_hl}">{esc(short[model])}</text>'
                f'<circle cx="{X(v):.1f}" cy="{cy:.1f}" r="{6 if hl else 5}" class="mv-dot{" mv-dot-hl" if hl else ""}"/>'
                f'<text x="{X(v) + 13:.1f}" y="{cy + 4:.1f}" class="mv-val{cls_hl}">{val_fmt(v)}</text></g>')
        return (f'<svg viewBox="0 0 {W} {H}" class="mv-svg" role="img" aria-label="{esc(aria)}">'
                + ''.join(g) + '</svg>')

    def responsive(wide, narrow):
        """Same chart drawn twice: text stays at its designed size on phones."""
        return f'<div class="mv-wide">{wide}</div><div class="mv-narrow">{narrow}</div>'

    accs = [m_(m, 'accuracy') for m in order]
    acc_hi = _math.ceil((max(accs) + 0.015) * 20) / 20
    acc_args = ('accuracy', 0.5, acc_hi, 0.05, lambda v: f'{v * 100:.1f}%',
                lambda t: f'{t * 100:.0f}%', 'Pairwise accuracy by model')
    acc_plot = responsive(dot_plot(*acc_args), dot_plot(*acc_args, W=360, LW=124, RW=50))
    lls = [m_(m, 'log_loss') for m in order]
    ll_lo = _math.floor((min(lls) - 0.015) * 20) / 20
    ll_hi = _math.ceil((max(lls) + 0.005) * 20) / 20
    ll_args = ('log_loss', ll_lo, ll_hi, 0.05, lambda v: f'{v:.3f}',
               lambda t: f'{t:.2f}', 'Pairwise log-loss by model, lower is better')
    ll_plot = responsive(dot_plot(*ll_args), dot_plot(*ll_args, W=360, LW=124, RW=50))

    # ── Calibration plot (site model) ───────────────────────────────────────────
    cal = fc['calibration'].get(site, [])
    cal_html = ''
    def calib_svg(W, H, L, R=18, T=14, B=58):
        lo = min(0.5, _math.floor(min(b['observed'] for b in cal) * 10) / 10)
        hi = 1.0

        def X(v):
            return L + (v - lo) / (hi - lo) * (W - L - R)

        def Y(v):
            return H - B - (v - lo) / (hi - lo) * (H - T - B)

        g = []
        t = lo
        while t <= hi + 1e-9:
            g.append(f'<line x1="{X(t):.1f}" y1="{T}" x2="{X(t):.1f}" y2="{H - B}" class="mv-grid"/>')
            g.append(f'<line x1="{L}" y1="{Y(t):.1f}" x2="{W - R}" y2="{Y(t):.1f}" class="mv-grid"/>')
            g.append(f'<text x="{X(t):.1f}" y="{H - B + 17}" text-anchor="middle" class="mv-tick">{t * 100:.0f}%</text>')
            g.append(f'<text x="{L - 8}" y="{Y(t) + 4:.1f}" text-anchor="end" class="mv-tick">{t * 100:.0f}%</text>')
            t = round(t + 0.1, 10)
        g.append(f'<line x1="{L}" y1="{H - B}" x2="{W - R}" y2="{H - B}" class="mv-axis"/>')
        g.append(f'<line x1="{X(max(lo, 0.5)):.1f}" y1="{Y(max(lo, 0.5)):.1f}" x2="{X(1):.1f}" y2="{Y(1):.1f}" class="mv-ref"/>')
        pts = sorted(cal, key=lambda b: b['mean_pred'])
        g.append('<path class="mv-line" d="' + ' '.join(
            f'{"M" if i == 0 else "L"}{X(b["mean_pred"]):.1f},{Y(b["observed"]):.1f}' for i, b in enumerate(pts)) + '"/>')
        for b in pts:
            cx, cy = X(b['mean_pred']), Y(b['observed'])
            g.append(f'<g class="mv-pt"><title>Model said {b["mean_pred"] * 100:.0f}% &middot; favourite finished ahead '
                     f'{b["observed"] * 100:.0f}% of the time &middot; {b["n"]:,} pairs</title>'
                     f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="13" class="mv-hit"/>'
                     f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" class="mv-dot mv-dot-hl"/></g>')
        g.append(f'<text x="{(L + W - R) / 2:.1f}" y="{H - 16}" text-anchor="middle" class="mv-axis-title">'
                 f'Model&#39;s probability the favourite finishes ahead</text>')
        g.append(f'<text transform="translate(15,{(T + H - B) / 2:.1f}) rotate(-90)" text-anchor="middle" '
                 f'class="mv-axis-title">How often the favourite actually did</text>')
        return (f'<svg viewBox="0 0 {W} {H}" class="mv-svg" style="max-width:{W}px" role="img" '
                f'aria-label="Calibration of the site model">' + ''.join(g) + '</svg>')

    if cal:
        svg = responsive(calib_svg(460, 420, 58), calib_svg(360, 350, 50))
        rows = ''.join(
            f'<tr class="row{i % 2}"><td>{b["lo"] * 100:.0f}&ndash;{b["hi"] * 100:.0f}%</td>'
            f'<td>{b["mean_pred"] * 100:.1f}%</td><td>{b["observed"] * 100:.1f}%</td><td>{b["n"]:,}</td></tr>'
            for i, b in enumerate(cal))
        cal_html = (
            '<figure class="mv-figure"><figcaption>Calibration of the site model, all forecast seasons pooled. '
            'Each blue point groups boat pairs by how confident the model was in the favourite; the '
            '<svg width="22" height="8" style="display:inline-block;vertical-align:middle" aria-hidden="true"><line x1="0" y1="4" x2="22" y2="4" '
            'stroke="#898781" stroke-width="1"/></svg> gray line is perfect calibration.</figcaption>'
            f'{svg}</figure>'
            '<details><summary>Calibration table</summary><div class="mv-tablewrap"><table class="results">'
            '<thead><tr><th>Confidence bin</th><th>Mean predicted</th><th>Observed</th><th>Pairs</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></details>')

    # ── Tables ──────────────────────────────────────────────────────────────────
    def model_rows(get_cells):
        out = ''
        for i, model in enumerate(order):
            cls = 'mv-site' if model == site else f'row{i % 2}'
            try:
                cells = get_cells(model)
            except (TypeError, KeyError, ValueError):
                continue      # a model this protocol could not score (e.g. the previous site model
                              # when its held-out split no longer matches) is left out of the table
            out += f'<tr class="{cls}"><td>{esc(labels[model])}</td>' + ''.join(
                f'<td>{c}</td>' for c in cells) + '</tr>'
        return out

    main_table = (
        '<div class="mv-tablewrap"><table class="results"><thead><tr><th>Model</th>'
        '<th>Pairwise<br>accuracy</th><th>Log-loss</th><th>Winner<br>picked</th><th>Rank<br>correlation</th>'
        '<th>ICSA<br>accuracy</th><th>ISSA<br>accuracy</th><th>Returning<br>sailors</th><th>With a<br>newcomer</th>'
        '</tr></thead><tbody>'
        + model_rows(lambda m: [pct(m_(m, 'accuracy')), f'{m_(m, "log_loss"):.4f}', pct(m_(m, 'winner_accuracy')),
                                f'{m_(m, "spearman"):.3f}', pct(m_(m, 'accuracy', 'ICSA')), pct(m_(m, 'accuracy', 'ISSA')),
                                pct(m_(m, 'accuracy', 'seen')), pct(m_(m, 'accuracy', 'unseen'))])
        + '</tbody></table></div>')

    season_rows = ''
    for i, fo in enumerate(folds):
        cells = ''.join(
            f'<td{" style=&quot;font-weight:bold&quot;" if m == site else ""}>{pct(fo["metrics"][m]["all"]["accuracy"])}</td>'
            .replace('&quot;', '"') for m in order)
        season_rows += (f'<tr class="row{i % 2}"><td>{esc(season_label(fo["season"]))}</td>'
                        f'<td class="mv-muted">{esc(fo["cutoff_week"])}</td><td>{fo["test_races"]:,}</td>'
                        f'<td>{fo["new_sailors"]:,}</td>{cells}</tr>')
    season_table = (
        '<div class="mv-tablewrap"><table class="results"><thead><tr><th>Forecast season</th><th>Ratings<br>frozen at</th>'
        '<th>Races</th><th>New<br>sailors</th>'
        + ''.join(f'<th>{esc(short[m])}</th>' for m in order)
        + f'</tr></thead><tbody>{season_rows}</tbody></table></div>')

    tuned_rows = ''.join(
        f'<tr class="row{i % 2}"><td>{esc(season_label(fo["season"]))}</td><td>{esc(season_label(fo["tuning_season"]))}</td>'
        f'<td>{fo["tuned"]["elo_k"]:g}</td><td>{fo["tuned"]["avg_finish_a"]:g}</td><td>{fo["tuned"]["static_lambda"]:g}</td></tr>'
        for i, fo in enumerate(folds))
    tuned_table = (
        '<div class="mv-tablewrap"><table class="results"><thead><tr><th>Forecast season</th><th>Tuned on</th>'
        '<th>Elo K</th><th>Avg-finish<br>shrinkage</th><th>Static PL<br>L2 &lambda;</th></tr></thead>'
        f'<tbody>{tuned_rows}</tbody></table></div>')

    ho = res.get('holdout')
    holdout_html = ''
    if ho:
        trainer = {}
        log_path = os.path.join(BASE, 'analysis', 'plackett_luce', 'eval', 'holdout', 'train.log')
        if os.path.exists(log_path):
            with open(log_path, encoding='utf-8') as f:
                for line in f:
                    mm = _re.search(r'(ICSA|ISSA)\s+pairwise accuracy : ([\d.]+)%\s+log-loss : ([\d.]+)', line)
                    if mm:
                        trainer[mm.group(1)] = (float(mm.group(2)) / 100, float(mm.group(3)))
        hm = ho['metrics']
        rows = model_rows(lambda m: [pct(hm[m]['ICSA']['accuracy']), f'{hm[m]["ICSA"]["log_loss"]:.4f}',
                                     pct(hm[m]['ISSA']['accuracy']), f'{hm[m]["ISSA"]["log_loss"]:.4f}'])
        if trainer.get('ICSA') and trainer.get('ISSA'):
            rows += ('<tr><td class="mv-muted">Trainer&#39;s own printout (weekly PL)</td>'
                     f'<td class="mv-muted">{pct(trainer["ICSA"][0], 2)}</td><td class="mv-muted">{trainer["ICSA"][1]:.4f}</td>'
                     f'<td class="mv-muted">{pct(trainer["ISSA"][0], 2)}</td><td class="mv-muted">{trainer["ISSA"][1]:.4f}</td></tr>')
        holdout_html = (
            f'<p>The trainer holds out every 20th regatta ({ho["test_regattas"]:,} regattas, {ho["test_races"]:,} races) '
            'and prints its accuracy on them. Re-scoring that split with this evaluator should reproduce the trainer&#39;s '
            'numbers, which confirms the metric code. These figures are <strong>optimistic</strong> for the two '
            'Plackett&ndash;Luce models: the weekly model smooths ratings across time, so a held-out race&#39;s rating is '
            'informed by races that came after it. Elo and average finish only use earlier weeks here.</p>'
            '<div class="mv-tablewrap"><table class="results"><thead><tr><th>Model</th><th>ICSA<br>accuracy</th>'
            '<th>ICSA<br>log-loss</th><th>ISSA<br>accuracy</th><th>ISSA<br>log-loss</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')

    edge_hits = sorted({(season_label(fo['season']), k) for fo in folds
                        for k, v in fo.get('tuning_at_edge', {}).items() if v})
    edge_names = {'elo_k': 'Elo K', 'avg_finish_a': 'average-finish shrinkage', 'static_lambda': 'static PL regularisation'}
    edge_caveat = ''
    if edge_hits:
        edge_caveat = ('<li>Some baseline settings were still best at the limit of their search range ('
                       + '; '.join(f'{esc(edge_names.get(k, k))} for {esc(sl)}' for sl, k in edge_hits)
                       + '), so those baselines may be slightly under-tuned.</li>\n      ')

    # ── Championships, ratings frozen the week before ───────────────────────────
    champ_html = ''
    ch = res.get('championships')
    if ch and ch.get('metrics') and ch['metrics'][site]['champ']['pairs']:
        cm = ch['metrics']
        cb = ((ch.get('bootstrap') or {}).get('champ') or {}).get('models', {})

        def cv(model, key, sl='champ'):
            return cm[model][sl][key]

        best_c = max(baselines, key=lambda mm: cv(mm, 'accuracy'))

        def ci_plot(W=600, LW=150, RW=64):
            RH, TOP, AX = 34, 6, 30
            n = len(order)
            H = TOP + RH * n + AX
            uppers = [(cb.get(mm, {}).get('accuracy_ci') or [cv(mm, 'accuracy')] * 2)[1] for mm in order]
            lo, hi = 0.5, _math.ceil((max(uppers) + 0.01) * 20) / 20
            x0, x1 = LW, W - RW

            def X(v):
                return x0 + (v - lo) / (hi - lo) * (x1 - x0)

            g = []
            t = lo
            while t <= hi + 1e-9:
                g.append(f'<line x1="{X(t):.1f}" y1="{TOP}" x2="{X(t):.1f}" y2="{TOP + RH * n}" class="mv-grid"/>')
                g.append(f'<text x="{X(t):.1f}" y="{TOP + RH * n + 18}" text-anchor="middle" class="mv-tick">{t * 100:.0f}%</text>')
                t = round(t + 0.05, 10)
            for i, mm in enumerate(order):
                v = cv(mm, 'accuracy')
                ci = cb.get(mm, {}).get('accuracy_ci') or [v, v]
                cy = TOP + RH * i + RH / 2
                hl = mm == site
                cls = ' mv-hl' if hl else ''
                g.append(
                    f'<g class="mv-row"><title>{esc(labels[mm])}: {v * 100:.1f}% '
                    f'(95% interval {ci[0] * 100:.1f}&ndash;{ci[1] * 100:.1f}%)</title>'
                    f'<rect x="0" y="{TOP + RH * i}" width="{W}" height="{RH}" class="mv-hit"/>'
                    f'<text x="{LW - 14}" y="{cy + 4.5:.1f}" text-anchor="end" class="mv-label{cls}">{esc(short[mm])}</text>'
                    f'<line x1="{X(ci[0]):.1f}" y1="{cy:.1f}" x2="{X(ci[1]):.1f}" y2="{cy:.1f}" '
                    f'class="mv-ci{" mv-ci-hl" if hl else ""}"/>'
                    f'<circle cx="{X(v):.1f}" cy="{cy:.1f}" r="{6 if hl else 5}" class="mv-dot{" mv-dot-hl" if hl else ""}"/>'
                    f'<text x="{X(ci[1]) + 12:.1f}" y="{cy + 4:.1f}" class="mv-val{cls}">{v * 100:.1f}%</text></g>')
            return (f'<svg viewBox="0 0 {W} {H}" class="mv-svg" role="img" '
                    f'aria-label="Championship accuracy by model with 95% intervals">' + ''.join(g) + '</svg>')

        s_acc, b_acc = cv(site, 'accuracy'), cv(best_c, 'accuracy')
        sci = cb.get(site, {}).get('accuracy_ci')
        mci = cb.get(best_c, {}).get('site_margin_ci')
        share = cb.get(best_c, {}).get('site_better_accuracy_share')
        interval = f' (95% interval {sci[0] * 100:.1f}&ndash;{sci[1] * 100:.1f}%)' if sci else ''
        verdict = ''
        if mci and share is not None:
            margin = f'{mci[0] * 100:+.1f} to {mci[1] * 100:+.1f} points'
            if mci[0] > 0:
                verdict = (f' Resampling whole regattas, the site model came out ahead of {esc(short[best_c])} in '
                           f'{share * 100:.0f}% of resamples, and the interval for its margin ({margin}) stays above zero.')
            elif mci[1] < 0:
                verdict = (f' Resampling whole regattas, {esc(short[best_c])} came out ahead: the interval for the site '
                           f'model&#39;s margin ({margin}) stays below zero.')
            else:
                verdict = (f' Resampling whole regattas, the site model came out ahead of {esc(short[best_c])} in '
                           f'{share * 100:.0f}% of resamples, but the interval for its margin ({margin}) includes zero, '
                           'so championships alone cannot separate the two.')
        stale = ''
        if pooled[site]['champ']['pairs']:
            ss = pooled[site]['champ']['accuracy']
            stale = (f' With ratings frozen at the start of the season instead, the same model scored {pct(ss)} on '
                     f'these regattas' + (', so results from earlier in the season add real information.'
                                          if s_acc - ss >= 0.005 else '.'))

        def crow(mm):
            ci = cb.get(mm, {}).get('accuracy_ci')
            sh = cb.get(mm, {}).get('site_better_accuracy_share')
            return [pct(cv(mm, 'accuracy')),
                    f'{ci[0] * 100:.1f}&ndash;{ci[1] * 100:.1f}%' if ci else '&mdash;',
                    f'{cv(mm, "log_loss"):.4f}', pct(cv(mm, 'winner_accuracy')),
                    pct(cm[mm]['champ_ICSA']['accuracy']), pct(cm[mm]['champ_ISSA']['accuracy']),
                    '&mdash;' if (mm == site or sh is None) else f'{sh * 100:.0f}%']

        champ_table = (
            '<div class="mv-tablewrap"><table class="results"><thead><tr><th>Model</th>'
            '<th>Pairwise<br>accuracy</th><th>95%<br>interval</th><th>Log-loss</th><th>Winner<br>picked</th>'
            '<th>ICSA</th><th>ISSA</th><th>Site model<br>better in</th></tr></thead><tbody>'
            + model_rows(crow) + '</tbody></table></div>')

        type_names = {'Showcase': 'Atlantic Coast Championship rounds', 'National Championship Semifina': 'National Championship Semifinals'}
        type_items = ''.join(
            f'<li>{esc(t["league"])}: {esc(type_names.get(t["type"], t["type"]))} &middot; {t["regattas"]}</li>'
            for t in ch.get('regattas_by_type', []))
        cov = ch.get('coverage') or []
        gaps = [c for c in cov if c['scored'] < c['listed']]
        cov_note = ''
        if gaps:
            cov_rows = ''.join(
                f'<tr class="row{i % 2}"><td>{esc(season_label(c["season"]))}</td><td>{esc(c["league"])}</td>'
                f'<td>{c["listed"]}</td><td>{c["scored"]}</td>'
                f'<td style="text-align:left;white-space:normal">{esc(", ".join(c["missing"][:8]))}'
                f'{" &hellip;" if len(c["missing"]) > 8 else ""}</td></tr>'
                for i, c in enumerate(gaps))
            cov_note = (
                '<p><strong>Not every championship is scored yet.</strong> Some championship regattas appear on the '
                'official scoring sites but their results are not in this site&#39;s data, so they are left out: '
                + '; '.join(f'{esc(season_label(c["season"]))} {esc(c["league"])} {c["listed"] - c["scored"]} of {c["listed"]}'
                            for c in gaps) + '.</p>'
                '<details><summary>Missing championship regattas</summary><div class="mv-tablewrap"><table class="results">'
                '<thead><tr><th>Season</th><th>League</th><th>Listed</th><th>Scored</th><th>Missing</th></tr></thead>'
                f'<tbody>{cov_rows}</tbody></table></div></details>')

        champ_points_link = ('<p><a href="championships/">Predicted versus actual team totals at every championship</a></p>'
                             if _champ_points_data(points_path) else '')
        champ_html = f'''
    <h2>Championship regattas</h2>
    <p>Championships are where ratings get the most attention, so this test scores only championship-level fleet
    regattas: ICSA national finals and semifinals, conference championships and Atlantic Coast Championship
    rounds, and ISSA national championships, district championships and district qualifiers. For each one, every
    model is fit on all races through the week before the event and then predicts it. Baseline settings come from
    that season&#39;s forecast tuning.</p>
    <p>The site model ordered <strong>{pct(s_acc)}</strong> of boat pairs correctly{interval} across
    {ch["regattas"]:,} regattas and {ch["races"]:,} races; {esc(labels[best_c])} managed {pct(b_acc)}.{verdict}{stale}</p>
    <figure class="mv-figure"><figcaption>Championship regattas: share of boat pairs ordered correctly, with 95%
      intervals from resampling whole regattas</figcaption>
      {responsive(ci_plot(), ci_plot(W=360, LW=124, RW=50))}</figure>
    {champ_table}
    {champ_points_link}
    {cov_note}
    <details><summary>Which regattas count as championships</summary><ul>{type_items}</ul></details>
'''

    cal_note = ''
    if cal:
        top = [b for b in cal if b['mean_pred'] >= 0.85]
        worst = max(top, key=lambda b: b['mean_pred'] - b['observed'], default=None)
        mid = [b for b in cal if b['mean_pred'] < 0.85]
        mid_gap = max((abs(b['mean_pred'] - b['observed']) for b in mid), default=0.0)
        if worst and worst['mean_pred'] - worst['observed'] >= 0.015:
            cal_note = (f' Up to about 85% confidence the points sit within {mid_gap * 100:.1f} percentage points of the '
                        f'diagonal. Above that the model is slightly overconfident: when it says '
                        f'{worst["mean_pred"] * 100:.0f}%, the favourite finishes ahead {worst["observed"] * 100:.0f}% '
                        'of the time.')
        elif cal:
            cal_note = (f' Every group sits within {max(abs(b["mean_pred"] - b["observed"]) for b in cal) * 100:.1f} '
                        'percentage points of the diagonal.')

    # ── Fleet race nationals: ratings going in, predicted vs actual standings ────
    nationals_html = ''
    pts = _champ_points_data(points_path)
    if pts:
        def _is_fleet_nationals(r):
            nm = r.get('name', '').lower()
            return (r.get('type') in ('National Championship Finals', 'National Championship')
                    and r.get('scoring', '').lower() not in ('singlehanded', 'team')
                    and ('fleet' in nm or 'mallory' in nm))

        def _start(r):
            try:
                return _dt.strptime(r.get('start_date', ''), '%m/%d/%Y')
            except ValueError:
                return _dt.min

        def _feature_order(r):
            nm = r.get('name', '').lower()
            return (-season_sort_key(r['season'])[0], -season_sort_key(r['season'])[1],
                    r['league'] != 'ICSA', 'women' in nm, -_start(r).toordinal() if _start(r) != _dt.min else 0)

        # Newest season first; ICSA Open, then ICSA Women's, then ISSA
        feats = sorted((r for r in pts['regattas'] if _is_fleet_nationals(r)), key=_feature_order)
        psite = pts.get('site', 'weekly_pl')
        blocks = ''
        for n_i, r in enumerate(feats):
            rm = r['metrics'][psite]
            ordered = r['results']
            pred_winner = min(ordered, key=lambda t: t['predicted_rank'][psite])
            href = f'../{r["url"].strip("/")}/' if r['league'] == 'ICSA' else f'../../issa/{r["url"].strip("/")}/'
            trs = ''
            for j, t in enumerate(ordered):
                pr = t['predicted_rank'][psite]
                off = pr - t['actual_rank']
                team = esc(t['school']) + (f' <span class="mv-muted-inline">{esc(t["team"])}</span>'
                                           if t['team'] and t['team'] != t['school'] else '')
                sks = t.get('skippers', [])
                subs = len(sks) > len({x['division'] for x in sks})
                lines = []
                for x in sks:
                    rating = f'<strong>{x["rating"]}</strong>' if x['rated'] else '<span class="mv-muted-inline">new</span>'
                    races = f' <span class="mv-muted-inline">({x["races"]} races)</span>' if subs else ''
                    lines.append(f'<span class="mv-muted-inline">{esc(x["division"])}</span> {_sailor_a(x["name"], t.get("school_slug"), r["league"])} {rating}{races}')
                prob_cells = (f'<td>{_pct_small(t.get("p_finish_or_better"))}</td><td>{_pct_small(t.get("win_prob"))}</td>'
                              if 'win_prob' in t else '')
                beat = off                       # predicted minus actual: positive means finished better than predicted
                trs += (f'<tr class="row{j % 2}"><td>{t["actual_rank"]}</td>'
                        f'<td style="text-align:left;white-space:nowrap">{team}</td>'
                        f'<td>{pr}</td><td>{beat:+d}</td>{prob_cells}'
                        f'<td>{t["actual_points"]:g}</td><td>{t["expected_points"][psite]:.1f}</td>'
                        f'<td style="text-align:left;white-space:nowrap;line-height:1.5">{"<br>".join(lines) or "&mdash;"}</td></tr>')
            table = ('<div class="mv-tablewrap"><table class="results"><thead><tr><th>Actual<br>finish</th>'
                     '<th style="text-align:left">Team</th><th>Predicted<br>finish</th>'
                     '<th title="Places better than predicted; negative means worse">Beat<br>prediction by</th>'
                     + ('<th>Chance of this<br>finish or better</th><th>Win<br>chance</th>' if 'win_prob' in ordered[0] else '')
                     + '<th>Points</th><th>Predicted<br>points</th>'
                     '<th style="text-align:left">Skippers and ratings going in</th></tr></thead>'
                     f'<tbody>{trs}</tbody></table></div>')
            winner_note = ('The ratings picked the winner.' if pred_winner is ordered[0]
                           else f'The ratings picked {esc(pred_winner["school"])}; {esc(ordered[0]["school"])} won.')
            summary = (f'{winner_note} Predicted totals were off by {rm["points_mae"]:.1f} points per team and finishes by '
                       f'{rm["rank_mae"]:.1f} places, with a rank correlation of {rm["spearman"]:.2f} between predicted '
                       'and actual standings.')
            sim = r.get('sim')
            if sim:
                ndiv = {'2 divisions': 2, '3 divisions': 3}.get(r.get('scoring', '').lower(), 1)
                per_div = round(r['races'] / ndiv)
                summary += (f' Over {per_div} races{" per division" if ndiv > 1 else ""}, the ratings gave '
                            f'{esc(ordered[0]["school"])} a {_pct_small(sim["winner_win_prob"])} chance to win, and standings '
                            f'at least this far from the prediction came up in '
                            + (f'none of the {sim["sims"]:,} simulated regattas.' if sim['surprise_p'] < 0.0005
                               else f'{_pct_small(sim["surprise_p"])} of {sim["sims"]:,} simulated regattas.'))
            meta = (f'<span class="mv-muted-inline">{esc(season_label(r["season"]))} &middot; {esc(r["league"])} &middot; '
                    f'{r["teams"]} teams</span>')
            if n_i == 0:
                blocks += (f'<h3 class="mv-season"><a href="{href}">{esc(r["name"])}</a> {meta}</h3>'
                           f'<p>{summary}</p>{table}')
            else:
                blocks += (f'<details class="mv-reg"><summary><a href="{href}">{esc(r["name"])}</a> {meta}</summary>'
                           f'<p>{summary}</p>{table}</details>')
        if feats:
            pen_words = (' Predicted points leave penalties out, because adding them makes the totals worse; the '
                         'chances below still include them.'
                         if 'weekly_pl_pen' in pts.get('order', []) else '')
            nationals_html = f'''
    <h2>Fleet race nationals: predicted vs actual</h2>
    <p>Nationals are the hardest test for the ratings. Each table uses only results from before the week of the
    regatta: every skipper&#39;s rating going in, the standings those ratings predict, and how teams actually
    finished. Ratings are on the rankings scale, and &ldquo;new&rdquo; marks a skipper with no earlier races, who
    starts at the typical rating of their school&#39;s recent debut sailors (or the league&#39;s, for a school with
    too few).{pen_words} The chance columns come from simulating the whole regatta,
    every race and every penalty, thousands of times with those ratings: how often each team won, and how often it
    finished where it did or better.</p>
    {blocks}
    <p><a href="championships/">Predicted versus actual standings at every championship</a></p>
'''

    # ── Penalties ───────────────────────────────────────────────────────────────
    fix_html = ''
    if prev_change == 'rating_fix' and 'weekly_pl_v1' in pooled and pooled['weekly_pl_v1']['all']['pairs']:
        v1a, v1ll = pooled['weekly_pl_v1']['all']['accuracy'], pooled['weekly_pl_v1']['all']['log_loss']
        ch_boot = ((res.get('championships') or {}).get('bootstrap') or {}).get('champ') or {}
        mv = ch_boot.get('models', {}).get('weekly_pl_v1', {})
        champ_part = ''
        if mv.get('site_margin_ci'):
            lo_, hi_ = mv['site_margin_ci']
            champ_part = (f' At championships, with ratings from the week before, the fix was worth {lo_ * 100:+.1f} to '
                          f'{hi_ * 100:+.1f} points of pairwise accuracy (95% interval).')
        sll = m_(site, 'log_loss')
        horizon = ''
        def _champ_ll(model):
            node = ((res.get('championships') or {}).get('metrics') or {}).get(model)
            for _ in range(3):
                if not isinstance(node, dict) or 'log_loss' in node:
                    break
                node = node.get('champ') or node.get('all') or next((v for v in node.values() if isinstance(v, dict)), None)
            return (node or {}).get('log_loss') if isinstance(node, dict) else None

        c_site = {'log_loss': _champ_ll(site)}
        c_prev = {'log_loss': _champ_ll('weekly_pl_v1')}
        if sll > v1ll + 0.0005:
            horizon = (' Sharper ratings age, though: a whole season ahead they order boats better but state their '
                       'confidence a little too strongly, which is why log-loss here went the other way')
            if c_site.get('log_loss') and c_prev.get('log_loss'):
                horizon += (f'. With ratings from the week before an event, which is how the site uses them, they are '
                            f'better on both counts: log-loss {c_prev["log_loss"]:.3f} before the fix against '
                            f'{c_site["log_loss"]:.3f} now')
            horizon += '.'
        fix_html = (f'<p><strong>The rating fix.</strong> {esc(prev_model.get("fix_summary", ""))} On these season-ahead '
                    f'forecasts, pairwise accuracy went from {pct(v1a)} before the fix to {pct(m_(site, "accuracy"))}, and '
                    f'log-loss from {v1ll:.3f} to {sll:.3f}.{horizon}{champ_part}</p>')
    _pts_for_form = _champ_points_data(points_path) or {}
    form_html = _regatta_form_section(champ_tau=_pts_for_form.get('form_tau'), champ=_pts_for_form)
    penalty_html = ''
    pairs_note = ''
    fpen = fc.get('penalty')
    cpen = (res.get('championships') or {}).get('penalty')
    models_li = ('<li><strong>Five models.</strong> A coin flip; each sailor&#39;s average finish percentile; multi-player '
                 'Elo; Plackett&ndash;Luce with one fixed rating per sailor; and the site&#39;s weekly Plackett&ndash;Luce model.</li>')
    if 'weekly_pl_v1' in order:
        prev_desc = esc(prev_model.get('description') or 'the previous version of the site model, which ranked boats that did not finish last')
        models_li = ('<li><strong>Six models.</strong> A coin flip; each sailor&#39;s average finish percentile; multi-player '
                     f'Elo; Plackett&ndash;Luce with one fixed rating per sailor; {prev_desc}; and the current weekly '
                     'Plackett&ndash;Luce site model.</li>')
    if fpen:
        pairs_note = ' Boats that did not finish are left out of every comparison.'
        cat_names = {'start': 'Start penalty (OCS)', 'dnf': 'Did not finish (DNF, RAF)',
                     'dns': 'Did not start (DNS)', 'any': 'Any of these'}

        def pen_gain(pc):
            return (1 - pc['log_loss'] / pc['log_loss_base_rate']) if pc.get('log_loss_base_rate') else 0.0

        prow_html = ''
        for i, c in enumerate(['start', 'dnf', 'dns', 'any']):
            cells = [pct(fpen[c]['rate'], 2), f'{pen_gain(fpen[c]) * 100:+.1f}%']
            if cpen:
                cells += [pct(cpen[c]['rate'], 2), f'{pen_gain(cpen[c]) * 100:+.1f}%']
            prow_html += (f'<tr class="row{i % 2}"><td>{esc(cat_names[c])}</td>'
                          + ''.join(f'<td>{x}</td>' for x in cells) + '</tr>')
        phead = ('<th>Outcome</th><th>Share of<br>race entries</th><th>Log-loss vs<br>league rate</th>'
                 + ('<th>Championships:<br>share</th><th>Championships:<br>vs league rate</th>' if cpen else ''))
        fa = fpen['any']
        lift = ''
        if fa.get('auc') is not None and fa.get('other_rate'):
            lift = (f' The tenth of race entries it rated most penalty-prone had a penalty {fa["top_decile_rate"] * 100:.1f}% '
                    f'of the time, against {fa["other_rate"] * 100:.1f}% for everyone else.')
        gains = {c: pen_gain(fpen[c]) for c in ('start', 'dnf', 'dns')}
        top_c = max(gains, key=gains.get)
        top_name = cat_names[top_c].split(' (')[0]
        if max(gains.values()) < 0.05:
            gain_note = (f'Penalties are rare and only loosely a habit, so these gains are small; '
                         f'{esc(top_name.lower())} is the most predictable.')
        else:
            gain_note = f'{esc(top_name)} is the most predictable.'
        v1_line = ''
        if prev_change == 'penalties' and 'weekly_pl_v1' in pooled and pooled['weekly_pl_v1']['all']['pairs']:
            v1a, sa = pooled['weekly_pl_v1']['all']['accuracy'], m_(site, 'accuracy')
            ch_boot = ((res.get('championships') or {}).get('bootstrap') or {}).get('champ') or {}
            mv = ch_boot.get('models', {}).get('weekly_pl_v1', {})
            champ_part = ''
            if mv.get('site_margin_ci'):
                lo_, hi_ = mv['site_margin_ci']
                champ_part = (f' At championships, with ratings from the week before, the change was worth '
                              f'{lo_ * 100:+.1f} to {hi_ * 100:+.1f} points of pairwise accuracy.')
            v1_line = (f'<p>Leaving non-finishers out of the ratings moved the site model&#39;s season-ahead pairwise '
                       f'accuracy from {pct(v1a)} to {pct(sa)}, scored on the same pairs of boats that both finished.'
                       f'{champ_part}</p>')
        champ_words = ' and at championships' if cpen else ''
        penalty_html = f'''
    <h2>Penalties</h2>
    <p>An OCS or a DNF says little about boat speed, so boats that did not finish are left out of each race&#39;s
    finishing order. They never count as losing to every boat that finished, and they are left out of the accuracy
    numbers above. Penalties are predicted separately: each skipper gets a per-race chance of a start penalty, a DNF
    and a DNS, pulled toward the league rate by an amount tuned on the previous season, so a few penalties in a
    handful of races do not brand a sailor.{lift}</p>
    {v1_line}
    <div class="mv-tablewrap"><table class="results"><thead><tr>{phead}</tr></thead><tbody>{prow_html}</tbody></table></div>
    <p style="color:#52514e;font-size:0.9em">&ldquo;Log-loss vs league rate&rdquo; is how much better the penalty scores
    predict than giving every skipper the league&#39;s average rate, season-ahead{champ_words}. {gain_note}</p>
'''

    site_acc = m_(site, 'accuracy')
    lede = (
        f'Predicting each of {seasons_txt} using only races sailed before that season began, the site&#39;s rating model '
        f'put <strong>{pct(site_acc)}</strong> of boat pairs in the right order across {n_pairs:,} pairs in '
        f'{n_races:,} races. The strongest baseline, {esc(labels[best_acc_b])}, managed {pct(m_(best_acc_b, "accuracy"))}; '
        'a coin flip gets 50%.')

    body = f'''
  <div class="port">
  <div class="mv">
    <p class="mv-lede">{lede}</p>
    {tiles_html}

    <h2>The question</h2>
    <p>A rating is only useful if it predicts results it has not seen. The ratings on this site come from a
    Plackett&ndash;Luce model: every skipper has a strength, and a race finish is modelled as repeatedly choosing the
    next boat across the line with probability proportional to strength. The site&#39;s version lets each
    sailor&#39;s strength drift week to week. This page tests whether that model forecasts future races better than
    simpler ways of rating sailors.</p>

    <h2>How the test works</h2>
    <ul>
      <li><strong>Strictly forward.</strong> For each forecast season, every model is fit only on races from weeks
      before the season&#39;s first race. Ratings are then frozen and used to predict every race of the season.
      There is no updating during the season, so this is harder than the model&#39;s real use on the site.</li>
      {models_li}
      <li><strong>Fair tuning.</strong> Baseline settings (Elo&#39;s K, average-finish shrinkage, the static
      model&#39;s regularisation, and a probability scale for Elo and average finish) are chosen on the season just
      before each forecast, using only data from before that season.</li>
      <li><strong>Newcomers.</strong> Sailors with no earlier races get, in every model, the median rating that model
      gave their own school&#39;s debut sailors over the two seasons before (when the school had at least two);
      otherwise the league&#39;s median debut rating. A school&#39;s newcomers are not a random draw from the league:
      on the four forecast seasons this called 65.8% of pairs involving a newcomer correctly against 62.3% with the
      league median alone, and it was better in every season.</li>
      <li><strong>Team pooling.</strong> The site model also pulls each sailor&#39;s career-average rating toward the
      average of their teammates in the same school and season (partial pooling; the pull fades as a sailor races
      more). Teams share coaching, boats and practice, so a sailor&#39;s team says something about them that their own
      few races cannot. Against the same model without it, season-ahead log-loss improved from 0.5676 to 0.5644
      and the forecasts were better calibrated; on the championships accuracy was unchanged (72.4%) and log-loss
      slightly better (0.5363 against 0.5367). Sailors with a lot of racing behind them are barely affected.</li>
      <li><strong>Scoring.</strong> Every pair of boats in a race counts once: accuracy is the share ordered correctly,
      log-loss penalises confident mistakes. Winner picked asks whether the highest-rated boat won; rank correlation
      compares the predicted order with the finish order.{pairs_note}</li>
    </ul>

    <h2>Results</h2>
    <figure class="mv-figure"><figcaption>Share of boat pairs ordered correctly, {esc(seasons_txt)} pooled</figcaption>
      {acc_plot}</figure>
    <figure class="mv-figure"><figcaption>Pairwise log-loss (lower is better; a coin flip scores 0.693)</figcaption>
      {ll_plot}</figure>
    {main_table}
    {fix_html}

    <h2>Season by season</h2>
    <p>Pairwise accuracy for each forecast season. Fall seasons bring a new freshman class, so a larger share of
    pairs involve sailors no model has seen race before.</p>
    {season_table}
{champ_html}
{nationals_html}
{form_html}

{_tr_methodology_html()}
    <h2>Are the probabilities honest?</h2>
    <p>When the model says a sailor has a 70% chance of finishing ahead of another, that should happen about 70% of
    the time. Points on the diagonal mean the stated probabilities can be taken at face value.{cal_note}</p>
    {cal_html}

{penalty_html}
    <h2>Sanity check: the trainer&#39;s own test split</h2>
    {holdout_html}

    <h2>Caveats</h2>
    <ul>
      <li>The weekly model&#39;s smoothing settings came from an earlier search scored on randomly held-out regattas
      that include these seasons, so its settings carry a little information from the forecast period. Baselines were
      tuned strictly forward.</li>
      {edge_caveat}<li>Pairs are pooled, so large fleets weigh more than small ones.</li>
      <li>Boats that did not finish are ordered last; the order among several non-finishers in the same race is
      arbitrary and counts against every model equally.</li>
      <li>Only skippers are rated. Singlehanded events and regattas without a date are excluded, matching the
      model&#39;s training data.</li>
      <li>High school and college profiles of the same sailor are linked before training, so a freshman with a high
      school record counts as a returning sailor.</li>
    </ul>

    <h2>Tuned baseline settings</h2>
    {tuned_table}

    <h2>Reproduce</h2>
    <pre>python3 analysis/plackett_luce/evaluate_models.py</pre>
    <p style="color:#52514e;font-size:0.9em">Generated {esc(res["generated"][:10])} from {res["data"]["races"]:,} races
    and {res["data"]["sailors"]:,} sailors ({esc(res["data"]["first_week"])} to {esc(res["data"]["last_week"])}).
    Full run {res.get("runtime_s", 0) // 60} minutes.</p>
  </div>
  </div>'''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Model Evaluation | Data Driven Sailing</title>
  <link rel="stylesheet" href="{P}styles.css" media="screen,print">
{METHODOLOGY_CSS}
{CHAMP_POINTS_CSS}
</head>
<body>
{nav(P + 'index.html', P + 'schools/index.html', P + 'sailors/index.html', P + 'img/icsa-logo-real.png',
     P + 'rankings/index.html', 'Rankings', logo_href=P + 'index.html')}
<div id="page-content">
  <div id="content-header">
    <h1><span id="left-fill"></span><span>How Accurate Are the Ratings?</span><span id="right-fill"></span></h1>
    <ul id="page-info">
      <li><span class="page-info-key">Test</span><span class="page-info-value">Season-ahead forecast &middot; championships</span></li>
      <li><span class="page-info-key">Seasons</span><span class="page-info-value">{esc(seasons_txt)}</span></li>
      <li><span class="page-info-key">Races scored</span><span class="page-info-value">{n_races:,}</span></li>
    </ul>
  </div>
{body}
</div>
</body>
</html>'''


CHAMP_POINTS_CSS = '''
  <style>
    .mv-muted-inline{color:#777;font-weight:normal;font-size:0.9em}
    .mv details.mv-reg{border-bottom:1px solid #e1e0d9;padding:0.4em 0;margin:0}
    .mv details.mv-reg summary{color:#0b0b0b;line-height:1.45}
    .mv details.mv-reg summary a{color:#3465A4}
    .mv .mv-good{color:#006300}
    .mv .mv-bad{color:#b42318}
    .mv h3.mv-season{font-family:Arial,Helvetica,sans-serif;font-size:1em;margin:1.4em 0 0.3em;color:#2f2f2e}
  </style>
'''


def _sailor_a(name, school_slug, league):
    """Name as a link to the sailor's page (when that page exists), for the methodology page at <league>/methodology/."""
    if not name or not school_slug:
        return esc(name)
    slug = sailor_slug(name, school_slug)
    lg = 'icsa' if league == 'ICSA' else 'issa'
    if not os.path.exists(os.path.join(BASE, 'website', lg, 'sailors', slug, 'index.html')):
        return esc(name)
    href = f'../sailors/{slug}/' if lg == 'icsa' else f'../../issa/sailors/{slug}/'
    return f'<a href="{esc(href)}">{esc(name)}</a>'


def _pct_small(x):
    if x is None:
        return '&mdash;'
    if x < 0.005:
        return '&lt;1%'
    if x > 0.995:
        return '&gt;99%'
    return f'{x * 100:.0f}%'


def _champ_points_data(path=None):
    """championship_points.json if it has a summary, else None."""
    import json as _json
    path = path or os.path.join(BASE, 'analysis', 'plackett_luce', 'eval', 'championship_points.json')
    try:
        with open(path, encoding='utf-8') as f:
            d = _json.load(f)
    except (OSError, ValueError):
        return None
    return d if (d.get('summary') or {}).get('all') else None


def _nice_axis(values, ticks=4):
    import math as _math
    lo_v, hi_v = min(values), max(values)
    raw = max((hi_v - lo_v) / ticks, 1e-9)
    step = next(s for s in (0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100) if s >= raw)
    lo = _math.floor(lo_v / step) * step
    hi = _math.ceil(hi_v / step) * step
    if hi - lo < step:
        hi = lo + step
    return lo, hi, step


def _mv_dot_rows_svg(rows, lo, hi, step, val_fmt, tick_fmt, aria, highlight, W=600, LW=190, RW=64):
    """rows: [(key, label, value)].  One dot per row on a shared axis; `highlight` in the accent."""
    RH, TOP, AX = 34, 6, 30
    n = len(rows)
    H = TOP + RH * n + AX
    x0, x1 = LW, W - RW

    def X(v):
        return x0 + (v - lo) / (hi - lo) * (x1 - x0)

    g = []
    t = lo
    while t <= hi + 1e-9:
        g.append(f'<line x1="{X(t):.1f}" y1="{TOP}" x2="{X(t):.1f}" y2="{TOP + RH * n}" class="mv-grid"/>')
        g.append(f'<text x="{X(t):.1f}" y="{TOP + RH * n + 18}" text-anchor="middle" class="mv-tick">{tick_fmt(t)}</text>')
        t = round(t + step, 10)
    for i, (key, label, v) in enumerate(rows):
        cy = TOP + RH * i + RH / 2
        hl = key == highlight
        cls = ' mv-hl' if hl else ''
        g.append(
            f'<g class="mv-row"><title>{esc(label)}: {val_fmt(v)}</title>'
            f'<rect x="0" y="{TOP + RH * i}" width="{W}" height="{RH}" class="mv-hit"/>'
            f'<text x="{LW - 14}" y="{cy + 4.5:.1f}" text-anchor="end" class="mv-label{cls}">{esc(label)}</text>'
            f'<circle cx="{X(v):.1f}" cy="{cy:.1f}" r="{6 if hl else 5}" class="mv-dot{" mv-dot-hl" if hl else ""}"/>'
            f'<text x="{X(v) + 13:.1f}" y="{cy + 4:.1f}" class="mv-val{cls}">{val_fmt(v)}</text></g>')
    return (f'<svg viewBox="0 0 {W} {H}" class="mv-svg" role="img" aria-label="{esc(aria)}">'
            + ''.join(g) + '</svg>')


def _tr_post_numbers():
    import json as _json
    try:
        with open(os.path.join(BASE, 'analysis', 'blog', 'team_race_post.json'), encoding='utf-8') as f:
            return _json.load(f)
    except (OSError, ValueError):
        return None


def _tr_methodology_html():
    """Short team race section for the methodology page, from the team race framework's held-out evaluation."""
    T = _tr_post_numbers()
    if not T:
        return ''
    V = {v['key']: v for v in T['forecasts']['variants']}
    best, main, school = V.get('best-lam30'), V.get('main'), V.get('school')
    if not (best and main and school):
        return ''
    rows = ''.join(f'<tr><td style="text-align:left">{esc(v["name"])}</td><td>{v["logloss"]:.3f}</td><td>{v["acc"] * 100:.1f}%</td></tr>'
                   for v in T['forecasts']['variants'])
    return f'''
    <h2 id="team-race">Team race forecasts</h2>
    <p>Team race matches are forecast by a separate model: a side&#39;s strength is the sum over its three boats of the
    skipper&#39;s fleet rating (scaled), small shrunk offsets for the skipper and crew, the school&#39;s team race term and
    a home-water edge; the win probability is the logistic of the difference. The test is the same as above: refit
    before every regatta date in spring 2025 and 2026 with fleet ratings frozen at the week before, then score the
    {best["matches"]:,} matches.</p>
    <table class="results" style="max-width:40em"><thead><tr><th style="text-align:left">Model</th><th>Log-loss</th><th>Matches called</th></tr></thead>
    <tbody>{rows}</tbody></table>
    <p>The stated chances are honest at every level (best temperature rescaling 1.00). Finishing positions as an extra
    training signal and within-event refits both failed to help. Every team race regatta page has a
    <em>Ratings &amp; Forecast</em> tab with the match-by-match forecast and the event&#39;s measured orderliness; see
    <a href="../team-racing/">the team racing page</a>, <a href="/blog/how-team-race-forecasts-work/">how team race
    forecasts work</a> and <a href="/blog/which-team-races-go-to-form/">which team races go to form</a>.</p>
'''


def build_team_racing_page():
    """icsa/team-racing/: one page that gathers what the site does with team racing (content from team_race_pages.hub_html)."""
    import team_race_pages
    P = '../'
    H = team_race_pages.hub_html('ICSA', P, SITE, os.path.join(CSV_DIR, 'icsa_schools.csv'), P + 'methodology/#team-race')
    if not H:
        return None
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Team Racing | Data Driven Sailing</title>
  <meta name="description" content="Team race forecasts, ratings, orderliness and winning plays for college sailing: what the site does with team racing and how well it works.">
  <link rel="stylesheet" href="{P}styles.css" media="screen,print">
{team_race_pages.HUB_CSS}
</head>
<body>
{nav(P + 'index.html', P + 'schools/index.html', P + 'sailors/index.html', P + 'img/icsa-logo-real.png',
     'index.html', 'Team Racing', logo_href=P + 'index.html')}
<div id="page-content">
  <div id="content-header">
    <h1><span id="left-fill"></span><span>Team Racing</span><span id="right-fill"></span></h1>
    <ul id="page-info">
{H['info']}
    </ul>
  </div>
{H['body']}
</div>
</body>
</html>
'''


def build_championship_points_page(points_path=None):
    """icsa/methodology/championships/: predicted vs actual team totals at every championship."""
    d = _champ_points_data(points_path)
    if not d or not _methodology_ready():
        return None
    P = '../../'                       # this page lives at icsa/methodology/championships/
    order, labels = d['order'], d['labels']
    site = d.get('site', 'weekly_pl')
    S = d['summary']['all']
    mods = S['models']
    boot = S.get('bootstrap') or {}
    short = {'uninformed': 'Coin flip', 'avg_finish': 'Avg finish + penalties', 'elo': 'Elo + penalties',
             'static_pl': 'Static PL + penalties', 'weekly_pl_v1': 'Previous site model',
             'weekly_pl_pen': 'Site + penalty in totals', 'weekly_pl': 'Site model'}
    # Alternatives are genuinely different models; the no-penalty row is an ablation of the site model itself
    others = [m for m in order if m not in ('uninformed', site, 'weekly_pl_pen')]
    best = min(others, key=lambda m: mods[m]['points_mae'])
    best_rank = min(others, key=lambda m: mods[m]['rank_mae'])
    best_win = max(others, key=lambda m: mods[m]['winner'])
    gap = S.get('median_place_gap')

    def pct(x, dgt=0):
        return '&mdash;' if x is None else f'{x * 100:.{dgt}f}%'

    good, bad = '#006300', '#b42318'

    def delta(text, is_good):
        col = good if is_good else bad
        return f'<div class="mv-tile-delta" style="color:{col}">{text}</div>'

    site_pts, best_pts = mods[site]['points_mae'], mods[best]['points_mae']
    tiles = [
        ('Points off per team', f'{site_pts:.1f}',
         delta(f'{site_pts - best_pts:+.2f} vs {esc(short[best])}', site_pts <= best_pts)),
        ('Places off per team', f'{mods[site]["rank_mae"]:.2f}',
         delta(f'{mods[site]["rank_mae"] - mods[best_rank]["rank_mae"]:+.2f} vs {esc(short[best_rank])}',
               mods[site]['rank_mae'] <= mods[best_rank]['rank_mae'])),
        ('Winner picked', pct(mods[site]['winner']),
         delta(f'{(mods[site]["winner"] - mods[best_win]["winner"]) * 100:+.0f} pts vs {esc(short[best_win])}',
               mods[site]['winner'] >= mods[best_win]['winner'])),
        ('Top-three teams found', pct(mods[site]['top3']),
         f'<div class="mv-tile-delta">coin flip finds {pct(mods["uninformed"]["top3"])}</div>'),
    ]
    tiles_html = '<div class="mv-tiles">' + ''.join(
        f'<div class="mv-tile"><div class="mv-tile-label">{lab}</div><div class="mv-tile-value">{val}</div>{dl}</div>'
        for lab, val, dl in tiles) + '</div>'

    b = boot.get(best, {}).get('points_mae', {})
    ci, share = b.get('ci'), b.get('share_regattas_site_better')
    verdict = ''
    if ci and share is not None:
        interval = f'{ci[0]:+.2f} to {ci[1]:+.2f} points per team'
        if ci[0] > 0:
            verdict = (f' Resampling whole regattas, the site model&#39;s edge over {esc(short[best])} is {interval}, '
                       f'and its totals were closer in {share * 100:.0f}% of regattas.')
        elif ci[1] < 0:
            verdict = (f' Resampling whole regattas, {esc(short[best])} was closer: the site model&#39;s edge is {interval}.')
        else:
            verdict = (f' Resampling whole regattas, the site model&#39;s edge over {esc(short[best])} is {interval}, which '
                       f'includes zero, so the regatta totals alone cannot separate the two; the site model was closer in '
                       f'{share * 100:.0f}% of regattas.')
    per_regatta = mods[best]['points_off_per_regatta'] - mods[site]['points_off_per_regatta']
    scale = ''
    if gap:
        scale = (f' Summed over every team, the site model&#39;s totals were {abs(per_regatta):.1f} points '
                 f'{"closer" if per_regatta >= 0 else "further"} per regatta, while adjacent teams in the final '
                 f'standings were typically {gap:.1f} points apart.')
    lede = (f'Before each of {S["regattas"]} championship regattas, every model predicted each team&#39;s total points using '
            'only ratings from the week before the event. The site model&#39;s predicted totals were off by '
            f'<strong>{site_pts:.1f} points per team</strong>; the closest alternative, {esc(labels[best])}, was off by '
            f'{best_pts:.1f}.{verdict}{scale}')

    ablation = ''
    if 'weekly_pl_v1' in mods and 'weekly_pl_pen' in mods:
        v1, pen_tot = mods['weekly_pl_v1']['points_mae'], mods['weekly_pl_pen']['points_mae']

        def moved(a, b):
            return 'improved that to' if b < a - 0.005 else ('worsened that to' if b > a + 0.005 else 'left that at')

        penalty_verdict = (' Penalties are rare and lumpy: adding each boat&#39;s expected penalty points nudges every '
                           'team&#39;s total up, which is right on average but wrong for the typical team that takes no '
                           'penalties at all, so predicted totals leave it out. The simulated win chances and finish '
                           'probabilities below still draw penalties race by race, where a rare, costly event belongs.')
        if (d.get('previous_model') or {}).get('change', 'penalties') == 'rating_fix':
            ablation = (
                '<p><strong>Where the difference comes from.</strong> The site model before the rating fix was off by '
                f'{v1:.2f} points per team; the refitted ratings {moved(v1, site_pts)} {site_pts:.2f}. Adding each '
                f'boat&#39;s penalty risk to the totals as well {moved(site_pts, pen_tot)} {pen_tot:.2f}.{penalty_verdict}</p>')
        else:
            ablation = (
                '<p><strong>Where the difference comes from.</strong> The previous site model, which ranked boats that did '
                f'not finish last in every race, was off by {v1:.2f} points per team. Leaving those boats out of the ratings '
                f'{moved(v1, site_pts)} {site_pts:.2f}, and adding each boat&#39;s penalty risk to the totals '
                f'{moved(site_pts, pen_tot)} {pen_tot:.2f}.{penalty_verdict}</p>')

    chart_models = [m for m in order if m != 'uninformed']
    lo, hi, step = _nice_axis([mods[m]['points_mae'] for m in chart_models])
    chart_rows = [(m, short[m], mods[m]['points_mae']) for m in chart_models]
    chart = (f'<div class="mv-wide">{_mv_dot_rows_svg(chart_rows, lo, hi, step, lambda v: f"{v:.2f}", lambda t: f"{t:g}", "Points off per team by model", site)}</div>'
             f'<div class="mv-narrow">{_mv_dot_rows_svg(chart_rows, lo, hi, step, lambda v: f"{v:.2f}", lambda t: f"{t:g}", "Points off per team by model", site, W=360, LW=150, RW=50)}</div>')

    rows = ''
    for i, m in enumerate(order):
        mm = mods[m]
        sh = boot.get(m, {}).get('points_mae', {}).get('share_regattas_site_better')
        cls = 'mv-site' if m == site else f'row{i % 2}'
        cells = [f'{mm["points_mae"]:.2f}', f'{mm["points_off_per_regatta"]:.1f}', f'{mm["rank_mae"]:.2f}',
                 f'{mm["spearman"]:.3f}', pct(mm['winner']), pct(mm['top3']),
                 '&mdash;' if (m == site or sh is None) else f'{sh * 100:.0f}%']
        rows += f'<tr class="{cls}"><td>{esc(labels[m])}</td>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr>'
    table = ('<div class="mv-tablewrap"><table class="results"><thead><tr><th>Model</th><th>Points off<br>per team</th>'
             '<th>Points off<br>per regatta</th><th>Places off<br>per team</th><th>Rank<br>correlation</th>'
             '<th>Winner<br>picked</th><th>Top three<br>found</th><th>Site model<br>closer in</th></tr></thead>'
             f'<tbody>{rows}</tbody></table></div>')

    def reg_href(r):
        path = r['url'].strip('/')
        return f'{P}{path}/' if r['league'] == 'ICSA' else f'{P}../issa/{path}/'

    def school_href(r, slug):
        return f'{P}schools/{slug}/' if r['league'] == 'ICSA' else f'{P}../issa/schools/{slug}/'

    simd = (d.get('simulation') or {}).get('all')
    sim_html = ''
    if simd:
        cal_rows = ''.join(
            f'<tr class="row{i % 2}"><td>{b["lo"] * 100:.0f}&ndash;{b["hi"] * 100:.0f}%</td>'
            f'<td>{b["mean_pred"] * 100:.1f}%</td><td>{b["observed"] * 100:.1f}%</td><td>{b["teams"]:,}</td></tr>'
            for i, b in enumerate(simd['win_calibration']))
        below10 = simd['surprise_below_10'] * 100
        indd = (d.get('simulation_independent') or {}).get('all')
        tau_ = d.get('form_tau')
        over_note = ''
        if simd['surprise_below_10'] > 0.2:
            over_note = ' So these probabilities are still too confident.'
        elif simd['surprise_below_10'] < 0.04:
            over_note = ' So these probabilities are, if anything, too cautious.'
        compare = ''
        offd = (d.get('simulation_offset') or {}).get('all')
        comp_ = d.get('form_components')
        if indd:
            ind_top = next((b for b in indd['win_calibration'] if b['lo'] >= 0.5), None)
            compare = (f'<p>Treating every race as an independent draw made the probabilities far too confident: standings '
                       f'that far off turned up at {indd["surprise_below_10"] * 100:.0f}% of championships instead of about 10%'
                       + (f', and teams given better than even odds averaged a {ind_top["mean_pred"] * 100:.0f}% win chance but won '
                          f'{ind_top["observed"] * 100:.0f}% of the time' if ind_top else '')
                       + '. Races in one regatta are not independent (see <a href="../#races-not-independent">how results '
                       'within a regatta are correlated</a>).')
            if comp_:
                compare += (' So each simulated regatta gives every sailor a good or bad regatta, form that drifts from race to '
                            'race, and the speed of whichever boat the rotation puts them in, with sizes fitted to those '
                            'correlations and scaled on the 2024&ndash;25 championships.')
                if d.get('sim_experience_table'):
                    compare += (' Swings are bigger for sailors whose ratings rest on fewer races.')
                if (d.get('sim_spread') or 1.0) > 1.0:
                    compare += (f' Ratings frozen a week ahead are a little too bunched together, so the gaps between them are '
                                f'stretched by {(d["sim_spread"] - 1) * 100:.0f}%.')
                if offd:
                    if abs(offd["winner_log_loss"] - simd["winner_log_loss"]) < 0.0005:
                        ll_cmp = f'the same winners&#39; log-loss, {simd["winner_log_loss"]:.3f}'
                    else:
                        lower = 'slightly lower' if offd["winner_log_loss"] < simd["winner_log_loss"] else 'slightly higher'
                        ll_cmp = (f'a {lower} winners&#39; log-loss of {offd["winner_log_loss"]:.3f}, against '
                                  f'{simd["winner_log_loss"]:.3f} for the full model')
                    compare += (f' A simpler version with only the good or bad regatta did about as well, with {ll_cmp}. '
                                f'Far-off standings turned up at {offd["surprise_below_10"] * 100:.0f}% of championships with '
                                f'the simpler version and {below10:.0f}% with the full model.')
            elif tau_:
                compare += (f' So each simulated regatta gives every sailor a good or bad regatta: one form offset, typically '
                            f'within &plusmn;{tau_ * 100:.0f} rating points, that lasts the whole event, chosen on the '
                            f'2024&ndash;25 championships.')
            compare += (f' The actual winners&#39; average log-loss went from {indd["winner_log_loss"]:.3f} with independent '
                        f'races to {simd["winner_log_loss"]:.3f} (lower is better).</p>')
        sim_html = f'''
    <h2>How likely was each result?</h2>
    <p>Predicted totals say where teams should finish, but a regatta of a few dozen races still carries a lot of luck.
    To put a probability on each result, every championship was simulated {simd["sims"]:,} times at its real number of
    races, using the site model&#39;s ratings and penalty risks from the week before: every race, every division,
    every penalty, and the way results within a regatta move together. That gives each team a chance to win and a chance of
    finishing where it did or better, and it measures how surprising the final standings were. Each team also gets two
    percentiles in its regatta&#39;s table: how many of the simulated regattas it beat on finishing place, and how many it
    beat on points. A team at 90 finished ahead of, or scored fewer points than, nine simulations in ten, so high numbers
    mark a regatta that went better than the ratings expected.</p>
    <p>If the probabilities are honest, teams given a win chance of about 30% should win about 30% of the time, and
    standings further from the prediction than 90% of simulations should turn up at about one championship in ten.
    They turned up at {below10:.0f}% of these {simd["regattas"]} championships, and at the typical championship
    {simd["surprise_median"] * 100:.0f}% of simulations were at least as far off as the real result.{over_note}</p>
    {compare}
    <div class="mv-tablewrap"><table class="results"><thead><tr><th>Win chance given</th><th>Average<br>given</th>
      <th>Actually<br>won</th><th>Teams</th></tr></thead><tbody>{cal_rows}</tbody></table></div>
'''

    sections = ''
    current = None
    compare = best if best != 'weekly_pl_v1' else next((m for m in others if m != 'weekly_pl_v1'), best)
    for r in d['regattas']:
        if r['season'] != current:
            current = r['season']
            sections += f'<h3 class="mv-season">{esc(season_label(current))}</h3>\n'
        rm = r['metrics']
        won = ' &middot; picked the winner' if rm[site]['winner'] >= 0.999 else ''
        sim_note = (f' &middot; winner&#39;s chance {_pct_small(r["sim"]["winner_win_prob"])} &middot; this far off in '
                    f'{_pct_small(r["sim"]["surprise_p"])} of simulations' if r.get('sim') else '')
        summary = (f'<a href="{reg_href(r)}">{esc(r["name"])}</a> '
                   f'<span class="mv-muted-inline">{esc(r["league"])} &middot; {esc(r["start_date"])} &middot; '
                   f'{r["teams"]} teams &middot; site model off {rm[site]["points_mae"]:.1f} pts/team, '
                   f'{esc(short[compare])} {rm[compare]["points_mae"]:.1f}{sim_note}{won}</span>')
        trs = ''
        for j, t in enumerate(r['results']):
            pe = t['expected_points'][site]
            diff = pe - t['actual_points']
            team = f' <span class="mv-muted-inline">{esc(t["team"])}</span>' if t['team'] and t['team'] != t['school'] else ''
            trs += (f'<tr class="row{j % 2}"><td>{t["actual_rank"]}</td>'
                    f'<td style="text-align:left;white-space:nowrap"><a href="{school_href(r, t["school_slug"])}">{esc(t["school"])}</a>{team}</td>'
                    f'<td>{t["actual_points"]:g}</td><td>{pe:.1f}</td>'
                    f'<td>{diff:+.1f}</td><td>{t["predicted_rank"][site]}</td>'
                    + (f'<td>{_pct_small(t["p_finish_or_better"])}</td>' if 'p_finish_or_better' in t else '')
                    + (f'<td>{t["finish_percentile"] * 100:.0f}</td><td>{t["points_percentile"] * 100:.0f}</td>'
                       if t.get('finish_percentile') is not None else '')
                    + f'<td class="mv-muted">{t["predicted_rank"][compare]}</td>'
                    + (f'<td class="mv-muted">{t["predicted_rank"]["weekly_pl_v1"]}</td>' if 'weekly_pl_v1' in t['predicted_rank'] else '')
                    + '</tr>')
        head = ('<th>Finish</th><th style="text-align:left">Team</th><th>Points</th><th>Predicted<br>points</th>'
                '<th>Predicted<br>minus actual</th><th>Predicted<br>finish</th>'
                + ('<th>Chance of this<br>finish or better</th>' if 'p_finish_or_better' in r['results'][0] else '')
                + ('<th>Finish<br>percentile</th><th>Points<br>percentile</th>'
                   if r['results'][0].get('finish_percentile') is not None else '')
                + f'<th>{esc(short[compare])}<br>finish</th>'
                + ('<th>Previous<br>model finish</th>' if 'weekly_pl_v1' in r['results'][0]['predicted_rank'] else ''))
        sections += (f'<details class="mv-reg"><summary>{summary}</summary><div class="mv-tablewrap">'
                     f'<table class="results"><thead><tr>{head}</tr></thead><tbody>{trs}</tbody></table></div></details>\n')

    body = f'''
  <div class="port">
  <div class="mv">
    <p style="margin:0.6em 0"><a href="../">&larr; How accurate are the ratings?</a></p>
    <p class="mv-lede">{lede}</p>
    {tiles_html}

    <h2>How predicted totals work</h2>
    <p>A team&#39;s predicted total is the sum, over every race its boats sailed, of the points the ratings expect
    them to score, with penalties left out: adding each boat&#39;s penalty risk makes the totals worse, because the
    typical team takes no penalty at all. The simulated chances further down do include penalties. For each race,
    a boat&#39;s expected points combine two separate predictions. Its penalty score gives the chance
    it scores fleet size plus one for an OCS, DNF or DNS. Otherwise it finishes among the other boats that finish, in an
    order drawn from the ratings. Summing a team&#39;s expected points over every race and division gives its predicted
    total, and sorting the totals gives the predicted standings. Actual points are recomputed from the recorded finishes
    with the same scoring, so penalty boats score fleet size plus one and redress is counted as the boat&#39;s average finish.</p>
    {ablation}

    <h2>Results</h2>
    <figure class="mv-figure"><figcaption>Average gap between a team&#39;s predicted and actual total points, all
      championships (lower is better)</figcaption>{chart}</figure>
    {table}
    <p style="color:#52514e;font-size:0.9em">A coin flip gives every team the same total, so it is off by
    {mods["uninformed"]["points_mae"]:.1f} points per team and is left off the chart. &ldquo;Site model closer in&rdquo;
    counts regattas where the site model&#39;s totals were nearer the real ones than that row&#39;s.</p>

{sim_html}
    <h2>Every championship</h2>
    <p>Teams are listed in their actual finishing order. Predictions are the site model&#39;s, from the week before the
    event, with the finish predicted by {esc(short[compare])} and the previous site model alongside.</p>
    {sections}

    <h2>Caveats</h2>
    <ul>
      <li>Actual totals are recomputed from recorded finishes and can differ slightly from official scores where a
      regatta used scoring penalties, averaged redress or tiebreakers.</li>
      <li>Only skippers are rated. A boat whose skipper has no earlier races gets their school&#39;s newcomer rating
      (the league&#39;s when the school has too few recent debuts) and the league penalty rate.</li>
      <li>Combined-division regattas are scored as a single fleet per race.</li>
    </ul>
  </div>
  </div>'''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Championship Predictions | Data Driven Sailing</title>
  <link rel="stylesheet" href="{P}styles.css" media="screen,print">
{METHODOLOGY_CSS}
{CHAMP_POINTS_CSS}
</head>
<body>
{nav(P + 'index.html', P + 'schools/index.html', P + 'sailors/index.html', P + 'img/icsa-logo-real.png',
     P + 'rankings/index.html', 'Rankings', logo_href=P + 'index.html')}
<div id="page-content">
  <div id="content-header">
    <h1><span id="left-fill"></span><span>Championship Predictions</span><span id="right-fill"></span></h1>
    <ul id="page-info">
      <li><span class="page-info-key">Regattas</span><span class="page-info-value">{S["regattas"]}</span></li>
      <li><span class="page-info-key">Teams</span><span class="page-info-value">{S["teams"]:,}</span></li>
      <li><span class="page-info-key">Ratings frozen</span><span class="page-info-value">the week before each event</span></li>
    </ul>
  </div>
{body}
</div>
</body>
</html>'''


def _womens_sailor_pages():
    """Sailor-page slugs of everyone who skippered or crewed in a women's event: the only gender signal the results carry."""
    out = set()
    with db() as con:
        for col in ('skipper', 'crew'):
            for raw, school_slug in con.execute(
                    f"SELECT DISTINCT {col}, school_slug FROM fleet_results WHERE is_womens=1 AND {col} != '' AND school_slug != ''"):
                nm = _parse_name(raw)[0]
                if nm and 'Unreported' not in nm:
                    out.add(sailor_slug(nm, school_slug))
    return out


def build_rankings_page():
    """Build ICSA power rankings — pre-rendered DOM rows + inline JS filtering."""
    import collections as _col, json as _json, math as _math

    latest_csv = os.path.join(BASE, 'analysis', 'plackett_luce', 'pl_weekly_latest.csv')
    weekly_csv = os.path.join(BASE, 'analysis', 'plackett_luce', 'pl_weekly_ratings.csv')
    if not os.path.exists(latest_csv):
        return None

    # ── School → (slug, conference), ICSA only ───────────────────────────────
    school_info = {}
    icsa_schools_path = os.path.join(CSV_DIR, 'icsa_schools.csv')
    if os.path.exists(icsa_schools_path):
        with open(icsa_schools_path, newline='', encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                school_info[row['school']] = (row['slug'], row.get('conference', ''))

    # ── Race counts ───────────────────────────────────────────────────────────
    race_counts = _col.defaultdict(int)
    with db() as con:
        for name, slug, cnt in con.execute(
            'SELECT skipper, school_slug, COUNT(*) FROM fleet_results'
            ' WHERE skipper != "" GROUP BY skipper, school_slug'
        ):
            race_counts[(name.lower().strip(), slug)] += cnt
        for name, slug, cnt in con.execute(
            'SELECT crew, school_slug, COUNT(*) FROM fleet_results'
            ' WHERE crew != "" GROUP BY crew, school_slug'
        ):
            race_counts[(name.lower().strip(), slug)] += cnt

    # ── Load ICSA sailors ─────────────────────────────────────────────────────
    sailor_list = []   # [name, school, year, slug, conf, races, pg_slug]
    sailor_id_to_idx = {}
    with open(latest_csv, newline='', encoding='utf-8') as f:
        for row in _csv.DictReader(f):
            if row.get('league') != 'ICSA':
                continue
            sid    = row['sailor_id']
            name   = row.get('name', '')
            school = row.get('school', '')
            slug, conf = school_info.get(school, ('', ''))
            pg_slug = sailor_slug(name, slug) if slug else ''
            races   = race_counts.get((name.lower().strip(), slug), 0)
            sailor_id_to_idx[sid] = len(sailor_list)
            sailor_list.append([name, school, row.get('year', ''), slug, conf, races, pg_slug])

    # ── Week → season ─────────────────────────────────────────────────────────
    def wk_to_season(wk):
        try:
            yr = int(wk[:4]) % 100
            return f's{yr:02d}' if int(wk[6:]) <= 25 else f'f{yr:02d}'
        except (ValueError, IndexError):
            return ''

    # ── One rating per (ICSA sailor, season) ──────────────────────────────────
    sailor_seasons = _col.defaultdict(dict)   # sid → {season: ri}
    if os.path.exists(weekly_csv):
        with open(weekly_csv, newline='', encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                sid = row['sailor_id']
                if row.get('league') != 'ICSA' or sid not in sailor_id_to_idx:
                    continue
                season = wk_to_season(row['iso_week'])
                if not season:
                    continue
                try:
                    ri = round(float(row['rating']) * 100)
                except (ValueError, KeyError):
                    continue
                sailor_seasons[sid][season] = ri   # last week wins

    # ── All seasons seen, chronological ───────────────────────────────────────
    all_seasons_chron = sorted(
        {s for seasons in sailor_seasons.values() for s in seasons},
        key=season_sort_key
    )

    # ── Fill-forward ratings string ───────────────────────────────────────────
    def _icsa_ratings_str(sid):
        by_season = sailor_seasons.get(sid, {})
        if not by_season:
            return ''
        first_idx = next((i for i, s in enumerate(all_seasons_chron) if s in by_season), None)
        if first_idx is None:
            return ''
        last_s = max(by_season, key=season_sort_key)
        last_idx = next((i for i, s in enumerate(all_seasons_chron) if s == last_s),
                        len(all_seasons_chron) - 1)
        last_r = None
        filled = {}
        for s in all_seasons_chron[first_idx:last_idx + 1]:
            if s in by_season:
                last_r = by_season[s]
            if last_r is not None:
                filled[s] = last_r
        return ','.join(f'{s}:{r/100:.3f}' for s, r in filled.items())

    # ── Latest rating per sailor ──────────────────────────────────────────────
    def _latest_rating(sid):
        by_season = sailor_seasons.get(sid, {})
        if not by_season:
            return None, None
        best = max(by_season, key=season_sort_key)
        return best, by_season[best]

    # ── Year ranges ───────────────────────────────────────────────────────────
    rating_years = sorted({2000 + int(s[1:]) for s in all_seasons_chron
                           if len(s) >= 3 and s[1:].isdigit()})
    min_rating_year = rating_years[0] if rating_years else 2020
    max_rating_year = rating_years[-1] if rating_years else 2026
    # The graduating class has left after a spring season, so a fall season defaults the class filter
    # to the next class up ('26 graduated before f26, so the default floor is '27).
    _latest_code = max(all_seasons_chron, key=lambda s: (2000 + int(s[1:]), 0 if s[0] == 's' else 1)) if all_seasons_chron else 'f26'
    default_class_min = int(_latest_code[1:]) + (1 if _latest_code[0] == 'f' else 0)

    def _norm_year(y):
        y = str(y or '').strip().lstrip("'")
        if y.isdigit():
            v = int(y)
            return 2000 + v if v < 100 else v
        return 0

    # ── Sort sailors by latest rating ─────────────────────────────────────────
    sailor_rows_data = []
    for sid, idx in sailor_id_to_idx.items():
        last_season, ri = _latest_rating(sid)
        if ri is None:
            continue
        ratings_str = _icsa_ratings_str(sid)
        if not ratings_str:
            continue
        sailor_rows_data.append((sid, idx, last_season, ri, ratings_str))
    sailor_rows_data.sort(key=lambda x: -x[3])

    # ── Percentile helpers ────────────────────────────────────────────────────
    n = len(sailor_rows_data)
    def _pct_ri(p):
        if not n: return 0
        return sailor_rows_data[min(int((1 - p / 100) * n), n - 1)][3]
    ri_99 = _pct_ri(99)
    ri_90 = _pct_ri(90)
    ri_50 = _pct_ri(50)
    def _vs(ri, bar): return f'{100.0 / (1.0 + _math.exp((bar - ri) / 100.0)):.1f}%'

    # ── ICSA_SCHOOLS JSON ─────────────────────────────────────────────────────
    icsa_schools_dict = {}
    for s in sailor_list:
        name, school, year, slug, conf, races, pg_slug = s
        if slug and school:
            icsa_schools_dict[slug] = school

    # ── Division filter options ───────────────────────────────────────────────
    confs_all = sorted({s[4] for s in sailor_list if s[4]})
    conf_options = ''.join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in confs_all)

    # ── Pre-render rows ───────────────────────────────────────────────────────
    _womens = _womens_sailor_pages()
    import help_tip
    _gender_tip = help_tip.tip("College sailing is coed and results do not record gender. <strong>Women</strong> are sailors who raced or crewed in at least one "
                               "women&#39;s event; <strong>Men</strong> are everyone else, which includes women who have only sailed open events. "
                               "<strong>Coed</strong> (the default) shows everyone.", title="Gender filter")
    sailor_rows_html = ''
    for i, (sid, idx, last_season, ri, ratings_str) in enumerate(sailor_rows_data[:1000]):
        s = sailor_list[idx]
        name, school, year, slug, conf, races, pg_slug = s
        norm_yr = _norm_year(year)
        yr2 = norm_yr % 100 if norm_yr else None
        yr_suffix = f" '{yr2:02d}" if yr2 is not None else ''
        logo = f'../icsa_logos/{esc(slug)}.png' if slug else ''
        logo_cell = (f'<img src="{logo}" alt="" height="30" width="45" '
                     f'onerror="this.style.display=\'none\'">' if logo else '')
        name_with_yr = f'{esc(name)}{yr_suffix}'
        sailor_href = f'../sailors/{esc(pg_slug)}/' if pg_slug else ''
        name_cell = (f'<a href="{sailor_href}">{name_with_yr}</a>'
                     if sailor_href else name_with_yr)
        school_cell = (f'<a href="../schools/{esc(slug)}/">{esc(school)}</a>'
                       if slug else esc(school))
        sailor_rows_html += (
            f'      <tr class="row{i % 2}"'
            f' data-races="{races}"'
            f' data-last-active="{esc(last_season or "")}"'
            f' data-school="{esc(school.lower())}"'
            f' data-slug="{esc(slug)}"'
            f' data-conf="{esc(conf)}"'
            f' data-year="{norm_yr or ""}"'
            f' data-w="{1 if pg_slug in _womens else 0}"'
            f' data-ratings="{ratings_str}">\n'
            f'        <td class="total rk-index">{i + 1}</td>\n'
            f'        <td class="burgee-cell">{logo_cell}</td>\n'
            f'        <td class="schoolname">{name_cell}</td>\n'
            f'        <td class="schoolname">{school_cell}</td>\n'
            f'        <td class="total rk-class-rank" style="text-align:center"></td>\n'
            f'        <td class="total rk-rating-val">{ri / 100:.3f}</td>\n'
            f'        <td class="rk-rating-season" style="text-align:center">{esc(last_season or "")}</td>\n'
            f'        <td style="text-align:center">{esc(last_season or "")}</td>\n'
            f'        <td style="text-align:center">{races}</td>\n'
            f'        <td style="text-align:center">{_vs(ri, ri_99)}</td>\n'
            f'        <td style="text-align:center">{_vs(ri, ri_90)}</td>\n'
            f'        <td style="text-align:center">{_vs(ri, ri_50)}</td>\n'
            f'      </tr>\n'
        )

    latest_season = all_seasons_chron[-1] if all_seasons_chron else ''
    icsa_schools_json = _json.dumps(icsa_schools_dict, ensure_ascii=False, separators=(',', ':'))
    import rankings_tabs
    _rt = rankings_tabs.build('ICSA', school_info, sailor_slug, womens=_womens_sailor_pages())

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Power Rankings | ICSA Sailing</title>
  <link rel="stylesheet" href="../styles.css" media="screen,print">
  <style>
    .rankings-filters{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.8em 1.4em;align-items:end;
      max-width:60em;margin:.4em auto .8em;padding:.8em 1em;background:#F2EDE6;border:1px solid #ddd;border-radius:.25em}}
    .rankings-filters label{{font:bold .68rem Arial,Helvetica,sans-serif;text-transform:uppercase;letter-spacing:.1em;color:#555}}
    .rankings-filters select,.rankings-filters input[type=text],.rankings-filters input[type=number]{{box-sizing:border-box;width:100%;height:2.1rem;
      font:.85rem "Lucida Grande",Tahoma,Verdana,sans-serif;padding:0 .5em;border:1px solid #bbb;border-radius:4px;background:#fff;color:#222}}
    .rankings-filters select:focus,.rankings-filters input:focus{{outline:0;border-color:#1a4f8a;box-shadow:0 0 0 2px rgba(26,79,138,.2)}}
    .filter-group{{display:flex;flex-direction:column;gap:.3em;min-width:0}}
    @media(max-width:600px){{.rankings-filters{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}
    #sailor-table th:nth-child(3),#sailor-table th:nth-child(4){{text-align:left}}
    #sailor-table th,#sailor-table td{{white-space:nowrap}}
    #sailor-table td.schoolname{{white-space:normal}}
    .slider-group input[type=range]{{width:160px;margin-top:3px;cursor:pointer}}
    .slider-val{{font-weight:normal;color:#555;margin-left:6px}}
    .dual-range{{position:relative;height:28px;width:100%;display:flex;align-items:center;margin-top:4px}}
    .dual-range input[type=range]{{position:absolute;width:100%;pointer-events:none;-webkit-appearance:none;appearance:none;background:transparent;height:4px;}}
    .dual-range input[type=range]::-webkit-slider-thumb{{pointer-events:all;-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:#1a4f8a;cursor:pointer;border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.3);}}
    .dual-range input[type=range]::-moz-range-thumb{{pointer-events:all;width:16px;height:16px;border-radius:50%;background:#1a4f8a;cursor:pointer;border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.3);border:none;}}
    .dual-range .dr-track{{position:absolute;width:100%;height:4px;background:#ddd;border-radius:2px;}}
    .dual-range .dr-fill{{position:absolute;height:4px;background:#1a4f8a;border-radius:2px;}}
  </style>
{_rt['css']}
</head>
<body>
{nav('../index.html', '../schools/index.html', '../sailors/index.html',
     '../img/icsa-logo-real.png', 'index.html', 'Rankings',
     logo_href='../index.html')}
<div id="page-content">
  <div id="content-header">
    <h1><span id="left-fill"></span><span>Power Rankings</span><span id="right-fill"></span></h1>
    <ul id="page-info">
      <li><strong>ICSA Plackett-Luce ratings</strong> &mdash; latest: {esc(latest_season)}</li>
      <li>School rank = avg of top-3 sailors per school in view</li>
{_methodology_li('../methodology/')}    </ul>
  </div>
  <div class="port">
    <div class="rankings-filters">
      <div class="filter-group">
        <label>Class Year <span class="slider-val" id="rk-class-lbl">'{default_class_min:02d} &ndash; '31</span></label>
        <div class="dual-range">
          <div class="dr-track"></div>
          <div class="dr-fill" id="rk-class-fill"></div>
          <input type="range" id="rk-class-min" min="0" max="31" value="{default_class_min}" step="1"
                 oninput="rkClassRange(this,false);applyFilter()">
          <input type="range" id="rk-class-max" min="0" max="31" value="31" step="1"
                 oninput="rkClassRange(this,true);applyFilter()">
        </div>
      </div>
      <div class="filter-group">
        <label>Rating Year <span class="slider-val" id="rk-year-lbl">Auto</span></label>
        <input type="range" id="rk-year" min="{min_rating_year}" max="{max_rating_year}" value="{max_rating_year}" step="1"
               oninput="ratingYearChanged(this)" style="width:100%;margin-top:3px;cursor:pointer">
      </div>
      <div class="filter-group">
        <label for="rk-conf">Division</label>
        <select id="rk-conf" onchange="applyFilter()">
          <option value="">All</option>
          {conf_options}
        </select>
      </div>
      <div class="filter-group">
        <label for="rk-gender">Gender {_gender_tip}</label>
        <select id="rk-gender" onchange="applyFilter()">
          <option value="">Coed</option>
          <option value="m">Men</option>
          <option value="w">Women</option>
        </select>
      </div>
      <div class="filter-group">
        <label for="rk-school">School</label>
        <input type="text" id="rk-school" placeholder="Search\u2026"
               oninput="applyFilter()" autocomplete="off">
      </div>
      <div class="filter-group">
        <label for="min-races-cb">Min races</label>
        <input type="number" id="min-races-cb" oninput="applyFilter()" value="10" min="0">
      </div>
    </div>
    <div class="tabs">
      <button class="tab-btn active" onclick="showTab(this,\'tab-sailors\')">Sailor Rankings</button>
      <button class="tab-btn" onclick="showTab(this,\'tab-schools\')">School Rankings</button>
    </div>
    <div id="tab-sailors" class="tab-panel active">
{_rt['sailor_pills']}
      <table class="results" id="sailor-table">
        <thead><tr>
            <th>#</th><th></th><th>Sailor</th><th>School</th>
            <th>Class Rank</th><th>Rating</th><th>Rating Season</th><th>Last Active</th><th>Races</th>
            <th title="Probability of beating the 99th-percentile sailor">vs p99</th>
            <th title="Probability of beating the 90th-percentile sailor">vs p90</th>
            <th title="Probability of beating the 50th-percentile sailor (median)">vs p50</th>
        </tr></thead>
        <tbody>
{sailor_rows_html}        </tbody>
      </table>
{_rt['sailor_tables']}
    </div>
    <div id="tab-schools" class="tab-panel">
{_rt['school_pills']}
      <table class="results" id="icsa-school-table">
        <thead><tr>
            <th>#</th><th></th><th>School</th>
            <th title="Average of top-3 sailors in class year selection">Avg Rating</th>
            <th title="Sailors in class year selection">Sailors</th>
        </tr></thead>
        <tbody id="icsa-school-tbody"></tbody>
      </table>
{_rt['school_tables']}
    </div>
  </div>
</div>
<script>
var ICSA_SCHOOLS={icsa_schools_json};
function _esc(s){{return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}}
function showTab(btn, id) {{
  btn.closest('.tabs').querySelectorAll('.tab-btn').forEach(function(b){{b.classList.remove('active');}});
  btn.classList.add('active');
  document.querySelectorAll('.tab-panel').forEach(function(p){{p.classList.remove('active');}});
  document.getElementById(id).classList.add('active');
}}
function renderSchools(visibleRows) {{
  var B={{}};
  visibleRows.forEach(function(r) {{
    var slug=r.dataset.slug||'';
    if(!slug)return;
    var ri=parseFloat(r.querySelector('.rk-rating-val').textContent)||0;
    if(!B[slug])B[slug]=[];
    B[slug].push(ri);
  }});
  var list=[];
  for(var slug in B) {{
    var sorted=B[slug].slice().sort(function(a,b){{return b-a;}});
    var top3=sorted.slice(0,3);
    var avg=top3.reduce(function(a,v){{return a+v;}},0)/top3.length;
    list.push({{slug:slug,name:ICSA_SCHOOLS[slug]||slug,avg:avg,count:B[slug].length}});
  }}
  list.sort(function(a,b){{return b.avg-a.avg;}});
  var html='';
  list.slice(0,150).forEach(function(s,i) {{
    var logo=s.slug?'../icsa_logos/'+s.slug+'.png':'';
    var lc=logo?'<img src="'+logo+'" alt="" height="30" width="45" onerror="this.hidden=1">':'';
    var sc=s.slug?'<a href="../schools/'+s.slug+'/">'+_esc(s.name)+'</a>':_esc(s.name);
    html+='<tr class="row'+(i%2)+'"><td class="total">'+(i+1)+'</td><td class="burgee-cell">'+lc+'</td><td class="schoolname">'+sc+'</td><td class="total">'+s.avg.toFixed(3)+'</td><td style="text-align:center">'+s.count+'</td></tr>';
  }});
  document.getElementById('icsa-school-tbody').innerHTML=html;
}}
var rkRatingYearManual=false;
function _pad2i(n){{return n<10?'0'+n:''+n;}}
function ratingYearChanged(el) {{
  rkRatingYearManual=true;
  document.getElementById('rk-year-lbl').textContent=el.value;
  applyFilter();
}}
function rkClassRange(el,isMax) {{
  var mn=parseInt(document.getElementById('rk-class-min').value);
  var mx=parseInt(document.getElementById('rk-class-max').value);
  if(mn>mx){{if(isMax){{document.getElementById('rk-class-max').value=mn;mx=mn;}}else{{document.getElementById('rk-class-min').value=mx;mn=mx;}}}}
  var pct1=(mn/31)*100,pct2=(mx/31)*100;
  document.getElementById('rk-class-fill').style.left=pct1+'%';
  document.getElementById('rk-class-fill').style.width=(pct2-pct1)+'%';
  var all=(mn===0&&mx===31);
  document.getElementById('rk-class-lbl').textContent=all?("'00 \u2013 '31"):("'"+_pad2i(mn)+" \u2013 '"+_pad2i(mx));
  if(!rkRatingYearManual) {{
    document.getElementById('rk-year-lbl').textContent='Auto';
  }}
}}
function _seasonVal(s) {{
  if(!s)return -1;
  var yr=parseInt(s.slice(1));
  return yr*2+(s[0]==='f'?1:0);
}}
function _parseRatings(str) {{
  var out={{}};
  if(!str)return out;
  str.split(',').forEach(function(part) {{
    var i=part.indexOf(':');
    if(i>0)out[part.slice(0,i)]=parseFloat(part.slice(i+1));
  }});
  return out;
}}
function _bestSeasonFor(ratings, ratingYear) {{
  // Cap at spring of ratingYear (academic year: class of 2023 ends at s23, not f23)
  var maxVal=(ratingYear%100)*2;
  var best='',bestVal=-1;
  for(var s in ratings) {{
    var v=_seasonVal(s);
    if(v<=maxVal&&v>bestVal){{bestVal=v;best=s;}}
  }}
  return best;
}}
function applyFilter() {{
  var minR       = parseInt(document.getElementById('min-races-cb').value) || 0;
  var clsMin     = parseInt(document.getElementById('rk-class-min').value);
  var clsMax     = parseInt(document.getElementById('rk-class-max').value);
  var allClasses = (clsMin===0&&clsMax===31);
  var confF      = document.getElementById('rk-conf').value;
  var genderF    = document.getElementById('rk-gender').value;
  var manualYear = rkRatingYearManual ? parseInt(document.getElementById('rk-year').value) : null;
  var schoolF    = document.getElementById('rk-school').value.toLowerCase().trim();
  // First pass: class + races + division filter determines ranked set
  var ranked = [];
  document.querySelectorAll('#sailor-table tbody tr').forEach(function(r) {{
    var rowYr2  = r.dataset.year ? (parseInt(r.dataset.year) % 100) : -1;
    var classOk = allClasses || (rowYr2 >= clsMin && rowYr2 <= clsMax);
    var racesOk = parseInt(r.dataset.races || 0) >= minR;
    var confOk  = !confF || (r.dataset.conf||'') === confF;
    var genderOk = !genderF || ((r.dataset.w === '1') === (genderF === 'w'));
    var ratings = _parseRatings(r.dataset.ratings);
    var rowYear = manualYear !== null ? manualYear : Math.min(parseInt(r.dataset.year)||{max_rating_year}, 2000+clsMin);
    var season  = _bestSeasonFor(ratings, rowYear);
    if(season !== '' && classOk && racesOk && confOk && genderOk) {{
      r.querySelector('.rk-rating-val').textContent    = ratings[season].toFixed(3);
      r.querySelector('.rk-rating-season').textContent = season;
      ranked.push(r);
    }} else {{
      r.style.display = 'none';
    }}
  }});
  // Sort ranked by rating, re-insert into DOM
  ranked.sort(function(a,b) {{
    return parseFloat(b.querySelector('.rk-rating-val').textContent)
          -parseFloat(a.querySelector('.rk-rating-val').textContent);
  }});
  var tbody = document.querySelector('#sailor-table tbody');
  ranked.forEach(function(r){{tbody.appendChild(r);}});
  // Second pass: assign stable index/class-rank/stripe, then apply school filter for display only
  var classRank = {{}};
  ranked.forEach(function(r, idx) {{
    var yr = r.dataset.year || '';
    classRank[yr] = (classRank[yr] || 0) + 1;
    r.querySelector('.rk-index').textContent      = idx + 1;
    r.querySelector('.rk-class-rank').textContent = classRank[yr];
    r.className = 'row' + (idx % 2);
    r.style.display = (!schoolF || (r.dataset.school||'').indexOf(schoolF) !== -1) ? '' : 'none';
  }});
  renderSchools(ranked);
  if(window.applyDiscFilters)applyDiscFilters();
}}
{_rt['js']}
rkClassRange(null,false);
applyFilter();
</script>
</body>
</html>'''


def build_schools_page():
    with db() as con:
        rows = con.execute("""
            SELECT school, slug, city, state, conference
            FROM schools
            ORDER BY conference, school
        """).fetchall()

    by_conf = {}
    for school, slug, city, state, conf in rows:
        conf = conf or 'Other'
        by_conf.setdefault(conf, []).append((school, slug, city or '', state or ''))

    n_conf   = len(by_conf)
    n_school = sum(len(v) for v in by_conf.values())

    ports = ''
    for conf in sorted(by_conf):
        if conf.upper() == 'GUEST':
            continue
        conf_slug = conf.lower()
        cards = ''
        for school, slug, city, state in by_conf[conf]:
            logo = f'../icsa_logos/{slug}.png'
            location = f'{city}, {state}' if city and state else (city or state)
            cards += (
                f'  <a class="school-card" href="../schools/{esc(slug)}/">\n'
                f'    <img src="{esc(logo)}" alt="{esc(school)}" '
                f'onerror="this.style.visibility=\'hidden\'">\n'
                f'    <span class="school-card-name">{esc(school)}</span>\n'
                f'    <span class="school-card-loc">{esc(location)}</span>\n'
                f'  </a>\n'
            )
        ports += f'''
  <div class="port" id="{esc(conf)}">
    <h3><a href="../conferences/{esc(conf_slug)}/">{esc(conf)} Conference</a></h3>
    <div class="school-cards">
{cards}    </div>
  </div>
'''

    # Build map data from venues.csv
    import csv as _csv, json as _json
    CONF_COLORS = {"NEISA":"#1a4f8a","MAISA":"#8b1a1a","SAISA":"#1a6b2f","SEISA":"#b85c00","PCCSC":"#5a1a8a","NWICSA":"#0a6b6b","MCSA":"#7a6200"}
    map_schools = []
    venues_path = os.path.join(BASE, 'data', 'venues.csv')
    if os.path.exists(venues_path):
        with open(venues_path, newline='', encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                if row['league'] != 'ICSA' or not row['lat'] or not row['lng']: continue
                if row['conference'].strip() not in CONF_COLORS: continue
                map_schools.append({'slug': row['slug'], 'name': row['school'],
                    'city': row['city'], 'state': row['state'],
                    'conf': row['conference'].strip(),
                    'lat': float(row['lat']), 'lng': float(row['lng'])})
    schools_json = _json.dumps(map_schools, separators=(',', ':'))
    conf_colors_json = _json.dumps(CONF_COLORS)
    conf_opts = ''.join(f'<option value="{c}">{c}</option>' for c in sorted(CONF_COLORS))

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>All Schools | ICSA Real-Time Regatta Results</title>
  <link rel="stylesheet" href="../styles.css" media="screen,print">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    #schools-map {{ height: 500px; width: 100%; border-radius: 4px; border: 1px solid #ccc; margin: 0.8em 0; }}
    .map-legend {{ display: flex; flex-wrap: wrap; gap: 0.6em 1.2em; padding: 0.8em 1em; background: #f5f5f5; border-radius: 4px; margin-bottom: 0.8em; font-size: 0.9em; }}
    .legend-item {{ display: flex; align-items: center; gap: 0.4em; }}
    .legend-dot {{ width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0; border: 1.5px solid rgba(0,0,0,0.25); }}
    .map-filters {{ display: flex; flex-wrap: wrap; gap: 0.8em; padding: 0.8em 1em; background: #f5f5f5; border-radius: 4px; margin-bottom: 0.6em; align-items: center; }}
    .map-filters label {{ font-size: 0.9em; font-weight: bold; color: #333; display: flex; align-items: center; gap: 0.4em; }}
    .map-filters select {{ font-size: 0.95em; padding: 0.3em 0.5em; border: 1px solid #bbb; border-radius: 4px; font-family: inherit; }}
    .school-map-count {{ color: #666; font-size: 0.85em; }}
  </style>
</head>
<body>
{nav('../index.html', 'index.html', '../sailors/index.html', '../img/icsa-logo-real.png',
     'index.html', 'Schools', logo_href='../index.html')}
<div id="page-content">

  <div id="content-header">
    <h1>
      <span id="left-fill"></span>
      <span>ICSA Conferences</span>
      <span id="right-fill"></span>
    </h1>
    <ul id="page-info">
      <li><span class="page-info-key"># of Conferences</span><span class="page-info-value">{n_conf}</span></li>
      <li><span class="page-info-key"># of Schools</span><span class="page-info-value">{n_school}</span></li>
    </ul>
  </div>

  <div class="port" style="margin-top:1.5em;">
    <div class="map-filters">
      <label>Conference
        <select id="f-conf" onchange="applyMapFilter()">
          <option value="">All Conferences</option>{conf_opts}
        </select>
      </label>
      <span class="school-map-count" id="school-map-count"></span>
    </div>
    <div class="map-legend" id="map-legend"></div>
    <div id="schools-map"></div>
  </div>
{ports}
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const SCHOOLS={schools_json};
const CONF_COLORS={conf_colors_json};
const legend=document.getElementById('map-legend');
Object.entries(CONF_COLORS).sort().forEach(([c,col])=>{{
  const n=SCHOOLS.filter(s=>s.conf===c).length;
  legend.innerHTML+=`<span class="legend-item"><span class="legend-dot" style="background:${{col}}"></span><span><strong>${{c}}</strong> <span style="color:#888">(${{n}})</span></span></span>`;
}});
const map=L.map('schools-map').setView([38.5,-96],4);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',maxZoom:18}}).addTo(map);
function makeIcon(color){{
  return L.divIcon({{html:`<svg xmlns="http://www.w3.org/2000/svg" width="22" height="32" viewBox="0 0 22 32"><path d="M11 0C4.9 0 0 4.9 0 11c0 7.7 11 21 11 21s11-13.3 11-21C22 4.9 17.1 0 11 0z" fill="${{color}}" stroke="rgba(0,0,0,0.35)" stroke-width="1.2"/><circle cx="11" cy="11" r="4.5" fill="white" opacity="0.85"/></svg>`,iconSize:[22,32],iconAnchor:[11,32],popupAnchor:[0,-32],className:''}});
}}
const markers=SCHOOLS.map(s=>{{
  const col=CONF_COLORS[s.conf]||'#888';
  const m=L.marker([s.lat,s.lng],{{icon:makeIcon(col)}});
  const badge=`<span style="background:${{col}};color:#fff;padding:1px 6px;border-radius:3px;font-size:0.8em;font-weight:bold">${{s.conf}}</span>`;
  m.bindPopup(`<strong><a href="./${{s.slug}}/" style="color:#1a4f8a">${{s.name}}</a></strong><br>${{s.city}}, ${{s.state}}<br>${{badge}}`);
  m._conf=s.conf;
  return m;
}});
const layerGroup=L.layerGroup(markers).addTo(map);
function applyMapFilter(){{
  const conf=document.getElementById('f-conf').value;
  layerGroup.clearLayers();
  const visible=markers.filter(m=>!conf||m._conf===conf);
  visible.forEach(m=>layerGroup.addLayer(m));
  document.getElementById('school-map-count').textContent=visible.length===markers.length?`${{markers.length}} schools`:`${{visible.length}} of ${{markers.length}} schools`;
  if(conf&&visible.length>0){{map.fitBounds(L.featureGroup(visible).getBounds(),{{padding:[50,50]}});}}
  else{{map.setView([38.5,-96],4);}}
}}
applyMapFilter();
</script>
</body>
</html>
'''


# ── individual school page builder ────────────────────────────────────────────

def _precompute_school_data():
    """Run 3 bulk queries and return a dict:
      school_slug -> {season -> [regatta_dict, ...]}
    where each regatta_dict has: season, regatta_name, regatta_url, type, place, n_schools.
    """
    from collections import defaultdict
    result = defaultdict(lambda: defaultdict(list))
    seen = defaultdict(set)  # school_slug -> set of regatta_urls added

    with db() as con:
        import leading
        _fin = leading.school_finishes(con)
        for season, name, url, slug, place, n_schools, typ in _fin:
            if typ == 'keelboat':
                continue
            result[slug][season].append({'season': season, 'regatta_name': name,
                                         'regatta_url': url, 'type': typ,
                                         'place': place, 'n_schools': n_schools})
            seen[slug].add(url)

        tr_sql = """
            WITH counts AS (
                SELECT regatta_url, COUNT(DISTINCT school_slug) AS n_schools
                FROM tr_standings GROUP BY regatta_url
            )
            SELECT tr.season, tr.regatta_name, tr.regatta_url,
                   tr.school_slug, tr.place, counts.n_schools
            FROM tr_standings tr
            JOIN counts ON counts.regatta_url = tr.regatta_url
        """
        for season, name, url, slug, place, n_schools in con.execute(tr_sql):
            if url not in seen[slug]:
                result[slug][season].append({'season': season, 'regatta_name': name,
                                             'regatta_url': url, 'type': 'tr',
                                             'place': place, 'n_schools': n_schools})
                seen[slug].add(url)

        for season, name, url, slug, place, n_schools, typ in _fin:
            if typ != 'keelboat':
                continue
            if url not in seen[slug]:
                result[slug][season].append({'season': season, 'regatta_name': name,
                                             'regatta_url': url, 'type': 'keelboat',
                                             'place': place, 'n_schools': n_schools})


    # Build date map from main CSV: (season, regatta_name_lower) -> start_date
    date_map = {}
    for csv_path in [os.path.join(CSV_DIR, 'icsa_regattas.csv')]:
        try:
            import csv as _csv
            with open(csv_path, encoding='utf-8') as f:
                for row in _csv.DictReader(f):
                    date_map[(row['season'], row['name'].lower())] = row.get('start_date', '')
        except FileNotFoundError:
            pass

    # Attach start_date to existing regatta dicts
    for slug in result:
        for season in result[slug]:
            for r in result[slug][season]:
                r['start_date'] = date_map.get((r['season'], r['regatta_name'].lower()), '')
                r['upcoming'] = False

    # Load PL ratings: school -> top-2 average rating
    pl_path = os.path.join('analysis', 'plackett_luce', 'pl_weekly_latest.csv')
    school_ratings = {}  # school_name.lower() -> sorted list of ratings
    try:
        import csv as _csv
        with open(pl_path, encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                sn = row.get('school', '').lower()
                try:
                    school_ratings.setdefault(sn, []).append(float(row['rating']))
                except (ValueError, KeyError):
                    pass
        for sn in school_ratings:
            school_ratings[sn].sort(reverse=True)
    except FileNotFoundError:
        pass

    def _school_strength(school_name):
        top2 = school_ratings.get(school_name.lower(), [])[:2]
        return sum(top2) / len(top2) if top2 else 0.0

    # Load upcoming entries CSV and precompute per-regatta avg strength
    entries_path = os.path.join(CSV_DIR, 'icsa_upcoming_entries.csv')
    reg_entries = defaultdict(list)   # regatta_url -> [school_name, ...]
    reg_meta    = {}                  # regatta_url -> {name, season, date, type, scoring}
    try:
        import csv as _csv
        from urllib.parse import urlparse as _urlparse
        with open(entries_path, encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                url = row['regatta_url']
                if url.startswith('http'):
                    url = _urlparse(url).path
                reg_entries[url].append(row['school_name'])
                if url not in reg_meta:
                    scoring = row.get('scoring', '')
                    reg_meta[url] = {
                        'name':    row['regatta'],
                        'season':  row['season'],
                        'date':    row.get('date', ''),
                        'type':    'tr' if 'team' in scoring.lower() else 'fleet',
                        'scoring': scoring,
                        'status':  row.get('regatta_status', 'upcoming'),
                    }
    except FileNotFoundError:
        pass

    def _reg_avg_strength(url):
        schools = reg_entries.get(url, [])
        strengths = [_school_strength(s) for s in schools]
        return sum(strengths) / len(strengths) if strengths else 0.0

    # Add upcoming regattas to result for schools that appear in entries
    for url, schools in reg_entries.items():
        meta = reg_meta.get(url, {})
        season = meta.get('season', '')
        if not season:
            continue
        strength = _reg_avg_strength(url)
        n_schools = len(set(schools))
        for school_name in set(schools):
            # Find slug for this school_name via existing data
            slug = None
            for s in result:
                for sn in result[s].values():
                    for r in sn:
                        pass  # can't reverse-lookup slug from school_name here
            # Instead, look it up from the entries CSV school_slug
            pass  # handled below

    # Better: reload entries CSV with school_slug
    try:
        import csv as _csv
        from urllib.parse import urlparse as _urlparse
        slug_entries = defaultdict(list)  # school_slug -> [{url, meta}]
        with open(entries_path, encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                url = row['regatta_url']
                if url.startswith('http'):
                    url = _urlparse(url).path
                slug_entries[row['school_slug']].append(url)
        for school_slug, urls in slug_entries.items():
            for url in urls:
                meta = reg_meta.get(url, {})
                season = meta.get('season', '')
                if not season:
                    continue
                # Skip if already in result (shouldn't be, but safety check)
                existing_urls = {r['regatta_url'] for r in result[school_slug].get(season, [])}
                if url in existing_urls:
                    continue
                result[school_slug][season].append({
                    'season':       season,
                    'regatta_name': meta.get('name', url),
                    'regatta_url':  url,
                    'type':         meta.get('type', 'fleet'),
                    'place':        None,
                    'n_schools':    len(set(reg_entries.get(url, []))),
                    'start_date':   meta.get('date', ''),
                    'upcoming':     True,
                    'strength':     _reg_avg_strength(url),
                })
    except FileNotFoundError:
        pass

    # Sort each season's list: upcoming first (by strength desc), past by date desc
    def _date_key(r):
        d = r.get('start_date', '')
        if d and '/' in d:
            p = d.split('/')
            if len(p) >= 3:
                try:
                    return (int(p[2]), int(p[0]), int(p[1]))
                except ValueError:
                    pass
        return (0, 0, 0)

    for slug in result:
        for season in result[slug]:
            upcoming_list = [r for r in result[slug][season] if r.get('upcoming')]
            past_list     = [r for r in result[slug][season] if not r.get('upcoming')]
            upcoming_list.sort(key=lambda r: r.get('strength', 0), reverse=True)
            past_list.sort(key=_date_key, reverse=True)
            result[slug][season] = upcoming_list + past_list

    return result


def _school_page_html(school_slug, school_name, city, state, conference,
                      season, active_seasons, depth, school_data=None):
    """Render one school season page.

    depth=2  → website/schools/{slug}/index.html       (paths like ../../)
    depth=3  → website/schools/{slug}/{season}/index.html (paths like ../../../)
    school_data: precomputed dict {season -> [regatta_dict, ...]}
    """
    import crew_site
    regattas = (school_data or {}).get(season, [])
    location = f'{city}, {state}' if city and state else (city or state or '')
    up = '../' * depth
    logo = f'{up}icsa_logos/{esc(school_slug)}.png'
    conf_slug = (conference or '').lower()
    label = season_label(season)

    past_for_stats = [r for r in regattas if not r.get('upcoming')]
    n_regattas = len(past_for_stats)
    wins = sum(1 for r in past_for_stats if r['place'] == 1)

    # Roster: collect unique sailors for this school+season
    with db() as con:
        fleet_sailors = con.execute('''
            SELECT skipper, skipper_grade FROM fleet_results
            WHERE school_slug=? AND season=? AND skipper != ''
            UNION
            SELECT crew, crew_grade FROM fleet_results
            WHERE school_slug=? AND season=? AND crew != ''
        ''', (school_slug, season, school_slug, season)).fetchall()
        tr_sailor_rows = con.execute('''
            SELECT skipper, skipper_grade FROM tr_sailors
            WHERE school_slug=? AND regatta_url LIKE ?  AND skipper != ''
            UNION
            SELECT crew, crew_grade FROM tr_sailors
            WHERE school_slug=? AND regatta_url LIKE ? AND crew != ''
        ''', (school_slug, f'/{season}/%', school_slug, f'/{season}/%')).fetchall()

    sailors_seen = {}
    for name, grade in fleet_sailors + tr_sailor_rows:
        if name and name not in sailors_seen:
            sailors_seen[name] = grade or ''

    def _grade_to_year(grade):
        """Convert "'27" → "2027", return '' if unrecognized."""
        g = (grade or '').strip().lstrip("'")
        if g.isdigit() and len(g) == 2:
            return f'20{g}'
        return ''

    roster_html = ''
    if sailors_seen:
        # Group by class year
        by_year = {}
        for name in sorted(sailors_seen):
            year = _grade_to_year(sailors_seen[name])
            by_year.setdefault(year, []).append(name)
        sorted_years = sorted((y for y in by_year if y), reverse=True) + (['' ] if '' in by_year else [])

        tables = ''
        for year in sorted_years:
            names = by_year[year]
            heading = f'Class of {year}' if year else 'Unknown'
            t_rows = ''
            for i, name in enumerate(names):
                slug = sailor_slug(name, school_slug)
                sailor_href = f'{up}sailors/{esc(slug)}/'
                t_rows += (
                    f'<tr class="row{i % 2}">'
                    f'<td class="sailor-name"><a href="{sailor_href}">{esc(name)}</a></td>'
                    f'</tr>\n'
                )
            tables += (
                f'<div style="flex:0 0 auto;">'
                f'<div style="font-weight:bold;margin-bottom:0.3em;font-size:0.9em">{esc(heading)}</div>'
                f'<table class="roster-table"><tbody>\n{t_rows}</tbody></table>'
                f'</div>\n'
            )
        roster_html = f'''  <div class="port">
    <h3>Roster &ndash; {esc(label)}</h3>
    <div style="display:flex;flex-wrap:wrap;gap:1.5em;align-items:flex-start;justify-content:center;">
{tables}    </div>
  </div>'''

    # Split into upcoming and past
    upcoming_regattas = [r for r in regattas if r.get('upcoming')]
    past_regattas     = [r for r in regattas if not r.get('upcoming')]

    def _reg_rows(reg_list, show_finish=True):
        rows = ''
        for i, r in enumerate(reg_list):
            reg_slug = r['regatta_url'].strip('/').split('/')[-1]
            reg_href = f'{up}{esc(r["season"])}/{esc(reg_slug)}/'
            type_label = {'fleet': 'Fleet Race', 'tr': 'Team Race', 'keelboat': 'Keelboat', 'singlehanded': 'Singlehanded'}.get(r['type'], r['type'])
            date_str = r.get('start_date', '').split('@')[0].strip() if r.get('start_date') else ''
            if show_finish:
                finish_td = f'<td style="text-align:right">{r["place"]} / {r["n_schools"]}</td>'
            else:
                finish_td = f'<td style="text-align:center;color:#888;font-size:.85em">{esc(date_str)}</td>'
            rows += (
                f'      <tr class="row{i % 2}">\n'
                f'        <td class="schoolname"><a href="{reg_href}">{esc(r["regatta_name"])}</a></td>\n'
                f'        <td style="text-align:center">{esc(type_label)}</td>\n'
                f'        {finish_td}\n'
                f'      </tr>\n'
            )
        return rows

    upcoming_rows = _reg_rows(upcoming_regattas, show_finish=False)
    past_rows     = _reg_rows(past_regattas, show_finish=True)

    def _table(heading, rows_html, third_header):
        return f'''  <div class="port">
    <h3>{heading}</h3>
    <table class="results">
      <thead>
        <tr>
          <th style="text-align:left">Regatta</th>
          <th style="text-align:center">Type</th>
          <th style="text-align:right">{third_header}</th>
        </tr>
      </thead>
      <tbody>
{rows_html}      </tbody>
    </table>
  </div>'''

    reg_port = ''
    if upcoming_rows:
        reg_port += _table(f'{esc(label)} Upcoming', upcoming_rows, 'Date')
    if past_rows:
        reg_port += '\n' + _table(f'{esc(label)} Results', past_rows, 'Finish')
    if not reg_port:
        reg_port = f'  <div class="port"><p>No results recorded for {esc(label)}.</p></div>'

    conf_link = (f'<a href="{up}conferences/{esc(conf_slug)}/">{esc(conference)}</a>'
                 if conference else '—')

    # Season history sidebar
    season_links = ''
    school_up = '../' * (depth - 1)   # path back to school root from current page
    for s in active_seasons:
        if s == season and depth == 2:
            href = 'index.html'
        elif s == season:
            href = 'index.html'
        else:
            # from depth-2 (school root): go into {s}/
            # from depth-3 (season page): go up then into {s}/
            href = (f'{s}/' if depth == 2 else f'../{s}/')
        season_links += f'      <li><a href="{href}">{esc(season_label(s))}</a></li>\n'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(school_name)} &ndash; {esc(label)} | ICSA Real-Time Regatta Results</title>
  <link rel="stylesheet" href="{up}styles.css" media="screen,print">
</head>
<body>
{nav(f'{up}index.html', f'{up}schools/index.html', f'{up}sailors/index.html',
     f'{up}img/icsa-logo-real.png', f'{up}schools/index.html', 'Schools', logo_href=f'{up}index.html', fantasy=f'{up}fantasy/index.html')}
<div id="page-content">

  <div id="content-header">
    <h1>
      <span id="left-fill"></span>
      <span>
        <img src="{logo}" alt="{esc(school_name)}" style="height:48px;width:auto;vertical-align:middle;margin-right:0.4em" onerror="this.style.display=\'none\'">
        {esc(school_name)}
      </span>
      <span id="right-fill"></span>
    </h1>
    <ul id="page-info">
      <li><span class="page-info-key">Conference</span><span class="page-info-value">{conf_link}</span></li>
      {"<li><span class='page-info-key'>Location</span><span class='page-info-value'>" + esc(location) + "</span></li>" if location else ""}
      <li><span class="page-info-key">Season</span><span class="page-info-value">{esc(label)}</span></li>
      <li><span class="page-info-key">Regattas</span><span class="page-info-value">{n_regattas}</span></li>
      <li><span class="page-info-key">Wins</span><span class="page-info-value">{wins}</span></li>
    </ul>
  </div>

{crew_site.tabs_block('ICSA', school_slug, season, up, reg_port, roster_html, school_name)}

  <div id="submenu-wrapper">
    <h3 class="nav">Season History</h3>
    <ul id="other-seasons">
{season_links}    </ul>
  </div>

</div>
</body>
</html>'''


def build_school_page(school_slug, school_name, city, state, conference,
                      active_seasons, school_data):
    """Build the main school page (shows most-recent active season, depth=2)."""
    if not active_seasons:
        return None
    latest = active_seasons[0]
    return _school_page_html(school_slug, school_name, city, state, conference,
                             latest, active_seasons, depth=2, school_data=school_data)


def build_school_season_page(school_slug, school_name, city, state, conference,
                             season, active_seasons, school_data):
    """Build a per-season school page at schools/{slug}/{season}/ (depth=3)."""
    return _school_page_html(school_slug, school_name, city, state, conference,
                             season, active_seasons, depth=3, school_data=school_data)


# ── conference page builders ──────────────────────────────────────────────────

def _school_conf_map():
    with db() as con:
        return {school: conf for school, conf in
                con.execute('SELECT school, conference FROM schools')}


def _regatta_conference(regatta_url, school_conf):
    """Return the conference that contributed the most schools to a regatta."""
    with db() as con:
        schools = [r[0] for r in con.execute(
            'SELECT DISTINCT school FROM fleet_results WHERE regatta_url=?', (regatta_url,)
        )]
        if not schools:
            schools = [r[0] for r in con.execute(
                'SELECT DISTINCT school FROM tr_standings WHERE regatta_url=?', (regatta_url,)
            )]
    counts = {}
    for s in schools:
        c = school_conf.get(s, '')
        if c and c.upper() != 'GUEST':
            counts[c] = counts.get(c, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _conf_season_regattas(conference, season, school_conf):
    """Return list of (name, url, host, type, scoring, start_date, status) for a conference+season."""
    meta = _get_regatta_meta()
    with db() as con:
        urls_fleet = {url: name for name, url in con.execute(
            'SELECT DISTINCT regatta_name, regatta_url FROM fleet_results WHERE season=?', (season,)
        ).fetchall()}
        urls_tr = {url: name for name, url in con.execute(
            'SELECT DISTINCT regatta_name, regatta_url FROM tr_standings WHERE season=?', (season,)
        ).fetchall()}
    all_urls = {**urls_fleet}
    for u, n in urls_tr.items():
        if u not in all_urls:
            all_urls[u] = n

    results = []
    for url, name in all_urls.items():
        if _regatta_conference(url, school_conf) != conference:
            continue
        m = meta.get((season, name.strip().lower()), {})
        results.append({
            'name': name, 'url': url,
            'host': m.get('host', ''), 'type': m.get('type', ''),
            'scoring': m.get('scoring', ''), 'start_date': m.get('start_date', ''),
            'status': m.get('status', ''),
        })
    results.sort(key=lambda r: r['start_date'])
    return results


def build_conference_page(conference, seasons):
    conf_slug = conference.lower()
    school_conf = _school_conf_map()

    with db() as con:
        schools = con.execute(
            'SELECT school, slug, city, state FROM schools WHERE conference=? ORDER BY school',
            (conference,)
        ).fetchall()

    latest = seasons[0]
    latest_label = season_label(latest)
    regattas = _conf_season_regattas(conference, latest, school_conf)

    n_schools = len(schools)
    n_regattas = len(regattas)

    # Regatta table
    reg_rows = ''
    for i, r in enumerate(regattas):
        slug = r['url'].strip('/').split('/')[-1]
        href = f'../../{latest}/{slug}/index.html'
        name_cell = f'<a href="{esc(href)}">{esc(r["name"])}</a>' if slug else esc(r['name'])
        status_cell = '<strong>Official</strong>' if r['status'].strip().lower() == 'official' else esc(r['status'])
        reg_rows += (
            f'      <tr class="row{i % 2}">\n'
            f'        <td class="schoolname">{name_cell}</td>\n'
            f'        <td>{esc(r["host"])}</td>\n'
            f'        <td>{esc(r["type"])}</td>\n'
            f'        <td>{esc(r["scoring"])}</td>\n'
            f'        <td>{esc(r["start_date"])}</td>\n'
            f'        <td>{status_cell}</td>\n'
            f'      </tr>\n'
        )

    reg_port = f'''  <div class="port">
    <h3>{esc(latest_label)} Regattas</h3>
    <table class="season-summary">
      <thead><tr>
          <th>Name</th><th>Host</th><th>Type</th>
          <th>Scoring</th><th>Date</th><th>Status</th>
      </tr></thead>
      <tbody>
{reg_rows}      </tbody>
    </table>
  </div>''' if reg_rows else ''

    # School cards
    cards = ''
    for school, slug, city, state in schools:
        logo = f'../../icsa_logos/{slug}.png'
        location = f'{city}, {state}' if city and state else (city or state or '')
        cards += (
            f'  <a class="school-card" href="../../schools/{esc(slug)}/">\n'
            f'    <img src="{esc(logo)}" alt="{esc(school)}" onerror="this.style.visibility=\'hidden\'">\n'
            f'    <span class="school-card-name">{esc(school)}</span>\n'
            f'    <span class="school-card-loc">{esc(location)}</span>\n'
            f'  </a>\n'
        )

    schools_port = f'''  <div class="port">
    <h3>Member Schools</h3>
    <div class="school-cards">
{cards}    </div>
  </div>'''

    # Season history sidebar
    season_links = ''
    for s in seasons:
        season_links += f'      <li><a href="../{s}/">{esc(season_label(s))}</a></li>\n'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(conference)} Conference | ICSA Real-Time Regatta Results</title>
  <link rel="stylesheet" href="../../styles.css" media="screen,print">
</head>
<body>
{nav('../../index.html', '../../schools/index.html', '../../sailors/index.html',
     '../../img/icsa-logo-real.png', f'../../{latest}/index.html', latest_label, logo_href='../../index.html', fantasy='../../fantasy/index.html')}
<div id="page-content">

  <div id="content-header">
    <h1>
      <span id="left-fill"></span>
      <span>{esc(conference)} Conference</span>
      <span id="right-fill"></span>
    </h1>
    <ul id="page-info">
      <li><span class="page-info-key">Number of Schools</span><span class="page-info-value">{n_schools}</span></li>
      <li><span class="page-info-key">Number of Regattas</span><span class="page-info-value">{n_regattas}</span></li>
    </ul>
  </div>

{reg_port}
{schools_port}

  <div id="submenu-wrapper">
    <h3 class="nav">Season History</h3>
    <ul id="other-seasons">
{season_links}    </ul>
  </div>

</div>
</body>
</html>'''


def build_conference_season_page(conference, season, seasons):
    """Conference page filtered to a specific season."""
    conf_slug = conference.lower()
    school_conf = _school_conf_map()
    label = season_label(season)
    regattas = _conf_season_regattas(conference, season, school_conf)

    reg_rows = ''
    for i, r in enumerate(regattas):
        slug = r['url'].strip('/').split('/')[-1]
        href = f'../../../{season}/{slug}/index.html'
        name_cell = f'<a href="{esc(href)}">{esc(r["name"])}</a>' if slug else esc(r['name'])
        status_cell = '<strong>Official</strong>' if r['status'].strip().lower() == 'official' else esc(r['status'])
        reg_rows += (
            f'      <tr class="row{i % 2}">\n'
            f'        <td class="schoolname">{name_cell}</td>\n'
            f'        <td>{esc(r["host"])}</td>\n'
            f'        <td>{esc(r["type"])}</td>\n'
            f'        <td>{esc(r["scoring"])}</td>\n'
            f'        <td>{esc(r["start_date"])}</td>\n'
            f'        <td>{status_cell}</td>\n'
            f'      </tr>\n'
        )

    # Season history sidebar
    season_links = ''
    for s in seasons:
        href = 'index.html' if s == season else f'../{s}/'
        season_links += f'      <li><a href="{href}">{esc(season_label(s))}</a></li>\n'

    latest = seasons[0]
    latest_label = season_label(latest)
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(conference)} | {esc(label)} | ICSA Real-Time Regatta Results</title>
  <link rel="stylesheet" href="../../../styles.css" media="screen,print">
</head>
<body>
{nav('../../../index.html', '../../../schools/index.html', '../../../sailors/index.html',
     '../../../img/icsa-logo-real.png', f'../../../{latest}/index.html', latest_label, logo_href='../../../index.html', fantasy='../../../fantasy/index.html')}
<div id="page-content">

  <div id="content-header">
    <h1>
      <span id="left-fill"></span>
      <span>{esc(conference)}</span>
      <span id="right-fill"></span>
    </h1>
    <ul id="page-info">
      <li><span class="page-info-key">Season</span><span class="page-info-value">{esc(label)}</span></li>
      <li><span class="page-info-key">Regattas</span><span class="page-info-value">{len(regattas)}</span></li>
    </ul>
  </div>

  <div class="port">
    <h3>Regattas</h3>
    <table class="season-summary">
      <thead><tr>
          <th>Name</th><th>Host</th><th>Type</th>
          <th>Scoring</th><th>Date</th><th>Status</th>
      </tr></thead>
      <tbody>
{reg_rows}      </tbody>
    </table>
  </div>

  <div id="submenu-wrapper">
    <h3 class="nav">Other seasons</h3>
    <ul id="other-seasons">
{season_links}    </ul>
  </div>

</div>
</body>
</html>'''


# ── regatta page builders ──────────────────────────────────────────────────────

_regatta_meta_cache = None

def _get_regatta_meta():
    global _regatta_meta_cache
    if _regatta_meta_cache is not None:
        return _regatta_meta_cache
    meta = {}
    csv_path = os.path.join(CSV_DIR, 'icsa_regattas.csv')
    if os.path.exists(csv_path):
        with open(csv_path, newline='', encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                key = (row['season'], row['name'].strip().lower())
                meta[key] = row
    _regatta_meta_cache = meta
    return meta


def _page_info_items(host, start_date, reg_type, boat, scoring, is_womens=False):
    html = ''
    if is_womens:
        html += '      <li><span class="badge badge-womens">Women\'s</span></li>\n'
    for key, val in [('Host', host), ('Date', start_date), ('Type', reg_type),
                     ('Boat', boat), ('Scoring', scoring)]:
        if val:
            html += (f'      <li><span class="page-info-key">{key}</span>'
                     f'<span class="page-info-value">{esc(val)}</span></li>\n')
    return html


# ── Coded results ─────────────────────────────────────────────────────────────
# (table, regatta_url) -> {(slug, team, division, race_num): code} for DNF, DSQ, BKD ...
_RESULT_CODES = {}
# (table, regatta_url) -> {(slug, team, division): (code, points)} for team penalties (LOP, PFD ...)
_TEAM_PENALTIES = {}

_CODE_NAMES = {
    'DNF': 'Did not finish', 'DNS': 'Did not start', 'DNC': 'Did not compete',
    'DSQ': 'Disqualified', 'OCS': 'Over the line at the start', 'RAF': 'Retired after finishing',
    'UFD': 'U flag disqualification', 'BFD': 'Black flag disqualification',
    'DGM': 'Disqualified, gross misconduct', 'RDG': 'Redress given',
    'BKD': 'Breakdown', 'BYE': 'Bye',
}


def _scored(table, regatta_url, rows, fleet_size):
    """Rows of (school, slug, team, div, race, place, points, code) -> the six-column rows the
    pages use, with the points the race was scored at in place of `place`.  Codes are kept
    to one side in _RESULT_CODES.  fleet_size(div, race) is the fallback for coded rows
    scraped before their points were recorded."""
    codes, pens, out = {}, {}, []
    for school, slug, team, div, rn, place, points, code, pen, pen_pts in rows:
        if pen:
            pens[(slug, team, div)] = (pen, pen_pts or 0)
        if points is None:
            points = min(place, fleet_size(div, rn) + 1)
        if place >= 900:
            codes[(slug, team, div, rn)] = code or ''
        out.append((school, slug, team, div, rn, points))
    _RESULT_CODES[(table, regatta_url)] = codes
    _TEAM_PENALTIES[(table, regatta_url)] = pens
    return out


def _add_penalties(table, regatta_url, teams, division=None):
    """Team penalties (LOP, PFD ...) go into the totals.  Works on either shape of `teams`:
    one entry per boat holding div_totals and grand_total, or one entry per boat and
    division holding total (`division` then names the division shown, None for all)."""
    for (slug, team, div), (code, pts) in _TEAM_PENALTIES.get((table, regatta_url), {}).items():
        t = teams.get((slug, team, div)) or teams.get((slug, team))
        if t is None:
            continue
        if 'grand_total' in t:
            if div in t['div_totals']:
                t['div_totals'][div] += pts
            t['grand_total'] += pts
        elif division is None or division == div:
            if t.get('div', div) != div:
                continue
            t['total'] += pts
        else:
            continue
        t.setdefault('penalties', []).append((div, code, pts))


def _total_cell(t, value, div=None):
    """A total, marked when a team penalty is part of it."""
    pens = [p for p in t.get('penalties', []) if div is None or p[0] == div]
    if not pens or value == '':
        return value
    note = '; '.join(f'{code}, {pts} points' + (f' in {d}' if div is None and d else '')
                     for d, code, pts in pens)
    label = ' '.join(sorted({code for _, code, _ in pens}))
    return (f'{value} <span title="Includes team penalty: {note}" '
            f'style="font-size:0.75em;font-weight:normal;color:#a33">{label}</span>')


def _team_codes(table, regatta_url, slug, team, div):
    """{race_num: code} for one boat."""
    return {k[3]: c for k, c in _RESULT_CODES.get((table, regatta_url), {}).items()
            if k[:3] == (slug, team, div)}


def _score_cell(t, n):
    """One race cell: the points, or the code with the points in its tooltip."""
    pts = t['scores'].get(n, '')
    code = (t.get('codes') or {}).get(n)
    if code is None or pts == '':
        return pts
    if not code:
        return f'<span title="Did not finish the race: {pts} points">{pts}</span>'
    name = _CODE_NAMES.get(code, code)
    return f'<span title="{name}: {pts} points" style="font-size:0.85em">{code}</span>'


def _load_fleet_results(regatta_url):
    """Load all fleet results for a regatta with DNF/DNS corrected to fleet_size+1."""
    with db() as con:
        rows = con.execute('''
            SELECT school, school_slug, team, division, race_num, place, points, code,
                   div_penalty, div_penalty_points
            FROM fleet_results WHERE regatta_url=?
            ORDER BY division, race_num, place
        ''', (regatta_url,)).fetchall()
    if not rows:
        return []
    # Compute fleet size per (division, race_num)
    fleet_sizes = {}
    for _, _, _, div, rn, *_ in rows:
        fleet_sizes[(div, rn)] = fleet_sizes.get((div, rn), 0) + 1
    # Cap DNF/DNS (place > fleet_size) at fleet_size + 1
    return _scored('fleet', regatta_url, rows, lambda div, rn: fleet_sizes[(div, rn)])


def _fleet_summary(regatta_url, _cache={}):
    """Returns (divisions, ranked_teams).
    ranked_teams: [{school, slug, team, div_totals:{div:int}, grand_total}] sorted by total.
    """
    if regatta_url not in _cache:
        _cache[regatta_url] = _load_fleet_results(regatta_url)
    rows = _cache[regatta_url]
    if not rows:
        return [], []
    teams, divisions = {}, set()
    for school, slug, team, div, rn, place in rows:
        k = (slug, team)
        if k not in teams:
            teams[k] = {'school': school, 'slug': slug, 'team': team,
                        'div_totals': {}, 'grand_total': 0}
        teams[k]['div_totals'][div] = teams[k]['div_totals'].get(div, 0) + place
        teams[k]['grand_total'] += place
        divisions.add(div)
    _add_penalties('fleet', regatta_url, teams)
    return sorted(divisions), sorted(teams.values(), key=lambda t: t['grand_total'])  # fleet


def _fleet_division_races(regatta_url, division, _cache={}):
    """Returns (race_nums, team_rows) sorted by total, for one division or, with division=None,
    for the whole fleet at once (combined scoring, where every division sails the same race)."""
    if regatta_url not in _cache:
        _cache[regatta_url] = _load_fleet_results(regatta_url)
    rows = _cache[regatta_url] if division is None else [r for r in _cache[regatta_url] if r[3] == division]
    if not rows:
        return [], []
    teams, race_nums = {}, set()
    for school, slug, team, div, rn, place in rows:
        k = (slug, team, div)
        if k not in teams:
            teams[k] = {'school': school, 'slug': slug, 'team': team, 'key': k, 'div': div,
                        'label': f'{team} {div}'.strip() if division is None else team,
                        'scores': {}, 'total': 0,
                        'codes': _team_codes('fleet', regatta_url, slug, team, div)}
        teams[k]['scores'][rn] = place
        teams[k]['total'] += place
        race_nums.add(rn)
    _add_penalties('fleet', regatta_url, teams, division)
    return sorted(race_nums), sorted(teams.values(), key=lambda t: t['total'])


def _compact_races(race_nums):
    if not race_nums:
        return ''
    nums = sorted(set(int(n) for n in race_nums))
    if len(nums) == 1:
        return str(nums[0])
    ranges, start, end = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == end + 1:
            end = n
        else:
            ranges.append(str(start) if start == end else f'{start}-{end}')
            start = end = n
    ranges.append(str(start) if start == end else f'{start}-{end}')
    return ', '.join(ranges)


def _fmt_person_cell(person_races, show_ranges, school_slug=None):
    if not person_races:
        return ''
    entries = sorted(person_races.items(), key=lambda kv: min(int(r) for r in kv[1]))
    parts = []
    for (name, grade), races in entries:
        if school_slug:
            slug = sailor_slug(name, school_slug)
            name_html = f'<a href="../../sailors/{esc(slug)}/">{esc(name)}</a>'
        else:
            name_html = esc(name)
        s = name_html + (f' <em>{esc(grade)}</em>' if grade else '')
        if show_ranges:
            s += f' <span class="race-range">[{_compact_races(races)}]</span>'
        parts.append(s)
    return '<br>'.join(parts)


_BOAT_QUALITY = None
SPEED_SPAN = 0.30      # places per race at either end of the colour scale (about the best/worst boat in an uneven fleet)
BOAT_POST = '../../../blog/are-the-boats-even/'


def _season_idx(code):
    """'f25' -> 51, 's26' -> 52 (half-season index, as analysis/blog/boat_pl.py)."""
    try:
        return 2 * int(code[1:]) + (0 if code[0] == 's' else 1)
    except (ValueError, IndexError):
        return None


def _boat_quality(host, boat, sail, season=None):
    """Expected finish change next regatta for one sail number at one venue, in places per race (positive = fast), with its
    standard error and the number of regattas behind it: (pred, se, regattas, era_from).  None when the boat is not in the fit.

    The values are the joint Plackett-Luce boat term (analysis/boat_quality/fit_boat_quality.py) scaled by 0.4, the share that
    holds on held-out regattas.  A fleet is split into eras (confirmed new fleets, multi-year gaps in hosting); the era that
    contains `season` is used, so an old regatta is shaded by the fleet it was sailed in, not by today's boats."""
    global _BOAT_QUALITY
    if _BOAT_QUALITY is None:
        from collections import defaultdict
        _BOAT_QUALITY = defaultdict(list)
        try:
            with open(os.path.join(CSV_DIR, 'boat_quality.csv'), newline='', encoding='utf-8') as f:
                for r in _csv.DictReader(f):
                    _BOAT_QUALITY[(r['host'], r['cls'], r['sail'])].append(
                        (_season_idx(r['era_from']) or 0, float(r['pred_places']), float(r['se_places']), int(r['regattas']), r['era_from']))
        except OSError:
            pass
        for v in _BOAT_QUALITY.values():
            v.sort()
    sail = str(sail).strip()
    si = _season_idx(season) if season else None
    for b in dict.fromkeys([boat.lower()] + boat.lower().split('/')[::-1]):          # '420/FJ' is filed as 'FJ' at some venues
        eras = _BOAT_QUALITY.get((host, b.strip(), sail))
        if not eras:
            continue
        if si is None:
            e = eras[-1]
        else:
            fit = [e for e in eras if e[0] <= si]
            if not fit:
                continue
            e = fit[-1]
        return e[1], e[2], e[3], e[4]
    return None


def _boat_speed(host, boat, sail, season=None):
    """Backwards-compatible scalar: the expected places per race for a sail, or None."""
    q = _boat_quality(host, boat, sail, season)
    return q[0] if q else None


def _speed_color(v, span=SPEED_SPAN):
    """Red (slow) through yellow to green (fast); span is the speed at either end of the scale."""
    t = max(-1.0, min(1.0, v / span))
    return f'hsl({60 + 60 * t:.0f},70%,{80 - 8 * abs(t):.0f}%)'


def _speed_legend(span=SPEED_SPAN, fleet=0, era_from='', nreg=0):
    """Scale bar for the rotation shading, in places per race."""
    stops = ', '.join(_speed_color(span * (i / 5 * 2 - 1), span) for i in range(6))
    since = f' since {_season_label(era_from)}' if era_from else ''
    return (f'<div class="speed-scale" style="max-width:34em;margin:0 auto 12px;font-size:.78rem;color:#555">'
            f'<div style="display:flex;justify-content:space-between"><span>Slow</span><span>Expected boat effect</span><span>Fast</span></div>'
            f'<div style="height:10px;border-radius:5px;background:linear-gradient(90deg,{stops})"></div>'
            f'<div style="display:flex;justify-content:space-between"><span>&minus;{span:.2f}</span><span>0</span>'
            f'<span>+{span:.2f}</span></div>'
            f'<div style="text-align:center;color:#888">Places per race a team should expect to gain or lose in this boat against '
            f'what its sailors\' ratings predict, from a model of this fleet fitted on every regatta sailed here{since}'
            f'{f" ({nreg} regattas)" if nreg else ""}. Shown at the 40% that holds up on regattas the model never saw; '
            f'hover a sail for its value and uncertainty. Grey = boat not in the fit. '
            f'<a href="{BOAT_POST}">How this is estimated</a>.</div></div>')


def _season_label(code):
    try:
        return ('Spring' if code[0] == 's' else 'Fall') + ' 20' + code[1:]
    except (IndexError, TypeError):
        return code


def _rotations_tab_html(regatta_url, logo_prefix='../../icsa_logos/', host='', boat=''):
    """Returns HTML for the Rotations tab, or empty string if no data.  With a host and boat class each sail
    number is shaded by the boat's expected effect, from the fleet-era boat model (see _boat_quality)."""
    season = regatta_url.strip('/').split('/')[0] if regatta_url else None
    with db() as con:
        rows = con.execute('''
            SELECT fr.school, r.school_slug, r.team, r.division, r.race_num, r.sail_num
            FROM rotations r
            JOIN fleet_results fr
              ON fr.regatta_url = r.regatta_url
             AND fr.school_slug = r.school_slug
             AND fr.team        = r.team
             AND fr.division    = r.division
             AND fr.race_num    = r.race_num
            WHERE r.regatta_url=? AND r.sail_num IS NOT NULL
            GROUP BY r.school_slug, r.team, r.division, r.race_num, r.sail_num
            ORDER BY r.division, r.school_slug, r.team, r.race_num
        ''', (regatta_url,)).fetchall()
    if not rows:
        return ''

    # Build data structure: {div: {(slug,school,team): {race_num: sail_num}}}
    from collections import defaultdict
    div_data  = defaultdict(dict)   # div → ordered list of (slug, school, team)
    div_races = defaultdict(set)    # div → set of race_nums
    cell_data = {}                  # (div, slug, team, race_num) → sail_num

    seen_teams = defaultdict(list)
    for school, slug, team, div, race_num, sail_num in rows:
        key = (div, slug, team)
        if key not in div_data[div]:
            div_data[div][key] = (school, slug, team)
            seen_teams[div].append(key)
        div_races[div].add(race_num)
        cell_data[(div, slug, team, race_num)] = sail_num

    # Scale ends at the fastest/slowest boat in this event (at least 0.03), so a fleet of similar boats still shows its spread.
    _qs = [_boat_quality(host, boat, sn, season) for sn in set(cell_data.values())] if host and boat else []
    _qs = [q for q in _qs if q is not None]
    span = min(SPEED_SPAN * 2, max(0.05, max((abs(q[0]) for q in _qs), default=0.0)))
    era_from = min((q[3] for q in _qs), key=_season_idx, default='')
    era_nreg = max((q[2] for q in _qs), default=0)
    fleet = max((len(v) for v in seen_teams.values()), default=0)
    sections = []
    shaded = []
    for div in sorted(div_data.keys()):
        race_nums = sorted(div_races[div])
        race_ths  = ''.join(
            f'<th class="sail">{n}{div}</th>' for n in race_nums
        )
        tbody_rows = ''
        for i, key in enumerate(seen_teams[div]):
            school, slug, team = div_data[div][key]
            logo = f'{logo_prefix}{esc(slug)}.png'
            def _cell(n):
                sail = cell_data.get((div, slug, team, n), "")
                q = _boat_quality(host, boat, sail, season) if (host and boat and sail != "") else None
                if q is None:
                    return f'<td class="sail">{sail}</td>'
                v, se, nreg, _ef = q
                shaded.append(1)
                return (f'<td class="sail" style="background:{_speed_color(v, span)}" '
                        f'title="sail {sail}: {v:+.2f} &plusmn; {se:.2f} places per race expected, from {nreg} regatta{"s" if nreg != 1 else ""} in this fleet">{sail}</td>')
            sails = ''.join(_cell(n) for n in race_nums)
            tbody_rows += (
                f'<tr class="row{i % 2}">'
                f'<td class="burgee-cell"><img src="{logo}" alt="{esc(school)}"'
                f' height="40" width="60" onerror="this.style.display:\'none\'"></td>'
                f'<td class="teamname">{school_link(school, slug)}'
                f'{" " + esc(team) if team else ""}</td>'
                f'{sails}'
                f'</tr>\n'
            )
        sections.append(
            f'<h3>Division {esc(div)}</h3>'
            f'<table class="rotation results">'
            f'<thead><tr><th></th><th>Team</th>{race_ths}</tr></thead>'
            f'<tbody>{tbody_rows}</tbody>'
            f'</table>'
        )

    legend = _speed_legend(span, max((len(v) for v in seen_teams.values()), default=0), era_from, era_nreg) if shaded else ''
    return legend + '\n'.join(sections)


def _sailors_table(regatta_url):
    """Returns HTML for the sailors tab, or empty string if no data."""
    with db() as con:
        rows = con.execute('''
            SELECT school, school_slug, team, division, race_num,
                   skipper, skipper_grade, crew, crew_grade
            FROM fleet_results
            WHERE regatta_url=?
              AND skipper != '' AND skipper NOT LIKE '%Unreported%'
            ORDER BY school, team, division, race_num
        ''', (regatta_url,)).fetchall()
    if not rows:
        return ''

    divisions = sorted(set(r[3] for r in rows))
    multi_div = len(divisions) > 1

    groups = {}
    order = []
    for school, slug, team, div, race_num, skipper, skip_gr, crew, crew_gr in rows:
        key = (school, slug, team)
        if key not in groups:
            groups[key] = {}
            order.append(key)
        if div not in groups[key]:
            groups[key][div] = {'skip': {}, 'crew': {}}
        skipper, skip_gr2 = _parse_name(skipper); skip_gr = skip_gr or skip_gr2
        crew, crew_gr2 = _parse_name(crew); crew_gr = crew_gr or crew_gr2
        groups[key][div]['skip'].setdefault((skipper, skip_gr), []).append(race_num)
        if crew and not crew.startswith('Unreported'):
            groups[key][div]['crew'].setdefault((crew, crew_gr), []).append(race_num)

    td = 'style="text-align:left"'
    td_nb = 'style="text-align:left;border-bottom:none"'

    table_rows = ''
    for i, (school, slug, team) in enumerate(order):
        logo = f'../../icsa_logos/{esc(slug)}.png'
        shade = f'row{i % 2}'
        school_divs = groups[(school, slug, team)]
        n_divs = len(divisions)

        for di, div in enumerate(divisions):
            div_data = school_divs.get(div, {'skip': {}, 'crew': {}})
            show_ranges = len(div_data['skip']) > 1 or len(div_data['crew']) > 1
            skip_cell = _fmt_person_cell(div_data['skip'], show_ranges, school_slug=slug)
            crew_cell = _fmt_person_cell(div_data['crew'], show_ranges, school_slug=slug)
            is_last = (di == n_divs - 1)
            border = td if is_last else td_nb

            if multi_div:
                if di == 0:
                    label_style = f'text-align:left;{"" if is_last else "border-bottom:none;"}'
                    table_rows += (
                        f'      <tr class="{shade}">\n'
                        f'        <td class="burgee-cell" rowspan="{n_divs}" style="vertical-align:middle">'
                        f'<img src="{logo}" alt="{esc(school)}" height="40" width="60" onerror="this.style.display=\'none\'"></td>\n'
                        f'        <td class="schoolname" style="{label_style}">{esc(school)}</td>\n'
                        f'        <td {border}>{skip_cell}</td>\n'
                        f'        <td {border}>{crew_cell}</td>\n'
                        f'      </tr>\n'
                    )
                elif di == 1:
                    label_style = f'text-align:left;{"" if is_last else "border-bottom:none;"}'
                    table_rows += (
                        f'      <tr class="{shade}">\n'
                        f'        <td style="{label_style}"><span style="color:#777;font-size:0.9em">{esc(team)}</span></td>\n'
                        f'        <td {border}>{skip_cell}</td>\n'
                        f'        <td {border}>{crew_cell}</td>\n'
                        f'      </tr>\n'
                    )
                else:
                    label_style = f'text-align:left;{"" if is_last else "border-bottom:none;"}'
                    table_rows += (
                        f'      <tr class="{shade}">\n'
                        f'        <td style="{label_style}"></td>\n'
                        f'        <td {border}>{skip_cell}</td>\n'
                        f'        <td {border}>{crew_cell}</td>\n'
                        f'      </tr>\n'
                    )
            else:
                table_rows += (
                    f'      <tr class="{shade}">\n'
                    f'        <td class="burgee-cell"><img src="{logo}" alt="{esc(school)}"'
                    f' height="40" width="60" onerror="this.style.display=\'none\'"></td>\n'
                    f'        <td class="schoolname">{esc(school)}</td>\n'
                    f'        <td class="teamname">{esc(team)}</td>\n'
                    f'        <td {td}>{skip_cell}</td>\n'
                    f'        <td {td}>{crew_cell}</td>\n'
                    f'      </tr>\n'
                )

    if multi_div:
        return f'''    <table class="results">
      <thead><tr>
          <th></th><th>School / Team</th>
          <th style="text-align:left">Skipper</th><th style="text-align:left">Crew</th>
      </tr></thead>
      <tbody>
{table_rows}      </tbody>
    </table>'''
    return f'''    <table class="results">
      <thead><tr>
          <th></th><th>School</th><th>Team</th>
          <th style="text-align:left">Skipper</th><th style="text-align:left">Crew</th>
      </tr></thead>
      <tbody>
{table_rows}      </tbody>
    </table>'''


def _div_sailors(regatta_url, division):
    """Returns dict mapping (school_slug, team) -> {'skip': ..., 'crew': ...} for one division."""
    with db() as con:
        rows = con.execute('''
            SELECT school_slug, team, race_num, skipper, skipper_grade, crew, crew_grade
            FROM fleet_results
            WHERE regatta_url=? AND division=?
              AND skipper != '' AND skipper NOT LIKE '%Unreported%'
            ORDER BY race_num
        ''', (regatta_url, division)).fetchall()
    result = {}
    for slug, team, rn, skipper, skip_gr, crew, crew_gr in rows:
        k = (slug, team)
        if k not in result:
            result[k] = {'skip': {}, 'crew': {}}
        skipper, skip_gr2 = _parse_name(skipper); skip_gr = skip_gr or skip_gr2
        result[k]['skip'].setdefault((skipper, skip_gr), []).append(rn)
        if crew and not crew.startswith('Unreported'):
            crew, crew_gr2 = _parse_name(crew); crew_gr = crew_gr or crew_gr2
            result[k]['crew'].setdefault((crew, crew_gr), []).append(rn)
    return result


def _race_chart_json(regatta_url):
    """Return compact JSON data for client-side chart rendering, or None."""
    import json as _json
    rows = _load_fleet_results(regatta_url)
    if not rows:
        return None
    all_race_keys = sorted(set((rn, div) for _, _, _, div, rn, _ in rows),
                           key=lambda x: (x[0], x[1]))
    if len(all_race_keys) < 2:
        return None
    multi_div = len(set(d for _, d in all_race_keys)) > 1
    race_labels = [f'R{rn}{div}' if multi_div else f'R{rn}' for rn, div in all_race_keys]
    teams, team_order = {}, []
    for school, slug, team, div, rn, place in rows:
        k = (slug, team)
        if k not in teams:
            teams[k] = {'school': school, 'team': team, 'scores': {}}
            team_order.append(k)
        teams[k]['scores'][(rn, div)] = place
    team_list = []
    for k in team_order:
        t = teams[k]
        team_list.append({
            'school': t['school'],
            'team':   t['team'],
            'scores': [t['scores'].get(rk, 0) for rk in all_race_keys],
        })
    return _json.dumps({'races': race_labels, 'teams': team_list}, separators=(',', ':'))


def _regatta_nav(season_code, season_label_str, prefix='../../'):
    return nav(f'{prefix}index.html', f'{prefix}schools/index.html',
               f'{prefix}sailors/index.html', f'{prefix}img/icsa-logo-real.png',
               f'{prefix}{season_code}/index.html', season_label_str,
               logo_href=f'{prefix}index.html', fantasy=f'{prefix}fantasy/index.html')


def _regatta_header(name, info_html):
    return f'''  <div id="content-header">
    <h1>
      <span id="left-fill"></span>
      <span>{esc(name)}</span>
      <span id="right-fill"></span>
    </h1>
    <ul id="page-info">
{info_html}    </ul>
  </div>'''


def build_scheduled_regatta_page(season_code, regatta_url, regatta_name, season_label_str):
    """A page for a regatta that is on the schedule but has not sailed.

    The results site has nothing for these yet, so the results-driven loop never builds
    them and the home page had to show them as plain text.  Everything we do know comes
    from the conference schedule and the entry list: who is entered, which boats, the
    published rank, and — where the selector has run — who each school is expected to
    bring.  When the regatta is published the normal builder overwrites this page with
    real results, so the URL is correct from the start.
    """
    meta = _temp_regatta_meta().get(regatta_url)
    if not meta:
        return None

    entries = _upcoming_entries_by_url().get(regatta_url, [])
    lineups = _upcoming_lineups_by_name().get(regatta_name, {})

    tier      = (meta.get('pr_tier') or '').strip()
    is_womens = bool(meta.get('is_womens')) or 'W' in tier.upper()
    scoring   = '3 Divisions' if meta.get('combined_divisions') else ''
    info_html = _page_info_items(meta.get('host', ''), meta.get('start_date', ''),
                                 '', meta.get('boats', ''), scoring, is_womens)
    if tier:
        info_html += ('      <li><span class="page-info-key">Rank</span>'
                      f'<span class="page-info-value">{esc(tier)}</span></li>\n')
    if meta.get('berths'):
        info_html += ('      <li><span class="page-info-key">Berths</span>'
                      f'<span class="page-info-value">{esc(meta["berths"])}</span></li>\n')

    # Entry list, with the predicted skipper alongside where we have one.
    rows = ''
    by_school = {}
    for e in entries:
        by_school.setdefault((e['school_name'], e['school_slug']), []).append(e)
    for i, ((school, slug), boats) in enumerate(sorted(by_school.items())):
        picks = [p for p in (lineups.get(slug) or []) if p.get('role') in (None, 'driver')]      # one entry per boat: the skipper
        for b_i, _b in enumerate(sorted(boats, key=lambda x: x.get('entry_num', '1'))):
            berth = '' if len(boats) == 1 else f' {b_i + 1}'
            pick = picks[b_i] if b_i < len(picks) else None
            pred = (f'{esc(pick["name"])} <span class="rt">({pick["rating"]:.2f})</span>'
                    if pick else '<span class="note">&mdash;</span>')
            rows += (f'      <tr class="row{i % 2}">\n'
                     f'        <td class="schoolname">{school_link(school, slug)}{esc(berth)}</td>\n'
                     f'        <td>{pred}</td>\n'
                     f'      </tr>\n')

    import forecast_block
    import team_race_pages
    _fc = (team_race_pages.forecast_html('icsa', regatta_name, lambda sch: _school_href_by_name(sch))
           or forecast_block.forecast_html('icsa', regatta_name, lambda sch: _school_href_by_name(sch)))   # team race model first
    forecast_section = (f'  <div class="port">\n    <h3>Forecast</h3>\n{_fc}\n  </div>') if _fc else ''

    if rows:
        body = f'''    <p class="note">This regatta has not sailed yet. Entries come from the
    conference schedule; predicted skippers are the sailor-selection model's best guess and
    are not official.</p>
    <table class="results">
      <thead><tr><th>School</th><th>Predicted skipper</th></tr></thead>
      <tbody>
{rows}      </tbody>
    </table>'''
    else:
        body = ('    <p class="note">This regatta is on the schedule but its entry list has '
                'not been published yet.</p>')

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(regatta_name)} | ICSA Real-Time Regatta Results</title>
  <link rel="stylesheet" href="../../styles.css" media="screen,print">
</head>
<body>
{_regatta_nav(season_code, season_label_str)}
<div id="page-content">
{_regatta_header(regatta_name, info_html)}
  <div class="port">
{body}
  </div>
{forecast_section}
</div>
</body>
</html>'''


def _school_href_by_name(name):
    """../../schools/<slug>/ for an ICSA school name, when that page exists."""
    with db() as con:
        r = con.execute('SELECT slug FROM schools WHERE school=?', ((name or '').strip(),)).fetchone()
    return f'../../schools/{r[0]}/' if r and os.path.exists(os.path.join(SITE, 'schools', r[0], 'index.html')) else None


_TEMP_META = None


def _temp_regatta_meta():
    """temp_url -> the schedule metadata row for regattas that have not sailed."""
    global _TEMP_META
    if _TEMP_META is None:
        import json as _json
        out = {}
        try:
            with open(os.path.join(CSV_DIR, 'neisa_schedule_meta.json'), encoding='utf-8') as f:
                for meta in (_json.load(f) or {}).values():
                    u = (meta.get('temp_url') or '').strip()
                    if u:
                        out[u] = meta
        except (OSError, ValueError):
            pass
        _TEMP_META = out
    return _TEMP_META


_UPCOMING_ENTRIES_BY_URL = None


def _upcoming_entries_by_url():
    global _UPCOMING_ENTRIES_BY_URL
    if _UPCOMING_ENTRIES_BY_URL is None:
        out = {}
        try:
            with open(os.path.join(CSV_DIR, 'icsa_upcoming_entries.csv'), encoding='utf-8') as f:
                for row in _csv.DictReader(f):
                    u = (row.get('regatta_url') or '').strip()
                    if u:
                        out.setdefault(u, []).append(row)
        except OSError:
            pass
        # The results site's entries carry the full URL, the schedule's carry a path: find either by path.
        for u in list(out):
            if u.startswith('http'):
                out.setdefault('/' + u.split('://', 1)[-1].split('/', 1)[-1].strip('/'), out[u])
        _UPCOMING_ENTRIES_BY_URL = out
    return _UPCOMING_ENTRIES_BY_URL


_UPCOMING_LINEUPS = None


def _upcoming_lineups_by_name():
    """regatta name -> {slug: [{name, rating}, ...]} from the selector's predictions."""
    global _UPCOMING_LINEUPS
    if _UPCOMING_LINEUPS is None:
        import json as _json
        out = {}
        try:
            with open(os.path.join(BASE, 'analysis', 'upcoming_lineups_icsa.json'),
                      encoding='utf-8') as f:
                for rec in _json.load(f) or []:
                    by_slug = {}
                    for sch in rec.get('schools') or []:
                        if sch.get('slug') and sch.get('lineup'):
                            by_slug[sch['slug']] = sch['lineup']
                    out[rec.get('regatta')] = by_slug
        except (OSError, ValueError):
            pass
        _UPCOMING_LINEUPS = out
    return _UPCOMING_LINEUPS


def build_fleet_regatta_page(season_code, regatta_url, regatta_name, season_label_str):
    m = _get_regatta_meta().get((season_code, regatta_name.strip().lower()), {})
    host       = m.get('host', '')
    start_date = m.get('start_date', '')
    reg_type   = m.get('type', '')
    scoring    = m.get('scoring', '')

    with db() as con:
        boat_row = con.execute(
            'SELECT boat, is_womens FROM fleet_results WHERE regatta_url=? LIMIT 1',
            (regatta_url,)).fetchone()
    boat      = boat_row[0] if boat_row else ''
    is_womens = bool(boat_row and boat_row[1])

    divisions, ranked = _fleet_summary(regatta_url)
    if not ranked:
        return None

    # Combined scoring: every division sails the same race, so there are no separate division standings
    combined = scoring.strip().lower() == 'combined'
    # Singlehanded: each entry is a sailor, so the tables list sailors, in one table of
    # full scores, and the separate Sailors tab would only repeat them.
    single = _regatta_kind(regatta_url, scoring) == 'singlehanded'
    entry_sailor = _entry_sailors(regatta_url) if single else {}
    show_divs = [] if (combined or single) else divisions
    panel_divs = [None] if (combined or single) else divisions

    def who(t):
        """The cell naming an entry: its sailor when singlehanded, its team name otherwise."""
        if single:
            return entry_sailor.get((t['slug'], t['team'])) or esc(t['team'])
        return esc(t.get('label') or t['team'])
    who_th = 'Sailor' if single else 'Team'

    # Score summary table
    div_th = ''.join(f'          <th>{esc(d)}</th>\n' for d in show_divs)
    summary_rows = ''
    for i, t in enumerate(ranked):
        logo = f'../../icsa_logos/{esc(t["slug"])}.png'
        div_cells = ''.join(
            f'          <td class="total">{_total_cell(t, t["div_totals"].get(d, ""), d)}</td>\n'
            for d in show_divs)
        summary_rows += (
            f'      <tr class="row{i % 2}">\n'
            f'        <td>{i + 1}</td>\n'
            f'        <td class="burgee-cell"><img src="{logo}" alt="{esc(t["school"])}"'
            f' height="40" width="60" onerror="this.style.display=\'none\'"></td>\n'
            f'        <td class="schoolname">{school_link(t["school"], t["slug"])}</td>\n'
            f'        <td class="teamname">{who(t)}</td>\n'
            f'{div_cells}'
            f'        <td class="totalcell">{_total_cell(t, t["grand_total"])}</td>\n'
            f'      </tr>\n'
        )

    summary_panel = f'''    <table class="results coordinate divisional">
      <thead><tr>
          <th>#</th><th></th>
          <th>School</th><th class="teamname">{who_th}</th>
{div_th}          <th><abbr title="Total">TOT</abbr></th>
      </tr></thead>
      <tbody>
{summary_rows}      </tbody>
    </table>'''

    # Per-division race tables
    div_panels = {}
    for div in panel_divs:
        race_nums, team_rows = _fleet_division_races(regatta_url, div)
        if div is None:
            sailors_by_team = {}
            for d in divisions:
                for k_, v_ in _div_sailors(regatta_url, d).items():
                    sailors_by_team[(k_[0], k_[1], d)] = v_
        else:
            sailors_by_team = {(k_[0], k_[1], div): v_ for k_, v_ in _div_sailors(regatta_url, div).items()}
        has_sailors = bool(sailors_by_team) and not single
        r_th = ''.join(f'          <th>R{n}</th>\n' for n in race_nums)
        sailors_th = '          <th style="text-align:left">Sailors</th>\n' if has_sailors else ''
        div_rows = ''
        for i, t in enumerate(team_rows):
            logo = f'../../icsa_logos/{esc(t["slug"])}.png'
            r_cells = ''.join(
                f'          <td style="width:2em;text-align:center;padding:0.15em 0.25em">{_score_cell(t, n)}</td>\n' for n in race_nums)
            sailor_cell = ''
            if has_sailors:
                sd = sailors_by_team.get(t.get('key', (t['slug'], t['team'])), {'skip': {}, 'crew': {}})
                show_ranges = len(sd['skip']) > 1 or len(sd['crew']) > 1
                skip_html = _fmt_person_cell(sd['skip'], show_ranges, school_slug=t['slug'])
                crew_html = _fmt_person_cell(sd['crew'], show_ranges, school_slug=t['slug'])
                if crew_html:
                    cell_content = f'{skip_html}<br><span style="color:#777">{crew_html}</span>'
                else:
                    cell_content = skip_html
                sailor_cell = f'          <td style="text-align:left;white-space:nowrap;min-width:14em">{cell_content}</td>\n'
            div_rows += (
                f'        <tr class="row{i % 2}">\n'
                f'          <td class="burgee-cell"><img src="{logo}" alt="{esc(t["school"])}"'
                f' height="40" width="60" onerror="this.style.display=\'none\'"></td>\n'
                f'          <td class="schoolname">{school_link(t["school"], t["slug"])}</td>\n'
                f'          <td class="teamname">{who(t)}</td>\n'
                f'{r_cells}'
                f'          <td class="totalcell">{_total_cell(t, t["total"])}</td>\n'
                f'{sailor_cell}'
                f'        </tr>\n'
            )
        div_panels[div] = f'''    <table class="tr-rotation-table results">
      <thead><tr>
          <th></th><th>School</th><th class="teamname">{who_th}</th>
{r_th}          <th><abbr title="Total">TOT</abbr></th>
{sailors_th}      </tr></thead>
      <tbody>
{div_rows}      </tbody>
    </table>'''

    chart_json     = _race_chart_json(regatta_url)
    sailors_html   = _sailors_table(regatta_url)
    rotations_html = _rotations_tab_html(regatta_url, host=host, boat=boat)
    if rotations_html and host and boat:
        h_enc = host.replace('&','&amp;').replace('"','&quot;')
        b_enc = boat.replace('&','&amp;').replace('"','&quot;')
        bsa_url = f'../../boat-speed/index.html?host={h_enc}&boat={b_enc}'
        rotations_html = (
            f'<div style="text-align:center;margin-bottom:10px;">'
            f'<a href="{bsa_url}" style="display:inline-block;padding:6px 16px;background:#eef4ff;'
            f'border:1px solid #b8d0f7;border-radius:6px;font-size:.85rem;font-weight:600;'
            f'color:#0984e3;text-decoration:none;">Boat Speed Analyzer</a></div>\n'
            + rotations_html
        )

    # Assemble tabs
    tab_btns   = '    <button class="tab-btn active" onclick="showTab(this,\'tab-summary\')">Score Summary</button>\n'
    tab_panels = f'  <div id="tab-summary" class="tab-panel active">\n{summary_panel}\n  </div>\n'

    for div in panel_divs:
        tid = 'tab-full' if div is None else f'tab-div-{div}'
        label = 'Full Scores' if div is None else f'Division {esc(div)}'
        tab_btns   += f'    <button class="tab-btn" onclick="showTab(this,\'{tid}\')">{label}</button>\n'
        tab_panels += f'  <div id="{tid}" class="tab-panel">\n{div_panels[div]}\n  </div>\n'

    if rotations_html:
        tab_btns   += '    <button class="tab-btn" onclick="showTab(this,\'tab-rotations\')">Rotations</button>\n'
        tab_panels += f'  <div id="tab-rotations" class="tab-panel">\n{rotations_html}\n  </div>\n'

    if sailors_html and not single:
        tab_btns   += '    <button class="tab-btn" onclick="showTab(this,\'tab-sailors\')">Sailors</button>\n'
        tab_panels += f'  <div id="tab-sailors" class="tab-panel">\n{sailors_html}\n  </div>\n'

    # Singlehanded: performance ratings on the singlehanded model (the fleet-model analysis sub-page rates the wrong thing here)
    disc_html = ''
    if single:
        import discipline_pages
        disc_html = discipline_pages.analysis_tab('singlehanded', 'ICSA', regatta_url, school_link, _disc_sailor_href)
        if disc_html:
            tab_btns   += '    <button class="tab-btn" onclick="showTab(this,\'tab-analysis\')">Analysis</button>\n'
            tab_panels += f'  <div id="tab-analysis" class="tab-panel">\n{disc_html}\n  </div>\n'

    if chart_json:
        tab_btns   += '    <button class="tab-btn" onclick="showTab(this,\'tab-chart\')">Chart</button>\n'
        tab_panels += '  <div id="tab-chart" class="tab-panel"><div class="rpc-container"></div></div>\n'

    wx_btn, wx_panel = weather_tab_html(season_code, regatta_name, host,
                                        logo_prefix='../../icsa_logos/')
    if wx_btn:
        tab_btns   += wx_btn
        tab_panels += wx_panel

    # Analysis sub-page (performance ratings) — link only if it has been built
    reg_slug = regatta_url.strip('/').split('/')[-1]
    if not disc_html and os.path.exists(os.path.join(SITE, season_code, reg_slug, 'analysis', 'index.html')):
        tab_btns += ('    <a class="tab-btn" href="analysis/" '
                     'style="text-decoration:none;color:inherit;display:inline-block;">'
                     'Analysis</a>\n')

    tabbed_port = f'''  <div class="port">
    <div class="tabs">
{tab_btns}    </div>
{tab_panels}  </div>'''

    chart_script = ''
    if chart_json:
        chart_script = f'var _rpcData={chart_json};'

    weather_css = WEATHER_CSS if wx_btn else ''
    info_html = _page_info_items(host, start_date, reg_type, boat, scoring, is_womens)
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(regatta_name)} | ICSA Real-Time Regatta Results</title>
  <link rel="stylesheet" href="../../styles.css" media="screen,print">
{weather_css}
</head>
<body>
{_regatta_nav(season_code, season_label_str)}
<div id="page-content">
{_regatta_header(regatta_name, info_html)}
{tabbed_port}
</div>
<script src="../../chart.js"></script>
<script>
{chart_script}
function showTab(btn, id) {{
  var port = btn.closest('.port');
  port.querySelectorAll('.tab-btn').forEach(function(b){{b.classList.remove('active');}});
  port.querySelectorAll('.tab-panel').forEach(function(p){{p.classList.remove('active');}});
  btn.classList.add('active');
  var panel = document.getElementById(id);
  panel.classList.add('active');
  if (typeof _rpcData !== 'undefined') {{
    var ctr = panel.querySelector('.rpc-container');
    if (ctr) buildRaceChart(ctr, _rpcData);
  }}
}}
</script>
</body>
</html>'''


def build_regatta_analysis_page(season_code, regatta_url, regatta_name, season_label_str):
    """Performance-rating analysis sub-page for a fleet regatta.

    For each skipper: the model rating going into the event, and the rating at
    which they would achieve their actual division finish (or better) 50% of
    the time with every other skipper's rating held fixed.  Computation lives in
    analysis/regatta_analysis.py; this only renders.
    """
    from analysis.regatta_analysis import analyze_regatta
    with db() as con:
        res = analyze_regatta(con, regatta_url)
    if not res:
        return None

    P = '../../../'
    m = _get_regatta_meta().get((season_code, regatta_name.strip().lower()), {})
    with db() as con:
        boat_row = con.execute(
            'SELECT boat, is_womens FROM fleet_results WHERE regatta_url=? LIMIT 1',
            (regatta_url,)).fetchone()
    boat      = boat_row[0] if boat_row else ''
    is_womens = bool(boat_row and boat_row[1])
    info_html = _page_info_items(m.get('host', ''), m.get('start_date', ''), m.get('type', ''),
                                 boat, m.get('scoring', ''), is_womens).replace('../../', P)

    # A regatta that is not Official is still being sailed or scored: the weekly ratings were last
    # updated before it finished, so the rating columns carry an asterisk and a banner says why.
    provisional = m.get('status', '').strip().lower() != 'official'
    star = '*' if provisional else ''

    def r100(x):
        return '&mdash;' if x is None else f'{round(x * 100):d}'

    def sailor_cell(row):
        label = esc(row['name'])
        if row['grade']:
            label += f' <span style="color:#777">&#39;{esc(row["grade"][-2:])}</span>'
        slug = sailor_slug(row['name'], row['school_slug'])
        if os.path.exists(os.path.join(SITE, 'sailors', slug, 'index.html')):
            return f'<a href="{P}sailors/{esc(slug)}/">{label}</a>'
        return label

    used_flags = set()
    panels = ''
    for d in res['divisions']:
        teams_per_school = {}
        for row in d['rows']:
            teams_per_school.setdefault(row['school_slug'], set()).add(row['team'])
        trs = ''
        for i, row in enumerate(d['rows']):
            school_html = school_link(row['school'], row['school_slug'], prefix=P)
            if len(teams_per_school.get(row['school_slug'], ())) > 1 and row['team']:
                school_html += f' <span style="color:#777">{esc(row["team"])}</span>'
            before = r100(row['rating_before'])
            if row['before_flag'] == 'new':
                before += ('<sup title="No rating before this event; using the model&#39;s '
                           'estimate from this week">n</sup>')
                used_flags.add('new')
            elif row['before_flag'] == 'unrated':
                before += '<sup title="Not rated by the model; division median used">u</sup>'
                used_flags.add('unrated')
            if row['perf_flag'] == 'nofinish':
                perf = ('<span style="color:#999" title="Did not finish any race sailed; penalties are left out '
                        'of the rating">&mdash;</span>')
                delta = '<span style="color:#999">&mdash;</span>'
            elif row['perf_flag'] == '<=':
                perf  = ('<span style="color:#999" title="Finished last in every race sailed: '
                         'no finite least-surprisal rating">&mdash;</span>')
                delta = '<span style="color:#999">&mdash;</span>'
                used_flags.add('last')
            else:
                pv = row['perf'] * 100
                perf = f'{round(pv):d}' + ('+' if row['perf_flag'] == '>=' else '')
                dv = pv - row['rating_before'] * 100
                col = '#1a8a3a' if dv >= 10 else ('#c0392b' if dv <= -10 else '#555')
                delta = f'<span style="color:{col};font-weight:600">{dv:+.0f}</span>'
            pp_ = row['p_points'] * 100
            pcol_ = '#1a8a3a' if pp_ <= 20 else ('#c0392b' if pp_ >= 80 else '#333')
            pp_txt = '&lt;1%' if pp_ < 0.5 else ('&gt;99%' if pp_ > 99.5 else f'{pp_:.0f}%')
            pct_html = f'<span style="color:{pcol_}">{pp_txt}</span>'
            xs = max(0.0, row['surprisal_before_bits'] - row['surprisal_bits'])
            surprise = f'{xs:.1f}'
            def _ord(v):
                if v is None:
                    return '&mdash;'
                n = int(round(v))
                n = max(1, min(99, n))
                suf = 'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
                return f'{n}{suf}'
            bp, pp = row.get('before_pct'), row.get('perf_pct')
            if pp is None:
                pct_cell = f'<span style="color:#777">{_ord(bp)}</span> &rarr; <span style="color:#999">&mdash;</span>'
            else:
                pcol = '#1a8a3a' if (bp is not None and pp - bp >= 10) else ('#c0392b' if (bp is not None and pp - bp <= -10) else '#333')
                pct_cell = (f'<span style="color:#777">{_ord(bp)}</span> &rarr; '
                            f'<span style="color:{pcol};font-weight:600">{_ord(pp)}</span>')
            races = f'{row["races"]}/{d["n_races"]}'
            trs += (
                f'        <tr class="row{i % 2}">\n'
                f'          <td class="totalcell">{row["finish"]}</td>\n'
                f'          <td style="text-align:left;white-space:nowrap">{sailor_cell(row)}</td>\n'
                f'          <td class="schoolname" style="text-align:left">{school_html}</td>\n'
                f'          <td>{races}</td>\n'
                f'          <td>{before}{star}</td>\n'
                f'          <td style="font-weight:600">{perf}</td>\n'
                f'          <td>{delta}</td>\n'
                f'          <td style="white-space:nowrap">{pct_cell}</td>\n'
                f'          <td>{surprise}</td>\n'
                f'          <td>{pct_html}</td>\n'
                f'          <td style="white-space:nowrap">{row["avg_finish_actual"]:.1f} / '
                f'<span style="color:#777">{row["avg_finish_expected"]:.1f}</span></td>\n'
                f'          <td>{r100(row["rating_after"])}{star}</td>\n'
                f'        </tr>\n'
            )
        panels += (
            f'    <h3 style="margin:1.2em 0 0.2em">{"Combined divisions" if d["division"] == "Combined" else "Division " + esc(d["division"])}\n'
            f'      <span style="font-weight:normal;color:#777;font-size:0.85em">&nbsp;'
            f'{d["n_boats"]} boats &middot; {d["n_races"]} races</span></h3>\n'
            '    <div style="overflow-x:auto">\n'
            '    <table class="results">\n'
            '      <thead><tr>\n'
            '          <th title="Boat&#39;s finishing place in the division">Fin</th>\n'
            '          <th style="text-align:left">Skipper</th>\n'
            '          <th style="text-align:left">School</th>\n'
            '          <th title="Races skippered / races sailed">Races</th>\n'
            f'          <th title="Model rating from the last rated week before this event">Rating<br>before{star}</th>\n'
            '          <th title="Rating at which this sailor&#39;s actual race finishes are least '
            'surprising, with everyone else fixed">Perf.<br>rating</th>\n'
            '          <th title="Performance rating minus rating before">&Delta;</th>\n'
            '          <th title="Where the pre-event rating and the performance rating fall among '
            'skippers active in this league during the past year (percentile)">Percentile<br>before &rarr; perf.</th>\n'
            '          <th title="Extra surprisal (bits) of the actual finishes at the pre-event rating '
            'versus the performance rating">Surprise<br>(bits)</th>\n'
            '          <th title="Chance that, at the pre-event rating, the sailor scores this many points or '
            'fewer in the races they skippered (against these exact fleets)">P(this good<br>or better)</th>\n'
            '          <th title="Average race finish: actual vs. expected at the pre-event rating">Avg finish<br>actual / exp.</th>\n'
            f'          <th title="Model rating for this week after including the event">Rating<br>after{star}</th>\n'
            '      </tr></thead>\n'
            '      <tbody>\n'
            f'{trs}      </tbody>\n'
            '    </table>\n'
            '    </div>\n'
        )

    notes = []
    if 'new' in used_flags:
        notes.append('<sup>n</sup> no rating before this event &mdash; the model&#39;s estimate '
                     'from this week is used as the baseline.')
    if 'unrated' in used_flags:
        notes.append('<sup>u</sup> not rated by the model &mdash; the division median is used as the baseline.')
    if 'last' in used_flags:
        notes.append('&mdash; in the performance column means the skipper finished last in every race '
                     'they sailed, so no finite least-surprisal rating exists.')
    notes_html = ''.join(f'<li>{n}</li>' for n in notes)
    notes_block = (f'<ul style="color:#555;margin:0.3em 0 0 1.2em">{notes_html}</ul>'
                   if notes_html else '')

    intro = (
        '    <div style="max-width:60em;margin:0.5em auto 0;font-size:0.92em;line-height:1.45">\n'
        '      <p><strong>Performance rating</strong> answers one question for each skipper:\n'
        '      <em>holding every other skipper&#39;s rating fixed, which rating makes my actual '
        'race-by-race finishes least surprising?</em>\n'
        '      It is the maximum-likelihood rating under the same Plackett&ndash;Luce model that '
        'produces the\n'
        f'      <a href="{P}rankings/">power rankings</a>{_methodology_note(P)}, '
        'with each opponent fixed at their rating from '
        'the last rated week before this event\n'
        f'      ({esc(res["week_label"])}). Substitute skippers are scored only on the races they '
        'sailed, and races the boat did not finish (OCS, DNF and similar) are left out.</p>\n'
        f'      <p><strong>Percentile</strong> places a rating among the {res.get("n_reference", 0):,} '
        f'{esc(res["league"])} skippers who were rated within the year before this event, so it reads as '
        '&ldquo;better than X% of the skippers racing in this league right now&rdquo;. The arrow shows '
        'where the sailor stood going in and where this performance would put them.</p>\n'
        f'      <p><strong>Surprise</strong> is how many bits more surprising the sailor&#39;s finishes '
        'were at their pre-event rating than at their performance rating (0 = exactly as expected). '
        f'<strong>P(this good or better)</strong> simulates the regatta {res["sims"]:,} times with the sailor at '
        'their pre-event rating against the actual fleet, and reports how often they would score this many '
        'points or fewer in the races they skippered. Races in one regatta are not independent, so each '
        'simulation also gives every skipper a good or bad regatta, form that drifts from race to race, and the '
        'speed of each boat the rotation puts them in '
        '(<a href="../../../methodology/#races-not-independent">why</a>). Low means an unusually good regatta, high means an '
        'unusually poor one. It deliberately ignores how rivals actually sailed, so it agrees with the '
        'performance rating rather than with the division finish, which also depends on everyone else&#39;s day. '
        'Ratings are shown on the rankings scale (model rating &times; 100). '
        'Only skippers are rated; crews are not part of the model.</p>\n'
        f'      {notes_block}\n'
        '    </div>\n'
    )

    if provisional:
        intro = (
            '    <!-- ratings-provisional -->\n'
            '    <p class="ratings-provisional" style="max-width:60em;margin:0.8em auto 0;padding:0.5em 0.8em;'
            'background:#fff8e1;border:1px solid #f0d58a;border-radius:6px;font-size:0.9em">'
            '<strong>*</strong> This regatta is not official yet. Ratings marked * are from the last weekly '
            'update and have not absorbed this event; the performance columns use the results posted so far, '
            'and everything refreshes as scoring is finalised.</p>\n') + intro

    tabbed_port = (
        '  <div class="port">\n'
        '    <div class="tabs">\n'
        '    <a class="tab-btn" href="../" style="text-decoration:none;color:inherit;'
        'display:inline-block;">&larr; Results</a>\n'
        '    <button class="tab-btn active">Analysis</button>\n'
        '    </div>\n'
        '  <div class="tab-panel active">\n'
        f'{intro}{panels}  </div>\n'
        '  </div>'
    )

    return (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '  <meta charset="UTF-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f'  <title>{esc(regatta_name)} Analysis | ICSA Real-Time Regatta Results</title>\n'
        f'  <link rel="stylesheet" href="{P}styles.css" media="screen,print">\n'
        '</head>\n'
        '<body>\n'
        f'{_regatta_nav(season_code, season_label_str, prefix=P)}\n'
        '<div id="page-content">\n'
        f'{_regatta_header(regatta_name + " — Analysis", info_html)}\n'
        f'{tabbed_port}\n'
        '</div>\n'
        '</body>\n'
        '</html>'
    )


def _tr_results_grid_panel(regatta_url, standings, logo_prefix):
    """Round-robin results grid: one matrix per round_num, colour-coded W/L/X.

    Uses tr_matches (winner_slug, loser_slug, positions) and tr_standings
    (final place) to order rows.
    """
    with db() as con:
        matches = con.execute('''
            SELECT round_num, winner_slug, winner_team, winner_positions,
                   loser_slug,  loser_team,  loser_positions
            FROM tr_matches WHERE regatta_url=?
            ORDER BY round_num, id
        ''', (regatta_url,)).fetchall()
    if not matches:
        return ''

    # Ordered team list by final standings place
    # standings = [(place, school, slug, team, wins, losses), ...]
    team_order = [(slug, f'{school} {team}'.strip()) for _, school, slug, team, _, _ in standings]
    # Fallback: add any slugs seen in matches but not in standings
    seen_slugs = {s for s in ([r[1] for r in matches] + [r[4] for r in matches])}
    for slug in seen_slugs:
        if not any(s == slug for s, _ in team_order):
            team_order.append((slug, slug))

    # Group matches by round
    from collections import defaultdict as _dd
    by_round = _dd(list)
    for row in matches:
        by_round[row[0]].append(row)

    html = ''
    for round_num in sorted(by_round):
        round_matches = by_round[round_num]

        # Build cell lookup: (row_slug, col_slug) -> ('W'|'L'|'X', positions)
        cell: dict[tuple, tuple] = {}
        for _, w_slug, _, w_pos, l_slug, _, l_pos in round_matches:
            cell[(w_slug, l_slug)] = ('W', w_pos or '')
            cell[(l_slug, w_slug)] = ('L', l_pos or '')
        for slug, _ in team_order:
            cell[(slug, slug)] = ('X', '')

        # Only include teams that appear in this round
        round_slugs = {r[1] for r in round_matches} | {r[4] for r in round_matches}
        active = [(s, lbl) for s, lbl in team_order if s in round_slugs]

        if not active:
            continue

        # Column headers (abbreviated to last word of label)
        col_ths = ''.join(
            f'<th style="writing-mode:vertical-rl;transform:rotate(180deg);'
            f'white-space:nowrap;padding:4px 2px;font-size:0.75em">'
            f'{esc(lbl.split()[-1])}</th>'
            for _, lbl in active
        )

        # Compute per-round record for each team
        round_record: dict[str, str] = {}
        for slug, _ in active:
            w = sum(1 for _, ws, _, _, ls, _, _ in round_matches if ws == slug)
            l = sum(1 for _, ws, _, _, ls, _, _ in round_matches if ls == slug)
            round_record[slug] = f'{w}–{l}'

        rows_html = ''
        for i, (slug, lbl) in enumerate(active):
            logo = f'{logo_prefix}{esc(slug)}.png'
            rec  = round_record.get(slug, '')
            cells = ''
            for col_slug, _ in active:
                result, pos = cell.get((slug, col_slug), (None, ''))
                tip = f' title="{esc(pos)}"' if pos else ''
                if result == 'X':
                    cells += '<td style="text-align:center;background:#eee;color:#aaa">✕</td>'
                elif result == 'W':
                    cells += f'<td style="text-align:center;background:#d4edda;color:#155724;font-weight:bold"{tip}>W</td>'
                elif result == 'L':
                    cells += f'<td style="text-align:center;background:#f8d7da;color:#721c24"{tip}>L</td>'
                else:
                    cells += '<td style="text-align:center;color:#ccc">–</td>'
            rows_html += (
                f'<tr class="row{i % 2}">'
                f'<td class="burgee-cell"><img src="{logo}" alt="" height="30" width="45"'
                f' onerror="this.style.display=\'none\'"></td>'
                f'<td class="schoolname" style="font-size:0.85em">{esc(lbl)}</td>'
                f'<td style="text-align:center;font-weight:bold;font-size:0.85em">{esc(rec)}</td>'
                f'{cells}'
                f'</tr>\n'
            )

        html += f'''    <h4 style="margin:1em 0 0.5em">Round {round_num}</h4>
    <div style="overflow-x:auto">
    <table class="results" style="font-size:0.85em">
      <thead><tr><th></th><th>Team</th><th>Rec.</th>{col_ths}</tr></thead>
      <tbody>
{rows_html}      </tbody>
    </table>
    </div>\n'''

    return html


def _tr_races_panel(regatta_url, logo_prefix):
    """Returns a single HTML panel with all match results and a team filter dropdown."""
    with db() as con:
        matches = con.execute('''
            SELECT round_num, match_num,
                   winner_school, winner_slug, winner_team, winner_positions,
                   loser_school,  loser_slug,  loser_team,  loser_positions
            FROM tr_matches WHERE regatta_url=?
            ORDER BY round_num, match_num
        ''', (regatta_url,)).fetchall()
    if not matches:
        return ''

    teams = sorted(set(wt for _, _, _, _, wt, _, _, _, _, _ in matches)
                 | set(lt for _, _, _, _, _, _, _, _, lt, _ in matches))
    opts = ''.join(f'<option value="{esc(t)}">{esc(t)}</option>' for t in teams)

    rows_html = ''
    for i, (rn, mn, ws, wslug, wt, wp, ls, lslug, lt, lp) in enumerate(matches):
        wlogo = f'{logo_prefix}{esc(wslug)}.png'
        llogo = f'{logo_prefix}{esc(lslug)}.png'
        rows_html += (
            f'      <tr class="row{i % 2}" data-winner="{esc(wt)}" data-loser="{esc(lt)}">\n'
            f'        <td style="text-align:center;color:#888;font-size:0.85em">{mn}</td>\n'
            f'        <td class="burgee-cell"><img src="{wlogo}" alt="{esc(ws)}" height="30" width="45" onerror="this.style.display=\'none\'"></td>\n'
            f'        <td class="schoolname" style="font-weight:bold">{school_link(wt, wslug)}</td>\n'
            f'        <td style="color:#555;font-size:0.85em">{esc(wp)}</td>\n'
            f'        <td style="text-align:center;color:#aaa;padding:0 0.3em">def.</td>\n'
            f'        <td class="burgee-cell"><img src="{llogo}" alt="{esc(ls)}" height="30" width="45" onerror="this.style.display=\'none\'"></td>\n'
            f'        <td class="schoolname">{school_link(lt, lslug)}</td>\n'
            f'        <td style="color:#555;font-size:0.85em">{esc(lp)}</td>\n'
            f'      </tr>\n'
        )
    return f'''    <div style="margin:0.75em 0 0.5em;text-align:center">
      <label for="tr-filter" style="font-size:0.9em;margin-right:0.4em">Filter by team:</label>
      <select id="tr-filter" onchange="filterTRMatches(this)" style="font-size:1em;padding:0.3em 0.5em">
        <option value="">All teams</option>
        {opts}
      </select>
    </div>
    <table class="results tr-match-table" id="tr-match-table">
      <thead><tr>
          <th title="Match">#</th>
          <th colspan="3" style="text-align:left">Winner</th>
          <th></th>
          <th colspan="2" style="text-align:left">Loser</th>
          <th title="Positions">Pos.</th>
      </tr></thead>
      <tbody>
{rows_html}      </tbody>
    </table>
    <script>
    function filterTRMatches(sel) {{
      var val = sel.value;
      var rows = document.querySelectorAll('#tr-match-table tbody tr');
      rows.forEach(function(r) {{
        r.style.display = (!val || r.dataset.winner === val || r.dataset.loser === val) ? '' : 'none';
      }});
    }}
    </script>'''


def _tr_sailors_panel(regatta_url, standings, logo_prefix):
    """Returns HTML for sailors tab — unique skippers and crew pooled per team."""
    with db() as con:
        sailor_rows = con.execute('''
            SELECT school_slug, team, skipper, skipper_grade, crew, crew_grade
            FROM tr_sailors WHERE regatta_url=?
            ORDER BY school_slug, team, skipper
        ''', (regatta_url,)).fetchall()
    if not sailor_rows:
        return ''

    from collections import defaultdict, OrderedDict
    # (slug, team) -> {skippers: OrderedDict, crew: OrderedDict}
    sailor_map = defaultdict(lambda: {'skippers': OrderedDict(), 'crew': OrderedDict()})
    for slug, team, sk, sg, cr, cg in sailor_rows:
        key = (slug, team)
        sk, sg2 = _parse_name(sk); sg = sg or sg2
        cr, cg2 = _parse_name(cr); cg = cg or cg2
        if sk and sk not in sailor_map[key]['skippers']:
            sailor_map[key]['skippers'][sk] = sg
        if cr and cr not in sailor_map[key]['crew']:
            sailor_map[key]['crew'][cr] = cg

    table_rows = ''
    for i, (place, school, slug, team, wins, losses) in enumerate(standings):
        entry = sailor_map.get((slug, team))
        if not entry:
            continue
        logo = f'{logo_prefix}{esc(slug)}.png'
        skip_cell = '<br>'.join(
            f'<a href="../../sailors/{esc(sailor_slug(n, slug))}/">{esc(n)}</a>' + (f' <em>{esc(g)}</em>' if g else '')
            for n, g in entry['skippers'].items()
        )
        crew_cell = '<br>'.join(
            f'<a href="../../sailors/{esc(sailor_slug(n, slug))}/">{esc(n)}</a>' + (f' <em>{esc(g)}</em>' if g else '')
            for n, g in entry['crew'].items()
        )
        table_rows += (
            f'      <tr class="row{i % 2}">\n'
            f'        <td class="burgee-cell"><img src="{logo}" alt="{esc(school)}"'
            f' height="40" width="60" onerror="this.style.display=\'none\'"></td>\n'
            f'        <td class="schoolname" style="text-align:left">{school_link(school, slug)}</td>\n'
            f'        <td class="teamname" style="text-align:left">{esc(team)}</td>\n'
            f'        <td style="text-align:left;vertical-align:top">{skip_cell}</td>\n'
            f'        <td style="text-align:left;vertical-align:top">{crew_cell}</td>\n'
            f'      </tr>\n'
        )

    if not table_rows:
        return ''
    return f'''    <table class="results">
      <thead><tr>
          <th></th><th style="text-align:left">School</th><th style="text-align:left">Team</th>
          <th style="text-align:left">Skippers</th><th style="text-align:left">Crew</th>
      </tr></thead>
      <tbody>
{table_rows}      </tbody>
    </table>'''


def _load_keelboat_results(regatta_url):
    with db() as con:
        rows = con.execute('''
            SELECT school, school_slug, team, division, race_num, place, points, code,
                   div_penalty, div_penalty_points
            FROM keelboat_results WHERE regatta_url=?
            ORDER BY division, race_num, place
        ''', (regatta_url,)).fetchall()
    if not rows:
        return []
    fleet_sizes = {}
    for _, _, _, div, rn, *_ in rows:
        fleet_sizes[(div, rn)] = fleet_sizes.get((div, rn), 0) + 1
    return _scored('keelboat', regatta_url, rows, lambda div, rn: fleet_sizes[(div, rn)])


def _keelboat_summary(regatta_url, _cache={}):
    if regatta_url not in _cache:
        _cache[regatta_url] = _load_keelboat_results(regatta_url)
    rows = _cache[regatta_url]
    if not rows:
        return [], []
    teams, divisions = {}, set()
    for school, slug, team, div, rn, place in rows:
        k = (slug, team)
        if k not in teams:
            teams[k] = {'school': school, 'slug': slug, 'team': team,
                        'div_totals': {}, 'grand_total': 0}
        teams[k]['div_totals'][div] = teams[k]['div_totals'].get(div, 0) + place
        teams[k]['grand_total'] += place
        divisions.add(div)
    _add_penalties('keelboat', regatta_url, teams)
    return sorted(divisions), sorted(teams.values(), key=lambda t: t['grand_total'])  # keelboat


def _keelboat_division_races(regatta_url, division, _cache={}):
    if regatta_url not in _cache:
        _cache[regatta_url] = _load_keelboat_results(regatta_url)
    rows = [r for r in _cache[regatta_url] if r[3] == division]
    if not rows:
        return [], []
    race_nums = sorted(set(r[4] for r in rows))
    teams = {}
    order = []
    for school, slug, team, div, rn, place in rows:
        k = (slug, team)
        if k not in teams:
            teams[k] = {'school': school, 'slug': slug, 'team': team, 'scores': {}, 'total': 0,
                        'codes': _team_codes('keelboat', regatta_url, slug, team, div)}
            order.append(k)
        teams[k]['scores'][rn] = place
        teams[k]['total'] += place
    _add_penalties('keelboat', regatta_url, teams, division)
    sorted_teams = sorted(order, key=lambda k: teams[k]['total'])
    return race_nums, [teams[k] for k in sorted_teams]


def _keelboat_chart_json(regatta_url):
    import json as _json
    rows = _load_keelboat_results(regatta_url)
    if not rows:
        return None
    all_race_keys = sorted(set((rn, div) for _, _, _, div, rn, _ in rows),
                           key=lambda x: (x[0], x[1]))
    if len(all_race_keys) < 2:
        return None
    multi_div = len(set(d for _, d in all_race_keys)) > 1
    race_labels = [f'R{rn}{div}' if multi_div else f'R{rn}' for rn, div in all_race_keys]
    teams, team_order = {}, []
    for school, slug, team, div, rn, place in rows:
        k = (slug, team)
        if k not in teams:
            teams[k] = {'school': school, 'team': team, 'scores': {}}
            team_order.append(k)
        teams[k]['scores'][(rn, div)] = place
    team_list = [{'school': teams[k]['school'], 'team': teams[k]['team'],
                  'scores': [teams[k]['scores'].get(rk, 0) for rk in all_race_keys]}
                 for k in team_order]
    return _json.dumps({'races': race_labels, 'teams': team_list}, separators=(',', ':'))


SAILOR_PREFIX = '../../'


# ── Kind of event ─────────────────────────────────────────────────────────────
def _regatta_kind(regatta_url, scoring='', _cache={}):
    """'fleet', 'singlehanded', 'keelboat' or 'team', as the scraper read it from the pages
    (event_kinds.py).  Regattas scraped before kinds were recorded fall back on the
    Scoring field of the regatta listing."""
    if 'kinds' not in _cache:
        try:
            with db() as con:
                _cache['kinds'] = dict(con.execute('SELECT regatta_url, kind FROM regatta_kinds'))
        except Exception:
            _cache['kinds'] = {}
    kind = _cache['kinds'].get(regatta_url)
    if kind:
        return kind
    return 'singlehanded' if scoring.strip().lower() == 'singlehanded' else 'fleet'


def _entry_sailors(regatta_url):
    """Singlehanded: (school_slug, team) -> 'Name <em>'27</em>' linked to the sailor's page."""
    with db() as con:
        rows = con.execute('''
            SELECT school_slug, team, skipper, skipper_grade FROM fleet_results
            WHERE regatta_url=? AND skipper != '' AND skipper NOT LIKE '%Unreported%'
            GROUP BY school_slug, team''', (regatta_url,)).fetchall()
    out = {}
    for slug, team, name, grade in rows:
        name, grade2 = _parse_name(name); grade = grade or grade2
        cell = f'<a href="{SAILOR_PREFIX}sailors/{esc(sailor_slug(name, slug))}/">{esc(name)}</a>'
        if grade:
            cell += f' <em>{esc(grade)}</em>'
        out[(slug, team)] = cell
    return out


def _keelboat_sailors_table(regatta_url):
    with db() as con:
        # A database built before roles were recorded has no role column.
        has_role = any(r[1] == 'role' for r in con.execute('PRAGMA table_info(keelboat_sailors)'))
        role = 'role' if has_role else "''"
        rows = con.execute(f'''
            SELECT school_slug, school, team, division, sailor_name, grade, races, {role} AS role
            FROM keelboat_sailors WHERE regatta_url=?
            ORDER BY division, school, team,
                     CASE {role} WHEN 'skipper' THEN 0 WHEN 'crew' THEN 1 WHEN 'reserve' THEN 2 ELSE 1 END,
                     id
        ''', (regatta_url,)).fetchall()
    if not rows:
        return ''

    # Group by (slug, team, division)
    from collections import defaultdict, OrderedDict
    groups = OrderedDict()
    for slug, school, team, div, name, grade, race_str, role in rows:
        key = (slug, school, team, div)
        if key not in groups:
            groups[key] = []
        groups[key].append((name, grade, race_str, role or ''))

    divisions = sorted(set(k[3] for k in groups))
    multi_div = len(divisions) > 1

    td_l = 'style="text-align:left"'

    table_rows = ''
    shade_idx = 0
    prev_team = None
    for (slug, school, team, div), sailors in groups.items():
        team_key = (slug, team)
        if team_key != prev_team:
            shade_idx = (shade_idx + 1) % 2 if prev_team is not None else 0
            prev_team = team_key
        shade = f'row{shade_idx}'
        logo = f'../../icsa_logos/{esc(slug)}.png'

        div_label = f' <span style="color:#777;font-size:0.85em">Div {esc(div)}</span>' if multi_div else ''
        sailor_lines = []
        has_roles = any(role for *_, role in sailors)
        for raw_name, grade, race_str, role in sailors:
            name, grade2 = _parse_name(raw_name); grade = grade or grade2
            s_slug = sailor_slug(name, slug)
            s = f'<a href="../../sailors/{esc(s_slug)}/">{esc(name)}</a>'
            if grade:
                s += f' <em>{esc(grade)}</em>'
            if race_str:
                s += f' <span style="color:#888;font-size:0.85em">[{esc(race_str)}]</span>'
            if has_roles:
                # The role is what sets a keelboat crew apart from a list of names.
                label = {'skipper': 'Skipper', 'crew': 'Crew', 'reserve': 'Reserve'}.get(role, '')
                tone = '#1a4f8a' if role == 'skipper' else '#888'
                weight = '700' if role == 'skipper' else '400'
                s = (f'<span style="display:inline-block;min-width:4.6em;font-size:0.8em;'
                     f'color:{tone};font-weight:{weight}">{label}</span>{s}')
            sailor_lines.append(s)
        sailors_cell = '<br>'.join(sailor_lines)

        table_rows += (
            f'      <tr class="{shade}">\n'
            f'        <td class="burgee-cell"><img src="{logo}" alt="{esc(school)}"'
            f' height="40" width="60" onerror="this.style.display=\'none\'"></td>\n'
            f'        <td class="schoolname" {td_l}>{school_link(school, slug)}{div_label}</td>\n'
            f'        <td class="teamname" {td_l}>{esc(team)}</td>\n'
            f'        <td {td_l} style="text-align:left;vertical-align:top">{sailors_cell}</td>\n'
            f'      </tr>\n'
        )

    if not table_rows:
        return ''
    return f'''    <table class="results">
      <thead><tr>
          <th></th><th style="text-align:left">School</th>
          <th style="text-align:left">Team</th>
          <th style="text-align:left">Sailors</th>
      </tr></thead>
      <tbody>
{table_rows}      </tbody>
    </table>'''


def _disc_sailor_href(name, school_slug):
    """Link target for a skipper on a regatta page's Analysis tab, or '' when the sailor has no page."""
    slug = sailor_slug(name, school_slug)
    return f'{SAILOR_PREFIX}sailors/{esc(slug)}/' if os.path.exists(os.path.join(SITE, 'sailors', slug, 'index.html')) else ''


def build_keelboat_regatta_page(season_code, regatta_url, regatta_name, season_label_str):
    m = _get_regatta_meta().get((season_code, regatta_name.strip().lower()), {})
    host       = m.get('host', '')
    start_date = m.get('start_date', '')
    reg_type   = m.get('type', '')
    scoring    = m.get('scoring', '')

    with db() as con:
        boat_row = con.execute(
            'SELECT boat, is_womens FROM keelboat_results WHERE regatta_url=? LIMIT 1',
            (regatta_url,)).fetchone()
    boat      = boat_row[0] if boat_row else ''
    is_womens = bool(boat_row and boat_row[1])

    divisions, ranked = _keelboat_summary(regatta_url)
    if not ranked:
        return None

    logo_prefix = '../../icsa_logos/'

    # Score summary
    div_th = ''.join(f'          <th>{esc(d)}</th>\n' for d in divisions)
    summary_rows = ''
    for i, t in enumerate(ranked):
        logo = f'{logo_prefix}{esc(t["slug"])}.png'
        div_cells = ''.join(
            f'          <td class="total">{_total_cell(t, t["div_totals"].get(d, ""), d)}</td>\n'
            for d in divisions)
        summary_rows += (
            f'      <tr class="row{i % 2}">\n'
            f'        <td>{i + 1}</td>\n'
            f'        <td class="burgee-cell"><img src="{logo}" alt="{esc(t["school"])}"'
            f' height="40" width="60" onerror="this.style.display=\'none\'"></td>\n'
            f'        <td class="schoolname">{school_link(t["school"], t["slug"])}</td>\n'
            f'        <td class="teamname">{esc(t["team"])}</td>\n'
            f'{div_cells}'
            f'        <td class="totalcell">{_total_cell(t, t["grand_total"])}</td>\n'
            f'      </tr>\n'
        )
    summary_panel = f'''    <table class="results coordinate divisional">
      <thead><tr>
          <th>#</th><th></th>
          <th>School</th><th class="teamname">Team</th>
{div_th}          <th><abbr title="Total">TOT</abbr></th>
      </tr></thead>
      <tbody>
{summary_rows}      </tbody>
    </table>'''

    # Per-division race tables
    div_panels = {}
    for div in divisions:
        race_nums, team_rows = _keelboat_division_races(regatta_url, div)
        r_th = ''.join(f'          <th>R{n}</th>\n' for n in race_nums)
        div_rows = ''
        for i, t in enumerate(team_rows):
            logo = f'{logo_prefix}{esc(t["slug"])}.png'
            r_cells = ''.join(
                f'          <td style="width:2em;text-align:center;padding:0.15em 0.25em">{_score_cell(t, n)}</td>\n' for n in race_nums)
            div_rows += (
                f'        <tr class="row{i % 2}">\n'
                f'          <td class="burgee-cell"><img src="{logo}" alt="{esc(t["school"])}"'
                f' height="40" width="60" onerror="this.style.display=\'none\'"></td>\n'
                f'          <td class="schoolname">{school_link(t["school"], t["slug"])}</td>\n'
                f'          <td class="teamname">{esc(t["team"])}</td>\n'
                f'{r_cells}'
                f'          <td class="totalcell">{_total_cell(t, t["total"])}</td>\n'
                f'        </tr>\n'
            )
        div_panels[div] = f'''    <table class="tr-rotation-table results">
      <thead><tr>
          <th></th><th>School</th><th class="teamname">Team</th>
{r_th}          <th><abbr title="Total">TOT</abbr></th>
      </tr></thead>
      <tbody>
{div_rows}      </tbody>
    </table>'''

    chart_json   = _keelboat_chart_json(regatta_url)
    sailors_html = _keelboat_sailors_table(regatta_url)

    # Assemble tabs
    tab_btns   = '    <button class="tab-btn active" onclick="showTab(this,\'tab-summary\')">Score Summary</button>\n'
    tab_panels = f'  <div id="tab-summary" class="tab-panel active">\n{summary_panel}\n  </div>\n'

    for div in divisions:
        tid = f'tab-div-{div}'
        tab_btns   += f'    <button class="tab-btn" onclick="showTab(this,\'{tid}\')">Division {esc(div)}</button>\n'
        tab_panels += f'  <div id="{tid}" class="tab-panel">\n{div_panels[div]}\n  </div>\n'

    if sailors_html:
        tab_btns   += '    <button class="tab-btn" onclick="showTab(this,\'tab-sailors\')">Sailors</button>\n'
        tab_panels += f'  <div id="tab-sailors" class="tab-panel">\n{sailors_html}\n  </div>\n'

    import discipline_pages          # performance ratings on the keelboat model
    disc_html = discipline_pages.analysis_tab('keelboat', 'ICSA', regatta_url, school_link, _disc_sailor_href)
    if disc_html:
        tab_btns   += '    <button class="tab-btn" onclick="showTab(this,\'tab-analysis\')">Analysis</button>\n'
        tab_panels += f'  <div id="tab-analysis" class="tab-panel">\n{disc_html}\n  </div>\n'

    if chart_json:
        tab_btns   += '    <button class="tab-btn" onclick="showTab(this,\'tab-chart\')">Chart</button>\n'
        tab_panels += '  <div id="tab-chart" class="tab-panel"><div class="rpc-container"></div></div>\n'

    wx_btn, wx_panel = weather_tab_html(season_code, regatta_name, host,
                                        logo_prefix='../../icsa_logos/')
    if wx_btn:
        tab_btns   += wx_btn
        tab_panels += wx_panel

    tabbed_port = f'''  <div class="port">
    <div class="tabs">
{tab_btns}    </div>
{tab_panels}  </div>'''

    chart_script = ''
    if chart_json:
        chart_script = f'var _rpcData={chart_json};'

    weather_css = WEATHER_CSS if wx_btn else ''
    info_html = _page_info_items(host, start_date, reg_type, boat, scoring, is_womens)
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(regatta_name)} | ICSA Real-Time Regatta Results</title>
  <link rel="stylesheet" href="../../styles.css" media="screen,print">
{weather_css}
</head>
<body>
{_regatta_nav(season_code, season_label_str)}
<div id="page-content">
{_regatta_header(regatta_name, info_html)}
{tabbed_port}
</div>
<script src="../../chart.js"></script>
<script>
{chart_script}
function showTab(btn, id) {{
  var port = btn.closest('.port');
  port.querySelectorAll('.tab-btn').forEach(function(b){{b.classList.remove('active');}});
  port.querySelectorAll('.tab-panel').forEach(function(p){{p.classList.remove('active');}});
  btn.classList.add('active');
  var panel = document.getElementById(id);
  panel.classList.add('active');
  if (typeof _rpcData !== 'undefined') {{
    var ctr = panel.querySelector('.rpc-container');
    if (ctr) buildRaceChart(ctr, _rpcData);
  }}
}}
</script>
</body>
</html>'''


def build_tr_regatta_page(season_code, regatta_url, regatta_name, season_label_str):
    m = _get_regatta_meta().get((season_code, regatta_name.strip().lower()), {})
    host       = m.get('host', '')
    start_date = m.get('start_date', '')
    reg_type   = m.get('type', '')
    scoring    = m.get('scoring', '')

    with db() as con:
        standings = con.execute('''
            SELECT place, school, school_slug, team, wins, losses
            FROM tr_standings WHERE regatta_url=? ORDER BY place
        ''', (regatta_url,)).fetchall()
        boat_row = con.execute(
            'SELECT boat, is_womens FROM tr_standings WHERE regatta_url=? LIMIT 1',
            (regatta_url,)).fetchone()
    boat      = boat_row[0] if boat_row else ''
    is_womens = bool(boat_row and boat_row[1])

    if not standings:
        return None

    logo_prefix = '../../icsa_logos/'

    # Score summary tab
    summary_rows = ''
    for i, (place, school, slug_s, team, wins, losses) in enumerate(standings):
        logo = f'{logo_prefix}{esc(slug_s)}.png'
        rec = f'{wins}–{losses}' if wins is not None and losses is not None else ''
        summary_rows += (
            f'      <tr class="row{i % 2} topborder">\n'
            f'        <td>{place}</td>\n'
            f'        <td class="burgee-cell"><img src="{logo}" alt="{esc(school)}"'
            f' height="40" width="60" onerror="this.style.display=\'none\'"></td>\n'
            f'        <td class="schoolname">{school_link(school, slug_s)}</td>\n'
            f'        <td class="teamname">{esc(team)}</td>\n'
            f'        <td>{esc(rec)}</td>\n'
            f'      </tr>\n'
        )
    summary_panel = f'''    <table class="teamranking results">
      <thead><tr>
          <th>#</th><th></th>
          <th>School</th><th class="teamname">Team</th>
          <th title="Win–loss record">Rec.</th>
      </tr></thead>
      <tbody>
{summary_rows}      </tbody>
    </table>'''

    races_panel  = _tr_races_panel(regatta_url, logo_prefix)
    grid_panel   = _tr_results_grid_panel(regatta_url, standings, logo_prefix)
    sailors_html = _tr_sailors_panel(regatta_url, standings, logo_prefix)
    import team_race_pages
    ratings_html = team_race_pages.regatta_panel('ICSA', regatta_url, standings, school_link,
                                                 lambda n, s: f'../../sailors/{sailor_slug(n, s)}/')

    # Assemble tabs
    tab_btns   = '    <button class="tab-btn active" onclick="showTab(this,\'tab-summary\')">Score Summary</button>\n'
    tab_panels = f'  <div id="tab-summary" class="tab-panel active">\n{summary_panel}\n  </div>\n'

    if ratings_html:
        tab_btns   += '    <button class="tab-btn" onclick="showTab(this,\'tab-ratings\')">Ratings &amp; Forecast</button>\n'
        tab_panels += f'  <div id="tab-ratings" class="tab-panel">\n{ratings_html}\n  </div>\n'

    if grid_panel:
        tab_btns   += '    <button class="tab-btn" onclick="showTab(this,\'tab-results-grid\')">Results</button>\n'
        tab_panels += f'  <div id="tab-results-grid" class="tab-panel">\n{grid_panel}\n  </div>\n'

    if races_panel:
        tab_btns   += '    <button class="tab-btn" onclick="showTab(this,\'tab-races\')">Race Results</button>\n'
        tab_panels += f'  <div id="tab-races" class="tab-panel">\n{races_panel}\n  </div>\n'

    if sailors_html:
        tab_btns   += '    <button class="tab-btn" onclick="showTab(this,\'tab-sailors\')">Sailors</button>\n'
        tab_panels += f'  <div id="tab-sailors" class="tab-panel">\n{sailors_html}\n  </div>\n'

    wx_btn, wx_panel = weather_tab_html(season_code, regatta_name, host, logo_prefix='../../icsa_logos/')
    if wx_btn:
        tab_btns   += wx_btn
        tab_panels += wx_panel
    weather_css = WEATHER_CSS if wx_btn else ''

    tabbed_port = f'''  <div class="port">
    <div class="tabs">
{tab_btns}    </div>
{tab_panels}  </div>'''

    info_html = _page_info_items(host, start_date, reg_type, boat, scoring)
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(regatta_name)} | ICSA Real-Time Regatta Results</title>
  <link rel="stylesheet" href="../../styles.css" media="screen,print">
{weather_css}
</head>
<body>
{_regatta_nav(season_code, season_label_str)}
<div id="page-content">
{_regatta_header(regatta_name, info_html)}
{tabbed_port}
</div>
<script>
function showTab(btn, id) {{
  var port = btn.closest('.port');
  port.querySelectorAll('.tab-btn').forEach(function(b){{b.classList.remove('active');}});
  port.querySelectorAll('.tab-panel').forEach(function(p){{p.classList.remove('active');}});
  btn.classList.add('active');
  document.getElementById(id).classList.add('active');
}}
</script>
</body>
</html>'''


# ── sailor pages ──────────────────────────────────────────────────────────────

def _collect_icsa_sailors():
    """
    Collect all sailor data from all three tables in one pass.
    Returns dict keyed by (name, school_slug) ->
        {'school': str, 'grade': str, 'appearances': [
            {'season': str, 'regatta_url': str, 'regatta_name': str,
             'role': str, 'divs': set, 'places': [int]}
        ]}
    """
    sailors = {}

    def _ensure(name, school_slug, school, grade):
        key = (name, school_slug)
        if key not in sailors:
            sailors[key] = {'school': school, 'grade': grade or '', 'appearances': {}}
        elif grade and not sailors[key]['grade']:
            sailors[key]['grade'] = grade
        return key

    def _add_appearance(key, season, regatta_url, regatta_name, role, div, place, is_womens=False):
        app_key = regatta_url
        apps = sailors[key]['appearances']
        if app_key not in apps:
            apps[app_key] = {
                'season': season, 'regatta_url': regatta_url,
                'regatta_name': regatta_name, 'role': role,
                'divs': set(), 'places': [], 'is_womens': is_womens,
            }
        apps[app_key]['divs'].add(div)
        if place < 900:
            apps[app_key]['places'].append(place)

    with db() as con:
        # Fleet results - skippers and crew
        rows = con.execute('''
            SELECT season, regatta_url, regatta_name, division, place,
                   school, school_slug, skipper, skipper_grade, crew, crew_grade, is_womens
            FROM fleet_results
            WHERE skipper != '' AND skipper NOT LIKE '%Unreported%'
            ORDER BY season, regatta_url
        ''').fetchall()
        for season, reg_url, reg_name, div, place, school, slug, skipper, skip_gr, crew, crew_gr, is_w in rows:
            skipper, skip_gr2 = _parse_name(skipper)
            crew, crew_gr2 = _parse_name(crew)
            if skipper and not skipper.startswith('Unreported'):
                key = _ensure(skipper, slug, school, skip_gr or skip_gr2)
                _add_appearance(key, season, reg_url, reg_name, 'Skipper', div, place, bool(is_w))
            if crew and not crew.startswith('Unreported'):
                key = _ensure(crew, slug, school, crew_gr or crew_gr2)
                _add_appearance(key, season, reg_url, reg_name, 'Crew', div, place, bool(is_w))

        # TR sailors
        tr_standings_map = {}
        for url, slug, team, wins, losses, win_pct in con.execute(
            'SELECT regatta_url, school_slug, team, wins, losses, win_pct FROM tr_standings'
        ).fetchall():
            tr_standings_map[(url, slug, team)] = (int(wins or 0), int(losses or 0), float(win_pct or 0))

        tr_reg_seasons = {url: (season, name, bool(is_w)) for url, season, name, is_w in con.execute(
            'SELECT DISTINCT regatta_url, season, regatta_name, is_womens FROM tr_standings'
        ).fetchall()}
        school_for_slug = {slug: school for slug, school in con.execute(
            'SELECT slug, school FROM schools'
        ).fetchall()}
        rows = con.execute('''
            SELECT regatta_url, school_slug, team,
                   skipper, skipper_grade, crew, crew_grade
            FROM tr_sailors
            WHERE skipper != ''
            ORDER BY regatta_url, school_slug, team
        ''').fetchall()
        for reg_url, slug, team, skipper, skip_gr, crew, crew_gr in rows:
            season_name = tr_reg_seasons.get(reg_url)
            if not season_name:
                continue
            season, reg_name, is_w = season_name
            school = school_for_slug.get(slug, slug)
            tr_rec = tr_standings_map.get((reg_url, slug, team), (0, 0, 0.0))
            for raw_p, col_gr, role in [
                (skipper, skip_gr, 'Skipper'),
                (crew, crew_gr, 'Crew'),
            ]:
                person, name_gr = _parse_name(raw_p)
                if not person:
                    continue
                key = _ensure(person, slug, school, col_gr or name_gr)
                app_key = reg_url
                apps = sailors[key]['appearances']
                if app_key not in apps:
                    apps[app_key] = {
                        'season': season, 'regatta_url': reg_url,
                        'regatta_name': reg_name, 'role': role,
                        'divs': {'TR'}, 'places': [], 'type': 'tr',
                        'tr_record': tr_rec, 'is_womens': is_w,
                    }

        # Keelboat sailors — look up actual team places from keelboat_results
        kb_places_map = {}
        for url, slug, team, div, place in con.execute(
            'SELECT regatta_url, school_slug, team, division, place FROM keelboat_results'
        ).fetchall():
            kb_places_map.setdefault((url, slug, team, div), []).append(int(place))

        rows = con.execute('''
            SELECT ks.regatta_url, ks.school_slug, ks.school, ks.team, ks.division,
                   ks.sailor_name, ks.grade,
                   kr.season, kr.regatta_name, ks.role, kr.is_womens
            FROM keelboat_sailors ks
            JOIN (SELECT regatta_url, season, regatta_name, MAX(COALESCE(is_womens, 0)) AS is_womens FROM keelboat_results GROUP BY regatta_url, season, regatta_name) kr
                 ON kr.regatta_url = ks.regatta_url
            WHERE ks.sailor_name != ''
        ''').fetchall()
        for reg_url, slug, school, team, div, raw_name, grade, season, reg_name, kb_role, kb_womens in rows:
            name, name_gr = _parse_name(raw_name)
            if not name:
                continue
            key = _ensure(name, slug, school, grade or name_gr)
            places_list = [p for p in kb_places_map.get((reg_url, slug, team, div), []) if p < 900]
            app_key = reg_url
            apps = sailors[key]['appearances']
            if app_key not in apps:
                apps[app_key] = {
                    'season': season, 'regatta_url': reg_url,
                    'regatta_name': reg_name,
                    'role': {'skipper': 'Skipper', 'crew': 'Crew', 'reserve': 'Reserve'}.get((kb_role or '').lower(), 'Crew'),
                    'divs': {div}, 'places': places_list, 'type': 'keelboat', 'is_womens': bool(kb_womens),
                }
            else:
                apps[app_key]['divs'].add(div)

    # Convert appearances dict to sorted list
    for key in sailors:
        apps = sailors[key]['appearances']
        sailors[key]['appearances'] = sorted(
            apps.values(),
            key=lambda a: (season_sort_key(a['season']), a['regatta_url']),
            reverse=True
        )

    return sailors


def _load_sailor_clusters():
    """
    Load data/sailor_clusters.csv.
    Returns dict: (league, name_lower, school_slug) -> list of peer dicts
    where each peer is {league, name, school_slug, school, grade, cluster_type}.
    """
    path = os.path.join(BASE, 'data', 'sailor_clusters.csv')
    if not os.path.exists(path):
        return {}

    # cluster_id -> list of row dicts
    by_cluster = {}
    with open(path, newline='', encoding='utf-8') as f:
        for row in _csv.DictReader(f):
            cid = row['cluster_id']
            by_cluster.setdefault(cid, []).append(row)

    # Build lookup: (league, name_lower, school_slug) -> list of OTHER members
    lookup = {}
    for cid, members in by_cluster.items():
        for m in members:
            key = (m['league'], m['name'].lower(), m['school_slug'])
            others = [o for o in members if o is not m]
            lookup[key] = {'cluster_type': m['cluster_type'], 'peers': others}
    return lookup


def _recompute_percentiles():
    """
    Recompute rating percentiles per league from pl_weekly_latest.csv and write:
      - analysis/plackett_luce/rating_percentiles.json
      - website/chart.js  (updates the hardcoded PCTILE_DATA block)
    """
    import json as _json
    import re as _re

    def _percentile(sorted_vals, p):
        n = len(sorted_vals)
        if n == 0:
            return 0.0
        idx = p / 100.0 * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)

    latest_csv = os.path.join(BASE, 'analysis', 'plackett_luce', 'pl_weekly_latest.csv')
    if not os.path.exists(latest_csv):
        print('  [percentiles] pl_weekly_latest.csv not found — skipping')
        return

    pct_keys = [1, 5, 10, 25, 50, 75, 90, 95, 99, 99.5, 99.9, 99.99]
    buckets = {'all': [], 'ICSA': [], 'ISSA': []}

    with open(latest_csv, newline='', encoding='utf-8') as f:
        for row in _csv.DictReader(f):
            try:
                r = float(row['rating'])
            except (ValueError, KeyError):
                continue
            buckets['all'].append(r)
            lg = row.get('league', '')
            if lg in buckets:
                buckets[lg].append(r)

    percentiles = {}
    for lg, vals in buckets.items():
        if not vals:
            continue
        sv = sorted(vals)
        percentiles[lg] = {str(p): round(_percentile(sv, p), 6) for p in pct_keys}

    # ── Write rating_percentiles.json ─────────────────────────────────────────
    out_json = os.path.join(BASE, 'analysis', 'plackett_luce', 'rating_percentiles.json')
    payload = {
        'source': 'pl_weekly_latest.csv',
        'n_sailors': len(buckets['all']),
        'percentiles': pct_keys,
        **percentiles,
    }
    with open(out_json, 'w', encoding='utf-8') as f:
        _json.dump(payload, f, indent=2)

    # ── Patch chart.js PCTILE_DATA block ─────────────────────────────────────
    chart_js = os.path.join(BASE, 'website', 'chart.js')
    if not os.path.exists(chart_js):
        print('  [percentiles] chart.js not found — skipping patch')
        return

    def _fmt(d):
        inner = ','.join(f"'{k}':{v}" for k, v in d.items())
        return '{' + inner + '}'

    all_str  = _fmt(percentiles.get('all',  {}))
    icsa_str = _fmt(percentiles.get('ICSA', {}))
    issa_str = _fmt(percentiles.get('ISSA', {}))
    n = len(buckets['all'])
    new_block = (
        f"    // Percentile data (auto-recomputed from pl_weekly_latest.csv, n={n:,})\n"
        f"    var PCTILE_DATA = {{\n"
        f"      all:  {all_str},\n"
        f"      ICSA: {icsa_str},\n"
        f"      ISSA: {issa_str}\n"
        f"    }};"
    )

    with open(chart_js, 'r', encoding='utf-8') as f:
        src = f.read()

    patched = _re.sub(
        r'    // Percentile data.*?var PCTILE_DATA = \{.*?\};',
        new_block,
        src,
        flags=_re.DOTALL,
    )
    if patched == src:
        print('  [percentiles] WARNING: could not find PCTILE_DATA block in chart.js')
    else:
        with open(chart_js, 'w', encoding='utf-8') as f:
            f.write(patched)
        print(f'  [percentiles] Updated chart.js + rating_percentiles.json'
              f'  (all={len(buckets["all"]):,}  ICSA={len(buckets["ICSA"]):,}'
              f'  ISSA={len(buckets["ISSA"]):,})')


def _load_rating_lookups():
    """Load weekly ratings, season info, and regatta→week mapping for chart generation."""
    import time as _time
    from datetime import date as _date, datetime as _datetime

    pl_dir = os.path.join(BASE, 'analysis', 'plackett_luce')
    data_dir = os.path.join(BASE, 'data')

    def iso_week_to_date(label):
        try:
            return _datetime.strptime(label + '-1', '%G-W%V-%u').strftime('%Y-%m-%d')
        except (ValueError, AttributeError):
            return None

    def wk_to_season(wk):
        try:
            yr = int(wk[:4]) % 100
            return f's{yr:02d}' if int(wk[6:]) <= 25 else f'f{yr:02d}'
        except (ValueError, IndexError):
            return ''

    # sailor_id -> [(iso_week, rating), ...] sorted chronologically
    sailor_ratings = {}
    weekly_csv = os.path.join(pl_dir, 'pl_weekly_ratings.csv')
    if os.path.exists(weekly_csv):
        with open(weekly_csv, newline='', encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                sailor_ratings.setdefault(row['sailor_id'], []).append(
                    (row['iso_week'], float(row['rating'])))
        for sid in sailor_ratings:
            sailor_ratings[sid].sort()

    # (sailor_id, season) -> (school, league)
    season_info = {}
    ss_csv = os.path.join(pl_dir, 'sailor_seasons.csv')
    if os.path.exists(ss_csv):
        with open(ss_csv, newline='', encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                season_info[(row['sailor_id'], row['season'])] = (row['school'], row['league'])

    # regatta_url -> iso_week_label  (from regattas + results CSVs)
    def parse_date_str(s):
        for fmt in ('%m/%d/%Y', '%Y-%m-%d'):
            try:
                t = _time.strptime(s, fmt)
                d = _date(t.tm_year, t.tm_mon, t.tm_mday)
                y, w, _ = d.isocalendar()
                return f'{y}-W{w:02d}'
            except (ValueError, TypeError):
                pass
        return None

    regatta_key_to_week = {}
    for fname in ('icsa_regattas.csv', 'issa_regattas.csv'):
        path = os.path.join(data_dir, fname)
        if os.path.exists(path):
            with open(path, newline='', encoding='utf-8') as f:
                for row in _csv.DictReader(f):
                    key = (row['season'].strip(), row['name'].strip().lower())
                    wk = parse_date_str(row.get('start_date', ''))
                    if wk:
                        regatta_key_to_week[key] = wk

    url_to_week = {}
    for fname in ('icsa_results.csv', 'issa_results.csv'):
        path = os.path.join(data_dir, fname)
        if os.path.exists(path):
            with open(path, newline='', encoding='utf-8') as f:
                for row in _csv.DictReader(f):
                    url = row['regatta_url']
                    if url not in url_to_week:
                        key = (row['season'].strip(), row['regatta_name'].strip().lower())
                        wk = regatta_key_to_week.get(key)
                        if wk:
                            url_to_week[url] = wk

    # (name_lower, school_lower) -> sailor_id
    name_to_sid = {}
    latest_csv = os.path.join(pl_dir, 'pl_weekly_latest.csv')
    if os.path.exists(latest_csv):
        with open(latest_csv, newline='', encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                key = (row['name'].lower().strip(), row['school'].lower().strip())
                name_to_sid[key] = row['sailor_id']

    # Supplement with cluster alternate names so merged sailors resolve to the
    # same rating even when their display name differs between ICSA and ISSA.
    clusters_csv = os.path.join(BASE, 'data', 'sailor_clusters.csv')
    if os.path.exists(clusters_csv):
        rated_cids = set(name_to_sid.values())
        with open(clusters_csv, newline='', encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                cid = 'C' + row['cluster_id']
                if cid not in rated_cids:
                    continue
                key = (row['name'].lower().strip(), row['school'].lower().strip())
                if key not in name_to_sid:
                    name_to_sid[key] = cid

    return {
        'sailor_ratings': sailor_ratings,
        'season_info': season_info,
        'url_to_week': url_to_week,
        'name_to_sid': name_to_sid,
        'iso_week_to_date': iso_week_to_date,
        'wk_to_season': wk_to_season,
    }


def _get_rating_points(name, school_name, appearances, rl):
    """Return sorted rating chart points for a sailor, or None if unavailable."""
    sid = rl['name_to_sid'].get((name.lower().strip(), school_name.lower().strip()))
    if not sid:
        return None
    ratings = rl['sailor_ratings'].get(sid)
    if not ratings or len(ratings) < 2:
        return None

    # iso_week -> [regatta_names] from appearances
    week_to_regattas = {}
    for app in appearances:
        wk = rl['url_to_week'].get(app['regatta_url'])
        if wk:
            rn = app['regatta_name']
            lst = week_to_regattas.setdefault(wk, [])
            if rn not in lst:
                lst.append(rn)

    points = []
    for iso_week, rating in ratings:
        date_str = rl['iso_week_to_date'](iso_week)
        if not date_str:
            continue
        season = rl['wk_to_season'](iso_week)
        school, league = rl['season_info'].get((sid, season), (school_name, 'ICSA'))
        points.append({
            'date': date_str,
            'iso': iso_week,
            'rating': round(rating, 4),
            'league': league,
            'school': school,
            'regattas': week_to_regattas.get(iso_week, []),
        })
    return points if len(points) >= 2 else None


_AWAY_EVENT = None
_AWAY_MATCHES = None


def _away_event():
    """The outside championship whose selected sailors and results we show (data/hinman_2026.json).

    Keys: event, url, start, end, season, slug, title, short_title, venue, host, results_url, standings
    [[rank, team]], names (a set).  The event is listed with its season's regattas, has its own page, and each
    selected sailor gets a row in that season on their profile.
    """
    global _AWAY_EVENT
    if _AWAY_EVENT is None:
        import json as _j
        try:
            with open(os.path.join(BASE, 'data', 'hinman_2026.json'), encoding='utf-8') as f:
                d = _j.load(f) or {}
            _AWAY_EVENT = {**d, 'event': d.get('event', ''), 'url': d.get('url', ''), 'names': set(d.get('names') or ())}
        except (OSError, ValueError):
            _AWAY_EVENT = {'event': '', 'url': '', 'names': set()}
    return _AWAY_EVENT


def _norm_person(s):
    import unicodedata
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]+', ' ', s)).strip()


def _away_matches(sailors_data=None):
    """({(name, school_slug): school_name} for selected sailors found in the ICSA results, [names not matched]).

    Exact match on normalised name only.  A name that fits several sailors is taken only when one of them raced
    more recently than the others; otherwise it is left unmatched rather than guessed.
    """
    global _AWAY_MATCHES
    if _AWAY_MATCHES is None:
        ev = _away_event()
        if not ev.get('names'):
            _AWAY_MATCHES = ({}, [])
            return _AWAY_MATCHES
        if sailors_data is None:
            sailors_data = _collect_icsa_sailors()
        idx = {}
        for (name, slug), info in sailors_data.items():
            if name and 'Unreported' not in name and info['appearances']:
                idx.setdefault(_norm_person(name), []).append((name, slug, info))
        matched, unmatched = {}, []
        for n in sorted(ev['names']):
            c = idx.get(_norm_person(n), [])
            if len(c) > 1:
                latest = max(season_sort_key(x[2]['appearances'][0]['season']) for x in c)
                c = [x for x in c if season_sort_key(x[2]['appearances'][0]['season']) == latest]
            if len(c) == 1:
                matched[(c[0][0], c[0][1])] = c[0][2]['school']
            else:
                unmatched.append(n)
        _AWAY_MATCHES = (matched, unmatched)
    return _AWAY_MATCHES


def _with_away(appearances, name, school_slug):
    """appearances plus a row for the outside championship when this sailor was selected for it."""
    ev = _away_event()
    if not ev.get('slug') or sailor_slug(name, school_slug) not in {sailor_slug(n, sl) for n, sl in _away_matches()[0]}:
        return appearances
    syn = {'season': ev['season'], 'regatta_url': f'/{ev["season"]}/{ev["slug"]}/', 'regatta_name': ev['short_title'],
           'role': 'Selected', 'divs': {'TR'}, 'places': [], 'type': 'away'}
    return sorted(list(appearances) + [syn], key=lambda a: (season_sort_key(a['season']), a['regatta_url']), reverse=True)


def _away_crew_linker():
    """name -> html for a kSail crew list entry: a link to the ICSA profile when the name is one of the vetted selected sailors
    (exact normalised name, or the same surname and first initial when that is unique among them), else plain text."""
    matched = _away_matches()[0]
    by_norm = {_norm_person(n): (n, sl) for (n, sl) in matched}
    by_key = {}
    for (n, sl) in matched:
        toks = _norm_person(n).split()
        if len(toks) >= 2:
            by_key.setdefault((toks[-1], toks[0][0]), []).append((n, sl))

    def link(name):
        hit = by_norm.get(_norm_person(name))
        if hit is None:
            toks = _norm_person(name).split()
            c = by_key.get((toks[-1], toks[0][0]), []) if len(toks) >= 2 else []
            hit = c[0] if len(c) == 1 else None
        if hit is None:
            return esc(name)
        return f'<a href="../../sailors/{esc(sailor_slug(hit[0], hit[1]))}/">{esc(name)}</a>'
    return link


def build_away_regatta_page():
    """website/icsa/<season>/<slug>/index.html for the outside championship: details, final standings, selected sailors."""
    ev = _away_event()
    matched, unmatched = _away_matches()
    season, slug = ev['season'], ev['slug']
    label = season_label(season)

    import hinman_page
    res = hinman_page.load()
    link = _away_crew_linker() if res else None
    by_team = {t['name']: t for t in (res or {}).get('teams', [])}
    standings_rows = ''
    for i, (rank, team) in enumerate(ev.get('standings', [])):
        t = by_team.get(team)
        extra = ''
        if t:
            s1 = next((x for x in t['stages'] if x['stage'] == 'Stage 1'), None)
            rec = f"{s1['won']}&ndash;{s1['sailed'] - s1['won']}" if s1 else ''
            extra = (f'<td style="text-align:left">{esc(t.get("school", ""))}</td><td style="text-align:center">{rec}</td>'
                     f'<td style="text-align:left;font-size:.9em">{", ".join(link(c) for c in t["crew"])}</td>')
        standings_rows += (f'      <tr class="row{i % 2}"><td style="text-align:center">{esc(rank)}</td>'
                           f'<td class="schoolname">{esc(team)}</td>{extra}</tr>\n')
    extra_head = ('<th style="text-align:left">Represents</th><th>Stage 1</th><th style="text-align:left">Crew</th>' if res else '')
    standings = (f'''  <div class="port">
    <h3>Final Standings</h3>
    <table class="results">
      <thead><tr><th style="text-align:center">Rank</th><th style="text-align:left">Team</th>{extra_head}</tr></thead>
      <tbody>
{standings_rows}      </tbody>
    </table>
    <p style="font-size:.85em;color:#666">Teams are club teams of three boats (six sailors). Crew names link to an ICSA profile when they match one of the selected sailors.
    Rotations and stage summaries are on the <a href="{esc(ev.get('results_url', ''))}" target="_blank" rel="noopener">official results page</a>.</p>
  </div>''') if standings_rows else ''

    by_school = {}
    for (name, school_slug), school_name in matched.items():
        by_school.setdefault((school_name, school_slug), []).append(name)
    school_blocks = ''
    for (school_name, school_slug), names in sorted(by_school.items()):
        items = ''.join(f'<li><a href="../../sailors/{esc(sailor_slug(n, school_slug))}/">{esc(n)}</a></li>' for n in sorted(names))
        school_blocks += (f'<div style="flex:0 0 14em;"><div style="font-weight:bold;margin-bottom:0.2em">'
                          f'<a href="../../schools/{esc(school_slug)}/">{esc(school_name)}</a></div>'
                          f'<ul style="margin:0 0 1em 1.1em;padding:0;font-size:.92em">{items}</ul></div>\n')
    other = ''
    if unmatched:
        other = (f'<p style="font-size:.9em;color:#555;max-width:70ch"><strong>Also selected, not matched to an ICSA sailor profile:</strong> '
                 f'{esc(", ".join(unmatched))}.</p>')
    sailors_panel = f'''  <div class="port">
    <h3>Selected Sailors</h3>
    <p style="font-size:.9em;color:#555;max-width:70ch">{len(matched)} sailors named on the official team rosters have ICSA profiles, listed here by school.
    The rosters do not say which club team each sailor raced for.</p>
    <div style="display:flex;flex-wrap:wrap;gap:1em 1.5em;align-items:flex-start">
{school_blocks}    </div>
{other}
  </div>'''

    extra_btns = extra_panels = ''
    if res:
        extra_btns = ('<button class="tab-btn" id="btn-matches" onclick="showAwayTab(this,\'tab-matches\')">Match Results</button>\n      '
                      '<button class="tab-btn" id="btn-teams" onclick="showAwayTab(this,\'tab-teams\')">Teams</button>')
        extra_panels = (f'    <div id="tab-matches" class="tab-panel"><div class="port">\n{hinman_page.matches_panel(res)}\n    </div></div>\n'
                        f'    <div id="tab-teams" class="tab-panel"><div class="port">\n{hinman_page.teams_panel(res, [tuple(x) for x in ev.get("standings", [])], link)}\n    </div></div>\n')

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(ev['title'])} | ICSA Real-Time Regatta Results</title>
  <link rel="stylesheet" href="../../styles.css" media="screen,print">
</head>
<body>
{nav('../../index.html', '../../schools/index.html', '../../sailors/index.html', '../../img/icsa-logo-real.png',
     '../index.html', label, logo_href='../../index.html', fantasy='../../fantasy/index.html')}
<div id="page-content">

  <div id="content-header">
    <h1>
      <span id="left-fill"></span>
      <span>{esc(ev['short_title'])}</span>
      <span id="right-fill"></span>
    </h1>
    <ul id="page-info">
      <li><span class="page-info-key">Season</span><span class="page-info-value"><a href="../index.html">{esc(label)}</a></span></li>
      <li><span class="page-info-key">Dates</span><span class="page-info-value">September 18&ndash;20, 2026</span></li>
      <li><span class="page-info-key">Venue</span><span class="page-info-value">{esc(ev.get('venue', ''))}</span></li>
      <li><span class="page-info-key">Host</span><span class="page-info-value">{esc(ev.get('host', ''))}</span></li>
      <li><span class="page-info-key">Format</span><span class="page-info-value">Team race, 24 teams</span></li>
      <li><span class="page-info-key">Links</span><span class="page-info-value"><a href="{esc(ev.get('results_url', ''))}" target="_blank" rel="noopener">Official results</a>
        &middot; <a href="{esc(ev.get('url', ''))}" target="_blank" rel="noopener">US Sailing event page</a></span></li>
    </ul>
  </div>

  <div class="port" id="away-tabs">
    <div class="tabs">
      <button class="tab-btn active" id="btn-standings" onclick="showAwayTab(this,'tab-standings')">Final Standings</button>
      {extra_btns}
      <button class="tab-btn" id="btn-sailors" onclick="showAwayTab(this,'tab-sailors')">Selected Sailors</button>
    </div>
    <div id="tab-standings" class="tab-panel active">
{standings}
    </div>
{extra_panels}
    <div id="tab-sailors" class="tab-panel">
{sailors_panel}
    </div>
  </div>

</div>
<script>
function showAwayTab(btn, id) {{
  var port = document.getElementById('away-tabs');
  port.querySelectorAll('.tab-btn').forEach(function(b){{b.classList.remove('active');}});
  port.querySelectorAll('.tab-panel').forEach(function(p){{p.classList.remove('active');}});
  btn.classList.add('active');
  document.getElementById(id).classList.add('active');
}}
(function () {{
  var want = (location.hash || '').replace('#', '');
  var btn = want && document.getElementById('btn-' + want);
  if (btn) {{ showAwayTab(btn, 'tab-' + want); }}
}}());
</script>
</body>
</html>'''


def _season_links(school_slug, season):
    """Links shown on a sailor's season header row: that season's team page and the season's regatta list."""
    out = ''
    if os.path.exists(os.path.join(SITE, 'schools', school_slug, season, 'index.html')):
        out += f' <a href="../../schools/{esc(school_slug)}/{esc(season)}/" style="font-weight:normal;font-size:.8em;margin-left:.8em">Team page</a>'
    if os.path.exists(os.path.join(SITE, season, 'index.html')):
        out += f' <a href="../../{esc(season)}/" style="font-weight:normal;font-size:.8em;margin-left:.6em">Season</a>'
    return out


def _asset_version() -> str:
    """Changes whenever the shared stylesheet or scripts change, so browsers (which keep them for 30 days) fetch the new ones."""
    import os as _os
    best = 0
    for rel in ('styles.css', 'chart.js', 'help.js'):
        try:
            best = max(best, int(_os.path.getmtime(_os.path.join(BASE, 'website', rel))))
        except OSError:
            pass
    return str(best)


ASSET_V = _asset_version()


def build_sailor_page(name, school_slug, school_name, grade, appearances, clusters=None, rating_points=None):
    """Build HTML for a single sailor's page."""
    slug = sailor_slug(name, school_slug)

    grad_year = grade if grade else ''

    total_regattas = len(appearances)
    def _app_races(app):
        if app.get('type') == 'tr':
            w, l, _ = app.get('tr_record', (0, 0, 0.0))
            return w + l
        return len(app['places'])

    total_races = sum(_app_races(app) for app in appearances)
    appearances = _with_away(appearances, name, school_slug)     # the outside championship is one more regatta in its season

    import performance_site
    _peers = [(q['name'], q['school']) for q in ((clusters or {}).get(('ICSA', name.lower(), school_slug)) or {}).get('peers', [])]
    perf_index = performance_site.regatta_perf_index(name, school_name, _peers)

    # Group by season
    from itertools import groupby
    rows_html = ''
    row_i = 0
    for season, group in groupby(appearances, key=lambda a: a['season']):
        rows_html += (
            f'      <tr class="totalrow" id="s-{esc(season)}">'
            f'<td colspan="6" style="text-align:left;font-weight:bold">'
            f'{esc(season_label(season))}{_season_links(school_slug, season)}</td></tr>\n'
        )
        for app in group:
            reg_url = app['regatta_url']
            reg_name = app['regatta_name']
            role = app['role']
            divs_str = ', '.join(sorted(app['divs']))
            app_type = app.get('type', 'fleet')

            if app_type == 'tr':
                wins, losses, win_pct = app.get('tr_record', (0, 0, 0.0))
                race_count = wins + losses
                avg_str = f'{win_pct:.0f}%' if race_count > 0 else '—'
            else:
                places = app['places']
                race_count = len(places)
                avg_str = f'{sum(places)/len(places):.1f}' if places else '—'

            reg_slug = reg_url.strip('/').split('/')[-1]
            reg_href = f'../../{season}/{esc(reg_slug)}/'
            womens_badge = ' <span class="badge badge-womens" style="font-size:.7em">Women&#39;s</span>' if app.get('is_womens') else ''
            name_cell = f'<a href="{reg_href}">{esc(reg_name)}</a>{womens_badge}'
            _sk = _regatta_kind(reg_url)
            if _sk in ('singlehanded', 'keelboat'):
                name_cell += (f' <span class="badge badge-kind" style="font-size:.7em;background:#e8eefc;color:#1a4f8a;padding:1px 6px;'
                              f'border-radius:8px;white-space:nowrap">{"Singlehanded" if _sk == "singlehanded" else "Keelboat"}</span>')

            rows_html += (
                f'      <tr class="row{row_i % 2}">\n'
                f'        <td class="schoolname">{name_cell}</td>\n'
                f'        <td>{esc(role)}</td>\n'
                f'        <td>{esc(divs_str)}</td>\n'
                f'        <td style="text-align:right">{race_count if race_count else "—"}</td>\n'
                f'        <td style="text-align:right">{avg_str}</td>\n'
                f'        <td style="text-align:right;white-space:nowrap">{performance_site.results_cell("ICSA", reg_url, role, name, school_name, perf_index)}</td>\n'
                f'      </tr>\n'
            )
            row_i += 1

    is_womens_sailor = any(app.get('is_womens') for app in appearances)
    womens_sailor_badge = ''          # not shown on profiles; women's events carry the tag instead (is_womens_sailor is still computed)

    away_badge = ''

    school_info = (f'<a href="../../schools/{esc(school_slug)}/">{esc(school_name)}</a>'
                   if os.path.exists(os.path.join(SITE, 'schools', school_slug, 'index.html')) else f'{esc(school_name)}')
    if grad_year:
        school_info += f' {esc(grad_year)}'

    logo = f'../../icsa_logos/{esc(school_slug)}.png'

    # Rating chart section
    import json as _json
    rating_chart_html = ''
    chart_script = ''
    if rating_points:
        import performance_site
        rating_json = _json.dumps({'points': rating_points, 'perf': performance_site.points(name, school_name, [(q['name'], q['school']) for q in ((clusters or {}).get(('ICSA', name.lower(), school_slug)) or {}).get('peers', [])])}, separators=(',', ':'))
        rating_chart_html = f'''
  <div class="port">
    <h3 style="margin-bottom:0.3em">Rating History</h3>
    <div id="rating-chart-container"></div>
  </div>'''
        chart_script = f'''<script src="../../chart.js?v={ASSET_V}"></script>
<script>
  buildRatingChart(document.getElementById('rating-chart-container'), {rating_json});
</script>'''

    import crew_site
    crew_html = crew_site.section('ICSA', name, school_name)
    import discipline_pages
    crew_html += discipline_pages.sailor_block('ICSA', name, school_name)
    import team_race_pages
    crew_html += team_race_pages.sailor_block('ICSA', name, school_name, school_slug)

    # Collapsible season rows — always included
    script_html = chart_script + f'<script src="../../../help.js?v={ASSET_V}"></script>' + '''<script>
  (function () {
    var tbody = document.querySelector('#results-table tbody');
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.rows);
    var groups = [], current = null;
    rows.forEach(function (tr) {
      if (tr.classList.contains('totalrow')) { current = { header: tr, rows: [] }; groups.push(current); }
      else if (current) { current.rows.push(tr); }
    });
    var want = (location.hash || '').replace('#', '');
    groups.forEach(function (g, idx) {
      var open = idx < 2 || g.header.id === want;
      var td = g.header.cells[0];
      td.style.cursor = 'pointer';
      var arrow = document.createElement('span');
      arrow.style.cssText = 'display:inline-block;margin-right:4px;transition:transform 0.15s;';
      arrow.textContent = '\u25bc';
      td.insertBefore(arrow, td.firstChild);
      function apply() {
        g.rows.forEach(function (r) { r.style.display = open ? '' : 'none'; });
        arrow.style.transform = open ? '' : 'rotate(-90deg)';
      }
      apply();
      g.header.addEventListener('click', function () { open = !open; apply(); });
      if (g.header.id === want) { setTimeout(function () { g.header.scrollIntoView(); }, 0); }
    });
  }());
</script>'''

    # Related sailors section
    related_html = ''
    if clusters:
        cluster_info = clusters.get(('ICSA', name.lower(), school_slug))
        if cluster_info:
            peer_rows = ''
            for p in sorted(cluster_info['peers'],
                            key=lambda x: (x['league'], x.get('grade', ''))):
                p_league = p['league']
                p_name   = p['name']
                p_slug   = p['school_slug']
                p_school = p['school']
                p_grade  = p.get('grade', '')
                p_sailor_slug = sailor_slug(p_name, p_slug)
                if p_league == 'ICSA':
                    href = f'../../sailors/{esc(p_sailor_slug)}/'
                else:
                    href = f'../../../issa/sailors/{esc(p_sailor_slug)}/'
                p_logo = (f'../../icsa_logos/{esc(p_slug)}.png' if p_league == 'ICSA'
                          else f'../../issa_logos/{esc(p_slug)}.png')
                peer_rows += (
                    f'      <tr>\n'
                    f'        <td><span class="badge badge-{"icsa" if p_league=="ICSA" else "issa"}">'
                    f'{esc(p_league)}</span></td>\n'
                    f'        <td class="schoolname">'
                    f'<a href="{href}">{esc(p_name)}</a></td>\n'
                    f'        <td style="text-align:left">'
                    f'<img src="{p_logo}" alt="{esc(p_school)}" height="20" style="vertical-align:middle;margin-right:0.3em" onerror="this.style.display=\'none\'">'
                    f'{esc(p_school)}</td>\n'
                    f'        <td>{esc(p_grade)}</td>\n'
                    f'      </tr>\n'
                )
            if peer_rows:
                related_html = f'''
  <div class="port">
    <h3 style="margin-bottom:0.5em">Linked Profiles</h3>
    <table class="results">
      <thead><tr>
        <th>League</th>
        <th style="text-align:left">Name</th>
        <th style="text-align:left">School</th>
        <th>Year</th>
      </tr></thead>
      <tbody>
{peer_rows}      </tbody>
    </table>
  </div>'''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(name)} | ICSA Real-Time Regatta Results</title>
  <link rel="stylesheet" href="../../styles.css?v={ASSET_V}" media="screen,print">
</head>
<body class="sailor">
{nav('../../index.html', '../../schools/index.html', '../../sailors/index.html',
     '../../img/icsa-logo-real.png', '../../sailors/index.html', 'Sailors', logo_href='../../index.html', fantasy='../../fantasy/index.html')}
<div id="page-content">

  <div id="content-header">
    <h1>
      <span id="left-fill"></span>
      <span>{esc(name)}</span>
      <span id="right-fill"></span>
    </h1>
    <ul id="page-info">
      {womens_sailor_badge}
      {away_badge}
      <li>
        <span class="page-info-key">School</span>
        <span class="page-info-value">
          <img src="{logo}" alt="{esc(school_name)}" style="height:24px;width:auto;vertical-align:middle;margin-right:0.3em" onerror="this.style.display=\'none\'">
          {school_info}
        </span>
      </li>
      <li>
        <span class="page-info-key">Regattas</span>
        <span class="page-info-value">{total_regattas}</span>
      </li>
      <li>
        <span class="page-info-key">Races</span>
        <span class="page-info-value">{total_races}</span>
      </li>
    </ul>
  </div>
{rating_chart_html}{crew_html}
  <div class="port">
    <h3>Regatta results</h3>
    <table class="results" id="results-table">
      <thead>
        <tr>
          <th style="text-align:left">Regatta</th>
          <th>Role</th>
          <th>Div</th>
          <th style="text-align:right">Races</th>
          <th style="text-align:right">Avg</th>
          <th style="text-align:right" title="Skippers: performance rating at the event. Crew: rating push at the event, how much better or worse the boats did with this crew than with their usual crews.">Rating</th>
        </tr>
      </thead>
      <tbody>
{rows_html}      </tbody>
    </table>
  </div>
{related_html}
</div>
{script_html}
</body>
</html>'''


def build_sailors_index_page(sailors_data):
    """Build the sailors index page with filter dropdowns, matching the ISSA sailors page."""
    import json as _j

    rows = []
    schools = {}
    for (name, school_slug), info in sailors_data.items():
        rows.append([name, school_slug, info.get('grade', ''), sailor_slug(name, school_slug)])
        schools[school_slug] = info['school']

    rows.sort(key=lambda r: r[0].lower())
    total = len(rows)
    schools_json = _j.dumps(schools, ensure_ascii=False)
    sailors_json  = _j.dumps(rows,   ensure_ascii=False, separators=(',', ':'))

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>All Sailors | ICSA Real-Time Regatta Results</title>
  <link rel="stylesheet" href="../styles.css" media="screen,print">
  <style>
    .sailor-filters{{display:flex;flex-wrap:wrap;gap:0.8em;padding:1em;background:#f5f5f5;border-radius:4px;margin:0.8em 0;}}
    .sailor-filters label{{display:flex;flex-direction:column;gap:0.3em;font-size:0.9em;font-weight:bold;color:#333;}}
    .sailor-filters select,.sailor-filters input{{font-size:1em;padding:0.4em 0.6em;border:1px solid #bbb;border-radius:4px;min-width:200px;font-family:inherit;}}
    .sailor-count{{color:#666;font-size:0.9em;margin:0.4em 0;}}
  </style>
</head>
<body>
{nav('../index.html', '../schools/index.html', '../sailors/index.html',
     '../img/icsa-logo-real.png', 'index.html', 'Sailors', logo_href='../index.html')}
<div id="page-content">

  <div id="content-header">
    <h1>
      <span id="left-fill"></span>
      <span>All Sailors</span>
      <span id="right-fill"></span>
    </h1>
    <ul id="page-info">
      <li><span class="page-info-key">Total</span>
          <span class="page-info-value">{total:,}</span></li>
    </ul>
  </div>

  <div class="port">
    <div class="sailor-filters">
      <label>School
        <select id="f-school" onchange="filterSailors()">
          <option value="">All Schools</option>
        </select>
      </label>
      <label>Year
        <select id="f-year" onchange="filterSailors()">
          <option value="">All Years</option>
        </select>
      </label>
      <label>Name
        <input id="f-name" type="text" placeholder="Search name…" oninput="filterSailors()">
      </label>
    </div>
    <p class="sailor-count" id="sailor-count"></p>
    <table class="results">
      <thead><tr>
        <th style="text-align:left">Name</th>
        <th style="text-align:left">School</th>
        <th style="text-align:center">Year</th>
        <th style="text-align:center">League</th>
      </tr></thead>
      <tbody id="sailor-tbody"></tbody>
    </table>
  </div>

</div>
<script>
const SCHOOLS = {schools_json};
const SAILORS = {sailors_json};
const MAX = 400;

function rebuildSchools() {{
  const sel = document.getElementById('f-school');
  const cur = sel.value;
  const seen = new Set();
  SAILORS.forEach(s => seen.add(s[1]));
  const opts = [...seen].sort((a,b)=>(SCHOOLS[a]||a).localeCompare(SCHOOLS[b]||b));
  sel.innerHTML = '<option value="">All Schools</option>' +
    opts.map(sl=>`<option value="${{sl}}"${{sl===cur?' selected':''}}>${{(SCHOOLS[sl]||sl).replace(/&/g,'&amp;').replace(/</g,'&lt;')}}</option>`).join('');
}}

function rebuildYears(school) {{
  const sel = document.getElementById('f-year');
  const cur = sel.value;
  const seen = new Set();
  SAILORS.forEach(s => {{ if ((!school||s[1]===school) && s[2]) seen.add(s[2]); }});
  const opts = [...seen].sort();
  sel.innerHTML = '<option value="">All Years</option>' +
    opts.map(y=>`<option value="${{y}}"${{y===cur?' selected':''}}>${{y}}</option>`).join('');
}}

function filterSailors() {{
  const school = document.getElementById('f-school').value;
  const year   = document.getElementById('f-year').value;
  const name   = document.getElementById('f-name').value.toLowerCase().trim();
  rebuildSchools();
  rebuildYears(school);
  const matches = SAILORS.filter(s =>
    (!school || s[1]===school) &&
    (!year   || s[2]===year)   &&
    (!name   || s[0].toLowerCase().includes(name))
  );
  const shown = matches.slice(0, MAX);
  const tbody = document.getElementById('sailor-tbody');
  tbody.innerHTML = shown.map((s,i) => {{
    const logo  = '../icsa_logos/'+s[1]+'.png';
    const sname = (SCHOOLS[s[1]]||s[1]).replace(/&/g,'&amp;').replace(/</g,'&lt;');
    const nm    = s[0].replace(/&/g,'&amp;').replace(/</g,'&lt;');
    return `<tr class="row${{i%2}}">
      <td class="schoolname"><a href="${{s[3]}}/">${{nm}}</a></td>
      <td style="text-align:left"><img src="${{logo}}" height="18" style="vertical-align:middle;margin-right:0.3em" onerror="this.style.display='none'">${{sname}}</td>
      <td style="text-align:center">${{s[2]||''}}</td>
      <td style="text-align:center"><span class="badge badge-icsa">ICSA</span></td>
    </tr>`;
  }}).join('');
  const countEl = document.getElementById('sailor-count');
  if (!school && !year && !name) {{
    countEl.textContent = 'Use the filters above to search {total:,} sailors.';
  }} else {{
    countEl.textContent = matches.length > MAX
      ? `Showing ${{MAX}} of ${{matches.length.toLocaleString()}} matches`
      : `${{matches.length.toLocaleString()}} sailor${{matches.length===1?'':'s'}}`;
  }}
}}

rebuildSchools(); rebuildYears(''); filterSailors();
</script>
</body>
</html>'''


# ── copy static assets ────────────────────────────────────────────────────────

def copy_assets():
    website_root = os.path.join(BASE, 'website')
    os.makedirs(website_root, exist_ok=True)

    for name in ('styles.css',):
        src = os.path.join(BASE, name)
        if not os.path.exists(src):
            src = os.path.join(website_root, name)
        if os.path.exists(src):
            dst1 = os.path.join(SITE, name)
            dst2 = os.path.join(website_root, name)
            if os.path.abspath(src) != os.path.abspath(dst1):
                shutil.copy2(src, dst1)
            if os.path.abspath(src) != os.path.abspath(dst2):
                shutil.copy2(src, dst2)

    # chart.js: ensure icsa/chart.js is a symlink → ../chart.js (never copy, so edits always take effect)
    chart_link = os.path.join(SITE, 'chart.js')
    if not os.path.islink(chart_link):
        if os.path.exists(chart_link):
            os.remove(chart_link)
        os.symlink('../chart.js', chart_link)

    for folder in ('img', 'icsa_logos'):
        src = os.path.join(BASE, folder)
        if os.path.exists(src):
            for dst in (os.path.join(SITE, folder), os.path.join(website_root, folder)):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)

    print('  Copied assets: styles.css, img/, icsa_logos/ (to website/icsa/ and website/)')



# ── main ──────────────────────────────────────────────────────────────────────

from clean_links import clean_bytes as _clean_links


def _write_alias_redirects(s):
    """Schedule rows matched to a results-site page by date and host keep their old placeholder path as an alias;
    that path becomes a redirect to the real page (only when the real page exists)."""
    try:
        import json as _jj
        with open(os.path.join(CSV_DIR, 'neisa_schedule_meta.json'), encoding='utf-8') as _f:
            _alias_rows = [(m.get('temp_url', ''), m) for m in (_jj.load(_f) or {}).values()]
    except (OSError, ValueError):
        return 0
    n = 0
    for _tu, _tm in _alias_rows:
        _al = (_tm.get('alias_url') or '').rstrip('/')
        if not (_al and _al.startswith(f'/{s}/') and _al != _tu.rstrip('/')):
            continue
        _al_slug, _real_slug2 = _al.split('/')[-1], _tu.strip('/').split('/')[-1]
        if not os.path.exists(os.path.join(SITE, s, _real_slug2, 'index.html')):
            continue
        write(os.path.join(SITE, s, _al_slug, 'index.html'),
              f'<!DOCTYPE html>\n<html lang="en"><head><meta charset="UTF-8"><title>{esc(_tm.get("name", ""))}</title>'
              f'<meta http-equiv="refresh" content="0; url=../{esc(_real_slug2)}/"><link rel="canonical" href="../{esc(_real_slug2)}/">'
              f'</head><body><p>This regatta is at <a href="../{esc(_real_slug2)}/">{esc(_tm.get("name", ""))}</a>.</p></body></html>')
        n += 1
    return n


def write(path, content):
    """Write a generated file only when its content changed.  Unchanged pages keep their
    modification time, so rsync skips them and a deploy uploads only what changed."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    data = content.encode('utf-8') if isinstance(content, str) else content
    if path.endswith('.html'):
        try:
            import auto_links
            data = auto_links.link_html(data.decode('utf-8'), path).encode('utf-8')   # plain-text school / season / conference names become links
            import notes_links
            data = notes_links.inject(data.decode('utf-8'), path).encode('utf-8')     # "From the notes" box: blog posts relevant to this page
        except UnicodeDecodeError:
            pass
        # Folder links end in "/" instead of "/index.html".  Done before the comparison
        # below, so a page that has not changed still keeps its modification time.
        data = _clean_links(data)
        import analytics
        data = analytics.inject(data.decode('utf-8'), path).encode('utf-8')
    try:
        with open(path, 'rb') as f:
            if f.read() == data:
                return
    except OSError:
        pass
    with open(path, 'wb') as f:
        f.write(data)


def _recent_participant_slugs(days=14):
    """Return (school_slugs, sailor_keys) for regattas that ended within `days` days."""
    import csv as _csv, datetime as _dtt
    cutoff = _dtt.datetime.utcnow() - _dtt.timedelta(days=days)
    recent_urls = set()
    csv_path = os.path.join(CSV_DIR, 'icsa_regattas.csv')
    if os.path.exists(csv_path):
        with open(csv_path, newline='', encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                end = row.get('end_date', '').strip()
                if not end:
                    continue
                try:
                    d = _dtt.datetime.strptime(end, '%m/%d/%Y')
                except ValueError:
                    continue
                if d >= cutoff:
                    url = row.get('url', '').strip()
                    if url:
                        # Normalise to path-only (strip https://scores.collegesailing.org etc.)
                        if url.startswith('http'):
                            from urllib.parse import urlparse as _up
                            url = _up(url).path
                        recent_urls.add(url)
    if not recent_urls:
        return set(), set()

    school_slugs = set()
    sailor_keys  = set()  # (name, school_slug)
    with db() as con:
        placeholders = ','.join('?' * len(recent_urls))
        url_list = list(recent_urls)
        for table in ('fleet_results', 'tr_standings', 'keelboat_results'):
            try:
                rows = con.execute(
                    f'SELECT DISTINCT school_slug FROM {table} WHERE regatta_url IN ({placeholders})',
                    url_list
                ).fetchall()
                for r in rows:
                    if r[0]:
                        school_slugs.add(r[0])
            except Exception:
                pass
        # Sailors (fleet_results only — skipper/crew)
        try:
            rows = con.execute(
                f'SELECT DISTINCT skipper, school_slug FROM fleet_results WHERE regatta_url IN ({placeholders}) AND skipper != ""',
                url_list
            ).fetchall()
            for r in rows:
                sailor_keys.add((r[0], r[1]))
        except Exception:
            pass
        try:
            rows = con.execute(
                f'SELECT DISTINCT crew, school_slug FROM fleet_results WHERE regatta_url IN ({placeholders}) AND crew != ""',
                url_list
            ).fetchall()
            for r in rows:
                sailor_keys.add((r[0], r[1]))
        except Exception:
            pass
    print(f'  Recent regattas ({days}d): {len(recent_urls)} regattas → {len(school_slugs)} schools, {len(sailor_keys)} sailors')
    return school_slugs, sailor_keys


def main():
    args = sys.argv[1:]
    schools_only = '--schools' in args
    sailors_only = '--sailors' in args
    # --quick: skip schools, conferences, sailors — only rebuild specified seasons + homepage
    quick        = '--quick' in args
    # --recent: like --quick but also rebuilds school+sailor pages for last-2-weeks participants
    recent       = '--recent' in args
    # --analysis: also build the performance-rating analysis sub-page per fleet regatta (slow)
    analysis_pages = '--analysis' in args
    args = [a for a in args if not a.startswith('--')]
    target_seasons = args if args else None

    print(f'Building site → {SITE}')
    # One shared file carries the build time, so pages don't change just because they were rebuilt
    write(os.path.join(BASE, 'website', 'last_updated.js'),
          "var e=document.getElementById('last_updated');if(e)e.textContent='Updated: " + _BUILD_TIMESTAMP + "';\n")
    os.makedirs(SITE, exist_ok=True)

    _ev = _away_event()
    if _ev.get('slug') and not sailors_only:
        write(os.path.join(SITE, _ev['season'], _ev['slug'], 'index.html'), build_away_regatta_page())
        print(f"  Built: icsa/{_ev['season']}/{_ev['slug']}/index.html")

    # Assets
    if not schools_only and not sailors_only:
        copy_assets()

    # Discover seasons
    seasons = all_seasons()
    print(f'  Seasons in DB: {len(seasons)}')

    to_generate = [s for s in seasons if target_seasons is None or s in target_seasons]

    if sailors_only:
        pass  # skip to sailor section below
    elif not schools_only:
        # Homepage
        write(os.path.join(SITE, 'index.html'), build_homepage(seasons))
        print('  Built: icsa/index.html')

        # Recompute percentiles from latest ratings before building pages
        _recompute_percentiles()

        # Rankings
        rankings_html = build_rankings_page()
        if rankings_html:
            write(os.path.join(SITE, 'rankings', 'index.html'), rankings_html)
            print('  Built: icsa/rankings/index.html')
            print('  Built: icsa/rankings/data.json')

        # Seasons list
        write(os.path.join(SITE, 'seasons', 'index.html'), build_seasons_page(seasons))
        print('  Built: icsa/seasons/index.html')

        # Model evaluation page (only if evaluate_models.py has been run)
        methodology_html = build_methodology_page() if _methodology_ready() else None
        if methodology_html:
            write(os.path.join(SITE, 'methodology', 'index.html'), methodology_html)
            print('  Built: icsa/methodology/index.html')
        champ_points_html = build_championship_points_page()
        if champ_points_html:
            write(os.path.join(SITE, 'methodology', 'championships', 'index.html'), champ_points_html)
            print('  Built: icsa/methodology/championships/index.html')
        team_racing_html = build_team_racing_page()
        if team_racing_html:
            write(os.path.join(SITE, 'team-racing', 'index.html'), team_racing_html)
            print('  Built: icsa/team-racing/index.html')

    if not sailors_only and not quick and not recent:
        # Schools index
        write(os.path.join(SITE, 'schools', 'index.html'), build_schools_page())
        print('  Built: icsa/schools/index.html')

        # Individual school pages — main + per-season
        print('  Precomputing school regatta data...')
        all_school_data = _precompute_school_data()
        with db() as con:
            all_schools = con.execute(
                "SELECT school, slug, city, state, conference FROM schools WHERE conference != 'GUEST' OR conference IS NULL"
            ).fetchall()
        school_count = 0
        season_page_count = 0
        for school_name, slug, city, state, conference in all_schools:
            school_data = all_school_data.get(slug, {})
            active = sorted(school_data.keys(), key=season_sort_key, reverse=True)
            if not active:
                continue
            html = build_school_page(slug, school_name, city or '', state or '', conference or '',
                                     active, school_data)
            if html:
                write(os.path.join(SITE, 'schools', slug, 'index.html'), html)
                school_count += 1
            for s in active:
                html = build_school_season_page(slug, school_name, city or '', state or '',
                                                conference or '', s, active, school_data)
                write(os.path.join(SITE, 'schools', slug, s, 'index.html'), html)
                season_page_count += 1
        print(f'  Built: {school_count} school pages + {season_page_count} season pages in website/schools/')

    if not sailors_only and recent:
        recent_school_slugs, _recent_sailor_keys = _recent_participant_slugs()
        if recent_school_slugs:
            print('  Precomputing school regatta data...')
            all_school_data = _precompute_school_data()
            with db() as con:
                all_schools = con.execute(
                    "SELECT school, slug, city, state, conference FROM schools WHERE conference != 'GUEST' OR conference IS NULL"
                ).fetchall()
            school_count = 0
            season_page_count = 0
            for school_name, slug, city, state, conference in all_schools:
                if slug not in recent_school_slugs:
                    continue
                school_data = all_school_data.get(slug, {})
                active = sorted(school_data.keys(), key=season_sort_key, reverse=True)
                if not active:
                    continue
                html = build_school_page(slug, school_name, city or '', state or '', conference or '',
                                         active, school_data)
                if html:
                    write(os.path.join(SITE, 'schools', slug, 'index.html'), html)
                    school_count += 1
                for s in active:
                    html = build_school_season_page(slug, school_name, city or '', state or '',
                                                    conference or '', s, active, school_data)
                    write(os.path.join(SITE, 'schools', slug, s, 'index.html'), html)
                    season_page_count += 1
            print(f'  Built: {school_count} school pages + {season_page_count} season pages (recent only)')


    if schools_only:
        return

    if not sailors_only and not quick and not recent:
        # Conference pages
        with db() as con:
            conferences = [r[0] for r in con.execute(
                "SELECT DISTINCT conference FROM schools WHERE conference IS NOT NULL AND conference != 'GUEST' ORDER BY conference"
            )]
        for conf in conferences:
            conf_slug = conf.lower()
            write(os.path.join(SITE, 'conferences', conf_slug, 'index.html'),
                  build_conference_page(conf, seasons))
            print(f'  Built: icsa/conferences/{conf_slug}/index.html')
            for s in seasons:
                write(os.path.join(SITE, 'conferences', conf_slug, s, 'index.html'),
                      build_conference_season_page(conf, s, seasons))
            print(f'    + {len(seasons)} season pages')

    if not sailors_only:
        # Season pages + regatta pages
        total_regattas = 0
        for s in to_generate:
            write(os.path.join(SITE, s, 'index.html'), build_season_page(s, seasons))
            print(f'  Built: icsa/{s}/index.html')

            slabel = season_label(s)

            with db() as con:
                fleet = {url: name for name, url in con.execute(
                    'SELECT DISTINCT regatta_name, regatta_url FROM fleet_results WHERE season=?', (s,)
                ).fetchall()}
                tr = {url: name for name, url in con.execute(
                    'SELECT DISTINCT regatta_name, regatta_url FROM tr_standings WHERE season=?', (s,)
                ).fetchall()}
                keelboat = {url: name for name, url in con.execute(
                    'SELECT DISTINCT regatta_name, regatta_url FROM keelboat_results WHERE season=?', (s,)
                ).fetchall()}

            all_reg = {**fleet}
            for url, name in tr.items():
                if url not in all_reg:
                    all_reg[url] = name
            for url, name in keelboat.items():
                if url not in all_reg:
                    all_reg[url] = name

            for regatta_url, regatta_name in all_reg.items():
                slug = regatta_url.strip('/').split('/')[-1]
                if not slug:
                    continue
                is_tr = regatta_url in tr and regatta_url not in fleet
                is_kb = regatta_url in keelboat and regatta_url not in fleet and regatta_url not in tr
                if is_tr:
                    html = build_tr_regatta_page(s, regatta_url, regatta_name, slabel)
                elif is_kb:
                    html = build_keelboat_regatta_page(s, regatta_url, regatta_name, slabel)
                else:
                    _apath = os.path.join(SITE, s, slug, 'analysis', 'index.html')
                    _unofficial = _get_regatta_meta().get((s, regatta_name.strip().lower()), {}) \
                        .get('status', '').strip().lower() != 'official'
                    _stale = os.path.exists(_apath) and 'ratings-provisional' in open(_apath, encoding='utf-8').read(4000)
                    # Unfinished events always get an analysis page; one that was provisional is redone
                    # once the event turns Official, so its asterisks do not outlive the event; the current season also gets
                    # any page it is missing.
                    if analysis_pages or _unofficial or _stale or (s == seasons[0] and not os.path.exists(_apath)):
                        a_html = build_regatta_analysis_page(s, regatta_url, regatta_name, slabel)
                        if a_html:
                            write(os.path.join(SITE, s, slug, 'analysis', 'index.html'), a_html)
                    html = build_fleet_regatta_page(s, regatta_url, regatta_name, slabel)
                if html:
                    write(os.path.join(SITE, s, slug, 'index.html'), html)
                    total_regattas += 1

            # Scheduled regattas that have not sailed.  They have no results, so the loop
            # above never reaches them, but we know the entry list and the expected
            # line-ups — enough for a real page, and it means the home page can link every
            # upcoming regatta instead of showing half of them as plain text.  Anything
            # already written above wins: a published regatta must keep its real results.
            # A schedule row's placeholder URL can differ from the slug Techscore later gave the regatta (/f26/regis-trophy
            # vs /f26/regis).  When the real regatta exists the placeholder becomes a redirect to it, not a second, empty page.
            _write_alias_redirects(s)
            _real_by_name = {}
            for _wk, _regs in season_regattas_by_week(s):
                for _r in _regs:
                    if _r['url']:
                        _real_by_name[_r['name'].strip().lower()] = _r['url']
            for temp_url, tmeta in _temp_regatta_meta().items():
                if not temp_url.startswith(f'/{s}/'):
                    continue
                t_slug = temp_url.strip('/').split('/')[-1]
                if not t_slug or temp_url in all_reg:
                    continue
                _ru = _real_by_name.get(tmeta.get('name', '').strip().lower())
                if _ru and _ru.rstrip('/') != temp_url.rstrip('/'):
                    _real_slug = _ru.strip('/').split('/')[-1]
                    write(os.path.join(SITE, s, t_slug, 'index.html'),
                          f'<!DOCTYPE html>\n<html lang="en"><head><meta charset="UTF-8"><title>{esc(tmeta.get("name", ""))}</title>'
                          f'<meta http-equiv="refresh" content="0; url=../{esc(_real_slug)}/"><link rel="canonical" href="../{esc(_real_slug)}/">'
                          f'</head><body><p>This regatta is at <a href="../{esc(_real_slug)}/">{esc(tmeta.get("name", ""))}</a>.</p></body></html>')
                    continue
                if temp_url not in _upcoming_entries_by_url():
                    continue                      # no entries yet: nothing to put on a page
                t_html = build_scheduled_regatta_page(s, temp_url, tmeta.get('name', ''), slabel)
                if t_html:
                    write(os.path.join(SITE, s, t_slug, 'index.html'), t_html)
                    total_regattas += 1

        print(f'\nDone. {3 + len(to_generate)} season pages + {total_regattas} regatta pages written to icsa/')

    if quick:
        return

    if sailors_only or recent:
        # Sailor pages
        print('\nCollecting sailor data...')
        sailors_data = _collect_icsa_sailors()
        sailors_data = {
            k: v for k, v in sailors_data.items()
            if k[0] and k[0].strip() and 'Unreported' not in k[0]
        }
        print(f'  Found {len(sailors_data)} unique sailors')

        clusters = _load_sailor_clusters()
        print(f'  Loaded {len(clusters)} cluster entries')

        if not recent:
            write(os.path.join(SITE, 'sailors', 'index.html'), build_sailors_index_page(sailors_data))
            print('  Built: icsa/sailors/index.html')

        print('  Loading rating data...')
        rating_lookups = _load_rating_lookups()

        if recent:
            _, recent_sailor_keys = _recent_participant_slugs()
            filter_keys = recent_sailor_keys
            print(f'  Filtering to {len(filter_keys)} recent sailors')
        else:
            filter_keys = None

        sailor_count = 0
        for (name, school_slug), info in sailors_data.items():
            if filter_keys is not None and (name, school_slug) not in filter_keys:
                continue
            slug = sailor_slug(name, school_slug)
            rating_points = _get_rating_points(name, info['school'], info['appearances'], rating_lookups)
            html = build_sailor_page(name, school_slug, info['school'], info['grade'], info['appearances'], clusters=clusters, rating_points=rating_points)
            write(os.path.join(SITE, 'sailors', slug, 'index.html'), html)
            sailor_count += 1
            if sailor_count % 500 == 0:
                print(f'  ... {sailor_count} sailor pages written')

        print(f'  Built: {sailor_count} sailor pages in icsa/sailors/')
        return

    # Sailor pages (full rebuild — no --recent, no --sailors)
    print('\nCollecting sailor data...')
    sailors_data = _collect_icsa_sailors()
    # Filter out empty/Unreported names
    sailors_data = {
        k: v for k, v in sailors_data.items()
        if k[0] and k[0].strip() and 'Unreported' not in k[0]
    }
    print(f'  Found {len(sailors_data)} unique sailors')

    clusters = _load_sailor_clusters()
    print(f'  Loaded {len(clusters)} cluster entries')

    write(os.path.join(SITE, 'sailors', 'index.html'), build_sailors_index_page(sailors_data))
    print('  Built: icsa/sailors/index.html')

    print('  Loading rating data...')
    rating_lookups = _load_rating_lookups()
    sailor_count = 0
    for (name, school_slug), info in sailors_data.items():
        slug = sailor_slug(name, school_slug)
        rating_points = _get_rating_points(name, info['school'], info['appearances'], rating_lookups)
        html = build_sailor_page(name, school_slug, info['school'], info['grade'], info['appearances'], clusters=clusters, rating_points=rating_points)
        write(os.path.join(SITE, 'sailors', slug, 'index.html'), html)
        sailor_count += 1
        if sailor_count % 1000 == 0:
            print(f'  ... {sailor_count} sailor pages written')

    print(f'  Built: {sailor_count} sailor pages in icsa/sailors/')


if __name__ == '__main__':
    main()
