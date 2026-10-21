"""Performance-rating points for the sailor-page rating chart (data/performance_ratings.csv, written by
analysis/plackett_luce/performance_ratings.py).  One point per division a sailor skippered: the rating their results
that weekend were worth against the boats they actually raced."""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import date as _date

_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "performance_ratings.csv")
_cache: dict = {"mtime": None, "rows": {}}


def _rows() -> dict:
    try:
        mt = os.path.getmtime(_CSV)
    except OSError:
        return {}
    if _cache["mtime"] != mt:
        rows = defaultdict(list)
        with open(_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["capped"] == "1":          # won or lost every race: no finite rating, so no point
                    continue
                rows[(r["skipper"], r["school"])].append(r)
        _cache.update(mtime=mt, rows=rows)
    return _cache["rows"]


def points(name: str, school_name: str, peers=()) -> list[dict]:
    """[{date, iso, perf, races, prior, field, regatta, division, school, league}] oldest first, or [].

    `peers` are the sailor's linked profiles as (name, school name) pairs (a high-school profile for a college sailor and back,
    transfers), so the chart shows every regatta the person sailed, whichever school they sailed it for."""
    out = []
    seen = set()
    for nm, sch in [(name, school_name), *peers]:
        if (nm, sch) in seen:
            continue
        seen.add((nm, sch))
        for r in _rows().get((nm, sch), ()):
            y, w, _ = _date.fromisoformat(r["date"]).isocalendar()
            out.append({"date": r["date"], "iso": f"{y}-W{w:02d}", "perf": round(float(r["perf"]), 2), "races": int(r["races"]),
                        "prior": round(float(r["prior"]), 2), "field": round(float(r["field_mean"]), 2),
                        "regatta": r["regatta"], "division": r["division"], "school": sch, "league": r["league"]})
    out.sort(key=lambda p: (p["date"], p["regatta"]))
    return out


# ── Regatta-results column on the sailor pages ───────────────────────────────────────────────────

_KB = {"mtime": None, "rows": {}}
_PUSH = {"mtime": None, "rows": {}}
_KB_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "performance_ratings_keelboat.csv")
_PUSH_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "crew_regatta_pushes.json")


def _keelboat() -> dict:
    """{(skipper, school, url): [(division, perf)]} from the keelboat file."""
    try:
        mt = os.path.getmtime(_KB_CSV)
    except OSError:
        return {}
    if _KB["mtime"] != mt:
        rows = defaultdict(list)
        with open(_KB_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[(r["skipper"], r["school"], r["regatta_url"])].append((r["division"], float(r["perf"])))
        _KB.update(mtime=mt, rows=rows)
    return _KB["rows"]


def _pushes() -> dict:
    try:
        mt = os.path.getmtime(_PUSH_JSON)
    except OSError:
        return {}
    if _PUSH["mtime"] != mt:
        import json
        with open(_PUSH_JSON, encoding="utf-8") as f:
            _PUSH.update(mtime=mt, rows=json.load(f))
    return _PUSH["rows"]


def regatta_perf_index(name: str, school_name: str, peers=()) -> dict:
    """{regatta url: [(division, perf)]} over the sailor and their linked profiles (fleet, singlehanded and keelboat events)."""
    out: dict = defaultdict(list)
    for nm, sch in dict.fromkeys([(name, school_name), *peers]):
        for r in _rows().get((nm, sch), ()):
            out[r["regatta_url"]].append((r["division"], float(r["perf"])))
    for (nm, sch, url), v in _keelboat().items():
        if (nm, sch) in {(name, school_name), *peers}:
            out[url].extend(v)
    return out


def results_cell(league: str, url: str, role: str, name: str, school_name: str, perf_index: dict) -> str:
    """HTML for the Rating column: a skipper's performance rating at the event, a crew's rating push at the event."""
    parts = []
    if "kipper" in role:
        divs = sorted(perf_index.get(url, ()))
        if divs:
            multi = len({d for d, _ in divs}) > 1
            parts.append(" &middot; ".join((f"{d} " if multi and d else "") + f"{p:.2f}" for d, p in divs))
    if "crew" in role.lower():
        push = _pushes().get(f"{league}|{name.lower().strip()}|{school_name.lower().strip()}", {}).get(url)
        if push is not None:
            col = "#1e5a9c" if push > 0 else "#c85a28" if push < 0 else "#555"
            parts.append(f'<span style="color:{col}" title="Rating push: how much better (+) or worse (&minus;) the skippers\' boats '
                         f'did with this crew than with their usual crews">{push:+.2f}</span>')
    return " / ".join(parts) if parts else "&mdash;"
