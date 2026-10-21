"""Who is leading (or won) each regatta of a season, for the season index's "Leading" column.

The winner is the boat with the lowest total, the way each page shows it, not the school with the lowest sum over everything it
entered: a school sends one team to a fleet regatta but several sailors to a singlehanded one, so summing by school made the
school with the fewest entries "lead" (Webb Institute for ILCA North, which Hobart and William Smith won).

    fleet / team racing   the team (school + team name) with the lowest total over its divisions
    singlehanded          the sailor with the lowest total
    keelboat              the boat (school + team) with the lowest total over its divisions
"""
from __future__ import annotations

from collections import defaultdict


def _points(place, points):
    if points is not None:
        return points
    return place if place is not None and place < 900 else 0


def leaders(con, season: str) -> dict:
    """regatta name (lower case) -> {'url', 'school', 'slug'} for every regatta of the season that has results."""
    kind = {}
    try:
        kind = dict(con.execute("SELECT regatta_url, kind FROM regatta_kinds"))
    except Exception:
        pass
    out: dict = {}

    # fleet and singlehanded (fleet_results)
    tot = defaultdict(lambda: defaultdict(float))        # url -> boat key -> total
    pen = {}                                             # (url, boat key, division) -> division penalty points
    info = {}                                            # (url, boat key) -> school, slug
    names = {}
    for url, name, school, slug, team, skipper, div, place, pts, dpen in con.execute(
            "SELECT regatta_url, regatta_name, school, school_slug, team, skipper, division, place, points, div_penalty_points "
            "FROM fleet_results WHERE season=?", (season,)):
        names[url] = name
        key = (slug, skipper) if kind.get(url) == "singlehanded" else (slug, team)
        tot[url][key] += _points(place, pts)
        info[(url, key)] = (school or "", slug or "")
        if dpen:
            pen[(url, key, div)] = max(pen.get((url, key, div), 0), dpen)
    for (url, key, _div), p in pen.items():
        tot[url][key] += p
    for url, boats in tot.items():
        best = min(boats, key=lambda k: boats[k])
        school, slug = info[(url, best)]
        out[names[url].lower()] = {"url": url, "school": school, "slug": slug}

    # keelboat
    ktot = defaultdict(lambda: defaultdict(float))
    kinfo, knames, kpen = {}, {}, {}
    try:
        for url, name, school, slug, team, div, place, pts, dpen in con.execute(
                "SELECT regatta_url, regatta_name, school, school_slug, team, division, place, points, div_penalty_points "
                "FROM keelboat_results WHERE season=?", (season,)):
            knames[url] = name
            key = (slug, team)
            ktot[url][key] += _points(place, pts)
            kinfo[(url, key)] = (school or "", slug or "")
            if dpen:
                kpen[(url, key, div)] = max(kpen.get((url, key, div), 0), dpen)
    except Exception:
        pass
    for (url, key, _div), p in kpen.items():
        ktot[url][key] += p
    for url, boats in ktot.items():
        best = min(boats, key=lambda k: boats[k])
        school, slug = kinfo[(url, best)]
        out.setdefault(knames[url].lower(), {"url": url, "school": school, "slug": slug})

    # team racing: first place in the standings
    try:
        for url, name, school, slug in con.execute(
                "SELECT regatta_url, regatta_name, school, school_slug FROM tr_standings WHERE season=? AND place=1", (season,)):
            out.setdefault(name.lower(), {"url": url, "school": school or "", "slug": slug or ""})
    except Exception:
        pass
    return out


def school_finishes(con) -> list:
    """(season, regatta name, url, school slug, school place, number of schools, type) for every fleet, singlehanded and
    keelboat regatta, one row per school.

    A school's place is where its best boat finished, ranking schools by that boat's total.  The old query summed over
    everything a school entered, so a school with two teams (or, in a singlehanded regatta, five sailors) was ranked on the
    sum and fell behind schools that entered one boat."""
    kind = {}
    try:
        kind = dict(con.execute("SELECT regatta_url, kind FROM regatta_kinds"))
    except Exception:
        pass
    out = []
    for table, is_keel in (("fleet_results", False), ("keelboat_results", True)):
        tot = defaultdict(lambda: defaultdict(float))     # url -> (slug, boat) -> total
        pen = {}
        meta = {}
        cols = "regatta_url, season, regatta_name, school_slug, " + ("team, " if is_keel else "team, skipper, ") + \
               "division, place, points, div_penalty_points"
        try:
            cur = con.execute(f"SELECT {cols} FROM {table}")
        except Exception:
            continue
        for row in cur:
            if is_keel:
                url, season, name, slug, team, div, place, pts, dpen = row
                boat = (slug, team)
            else:
                url, season, name, slug, team, skipper, div, place, pts, dpen = row
                boat = (slug, skipper) if kind.get(url) == "singlehanded" else (slug, team)
            meta[url] = (season, name)
            tot[url][boat] += _points(place, pts)
            if dpen:
                pen[(url, boat, div)] = max(pen.get((url, boat, div), 0), dpen)
        for (url, boat, _d), p in pen.items():
            tot[url][boat] += p
        for url, boats in tot.items():
            best = {}
            for (slug, _b), t in boats.items():
                best[slug] = min(best.get(slug, t), t)
            order = sorted(best.items(), key=lambda kv: kv[1])
            place, prev = 0, None
            for i, (slug, t) in enumerate(order, 1):
                if t != prev:
                    place = i
                    prev = t
                season, name = meta[url]
                typ = "keelboat" if is_keel else ("singlehanded" if kind.get(url) == "singlehanded" else "fleet")
                out.append((season, name, url, slug, place, len(order), typ))
    return out
