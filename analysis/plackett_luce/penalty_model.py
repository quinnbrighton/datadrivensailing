#!/usr/bin/env python3
"""
penalty_model.py — per-sailor penalty propensity, kept separate from the ratings.

The rating model only sees boats that finished (preprocess.py removes non-finishers
from each race's order), so an OCS or DNF never counts as losing to every finisher.
This module models those outcomes on their own.  For each skipper and category c,
the per-race probability is an empirical-Bayes estimate

    p_c = (k_c + a_c * base_c[league]) / (n + a_c)

k_c   the sailor's category-c penalties in races before the cutoff
n     races the sailor was entered in before the cutoff (finishes + penalties)
base  the league's category rate over the same races
a_c   shrinkage strength in pseudo-races, tuned forward on a held-out season

A sailor with no history gets the league base rate.

Categories (scraper.PENALTY_SCORES codes):
    start  OCS 992, BFD 993, UFD 994
    dnf    DNF 999, RAF 991, DGM 995
    dns    DNS 998, DNC 997

In a race of m boats a penalised boat scores m + 1.  With q_i = 1 - p_i the chance a
boat finishes, and finishers ordered by Plackett–Luce (a PL model restricted to any
subset of boats is still PL, so pairwise probabilities are unchanged):

    E[points_i] = p_i (m + 1) + q_i (1 + sum_{j != i} q_j * sigmoid(scale (s_j - s_i)))

Race objects passed in need .idx (finisher sailor indices in finishing order),
.pen_idx and .pen_code (penalised sailor indices and their codes) and .league.

CLI: write every sailor's current penalty scores
    python3 analysis/plackett_luce/penalty_model.py
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
from scipy.stats import rankdata

CATEGORIES = {"start": (992, 993, 994), "dnf": (999, 991, 995), "dns": (998, 997)}
CAT_ORDER = ["start", "dnf", "dns"]
CAT_INDEX = {c: i for i, c in enumerate(CAT_ORDER)}
CODE_CAT = {code: CAT_INDEX[c] for c, codes in CATEGORIES.items() for code in codes}
CAT_LABELS = {"start": "Start penalty (OCS, BFD, UFD)", "dnf": "Did not finish (DNF, RAF, DGM)",
              "dns": "Did not start (DNS, DNC)"}
LEAGUES = ("ICSA", "ISSA")
ALPHA_GRID = [1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0, 3000.0, 10000.0]
MAX_P_ANY = 0.95

HERE = Path(__file__).resolve().parent
OUT_CSV = HERE / "penalty_scores.csv"


def fit_counts(races, N: int) -> dict:
    """Per-sailor entries and category penalty counts, plus league base rates."""
    n = np.zeros(N)
    k = np.zeros((len(CAT_ORDER), N))
    entries = {lg: 0.0 for lg in LEAGUES}
    pens = {lg: np.zeros(len(CAT_ORDER)) for lg in LEAGUES}
    fin_parts, pen_parts, cat_parts = [], [], []
    for r in races:
        fin_parts.append(r.idx)
        entries[r.league] = entries.get(r.league, 0.0) + len(r.idx) + len(r.pen_idx)
        if len(r.pen_idx):
            cats = np.array([CODE_CAT[int(c)] for c in r.pen_code], dtype=np.int64)
            pen_parts.append(r.pen_idx)
            cat_parts.append(cats)
            np.add.at(pens.setdefault(r.league, np.zeros(len(CAT_ORDER))), cats, 1.0)
    if fin_parts:
        np.add.at(n, np.concatenate(fin_parts), 1.0)
    if pen_parts:
        pi = np.concatenate(pen_parts)
        pc = np.concatenate(cat_parts)
        np.add.at(n, pi, 1.0)
        np.add.at(k, (pc, pi), 1.0)
    base = {lg: (pens[lg] / entries[lg]) if entries.get(lg) else np.zeros(len(CAT_ORDER)) for lg in pens}
    return {"n": n, "k": k, "base": base, "entries": entries}


def probabilities(counts: dict, idx: np.ndarray, league: str, alphas) -> np.ndarray:
    """(3, len(idx)) per-race probabilities for each category."""
    a = np.asarray(alphas, dtype=np.float64)[:, None]
    b = counts["base"].get(league, np.zeros(len(CAT_ORDER)))[:, None]
    if len(idx) == 0:
        return np.zeros((len(CAT_ORDER), 0))
    return (counts["k"][:, idx] + a * b) / (counts["n"][idx][None, :] + a)


def p_any(p: np.ndarray) -> np.ndarray:
    return np.minimum(p.sum(axis=0), MAX_P_ANY)


def race_entries(r) -> tuple[np.ndarray, np.ndarray]:
    """All entries of a race and a (3, n) outcome matrix (1 = that category happened)."""
    idx = np.concatenate([r.idx, r.pen_idx]).astype(np.int64)
    y = np.zeros((len(CAT_ORDER), len(idx)))
    for j, code in enumerate(r.pen_code):
        y[CODE_CAT[int(code)], len(r.idx) + j] = 1.0
    return idx, y


def _logloss(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def _new_acc() -> dict:
    return {"sums": {c: {"ll": 0.0, "ll_base": 0.0, "brier": 0.0, "brier_base": 0.0, "pos": 0.0}
                     for c in CAT_ORDER + ["any"]},
            "n": 0, "pred_any": [], "obs_any": []}


def accumulate(acc: dict, counts: dict, races, alphas) -> dict:
    """Add forward predictions for `races` (counts must come from earlier races) to acc."""
    base_alphas = [1e12] * len(CAT_ORDER)                # alpha -> infinity: everyone gets the base rate
    sums = acc["sums"]
    for r in races:
        idx, y = race_entries(r)
        if len(idx) == 0:
            continue
        p = probabilities(counts, idx, r.league, alphas)
        pb = probabilities(counts, idx, r.league, base_alphas)
        acc["n"] += len(idx)
        for ci, c in enumerate(CAT_ORDER):
            sc = sums[c]
            sc["ll"] += float(_logloss(p[ci], y[ci]).sum())
            sc["ll_base"] += float(_logloss(pb[ci], y[ci]).sum())
            sc["brier"] += float(((p[ci] - y[ci]) ** 2).sum())
            sc["brier_base"] += float(((pb[ci] - y[ci]) ** 2).sum())
            sc["pos"] += float(y[ci].sum())
        pa, pab, ya = p_any(p), p_any(pb), y.max(axis=0)
        sc = sums["any"]
        sc["ll"] += float(_logloss(pa, ya).sum())
        sc["ll_base"] += float(_logloss(pab, ya).sum())
        sc["brier"] += float(((pa - ya) ** 2).sum())
        sc["brier_base"] += float(((pab - ya) ** 2).sum())
        sc["pos"] += float(ya.sum())
        acc["pred_any"].append(pa)
        acc["obs_any"].append(ya)
    return acc


def finish(acc: dict) -> dict:
    n = acc["n"]
    out = {"entries": n}
    for c, sc in acc["sums"].items():
        out[c] = {"rate": round(sc["pos"] / n, 5) if n else None,
                  "log_loss": round(sc["ll"] / n, 6) if n else None,
                  "log_loss_base_rate": round(sc["ll_base"] / n, 6) if n else None,
                  "brier_skill": round(1 - sc["brier"] / sc["brier_base"], 5) if sc["brier_base"] else None}
    if acc["pred_any"]:
        pa = np.concatenate(acc["pred_any"])
        ya = np.concatenate(acc["obs_any"])
        top = pa >= np.percentile(pa, 90)
        pos = ya == 1
        auc = None
        if pos.any() and (~pos).any():
            ranks = rankdata(pa)
            auc = (ranks[pos].sum() - pos.sum() * (pos.sum() + 1) / 2) / (pos.sum() * (~pos).sum())
        out["any"]["auc"] = round(float(auc), 4) if auc is not None else None
        out["any"]["top_decile_rate"] = round(float(ya[top].mean()), 5)
        out["any"]["other_rate"] = round(float(ya[~top].mean()), 5) if (~top).any() else None
    return out


def score(counts: dict, races, alphas) -> dict:
    return finish(accumulate(_new_acc(), counts, races, alphas))


def score_many(items) -> dict:
    """Pool forward predictions over (counts, races, alphas) items, e.g. folds or cutoff weeks."""
    acc = _new_acc()
    for counts, races, alphas in items:
        accumulate(acc, counts, races, alphas)
    return finish(acc)


def tune_alphas(counts_val: dict, val_races) -> tuple[list[float], dict]:
    """Choose each category's shrinkage by forward log-loss on a validation season."""
    alphas, curves = [], {}
    entries = [race_entries(r) + (r.league,) for r in val_races]
    for ci, c in enumerate(CAT_ORDER):
        curve = {}
        for a in ALPHA_GRID:
            al = [1e12] * len(CAT_ORDER)
            al[ci] = a
            tot = cnt = 0.0
            for idx, y, lg in entries:
                if len(idx) == 0:
                    continue
                p = probabilities(counts_val, idx, lg, al)[ci]
                tot += float(_logloss(p, y[ci]).sum())
                cnt += len(idx)
            curve[f"{a:g}"] = round(tot / cnt, 6) if cnt else None
        best = min(ALPHA_GRID, key=lambda a: curve[f"{a:g}"])
        alphas.append(best)
        curves[c] = {"curve": curve, "best": best, "at_edge": best in (ALPHA_GRID[0], ALPHA_GRID[-1])}
    return alphas, curves


