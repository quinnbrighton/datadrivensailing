#!/usr/bin/env python3
"""Team race prediction model: who wins a 3-on-3 match, given both lineups.

A side's strength is the sum over its three boats of what each boat brings, plus what the school brings:

    S = sum_boats [ k * main(skipper) + kc * crew_main(crew) + off_skipper + off_crew ] + off_school + off_school_season

    main(skipper)      the skipper's main (fleet) rating as of the match week; unrated skippers get a fitted level b0
    crew_main(crew)    the crew's main rating when they have one (people who also skipper), else 0 with a fitted level c0
    off_*              team-race-specific offsets, L2-shrunk toward 0 (a sailor with little team racing falls back on the main rating)
    off_school         the program's team-race strength beyond its sailors; off_school_season its drift this season

P(side 1 beats side 2) = sigmoid(S1 - S2).  Individual contributions are identified because lineups change between matches: the same
skipper sails with different team-mates, so the fit attributes the team's results to the people who were in the boats when they happened.

Optional auxiliary likelihood on the six-boat finishing order (positions like 1-3-6 vs 2-4-5): Plackett-Luce with per-boat strengths
k*main + off, weighted by `pos_weight`.  Team-race finishes are tactical, so this is tested, not assumed.

Match weights decay with age (half-life in years).  Fitted with L-BFGS in torch (GPU when available).
"""
from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
DEV = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


@dataclass
class Config:
    name: str = 'full'
    use_main: bool = True
    use_sailor: bool = True
    use_crew: bool = True
    use_school: bool = True
    use_school_season: bool = True
    use_crew_main: bool = False
    lam_sailor: float = 3.0
    lam_crew: float = 5.0
    lam_school: float = 3.0
    lam_school_season: float = 3.0
    half_life: float | None = 2.0
    pos_weight: float = 0.0
    use_home: bool = False   # home-water edge when a side's school hosts the regatta
    use_exp: bool = False    # team-race experience: log1p(prior matches) of the skippers and crews
    agg: str = 'sum'        # 'sum' of the three boats, or 'ordered': fitted weights on the best / middle / weakest boat
    use_combo: bool = False  # familiarity: log1p(prior matches of this skipper-crew pair) per boat and log1p(prior matches of this skipper trio)
    use_trio: bool = False   # an offset per skipper trio (the "combination" as a unit), L2-shrunk
    use_pair: bool = False   # an offset per skipper-crew pair
    lam_trio: float = 30.0
    lam_pair: float = 30.0
    iters: int = 300


def parse_pos(s: str) -> list[int] | None:
    nums = [int(x) for x in re.findall(r'\d+', s or '')]
    return nums[:3] if len(nums) >= 3 and all(1 <= n <= 6 for n in nums[:3]) else None


class Index:
    def __init__(self):
        self.map: dict = {}

    def __call__(self, key) -> int:
        if key not in self.map:
            self.map[key] = len(self.map)
        return self.map[key]

    def __len__(self):
        return len(self.map)


