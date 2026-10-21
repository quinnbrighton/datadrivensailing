"""The kind of event is read from the page layout, not from a list of boat names."""
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

import event_kinds as ek

FIX = Path(__file__).parent / "fixtures"


def entries(name):
    return ek.parse_entries(BeautifulSoup((FIX / name).read_text(encoding="utf-8"), "html.parser"))


@pytest.mark.unit
def test_keelboat_page_gives_one_skipper_and_the_rest_crew():
    boats = entries("techscore_keelboat_sailors.html")
    cal = next(b for b in boats if b.school_slug == "cal-maritime")
    assert [m.name for m in cal.aboard(ek.SKIPPER)] == ["Owen Cuyler"]
    assert len(cal.aboard(ek.CREW)) >= 7
    assert ek.crew_aboard(cal) >= 7
    kind = ek.classify(boat="Some Boat Nobody Listed", scoring="1 Division", entries=boats)
    assert kind.kind == ek.KEELBOAT and kind.crew_size >= 4


@pytest.mark.unit
def test_doublehanded_substitutes_do_not_look_like_a_big_crew():
    boats = entries("techscore_fleet_sailors.html")
    uva = next(b for b in boats if b.school_slug == "virginia" and b.division == "A")
    assert [m.name for m in uva.aboard(ek.CREW)] == ["Nick Feddersen", "Elsa Fellon", "Lola Humes"]
    assert ek.crew_aboard(uva) == 1               # three crews over the day, one at a time
    # a row holding only a crew (no skipper change) is still read
    assert next(m for m in uva.members if m.name == "Lola Humes").races == {9, 10}
    assert ek.classify(boat="FJ", scoring="2 Divisions", entries=boats) == \
        ek.EventKind(ek.FLEET, 2, "one skipper and one crew per boat")


@pytest.mark.unit
def test_singlehanded_is_recognised_three_ways():
    boats = entries("techscore_singlehanded_sailors.html")
    assert all(not b.aboard(ek.CREW) for b in boats)
    assert ek.classify(boat="ILCA", scoring="1 Division", sailor_rows=True).kind == ek.SINGLEHANDED
    assert ek.classify(boat="Whatever", scoring="Singlehanded").kind == ek.SINGLEHANDED
    assert ek.classify(boat="Laser", scoring="1 Division", entries=boats).kind == ek.SINGLEHANDED


@pytest.mark.unit
@pytest.mark.parametrize("boat, scoring, keel, expected", [
    ("Navy 44", "1 Division", {"Navy 44"}, ek.KEELBOAT),   # no sailors entered: the list decides
    ("Laser", "1 Division", set(), ek.SINGLEHANDED),
    ("420/FJ/Laser", "2 Divisions", set(), ek.FLEET),      # a mixed fleet is not singlehanded
    ("FJ", "2 Divisions", set(), ek.FLEET),
    ("FJ", "Team", set(), ek.TEAM),
])
def test_fallback_when_no_sailors_were_entered(boat, scoring, keel, expected):
    assert ek.classify(boat=boat, scoring=scoring, known_keelboats=keel).kind == expected


@pytest.mark.unit
def test_parse_races():
    assert ek.parse_races("1-4, 7") == {1, 2, 3, 4, 7}
    assert ek.parse_races("") == frozenset()


@pytest.mark.unit
def test_skippers_only_is_not_proof_of_singlehanded():
    boats = entries("techscore_singlehanded_sailors.html")      # nobody has a crew
    assert ek.classify(boat="FJ", scoring="2 Divisions", entries=boats, name="South Points 5").kind == ek.FLEET
    assert ek.classify(boat="FJ", scoring="1 Division", entries=boats, name="Laser North laser-north").kind == ek.SINGLEHANDED
    assert ek.classify(boat="C420/Radial", scoring="1 Division", entries=boats,
                       name="NWISA Singlehanded Championships").kind == ek.SINGLEHANDED
