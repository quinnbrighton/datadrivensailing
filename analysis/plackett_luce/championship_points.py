#!/usr/bin/env python3
"""
championship_points.py — regatta-level check of the championship forecasts.

For every championship regatta in evaluate_models.py's week-before protocol, each
model's frozen ratings (fit on races through the week before the event) predict
every team's expected total points: the sum, over every race its boats sailed, of
the boat's expected points.  Each team also records its median simulated total ("median_points"),
which scored worse than expected points and is kept only for reference.  Each team also records the skippers who sailed each
division, with their site-model rating going into the event.

Ratings order the boats that finish.  Penalties are modelled separately by
penalty_model.py: a boat whose skipper has penalty probability p scores fleet size
+ 1 with probability p, otherwise it finishes among the other finishers, so

    E[points_i] = p_i (m + 1) + (1 - p_i) (1 + sum_j (1 - p_j) P(j ahead of i))

Rows (all with ratings frozen the week before):
    uninformed        coin flip, every boat (m + 1) / 2
    avg_finish, elo,
    static_pl         baselines, each with the same penalty model
    weekly_pl_v1      previous site model (evaluate_models.PREVIOUS: eval/previous_model.json)
    weekly_pl_pen     current ratings with each boat's penalty risk added to its expected points
    weekly_pl         current ratings, penalties left out of the totals (site model)

Every team also gets two percentiles against those simulations: the share of simulated regattas its real
finishing place beat, and the share its real points total beat (90 = better than 90% of simulations).

Each championship is also simulated N_SIMS times under the site model at its real
race count: every race's penalties (Bernoulli, scored fleet size + 1) and finishing
order (Plackett–Luce via Gumbel-max), with low-point team totals.  Races in one regatta
are not independent.  regatta_form.py fits a correlation model to real championships:
every simulated race adds  sa * A_sailor + sd * D_sailor,race + sb * B_boat  (A a
regatta-long offset, D form drifting race to race as AR(1) with coefficient rho, B the
speed of the boat from the rotations table, shared by every team that sails it).  The
fitted sizes are scaled by a factor from COMPONENT_SCALES, and a single regatta-long offset
tau from FORM_GRID is tried too; both are chosen by the Brier score of win probabilities at
the 2024-25 championships (FORM_TUNE_SEASONS).  The site uses the correlation model;
independent races and the best single offset are kept for comparison.  That gives each
team a win probability and a probability of finishing where it did or better, and
the regatta a surprise score: the share of simulations whose standings were at
least as far from the model's expected ranks as the real standings were.

Predictions are compared with points recomputed from the recorded finishes:
low-point scoring with one team total across all divisions; DNF, DNS, DNC, OCS,
BFD, UFD, DGM and RAF scored as fleet size + 1; redress (RDG, whose awarded score
the scraper does not keep) as the boat's average finish in its other races.
Combined-division regattas are scored as one fleet per race.

Inputs:  eval/results.json (tuned settings incl. penalty shrinkage per season),
         eval/champ_<week>/ and eval/v1_penalties_last/champ_<week>/ (weekly ratings),
         eval/static_<week>_lam<λ>.npy (static fits saved by evaluate_models.py),
         races via evaluate_models.load_data, db/icsa.db and db/issa.db.
Output:  eval/championship_points.json

Usage:
    python3 analysis/plackett_luce/championship_points.py
    python3 analysis/plackett_luce/championship_points.py --limit-weeks 1   # smoke test
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
import zlib
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import torch
from scipy.stats import rankdata

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import evaluate_models as em          # noqa: E402
import newcomer_prior as npri         # noqa: E402

KEYS = None   # per sailor index "LEAGUE|school", set in main()
import penalty_model as pm            # noqa: E402
from regatta_analysis import sailor_id  # noqa: E402

PENALTY_MIN = 990
RDG = 996
N_BOOT = 2000
N_SIMS = 2000          # full-regatta simulations per championship (site model)
SITE = "weekly_pl"
DBS = {"ICSA": em.BASE / "db" / "icsa.db", "ISSA": em.BASE / "db" / "issa.db"}
OUT = em.EVAL_DIR / "championship_points.json"
FORM_JSON = em.EVAL_DIR / "regatta_form.json"
FORM_GRID = [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
COMPONENT_SCALES = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1]
FORM_TUNE_SEASONS = {"f24", "s25"}
METRICS = ("points_mae", "points_rmse", "rank_mae", "spearman", "winner", "top3")
LOWER_IS_BETTER = {"points_mae": True, "points_rmse": True, "rank_mae": True, "spearman": False, "winner": False, "top3": False}

ORDER = ["uninformed", "avg_finish", "elo", "static_pl", "weekly_pl_v1", "weekly_pl_pen", "weekly_pl"]
LABELS = {
    "uninformed": "No information (coin flip)",
    "avg_finish": "Average finish percentile + penalty model",
    "elo": "Elo + penalty model",
    "static_pl": "Plackett–Luce, static rating + penalty model",
    "weekly_pl_v1": em.PREVIOUS["points_label"],
    "weekly_pl_pen": "Site ratings, penalty risk added to the totals",
    "weekly_pl": "Site ratings (site model)",
}
# Penalties are rare, so adding each boat's expected penalty points lifts every predicted total above what a
# team without one actually scores: it costs the site model 0.26 points per team at championships and picks the
# winner less often.  Predicted totals therefore leave it out; the simulations still draw penalties, so win
# chances and finish probabilities include them.
USE_PENALTY = {"uninformed": False, "avg_finish": True, "elo": True, "static_pl": True,
               "weekly_pl_v1": False, "weekly_pl_pen": True, "weekly_pl": False}


def load_listing() -> dict[str, dict]:
    meta: dict[str, dict] = {}
    for p in em.REGATTAS:
        with open(p, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                path = urlparse(row.get("url", "")).path.rstrip("/")
                if path:
                    meta[path] = {k: row.get(k, "").strip() for k in ("name", "scoring", "type", "host", "start_date")}
    return meta


def newcomer_prior(S, seen, first_season, first_league, prev, keys=None, prev2=None):
    """Same rule as evaluate_models: a newcomer starts at their school's debut median, else
    the league's.  Returns (S with unseen sailors filled, league priors for sailors with no
    record at all)."""
    prev2 = prev2 or (prev,)
    league, _school = npri.priors(S, seen, first_season, first_league, keys, prev2)
    return npri.fill(S, seen, first_season, first_league, keys, prev2), league


def score_regatta(rows, scoring, league, strengths, id_to_idx, pen_counts, pen_alphas, rotation=None, races_before=None):
    """Returns (teams, n_races, sim_groups).  teams[key] = school/team info, actual points, boat races,
    and expected points per model.  rotation maps (slug, team, division, race_num) -> sail number."""
    rotation = rotation or {}
    combined = scoring.lower() == "combined"
    groups: dict[tuple, list] = defaultdict(list)
    race_nums: dict = defaultdict(set)
    for division, race_num, place, school, slug, team, skipper, grade in rows:
        key = (race_num,) if combined else (division, race_num)
        groups[key].append((division, place, school, slug, team or "", (skipper or "").strip(), (grade or "").strip()))
        race_nums[None if combined else division].add(race_num)
    race_index = {d: {rn: i for i, rn in enumerate(sorted(v))} for d, v in race_nums.items()}

    finishes: dict[tuple, list] = defaultdict(list)          # (slug, team, division) -> recorded finishes
    for ents in groups.values():
        for division, place, _school, slug, team, _sk, _gr in ents:
            if place < PENALTY_MIN:
                finishes[(slug, team, division)].append(place)

    base_any = min(float(pen_counts["base"].get(league, np.zeros(len(pm.CAT_ORDER))).sum()), pm.MAX_P_ANY)
    teams: dict[str, dict] = {}
    sim_groups = []
    for gkey, ents in groups.items():
        m = len(ents)
        race_num = gkey[-1]
        ri = race_index[None if combined else gkey[0]][race_num]
        idx, actual = [], []
        for division, place, _school, slug, team, sk, gr in ents:
            idx.append(id_to_idx.get(sailor_id(league, sk, slug, gr)) if sk and gr else None)
            if place < PENALTY_MIN:
                actual.append(float(place))
            elif place == RDG:
                f = finishes.get((slug, team, division))
                actual.append(float(np.mean(f)) if f else (m + 1) / 2.0)
            else:
                actual.append(float(m + 1))
        idx_arr = np.array([i if i is not None else -1 for i in idx], dtype=np.int64)
        known = idx_arr >= 0
        p_ent = np.full(m, base_any)
        if known.any():
            p_ent[known] = pm.p_any(pm.probabilities(pen_counts, idx_arr[known], league, pen_alphas))
        sailor_keys = [int(i) if i is not None else f"{e[3]}|{e[4]}|{e[0]}" for i, e in zip(idx, ents)]
        boat_keys = []
        for e in ents:
            sn = rotation.get((e[3], e[4], e[0], race_num))
            boat_keys.append((e[0], sn) if sn is not None else ("none", e[3], e[4], e[0], race_num))
        exp_ = np.array([float(races_before[i]) if (races_before is not None and i is not None) else 0.0 for i in idx])
        expected, model_inputs = {}, {}
        zero_p = np.zeros(m)
        for model, (S, scale, prior, seen) in strengths.items():
            s = np.array([S[i] if i is not None else prior[league] for i in idx])   # S already holds newcomers' priors
            expected[model] = pm.expected_points(s, scale, p_ent if USE_PENALTY[model] else None, m)
            model_inputs[model] = (s.copy(), p_ent.copy() if USE_PENALTY[model] else zero_p, scale)
        s_site, p_site, scale_site = model_inputs[SITE]
        sim_groups.append(([f"{e[3]}|{e[4]}" for e in ents], s_site, p_site, scale_site, sailor_keys,
                           ri, boat_keys, exp_, model_inputs))
        S_site, _scale_site, prior_site, seen_site = strengths[SITE]
        for k, (division, _place, school, slug, team, sk, _gr) in enumerate(ents):
            key = f"{slug}|{team}"
            t = teams.setdefault(key, {"school": school, "school_slug": slug, "team": team,
                                       "actual": 0.0, "boat_races": 0, "expected": defaultdict(float),
                                       "skippers": {}})
            t["actual"] += actual[k]
            t["boat_races"] += 1
            for model in strengths:
                t["expected"][model] += float(expected[model][k])
            if sk:
                i = idx[k]
                rated = i is not None and bool(seen_site[i])
                rec = t["skippers"].setdefault((division, sk), {
                    "races": 0, "rated": rated, "p": float(p_ent[k]),
                    "rating": float(S_site[i]) if i is not None else float(prior_site[league])})
                rec["races"] += 1
    return teams, len(groups), sim_groups


def exp_multiplier(races_before: np.ndarray, table) -> np.ndarray:
    """Per-entry multiplier on the sailor offsets (regatta and drift) from races sailed before the event.
    table = [(max_races, multiplier), ...] ascending; the last entry covers everything above."""
    out_ = np.full(len(races_before), table[-1][1])
    for hi, mult in reversed(table):
        out_[races_before <= hi] = mult
    return out_


def simulate_regatta(sim_groups, keys, actual_ranks, seed, sims=N_SIMS, params=(0.0, 0.0, 0.0, 0.0),
                     spread=1.0, exp_table=None, model=None, actual_points=None, _totals_out=None):
    """Monte Carlo of the whole regatta under the site model.  params = (sa, sd, rho, sb): every
    race adds sa * A_sailor (regatta-long) + sd * D_sailor,race (AR(1) drift over the regatta's races)
    + sb * B_boat (boat speed, shared by every team sailing that boat); all zero = independent races.
    Draws are identical for any params with the same seed (common random numbers).  Returns
    model picks whose ratings, penalty risks and probability scale to simulate (default: the site model).
    Returns ({team: (win_prob, p_finish_or_better, mean_rank, median_total, finish_pct, points_pct)},
    surprise_p, actual_rank_deviation), where the two percentiles are the share of simulated regattas the team
    beat, by finishing place and by total points (ties count half; 90 = better than 90% of simulations)."""
    sa, sd, rho, sb = params
    rng = np.random.default_rng(seed)
    col = {k: j for j, k in enumerate(keys)}
    n = len(keys)
    sailor_col, boat_col = {}, {}
    n_race = 1
    for g in sim_groups:
        for sk in g[4]:
            sailor_col.setdefault(sk, len(sailor_col))
        for bk in g[6]:
            boat_col.setdefault(bk, len(boat_col))
        n_race = max(n_race, g[5] + 1)
    A = rng.standard_normal((sims, len(sailor_col)))
    Bst = rng.standard_normal((sims, len(boat_col)))
    Z = rng.standard_normal((sims, len(sailor_col), n_race))
    D = np.empty_like(Z)
    D[:, :, 0] = Z[:, :, 0]
    kk = np.sqrt(max(1.0 - rho * rho, 0.0))
    for r_ in range(1, n_race):
        D[:, :, r_] = rho * D[:, :, r_ - 1] + kk * Z[:, :, r_]
    totals = np.zeros((sims, n))
    for ent_keys, s, p, scale, sailor_keys, ri, boat_keys, races_before, model_inputs in sim_groups:
        if model is not None and model in model_inputs:
            s, p, scale = model_inputs[model]
        m = len(s)
        cols = np.array([sailor_col[sk] for sk in sailor_keys])
        bcols = np.array([boat_col[bk] for bk in boat_keys])
        mult = exp_multiplier(races_before, exp_table)[None, :] if exp_table else 1.0
        off = mult * (sa * A[:, cols] + sd * D[:, cols, ri]) + sb * Bst[:, bcols]
        key = spread * scale * s[None, :] + off + rng.gumbel(size=(sims, m))
        pen = rng.random((sims, m)) < p[None, :]
        key = np.where(pen, -np.inf, key)
        order = np.argsort(-key, axis=1, kind="stable")
        places = np.empty_like(order)
        np.put_along_axis(places, order, np.broadcast_to(np.arange(1, m + 1), (sims, m)), axis=1)
        pts = np.where(pen, m + 1, places)
        for e, k in enumerate(ent_keys):
            totals[:, col[k]] += pts[:, e]
    ranks = 1 + (totals[:, None, :] < totals[:, :, None]).sum(axis=2)        # [sim, i] = 1 + teams strictly ahead
    if _totals_out is not None:      # diagnostics only: hand back the raw simulated totals and ranks
        _totals_out.update(totals=totals, ranks=ranks, keys=list(keys))
    act = np.array([actual_ranks[k] for k in keys])
    win = (ranks == 1).mean(axis=0)
    or_better = (ranks <= act[None, :]).mean(axis=0)
    mean_rank = ranks.mean(axis=0)
    dev_sims = np.abs(ranks - mean_rank[None, :]).mean(axis=1)
    dev_act = float(np.abs(act - mean_rank).mean())
    surprise = float((dev_sims >= dev_act - 1e-9).mean())
    median_total = np.median(totals, axis=0)
    finish_pct = (ranks > act[None, :]).mean(axis=0) + 0.5 * (ranks == act[None, :]).mean(axis=0)
    if actual_points is not None:
        ap = np.array([actual_points[k] for k in keys])
        points_pct = (totals > ap[None, :]).mean(axis=0) + 0.5 * (totals == ap[None, :]).mean(axis=0)
    else:
        points_pct = np.full(n, np.nan)
    return ({k: (float(win[j]), float(or_better[j]), float(mean_rank[j]), float(median_total[j]),
                 float(finish_pct[j]), float(points_pct[j])) for j, k in enumerate(keys)}, surprise, dev_act)


def sim_summary(regs: list[dict], suffix: str = ""):
    """Are the simulated probabilities honest across championships?  suffix "_independent"
    summarises the simulation without form offsets."""
    if not regs:
        return None
    probs, won, ll, llu, surprise, orb = [], [], [], [], [], []
    for r in regs:
        for t in r["results"]:
            probs.append(t["win_prob" + suffix])
            orb.append(t["p_finish_or_better" + suffix])
            won.append(1.0 if t["actual_rank"] == 1 else 0.0)
        ll.append(-np.log(max(r["sim" + suffix]["winner_win_prob"], 0.5 / N_SIMS)))
        llu.append(np.log(r["teams"]))
        surprise.append(r["sim" + suffix]["surprise_p"])
    probs, won, surprise = np.array(probs), np.array(won), np.array(surprise)
    edges = [0.0, 0.05, 0.15, 0.30, 0.50, 1.0001]
    bins = []
    for lo, hi in zip(edges, edges[1:]):
        mask = (probs >= lo) & (probs < hi)
        if mask.any():
            bins.append({"lo": lo, "hi": min(hi, 1.0), "mean_pred": round(float(probs[mask].mean()), 4),
                         "observed": round(float(won[mask].mean()), 4), "teams": int(mask.sum())})
    return {"regattas": len(regs), "sims": N_SIMS,
            "winner_log_loss": round(float(np.mean(ll)), 4), "uniform_log_loss": round(float(np.mean(llu)), 4),
            "win_calibration": bins,
            "surprise_below_10": round(float((surprise < 0.10).mean()), 4),
            "surprise_below_50": round(float((surprise < 0.50).mean()), 4),
            "surprise_median": round(float(np.median(surprise)), 4),
            "finish_or_better_below_10": round(float((np.array(orb) < 0.10).mean()), 4),
            "finish_or_better_below_2": round(float((np.array(orb) < 0.02).mean()), 4)}


def min_ranks(values: np.ndarray) -> np.ndarray:
    """1 = best (lowest points); ties share the better rank."""
    return np.array([1 + int((values < v - 1e-9).sum()) for v in values])


def _top3(keys, values):
    """Three lowest totals.  Ties break by a hash of the team key, which has no relation to the
    result; breaking them by list order would leak the finishing order into tied predictions
    (the database lists teams in finishing order)."""
    order = sorted(range(len(keys)), key=lambda j: (values[j], zlib.crc32(keys[j].encode())))
    return {keys[j] for j in order[:3]}


def regatta_metrics(teams: dict[str, dict], field: str = "predicted"):
    keys = list(teams)
    act = np.array([teams[k]["actual"] for k in keys])
    act_rank = min_ranks(act)
    act_top3 = _top3(keys, act)
    winners = {k for k, r in zip(keys, act_rank) if r == 1}
    ordered = np.sort(act)
    gap = float(np.median(np.diff(ordered))) if len(ordered) > 1 else None
    metrics, pred_ranks = {}, {}
    for model in ORDER:
        ex = np.array([teams[k][field][model] for k in keys])
        pr = min_ranks(ex)
        best = [k for k, r in zip(keys, pr) if r == 1]
        if np.ptp(ex) < 1e-9 or np.ptp(act) < 1e-9:
            rho = 0.0
        else:
            rho = float(np.corrcoef(rankdata(ex), rankdata(act))[0, 1])
        pred_top3 = _top3(keys, ex)
        metrics[model] = {
            "points_mae": float(np.mean(np.abs(ex - act))),
            "points_rmse": float(np.sqrt(np.mean((ex - act) ** 2))),
            "rank_mae": float(np.mean(np.abs(pr - act_rank))),
            "spearman": rho,
            "winner": len(set(best) & winners) / len(best),
            "top3": len(pred_top3 & act_top3) / min(3, len(keys)),
        }
        pred_ranks[model] = dict(zip(keys, (int(x) for x in pr)))
    return metrics, pred_ranks, dict(zip(keys, (int(x) for x in act_rank))), gap


def summarize(regs: list[dict], metrics_key: str = "metrics", points_key: str = "expected_points"):
    if not regs:
        return None
    arr = {m: {k: np.array([r[metrics_key][m][k] for r in regs]) for k in METRICS} for m in ORDER}
    summary = {}
    for m in ORDER:
        abs_err = [sum(abs(t[points_key][m] - t["actual_points"]) for t in r["results"]) for r in regs]
        boat_races = sum(t["boat_races"] for r in regs for t in r["results"])
        summary[m] = {k: round(float(arr[m][k].mean()), 4) for k in METRICS}
        summary[m]["points_off_per_regatta"] = round(float(np.mean(abs_err)), 1)
        summary[m]["points_off_per_boat_race"] = round(float(sum(abs_err)) / boat_races, 4)
    gaps = [r["median_place_gap"] for r in regs if r["median_place_gap"] is not None]
    rng = np.random.default_rng(0)
    idx = rng.integers(0, len(regs), size=(N_BOOT, len(regs)))
    boot = {}
    for m in ORDER:
        if m == SITE:
            continue
        boot[m] = {}
        for k in METRICS:
            diff = (arr[m][k] - arr[SITE][k]) if LOWER_IS_BETTER[k] else (arr[SITE][k] - arr[m][k])   # > 0: site better
            bd = diff[idx].mean(axis=1)
            boot[m][k] = {"site_advantage": round(float(diff.mean()), 4),
                          "ci": [round(float(np.percentile(bd, 2.5)), 4), round(float(np.percentile(bd, 97.5)), 4)],
                          "share_resamples_site_better": round(float((bd > 0).mean()), 4),
                          "share_regattas_site_better": round(float((diff > 1e-9).mean()), 4),
                          "share_regattas_tied": round(float((np.abs(diff) <= 1e-9).mean()), 4)}
    return {"regattas": len(regs), "teams": int(sum(r["teams"] for r in regs)),
            "median_place_gap": round(float(np.median(gaps)), 2) if gaps else None,
            "models": summary, "bootstrap": boot}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-weeks", type=int, default=None, help="only the first N cutoff weeks (smoke test)")
    ap.add_argument("--out", type=str, default=str(OUT))
    ap.add_argument("--tau", type=float, default=None, help="override the regatta form offset (default: eval/regatta_form.json)")
    ap.add_argument("--spread", type=float, default=None,
                    help="multiply site ratings in the simulation (default: regatta_form.json champ_spread, else 1)")
    ap.add_argument("--exp-table", type=str, default=None,
                    help='offset multiplier by races before the event, e.g. "0:1.6,60:1.3,200:0.8,inf:0.6" '
                         '(default: regatta_form.json experience table, else none)')
    args = ap.parse_args()
    t0 = time.perf_counter()

    res = json.loads((em.EVAL_DIR / "results.json").read_text(encoding="utf-8"))
    tuned = {f["season"]: f["tuned"] for f in res["forecast"]["folds"]}
    missing = [se for se, t in tuned.items() if "penalty_alphas" not in t]
    if missing:
        raise SystemExit(f"results.json has no penalty tuning for {missing}; rerun evaluate_models.py first")
    seasons = set(tuned)
    races, ids, id_to_idx = em.load_data()
    global KEYS
    KEYS = npri.school_keys(ids)
    N = len(ids)
    all_seasons = sorted({r.season for r in races}, key=em.season_key)

    reg: dict[str, tuple] = {}
    for r in races:
        if r.champ and r.season in seasons:
            reg.setdefault(r.url, (r.league, r.season, r.wn, r.event_type))
    groups = sorted({(wn, season) for (_lg, season, wn, _et) in reg.values()})
    if args.limit_weeks:
        groups = groups[:args.limit_weeks]
    weeks = sorted({w for w, _ in groups})
    print(f"{len(reg)} championship regattas in {len(groups)} cutoff groups", flush=True)

    snaps = em.snapshots_before(races, N, weeks)
    elo = {K: em.elo_ratings(races, N, K, weeks) for K in sorted({t["elo_k"] for t in tuned.values()})}
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    listing = load_listing()
    form = json.loads(FORM_JSON.read_text(encoding="utf-8")) if FORM_JSON.exists() else {}
    exp_table = None
    if args.exp_table:
        exp_table = [(float(a), float(b)) for a, b in (x.split(":") for x in args.exp_table.split(","))]
    elif form.get("experience"):
        exp_table = [(float(row["max_races"]), float(row["multiplier"])) for row in form["experience"]["table"]]
    spread = args.spread if args.spread is not None else float((form.get("champ_spread") or {}).get("k", 1.0))
    print(f"simulation rating spread x{spread:.2f}; offsets by experience: {exp_table}", flush=True)
    comp = form.get("components")
    variants: dict[tuple, tuple] = {("offset", t): (t, 0.0, 0.0, 0.0)
                                    for t in sorted(set(FORM_GRID) | ({args.tau} if args.tau is not None else set()))}
    if comp and args.tau is None:
        for c in COMPONENT_SCALES:
            variants[("correlated", c)] = (c * comp["sa"], c * comp["sd"], comp["rho"], c * comp["sb"])
        print(f"correlation model from regatta_form.py: sa {comp['sa']:.3f} sd {comp['sd']:.3f} rho {comp['rho']:.3f} sb {comp['sb']:.3f}", flush=True)
    else:
        print("no correlation model (eval/regatta_form.json has no components, or --tau given): single offset only", flush=True)
    cons = {lg: sqlite3.connect(p) for lg, p in DBS.items()}
    out_regs: list[dict] = []

    for w, season in groups:
        wl = em.week_label(w)
        tu = tuned[season]
        prev = all_seasons[all_seasons.index(season) - 1]
        st = snaps[w]
        train = [r for r in races if r.wn < w]
        static_file = em.EVAL_DIR / f"static_{wl}_lam{tu['static_lambda']:g}.npy"
        static_S = np.load(static_file) if static_file.exists() else em.fit_static_pl(train, N, tu["static_lambda"], device)
        v1_dir = em.V1_DIR / f"champ_{wl}"
        if not (v1_dir / "pl_weekly_latest.csv").exists():
            raise SystemExit(f"previous model cache missing: {v1_dir}")
        weekly_S = em.load_latest(em.EVAL_DIR / f"champ_{wl}", id_to_idx, N)
        frozen = {"uninformed": (np.zeros(N), 1.0),
                  "avg_finish": (em.avg_finish_scores(st["s"], st["n"], tu["avg_finish_a"]), tu["avg_finish_scale"]),
                  "elo": (elo[tu["elo_k"]][w], tu["elo_scale"]),
                  "static_pl": (static_S, 1.0),
                  "weekly_pl_v1": (em.load_latest(v1_dir, id_to_idx, N), 1.0),
                  "weekly_pl_pen": (weekly_S, 1.0),
                  "weekly_pl": (weekly_S, 1.0)}
        prev2 = npri.previous_seasons(all_seasons, season)
        strengths = {}
        for m in ORDER:
            S, scale = frozen[m]
            S_f, league_pri = newcomer_prior(S, st["seen"], st["first_season"], st["first_league"], prev, KEYS, prev2)
            strengths[m] = (S_f, scale, league_pri, st["seen"])
        pen_counts = pm.fit_counts(train, N)
        pen_alphas = tu["penalty_alphas"]
        n_week = 0
        for url, (lg, se, wn, et) in sorted(reg.items()):
            if wn != w or se != season:
                continue
            rows = cons[lg].execute(
                "SELECT division, race_num, place, school, school_slug, team, skipper, skipper_grade "
                "FROM fleet_results WHERE regatta_url=? AND (keelboat IS NULL OR keelboat=0)", (url,)).fetchall()
            if not rows:
                continue
            rotation = {(slug, team or "", div, rn): sn for slug, team, div, rn, sn in cons[lg].execute(
                "SELECT school_slug, team, division, race_num, sail_num FROM rotations WHERE regatta_url=?", (url,))}
            meta = listing.get(url, {})
            teams, n_races, sim_groups = score_regatta(rows, meta.get("scoring", ""), lg, strengths, id_to_idx,
                                                       pen_counts, pen_alphas, rotation, st["n"])
            if len(teams) < 2:
                continue
            metrics, pred_ranks, act_ranks, gap = regatta_metrics(teams, "expected")
            seed = zlib.crc32(url.encode())
            actual_points = {k: t["actual"] for k, t in teams.items()}
            sims_by_tau = {v: simulate_regatta(sim_groups, list(teams), act_ranks, seed, params=pr, spread=spread,
                                               exp_table=exp_table, actual_points=actual_points)
                           for v, pr in variants.items()}
            results = []
            for key, t in sorted(teams.items(), key=lambda kv: (kv[1]["actual"], kv[0])):
                results.append({
                    "school": t["school"], "school_slug": t["school_slug"], "team": t["team"],
                    "boat_races": t["boat_races"], "actual_points": round(t["actual"], 1),
                    "skippers": [{"division": dv, "name": nm, "races": v["races"], "rating": round(v["rating"] * 100),
                                  "rated": v["rated"], "p_penalty": round(v["p"], 4)}
                                 for (dv, nm), v in sorted(t["skippers"].items(),
                                                           key=lambda kv: (kv[0][0], -kv[1]["races"], kv[0][1]))],
                    "actual_rank": act_ranks[key], "_key": key,
                    "expected_points": {m: round(t["expected"][m], 1) for m in ORDER},
                    "predicted_rank": {m: pred_ranks[m][key] for m in ORDER}})
            out_regs.append({"url": url, "league": lg, "season": se, "week": wl,
                             "name": meta.get("name", url), "type": et, "scoring": meta.get("scoring", ""),
                             "host": meta.get("host", ""), "start_date": meta.get("start_date", ""),
                             "races": n_races, "teams": len(teams), "median_place_gap": gap,
                             "_sims": sims_by_tau, "_winners": [k for k in teams if act_ranks[k] == 1],
                             "metrics": {m: {k: round(v, 4) for k, v in mm.items()} for m, mm in metrics.items()},
                             "results": results})
            n_week += 1
        print(f"  {wl} {season}: {n_week} regattas scored", flush=True)

    # Choose the form offset: Brier score of every team's win probability at the tuning championships.
    def brier(regs, t):
        err = [(r["_sims"][t][0][res_["_key"]][0] - (1.0 if res_["actual_rank"] == 1 else 0.0)) ** 2
               for r in regs for res_ in r["results"]]
        return float(np.mean(err)) if err else float("nan")

    def winner_ll(regs, t):
        return float(np.mean([-np.log(max(max(r["_sims"][t][0][k][0] for k in r["_winners"]), 0.5 / N_SIMS)) for r in regs]))

    tune_regs = [r for r in out_regs if r["season"] in FORM_TUNE_SEASONS]
    test_regs = [r for r in out_regs if r["season"] not in FORM_TUNE_SEASONS]
    form_table = []
    for v in variants:
        row = {"model": v[0], "value": v[1], "params": [round(x, 4) for x in variants[v]]}
        for part, regs in (("tune", tune_regs), ("test", test_regs)):
            sp = np.array([r["_sims"][v][1] for r in regs])
            row[part] = {"regattas": len(regs), "brier": round(brier(regs, v), 5), "winner_log_loss": round(winner_ll(regs, v), 4),
                         "surprise_below_10": round(float((sp < 0.10).mean()), 4) if len(sp) else None}
        form_table.append(row)
    offset_v = min((v for v in variants if v[0] == "offset"), key=lambda v: brier(tune_regs, v))
    corr_vs = [v for v in variants if v[0] == "correlated"]
    corr_v = min(corr_vs, key=lambda v: brier(tune_regs, v)) if corr_vs else None
    site_v = corr_v or offset_v
    tau = offset_v[1]
    print(f"\nsimulation models, Brier of win chances; chosen on {', '.join(sorted(FORM_TUNE_SEASONS))}: "
          f"single offset tau {tau:g}" + (f", correlation model scale {corr_v[1]:g} (site)" if corr_v else ""))
    for row in form_table:
        v = (row["model"], row["value"])
        mark = "  <- site" if v == site_v else ("  <- best single offset" if v == offset_v else "")
        print(f"  {row['model']:<10} {row['value']:.1f}  tune brier {row['tune']['brier']:.5f} winLL {row['tune']['winner_log_loss']:.3f} surprise<10 "
              f"{row['tune']['surprise_below_10'] * 100:4.1f}%   test brier {row['test']['brier']:.5f} winLL {row['test']['winner_log_loss']:.3f} "
              f"surprise<10 {row['test']['surprise_below_10'] * 100:4.1f}%{mark}")
    for r in out_regs:
        sims_by_tau = r.pop("_sims")
        winners = r.pop("_winners")
        team_sim, surprise_p, dev_act = sims_by_tau[site_v]
        team_ind, surprise_ind, _ = sims_by_tau[("offset", 0.0)]
        team_off, surprise_off, _ = sims_by_tau[offset_v]
        for res_ in r["results"]:
            key = res_.pop("_key")
            res_.update({"win_prob": round(team_sim[key][0], 4), "p_finish_or_better": round(team_sim[key][1], 4),
                         "sim_mean_rank": round(team_sim[key][2], 2), "median_points": round(team_sim[key][3], 1),
                         "finish_percentile": round(team_sim[key][4], 4), "points_percentile": round(team_sim[key][5], 4),
                         "win_prob_independent": round(team_ind[key][0], 4),
                         "p_finish_or_better_independent": round(team_ind[key][1], 4),
                         "win_prob_offset": round(team_off[key][0], 4),
                         "p_finish_or_better_offset": round(team_off[key][1], 4)})
        r["sim"] = {"sims": N_SIMS, "surprise_p": round(surprise_p, 4), "rank_deviation": round(dev_act, 3),
                    "winner_win_prob": round(max(team_sim[k][0] for k in winners), 4)}
        r["sim_independent"] = {"sims": N_SIMS, "surprise_p": round(surprise_ind, 4),
                                "winner_win_prob": round(max(team_ind[k][0] for k in winners), 4)}
        r["sim_offset"] = {"sims": N_SIMS, "surprise_p": round(surprise_off, 4),
                           "winner_win_prob": round(max(team_off[k][0] for k in winners), 4)}

    def sort_key(r):
        d = em.parse_date(r["start_date"])
        return (em.season_key(r["season"]), d or datetime.min.date(), r["league"], r["name"])

    out_regs.sort(key=sort_key)
    out = {"generated": datetime.now().isoformat(timespec="seconds"),
           "runtime_s": round(time.perf_counter() - t0),
           "labels": LABELS, "order": ORDER, "site": SITE, "form_tau": tau, "previous_model": em.PREVIOUS,
           "form_model": site_v[0], "sim_spread": spread,
           "sim_experience_table": exp_table,
           "form_components": ({"sa": round(variants[site_v][0], 4), "sd": round(variants[site_v][1], 4),
                                "rho": round(variants[site_v][2], 4), "sb": round(variants[site_v][3], 4),
                                "scale": site_v[1]} if corr_v else None),
           "form_tuning": {"seasons": sorted(FORM_TUNE_SEASONS), "metric": "brier of win probability", "grid": form_table},
           "summary": {"all": summarize(out_regs),
                       "ICSA": summarize([r for r in out_regs if r["league"] == "ICSA"]),
                       "ISSA": summarize([r for r in out_regs if r["league"] == "ISSA"])},
           "simulation": {"all": sim_summary(out_regs),
                          "ICSA": sim_summary([r for r in out_regs if r["league"] == "ICSA"]),
                          "ISSA": sim_summary([r for r in out_regs if r["league"] == "ISSA"])},
           "simulation_independent": {"all": sim_summary(out_regs, "_independent"),
                                      "ICSA": sim_summary([r for r in out_regs if r["league"] == "ICSA"], "_independent"),
                                      "ISSA": sim_summary([r for r in out_regs if r["league"] == "ISSA"], "_independent")},
           "simulation_offset": {"all": sim_summary(out_regs, "_offset"),
                                 "ICSA": sim_summary([r for r in out_regs if r["league"] == "ICSA"], "_offset"),
                                 "ISSA": sim_summary([r for r in out_regs if r["league"] == "ISSA"], "_offset")},
           "regattas": out_regs}
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")

    s = out["summary"]["all"]
    print(f"\n{s['regattas']} regattas, {s['teams']} teams; median points between adjacent places {s['median_place_gap']}")
    print(f"  {'model':<46} {'pts off/team':>12} {'rms pts':>8} {'pts off/regatta':>15} {'places off':>10} {'rho':>6} {'winner':>7} {'top3':>6}")
    for m in ORDER:
        mm = s["models"][m]
        print(f"  {LABELS[m]:<46} {mm['points_mae']:12.2f} {mm['points_rmse']:8.2f} {mm['points_off_per_regatta']:15.1f} "
              f"{mm['rank_mae']:10.2f} {mm['spearman']:6.3f} {mm['winner']*100:6.1f}% {mm['top3']*100:5.1f}%")
    for m, b in (s["bootstrap"] or {}).items():
        pm_, pr_ = b["points_mae"], b["points_rmse"]
        print(f"  site vs {m:<16} points closer by {pm_['site_advantage']:+.2f}/team "
              f"[{pm_['ci'][0]:+.2f}, {pm_['ci'][1]:+.2f}], closer in {pm_['share_regattas_site_better']*100:.0f}% of regattas; "
              f"rms {pr_['site_advantage']:+.2f} [{pr_['ci'][0]:+.2f}, {pr_['ci'][1]:+.2f}]")
    for label, sm in (("independent races", out["simulation_independent"]["all"]),
                      (f"single form offset tau {tau:.2f}", out["simulation_offset"]["all"]),
                      (f"site: {site_v[0]} {site_v[1]:g}", out["simulation"]["all"])):
        if not sm:
            continue
        print(f"  [{label}]")
        print(f"  simulation: actual winners' log-loss {sm['winner_log_loss']:.3f} vs {sm['uniform_log_loss']:.3f} if all teams "
              f"equal; surprise p < 0.10 at {sm['surprise_below_10']*100:.0f}% of regattas (honest ~10%), median {sm['surprise_median']:.2f}")
        for b in sm["win_calibration"]:
            print(f"    win chance {b['lo']*100:.0f}-{b['hi']*100:.0f}%: given {b['mean_pred']*100:.1f}%, won {b['observed']*100:.1f}% ({b['teams']} teams)")
    print(f"Written: {args.out} ({out['runtime_s']}s)")


if __name__ == "__main__":
    main()
