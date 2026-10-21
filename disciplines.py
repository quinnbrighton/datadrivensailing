"""What kind of racing an upcoming regatta is, and the ratings that go with it.

One place for the questions the predictions and the fantasy game both ask about a regatta
that has not been sailed yet:

    kind_of(season, name, scoring)   fleet, singlehanded, keelboat or team; and the crew size
    ratings(kind)                    sailor name -> rating in that discipline
    veterans(kind)                   sailors who have raced that discipline

An upcoming regatta has no results pages to read, so its kind is taken, in order, from:
  1. its Scoring field, when that says Singlehanded or Team;
  2. the last time the same regatta was sailed (data/<site>_regatta_kinds.csv, which the
     scraper fills from page layout), matched by name with the year taken off;
  3. words in its name ("match race", "singlehanded", "laser" ...), the only rule there
     used to be.
A keelboat regatta's crew size likewise comes from its last edition instead of a flat four.

Reads only files the nightly pipeline writes; holds no state beyond a cache keyed on the
files' modification times, so a long-running server picks up a rebuild by itself.
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
RATINGS_DIR = BASE / "analysis" / "disciplines"

FLEET, SINGLEHANDED, KEELBOAT, TEAM = "fleet", "singlehanded", "keelboat", "team"

SINGLEHANDED_RE = re.compile(r"singlehand|single[- ]hand|\bilca\b|\blaser\b", re.I)
MATCH_RACE_RE = re.compile(r"match[- ]rac", re.I)
KEELBOAT_RE = re.compile(r"match[- ]rac|\bkeelboat\b|\bsloop\b|\boffshore\b|\bj[- ]?(22|70|24|80|105)\b", re.I)

DEFAULT_KEELBOAT_CREW = 4      # skipper included; used when no earlier edition says otherwise


@dataclass(frozen=True)
class Kind:
    kind: str
    crew_size: int      # people aboard, skipper included; 0 = not decided here (fleet, team)
    reason: str

    @property
    def singlehanded(self) -> bool:
        return self.kind == SINGLEHANDED

    @property
    def keelboat(self) -> bool:
        return self.kind == KEELBOAT


_YEAR = re.compile(r"\b(19|20)\d{2}\b|\b\d{1,3}(st|nd|rd|th)\b|'\d{2}\b")


def name_key(name: str) -> str:
    """A regatta's name without the parts that change from year to year."""
    s = _YEAR.sub(" ", (name or "").lower())
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(w for w in s.split() if w not in ("annual", "the"))


def season_order(s: str) -> tuple:
    return (int(s[1:]), 1 if s[0] == "f" else 0) if s and s[1:].isdigit() else (0, 0)


# ── Cached file readers ───────────────────────────────────────────────────────

_cache: dict = {}


def _cached(key: str, paths: list[Path], build):
    stamp = tuple(os.path.getmtime(p) if p.exists() else 0 for p in paths)
    hit = _cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    value = build()
    _cache[key] = (stamp, value)
    return value


