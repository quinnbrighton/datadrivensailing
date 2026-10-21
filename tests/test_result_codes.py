"""Coded race results (DSQ, BKD, DNF ...) keep their code and the points awarded."""
import pytest
from bs4 import BeautifulSoup

import scraper

PAGE = """
<table class="results">
<tr><th></th><th></th><th>Team</th><th>Div.</th><th>1</th><th>2</th><th>3</th><th>4</th><th></th><th>TOT</th></tr>
<tr class="divA"><td></td><td>1</td><td><a href="/schools/virginia-tech/f26/">Virginia Tech</a></td><td>A</td>
  <td class="right">1</td><td title="(2: average in division) " class="right">BKD</td>
  <td title="(9, Fleet + 1)" class="right">DSQ</td><td class="right">4</td><td></td><td>16</td></tr>
<tr class="divB"><td></td><td></td><td>Hokies</td><td>B</td>
  <td title="(9, Fleet + 1)" class="right">DNF</td><td class="right">3</td>
  <td title="(9, Fleet + 1)" class="right">XYZ</td><td class="right"></td><td></td><td>21</td></tr>
<tr class="totalrow"><td></td><td></td><td></td><td></td><td>10</td><td>15</td><td>33</td><td>37</td><td></td><td>37</td></tr>
</table>
"""


@pytest.mark.unit
def test_every_coded_cell_is_kept_with_its_points():
    a, b = scraper.parse_full_scores(BeautifulSoup(PAGE, "html.parser"))
    assert (a.school_slug, a.team_name, a.division) == ("virginia-tech", "Hokies", "A")
    assert a.race_scores == {1: 1, 2: 996, 3: 990, 4: 4}
    assert a.race_codes == {2: ("BKD", 2), 3: ("DSQ", 9)}
    assert b.race_scores == {1: 999, 2: 3, 3: 990}          # race 4 was not sailed: no entry
    assert b.race_codes == {1: ("DNF", 9), 3: ("XYZ", 9)}   # an unknown code is kept, not dropped


@pytest.mark.unit
def test_coded_results_never_look_like_a_finish():
    for code, value in scraper.PENALTY_SCORES.items():
        assert value >= 990, code


from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def soup(name):
    return BeautifulSoup((FIX / name).read_text(encoding="utf-8"), "html.parser")


@pytest.mark.unit
def test_real_page_with_codes_adds_up_to_the_published_totals():
    entries = scraper.parse_full_scores(soup("techscore_codes.html"))
    totals = {}
    for e in entries:
        pts = sum(e.race_codes[r][1] if r in e.race_codes else p for r, p in e.race_scores.items())
        totals[e.school_slug] = totals.get(e.school_slug, 0) + pts
    assert totals["virginia-tech"] == 36 and totals["virginia"] == 86 and totals["catholic-america"] == 155


@pytest.mark.unit
def test_team_penalty_column_is_read():
    entries = scraper.parse_full_scores(soup("techscore_team_penalty.html"))
    uva = {e.division: e for e in entries if e.school_slug == "virginia"}
    assert uva["A"].penalty == ("LOP", 20) and uva["B"].penalty == ("LOP", 20)
    assert sum(1 for e in entries if e.penalty[0]) == 2       # nobody else was penalised


@pytest.mark.unit
def test_singlehanded_keeps_every_sailor_from_a_school():
    scores = scraper.parse_full_scores(soup("techscore_singlehanded.html"))
    sailors = scraper.parse_sailors(soup("techscore_singlehanded_sailors.html"))
    rows = scraper.join_results("f26", "SEISA Men", "/f26/seisa-men-single-handed", "ILCA", scores, sailors)
    tulane = {(r.team, r.skipper) for r in rows if r.school_slug == "tulane"}
    assert len(tulane) == 3, tulane
    assert ("Green Wave 1", "Hamilton Barclay") in tulane
    keys = [(r.division, r.race_num, r.school_slug, r.team) for r in rows]
    assert len(keys) == len(set(keys))            # nothing collides on the database's unique key


@pytest.mark.unit
def test_boat_with_no_sailors_is_not_merged_into_its_sister_boat():
    scores = [scraper.ScoreEntry("st-mary", "Seahawks 1", "A", {1: 4, 2: 6}),
              scraper.ScoreEntry("st-mary", "Seahawks 2", "A", {1: 998, 2: 998},
                                 {1: ("DNS", 19), 2: ("DNS", 19)})]
    sailors = [scraper.SailorSlot(school="St. Mary's College of Maryland", school_slug="st-mary",
                                  team="Seahawks 1", division="A", skipper="Lina Carper",
                                  skipper_grade="'27", skipper_races=set(), crew="", crew_grade="",
                                  crew_races=set())]
    rows = scraper.join_results("f26", "X", "/f26/x", "ILCA", scores, sailors)
    assert {(r.team, r.skipper) for r in rows} == {("Seahawks 1", "Lina Carper"), ("Seahawks 2", "")}
    assert {r.school for r in rows} == {"St. Mary's College of Maryland"}
    assert [r.points for r in rows if r.team == "Seahawks 2"] == [19, 19]
