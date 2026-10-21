"""The rating a sailor with no earlier races starts a forecast at.

Until 2026-09-29 every newcomer got the league median of the previous season's debut
sailors.  A school's newcomers are not a random draw from the league, so now a newcomer gets
the median rating of their OWN school's debut sailors from the two seasons before the
cutoff, when the school had at least MIN_DEBUTS of them; otherwise the league median as
before.  Forward-tested on f24, s25, f25 and s26 (eval/school_prior/results.json): pairs
involving a newcomer went from 62.3% to 65.8% called right, all pairs from 69.5% to 70.4%,
better in every season.  Retraining the model with a school anchor instead was worse.

Used by evaluate_models.py, championship_points.py and regatta_form.py, so every page that
scores a forecast applies the same rule.  Everything here is forward-legal: the debut
seasons come from races before the cutoff and the ratings from the snapshot taken then.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
LEAGUES = ("ICSA", "ISSA")
MIN_DEBUTS = 2


def season_key(s: str) -> tuple:
    return (2000 + int(s[1:]), 1 if s[0] == "f" else 0)


def previous_seasons(all_seasons: list, season: str, n: int = 2) -> tuple:
    """The n seasons sailed before `season`, oldest first."""
    ordered = sorted(set(all_seasons) | {season}, key=season_key)
    i = ordered.index(season)
    return tuple(ordered[max(i - n, 0):i])


def school_keys(ids: list, sailors_csv: Path = HERE / "sailors.csv") -> np.ndarray:
    """Per sailor index, "LEAGUE|school" ("" when no school is recorded)."""
    meta = {}
    with open(sailors_csv, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            meta[r["sailor_id"]] = (r.get("league", ""), r.get("school", "").strip())
    out = np.empty(len(ids), dtype=object)
    for i, sid in enumerate(ids):
        lg, sch = meta.get(sid, ("", ""))
        out[i] = f"{lg}|{sch}" if sch else ""
    return out


def priors(S: np.ndarray, seen: np.ndarray, first_season: np.ndarray, first_league: np.ndarray,
           keys: np.ndarray, prev_seasons: tuple) -> tuple[dict, dict]:
    """(league -> prior, "LEAGUE|school" -> prior).

    League prior: median of the most recent previous season's debut sailors (the old rule).
    School prior: median of the school's debut sailors over `prev_seasons`, MIN_DEBUTS or more.
    """
    prev = prev_seasons[-1] if prev_seasons else ""
    league = {}
    for lg in LEAGUES:
        mask = (first_season == prev) & (first_league == lg)
        league[lg] = float(np.median(S[mask])) if mask.any() else (float(np.median(S[seen])) if seen.any() else 0.0)
    by_school: dict = defaultdict(list)
    debut = np.isin(first_season, list(prev_seasons)) & seen
    for i in np.flatnonzero(debut):
        if keys[i]:
            by_school[keys[i]].append(float(S[i]))
    school = {k: float(np.median(v)) for k, v in by_school.items() if len(v) >= MIN_DEBUTS}
    return league, school


def fill(S: np.ndarray, seen: np.ndarray, first_season: np.ndarray, first_league: np.ndarray,
         keys: np.ndarray, prev_seasons: tuple, race_league: str | None = None) -> np.ndarray:
    """S with every unseen sailor set to their prior.  A sailor with no recorded school, or
    whose school had too few debuts, gets the league prior (their own league, else the race's)."""
    league, school = priors(S, seen, first_season, first_league, keys, prev_seasons)
    out = S.copy()
    for i in np.flatnonzero(~seen):
        k = keys[i]
        if k in school:
            out[i] = school[k]
        else:
            lg = k.split("|")[0] if k else (race_league or LEAGUES[0])
            out[i] = league.get(lg, league[LEAGUES[0]])
    return out


def write_priors(path: Path, league: dict, school: dict) -> None:
    """For anything outside the evaluators that wants the current priors."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["league", "school", "prior"])
        for lg, v in league.items():
            w.writerow([lg, "", f"{v:.6f}"])
        for k, v in sorted(school.items(), key=lambda kv: -kv[1]):
            lg, sch = k.split("|", 1)
            w.writerow([lg, sch, f"{v:.6f}"])
