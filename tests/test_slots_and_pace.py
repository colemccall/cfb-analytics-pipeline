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
    def test_weights_sum_to_one(self):
        assert sum(t10.SLOT_WEIGHTS.values()) == pytest.approx(1.0, abs=0.01)

    def test_every_group_carries_weight(self):
        assert all(w > 0 for w in t10.SLOT_WEIGHTS.values())

    def test_a_uniform_roster_scores_its_rating(self):
        by_pos = {p: [70.0] * 8 for p in ("QB", "RB", "WR", "TE", "DL", "EDGE", "LB", "CB", "S", "DB", "K", "P")}
        assert t10.slot_score(by_pos) == pytest.approx(70.0)

    def test_depth_counts_but_less_than_starters(self):
        base = {"QB": [80.0], "DL": [80.0] * 4}
        deep = {"QB": [80.0, 60.0], "DL": [80.0] * 4 + [60.0] * 3}
        assert t10.slot_score(deep) < t10.slot_score(base)
        # the starters carry 80%: a weaker depth moves a group by 0.2 x 20
        assert t10.slot_score({"QB": [80.0, 60.0]}) == pytest.approx(76.0)

    def test_a_missing_group_renormalises_instead_of_defaulting(self):
        assert t10.slot_score({"QB": [90.0]}) == pytest.approx(90.0)

    def test_no_players_is_none(self):
        assert t10.slot_score({}) is None

    def test_cb_s_and_db_pool_and_edge_joins_dl(self):
        a = t10.slot_score({"CB": [80.0] * 3, "S": [80.0] * 2})
        b = t10.slot_score({"DB": [80.0] * 5})
        assert a == pytest.approx(b)
        assert t10.slot_score({"EDGE": [75.0] * 2, "DL": [75.0] * 2}) == pytest.approx(75.0)

    def test_ol_is_not_a_slot(self):
        assert "OL" not in t10.SLOT_WEIGHTS
        assert t10.slot_score({"OL": [99.0], "QB": [70.0]}) == pytest.approx(70.0)


class TestPaceFactor:
    def test_a_regular_full_share_player_is_near_the_old_rule(self):
        out = t06.pace_factor_for(10.0, 2.0, 8, 8, None)
        assert out == pytest.approx(10.0 * 2 ** 0.5 * t06.PACE_SHARE_K[-1])

    def test_a_player_who_played_few_of_his_teams_games_is_scaled_down_more(self):
        full = t06.pace_factor_for(10.0, 2.0, 8, 8, None) / (10.0 * 2 ** 0.5)
        part = t06.pace_factor_for(10.0, 2.0, 3, 8, None) / (10.0 * 2 ** 0.5)
        assert part < full

    def test_shrink_pulls_toward_the_position_mean(self):
        hi = t06.pace_factor_for(40.0, 2.0, 3, 8, 10.0)
        none = t06.pace_factor_for(40.0, 2.0, 3, 8, None)
        assert hi < none
        lo = t06.pace_factor_for(2.0, 2.0, 3, 8, 10.0)
        assert lo > t06.pace_factor_for(2.0, 2.0, 3, 8, None)

    def test_constants_are_ordered(self):
        assert list(t06.PACE_SHARE_EDGES) == sorted(t06.PACE_SHARE_EDGES)
        assert list(t06.PACE_SHARE_K) == sorted(t06.PACE_SHARE_K)
