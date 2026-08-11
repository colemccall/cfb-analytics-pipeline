"""Tests for composite_to_ovr — fixed OL/K/P composite → OVR anchors.

These lock in the fix for the forced-distribution bug documented in
docs/AUDIT_FINDINGS.md §9: OL/K/P used to map through percentiles OF THE POOL,
so the best composite in the pool always became a 99 no matter its absolute
value. That put G5 offensive linemen above Derrick Henry.
"""

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

_mod = importlib.import_module("07_compute_player_ratings")
composite_to_ovr = _mod.composite_to_ovr
ANCHORS = _mod.COMPOSITE_OVR_ANCHORS


class TestAbsoluteNotRelative:
    def test_pool_max_is_not_forced_to_99(self):
        """The whole point: topping a weak pool must not yield a 99."""
        weak_pool = np.array([0.30, 0.32, 0.35, 0.38, 0.40])
        out = composite_to_ovr(weak_pool, "OL")
        assert out.max() < 70, f"top of a weak pool rated {out.max()}"

    def test_same_composite_same_ovr_across_pools(self):
        """A 0.40 composite rates the same whether the pool is strong or weak."""
        weak = composite_to_ovr(np.array([0.40, 0.30, 0.31, 0.32, 0.33]), "OL")[0]
        strong = composite_to_ovr(np.array([0.40, 0.64, 0.65, 0.65, 0.65]), "OL")[0]
        assert weak == strong

    def test_saturated_pool_does_not_split_ties(self):
        """The OL composite saturates; identical inputs must give identical OVR.

        The old percentile mapping produced non-increasing interp anchors when
        p90 == p99 == max, sending tied players to different ratings.
        """
        tied = np.array([0.65, 0.65, 0.65, 0.65, 0.65])
        out = composite_to_ovr(tied, "OL")
        assert len(set(out.tolist())) == 1


class TestPositionCeilings:
    def test_ol_caps_at_88(self):
        """OL inputs are team proxies — 99 would overclaim precision."""
        out = composite_to_ovr(np.array([1.0, 0.65]), "OL")
        assert out.max() == pytest.approx(88.0)

    @pytest.mark.parametrize("pg", ["K", "P"])
    def test_specialists_stay_in_a_narrow_band(self, pg):
        """A perfect specialist season tops out near 90, not in the high 90s.

        These positions used to reach 96-97, which put 38 punters at 85+ and 9 at
        90+ in a single season — a punter outranked the receivers on his own team
        page. Their impact range is genuinely narrower than a skill player's, so
        their rating band is too.
        """
        assert composite_to_ovr(np.array([1.0]), pg)[0] == pytest.approx(90.0)

    @pytest.mark.parametrize("pg", ["K", "P"])
    def test_an_average_specialist_is_an_average_player(self, pg):
        """Mid-range production must not read as a star. Was the whole problem."""
        mid = composite_to_ovr(np.array([0.55 if pg == "K" else 0.60]), pg)[0]
        assert 58 <= mid <= 68, f"{pg} mid-range composite rated {mid}"

    def test_nobody_lands_below_the_floor(self):
        for pg in ANCHORS:
            assert composite_to_ovr(np.array([0.0, -1.0]), pg).min() >= 30.0


class TestMonotonicity:
    @pytest.mark.parametrize("pg", ["OL", "K", "P"])
    def test_better_composite_never_rates_lower(self, pg):
        xs = np.linspace(0.0, 1.0, 60)
        out = composite_to_ovr(xs, pg)
        assert np.all(np.diff(out) >= 0), f"{pg} mapping is not monotonic"

    @pytest.mark.parametrize("pg", ["OL", "K", "P"])
    def test_anchors_are_strictly_increasing(self, pg):
        """np.interp needs increasing x — duplicates caused the original bug."""
        xs = [a[0] for a in ANCHORS[pg]]
        assert all(b > a for a, b in zip(xs, xs[1:])), f"{pg} anchor x-values not increasing"


class TestUnknownPosition:
    def test_unknown_group_returns_neutral(self):
        out = composite_to_ovr(np.array([0.5, 0.9]), "QB")
        assert np.all(out == 65.0)
