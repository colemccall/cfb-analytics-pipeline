"""The weekly progression (v4.22): box-score season totals, the two single-game grades, and the exported weekly files.

The files are written by scripts/17b_weekly_progression.py; the contract tests skip until they exist.
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
APP_DATA = ROOT.parent / "cfb-analytics-app" / "data"

from utils import api_client  # noqa: E402
from utils.box_scores import fetch_season_rows, season_rows  # noqa: E402


def _load_script(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, ROOT / "scripts" / name)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _team(name, **stats):
    return {"team": name, "stats": [{"category": k, "stat": v} for k, v in stats.items()]}


class TestBoxScoreTotals:
    def _games(self):
        return [
            {"id": 1, "teams": [_team("A", totalYards="400", thirdDownEff="6-13", possessionTime="30:30", sacks="2"),
                                _team("B", totalYards="250", thirdDownEff="3-12", possessionTime="29:30", sacks="1")]},
            {"id": 2, "teams": [_team("B", totalYards="300", thirdDownEff="5-10", possessionTime="31:00", sacks="0"),
                                _team("C", totalYards="500", thirdDownEff="7-11", possessionTime="29:00", sacks="4")]},
        ]

    def _table(self):
        t = {}
        for r in season_rows(self._games()):
            t.setdefault(r["team"], {})[r["statName"]] = r["statValue"]
        return t

    def test_own_stats_sum_and_the_opponent_side_is_the_other_team(self):
        t = self._table()
        assert t["B"]["totalYards"] == 550 and t["B"]["totalYardsOpponent"] == 900     # 400 + 500 allowed
        assert t["A"]["sacks"] == 2 and t["A"]["sacksOpponent"] == 1

    def test_made_and_attempted_are_split_and_the_clock_is_in_seconds(self):
        t = self._table()
        assert (t["B"]["thirdDownConversions"], t["B"]["thirdDowns"]) == (8, 22)
        assert t["A"]["possessionTime"] == 30 * 60 + 30

    def test_games_counts_the_box_scores_a_team_appears_in(self):
        t = self._table()
        assert (t["A"]["games"], t["B"]["games"], t["C"]["games"]) == (1, 2, 1)

    def test_a_game_without_two_teams_is_skipped(self):
        assert season_rows([{"id": 9, "teams": [_team("A", totalYards="1")]}]) == []

    def test_a_missing_stat_is_absent_not_zero(self):
        t = {}
        for r in season_rows([{"id": 1, "teams": [_team("A", totalYards="10"), _team("B")]}]):
            t.setdefault(r["team"], {})[r["statName"]] = r["statValue"]
        assert "totalYards" not in t["B"] and t["B"]["totalYardsOpponent"] == 10

    def test_the_sums_are_what_stats_season_reports(self):
        """Every statistic both sources carry, every FBS team, 2026 through the weeks on file: the rule the weekly stats half rests on."""
        season = 2026
        needed = [("/stats/season", {"year": season})] + [("/games/teams", {"year": season, "week": w, "seasonType": "regular"}) for w in range(1, 10)]
        if not all(os.path.exists(os.path.join(api_client.CACHE_DIR, api_client._cache_key(p, q) + ".json")) for p, q in needed):
            pytest.skip("2026 box scores or season stats are not in the API cache")
        season_tab = {}
        for r in api_client._get_list("x", "/stats/season", {"year": season}):
            season_tab.setdefault(r["team"], {})[r["statName"]] = float(r["statValue"] or 0)
        games = max(v.get("games", 0) for v in season_tab.values())
        mine = {}
        for r in fetch_season_rows("x", season, 9, api_client._get_list):
            mine.setdefault(r["team"], {})[r["statName"]] = r["statValue"]
        if games > 9:
            pytest.skip("the season stats cover more than the nine weeks compared")
        bad = [(t, k, v, mine[t][k]) for t, d in season_tab.items() if t in mine for k, v in d.items()
               if k in mine[t] and abs(v - mine[t][k]) > 0.5]
        assert not bad, f"{len(bad)} statistics differ between /stats/season and the summed box scores, e.g. {bad[:3]}"


class TestShutoutsReadBack:
    def test_the_harvest_keeps_a_zero(self):
        h = _load_script("01_harvest_games_players_stats.py", "h01")
        assert h._points({"homePoints": 0}, "homePoints", "home_points") == 0
        assert h._points({"home_points": 0}, "homePoints", "home_points") == 0
        assert h._points({}, "homePoints", "home_points") is None
        assert h._points({"homePoints": None, "home_points": 7}, "homePoints", "home_points") == 7

    def test_the_live_records_count_a_game_won_by_shutout(self):
        import pandas as pd
        from utils.store import fill_shutouts
        live = _load_script("17_live_season.py", "live17t")
        games = pd.DataFrame([{"season": 2026, "season_type": "regular", "week": 1, "home_team_id": 1, "away_team_id": None,
                               "home_score": 34.0, "away_score": None}])
        assert live.season_progress(games, 2026)["completed"] == 0           # read raw, the game has no result
        got = live.season_progress(fill_shutouts(games), 2026)
        assert got["completed"] == 1 and got["records"] == {1: (1, 0)}


class TestSingleGameGrades:
    @pytest.fixture(scope="class")
    def w(self):
        return _load_script("17b_weekly_progression.py", "wk17b")

    ident = staticmethod(lambda x: x)

    def test_a_blowout_is_capped_at_28(self, w):
        assert w.team_game_grade(60, 0.0, 0.0, "neutral", self.ident) == w.team_game_grade(28, 0.0, 0.0, "neutral", self.ident) == 28.0

    def test_the_home_edge_comes_off_a_home_margin_and_onto_an_away_one(self, w):
        assert w.team_game_grade(10, 0.0, 3.0, "home", self.ident) == 7.0
        assert w.team_game_grade(10, 0.0, 3.0, "away", self.ident) == 13.0
        assert w.team_game_grade(10, 0.0, 3.0, "neutral", self.ident) == 10.0

    def test_the_same_margin_against_a_better_opponent_grades_higher(self, w):
        assert w.team_game_grade(7, 10.0, 2.0, "home", self.ident) > w.team_game_grade(7, -5.0, 2.0, "home", self.ident)

    def test_a_player_game_is_one_game_of_a_twelve_game_season(self, w):
        seen = []
        w.player_game_grade(10.0, "WR", lambda e, pg: seen.append((e, pg)) or 0.0)
        assert seen == [(pytest.approx(10.0 * 12 ** 0.5), "WR")]


def _weekly(name):
    path = APP_DATA / name
    if not path.exists():
        pytest.skip(f"{name} not exported - run scripts/17b_weekly_progression.py")
    return json.loads(path.read_text(encoding="utf-8"))


class TestWeeklyFiles:
    def test_every_team_row_has_a_rank_that_is_unique_within_its_week(self):
        doc = _weekly("weekly_teams_2026.json")
        for wk in doc["weeks"]:
            rows = [r for t in doc["teams"].values() for r in t["weeks"] if r["week"] == wk]
            heads = sorted(r["rk_head"] for r in rows)
            assert heads == list(range(1, len(rows) + 1)), f"week {wk}: headline ranks are not 1..{len(rows)}"
            assert sorted(r["rk_power"] for r in rows) == list(range(1, len(rows) + 1))

    def test_a_record_counts_only_games_through_that_week(self):
        """No later game may reach an earlier week: wins + losses is the scored games of weeks up to it, FCS opponents included."""
        doc = _weekly("weekly_teams_2026.json")
        sched = _weekly("schedules_2026.json")
        bad = []
        for tid, t in doc["teams"].items():
            games = [g for g in sched.get(tid, []) if g.get("season_type") == "regular" and g.get("team_score") is not None]
            for r in t["weeks"]:
                want = sum(1 for g in games if g["week"] <= r["week"])
                if r["w"] + r["l"] != want:
                    bad.append((t["school"], r["week"], r["w"] + r["l"], want))
        assert not bad, f"{len(bad)} weekly records disagree with the schedule through that week, e.g. {bad[:3]}"

    def test_the_headline_is_the_weighted_readings_every_week(self):
        doc = _weekly("weekly_teams_2026.json")
        w = {"roster": 1 / 6, "stats": 1 / 6, "results": 2 / 3}
        for t in doc["teams"].values():
            for r in t["weeks"]:
                if r["head"] is None or any(r[k] is None for k in w):
                    continue
                assert abs(r["head"] - sum(r[k] * v for k, v in w.items())) < 0.11, (t["school"], r["week"])   # four values rounded to 0.1: up to 0.1 apart

    def test_the_last_week_matches_the_live_file(self):
        """Players: identical (same games, same chain). Teams: only the line unit is missing, so within a point."""
        doc = _weekly("weekly_teams_2026.json")
        live = _weekly("live_2026.json")
        if doc["weeks"][-1] != live["through_week"]:
            pytest.skip("the weekly files are not at the live file's week - rerun script 17b")
        lt = {str(t["team_id"]): t["overall_rating"] for t in live["teams"]}
        gaps = [abs(t["weeks"][-1]["head"] - lt[tid]) for tid, t in doc["teams"].items()
                if tid in lt and t["weeks"][-1]["head"] is not None and lt[tid] is not None]
        assert gaps and max(gaps) < 1.0, f"largest headline gap to the live file is {max(gaps):.2f}"
        wp = _weekly("weekly_players_2026.json")
        lp = {str(p["player_id"]): p["overall_rating"] for p in live["players"]}
        pg = [abs(rows[-1][1] - lp[pid]) for pid, rows in wp["players"].items() if pid in lp and rows[-1][0] == live["through_week"]]
        assert pg and max(pg) < 0.05, f"largest player gap to the live file is {max(pg):.2f}"

    def test_a_game_grade_exists_only_where_a_game_was_played(self):
        doc = _weekly("weekly_teams_2026.json")
        for t in doc["teams"].values():
            for r in t["weeks"]:
                assert (r.get("gid") is None) == (r.get("grade") is None), (t["school"], r["week"])
        wp = _weekly("weekly_players_2026.json")
        for rows in wp["players"].values():
            for row in rows:
                assert (row[4] is None) == (row[5] is None)


class TestRankingMethods:
    """The live file and the weekly files carry the three ranking methods (v4.23): games-only power, rating-weighted power, the rating."""

    def test_the_live_file_carries_the_blend_beside_the_games_only_power(self):
        live = _weekly("live_2026.json")
        for t in live["teams"]:
            if t["power_rating"] is None:
                continue
            assert t["rating_part"] == 0.0, "the games-only power has no rating part while a season is played"
            assert t["power_blend"] is not None and t["rating_part_blend"] is not None and t["results_blend"] is not None, t["school"]
            assert abs(t["power_blend"] - (t["rating_part_blend"] + t["results_blend"])) < 0.03, (t["school"], t["power_blend"])

    def test_a_weekly_row_has_both_ranks_and_the_units(self):
        doc = _weekly("weekly_teams_2026.json")
        for wk in doc["weeks"]:
            rows = [r for t in doc["teams"].values() for r in t["weeks"] if r["week"] == wk]
            assert sorted(r["rk_blend"] for r in rows) == list(range(1, len(rows) + 1)), f"week {wk}: blend ranks are not 1..{len(rows)}"
        last = [t["weeks"][-1] for t in doc["teams"].values()]
        assert all(r["groups"] for r in last), "a team's last week has no position groups"
        assert all(set(r["groups"]) <= {"QB", "RB", "WR", "TE", "DL", "LB", "DB", "K", "P", "OL"} for r in last)


class TestGameLogs:
    """gamelog_{season}_{team_id}.json (script 12): every player's box score for every game, joined to the schedule by game id."""

    def _files(self):
        files = sorted(APP_DATA.glob("gamelog_2026_*.json"))
        if not files:
            pytest.skip("no gamelog_2026_*.json exported - run scripts/12_export_frontend_json.py --game-logs")
        return files

    def test_a_file_per_team_and_every_game_is_on_that_teams_schedule(self):
        files = self._files()
        assert len(files) >= 130, f"{len(files)} team game logs"
        sched = _weekly("schedules_2026.json")
        bad = []
        for f in files:
            tid = f.stem.split("_")[-1]
            doc = json.loads(f.read_text(encoding="utf-8"))
            games = {g["game_id"] for g in sched.get(tid, [])}
            for pid, rows in doc["players"].items():
                for gid, stats in rows:
                    if gid not in games or not isinstance(stats, dict) or not stats:
                        bad.append((tid, pid, gid))
        assert not bad, f"{len(bad)} game-log rows are not on the team's schedule or have no stats, e.g. {bad[:3]}"

    def test_a_player_appears_in_one_teams_file_and_once_per_game(self):
        seen = {}
        for f in self._files():
            doc = json.loads(f.read_text(encoding="utf-8"))
            for pid, rows in doc["players"].items():
                assert pid not in seen, f"player {pid} is in {seen[pid]} and {f.name}"
                seen[pid] = f.name
                gids = [g for g, _ in rows]
                assert len(gids) == len(set(gids)), f"player {pid} has a game twice"

    def test_export_game_logs_groups_by_team_and_keeps_every_stat_as_stored(self, tmp_path):
        import pandas as pd
        m = _load_script("12_export_frontend_json.py", "exp12")
        T = {"stats": pd.DataFrame([
                {"player_season_id": 1, "player_id": 999, "game_id": 10, "season": 2026, "stat_type": "game_aggregate", "data": {"passingYDS": "19", "passingC/ATT": "3/5"}},
                {"player_season_id": 1, "game_id": 11, "season": 2026, "stat_type": "game_aggregate", "data": json.dumps({"passingYDS": "0"})},
                {"player_season_id": 2, "game_id": 10, "season": 2026, "stat_type": "game_aggregate", "data": {"defensiveTOT": "4"}},
                {"player_season_id": 1, "game_id": 10, "season": 2025, "stat_type": "game_aggregate", "data": {"passingYDS": "99"}},
                {"player_season_id": 1, "season": 2026, "stat_type": "season_aggregate", "data": {"passingYDS": "500"}, "game_id": None}]),
             "player_seasons": pd.DataFrame([{"id": 1, "player_id": 100, "team_id": 7, "season": 2026}, {"id": 2, "player_id": 200, "team_id": 8, "season": 2026}])}
        m.export_game_logs(T, tmp_path, 2026)
        a = json.loads((tmp_path / "gamelog_2026_7.json").read_text(encoding="utf-8"))
        assert a["players"] == {"100": [[10, {"passingYDS": "19", "passingC/ATT": "3/5"}], [11, {"passingYDS": "0"}]]}      # a stored 0 is kept
        b = json.loads((tmp_path / "gamelog_2026_8.json").read_text(encoding="utf-8"))
        assert b["players"] == {"200": [[10, {"defensiveTOT": "4"}]]}
