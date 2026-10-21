import numpy as np
import pytest

import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("newcomer_prior", Path("analysis/plackett_luce/newcomer_prior.py"))
npri = importlib.util.module_from_spec(spec); spec.loader.exec_module(npri)


@pytest.mark.unit
def test_school_prior_when_enough_debuts_else_league():
    S = np.array([3.0, 1.0, 1.2, 0.0, 2.0, 0.0])
    seen = np.array([True, True, True, False, True, False])
    first = np.array(["f22", "s24", "f24", "", "f24", ""], dtype=object)
    league = np.array(["ICSA", "ICSA", "ICSA", "", "ISSA", ""], dtype=object)
    keys = np.array(["ICSA|Yale", "ICSA|Yale", "ICSA|Yale", "ICSA|Yale", "ISSA|Tabor", "ICSA|Brown"], dtype=object)
    out = npri.fill(S, seen, first, league, keys, ("s24", "f24"))
    assert out[3] == pytest.approx(1.1)          # Yale: median of its two debuts (1.0, 1.2)
    assert out[4] == 2.0                          # seen: untouched
    # Brown had no debuts: league median of f24 ICSA debuts = 1.2
    assert out[5] == pytest.approx(1.2)


@pytest.mark.unit
def test_previous_seasons():
    assert npri.previous_seasons(["f23", "s24", "f24", "s25"], "s25") == ("s24", "f24")
    assert npri.previous_seasons(["f24"], "s25") == ("f24",)
