"""A week-N snapshot holds only what was known after week N, and a week counts only when it is complete (v4.24).

Until v4.24 the weekly builder removed the SCORES of later games but left the stats whole, so season totals fetched on
the last day were read into every earlier week, and the stored "week 9" had been built from 7 of its 314 games.
validate_snapshot_invariance.py measures the same thing on real data; these tests pin the pieces on a few rows.
"""

import importlib.util
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent


def _script(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, ROOT / "scripts" / name)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


live = _script("17_live_season.py", "live17_integrity")
w17b = _script("17b_weekly_progression.py", "w17b_integrity")


def _games(week_scored: dict) -> pd.DataFrame:
    """FBS-involved games for each week: {week: (games, scored)}; the rest of a week's games have no score."""
    rows = []
    for wk, (n, scored) in week_scored.items():
        for i in range(n):
            rows.append({"season": 2026, "season_type": "regular", "week": wk, "home_team_id": 1, "away_team_id": None,
                         "home_score": 30.0 if i < scored else None, "away_score": 10.0 if i < scored else None})
    return pd.DataFrame(rows)


class TestCompleteWeeks:
    def test_a_week_with_a_few_games_played_is_not_complete(self):
        """The stored week 9 had 7 of 314 games (the Tuesday to Thursday ones)."""
        g = _games({8: (100, 96), 9: (100, 7)})
        assert live.complete_weeks(g, 2026, {1}) == [8]

    def test_a_week_with_most_games_played_is_complete(self):
        """A cancelled game or an FCS score the API never wrote does not hold a week back."""
        g = _games({8: (100, 96), 9: (100, 91)})
        assert live.complete_weeks(g, 2026, {1}) == [8, 9]

    def test_games_without_an_fbs_team_do_not_count_toward_a_week(self):
        g = _games({9: (10, 10)})
        other = _games({9: (300, 0)}).assign(home_team_id=99)          # 300 unplayed FCS-only games
        assert live.complete_weeks(pd.concat([g, other]), 2026, {1}) == [9]

    def test_the_progress_label_is_the_last_complete_week_and_counts_the_games_after_it(self):
        g = _games({8: (100, 96), 9: (100, 7)})
        got = live.season_progress(g, 2026, {1})
        assert got["through_week"] == 8
        assert got["games_after_through_week"] == 7
        assert got["completed"] == 96 + 7                                # every played game still counts toward a record


class TestLimitToWeek:
    def _sandbox(self, tmp_path):
        (tmp_path / "raw").mkdir()
        (tmp_path / "computed").mkdir()
        games = [{"id": 1, "season": 2026, "season_type": "regular", "week": 1},
                 {"id": 2, "season": 2026, "season_type": "regular", "week": 2},
                 {"id": 3, "season": 2026, "season_type": "regular", "week": 3}]
        stats = [
            {"player_season_id": 10, "game_id": 1, "season": 2026, "stat_type": "game_aggregate", "data": {"rushingYDS": "50"}},
            {"player_season_id": 10, "game_id": 2, "season": 2026, "stat_type": "game_aggregate", "data": {"rushingYDS": "60"}},
            {"player_season_id": 10, "game_id": 3, "season": 2026, "stat_type": "game_aggregate", "data": {"rushingYDS": "70"}},
            {"player_season_id": 10, "game_id": None, "season": 2026, "stat_type": "season_aggregate",
             "data": {"rushingYDS": "180", "rushingCAR": "30", "ppa": 0.3, "snap_pct": 0.5}},
            {"player_season_id": 10, "game_id": None, "season": 2026, "stat_type": "postseason_aggregate", "data": {"rushingYDS": "99"}},
            {"player_season_id": 11, "game_id": None, "season": 2025, "stat_type": "season_aggregate", "data": {"rushingYDS": "500"}},
        ]
        (tmp_path / "raw" / "stats.json").write_text(json.dumps(stats), encoding="utf-8")
        (tmp_path / "raw" / "team_advanced_season.json").write_text(json.dumps(
            [{"season": 2026, "team_id": 1, "def_plays": 400}, {"season": 2025, "team_id": 1, "def_plays": 800}]), encoding="utf-8")
        (tmp_path / "computed" / "ratings.json").write_text(json.dumps(
            [{"season": 2026, "engine": "edge", "player_season_id": 10}, {"season": 2026, "engine": "projected", "player_season_id": 10},
             {"season": 2025, "engine": "edge", "player_season_id": 11}]), encoding="utf-8")
        (tmp_path / "computed" / "team_ratings.json").write_text(json.dumps(
            [{"season": 2026, "engine": "edge", "team_id": 1}]), encoding="utf-8")
        return games

    def _read(self, tmp_path, rel):
        return json.loads((tmp_path / rel).read_text(encoding="utf-8"))

    def test_an_earlier_week_loses_later_games_the_production_totals_and_the_final_ratings(self, tmp_path):
        games = self._sandbox(tmp_path)
        cut = w17b.limit_to_week(tmp_path, 2026, games, 1, latest_week=3)
        assert cut["game_rows"] == 2 and cut["postseason_totals"] == 1
        rows = self._read(tmp_path, "raw/stats.json")
        assert [r["game_id"] for r in rows if r["season"] == 2026 and r["game_id"] is not None] == [1]
        total = next(r for r in rows if r.get("stat_type") == "season_aggregate" and r["season"] == 2026)
        assert total["data"] == {"ppa": 0.3, "snap_pct": 0.5}                 # usage and PPA stay; production is gone
        assert not [r for r in rows if r.get("stat_type") == "postseason_aggregate"]
        assert any(r["season"] == 2025 for r in rows)                          # another season is untouched
        assert self._read(tmp_path, "raw/team_advanced_season.json") == [{"season": 2025, "team_id": 1, "def_plays": 800}]
        ratings = self._read(tmp_path, "computed/ratings.json")
        assert {(r["season"], r["engine"]) for r in ratings} == {(2026, "projected"), (2025, "edge")}
        assert self._read(tmp_path, "computed/team_ratings.json") == []

    def test_the_latest_week_keeps_the_totals_that_are_cumulative_to_it(self, tmp_path):
        games = self._sandbox(tmp_path)
        w17b.limit_to_week(tmp_path, 2026, games, 3, latest_week=3)
        rows = self._read(tmp_path, "raw/stats.json")
        total = next(r for r in rows if r.get("stat_type") == "season_aggregate" and r["season"] == 2026)
        assert total["data"]["rushingYDS"] == "180"
        assert len(self._read(tmp_path, "raw/team_advanced_season.json")) == 2
        assert len([r for r in rows if r.get("game_id") is not None]) == 3
