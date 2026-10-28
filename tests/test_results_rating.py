"""Tests for the results rating and power (utils/results_rating.py), the ranking number."""

import json
from pathlib import Path

import pytest

from utils.results_rating import CAP, W_QUALITY, W_RESULTS, power_rating, srs_ratings

APP_DATA = Path(__file__).parent.parent.parent / "cfb-analytics-app" / "data"


def _round_robin(strength, reps=3):
    """Every pair plays `reps` times, margin = strength gap exactly, no home edge."""
    games = []
    ids = list(strength)
    for _ in range(reps):
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                games.append({"home_team_id": a, "away_team_id": b, "neutral_site": True,
                              "home_score": 30 + strength[a] - strength[b], "away_score": 30})
    return games


class TestSrs:
    def test_the_order_of_true_strengths_is_recovered(self):
        s = {1: 10, 2: 4, 3: 0, 4: -6, 5: -8}
        r, _, _ = srs_ratings(_round_robin(s), s.keys())
        assert [t for t, _ in sorted(r.items(), key=lambda kv: -kv[1])] == [1, 2, 3, 4, 5]

    def test_beating_a_strong_team_counts_more_than_beating_a_weak_one(self):
        # A and B both win by 7 once, A against the best team and B against the worst.
        base = _round_robin({10: 12, 11: 0, 12: -12}, reps=4)
        games = base + [
            {"home_team_id": 1, "away_team_id": 10, "neutral_site": True, "home_score": 27, "away_score": 20},
            {"home_team_id": 2, "away_team_id": 12, "neutral_site": True, "home_score": 27, "away_score": 20},
        ]
        r, _, _ = srs_ratings(games, {1, 2, 10, 11, 12})
        assert r[1] > r[2]

    def test_a_blowout_is_capped(self):
        a = [{"home_team_id": 1, "away_team_id": 2, "neutral_site": True, "home_score": 100, "away_score": 0}]
        b = [{"home_team_id": 1, "away_team_id": 2, "neutral_site": True, "home_score": int(CAP), "away_score": 0}]
        ra, _, _ = srs_ratings(a, {1, 2})
        rb, _, _ = srs_ratings(b, {1, 2})
        assert ra[1] == pytest.approx(rb[1])

    def test_a_team_with_one_game_is_pulled_toward_average(self):
        r, _, n = srs_ratings([{"home_team_id": 1, "away_team_id": 2, "neutral_site": True, "home_score": 28, "away_score": 0}], {1, 2})
        assert 0 < r[1] < CAP / 2 and n[1] == 1

    def test_games_with_a_missing_score_or_a_non_fbs_team_are_ignored(self):
        games = [{"home_team_id": 1, "away_team_id": 2, "neutral_site": True, "home_score": None, "away_score": 3},
                 {"home_team_id": 1, "away_team_id": 99, "neutral_site": True, "home_score": 40, "away_score": 0}]
        assert srs_ratings(games, {1, 2}) == ({}, 0.0, {})


class TestPower:
    def test_power_is_none_without_a_team_rating(self):
        assert power_rating(None, 3.0, 70.0) is None

    def test_a_team_with_no_results_is_ranked_on_quality_alone(self):
        assert power_rating(80.0, None, 70.0) == pytest.approx(W_QUALITY * 10.0)

    def test_results_move_power_by_their_weight(self):
        assert power_rating(70.0, 2.0, 70.0) == pytest.approx(W_RESULTS * 2.0)

    def test_a_team_that_loses_to_you_cannot_outrank_you_on_rating_alone(self):
        """The defect this exists for: equal ratings, but one has results and the other does not."""
        good, bad = power_rating(85.0, 12.0, 75.0), power_rating(85.0, -3.0, 75.0)
        assert good > bad


@pytest.mark.skipif(not (APP_DATA / "team_ratings.json").exists(), reason="no exported team ratings")
class TestExported:
    def test_every_rated_team_season_carries_power(self):
        rows = json.loads((APP_DATA / "team_ratings.json").read_text(encoding="utf-8"))
        missing = [r for r in rows if r["overall_rating"] is not None and r.get("power_rating") is None]
        assert not missing, f"{len(missing)} rated team-seasons have no power rating"

    def test_power_is_centred_on_an_average_team_each_played_season(self):
        rows = json.loads((APP_DATA / "team_ratings.json").read_text(encoding="utf-8"))
        for season in range(2012, 2026):
            ps = [r["power_rating"] for r in rows if r["season"] == season and r.get("power_rating") is not None]
            assert abs(sum(ps) / len(ps)) < 1.5, f"{season}: power is not centred on zero"
