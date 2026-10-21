#!/usr/bin/env python3
"""
build_db.py — Build db/icsa.db from the local CSV files.

Imports:
  data/icsa_results.csv          → fleet_results
  data/icsa_tr_standings.csv     → tr_standings
  data/icsa_keelboat_results.csv → keelboat_results
  data/icsa_rotations.csv        → rotations
  data/icsa_schools.csv          → schools

Usage:
    python3 build_db.py
"""

import os, csv, sqlite3

BASE     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
DB_PATH  = os.path.join(BASE, 'db', 'icsa.db')

os.makedirs(os.path.join(BASE, 'db'), exist_ok=True)


_MOJIBAKE_MARKER = '\u00e2\u0080'  # start of double-encoded UTF-8 sequence

def _fix_encoding(s):
    """Reverse double-encoded UTF-8 text (UTF-8 bytes misread as latin-1)."""
    if not isinstance(s, str) or _MOJIBAKE_MARKER not in s:
        return s
    try:
        fixed = s.encode('latin-1').decode('utf-8')
        return fixed if fixed != s else s
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s

def load_csv(path):
    with open(path, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    return [{k: _fix_encoding(v) for k, v in row.items()} for row in rows]


def clean_name(s):
    """Strip trailing graduation-year apostrophe artifacts like \"Name '\"."""
    return s.rstrip(" '") if s else s


def _points(r):
    """Points as scraped, or None for rows scraped before they were recorded."""
    v = (r.get('points') or '').strip() if isinstance(r.get('points'), str) else r.get('points')
    return int(v) if v not in (None, '') else None


def fill_points(cur, table):
    """A finish scores its place.  A coded result (DNF, DSQ, BKD ...) scores what the results
    site awarded; rows scraped before that was recorded keep points NULL, and the page
    generators fall back to fleet size plus one for them."""
    cur.execute(f"UPDATE {table} SET points = place WHERE points IS NULL AND place < 900")


def build():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # ── fleet_results ──────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE fleet_results (
            id            INTEGER PRIMARY KEY,
            season        TEXT,
            regatta_name  TEXT,
            regatta_url   TEXT,
            boat          TEXT,
            division      TEXT,
            race_num      INTEGER,
            place         INTEGER,
            school        TEXT,
            school_slug   TEXT,
            team          TEXT,
            skipper       TEXT,
            skipper_grade TEXT,
            crew          TEXT,
            crew_grade    TEXT,
            keelboat      INTEGER DEFAULT 0,
            code          TEXT DEFAULT '',
            points        INTEGER,
            div_penalty   TEXT DEFAULT '',
            div_penalty_points INTEGER
        )
    """)
    cur.execute("CREATE INDEX idx_fr_season ON fleet_results(season)")
    cur.execute("CREATE INDEX idx_fr_regatta ON fleet_results(regatta_url)")

    rows = load_csv(os.path.join(DATA_DIR, 'icsa_results.csv'))
    cur.executemany("""
        INSERT INTO fleet_results
            (id,season,regatta_name,regatta_url,boat,division,race_num,place,
             school,school_slug,team,skipper,skipper_grade,crew,crew_grade,keelboat,code,points,
             div_penalty,div_penalty_points)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [
        (r['id'], r['season'], r['regatta_name'], r['regatta_url'],
         r['boat'], r['division'], r['race_num'], r['place'],
         r['school'], r['school_slug'], r['team'],
         clean_name(r.get('skipper','')), r.get('skipper_grade',''),
         clean_name(r.get('crew','')), r.get('crew_grade',''),
         int(r.get('keelboat', 0) or 0), r.get('code', '') or '', _points(r),
         r.get('div_penalty', '') or '', int(r['div_penalty_points']) if r.get('div_penalty_points') else None)
        for r in rows
    ])
    fill_points(cur, 'fleet_results')
    print(f'  fleet_results:  {len(rows):,} rows')

    # ── tr_standings ───────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE tr_standings (
            id          INTEGER PRIMARY KEY,
            season      TEXT,
            regatta_name TEXT,
            regatta_url TEXT,
            boat        TEXT,
            place       INTEGER,
            school      TEXT,
            school_slug TEXT,
            team        TEXT,
            wins        INTEGER,
            losses      INTEGER,
            win_pct     REAL
        )
    """)
    cur.execute("CREATE INDEX idx_tr_season ON tr_standings(season)")
    cur.execute("CREATE INDEX idx_tr_regatta ON tr_standings(regatta_url)")

    rows = load_csv(os.path.join(DATA_DIR, 'icsa_tr_standings.csv'))
    cur.executemany("""
        INSERT INTO tr_standings
            (id,season,regatta_name,regatta_url,boat,place,school,school_slug,
             team,wins,losses,win_pct)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
    """, [
        (r['id'], r['season'], r['regatta_name'], r['regatta_url'],
         r['boat'], r['place'], r['school'], r['school_slug'],
         r['team'], r['wins'], r['losses'], r['win_pct'])
        for r in rows
    ])
    print(f'  tr_standings:   {len(rows):,} rows')

    # ── tr_matches ─────────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE tr_matches (
            id               INTEGER PRIMARY KEY,
            season           TEXT,
            regatta_name     TEXT,
            regatta_url      TEXT,
            boat             TEXT,
            round_num        INTEGER,
            match_num        INTEGER,
            winner_school    TEXT,
            winner_slug      TEXT,
            winner_team      TEXT,
            winner_positions TEXT,
            loser_school     TEXT,
            loser_slug       TEXT,
            loser_team       TEXT,
            loser_positions  TEXT
        )
    """)
    cur.execute("CREATE INDEX idx_tm_regatta ON tr_matches(regatta_url)")

    rows = load_csv(os.path.join(DATA_DIR, 'icsa_tr_matches.csv'))
    cur.executemany("""
        INSERT INTO tr_matches
            (id,season,regatta_name,regatta_url,boat,round_num,match_num,
             winner_school,winner_slug,winner_team,winner_positions,
             loser_school,loser_slug,loser_team,loser_positions)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [
        (r['id'], r['season'], r['regatta_name'], r['regatta_url'], r['boat'],
         r['round_num'], r['match_num'],
         r['winner_school'], r['winner_slug'], r['winner_team'], r['winner_positions'],
         r['loser_school'], r['loser_slug'], r['loser_team'], r['loser_positions'])
        for r in rows
    ])
    print(f'  tr_matches:     {len(rows):,} rows')

    # ── tr_sailors ─────────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE tr_sailors (
            id           INTEGER PRIMARY KEY,
            regatta_url  TEXT,
            school_slug  TEXT,
            team         TEXT,
            skipper      TEXT,
            skipper_grade TEXT,
            crew         TEXT,
            crew_grade   TEXT
        )
    """)
    cur.execute("CREATE INDEX idx_ts_regatta ON tr_sailors(regatta_url)")

    rows = load_csv(os.path.join(DATA_DIR, 'icsa_tr_sailors.csv'))
    cur.executemany("""
        INSERT INTO tr_sailors(id,regatta_url,school_slug,team,skipper,skipper_grade,crew,crew_grade)
        VALUES(?,?,?,?,?,?,?,?)
    """, [
        (r['id'], r['regatta_url'], r['school_slug'], r['team'],
         clean_name(r['skipper']), r.get('skipper_grade',''), clean_name(r.get('crew','')), r.get('crew_grade',''))
        for r in rows
    ])
    print(f'  tr_sailors:     {len(rows):,} rows')

    # ── tr_match_sailors ───────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE tr_match_sailors (
            id           INTEGER PRIMARY KEY,
            regatta_url  TEXT,
            match_num    INTEGER,
            side         TEXT,
            boat         TEXT,
            school_slug  TEXT,
            team         TEXT,
            skipper      TEXT,
            skipper_grade TEXT,
            crew         TEXT,
            crew_grade   TEXT
        )
    """)
    cur.execute("CREATE INDEX idx_tms_regatta ON tr_match_sailors(regatta_url)")

    rows = load_csv(os.path.join(DATA_DIR, 'icsa_tr_match_sailors.csv'))
    cur.executemany("""
        INSERT INTO tr_match_sailors
            (id,regatta_url,match_num,side,boat,school_slug,team,
             skipper,skipper_grade,crew,crew_grade)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
    """, [
        (r['id'], r['regatta_url'], r['match_num'], r['side'], r['boat'],
         r['school_slug'], r['team'],
         clean_name(r['skipper']), r.get('skipper_grade',''), clean_name(r.get('crew','')), r.get('crew_grade',''))
        for r in rows
    ])
    print(f'  tr_match_sailors:{len(rows):,} rows')

    # ── regatta_kinds ──────────────────────────────────────────────────────────
    # What kind of event each regatta is (fleet, singlehanded, keelboat, team), as the
    # scraper read it from the pages.  See event_kinds.py.
    cur.execute("""
        CREATE TABLE regatta_kinds (
            regatta_url TEXT PRIMARY KEY,
            season      TEXT,
            kind        TEXT,
            crew_size   INTEGER,
            boat        TEXT,
            scoring     TEXT,
            reason      TEXT
        )
    """)
    kinds_csv = os.path.join(DATA_DIR, 'icsa_regatta_kinds.csv')
    rows = load_csv(kinds_csv) if os.path.exists(kinds_csv) else []
    cur.executemany("INSERT OR REPLACE INTO regatta_kinds VALUES(?,?,?,?,?,?,?)", [
        (r['regatta_url'], r['season'], r['kind'], int(r['crew_size'] or 0),
         r['boat'], r['scoring'], r['reason'])
        for r in rows
    ])
    print(f'  regatta_kinds:   {len(rows):,} rows')

    # ── keelboat_results ───────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE keelboat_results (
            id          INTEGER PRIMARY KEY,
            season      TEXT,
            regatta_name TEXT,
            regatta_url TEXT,
            boat        TEXT,
            division    TEXT,
            race_num    INTEGER,
            place       INTEGER,
            school      TEXT,
            school_slug TEXT,
            team        TEXT,
            code        TEXT DEFAULT '',
            points      INTEGER,
            div_penalty TEXT DEFAULT '',
            div_penalty_points INTEGER
        )
    """)
    cur.execute("CREATE INDEX idx_kb_season ON keelboat_results(season)")
    cur.execute("CREATE INDEX idx_kb_regatta ON keelboat_results(regatta_url)")

    rows = load_csv(os.path.join(DATA_DIR, 'icsa_keelboat_results.csv'))
    cur.executemany("""
        INSERT INTO keelboat_results
            (id,season,regatta_name,regatta_url,boat,division,race_num,place,
             school,school_slug,team,code,points,div_penalty,div_penalty_points)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [
        (r['id'], r['season'], r['regatta_name'], r['regatta_url'],
         r['boat'], r['division'], r['race_num'], r['place'],
         r['school'], r['school_slug'], r['team'], r.get('code', '') or '', _points(r),
         r.get('div_penalty', '') or '', int(r['div_penalty_points']) if r.get('div_penalty_points') else None)
        for r in rows
    ])
    fill_points(cur, 'keelboat_results')
    print(f'  keelboat_results:{len(rows):,} rows')

    # ── keelboat_sailors ───────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE keelboat_sailors (
            id          INTEGER PRIMARY KEY,
            regatta_url TEXT,
            school_slug TEXT,
            school      TEXT,
            team        TEXT,
            division    TEXT,
            sailor_name TEXT,
            grade       TEXT,
            races       TEXT,
            role        TEXT DEFAULT ''
        )
    """)
    cur.execute("CREATE INDEX idx_ks_regatta ON keelboat_sailors(regatta_url)")

    rows = load_csv(os.path.join(DATA_DIR, 'icsa_keelboat_sailors.csv'))
    cur.executemany("""
        INSERT INTO keelboat_sailors(id,regatta_url,school_slug,school,team,division,sailor_name,grade,races,role)
        VALUES(?,?,?,?,?,?,?,?,?,?)
    """, [
        (r['id'], r['regatta_url'], r['school_slug'], r['school'], r['team'],
         r['division'], r['sailor_name'], r.get('grade',''), r.get('races',''), r.get('role','') or '')
        for r in rows
    ])
    print(f'  keelboat_sailors:{len(rows):,} rows')

    # ── rotations ──────────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE rotations (
            id          INTEGER PRIMARY KEY,
            regatta_url TEXT,
            school_slug TEXT,
            team        TEXT,
            division    TEXT,
            race_num    INTEGER,
            sail_num    INTEGER
        )
    """)
    cur.execute("CREATE INDEX idx_rot_regatta ON rotations(regatta_url)")

    rows = load_csv(os.path.join(DATA_DIR, 'icsa_rotations.csv'))
    cur.executemany("""
        INSERT INTO rotations(id,regatta_url,school_slug,team,division,race_num,sail_num)
        VALUES(?,?,?,?,?,?,?)
    """, [
        (r['id'], r['regatta_url'], r['school_slug'], r['team'],
         r['division'], r['race_num'], r.get('sail_num') or None)
        for r in rows
    ])
    print(f'  rotations:      {len(rows):,} rows')

    # ── schools ────────────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE schools (
            school     TEXT,
            slug       TEXT PRIMARY KEY,
            city       TEXT,
            state      TEXT,
            png_name   TEXT,
            conference TEXT
        )
    """)

    rows = load_csv(os.path.join(DATA_DIR, 'icsa_schools.csv'))
    cur.executemany("""
        INSERT OR IGNORE INTO schools(school,slug,city,state,png_name,conference)
        VALUES(?,?,?,?,?,?)
    """, [
        (r['school'], r['slug'], r.get('city',''), r.get('state',''),
         r.get('png_name',''), r.get('conference',''))
        for r in rows
    ])
    print(f'  schools:        {len(rows):,} rows')

    con.commit()
    con.close()
    print(f'\nBuilt {DB_PATH}')


if __name__ == '__main__':
    print('Building db/icsa.db from CSVs...')
    build()
