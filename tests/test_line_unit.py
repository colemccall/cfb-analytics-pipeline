"""The line-unit rating — the thing that replaced the withdrawn OL player rating.

The old OL number failed silently for a year because two of its five inputs read
keys that were never written, and nothing asserted otherwise. These tests exist
so that cannot happen twice: a missing input must renormalise, never contribute
a zero, and the rating must actually move when the inputs do.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.line_unit import (  # noqa: E402
    LINE_UNIT_ANCHORS,
    LINE_UNIT_BOUNDS,
    LINE_UNIT_BOUNDS_BY_ERA,
    LINE_UNIT_WEIGHTS,
    bounds_for,
    era_for,
    line_unit_composite,
    line_unit_rating,
)

# Built from the MODERN bucket's own bounds, which is what a caller with no
# season gets. Anything at the ceiling of every input is an elite line for that
# era, by construction.
_M = LINE_UNIT_BOUNDS_BY_ERA["modern"]
ELITE = {k: (hi if k not in ("stuff_rate", "sack_rate_allowed") else lo)
         for k, (lo, hi) in _M.items()}
POOR = {k: (lo if k not in ("stuff_rate", "sack_rate_allowed") else hi)
        for k, (lo, hi) in _M.items()}
TYPICAL = {k: (lo + hi) / 2 for k, (lo, hi) in _M.items()}


class TestScale:
    def test_weights_sum_to_one(self):
        assert sum(LINE_UNIT_WEIGHTS.values()) == pytest.approx(1.0)

    def test_elite_line_tops_the_scale(self):
        rating, _ = line_unit_rating(ELITE)
        assert rating == pytest.approx(95.0)

    def test_poor_line_bottoms_it(self):
        rating, _ = line_unit_rating(POOR)
        assert rating == pytest.approx(30.0)

    def test_typical_line_lands_mid_scale(self):
        rating, _ = line_unit_rating(TYPICAL)
        assert 62 <= rating <= 75

    def test_anchors_are_monotone(self):
        xs = [a[0] for a in LINE_UNIT_ANCHORS]
        ys = [a[1] for a in LINE_UNIT_ANCHORS]
        assert xs == sorted(xs)
        assert ys == sorted(ys)

    def test_ceiling_is_below_the_player_scale(self):
        """95, not 99. Five blockers measured together cannot claim the top."""
        assert LINE_UNIT_ANCHORS[-1][1] == 95


class TestInvertedInputs:
    """Stuff rate and sack rate are bad when high. Getting a sign backwards here
    would rank the worst lines first and look entirely plausible on a page."""

    def test_more_stuffs_is_worse(self):
        good, _ = line_unit_rating({**TYPICAL, "stuff_rate": 0.147})
        bad, _ = line_unit_rating({**TYPICAL, "stuff_rate": 0.240})
        assert good > bad

    def test_more_sacks_allowed_is_worse(self):
        good, _ = line_unit_rating({**TYPICAL, "sack_rate_allowed": 0.034})
        bad, _ = line_unit_rating({**TYPICAL, "sack_rate_allowed": 0.094})
        assert good > bad

    def test_more_line_yards_is_better(self):
        good, _ = line_unit_rating({**TYPICAL, "line_yards": 3.31})
        bad, _ = line_unit_rating({**TYPICAL, "line_yards": 2.54})
        assert good > bad


class TestMissingInputsRenormalise:
    """The failure that killed the old OL rating: absent inputs read as zeros."""

    def test_absent_input_does_not_score_as_worst(self):
        full, _ = line_unit_rating(TYPICAL)
        partial, _ = line_unit_rating({k: v for k, v in TYPICAL.items()
                                       if k != "sack_rate_allowed"})
        # Dropping an average input should barely move an average line. If it
        # were treated as zero the rating would collapse instead.
        assert abs(full - partial) < 5.0

    def test_single_input_still_rates(self):
        rating, parts = line_unit_rating({"line_yards": ELITE["line_yards"]})
        assert rating is not None
        assert set(parts) == {"line_yards", "composite", "era"}

    def test_no_inputs_returns_none_not_a_default(self):
        assert line_unit_rating({}) == (None, {})
        assert line_unit_composite({}) == (None, {})

    def test_nan_is_treated_as_absent(self):
        rating, parts = line_unit_rating({**TYPICAL, "line_yards": float("nan")})
        assert "line_yards" not in parts
        assert rating is not None

    def test_all_none_is_none(self):
        assert line_unit_rating({k: None for k in LINE_UNIT_BOUNDS}) == (None, {})


class TestClipping:
    def test_beyond_the_bounds_clips_rather_than_extrapolates(self):
        absurd, _ = line_unit_rating({**ELITE, "line_yards": 99.0})
        elite, _ = line_unit_rating(ELITE)
        assert absurd == elite == pytest.approx(95.0)


class TestEraCalibration:
    """Pooled bounds made the rating drift 52 -> 77 across the archive.

    The line metrics have two definitional step changes — median line yards jumps
    2.885 to 3.095 between 2020 and 2021, median stuff rate drops 0.199 to 0.165
    — which is a provider changing a formula, not 130 teams learning to block.
    Era bucketing is the fix, and these tests are what stop it being pooled again.
    """

    def test_era_boundaries(self):
        assert era_for(2008) == "classic"
        assert era_for(2013) == "classic"
        assert era_for(2014) == "transition"
        assert era_for(2020) == "transition"
        assert era_for(2021) == "modern"
        assert era_for(2026) == "modern"

    def test_no_season_means_modern(self):
        assert era_for(None) == "modern"
        assert bounds_for(None) == LINE_UNIT_BOUNDS

    def test_every_era_defines_every_input(self):
        keys = set(LINE_UNIT_BOUNDS_BY_ERA["modern"])
        for era, b in LINE_UNIT_BOUNDS_BY_ERA.items():
            assert set(b) == keys, f"{era} is missing inputs"
            for k, (lo, hi) in b.items():
                assert lo < hi, f"{era}.{k} bounds are inverted"

    def test_the_2021_step_change_is_absorbed(self):
        """The same physical line quality either side of the break must rate the
        same. Before era bucketing, these differed by roughly 20 points."""
        late_classic = {"line_yards": 2.885, "stuff_rate": 0.199,
                        "power_success": 0.708, "second_level_yards": 1.122}
        early_modern = {"line_yards": 3.095, "stuff_rate": 0.165,
                        "power_success": 0.725, "second_level_yards": 1.097}
        a, _ = line_unit_rating(late_classic, season=2020)
        b, _ = line_unit_rating(early_modern, season=2021)
        assert abs(a - b) < 8, (
            f"a median line rates {a:.1f} in 2020 and {b:.1f} in 2021 — "
            "the era buckets are not absorbing the definitional change")

    def test_a_median_line_rates_mid_scale_in_every_era(self):
        medians = {
            2010: {"line_yards": 2.856, "stuff_rate": 0.206,
                   "power_success": 0.659, "second_level_yards": 1.032},
            2018: {"line_yards": 2.928, "stuff_rate": 0.193,
                   "power_success": 0.718, "second_level_yards": 1.103},
            2023: {"line_yards": 3.123, "stuff_rate": 0.166,
                   "power_success": 0.742, "second_level_yards": 1.095},
        }
        for season, m in medians.items():
            r, _ = line_unit_rating(m, season=season)
            assert 58 <= r <= 78, f"{season} median line rates {r:.1f}"

    def test_era_is_reported_in_the_contributions(self):
        _, parts = line_unit_rating(TYPICAL, season=2010)
        assert parts["era"] == "classic"
