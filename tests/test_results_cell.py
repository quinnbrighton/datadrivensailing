"""The Rating column on sailor pages: skippers get a performance rating, crews get their rating push."""
import performance_site as ps


def test_skipper_gets_performance_rating_per_division():
    idx = {'/f26/x': [('A', 1.234), ('B', 2.0)]}
    out = ps.results_cell('ICSA', '/f26/x', 'Skipper', 'A B', 'S', idx)
    assert 'A 1.23' in out and 'B 2.00' in out


def test_crew_gets_push_with_sign(monkeypatch):
    monkeypatch.setattr(ps, '_pushes', lambda: {'ICSA|a b|s': {'/f26/x': -0.31}})
    out = ps.results_cell('ICSA', '/f26/x', 'Crew', 'A B', 'S', {})
    assert '-0.31' in out


def test_missing_data_is_a_dash():
    assert ps.results_cell('ICSA', '/f26/none', 'Skipper', 'A B', 'S', {}) == '&mdash;'
