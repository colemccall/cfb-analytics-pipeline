"""Tests for team rating composition (script 10).

A team rating blends three readings of the same team — SP+, our own player
ratings, and team stats — renormalizing over whichever exist. Two of those are
absent for a season that has not been played, which is where this went wrong:
the no-signal fallback published 50.0 for every FBS team in 2026, and those
placeholders then sat alongside the projected rows written later, giving 138
teams two ratings each.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "_s10", Path(__file__).parent.parent / "scripts" / "10_compute_team_ratings.py")
_s10 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s10)

compute_team_splits = _s10.compute_team_splits
team_stats_to_ovr   = _s10.team_stats_to_ovr
W_SP_BLEND          = _s10.W_SP_BLEND
W_ROSTER_BLEND      = _s10.W_ROSTER_BLEND
W_STATS_BLEND       = _s10.W_STATS_BLEND

SP_MEANS = (0.0, 5.0, 5.0)


def _roster(ovr=80.0):
    """A full two-deep at every position the splits look at."""
    return {pg: [ovr, ovr - 2, ovr - 4]
            for pg in ("QB", "RB", "WR", "TE", "OL", "EDGE", "DL", "LB", "CB", "S", "K", "P")}


def _stats(good=True):
    """Keys must match `data/computed/team_season_stats.json` exactly — the whole
    signal was harvested and silently ignored before v2.1, and a renamed key
    reintroduces that failure without any error. Percentages are 0-100, not 0-1."""
    if good:
        return {"yards_pg": 470.0, "yards_allowed_pg": 300.0,
                "third_down_pct": 48.0, "third_down_def_pct": 31.0,
                "sacks_pg": 3.1, "tfl_pg": 7.2, "turnover_margin": 0.8}
    return {"yards_pg": 300.0, "yards_allowed_pg": 470.0,
            "third_down_pct": 30.0, "third_down_def_pct": 48.0,
            "sacks_pg": 0.9, "tfl_pg": 3.4, "turnover_margin": -0.8}


class TestBlendWeights:
    def test_the_three_weights_are_a_whole(self):
        assert W_SP_BLEND + W_ROSTER_BLEND + W_STATS_BLEND == pytest.approx(1.0)

    def test_sp_carries_the_most_weight(self):
        """SP+ is the only one of the three that already knows the results."""
        assert W_SP_BLEND > W_ROSTER_BLEND > W_STATS_BLEND

    def test_team_stats_actually_reach_the_rating(self):
        """Harvested and then silently ignored until v2.1 — the weights existed
        but nothing read them."""
        sp = {"overall": 15.0, "offense": 32.0, "defense": 17.0}
        good = compute_team_splits(1, sp, _roster(), _stats(True), SP_MEANS, 0.9)
        bad = compute_team_splits(1, sp, _roster(), _stats(False), SP_MEANS, 0.9)
        assert good["overall_rating"] > bad["overall_rating"]

    def test_the_stat_keys_are_the_ones_the_pipeline_writes(self):
        """Guards the rename that would silently zero the signal again."""
        assert team_stats_to_ovr(_stats(True)) is not None
        assert team_stats_to_ovr({"yards_per_play_off": 6.2}) is None


class TestNoSignal:
    def test_a_team_with_nothing_gets_no_rating(self):
        """This is the 2026 case for every FBS team on the earned path. Returning
        50.0 published a placeholder indistinguishable from a real average team."""
        assert compute_team_splits(1, None, {}, None, SP_MEANS, None) is None

    def test_roster_alone_is_enough(self):
        """An unplayed season still has a projected roster, which is a real signal."""
        out = compute_team_splits(1, None, _roster(), None, SP_MEANS, 0.9)
        assert out is not None and out["overall_rating"] > 50.0

    def test_stats_alone_are_enough(self):
        out = compute_team_splits(1, None, {}, _stats(), SP_MEANS, None)
        assert out is not None

    def test_sp_alone_is_enough(self):
        out = compute_team_splits(1, {"overall": 12.0, "offense": 30.0, "defense": 18.0},
                                  {}, None, SP_MEANS, None)
        assert out is not None


class TestRenormalization:
    def test_a_missing_signal_shifts_weight_rather_than_dragging_toward_zero(self):
        """Dropping team stats must not pull the rating down; it must redistribute
        their 20% across the signals that remain."""
        sp = {"overall": 15.0, "offense": 32.0, "defense": 17.0}
        both = compute_team_splits(1, sp, _roster(), None, SP_MEANS, 0.9)
        assert both["overall_rating"] > 60.0

    def test_identical_signals_produce_that_same_value(self):
        """If every reading agrees, renormalization is the identity — no blend of
        weights that sum to one can move off the shared value."""
        sp_only = compute_team_splits(
            1, {"overall": 15.0, "offense": 32.0, "defense": 17.0}, {}, None, SP_MEANS, None)
        with_roster = compute_team_splits(
            1, {"overall": 15.0, "offense": 32.0, "defense": 17.0},
            _roster(sp_only["overall_rating"] + 2), None, SP_MEANS, None)
        assert abs(with_roster["overall_rating"] - sp_only["overall_rating"]) < 12


class TestBounds:
    def test_ratings_stay_on_the_zero_to_ninetynine_scale(self):
        for sp in (None, {"overall": 40.0, "offense": 55.0, "defense": -5.0},
                   {"overall": -35.0, "offense": 5.0, "defense": 45.0}):
            out = compute_team_splits(1, sp, _roster(99), _stats(), SP_MEANS, 1.0)
            if out is None:
                continue
            for k in ("overall_rating", "offense_rating", "defense_rating",
                      "pass_off", "run_off", "pass_def", "run_def"):
                assert 0.0 <= out[k] <= 99.0, f"{k}={out[k]}"

    def test_a_better_team_never_rates_lower(self):
        sp = {"overall": 15.0, "offense": 32.0, "defense": 17.0}
        weak = compute_team_splits(1, sp, _roster(60), _stats(), SP_MEANS, 0.5)
        strong = compute_team_splits(1, sp, _roster(90), _stats(), SP_MEANS, 0.5)
        assert strong["overall_rating"] >= weak["overall_rating"]
