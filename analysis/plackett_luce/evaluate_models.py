#!/usr/bin/env python3
"""
evaluate_models.py — out-of-sample evaluation of the weekly Plackett–Luce model.

Two protocols, same metrics, same model ladder.

1. Season-ahead forecast (the headline).
   For each target season S, every model is fit ONLY on races in ISO weeks before
   S's first race week, then predicts every race in S with its ratings frozen at
   that cutoff.  No in-season updating, so this is a deliberately hard, strictly
   forward test.  Baseline hyperparameters (Elo K, average-finish shrinkage, static
   PL L2 strength, and the logistic scale for the two non-probabilistic baselines)
   are tuned on the season immediately before S, using data before that season.

3. Championships, ratings frozen the week before (run by default).
   Every regatta typed as a championship (see CHAMP_TYPES) in the forecast seasons
   is predicted with each model fit only on races from weeks before the
   championship's own week.  Baseline settings come from that season's forecast
   fold (tuned on the prior season).  95% confidence intervals and "site model
   better" shares come from a bootstrap that resamples whole regattas.

2. Held-out regattas (sanity check).
   Reproduces train_pl_weekly.py's own split (every 20th regatta URL held out) and
   its filter (test races whose sailors all appear in training).  The weekly model
   is a smoother, so a held-out race's rating is informed by later races — this
   protocol is optimistic for the PL models and is reported only to confirm the
   metric code matches the trainer's printed numbers.  Elo and average-finish use
   strictly past weeks here.

Models
  uninformed   every sailor equal (pairwise accuracy 50%, log-loss ln 2)
  avg_finish   shrunken mean finish percentile  (1 = won, 0 = last)
  elo          multi-player Elo, pairwise updates scaled by K/(n-1)
  static_pl    Plackett–Luce with one rating per sailor, L2 prior (no time)
  weekly_pl    the site's model: train_pl_weekly.py (temporal smoothing)
  weekly_pl_v1 the previous site model (non-finishers ranked last), read from the
               eval/v1_penalties_last/ caches and scored on the same finisher-only pairs

Penalties
  Boats that did not finish are not in any race's finishing order (preprocess.py), so
  they never count toward pairwise accuracy.  penalty_model.py predicts them instead;
  its per-entry predictions are scored forward against the league base rate.

Newcomers
  Sailors with no race before the cutoff get, in every model, the median strength
  that model assigned to sailors who debuted in the tuning season (per league).

Metrics (per race, pooled over pairs / races)
  pairwise accuracy   share of boat pairs ordered correctly (exact ties = 0.5)
  pairwise log-loss   -log P(actual pair order)
  winner accuracy     predicted strongest boat actually won (ties split)
  Spearman rho        rank correlation of predicted strength vs finish order
  calibration         favourite's predicted win prob vs observed frequency

Usage
  python3 analysis/plackett_luce/evaluate_models.py                  # full run (~20 min, GPU)
  python3 analysis/plackett_luce/evaluate_models.py --skip-train     # reuse trained folds
  python3 analysis/plackett_luce/evaluate_models.py --seasons f25 s26

Outputs
  analysis/plackett_luce/eval/results.json
  analysis/plackett_luce/eval/report.txt
  analysis/plackett_luce/eval/<fold>/pl_weekly_*.csv   (trainer outputs per fold)
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import newcomer_prior as npri   # noqa: E402  (school-then-league starting rating for unseen sailors)

SCHOOL_KEYS = None   # per sailor index "LEAGUE|school", set once the sailor list is loaded
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

import penalty_model as pm

import numpy as np
import torch
from scipy.stats import rankdata

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
EVAL_DIR = HERE / "eval"
RACES_CSV = HERE / "races.csv"
SAILORS_CSV = HERE / "sailors.csv"
TRAINER = HERE / "train_pl_weekly.py"
PREVIOUS_JSON = HERE / "eval" / "previous_model.json"   # which cached model is "the previous site model"


def _previous_model() -> dict:
    """Description of the previous site model whose cached fits are compared against the current one.
    eval/previous_model.json may name another cache (e.g. the model before a rating fix); without it the
    comparison is the penalties-ranked-last model in eval/v1_penalties_last/."""
    d = {}
    try:
        d = json.loads(PREVIOUS_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {"dir": d.get("dir", "v1_penalties_last"),
            "change": d.get("change", "penalties"),
            "label": d.get("label", "Plackett–Luce, weekly, penalties ranked last (previous site model)"),
            "points_label": d.get("points_label", "Previous site model (penalties ranked last)"),
            "description": d.get("description", "the previous version of the site model, which ranked boats that "
                                                "did not finish last"),
            "fix_summary": d.get("fix_summary", ""),
            "settings": d.get("settings")}


PREVIOUS = _previous_model()
V1_DIR = HERE / "eval" / PREVIOUS["dir"]      # previous model's cached training runs
HAVE_V1 = V1_DIR.exists()
REGATTAS = [BASE / "data" / "icsa_regattas.csv", BASE / "data" / "issa_regattas.csv"]
RESULTS = [BASE / "data" / "icsa_results.csv", BASE / "data" / "issa_results.csv"]

TEST_SAMPLE = 20                      # must match train_pl_weekly.py
DEFAULT_SEASONS = ["f24", "s25", "f25", "s26"]
# Starting grids; tune_scalar() keeps doubling/halving past an edge until the
# optimum is interior, so baselines are never handicapped by a short grid.
ELO_K_GRID = [8.0, 16.0, 32.0, 64.0]
AVG_A_GRID = [1.0, 3.0, 10.0]
STATIC_LAMBDA_GRID = [0.3, 1.0, 3.0]
CALIB_BINS = np.linspace(0.5, 1.0, 11)
N_BOOT = 2000

# Regatta "type" values (from the scraped regatta listings) that count as
# championship level.  ICSA's Atlantic Coast Championship rounds are typed
# "Showcase"; the listing truncates "Semifinals" to "Semifina".
CHAMP_TYPES = {
    "ICSA": {"National Championship Finals", "National Championship Semifina",
             "National Championship Semifinals", "Conference Championship", "Showcase"},
    "ISSA": {"National Championship", "District Championship", "District Champ Qualifier"},
}

MODEL_ORDER = ["uninformed", "avg_finish", "elo", "static_pl"] + (["weekly_pl_v1"] if HAVE_V1 else []) + ["weekly_pl"]
MODEL_LABELS = {
    "uninformed": "No information (coin flip)",
    "avg_finish": "Average finish percentile",
    "elo": "Elo (multi-player, tuned K)",
    "static_pl": "Plackett–Luce, static rating",
    "weekly_pl_v1": PREVIOUS["label"],
    "weekly_pl": "Plackett–Luce, weekly (site model)",
}


# ── Week helpers (identical to train_pl_weekly.py) ────────────────────────────

def iso_week_num(d: date) -> int:
    y, w, _ = d.isocalendar()
    return y * 53 + w


def week_label(wn: int) -> str:
    from datetime import timedelta
    iso_year, iso_week = wn // 53, wn % 53
    jan4 = date(iso_year, 1, 4)
    monday = jan4 - timedelta(days=jan4.isocalendar()[2] - 1)
    monday += timedelta(weeks=iso_week - 1)
    return monday.strftime("%G-W%V")


def parse_week_label(s: str) -> int:
    return int(s[:4]) * 53 + int(s[6:8])


def parse_date(s: str):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            pass
    return None


def season_key(code: str):
    return (int(code[1:]), 0 if code[0] == "s" else 1)


# ── Data ──────────────────────────────────────────────────────────────────────

EMPTY_IDX = np.zeros(0, dtype=np.int64)


class Race(NamedTuple):
    idx: np.ndarray       # sailor indices in finishing order (winner first)
    wn: int
    url: str
    league: str
    season: str
    division: str
    race_num: int
    champ: bool = False
    event_type: str = ""
    pen_idx: np.ndarray = EMPTY_IDX      # penalised sailors (not in the finishing order)
    pen_code: np.ndarray = EMPTY_IDX     # their scraper penalty codes


def load_data():
    """Mirror train_pl_weekly.py's race filtering exactly."""
    t0 = time.perf_counter()
    key_week: dict[tuple, int] = {}
    single_keys: set[tuple] = set()
    url_type: dict[str, str] = {}
    for p in REGATTAS:
        with open(p, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                path = urlparse(row.get("url", "")).path.rstrip("/")
                if path:
                    url_type[path] = row.get("type", "").strip()
                k = (row["season"].strip(), row["name"].strip().lower())
                d = parse_date(row.get("start_date", ""))
                if d:
                    key_week[k] = iso_week_num(d)
                if row.get("scoring", "").strip().lower() == "singlehanded":
                    single_keys.add(k)

    url_week: dict[str, int] = {}
    single_urls: set[str] = set()
    for p in RESULTS:
        with open(p, newline="", encoding="utf-8") as f:
            rd = csv.reader(f)
            hdr = next(rd)
            iu, isn, irn = hdr.index("regatta_url"), hdr.index("season"), hdr.index("regatta_name")
            for row in rd:
                url = row[iu]
                k = (row[isn].strip(), row[irn].strip().lower())
                if k in single_keys:
                    single_urls.add(url)
                if url not in url_week:
                    wn = key_week.get(k)
                    if wn:
                        url_week[url] = wn

    ids: list[str] = []
    with open(SAILORS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ids.append(row["sailor_id"])
    id_to_idx = {s: i for i, s in enumerate(ids)}

    races: list[Race] = []
    with open(RACES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = row["regatta_url"]
            if url in single_urls:
                continue
            wn = url_week.get(url)
            if wn is None:
                continue
            idx = [id_to_idx[s] for s in row["sailors"].split("|") if s in id_to_idx]
            if len(idx) < 2:
                continue
            try:
                rn = int(row["race_num"])
            except ValueError:
                rn = 0
            lg = row.get("league", "ICSA")
            et = url_type.get(url, "")
            pen_idx, pen_code = [], []
            for ent in (row.get("penalties") or "").split("|"):
                sid_, _, code = ent.rpartition(":")
                j = id_to_idx.get(sid_)
                if j is not None and code.isdigit():
                    pen_idx.append(j)
                    pen_code.append(int(code))
            races.append(Race(np.array(idx, dtype=np.int64), wn, url, lg,
                              row["season"], row.get("division", ""), rn,
                              et in CHAMP_TYPES.get(lg, ()), et,
                              np.array(pen_idx, dtype=np.int64), np.array(pen_code, dtype=np.int64)))
    races.sort(key=lambda r: (r.wn, r.url, r.division, r.race_num))
    print(f"Loaded {len(races):,} races, {len(ids):,} sailors in {time.perf_counter() - t0:.0f}s")
    return races, ids, id_to_idx


# ── Metrics ───────────────────────────────────────────────────────────────────

_TRIU: dict[int, tuple[np.ndarray, np.ndarray]] = {}


def triu(m: int):
    if m not in _TRIU:
        _TRIU[m] = np.triu_indices(m, k=1)
    return _TRIU[m]


class Acc:
    def __init__(self):
        self.pairs = 0
        self.correct = 0.0
        self.ll = 0.0
        self.races = 0
        self.win = 0.0
        self.rho = 0.0

    def add_pairs(self, correct: np.ndarray, ll: np.ndarray):
        self.pairs += correct.size
        self.correct += float(correct.sum())
        self.ll += float(ll.sum())

    def add_race(self, win: float, rho: float):
        self.races += 1
        self.win += win
        self.rho += rho

    def merge(self, o: "Acc"):
        self.pairs += o.pairs
        self.correct += o.correct
        self.ll += o.ll
        self.races += o.races
        self.win += o.win
        self.rho += o.rho

    def result(self) -> dict:
        r = {"pairs": self.pairs,
             "accuracy": round(self.correct / self.pairs, 5) if self.pairs else None,
             "log_loss": round(self.ll / self.pairs, 5) if self.pairs else None}
        if self.races:
            r.update(races=self.races, winner_accuracy=round(self.win / self.races, 5),
                     spearman=round(self.rho / self.races, 5))
        return r


SLICES = ["all", "ICSA", "ISSA", "seen", "unseen", "champ", "champ_ICSA", "champ_ISSA"]


def new_accs():
    return {name: {s: Acc() for s in SLICES} for name in MODEL_ORDER}


def new_calib():
    return {name: np.zeros((len(CALIB_BINS) - 1, 3)) for name in MODEL_ORDER}   # sum_pred, sum_obs, n


def evaluate(test: list[Race], scores: dict[str, tuple[list[np.ndarray], float]],
             seen: np.ndarray, accs=None, calib=None, per_reg=None):
    """scores[name] = (per-race strength arrays aligned with `test`, logistic scale).
    Accumulates into accs / calib / per_reg when they are passed in (pooling)."""
    accs = accs if accs is not None else new_accs()
    calib = calib if calib is not None else new_calib()
    per_reg = per_reg if per_reg is not None else {name: {} for name in MODEL_ORDER}
    for k, r in enumerate(test):
        m = len(r.idx)
        iu, ju = triu(m)
        sp = seen[r.idx]
        pair_seen = sp[iu] & sp[ju]
        true_rank = np.arange(m, 0, -1, dtype=np.float64)
        slices = ["all", r.league] + (["champ", f"champ_{r.league}"] if r.champ else [])
        for name in MODEL_ORDER:
            if name not in scores:
                continue
            sc_list, scale = scores[name]
            sc = sc_list[k]
            d = (sc[iu] - sc[ju]) * scale           # >0 means model favours the actual winner of the pair
            correct = np.where(d > 0, 1.0, np.where(d < 0, 0.0, 0.5))
            ll = np.logaddexp(0.0, -d)
            a = accs[name]
            for sl in slices:
                a[sl].add_pairs(correct, ll)
            a["seen"].add_pairs(correct[pair_seen], ll[pair_seen])
            a["unseen"].add_pairs(correct[~pair_seen], ll[~pair_seen])
            pr = per_reg[name].setdefault(r.url, [0.0, 0.0, 0.0])
            pr[0] += correct.size
            pr[1] += float(correct.sum())
            pr[2] += float(ll.sum())
            mx = sc.max()
            top = np.flatnonzero(sc == mx)
            win = (1.0 / len(top)) if top[0] == 0 else 0.0
            if np.all(sc == sc[0]):
                rho = 0.0
            else:
                rho = float(np.corrcoef(rankdata(sc), true_rank)[0, 1])
            for sl in slices:
                a[sl].add_race(win, rho)
            nz = d != 0
            if nz.any():
                fav_p = 1.0 / (1.0 + np.exp(-np.abs(d[nz])))
                fav_won = (d[nz] > 0).astype(np.float64)
                b = np.clip(np.searchsorted(CALIB_BINS, fav_p, side="right") - 1, 0, len(CALIB_BINS) - 2)
                c = calib[name]
                np.add.at(c[:, 0], b, fav_p)
                np.add.at(c[:, 1], b, fav_won)
                np.add.at(c[:, 2], b, 1.0)
    return accs, calib, per_reg


def bootstrap_ci(per_reg, reg_info, keep, n_boot=N_BOOT, seed=0):
    """Resample whole regattas.  Returns 95% intervals for each model's pairwise
    accuracy / log-loss, the site model's accuracy margin over each other model,
    and the share of resamples in which the site model is better."""
    urls = sorted(u for u, (lg, ch) in reg_info.items() if keep(lg, ch) and u in per_reg["weekly_pl"])
    if len(urls) < 5:
        return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(urls), size=(n_boot, len(urls)))
    acc_b, ll_b, out = {}, {}, {"regattas": len(urls), "resamples": n_boot, "models": {}}

    def ci(x):
        return [round(float(np.percentile(x, 2.5)), 5), round(float(np.percentile(x, 97.5)), 5)]

    for mdl in MODEL_ORDER:
        if any(u not in per_reg.get(mdl, {}) for u in urls):
            continue
        arr = np.array([per_reg[mdl][u] for u in urls])
        P = arr[:, 0][idx].sum(axis=1)
        acc_b[mdl] = arr[:, 1][idx].sum(axis=1) / P
        ll_b[mdl] = arr[:, 2][idx].sum(axis=1) / P
        out["models"][mdl] = {"accuracy_ci": ci(acc_b[mdl]), "log_loss_ci": ci(ll_b[mdl])}
    for mdl in MODEL_ORDER:
        if mdl == "weekly_pl" or mdl not in acc_b:
            continue
        diff = acc_b["weekly_pl"] - acc_b[mdl]
        out["models"][mdl]["site_margin_ci"] = ci(diff)
        out["models"][mdl]["site_better_accuracy_share"] = round(float((diff > 0).mean()), 4)
        out["models"][mdl]["site_better_logloss_share"] = round(float((ll_b[mdl] - ll_b["weekly_pl"] > 0).mean()), 4)
    return out


def accs_json(accs):
    return {name: {s: accs[name][s].result() for s in SLICES} for name in MODEL_ORDER}


def calib_json(calib):
    out = {}
    for name in MODEL_ORDER:
        rows = []
        for i in range(len(CALIB_BINS) - 1):
            sp, so, n = calib[name][i]
            if n:
                rows.append({"lo": round(float(CALIB_BINS[i]), 3), "hi": round(float(CALIB_BINS[i + 1]), 3),
                             "mean_pred": round(sp / n, 5), "observed": round(so / n, 5), "n": int(n)})
        out[name] = rows
    return out


def pair_diffs(races: list[Race], S: np.ndarray) -> np.ndarray:
    """Concatenated (winner - loser) strength differences over all pairs."""
    parts = []
    for r in races:
        iu, ju = triu(len(r.idx))
        sc = S[r.idx]
        parts.append(sc[iu] - sc[ju])
    return np.concatenate(parts) if parts else np.zeros(0)


def fit_scale(d: np.ndarray, iters: int = 30) -> float:
    """1-D logistic regression without intercept: maximise sum log sigmoid(k d)."""
    if d.size > 2_000_000:
        d = np.random.default_rng(0).choice(d, 2_000_000, replace=False)
    d = d[d != 0]
    if d.size == 0:
        return 1.0
    k = 1.0 / (np.std(d) + 1e-9)
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-k * d))
        g = float((d * (1.0 - p)).sum())
        h = float((d * d * p * (1.0 - p)).sum())
        if h <= 0:
            break
        step = g / h
        k += step
        if abs(step) < 1e-7 * max(1.0, abs(k)):
            break
    return max(k, 1e-9)


