"""
Preprocess ICSA + ISSA fleet-race data for Plackett-Luce training.

Sailors are unified using sailor_clusters.csv:
  - Cross-league clusters: same sailor in ICSA and ISSA share one ID
  - Within-league clusters: same sailor at multiple schools share one ID
  - All others: unique SOLO ID per (league, name, school_slug, grade)

Reads:
  data/icsa_results.csv / issa_results.csv
  data/icsa_regattas.csv / issa_regattas.csv
  data/sailor_clusters.csv
  data/women_regattas.csv   — women's regatta names (from identify_womens_regattas.py)
  data/women_sailors.csv    — known women sailors (first, last)

Writes:
  analysis/plackett_luce/races.csv
  analysis/plackett_luce/sailors.csv

races.csv columns:
  race_id, league, season, regatta_url, boat, division, race_num,
  split (train|test), women (0|1), sailors (|‑separated IDs in finish order)

sailors.csv columns:
  sailor_id, name, year, school, league, women (0|1)
"""

import csv
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent.parent / "data"
OUT_RACES          = Path(__file__).parent / "races.csv"
OUT_SAILORS        = Path(__file__).parent / "sailors.csv"
OUT_SAILOR_SEASONS = Path(__file__).parent / "sailor_seasons.csv"

ICSA_SEASONS = {
    "f08", "s09", "f09", "s10", "f10", "s11", "f11", "s12", "f12",
    "s13", "f13", "s14", "f14", "s15", "f15", "s16", "f16", "s17",
    "f17", "s18", "f18", "s19", "f19", "s20", "f20", "s21", "f21",
    "s22", "f22", "s23", "f23", "s24", "f24", "s25", "f25", "s26", "f26",
}
ISSA_SEASONS = {
    "f13", "s14", "f14", "s15", "f15", "s16", "f16", "s17", "f17",
    "s18", "f18", "s19", "f19", "s20", "f20", "s21", "f21", "s22",
    "f22", "s23", "f23", "s24", "f24", "s25", "f25", "s26", "f26",
}

# Non-finish codes (scraper.PENALTY_SCORES).  Boats with these codes are removed from
# the finishing order so they do not count as losing to every finisher; they are
# written to the "penalties" column instead, for the separate penalty model.
# RDG (996, redress) is neither a finish nor the sailor's own outcome and is dropped.
PENALTY_CODES = {991, 992, 993, 994, 995, 997, 998, 999}
RDG_CODE = 996
NONFINISH_MIN = 990

TEST_EVERY_NTH = 10   # hold out every Nth fleet regatta (chronologically within each season)


# ── Parse helpers ─────────────────────────────────────────────────────────────

def parse_date(s):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return datetime.min


def grade_to_year(grade: str) -> str:
    m = re.search(r"'(\d{2})", grade)
    return str(2000 + int(m.group(1))) if m else ""


# ── Build sailor cluster lookup ────────────────────────────────────────────────

print("Loading sailor clusters…")
cluster_exact: dict[tuple, str] = {}      # (league, name_lower, school_slug, grade) → cluster_id
cluster_name_grade: dict[tuple, str] = {} # (league, name_lower, grade) → cluster_id
cluster_meta: dict[str, dict] = {}        # cluster_id → {name, year, school, league}

with open(DATA_DIR / "sailor_clusters.csv", newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        cid  = row["cluster_id"]
        n    = ' '.join(row["name"].strip().split()).lower()   # collapse double spaces
        g    = row["grade"].strip()
        slug = row["school_slug"]
        lg   = row["league"]
        cluster_exact[(lg, n, slug, g)]   = cid
        cluster_name_grade[(lg, n, g)]    = cid
        # keep most recent meta (ICSA preferred over ISSA for name display)
        if cid not in cluster_meta or lg == "ICSA":
            cluster_meta[cid] = {
                "name":   row["name"].strip(),
                "year":   grade_to_year(g),
                "school": row["school"].strip(),
                "league": lg,
            }

print(f"  {len(cluster_meta):,} clusters loaded")


# ── Load women's regattas and sailors ─────────────────────────────────────────

print("Loading women's data…")
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent.parent))   # repo root, for womens_rules
from womens_rules import build_index as _build_women_index, is_womens_regatta as _is_womens_regatta

