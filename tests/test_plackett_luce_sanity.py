"""Plackett-Luce sanity check: strengths are recoverable from simulated finishing orders.

The project simulates races with the Gumbel-max trick (analysis/simulate_results.py) and fits ratings with L-BFGS on the
Plackett-Luce likelihood (analysis/plackett_luce/train_pl.py, a script with no importable helper).  This test does both
on a small synthetic fleet, with the likelihood written out here, and checks that the fitted ordering matches the planted
one and that the simulator's finishing frequencies agree with the PL win probability.
"""
import numpy as np
import pytest
from scipy.optimize import minimize
from scipy.stats import spearmanr

pytestmark = pytest.mark.unit

N_SAILORS, N_RACES = 12, 400


def simulate_orders(strength: np.ndarray, n_races: int, seed: int) -> np.ndarray:
    """Finishing orders (best first), one row per race, by perturbing strengths with Gumbel noise."""
    rng = np.random.default_rng(seed)
    noisy = strength[None, :] + rng.gumbel(size=(n_races, len(strength)))
    return np.argsort(-noisy, axis=1)


def nll_and_grad(theta: np.ndarray, orders: np.ndarray, l2: float = 0.01):
    """Negative PL log-likelihood of the orders, with a small L2 term; returns (value, gradient)."""
    n = len(theta)
    value, grad = 0.0, np.zeros(n)
    for order in orders:
        s = theta[order]
        tail = np.logaddexp.accumulate(s[::-1])[::-1]          # log sum of exp over positions i..end
        value -= float(np.sum(s - tail))
        weights = np.exp(s[:, None] - tail[None, :]) * np.tril(np.ones((n, n)))   # chance of m at stage j <= m
        np.add.at(grad, order, -1.0)
        np.add.at(grad, order, weights.sum(axis=1))
    return value + 0.5 * l2 * theta @ theta, grad + l2 * theta


def fit(orders: np.ndarray) -> np.ndarray:
    res = minimize(nll_and_grad, np.zeros(N_SAILORS), args=(orders,), jac=True, method="L-BFGS-B")
    return res.x


@pytest.fixture(scope="module")
def planted() -> np.ndarray:
    return np.linspace(-1.5, 1.5, N_SAILORS)


def test_gradient_matches_finite_differences(planted):
    orders = simulate_orders(planted, 5, seed=1)
    theta = np.random.default_rng(2).normal(size=N_SAILORS) * 0.3
    _, grad = nll_and_grad(theta, orders)
    for i in (0, 5, 11):
        step = np.zeros(N_SAILORS)
        step[i] = 1e-6
        numeric = (nll_and_grad(theta + step, orders)[0] - nll_and_grad(theta - step, orders)[0]) / 2e-6
        assert grad[i] == pytest.approx(numeric, rel=1e-4, abs=1e-6)


def test_fit_recovers_the_planted_strength_order(planted):
    fitted = fit(simulate_orders(planted, N_RACES, seed=7))
    assert spearmanr(fitted, planted).statistic > 0.9
    assert fitted[-1] > fitted[0]


def test_fitted_spread_is_close_to_the_planted_spread(planted):
    fitted = fit(simulate_orders(planted, N_RACES, seed=7))
    assert np.std(fitted) == pytest.approx(np.std(planted), rel=0.25)


def test_gumbel_win_frequency_matches_the_pl_probability():
    strength = np.array([1.0, 0.0, -1.0])
    wins = np.bincount(simulate_orders(strength, 20000, seed=3)[:, 0], minlength=3) / 20000
    expected = np.exp(strength) / np.exp(strength).sum()
    assert wins == pytest.approx(expected, abs=0.02)
