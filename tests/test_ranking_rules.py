"""Only teams with a predicted A and B skipper are ranked at multi-division fleet regattas."""
import ranking_rules as rr


def boat(name, r=3.0):
    return {"name": name, "rating": r}


def school(name, n):
    return {"school": name, "lineup": [boat(f"{name}{i}") for i in range(n)] or None}


def test_two_division_event_needs_a_and_b():
    s = [school("Full", 2), school("OnlyA", 1), school("None", 0)]
    assert [x["school"] for x in rr.rankable_schools(s, "fleet", divisions=2)] == ["Full"]


def test_three_division_event_needs_only_a_and_b():
    s = [school("ThreeBoats", 3), school("TwoBoats", 2), school("One", 1)]
    assert [x["school"] for x in rr.rankable_schools(s, "fleet", divisions=3)] == ["ThreeBoats", "TwoBoats"]


def test_single_division_event_is_untouched():
    s = [school("A", 1), school("B", 1)]
    assert rr.rankable_schools(s, "fleet", divisions=1) == s


def test_non_fleet_kinds_are_untouched():
    s = [school("A", 1)]
    assert rr.rankable_schools(s, "singlehanded", divisions=2) == s
    assert rr.rankable_schools(s, "keelboat", divisions=2) == s


def test_blank_names_do_not_count():
    s = [{"school": "Blank", "lineup": [boat("X"), {"name": "", "rating": 1.0}]}]
    assert rr.rankable_schools(s, "fleet", divisions=2) == []


def test_divisions_inferred_when_missing():
    s = [school("Full", 2), school("Short", 1)]
    assert [x["school"] for x in rr.rankable_schools(s, "fleet", divisions=None)] == ["Full"]
    solo = [school("A", 1), school("B", 1)]
    assert rr.rankable_schools(solo, "fleet", divisions=None) == solo