def _history(site: str) -> dict[str, list]:
    """name_key -> [(season, kind, crew_size)] for every edition with a known kind, newest first."""
    kinds_csv = DATA / f"{site}_regatta_kinds.csv"
    regattas_csv = DATA / f"{site}_regattas.csv"

    def build() -> dict:
        if not kinds_csv.exists() or not regattas_csv.exists():
            return {}
        with open(kinds_csv, newline="", encoding="utf-8") as f:
            kinds = {r["regatta_url"].rstrip("/"): r for r in csv.DictReader(f)}
        out: dict[str, list] = {}
        with open(regattas_csv, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                path = "/" + "/".join((r.get("url") or "").rstrip("/").split("/")[-2:])
                k = kinds.get(path)
                if not k:
                    continue
                key = name_key(r["name"])
                rec = (r["season"], k["kind"], int(k["crew_size"] or 0))
                if key:
                    out.setdefault(key, []).append(rec)
        for editions in out.values():
            editions.sort(key=lambda e: season_order(e[0]), reverse=True)
        return out

    return _cached(f"history:{site}", [kinds_csv, regattas_csv], build)


def kind_of(season: str, name: str, scoring: str = "", site: str = "icsa") -> Kind:
    """The kind of an upcoming regatta.  `season` keeps a regatta from being its own history."""
    sc = (scoring or "").strip().lower()
    if sc == "singlehanded":
        return Kind(SINGLEHANDED, 1, "scoring is Singlehanded")
    if sc == "team":
        return Kind(TEAM, 0, "scoring is Team")

    before = season_order(season) if season else (99, 9)
    past = next((e for e in _history(site).get(name_key(name), []) if season_order(e[0]) < before), None)
    if past:
        p_season, kind, crew = past
        if kind == KEELBOAT:
            # Fewer than three aboard means the hosts entered a skipper and at most one crew,
            # not that the boat was sailed two-up.
            if crew >= 3:
                return Kind(KEELBOAT, crew, f"keelboat in {p_season}, {crew} aboard")
            return Kind(KEELBOAT, DEFAULT_KEELBOAT_CREW, f"keelboat in {p_season}, crew not fully entered")
        if kind == SINGLEHANDED:
            return Kind(SINGLEHANDED, 1, f"singlehanded in {p_season}")
        if kind == TEAM:
            return Kind(TEAM, 0, f"team race in {p_season}")
        # Sailed as an ordinary fleet regatta last time: the name does not overrule that,
        # unless it says singlehanded or match race outright.
        if not (SINGLEHANDED_RE.search(name or "") or MATCH_RACE_RE.search(name or "")):
            return Kind(FLEET, 0, f"fleet in {p_season}")

    if SINGLEHANDED_RE.search(name or ""):
        return Kind(SINGLEHANDED, 1, "name")
    if KEELBOAT_RE.search(name or ""):
        return Kind(KEELBOAT, DEFAULT_KEELBOAT_CREW, "name")
    return Kind(FLEET, 0, "default")


# ── Ratings and experience ────────────────────────────────────────────────────

def _rows(kind: str, league: str) -> list[dict]:
    path = RATINGS_DIR / f"ratings_{kind}.csv"

    def build() -> list:
        if not path.exists():
            return []
        with open(path, newline="", encoding="utf-8") as f:
            return [r for r in csv.DictReader(f) if r["league"] == league]

    return _cached(f"ratings:{kind}:{league}", [path], build)


def ratings(kind: str, league: str = "ICSA") -> dict[str, float]:
    """Sailor name -> rating in this discipline.  Empty for fleet and team racing, which use
    the main ratings.  Where two sailors share a name the one who raced most recently wins."""
    if kind not in (SINGLEHANDED, KEELBOAT):
        return {}
    out: dict[str, tuple] = {}
    for r in _rows(kind, league):
        name = r["name"].strip()
        rank = season_order(r["last_season"])
        if name and (name not in out or rank > out[name][0]):
            out[name] = (rank, float(r["rating"]))
    return {n: v[1] for n, v in out.items()}


def _norm_school(school: str) -> str:
    return " ".join((school or "").lower().replace("&", "and").split())


def rating_for(kind: str, name: str, school: str = "", league: str = "ICSA") -> float | None:
    """One sailor's rating in a discipline, matched on name AND school.

    Names repeat: a Fordham "Michael Burns" must not inherit the rating of the Merchant
    Marine "Michael Burns" who left in 2022, so with a school given only a row for that
    school counts.  Without a school the name alone is used when it belongs to one sailor.
    """
    rows = [r for r in _rows(kind, league) if r["name"].strip() == (name or "").strip()]
    if not rows:
        return None
    if school:
        same = [r for r in rows if _norm_school(r["school"]) == _norm_school(school)]
        if not same:
            return None
        same.sort(key=lambda r: season_order(r["last_season"]), reverse=True)
        return float(same[0]["rating"])
    return float(rows[0]["rating"]) if len(rows) == 1 else None


def veterans(kind: str, league: str = "ICSA") -> set[str]:
    """Names of sailors who have finished a race in this discipline."""
    return set(ratings(kind, league))


def newcomer_rating(kind: str, league: str = "ICSA") -> float | None:
    """A rating for someone with no history in the discipline: the level of the least
    experienced sailors who do have one (lowest quarter by races sailed)."""
    rows = sorted(_rows(kind, league), key=lambda r: int(r["races"] or 0))
    few = rows[: max(len(rows) // 4, 1)]
    if not few:
        return None
    vals = sorted(float(r["rating"]) for r in few)
    return vals[len(vals) // 2]


# ── Boat strength (the unified model, analysis/disciplines/unified.py) ────────

def _model(kind: str) -> dict:
    import json
    path = RATINGS_DIR / f"model_{kind}.json"

    def build() -> dict:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    return _cached(f"model:{kind}", [path], build)


def _school_rows(kind: str) -> dict:
    path = RATINGS_DIR / f"school_effects_{kind}.csv"

    def build() -> dict:
        out: dict = {}
        if path.exists():
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    out[(r["league"], _norm_school(r["school"]))] = float(r["offset"])
        return out

    return _cached(f"schools:{kind}", [path], build)


def school_offset(kind: str, school: str, league: str = "ICSA") -> float:
    """The school's own difference in this discipline (0 for a school with no history there, or when the fitted
    model has no school layer, as for singlehanded)."""
    if not _model(kind).get("variant") in ("school", "full"):
        return 0.0
    return _school_rows(kind).get((league, _norm_school(school)), 0.0)


def main_rating_for(kind: str, name: str, school: str = "", league: str = "ICSA") -> float | None:
    """A sailor's latest main (fleet) rating as recorded in the discipline ratings file, matched like rating_for."""
    rows = [r for r in _rows(kind, league) if r["name"].strip() == (name or "").strip() and r.get("main_rating")]
    if school:
        rows = [r for r in rows if _norm_school(r["school"]) == _norm_school(school)]
    if not rows:
        return None
    rows.sort(key=lambda r: season_order(r["last_season"]), reverse=True)
    return float(rows[0]["main_rating"])


def boat_rating(kind: str, people: list, school: str = "", league: str = "ICSA", helm: str | None = None) -> tuple:
    """(strength, helm name) of one entry on the scale of the fitted model, or (None, None) when nobody aboard is known.

    Singlehanded: the sailor's rating.  Keelboat: the best rating aboard is the helm (those ratings are skippers'); the
    others add kc times the mean of their main ratings (c0 where none is known), and the school's program offset is added.
    """
    known = [(r, nm) for nm in people for r in [rating_for(kind, nm, school, league)] if r is not None]
    m = _model(kind)
    if helm and kind == KEELBOAT and helm in people:
        # The helm is named (a predicted squad: only the driver is ranked, whoever else is aboard).
        hr = rating_for(kind, helm, school, league)
        if hr is None:
            hr = newcomer_rating(kind, league)
        if hr is None:
            return None, None
        temp = float(m.get("temperature", 1.0))
        crew = [c for c in (main_rating_for(kind, nm, school, league) for nm in people if nm != helm) if c is not None]
        crew_term = 0.0
        if m.get("use_crew"):
            crew_term = m.get("kc", 0.0) * (sum(crew) / len(crew) if crew else m.get("c0", 0.0))
        return (hr + crew_term + school_offset(kind, school, league)) * temp, helm
    temp = float(m.get("temperature", 1.0))          # <1: the fitted model was over-confident on unseen seasons
    if kind != KEELBOAT:
        return (max(known)[0] * temp, max(known)[1]) if known else (None, None)
    off = school_offset(kind, school, league)
    if not known:
        base = newcomer_rating(kind, league)
        return (None, None) if base is None else ((base + off) * temp, people[0] if people else None)
    helm_rating, helm = max(known)
    crew = [main_rating_for(kind, nm, school, league) for nm in people if nm != helm]
    crew = [c for c in crew if c is not None]
    crew_term = 0.0
    if m.get("use_crew"):
        crew_term = m.get("kc", 0.0) * (sum(crew) / len(crew) if crew else m.get("c0", 0.0))
    return (helm_rating + crew_term + off) * temp, helm


def squad_boats(sch: dict, pool, upto_rank: int, team_size: int, exclude=frozenset()):
    """Boats 0..upto_rank for one school from its predicted-squad ranking (analysis/disciplines/squads.py), or None.

    `sch` is {'drivers': [{'name','p'}...], 'crews': {driver: [{'name','p'}...]}}; `pool` is whoever may sail right now
    (the caller applies roster, graduation, away and same-day filters), or None to allow everyone in the ranking except
    `exclude` (sailors who are away or already committed) — a match race does not need its crew to be rated.  Boat by boat the driver is the best-ranked
    sailor still in the pool and, for a keelboat (team_size > 1), the crew are the best-ranked for that driver still in
    the pool, topped up from the driver ranking.  Probabilities are renormalised over what is left.  Returns
    [{'driver': (name, prob, [(name, prob)...]), 'crew': [(name, prob, [(name, prob)...])]}].
    """
    if not sch or not sch.get("drivers"):
        return None
    taken: set = set(exclude)
    ok = (lambda n: True) if pool is None else (lambda n: n in pool)
    boats = []
    n_crew = max(0, team_size - 1)
    for _ in range(upto_rank + 1):
        drv = [d for d in sch["drivers"] if ok(d["name"]) and d["name"] not in taken]
        if not drv:
            break
        tot = sum(d["p"] for d in drv) or 1.0
        boat = {"driver": (drv[0]["name"], drv[0]["p"] / tot, [(d["name"], d["p"] / tot) for d in drv[1:6]]), "crew": []}
        taken.add(drv[0]["name"])
        if n_crew:
            ranked = [c for c in (sch.get("crews") or {}).get(drv[0]["name"], []) if ok(c["name"]) and c["name"] not in taken]
            seen = {c["name"] for c in ranked}
            ranked += [{"name": d["name"], "p": d["p"] * 0.01} for d in sch["drivers"]
                       if ok(d["name"]) and d["name"] not in taken and d["name"] not in seen]
            for _i in range(min(n_crew, len(ranked))):
                tot = sum(c["p"] for c in ranked) or 1.0
                boat["crew"].append((ranked[0]["name"], ranked[0]["p"] / tot, [(c["name"], c["p"] / tot) for c in ranked[1:5]]))
                taken.add(ranked[0]["name"])
                ranked = ranked[1:]
        boats.append(boat)
    return boats or None


def squad_boat_rating(sch: dict, driver: str, boat_rank: int = 0):
    """Calibrated rating of the boat this driver helms (analysis/disciplines/squads.py), or None without data.

    Preferred: `sch['mc_rating']`, the entry's calibrated rating averaged over a Monte Carlo of the weekend (stronger events
    take a school's likeliest drivers first, so the rating already allows for them being unavailable).  A school with
    several boats keeps the boat-to-boat differences of `sch['ratings']`, the calibrated rating with the j best-ranked drivers
    removed, around that mean.  Without a Monte Carlo, the ladder entry for the driver's place in the ranking."""
    ladder = (sch or {}).get("ratings")
    names = [d["name"] for d in (sch or {}).get("drivers", [])]
    pos = names.index(driver) if driver in names else len(ladder or [])
    mc = (sch or {}).get("mc_rating")
    if mc is not None:
        n = int(sch.get("boats", 1))
        if n > 1 and ladder:
            return mc + ladder[min(boat_rank, len(ladder) - 1)] - sum(ladder[:min(n, len(ladder))]) / min(n, len(ladder))
        return mc
    if not ladder:
        return None
    return ladder[min(max(0, pos - boat_rank), len(ladder) - 1)]
