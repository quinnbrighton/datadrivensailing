"""
ISSA Regatta Scraper
Scrapes scores.hssailing.org for fleet-race and team-race regatta results.

Fleet race DB (issa_results.db) — race_results table:
  season, regatta_name, regatta_url, boat, division, race_num, place,
  school, school_slug, team, skipper, skipper_grade, crew, crew_grade

Team race DB (issa_tr_results.db):
  tr_standings: season, regatta_name, regatta_url, boat, place,
                school, school_slug, team, wins, losses, win_pct
  tr_sailors:   regatta_url, school_slug, team,
                skipper, skipper_grade, crew, crew_grade
"""

import re
import sqlite3
import csv
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup, NavigableString

import event_kinds

WORKERS = 20

ISSA_KEELBOATS = {"J22", "J70", "Sonar", "IOD", "Etchells"}
ICSA_KEELBOATS = {
    "Catalina 37", "Navy 44", "J-70", "J-22", "Colgate 26",
    "Tartan 10", "Sonar", "Rhodes", "Flying Scot", "Match Race 3-4",
}

DB_DIR   = Path("db")
DATA_DIR = Path("data")

SITES = {
    "issa": {
        "base_url":      "https://scores.hssailing.org",
        "db_path":       DB_DIR   / "issa_results.db",
        "csv_path":      DATA_DIR / "issa_results.csv",
        "tr_db_path":    DB_DIR   / "issa_tr_results.db",
        "tr_csv_prefix": str(DATA_DIR / "issa_tr_"),
        "kb_prefix":     str(DATA_DIR / "issa_keelboat_"),
        "keelboats":     ISSA_KEELBOATS,
    },
    "icsa": {
        "base_url":      "https://scores.collegesailing.org",
        "db_path":       DB_DIR   / "icsa_results.db",
        "csv_path":      DATA_DIR / "icsa_results.csv",
        "tr_db_path":    DB_DIR   / "icsa_tr_results.db",
        "tr_csv_prefix": str(DATA_DIR / "icsa_tr_"),
        "kb_prefix":     str(DATA_DIR / "icsa_keelboat_"),
        "keelboats":     ICSA_KEELBOATS,
    },
}

