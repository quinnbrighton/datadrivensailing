"""
Plackett-Luce weekly model — fast GPU version.

Variables: θ[sailor, week]  shape = (N_sailors, N_weeks)
  N_weeks covers every ISO calendar week from the first race in the data to
  the last. Every sailor has a variable for every week (full fixed grid).

Regularisation (same structure as train_pl_temporal.py):
  L2 anchor  : λ_a/2  · Σ_i θ_{i,0}²
  Velocity   : μ_v    · Σ_i Σ_t log cosh(θ_{i,t+1} − θ_{i,t})
  Accel      : μ_a    · Σ_i Σ_t log cosh(θ_{i,t+1} − 2θ_{i,t} + θ_{i,t-1})

PL likelihood is computed in O(fleet-size groups) via fully batched GPU ops.

Output:
  pl_weekly_ratings.csv  — (sailor_id, iso_week, rating) for *active* weeks only
  pl_weekly_latest.csv   — latest rating per sailor (for ranking)
"""

import argparse
import csv
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import torch

_parser = argparse.ArgumentParser()
_parser.add_argument("--lambda",  type=float, default=1, dest="lam")
_parser.add_argument("--mu-v",    type=float, default=500, dest="mu_v")
_parser.add_argument("--mu-a",    type=float, default=20, dest="mu_a")
_parser.add_argument("--mu-up",   type=float, default=150, dest="mu_up")
_parser.add_argument("--lr",      type=float, default=0.01,  dest="lr")
_parser.add_argument("--steps",    type=int,   default=1500, dest="steps")
_parser.add_argument("--all-data", action="store_true",       dest="all_data",
                     help="Use all races for training; skip train/test split")
_parser.add_argument("--train-before", type=str, default=None, dest="train_before",
                     help="Forecast mode: train only on races in ISO weeks before YYYY-Www "
                          "(implies --all-data). Used by evaluate_models.py.")
_parser.add_argument("--leagues", type=str, default="", dest="leagues",
                     help="Comma-separated leagues to train on (e.g. ISSA); default all. A sailor who raced in both "
                          "leagues then only has their races in the kept leagues (used to test cross-league propagation).")
_parser.add_argument("--out-dir", type=str, default=None, dest="out_dir",
                     help="Write rating CSVs (and test_urls.txt) here instead of next to this script")
_parser.add_argument("--param", choices=["grid", "increments"], default="increments", dest="param",
                     help="grid: one free rating per sailor-week; increments: rating = level + cumulative "
                          "week-to-week changes (same model, much faster convergence)")
_parser.add_argument("--anchor", choices=["week0", "first"], default="first", dest="anchor",
                     help="where the L2 prior (--lambda) applies: week0 = every sailor's rating in the first week of "
                          "the data; first = each sailor's rating in their own first active week")
_parser.add_argument("--school-prior", type=float, default=0.0, dest="school_prior",
                     help="Anchor each sailor's first rating to a level learned for their school (league + school) "
                          "instead of to 0; the value is the L2 weight pulling those school levels toward 0. "
                          "Off (0) keeps the plain anchor. Writes pl_school_priors.csv.")
_parser.add_argument("--pool-kappa", type=float, default=1.0, dest="pool_kappa",
                     help="Partial pooling toward the team: a prior of this precision on each sailor's mean rating "
                          "over their career relative to their team's average (other sailors of the same school and "
                          "season; league average when the team is too small).  The prior's precision is fixed, so its "
                          "weight fades as the sailor's own races accumulate.  Default 1, chosen on season-ahead forecasts "
                          "(eval_team_pool.py: 0.0031 better log-loss than no pooling in every held-out season; 0.3 gained "
                          "less, 3 was inconsistent).  0 = off, the model as it was before 2026-09-30.")
_parser.add_argument("--pool-first", action="store_true", dest="pool_first",
                     help="Pull each sailor's first rating toward their team's average instead of toward 0 "
                          "(the --lambda anchor keeps its strength).")
_parser.add_argument("--pool-min-team", type=int, default=3, dest="pool_min_team",
                     help="Other sailors a team-season needs before its average is used; fewer falls back to the league.")
_parser.add_argument("--lr-inc", type=float, default=0.002, dest="lr_inc",
                     help="Adam learning rate for the week-to-week changes when --param increments")
_parser.add_argument("--optimizer", choices=["adam", "lbfgs"], default="adam", dest="optimizer",
                     help="adam (fixed --steps) or lbfgs (full-batch quasi-Newton; --steps = max iterations)")
