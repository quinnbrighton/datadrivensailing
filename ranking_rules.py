#!/usr/bin/env python3
"""
Which predicted teams may be used in a ranking.

A fleet regatta with two or more divisions is sailed A and B (and C...).  A team we can
only predict one skipper for is not a real team in the simulation: it would be scored on
one boat against two-boat teams and rank well or badly for no reason about its sailing.
So a team is ranked only when it has a predicted skipper in BOTH the A and B division.
A third division (C) is not required.

Only multi-division fleet events are filtered.  Single-division events have no B to
predict, and singlehanded / keelboat events are staffed by their own rules.
"""


def _named(lineup):
    return [p for p in (lineup or []) if p.get("name")]


def has_a_and_b(lineup):
    return len(_named(lineup)) >= 2


def rankable_schools(schools, kind, divisions=None):
    """Drop teams without an A and a B predicted skipper; return the others, order unchanged.

    `divisions` is the event's division count; when unknown it is taken to be the largest
    predicted lineup in the field (so a field of all one-boat teams is left alone)."""
    if kind != "fleet":
        return list(schools)
    if divisions is None:
        divisions = max((len(_named(s.get("lineup"))) for s in schools), default=0)
    if divisions < 2:
        return list(schools)
    return [s for s in schools if has_a_and_b(s.get("lineup"))]
