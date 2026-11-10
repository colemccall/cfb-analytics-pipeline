"""The earned order (v4.27): a second number beside power, with wins and losses counted first. Power itself must not move."""

import json
from pathlib import Path

import pytest

from utils.results_rating import BONUS, CAP, EARNED_BONUS, EARNED_CAP, earned_ratings, game_value, power_components

APP_DATA = Path(__file__).parent.parent.parent / "cfb-analytics-app" / "data"


def _g(h, a, hs, as_):
    return {"home_team_id": h, "away_team_id": a, "home_score": hs, "away_score": as_, "neutral_site": True, "week": 1}


def _none(_t):
    return None


class TestConstants:
    def test_power_keeps_its_own_constants(self):
        assert (CAP, BONUS) == (28.0, 8.0)

    def test_a_game_is_worth_its_capped_margin_plus_the_result(self):
        assert game_value(30, EARNED_CAP, EARNED_BONUS) == pytest.approx(EARNED_CAP + EARNED_BONUS)
        assert game_value(-3, EARNED_CAP, EARNED_BONUS) == pytest.approx(-3 - EARNED_BONUS)


class TestOrder:
    # Team 1 wins two close games and loses none; team 2 wins a blowout and loses a close one. Same opponents.
    GAMES = [_g(1, 3, 21, 20), _g(1, 4, 21, 20), _g(2, 3, 56, 0), _g(4, 2, 21, 20)]

    def test_the_team_with_fewer_losses_ranks_first_here_and_power_disagrees(self):
        e = earned_ratings(self.GAMES, [1, 2, 3, 4], _none)
        assert e[1] > e[2]
        p, _ = power_components(self.GAMES, {t: 0.0 for t in (1, 2, 3, 4)}, _none, cap=CAP, bonus=20.0)
        q, _ = power_components(self.GAMES, {t: 0.0 for t in (1, 2, 3, 4)}, _none, cap=56.0, bonus=0.0)
        assert q[2]["power"] > q[1]["power"]      # by margin alone the blowout team is ahead
        assert p[1]["games"] == 2

    def test_beating_a_team_that_has_won_is_worth_more(self):
        games = [_g(5, 6, 10, 0), _g(6, 8, 10, 0), _g(7, 9, 10, 0), _g(9, 10, 0, 10)]
        e = earned_ratings(games, [5, 6, 7, 8, 9, 10], _none)
        assert e[5] > e[7]                        # 5 beat a team that won; 7 beat a team that lost

    def test_a_team_with_no_game_has_no_number_rather_than_a_zero(self):
        e = earned_ratings([_g(1, 2, 10, 0)], [1, 2, 3], _none)
        assert 3 not in e and set(e) == {1, 2}


class TestPublished:
    def test_every_team_with_power_in_the_live_file_has_an_earned_number(self):
        f = APP_DATA / "live_2026.json"
        if not f.exists():
            pytest.skip("no live file")
        rows = [t for t in json.loads(f.read_text(encoding="utf-8"))["teams"] if t.get("results_games")]
        assert rows and all(t.get("earned_rating") is not None for t in rows)

    def test_a_played_season_exports_it_and_a_projected_row_does_not_invent_one(self):
        rows = json.loads((APP_DATA / "team_ratings.json").read_text(encoding="utf-8"))
        assert any(r.get("earned_rating") is not None for r in rows if r["season"] == 2025)
        assert all(r.get("earned_rating") is None for r in rows if r["season"] == 2026)

    def test_each_game_carries_what_it_was_worth(self):
        games = json.loads((APP_DATA / "power_games_2025.json").read_text(encoding="utf-8"))
        g = next(iter(games.values()))[0]
        m = g["margin"]
        assert g["earned_value"] == pytest.approx(game_value(m, EARNED_CAP, EARNED_BONUS), abs=0.05)
