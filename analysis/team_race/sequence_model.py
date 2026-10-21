#!/usr/bin/env python3
"""Does the rating imbalance inside a team shape how it wins, and can a network predict the whole finish combination?

Per match: the three pre-event boat values of each side (from regatta_forecasts_<league>.json, matched by skipper name within the
regatta: ids in old forecasts are stale), the school terms and the home flag; outcome = the set of positions team X took (20 possible
sets, 10 winning).  Orientation of X is random, with the swapped copy added, so the model is symmetric.

1. Imbalance, directly: within sum-of-strength thirds, the winning combination mix for balanced vs lopsided sides (sd of the three boat values).
2. Models, trained on seasons before the test seasons (s25, s26) and scored by held-out negative log-likelihood per match (nats):
     marginal        combination frequencies alone
     two-stage       the production win model's probability x the combination frequencies given win / loss (no rating -> shape link)
     multinomial     softmax regression on the sorted boat values, school terms, home, pairwise differences
     mlp             a small network on the same inputs
   Also the implied win log-loss (sum of the 10 winning sets) against the production model's forecast on the same matches.
3. What the network says: with the two sides' totals fixed, how the predicted combination distribution changes with the spread.

    python3 analysis/team_race/sequence_model.py [ICSA|ISSA] 2>&1 | tee analysis/team_race/sequence_model.log
"""
from __future__ import annotations

import csv
import itertools
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
LEAGUE = sys.argv[1] if len(sys.argv) > 1 else 'ICSA'
TEST = {'s25', 's26'}
COMBOS = [c for c in itertools.combinations(range(1, 7), 3)]            # 20 position sets for team X
CIDX = {c: i for i, c in enumerate(COMBOS)}
WIN = np.array([sum(c) <= 10 for c in COMBOS])
torch.manual_seed(0); rng = np.random.default_rng(0)


def load():
    fcs = json.load(open(HERE / f'regatta_forecasts_{LEAGUE}.json', encoding='utf-8'))
    names = {}
    for suf in ('', '_issa'):
        for r in csv.DictReader(open(HERE / f'ratings_teamrace{suf}.csv', newline='', encoding='utf-8')):
            if r['league'] == LEAGUE:
                names[r['sailor_id']] = r['name'].lower().strip()
    boats, teams, pm = {}, {}, {}
    for u, fc in fcs.items():
        if int(fc['n_train']) < 1000:
            continue
        for t in fc['teams']:
            teams[(u, t['school'], t['team'])] = (t.get('school_offset', 0.0) or 0.0, t.get('home', 0))
            for b in t['boats']:
                if b.get('strength') is not None and b.get('skipper'):
                    boats[(u, b['skipper'].lower().strip())] = b['strength']
        for m in fc['matches']:
            pm[(u, m['match'])] = m['p']
    X, Y, S, meta = [], [], [], []
    miss = 0
    for r in csv.DictReader(open(HERE / 'matches.csv', newline='', encoding='utf-8')):
        if r['league'] != LEAGUE:
            continue
        u = r['regatta_url']; k = (u, int(r['match_num']))
        if k not in pm:
            continue
        try:
            wp = tuple(sorted(int(x) for x in r['w_pos'].split('-')))
        except ValueError:
            continue
        if len(wp) != 3 or wp not in CIDX:
            continue
        vals = []
        for pre in ('w_', 'l_'):
            v = [boats.get((u, names.get(s, '').lower())) for s in r[pre + 'skippers'].split(';')]
            if len(v) != 3 or any(x is None for x in v):
                vals = None; break
            vals.append(sorted(v, reverse=True))
        if vals is None:
            miss += 1; continue
        tw = teams.get((u, r['w_school'], r['w_team']), (0.0, 0)); tl = teams.get((u, r['l_school'], r['l_team']), (0.0, 0))
        lp = tuple(sorted(set(range(1, 7)) - set(wp)))
        # orientation X = winner, then the swapped copy
        X.append(vals[0] + vals[1] + [tw[0], tl[0], tw[1], tl[1]]); Y.append(CIDX[wp]); S.append(r['season']); meta.append((k, pm[k], 1))
        X.append(vals[1] + vals[0] + [tl[0], tw[0], tl[1], tw[1]]); Y.append(CIDX[lp]); S.append(r['season']); meta.append((k, 1 - pm[k], 0))
    print(f'{LEAGUE}: {len(X) // 2:,} matches with six pre-event boat values ({miss:,} skipped for a missing value)')
    return np.array(X, float), np.array(Y), np.array(S), meta


