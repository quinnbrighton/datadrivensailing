"""Deciding whether a regatta is a women's event (womens_rules.py).

Protects the three layers (exact CSV name, the "wom[ae]n" name rule, the year-stripped CSV lookup), their reporting
labels, and the trap noted in the module: "Women's" contains "men's", so men's events must not match.
"""
import pytest

import womens_rules as wr

pytestmark = pytest.mark.unit

CSV_NAMES = {"Toni Deutsch '58 Trophy 2025", "Regatta With No Hint"}


@pytest.mark.parametrize("name", [
    "Women's Singlehanded Championship", "NEISA Womens Dinghy", "ICSA Women’s Semis", "Open (woman) Cup", "WOMEN'S TEAM RACE",
])
def test_women_in_the_name_is_enough(name):
    assert wr.is_womens_regatta(name, set())
    assert wr.womens_reason(name, set()) == "name-rule"


@pytest.mark.parametrize("name", [
    "Men's Singlehanded Championship", "Mens Team Race", "Hinman Trophy", "Menominee Invite", "Fall Open 2026",
])
def test_men_only_names_do_not_match_by_substring(name):
    assert not wr.is_womens_regatta(name, set())
    assert wr.womens_reason(name, set()) == ""


def test_exact_csv_name_matches_even_without_a_hint():
    assert wr.is_womens_regatta("Regatta With No Hint", CSV_NAMES)
    assert wr.womens_reason("Regatta With No Hint", CSV_NAMES) == "csv-exact"


def test_csv_name_takes_precedence_over_the_name_rule():
    assert wr.womens_reason("Women's Cup", {"Women's Cup"}) == "csv-exact"


def test_new_edition_of_a_csv_event_matches_with_the_year_stripped():
    name = "Toni Deutsch '58 Trophy 2026"
    assert wr.is_womens_regatta(name, CSV_NAMES)
    assert wr.womens_reason(name, CSV_NAMES) == "year-stripped"


def test_prebuilt_index_gives_the_same_answers():
    index = wr.build_index(CSV_NAMES)
    for name in ("Toni Deutsch '58 Trophy 2027", "Hinman Trophy", "Regatta With No Hint"):
        assert wr.is_womens_regatta(name, CSV_NAMES, index) == wr.is_womens_regatta(name, CSV_NAMES)
        assert wr.womens_reason(name, CSV_NAMES, index) == wr.womens_reason(name, CSV_NAMES)


@pytest.mark.parametrize("name", ["", None])
def test_empty_name_is_not_womens(name):
    assert not wr.is_womens_regatta(name, CSV_NAMES)
    assert wr.womens_reason(name, CSV_NAMES) == ""


@pytest.mark.parametrize("name, expected", [
    ("Toni Deutsch '58 Trophy 2026", "toni deutsch '58 trophy"),
    ("SAISA Womens South #2", "saisa womens south #2"),
    ("Cup 2026 Qualifier", "cup 2026 qualifier"),         # only a trailing year goes
    ("Trophy 1899", "trophy 1899"),                         # not a plausible year
])
def test_strip_year_removes_only_a_trailing_four_digit_year(name, expected):
    assert wr.strip_year(name) == expected


def test_load_women_regatta_names_reads_the_csv_and_tolerates_absence(tmp_path):
    path = tmp_path / "women_regattas.csv"
    path.write_text("regatta_name,score\nAlpha Cup,0.9\n  Beta Bowl  ,0.8\n,0.1\n", encoding="utf-8")
    assert wr.load_women_regatta_names(str(path)) == {"Alpha Cup", "Beta Bowl"}
    assert wr.load_women_regatta_names(str(tmp_path / "missing.csv")) == set()
