"""The v4.20 changes to what a live player rating is built from (scripts 06 and 07).

1. A season with games and no `team_records` rows (one in progress) gets an opponent map from the current conferences, where it
   used to get {} and so a multiplier of 1.0 for every game; a finished season is untouched.
2. A game against a non-FBS opponent counts at one fixed multiplier, for every position and both sides of the ball.
3. A quarterback's game score is half the volume score and half the same score at a standard number of attempts.
4. The games damp in a season in progress reads the share of his team's games a player played, not the raw count.
"""

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


t06 = _load("_t06v420", "06_compute_edge_scores.py")
t07 = _load("_t07v420", "07_compute_player_ratings.py")


def _games(season, pairs):
    """Scored games from (home, away, home points, away points) tuples."""
    return pd.DataFrame([{"season": season, "season_type": "regular", "week": i + 1, "home_team_id": h, "away_team_id": a,
                          "home_score": hs, "away_score": as_, "neutral_site": False} for i, (h, a, hs, as_) in enumerate(pairs)])


TEAMS = pd.DataFrame({"id": [1, 2, 3, 4], "school": list("ABCD"), "conference": ["X", "X", "Y", "Y"]})
PAIRS = [(1, 2, 30, 10), (3, 4, 24, 20), (1, 3, 27, 24), (2, 4, 14, 21), (1, 4, 35, 7), (2, 3, 10, 17), (1, None, 63, 0)]


def _patch_store(monkeypatch, records):
    def fake(name):
        return {"games": _games(2026, PAIRS), "team_records": records, "teams": TEAMS}[name]
    monkeypatch.setattr(t06, "read_raw", fake)


class TestOpponentMapFallback:
    def test_a_season_with_no_records_uses_current_conferences(self, monkeypatch):
        _patch_store(monkeypatch, pd.DataFrame(columns=["season", "team_id", "conference"]))
        m = t06.build_opponent_map(2026)
        assert {1, 2, 3, 4} <= set(m) and t06.OPP_FCS_KEY in m

    def test_the_fallback_gives_a_real_adjustment_not_one(self, monkeypatch):
        _patch_store(monkeypatch, pd.DataFrame(columns=["season", "team_id", "conference"]))
        m = t06.build_opponent_map(2026)
        means = t06._season_means(m)
        mults = {t6: t06.opponent_multiplier(t6, m, "defense", means) for t6 in (1, 2, 3, 4)}
        assert len({round(v, 3) for v in mults.values()}) > 1          # teams differ, so the multipliers differ

    def test_a_season_with_records_ignores_the_fallback(self, monkeypatch):
        recs = pd.DataFrame({"season": [2026, 2026], "team_id": [1, 2], "conference": ["X", "X"]})
        _patch_store(monkeypatch, recs)
        m = t06.build_opponent_map(2026)
        assert {1, 2} <= set(m) and 3 not in m and 4 not in m          # a team without a record that season is not FBS

    def test_current_conferences_skips_teams_with_no_conference(self, monkeypatch):
        t = TEAMS.copy()
        t.loc[0, "conference"] = None
        monkeypatch.setattr(t06, "read_raw", lambda name: t)
        assert t06.current_conferences() == {2: "X", 3: "Y", 4: "Y"}


class TestFcsWeight:
    MAP = {-1: {"offense": 20.0, "defense": 20.0, "overall": -10.0}, 1: {"offense": 30.0, "defense": 24.0, "overall": 5.0}}

    def test_a_missing_or_unknown_opponent_is_the_fixed_multiplier(self):
        means = t06._season_means(self.MAP)
        assert t06.opponent_multiplier(None, self.MAP, "defense", means) == t06.OPP_FCS_MULT
        assert t06.opponent_multiplier(999, self.MAP, "offense", means) == t06.OPP_FCS_MULT

    def test_the_value_is_the_owners_085(self):
        assert t06.OPP_FCS_MULT == pytest.approx(0.85)

    def test_it_is_the_same_for_both_sides_and_every_position(self):
        means = t06._season_means(self.MAP)
        assert {t06.opponent_multiplier(None, self.MAP, side, means) for side in ("defense", "offense")} == {t06.OPP_FCS_MULT}

    def test_an_fbs_opponent_still_reads_its_strength(self):
        means = t06._season_means(self.MAP)
        assert t06.opponent_multiplier(1, self.MAP, "defense", means) != t06.OPP_FCS_MULT

    def test_a_map_without_the_fcs_key_is_the_sp_comparison_path(self):
        sp_only = {1: self.MAP[1]}
        assert t06.opponent_multiplier(None, sp_only, "defense", t06._season_means(sp_only)) == 1.0