def pack(rows: list[dict], main_at, crew_at, now_week: int, cfg: Config, index: dict | None = None) -> dict:
    """Tensors for a list of match rows.  main_at(sid, week) -> rating or None; crew_at(sid) -> rating or None.
    When `index` is given (prediction), unknown sailors/schools map to the spare slot N (offset 0)."""
    fit = index is None
    idx = index or {'s': Index(), 'c': Index(), 'h': Index(), 'hs': Index(), 't': Index(), 'p': Index()}
    n = len(rows)
    skip = np.zeros((n, 2, 3), np.int64); crew = np.zeros((n, 2, 3), np.int64)
    sch = np.zeros((n, 2), np.int64); schs = np.zeros((n, 2), np.int64)
    trio = np.zeros((n, 2), np.int64); pair = np.zeros((n, 2, 3), np.int64)
    pexp = np.zeros((n, 2, 3), np.float32); texp = np.zeros((n, 2), np.float32)
    ms = np.zeros((n, 2, 3), np.float32); hasm = np.zeros((n, 2, 3), np.float32)
    mc = np.zeros((n, 2, 3), np.float32); hasc = np.zeros((n, 2, 3), np.float32)
    pos = np.zeros((n, 2, 3), np.int64); haspos = np.zeros(n, bool)
    home = np.zeros((n, 2), np.float32); exps = np.zeros((n, 2, 3), np.float32); expc = np.zeros((n, 2, 3), np.float32)
    w = np.ones(n, np.float32)

    def look(ix, key):
        if fit:
            return ix(key)
        return ix.map.get(key, len(ix))

    for i, r in enumerate(rows):
        week = int(r['week'])
        if cfg.half_life:
            w[i] = 0.5 ** ((now_week - week) / 53.0 / cfg.half_life)
        pw, pl = parse_pos(r['w_pos']), parse_pos(r['l_pos'])
        if pw and pl and sorted(pw + pl) == [1, 2, 3, 4, 5, 6]:
            haspos[i] = True
            pos[i, 0] = pw; pos[i, 1] = pl
        for side, pre in enumerate(('w_', 'l_')):
            sch[i, side] = look(idx['h'], (r['league'], r[pre + 'school']))
            home[i, side] = float(r.get(pre + 'home', 0) or 0)
            if r.get(pre + 'exp'):
                exps[i, side] = [math.log1p(int(x)) for x in r[pre + 'exp'].split(';')]
                expc[i, side] = [math.log1p(int(x)) for x in r[pre + 'expc'].split(';')]
            schs[i, side] = look(idx['hs'], (r['league'], r[pre + 'school'], r['season']))
            sks = r[pre + 'skippers'].split(';')
            trio[i, side] = look(idx['t'], (r['league'], r[pre + 'school']) + tuple(sorted(sks)))
            if r.get(pre + 'trioexp') is not None:
                texp[i, side] = math.log1p(int(r[pre + 'trioexp']))
                pexp[i, side] = [math.log1p(int(x)) for x in r[pre + 'pairexp'].split(';')]
            for j, (sid, cid) in enumerate(zip(sks, r[pre + 'crews'].split(';'))):
                skip[i, side, j] = look(idx['s'], sid)
                pair[i, side, j] = look(idx['p'], (sid, cid))
                m = main_at(sid, week - 1)
                if m is not None:
                    ms[i, side, j] = m; hasm[i, side, j] = 1.0
                crew[i, side, j] = look(idx['c'], cid)      # '' (no crew recorded) gets its own level
                cm = crew_at(cid, week - 1) if (crew_at and cid) else None
                if cm is not None:
                    mc[i, side, j] = cm; hasc[i, side, j] = 1.0
    T = lambda a, dt=None: torch.tensor(a, device=DEV, dtype=dt)  # noqa: E731
    return dict(idx=idx, n=n, skip=T(skip), crew=T(crew), sch=T(sch), schs=T(schs), ms=T(ms), hasm=T(hasm), mc=T(mc), hasc=T(hasc),
                pos=T(pos), haspos=T(haspos), w=T(w), home=T(home), exps=T(exps), expc=T(expc),
                trio=T(trio), pair=T(pair), pexp=T(pexp), texp=T(texp))


