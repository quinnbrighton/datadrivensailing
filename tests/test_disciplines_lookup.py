"""Kind and crew size of an upcoming regatta, from its history first and its name last."""
import csv

import pytest

import disciplines as d


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "DATA", tmp_path)
    monkeypatch.setattr(d, "RATINGS_DIR", tmp_path)
    d._cache.clear()
    regs = [("f25", "2025 Shields Trophy", "1 Division", "https://x/f25/shields"),
            ("f25", "Danmark Trophy", "2 Divisions", "https://x/f25/danmark"),
            ("f25", "NEISA Open Singlehanded Championship", "Singlehanded", "https://x/f25/neisa-single"),
            ("s26", "Laser Performance Team Race", "Team", "https://x/s26/lp-team"),
            ("f24", "Harbor Cup Offshore", "1 Division", "https://x/f24/harbor-cup"),
            ("f25", "Harbor Cup Offshore", "1 Division", "https://x/f25/harbor-cup")]
    with open(tmp_path / "icsa_regattas.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["season", "name", "scoring", "url"]); w.writerows(regs)
    kinds = [("/f25/shields", "f25", "keelboat", 8), ("/f25/danmark", "f25", "fleet", 2),
             ("/f25/neisa-single", "f25", "singlehanded", 1), ("/s26/lp-team", "s26", "team", 0),
             ("/f24/harbor-cup", "f24", "keelboat", 6), ("/f25/harbor-cup", "f25", "keelboat", 9)]
    with open(tmp_path / "icsa_regatta_kinds.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["regatta_url", "season", "kind", "crew_size"]); w.writerows(kinds)
    with open(tmp_path / "ratings_keelboat.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sailor_id", "name", "year", "school", "league", "rating", "main_rating", "offset",
                    "races", "last_season"])
        w.writerows([["C1", "Ann Lee", "2027", "Yale", "ICSA", "1.5", "", "0.4", "40", "f26"],
                     ["S2", "Ann Lee", "2019", "Navy", "ICSA", "0.2", "", "0.1", "12", "f18"],
                     ["C3", "Bo Roy", "2028", "Yale", "ICSA", "0.4", "", "0.0", "2", "f26"],
                     ["C4", "Cy Hsu", "2028", "Tabor", "ISSA", "0.9", "", "0.0", "9", "f26"]])
    return tmp_path


@pytest.mark.unit
def test_name_key_drops_what_changes_each_year():
    assert d.name_key("2025 Shields Trophy") == d.name_key("Shields Trophy 2026") == "shields trophy"
    assert d.name_key("84th Erwin Schell") == d.name_key("85th Erwin Schell")


@pytest.mark.unit
def test_history_decides_kind_and_crew_size(site):
    k = d.kind_of("f26", "2026 Shields Trophy")
    assert (k.kind, k.crew_size) == ("keelboat", 8)           # not a flat four
    assert d.kind_of("f26", "Harbor Cup Offshore").crew_size == 9   # the latest edition wins
    assert d.kind_of("f26", "Danmark Trophy").kind == "fleet"
    assert d.kind_of("f26", "NEISA Open Singlehanded Championship").singlehanded


@pytest.mark.unit
def test_scoring_beats_history_and_name(site):
    assert d.kind_of("f26", "Danmark Trophy", "Singlehanded").singlehanded
    assert d.kind_of("f26", "Laser Performance Team Race", "Team").kind == "team"


@pytest.mark.unit
def test_name_is_the_last_resort(site):
    assert d.kind_of("f26", "Brand New Laser Open").singlehanded
    k = d.kind_of("f26", "Brand New Match Race Invite")
    assert (k.kind, k.crew_size, k.reason) == ("keelboat", 4, "name")
    assert d.kind_of("f26", "Brand New Dinghy Open").kind == "fleet"
    # a team race called "Laser Performance ..." is not singlehanded: its history says team
    assert d.kind_of("f26", "Laser Performance Team Race").kind == "team"


@pytest.mark.unit
def test_a_regatta_is_not_its_own_history(site):
    # asked about f25 itself, the f25 edition must not answer; f24 does
    assert d.kind_of("f25", "Harbor Cup Offshore").crew_size == 6


@pytest.mark.unit
def test_ratings_by_name_prefer_the_current_sailor(site):
    r = d.ratings("keelboat")
    assert r == {"Ann Lee": 1.5, "Bo Roy": 0.4}              # ISSA sailor excluded, namesake resolved
    assert d.veterans("keelboat") == {"Ann Lee", "Bo Roy"}
    assert d.ratings("fleet") == {}
    assert d.newcomer_rating("keelboat") == 0.4


@pytest.mark.unit
def test_rating_for_matches_the_school_too(site):
    assert d.rating_for("keelboat", "Ann Lee", "Yale") == 1.5
    assert d.rating_for("keelboat", "Ann Lee", "Navy") == 0.2
    assert d.rating_for("keelboat", "Ann Lee", "Fordham University") is None   # two Ann Lees, neither here
    assert d.rating_for("keelboat", "Bo Roy", "Fordham University") is None    # a different Bo Roy
    assert d.rating_for("keelboat", "Bo Roy") == 0.4                           # no school given: unique name
    assert d.rating_for("keelboat", "Nobody", "Yale") is None
