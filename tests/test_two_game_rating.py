"""A two-game player of a season in progress is rated on how he played (v4.27).

Until v4.27 he was on the low-game rule: a recruiting prior plus about a fifth of his stretch, capped at 72. A player with no recruiting grade
started near 40 however well he played. These tests pin the new path and the edges of it: one game stays on the low-game rule, a finished
season is untouched, and a recruiting grade plays no part.
"""

import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

_spec = importlib.util.spec_from_file_location("_s07two", Path(__file__).parent.parent / "scripts" / "07_compute_player_ratings.py")
_s07 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s07)
two = _s07.two_game_rating


def _edge_all_for_level(level, pg="QB", games=2):
    """The stored edge_all (sum / sqrt(games)) of a player whose level through the anchors is `level`."""
    xs = [a[0] for a in _s07.EDGE_OVR_ANCHORS[pg]]
    ys = [float(a[1]) for a in _s07.EDGE_OVR_ANCHORS[pg]]
    return float(np.interp(level, ys, xs)) / math.sqrt(12 / games)


def _row(level=80.0, games=2, team_games=10, stars=None, pg="QB"):
    return {"edge_all": _edge_all_for_level(level, pg, games), "games_played": games, "team_games": team_games, "stars": stars, "stats_measured": 30}


class TestWhoIsOnIt:
    def test_two_games_in_a_season_in_progress(self):
        r = two(_row(), "QB")
        assert r is not None and r[1]["games_played"] == 2

    def test_one_game_stays_on_the_low_game_rule(self):
        assert two(_row(games=1), "QB") is None

    def test_three_games_is_the_part_season_path(self):
        assert two(_row(games=3), "QB") is None

    def test_a_finished_season_is_untouched(self):
        assert two(_row(team_games=0), "QB") is None
        assert two({**_row(), "team_games": None}, "QB") is None

    def test_no_score_is_not_rated_by_this_path(self):
        assert two({**_row(), "edge_all": None}, "QB") is None
        assert two({**_row(), "edge_all": float("nan")}, "QB") is None
        assert two({**_row(), "edge_all": 0.0}, "QB") is None


class TestTheNumber:
    @pytest.mark.parametrize("pg", ["QB", "RB", "WR", "TE", "DL", "EDGE", "LB", "CB", "S", "DB"])
    def test_every_position_has_the_line_it_uses(self, pg):
        assert (_s07.PART_SEASON_FAMILY[pg], str(_s07.TWO_GAME_LINE)) in _s07.PART_SEASON_CURVE

    def test_it_is_his_level_through_the_three_game_line(self):
        a, b = _s07.PART_SEASON_CURVE[("QB", "3")]
        assert two(_row(level=80.0), "QB")[0] == pytest.approx(a + b * 80.0, abs=0.05)

    def test_two_games_and_three_games_at_the_same_level_rate_the_same(self):
        assert two(_row(level=70.0), "QB")[0] == pytest.approx(_s07.part_season_curve(70.0, "QB", 3), abs=0.05)

    def test_a_better_stretch_rates_higher(self):
        assert two(_row(level=85.0), "QB")[0] > two(_row(level=60.0), "QB")[0]

    def test_a_missing_recruiting_grade_costs_nothing(self):
        assert two(_row(stars=None), "QB")[0] == two(_row(stars=5), "QB")[0]


class TestRecord:
    def test_the_basis_is_production_whatever_the_tier(self):
        for tier in ("starter", "role", "reserve", "bench"):
            assert _s07.rating_basis_for("k", tier, {"k": "two_games"}) == "production"

    def test_the_low_game_rule_is_unchanged_for_one_game(self):
        assert _s07.LOW_GAME_MAX == 2 and _s07.LOW_GAME_CEILING == 72.0