class TestQbEfficiencyBlend:
    STATS = {"passingYDS": 280, "passingTD": 2, "passingINT": 0, "rushingYDS": 0, "rushingTD": 0}

    def _with_attempts(self, att, **extra):
        return {**self.STATS, "passingC/ATT": f"{int(att * 0.6)}/{att}", **extra}

    def test_a_typical_attempt_count_is_unchanged(self):
        s = self._with_attempts(int(t06.QB_ATT_REF))
        assert t06._off_stat_composite("QB", s) == pytest.approx(self._base(self.STATS))

    def test_fewer_attempts_for_the_same_score_is_worth_more(self):
        assert t06._off_stat_composite("QB", self._with_attempts(20)) > t06._off_stat_composite("QB", self._with_attempts(28))

    def test_more_attempts_for_the_same_score_is_worth_less(self):
        assert t06._off_stat_composite("QB", self._with_attempts(45)) < t06._off_stat_composite("QB", self._with_attempts(28))

    @staticmethod
    def _base(stats):
        """The volume score for a game from the shipped weights, before the efficiency blend."""
        w = t06.OFFENSIVE_COMPOSITE["QB"]
        return sum(stats.get(k, 0) * v for k, v in w.items())

    def test_half_the_move_of_a_full_normalisation(self):
        att = 14
        base = self._base(self.STATS)
        full = base / att * t06.QB_ATT_REF
        assert t06._off_stat_composite("QB", self._with_attempts(att)) == pytest.approx(0.5 * base + 0.5 * full)

    def test_a_relief_appearance_is_floored(self):
        s2 = self._with_attempts(2)
        base = self._base(self.STATS)
        floored = 0.5 * base + 0.5 * base / t06.QB_ATT_FLOOR * t06.QB_ATT_REF
        assert t06._off_stat_composite("QB", s2) == pytest.approx(floored)

    def test_a_game_with_no_positive_score_is_not_rescaled(self):
        bad = {"passingYDS": 20, "passingTD": 0, "passingINT": 2, "passingC/ATT": "4/12"}
        assert t06._off_stat_composite("QB", bad) == pytest.approx(20 * t06.OFFENSIVE_COMPOSITE["QB"]["passingYDS"] - 40)

    def test_attempts_are_read_from_either_form(self):
        assert t06._qb_attempts({"passingC/ATT": "12/20"}) == 20.0
        assert t06._qb_attempts({"passingATT": "31"}) == 31.0
        assert t06._qb_attempts({}) == 0.0

    def test_other_positions_are_untouched(self):
        s = {"rushingYDS": 100, "rushingTD": 1, "receivingYDS": 20}
        assert t06._off_stat_composite("RB", s) == pytest.approx(100 + 20.0 + 0.9 * 20)

    def test_passing_volume_outweighs_rushing_volume_for_the_same_yards(self):
        # v4.21 tilt: a passing yard is worth more than a rushing yard, and a rushing touchdown less than before
        w = t06.OFFENSIVE_COMPOSITE["QB"]
        assert w["passingYDS"] > w["rushingYDS"] > 0
        assert w["passingYDS"] == pytest.approx(1.05) and w["rushingYDS"] == pytest.approx(0.6) and w["rushingTD"] == pytest.approx(18.0)
        assert w["passingTD"] == 25.0 and w["passingINT"] == -20.0
        air = {"passingYDS": 150, "rushingYDS": 0, "passingC/ATT": "15/28"}
        ground = {"passingYDS": 0, "rushingYDS": 150, "passingC/ATT": "15/28"}
        assert t06._off_stat_composite("QB", air) > t06._off_stat_composite("QB", ground)

    def test_the_blend_is_in_range(self):
        assert 0.0 < t06.QB_EFFICIENCY_BLEND < 1.0 and t06.QB_ATT_FLOOR < t06.QB_ATT_REF


def _frame(rows):
    return pd.DataFrame(rows).set_index(pd.Index(range(len(rows))))