class Model:
    def __init__(self, cfg: Config, idx: dict):
        self.cfg = cfg
        self.idx = idx
        z = lambda n: torch.zeros(n + 1, device=DEV, requires_grad=True)  # noqa: E731  (+1 = spare slot for unknowns)
        self.off_s = z(len(idx['s'])); self.off_c = z(len(idx['c'])); self.off_h = z(len(idx['h'])); self.off_hs = z(len(idx['hs']))
        self.off_t = z(len(idx.get('t', ()))); self.off_p = z(len(idx.get('p', ())))
        self.g_combo = torch.zeros(2, device=DEV, requires_grad=True)   # pair familiarity, trio familiarity slopes
        self.k = torch.tensor(1.0, device=DEV, requires_grad=True)
        self.kc = torch.tensor(0.3, device=DEV, requires_grad=True)
        self.b0 = torch.tensor(0.0, device=DEV, requires_grad=True)     # level of an unrated skipper, relative to a rated one at 0
        self.c0 = torch.tensor(0.0, device=DEV, requires_grad=True)
        self.beta_pos = torch.tensor(0.5, device=DEV, requires_grad=True)
        self.ordw = torch.zeros(3, device=DEV, requires_grad=True)   # log-weights of the sorted boats (ordered aggregation)
        self.g_home = torch.tensor(0.0, device=DEV, requires_grad=True)
        self.g_exp = torch.zeros(2, device=DEV, requires_grad=True)     # skipper, crew experience slopes

    def params(self):
        p = []
        c = self.cfg
        if c.use_main:
            p += [self.k, self.b0]
        if c.use_crew_main:
            p += [self.kc, self.c0]
        if c.use_sailor:
            p.append(self.off_s)
        if c.use_crew:
            p.append(self.off_c)
        if c.use_school:
            p.append(self.off_h)
        if c.use_school_season:
            p.append(self.off_hs)
        if c.pos_weight > 0:
            p.append(self.beta_pos)
        if c.agg == 'ordered':
            p.append(self.ordw)
        if c.use_home:
            p.append(self.g_home)
        if c.use_exp:
            p.append(self.g_exp)
        if c.use_combo:
            p.append(self.g_combo)
        if c.use_trio:
            p.append(self.off_t)
        if c.use_pair:
            p.append(self.off_p)
        return p

    def boat_strength(self, d: dict, mask_spare: bool = True) -> torch.Tensor:
        """(n, 2, 3) strength of each boat."""
        c = self.cfg
        s = torch.zeros_like(d['ms'])
        if c.use_main:
            s = s + self.k * d['ms'] * d['hasm'] + self.b0 * (1 - d['hasm'])
        if c.use_crew_main:
            s = s + self.kc * d['mc'] * d['hasc'] + self.c0 * (1 - d['hasc'])
        if c.use_sailor:
            s = s + self.off_s[d['skip']]
        if c.use_crew:
            s = s + self.off_c[d['crew']]
        if c.use_exp:
            s = s + self.g_exp[0] * d['exps'] + self.g_exp[1] * d['expc']
        if c.use_combo:
            s = s + self.g_combo[0] * d['pexp']
        if c.use_pair:
            s = s + self.off_p[d['pair']]
        return s

    def side_strength(self, d: dict) -> torch.Tensor:
        """(n, 2)"""
        b = self.boat_strength(d)
        if self.cfg.agg == 'ordered':
            w = 3 * torch.softmax(self.ordw, 0)                     # mean weight 1, so the scale matches the plain sum
            s = (b.sort(-1, descending=True).values * w).sum(-1)
        else:
            s = b.sum(-1)
        if self.cfg.use_school:
            s = s + self.off_h[d['sch']]
        if self.cfg.use_school_season:
            s = s + self.off_hs[d['schs']]
        if self.cfg.use_home:
            s = s + self.g_home * d['home']
        if self.cfg.use_combo:
            s = s + self.g_combo[1] * d['texp']
        if self.cfg.use_trio:
            s = s + self.off_t[d['trio']]
        return s

    def prob_first_wins(self, d: dict) -> torch.Tensor:
        s = self.side_strength(d)
        return torch.sigmoid(s[:, 0] - s[:, 1])

    def nll(self, d: dict) -> torch.Tensor:
        s = self.side_strength(d)
        ll = torch.nn.functional.logsigmoid(s[:, 0] - s[:, 1])
        loss = -(ll * d['w']).sum()
        c = self.cfg
        if c.pos_weight > 0 and d['haspos'].any():
            b = self.beta_pos * self.boat_strength(d)                      # (n,2,3)
            m = d['haspos']
            st = torch.full((int(m.sum()), 6), 0.0, device=DEV)
            st.scatter_(1, (d['pos'][m] - 1).reshape(-1, 6), b[m].reshape(-1, 6))   # strength by finishing place
            tail = torch.logcumsumexp(st.flip(1), dim=1).flip(1)
            pl = (st - tail)[:, :5].sum(1)
            loss = loss - c.pos_weight * (pl * d['w'][m]).sum()
        loss = loss + 0.5 * c.lam_sailor * (self.off_s ** 2).sum() + 0.5 * c.lam_crew * (self.off_c ** 2).sum() \
            + 0.5 * c.lam_school * (self.off_h ** 2).sum() + 0.5 * c.lam_school_season * (self.off_hs ** 2).sum() \
            + 0.5 * c.lam_trio * (self.off_t ** 2).sum() + 0.5 * c.lam_pair * (self.off_p ** 2).sum()
        return loss

    def fit(self, d: dict) -> float:
        params = self.params()
        if not params:
            return 0.0
        opt = torch.optim.LBFGS(params, lr=1, max_iter=self.cfg.iters, history_size=20, line_search_fn='strong_wolfe', tolerance_grad=1e-6)

        def closure():
            opt.zero_grad()
            L = self.nll(d)
            L.backward()
            return L
        L = opt.step(closure)
        return float(L)

    @torch.no_grad()
    def predict(self, rows: list[dict], main_at, crew_at, now_week: int) -> np.ndarray:
        d = pack(rows, main_at, crew_at, now_week, self.cfg, index=self.idx)
        return self.prob_first_wins(d).cpu().numpy()