def feats(X):
    a, b = X[:, :3], X[:, 3:6]
    return np.column_stack([a, b, a.sum(1) - b.sum(1), a.std(1), b.std(1), a[:, 0] - b[:, 0], a[:, 2] - b[:, 2], X[:, 6] - X[:, 7], X[:, 8], X[:, 9]])


def nll(P, y):
    return float(-np.log(np.clip(P[np.arange(len(y)), y], 1e-9, 1)).mean())


def fit_softmax(F, y, hidden, val=None, epochs=300, wd=1e-2, lr=2e-3):
    """Softmax regression (hidden 0) or a 2-layer MLP; `val` = boolean mask of training rows held out for early stopping."""
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    mu, sd = F.mean(0), F.std(0) + 1e-9
    Xt = torch.tensor((F - mu) / sd, dtype=torch.float32, device=dev); yt = torch.tensor(y, device=dev)
    if val is None:
        val = np.zeros(len(y), bool)
    vt = torch.tensor(val, device=dev); tr_idx = torch.nonzero(~vt).flatten()
    d = Xt.shape[1]
    net = (torch.nn.Linear(d, 20) if not hidden else torch.nn.Sequential(torch.nn.Linear(d, hidden), torch.nn.GELU(), torch.nn.Dropout(0.2), torch.nn.Linear(hidden, hidden), torch.nn.GELU(), torch.nn.Dropout(0.2), torch.nn.Linear(hidden, 20))).to(dev)
    opt = torch.optim.AdamW(net.parameters(), lr=lr, weight_decay=wd)
    bs = 1024; best = (1e9, None); bad = 0
    for ep in range(epochs):
        net.train()
        perm = tr_idx[torch.randperm(len(tr_idx), device=dev)]
        for i in range(0, len(perm), bs):
            idx = perm[i:i + bs]
            loss = torch.nn.functional.cross_entropy(net(Xt[idx]), yt[idx])
            opt.zero_grad(); loss.backward(); opt.step()
        if val.any():
            net.eval()
            with torch.no_grad():
                vl = float(torch.nn.functional.cross_entropy(net(Xt[vt]), yt[vt]))
            if vl < best[0] - 1e-4:
                best = (vl, {k: v.clone() for k, v in net.state_dict().items()}); bad = 0
            else:
                bad += 1
                if bad >= 15:
                    break
    if best[1] is not None:
        net.load_state_dict(best[1])
    net.eval()

    def predict(Fn):
        with torch.no_grad():
            return torch.softmax(net(torch.tensor((Fn - mu) / sd, dtype=torch.float32, device=dev)), 1).cpu().numpy()
    return predict


