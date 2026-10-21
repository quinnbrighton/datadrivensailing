#!/usr/bin/env python3
"""Team race matches with lineups, for the team race prediction model.

One row per match (3 boats a side) from db/<league>.db tr_matches + tr_match_sailors, with the main model's sailor IDs
(data/sailor_clusters.csv, as analysis/disciplines/build_races.py) so skipper and crew ratings can be joined.

Writes analysis/team_race/matches.csv:
  league, season, regatta_url, regatta_name, date, week, round_num, match_num, boat, womens,
  w_school, w_team, w_skippers, w_crews, w_pos, l_school, l_team, l_skippers, l_crews, l_pos
  (skippers / crews: three IDs joined by ';' in boat order A, B, C; a missing crew is an empty string; pos as Techscore gives it)
    python3 analysis/team_race/build_matches.py
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / 'analysis' / 'disciplines'))
from build_races import SailorIds, iso_week_num, parse_date  # noqa: E402

LEAGUES = (('ICSA', 'icsa'), ('ISSA', 'issa'))


def main() -> None:
    ids = SailorIds()
    out_rows = []
    stats = defaultdict(int)
    for league, site in LEAGUES:
        dates = {}
        with open(BASE / 'data' / f'{site}_regattas.csv', newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                d = parse_date(r.get('start_date', ''))
                if d:
                    dates[r['url'].rstrip('/').split('.org', 1)[-1]] = d
        con = sqlite3.connect(BASE / 'db' / f'{site}.db')
        try:
            womens = {u for (u,) in con.execute("SELECT DISTINCT regatta_url FROM tr_standings WHERE is_womens=1")}
        except sqlite3.OperationalError:
            womens = set()
        lineup = defaultdict(dict)   # (url, match, side) -> boat -> (skipper id, crew id)
        for url, mnum, side, boat, slug, skip, sg, crew, cg in con.execute(
                "SELECT regatta_url, match_num, side, boat, school_slug, skipper, skipper_grade, crew, crew_grade FROM tr_match_sailors"):
            sk = ids.get(league, skip.replace(';', ','), slug, sg or '') if (skip or '').strip() else ''
            cr = ids.get(league, crew.replace(';', ','), slug, cg or '') if (crew or '').strip() else ''
            lineup[(url, mnum, side)][boat] = (sk, cr)
        for row in con.execute("SELECT season, regatta_name, regatta_url, boat, round_num, match_num, winner_slug, winner_team, "
                               "winner_positions, loser_slug, loser_team, loser_positions FROM tr_matches ORDER BY regatta_url, match_num"):
            season, name, url, bcls, rnd, mnum, wslug, wteam, wpos, lslug, lteam, lpos = row
            stats[f'{league} matches'] += 1
            d = dates.get(url)
            if d is None:
                stats[f'{league} no date'] += 1
                continue
            sides = []
            for side in ('winner', 'loser'):
                lu = lineup.get((url, mnum, side), {})
                boats = sorted(lu)
                if len(boats) != 3 or not all(lu[b][0] for b in boats):
                    sides = None
                    break
                sides.append((';'.join(lu[b][0] for b in boats), ';'.join(lu[b][1] for b in boats)))
            if sides is None:
                stats[f'{league} incomplete lineup'] += 1
                continue
            stats[f'{league} kept'] += 1
            out_rows.append([league, season, url, name, d.isoformat(), iso_week_num(d), rnd or '', mnum, bcls or '', int(url in womens),
                             wslug, wteam, sides[0][0], sides[0][1], wpos or '', lslug, lteam, sides[1][0], sides[1][1], lpos or ''])
    out_rows.sort(key=lambda r: (r[4], r[2], r[7]))
    with open(HERE / 'matches.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['league', 'season', 'regatta_url', 'regatta_name', 'date', 'week', 'round_num', 'match_num', 'boat', 'womens',
                    'w_school', 'w_team', 'w_skippers', 'w_crews', 'w_pos', 'l_school', 'l_team', 'l_skippers', 'l_crews', 'l_pos'])
        w.writerows(out_rows)
    for k in sorted(stats):
        print(f'{k}: {stats[k]:,}')
    print(f'wrote {HERE / "matches.csv"} ({len(out_rows):,} matches)')


if __name__ == '__main__':
    main()
