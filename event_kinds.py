"""What kind of event a regatta is, worked out from how its pages are laid out.

Doublehanded fleet racing is the common case and everything was first built around it:
a team is a school's boat in a division, sailed by one skipper and one crew.  Two kinds
of event do not fit that shape:

  singlehanded   the entry is a sailor.  A school may have several, there is no crew, and
                 the scores page names the sailor where a team name would be.
  keelboat       the entry is a whole crew: one skipper and several crew aboard at once,
                 often with reserves.

The kind used to be guessed from a fixed list of boat names, so a boat missing from the
list (Yngling, PHRF, J-105 ...) was read as a dinghy and lost all but one of its crew, and
singlehanded events were not recognised at all.  Here the pages decide, and the boat list
is only a fallback for a regatta whose sailors were never entered.

This module only reads pages and decides; it writes nothing.  scraper.py stores what it
returns (regatta_kinds, keelboat_sailors.role).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from bs4 import BeautifulSoup

FLEET = "fleet"
SINGLEHANDED = "singlehanded"
KEELBOAT = "keelboat"
TEAM = "team"
KINDS = (FLEET, SINGLEHANDED, KEELBOAT, TEAM)

SKIPPER = "skipper"
CREW = "crew"
RESERVE = "reserve"

# Boats sailed by one person, and words that mark a singlehanded regatta's name.  Only
# consulted when the pages cannot say.
SINGLEHANDED_BOATS = ("laser", "radial", "ilca", "(single)")
SINGLEHANDED_NAMES = ("singlehand", "single-hand", "single hand", "laser", "radial", "ilca", "solo")


def _solo_boat(boat: str) -> bool:
    return "/" not in boat and any(b in boat.lower() for b in SINGLEHANDED_BOATS)


def _solo_name(name: str) -> bool:
    return any(w in name.lower().replace("_", "-") for w in SINGLEHANDED_NAMES)


@dataclass(frozen=True)
class CrewMember:
    name: str
    grade: str
    role: str                     # SKIPPER, CREW or RESERVE
    races: frozenset = frozenset()   # empty = every race
    races_text: str = ""


@dataclass
class Entry:
    """One boat on the sailors page: a school's team in a division, and who sailed it."""
    school: str
    school_slug: str
    team: str
    division: str
    members: list = field(default_factory=list)

    def aboard(self, role: Optional[str] = None) -> list:
        return [m for m in self.members if m.role != RESERVE and (role is None or m.role == role)]


@dataclass(frozen=True)
class EventKind:
    kind: str
    crew_size: int       # people aboard one boat at once, skipper included; 0 if unknown
    reason: str          # what decided it, for the log and for anyone checking a surprise


# ── Reading the sailors page ──────────────────────────────────────────────────

_GRADE = re.compile(r"\s*('\d{2})\s*$")


def _name_grade(text: str) -> tuple[str, str]:
    text = re.sub(r"[\s*#^~+]+$", "", " ".join(text.split()))
    m = _GRADE.search(text)
    if m:
        return text[:m.start()].strip(), m.group(1)
    return text, ""


