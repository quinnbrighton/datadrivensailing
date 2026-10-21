"""Freshman-only regattas (Nickerson Trophy): only first-year sailors are candidates."""
import freshman_rules as fr


def test_nickerson_is_freshman_only():
    assert fr.is_freshman_only("Nickerson Trophy")
    assert fr.is_freshman_only("nickerson trophy 2027")
    assert not fr.is_freshman_only("Oberg Trophy")
    assert not fr.is_freshman_only("")
    assert not fr.is_freshman_only(None)


def test_first_year_class_by_season():
    # fall 2026 freshmen graduate in 2030; spring 2027 freshmen also 2030
    assert fr.freshman_class_year("f26") == 2030
    assert fr.freshman_class_year("s27") == 2030
    assert fr.freshman_class_year("f25") == 2029
    assert fr.freshman_class_year("junk") is None


def test_pool_keeps_only_first_years_no_fallback():
    gy = {"A": 2030, "B": 2029, "C": 2026, "D": None}
    pool = {"A", "B", "C", "D", "E"}      # E has no class year on file
    assert fr.freshman_pool(pool, gy, "f26", "Nickerson Trophy") == {"A"}


def test_pool_untouched_at_other_regattas():
    gy = {"A": 2030, "B": 2029}
    assert fr.freshman_pool({"A", "B"}, gy, "f26", "Oberg Trophy") == {"A", "B"}


def test_school_with_no_freshmen_is_empty():
    gy = {"B": 2029}
    assert fr.freshman_pool({"B"}, gy, "f26", "Nickerson Trophy") == set()
