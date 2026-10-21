#!/usr/bin/env python3
"""Fit the team race model on every match and write the team race ratings.

Per league (ICSA, ISSA), with today's main ratings (analysis/plackett_luce) as of each match's week:
  ratings_teamrace.csv        one row per sailor: main rating now, team-race offset and its se, matches / wins as skipper and crew,
                              tr_rating = k * main + offset for skippers (what one boat brings), offset alone for crews
  school_effects_teamrace.csv school offset (+ se), matches, win share
  model_teamrace.json         fitted k, b0, home edge, experience slopes, config, in-sample log-loss
The config is the forward-tested one from evaluate.py (see evaluate_grid*.log).

    python3 analysis/team_race/fit.py 2>&1 | tee analysis/team_race/fit.log
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BASE / 'analysis' / 'disciplines'))
import model as M  # noqa: E402
import fit as F  # noqa: E402

PL = BASE / 'analysis' / 'plackett_luce'
TEMPERATURE = {'ICSA': 1.00, 'ISSA': 0.78}   # logit scale that minimises held-out log-loss (evaluate_final*.log): ISSA is over-confident
CONFIG = M.Config('production', use_school_season=False, lam_sailor=30.0, lam_crew=30.0, use_home=True, use_exp=True)


def sailor_info() -> dict:
    """(sailor_id, league) -> (name, year, school, league); a cluster that spans high school and college has one entry per league,
    so the ISSA ratings name the high school and the ICSA ratings the college.  Also keyed by sailor_id alone (college first)."""
    out = {}
    with open(BASE / 'data' / 'sailor_clusters.csv', newline='', encoding='utf-8') as f:     # crews the main model never rated
        for r in csv.DictReader(f):
            g = r['grade'].strip()
            v = (r['name'].strip(), str(2000 + int(g[1:])) if g[1:].isdigit() else '', r['school'].strip() or r['school_slug'], r['league'])
            out.setdefault(('C' + r['cluster_id'], r['league']), v)
            out.setdefault('C' + r['cluster_id'], v)
    with open(PL / 'sailors.csv', newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            v = (r['name'], r['year'], r['school'], r['league'])
            out[(r['sailor_id'], r['league'])] = v
            if r['league'] == 'ICSA' or r['sailor_id'] not in out:
                out[r['sailor_id']] = v
    return out


def describe(sid: str, info: dict, league: str = '') -> tuple:
    if league and (sid, league) in info:
        return info[(sid, league)]
    if sid in info:
        return info[sid]
    if sid.startswith('SOLO:'):
        parts = sid.split(':')
        grade = parts[-1]
        return (parts[2].title(), str(2000 + int(grade[1:])) if grade[1:].isdigit() else '', parts[-2], parts[1])
    return (sid, '', '', '')


def main() -> None:
    rows = M.load_matches()
    main = F.MainRatings(PL)
    info = sailor_info()
    out_model = {}
    for league in ('ICSA', 'ISSA'):
        lr = [r for r in rows if r['league'] == league]
        if not lr:
            continue
        now_week = max(int(r['week']) for r in lr) + 1
        d = M.pack(lr, main.at, None, now_week, CONFIG)
        mdl = M.Model(CONFIG, d['idx'])
        loss = mdl.fit(d)
        with torch.no_grad():
            p = mdl.prob_first_wins(d)
            ll = float(-torch.log(p.clamp(1e-6, 1)).mean())
            # information per offset: sum of w p(1-p) over appearances (each side once), + lambda -> se
            q = (d['w'] * p * (1 - p)).cpu().numpy()
            info_s = np.full(len(mdl.off_s), CONFIG.lam_sailor); info_c = np.full(len(mdl.off_c), CONFIG.lam_crew); info_h = np.full(len(mdl.off_h), CONFIG.lam_school)
            skip = d['skip'].cpu().numpy(); crew = d['crew'].cpu().numpy(); sch = d['sch'].cpu().numpy()
            for side in (0, 1):
                for j in range(3):
                    np.add.at(info_s, skip[:, side, j], q); np.add.at(info_c, crew[:, side, j], q)
                np.add.at(info_h, sch[:, side], q)
        off_s = mdl.off_s.detach().cpu().numpy(); off_c = mdl.off_c.detach().cpu().numpy(); off_h = mdl.off_h.detach().cpu().numpy()
        k = float(mdl.k); b0 = float(mdl.b0)
        # counts
        n_sk = defaultdict(int); w_sk = defaultdict(int); n_cr = defaultdict(int); w_cr = defaultdict(int); n_h = defaultdict(int); w_h = defaultdict(int)
        for r in lr:
            for pre, won in (('w_', 1), ('l_', 0)):
                for x in r[pre + 'skippers'].split(';'):
                    n_sk[x] += 1; w_sk[x] += won
                for x in r[pre + 'crews'].split(';'):
                    if x:
                        n_cr[x] += 1; w_cr[x] += won
                n_h[r[pre + 'school']] += 1; w_h[r[pre + 'school']] += won
        suffix = '' if league == 'ICSA' else '_issa'
        with open(HERE / f'ratings_teamrace{suffix}.csv', 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['sailor_id', 'name', 'year', 'school', 'league', 'main_rating', 'tr_rating', 'offset', 'offset_se',
                        'matches_skipper', 'wins_skipper', 'matches_crew', 'wins_crew', 'crew_offset', 'crew_offset_se'])
            sids = sorted(set(n_sk) | set(n_cr), key=lambda x: -(n_sk[x] + n_cr[x]))
            for sid in sids:
                name, year, school, _lg = describe(sid, info, league)
                mr = main.latest(sid)
                i_s = d['idx']['s'].map.get(sid); i_c = d['idx']['c'].map.get(sid)
                o = float(off_s[i_s]) if i_s is not None else 0.0; se = float(1 / np.sqrt(info_s[i_s])) if i_s is not None else ''
                oc = float(off_c[i_c]) if i_c is not None else ''; sec = float(1 / np.sqrt(info_c[i_c])) if i_c is not None else ''
                tr = (k * mr if mr is not None else b0) + o
                w.writerow([sid, name, year, school, league, f'{mr:.4f}' if mr is not None else '', f'{tr:.4f}', f'{o:.4f}', f'{se:.4f}' if se != '' else '',
                            n_sk[sid], w_sk[sid], n_cr[sid], w_cr[sid], f'{oc:.4f}' if oc != '' else '', f'{sec:.4f}' if sec != '' else ''])
        with open(HERE / f'school_effects_teamrace{suffix}.csv', 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['school_slug', 'league', 'offset', 'offset_se', 'matches', 'win_share'])
            for (lg, slug), i in sorted(d['idx']['h'].map.items(), key=lambda kv: -off_h[kv[1]]):
                w.writerow([slug, lg, f'{off_h[i]:.4f}', f'{1 / np.sqrt(info_h[i]):.4f}', n_h[slug], f'{w_h[slug] / max(n_h[slug], 1):.3f}'])
        out_model[league] = dict(k=k, b0=b0, temperature=TEMPERATURE.get(league, 1.0), g_home=float(mdl.g_home), g_exp=[float(x) for x in mdl.g_exp], matches=len(lr),
                                 sailors=len(sids), schools=len(d['idx']['h']), logloss_insample=ll, offset_sd=float(off_s[:-1].std()),
                                 crew_offset_sd=float(off_c[:-1].std()), school_sd=float(off_h[:-1].std()), objective=loss)
        print(f"{league}: {len(lr):,} matches, k {k:.3f}, unrated level {b0:+.3f}, home edge {float(mdl.g_home):+.3f}, experience slopes "
              f"{[round(float(x), 3) for x in mdl.g_exp]}, offsets sd skipper {off_s[:-1].std():.3f} crew {off_c[:-1].std():.3f} school {off_h[:-1].std():.3f}, in-sample log-loss {ll:.4f}")
    out_model['config'] = asdict(CONFIG)
    json.dump(out_model, open(HERE / 'model_teamrace.json', 'w'), indent=1)
    print('wrote ratings_teamrace*.csv, school_effects_teamrace*.csv, model_teamrace.json')


if __name__ == '__main__':
    main()
