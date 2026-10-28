"""Tests for power: the number teams are ranked by (utils/results_rating.py)."""

import json
from pathlib import Path

import pytest

from utils.results_rating import BETA, BONUS, CAP, expected_value, game_value, power_components

APP_DATA = Path(__file__).parent.parent.parent / "cfb-analytics-app" / "data"


def _g(h, a, hs, as_, neutral=True, week=1):
    return {"home_team_id": h, "away_team_id": a, "home_score": hs, "away_score": as_, "neutral_site": neutral, "week": week}


def _none(_t):
    return None


class TestGameValue:
    def test_a_win_is_worth_its_margin_plus_the_bonus(self):
        assert game_value(10) == pytest.approx(10 + BONUS)

    def test_a_loss_costs_its_margin_and_the_bonus(self):
        assert game_value(-3) == pytest.approx(-3 - BONUS)

    def test_a_blowout_is_capped(self):
        assert game_value(80) == pytest.approx(CAP + BONUS)

    def test_a_tie_is_worth_nothing(self):
        assert game_value(0) == 0.0


class TestPowerComponents:
    def test_with_no_games_power_is_the_rating_part_alone(self):
        teams, _ = power_components([], {1: 90.0, 2: 70.0}, _none)
        assert teams[1]["power"] == pytest.approx(teams[1]["rating_part"]) and teams[1]["results_part"] == 0.0
        assert teams[1]["rating_part"] == pytest.approx(BETA * 10.0)

    def test_the_three_parts_add_up_to_power(self):
        games = [_g(1, 2, 31, 10), _g(2, 3, 20, 17), _g(3, 1, 7, 24)]
        teams, _ = power_components(games, {1: 80.0, 2: 75.0, 3: 70.0}, _none)
        for p in teams.values():
            assert p["power"] == pytest.approx(p["rating_part"] + p["conf_part"] + p["results_part"])

    def test_a_loss_counts_against_a_team_the_rating_alone_would_rank_first(self):
        """The defect this exists for: equal ratings, one team has lost twice and the other has not."""
        games = [_g(1, 3, 10, 24), _g(1, 4, 14, 28), _g(2, 3, 35, 7), _g(2, 4, 31, 10)]
        teams, _ = power_components(games, {1: 85.0, 2: 85.0, 3: 75.0, 4: 75.0}, _none)
        assert teams[2]["power"] > teams[1]["power"] + 10

    def test_beating_a_team_the_ratings_call_strong_counts_more_than_beating_a_weak_one(self):
        games = [_g(1, 10, 27, 20), _g(2, 12, 27, 20)]
        teams, _ = power_components(games, {1: 80.0, 2: 80.0, 10: 92.0, 12: 60.0}, _none)
        assert teams[1]["results_part"] > teams[2]["results_part"]

    def test_a_team_with_one_game_is_pulled_toward_its_rating(self):
        teams, _ = power_components([_g(1, 2, 70, 0)], {1: 80.0, 2: 80.0}, _none)
        assert 0 < teams[1]["results_part"] < CAP

    def test_games_with_a_missing_score_or_an_unrated_team_are_ignored(self):
        games = [_g(1, 2, None, 3), _g(1, 99, 40, 0)]
        teams, info = power_components(games, {1: 80.0, 2: 80.0}, _none)
        assert teams[1]["games"] == 0 and info["home_edge"] == 0.0

    def test_wins_and_losses_are_counted(self):
        teams, _ = power_components([_g(1, 2, 20, 10), _g(2, 1, 3, 17)], {1: 80.0, 2: 80.0}, _none)
        assert (teams[1]["wins"], teams[1]["losses"]) == (2, 0) and (teams[2]["wins"], teams[2]["losses"]) == (0, 2)

    def test_the_conference_part_is_off(self):
        """Switched off on evidence (it changes the result by 0.1 point): it is always 0."""
        games = [_g(1, 2, 40, 0), _g(3, 4, 0, 40)]
        teams, _ = power_components(games, {1: 80.0, 2: 80.0, 3: 80.0, 4: 80.0}, lambda t: "A" if t in (1, 2) else "B")
        assert all(abs(p["conf_part"]) < 1e-6 for p in teams.values())

    def test_expected_value_comes_from_ratings_and_the_home_edge(self):
        home, away = {"rating_part": 6.0, "conf_part": 0.0}, {"rating_part": 1.0, "conf_part": 0.0}
        assert expected_value(home, away, 2.5, False) == pytest.approx(7.5)
        assert expected_value(home, away, 2.5, True) == pytest.approx(5.0)


@pytest.mark.skipif(not (APP_DATA / "team_ratings.json").exists(), reason="no exported team ratings")
class TestExported:
    def test_every_rated_team_season_carries_power_and_its_parts(self):
        rows = json.loads((APP_DATA / "team_ratings.json").read_text(encoding="utf-8"))
        bad = [r for r in rows if r["overall_rating"] is not None
               and (r.get("power_rating") is None or r.get("rating_part") is None)]
        assert not bad, f"{len(bad)} rated team-seasons have no power rating"
        for r in rows:
            if r.get("power_rating") is not None and r.get("results_rating") is not None:
                assert abs(r["power_rating"] - (r["rating_part"] + (r.get("conf_part") or 0) + r["results_rating"])) < 0.05

    def test_power_is_centred_on_an_average_team_each_played_season(self):
        rows = json.loads((APP_DATA / "team_ratings.json").read_text(encoding="utf-8"))
        for season in range(2012, 2026):
            ps = [r["power_rating"] for r in rows if r["season"] == season and r.get("power_rating") is not None]
            assert abs(sum(ps) / len(ps)) < 1.5, f"{season}: power is not centred on zero"

    def test_each_games_table_adds_up_to_its_teams_results(self):
        """power_games_{season}.json explains the ranking: over = value - expected, per game."""
        f = APP_DATA / "power_games_2025.json"
        if not f.exists():
            pytest.skip("no power games exported")
        for games in json.loads(f.read_text(encoding="utf-8")).values():
            for g in games:
                assert g["over"] == pytest.approx(g["value"] - g["expected"], abs=0.11)