class TestGamesDampShare:
    def _damp(self, rows, scaled):
        return t07.apply_games_confidence(np.array(scaled, dtype=float), _frame(rows), pg="QB")

    def test_a_finished_season_is_unchanged(self):
        out = self._damp([{"games_played": 7, "_season": 2024}, {"games_played": 8, "_season": 2024}, {"games_played": 12, "_season": 2024}],
                         [90.0, 90.0, 90.0])
        avg = 90.0
        assert list(out) == [avg, avg, avg]            # all equal: the mean is the rating, so nothing moves
        mixed = self._damp([{"games_played": 7, "_season": 2024}, {"games_played": 12, "_season": 2024}, {"games_played": 12, "_season": 2024}],
                           [90.0, 60.0, 60.0])
        avg = np.mean([90.0, 60.0, 60.0])
        assert mixed[0] == pytest.approx(avg + 7 / 8 * (90.0 - avg), abs=0.01)

    def test_a_player_in_every_team_game_is_a_full_timer_at_any_point_of_the_season(self):
        rows = [{"games_played": 7, "team_games": 7, "_season": 2026}, {"games_played": 5, "team_games": 5, "_season": 2026},
                {"games_played": 12, "team_games": 12, "_season": 2026}]
        out = self._damp(rows, [90.0, 90.0, 90.0])
        assert list(out) == [90.0, 90.0, 90.0]

    def test_a_bye_week_does_not_change_the_rating(self):
        # seven games for a team that has played seven, and seven for one that has played eight (a bye or a missed game): the
        # first used to be damped at 7/8 and the second at 7/8 too; now both are full-timers (share 1.0 and 0.875 >= 2/3)
        a = self._damp([{"games_played": 7, "team_games": 7, "_season": 2026}, {"games_played": 8, "team_games": 8, "_season": 2026}], [90.0, 60.0])
        b = self._damp([{"games_played": 7, "team_games": 8, "_season": 2026}, {"games_played": 8, "team_games": 8, "_season": 2026}], [90.0, 60.0])
        assert a[0] == b[0] == 90.0

    def test_a_season_in_progress_has_no_games_damp_the_curve_replaces_it(self):
        """v4.24: missing a share of his team's games costs nothing; the sample-size rule is PART_SEASON_CURVE, applied in the rating."""
        out = self._damp([{"games_played": 3, "team_games": 7, "_season": 2026}, {"games_played": 7, "team_games": 7, "_season": 2026}], [90.0, 50.0])
        assert list(out) == [90.0, 50.0]


class TestPartSeasonCurve:
    def test_the_same_level_is_believed_less_on_fewer_games(self):
        """The curve is stricter the fewer games he has played, and never above the level it was given at these levels."""
        three, five, eight = (t07.part_season_curve(90.0, "RB", n) for n in (3, 5, 8))
        assert three < five < eight < 90.0

    def test_a_game_his_team_played_without_him_changes_nothing(self):
        """The rating is a function of his own games only: nothing in it reads how many games his team has played."""
        import inspect
        assert "team_games" not in inspect.signature(t07.part_season_curve).parameters

    def test_eight_or_more_games_use_the_8_9_line_and_unfitted_groups_are_left_alone(self):
        assert t07.part_season_curve(80.0, "QB", 11) == t07.part_season_curve(80.0, "QB", 8)
        assert t07.part_season_curve(80.0, "K", 5) == 80.0        # no line for a kicker: nothing is applied

    def test_the_level_edge_is_a_twelve_game_season_at_his_own_games(self):
        # game scores summing to 40 over 4 games make an EDGE of 40 / sqrt(4) = 20; at 12 games' worth that is 20 x sqrt(12 / 4)
        assert t06.level_edge(20.0, 4) == pytest.approx(20.0 * (12 / 4) ** 0.5)
        assert t06.level_edge(20.0, 12) == pytest.approx(20.0)


class TestPartTimeThroughTheEdgePath:
    """v4.24: a part-time player of a season in progress is rated by his EDGE level and the curve, not by a volume gate."""

    @staticmethod
    def _row(team_games, games=3, measured=18, edge=53.2247):
        return pd.DataFrame([{"edge_score": edge, "stats_measured": measured, "games_played": games, "team_games": team_games,
                              "_season": 2026, "opp_avg_sp": 15.0}])

    def test_below_the_volume_gate_he_is_still_rated_by_his_level_in_season(self):
        """Eighteen counted events is under a DL's 20, which sent him to the stat fallback (73.6 for Harkless's 93.6 level)."""
        in_season = t07.compute_edge_ratings(self._row(8.0), "DL")[0][0]
        level = t07.edge_to_ovr(53.2247, "DL", 2026)
        assert in_season == pytest.approx(t07.part_season_curve(level, "DL", 3))
        assert in_season > 76.0

    def test_a_finished_season_keeps_the_volume_gate(self):
        """No team_games: the same row below the gate goes to the stat fallback, as before."""
        finished = t07.compute_edge_ratings(self._row(0.0), "DL")[0][0]
        assert finished != pytest.approx(t07.part_season_curve(t07.edge_to_ovr(53.2247, "DL", 2026), "DL", 3))

    def test_one_or_two_games_and_a_missing_score_are_not_routed(self):
        row = lambda **kw: self._row(8.0, **kw).iloc[0]          # noqa: E731
        assert t07.part_time_edge(row())
        assert not t07.part_time_edge(row(games=2))
        assert not t07.part_time_edge(row(edge=float("nan")))
        assert not t07.part_time_edge(self._row(0.0).iloc[0])
