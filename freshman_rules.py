#!/usr/bin/env python3
"""
Shared rule for freshman-only regattas.

A few events are sailed by first-year sailors only; the Nickerson Trophy is one.  The lineup
predictor (analysis/predict_upcoming_lineups.py) and the fantasy game (fantasy_server.py) both
build a candidate pool per school, so the rule lives here rather than being written twice
(compare womens_rules.py).

Like the singlehanded filter this is a HARD filter with no fallback: a school with no known
first-year sailors simply has no prediction, rather than being handed upperclassmen who could
not legally sail.  A sailor with no class year on file is not a freshman we can vouch for, so
is excluded too.

To add another freshman-only event, add its name to FRESHMAN_ONLY_RE.
"""
import re

FRESHMAN_ONLY_RE = re.compile(r"\bnickerson\b", re.I)


def is_freshman_only(reg_name):
    return bool(reg_name) and bool(FRESHMAN_ONLY_RE.search(reg_name))


def freshman_class_year(season):
    """Graduation year of that season's first-year class: f26 -> 2030, s27 -> 2030."""
    try:
        yy = int(str(season)[1:])
    except (ValueError, IndexError):
        return None
    year = 2000 + yy
    if str(season)[0] == "f":
        year += 1          # a fall season belongs to the academic year that ends the next spring
    return year + 3


def freshman_pool(pool, grade_year, season, reg_name):
    """Restrict a candidate pool to first-years at a freshman-only regatta; else return it unchanged."""
    if not is_freshman_only(reg_name):
        return pool
    cls = freshman_class_year(season)
    if cls is None:
        return pool
    return {nm for nm in pool if grade_year.get(nm) is not None and grade_year[nm] >= cls}
