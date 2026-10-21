#!/usr/bin/env python3
"""Forward evaluation of the team race model on held-out events.

For every test regatta (chronological) the model is fitted on every match sailed BEFORE that regatta's first day (both leagues'
team racing is fitted separately), with the main ratings taken from the snapshot trained before the test season
(analysis/plackett_luce/eval/forecast_<season>_joint), so nothing from the test season's fleet racing leaks in either.  Scored on the
match winner: log-loss (what the user asked to minimise) and accuracy, per variant.  Refits happen once per distinct regatta date.

    python3 analysis/team_race/evaluate.py [--league ICSA] [--seasons s25,s26] [--variants main,full,...] 2>&1 | tee analysis/team_race/evaluate.log
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BASE / 'analysis' / 'disciplines'))
import model as M  # noqa: E402
import fit as F  # noqa: E402  (MainRatings with snapshot ID translation)

PL = BASE / 'analysis' / 'plackett_luce'

VARIANTS = {
    'const': M.Config('const', use_main=False, use_sailor=False, use_crew=False, use_school=False, use_school_season=False),
    'school': M.Config('school', use_main=False, use_sailor=False, use_crew=False),
    'main': M.Config('main', use_sailor=False, use_crew=False, use_school=False, use_school_season=False),
    'main+school': M.Config('main+school', use_sailor=False, use_crew=False),
    'main+sailor': M.Config('main+sailor', use_school=False, use_school_season=False),
    'full': M.Config('full'),
    'full-noseason': M.Config('full-noseason', use_school_season=False),
    'full+pos': M.Config('full+pos', pos_weight=0.3),
    'full+pos1': M.Config('full+pos1', pos_weight=1.0),
    'full+crewmain': M.Config('full+crewmain', use_crew_main=True),
    'full-hl1': M.Config('full-hl1', half_life=1.0),
    'full-hlnone': M.Config('full-hlnone', half_life=None),
    'full-lam1': M.Config('full-lam1', lam_sailor=1.0, lam_crew=2.0),
    'full-lam10': M.Config('full-lam10', lam_sailor=10.0, lam_crew=10.0),
    'full-sch1': M.Config('full-sch1', lam_school=1.0, lam_school_season=1.0),
    'full-sch10': M.Config('full-sch10', lam_school=10.0, lam_school_season=10.0),
    'full-ordered': M.Config('full-ordered', agg='ordered'),
    'best': M.Config('best', use_school_season=False, lam_sailor=10.0, lam_crew=10.0),
    'best+home': M.Config('best+home', use_school_season=False, lam_sailor=10.0, lam_crew=10.0, use_home=True),
    'best+exp': M.Config('best+exp', use_school_season=False, lam_sailor=10.0, lam_crew=10.0, use_exp=True),
    'best+home+exp': M.Config('best+home+exp', use_school_season=False, lam_sailor=10.0, lam_crew=10.0, use_home=True, use_exp=True),
    'best-lam30': M.Config('best-lam30', use_school_season=False, lam_sailor=30.0, lam_crew=30.0, use_home=True, use_exp=True),
    'best-lam100': M.Config('best-lam100', use_school_season=False, lam_sailor=100.0, lam_crew=100.0, use_home=True, use_exp=True),
    'best-sch10': M.Config('best-sch10', use_school_season=False, lam_sailor=10.0, lam_crew=10.0, lam_school=10.0, use_home=True, use_exp=True),
    'best-hl1': M.Config('best-hl1', use_school_season=False, lam_sailor=10.0, lam_crew=10.0, half_life=1.0, use_home=True, use_exp=True),
    'best-hl4': M.Config('best-hl4', use_school_season=False, lam_sailor=10.0, lam_crew=10.0, half_life=4.0, use_home=True, use_exp=True),
    'b30-season10': M.Config('b30-season10', lam_sailor=30.0, lam_crew=30.0, lam_school_season=10.0, use_home=True, use_exp=True),
    'b30-season30': M.Config('b30-season30', lam_sailor=30.0, lam_crew=30.0, lam_school_season=30.0, use_home=True, use_exp=True),
    'b30-crew100': M.Config('b30-crew100', use_school_season=False, lam_sailor=30.0, lam_crew=100.0, use_home=True, use_exp=True),
    'b30-sch1': M.Config('b30-sch1', use_school_season=False, lam_sailor=30.0, lam_crew=30.0, lam_school=1.0, use_home=True, use_exp=True),
    'sailor30': M.Config('sailor30', use_school=False, use_school_season=False, lam_sailor=30.0, lam_crew=30.0, use_home=True, use_exp=True),
    'sailor10': M.Config('sailor10', use_school=False, use_school_season=False, lam_sailor=10.0, lam_crew=10.0, use_home=True, use_exp=True),
    'sailor100': M.Config('sailor100', use_school=False, use_school_season=False, lam_sailor=100.0, lam_crew=100.0, use_home=True, use_exp=True),
    'school-only+main': M.Config('school-only+main', use_sailor=False, use_crew=False, use_school_season=False, use_home=True, use_exp=True),
    'main+school+home+exp': M.Config('main+school+home+exp', use_sailor=False, use_crew=False, use_school_season=False, use_home=True, use_exp=True),
    'main-ordered': M.Config('main-ordered', use_sailor=False, use_crew=False, use_school=False, use_school_season=False, agg='ordered'),
    # "winning combinations": familiarity of the pair / trio, or the trio and pair as units with their own shrunk offsets
    'b30+combo': M.Config('b30+combo', use_school_season=False, lam_sailor=30.0, lam_crew=30.0, use_home=True, use_exp=True, use_combo=True),
    'b30+trio30': M.Config('b30+trio30', use_school_season=False, lam_sailor=30.0, lam_crew=30.0, use_home=True, use_exp=True, use_trio=True, lam_trio=30.0),
    'b30+trio10': M.Config('b30+trio10', use_school_season=False, lam_sailor=30.0, lam_crew=30.0, use_home=True, use_exp=True, use_trio=True, lam_trio=10.0),
    'b30+trio100': M.Config('b30+trio100', use_school_season=False, lam_sailor=30.0, lam_crew=30.0, use_home=True, use_exp=True, use_trio=True, lam_trio=100.0),
    'b30+pair30': M.Config('b30+pair30', use_school_season=False, lam_sailor=30.0, lam_crew=30.0, use_home=True, use_exp=True, use_pair=True, lam_pair=30.0),
    'b30+pair10': M.Config('b30+pair10', use_school_season=False, lam_sailor=30.0, lam_crew=30.0, use_home=True, use_exp=True, use_pair=True, lam_pair=10.0),
    'b30+combo+trio30': M.Config('b30+combo+trio30', use_school_season=False, lam_sailor=30.0, lam_crew=30.0, use_home=True, use_exp=True, use_combo=True, use_trio=True, lam_trio=30.0),
    'trio30-nosailor': M.Config('trio30-nosailor', use_sailor=False, use_crew=False, use_school_season=False, use_home=True, use_exp=True, use_trio=True, lam_trio=30.0),
}


def crew_ratings() -> dict:
    out = {}
    p = BASE / 'analysis' / 'crew_ratings' / 'crew_rating.csv'
    if p.exists():
        import csv
        with open(p, newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                out[r['sailor_id']] = float(r['rating'])
    return out


def inevent(cfg, mdl, train, batch, main_at, crew_at, now_week, p0):
    """Replace the pre-event predictions of rounds >= 2 by predictions from a model refitted (warm start) on earlier rounds of the
    same regatta too.  Within a regatta, matches without a round number are left as pre-event."""
    import torch
    p = np.array(p0, dtype=float)
    by_reg = defaultdict(list)
    for i, r in enumerate(batch):
        by_reg[r['regatta_url']].append(i)
    cfg2 = replace(cfg, iters=60)
    for url, ids in by_reg.items():
        rounds = sorted({int(batch[i]['round_num']) for i in ids if batch[i]['round_num']})
        if len(rounds) < 2:
            continue
        state = [t.detach().clone() for t in mdl.params()]
        for rn in rounds[1:]:
            seen = [batch[i] for i in ids if batch[i]['round_num'] and int(batch[i]['round_num']) < rn]
            todo = [i for i in ids if batch[i]['round_num'] and int(batch[i]['round_num']) == rn]
            if not seen or not todo:
                continue
            d = M.pack(train + seen, main_at, crew_at, now_week, cfg2, index=None)
            m2 = M.Model(cfg2, d['idx'])
            with torch.no_grad():     # warm start: copy what matches by key
                for name in ('s', 'c', 'h', 'hs'):
                    src = getattr(mdl, 'off_' + name); dst = getattr(m2, 'off_' + name)
                    for key, j in d['idx'][name].map.items():
                        i0 = mdl.idx[name].map.get(key)
                        if i0 is not None:
                            dst[j] = src[i0]
                for a in ('k', 'kc', 'b0', 'c0', 'beta_pos', 'ordw'):
                    getattr(m2, a).copy_(getattr(mdl, a))
            m2.fit(d)
            p[todo] = m2.predict([batch[i] for i in todo], main_at, crew_at, now_week)
    return p


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--league', default='ICSA')
    ap.add_argument('--seasons', default='s25,s26')
    ap.add_argument('--variants', default='const,school,main,main+school,main+sailor,full,full+pos,full+crewmain')
    ap.add_argument('--out', default=str(HERE / 'evaluate.json'))
    ap.add_argument('--snapshot', default='frozen', help="'frozen' = season-start snapshot (leak-free); 'current' = today's weekly ratings as of 2 weeks before (leaky, more seasons)")
    ap.add_argument('--inevent', action='store_true', help='also refit after every completed round of a regatta (predict round r from rounds < r)')
    args = ap.parse_args()
    seasons = args.seasons.split(',')
    variants = [VARIANTS[v] for v in args.variants.split(',')]
    rows = [r for r in M.load_matches() if r['league'] == args.league]
    print(f'{len(rows):,} {args.league} matches; test seasons {seasons}')
    crew_now = crew_ratings()
    results = {v.name: defaultdict(list) for v in variants}
    per_regatta = defaultdict(dict)
    for season in seasons:
        if args.snapshot == 'frozen':
            snap = PL / 'eval' / f'forecast_{season}_joint'
            main = F.MainRatings(snap)
            print(f'  {season}: snapshot {snap.name}, {main.unmatched} unmatched rating rows')
            main_at = main.at
        else:
            main = F.MainRatings(PL)
            main_at = lambda sid, week: main.at(sid, week - 1)       # noqa: E731  two weeks before the match
        crew_at = (lambda sid, week: crew_now.get(sid))          # NOTE: today's crew ratings (no snapshot exists) -> leaky, reported as such
        test = [r for r in rows if r['season'] == season]
        dates = sorted({r['date'] for r in test})
        for date in dates:
            train = [r for r in rows if r['date'] < date]
            batch = [r for r in test if r['date'] == date]
            now_week = int(batch[0]['week'])
            t0 = time.time()
            for cfg in variants:
                d = M.pack(train, main_at, crew_at if cfg.use_crew_main else None, now_week, cfg)
                mdl = M.Model(cfg, d['idx'])
                mdl.fit(d)
                p = mdl.predict(batch, main_at, crew_at if cfg.use_crew_main else None, now_week)
                if args.inevent:
                    p = inevent(cfg, mdl, train, batch, main_at, crew_at if cfg.use_crew_main else None, now_week, p)
                res = results[cfg.name]
                res['p'].extend(p.tolist()); res['season'].extend([season] * len(p))
                for r, pi in zip(batch, p):
                    per_regatta[r['regatta_url']].setdefault(cfg.name, []).append(float(pi))
                    results[cfg.name]['rows'].append((r['regatta_url'], r['match_num'], r['w_school'], r['l_school'], float(pi)))
            print(f'    {date}: {len(batch):4d} matches, trained on {len(train):,} ({time.time() - t0:.0f}s)' +
                  ''.join(f'  {cfg.name} {M.logloss(np.array(results[cfg.name]["p"][-len(batch):])):.3f}' for cfg in variants[:4]))
    summary = {}
    print(f'\n{"variant":16s} {"matches":>8s} {"logloss":>8s} {"acc":>6s}   ' + '  '.join(f'{s:>12s}' for s in seasons))
    for cfg in variants:
        p = np.array(results[cfg.name]['p']); se = np.array(results[cfg.name]['season'])
        ll = M.logloss(p); acc = float((p > 0.5).mean() + 0.5 * (p == 0.5).mean())
        by = {s: (M.logloss(p[se == s]), float((p[se == s] > 0.5).mean())) for s in seasons}
        summary[cfg.name] = dict(matches=int(len(p)), logloss=ll, acc=acc, by_season={s: dict(logloss=v[0], acc=v[1]) for s, v in by.items()})
        print(f'{cfg.name:16s} {len(p):8,d} {ll:8.4f} {acc:6.3f}   ' + '  '.join(f'{by[s][0]:.4f}/{by[s][1]:.3f}' for s in seasons))
    # calibration of the favourite: among matches where one side was given q (>= 0.5), that side should win a share q
    for cfg in variants:
        p = np.array(results[cfg.name]['p']); q = np.maximum(p, 1 - p); won = (p >= 0.5)
        bins = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
        cal = [(lo, hi, int(((q >= lo) & (q < hi)).sum()), float(q[(q >= lo) & (q < hi)].mean()) if ((q >= lo) & (q < hi)).any() else None,
                float(won[(q >= lo) & (q < hi)].mean()) if ((q >= lo) & (q < hi)).any() else None) for lo, hi in bins]
        summary[cfg.name]['calibration'] = cal
        # temperature: slope of a logistic fit of outcome on the logit (1 = calibrated)
        z = np.log(p / (1 - p)).clip(-10, 10)
        best = min(((t, M.logloss(1 / (1 + np.exp(-t * z)))) for t in np.arange(0.5, 1.51, 0.02)), key=lambda x: x[1])
        summary[cfg.name]['temperature'] = dict(t=float(best[0]), logloss_at_t=best[1])
    print('\ncalibration (favourite bins: n, mean p, won share) and best temperature:')
    for cfg in variants:
        c = summary[cfg.name]['calibration']; t = summary[cfg.name]['temperature']
        print(f"  {cfg.name:16s} " + ' '.join(f"[{lo:.1f}-{hi:.1f}: {n} {mp:.3f}/{ws:.3f}]" if n else f"[{lo:.1f}-{hi:.1f}: 0]" for lo, hi, n, mp, ws in c) + f"  t={t['t']:.2f} ll {t['logloss_at_t']:.4f}")
    json.dump(dict(league=args.league, seasons=seasons, summary=summary), open(args.out, 'w'), indent=1)
    with open(Path(args.out).with_suffix('.predictions.csv'), 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['variant', 'regatta_url', 'match_num', 'w_school', 'l_school', 'p_winner'])
        for cfg in variants:
            for row in results[cfg.name]['rows']:
                w.writerow([cfg.name, *row])


if __name__ == '__main__':
    main()