def mean_logloss(d: np.ndarray, scale: float) -> float:
    return float(np.logaddexp(0.0, -d * scale).mean()) if d.size else float("nan")


# ── Models ────────────────────────────────────────────────────────────────────

def tune_scalar(start_grid, evaluate_fn, lo_limit, hi_limit, max_expand=8):
    """Minimise evaluate_fn(v)[0] over v.  If the best value sits on an edge of
    the values tried, extend by a factor of 2 in that direction (within limits)
    until the optimum is interior.  Returns (best, extra, at_edge, curve)."""
    tried = {v: evaluate_fn(v) for v in sorted(start_grid)}
    for _ in range(max_expand):
        best = min(tried, key=lambda v: tried[v][0])
        vals = sorted(tried)
        if best == vals[-1] and best * 2 <= hi_limit:
            nxt = best * 2
        elif best == vals[0] and best / 2 >= lo_limit:
            nxt = best / 2
        else:
            break
        tried[nxt] = evaluate_fn(nxt)
    best = min(tried, key=lambda v: tried[v][0])
    vals = sorted(tried)
    at_edge = best in (vals[0], vals[-1])
    curve = {f"{v:g}": round(tried[v][0], 5) for v in vals}
    return best, tried[best][1], at_edge, curve


def elo_update(R: np.ndarray, idx: np.ndarray, K: float):
    r = R[idx]
    m = len(idx)
    diff = (r[None, :] - r[:, None]) / 400.0          # [i, j] = (r_j - r_i) / 400
    E = (1.0 / (1.0 + np.power(10.0, diff))).sum(axis=1) - 0.5
    S = (m - 1) - np.arange(m)
    R[idx] = r + (K / (m - 1)) * (S - E)


