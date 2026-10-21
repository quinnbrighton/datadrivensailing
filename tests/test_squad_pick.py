"""Predicted-squad selection: who helms and crews each boat, and what rating the boat gets.

Protects `disciplines.squad_boats` (driver / crew choice boat by boat, pool and exclusion filters, probability
renormalisation) and `disciplines.squad_boat_rating` (Monte Carlo rating with the multi-boat ladder offset, else the
ladder entry by driver position minus boat rank).
"""
import pytest

import disciplines as D

pytestmark = pytest.mark.unit


def drivers(*pairs):
    return [{"name": n, "p": p} for n, p in pairs]


@pytest.fixture
def school() -> dict:
    return {
        "drivers": drivers(("Ann", 0.5), ("Bo", 0.3), ("Cy", 0.15), ("Di", 0.05)),
        "crews": {"Ann": drivers(("Eve", 0.6), ("Bo", 0.4))},
    }


def test_best_ranked_available_driver_is_boat_zero(school):
    boats = D.squad_boats(school, None, 0, 1)
    assert [b["driver"][0] for b in boats] == ["Ann"]
    assert boats[0]["driver"][1] == pytest.approx(0.5)


def test_pool_filters_drivers(school):
    boats = D.squad_boats(school, {"Bo", "Cy"}, 1, 1)
    assert [b["driver"][0] for b in boats] == ["Bo", "Cy"]


def test_exclude_applies_when_pool_is_none(school):
    boats = D.squad_boats(school, None, 1, 1, exclude={"Ann"})
    assert [b["driver"][0] for b in boats] == ["Bo", "Cy"]


def test_a_driver_of_boat_zero_is_not_reused_by_boat_one(school):
    names = [b["driver"][0] for b in D.squad_boats(school, None, 3, 1)]
    assert names == ["Ann", "Bo", "Cy", "Di"]


def test_probabilities_renormalise_over_remaining_pool(school):
    boats = D.squad_boats(school, {"Bo", "Cy"}, 0, 1)
    driver, prob, others = boats[0]["driver"]
    assert prob == pytest.approx(0.3 / 0.45)
    assert others == [("Cy", pytest.approx(0.15 / 0.45))]


def test_boat_two_probabilities_renormalise_after_boat_zero_is_taken(school):
    second = D.squad_boats(school, None, 1, 1)[1]["driver"]
    assert second[0] == "Bo"
    assert second[1] == pytest.approx(0.3 / 0.5)


def test_singlehanded_boats_have_no_crew(school):
    assert all(b["crew"] == [] for b in D.squad_boats(school, None, 2, 1))


def test_keelboat_crew_come_from_the_drivers_own_ranking_first(school):
    boat = D.squad_boats(school, None, 0, 3)[0]
    assert [c[0] for c in boat["crew"]] == ["Eve", "Bo"]
    assert boat["crew"][0][1] == pytest.approx(0.6, rel=0.01)   # topped-up sailors carry 1% weight


def test_keelboat_crew_are_topped_up_from_the_driver_ranking(school):
    crew = [c[0] for c in D.squad_boats(school, None, 0, 5)[0]["crew"]]
    assert crew == ["Eve", "Bo", "Cy", "Di"]      # Ann herself is aboard as the driver, never as crew


def test_keelboat_boat_one_crew_exclude_boat_zero_people(school):
    boats = D.squad_boats(school, None, 1, 3)
    aboard_zero = {boats[0]["driver"][0]} | {c[0] for c in boats[0]["crew"]}
    aboard_one = {boats[1]["driver"][0]} | {c[0] for c in boats[1]["crew"]}
    assert not aboard_zero & aboard_one


def test_crew_pool_filter_applies(school):
    crew = [c[0] for c in D.squad_boats(school, {"Ann", "Cy"}, 0, 3)[0]["crew"]]
    assert crew == ["Cy"]


@pytest.mark.parametrize("sch", [None, {}, {"drivers": []}])
def test_empty_school_returns_none(sch):
    assert D.squad_boats(sch, None, 0, 1) is None


def test_stops_when_the_pool_runs_out(school):
    assert len(D.squad_boats(school, {"Ann", "Bo"}, 5, 1)) == 2


def test_no_one_in_pool_returns_none(school):
    assert D.squad_boats(school, set(), 0, 1) is None


# ---- squad_boat_rating -------------------------------------------------------------------------------------------

LADDER = [3.0, 2.0, 1.0]


def test_rating_prefers_the_monte_carlo_value():
    sch = {"mc_rating": 2.2, "ratings": LADDER, "drivers": drivers(("Ann", 1.0))}
    assert D.squad_boat_rating(sch, "Ann") == 2.2


def test_single_boat_monte_carlo_ignores_the_ladder():
    sch = {"mc_rating": 2.2, "boats": 1, "ratings": LADDER, "drivers": drivers(("Ann", 1.0))}
    assert D.squad_boat_rating(sch, "Ann", 1) == 2.2


@pytest.mark.parametrize("boat_rank, expected", [(0, 2.2 + 1.0), (1, 2.2), (2, 2.2 - 1.0)])
def test_multi_boat_monte_carlo_keeps_ladder_differences_around_the_mean(boat_rank, expected):
    sch = {"mc_rating": 2.2, "boats": 3, "ratings": LADDER, "drivers": drivers(("Ann", 1.0))}
    assert D.squad_boat_rating(sch, "Ann", boat_rank) == pytest.approx(expected)


def test_multi_boat_offset_is_clamped_to_the_ladder_length():
    sch = {"mc_rating": 2.0, "boats": 2, "ratings": LADDER, "drivers": drivers(("Ann", 1.0))}
    assert D.squad_boat_rating(sch, "Ann", 9) == pytest.approx(2.0 + 1.0 - 2.5)


def test_ladder_entry_is_by_driver_position_minus_boat_rank():
    sch = {"ratings": LADDER, "drivers": drivers(("Ann", .5), ("Bo", .3), ("Cy", .2))}
    assert D.squad_boat_rating(sch, "Ann") == 3.0
    assert D.squad_boat_rating(sch, "Bo") == 2.0
    assert D.squad_boat_rating(sch, "Cy", 1) == 2.0      # position 2 minus rank 1
    assert D.squad_boat_rating(sch, "Ann", 1) == 3.0     # position cannot go below zero


def test_unlisted_driver_gets_the_last_ladder_entry():
    sch = {"ratings": LADDER, "drivers": drivers(("Ann", 1.0))}
    assert D.squad_boat_rating(sch, "Stranger") == 1.0


@pytest.mark.parametrize("sch", [None, {}, {"ratings": []}, {"drivers": drivers(("Ann", 1.0))}])
def test_rating_is_none_without_data(sch):
    assert D.squad_boat_rating(sch, "Ann") is None
