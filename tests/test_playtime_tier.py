"""Tests for classify_playtime_tier — four-tier playing-time classification."""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

_mod = importlib.import_module("07_compute_player_ratings")
classify_playtime_tier = _mod.classify_playtime_tier


class TestClassifyPlaytimeTier:
    def test_qb_starter(self):
        stats = {"passingATT": 250}
        assert classify_playtime_tier("QB", stats) == "starter"

    def test_qb_role(self):
        stats = {"passingATT": 50}
        assert classify_playtime_tier("QB", stats) == "role"

    def test_qb_reserve(self):
        stats = {"passingATT": 10}
        assert classify_playtime_tier("QB", stats) == "reserve"

    def test_qb_bench(self):
        stats = {"passingATT": 2}
        assert classify_playtime_tier("QB", stats) == "bench"

    def test_rb_starter_by_carries(self):
        stats = {"rushingCAR": 80}
        assert classify_playtime_tier("RB", stats) == "starter"

    def test_ol_always_starter(self):
        """OL has no individual stats — always returns starter."""
        assert classify_playtime_tier("OL", {}) == "starter"

    def test_wr_starter(self):
        stats = {"receivingREC": 30}
        assert classify_playtime_tier("WR", stats) == "starter"

    def test_wr_bench_empty_stats(self):
        assert classify_playtime_tier("WR", {}) == "bench"

    def test_edge_starter_by_tackles(self):
        stats = {"defensiveTOT": 15}
        assert classify_playtime_tier("EDGE", stats) == "starter"

    def test_volume_score_fallback(self):
        """volume_score alias used when canonical stat key absent."""
        stats = {"volume_score": 200}
        assert classify_playtime_tier("QB", stats) == "starter"

    def test_invalid_stat_value(self):
        """Non-numeric stat value → bench (safe default)."""
        stats = {"passingATT": "invalid"}
        assert classify_playtime_tier("QB", stats) == "bench"


class TestEveryRatedPositionIsTiered:
    """A position missing from PLAYTIME_TIERS is silently rated as an all-starter.

    DB was missing from v2 until v4.5. `cfg` came back None, every one of 23,353 DB
    player-seasons classified as "starter", and each was rated on the full
    production formula with no recruiting anchor however little he played. The
    2025 tier split was 919 starters and nothing else, against CB's 245/70/123.
    Fixing it moved 7,138 DB ratings and raised agreement with EA CFB 27 from
    0.6132 to 0.6497 — the largest single-position gain in either recent pass, and
    it came from a lookup table, not from tuning.

    This test is the thing that stops it happening to the next position group.
    """

    def test_every_rated_position_has_tiers(self):
        rated = set(_mod.WEIGHTS)
        tiered = set(_mod.PLAYTIME_TIERS)
        missing = sorted(rated - tiered)
        assert not missing, (
            f"{missing} can be rated but has no PLAYTIME_TIERS entry, so every player "
            "at that position classifies as a starter and is rated on the full formula.")

    def test_db_tiers_sit_between_cb_and_s(self):
        """DB is the API's own generic label for both, so its thresholds are too."""
        cb, s, db = (_mod.PLAYTIME_TIERS[p] for p in ("CB", "S", "DB"))
        for key in ("starter", "role"):
            assert cb[key] <= db[key] <= s[key], f"DB {key} threshold is outside [CB, S]"


class TestZeroIsNotOne:
    """A defender with no tackles must reach the bench tier.

    Defensive features divide by tackles, so `volume_score` is max(TOT, 1). The
    tier lookup read that floored value, and the reserve threshold at CB, DL and
    EDGE is exactly 1 — so a zero-tackle defender presented as a one-tackle
    defender and the bench tier was unreachable for those positions: 0 bench rows
    at all three in 2025. `tier_volume` carries the unfloored count.
    """

    @pytest.mark.parametrize("pg", ["CB", "DL", "EDGE"])
    def test_zero_tackles_is_bench(self, pg):
        assert classify_playtime_tier(pg, {"tier_volume": 0, "volume_score": 1}) == "bench"

    @pytest.mark.parametrize("pg", ["CB", "DL", "EDGE"])
    def test_one_real_tackle_is_reserve(self, pg):
        assert classify_playtime_tier(pg, {"tier_volume": 1, "volume_score": 1}) == "reserve"

    def test_a_genuine_zero_does_not_fall_through_to_the_floor(self):
        """`or` chaining is what made a real 0 read as the floored 1."""
        assert _mod._tier_value({"defensiveTOT": 0, "volume_score": 1},
                                _mod.PLAYTIME_TIERS["CB"]) == 0.0


class TestStatFallbackIsAbsolute:
    """AUDIT_FINDINGS §9: no rating path may scale against the pool it sits in.

    The no-EDGE fallback mapped through np.percentile of whoever happened to be in
    the pool, so its bottom always became 30 and its top always 78 — the one path
    where pool-relative scaling survived after being removed everywhere else.
    """

    def test_same_input_gives_same_output_regardless_of_pool(self):
        assert _mod.stat_fallback_to_ovr(0.30, "LB") == _mod.stat_fallback_to_ovr(0.30, "LB")

    def test_a_weak_crop_maps_low_instead_of_being_restretched(self):
        """Every player at half the median composite must stay well below the cap."""
        for pg in ("QB", "LB", "CB"):
            mid = _mod.STAT_FALLBACK_ANCHORS[pg][2][0]      # the p50 anchor
            assert _mod.stat_fallback_to_ovr(mid * 0.5, pg) < 55.0

    def test_the_cap_holds(self):
        for pg in _mod.STAT_FALLBACK_ANCHORS:
            assert _mod.stat_fallback_to_ovr(99.0, pg) <= 78.0
            assert _mod.stat_fallback_to_ovr(-1.0, pg) >= 30.0

    def test_anchors_are_monotone(self):
        for pg, anchors in _mod.STAT_FALLBACK_ANCHORS.items():
            xs = [a[0] for a in anchors]
            ys = [a[1] for a in anchors]
            assert xs == sorted(xs), f"{pg} anchor x-values are not increasing"
            assert ys == sorted(ys), f"{pg} anchor y-values are not increasing"