def load_matches(path: Path | None = None) -> list[dict]:
    with open(path or HERE / 'matches.csv', newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: (r['date'], r['regatta_url'], int(r['match_num'])))
    # team-race experience before each match (matches sailed so far, any role), per skipper and crew slot
    seen: dict = {}
    seen_pair: dict = {}; seen_trio: dict = {}      # familiarity: matches this skipper-crew pair / this skipper trio sailed before
    for r in rows:
        for pre in ('w_', 'l_'):
            sks = r[pre + 'skippers'].split(';'); crs = r[pre + 'crews'].split(';')
            r[pre + 'exp'] = ';'.join(str(seen.get(x, 0)) for x in sks)
            r[pre + 'expc'] = ';'.join(str(seen.get(x, 0)) if x else '0' for x in crs)
            r[pre + 'pairexp'] = ';'.join(str(seen_pair.get((s, c), 0)) if c else '0' for s, c in zip(sks, crs))
            r[pre + 'trioexp'] = str(seen_trio.get((r[pre + 'school'],) + tuple(sorted(sks)), 0))
        for pre in ('w_', 'l_'):
            sks = r[pre + 'skippers'].split(';'); crs = r[pre + 'crews'].split(';')
            for x in sks + crs:
                if x:
                    seen[x] = seen.get(x, 0) + 1
            for s, c in zip(sks, crs):
                if c:
                    seen_pair[(s, c)] = seen_pair.get((s, c), 0) + 1
            t = (r[pre + 'school'],) + tuple(sorted(sks))
            seen_trio[t] = seen_trio.get(t, 0) + 1
    # host school (home water) from the regatta lists + venue slugs
    base = HERE.parent.parent
    slug_of = {}
    with open(base / 'data' / 'regatta_venues.csv', newline='', encoding='utf-8') as f:
        for v in csv.DictReader(f):
            if v.get('slug'):
                slug_of[(v['league'], v['host'].strip())] = v['slug']
    host = {}
    for league, site in (('ICSA', 'icsa'), ('ISSA', 'issa')):
        with open(base / 'data' / f'{site}_regattas.csv', newline='', encoding='utf-8') as f:
            for v in csv.DictReader(f):
                u = v['url'].rstrip('/').split('.org', 1)[-1]
                host[(league, u)] = slug_of.get((league, v['host'].strip()), '')
    for r in rows:
        h = host.get((r['league'], r['regatta_url']), '')
        r['w_home'] = int(bool(h) and r['w_school'] == h)
        r['l_home'] = int(bool(h) and r['l_school'] == h)
    return rows


def logloss(p: np.ndarray, eps: float = 1e-6) -> float:
    return float(-np.mean(np.log(np.clip(p, eps, 1 - eps))))