# Defaults (overridden by --site)
BASE_URL = SITES["issa"]["base_url"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ScoreEntry:
    school_slug: str
    team_name: str
    division: str
    race_scores: dict   # {race_num: place}
    race_codes: dict = field(default_factory=dict)   # {race_num: (code, points)} for coded results
    penalty: tuple = ("", None)   # team penalty on the division as a whole, e.g. ("LOP", 20)
    sailor: str = ""              # singlehanded: the row is a sailor, not a team


@dataclass
class SailorSlot:
    school: str
    school_slug: str
    team: str
    division: str
    skipper: str
    skipper_grade: str
    skipper_races: set
    crew: str
    crew_grade: str
    crew_races: set


@dataclass
class RaceResult:
    season: str
    regatta_name: str
    regatta_url: str
    boat: str
    division: str
    race_num: int
    place: int
    school: str
    school_slug: str
    team: str
    skipper: str
    skipper_grade: str
    crew: str
    crew_grade: str
    code: str = ""                  # DNF, DSQ, BKD ... empty for a plain finish
    points: Optional[int] = None    # what the results site scored the race at
    div_penalty: str = ""           # team penalty on this boat's division (same on each of its rows)
    div_penalty_points: Optional[int] = None


@dataclass
class KeelboatResult:
    season: str
    regatta_name: str
    regatta_url: str
    boat: str
    division: str
    race_num: int
    place: int
    school: str
    school_slug: str
    team: str
    code: str = ""
    points: Optional[int] = None
    div_penalty: str = ""
    div_penalty_points: Optional[int] = None


@dataclass
class KeelboatSailor:
    regatta_url: str
    school_slug: str
    school: str
    team: str
    division: str
    sailor_name: str
    grade: str
    races: str
    role: str = ""      # skipper, crew or reserve


@dataclass
class RegattaKind:
    regatta_url: str
    season: str
    kind: str           # fleet, singlehanded, keelboat or team (event_kinds.KINDS)
    crew_size: int      # people aboard one boat, skipper included; 0 when unknown
    boat: str
    scoring: str
    reason: str


@dataclass
class RotationEntry:
    regatta_url: str
    school_slug: str
    team: str
    division: str
    race_num: int
    sail_num: int


@dataclass
class TRRotationMatch:
    regatta_url: str
    round_name: str
    race_num: int
    slug1: str
    name1: str
    team1: str
    sails1: str   # comma-separated sail numbers
    slug2: str
    name2: str
    team2: str
    sails2: str   # comma-separated sail numbers


@dataclass
class TRMatchResult:
    regatta_url: str
    round_name: str
    team_slug: str    # row team
    team_label: str   # display name + mascot
    opp_slug: str     # column opponent
    result: str       # 'W', 'L', or 'X'
    score: str        # e.g. "1-4-5"
    record: str       # round record, e.g. "4-1"


@dataclass
class TRStanding:
    season: str
    regatta_name: str
    regatta_url: str
    boat: str
    place: int
    school: str
    school_slug: str
    team: str
    wins: int
    losses: int
    win_pct: float
    team_id: str = ""   # internal join key, not stored


@dataclass
class TRSailor:
    regatta_url: str
    school_slug: str
    team: str
    skipper: str
    skipper_grade: str
    crew: str
    crew_grade: str


@dataclass
class TRMatchSailor:
    regatta_url: str
    match_num: int
    side: str           # 'winner' or 'loser'
    boat: str           # 'A', 'B', 'C'
    school_slug: str
    team: str
    skipper: str
    skipper_grade: str
    crew: str
    crew_grade: str


@dataclass
class TRMatch:
    season: str
    regatta_name: str
    regatta_url: str
    boat: str
    round_num: int
    match_num: int
    winner_school: str
    winner_slug: str
    winner_team: str
    winner_positions: str   # e.g. "1-2-4"
    loser_school: str
    loser_slug: str
    loser_team: str
    loser_positions: str    # e.g. "3-5-6"


# ---------------------------------------------------------------------------
# HTTP — thread-local sessions (no delay)
# ---------------------------------------------------------------------------

_thread_local = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers["User-Agent"] = "Mozilla/5.0 (compatible; ISSAScraper/1.0)"
        _thread_local.session = s
    return _thread_local.session


def fix_encoding(s: str) -> str:
    """Reverse double-encoded UTF-8 text (UTF-8 bytes misread as latin-1, then re-encoded).
    E.g. â€™ -> ' (right single quote), â€" -> – (en dash), etc."""
    if not s:
        return s
    try:
        fixed = s.encode("latin-1").decode("utf-8")
        return fixed if fixed != s else s
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s

def get(url: str) -> Optional[BeautifulSoup]:
    try:
        r = get_session().get(url, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        # Use r.content (bytes) so lxml detects encoding from HTML meta charset,
        # avoiding the requests latin-1 fallback that causes double-encoding of UTF-8 text.
        return BeautifulSoup(r.content, "lxml")
    except requests.RequestException as e:
        log.warning(f"Request failed: {url} — {e}")
        return None


# ---------------------------------------------------------------------------
# Season codes
# ---------------------------------------------------------------------------

def get_all_seasons(base_url: str) -> list[str]:
    """Fetch season list from /seasons/ page, oldest first."""
    soup = get(f"{base_url}/seasons/")
    if not soup:
        return []
    season_pattern = re.compile(r"^[sf]\d{2}$")
    seen, seasons = set(), []
    for a in soup.find_all("a", href=True):
        s = a["href"].strip("/")
        if season_pattern.match(s) and s not in seen:
            seen.add(s)
            seasons.append(s)
    # Return oldest → newest (reverse the page order)
    return list(reversed(seasons))


def get_regattas(season: str, base_url: str) -> list[tuple[str, str]]:
    soup = get(f"{base_url}/{season}/")
    if not soup:
        return []
    slug_pattern = re.compile(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$")
    seen, regattas = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if slug_pattern.match(href):
            name = a.get_text(strip=True)
            if href not in seen and name:
                seen.add(href)
                regattas.append((name, href))
    return regattas


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A coded result is stored as a place of 990 or more, so nothing downstream mistakes it
# for a finishing position.  The code itself and the points awarded are kept beside it.
PENALTY_SCORES = {
    "DNF": 999, "DNS": 998, "DNC": 997, "RDG": 996,
    "DGM": 995, "UFD": 994, "BFD": 993, "OCS": 992, "RAF": 991,
    "DSQ": 990,
    # Breakdown and bye are scored as an average, like redress: not the sailor's own
    # finish and not a penalty either, so they share the redress value.
    "BKD": 996, "BYE": 996,
}
# Any other three-letter code the results site may add: kept as a non-finish rather
# than dropped, which is what left cells blank before.
UNKNOWN_CODE_SCORE = 990


def parse_score_cell(cell) -> tuple[Optional[int], str, Optional[int]]:
    """(place, code, points) for one race cell.  For a coded cell the points come from
    its tooltip, e.g. title="(9, Fleet + 1)" or title="(2: average in division)"."""
    raw = cell.get_text(strip=True)
    place = parse_score(raw)
    if place is None:
        m = re.fullmatch(r"[A-Za-z]{3}", raw)
        if not m:
            return None, "", None
        place = UNKNOWN_CODE_SCORE
    if place < 900:
        return place, "", place
    m = re.search(r"[A-Za-z]{3}", raw)
    code = m.group(0).upper() if m else ""
    pts = re.search(r"\((\d+)", cell.get("title", "") or "")
    return place, code, (int(pts.group(1)) if pts else None)


def parse_score(raw: str) -> Optional[int]:
    raw = raw.strip()
    if not raw or raw in ("—", "-"):
        return None
    for code, val in PENALTY_SCORES.items():
        if code in raw.upper():
            return val
    m = re.match(r"^(\d+)", raw)
    return int(m.group(1)) if m else None


def school_slug_from_href(href: str) -> str:
    m = re.search(r"/schools/([^/]+)/", href)
    return m.group(1) if m else ""


def normalize_name(text: str) -> str:
    """Clean a raw sailor name scraped from the site.

    - Strip surrounding whitespace
    - Remove asterisks and other stray punctuation (*, #, ^, ~, +)
    - Remove any embedded digits
    - Collapse multiple spaces into one
    - Title-case the result
    """
    s = text.strip()
    s = re.sub(r"[*#^~+]", "", s)   # remove common annotation characters
    s = re.sub(r"\d+", "", s)        # remove digits (year already parsed separately)
    s = re.sub(r"\s+", " ", s)       # collapse whitespace
    s = s.strip()
    if s:
        s = s.title()
    return s


def split_name_grade(text: str) -> tuple[str, str]:
    # Strip trailing annotation characters (*#^~+ and whitespace) before year
    # extraction so that formats like "John Smith '26 *" parse correctly.
    cleaned = re.sub(r"[\s*#^~+]+$", "", text)
    # Extract trailing year token like '26
    m = re.search(r"'(\d{2})$", cleaned)
    if m:
        grade = f"'{m.group(1)}"
        name  = normalize_name(cleaned[:m.start()])
        return name, grade
    # Also handle bare 4-digit year appended (e.g. "John Smith 2026")
    m4 = re.search(r"\b(20\d{2})$", cleaned)
    if m4:
        yr   = m4.group(1)[-2:]
        name = normalize_name(cleaned[:m4.start()])
        return name, f"'{yr}"
    # Strip a bare trailing apostrophe (year marker with no parseable digits)
    cleaned = re.sub(r"\s*'$", "", cleaned)
    return normalize_name(cleaned), ""


def parse_race_range(text: str) -> set:
    races = set()
    for part in text.replace(" ", "").split(","):
        m = re.match(r"(\d+)-(\d+)", part)
        if m:
            races.update(range(int(m.group(1)), int(m.group(2)) + 1))
        elif re.match(r"^\d+$", part):
            races.add(int(part))
    return races


def get_page_info(soup: BeautifulSoup, wanted: str) -> str:
    """A field from the regatta's info box: Host, Date, Type, Boat, Scoring."""
    for li in soup.select("ul#page-info li"):
        key = li.find(class_="page-info-key")
        val = li.find(class_="page-info-value")
        if key and val and key.get_text(strip=True) == wanted:
            return val.get_text(strip=True)
    return ""


# What kind of event each regatta scraped in this run turned out to be.  The scrape tasks
# run on worker threads and return a fixed tuple, so the kinds are collected here and
# written by save_regatta_kinds() once the workers are done.
_KINDS: dict[str, RegattaKind] = {}
_KINDS_LOCK = threading.Lock()


def note_kind(regatta_url: str, season: str, kind: "event_kinds.EventKind", boat: str, scoring: str) -> None:
    with _KINDS_LOCK:
        _KINDS[regatta_url] = RegattaKind(regatta_url, season, kind.kind, kind.crew_size,
                                          boat, scoring, kind.reason)


def keelboat_sailors_from_entries(entries: list, regatta_url: str) -> list[KeelboatSailor]:
    """Everyone aboard each keelboat, with the role the sailors page gives them."""
    out, seen = [], set()
    for e in entries:
        for m in e.members:
            key = (e.school_slug, e.team, e.division, m.name)
            if key in seen:
                continue
            seen.add(key)
            out.append(KeelboatSailor(
                regatta_url=regatta_url, school_slug=e.school_slug, school=e.school,
                team=e.team, division=e.division, sailor_name=m.name, grade=m.grade,
                races=m.races_text, role=m.role))
    return out


def get_boat_class(soup: BeautifulSoup) -> str:
    """Extract boat class from the regatta landing page (e.g. 'C420', 'CFJ')."""
    for li in soup.select("ul#page-info li"):
        key = li.find(class_="page-info-key")
        val = li.find(class_="page-info-value")
        if key and val and key.get_text(strip=True) == "Boat":
            return val.get_text(strip=True)
    return ""


def is_team_race(full_scores_soup: BeautifulSoup) -> bool:
    table = full_scores_soup.find("table")
    if not table:
        return False
    header = table.find("tr")
    if not header:
        return False
    headers = [c.get_text(strip=True) for c in header.find_all(["th", "td"])]
    return "Rec." in headers


# ---------------------------------------------------------------------------
# Fleet race: parse full-scores
# ---------------------------------------------------------------------------

def parse_full_scores(soup: BeautifulSoup) -> list[ScoreEntry]:
    """
    Format 1 — header has 'Div.' column:
      divA row: cells[2]=school_link, cells[div_col]='A', cells[race...]
      divB row: cells[2]=team_name,   cells[div_col]='B', cells[race...]

    Format 2 — no 'Div.' column; one row per team:
      divA/B row: cells[2]='TeamName<br/><school_link>', cells[race...]
      Division from row CSS class.
    """
    entries = []
    table = soup.find("table")
    if not table:
        return entries

    rows = table.find_all("tr")
    if not rows:
        return entries

    header_cells = rows[0].find_all(["th", "td"])

    div_col: Optional[int] = None
    for i, cell in enumerate(header_cells):
        if re.match(r"^div", cell.get_text(strip=True), re.IGNORECASE):
            div_col = i
            break

    race_cols: list[tuple[int, int]] = []
    for i, cell in enumerate(header_cells):
        text = cell.get_text(strip=True)
        if re.match(r"^\d+$", text):
            race_cols.append((i, int(text)))
        else:
            title = cell.get("title", "")
            m = re.match(r"Race\s*(\d+)", title, re.IGNORECASE)
            if m:
                race_cols.append((i, int(m.group(1))))

    # Team penalties (LOP, PFD, MRP ...) sit in an unlabelled column just before the total.
    tot_col: Optional[int] = None
    for i, cell in enumerate(header_cells):
        if cell.get_text(strip=True).upper() == "TOT":
            tot_col = i
    pen_col: Optional[int] = None
    if tot_col and not header_cells[tot_col - 1].get_text(strip=True) \
            and (tot_col - 1) not in {c for c, _ in race_cols}:
        pen_col = tot_col - 1

    def extract_penalty(cells, scores, codes):
        """(code, points) for a team penalty on this row.  The points are whatever the
        row total holds beyond its races; 20, the standard penalty, when that cannot be worked out."""
        if pen_col is None or pen_col >= len(cells):
            return ("", None)
        m = re.fullmatch(r"[A-Za-z]{2,4}", cells[pen_col].get_text(strip=True))
        if not m:
            return ("", None)
        race_pts = [codes[r][1] if r in codes else p for r, p in scores.items()]
        tot = cells[tot_col].get_text(strip=True) if tot_col < len(cells) else ""
        if tot.isdigit() and all(p is not None for p in race_pts) and int(tot) > sum(race_pts):
            return (m.group(0).upper(), int(tot) - sum(race_pts))
        return (m.group(0).upper(), 20)

    def extract_race_scores(cells):
        scores, codes = {}, {}
        for col_idx, race_num in race_cols:
            if col_idx >= len(cells):
                continue
            s, code, pts = parse_score_cell(cells[col_idx])
            if s is not None:
                scores[race_num] = s
                if code:
                    codes[race_num] = (code, pts)
        return scores, codes

    def div_from_row_class(tr) -> str:
        for cls in (tr.get("class") or []):
            m = re.match(r"div([A-Za-z])", cls)
            if m:
                return m.group(1).upper()
        return ""

    def team_name_from_cell(cell) -> str:
        parts = [str(t).strip() for t in cell.children if isinstance(t, NavigableString)]
        return " ".join(p for p in parts if p)

    current_block: list = []

    def flush_block(block):
        if not block:
            return []
        entries_out = []

        if div_col is not None:
            school_slug, team_name = "", ""
            for tr, cells in block:
                if len(cells) > 2:
                    link = cells[2].find("a", href=re.compile(r"/schools/"))
                    if link:
                        school_slug = school_slug_from_href(link["href"])
                        break
            if not school_slug:
                return []
            for tr, cells in block:
                if len(cells) > 2:
                    link = cells[2].find("a", href=re.compile(r"/schools/"))
                    display = cells[2].get_text(strip=True)
                    if not link and display:
                        team_name = display
            if not team_name:
                for tr, cells in block:
                    if len(cells) > 2:
                        link = cells[2].find("a", href=re.compile(r"/schools/"))
                        if link:
                            team_name = link.get_text(strip=True)
                            break
            for tr, cells in block:
                if len(cells) <= div_col:
                    continue
                division = cells[div_col].get_text(strip=True).upper()
                if not re.match(r"^[A-Z]$", division):
                    continue
                scores, codes = extract_race_scores(cells)
                if scores:
                    entries_out.append(ScoreEntry(school_slug, team_name, division, scores, codes,
                                                  extract_penalty(cells, scores, codes)))
        else:
            for tr, cells in block:
                if len(cells) < 3:
                    continue
                team_cell = cells[2]
                link = team_cell.find("a", href=re.compile(r"/schools/"))
                if not link:
                    continue
                slug = school_slug_from_href(link["href"])
                if not slug:
                    continue
                tname = team_name_from_cell(team_cell) or link.get_text(strip=True)
                division = div_from_row_class(tr) or "A"
                scores, codes = extract_race_scores(cells)
                if scores:
                    # Singlehanded: the cell names the sailor, and a school may have several.
                    span = team_cell.find(class_="singlehanded-sailor-span")
                    sailor = split_name_grade(span.get_text(strip=True))[0] if span else ""
                    entries_out.append(ScoreEntry(slug, tname, division, scores, codes,
                                                  extract_penalty(cells, scores, codes), sailor))

        return entries_out

    for tr in rows[1:]:
        tr_classes = tr.get("class") or []
        if "totalrow" in tr_classes:
            entries.extend(flush_block(current_block))
            current_block = []
            continue
        cells = tr.find_all(["td", "th"])
        if len(cells) >= 3:
            current_block.append((tr, cells))

    entries.extend(flush_block(current_block))
    return entries


# ---------------------------------------------------------------------------
# Fleet race: parse sailors
# ---------------------------------------------------------------------------

def parse_sailors(soup: BeautifulSoup, scores=None, school_lookup: dict | None = None) -> list[SailorSlot]:
    slots = []
    table = soup.find("table")
    if not table:
        return slots

    current_school = current_school_slug = current_team = current_division = ""

    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        if any("reserves-cell" in (c.get("class") or []) for c in cells):
            continue
        if cells[0].get("colspan"):
            continue

        new_team_row = False
        for c in cells:
            if "schoolname" in (c.get("class") or []):
                new_team_row = True
                a = c.find("a", href=re.compile(r"/schools/"))
                if a:
                    current_school = a.get_text(strip=True)
                    current_school_slug = school_slug_from_href(a["href"])
                else:
                    current_school = c.get_text(strip=True)
                    current_school_slug = ""
            if "teamname" in (c.get("class") or []):
                current_team = c.get_text(strip=True)

        for c in cells:
            if "division-cell" in (c.get("class") or []):
                current_division = c.get_text(strip=True).upper()

        # Older pages leave the school unlinked (and may put school and team in one cell): the scores page names every
        # boat with a slug, so find this one there rather than dropping its sailors.
        if new_team_row and not current_school_slug and scores:
            slug, team = event_kinds._resolve_school(current_school, current_team, current_division, scores)
            if slug:
                current_school_slug, current_team = slug, team
                current_school = (school_lookup or {}).get(slug, current_school)

        if not current_school_slug:
            continue

        structural = {"schoolname", "teamname", "division-cell", "rank-cell"}
        data_cells = [c for c in cells if not (set(c.get("class") or []) & structural)]

        if len(data_cells) < 2:
            continue

        skipper_raw = data_cells[0].get_text(strip=True)
        skipper_races_raw = data_cells[1].get_text(strip=True) if "races" in (data_cells[1].get("class") or []) else ""
        crew_raw = data_cells[2].get_text(strip=True) if len(data_cells) > 2 else ""
        crew_races_raw = data_cells[3].get_text(strip=True) if len(data_cells) > 3 and "races" in (data_cells[3].get("class") or []) else ""

        # A row can hold a substitute crew alone, under the skipper they sailed with.
        if not skipper_raw and not crew_raw:
            continue

        skipper, s_grade = split_name_grade(skipper_raw)
        crew, c_grade = split_name_grade(crew_raw)

        slots.append(SailorSlot(
            school=current_school, school_slug=current_school_slug,
            team=current_team, division=current_division,
            skipper=skipper, skipper_grade=s_grade,
            skipper_races=parse_race_range(skipper_races_raw),
            crew=crew, crew_grade=c_grade,
            crew_races=parse_race_range(crew_races_raw),
        ))

    return slots


# ---------------------------------------------------------------------------
# Fleet race: join scores + sailors
# ---------------------------------------------------------------------------

def school_lookup_for(base_url: str) -> dict:
    """school slug -> display name from the site's schools list (data/<site>_schools.csv).

    The name a boat is stored under when its sailors-page row carries no school (older pages put school and team in one
    unlinked cell, and some boats have no roster at all).  Without it the team name ("Hoosiers") was stored as the school."""
    site = "issa" if "hssailing" in base_url else "icsa"
    path = DATA_DIR / f"{site}_schools.csv"
    out: dict = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[r["slug"]] = r["school"]
    except OSError:
        pass
    return out


def join_results(
    season: str, regatta_name: str, regatta_url: str, boat: str,
    score_entries: list[ScoreEntry], sailor_slots: list[SailorSlot],
    school_lookup: dict | None = None,
) -> list[RaceResult]:
    sailor_map: dict[tuple, list[SailorSlot]] = {}
    for s in sailor_slots:
        sailor_map.setdefault((s.school_slug, s.team, s.division), []).append(s)

    sailor_map_nodiv: dict[tuple, list[SailorSlot]] = {}
    for s in sailor_slots:
        sailor_map_nodiv.setdefault((s.school_slug, s.team), []).append(s)

    sailor_map_slug_div: dict[tuple, list[SailorSlot]] = {}
    for s in sailor_slots:
        sailor_map_slug_div.setdefault((s.school_slug, s.division), []).append(s)

    school_names = {s.school_slug: s.school for s in sailor_slots}

    # Team names that belong to some entry on the scores page.
    claimed = {(e.school_slug, e.team_name) for e in score_entries if not e.sailor}
    claimed |= {(s.school_slug, s.team) for e in score_entries if e.sailor
                for s in sailor_slots if s.school_slug == e.school_slug and s.skipper == e.sailor}

    school_lookup = school_lookup or {}
    results = []
    same_name_seen: dict = {}
    for entry in score_entries:
        if entry.sailor:
            # Singlehanded: find this sailor's own entry ("Green Wave 2"), so boats from one
            # school stay separate instead of all landing on the school's first team.  Two sailors of one
            # school with the same name take the sailors page's entries in turn, not the first one twice
            # (that dropped the second boat as a duplicate row).
            own = [s for s in sailor_slots
                   if s.school_slug == entry.school_slug and s.skipper == entry.sailor]
            nth = same_name_seen.get((entry.school_slug, entry.sailor), 0)
            same_name_seen[(entry.school_slug, entry.sailor)] = nth + 1
            if len(own) > 1:
                own = own[min(nth, len(own) - 1):]
            # Older pages give every sailor of a school the school's name as their team; boats of one school are then
            # told apart by the sailor, as they are when there is no sailors-page row at all.
            shared = own and sum(1 for sl in sailor_slots
                                 if sl.school_slug == entry.school_slug and sl.team == own[0].team
                                 and sl.skipper != entry.sailor) > 0
            if shared:
                own = []
            entry.team_name = own[0].team if own else entry.sailor
            slots = own[:1] or [SailorSlot(
                school=school_lookup.get(entry.school_slug, entry.team_name), school_slug=entry.school_slug, team=entry.sailor,
                division=entry.division, skipper=entry.sailor, skipper_grade="",
                skipper_races=set(), crew="", crew_grade="", crew_races=set())]
            if not own:
                school_slots = [s for s in sailor_slots if s.school_slug == entry.school_slug]
                if school_slots:
                    slots[0].school = school_slots[0].school
        else:
            slots = sailor_map.get((entry.school_slug, entry.team_name, entry.division))
        if not slots:
            all_slots = sailor_map_nodiv.get((entry.school_slug, entry.team_name), [])
            slots = [s for s in all_slots if s.division == entry.division] or all_slots
        if not slots:
            # Last resort, for a team whose name differs between the two pages: the school's
            # sailors in this division.  Never another entry's sailors, though.  A boat with
            # no sailors reported ("Seahawks 2", all DNS) used to borrow its sister boat's
            # sailors and team name, and was then dropped as a duplicate of it.
            slots = [s for s in sailor_map_slug_div.get((entry.school_slug, entry.division), [])
                     if (s.school_slug, s.team) not in claimed]

        for race_num, place in entry.race_scores.items():
            skipper_slot = next((s for s in slots if race_num in s.skipper_races), slots[0] if slots else None)
            crew_slot = next((s for s in slots if race_num in s.crew_races), skipper_slot)

            results.append(RaceResult(
                season=season, regatta_name=regatta_name, regatta_url=regatta_url, boat=boat,
                division=entry.division, race_num=race_num, place=place,
                school=(skipper_slot.school if skipper_slot
                        else school_names.get(entry.school_slug) or school_lookup.get(entry.school_slug, entry.team_name)),
                school_slug=entry.school_slug,
                team=skipper_slot.team if skipper_slot else entry.team_name,
                skipper=skipper_slot.skipper if skipper_slot else "",
                skipper_grade=skipper_slot.skipper_grade if skipper_slot else "",
                crew=crew_slot.crew if crew_slot else "",
                crew_grade=crew_slot.crew_grade if crew_slot else "",
                code=entry.race_codes.get(race_num, ("", None))[0],
                points=entry.race_codes.get(race_num, ("", place if place < 900 else None))[1],
                div_penalty=entry.penalty[0], div_penalty_points=entry.penalty[1],
            ))

    return results


# ---------------------------------------------------------------------------
# Fleet race: parse rotations
# ---------------------------------------------------------------------------

def parse_rotations(soup: BeautifulSoup, regatta_url: str) -> list[RotationEntry]:
    """Parse /rotations/ page — one entry per (school, division, race).

    Header formats:
      ICSA / multi-div ISSA: "1A", "2A", "1B", "2B" — division embedded in cell text
      Single-div ISSA:       "1", "2", "3" — division from preceding h2/h3/h4 heading
    """
    entries = []
    for table in soup.find_all("table", class_="rotation"):
        # Try to get division from heading above the table (single-div ISSA format)
        heading_div = ""
        prev = table.find_previous(["h2", "h3", "h4"])
        if prev:
            m = re.search(r"\b([A-Z])\s+[Dd]ivision", prev.get_text(strip=True))
            if m:
                heading_div = m.group(1).upper()

        header_row = table.find("thead").find("tr") if table.find("thead") else None
        if not header_row:
            continue
        header_cells = header_row.find_all(["th", "td"])
        col_map = {}  # col_index → (race_num, division)
        for i, cell in enumerate(header_cells):
            text = cell.get_text(strip=True)
            m = re.match(r"^(\d+)([A-Za-z])$", text)
            if m:
                col_map[i] = (int(m.group(1)), m.group(2).upper())
            elif re.match(r"^\d+$", text) and heading_div:
                col_map[i] = (int(text), heading_div)

        if not col_map:
            continue

        tbody = table.find("tbody")
        if not tbody:
            continue
        for row in tbody.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            link = cells[1].find("a", href=re.compile(r"/schools/"))
            if not link:
                continue
            slug = school_slug_from_href(link["href"])
            if not slug:
                continue
            # Team name is the NavigableString(s) after the school link
            from bs4 import NavigableString as _NS
            team_parts = [str(t).strip() for t in cells[1].children
                          if isinstance(t, _NS) and str(t).strip()]
            team = " ".join(team_parts)
            for i, (race_num, division) in col_map.items():
                if i >= len(cells):
                    continue
                raw = cells[i].get_text(strip=True)
                m = re.match(r"^(\d+)", raw)
                if m:
                    entries.append(RotationEntry(
                        regatta_url=regatta_url,
                        school_slug=slug,
                        team=team,
                        division=division,
                        race_num=race_num,
                        sail_num=int(m.group(1)),
                    ))
    return entries


# ---------------------------------------------------------------------------
# Team race: parse rotation matchup tables
# ---------------------------------------------------------------------------

def parse_tr_rotations(soup: BeautifulSoup, regatta_url: str) -> list[TRRotationMatch]:
    """Parse tr-rotation-table tables on a /rotations/ page.

    Each row is one match: race_num, team1 (slug/name/team/sails), team2.
    Sail cells have class 'sail team1' or 'sail team2'; sail number is the
    leading digits before any <span> child.
    """
    entries = []
    for table in soup.find_all("table", class_="tr-rotation-table"):
        current_round = ""
        for row in table.find_all("tr"):
            # Flight/round divider row
            if row.get("class") and "tr-flight" in row["class"]:
                current_round = row.get_text(strip=True)
                continue
            # Skip header rows
            if row.find("th"):
                continue

            cells = row.find_all("td")
            if len(cells) < 8:
                continue

            race_num_text = cells[0].get_text(strip=True)
            if not race_num_text.isdigit():
                continue
            race_num = int(race_num_text)

            # Find the td cells containing school links for team1 and team2
            td_t1 = next(
                (c for c in cells if "team1" in c.get("class", []) and c.find("a")), None
            )
            td_t2 = next(
                (c for c in cells if "team2" in c.get("class", []) and c.find("a")), None
            )
            if not td_t1 or not td_t2:
                continue

            def parse_team_cell(td):
                link = td.find("a")
                if not link:
                    return "", "", ""
                slug = school_slug_from_href(link["href"])
                name = link.get_text(strip=True)
                team = "".join(
                    str(s) for s in link.next_siblings if isinstance(s, NavigableString)
                ).strip()
                return slug, name, team

            def parse_sails(cells, side):
                sails = []
                for c in cells:
                    cls = c.get("class", [])
                    if "sail" in cls and side in cls:
                        # Sail number is the direct text before any <span>
                        raw = c.get_text(strip=True)
                        m = re.match(r"^(\d+)", raw)
                        if m:
                            sails.append(m.group(1))
                return ",".join(sails)

            slug1, name1, team1 = parse_team_cell(td_t1)
            slug2, name2, team2 = parse_team_cell(td_t2)
            sails1 = parse_sails(cells, "team1")
            sails2 = parse_sails(cells, "team2")

            if slug1 and slug2:
                entries.append(TRRotationMatch(
                    regatta_url=regatta_url,
                    round_name=current_round,
                    race_num=race_num,
                    slug1=slug1, name1=name1, team1=team1, sails1=sails1,
                    slug2=slug2, name2=name2, team2=team2, sails2=sails2,
                ))
    return entries


# ---------------------------------------------------------------------------
# Team race: parse full-scores match result grids
# ---------------------------------------------------------------------------

def parse_tr_results(soup: BeautifulSoup, regatta_url: str) -> list[TRMatchResult]:
    """Parse /full-scores/ page for team race result matrices.

    Each `table.teamscores` is one round.  The first row (class `tr-cols`)
    has column headers (`th.tr-vert-label`).  Subsequent rows have
    `th.tr-horiz-label` (row-team label) and result cells with classes
    `tr-win`, `tr-lose`, or `tr-ns`.

    Slug mapping is built from the `table.teamranking` summary table which
    contains school links.  Column/row headers are plain text so we match
    them against the slug map by prefix.
    """
    # ── Build name → slug from the summary rankings table ────────────────────
    name_to_slug: dict[str, str] = {}
    ranking_table = soup.find("table", class_="teamranking")
    if ranking_table:
        for link in ranking_table.find_all("a", href=re.compile(r"/schools/")):
            slug = school_slug_from_href(link["href"])
            name = link.get_text(strip=True)
            if slug and name:
                name_to_slug[name.lower()] = slug

    # Also build slug→name for reverse lookup
    slug_to_name = {v: k for k, v in name_to_slug.items()}

    def slug_for_label(label: str) -> str:
        """Match a label like 'Boston University Terriers' or 'MIT' to a slug."""
        low = label.lower().strip()
        # 1. Exact name match
        if low in name_to_slug:
            return name_to_slug[low]
        # 2. Label is prefix of a school name (e.g. row label "Boston University Terriers")
        for name, slug in name_to_slug.items():
            if low.startswith(name):
                return slug
        # 3. Label matches slug directly (e.g. "MIT" -> slug "mit")
        as_slug = re.sub(r"[^a-z0-9]+", "-", low).strip("-")
        if as_slug in slug_to_name:
            return as_slug
        # 4. Slug is a prefix of the label (e.g. "mit engineers" starts with slug "mit")
        for slug in slug_to_name:
            if low.startswith(slug.replace("-", " ")):
                return slug
        # 5. Any school name contains all words of the label (e.g. "Mass Maritime")
        label_words = low.split()
        for name, slug in name_to_slug.items():
            if all(w in name for w in label_words):
                return slug
        return label  # fall back to raw label as identifier

    entries: list[TRMatchResult] = []

    for table in soup.find_all("table", class_="teamscores"):
        # Round name from nearest preceding heading
        heading = table.find_previous(["h3", "h2", "h4"])
        round_name = heading.get_text(strip=True) if heading else ""

        rows = table.find_all("tr")
        if not rows:
            continue

        # First row: column headers (tr-vert-label = opponent display names)
        col_labels: list[str] = [
            th.get_text(strip=True)
            for th in rows[0].find_all("th", class_="tr-vert-label")
        ]
        col_slugs = [slug_for_label(lbl) for lbl in col_labels]

        # Data rows
        for row in rows[1:]:
            th_label = row.find("th", class_="tr-horiz-label")
            if not th_label:
                continue
            team_label = th_label.get_text(strip=True)
            team_slug  = slug_for_label(team_label)

            rec_td = row.find("td", class_="tr-record")
            record = rec_td.get_text(strip=True) if rec_td else ""

            result_cells = [
                td for td in row.find_all("td")
                if any(c in td.get("class", []) for c in ("tr-win", "tr-lose", "tr-ns"))
            ]
            for opp_slug, cell in zip(col_slugs, result_cells):
                cls = cell.get("class", [])
                if "tr-ns" in cls:
                    result = "X"
                elif "tr-win" in cls:
                    result = "W"
                elif "tr-lose" in cls:
                    result = "L"
                else:
                    continue

                text = cell.get_text(strip=True)
                m = re.search(r"\(([^)]+)\)", text)
                score = m.group(1) if m else ""

                entries.append(TRMatchResult(
                    regatta_url=regatta_url,
                    round_name=round_name,
                    team_slug=team_slug,
                    team_label=team_label,
                    opp_slug=opp_slug,
                    result=result,
                    score=score,
                    record=record,
                ))

    return entries


# ---------------------------------------------------------------------------
# Keelboat: parse sailors (flat list, variable crew count)
# ---------------------------------------------------------------------------

def parse_keelboat_sailors(soup: BeautifulSoup, regatta_url: str) -> list[KeelboatSailor]:
    """
    Sailors page for keelboat regattas. Three row types:

    1. School header row (8 cells):
         schoolname | teamname | division-cell | rank-cell | sailor1 | races | sailor2 | races
       The first two crew members are inline in cells[4] and [6].

    2. Continuation sailor row (4 cells):
         empty | empty | 'Name 'YY' | races
       One additional crew member per row.

    3. Reserves row (2 cells):
         colspan=2 "Reserves" | reserves-cell colspan=4 with <a>Name</a> tags
       One or more reserve sailors embedded in the second cell.
    """
    slots = []
    table = soup.find("table")
    if not table:
        return slots

    current_school = current_school_slug = current_team = current_division = ""

    def _add(text: str, races: str = "") -> None:
        # Strip trailing annotation characters (*, #, etc.) so grade regex matches
        text = re.sub(r"[\s*#^~+]+$", "", text)
        name, grade = split_name_grade(text)
        if name and current_school_slug:
            slots.append(KeelboatSailor(
                regatta_url=regatta_url,
                school_slug=current_school_slug,
                school=current_school,
                team=current_team,
                division=current_division,
                sailor_name=name,
                grade=grade,
                races=races,
            ))

    for tr in table.find_all("tr")[1:]:  # skip column-header thead row
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        # --- Reserves row (2 cells: colspan "Reserves" label + reserves-cell with name(s)) ---
        # Must be checked before the colspan skip below, as cells[0] has colspan=2.
        if len(cells) == 2 and "reserves-cell" in (cells[1].get("class") or []):
            reserves_cell = cells[1]
            anchors = reserves_cell.find_all("a")
            if anchors:
                for a in anchors:
                    _add(a.get_text(strip=True))
            else:
                _add(reserves_cell.get_text(strip=True))
            continue

        # Skip other rows where the first cell spans columns (section separators)
        if cells[0].get("colspan"):
            continue

        # --- School header row (8 cells) ---
        if len(cells) == 8 and "schoolname" in (cells[0].get("class") or []):
            c = cells[0]
            a = c.find("a", href=re.compile(r"/schools/"))
            if a:
                current_school = a.get_text(strip=True)
                current_school_slug = school_slug_from_href(a["href"])
            else:
                current_school = c.get_text(strip=True)
                current_school_slug = ""
            current_team = cells[1].get_text(strip=True)
            current_division = cells[2].get_text(strip=True).upper()
            # First two sailors: skipper in cells[4], crew in cells[6]
            if cells[4].get_text(strip=True):
                _add(cells[4].get_text(strip=True),
                     cells[5].get_text(strip=True) if "races" in (cells[5].get("class") or []) else "")
            if cells[6].get_text(strip=True):
                _add(cells[6].get_text(strip=True),
                     cells[7].get_text(strip=True) if "races" in (cells[7].get("class") or []) else "")
            continue

        # --- Continuation sailor row (4 cells, first two have no class) ---
        if len(cells) != 4:
            continue
        if any(c.get("class") for c in cells[:2]):
            continue
        sailor_text = cells[2].get_text(strip=True)
        races_text = cells[3].get_text(strip=True) if "races" in (cells[3].get("class") or []) else ""
        if sailor_text:
            _add(sailor_text, races_text)

    return slots


def build_keelboat_results(
    season: str, regatta_name: str, regatta_url: str, boat: str,
    score_entries: list[ScoreEntry],
    kb_sailors: list[KeelboatSailor],
    school_lookup: dict | None = None,
) -> list[KeelboatResult]:
    # Build school-name lookup from sailors page
    school_map: dict[tuple, str] = {}
    by_slug: dict[str, str] = {}
    for s in kb_sailors:
        school_map.setdefault((s.school_slug, s.team, s.division), s.school)
        by_slug.setdefault(s.school_slug, s.school)
    school_lookup = school_lookup or {}

    results = []
    for entry in score_entries:
        school = (school_map.get((entry.school_slug, entry.team_name, entry.division))
                  or by_slug.get(entry.school_slug) or school_lookup.get(entry.school_slug) or entry.team_name)
        for race_num, place in entry.race_scores.items():
            results.append(KeelboatResult(
                season=season, regatta_name=regatta_name, regatta_url=regatta_url,
                boat=boat, division=entry.division, race_num=race_num, place=place,
                school=school, school_slug=entry.school_slug, team=entry.team_name,
                code=entry.race_codes.get(race_num, ("", None))[0],
                points=entry.race_codes.get(race_num, ("", place if place < 900 else None))[1],
                div_penalty=entry.penalty[0], div_penalty_points=entry.penalty[1],
            ))
    return results


# ---------------------------------------------------------------------------
# Team race: parse standings
# ---------------------------------------------------------------------------

def parse_tr_scores(
    soup: BeautifulSoup, season: str, regatta_name: str, regatta_url: str, boat: str,
) -> tuple[list[TRStanding], dict[str, tuple[str, str]]]:
    """
    Returns (standings, team_map) where team_map maps
    'team-XXXXX' CSS class → (school_slug, team_name).
    """
    standings = []
    team_map: dict[str, tuple[str, str]] = {}

    table = soup.find("table")
    if not table:
        return standings, team_map

    for row in table.find_all("tr")[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) < 6:
            continue

        place_text = cells[1].get_text(strip=True)
        if not re.match(r"^\d+$", place_text):
            continue

        school_cell = cells[3]
        link = school_cell.find("a", href=re.compile(r"/schools/"))
        school = link.get_text(strip=True) if link else school_cell.get_text(strip=True)
        slug = school_slug_from_href(link["href"]) if link else ""

        team = cells[4].get_text(strip=True)

        rec = cells[5].get_text(strip=True)
        m = re.match(r"(\d+)/(\d+)", rec)
        wins   = int(m.group(1)) if m else 0
        losses = int(m.group(2)) if m else 0

        try:
            win_pct = float(cells[6].get_text(strip=True)) if len(cells) > 6 else 0.0
        except ValueError:
            win_pct = 0.0

        # Extract the team-XXXXX class for cross-page joining
        team_id = next((c for c in (row.get("class") or []) if c.startswith("team-")), "")
        if team_id:
            team_map[team_id] = (slug, team)

        standings.append(TRStanding(
            season=season, regatta_name=regatta_name, regatta_url=regatta_url, boat=boat,
            place=int(place_text), school=school, school_slug=slug, team=team,
            wins=wins, losses=losses, win_pct=win_pct, team_id=team_id,
        ))

    return standings, team_map


# ---------------------------------------------------------------------------
# Team race: parse sailors
# ---------------------------------------------------------------------------

def parse_tr_sailors(
    soup: BeautifulSoup, regatta_url: str, team_map: dict[str, tuple[str, str]],
) -> list[TRSailor]:
    """
    Each tr-row has class 'tr-row team-XXXXX'. We use team_map to resolve
    school_slug and team name. Unique (skipper, crew) pairs per team are stored.
    """
    sailors = []
    table = soup.find("table")
    if not table:
        return sailors

    for row in table.find_all("tr"):
        if "tr-row" not in (row.get("class") or []):
            continue

        team_id = next((c for c in (row.get("class") or []) if c.startswith("team-")), "")
        slug, team_name = team_map.get(team_id, ("", ""))

        seen_pairs: set[tuple[str, str]] = set()

        # Each tr-boattable contains a mini-table of boat-A/B/C rows
        for boattable in row.find_all(class_="tr-boattable"):
            for boat_row in boattable.find_all("tr"):
                cell = boat_row.find("td")
                if not cell:
                    continue
                parts = [t.strip() for t in cell.get_text(separator="\n").split("\n") if t.strip()]
                skipper_raw = parts[0] if parts else ""
                crew_raw    = parts[1] if len(parts) > 1 else ""

                skipper, s_grade = split_name_grade(skipper_raw)
                crew, c_grade    = split_name_grade(crew_raw)

                if not skipper:
                    continue
                pair = (skipper, crew)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                sailors.append(TRSailor(
                    regatta_url=regatta_url,
                    school_slug=slug,
                    team=team_name,
                    skipper=skipper, skipper_grade=s_grade,
                    crew=crew, crew_grade=c_grade,
                ))

    return sailors


# ---------------------------------------------------------------------------
# Team race: parse sailor matrix → per-match sailor assignments
# ---------------------------------------------------------------------------

SailorBoat = tuple[str, str, str, str, str]  # (boat_letter, skipper, s_grade, crew, c_grade)


def parse_tr_sailor_matrix(
    soup: BeautifulSoup,
    team_map: dict[str, tuple[str, str]],  # team_id → (slug, team_name)
) -> dict[tuple[str, str], list[SailorBoat]]:
    """
    Returns {(my_team_id, opponent_team_id): [SailorBoat, ...]}
    Built from the matrix table on /sailors/.
    Column i in each tr-row corresponds to the i-th team in tr-row order.
    """
    result: dict[tuple[str, str], list[SailorBoat]] = {}
    table = soup.find("table")
    if not table:
        return result

    rows = table.find_all("tr")

    # Ordered list of team_ids matches the column/row order of the matrix
    team_id_order = [
        next((c for c in (row.get("class") or []) if c.startswith("team-")), "")
        for row in rows
        if "tr-row" in (row.get("class") or [])
    ]

    for row in rows:
        if "tr-row" not in (row.get("class") or []):
            continue
        my_id = next((c for c in (row.get("class") or []) if c.startswith("team-")), "")
        if not my_id:
            continue

        # Direct td/th children only — recursive=False prevents nested boattable
        # <td> elements from polluting the column-index mapping
        data_cells = [
            c for c in row.find_all(["td", "th"], recursive=False)
            if "tr-horiz-label" not in (c.get("class") or [])
        ]

        for i, cell in enumerate(data_cells):
            if i >= len(team_id_order):
                break
            opp_id = team_id_order[i]
            if "tr-ns" in (cell.get("class") or []):
                continue  # self vs self
            if "tr-boattable" not in (cell.get("class") or []):
                continue

            boats: list[SailorBoat] = []
            for boat_row in cell.find_all("tr"):
                m = next(
                    (re.match(r"tr-boat-([A-Z])", c) for c in (boat_row.get("class") or []) if re.match(r"tr-boat-([A-Z])", c)),
                    None,
                )
                if not m:
                    continue
                boat_letter = m.group(1)
                td = boat_row.find("td")
                if not td:
                    continue
                parts = [t.strip() for t in td.get_text(separator="\n").split("\n") if t.strip()]
                skipper, s_grade = split_name_grade(parts[0] if parts else "")
                crew, c_grade    = split_name_grade(parts[1] if len(parts) > 1 else "")
                if skipper:
                    boats.append((boat_letter, skipper, s_grade, crew, c_grade))

            if boats:
                result[(my_id, opp_id)] = boats

    return result


def build_match_sailors(
    matches: list["TRMatch"],
    sailor_matrix: dict[tuple[str, str], list[SailorBoat]],
    team_map: dict[str, tuple[str, str]],
) -> list[TRMatchSailor]:
    """
    For each match, look up the boats each team used against its opponent
    from the sailor matrix and produce TRMatchSailor rows.
    """
    # Reverse team_map: (slug, team_name) → team_id
    reverse_map: dict[tuple[str, str], str] = {v: k for k, v in team_map.items()}

    result = []
    for match in matches:
        winner_id = reverse_map.get((match.winner_slug, match.winner_team), "")
        loser_id  = reverse_map.get((match.loser_slug,  match.loser_team),  "")

        for side, my_id, opp_id, slug, team in [
            ("winner", winner_id, loser_id, match.winner_slug, match.winner_team),
            ("loser",  loser_id,  winner_id, match.loser_slug,  match.loser_team),
        ]:
            for boat_letter, skipper, s_grade, crew, c_grade in sailor_matrix.get((my_id, opp_id), []):
                result.append(TRMatchSailor(
                    regatta_url=match.regatta_url,
                    match_num=match.match_num,
                    side=side,
                    boat=boat_letter,
                    school_slug=slug,
                    team=team,
                    skipper=skipper,
                    skipper_grade=s_grade,
                    crew=crew,
                    crew_grade=c_grade,
                ))

    return result


# ---------------------------------------------------------------------------
# Team race: parse individual matches (/all/ page)
# ---------------------------------------------------------------------------

def parse_tr_matches(
    soup: BeautifulSoup, season: str, regatta_name: str, regatta_url: str, boat: str,
) -> list[TRMatch]:
    matches = []
    table = soup.find("table")
    if not table:
        return matches

    current_round = 0

    for row in table.find_all("tr"):
        classes = row.get("class") or []

        # Round header row
        if "roundrow" in classes:
            cells = row.find_all(["td", "th"])
            if cells:
                m = re.search(r"(\d+)", cells[0].get_text())
                if m:
                    current_round = int(m.group(1))
            continue

        cells = row.find_all(["td", "th"])
        if len(cells) != 8:
            continue

        match_text = cells[0].get_text(strip=True)
        if not re.match(r"^\d+$", match_text):
            continue
        match_num = int(match_text)

        def parse_team_cell(cell):
            link = cell.find("a", href=re.compile(r"/schools/"))
            slug   = school_slug_from_href(link["href"]) if link else ""
            school = link.get_text(strip=True) if link else ""
            parts  = [str(t).strip() for t in cell.children if isinstance(t, NavigableString)]
            team   = " ".join(p for p in parts if p)
            return school, slug, team

        # cells[2] = team1 info (has tr-win or tr-lose class)
        # cells[3] = team1 positions
        # cells[5] = team2 positions
        # cells[6] = team2 info
        cell2_classes = cells[2].get("class") or []
        team1_wins = "tr-win" in cell2_classes

        school1, slug1, team1 = parse_team_cell(cells[2])
        pos1 = cells[3].get_text(strip=True)
        pos2 = cells[5].get_text(strip=True)
        school2, slug2, team2 = parse_team_cell(cells[6])

        if team1_wins:
            winner_school, winner_slug, winner_team, winner_pos = school1, slug1, team1, pos1
            loser_school,  loser_slug,  loser_team,  loser_pos  = school2, slug2, team2, pos2
        else:
            winner_school, winner_slug, winner_team, winner_pos = school2, slug2, team2, pos2
            loser_school,  loser_slug,  loser_team,  loser_pos  = school1, slug1, team1, pos1

        matches.append(TRMatch(
            season=season, regatta_name=regatta_name, regatta_url=regatta_url, boat=boat,
            round_num=current_round, match_num=match_num,
            winner_school=winner_school, winner_slug=winner_slug,
            winner_team=winner_team, winner_positions=winner_pos,
            loser_school=loser_school, loser_slug=loser_slug,
            loser_team=loser_team, loser_positions=loser_pos,
        ))

    return matches


# ---------------------------------------------------------------------------
# Fleet race DB
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS race_results (
            id            INTEGER PRIMARY KEY,
            season        TEXT,
            regatta_name  TEXT,
            regatta_url   TEXT,
            boat          TEXT,
            division      TEXT,
            race_num      INTEGER,
            place         INTEGER,
            school        TEXT,
            school_slug   TEXT,
            team          TEXT,
            skipper       TEXT,
            skipper_grade TEXT,
            crew          TEXT,
            crew_grade    TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_row
        ON race_results(regatta_url, division, race_num, school_slug, team)
    """)
    _add_code_columns(conn, "race_results")
    conn.commit()


def _add_code_columns(conn: sqlite3.Connection, table: str) -> None:
    """Databases made before result codes were kept gain the two columns in place."""
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if "code" not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN code TEXT DEFAULT ''")
    if "points" not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN points INTEGER")
    if "div_penalty" not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN div_penalty TEXT DEFAULT ''")
        conn.execute(f"ALTER TABLE {table} ADD COLUMN div_penalty_points INTEGER")


def already_scraped(conn: sqlite3.Connection, regatta_url: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM race_results WHERE regatta_url = ? LIMIT 1", (regatta_url,)
    ).fetchone() is not None


def insert_results(conn: sqlite3.Connection, results: list[RaceResult]):
    conn.executemany(
        """INSERT OR IGNORE INTO race_results
           (season, regatta_name, regatta_url, boat, division, race_num, place,
            school, school_slug, team, skipper, skipper_grade, crew, crew_grade, code, points,
            div_penalty, div_penalty_points)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(r.season, r.regatta_name, r.regatta_url, r.boat, r.division, r.race_num, r.place,
          r.school, r.school_slug, r.team, r.skipper, r.skipper_grade, r.crew, r.crew_grade,
          r.code, r.points, r.div_penalty, r.div_penalty_points)
         for r in results],
    )
    conn.commit()


def init_rotation_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fleet_rotations (
            id          INTEGER PRIMARY KEY,
            regatta_url TEXT,
            school_slug TEXT,
            team        TEXT,
            division    TEXT,
            race_num    INTEGER,
            sail_num    INTEGER
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_rotation
        ON fleet_rotations(regatta_url, school_slug, team, division, race_num)
    """)
    conn.commit()


def insert_rotations(conn: sqlite3.Connection, entries: list[RotationEntry]):
    conn.executemany(
        """INSERT OR IGNORE INTO fleet_rotations
           (regatta_url, school_slug, team, division, race_num, sail_num)
           VALUES (?,?,?,?,?,?)""",
        [(e.regatta_url, e.school_slug, e.team, e.division, e.race_num, e.sail_num)
         for e in entries],
    )
    conn.commit()


def export_rotations_csv(conn: sqlite3.Connection, path: Path):
    cur = conn.execute(
        "SELECT * FROM fleet_rotations ORDER BY regatta_url, division, school_slug, race_num"
    )
    col_names = [d[0] for d in cur.description]
    rows = cur.fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(col_names)
        w.writerows(rows)
    log.info(f"Exported {len(rows)} rotation rows to {path}")


def export_csv(conn: sqlite3.Connection, path: Path):
    cur = conn.execute(
        "SELECT * FROM race_results ORDER BY season, regatta_url, division, race_num, place"
    )
    col_names = [d[0] for d in cur.description]
    rows = cur.fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(col_names)
        w.writerows(rows)
    log.info(f"Exported {len(rows)} rows to {path}")


# ---------------------------------------------------------------------------
# Keelboat DB
# ---------------------------------------------------------------------------

def init_keelboat_tables(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS keelboat_results (
            id           INTEGER PRIMARY KEY,
            season       TEXT,
            regatta_name TEXT,
            regatta_url  TEXT,
            boat         TEXT,
            division     TEXT,
            race_num     INTEGER,
            place        INTEGER,
            school       TEXT,
            school_slug  TEXT,
            team         TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_unique
        ON keelboat_results(regatta_url, division, race_num, school_slug, team)
    """)
    _add_code_columns(conn, "keelboat_results")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS keelboat_sailors (
            id          INTEGER PRIMARY KEY,
            regatta_url TEXT,
            school_slug TEXT,
            school      TEXT,
            team        TEXT,
            division    TEXT,
            sailor_name TEXT,
            grade       TEXT,
            races       TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_sailor_unique
        ON keelboat_sailors(regatta_url, school_slug, team, division, sailor_name)
    """)
    if "role" not in {r[1] for r in conn.execute("PRAGMA table_info(keelboat_sailors)")}:
        conn.execute("ALTER TABLE keelboat_sailors ADD COLUMN role TEXT DEFAULT ''")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS regatta_kinds (
            regatta_url TEXT PRIMARY KEY,
            season      TEXT,
            kind        TEXT,
            crew_size   INTEGER,
            boat        TEXT,
            scoring     TEXT,
            reason      TEXT
        )
    """)
    conn.commit()


def already_scraped_keelboat(conn: sqlite3.Connection, regatta_url: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM keelboat_results WHERE regatta_url = ? LIMIT 1", (regatta_url,)
    ).fetchone() is not None


def insert_keelboat_results(
    conn: sqlite3.Connection,
    results: list[KeelboatResult],
    sailors: list[KeelboatSailor],
):
    conn.executemany(
        """INSERT OR IGNORE INTO keelboat_results
           (season, regatta_name, regatta_url, boat, division, race_num, place,
            school, school_slug, team, code, points, div_penalty, div_penalty_points)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(r.season, r.regatta_name, r.regatta_url, r.boat, r.division, r.race_num, r.place,
          r.school, r.school_slug, r.team, r.code, r.points,
          r.div_penalty, r.div_penalty_points) for r in results],
    )
    conn.executemany(
        """INSERT OR IGNORE INTO keelboat_sailors
           (regatta_url, school_slug, school, team, division, sailor_name, grade, races, role)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [(s.regatta_url, s.school_slug, s.school, s.team, s.division,
          s.sailor_name, s.grade, s.races, s.role) for s in sailors],
    )
    conn.commit()


def save_regatta_kinds(conn: sqlite3.Connection, path: Path) -> None:
    """Write the kinds noted during this run and export the whole table."""
    with _KINDS_LOCK:
        kinds = list(_KINDS.values())
        _KINDS.clear()
    conn.executemany(
        """INSERT OR REPLACE INTO regatta_kinds
           (regatta_url, season, kind, crew_size, boat, scoring, reason)
           VALUES (?,?,?,?,?,?,?)""",
        [(k.regatta_url, k.season, k.kind, k.crew_size, k.boat, k.scoring, k.reason) for k in kinds],
    )
    # A regatta that changed kind (a keelboat once read as a dinghy) leaves the table it
    # no longer belongs in.
    for k in kinds:
        gone = "race_results" if k.kind == event_kinds.KEELBOAT else "keelboat_results"
        if k.kind != event_kinds.TEAM:
            conn.execute(f"DELETE FROM {gone} WHERE regatta_url = ?", (k.regatta_url,))
        if k.kind != event_kinds.KEELBOAT:
            conn.execute("DELETE FROM keelboat_sailors WHERE regatta_url = ?", (k.regatta_url,))
    conn.commit()
    cur = conn.execute("SELECT * FROM regatta_kinds ORDER BY season, regatta_url")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    log.info(f"Saved {len(kinds)} regatta kinds; exported {len(rows)} to {path}")


def export_keelboat_csvs(conn: sqlite3.Connection, prefix: str):
    for table, order in [
        ("keelboat_results", "season, regatta_url, division, race_num, place"),
        ("keelboat_sailors", "regatta_url, school_slug, team, division, sailor_name"),
    ]:
        path = Path(f"{prefix}{table.removeprefix('keelboat_')}.csv")
        cur = conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
        col_names = [d[0] for d in cur.description]
        rows = cur.fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(col_names)
            w.writerows(rows)
        log.info(f"Exported {len(rows)} rows to {path}")


# ---------------------------------------------------------------------------
# Team race DB
# ---------------------------------------------------------------------------

def init_tr_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tr_standings (
            id           INTEGER PRIMARY KEY,
            season       TEXT,
            regatta_name TEXT,
            regatta_url  TEXT,
            boat         TEXT,
            place        INTEGER,
            school       TEXT,
            school_slug  TEXT,
            team         TEXT,
            wins         INTEGER,
            losses       INTEGER,
            win_pct      REAL
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tr_unique
        ON tr_standings(regatta_url, school_slug, team)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tr_sailors (
            id            INTEGER PRIMARY KEY,
            regatta_url   TEXT,
            school_slug   TEXT,
            team          TEXT,
            skipper       TEXT,
            skipper_grade TEXT,
            crew          TEXT,
            crew_grade    TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tr_sailor_unique
        ON tr_sailors(regatta_url, school_slug, team, skipper, crew)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tr_matches (
            id               INTEGER PRIMARY KEY,
            season           TEXT,
            regatta_name     TEXT,
            regatta_url      TEXT,
            boat             TEXT,
            round_num        INTEGER,
            match_num        INTEGER,
            winner_school    TEXT,
            winner_slug      TEXT,
            winner_team      TEXT,
            winner_positions TEXT,
            loser_school     TEXT,
            loser_slug       TEXT,
            loser_team       TEXT,
            loser_positions  TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tr_match_unique
        ON tr_matches(regatta_url, match_num)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tr_match_sailors (
            id            INTEGER PRIMARY KEY,
            regatta_url   TEXT,
            match_num     INTEGER,
            side          TEXT,
            boat          TEXT,
            school_slug   TEXT,
            team          TEXT,
            skipper       TEXT,
            skipper_grade TEXT,
            crew          TEXT,
            crew_grade    TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tr_match_sailor_unique
        ON tr_match_sailors(regatta_url, match_num, side, boat)
    """)
    conn.commit()


def already_scraped_tr(conn: sqlite3.Connection, regatta_url: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM tr_standings WHERE regatta_url = ? LIMIT 1", (regatta_url,)
    ).fetchone() is not None


def insert_tr_results(
    conn: sqlite3.Connection,
    standings: list[TRStanding],
    sailors: list[TRSailor],
    matches: list[TRMatch],
    match_sailors: list[TRMatchSailor],
):
    conn.executemany(
        """INSERT OR IGNORE INTO tr_standings
           (season, regatta_name, regatta_url, boat, place,
            school, school_slug, team, wins, losses, win_pct)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [(s.season, s.regatta_name, s.regatta_url, s.boat, s.place,
          s.school, s.school_slug, s.team, s.wins, s.losses, s.win_pct)
         for s in standings],
    )
    conn.executemany(
        """INSERT OR IGNORE INTO tr_sailors
           (regatta_url, school_slug, team, skipper, skipper_grade, crew, crew_grade)
           VALUES (?,?,?,?,?,?,?)""",
        [(s.regatta_url, s.school_slug, s.team, s.skipper, s.skipper_grade, s.crew, s.crew_grade)
         for s in sailors],
    )
    if matches:
        insert_tr_matches(conn, matches)
    if match_sailors:
        conn.executemany(
            """INSERT OR IGNORE INTO tr_match_sailors
               (regatta_url, match_num, side, boat, school_slug, team,
                skipper, skipper_grade, crew, crew_grade)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [(s.regatta_url, s.match_num, s.side, s.boat, s.school_slug, s.team,
              s.skipper, s.skipper_grade, s.crew, s.crew_grade)
             for s in match_sailors],
        )
    conn.commit()


def insert_tr_matches(conn: sqlite3.Connection, matches: list[TRMatch]):
    conn.executemany(
        """INSERT OR IGNORE INTO tr_matches
           (season, regatta_name, regatta_url, boat, round_num, match_num,
            winner_school, winner_slug, winner_team, winner_positions,
            loser_school, loser_slug, loser_team, loser_positions)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(m.season, m.regatta_name, m.regatta_url, m.boat, m.round_num, m.match_num,
          m.winner_school, m.winner_slug, m.winner_team, m.winner_positions,
          m.loser_school, m.loser_slug, m.loser_team, m.loser_positions)
         for m in matches],
    )
    conn.commit()


def export_tr_csvs(conn: sqlite3.Connection, prefix: str = "issa_tr_"):
    for table in ["tr_standings", "tr_sailors", "tr_matches", "tr_match_sailors"]:
        path = Path(f"{prefix}{table.removeprefix('tr_')}.csv")
        cur = conn.execute(f"SELECT * FROM {table}")
        col_names = [d[0] for d in cur.description]
        rows = cur.fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(col_names)
            w.writerows(rows)
        log.info(f"Exported {len(rows)} rows to {path}")


# ---------------------------------------------------------------------------
# School logo downloader
# ---------------------------------------------------------------------------

def get_school_slugs(base_url: str) -> list[str]:
    """Return all school slugs from the /schools/ listing page."""
    soup = get(f"{base_url}/schools/")
    if not soup:
        return []
    slugs = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.match(r"^/schools/([^/]+)/$", href)
        if m:
            slug = m.group(1)
            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)
    return slugs


def scrape_schools_listing(base_url: str) -> list[dict]:
    """
    Fetch /schools/ table: returns list of dicts with
    slug, school, city, state, png_name (from thumbnail, -40 stripped), conference.
    Handles both single-table (ISSA) and multi-table per-conference (ICSA) layouts.
    """
    soup = get(f"{base_url}/schools/")
    if not soup:
        return []
    tables = soup.find_all("table")
    if not tables:
        return []
    rows = []
    for table in tables:
        # Derive conference from the nearest preceding h3 sibling
        conference = ""
        for sib in table.previous_siblings:
            if hasattr(sib, "name") and sib.name == "h3":
                conference = re.sub(r"\s+Conference$", "", sib.get_text(strip=True), flags=re.I)
                break

        for tr in table.find_all("tr")[1:]:   # skip header
            cells = tr.find_all(["td", "th"])
            if len(cells) < 4:
                continue
            img = cells[0].find("img")
            png_src = img.get("src", "") if img else ""
            # Strip the thumbnail suffix: "8322-40.png" → "8322.png"
            png_name = re.sub(r"-40(\.\w+)$", r"\1", Path(png_src).name) if png_src else ""

            a = cells[1].find("a", href=True)
            if not a:
                continue
            m = re.match(r"^/schools/([^/]+)/$", a["href"])
            if not m:
                continue
            slug = m.group(1)
            school = a.get_text(strip=True)
            city  = cells[2].get_text(strip=True)
            state = cells[3].get_text(strip=True)
            rows.append({"slug": slug, "school": school, "city": city,
                         "state": state, "png_name": png_name,
                         "conference": conference})
    return rows


def get_school_conference(slug: str, base_url: str) -> str:
    """Fetch individual school page and return district/conference value."""
    soup = get(f"{base_url}/schools/{slug}/")
    if not soup:
        return ""
    for li in soup.select("ul#page-info li"):
        key = li.find(class_="page-info-key")
        val = li.find(class_="page-info-value")
        if key and val:
            k = key.get_text(strip=True).lower()
            if k in ("district", "conference"):
                return val.get_text(strip=True)
    return ""


def get_school_detail(slug: str, base_url: str) -> dict:
    """
    Fetch individual school page. Returns dict with school name, city, state,
    png_name, and conference/district. Used as fallback when listing page is sparse.
    """
    soup = get(f"{base_url}/schools/{slug}/")
    if not soup:
        return {"school": slug, "city": "", "state": "", "png_name": "", "conference": ""}

    school = ""
    h1 = soup.find("h1")
    if h1:
        school = h1.get_text(strip=True)

    conference = ""
    city = ""
    state = ""
    for li in soup.select("ul#page-info li"):
        key = li.find(class_="page-info-key")
        val = li.find(class_="page-info-value")
        if key and val:
            k = key.get_text(strip=True).lower()
            v = val.get_text(strip=True)
            if k in ("district", "conference"):
                conference = v
            elif k == "city":
                city = v
            elif k == "state":
                state = v

    png_name = ""
    burgee = soup.find("p", class_="burgee")
    if burgee:
        img = burgee.find("img")
        if img and img.get("src"):
            png_name = Path(img["src"]).name

    return {"school": school, "slug": slug, "city": city, "state": state,
            "png_name": png_name, "conference": conference}


def scrape_regatta_end_date(url: str, base_url: str) -> str:
    """
    Fetch a regatta detail page and return the end_date as MM/DD/YYYY.
    Returns "" if not found.

    Techscore shows dates as:
      - "March 8, 2026"          (1-day)
      - "March 7-8, 2026"        (2-day, same month)
      - "February 28 - March 1, 2026"  (2-day, cross-month)
    """
    if not url:
        return ""
    soup = get(url)
    if not soup:
        return ""
    # Find the span labelled "Date" and grab the next sibling span
    spans = soup.find_all("span")
    date_text = ""
    for i, s in enumerate(spans):
        if s.get_text(strip=True) == "Date" and i + 1 < len(spans):
            date_text = spans[i + 1].get_text(strip=True)
            break
    if not date_text:
        return ""

    # Parse end date from the text
    MONTHS = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }

    # "February 28 - March 1, 2026"
    cross = re.match(
        r"(\w+)\s+\d+\s*[-–]\s*(\w+)\s+(\d+),\s*(\d{4})", date_text
    )
    if cross:
        month = MONTHS.get(cross.group(2).lower(), 0)
        day   = int(cross.group(3))
        year  = int(cross.group(4))
        if month:
            return f"{month:02d}/{day:02d}/{year}"

    # "March 7-8, 2026"
    range_same = re.match(r"(\w+)\s+\d+[-–](\d+),\s*(\d{4})", date_text)
    if range_same:
        month = MONTHS.get(range_same.group(1).lower(), 0)
        day   = int(range_same.group(2))
        year  = int(range_same.group(3))
        if month:
            return f"{month:02d}/{day:02d}/{year}"

    # "March 8, 2026"
    single = re.match(r"(\w+)\s+(\d+),\s*(\d{4})", date_text)
    if single:
        month = MONTHS.get(single.group(1).lower(), 0)
        day   = int(single.group(2))
        year  = int(single.group(3))
        if month:
            return f"{month:02d}/{day:02d}/{year}"

    return ""


def scrape_season_regattas_listing(season: str, base_url: str) -> list[dict]:
    """
    Parse the season index page table for a given season.
    Returns a list of dicts with keys:
      season, name, host, type, scoring, start_date, status, leading_week, url
    """
    soup = get(f"{base_url}/{season}/")
    if not soup:
        return []

    # Use the largest table on the page (full season listing, not "coming soon")
    tables = soup.find_all("table")
    if not tables:
        return []
    table = max(tables, key=lambda t: len(t.find_all("tr")))

    rows = table.find_all("tr")
    if not rows:
        return []

    # Parse header row to find column indices
    header_cells = rows[0].find_all(["th", "td"])
    headers = [c.get_text(strip=True).lower() for c in header_cells]

    col = {}
    for i, h in enumerate(headers):
        if "name" in h or "regatta" in h:
            col.setdefault("name", i)
        elif "host" in h:
            col["host"] = i
        elif "type" in h:
            col["type"] = i
        elif "scor" in h:
            col["scoring"] = i
        elif "date" in h or "start" in h:
            col["start_date"] = i
        elif "status" in h:
            col["status"] = i
        elif "leading" in h or "week" in h:
            col["leading_week"] = i

    week_pattern = re.compile(r"^Week\s+\d+$", re.IGNORECASE)
    current_week = ""
    results = []
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        def cell_text(key):
            i = col.get(key)
            if i is None or i >= len(cells):
                return ""
            return cells[i].get_text(strip=True)

        name = cell_text("name")
        if not name:
            continue

        # Section header rows (Week X, Preweek X, etc.) — single th with colspan
        first_cell = cells[0]
        is_header = (
            first_cell.name == "th"
            and (int(first_cell.get("colspan", 1)) > 1 or len(cells) == 1)
            and not cell_text("host")
            and not cell_text("start_date")
        )
        if is_header:
            current_week = name  # covers Week X, Preweek X, and any other section headers
            continue

        # Capture URL from the link in the name cell
        name_col_idx = col.get("name")
        url = ""
        if name_col_idx is not None and name_col_idx < len(cells):
            a = cells[name_col_idx].find("a", href=True)
            if a:
                href = a["href"]
                if href.startswith("http"):
                    url = href
                elif href.startswith("/"):
                    url = base_url + href
                else:
                    url = f"{base_url}/{season}/{href}"

        results.append({
            "season":       season,
            "name":         name,
            "host":         cell_text("host"),
            "type":         cell_text("type"),
            "scoring":      cell_text("scoring"),
            "start_date":   cell_text("start_date"),
            "status":       cell_text("status"),
            "leading_week": current_week,
            "url":          url,
        })

    return results


def scrape_regattas_csv(site: str, only_seasons: list[str] = None):
    """Scrape all season pages and write <site>_regattas.csv."""
    cfg = SITES[site]
    base_url = cfg["base_url"]
    out_path = DATA_DIR / f"{site}_regattas.csv"

    seasons = only_seasons if only_seasons else get_all_seasons(base_url)
    log.info(f"[{site}] Scraping regatta listings for {len(seasons)} seasons")

    all_rows = []
    for season in seasons:
        rows = scrape_season_regattas_listing(season, base_url)
        all_rows.extend(rows)
        log.info(f"[{site}] {season}: {len(rows)} regattas")

    # Fetch end_date from each regatta detail page in parallel
    log.info(f"[{site}] Fetching end dates for {len(all_rows)} regattas...")
    end_dates: dict[str, str] = {}
    urls_to_fetch = [r["url"] for r in all_rows if r.get("url")]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(scrape_regatta_end_date, url, base_url): url
                   for url in urls_to_fetch}
        done = 0
        for fut in as_completed(futures):
            url = futures[fut]
            end_dates[url] = fut.result()
            done += 1
            if done % 100 == 0:
                log.info(f"[{site}]   end dates: {done}/{len(urls_to_fetch)}")

    for r in all_rows:
        r["end_date"] = end_dates.get(r.get("url", ""), "")

    fieldnames = ["season", "name", "host", "type", "scoring", "start_date", "end_date", "status", "leading_week", "url"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)

    log.info(f"[{site}] Written {len(all_rows)} regattas to {out_path}")


def scrape_schools_csv(site: str):
    """Scrape all schools for a site and write <site>_schools.csv."""
    cfg = SITES[site]
    base_url = cfg["base_url"]
    db_path  = cfg["db_path"]
    out_path = DATA_DIR / f"{site}_schools.csv"

    # Try listing page first (works well for ISSA)
    listing = scrape_schools_listing(base_url)

    if len(listing) > 5:
        has_conf = any(s.get("conference") for s in listing)
        if has_conf:
            # Listing already includes conference (ICSA multi-table layout)
            log.info(f"[{site}] {len(listing)} schools from listing (with conferences)")
            rows = listing
        else:
            # Listing has city/state but no conference — fetch conference per school (ISSA)
            log.info(f"[{site}] {len(listing)} schools from listing, fetching conferences...")
            conf_map: dict[str, str] = {}
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futures = {ex.submit(get_school_conference, s["slug"], base_url): s["slug"]
                           for s in listing}
                for fut in as_completed(futures):
                    conf_map[futures[fut]] = fut.result()
            rows = [{**s, "conference": conf_map.get(s["slug"], "")} for s in listing]
    else:
        # Listing page is sparse (ICSA) — fall back to existing CSV slugs or DB slugs
        slugs: set[str] = set()
        if out_path.exists():
            with open(out_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("slug"):
                        slugs.add(row["slug"])
            log.info(f"[{site}] {len(slugs)} school slugs from existing CSV")
        if not slugs and db_path.exists():
            conn    = sqlite3.connect(db_path)
            tr_conn = sqlite3.connect(cfg["tr_db_path"])
            slugs = {r[0] for r in conn.execute(
                "SELECT DISTINCT school_slug FROM race_results WHERE school_slug != '' "
                "UNION SELECT DISTINCT school_slug FROM keelboat_results WHERE school_slug != ''"
            ).fetchall()}
            slugs |= {r[0] for r in tr_conn.execute(
                "SELECT DISTINCT school_slug FROM tr_standings WHERE school_slug != ''"
            ).fetchall()}
            conn.close()
            tr_conn.close()
        slugs = sorted(slugs)
        log.info(f"[{site}] {len(slugs)} school slugs from DB, fetching individual pages...")
        rows = []
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {ex.submit(get_school_detail, slug, base_url): slug for slug in slugs}
            for fut in as_completed(futures):
                rows.append(fut.result())
        rows.sort(key=lambda r: r["school"].lower())

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["school", "slug", "city", "state", "png_name", "conference"])
        w.writeheader()
        w.writerows(rows)

    log.info(f"[{site}] Written {len(rows)} schools to {out_path}")


def download_logo(slug: str, base_url: str, out_dir: Path) -> bool:
    """Fetch school page, find logo in p.burgee, download it. Returns True on success."""
    soup = get(f"{base_url}/schools/{slug}/")
    if not soup:
        return False
    burgee = soup.find("p", class_="burgee")
    if not burgee:
        return False
    img = burgee.find("img")
    if not img or not img.get("src"):
        return False

    src = img["src"]
    img_url = src if src.startswith("http") else f"{base_url}{src}"
    ext = Path(src).suffix or ".png"
    dest = out_dir / f"{slug}{ext}"

    if dest.exists():
        return True  # already downloaded

    try:
        r = get_session().get(img_url, timeout=15)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True
    except requests.RequestException as e:
        log.warning(f"Logo download failed for {slug}: {e}")
        return False


def download_school_logos(site: str):
    cfg = SITES[site]
    base_url = cfg["base_url"]
    out_dir = Path(f"{site}_logos")
    out_dir.mkdir(exist_ok=True)

    slugs = get_school_slugs(base_url)
    log.info(f"[{site}] Downloading logos for {len(slugs)} schools → {out_dir}/")

    ok = fail = skip = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(download_logo, slug, base_url, out_dir): slug for slug in slugs}
        for future in as_completed(futures):
            slug = futures[future]
            try:
                result = future.result()
                if result:
                    ok += 1
                else:
                    skip += 1
            except Exception as e:
                log.warning(f"Error for {slug}: {e}")
                fail += 1

    log.info(f"[{site}] Logos: {ok} downloaded, {skip} skipped (no logo), {fail} errors")


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def scrape_regatta_task(
    season: str, name: str, slug: str, base_url: str, keelboats: set,
) -> tuple[str, list[RaceResult], list[TRStanding], list[TRSailor], list[TRMatch], list[TRMatchSailor], list[KeelboatResult], list[KeelboatSailor], list[RotationEntry]]:
    regatta_url = f"/{season}/{slug}"

    landing_soup     = get(f"{base_url}{regatta_url}/")
    full_scores_soup = get(f"{base_url}{regatta_url}/full-scores/")
    sailors_soup     = get(f"{base_url}{regatta_url}/sailors/")

    boat = get_boat_class(landing_soup) if landing_soup else ""
    scoring = get_page_info(landing_soup, "Scoring") if landing_soup else ""

    if full_scores_soup is None:
        log.warning(f"  no full-scores: {regatta_url}")
        return regatta_url, [], [], [], [], [], [], [], []

    # The pages decide what kind of event this is; the boat list is only a fallback.
    team_race = is_team_race(full_scores_soup)
    entries = (event_kinds.parse_entries(sailors_soup, parse_full_scores(full_scores_soup))
               if sailors_soup and not team_race else [])
    kind = event_kinds.classify(
        scoring=scoring, boat=boat, is_team_race=team_race, name=f"{name} {slug}",
        sailor_rows=full_scores_soup.find(class_="singlehanded-sailor-span") is not None,
        entries=entries, known_keelboats=keelboats)
    note_kind(regatta_url, season, kind, boat, scoring)

    if team_race:
        all_soup = get(f"{base_url}{regatta_url}/all/")
        standings, team_map = parse_tr_scores(full_scores_soup, season, name, regatta_url, boat)
        tr_sailors = parse_tr_sailors(sailors_soup, regatta_url, team_map) if sailors_soup else []
        tr_matches = parse_tr_matches(all_soup, season, name, regatta_url, boat) if all_soup else []
        if sailors_soup and team_map and tr_matches:
            sailor_matrix = parse_tr_sailor_matrix(sailors_soup, team_map)
            match_sailors = build_match_sailors(tr_matches, sailor_matrix, team_map)
        else:
            match_sailors = []
        if not standings:
            log.warning(f"  no TR scores parsed: {regatta_url}")
        return regatta_url, [], standings, tr_sailors, tr_matches, match_sailors, [], [], []

    elif kind.kind == event_kinds.KEELBOAT:
        scores = parse_full_scores(full_scores_soup)
        kb_sailors = keelboat_sailors_from_entries(entries, regatta_url)
        if not scores:
            log.warning(f"  no keelboat scores parsed: {regatta_url}")
            return regatta_url, [], [], [], [], [], [], [], []
        kb_results = build_keelboat_results(season, name, regatta_url, boat, scores, kb_sailors, school_lookup_for(base_url))
        return regatta_url, [], [], [], [], [], kb_results, kb_sailors, []

    else:
        scores    = parse_full_scores(full_scores_soup)
        sailors   = parse_sailors(sailors_soup, scores, school_lookup_for(base_url)) if sailors_soup else []
        rot_soup  = get(f"{base_url}{regatta_url}/rotations/")
        rotations = parse_rotations(rot_soup, regatta_url) if rot_soup else []
        if not scores:
            log.warning(f"  no scores parsed: {regatta_url}")
            return regatta_url, [], [], [], [], [], [], [], []
        results = join_results(season, name, regatta_url, boat, scores, sailors, school_lookup_for(base_url))
        return regatta_url, results, [], [], [], [], [], [], rotations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(
    site: str = "issa",
    limit: Optional[int] = None,
    only_seasons: Optional[list[str]] = None,
):
    DB_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    cfg = SITES[site]
    base_url     = cfg["base_url"]
    db_path      = cfg["db_path"]
    csv_path     = cfg["csv_path"]
    tr_db_path   = cfg["tr_db_path"]
    tr_prefix    = cfg["tr_csv_prefix"]
    kb_prefix    = cfg["kb_prefix"]
    keelboats    = cfg["keelboats"]

    conn    = sqlite3.connect(db_path,    check_same_thread=False)
    tr_conn = sqlite3.connect(tr_db_path, check_same_thread=False)
    init_db(conn)
    init_tr_db(tr_conn)
    init_keelboat_tables(conn)
    init_rotation_table(conn)
    db_lock = threading.Lock()

    seasons = only_seasons if only_seasons else get_all_seasons(base_url)
    log.info(f"[{site}] Seasons: {seasons[0]} → {seasons[-1]} ({len(seasons)} total)")

    all_regattas: list[tuple[str, str, str]] = []
    for season in seasons:
        regattas = get_regattas(season, base_url)
        log.info(f"{season}: {len(regattas)} regattas")
        for name, slug in regattas:
            regatta_url = f"/{season}/{slug}"
            with db_lock:
                if (not already_scraped(conn, regatta_url)
                        and not already_scraped_tr(tr_conn, regatta_url)
                        and not already_scraped_keelboat(conn, regatta_url)):
                    all_regattas.append((season, name, slug))

    if limit:
        all_regattas = all_regattas[:limit]

    log.info(f"Scraping {len(all_regattas)} regattas with {WORKERS} workers")

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(scrape_regatta_task, season, name, slug, base_url, keelboats): slug
            for season, name, slug in all_regattas
        }
        for future in as_completed(futures):
            regatta_url, fleet_results, tr_standings, tr_sailors, tr_matches, match_sailors, kb_results, kb_sailors, rotations = future.result()
            with db_lock:
                if fleet_results:
                    insert_results(conn, fleet_results)
                    log.info(f"  {len(fleet_results):>4} fleet rows — {regatta_url}")
                if tr_standings:
                    insert_tr_results(tr_conn, tr_standings, tr_sailors, tr_matches, match_sailors)
                    log.info(f"  {len(tr_standings):>4} TR teams, {len(tr_matches):>4} matches, {len(match_sailors):>4} match-sailors — {regatta_url}")
                if kb_results:
                    insert_keelboat_results(conn, kb_results, kb_sailors)
                    log.info(f"  {len(kb_results):>4} keelboat rows, {len(kb_sailors):>4} sailors — {regatta_url}")
                if rotations:
                    insert_rotations(conn, rotations)
                    log.info(f"  {len(rotations):>4} rotation rows — {regatta_url}")

    # Before the exports: a regatta that changed kind is moved out of its old table first.
    save_regatta_kinds(conn, Path(str(csv_path).replace("_results.csv", "_regatta_kinds.csv")))
    export_csv(conn, csv_path)
    export_tr_csvs(tr_conn, tr_prefix)
    export_keelboat_csvs(conn, kb_prefix)
    rot_csv = Path(str(cfg["csv_path"]).replace("_results.csv", "_rotations.csv"))
    export_rotations_csv(conn, rot_csv)
    conn.close()
    tr_conn.close()
    log.info("Done.")