def elo_ratings(races: list[Race], N: int, K: float, snapshot_weeks: list[int]) -> dict[int, np.ndarray]:
    """Run Elo chronologically; return ratings snapshot before each week in snapshot_weeks."""
    R = np.full(N, 1500.0)
    snaps: dict[int, np.ndarray] = {}
    weeks = sorted(snapshot_weeks)
    si = 0
    for r in races:
        while si < len(weeks) and r.wn >= weeks[si]:
            snaps[weeks[si]] = R.copy()
            si += 1
        if si >= len(weeks):
            break
        elo_update(R, r.idx, K)
    while si < len(weeks):
        snaps[weeks[si]] = R.copy()
        si += 1
    return snaps


def avg_finish_stats(races: list[Race], N: int):
    s = np.zeros(N)
    n = np.zeros(N)
    if races:
        idx = np.concatenate([r.idx for r in races])
        val = np.concatenate([1.0 - np.arange(len(r.idx)) / (len(r.idx) - 1) for r in races])
        np.add.at(s, idx, val)
        np.add.at(n, idx, 1.0)
    return s, n


def avg_finish_scores(s, n, a):
    return (s + 0.5 * a) / (n + a)


def fit_static_pl(races: list[Race], N: int, lam: float, device) -> np.ndarray:
    by_size: dict[int, list[np.ndarray]] = defaultdict(list)
    for r in races:
        by_size[len(r.idx)].append(r.idx)
    batches = [torch.tensor(np.stack(v), device=device) for v in by_size.values()]
    theta = torch.zeros(N, device=device, dtype=torch.float64, requires_grad=True)
    opt = torch.optim.LBFGS([theta], lr=1.0, max_iter=400, history_size=20,
                            line_search_fn="strong_wolfe", tolerance_grad=1e-7, tolerance_change=1e-12)

    def closure():
        opt.zero_grad()
        nll = 0.5 * lam * (theta ** 2).sum()
        for b in batches:
            w = theta[b]
            lse = torch.logcumsumexp(w.flip(1), dim=1).flip(1)
            nll = nll + (lse - w).sum()
        nll.backward()
        return nll

    opt.step(closure)
    return theta.detach().cpu().numpy()


