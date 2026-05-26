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
