"""Empirical-Bayes shrinkage for the ranked findings.

Rank 2,310 noisy things and the top of the list is selected for noise. These
tests pin the properties that make shrinkage worth having: more evidence moves
you further from the prior, less evidence moves you closer, and nothing ever
produces a confident-looking number out of a two-recruit sample.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.shrinkage import (  # noqa: E402
    population_stats,
    residualize,
    shrink_mean,
    shrink_rate,
)


class TestPopulationStats:
    def test_mean_and_sd(self):
        mu, sd = population_stats([1, 2, 3, 4, 5])
        assert mu == pytest.approx(3.0)
        assert sd == pytest.approx(1.5811, abs=1e-3)

    def test_non_numeric_is_ignored(self):
        mu, _ = population_stats([1, None, "x", 3])
        assert mu == pytest.approx(2.0)

    def test_single_value_has_no_spread(self):
        assert population_stats([7])[1] == 0.0

    def test_empty(self):
        assert population_stats([]) == (0.0, 0.0)


class TestShrinkMean:
    def test_more_evidence_moves_further_from_the_prior(self):
        few = shrink_mean(20.0, 1, prior_mean=0.0, prior_sd=5.0, obs_sd=10.0)
        many = shrink_mean(20.0, 50, prior_mean=0.0, prior_sd=5.0, obs_sd=10.0)
        assert many["value"] > few["value"]
        assert many["weight"] > few["weight"]

    def test_shrunk_value_never_overshoots_the_observation(self):
        r = shrink_mean(20.0, 5, 0.0, 5.0, 10.0)
        assert 0.0 <= r["value"] <= 20.0

    def test_noisier_observations_shrink_harder(self):
        quiet = shrink_mean(20.0, 5, 0.0, 5.0, obs_sd=2.0)
        noisy = shrink_mean(20.0, 5, 0.0, 5.0, obs_sd=30.0)
        assert quiet["value"] > noisy["value"]

    def test_interval_brackets_the_value(self):
        r = shrink_mean(20.0, 10, 0.0, 5.0, 10.0)
        assert r["low"] < r["value"] < r["high"]

    def test_more_evidence_narrows_nothing_below_zero_width(self):
        r = shrink_mean(20.0, 500, 0.0, 5.0, 10.0)
        assert r["high"] >= r["low"]

    def test_no_evidence_returns_the_prior_with_no_interval(self):
        r = shrink_mean(20.0, 0, 3.0, 5.0, 10.0)
        assert r["value"] == pytest.approx(3.0)
        assert r["low"] is None and r["high"] is None

    def test_zero_population_spread_returns_the_prior(self):
        """If no team genuinely differs from any other, every observation is noise."""
        r = shrink_mean(20.0, 10, 3.0, prior_sd=0.0, obs_sd=10.0)
        assert r["value"] == pytest.approx(3.0)


class TestShrinkRate:
    def test_small_sample_lands_near_the_prior(self):
        r = shrink_rate(2, 2, prior_rate=0.30, prior_strength=25.0)
        assert 0.30 < r["value"] < 0.45, "two-for-two must not read as 100%"

    def test_large_sample_dominates_the_prior(self):
        r = shrink_rate(80, 100, prior_rate=0.30, prior_strength=25.0)
        assert r["value"] > 0.65

    def test_perfect_record_never_reaches_one(self):
        assert shrink_rate(40, 40, 0.30, 25.0)["value"] < 1.0

    def test_zero_record_never_reaches_zero(self):
        assert shrink_rate(0, 40, 0.30, 25.0)["value"] > 0.0

    def test_interval_stays_inside_zero_one(self):
        r = shrink_rate(39, 40, 0.30, 25.0)
        assert 0.0 <= r["low"] <= r["value"] <= r["high"] <= 1.0

    def test_weight_reports_how_much_is_own_evidence(self):
        assert shrink_rate(5, 25, 0.3, 25.0)["weight"] == pytest.approx(0.5)

    def test_no_trials_is_the_prior_only(self):
        r = shrink_rate(0, 0, 0.30, 25.0)
        assert r["value"] == pytest.approx(0.30)


class TestResidualize:
    def test_recovers_a_known_line(self):
        xs = [1, 2, 3, 4, 5]
        ys = [3, 5, 7, 9, 11]          # y = 2x + 1
        resid, fit = residualize(xs, ys)
        assert fit["slope"] == pytest.approx(2.0)
        assert fit["intercept"] == pytest.approx(1.0)
        assert all(abs(r) < 1e-9 for r in resid)
        assert fit["r2"] == pytest.approx(1.0)

    def test_residuals_sum_to_zero(self):
        resid, _ = residualize([1, 2, 3, 4], [2, 5, 6, 9])
        assert sum(resid) == pytest.approx(0.0, abs=1e-9)

    def test_residual_is_positive_above_the_line(self):
        """The whole point: outrunning your recruiting ranking scores positive."""
        xs = [0.75, 0.80, 0.85, 0.90, 0.95]
        ys = [50, 55, 60, 65, 70]
        resid, _ = residualize(xs + [0.75], ys + [80])
        assert resid[-1] > 0

    def test_too_few_points_returns_a_null_fit(self):
        _, fit = residualize([1], [2])
        assert fit["slope"] == 0.0 and fit["r2"] is None

    def test_non_finite_pairs_are_dropped(self):
        resid, fit = residualize([1, 2, 3, float("nan")], [2, 4, 6, 8])
        assert fit["n"] == 3
        assert len(resid) == 3
