#!/usr/bin/env python3
"""Data for the unlinked audit page website/icsa/lab/team-race-combinations/: winning-combination mixes by breeze, by strength level,
balanced vs lopsided sides, the held-out model comparison, and the multinomial model's weights for the in-page explorer.

    python3 analysis/team_race/export_combinations.py 2>&1 | tee analysis/team_race/export_combinations.log
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import sequence_model as M  # noqa: E402

OUT = ROOT / 'website' / 'icsa' / 'lab' / 'team-race-combinations'
ORDER = {'drifter': 0, 'light': 1, 'moderate': 2, 'fresh': 3, 'heavy': 4}
NAMES = ['drifter', 'light', 'moderate', 'fresh', 'heavy']
GRP = {'drifter': 'light', 'light': 'light', 'moderate': 'moderate', 'fresh': 'fresh/heavy', 'heavy': 'fresh/heavy'}
WINSETS = [c for c in M.COMBOS if sum(c) <= 10]


def breeze_classes():
    words = defaultdict(list)
    for r in csv.DictReader(open(ROOT / 'data' / 'regatta_days_llm.csv', newline='', encoding='utf-8')):
        if r['wind_qual'] in ORDER:
            words[(r['league'], r['url'].rstrip('/').split('.org', 1)[-1])].append(ORDER[r['wind_qual']])
    return {k: NAMES[round(np.mean(v))] for k, v in words.items()}


def mix(counter, n):
    return {'-'.join(map(str, c)): round(counter[c] / n, 4) for c in WINSETS} | {'n': int(n), 'first': round(sum(v for c, v in counter.items() if c[0] == 1) / n, 4)}


def main():
    cls = breeze_classes()
    out = {'winsets': ['-'.join(map(str, c)) for c in WINSETS], 'combos': ['-'.join(map(str, c)) for c in M.COMBOS], 'leagues': {}}
    for league in ('ICSA', 'ISSA'):
        M.LEAGUE = league
        X, Y, S, meta = M.load()
        win_rows = np.array([m[2] == 1 for m in meta])
        urls = {k: None for k, _, _ in meta}
        # breeze word per row (event level)
        bw = np.array([cls.get((league, m[0][0]), '') for m in meta])
        a = X[:, :3]; tot = a.sum(1); spread = a.std(1); level = (X[:, :3].sum(1) + X[:, 3:6].sum(1)) / 2
        q = np.quantile(level[win_rows], [1 / 3, 2 / 3])
        L = {}
        # 1. by breeze (winners' rows, all seasons)
        L['by_breeze'] = {}
        for b in NAMES:
            sel = win_rows & (bw == b)
            if sel.sum() >= 100:
                L['by_breeze'][b] = mix(Counter(M.COMBOS[y] for y in Y[sel]), sel.sum())
        # 2. by level x breeze group
        L['by_level'] = {}
        for li, lab in enumerate(('weakest third', 'middle third', 'strongest third')):
            lsel = (level < q[0]) if li == 0 else (level >= q[1]) if li == 2 else ((level >= q[0]) & (level < q[1]))
            L['by_level'][lab] = {}
            for g in ('light', 'moderate', 'fresh/heavy'):
                sel = win_rows & lsel & np.isin(bw, [k for k, v in GRP.items() if v == g])
                if sel.sum() >= 100:
                    L['by_level'][lab][g] = mix(Counter(M.COMBOS[y] for y in Y[sel]), sel.sum())
        # 3. balanced vs lopsided within sum thirds (winner's own make-up)
        qs = np.quantile(tot[win_rows], [1 / 3, 2 / 3])
        L['by_spread'] = {}
        for li, lab in enumerate(('weakest third', 'middle third', 'strongest third')):
            sel = win_rows & ((tot < qs[0]) if li == 0 else (tot >= qs[1]) if li == 2 else ((tot >= qs[0]) & (tot < qs[1])))
            med = float(np.median(spread[sel]))
            L['by_spread'][lab] = {'split_sd': round(med, 3)}
            for lab2, s2 in (('balanced', sel & (spread < med)), ('lopsided', sel & (spread >= med))):
                L['by_spread'][lab][lab2] = mix(Counter(M.COMBOS[y] for y in Y[s2]), s2.sum()) | {'mean_sd': round(float(spread[s2].mean()), 3)}
        # 3b. empirical movement: share of each of the 20 sets (X orientation, both copies) by strength gap, and by X's spread at near-equal totals
        gap = X[:, :3].sum(1) - X[:, 3:6].sum(1)
        edges = np.arange(-4.0, 4.01, 0.5)
        L['by_gap'] = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            sel = (gap >= lo) & (gap < hi)
            if sel.sum() >= 150:
                C = np.bincount(Y[sel], minlength=20) / sel.sum()
                L['by_gap'].append({'lo': lo, 'hi': hi, 'n': int(sel.sum()), 'p': [round(float(x), 4) for x in C]})
        L['by_star'] = []
        near = np.abs(gap) < 0.75
        sdx = X[:, :3].std(1)
        for lo, hi in ((0, .1), (.1, .2), (.2, .3), (.3, .45), (.45, .7), (.7, 9)):
            sel = near & (sdx >= lo) & (sdx < hi)
            if sel.sum() >= 150:
                C = np.bincount(Y[sel], minlength=20) / sel.sum()
                L['by_star'].append({'lo': lo, 'hi': hi, 'n': int(sel.sum()), 'p': [round(float(x), 4) for x in C]})
        # 4. models (held out s25+s26)
        is_test = np.array([s in M.TEST for s in S]); tr = ~is_test
        F = M.feats(X); ytr, yte = Y[tr], Y[is_test]
        marg = np.bincount(ytr, minlength=20) / tr.sum()
        pwin = np.array([m[1] for m in meta])[is_test]; outc = np.array([m[2] for m in meta])[is_test]
        cw = marg * M.WIN / marg[M.WIN].sum(); cl = marg * ~M.WIN / marg[~M.WIN].sum()
        P_two = pwin[:, None] * cw[None, :] + (1 - pwin)[:, None] * cl[None, :]
        models = {'Combination frequencies alone': {'nll': M.nll(np.tile(marg, (is_test.sum(), 1)), yte)},
                  'Production win probability x combination shape': {'nll': M.nll(P_two, yte), 'win_ll': float(-np.log(np.clip(np.where(outc == 1, pwin, 1 - pwin), 1e-9, 1)).mean())}}
        val = (S[tr] == 's24')
        # multinomial with weights kept for the explorer
        dev = 'cuda' if torch.cuda.is_available() else 'cpu'
        mu, sd = F[tr].mean(0), F[tr].std(0) + 1e-9
        Xt = torch.tensor((F[tr] - mu) / sd, dtype=torch.float32, device=dev); yt = torch.tensor(ytr, device=dev)
        lin = torch.nn.Linear(F.shape[1], 20).to(dev); opt = torch.optim.AdamW(lin.parameters(), lr=2e-3, weight_decay=1e-2)
        for ep in range(300):
            perm = torch.randperm(len(ytr), device=dev)
            for i in range(0, len(ytr), 1024):
                idx = perm[i:i + 1024]; loss = torch.nn.functional.cross_entropy(lin(Xt[idx]), yt[idx]); opt.zero_grad(); loss.backward(); opt.step()
        with torch.no_grad():
            P = torch.softmax(lin(torch.tensor((F[is_test] - mu) / sd, dtype=torch.float32, device=dev)), 1).cpu().numpy()
        pw = P[:, M.WIN].sum(1)
        pc = np.where(outc[:, None] == 1, P * M.WIN, P * ~M.WIN); pc = pc / pc.sum(1, keepdims=True)
        models['Multinomial on the six boat values'] = {'nll': M.nll(P, yte), 'win_ll': float(-np.log(np.clip(np.where(outc == 1, pw, 1 - pw), 1e-9, 1)).mean()),
                                                        'shape_nll': M.nll(pc, yte)}
        models['Production win probability x combination shape']['shape_nll'] = M.nll(np.where(outc[:, None] == 1, cw, cl), yte)
        pr = M.fit_softmax(F[tr], ytr, 64, val)
        P = pr(F[is_test]); pw = P[:, M.WIN].sum(1)
        pc = np.where(outc[:, None] == 1, P * M.WIN, P * ~M.WIN); pc = pc / pc.sum(1, keepdims=True)
        models['Small network (64 hidden, early stopped)'] = {'nll': M.nll(P, yte), 'win_ll': float(-np.log(np.clip(np.where(outc == 1, pw, 1 - pw), 1e-9, 1)).mean()), 'shape_nll': M.nll(pc, yte)}
        L['models'] = {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in models.items()}
        L['n_train'] = int(tr.sum() // 2); L['n_test'] = int(is_test.sum() // 2); L['n_all'] = int(len(Y) // 2)
        L['explorer'] = {'W': [[round(float(x), 5) for x in row] for row in lin.weight.detach().cpu().numpy()], 'b': [round(float(x), 5) for x in lin.bias.detach().cpu().numpy()],
                         'mu': [round(float(x), 5) for x in mu], 'sd': [round(float(x), 5) for x in sd], 'combos': ['-'.join(map(str, c)) for c in M.COMBOS],
                         'typical_boat': round(float(np.median(tot) / 3), 3), 'boat_q': [round(float(x), 2) for x in np.quantile(a.ravel(), [0.05, 0.25, 0.5, 0.75, 0.95])],
                         'school_q': [round(float(x), 2) for x in np.quantile(X[:, 6], [0.05, 0.5, 0.95])]}
        out['leagues'][league] = L
        print(league, 'done:', {k: v['nll'] for k, v in L['models'].items()})
    OUT.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(OUT / 'data.json', 'w', encoding='utf-8'), separators=(',', ':'))
    print('wrote', OUT / 'data.json')


if __name__ == '__main__':
    main()