# ---------------------------------------------------------------------------
# Roster scraper
# ---------------------------------------------------------------------------

# Seasons with known rosters per site
ROSTER_SEASONS = {
    "issa": ["s24", "f24", "s25", "f25"],
    "icsa": ["f24", "f25"],          # spring rosters return 404 on ICSA
}


def fetch_roster(school_slug: str, season: str, base_url: str) -> list[tuple[str, str]]:
    """Return list of (name, class_year) from a school's season roster page."""
    soup = get(f"{base_url}/schools/{school_slug}/{season}/roster/")
    if not soup:
        return []
    table = soup.find("table")
    if not table:
        return []
    rows = []
    for tr in table.find_all("tr")[1:]:   # skip header
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        name = " ".join(cells[0].get_text(strip=True).split())   # collapse double-spaces
        class_year = cells[1].get_text(strip=True)
        if name:
            rows.append((name, class_year))
    return rows


def _roster_task(school_slug: str, season: str, base_url: str) -> tuple[str, str, list[tuple[str, str]]]:
    return school_slug, season, fetch_roster(school_slug, season, base_url)


def init_sailors_db(conn: sqlite3.Connection, seasons: list[str]):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sailors (
            id         INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            school_slug TEXT NOT NULL,
            site       TEXT NOT NULL,
            class_year TEXT,
            UNIQUE(name, school_slug, site)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sailor_seasons (
            sailor_id INTEGER NOT NULL REFERENCES sailors(id),
            season    TEXT NOT NULL,
            PRIMARY KEY (sailor_id, season)
        )
    """)
    conn.commit()


def export_sailors_csv(conn: sqlite3.Connection, seasons: list[str], path: Path):
    """Export sailors table with binary season columns to CSV."""
    sailors = conn.execute("SELECT id, name, school_slug, site, class_year FROM sailors ORDER BY site, school_slug, name").fetchall()
    # Build lookup: sailor_id -> set of seasons
    appearances = {}
    for row in conn.execute("SELECT sailor_id, season FROM sailor_seasons"):
        appearances.setdefault(row[0], set()).add(row[1])

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "school_slug", "site", "class_year"] + seasons)
        for sid, name, school_slug, site, class_year in sailors:
            seen = appearances.get(sid, set())
            w.writerow([name, school_slug, site, class_year] + [1 if s in seen else 0 for s in seasons])
    log.info(f"Exported {len(sailors)} sailors to {path}")


def scrape_rosters(sites_to_run: list[str] = None, only_seasons: list[str] = None):
    """
    Scrape school rosters into sailors.db / sailors.csv.

    only_seasons: if given, restrict to those season codes (e.g. ["s24","f24"]).
                  if None, fetch ALL seasons dynamically from each site.
    Note: ICSA spring rosters return 404, so those are always skipped.
    """
    if sites_to_run is None:
        sites_to_run = ["issa", "icsa"]

    DB_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)

    # Build the ordered season list for CSV columns
    if only_seasons:
        all_seasons_ordered = sorted(set(only_seasons), key=lambda s: (s[1:], s[0]))
    else:
        # Union of all seasons across all chosen sites, sorted chronologically
        season_set: set[str] = set()
        for site in sites_to_run:
            season_set.update(get_all_seasons(SITES[site]["base_url"]))
        all_seasons_ordered = sorted(season_set, key=lambda s: (s[1:], s[0]))

    db_path = DB_DIR / "sailors.db"
    csv_path = DATA_DIR / "sailors.csv"
    conn = sqlite3.connect(db_path)
    init_sailors_db(conn, all_seasons_ordered)
    db_lock = threading.Lock()

    for site in sites_to_run:
        cfg = SITES[site]
        base_url = cfg["base_url"]

        if only_seasons:
            # For ICSA filter out spring (always 404)
            if site == "icsa":
                seasons = [s for s in only_seasons if s.startswith("f")]
            else:
                seasons = list(only_seasons)
        else:
            all_site_seasons = get_all_seasons(base_url)
            if site == "icsa":
                seasons = [s for s in all_site_seasons if s.startswith("f")]
            else:
                seasons = all_site_seasons

        slugs = get_school_slugs(base_url)
        log.info(f"[{site}] Fetching rosters for {len(slugs)} schools × {seasons}")

        tasks = [(slug, season) for slug in slugs for season in seasons]

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {
                executor.submit(_roster_task, slug, season, base_url): (slug, season)
                for slug, season in tasks
            }
            done = 0
            for future in as_completed(futures):
                slug, season, entries = future.result()
                done += 1
                if not entries:
                    continue
                with db_lock:
                    for name, class_year in entries:
                        conn.execute(
                            """INSERT INTO sailors(name, school_slug, site, class_year)
                               VALUES(?, ?, ?, ?)
                               ON CONFLICT(name, school_slug, site)
                               DO UPDATE SET class_year = COALESCE(excluded.class_year, class_year)""",
                            (name, slug, site, class_year or None),
                        )
                        row = conn.execute(
                            "SELECT id FROM sailors WHERE name=? AND school_slug=? AND site=?",
                            (name, slug, site),
                        ).fetchone()
                        if row:
                            conn.execute(
                                "INSERT OR IGNORE INTO sailor_seasons(sailor_id, season) VALUES(?,?)",
                                (row[0], season),
                            )
                    conn.commit()
                if done % 500 == 0:
                    log.info(f"[{site}] {done}/{len(tasks)} roster pages processed")

        log.info(f"[{site}] Rosters complete")

    export_sailors_csv(conn, all_seasons_ordered, csv_path)
    conn.close()
    log.info("Roster scraping done.")


# ---------------------------------------------------------------------------
# Season refresh  (rosters + regattas for a single season, both sites)
# ---------------------------------------------------------------------------

def _delete_season_regattas(conn: sqlite3.Connection, tr_conn: sqlite3.Connection, season: str):
    """Wipe all regatta rows whose URL starts with /{season}/ from fleet and TR DBs."""
    prefix = f"/{season}/%"
    # keelboat_sailors too: its rows are inserted with OR IGNORE, so stale ones would
    # shadow whatever the re-scrape reads.
    for tbl in ("race_results", "keelboat_results", "keelboat_sailors"):
        conn.execute(f"DELETE FROM {tbl} WHERE regatta_url LIKE ?", (prefix,))
    conn.commit()
    for tbl in ("tr_standings", "tr_sailors", "tr_matches", "tr_match_sailors"):
        tr_conn.execute(f"DELETE FROM {tbl} WHERE regatta_url LIKE ?", (prefix,))
    tr_conn.commit()


def _delete_season_rosters(sailors_conn: sqlite3.Connection, season: str, site: str):
    """Remove all sailor_seasons rows for this season+site combo."""
    sailors_conn.execute(
        """DELETE FROM sailor_seasons
           WHERE season = ?
             AND sailor_id IN (SELECT id FROM sailors WHERE site = ?)""",
        (season, site),
    )
    sailors_conn.commit()


def refresh_season(season: str, sites_to_run: list[str] = None, rosters: bool = True):
    if sites_to_run is None:
        sites_to_run = ["issa", "icsa"]

    DB_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    log.info(f"Refreshing season {season} for sites: {sites_to_run}")

    # Rosters are left alone when only the results are being re-read (old seasons).
    if rosters:
        # --- Rosters ---
        sailors_db = DB_DIR / "sailors.db"
        # Determine full ordered season list for CSV (include refresh season if new)
        known = ["s24", "f24", "s25", "f25", "s26", "f26"]
        all_seasons_ordered = [s for s in known if s <= season or s in ["s24", "f24", "s25", "f25"]]
        if season not in all_seasons_ordered:
            all_seasons_ordered.append(season)

        sailors_conn = sqlite3.connect(sailors_db)
        init_sailors_db(sailors_conn, all_seasons_ordered)
        db_lock = threading.Lock()

        for site in sites_to_run:
            cfg = SITES[site]
            base_url = cfg["base_url"]

            # Wipe existing roster data for this season+site
            _delete_season_rosters(sailors_conn, season, site)
            log.info(f"[{site}] Cleared {season} roster entries")

            slugs = get_school_slugs(base_url)
            log.info(f"[{site}] Re-fetching {season} rosters for {len(slugs)} schools")

            with ThreadPoolExecutor(max_workers=WORKERS) as executor:
                futures = {
                    executor.submit(_roster_task, slug, season, base_url): slug
                    for slug in slugs
                }
                for future in as_completed(futures):
                    slug, seas, entries = future.result()
                    if not entries:
                        continue
                    with db_lock:
                        for name, class_year in entries:
                            sailors_conn.execute(
                                """INSERT INTO sailors(name, school_slug, site, class_year)
                                   VALUES(?, ?, ?, ?)
                                   ON CONFLICT(name, school_slug, site)
                                   DO UPDATE SET class_year = COALESCE(excluded.class_year, class_year)""",
                                (name, slug, site, class_year or None),
                            )
                            row = sailors_conn.execute(
                                "SELECT id FROM sailors WHERE name=? AND school_slug=? AND site=?",
                                (name, slug, site),
                            ).fetchone()
                            if row:
                                sailors_conn.execute(
                                    "INSERT OR IGNORE INTO sailor_seasons(sailor_id, season) VALUES(?,?)",
                                    (row[0], season),
                                )
                        sailors_conn.commit()

            log.info(f"[{site}] {season} rosters refreshed")

        export_sailors_csv(sailors_conn, all_seasons_ordered, DATA_DIR / "sailors.csv")
        sailors_conn.close()

    # --- Regattas ---
    for site in sites_to_run:
        cfg = SITES[site]
        base_url   = cfg["base_url"]
        db_path    = cfg["db_path"]
        tr_db_path = cfg["tr_db_path"]
        tr_prefix  = cfg["tr_csv_prefix"]
        csv_path   = cfg["csv_path"]
        kb_prefix  = cfg["kb_prefix"]
        keelboats  = cfg["keelboats"]

        conn    = sqlite3.connect(db_path,    check_same_thread=False)
        tr_conn = sqlite3.connect(tr_db_path, check_same_thread=False)
        init_db(conn)
        init_keelboat_tables(conn)
        init_tr_db(tr_conn)
        init_rotation_table(conn)

        # Wipe existing regatta rows for this season
        _delete_season_regattas(conn, tr_conn, season)
        log.info(f"[{site}] Cleared {season} regatta rows")

        regattas = get_regattas(season, base_url)
        log.info(f"[{site}] {len(regattas)} regattas found for {season}")

        regatta_db_lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {
                executor.submit(scrape_regatta_task, season, name, slug, base_url, keelboats): slug
                for name, slug in regattas
            }
            for future in as_completed(futures):
                regatta_url, fleet_results, tr_standings, tr_sailors, tr_matches, match_sailors, kb_results, kb_sailors, rotations = future.result()
                with regatta_db_lock:
                    if fleet_results:
                        insert_results(conn, fleet_results)
                        log.info(f"  {len(fleet_results):>4} fleet rows — {regatta_url}")
                    if tr_standings:
                        insert_tr_results(tr_conn, tr_standings, tr_sailors, tr_matches, match_sailors)
                        log.info(f"  {len(tr_standings):>4} TR teams — {regatta_url}")
                    if kb_results:
                        insert_keelboat_results(conn, kb_results, kb_sailors)
                        log.info(f"  {len(kb_results):>4} keelboat rows — {regatta_url}")
                    if rotations:
                        insert_rotations(conn, rotations)
                        log.info(f"  {len(rotations):>4} rotation rows — {regatta_url}")

        # Before the exports: a regatta that changed kind is moved out of its old table first.
        save_regatta_kinds(conn, Path(str(csv_path).replace("_results.csv", "_regatta_kinds.csv")))
        export_csv(conn, csv_path)
        export_tr_csvs(tr_conn, tr_prefix)
        export_keelboat_csvs(conn, kb_prefix)
        rot_csv = Path(str(cfg["csv_path"]).replace("_results.csv", "_rotations.csv"))
        export_rotations_csv(conn, rot_csv)
        conn.close()
        tr_conn.close()
        log.info(f"[{site}] {season} regattas done")

    log.info(f"Refresh of {season} complete.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site",           choices=["issa", "icsa"], default=None)
    parser.add_argument("--limit",          type=int,  default=None)
    parser.add_argument("--seasons",        nargs="+", default=None, help="e.g. s25 f25")
    parser.add_argument("--logos",          action="store_true", help="Download school logos only")
    parser.add_argument("--rosters",        action="store_true", help="Scrape sailor rosters for s24/f24/s25/f25")
    parser.add_argument("--schools",        action="store_true", help="Scrape schools CSV (name, slug, city, state, png, conference)")
    parser.add_argument("--regattas",       action="store_true", help="Scrape regatta listings CSV (name, host, type, scoring, start_date, status, leading_week)")
    parser.add_argument("--refresh-season", metavar="SEASON",   help="Wipe and re-scrape one season's rosters + regattas (e.g. s26)")
    parser.add_argument("--results-only",   action="store_true", help="With --refresh-season: re-scrape the regattas and leave rosters untouched")
    args = parser.parse_args()
    if args.logos:
        sites = ["issa", "icsa"] if args.site is None else [args.site]
        for s in sites:
            download_school_logos(s)
    elif args.rosters:
        sites = ["issa", "icsa"] if args.site is None else [args.site]
        scrape_rosters(sites, only_seasons=args.seasons)
    elif args.schools:
        sites = ["issa", "icsa"] if args.site is None else [args.site]
        for s in sites:
            scrape_schools_csv(s)
    elif args.regattas:
        sites = ["issa", "icsa"] if args.site is None else [args.site]
        for s in sites:
            scrape_regattas_csv(s, only_seasons=args.seasons)
    elif args.refresh_season:
        sites = ["issa", "icsa"] if args.site is None else [args.site]
        refresh_season(args.refresh_season, sites, rosters=not args.results_only)
    else:
        main(site=args.site or "issa", limit=args.limit, only_seasons=args.seasons)