def parse_races(text: str) -> frozenset:
    """'1-4, 7' -> {1, 2, 3, 4, 7}.  Empty text means every race and gives an empty set."""
    out = set()
    for part in re.split(r"[,\s]+", text.strip()):
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            out.update(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.isdigit():
            out.add(int(part))
    return frozenset(out)


def _resolve_school(school_text: str, team_text: str, division: str, scores) -> tuple:
    """(school_slug, team name) for a boat whose school cell carries no link.

    Pages from before the results site linked school names (the 2015 sloop and match-race regattas) leave the slug
    out, which used to drop the whole boat.  The full-scores page names every boat with a slug, a team and a division,
    so the team name (or, where one cell holds "Michigan State Spartans", the end of the text) finds it.  Two boats with
    the same team name are told apart by how many words of the school text appear in each slug; a tie resolves nothing.
    """
    if not scores:
        return "", team_text
    g = lambda r, k: (r.get(k) if isinstance(r, dict) else getattr(r, k, "")) or ""
    rows = [r for r in scores if not division or g(r, "division").upper() == division]
    tn = lambda r: g(r, "team_name").strip().lower()
    st, tt = school_text.strip().lower(), (team_text or "").strip().lower()
    hits = [r for r in rows if tn(r) and tn(r) == tt]
    if not hits:
        hits = [r for r in rows if tn(r) and (st.endswith(tn(r)) or tn(r) in st)]
    slugs = {g(r, "school_slug") for r in hits if g(r, "school_slug")}
    if len(slugs) == 1:
        return next(iter(slugs)), g(hits[0], "team_name") or team_text
    if len(slugs) > 1:
        words = set(re.findall(r"[a-z0-9]+", st))
        ranked = sorted(slugs, key=lambda x: -len(words & set(x.split("-"))))
        if len(words & set(ranked[0].split("-"))) > len(words & set(ranked[1].split("-"))):
            best = next(r for r in hits if g(r, "school_slug") == ranked[0])
            return ranked[0], g(best, "team_name") or team_text
    return "", team_text


def parse_entries(soup: BeautifulSoup, scores=None) -> list[Entry]:
    """Every boat on a sailors page with its people and their roles.

    The table has a Skipper column and a Crew column, each followed by a Races column.  A
    boat's first row carries the school, team and division; the rows under it carry more
    people, in whichever of the two columns they belong.  The column is the role: that is
    the one thing the page tells us that a flat list of names does not.
    """
    table = soup.find("table")
    if not table:
        return []
    entries: list[Entry] = []
    school = slug = team = division = ""
    current: Optional[Entry] = None

    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue

        reserves = [c for c in cells if "reserves-cell" in (c.get("class") or [])]
        if reserves:
            if current is not None:
                names = [a.get_text(strip=True) for a in reserves[0].find_all("a")] \
                        or [reserves[0].get_text(strip=True)]
                for n in names:
                    name, grade = _name_grade(n)
                    if name:
                        current.members.append(CrewMember(name, grade, RESERVE))
            continue

        new_boat = False
        for c in cells:
            cls = c.get("class") or []
            if "schoolname" in cls:
                a = c.find("a", href=re.compile(r"/schools/"))
                school = (a or c).get_text(strip=True)
                m = re.search(r"/schools/([^/]+)/", a["href"]) if a else None
                slug = m.group(1) if m else ""
                new_boat = True
            if "teamname" in cls:                        # older pages put school and team in one cell with both classes
                team = c.get_text(strip=True)
                new_boat = True
            if "division-cell" in cls:
                division = c.get_text(strip=True).upper()
                new_boat = True
        if new_boat and not slug and scores:
            slug, team = _resolve_school(school, team, division, scores)
        if not slug:
            continue
        if new_boat or current is None:
            current = Entry(school, slug, team, division)
            entries.append(current)

        structural = {"schoolname", "teamname", "division-cell", "rank-cell"}
        data = [c for c in cells if not (set(c.get("class") or []) & structural)]
        # data is [skipper, races, crew, races]; a short row still keeps that order
        for idx, role in ((0, SKIPPER), (2, CREW)):
            if idx >= len(data):
                continue
            name, grade = _name_grade(data[idx].get_text(strip=True))
            if not name:
                continue
            races_text = data[idx + 1].get_text(strip=True) if idx + 1 < len(data) else ""
            current.members.append(CrewMember(name, grade, role, parse_races(races_text), races_text))
    return entries


def crew_aboard(entry: Entry) -> int:
    """The most crew (skipper not counted) aboard this boat in any one race.

    Substitutes do not count twice: two crews who sailed races 1-4 and 5-8 are one seat.
    Someone with no races listed sailed them all.
    """
    crews = entry.aboard(CREW)
    if not crews:
        return 0
    races = set().union(*(m.races for m in crews))
    if not races:
        return len(crews)
    return max(sum(1 for m in crews if not m.races or r in m.races) for r in races)


# ── Deciding ──────────────────────────────────────────────────────────────────

def _median(values: list[int]) -> int:
    values = sorted(values)
    return values[len(values) // 2] if values else 0


def classify(
    *,
    scoring: str = "",
    boat: str = "",
    sailor_rows: bool = False,
    entries: Iterable[Entry] = (),
    known_keelboats: Iterable[str] = (),
    is_team_race: bool = False,
    name: str = "",
) -> EventKind:
    """Decide the kind of event.

    scoring          the regatta's Scoring field ("Singlehanded", "2 Divisions", "Team" ...)
    boat             its Boat field
    sailor_rows      the scores page names sailors where team names would be
    entries          parse_entries() of its sailors page
    known_keelboats  boat names to treat as keelboats when no sailors were entered
    name             the regatta's name or address, a hint of last resort
    """
    entries = list(entries)
    if is_team_race or scoring.strip().lower() == "team":
        return EventKind(TEAM, 0, "team race scoring")

    if sailor_rows:
        return EventKind(SINGLEHANDED, 1, "scores page lists sailors, not teams")
    if scoring.strip().lower() == "singlehanded":
        return EventKind(SINGLEHANDED, 1, "scoring is Singlehanded")

    manned = [e for e in entries if e.aboard()]
    if manned:
        seats = [crew_aboard(e) for e in manned]
        typical = _median(seats)
        if typical >= 2:
            return EventKind(KEELBOAT, typical + 1, f"{typical} crew aboard at once")
        if typical == 0 and all(not e.aboard(CREW) for e in manned) \
                and (_solo_boat(boat) or _solo_name(name)):
            # An empty crew column is not proof by itself: hosts of doublehanded regattas
            # sometimes enter skippers only.
            return EventKind(SINGLEHANDED, 1, "no boat has a crew, singlehanded boat or name")
        if boat in set(known_keelboats):
            # A keelboat whose hosts entered only a skipper and one crew.
            return EventKind(KEELBOAT, typical + 1, f"listed keelboat ({boat})")
        return EventKind(FLEET, 2, "one skipper and one crew per boat")

    # Nobody entered sailors: fall back on the boat's name.
    if boat in set(known_keelboats):
        return EventKind(KEELBOAT, 0, f"listed keelboat ({boat}), no sailors entered")
    if _solo_boat(boat):
        return EventKind(SINGLEHANDED, 1, f"singlehanded boat ({boat}), no sailors entered")
    return EventKind(FLEET, 2, "default")