def run_trainer(out_dir: Path, train_before: str | None, skip: bool) -> Path:
    latest = out_dir / "pl_weekly_latest.csv"
    if skip and latest.exists():
        print(f"  [weekly_pl] reusing {out_dir.relative_to(BASE)}")
        return out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(TRAINER), "--out-dir", str(out_dir)]
    if train_before:
        cmd += ["--train-before", train_before]
    print(f"  [weekly_pl] training: {' '.join(cmd[1:])}", flush=True)
    t0 = time.perf_counter()
    with open(out_dir / "train.log", "w", encoding="utf-8") as log:
        subprocess.run(cmd, check=True, stdout=log, stderr=subprocess.STDOUT, cwd=BASE)
    print(f"  [weekly_pl] done in {time.perf_counter() - t0:.0f}s", flush=True)
    return out_dir


def load_latest(out_dir: Path, id_to_idx: dict[str, int], N: int) -> np.ndarray:
    S = np.zeros(N)
    with open(out_dir / "pl_weekly_latest.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            i = id_to_idx.get(row["sailor_id"])
            if i is not None:
                S[i] = float(row["rating"])
    return S


# ── Protocol 1: season-ahead forecast ─────────────────────────────────────────

def run_forecast_fold(races, ids, id_to_idx, target, prev, device, skip_train):
    N = len(ids)
    cut = min(r.wn for r in races if r.season == target)
    vcut = min(r.wn for r in races if r.season == prev)
    train = [r for r in races if r.wn < cut]
    test = [r for r in races if r.season == target]
    val_train = [r for r in races if r.wn < vcut]
    val = [r for r in races if r.season == prev and r.wn < cut]
    print(f"\n=== Forecast {target}: train < {week_label(cut)} ({len(train):,} races), "
          f"test {len(test):,} races; tune on {prev} ({len(val):,} races) ===", flush=True)

    # Elo: tune K (and logistic scale) on the previous season
    def elo_eval(K):
        snaps = elo_ratings(races, N, K, [vcut, cut])
        d = pair_diffs(val, snaps[vcut])
        k = fit_scale(d)
        ll = mean_logloss(d, k)
        print(f"  [elo] K={K:<6g} val log-loss {ll:.4f}", flush=True)
        return ll, (k, snaps[cut])
    elo_K, (elo_scale, elo_S), elo_edge, elo_curve = tune_scalar(ELO_K_GRID, elo_eval, 1.0, 4096.0)

    # Average finish: tune shrinkage (and scale)
    vs, vn = avg_finish_stats(val_train, N)

    def avg_eval(a):
        d = pair_diffs(val, avg_finish_scores(vs, vn, a))
        k = fit_scale(d)
        ll = mean_logloss(d, k)
        print(f"  [avg_finish] a={a:<6g} val log-loss {ll:.4f}", flush=True)
        return ll, k
    avg_a, avg_scale, avg_edge, avg_curve = tune_scalar(AVG_A_GRID, avg_eval, 0.05, 2000.0)
    ts, tn = avg_finish_stats(train, N)
    avg_S = avg_finish_scores(ts, tn, avg_a)

    # Static PL: tune L2 strength
    def static_eval(lam):
        th = fit_static_pl(val_train, N, lam, device)
        ll = mean_logloss(pair_diffs(val, th), 1.0)
        print(f"  [static_pl] lambda={lam:<6g} val log-loss {ll:.4f}", flush=True)
        return ll, None
    static_lam, _, static_edge, static_curve = tune_scalar(STATIC_LAMBDA_GRID, static_eval, 0.01, 1000.0)
    static_S = fit_static_pl(train, N, static_lam, device)
    for nm, edge in (("elo", elo_edge), ("avg_finish", avg_edge), ("static_pl", static_edge)):
        if edge:
            print(f"  WARNING: {nm} optimum still on the edge of its search range")

    # Weekly PL: the site model, retrained with the cutoff
    out = run_trainer(EVAL_DIR / f"forecast_{target}", week_label(cut), skip_train)
    weekly_S = load_latest(out, id_to_idx, N)
    v1_S = load_latest(V1_DIR / f"forecast_{target}", id_to_idx, N) if HAVE_V1 else None

    seen = np.zeros(N, dtype=bool)
    first_season = np.full(N, "", dtype=object)
    first_league = np.full(N, "", dtype=object)
    for r in train:
        fresh = ~seen[r.idx]
        if fresh.any():
            first_season[r.idx[fresh]] = r.season
            first_league[r.idx[fresh]] = r.league
        seen[r.idx] = True
    debut = first_season == prev

    prev2 = npri.previous_seasons(sorted({r.season for r in races}, key=season_key), target)

    def newcomer_prior(S: np.ndarray) -> dict[str, float]:
        """The league priors, for the report.  The strengths themselves are filled by
        npri.fill: a newcomer starts at their school's debut median, else the league's,
        so every model treats newcomers by the same forward-legal rule instead of by an
        arbitrary zero point (1500, 0.5, 0) that happens to sit differently in each
        model's distribution."""
        return npri.priors(S, seen, first_season, first_league, SCHOOL_KEYS, prev2)[0]

    frozen = {"uninformed": (np.zeros(N), 1.0), "avg_finish": (avg_S, avg_scale),
              "elo": (elo_S, elo_scale), "static_pl": (static_S, 1.0), "weekly_pl": (weekly_S, 1.0)}
    if v1_S is not None:
        frozen["weekly_pl_v1"] = (v1_S, 1.0)
    scores, priors = {}, {}
    for name, (S, scale) in frozen.items():
        pri = newcomer_prior(S) if name != "uninformed" else None
        S_f = npri.fill(S, seen, first_season, first_league, SCHOOL_KEYS, prev2) if pri is not None else S
        lst = [S_f[r.idx].copy() for r in test]
        scores[name] = (lst, scale)
        if pri is not None:
            priors[name] = {k: round(v, 5) for k, v in pri.items()}
    accs, calib, per_reg = evaluate(test, scores, seen)

    # Penalty model: shrinkage tuned on the previous season, then scored forward on this one
    pen_alphas, pen_curves = pm.tune_alphas(pm.fit_counts(val_train, N), val)
    pen_counts = pm.fit_counts(train, N)
    pen_metrics = pm.score(pen_counts, test, pen_alphas)
    pa = pen_metrics["any"]
    print(f"  [penalty] alphas {dict(zip(pm.CAT_ORDER, pen_alphas))}  any-penalty rate {pa['rate']*100:.2f}%  "
          f"log-loss {pa['log_loss']:.5f} vs base {pa['log_loss_base_rate']:.5f}  AUC {pa['auc']}", flush=True)

    new_sailors = int(len({int(i) for r in test for i in r.idx} - set(np.flatnonzero(seen).tolist())))
    fold = {
        "season": target, "cutoff_week": week_label(cut), "tuning_season": prev,
        "train_races": len(train), "test_races": len(test),
        "test_sailors": len({int(i) for r in test for i in r.idx}), "new_sailors": new_sailors,
        "newcomer_prior": priors,
        "tuned": {"elo_k": elo_K, "elo_scale": round(elo_scale, 6), "avg_finish_a": avg_a,
                  "avg_finish_scale": round(avg_scale, 4), "static_lambda": static_lam,
                  "penalty_alphas": [float(a) for a in pen_alphas]},
        "tuning_curves": {"elo_k": elo_curve, "avg_finish_a": avg_curve, "static_lambda": static_curve},
        "tuning_at_edge": {"elo_k": elo_edge, "avg_finish_a": avg_edge, "static_lambda": static_edge},
        "penalty": {"alphas": [float(a) for a in pen_alphas], "tuning": pen_curves, "metrics": pen_metrics},
        "metrics": accs_json(accs),
    }
    for name in MODEL_ORDER:
        a = fold["metrics"][name]["all"]
        print(f"  {MODEL_LABELS[name]:<36} acc {a['accuracy']*100:6.2f}%  ll {a['log_loss']:.4f}  "
              f"win {a['winner_accuracy']*100:5.1f}%  rho {a['spearman']:.3f}")
    return fold, accs, calib, per_reg, (pen_counts, test, pen_alphas)


# ── Protocol 2: held-out regattas (trainer's split) ───────────────────────────

def run_holdout(races, ids, id_to_idx, tuned, device, skip_train):
    N = len(ids)
    urls = sorted({r.url for r in races})
    test_urls = set(urls[::TEST_SAMPLE])
    train = [r for r in races if r.url not in test_urls]
    seen = np.zeros(N, dtype=bool)
    for r in train:
        seen[r.idx] = True
    test_flags = [(r.url in test_urls) and bool(seen[r.idx].all()) for r in races]
    test = [r for r, t in zip(races, test_flags) if t]
    print(f"\n=== Held-out regattas: {len(test_urls):,} regattas, {len(test):,} test races "
          f"(all sailors seen), {len(train):,} train races ===", flush=True)

    out = run_trainer(EVAL_DIR / "holdout", None, skip_train)
    tu = out / "test_urls.txt"
    if tu.exists():
        trainer_urls = set(tu.read_text(encoding="utf-8").split())
        match = "match" if trainer_urls == test_urls else f"MISMATCH ({len(trainer_urls ^ test_urls)} differ)"
        print(f"  trainer test split vs evaluator: {match}")

    # weekly PL rating at the race's own week
    series: dict[tuple[int, int], float] = {}
    with open(out / "pl_weekly_ratings.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            i = id_to_idx.get(row["sailor_id"])
            if i is not None:
                series[(i, parse_week_label(row["iso_week"]))] = float(row["rating"])
    weekly = [np.array([series.get((int(i), r.wn), 0.0) for i in r.idx]) for r in test]

    # Previous site model: only comparable if its held-out split is the same regattas
    v1 = None
    v1_dir = V1_DIR / "holdout"
    if HAVE_V1 and (v1_dir / "pl_weekly_ratings.csv").exists():
        v1_urls = set((v1_dir / "test_urls.txt").read_text(encoding="utf-8").split()) if (v1_dir / "test_urls.txt").exists() else set()
        if v1_urls == test_urls:
            v1_series: dict[tuple[int, int], float] = {}
            with open(v1_dir / "pl_weekly_ratings.csv", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    i = id_to_idx.get(row["sailor_id"])
                    if i is not None:
                        v1_series[(i, parse_week_label(row["iso_week"]))] = float(row["rating"])
            v1 = [np.array([v1_series.get((int(i), r.wn), 0.0) for i in r.idx]) for r in test]
        else:
            print(f"  previous model's held-out split differs ({len(v1_urls ^ test_urls)} regattas); not scored here")

    static_S = fit_static_pl(train, N, 1.0, device)

    # Elo and average finish: strictly past weeks, no updates on test races
    R = np.full(N, 1500.0)
    s = np.zeros(N)
    n = np.zeros(N)
    elo_sc, avg_sc = [], []
    i = 0
    while i < len(races):
        w = races[i].wn
        j = i
        while j < len(races) and races[j].wn == w:
            j += 1
        for k in range(i, j):
            if test_flags[k]:
                idx = races[k].idx
                elo_sc.append(R[idx].copy())
                avg_sc.append(avg_finish_scores(s[idx], n[idx], tuned["avg_finish_a"]))
        for k in range(i, j):
            r = races[k]
            if r.url in test_urls:
                continue
            elo_update(R, r.idx, tuned["elo_k"])
            m = len(r.idx)
            s[r.idx] += 1.0 - np.arange(m) / (m - 1)
            n[r.idx] += 1.0
        i = j

    scores = {"uninformed": ([np.zeros(len(r.idx)) for r in test], 1.0),
              "avg_finish": (avg_sc, tuned["avg_finish_scale"]),
              "elo": (elo_sc, tuned["elo_scale"]),
              "static_pl": ([static_S[r.idx] for r in test], 1.0),
              "weekly_pl": (weekly, 1.0)}
    if v1 is not None:
        scores["weekly_pl_v1"] = (v1, 1.0)
    accs, calib, per_reg = evaluate(test, scores, seen)
    res = {"test_regattas": len(test_urls), "test_races": len(test), "train_races": len(train),
           "params": {"elo_k": tuned["elo_k"], "avg_finish_a": tuned["avg_finish_a"], "static_lambda": 1.0},
           "metrics": accs_json(accs), "calibration": calib_json(calib)}
    for name in MODEL_ORDER:
        a = res["metrics"][name]
        if name not in scores or a["ICSA"]["accuracy"] is None:
            continue          # e.g. the previous site model when its held-out split no longer matches
        print(f"  {MODEL_LABELS[name]:<36} ICSA acc {a['ICSA']['accuracy']*100:6.2f}% ll {a['ICSA']['log_loss']:.4f} | "
              f"ISSA acc {a['ISSA']['accuracy']*100:6.2f}% ll {a['ISSA']['log_loss']:.4f}")
    return res


# ── Protocol 3: championships, ratings frozen the week before ─────────────────

def snapshots_before(races: list[Race], N: int, weeks: list[int]) -> dict[int, dict]:
    """Cumulative average-finish stats and seen/debut bookkeeping at the start of
    each week in `weeks` (only races from strictly earlier weeks)."""
    weeks = sorted(set(weeks))
    s = np.zeros(N)
    n = np.zeros(N)
    seen = np.zeros(N, dtype=bool)
    first_season = np.full(N, "", dtype=object)
    first_league = np.full(N, "", dtype=object)
    out: dict[int, dict] = {}

    def snap(w):
        out[w] = {"s": s.copy(), "n": n.copy(), "seen": seen.copy(),
                  "first_season": first_season.copy(), "first_league": first_league.copy()}

    wi = 0
    for r in races:
        while wi < len(weeks) and r.wn >= weeks[wi]:
            snap(weeks[wi])
            wi += 1
        if wi >= len(weeks):
            break
        m = len(r.idx)
        s[r.idx] += 1.0 - np.arange(m) / (m - 1)
        n[r.idx] += 1.0
        fresh = ~seen[r.idx]
        if fresh.any():
            first_season[r.idx[fresh]] = r.season
            first_league[r.idx[fresh]] = r.league
        seen[r.idx] = True
    while wi < len(weeks):
        snap(weeks[wi])
        wi += 1
    return out


def score_lists(test, frozen, seen, first_season, first_league, prev, prev2=None):
    """Per-race score arrays with the shared newcomer rule applied (newcomer_prior.fill)."""
    prev2 = prev2 or (prev,)
    scores = {}
    for name, (S, scale) in frozen.items():
        S_f = S if name == "uninformed" else npri.fill(S, seen, first_season, first_league, SCHOOL_KEYS, prev2)
        scores[name] = ([S_f[r.idx].copy() for r in test], scale)
    return scores


def run_champ_weeks(races, ids, id_to_idx, seasons, tuned_by_season, device, skip_train, workers):
    N = len(ids)
    all_seasons = sorted({r.season for r in races}, key=season_key)
    champ = [r for r in races if r.champ and r.season in set(seasons)]
    groups = sorted({(r.wn, r.season) for r in champ})
    weeks = sorted({w for w, _ in groups})
    n_reg = len({r.url for r in champ})
    print(f"\n=== Championships, ratings frozen the week before: {n_reg:,} regattas, {len(champ):,} races, "
          f"{len(weeks)} cutoff weeks ===", flush=True)

    jobs = [(EVAL_DIR / f"champ_{week_label(w)}", week_label(w)) for w in weeks]
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(lambda j: run_trainer(j[0], j[1], skip_train), jobs))
    print(f"  weekly PL ready for {len(jobs)} cutoffs in {time.perf_counter() - t0:.0f}s", flush=True)

    snaps = snapshots_before(races, N, weeks)
    elo_snaps = {K: elo_ratings(races, N, K, weeks)
                 for K in sorted({tuned_by_season[s]["elo_k"] for s in seasons})}
    accs, calib, per_reg = new_accs(), new_calib(), {m: {} for m in MODEL_ORDER}
    per_week = []
    pen_items = []
    static_cache: dict[tuple[int, float], np.ndarray] = {}
    for w, season in groups:
        test = [r for r in champ if r.wn == w and r.season == season]
        prev = all_seasons[all_seasons.index(season) - 1]
        tu = tuned_by_season[season]
        st = snaps[w]
        key = (w, tu["static_lambda"])
        if key not in static_cache:
            static_cache[key] = fit_static_pl([r for r in races if r.wn < w], N, tu["static_lambda"], device)
            np.save(EVAL_DIR / f"static_{week_label(w)}_lam{tu['static_lambda']:g}.npy", static_cache[key])
        frozen = {"uninformed": (np.zeros(N), 1.0),
                  "avg_finish": (avg_finish_scores(st["s"], st["n"], tu["avg_finish_a"]), tu["avg_finish_scale"]),
                  "elo": (elo_snaps[tu["elo_k"]][w], tu["elo_scale"]),
                  "static_pl": (static_cache[key], 1.0),
                  "weekly_pl": (load_latest(EVAL_DIR / f"champ_{week_label(w)}", id_to_idx, N), 1.0)}
        if HAVE_V1 and (V1_DIR / f"champ_{week_label(w)}" / "pl_weekly_latest.csv").exists():
            frozen["weekly_pl_v1"] = (load_latest(V1_DIR / f"champ_{week_label(w)}", id_to_idx, N), 1.0)
        scores = score_lists(test, frozen, st["seen"], st["first_season"], st["first_league"], prev,
                             npri.previous_seasons(all_seasons, season))
        evaluate(test, scores, st["seen"], accs, calib, per_reg)
        pen_items.append((pm.fit_counts([r for r in races if r.wn < w], N), test, tu["penalty_alphas"]))
        per_week.append({"week": week_label(w), "season": season,
                         "regattas": len({r.url for r in test}), "races": len(test)})
        print(f"  {week_label(w)} {season}: {per_week[-1]['regattas']} regattas, {len(test)} races", flush=True)

    reg_info = {r.url: (r.league, r.champ) for r in champ}
    boots = {"champ": bootstrap_ci(per_reg, reg_info, lambda lg, ch: ch),
             "champ_ICSA": bootstrap_ci(per_reg, reg_info, lambda lg, ch: ch and lg == "ICSA"),
             "champ_ISSA": bootstrap_ci(per_reg, reg_info, lambda lg, ch: ch and lg == "ISSA")}
    firsts = {}
    for r in champ:
        firsts.setdefault(r.url, r)
    by_type = Counter((r.league, r.event_type) for r in firsts.values())
    res = {"definition": {lg: sorted(v) for lg, v in CHAMP_TYPES.items()},
           "weeks": len(weeks), "regattas": n_reg, "races": len(champ),
           "regattas_by_type": [{"league": lg, "type": t, "regattas": c} for (lg, t), c in sorted(by_type.items())],
           "per_week": per_week, "metrics": accs_json(accs), "calibration": calib_json(calib), "bootstrap": boots,
           "penalty": pm.score_many(pen_items)}
    for name in MODEL_ORDER:
        a = res["metrics"][name]["champ"]
        print(f"  {MODEL_LABELS[name]:<36} acc {a['accuracy']*100:6.2f}%  ll {a['log_loss']:.4f}  "
              f"win {a['winner_accuracy']*100:5.1f}%")
    return res


def championship_coverage(races: list[Race], seasons: set[str]) -> list[dict]:
    """Championship fleet regattas in the scoring-site listings vs. those with scored
    races in this data.  A gap means results were not scraped (or were filtered out)."""
    scored = {r.url for r in races if r.champ}
    keelboat_urls: set[str] = set()
    for kb in (BASE / "data" / "icsa_keelboat_results.csv", BASE / "data" / "issa_keelboat_results.csv"):
        if kb.exists():
            with open(kb, newline="", encoding="utf-8") as f:
                keelboat_urls.update(row["regatta_url"].rstrip("/") for row in csv.DictReader(f))
    not_fleet = ("keelboat", "match rac", "kennedy cup")
    rows: dict[tuple, list] = defaultdict(lambda: [0, 0, []])
    for p, lg in zip(REGATTAS, ("ICSA", "ISSA")):
        with open(p, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["season"] not in seasons or row.get("type", "").strip() not in CHAMP_TYPES[lg]:
                    continue
                name = row["name"].strip()
                path = urlparse(row.get("url", "")).path.rstrip("/")
                if (row.get("scoring", "").strip().lower() in ("singlehanded", "team")
                        or any(k in name.lower() for k in not_fleet) or path in keelboat_urls):
                    continue
                k = (row["season"], lg)
                rows[k][0] += 1
                if path in scored:
                    rows[k][1] += 1
                else:
                    rows[k][2].append(name)
    return [{"season": se, "league": lg, "listed": v[0], "scored": v[1], "missing": v[2]}
            for (se, lg), v in sorted(rows.items(), key=lambda kv: (season_key(kv[0][0]), kv[0][1]))]


# ── Report ────────────────────────────────────────────────────────────────────

def penalty_lines(pen: dict | None, title: str) -> list[str]:
    if not pen:
        return []
    out = [f"\n  {title} ({pen['entries']:,} race entries; forecast vs. league base rate):"]
    for c in pm.CAT_ORDER + ["any"]:
        pc = pen[c]
        line = (f"   {c:<5} rate {pc['rate']*100:5.2f}%  log-loss {pc['log_loss']:.5f} vs {pc['log_loss_base_rate']:.5f}  "
                f"Brier skill {pc['brier_skill']:+.4f}")
        if c == "any" and pc.get("auc") is not None:
            line += (f"  AUC {pc['auc']:.3f}  most penalty-prone 10% of entries {pc['top_decile_rate']*100:.2f}% "
                     f"vs others {pc['other_rate']*100:.2f}%")
        out.append(line)
    return out


def write_report(results: dict):
    L = []
    fc = results["forecast"]
    L.append(f"Model evaluation — generated {results['generated']}")
    L.append(f"Data: {results['data']['races']:,} races, {results['data']['sailors']:,} sailors, "
             f"{results['data']['first_week']} to {results['data']['last_week']}\n")
    L.append(f"SEASON-AHEAD FORECAST (pooled {', '.join(f['season'] for f in fc['folds'])})")
    hdr = f"  {'model':<36} {'acc':>7} {'logloss':>8} {'winner':>7} {'rho':>6} | {'ICSA acc':>8} {'ISSA acc':>8} | {'seen':>6} {'new':>6}"
    L.append(hdr)
    for name in MODEL_ORDER:
        m = fc["pooled"][name]
        L.append(f"  {MODEL_LABELS[name]:<36} {m['all']['accuracy']*100:6.2f}% {m['all']['log_loss']:8.4f} "
                 f"{m['all']['winner_accuracy']*100:6.1f}% {m['all']['spearman']:6.3f} | "
                 f"{m['ICSA']['accuracy']*100:7.2f}% {m['ISSA']['accuracy']*100:7.2f}% | "
                 f"{m['seen']['accuracy']*100:5.1f}% {m['unseen']['accuracy']*100:5.1f}%")
    L.append("\n  Per season (pairwise accuracy / log-loss):")
    for f in fc["folds"]:
        cells = "  ".join(f"{name}: {f['metrics'][name]['all']['accuracy']*100:.2f}%/{f['metrics'][name]['all']['log_loss']:.4f}"
                          for name in MODEL_ORDER)
        L.append(f"   {f['season']} (cutoff {f['cutoff_week']}, {f['test_races']:,} races, "
                 f"{f['new_sailors']:,} new sailors)  {cells}")
        L.append(f"      tuned: {f['tuned']}")
    L.append("\n  Calibration, weekly PL (favourite win prob: predicted -> observed, n):")
    for b in fc["calibration"]["weekly_pl"]:
        L.append(f"   {b['lo']:.2f}-{b['hi']:.2f}: {b['mean_pred']:.3f} -> {b['observed']:.3f}  ({b['n']:,})")
    L.extend(penalty_lines(fc.get("penalty"), "Penalty model, season-ahead"))
    L.append("\n  Championship regattas inside the season-ahead forecast (ratings frozen at season start):")
    for name in MODEL_ORDER:
        c = fc["pooled"][name]["champ"]
        if c["pairs"]:
            L.append(f"   {MODEL_LABELS[name]:<36} acc {c['accuracy']*100:6.2f}%  ll {c['log_loss']:.4f}  ({c['pairs']:,} pairs)")
    ch = results.get("championships")
    if ch:
        b = ch["bootstrap"].get("champ") or {"models": {}}
        L.append(f"\nCHAMPIONSHIPS, RATINGS FROZEN THE WEEK BEFORE ({ch['regattas']} regattas, {ch['races']:,} races, "
                 f"{ch['weeks']} cutoff weeks; 95% CI by regatta bootstrap)")
        for name in MODEL_ORDER:
            mt = ch["metrics"][name]
            bm = b["models"].get(name, {})
            ci = bm.get("accuracy_ci")
            ci_s = f"[{ci[0]*100:.1f}, {ci[1]*100:.1f}]" if ci else ""
            extra = ""
            if name != "weekly_pl" and bm.get("site_margin_ci"):
                mc = bm["site_margin_ci"]
                extra = (f"  site margin [{mc[0]*100:+.1f}, {mc[1]*100:+.1f}] pts, "
                         f"site better in {bm['site_better_accuracy_share']*100:.0f}% of resamples")
            icsa = mt["champ_ICSA"]["accuracy"]
            issa = mt["champ_ISSA"]["accuracy"]
            L.append(f"   {MODEL_LABELS[name]:<36} acc {mt['champ']['accuracy']*100:6.2f}% {ci_s:<14} "
                     f"ll {mt['champ']['log_loss']:.4f}  win {mt['champ']['winner_accuracy']*100:5.1f}%  "
                     f"ICSA {icsa*100 if icsa is not None else float('nan'):5.1f}%  "
                     f"ISSA {issa*100 if issa is not None else float('nan'):5.1f}%{extra}")
    if ch:
        L.extend(penalty_lines(ch.get("penalty"), "Penalty model at championships, week before"))
    if ch and ch.get("coverage"):
        L.append("\n  Championship regattas listed vs. scored (a gap means results are not in the data):")
        for c in ch["coverage"]:
            gap = f"  missing: {', '.join(c['missing'][:5])}{' ...' if len(c['missing']) > 5 else ''}" if c["missing"] else ""
            L.append(f"   {c['season']} {c['league']}: {c['scored']}/{c['listed']}{gap}")
    ho = results["holdout"]
    L.append(f"\nHELD-OUT REGATTAS (trainer split; smoother sees later races — optimistic for PL)")
    L.append(f"  {ho['test_regattas']:,} regattas, {ho['test_races']:,} races")
    for name in MODEL_ORDER:
        m = ho["metrics"][name]
        if not m["ICSA"]["pairs"]:
            continue
        L.append(f"  {MODEL_LABELS[name]:<36} ICSA {m['ICSA']['accuracy']*100:6.2f}% / {m['ICSA']['log_loss']:.4f}   "
                 f"ISSA {m['ISSA']['accuracy']*100:6.2f}% / {m['ISSA']['log_loss']:.4f}")
    text = "\n".join(L) + "\n"
    (EVAL_DIR / "report.txt").write_text(text, encoding="utf-8")
    print("\n" + text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs="+", default=DEFAULT_SEASONS)
    ap.add_argument("--skip-train", action="store_true", help="reuse weekly-PL fold outputs if present")
    ap.add_argument("--workers", type=int, default=3, help="parallel weekly-PL training jobs for championships")
    ap.add_argument("--no-championships", action="store_true", help="skip the week-before championship protocol")
    args = ap.parse_args()
    t0 = time.perf_counter()
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")

    races, ids, id_to_idx = load_data()
    global SCHOOL_KEYS
    SCHOOL_KEYS = npri.school_keys(ids)
    all_seasons = sorted({r.season for r in races}, key=season_key)

    folds, pooled_accs, pooled_calib = [], new_accs(), new_calib()
    pooled_reg = {m: {} for m in MODEL_ORDER}
    pen_items = []
    tuned_by_season = {}
    tuned_last = None
    for target in sorted(args.seasons, key=season_key):
        prev = all_seasons[all_seasons.index(target) - 1]
        fold, accs, calib, per_reg, pen_item = run_forecast_fold(races, ids, id_to_idx, target, prev, device, args.skip_train)
        folds.append(fold)
        pen_items.append(pen_item)
        tuned_by_season[target] = fold["tuned"]
        for name in MODEL_ORDER:
            for s in SLICES:
                pooled_accs[name][s].merge(accs[name][s])
            pooled_calib[name] += calib[name]
            pooled_reg[name].update(per_reg[name])
        tuned_last = {"elo_k": fold["tuned"]["elo_k"], "elo_scale": fold["tuned"]["elo_scale"],
                      "avg_finish_a": fold["tuned"]["avg_finish_a"],
                      "avg_finish_scale": fold["tuned"]["avg_finish_scale"]}

    holdout = run_holdout(races, ids, id_to_idx, tuned_last, device, args.skip_train)

    championships = None
    if not args.no_championships:
        championships = run_champ_weeks(races, ids, id_to_idx, args.seasons, tuned_by_season,
                                        device, args.skip_train, args.workers)
        championships["coverage"] = championship_coverage(races, set(args.seasons))

    reg_info = {r.url: (r.league, r.champ) for r in races if r.season in set(args.seasons)}
    forecast_boot = {"all": bootstrap_ci(pooled_reg, reg_info, lambda lg, ch: True),
                     "ICSA": bootstrap_ci(pooled_reg, reg_info, lambda lg, ch: lg == "ICSA"),
                     "ISSA": bootstrap_ci(pooled_reg, reg_info, lambda lg, ch: lg == "ISSA"),
                     "champ": bootstrap_ci(pooled_reg, reg_info, lambda lg, ch: ch)}

    results = {"previous_model": PREVIOUS if HAVE_V1 else None,

               
        "generated": datetime.now().isoformat(timespec="seconds"),
        "runtime_s": None,
        "labels": MODEL_LABELS,
        "order": MODEL_ORDER,
        "data": {"races": len(races), "sailors": len(ids),
                 "first_week": week_label(races[0].wn), "last_week": week_label(races[-1].wn)},
        "forecast": {"folds": folds, "pooled": accs_json(pooled_accs), "calibration": calib_json(pooled_calib),
                     "bootstrap": forecast_boot, "penalty": pm.score_many(pen_items)},
        "holdout": holdout,
        "championships": championships,
    }
    results["runtime_s"] = round(time.perf_counter() - t0)
    (EVAL_DIR / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    write_report(results)
    print(f"Written: {EVAL_DIR / 'results.json'}  ({results['runtime_s']}s)")


if __name__ == "__main__":
    main()
