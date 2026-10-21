#!/usr/bin/env python3
"""Is there an event "orderliness" (rating temperature) for team race regattas, and what explains it?

For every finished team race regatta with a pre-event forecast (regatta_forecasts_<league>.json, line-ups as sailed) fit a temperature
beta in P(winner) = sigmoid(beta * logit_i), logit_i = the forecast's log-odds for the side that won.  beta > global = the favourites
won more than the model expected (an orderly event), beta < global = an upset-heavy event.  As in analysis/blog/race_temperature.py
everything uses the score s and information I of the log-likelihood at the global beta: beta_hat = beta_g + s / I, se = 1 / sqrt(I).

Then:  is the temperature even a property of the event (split-half persistence inside the event, same event across years)?
       what the entry list explains (field model: teams, matches, strength spread, newcomers, championship, women's);
       whether the venue explains anything (host effects, and the host's FLEET orderliness offset);
       whether the wind explains anything (scorer-stated wind, reanalysis wind, and the fleet wind surface of the venue evaluated
       at this event's wind);  whether the fleet regatta sailed at the same venue the same weekend was orderly too.
All regressions are weighted by I and scored by 10-fold cross-validated R^2.

    python3 analysis/team_race/orderliness.py 2>&1 | tee analysis/team_race/orderliness.log
Writes data/orderliness_teamrace.csv (league, season, name, url, host, matches, info, field, pred, actual, se).
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / 'analysis' / 'orderliness'))
from build_orderliness import CHAMP_WORDS, circ_mean, dir_deg  # noqa: E402

MIN_TRAIN = 1000
MIN_MATCHES = 10
FIELD = ['teams', 'matches', 'mean_s', 'sd_s', 'unr', 'champ', 'womens', 'ln_train', 'abs_logit']


def sig(x):
    return 1 / (1 + np.exp(-x))


def ridge(X, pen, y, w):
    sw = np.sqrt(w); A = X * sw[:, None]
    return np.linalg.solve(A.T @ A + np.diag(pen), A.T @ (y * sw))


def cv_r2(X, pen, y, w, seed=0, folds=10):
    rng = np.random.default_rng(seed); fold = rng.integers(0, folds, len(y)); pred = np.zeros(len(y))
    for k in range(folds):
        tr = fold != k
        pred[fold == k] = X[fold == k] @ ridge(X[tr], pen, y[tr], w[tr])
    ybar = np.average(y, weights=w)
    return 1 - (w * (y - pred) ** 2).sum() / (w * (y - ybar) ** 2).sum(), pred


def wcorr(x, y, w):
    mx = np.average(x, weights=w); my = np.average(y, weights=w)
    return float(np.average((x - mx) * (y - my), weights=w) / math.sqrt(np.average((x - mx) ** 2, weights=w) * np.average((y - my) ** 2, weights=w)))


def main() -> None:
    meta = {}
    for lg, p in (('ICSA', 'icsa_regattas.csv'), ('ISSA', 'issa_regattas.csv')):
        for r in csv.DictReader(open(ROOT / 'data' / p, newline='', encoding='utf-8')):
            u = r['url'].rstrip('/').split('.org', 1)[-1]
            meta.setdefault((lg, u), r)
    stated = defaultdict(list)   # (league, url) -> [(spd, dir)]
    for r in csv.DictReader(open(ROOT / 'data' / 'regatta_days_llm.csv', newline='', encoding='utf-8')):
        if r['day'] and r['wind_lo'] != '':
            try:
                lo = float(r['wind_lo']); hi = float(r['wind_hi']) if r['wind_hi'] != '' else lo
                stated[(r['league'], r['url'].rstrip('/').split('.org', 1)[-1])].append(((lo + hi) / 2, dir_deg(r['wind_dir'])))
            except ValueError:
                pass
    hourly = defaultdict(lambda: [[], []])   # (league, season, name) -> speeds, dirs over 9-17h
    for r in csv.DictReader(open(ROOT / 'data' / 'regatta_wind_hourly.csv', newline='', encoding='utf-8')):
        if 9 <= int(r['hour']) <= 17:
            a = hourly[(r['league'], r['season'], r['name'])]; a[0].append(float(r['wind_kn'])); a[1].append(float(r['dir_deg']))
    venues = json.load(open(ROOT / 'data' / 'orderliness_venues.json', encoding='utf-8'))
    fleet_rows = list(csv.DictReader(open(ROOT / 'data' / 'orderliness_regattas.csv', newline='', encoding='utf-8')))
    fleet_by_host = defaultdict(list)
    for r in fleet_rows:
        m = meta.get((r['league'], r['url']))
        if r['host'] and m:
            d0 = dt.datetime.strptime(m['start_date'], '%m/%d/%Y').date()
            fleet_by_host[(r['league'], r['host'])].append((d0, float(r['actual']), float(r['se']), float(r['pred'])))

    def surf_at(sf, dr, spd):
        t = math.radians(dr)
        return sf['c0'] + sf['off'] + sf['a'] * (spd - sf['sbar']) + sf['h'][0] * math.cos(t) + sf['h'][1] * math.sin(t) + sf['h'][2] * math.cos(2 * t) + sf['h'][3] * math.sin(2 * t)

    out_rows = []
    for league in ('ICSA', 'ISSA'):
        fcs = json.load(open(HERE / f'regatta_forecasts_{league}.json', encoding='utf-8'))
        ev = []
        for url, fc in fcs.items():
            if int(fc.get('n_train', 0) or 0) < MIN_TRAIN or len(fc.get('matches', [])) < MIN_MATCHES:
                continue
            p = np.clip([m['p'] for m in fc['matches']], 1e-6, 1 - 1e-6)
            ev.append((url, fc, np.log(p / (1 - p)), np.array([m['match'] for m in fc['matches']])))
        if not ev:
            continue
        all_l = np.concatenate([e[2] for e in ev])
        bg = 1.0
        for _ in range(30):                                        # Newton for the global temperature
            s = (all_l * (1 - sig(bg * all_l))).sum(); I = (all_l ** 2 * sig(bg * all_l) * (1 - sig(bg * all_l))).sum()
            bg += s / I
        print(f'\n===== {league}: {len(ev)} regattas, {len(all_l):,} matches; global temperature {bg:.3f} (1 = the forecasts were calibrated) =====')

        def si(l):
            q = sig(bg * l)
            return float((l * (1 - q)).sum()), float((l ** 2 * q * (1 - q)).sum())

        recs = []
        for url, fc, l, mnum in ev:
            s, I = si(l)
            if I <= 0:
                continue
            m = meta.get((league, url), {})
            name = fc['name']; nl = name.lower()
            boats = [b for t in fc['teams'] for b in t.get('boats', [])]
            strengths = np.array([t['strength'] for t in fc['teams']])
            s_odd, I_odd = si(l[mnum % 2 == 1]); s_even, I_even = si(l[mnum % 2 == 0])
            half = len(l) // 2
            s_a, I_a = si(l[:half]); s_b, I_b = si(l[half:])
            recs.append(dict(
                league=league, url=url, season=fc['season'], name=name, host=m.get('host', ''), date=fc['date'], n=len(l), s=s, I=I,
                beta=bg + s / I, se=1 / math.sqrt(I),
                odd=(bg + s_odd / I_odd, I_odd) if I_odd > 0 else None, even=(bg + s_even / I_even, I_even) if I_even > 0 else None,
                first=(bg + s_a / I_a, I_a) if I_a > 0 else None, second=(bg + s_b / I_b, I_b) if I_b > 0 else None,
                teams=len(fc['teams']), matches=len(l), mean_s=float(strengths.mean()), sd_s=float(strengths.std()),
                unr=float(np.mean([b.get('main') is None for b in boats])) if boats else 0.0,
                champ=int(any(x in nl for x in CHAMP_WORDS)), womens=int('women' in nl), ln_train=math.log(fc['n_train']),
                abs_logit=float(np.abs(l).mean()), slug=url.rstrip('/').split('/')[-1]))
        y = np.array([r['beta'] for r in recs]); w = np.array([r['I'] for r in recs])
        print(f"temperature: weighted mean {np.average(y, weights=w):.3f}, sd of estimates {y.std():.3f}, median se {np.median([r['se'] for r in recs]):.3f}")
        het = np.mean([r['s'] ** 2 / r['I'] for r in recs])
        print(f"heterogeneity: mean s^2/I = {het:.2f} (1.0 if every event had the same temperature); true sd of event temperature "
              f"~ {math.sqrt(max(0.0, (het - 1) * np.mean([1 / r['I'] for r in recs]))):.3f}")
        # persistence inside the event and across years
        for a, b, lab in (('odd', 'even', 'odd vs even matches'), ('first', 'second', 'first half vs second half')):
            ok = [r for r in recs if r[a] and r[b]]
            xa = np.array([r[a][0] for r in ok]); xb = np.array([r[b][0] for r in ok]); ww = np.array([min(r[a][1], r[b][1]) for r in ok])
            print(f"split-half persistence ({lab}): weighted r = {wcorr(xa, xb, ww):+.3f} over {len(ok)} events")
        by_slug = defaultdict(list)
        for r in recs:
            key = ''.join(c for c in r['slug'] if not c.isdigit()).replace('--', '-')
            by_slug[key].append(r)
        pa, pb, pw = [], [], []
        for k, rs in by_slug.items():
            rs = sorted(rs, key=lambda r: r['date'])
            for r1, r2 in zip(rs, rs[1:]):
                pa.append(r1['beta']); pb.append(r2['beta']); pw.append(min(r1['I'], r2['I']))
        if len(pa) > 10:
            print(f"same event, consecutive editions: weighted r = {wcorr(np.array(pa), np.array(pb), np.array(pw)):+.3f} over {len(pa)} pairs")

        # field model
        F = np.array([[r[k] for k in FIELD] for r in recs], float)
        mu = np.average(F, axis=0, weights=w); sd = np.sqrt(np.average((F - mu) ** 2, axis=0, weights=w)) + 1e-9
        XF = np.column_stack([np.ones(len(recs)), (F - mu) / sd]); penF = np.concatenate([[0.0], np.full(len(FIELD), 0.1)])
        r2f, pred_cv = cv_r2(XF, penF, y, w)
        cf = ridge(XF, penF, y, w)
        print(f"field model CV R^2 {r2f:+.4f}; effects per sd: " + ', '.join(f'{n} {c:+.3f}' for n, c in zip(FIELD, cf[1:])))
        r2_0, _ = cv_r2(XF[:, :1], penF[:1], y, w)
        for k, nme in enumerate(FIELD):            # single-feature models, to see what carries it
            r2k, _ = cv_r2(XF[:, [0, k + 1]], penF[[0, k + 1]], y, w)
            print(f"   {nme:>10s} alone: CV R^2 {r2k:+.4f}")
        pred_field = XF @ cf
        resid = y - pred_field

        # venue: host effects on the field residual, and the host's fleet offset
        hosts = sorted({r['host'] for r in recs if r['host']}); hix = {h: i for i, h in enumerate(hosts)}
        H = np.zeros((len(recs), len(hosts)))
        for i, r in enumerate(recs):
            if r['host']:
                H[i, hix[r['host']]] = 1
        for lam in (3.0, 10.0, 30.0):
            r2h, _ = cv_r2(np.column_stack([np.ones(len(recs)), H]), np.concatenate([[0.0], np.full(len(hosts), lam)]), resid, w)
            print(f"host effects on the field residual (lambda {lam:g}, {len(hosts)} hosts): CV R^2 {r2h:+.4f}")
        foff = np.array([venues['hosts'].get(r['host'], {}).get('surf', {}).get('off', 0.0) for r in recs])
        has = np.array([r['host'] in venues['hosts'] for r in recs])
        if has.sum() > 20:
            print(f"host's fleet orderliness offset vs TR residual: weighted r = {wcorr(foff[has], resid[has], w[has]):+.3f} over {has.sum()} events at fleet-mapped venues")

        # wind
        def wind_feats(spd, dr):
            t = math.radians(dr) if dr is not None else None
            return [spd, (spd - 10) ** 2 / 10] + ([math.cos(t), math.sin(t), math.cos(2 * t), math.sin(2 * t)] if t is not None else [0, 0, 0, 0])

        for lab, getter in (('stated wind (scorer)', lambda r: stated.get((league, r['url']))),
                            ('reanalysis wind (9-17h, in stated units)', lambda r: [(0.81 * np.mean(a[0]) + 3.7, circ_mean(a[1]))] if (a := hourly.get((league, r['season'], r['name']))) and a[0] else None)):
            idx, X, surf, surf_g = [], [], [], []
            for i, r in enumerate(recs):
                days = getter(r)
                if not days:
                    continue
                spd = float(np.mean([d[0] for d in days])); dirs = [d[1] for d in days if d[1] is not None]
                dr = circ_mean(dirs) if dirs else None
                idx.append(i); X.append(wind_feats(spd, dr))
                sf = venues['hosts'].get(r['host'], {}).get('surf') or venues['global']
                surf.append(surf_at(sf, dr if dr is not None else 0.0, spd) if dr is not None else sf['c0'] + sf['off'] + sf['a'] * (spd - sf['sbar']))
                surf_g.append(surf_at(venues['global'], dr if dr is not None else 0.0, spd) if dr is not None else venues['global']['c0'] + venues['global']['a'] * (spd - venues['global']['sbar']))
            if len(idx) < 30:
                print(f"{lab}: only {len(idx)} events, skipped")
                continue
            idx = np.array(idx); X = np.array(X); rr = resid[idx]; ww = w[idx]
            spd = X[:, 0]
            print(f"{lab}: {len(idx)} events; mean {np.average(spd, weights=ww):.1f} kn")
            print(f"   speed vs residual: weighted r = {wcorr(spd, rr, ww):+.3f}; slope {ridge(np.column_stack([np.ones(len(idx)), spd]), np.array([0, 0.0]), rr, ww)[1]:+.4f} per knot")
            Xw = np.column_stack([np.ones(len(idx)), X]); r2w, _ = cv_r2(Xw, np.concatenate([[0.0], np.full(X.shape[1], 1.0)]), rr, ww)
            print(f"   speed + speed^2 + direction harmonics: CV R^2 on the field residual {r2w:+.4f}")
            for nm, sv in (('venue fleet surface at this wind', np.array(surf)), ('global fleet surface at this wind', np.array(surf_g))):
                print(f"   {nm}: weighted r = {wcorr(sv, rr, ww):+.3f}; slope {ridge(np.column_stack([np.ones(len(idx)), sv]), np.array([0, 0.0]), rr, ww)[1]:+.3f} (1 = same effect as in fleet racing)")
            for lo, hi in ((0, 7), (7, 12), (12, 99)):
                mk = (spd >= lo) & (spd < hi)
                if mk.sum() >= 5:
                    print(f"   {lo:>2d}-{hi:<2d} kn: {mk.sum():3d} events, mean temperature {np.average(y[idx][mk], weights=ww[mk]):.3f}, residual {np.average(rr[mk], weights=ww[mk]):+.3f}")

        # the fleet regatta at the same venue the same weekend
        xa, xb, ww, xp = [], [], [], []
        for i, r in enumerate(recs):
            if not r['host']:
                continue
            d0 = dt.date.fromisoformat(r['date'])
            near = [f for f in fleet_by_host.get((league, r['host']), []) if abs((f[0] - d0).days) <= 2]
            if near:
                wf = np.array([1 / f[2] ** 2 for f in near])
                xa.append(np.average([f[1] for f in near], weights=wf)); xp.append(np.average([f[3] for f in near], weights=wf)); xb.append(resid[i]); ww.append(w[i])
        if len(xa) >= 15:
            xa, xb, ww, xp = map(np.array, (xa, xb, ww, xp))
            print(f"fleet regatta at the same host within 2 days: {len(xa)} events; fleet ACTUAL orderliness vs TR residual r = {wcorr(xa, xb, ww):+.3f}; "
                  f"fleet PREDICTED orderliness vs TR residual r = {wcorr(xp, xb, ww):+.3f}")

        for r, pf in zip(recs, pred_field):
            out_rows.append([league, r['season'], r['name'], r['url'], r['host'], r['n'], f"{r['I']:.1f}", f"{pf - bg:+.4f}", f"{pf - bg:+.4f}", f"{r['beta'] - bg:+.4f}", f"{r['se']:.4f}"])
        # the most orderly / most surprising events
        top = sorted(recs, key=lambda r: (r['beta'] - bg) / r['se'])
        print('most upset-heavy: ' + '; '.join(f"{r['name']} {r['season']} ({r['beta'] - bg:+.2f} ± {r['se']:.2f})" for r in top[:4]))
        print('most orderly:     ' + '; '.join(f"{r['name']} {r['season']} ({r['beta'] - bg:+.2f} ± {r['se']:.2f})" for r in top[-4:]))

    with open(ROOT / 'data' / 'orderliness_teamrace.csv', 'w', newline='', encoding='utf-8') as fh:
        wr = csv.writer(fh); wr.writerow(['league', 'season', 'name', 'url', 'host', 'matches', 'info', 'field', 'pred', 'actual', 'se']); wr.writerows(out_rows)
    print(f'\nwrote data/orderliness_teamrace.csv ({len(out_rows)} regattas)')


if __name__ == '__main__':
    main()
