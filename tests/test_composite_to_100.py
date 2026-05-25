"""Tests for _composite_to_100 — 247Sports composite scale normalization."""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

_mod = importlib.import_module("07_compute_player_ratings")
_composite_to_100 = _mod._composite_to_100


class TestCompositeTo100:
    def test_none_returns_floor(self):
        """No composite data → 40.0 (unrated fallback)."""
        assert _composite_to_100(None) == pytest.approx(40.0)

    def test_zero_returns_floor(self):
        assert _composite_to_100(0) == pytest.approx(40.0)

    def test_elite_five_star(self):
        """0.9900+ → near 100."""
        result = _composite_to_100(0.99)
        assert result >= 96.0

    def test_average_three_star(self):
        """~0.82 → ~40 (p50 of recruiting pool)."""
        result = _composite_to_100(0.82)
        assert 30.0 <= result <= 60.0

    def test_floor_at_zero(self):
        """Values below 0.70 (247Sports min) clip to 0.0."""
        assert _composite_to_100(0.50) == pytest.approx(0.0)

    def test_ceiling_at_100(self):
        assert _composite_to_100(1.50) == pytest.approx(100.0)

    def test_boundary_0_7(self):
        """Exactly 0.70 → 0.0."""
        assert _composite_to_100(0.70) == pytest.approx(0.0, abs=0.01)

    def test_boundary_1_0(self):
        """Exactly 1.00 → 100.0."""
        assert _composite_to_100(1.00) == pytest.approx(100.0, abs=0.01)

    def test_monotone_increasing(self):
        """Higher composite always yields higher normalized score."""
        scores = [0.75, 0.80, 0.85, 0.90, 0.95, 1.00]
        normed = [_composite_to_100(s) for s in scores]
        assert normed == sorted(normed)