_parser.add_argument("--lbfgs-chunk", type=int, default=25, dest="lbfgs_chunk",
                     help="L-BFGS iterations between progress lines, snapshots and the convergence check")
_parser.add_argument("--lbfgs-history", type=int, default=20, dest="lbfgs_history")
_parser.add_argument("--rel-tol", type=float, default=1e-7, dest="rel_tol",
                     help="L-BFGS stops when a chunk improves the objective by less than this fraction")
_parser.add_argument("--snapshot-steps", type=str, default="", dest="snapshot_steps",
                     help="Comma-separated steps at which to also write <out-dir>/step_<n>/pl_weekly_latest.csv "
                          "(convergence checks)")
_args = _parser.parse_args()

_t0 = time.perf_counter()

RACES_CSV     = Path(__file__).parent / "races.csv"
SAILORS_CSV   = Path(__file__).parent / "sailors.csv"
REGATTAS_ICSA = Path(__file__).parent.parent.parent / "data" / "icsa_regattas.csv"
REGATTAS_ISSA = Path(__file__).parent.parent.parent / "data" / "issa_regattas.csv"
RESULTS_ICSA  = Path(__file__).parent.parent.parent / "data" / "icsa_results.csv"
RESULTS_ISSA  = Path(__file__).parent.parent.parent / "data" / "issa_results.csv"

_OUT_DIR   = Path(_args.out_dir) if _args.out_dir else Path(__file__).parent
_OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_SERIES = _OUT_DIR / "pl_weekly_ratings.csv"
OUT_LATEST = _OUT_DIR / "pl_weekly_latest.csv"

LAMBDA      = _args.lam
MU_V        = _args.mu_v
MU_A        = _args.mu_a
MU_UP       = _args.mu_up
LR          = _args.lr
MAX_STEPS   = _args.steps
TEST_SAMPLE = 20     # hold out every Nth regatta as test (1-in-N sampling)


# ── Date helpers ──────────────────────────────────────────────────────────────

def iso_week_num(d: date) -> int:
    """Globally unique week index: iso_year*53 + iso_week."""
    y, w, _ = d.isocalendar()
    return y * 53 + w


def week_label(wn: int) -> str:
    """Convert packed week number back to YYYY-Www."""
    iso_year, iso_week = wn // 53, wn % 53
    jan4   = date(iso_year, 1, 4)
    monday = jan4 - timedelta(days=jan4.isocalendar()[2] - 1)
    monday += timedelta(weeks=iso_week - 1)
    return monday.strftime("%G-W%V")


def parse_date(s: str):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            t = time.strptime(s, fmt)
            return date(t.tm_year, t.tm_mon, t.tm_mday)
        except (ValueError, TypeError):
            pass
    return None


# ── Build regatta_url → ISO week number ──────────────────────────────────────

print("Loading regatta dates…")
_regatta_key_to_week: dict[tuple, int] = {}
_singlehanded_keys: set[tuple] = set()
for path in (REGATTAS_ICSA, REGATTAS_ISSA):
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["season"].strip(), row["name"].strip().lower())
            d = parse_date(row.get("start_date", ""))
            if d:
                _regatta_key_to_week[key] = iso_week_num(d)
            if row.get("scoring", "").strip().lower() == "singlehanded":
                _singlehanded_keys.add(key)

singlehanded_urls: set[str] = set()
url_to_week: dict[str, int] = {}
for path in (RESULTS_ICSA, RESULTS_ISSA):
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = row["regatta_url"]
            key = (row["season"].strip(), row["regatta_name"].strip().lower())
            if key in _singlehanded_keys:
                singlehanded_urls.add(url)
            if url not in url_to_week:
                wn = _regatta_key_to_week.get(key)
                if wn:
                    url_to_week[url] = wn

print(f"  {len(url_to_week):,} regattas date-resolved  ({len(singlehanded_urls):,} singlehanded excluded)")


# ── Load sailors ──────────────────────────────────────────────────────────────

