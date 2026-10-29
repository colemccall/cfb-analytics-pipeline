"""Tests for the slot team rating (script 10) and the in-season pace correction (script 06)."""

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


t10 = _load("t10", "10_compute_team_ratings.py")
t06 = _load("t06", "06_compute_edge_scores.py")


class TestSlotScore:
    ALL = ("QB", "RB", "WR", "TE", "DL", "EDGE", "LB", "CB", "S", "DB", "K", "P")

    def test_weights_sum_to_one(self):
        assert sum(t10.SLOT_WEIGHTS.values()) == pytest.approx(1.0, abs=0.01)

    def test_every_group_carries_weight(self):
        assert all(w > 0 for w in t10.SLOT_WEIGHTS.values())

    def test_the_layout_is_the_owners(self):
        assert t10.SLOT_STARTERS == {"QB": 1, "RB": 2, "WR": 5, "TE": 1, "DL": 4, "LB": 4, "DB": 6}
        assert t10.SLOT_DEPTH == {"QB": 1, "RB": 2, "WR": 3, "TE": 3, "DL": 3, "LB": 3, "DB": 4}
        assert 0.8 <= t10.STARTER_SHARE <= 0.9

    def test_kickers_and_punters_are_light(self):
        assert t10.SLOT_WEIGHTS["K"] + t10.SLOT_WEIGHTS["P"] <= 0.05

    def test_a_uniform_roster_scores_its_rating(self):
        by_pos = {p: [70.0] * 12 for p in self.ALL}
        assert t10.slot_score(by_pos, 70.0) == pytest.approx(70.0)

    def test_depth_counts_but_less_than_starters(self):
        base = {"QB": [80.0], "DL": [80.0] * 4}
        deep = {"QB": [80.0, 60.0], "DL": [80.0] * 4 + [60.0] * 3}
        assert t10.slot_score(deep) < t10.slot_score(base)
        # the starters carry STARTER_SHARE: a weaker depth moves a group by (1 - share) x 20
        assert t10.slot_score({"QB": [80.0, 60.0]}) == pytest.approx(80.0 - (1 - t10.STARTER_SHARE) * 20.0)

    def test_the_depth_window_ends(self):
        window = t10.SLOT_DEPTH["WR"]
        a = t10.slot_score({"WR": [80.0] * 5 + [60.0] * window})
        b = t10.slot_score({"WR": [80.0] * 5 + [60.0] * window + [10.0] * 4})
        assert a == pytest.approx(b)

    def test_a_short_list_averages_who_is_there(self):
        assert t10.slot_score({"WR": [80.0, 70.0]}) == pytest.approx(75.0)

    def test_a_missing_group_renormalises_instead_of_defaulting(self):
        assert t10.slot_score({"QB": [90.0]}) == pytest.approx(90.0)

    def test_no_players_is_none(self):
        assert t10.slot_score({}) is None
        assert t10.slot_score({}, None) is None

    def test_cb_s_and_db_pool_and_edge_joins_dl(self):
        a = t10.slot_score({"CB": [80.0] * 3, "S": [80.0] * 3})
        b = t10.slot_score({"DB": [80.0] * 6})
        assert a == pytest.approx(b)
        assert t10.slot_score({"EDGE": [75.0] * 2, "DL": [75.0] * 2}) == pytest.approx(75.0)

    def test_a_pooled_group_takes_the_best_in_any_mix(self):
        mixed = t10.slot_score({"DL": [60.0, 60.0], "EDGE": [90.0, 90.0, 90.0, 90.0]})
        assert mixed == pytest.approx(90.0 * t10.STARTER_SHARE + 60.0 * (1 - t10.STARTER_SHARE))

    def test_the_line_unit_is_its_own_group(self):
        assert "OL" in t10.SLOT_WEIGHTS
        assert t10.group_scores({"QB": [70.0]}, 85.0)["OL"] == 85.0
        assert "OL" not in t10.group_scores({"QB": [70.0]}, None)
        assert t10.slot_score({"QB": [70.0]}, 90.0) > t10.slot_score({"QB": [70.0]}, 50.0)

    def test_a_player_rated_ol_is_not_read(self):
        assert t10.slot_score({"OL": [99.0], "QB": [70.0]}) == pytest.approx(70.0)

    def test_k_and_p_are_one_player_each(self):
        g = t10.group_scores({"K": [60.0, 90.0], "P": [70.0]})
        assert g["K"] == 90.0 and g["P"] == 70.0


class TestPaceFactor:
    def test_a_regular_full_share_player_is_the_sqrt_r_rule(self):
        out = t06.pace_factor_for(10.0, 2.0, 8, 8)
        assert out == pytest.approx(10.0 * 2 ** 0.5 * t06.PACE_SHARE_K[-1])

    def test_a_player_who_played_few_of_his_teams_games_is_scaled_down_more(self):
        full = t06.pace_factor_for(10.0, 2.0, 8, 8) / (10.0 * 2 ** 0.5)
        part = t06.pace_factor_for(10.0, 2.0, 3, 8) / (10.0 * 2 ** 0.5)
        assert part < full

    def test_it_is_a_scale_not_a_shrink(self):
        # the result is proportional to the part-season value at a given share: nothing is pulled toward a mean
        a = t06.pace_factor_for(40.0, 2.0, 3, 8)
        b = t06.pace_factor_for(20.0, 2.0, 3, 8)
        assert a == pytest.approx(2 * b)
        assert t06.pace_factor_for(0.0, 2.0, 3, 8) == 0.0

    def test_the_games_damp_is_not_applied_twice(self):
        assert not hasattr(t06, "PACE_SHRINK_GAMES") and not hasattr(t06, "prior_position_means")

    def test_constants_are_ordered(self):
        assert list(t06.PACE_SHARE_EDGES) == sorted(t06.PACE_SHARE_EDGES)
        assert list(t06.PACE_SHARE_K) == sorted(t06.PACE_SHARE_K)
        assert len(t06.PACE_SHARE_K) == len(t06.PACE_SHARE_EDGES) + 1