def main():
    X, Y, S, meta = load()
    is_test = np.array([s in TEST for s in S]); tr = ~is_test
    print(f'train {tr.sum() // 2:,} matches, test (s25+s26) {is_test.sum() // 2:,}')

    # 1. imbalance, directly (winners' rows only; sum thirds then spread halves)
    win_rows = np.array([m[2] == 1 for m in meta])
    a = X[:, :3]; tot = a.sum(1); spread = a.std(1)
    q = np.quantile(tot[win_rows], [1 / 3, 2 / 3])
    print('\nWinning combination mix by the winner\'s own make-up (all seasons): sum of the three boat values in thirds, then balanced vs lopsided (sd of the three)')
    print(f'{"sum third":10s} {"make-up":9s} {"n":>6s}  1-2-3  1-2-4/5/6  2-3-4  1-3-x  1-4-5/1-3-6   holds 1st   mean sd')
    for li, lab in enumerate(('low', 'middle', 'top')):
        sel = win_rows & ((tot < q[0]) if li == 0 else (tot >= q[1]) if li == 2 else ((tot >= q[0]) & (tot < q[1])))
        med = np.median(spread[sel])
        for lab2, s2 in (('balanced', sel & (spread < med)), ('lopsided', sel & (spread >= med))):
            C = Counter(COMBOS[y] for y in Y[s2]); n = s2.sum()
            f = lambda *cs: sum(C[c] for c in cs) / n
            print(f'{lab:10s} {lab2:9s} {n:6d}  {f((1,2,3)):5.1%}  {f((1,2,4),(1,2,5),(1,2,6)):9.1%}  {f((2,3,4)):5.1%}  {f((1,3,4),(1,3,5)):5.1%}  {f((1,4,5),(1,3,6)):11.1%}   {sum(v for c, v in C.items() if c[0] == 1) / n:8.1%}   {spread[s2].mean():7.2f}')

    # 2. models
    F = feats(X)
    ytr, yte = Y[tr], Y[is_test]
    marg = np.bincount(ytr, minlength=20) / tr.sum()
    P_marg = np.tile(marg, (is_test.sum(), 1))
    print(f'\nheld-out NLL per match (nats; lower is better), {is_test.sum() // 2:,} matches x 2 orientations')
    print(f'  marginal frequencies            {nll(P_marg, yte):.4f}')
    pwin = np.array([m[1] for m in meta])[is_test]
    cw = marg * WIN / marg[WIN].sum(); cl = marg * ~WIN / marg[~WIN].sum()
    P_two = pwin[:, None] * cw[None, :] + (1 - pwin)[:, None] * cl[None, :]
    print(f'  production win prob x shape     {nll(P_two, yte):.4f}   (win log-loss of the production forecast {float(-np.log(np.clip(pwin, 1e-9, 1)).mean()):.4f} on the X=winner rows... see below)')
    preds = {}
    val = (S[tr] == 's24')                                   # last training season for early stopping
    for name, hidden in (('multinomial', 0), ('mlp 64', 64), ('mlp 128', 128)):
        pr = fit_softmax(F[tr], ytr, hidden, val)
        P = pr(F[is_test]); preds[name] = (pr, P)
        pw = P[:, WIN].sum(1)
        print(f'  {name:30s}  {nll(P, yte):.4f}   implied win log-loss {float(-np.log(np.clip(np.where(np.array([m[2] for m in meta])[is_test] == 1, pw, 1 - pw), 1e-9, 1)).mean()):.4f}')
    outc = np.array([m[2] for m in meta])[is_test]
    print(f'  production forecast win log-loss on these matches: {float(-np.log(np.clip(np.where(outc == 1, pwin, 1 - pwin), 1e-9, 1)).mean()):.4f}')
    # shape only: NLL of the combination given the winner is known
    for name, (pr, P) in preds.items():
        Pc = np.where(WIN[None, :], P, 0) if True else P
        pc = np.where(outc[:, None] == 1, P * WIN, P * ~WIN); pc = pc / pc.sum(1, keepdims=True)
        print(f'  {name:30s} NLL of the combination given who won {nll(pc, yte):.4f}   (shape frequencies alone {nll(np.where(outc[:, None] == 1, cw, cl), yte):.4f})')

    # 3. what the network says about spread, totals fixed
    for mname in ('multinomial', 'mlp 64'):
        pr, _ = preds[mname]
        base = np.median(X[:, :3].sum(1)) / 3
        print(f'\n{mname}: equal-strength sides (each boat {base:.2f}), X balanced vs lopsided (same total); probability of each winning set for X')
        print(f'{"X make-up":34s}  P(win)  1-2-3  1-2-4  1-2-5  1-2-6  1-3-4  1-3-5  1-3-6  1-4-5  2-3-4  2-3-5')
        for lab, d in (('balanced', 0.0), ('one star (+1.0, -0.5, -0.5)', 1.0), ('two strong, one weak (+.5,+.5,-1)', -1.0)):
            if d == 0:
                xa = [base] * 3
            elif d > 0:
                xa = [base + 1.0, base - 0.5, base - 0.5]
            else:
                xa = [base + 0.5, base + 0.5, base - 1.0]
            for lab2, xb in (('vs balanced', [base] * 3), ('vs one star', [base + 1.0, base - 0.5, base - 0.5])):
                row = np.array([xa + xb + [0, 0, 0, 0]], float)
                P = pr(feats(row))[0]
                print(f'{lab + " " + lab2:34s}  {P[WIN].sum():5.1%}  ' + '  '.join(f'{P[CIDX[c]]:5.1%}' for c in [(1,2,3),(1,2,4),(1,2,5),(1,2,6),(1,3,4),(1,3,5),(1,3,6),(1,4,5),(2,3,4),(2,3,5)]))


if __name__ == '__main__':
    main()
