#!/usr/bin/env python3
"""
regatta_form.py — are races within a regatta correlated beyond what ratings explain?

The rating model treats every race as an independent Plackett–Luce draw given each
sailor's rating.  If a sailor's form (or crew, boat, conditions fit, or simply a stale
rating) shifts for a whole regatta, races are correlated: beating your rating early
predicts beating it again later.  Model that as a per-sailor, per-regatta offset

    strength in every race of the regatta = rating + delta,   delta ~ Normal(0, tau^2)

Everything below uses championship regattas with ratings frozen the week before
(the cached fits evaluate_models.py wrote to eval/champ_<week>/), so no result leaks.

Tests:
  1. Split-half correlation.  Each sailor's standardised Plackett–Luce residual in the
     odd-numbered races vs. the even-numbered races of the same division.  Independent
     races give ~0.  The same statistic is computed on simulated regattas (same fleets,
     same race counts) for a grid of tau; the tau whose simulated correlation matches the
     real one is the moment estimate.
  2. In-regatta updating.  Before each race, estimate every sailor's delta from their
     earlier races in the regatta (one-step posterior mean, delta = G / (H + 1/tau^2))
     and predict the race's pairwise order from rating + delta.  tau is tuned on the
     older championships and scored on the newer ones.
  3. Race-1 winners (and last-place finishers): their later finishes vs. the rating-only
     prediction and the prediction updated after race 1.
  4. Carry-over.  The same residual in the sailor's next regatta within four weeks
     (still using the frozen ratings): how much of the effect is a stale rating rather
     than form that stays at the regatta.
  5. Per-sailor calibration of "this good or better" by tau; the tau closest to honest on the
     older championships is saved as sim_tau.
  6. Race-by-race correlation.  Each sailor's result against their rating in every race
     (expected place minus actual place, as a share of the fleet), correlated between race i and
     race j of the same division, for real results and for simulated regattas with independent
     races (tau 0) and with one form offset per regatta (tau = the moment estimate).  Averaged by
     how many races apart the pair is, this shows whether nearby races are more alike than
     distant ones.
  8. Correlation model.  Offsets for sailor s in race k (0-based within the division) of
         sa * A_s  +  sd * D_{s,k}  +  sb * B_{boat(s,k)},   D AR(1) over races with coefficient rho,
     A, D, B standard normal; B belongs to the physical boat (sail number) and is shared by every
     team that sails it.  sa, sd, rho, sb are fitted so simulated correlations match the real ones
     for every pair of races (race i, race j, same boat or not), weighted by pairs.  Saved as
     "components"; championship_points.py and regatta_analysis.py simulate with them.
  9. Per-sailor calibration of the correlation model at several scales (as in 5).
 10. Why team totals wanted smaller offsets than individual sailors.  (a) Rating spread: frozen
     ratings are compressed, so the pairwise likelihood of rating * k is maximised above 1, at
     championships and at other regattas in the same weeks; k for championships is saved as
     champ_spread.  (b) Offsets measure how uncertain each rating is, so their size depends on
     races sailed before the event: per-sailor calibration by experience group, sizes chosen on
     the older championships with stretched ratings and checked on the newer ones, saved as
     experience.table (multipliers on the sailor offsets; the boat term is unchanged).
  7. Same boat.  Using rotations (sail number per race), the same correlation for race pairs sailed
     in the same boat vs. different boats at each distance.

Output: eval/regatta_form.json

Usage:
    python3 analysis/plackett_luce/regatta_form.py 2>&1 | tee analysis/plackett_luce/regatta_form.log
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import evaluate_models as em  # noqa: E402
import newcomer_prior as npri  # noqa: E402

OUT = em.EVAL_DIR / "regatta_form.json"
TAU_GRID = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0, 1.2]
SIM_REPS = 12
TUNE_SEASONS = {"f24", "s25"}
CARRY_WEEKS = 4
PIT_SIMS = 1000
EXP_GROUPS = [(0, "newcomer"), (60, "1-60 races"), (200, "61-200 races"), (1e9, "200+ races")]
EXP_SCALES = [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.15, 1.3, 1.45, 1.6, 1.8, 2.0]
K_GRID = [round(x, 2) for x in np.arange(0.8, 1.61, 0.05)]
MAX_RACE = 12          # heatmap covers races 1..MAX_RACE of a division
LAG_SIM_REPS = 6


def gh(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Plackett–Luce score and observed information for each finisher's strength.
    x = strengths in finishing order (winner first).  Returns (g, h) in the same order."""
    w = np.exp(x - x.max())
    W = np.cumsum(w[::-1])[::-1]
    C = np.cumsum(1.0 / W)
    D = np.cumsum(1.0 / W ** 2)
    g = 1.0 - w * C
    h = w * C - w * w * D
    return g, np.maximum(h, 0.0)


def split_half_corr(groups, strengths_of, order_of) -> tuple[float, int]:
    """groups: list of race lists (one regatta division, races in race order).
    order_of(race, gi, ri) -> sailor indices in finishing order; strengths_of(race) -> frozen strengths aligned."""
    z1, z2 = [], []
    for gi, races in enumerate(groups):
        acc = defaultdict(lambda: np.zeros(4))          # sailor -> [G_odd, H_odd, G_even, H_even]
        cnt = defaultdict(lambda: [0, 0])
        for ri, r in enumerate(races):
            order = order_of(r, gi, ri)
            g, h = gh(strengths_of(r, order))
            half = ri % 2
            for k, s in enumerate(order):
                a = acc[s]
                a[2 * half] += g[k]
                a[2 * half + 1] += h[k]
                cnt[s][half] += 1
        for s, a in acc.items():
            c = cnt[s]
            if c[0] >= 2 and c[1] >= 2 and a[1] > 1e-6 and a[3] > 1e-6:
                z1.append(a[0] / np.sqrt(a[1]))
                z2.append(a[2] / np.sqrt(a[3]))
    if len(z1) < 3:
        return float("nan"), len(z1)
    return float(np.corrcoef(z1, z2)[0, 1]), len(z1)


