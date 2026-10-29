"""Players under the 3-game line are rated from their stretch, blended with their recruiting prior and capped (v4.19).

A rating needs three productive games, so a player with one or two was rated from his recruiting grade alone and a good stretch
counted for nothing. These tests pin the rule: a short stretch moves a player a little and never past the ceiling, more games
and more events earn more weight, an average stretch changes nothing, and a player with no score is not rated by this path.
"""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "_s07low", Path(__file__).parent.parent / "scripts" / "07_compute_player_ratings.py")
_s07 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s07)

low = _s07.low_game_rating
MEAN = 40.0                               # an arbitrary R_edge level for low-game players at the position


def _edge_for_r(r_edge, pg="WR"):
    """An edge_all value whose R_edge is `r_edge` (inverting the position's anchors)."""
    xs = [a[0] for a in _s07.EDGE_OVR_ANCHORS[pg]]
    ys = [float(a[1]) for a in _s07.EDGE_OVR_ANCHORS[pg]]
    import numpy as np
    return float(np.interp(r_edge, ys, xs))


class TestEligibility:
    def test_no_score_is_not_rated_by_this_path(self):
        assert low(None, 2, 5, "WR", 50.0, MEAN) is None
        assert low(float("nan"), 2, 5, "WR", 50.0, MEAN) is None
        assert low(0.0, 2, 5, "WR", 50.0, MEAN) is None

    def test_three_games_or_more_is_the_normal_path(self):
        assert low(2.0, 3, 5, "WR", 50.0, MEAN) is None

    def test_zero_games_is_not_rated(self):
        assert low(2.0, 0, 5, "WR", 50.0, MEAN) is None


class TestMovement:
    def test_an_average_stretch_leaves_the_prior_alone(self):
        e = _edge_for_r(MEAN)
        assert low(e, 2, 6, "WR", 52.0, MEAN)[0] == pytest.approx(52.0, abs=0.05)

    def test_a_better_stretch_rates_higher_and_a_worse_one_lower(self):
        hi = low(_edge_for_r(MEAN + 20), 2, 6, "WR", 50.0, MEAN)[0]
        lo = low(_edge_for_r(MEAN - 3), 2, 6, "WR", 50.0, MEAN)[0]
        assert lo < 50.0 < hi

    def test_a_twenty_point_stretch_moves_him_a_few_points_not_twenty(self):
        r = low(_edge_for_r(MEAN + 20), 2, 6, "WR", 50.0, MEAN)[0]
        assert 2.5 < r - 50.0 < 4.5

    def test_more_games_and_more_events_earn_more_weight(self):
        w = _s07.LOW_GAME_WEIGHT
        assert w[(1, False)] < w[(1, True)] <= w[(2, True)]
        assert w[(1, False)] < w[(2, False)] < w[(2, True)]

    def test_busy_is_decided_by_counted_events(self):
        e = _edge_for_r(MEAN + 20)
        quiet = low(e, 2, _s07.LOW_GAME_BUSY_EVENTS - 1, "WR", 50.0, MEAN)
        busy = low(e, 2, _s07.LOW_GAME_BUSY_EVENTS, "WR", 50.0, MEAN)
        assert quiet[1]["low_game_weight"] < busy[1]["low_game_weight"] and quiet[0] < busy[0]


class TestCeilingAndFloor:
    def test_nobody_is_rated_above_the_ceiling_on_one_or_two_games(self):
        r = low(_edge_for_r(99.0), 2, 30, "WR", 70.0, MEAN)[0]
        assert r == _s07.LOW_GAME_CEILING

    def test_nobody_is_rated_below_the_floor(self):
        assert low(0.0001, 1, 1, "WR", 31.0, 99.0)[0] >= 30.0

    def test_the_ceiling_is_below_a_star(self):
        assert _s07.LOW_GAME_CEILING < 78


class TestRecord:
    def test_the_contribution_record_names_the_games_and_weight(self):
        _, c = low(_edge_for_r(MEAN + 5), 1, 2, "WR", 50.0, MEAN)
        assert c["games_played"] == 1 and c["low_game_weight"] == _s07.LOW_GAME_WEIGHT[(1, False)] and "edge_all" in c

    def test_the_basis_is_blended_even_for_a_bench_tier_player(self):
        assert _s07.rating_basis_for("k", "bench", {"k": "low_games"}) == "blended"
        assert _s07.rating_basis_for("k", "bench", {"k": "recruiting"}) == "recruiting"