def expected_points(s: np.ndarray, scale: float, p: np.ndarray | None, m: int | None = None) -> np.ndarray:
    """Expected points per boat in one race.  s: strengths; p: per-boat penalty probability
    (None = ignore penalties); m: boats in the race (defaults to len(s))."""
    m = len(s) if m is None else m
    d = scale * (s[None, :] - s[:, None])                  # [i, j] = scale (s_j - s_i)
    win = 1.0 / (1.0 + np.exp(-d))                          # P(j ahead of i | both finish)
    np.fill_diagonal(win, 0.0)
    if p is None:
        return 1.0 + win.sum(axis=1)
    q = 1.0 - p
    return p * (m + 1) + q * (1.0 + (win * q[None, :]).sum(axis=1))


def main():
    import sys
    sys.path.insert(0, str(HERE))
    import evaluate_models as em
    races, ids, _ = em.load_data()
    N = len(ids)
    alphas = [50.0] * len(CAT_ORDER)
    res_path = em.EVAL_DIR / "results.json"
    if res_path.exists():
        folds = json.loads(res_path.read_text(encoding="utf-8")).get("forecast", {}).get("folds", [])
        tuned = [f.get("penalty", {}).get("alphas") for f in folds if f.get("penalty")]
        if tuned:
            alphas = tuned[-1]
    counts = fit_counts(races, N)
    meta = {}
    with open(HERE / "sailors.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            meta[row["sailor_id"]] = row
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sailor_id", "name", "school", "league", "races", "start", "dnf", "dns",
                    "p_start", "p_dnf", "p_dns", "p_any"])
        for i, sid in enumerate(ids):
            if counts["n"][i] == 0:
                continue
            lg = meta.get(sid, {}).get("league", "ICSA")
            p = probabilities(counts, np.array([i]), lg, alphas)[:, 0]
            w.writerow([sid, meta.get(sid, {}).get("name", ""), meta.get(sid, {}).get("school", ""), lg,
                        int(counts["n"][i]), *(int(counts["k"][c, i]) for c in range(len(CAT_ORDER))),
                        *(f"{x:.5f}" for x in p), f"{min(p.sum(), MAX_P_ANY):.5f}"])
    print(f"alphas {dict(zip(CAT_ORDER, alphas))}; base rates " +
          ", ".join(f"{lg}: " + "/".join(f"{x*100:.2f}%" for x in counts['base'][lg]) for lg in counts["base"]))
    print(f"Written: {OUT_CSV}")


if __name__ == "__main__":
    main()
