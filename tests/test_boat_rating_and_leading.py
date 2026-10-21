"""Boat strength for singlehanded / keelboat entries, and who leads a regatta."""
import csv
import json
import sqlite3

import pytest

import disciplines as d
import leading


@pytest.fixture
def kb(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "RATINGS_DIR", tmp_path)
    d._cache.clear()
    with open(tmp_path / "ratings_keelboat.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sailor_id", "name", "year", "school", "league", "rating", "main_rating", "offset", "races", "last_season"])
        w.writerows([["C1", "Ann Lee", "2027", "Navy", "ICSA", "2.0", "4.0", "0", "20", "f26"],
                     ["C2", "Bo Roy", "2027", "Navy", "ICSA", "1.0", "2.0", "0", "20", "f26"]])
    with open(tmp_path / "school_effects_keelboat.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["school_slug", "school", "league", "offset", "boat_races"])
        w.writerow(["navy", "Navy", "ICSA", "0.9", "400"])
    (tmp_path / "model_keelboat.json").write_text(json.dumps(
        {"variant": "full", "k": 0.5, "kc": 0.25, "c0": 0.0, "use_crew": True, "temperature": 1.0}))
    return tmp_path


@pytest.mark.unit
def test_keelboat_boat_is_helm_plus_crew_plus_school(kb):
    strength, helm = d.boat_rating("keelboat", ["Ann Lee", "Bo Roy", "Nobody"], "Navy")
    assert helm == "Ann Lee"
    assert strength == pytest.approx(2.0 + 0.25 * 2.0 + 0.9)          # helm rating + kc * crew main + school effect


@pytest.mark.unit
def test_unknown_school_adds_nothing_and_unknown_crew_uses_c0(kb):
    strength, helm = d.boat_rating("keelboat", ["Ann Lee"], "Somewhere Else")
    assert helm == "Ann Lee" and strength == pytest.approx(2.0)


@pytest.mark.unit
def test_temperature_compresses_the_scale(kb):
    (kb / "model_keelboat.json").write_text(json.dumps({"variant": "base", "k": 0.5, "temperature": 0.5}))
    d._cache.clear()
    assert d.boat_rating("keelboat", ["Ann Lee"], "Navy")[0] == pytest.approx(1.0)


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE regatta_kinds (regatta_url TEXT, kind TEXT)")
    c.execute("CREATE TABLE fleet_results (regatta_url, season, regatta_name, school, school_slug, team, skipper, division, place, points, div_penalty_points)")
    c.execute("CREATE TABLE keelboat_results (regatta_url, season, regatta_name, school, school_slug, team, division, place, points, div_penalty_points)")
    c.execute("CREATE TABLE tr_standings (regatta_url, season, regatta_name, school, school_slug, place)")
    c.execute("INSERT INTO regatta_kinds VALUES ('/f26/solo','singlehanded')")
    rows = [  # the school with most sailors must not win just because it entered the most
        ("/f26/solo", "f26", "Solo Cup", "Big U", "big", "Bears", "A", "A", 5, 5, None),
        ("/f26/solo", "f26", "Solo Cup", "Big U", "big", "Bears", "B", "A", 6, 6, None),
        ("/f26/solo", "f26", "Solo Cup", "Big U", "big", "Bears", "C", "A", 7, 7, None),
        ("/f26/solo", "f26", "Solo Cup", "Tiny U", "tiny", "Owls", "Z", "A", 1, 1, None)]
    c.executemany("INSERT INTO fleet_results VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    return c


@pytest.mark.unit
def test_leader_is_the_best_boat_not_the_smallest_school_total(con):
    assert leading.leaders(con, "f26")["solo cup"]["slug"] == "tiny"
    fin = {r[3]: r for r in leading.school_finishes(con)}
    assert fin["tiny"][4] == 1 and fin["big"][4] == 2 and fin["big"][6] == "singlehanded"


@pytest.mark.unit
def test_school_with_two_teams_is_ranked_on_its_best_team(con):
    con.execute("INSERT INTO fleet_results VALUES ('/f26/fleet','f26','Fleet Open','Two U','two','Teal','','A',2,2,NULL)")
    con.execute("INSERT INTO fleet_results VALUES ('/f26/fleet','f26','Fleet Open','Two U','two','Gold','','A',9,9,NULL)")
    con.execute("INSERT INTO fleet_results VALUES ('/f26/fleet','f26','Fleet Open','One U','one','Owls','','A',5,5,NULL)")
    fin = {r[3]: r[4] for r in leading.school_finishes(con) if r[2] == "/f26/fleet"}
    assert fin == {"two": 1, "one": 2}