women_regatta_names: set[str] = set()
with open(DATA_DIR / "women_regattas.csv", newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        women_regatta_names.add(row["regatta_name"])
# The CSV alone misses singlehanded women's events and annual events whose title carries
# the year; womens_rules layers those on top.  Shared with tag_womens.py and the server
# so all three agree on what counts as a women's regatta.
_women_stripped_index = _build_women_index(women_regatta_names)

women_sailor_pairs: set[tuple] = set()
with open(DATA_DIR / "women_sailors.csv", newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        women_sailor_pairs.add((row["first"], row["last"]))

print(f"  {len(women_regatta_names):,} women's regattas, {len(women_sailor_pairs):,} women sailors")


def parse_first_last(name: str) -> tuple[str, str]:
    s = re.sub(r"\s*'\d{2}\s*$", "", name.strip())
    parts = s.split()
    if not parts:
        return "", ""
    first = parts[0].split("-")[0].strip().lower()
    last  = parts[-1].lower() if len(parts) > 1 else ""
    return first, last


def is_women_sailor(name: str) -> bool:
    return parse_first_last(name) in women_sailor_pairs


def get_sailor_id(league: str, name: str, school_slug: str, grade: str) -> str:
    """Return cluster_id if found, else a unique SOLO: id."""
    n = ' '.join(name.strip().split()).lower()   # collapse double spaces
    g = grade.strip()
    cid = cluster_exact.get((league, n, school_slug, g)) \
       or cluster_name_grade.get((league, n, g))
    if cid:
        return f"C{cid}"
    return f"SOLO:{league}:{n}:{school_slug}:{g}"


# ── Determine test regattas per league ────────────────────────────────────────

def build_test_set(regattas_csv: Path, seasons: set) -> set[tuple]:
    """Return every TEST_EVERY_NTH-th fleet regatta (by start_date) within each season."""
    by_season: dict = defaultdict(list)
    with open(regattas_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["season"] in seasons:
                by_season[row["season"]].append(row)

    test: set[tuple] = set()
    for season, rows in by_season.items():
        fleet = [r for r in rows if r["scoring"] not in ("Team", "")]
        fleet.sort(key=lambda r: parse_date(r["start_date"]))
        for i, r in enumerate(fleet):
            if (i + 1) % TEST_EVERY_NTH == 0:
                test.add((season, r["name"]))
    return test


print("Determining test regattas…")
icsa_test = build_test_set(DATA_DIR / "icsa_regattas.csv", ICSA_SEASONS)
issa_test = build_test_set(DATA_DIR / "issa_regattas.csv", ISSA_SEASONS)
print(f"  ICSA test regattas: {len(icsa_test)}")
print(f"  ISSA test regattas: {len(issa_test)}")


# ── Load results from one league ──────────────────────────────────────────────

def load_results(
    results_csv: Path,
    league: str,
    seasons: set,
    test_regattas: set[tuple],
    is_keelboat_col: bool,
) -> tuple[dict, dict, dict, dict]:
    """
    Returns:
      raw_races      : race_key → [(place, sailor_id), ...]
      race_meta      : race_key → {league, season, regatta_url, boat, division, race_num, is_test}
      sailor_meta    : sailor_id → {name, year, school, league}
      sailor_seasons : (sailor_id, season) → {name, school, league}
    """
    raw_races:      dict = defaultdict(list)
    race_meta:      dict = {}
    sailor_meta:    dict = {}
    sailor_seasons: dict = {}   # (sailor_id, season) → {name, school, league}

    with open(results_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["season"] not in seasons:
                continue
            if is_keelboat_col and row.get("keelboat", "0").strip() == "1":
                continue
            skipper = row["skipper"].strip()
            grade   = row["skipper_grade"].strip()
            if not skipper or not grade:
                continue
            place  = int(row["place"])
            school = row["school"].strip()
            sid    = get_sailor_id(league, skipper, row["school_slug"], grade)

            # overall sailor meta (most-recent school)
            if sid not in sailor_meta:
                if sid.startswith("C"):
                    sailor_meta[sid] = dict(cluster_meta[sid[1:]])
                else:
                    sailor_meta[sid] = {
                        "name":   skipper,
                        "year":   grade_to_year(grade),
                        "school": "",
                        "league": league,
                    }
            if school:
                sailor_meta[sid]["school"] = school

            # per-season school/name (last seen wins within the season)
            sk = (sid, row["season"])
            sailor_seasons[sk] = {"name": skipper, "school": school, "league": league}

            key = (league, row["regatta_url"], row["boat"], row["division"], row["race_num"])
            raw_races[key].append((place, sid))

            if key not in race_meta:
                race_meta[key] = {
                    "league":      league,
                    "season":      row["season"],
                    "regatta_url": row["regatta_url"],
                    "boat":        row["boat"],
                    "division":    row["division"],
                    "race_num":    row["race_num"],
                    "is_test":     (row["season"], row["regatta_name"]) in test_regattas,
                    "is_women":    _is_womens_regatta(row["regatta_name"], women_regatta_names,
                                                      _women_stripped_index),
                }

    return raw_races, race_meta, sailor_meta, sailor_seasons


print("Loading ICSA results…")
icsa_races, icsa_meta, icsa_sailors, icsa_ss = load_results(
    DATA_DIR / "icsa_results.csv", "ICSA", ICSA_SEASONS, icsa_test, is_keelboat_col=True
)

print("Loading ISSA results…")
issa_races, issa_meta, issa_sailors, issa_ss = load_results(
    DATA_DIR / "issa_results.csv", "ISSA", ISSA_SEASONS, issa_test, is_keelboat_col=False
)

# Merge
all_raw_races = {**icsa_races, **issa_races}
all_race_meta = {**icsa_meta,  **issa_meta}

# Merge sailor meta: cluster sailors appear in both — keep ICSA school if available
all_sailors: dict = {**issa_sailors}
for sid, meta in icsa_sailors.items():
    if sid not in all_sailors:
        all_sailors[sid] = meta
    else:
        all_sailors[sid]["school"] = meta["school"] or all_sailors[sid]["school"]

# Merge sailor_seasons: ICSA entries override ISSA for same (sailor, season) key
all_sailor_seasons: dict = {**issa_ss, **icsa_ss}

total_rows = sum(len(v) for v in all_raw_races.values())
print(f"  ICSA: {len(icsa_races):,} races,  ISSA: {len(issa_races):,} races")
print(f"  Combined: {len(all_raw_races):,} races,  {len(all_sailors):,} sailors")


# ── Write races.csv ───────────────────────────────────────────────────────────

print(f"Writing {OUT_RACES} …")
race_count = 0
with open(OUT_RACES, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["race_id", "league", "season", "regatta_url", "boat", "division",
                "race_num", "split", "women", "sailors", "penalties"])
    n_pen = n_rdg = 0
    for race_id, (key, entries) in enumerate(all_raw_races.items()):
        entries.sort()
        ordered = [sid for place, sid in entries if place < NONFINISH_MIN]          # finishers only
        penalties = [f"{sid}:{place}" for place, sid in entries if place in PENALTY_CODES]
        n_rdg += sum(1 for place, _ in entries if place == RDG_CODE)
        if len(ordered) < 2:
            continue
        n_pen += len(penalties)
        meta  = all_race_meta[key]
        split = "test" if meta["is_test"] else "train"
        women = 1 if meta.get("is_women") else 0
        w.writerow([
            race_id, meta["league"], meta["season"], meta["regatta_url"],
            meta["boat"], meta["division"], meta["race_num"],
            split, women, "|".join(ordered), "|".join(penalties),
        ])
        race_count += 1

print(f"  {race_count:,} races written  ({n_pen:,} penalty entries kept separately, {n_rdg:,} redress entries dropped)")


# ── Write sailors.csv ─────────────────────────────────────────────────────────

print(f"Writing {OUT_SAILORS} …")
with open(OUT_SAILORS, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["sailor_id", "name", "year", "school", "league", "women"])
    for sid, meta in all_sailors.items():
        women = 1 if is_women_sailor(meta["name"]) else 0
        w.writerow([sid, meta["name"], meta["year"], meta["school"], meta.get("league", ""), women])

print(f"  {len(all_sailors):,} sailors written")


# ── Write sailor_seasons.csv ───────────────────────────────────────────────────

print(f"Writing {OUT_SAILOR_SEASONS} …")
with open(OUT_SAILOR_SEASONS, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["sailor_id", "season", "name", "school", "league", "women"])
    for (sid, season), meta in sorted(all_sailor_seasons.items()):
        women = 1 if is_women_sailor(meta["name"]) else 0
        w.writerow([sid, season, meta["name"], meta["school"], meta["league"], women])

print(f"  {len(all_sailor_seasons):,} (sailor, season) entries written")
print("Done.")