print("Loading sailors…")
all_ids    = []
sailor_meta: list[dict] = []
with open(SAILORS_CSV, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        all_ids.append(row["sailor_id"])
        sailor_meta.append({"name": row["name"], "year": row["year"],
                             "school": row["school"], "league": row.get("league", "")})

id_to_idx = {sid: i for i, sid in enumerate(all_ids)}
N = len(all_ids)
print(f"  {N:,} sailors")

# School of each sailor, for the school prior.  A sailor with no school recorded keeps the
# plain anchor at 0 (group -1).
school_keys: list = []
school_index: dict = {}
sailor_group = np.full(N, -1, dtype=np.int64)
for _i, _m in enumerate(sailor_meta):
    if _m["school"].strip():
        _k = (_m["league"], _m["school"].strip())
        if _k not in school_index:
            school_index[_k] = len(school_keys)
            school_keys.append(_k)
        sailor_group[_i] = school_index[_k]
if _args.school_prior > 0:
    print(f"  school prior on: {len(school_keys):,} schools, "
          f"{int((sailor_group >= 0).sum()):,} sailors with a school, λ_school={_args.school_prior}")


# ── Load races, attach week index ─────────────────────────────────────────────

print("Loading races…")
races_all: list[tuple[np.ndarray, int, str, str]] = []   # (arr, wn, regatta_url, league)
skipped = 0

with open(RACES_CSV, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if row["regatta_url"] in singlehanded_urls:
            skipped += 1
            continue
        wn = url_to_week.get(row["regatta_url"])
        if wn is None:
            skipped += 1
            continue
        ids  = [s for s in row["sailors"].split("|") if s in id_to_idx]
        arr  = np.array([id_to_idx[s] for s in ids], dtype=np.int32)
        if len(arr) < 2:
            continue
        races_all.append((arr, wn, row["regatta_url"], row.get("league", "ICSA")))

print(f"  {len(races_all):,} races  ({skipped:,} skipped — no date)")

if _args.leagues:
    _keep = {x.strip() for x in _args.leagues.split(",") if x.strip()}
    _n0 = len(races_all)
    races_all = [r for r in races_all if r[3] in _keep]
    print(f"  --leagues {','.join(sorted(_keep))}: kept {len(races_all):,} of {_n0:,} races")

if _args.train_before:
    _cut = int(_args.train_before[:4]) * 53 + int(_args.train_before[6:8])
    _n0 = len(races_all)
    races_all = [r for r in races_all if r[1] < _cut]
    _args.all_data = True
    print(f"  --train-before {_args.train_before}: kept {len(races_all):,} of {_n0:,} races "
          f"(forecast mode, no internal test split)")


# ── Build sparse week grid (only weeks with actual races) ─────────────────────

all_weeks = sorted({wn for _, wn, _, _ in races_all})   # unique regatta weeks only
T        = len(all_weeks)
wk_to_t  = {wn: i for i, wn in enumerate(all_weeks)}

# Gap between consecutive week-steps in calendar weeks (for scaled smoothness)
week_gaps = np.array([all_weeks[i+1] - all_weeks[i] for i in range(T - 1)], dtype=np.float32)
gap_v     = torch.tensor(1.0 / np.sqrt(week_gaps),        device='cpu')  # moved to device later
gap_a     = torch.tensor(1.0 / np.sqrt(
                week_gaps[:-1] + week_gaps[1:]), device='cpu')

print(f"  Week grid: {week_label(all_weeks[0])} → {week_label(all_weeks[-1])}"
      f"  ({T} unique weeks, avg gap {week_gaps.mean():.1f} cal-weeks)")
print(f"  Total parameters: {N * T:,}  ({N}×{T})")


# ── Track which weeks each sailor actually raced ──────────────────────────────

sailor_active: list[set] = [set() for _ in range(N)]
for arr, wn, _, _ in races_all:
    t = wk_to_t[wn]
    for idx in arr:
        sailor_active[idx].add(t)


# ── Train/test split ──────────────────────────────────────────────────────────

if _args.all_data:
    train_races     = [(arr, wk_to_t[wn]) for arr, wn, _, _ in races_all]
    test_races_icsa = []
    test_races_issa = []
    print(f"  Train: {len(train_races):,} races   (all data — no test split)")
else:
    all_urls_sorted = sorted({url for _, _, url, _ in races_all})
    test_urls = set(all_urls_sorted[i] for i in range(0, len(all_urls_sorted), TEST_SAMPLE))

    train_races = [(arr, wk_to_t[wn])
                   for arr, wn, url, _ in races_all if url not in test_urls]
    test_races_icsa = [(arr, wk_to_t[wn])
                       for arr, wn, url, league in races_all
                       if url in test_urls and league == "ICSA"]
    test_races_issa = [(arr, wk_to_t[wn])
                       for arr, wn, url, league in races_all
                       if url in test_urls and league == "ISSA"]

    train_sailors: set[int] = set()
    for arr, _ in train_races:
        train_sailors.update(arr.tolist())

    test_races_icsa = [(arr, t) for arr, t in test_races_icsa if all(k in train_sailors for k in arr)]
    test_races_issa = [(arr, t) for arr, t in test_races_issa if all(k in train_sailors for k in arr)]

    print(f"  Train: {len(train_races):,} races   "
          f"Test ICSA: {len(test_races_icsa):,}  ISSA: {len(test_races_issa):,}  "
          f"(1-in-{TEST_SAMPLE} regatta sampling, {len(test_urls):,} test regattas)")
    if _args.out_dir:
        (_OUT_DIR / "test_urls.txt").write_text("\n".join(sorted(test_urls)) + "\n", encoding="utf-8")


# ── Batch races by fleet size (key speedup) ───────────────────────────────────

print("Building race batches…")
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"  Device: {device}")

def _make_batches(race_list):
    by_size = defaultdict(list)
    for arr, t in race_list:
        by_size[len(arr)].append((arr, t))
    batches = []
    for m_size, batch in sorted(by_size.items()):
        B          = len(batch)
        b_arr      = np.array([a     for a, t in batch], dtype=np.int32)   # (B, m)
        b_t        = np.array([t     for a, t in batch], dtype=np.int32)   # (B,)
        b_t_broad  = np.broadcast_to(b_t[:, None], (B, m_size)).copy()     # (B, m)
        batches.append((
            torch.tensor(b_arr,    device=device, dtype=torch.long),
            torch.tensor(b_t_broad, device=device, dtype=torch.long),
        ))
    return batches

train_batches = _make_batches(train_races)
test_batches_icsa = _make_batches(test_races_icsa)
test_batches_issa = _make_batches(test_races_issa)

def _make_eval_batches(batches):
    """Add upper-triangle index pairs for pairwise accuracy/logloss."""
    result = []
    for b_arr_t, b_t_broad_t in batches:
        m = b_arr_t.shape[1]
        ti, tj = np.triu_indices(m, k=1)
        result.append((
            b_arr_t, b_t_broad_t,
            torch.tensor(ti, device=device, dtype=torch.long),
            torch.tensor(tj, device=device, dtype=torch.long),
            b_arr_t.shape[0] * len(ti),
        ))
    return result

test_batches_eval_icsa = _make_eval_batches(test_batches_icsa)
test_batches_eval_issa = _make_eval_batches(test_batches_issa)

print(f"  Train batches: {len(train_batches)}  fleet sizes: {sorted(set(b[0].shape[1] for b in train_batches))[:5]}…")

gap_v = gap_v.to(device)   # (T-1,)  1/√gap for velocity
first_active_t = torch.tensor([[min(a) if a else 0] for a in sailor_active], device=device, dtype=torch.long)  # (N, 1)
gap_a = gap_a.to(device)   # (T-2,)  1/√(gap_l+gap_r) for acceleration


# ── Team pooling structures (only when --pool-kappa or --pool-first) ──────────
# A "unit" is one sailor in one season.  Its team is the school they raced for that season
# (sailor_seasons.csv), and the team's average is the mean over the OTHER units of that
# school-season.  Everything here is index arithmetic; the reference is recomputed from the
# current ratings at every step and held fixed (no gradient), which is an EM-style hierarchical fit.
POOL = _args.pool_kappa > 0 or _args.pool_first
if POOL:
    def _wk_season(wn: int) -> str:
        yr = (wn // 53) % 100
        return f"s{yr:02d}" if wn % 53 <= 25 else f"f{yr:02d}"

    season_of_t = [_wk_season(wn) for wn in all_weeks]
    seasons_u = sorted(set(season_of_t), key=lambda x: (int(x[1:]), 0 if x[0] == "s" else 1))
    S_n = len(seasons_u)
    s_idx = {x: k for k, x in enumerate(seasons_u)}
    season_t_idx = np.array([s_idx[x] for x in season_of_t])
    sailor_season_team = {}
    with open(Path(__file__).parent / "sailor_seasons.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sailor_season_team[(row["sailor_id"], row["season"])] = (row["league"], row["school"].strip())
    team_index: dict = {}
    ent_i, ent_t, ent_unit = [], [], []
    unit_gs, unit_ls, unit_key = [], [], {}
    first_unit = np.full(N, -1, dtype=np.int64)
    for i in range(N):
        ts = sorted(sailor_active[i])
        for t in ts:
            season = season_of_t[t]
            lg, sch = sailor_season_team.get((all_ids[i], season), (sailor_meta[i]["league"], sailor_meta[i]["school"].strip()))
            ukey = (i, season)
            if ukey not in unit_key:
                unit_key[ukey] = len(unit_gs)
                if sch:
                    tix = team_index.setdefault((lg, sch), len(team_index))
                    unit_gs.append(tix * S_n + s_idx[season])
                else:
                    unit_gs.append(-1)
                unit_ls.append((0 if lg == "ICSA" else 1) * S_n + s_idx[season])
                if first_unit[i] < 0:
                    first_unit[i] = unit_key[ukey]
            ent_i.append(i); ent_t.append(t); ent_unit.append(unit_key[ukey])
    U, E, G, L = len(unit_gs), len(ent_i), max(len(team_index), 1) * S_n, 2 * S_n
    ent_i_t = torch.tensor(ent_i, device=device, dtype=torch.long)
    ent_t_t = torch.tensor(ent_t, device=device, dtype=torch.long)
    ent_unit_t = torch.tensor(ent_unit, device=device, dtype=torch.long)
    unit_gs_t = torch.tensor(unit_gs, device=device, dtype=torch.long)
    unit_ls_t = torch.tensor(unit_ls, device=device, dtype=torch.long)
    first_unit_t = torch.tensor(np.maximum(first_unit, 0), device=device, dtype=torch.long)
    has_first_t = torch.tensor(first_unit >= 0, device=device)
    ent_count_unit = torch.bincount(ent_unit_t, minlength=U).clamp(min=1).float()
    ent_count_sailor = torch.bincount(ent_i_t, minlength=N).clamp(min=1).float()
    print(f"  team pooling: {U:,} sailor-seasons in {len(team_index):,} teams, {E:,} sailor-weeks; "
          f"kappa={_args.pool_kappa} first={_args.pool_first} min_team={_args.pool_min_team}")


def pool_reference(th: torch.Tensor) -> torch.Tensor:
    """(U,) each sailor-season's pooling target: the mean of the other sailors on their team that
    season, else the league's mean that season.  Detached."""
    with torch.no_grad():
        u_val = torch.zeros(U, device=device).index_add_(0, ent_unit_t, th[ent_i_t, ent_t_t]) / ent_count_unit
        ones = torch.ones(U, device=device)
        l_sum = torch.zeros(L, device=device).index_add_(0, unit_ls_t, u_val)
        l_n = torch.zeros(L, device=device).index_add_(0, unit_ls_t, ones)
        ref = (l_sum / l_n.clamp(min=1))[unit_ls_t]
        has = unit_gs_t >= 0
        gs = unit_gs_t.clamp(min=0)
        g_sum = torch.zeros(G, device=device).index_add_(0, gs[has], u_val[has])
        g_n = torch.zeros(G, device=device).index_add_(0, gs[has], ones[has])
        others = g_n[gs] - 1.0
        loo = (g_sum[gs] - u_val) / others.clamp(min=1)
        return torch.where(has & (others >= _args.pool_min_team), loo, ref)


# ── Regularisation index helpers (built once, on device) ─────────────────────

_zero = torch.zeros(1, device=device)

def _log_cosh(x: torch.Tensor) -> torch.Tensor:
    ax = x.abs()
    return ax + torch.log1p(torch.exp(-2.0 * ax)) - 0.6931471805599453


# ── Model parameter (stays on GPU throughout) ─────────────────────────────────

if _args.param == "increments":
    level_param = torch.nn.Parameter(torch.zeros(N, 1, device=device, dtype=torch.float32))
    inc_param   = torch.nn.Parameter(torch.zeros(N, T - 1, device=device, dtype=torch.float32))
    params      = [level_param, inc_param]

    def current_theta() -> torch.Tensor:
        """(N, T) ratings: each sailor's level plus the cumulative week-to-week changes."""
        return torch.cat([level_param, level_param + torch.cumsum(inc_param, dim=1)], dim=1)

    optimizer = torch.optim.Adam([{"params": [level_param], "lr": LR}, {"params": [inc_param], "lr": _args.lr_inc}])
else:
    theta_param = torch.nn.Parameter(torch.zeros(N, T, device=device, dtype=torch.float32))
    params      = [theta_param]

    def current_theta() -> torch.Tensor:
        return theta_param

    optimizer = torch.optim.Adam(params, lr=LR)

# School levels: one parameter per (league, school), toward which its sailors' first ratings
# are pulled.  Only the anchor term changes; a sailor with results is still driven by them.
group_t     = torch.tensor(np.maximum(sailor_group, 0), device=device, dtype=torch.long)
has_group_t = torch.tensor(sailor_group >= 0, device=device)
if _args.school_prior > 0:
    school_param = torch.nn.Parameter(torch.zeros(max(len(school_keys), 1), device=device, dtype=torch.float32))
    params.append(school_param)
    optimizer.add_param_group({"params": [school_param], "lr": LR})


def anchor_targets(ref_u: torch.Tensor | None = None) -> torch.Tensor:
    """(N,) the level each sailor's first rating is pulled toward: their team's average
    (--pool-first), their school's learned level (--school-prior), or 0."""
    if POOL and _args.pool_first and ref_u is not None:
        return torch.where(has_first_t, ref_u[first_unit_t], torch.zeros((), device=device))
    if _args.school_prior <= 0:
        return torch.zeros(N, device=device)
    return torch.where(has_group_t, school_param[group_t], torch.zeros((), device=device))


# ── Objective (fully on device) ───────────────────────────────────────────────

def compute_nll():
    th = current_theta()
    w = th.exp()

    # Anchor: L2 prior on each sailor's rating in the first data week, or in their own first active week
    if _args.anchor == "first":
        first = th.gather(1, first_active_t).squeeze(1)
    else:
        first = th[:, 0]
    ref_u = pool_reference(th) if POOL else None
    nll = (LAMBDA / 2.0) * ((first - anchor_targets(ref_u)) ** 2).sum()
    if _args.school_prior > 0:
        nll = nll + (_args.school_prior / 2.0) * (school_param ** 2).sum()
    if POOL and _args.pool_kappa > 0:
        # Partial pooling: one prior per sailor on their career-average rating relative to their
        # team.  Fixed precision, so it fades as their own races accumulate.
        dev = th[ent_i_t, ent_t_t] - ref_u[ent_unit_t]
        dev_mean = torch.zeros(N, device=device).index_add_(0, ent_i_t, dev) / ent_count_sailor
        nll = nll + (_args.pool_kappa / 2.0) * (dev_mean ** 2).sum()

    # Velocity (gap-scaled log-cosh + asymmetric relu on raw diff)
    d1_raw = th[:, 1:] - th[:, :-1]          # (N, T-1)
    d1     = d1_raw * gap_v
    nll    = nll + MU_V * _log_cosh(d1).sum()
    if MU_UP > 0:
        nll = nll + MU_UP * torch.relu(-d1_raw).pow(2).sum()

    # Acceleration (gap-scaled log-cosh)
    if MU_A > 0:
        d2  = (th[:, 2:] - 2*th[:, 1:-1] + th[:, :-2]) * gap_a
        nll = nll + MU_A * _log_cosh(d2).sum()

    # PL log-likelihood (batched by fleet size)
    for b_arr_t, b_t_broad_t in train_batches:
        wts    = w[b_arr_t, b_t_broad_t]
        suffix = wts.flip(1).cumsum(1).flip(1).clamp(min=1e-30)
        nll    = nll + (suffix.log() - wts.log()).sum()

    return nll


# ── Test metrics (tensor in, no numpy conversion) ─────────────────────────────

def _eval_batches(w, batches_eval):
    correct = total = 0
    logloss = 0.0
    for b_arr_t, b_t_broad_t, ti, tj, n_pairs in batches_eval:
        wts = w[b_arr_t, b_t_broad_t]
        wi, wj = wts[:, ti], wts[:, tj]
        correct += int((wi > wj).sum())
        logloss += float(torch.logaddexp(_zero, wj.log() - wi.log()).sum())
        total   += n_pairs
    acc = correct / total if total else 0.0
    ll  = logloss / total if total else 0.0
    return acc, ll


def test_metrics(theta_2d: torch.Tensor):
    """theta_2d: (N, T) on device."""
    with torch.no_grad():
        w = theta_2d.exp()
        acc_icsa, ll_icsa = _eval_batches(w, test_batches_eval_icsa)
        acc_issa, ll_issa = _eval_batches(w, test_batches_eval_issa)
        d1 = (theta_2d[:, 1:] - theta_2d[:, :-1]).abs()
        d2 = (theta_2d[:, 2:] - 2*theta_2d[:, 1:-1] + theta_2d[:, :-2]).abs()
        vel_per_week = d1.mean(dim=0)   # (T-1,)
        acc_per_week = d2.mean(dim=0)   # (T-2,)
    return acc_icsa, ll_icsa, acc_issa, ll_issa, vel_per_week, acc_per_week


# ── Optimize ──────────────────────────────────────────────────────────────────

_snapshot_steps = {int(x) for x in _args.snapshot_steps.split(",") if x.strip()}


def _write_latest(path: Path, theta_arr: np.ndarray) -> None:
    """Latest rating per active sailor (their last active week), same format as OUT_LATEST."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(N):
        if sailor_active[i]:
            last_t = max(sailor_active[i])
            rows.append((float(theta_arr[i, last_t]), i, last_t))
    rows.sort(reverse=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["sailor_id", "name", "year", "school", "league", "last_week", "rating"])
        for rating, i, last_t in rows:
            meta = sailor_meta[i]
            wr.writerow([all_ids[i], meta["name"], meta["year"], meta["school"], meta["league"],
                         week_label(all_weeks[last_t]), f"{rating:.6f}"])


print(f"\nOptimizing  N={N:,}  T={T}  params={N*T:,}"
      f"  λ={LAMBDA}  μ_v={MU_V}  μ_a={MU_A}  μ_up={MU_UP}  lr={LR}…")
_t_opt = time.perf_counter()

def _progress(_step: int, nll_value: float) -> None:
    _, _, _, _, vel_per_week, acc_per_week = test_metrics(current_theta().detach())
    if _args.all_data:
        print(f"  step {_step:5d}  objective={nll_value:.1f}  "
              f"mean|vel|={float(vel_per_week.mean()):.4f}  "
              f"mean|acc|={float(acc_per_week.mean()):.4f}  "
              f"sd(latest)={float(current_theta().detach()[:, -1].std()):.4f}  "
              f"({time.perf_counter() - _t_opt:.0f}s)", flush=True)
    else:
        acc_icsa, ll_icsa, acc_issa, ll_issa, _, _ = test_metrics(current_theta().detach())
        print(f"  step {_step:5d}  objective={nll_value:.1f}  "
              f"ICSA acc={acc_icsa*100:.2f}% ll={ll_icsa:.4f}  "
              f"ISSA acc={acc_issa*100:.2f}% ll={ll_issa:.4f}  "
              f"mean|vel|={float(vel_per_week.mean()):.4f}  "
              f"mean|acc|={float(acc_per_week.mean()):.4f}  "
              f"({time.perf_counter() - _t_opt:.0f}s)", flush=True)


if _args.optimizer == "lbfgs":
    optimizer = torch.optim.LBFGS(params, lr=1.0, max_iter=_args.lbfgs_chunk, history_size=_args.lbfgs_history,
                                  tolerance_grad=1e-10, tolerance_change=1e-14, line_search_fn="strong_wolfe")

    def _closure():
        optimizer.zero_grad()
        loss = compute_nll()
        loss.backward()
        return loss

    _prev = None
    _done_steps = 0
    while _done_steps < MAX_STEPS:
        optimizer.step(_closure)
        _done_steps += _args.lbfgs_chunk
        with torch.no_grad():
            _val = float(compute_nll())
        for _snap in sorted(_snapshot_steps):
            if _done_steps - _args.lbfgs_chunk < _snap <= _done_steps:
                _write_latest(_OUT_DIR / f"step_{_snap}" / "pl_weekly_latest.csv", current_theta().detach().cpu().numpy())
        _progress(_done_steps, _val)
        if _prev is not None and (_prev - _val) < _args.rel_tol * abs(_prev):
            print(f"  converged: chunk improvement {(_prev - _val):.3f} < rel-tol {_args.rel_tol:g} x objective", flush=True)
            break
        _prev = _val
    MAX_STEPS = _done_steps
else:
    for _step in range(1, MAX_STEPS + 1):
        optimizer.zero_grad()
        nll = compute_nll()
        nll.backward()
        optimizer.step()
        if _step in _snapshot_steps:
            _write_latest(_OUT_DIR / f"step_{_step}" / "pl_weekly_latest.csv", current_theta().detach().cpu().numpy())
        if _step % 50 == 0:
            _progress(_step, float(nll))

print(f"\nCompleted {MAX_STEPS} steps")

print(f"Optimization: {time.perf_counter() - _t_opt:.1f}s")

_, _, _, _, vel_per_week, acc_per_week = test_metrics(current_theta().detach())
if not _args.all_data:
    acc_icsa, ll_icsa, acc_issa, ll_issa, _, _ = test_metrics(current_theta().detach())
    print(f"\n── Test set ──────────────────────────────────────────────")
    print(f"  ICSA  pairwise accuracy : {acc_icsa*100:.2f}%  log-loss : {ll_icsa:.4f}")
    print(f"  ISSA  pairwise accuracy : {acc_issa*100:.2f}%  log-loss : {ll_issa:.4f}")
    print(f"  baseline log-loss       : {np.log(2):.4f}")
print(f"\n── Per-week motion (mean over sailors) ───────────────────")
print(f"  {'week':<12}  {'|vel|':>8}  {'|acc|':>8}")
for i in range(T - 1):
    v = float(vel_per_week[i])
    a = float(acc_per_week[i]) if i < T - 2 else float("nan")
    print(f"  {week_label(all_weeks[i+1]):<12}  {v:8.4f}  {a:8.4f}")
print(f"  Total time        : {time.perf_counter() - _t0:.1f}s")


# ── Write output ──────────────────────────────────────────────────────────────

theta_arr = current_theta().detach().cpu().numpy()   # (N, T)

if _args.school_prior > 0:
    # The level a newcomer from each school starts at, for forecasts of sailors with no races.
    _sp_path = _OUT_DIR / "pl_school_priors.csv"
    _levels = school_param.detach().cpu().numpy()
    _counts = np.bincount(sailor_group[sailor_group >= 0], minlength=len(school_keys))
    with open(_sp_path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["league", "school", "prior", "n_sailors"])
        for (lg, sch), lv, n in sorted(zip(school_keys, _levels, _counts), key=lambda x: -x[1]):
            wr.writerow([lg, sch, f"{lv:.6f}", int(n)])
    print(f"Wrote {_sp_path}  ({len(school_keys):,} schools; sd of levels {float(_levels.std()):.3f})")

if POOL:
    # Each team-season's average rating: what a newcomer to that team is expected to start near.
    _th = torch.tensor(theta_arr, device=device)
    with torch.no_grad():
        _u = torch.zeros(U, device=device).index_add_(0, ent_unit_t, _th[ent_i_t, ent_t_t]) / ent_count_unit
        _has = unit_gs_t >= 0
        _gsum = torch.zeros(G, device=device).index_add_(0, unit_gs_t[_has], _u[_has])
        _gn = torch.zeros(G, device=device).index_add_(0, unit_gs_t[_has], torch.ones(U, device=device)[_has])
    _inv = {v: k for k, v in team_index.items()}
    _tm_path = _OUT_DIR / "pl_team_means.csv"
    with open(_tm_path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["league", "school", "season", "mean_rating", "n_sailors"])
        for g_ in range(G):
            if float(_gn[g_]) > 0:
                lg, sch = _inv[g_ // S_n]
                wr.writerow([lg, sch, seasons_u[g_ % S_n], f"{float(_gsum[g_] / _gn[g_]):.6f}", int(_gn[g_])])
    print(f"Wrote {_tm_path}")

print(f"\nWriting {OUT_SERIES} …")
rows_written = 0
with open(OUT_SERIES, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["sailor_id", "name", "year", "school", "league", "iso_week", "rating"])
    for i, sid in enumerate(all_ids):
        if not sailor_active[i]:
            continue
        meta = sailor_meta[i]
        for t in sorted(sailor_active[i]):
            w.writerow([sid, meta["name"], meta["year"], meta["school"],
                        meta["league"], week_label(all_weeks[t]),
                        f"{theta_arr[i, t]:.6f}"])
            rows_written += 1

print(f"  {rows_written:,} (sailor, week) rows written")

print(f"Writing {OUT_LATEST} …")
latest: list[tuple[float, int]] = []
for i in range(N):
    if not sailor_active[i]:
        continue
    last_t = max(sailor_active[i])
    latest.append((float(theta_arr[i, last_t]), i))

latest.sort(reverse=True)
with open(OUT_LATEST, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["sailor_id", "name", "year", "school", "league", "last_week", "rating"])
    for rating, i in latest:
        meta  = sailor_meta[i]
        last_t = max(sailor_active[i])
        w.writerow([all_ids[i], meta["name"], meta["year"], meta["school"],
                    meta["league"], week_label(all_weeks[last_t]), f"{rating:.6f}"])

print(f"  {len(latest):,} sailors written")
print("\nTop 10 by latest rating:")
for rating, i in latest[:10]:
    meta   = sailor_meta[i]
    last_t = max(sailor_active[i])
    print(f"  {meta['name']:<30} {meta['year']}  {meta['school']:<25}  "
          f"{rating:.4f}  (last: {week_label(all_weeks[last_t])})")
