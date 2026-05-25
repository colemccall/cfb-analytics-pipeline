"""Tests for EDGE → OVR piecewise linear mapping (edge_to_ovr)."""

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

_mod = importlib.import_module("07_compute_player_ratings")
edge_to_ovr      = _mod.edge_to_ovr
EDGE_OVR_ANCHORS = _mod.EDGE_OVR_ANCHORS


def _ovr(pg, score):
    return edge_to_ovr(score, pg)


class TestEdgeToOvr:
    def test_zero_score_returns_low_value(self):
        """Zero edge_score → floor rating (30)."""
        assert _ovr("QB", 0.0) == pytest.approx(30.0, abs=2)

    def test_none_score_returns_midpoint(self):
        """None edge_score → neutral fallback 50.0."""
        assert edge_to_ovr(None, "QB") == pytest.approx(50.0)

    def test_nan_score_returns_midpoint(self):
        assert edge_to_ovr(float("nan"), "RB") == pytest.approx(50.0)

    def test_known_anchor_qb(self):
        """First anchor for QB maps correctly."""
        anchors = EDGE_OVR_ANCHORS["QB"]
        x0, y0 = anchors[0]
        assert _ovr("QB", x0) == pytest.approx(y0, abs=0.5)

    def test_known_anchor_rb(self):
        anchors = EDGE_OVR_ANCHORS["RB"]
        x_mid = anchors[len(anchors) // 2][0]
        y_mid = anchors[len(anchors) // 2][1]
        assert _ovr("RB", x_mid) == pytest.approx(y_mid, abs=0.5)

    def test_score_clipped_to_99(self):
        """Extremely high score never exceeds 99."""
        assert _ovr("QB", 999.0) <= 99.0

    def test_score_clipped_to_30(self):
        assert _ovr("RB", -10.0) >= 30.0

    def test_unknown_position_returns_midpoint(self):
        assert _ovr("ATH", 5.0) == pytest.approx(50.0)

    def test_monotone_increasing_qb(self):
        """Higher edge_score always → higher OVR within a position."""
        scores = [0.5, 1.0, 2.0, 4.0, 8.0]
        ovrs = [_ovr("QB", s) for s in scores]
        assert ovrs == sorted(ovrs)

    def test_monotone_increasing_edge(self):
        scores = [1.0, 5.0, 10.0, 15.0, 20.0]
        ovrs = [_ovr("EDGE", s) for s in scores]
        assert ovrs == sorted(ovrs)