def main():
    t0 = time.perf_counter()
    res = json.loads((em.EVAL_DIR / "results.json").read_text(encoding="utf-8"))
    seasons = {f["season"] for f in res["forecast"]["folds"]}
    races, ids, id_to_idx = em.load_data()
    N = len(ids)
    all_seasons = sorted({r.season for r in races}, key=em.season_key)

    champ = [r for r in races if r.champ and r.season in seasons]
    weeks = sorted({r.wn for r in champ})
    snaps = em.snapshots_before(races, N, weeks)

    # Frozen strengths per cutoff week; unseen sailors start at their school's debut median,
    # else their league's (newcomer_prior.fill, the rule every evaluator shares).
    keys = npri.school_keys(ids)
    S_week: dict[int, np.ndarray] = {}
    for w in weeks:
        S = em.load_latest(em.EVAL_DIR / f"champ_{em.week_label(w)}", id_to_idx, N)
        st = snaps[w]
        season = next(r.season for r in champ if r.wn == w)
        S_week[w] = npri.fill(S, st["seen"], st["first_season"], st["first_league"], keys,
                              npri.previous_seasons(all_seasons, season))

    def S_for(r) -> np.ndarray:
        return S_week[r.wn]

    # Groups: one regatta division (combined regattas already share division labels per race).
    by_group: dict[tuple, list] = defaultdict(list)
    for r in champ:
        by_group[(r.url, r.division)].append(r)
    keys = sorted(by_group)
    groups = [sorted(by_group[k], key=lambda r: r.race_num) for k in keys]
    seasons_of = [g[0].season for g in groups]
    print(f"{len(groups)} championship divisions, {len(champ):,} races, {len(weeks)} cutoff weeks", flush=True)

    # ── 1. Split-half correlation, real and simulated ─────────────────────────
    real_corr, n_pairs = split_half_corr(groups, lambda r, o: S_for(r)[o], lambda r, gi, ri: r.idx)
    print(f"\n1. Split-half residual correlation (odd vs even races): real {real_corr:+.3f} over {n_pairs:,} sailor-divisions")
    rng = np.random.default_rng(7)
    sim_corr = {}
    for tau in TAU_GRID:
        vals = []
        for rep in range(SIM_REPS):
            deltas = [defaultdict(lambda: None) for _ in groups]
            dcache: list[dict] = [dict() for _ in groups]

            def order_of(r, gi, ri, tau=tau, dcache=dcache):
                S = S_for(r)
                d = dcache[gi]
                dd = np.array([d.setdefault(int(s), rng.normal(0.0, tau) if tau > 0 else 0.0) for s in r.idx])
                key = S[r.idx] + dd + rng.gumbel(size=len(r.idx))
                return r.idx[np.argsort(-key, kind="stable")]

            c, _ = split_half_corr(groups, lambda r, o: S_for(r)[o], order_of)
            vals.append(c)
        sim_corr[tau] = (float(np.mean(vals)), float(np.std(vals) / np.sqrt(len(vals))))
        print(f"   simulated tau={tau:.1f}: {sim_corr[tau][0]:+.3f} ± {sim_corr[tau][1]:.3f}", flush=True)
    taus = np.array(TAU_GRID)
    means = np.array([sim_corr[t][0] for t in TAU_GRID])
    if real_corr <= means[0]:
        tau_moment = 0.0
    elif real_corr >= means[-1]:
        tau_moment = float(taus[-1])
    else:
        j = int(np.searchsorted(means, real_corr))
        j = max(1, min(j, len(taus) - 1))
        f = (real_corr - means[j - 1]) / max(means[j] - means[j - 1], 1e-9)
        tau_moment = float(taus[j - 1] + f * (taus[j] - taus[j - 1]))
    print(f"   moment estimate tau = {tau_moment:.2f}  (rating points: {tau_moment * 100:.0f})")

    # ── 2. In-regatta updating ────────────────────────────────────────────────
    ugrid = np.array([0.0, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5, 0.6, 0.8, 1.0])
    buckets = [(1, 1, "race 2"), (2, 3, "races 3-4"), (4, 7, "races 5-8"), (8, 10 ** 6, "race 9 on")]

    def blank():
        return {"pairs": 0, "acc": np.zeros(len(ugrid)), "ll": np.zeros(len(ugrid))}

    upd = {"tune": blank(), "test": blank()}
    upd_b = {b[2]: blank() for b in buckets}
    iu_cache: dict[int, tuple] = {}
    for gi, rl in enumerate(groups):
        part = "tune" if seasons_of[gi] in TUNE_SEASONS else "test"
        G = defaultdict(float)
        H = defaultdict(float)
        for ri, r in enumerate(rl):
            m = len(r.idx)
            S = S_for(r)
            base = S[r.idx]
            if ri > 0 and m >= 2:
                Gv = np.array([G[int(s)] for s in r.idx])
                Hv = np.array([H[int(s)] for s in r.idx])
                prec = np.where(ugrid > 0, 1.0 / np.maximum(ugrid, 1e-9) ** 2, np.inf)
                x = base[None, :] + Gv[None, :] / (Hv[None, :] + prec[:, None])   # [tau, m]
                if m not in iu_cache:
                    iu_cache[m] = np.triu_indices(m, 1)
                a, b = iu_cache[m]
                d = x[:, a] - x[:, b]                                               # winner-first order: a ahead of b
                acc = ((d > 0) + 0.5 * (d == 0)).sum(axis=1)
                ll = np.logaddexp(0.0, -d).sum(axis=1)
                for tgt in (upd[part],) + ((next(upd_b[lab] for lo, hi, lab in buckets if lo <= ri <= hi),) if part == "test" else ()):
                    tgt["pairs"] += len(a)
                    tgt["acc"] += acc
                    tgt["ll"] += ll
            g, h = gh(base)
            for k, s in enumerate(r.idx):
                G[int(s)] += g[k]
                H[int(s)] += h[k]

    tune_ll = upd["tune"]["ll"] / max(upd["tune"]["pairs"], 1)
    best = int(np.argmin(tune_ll))
    tau_update = float(ugrid[best])
    print(f"\n2. In-regatta updating (pairwise order of races 2+, strengths = rating + estimate from earlier races)")
    for part in ("tune", "test"):
        u = upd[part]
        print(f"   {part} ({'f24, s25' if part == 'tune' else 'f25, s26'}; {u['pairs']:,} pairs)")
        for j, t in enumerate(ugrid):
            mark = "  <- tuned" if j == best else ""
            print(f"     tau={t:.2f}  acc {u['acc'][j] / u['pairs'] * 100:6.2f}%  log-loss {u['ll'][j] / u['pairs']:.4f}{mark}")
    print("   test by race number (tau 0 vs tuned):")
    for _lo, _hi, lab in buckets:
        u = upd_b[lab]
        if u["pairs"]:
            print(f"     {lab:<10} {u['pairs']:>8,} pairs  acc {u['acc'][0] / u['pairs'] * 100:.2f}% -> {u['acc'][best] / u['pairs'] * 100:.2f}%"
                  f"   log-loss {u['ll'][0] / u['pairs']:.4f} -> {u['ll'][best] / u['pairs']:.4f}")

    # ── 3. Race-1 winners and last-place finishers ────────────────────────────
    tau_u = max(tau_update, 1e-9)
    prec_u = 1.0 / tau_u ** 2 if tau_update > 0 else np.inf
    r1 = {"won race 1": [], "last in race 1": []}
    for rl in groups:
        if len(rl) < 6 or len(rl[0].idx) < 6:
            continue
        first = rl[0]
        S = S_for(first)
        g, h = gh(S[first.idx])
        delta = {int(s): g[k] / (h[k] + prec_u) for k, s in enumerate(first.idx)}
        for label, sailor in (("won race 1", int(first.idx[0])), ("last in race 1", int(first.idx[-1]))):
            act, pred0, pred1, n = 0.0, 0.0, 0.0, 0
            for r in rl[1:]:
                pos = np.where(r.idx == sailor)[0]
                if not len(pos):
                    continue
                others = r.idx[r.idx != sailor]
                x0 = S[r.idx]
                xi = S[sailor]
                pred0 += 1.0 + float((1.0 / (1.0 + np.exp(-(S[others] - xi)))).sum())
                xi1 = xi + delta.get(sailor, 0.0)
                xo1 = S[others] + np.array([delta.get(int(o), 0.0) for o in others])
                pred1 += 1.0 + float((1.0 / (1.0 + np.exp(-(xo1 - xi1)))).sum())
                act += float(pos[0] + 1)
                n += 1
                del x0
            if n >= 4:
                r1[label].append((act / n, pred0 / n, pred1 / n, n))
    r1_out = {}
    print(f"\n3. Later races of sailors who won or were last in race 1 (divisions with 6+ races and boats; tau {tau_update:.2f})")
    for label, rows in r1.items():
        a = np.array(rows)
        if not len(a):
            continue
        r1_out[label] = {"sailors": len(a), "actual": round(float(a[:, 0].mean()), 3),
                         "rating_only": round(float(a[:, 1].mean()), 3), "updated": round(float(a[:, 2].mean()), 3),
                         "races": int(a[:, 3].sum())}
        print(f"   {label:<15} {len(a):4d} sailors: average later finish {a[:, 0].mean():.2f}, rating predicts {a[:, 1].mean():.2f}, "
              f"after updating on race 1 {a[:, 2].mean():.2f}")

    # ── 4. Carry-over into the next regatta ───────────────────────────────────
    later_by_week: dict[int, dict[int, tuple]] = {}
    z_this, z_next = [], []
    races_by_wn: dict[int, list] = defaultdict(list)
    for r in races:
        races_by_wn[r.wn].append(r)
    for w in weeks:
        S_icsa = S_issa = S_week[w]     # one filled array now serves both leagues
        nxt: dict[int, np.ndarray] = {}
        nxt_week: dict[int, int] = {}
        for wn in range(w + 1, w + CARRY_WEEKS + 1):
            grp = defaultdict(lambda: np.zeros(2))
            for r in races_by_wn.get(wn, []):
                S = S_icsa if r.league == "ICSA" else S_issa
                g, h = gh(S[r.idx])
                for k, s in enumerate(r.idx):
                    s = int(s)
                    if s in nxt_week and nxt_week[s] < wn:
                        continue
                    a = nxt.setdefault(s, np.zeros(2))
                    a[0] += g[k]
                    a[1] += h[k]
                    nxt_week[s] = wn
        this = defaultdict(lambda: np.zeros(2))
        for rl in groups:
            if rl[0].wn != w:
                continue
            for r in rl:
                g, h = gh(S_for(r)[r.idx])
                for k, s in enumerate(r.idx):
                    this[int(s)] += (g[k], h[k])
        for s, a in this.items():
            b = nxt.get(s)
            if b is not None and a[1] > 1e-6 and b[1] > 1e-6:
                z_this.append(a[0] / np.sqrt(a[1]))
                z_next.append(b[0] / np.sqrt(b[1]))
    carry = float(np.corrcoef(z_this, z_next)[0, 1]) if len(z_this) > 2 else float("nan")
    print(f"\n4. Residual at the championship vs. the sailor's next regatta within {CARRY_WEEKS} weeks: r = {carry:+.3f} "
          f"({len(z_this):,} sailors)")

    # ── 5. Per-sailor "this good or better" calibration ───────────────────────
    # Each skipper's total finishing places over the races they finished, against simulations of the same
    # fleets with every sailor's form drawn once per regatta.  Mid-p (ties count half) should be uniform:
    # about 10% of sailors below 0.10 and 10% above 0.90 when tau is right.
    pit_grid = [0.0, 0.2, 0.3, 0.4, 0.5, 0.6]
    pit = {t: {"tune": [], "test": []} for t in pit_grid}
    rng5 = np.random.default_rng(11)
    for gi, rl in enumerate(groups):
        part = "tune" if seasons_of[gi] in TUNE_SEASONS else "test"
        sailors = sorted({int(s) for r in rl for s in r.idx})
        col = {s: j for j, s in enumerate(sailors)}
        actual = np.zeros(len(sailors))
        nrace = np.zeros(len(sailors), dtype=int)
        gumbels = [rng5.gumbel(size=(PIT_SIMS, len(r.idx))) for r in rl]
        z = rng5.standard_normal(size=(PIT_SIMS, len(sailors)))
        for r in rl:
            for k, s in enumerate(r.idx):
                actual[col[int(s)]] += k + 1
                nrace[col[int(s)]] += 1
        keep = nrace >= 3
        if not keep.any():
            continue
        for t in pit_grid:
            tot = np.zeros((PIT_SIMS, len(sailors)))
            for r, gmb in zip(rl, gumbels):
                cols = np.array([col[int(s)] for s in r.idx])
                key = S_for(r)[r.idx][None, :] + t * z[:, cols] + gmb
                order = np.argsort(-key, axis=1, kind="stable")
                places = np.empty_like(order)
                np.put_along_axis(places, order, np.broadcast_to(np.arange(1, len(r.idx) + 1), order.shape), axis=1)
                tot[:, cols] += places
            below = (tot < actual[None, :]).mean(axis=0)
            equal = (tot == actual[None, :]).mean(axis=0)
            pit[t][part].extend((below + 0.5 * equal)[keep].tolist())
    print("\n5. Per-sailor chance of scoring this well or better (mid-p; honest: 10% below 0.10, 10% above 0.90)")
    pit_out = {}
    for t in pit_grid:
        pit_out[f"{t:g}"] = {}
        line = f"   tau={t:.1f}"
        for part in ("tune", "test"):
            v = np.array(pit[t][part])
            lo, hi = float((v < 0.10).mean()), float((v > 0.90).mean())
            pit_out[f"{t:g}"][part] = {"sailors": len(v), "below_10": round(lo, 4), "above_90": round(hi, 4)}
            line += f"   {part}: {lo * 100:5.1f}% below, {hi * 100:5.1f}% above ({len(v):,})"
        print(line)
    tau_sim = min(pit_grid, key=lambda t: abs(pit_out[f"{t:g}"]["tune"]["below_10"] - 0.10)
                  + abs(pit_out[f"{t:g}"]["tune"]["above_90"] - 0.10))
    print(f"   simulation tau (closest to honest on {', '.join(sorted(TUNE_SEASONS))}): {tau_sim:.1f}")

    # ── 6. Race-by-race correlation ──────────────────────────────────────────
    def place_resid(S, order):
        """(expected place - actual place) / (m - 1) for boats in finishing order: + means better than rated."""
        x = S[order]
        m = len(x)
        if m < 3:
            return None
        diff = x[None, :] - x[:, None]                      # [i, j] = x_j - x_i
        p_ahead = 1.0 / (1.0 + np.exp(-diff))
        np.fill_diagonal(p_ahead, 0.0)
        exp_place = 1.0 + p_ahead.sum(axis=1)
        return (exp_place - np.arange(1, m + 1)) / (m - 1)

    def lag_stats(order_fn):
        K = MAX_RACE
        mom = np.zeros((K, K, 6))                           # n, sx, sy, sxx, syy, sxy for race i (x) vs race j (y)
        for gi, rl in enumerate(groups):
            per = defaultdict(dict)
            for ri, r in enumerate(rl[:K]):
                order = order_fn(r, gi, ri)
                res_ = place_resid(S_for(r), order)
                if res_ is None:
                    continue
                for k, sl in enumerate(order):
                    per[int(sl)][ri] = res_[k]
            for d in per.values():
                items = sorted(d.items())
                if len(items) < 2:
                    continue
                idx_ = np.array([i for i, _ in items])
                val = np.array([v for _, v in items])
                I, J = np.meshgrid(idx_, idx_, indexing="ij")
                X, Y = np.meshgrid(val, val, indexing="ij")
                off = I != J
                np.add.at(mom, (I[off], J[off], 0), 1.0)
                np.add.at(mom, (I[off], J[off], 1), X[off])
                np.add.at(mom, (I[off], J[off], 2), Y[off])
                np.add.at(mom, (I[off], J[off], 3), X[off] ** 2)
                np.add.at(mom, (I[off], J[off], 4), Y[off] ** 2)
                np.add.at(mom, (I[off], J[off], 5), X[off] * Y[off])
        return mom

    def corr_from(m_):
        n, sx, sy, sxx, syy, sxy = (m_[..., k] for k in range(6))
        with np.errstate(invalid="ignore", divide="ignore"):
            cov = sxy / n - (sx / n) * (sy / n)
            vx = sxx / n - (sx / n) ** 2
            vy = syy / n - (sy / n) ** 2
            return cov / np.sqrt(vx * vy)

    def lag_curve(mom):
        K = mom.shape[0]
        out_ = []
        for L in range(1, K):
            agg = np.zeros(6)
            for i in range(K - L):
                agg += mom[i, i + L] + mom[i + L, i]
            out_.append((L, float(corr_from(agg)), int(agg[0] / 2)))
        return out_

    rng6 = np.random.default_rng(17)
    mom_real = lag_stats(lambda r, gi, ri: r.idx)
    sims6 = {}
    for label, tau6 in (("independent", 0.0), ("regatta_form", tau_moment)):
        acc6 = np.zeros_like(mom_real)
        for _rep in range(LAG_SIM_REPS):
            forms = [dict() for _ in groups]

            def order6(r, gi, ri, tau6=tau6, forms=forms):
                S = S_for(r)
                f = forms[gi]
                dd = np.array([f.setdefault(int(sl), rng6.normal(0.0, tau6) if tau6 > 0 else 0.0) for sl in r.idx])
                key = S[r.idx] + dd + rng6.gumbel(size=len(r.idx))
                return r.idx[np.argsort(-key, kind="stable")]

            acc6 += lag_stats(order6)
        sims6[label] = acc6
    heat = {"real": corr_from(mom_real), **{k: corr_from(v) for k, v in sims6.items()}}
    curves = {"real": lag_curve(mom_real), **{k: lag_curve(v) for k, v in sims6.items()}}
    pairs_n = mom_real[..., 0]
    print(f"\n6. Correlation of result-vs-rating between two races of the same division (races 1-{MAX_RACE})")
    print(f"   {'races apart':>11} {'real':>7} {'indep.':>7} {'form':>7} {'pairs':>8}")
    for (L, c, n), (_, ci, _), (_, cf, _) in zip(curves["real"], curves["independent"], curves["regatta_form"]):
        print(f"   {L:>11} {c:+7.3f} {ci:+7.3f} {cf:+7.3f} {n:>8,}")
    K = MAX_RACE
    off = ~np.eye(K, dtype=bool)
    print(f"   average over all race pairs: real {np.nanmean(heat['real'][off]):+.3f}, independent {np.nanmean(heat['independent'][off]):+.3f}, "
          f"form tau {tau_moment:.2f} {np.nanmean(heat['regatta_form'][off]):+.3f}")
    print("   real matrix (rows race i, columns race j):")
    for i in range(K):
        print("   " + " ".join("  .  " if i == j else f"{heat['real'][i, j]:+.2f}" for j in range(K)) + f"   n~{int(pairs_n[i].max()):,}")

    # ── 7. Same boat or not ───────────────────────────────────────────────────
    # Rotations record each team's sail number per race.  If boats differ in speed, two races in the same
    # boat should be more alike than two races the same distance apart in different boats.
    import sqlite3
    sys.path.insert(0, str(HERE.parent))
    from regatta_analysis import sailor_id
    dbs = {"ICSA": em.BASE / "db" / "icsa.db", "ISSA": em.BASE / "db" / "issa.db"}
    urls_by_lg: dict[str, set] = defaultdict(set)
    for rl in groups:
        urls_by_lg[rl[0].league].add(rl[0].url)
    boat_of: dict[tuple, int] = {}
    for lg, urls in urls_by_lg.items():
        con = sqlite3.connect(dbs[lg])
        for url in urls:
            rot = {(slug, team or "", div, int(rn)): sn for slug, team, div, rn, sn in con.execute(
                "SELECT school_slug, team, division, race_num, sail_num FROM rotations WHERE regatta_url=?", (url,))}
            if not rot:
                continue
            for div, rn, slug, team, sk, gr in con.execute(
                    "SELECT division, race_num, school_slug, team, skipper, skipper_grade FROM fleet_results "
                    "WHERE regatta_url=? AND (keelboat IS NULL OR keelboat=0)", (url,)):
                if not sk or not gr:
                    continue
                i = id_to_idx.get(sailor_id(lg, sk.strip(), slug, gr.strip()))
                sn = rot.get((slug, team or "", div, int(rn)))
                if i is not None and sn is not None:
                    boat_of[(url, div, int(rn), i)] = sn
        con.close()
    bmom: dict[tuple, np.ndarray] = defaultdict(lambda: np.zeros(6))
    covered = total = 0
    for rl in groups:
        per = defaultdict(dict)
        for ri, r in enumerate(rl[:MAX_RACE]):
            res_ = place_resid(S_for(r), r.idx)
            if res_ is None:
                continue
            for k, sl in enumerate(r.idx):
                total += 1
                sn = boat_of.get((r.url, r.division, r.race_num, int(sl)))
                if sn is not None:
                    covered += 1
                    per[int(sl)][ri] = (res_[k], sn)
        for d in per.values():
            items = sorted(d.items())
            for a in range(len(items)):
                for b in range(a + 1, len(items)):
                    (i, (x, bx)), (j, (y, by)) = items[a], items[b]
                    key = (j - i, bx == by, i // 2 == j // 2)
                    for xx, yy in ((x, y), (y, x)):
                        bmom[key] += (1.0, xx, yy, xx * xx, yy * yy, xx * yy)

    def pooled(pred):
        agg = np.zeros(6)
        for k, v in bmom.items():
            if pred(k):
                agg += v
        return (float(corr_from(agg)) if agg[0] > 20 else None), int(agg[0] / 2)

    boat_lag = []
    print(f"\n7. Same boat vs different boat (rotations cover {covered / max(total, 1) * 100:.0f}% of championship finishes)")
    print(f"   {'races apart':>11} {'same boat':>10} {'pairs':>8} {'diff boat':>10} {'pairs':>8}")
    for L in range(1, MAX_RACE):
        rs, ns = pooled(lambda k, L=L: k[0] == L and k[1])
        rd, nd = pooled(lambda k, L=L: k[0] == L and not k[1])
        boat_lag.append({"lag": L, "same_r": None if rs is None else round(rs, 4), "same_pairs": ns,
                         "diff_r": None if rd is None else round(rd, 4), "diff_pairs": nd})
        print(f"   {L:>11} {'' if rs is None else f'{rs:+.3f}':>10} {ns:>8,} {'' if rd is None else f'{rd:+.3f}':>10} {nd:>8,}")
    lag1 = {}
    print("   back-to-back races, by same two-race block and same boat:")
    for same_block in (True, False):
        for same_boat in (True, False):
            r_, n_ = pooled(lambda k, sb=same_block, bt=same_boat: k[0] == 1 and k[2] == sb and k[1] == bt)
            lag1[f"{'same' if same_block else 'across'}_block_{'same' if same_boat else 'diff'}_boat"] = {
                "r": None if r_ is None else round(r_, 4), "pairs": n_}
            print(f"     {'same block' if same_block else 'across blocks':<14} {'same boat' if same_boat else 'different boat':<15} "
                  f"{'' if r_ is None else f'{r_:+.3f}':>7} ({n_:,} pairs)")

    # ── 8. Fit the correlation model ─────────────────────────────────────────
    from scipy.optimize import minimize
    K = MAX_RACE
    rng8 = np.random.default_rng(23)
    FIT_REPS = 3
    fit_groups = []
    for rl in groups:
        rl = rl[:K]
        sailors = sorted({int(sl) for r in rl for sl in r.idx})
        col = {sl: j for j, sl in enumerate(sailors)}
        boats: dict = {}
        rows = []
        Bm = np.full((len(sailors), K), -1, dtype=np.int64)
        for ri, r in enumerate(rl):
            if len(r.idx) < 3:
                continue
            cols = np.array([col[int(sl)] for sl in r.idx])
            bk = []
            for sl in r.idx:
                sn = boat_of.get((r.url, r.division, r.race_num, int(sl)))
                key = ("sail", sn) if sn is not None else ("none", int(sl), ri)
                bk.append(boats.setdefault(key, len(boats)))
                if sn is not None:
                    Bm[col[int(sl)], ri] = boats[key]
            rows.append((ri, cols, np.array(bk), S_for(r)[r.idx], rng8.gumbel(size=(FIT_REPS, len(r.idx)))))
        fit_groups.append({"n": len(sailors), "rows": rows, "Bm": Bm, "sailors": np.array(sailors), "wn": rl[0].wn,
                           "za": rng8.standard_normal((FIT_REPS, len(sailors))),
                           "zd": rng8.standard_normal((FIT_REPS, K, len(sailors))),
                           "zb": rng8.standard_normal((FIT_REPS, max(len(boats), 1)))})

    def split_moments(params=None):
        tot = np.zeros((2, 6, K, K))              # [same boat?][n, sx, sy, sxx, syy, sxy][i, j]
        for g in fit_groups:
            same = (g["Bm"][:, :, None] == g["Bm"][:, None, :]) & (g["Bm"][:, :, None] >= 0)
            reps = range(1) if params is None else range(FIT_REPS)
            for rep_ in reps:
                X = np.zeros((g["n"], K)); M = np.zeros((g["n"], K))
                if params is not None:
                    sa, sd, rho, sb = params
                    D = np.empty((K, g["n"])); D[0] = g["zd"][rep_, 0]
                    for kk in range(1, K):
                        D[kk] = rho * D[kk - 1] + np.sqrt(max(1.0 - rho * rho, 0.0)) * g["zd"][rep_, kk]
                for ri, cols, bk, sv, gum in g["rows"]:
                    if params is None:
                        order = np.arange(len(cols))
                    else:
                        offv = sa * g["za"][rep_, cols] + sd * D[ri, cols] + sb * g["zb"][rep_, bk]
                        order = np.argsort(-(sv + offv + gum[rep_]), kind="stable")
                    X[cols[order], ri] = place_resid_arr(sv[order]); M[cols[order], ri] = 1.0
                for flag, mask in ((1, same), (0, ~same)):
                    mm = mask.astype(float)
                    tot[flag, 0] += np.einsum("si,sj,sij->ij", M, M, mm)
                    tot[flag, 1] += np.einsum("si,sj,sij->ij", X, M, mm)
                    tot[flag, 2] += np.einsum("si,sj,sij->ij", M, X, mm)
                    tot[flag, 3] += np.einsum("si,sj,sij->ij", X * X, M, mm)
                    tot[flag, 4] += np.einsum("si,sj,sij->ij", M, X * X, mm)
                    tot[flag, 5] += np.einsum("si,sj,sij->ij", X, X, mm)
        n, sx, sy, sxx, syy, sxy = (tot[:, k] for k in range(6))
        with np.errstate(invalid="ignore", divide="ignore"):
            c = (sxy / n - sx / n * sy / n) / np.sqrt((sxx / n - (sx / n) ** 2) * (syy / n - (sy / n) ** 2))
        return c, n

    def place_resid_arr(x):
        m = len(x)
        pa = 1.0 / (1.0 + np.exp(-(x[None, :] - x[:, None])))
        np.fill_diagonal(pa, 0.0)
        return (1.0 + pa.sum(axis=1) - np.arange(1, m + 1)) / (m - 1)

    real8, n8 = split_moments()
    offd = ~np.eye(K, dtype=bool)[None, :, :]
    w8 = np.where(offd & (n8 >= 30), n8, 0.0)

    def loss8(th):
        params = (abs(th[0]), abs(th[1]), float(np.clip(th[2], 0.0, 0.99)), abs(th[3]))
        c, _ = split_moments(params)
        return float(np.nansum(w8 * np.nan_to_num(c - real8) ** 2) / w8.sum()), c, params

    fits8 = {}
    print("\n8. Correlation model fit (regatta offset sa, drifting form sd with AR coefficient rho, boat sb)")
    for name, x0, free in (("regatta offset only", [0.55, 0.0, 0.8, 0.0], [0]),
                           ("regatta + drift", [0.45, 0.4, 0.8, 0.0], [0, 1, 2]),
                           ("regatta + drift + boat", [0.45, 0.4, 0.8, 0.3], [0, 1, 2, 3])):
        def fx(z, x0=x0, free=free):
            th = list(x0)
            for i, zi in zip(free, z):
                th[i] = zi
            return loss8(th)[0]
        r8 = minimize(fx, [x0[i] for i in free], method="Nelder-Mead",
                      options={"xatol": 0.01, "fatol": 1e-7, "maxfev": 160})
        th = list(x0)
        for i, zi in zip(free, r8.x):
            th[i] = zi
        v, c, params = loss8(th)
        sa, sd, rho, sb = params
        if 1 not in free:
            sd = 0.0
        if 3 not in free:
            sb = 0.0
        lag_same = [float(np.nanmean([c[1, i, i + L] for i in range(K - L)])) for L in range(1, K)]
        lag_diff = [float(np.nanmean([c[0, i, i + L] for i in range(K - L)])) for L in range(1, K)]
        fits8[name] = {"sa": round(sa, 4), "sd": round(sd, 4), "rho": round(rho, 4), "sb": round(sb, 4),
                       "weighted_sq_err": v, "by_lag_same_boat": [round(x, 4) for x in lag_same],
                       "by_lag_diff_boat": [round(x, 4) for x in lag_diff]}
        print(f"   {name:<24} sa {sa:.3f}  sd {sd:.3f}  rho {rho:.3f}  sb {sb:.3f}   weighted sq err {v:.3e}", flush=True)
    real_same = [float(np.nanmean([real8[1, i, i + L] for i in range(K - L) if n8[1, i, i + L] >= 30] or [np.nan])) for L in range(1, K)]
    real_diff = [float(np.nanmean([real8[0, i, i + L] for i in range(K - L)])) for L in range(1, K)]
    best8 = fits8["regatta + drift + boat"]
    print("   races apart:      " + " ".join(f"{L:>6}" for L in range(1, K)))
    print("   real, same boat:  " + " ".join(f"{x:+.3f}" for x in real_same))
    print("   model, same boat: " + " ".join(f"{x:+.3f}" for x in best8["by_lag_same_boat"]))
    print("   real, diff boat:  " + " ".join(f"{x:+.3f}" for x in real_diff))
    print("   model, diff boat: " + " ".join(f"{x:+.3f}" for x in best8["by_lag_diff_boat"]))

    # ── 9. Per-sailor calibration of the correlation model ───────────────────
    # Same test as section 5 (mid-p of each skipper's total places over the first MAX_RACE races they finished),
    # simulated with the fitted correlation model at several scales.
    SIM9 = 400
    scales9 = [0.0, 0.6, 0.8, 1.0]
    pit9 = {c: {"tune": [], "test": []} for c in scales9}
    rng9 = np.random.default_rng(29)
    for gi, g in enumerate(fit_groups):
        part = "tune" if seasons_of[gi] in TUNE_SEASONS else "test"
        if not g["rows"]:
            continue
        n = g["n"]
        nb = int(max(bk.max() for _ri, _c, bk, _s, _g in g["rows"])) + 1
        A = rng9.standard_normal((SIM9, n))
        Bz = rng9.standard_normal((SIM9, nb))
        Z = rng9.standard_normal((SIM9, K, n))
        D = np.empty_like(Z); D[:, 0] = Z[:, 0]
        rho9 = best8["rho"]
        for kk in range(1, K):
            D[:, kk] = rho9 * D[:, kk - 1] + np.sqrt(max(1 - rho9 * rho9, 0.0)) * Z[:, kk]
        gum = [rng9.gumbel(size=(SIM9, len(c_))) for _ri, c_, _bk, _s, _g in g["rows"]]
        actual = np.zeros(n); nr = np.zeros(n)
        for _ri, cols, _bk, _sv, _g in g["rows"]:
            actual[cols] += np.arange(1, len(cols) + 1)
            nr[cols] += 1
        keep = nr >= 3
        if not keep.any():
            continue
        for c in scales9:
            tot = np.zeros((SIM9, n))
            for (ri, cols, bk, sv, _g), gm in zip(g["rows"], gum):
                offv = c * (best8["sa"] * A[:, cols] + best8["sd"] * D[:, ri, cols] + best8["sb"] * Bz[:, bk])
                order = np.argsort(-(sv[None, :] + offv + gm), axis=1, kind="stable")
                places = np.empty_like(order)
                np.put_along_axis(places, order, np.broadcast_to(np.arange(1, len(cols) + 1), order.shape), axis=1)
                tot[:, cols] += places
            below = (tot < actual[None, :]).mean(axis=0)
            equal = (tot == actual[None, :]).mean(axis=0)
            pit9[c][part].extend((below + 0.5 * equal)[keep].tolist())
    pit9_out = {}
    print("\n9. Per-sailor calibration of the correlation model (mid-p; honest: 10% below 0.10, 10% above 0.90)")
    for c in scales9:
        pit9_out[f"{c:g}"] = {}
        line = f"   scale {c:.1f}"
        for part in ("tune", "test"):
            v = np.array(pit9[c][part])
            lo, hi = float((v < 0.10).mean()), float((v > 0.90).mean())
            pit9_out[f"{c:g}"][part] = {"sailors": len(v), "below_10": round(lo, 4), "above_90": round(hi, 4)}
            line += f"   {part}: {lo * 100:5.1f}% below, {hi * 100:5.1f}% above ({len(v):,})"
        print(line, flush=True)

    # ── 10. Rating spread and offset size by experience ──────────────────────
    def spread_fit(race_list):
        d = {"tune": [], "test": []}
        for r in race_list:
            sv = S_for(r)[r.idx]
            if len(sv) < 2:
                continue
            a, b = np.triu_indices(len(sv), 1)
            d["tune" if r.season in TUNE_SEASONS else "test"].append(sv[a] - sv[b])
        d = {k: np.concatenate(v) for k, v in d.items() if v}
        nll = lambda x, k: float(np.mean(np.logaddexp(0.0, -k * x)))
        k_best = min(K_GRID, key=lambda k: nll(d["tune"], k))
        return {"k": k_best, "pairs": int(sum(len(v) for v in d.values())),
                "test_log_loss_k1": round(nll(d["test"], 1.0), 5), "test_log_loss_k": round(nll(d["test"], k_best), 5)}

    week_set = set(weeks)
    champ_spread = spread_fit(champ)
    other_spread = spread_fit([r for r in races if r.wn in week_set and not r.champ and r.season in seasons])
    print(f"\n10. Rating spread (pairwise likelihood of rating x k; chosen on {', '.join(sorted(TUNE_SEASONS))})")
    for label, sp_ in (("championships", champ_spread), ("other regattas, same weeks", other_spread)):
        print(f"   {label:<28} k = {sp_['k']:.2f}   test log-loss {sp_['test_log_loss_k1']:.4f} -> {sp_['test_log_loss_k']:.4f}  ({sp_['pairs']:,} pairs)")
    ks = champ_spread["k"]
    n_before = {w: snaps[w]["n"] for w in weeks}
    rng10 = np.random.default_rng(31)
    SIM10 = 500
    pit10 = defaultdict(list)                     # (part, group, scale) -> mid-p
    for gi, g in enumerate(fit_groups):
        if not g["rows"]:
            continue
        part = "tune" if seasons_of[gi] in TUNE_SEASONS else "test"
        n = g["n"]
        nb = int(max(bk.max() for _ri, _c, bk, _s, _g in g["rows"])) + 1
        A = rng10.standard_normal((SIM10, n)); Bz = rng10.standard_normal((SIM10, nb)); Z = rng10.standard_normal((SIM10, K, n))
        D = np.empty_like(Z); D[:, 0] = Z[:, 0]
        for kk in range(1, K):
            D[:, kk] = best8["rho"] * D[:, kk - 1] + np.sqrt(max(1 - best8["rho"] ** 2, 0.0)) * Z[:, kk]
        gum = [rng10.gumbel(size=(SIM10, len(c_))) for _ri, c_, _bk, _s, _g in g["rows"]]
        actual = np.zeros(n); nr = np.zeros(n)
        for _ri, cols, _bk, _sv, _g in g["rows"]:
            actual[cols] += np.arange(1, len(cols) + 1); nr[cols] += 1
        exp_n = n_before[g["wn"]][g["sailors"]]
        group_of = [next(lab for hi, lab in EXP_GROUPS if e <= hi) for e in exp_n]
        keep = nr >= 3
        for c in EXP_SCALES:
            tot = np.zeros((SIM10, n))
            for (ri, cols, bk, sv, _g), gm in zip(g["rows"], gum):
                offv = c * (best8["sa"] * A[:, cols] + best8["sd"] * D[:, ri, cols]) + best8["sb"] * Bz[:, bk]
                order = np.argsort(-(ks * sv[None, :] + offv + gm), axis=1, kind="stable")
                places = np.empty_like(order)
                np.put_along_axis(places, order, np.broadcast_to(np.arange(1, len(cols) + 1), order.shape), axis=1)
                tot[:, cols] += places
            mid = (tot < actual[None, :]).mean(axis=0) + 0.5 * (tot == actual[None, :]).mean(axis=0)
            for j in np.where(keep)[0]:
                pit10[(part, group_of[j], c)].append(mid[j])

    def tails(v):
        v = np.array(v)
        return (float((v < 0.1).mean()), float((v > 0.9).mean())) if len(v) else (float("nan"), float("nan"))

    exp_table, exp_rows = [], []
    print(f"   offset size by experience, ratings x{ks:.2f} (tails below 10% / above 90%; honest 10/10)")
    for hi, lab in EXP_GROUPS:
        best_c = min(EXP_SCALES, key=lambda c: sum(abs(t - 0.1) for t in tails(pit10[("tune", lab, c)])))
        lo_t, hi_t = tails(pit10[("test", lab, best_c)])
        lo_1, hi_1 = tails(pit10[("test", lab, 1.0)])
        exp_table.append({"max_races": hi, "label": lab, "multiplier": best_c})
        exp_rows.append({"label": lab, "multiplier": best_c, "test_sailors": len(pit10[("test", lab, best_c)]),
                         "test_tails": [round(lo_t, 4), round(hi_t, 4)], "test_tails_at_1": [round(lo_1, 4), round(hi_1, 4)]})
        print(f"   {lab:<14} size {best_c:.2f}   test {lo_t * 100:4.1f}/{hi_t * 100:4.1f}   (at 1.0: {lo_1 * 100:4.1f}/{hi_1 * 100:4.1f})"
              f"   {len(pit10[('test', lab, best_c)]):,} test sailors", flush=True)

    out = {"generated": datetime.now().isoformat(timespec="seconds"),
           "runtime_s": round(time.perf_counter() - t0),
           "divisions": len(groups), "races": len(champ),
           "split_half": {"real": round(real_corr, 4), "sailor_divisions": n_pairs,
                          "simulated": {f"{t:g}": [round(m, 4), round(se, 4)] for t, (m, se) in sim_corr.items()},
                          "tau_moment": round(tau_moment, 3)},
           "updating": {"grid": ugrid.tolist(), "tau_tuned": tau_update,
                        **{p: {"pairs": upd[p]["pairs"], "acc": (upd[p]["acc"] / max(upd[p]["pairs"], 1)).round(5).tolist(),
                               "log_loss": (upd[p]["ll"] / max(upd[p]["pairs"], 1)).round(5).tolist()} for p in upd},
                        "test_by_race": {lab: {"pairs": upd_b[lab]["pairs"],
                                               "acc": [round(float(upd_b[lab]["acc"][j] / max(upd_b[lab]["pairs"], 1)), 5) for j in (0, best)],
                                               "log_loss": [round(float(upd_b[lab]["ll"][j] / max(upd_b[lab]["pairs"], 1)), 5) for j in (0, best)]}
                                         for _lo, _hi, lab in buckets}},
           "race1": r1_out,
           "carry_over": {"r": round(carry, 4), "sailors": len(z_this), "weeks": CARRY_WEEKS},
           "sailor_calibration": pit_out,
           "sim_tau": tau_sim,
           "race_correlation": {"max_race": MAX_RACE, "form_tau": round(tau_moment, 3),
                                "heatmap": {k: [[None if (i == j or not np.isfinite(v[i, j])) else round(float(v[i, j]), 4)
                                                 for j in range(MAX_RACE)] for i in range(MAX_RACE)] for k, v in heat.items()},
                                "pairs": [[int(pairs_n[i, j]) for j in range(MAX_RACE)] for i in range(MAX_RACE)],
                                "by_lag": {k: [{"lag": L, "r": round(c, 4), "pairs": n} for L, c, n in v] for k, v in curves.items()}},
           "boat": {"coverage": round(covered / max(total, 1), 4), "by_lag": boat_lag, "back_to_back": lag1},
           "components": {**{k: best8[k] for k in ("sa", "sd", "rho", "sb")}, "fits": fits8,
                          "real_by_lag_same_boat": [round(x, 4) for x in real_same],
                          "real_by_lag_diff_boat": [round(x, 4) for x in real_diff],
                          "sailor_calibration": pit9_out},
           "champ_spread": champ_spread, "other_spread": other_spread,
           "experience": {"spread_used": ks, "table": exp_table, "check": exp_rows}}
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\nWritten: {OUT} ({out['runtime_s']}s)")


if __name__ == "__main__":
    main()
