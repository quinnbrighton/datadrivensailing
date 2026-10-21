#!/usr/bin/env python3
"""Win probability for a team race match from the fitted model (fit.py outputs), given both lineups.

    from analysis.team_race.predict import TeamRacePredictor
    tr = TeamRacePredictor('ICSA')
    p = tr.prob(side_a, side_b, host_slug=None)          # side = dict(school='yale', boats=[(skipper_id, crew_id), ...3])
    s = tr.side_strength(side)                           # what the lineup is worth on the model's logit scale

Sailor IDs are the main model's (C#### clusters or SOLO:league:name:school:'yy).  Unknown skippers get the unrated level b0 and no
offset; unknown crews get no offset.  Experience is taken from the ratings file (matches sailed so far).

    python3 analysis/team_race/predict.py yale "C1,C2;C3,C4;C5,C6" harvard "C7,C8;C9,C10;C11,C12" [host_slug]
"""
from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


class TeamRacePredictor:
    def __init__(self, league: str = 'ICSA'):
        self.league = league
        suffix = '' if league == 'ICSA' else '_issa'
        M = json.load(open(HERE / 'model_teamrace.json'))
        self.m = M[league]
        self.cfg = M['config']
        self.sailors = {}
        with open(HERE / f'ratings_teamrace{suffix}.csv', newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                self.sailors[r['sailor_id']] = r
        self.schools = {}
        with open(HERE / f'school_effects_teamrace{suffix}.csv', newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                self.schools[r['school_slug']] = float(r['offset'])

    def boat_strength(self, skipper: str, crew: str | None, main_rating: float | None = None) -> float:
        s = self.sailors.get(skipper)
        mr = main_rating if main_rating is not None else (float(s['main_rating']) if s and s['main_rating'] else None)
        v = self.m['k'] * mr if mr is not None else self.m['b0']
        if s:
            v += float(s['offset'] or 0)
            if self.cfg['use_exp']:
                v += self.m['g_exp'][0] * math.log1p(int(s['matches_skipper']) + int(s['matches_crew']))
        c = self.sailors.get(crew or '')
        if c:
            v += float(c['crew_offset'] or 0)
            if self.cfg['use_exp']:
                v += self.m['g_exp'][1] * math.log1p(int(c['matches_skipper']) + int(c['matches_crew']))
        return v

    def side_strength(self, side: dict, host_slug: str | None = None) -> float:
        v = sum(self.boat_strength(sk, cr, (side.get('main') or {}).get(sk)) for sk, cr in side['boats'])
        v += self.schools.get(side.get('school', ''), 0.0)
        if self.cfg['use_home'] and host_slug and side.get('school') == host_slug:
            v += self.m['g_home']
        return v

    def prob(self, a: dict, b: dict, host_slug: str | None = None) -> float:
        d = self.side_strength(a, host_slug) - self.side_strength(b, host_slug)
        return 1 / (1 + math.exp(-self.m.get('temperature', 1.0) * d))


def _parse(spec: str) -> list:
    return [tuple((b.split(',') + [''])[:2]) for b in spec.split(';')]


if __name__ == '__main__':
    sa, la, sb, lb = sys.argv[1:5]
    host = sys.argv[5] if len(sys.argv) > 5 else None
    tr = TeamRacePredictor()
    a = dict(school=sa, boats=_parse(la)); b = dict(school=sb, boats=_parse(lb))
    print(f'{sa} {tr.side_strength(a, host):+.3f} vs {sb} {tr.side_strength(b, host):+.3f}: P({sa} wins) = {tr.prob(a, b, host):.3f}')
