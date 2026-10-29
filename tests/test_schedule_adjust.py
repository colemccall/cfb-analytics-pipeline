"""The stats half of the team rating is adjusted for the schedule the team faced (v4.18).

A box score is what a team did against the opponents it happened to play. Until v4.18 the stats half read
it raw, so a Group-of-6 team beating weak opponents looked as strong as a Power-4 team of the same
strength; with the roster half compressed in-season it supplied about 70% of the spread instead of its
nominal 40%. These tests pin the mechanics: the schedule is measured from our own opponent strengths, a
missing opponent id is the pooled FCS team, an average schedule moves nothing, and the published halves
add up to the headline.
"""

import importlib.util
from pathlib import Path

import pytest

from utils import opponent_strength as ostr

_spec = importlib.util.spec_from_file_location(
    "_s10s", Path(__file__).parent.parent / "scripts" / "10_compute_team_ratings.py")
_s10 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s10)

SP_MEANS = (0.0, 5.0, 5.0)
STATS = {"yards_pg": 430.0, "yards_allowed_pg": 340.0, "third_down_pct": 42.0, "third_down_def_pct": 38.0,
         "sacks_pg": 2.5, "tfl_pg": 6.0, "turnover_margin": 0.0}
ROSTER = {pg: [80.0, 78.0, 76.0] for pg in ("QB", "RB", "WR", "TE", "OL", "EDGE", "DL", "LB", "CB", "S", "K", "P")}


def _g(h, a, hs, as_, **kw):
    return {"home_team_id": h, "away_team_id": a, "home_score": hs, "away_score": as_, "neutral_site": False,
            "season_type": "regular", **kw}


def _league():
    """Two conferences of four. Each team plays a round robin in its own conference and one cross game
    (the stronger conference A wins it 38-21); teams 1 and 5 also beat an FCS team (missing id) 52-7."""
    games = []
    for conf_teams in ((1, 2, 3, 4), (5, 6, 7, 8)):
        for i, h in enumerate(conf_teams):
            for a in conf_teams[i + 1:]:
                games.append(_g(h, a, 30, 27))
    games += [_g(1, 5, 38, 21), _g(2, 6, 38, 21), _g(3, 7, 38, 21), _g(4, 8, 38, 21)]
    games += [_g(1, float("nan"), 52, 7), _g(5, float("nan"), 52, 7)]
    conf = {**{t: "A" for t in (1, 2, 3, 4)}, **{t: "B" for t in (5, 6, 7, 8)}}
    return games, (lambda t: conf.get(int(t)) if t != ostr.FCS else None), list(conf)


class TestScheduleFaced:
    def test_a_stronger_conference_means_a_tougher_schedule(self):
        games, conf_of, ids = _league()
        s = ostr.schedule_faced(games, conf_of, ids)
        assert s[2][0] > s[6][0] and s[2][1] > s[6][1], "conference A faced the stronger defenses and offenses"

    def test_centred_so_an_average_schedule_moves_nothing(self):
        games, conf_of, ids = _league()
        s = ostr.schedule_faced(games, conf_of, ids)
        assert sum(v[0] for v in s.values()) / len(s) == pytest.approx(0.0, abs=1e-9)
        assert sum(v[1] for v in s.values()) / len(s) == pytest.approx(0.0, abs=1e-9)

    def test_a_missing_opponent_id_is_the_pooled_fcs_team_not_a_skipped_game(self):
        games, conf_of, ids = _league()
        with_fcs = ostr.schedule_faced(games, conf_of, ids)
        without = ostr.schedule_faced([g for g in games if g["away_team_id"] == g["away_team_id"]], conf_of, ids)
        # teams 1 and 2 are interchangeable except that team 1 also played an FCS team
        assert abs(without[1][0] - without[2][0]) < 0.5 and abs(without[1][1] - without[2][1]) < 0.5
        assert with_fcs[2][0] - with_fcs[1][0] > 1.0, "the FCS game made team 1's schedule easier against defenses"
        assert with_fcs[2][1] - with_fcs[1][1] > 1.0, "and against offenses"

    def test_a_team_with_no_scored_game_has_no_schedule(self):
        games, conf_of, ids = _league()
        s = ostr.schedule_faced(games, conf_of, ids + [99])
        assert 99 not in s

    def test_unscored_games_do_not_count(self):
        games, conf_of, ids = _league()
        games.append(_g(1, 5, None, None))
        assert ostr.schedule_faced(games, conf_of, ids) == ostr.schedule_faced(games[:-1], conf_of, ids)


class TestStatsHalfAdjustment:
    def test_no_schedule_is_the_raw_box_score(self):
        assert _s10.team_stats_split(STATS) == _s10.team_stats_split(STATS, None)
        assert _s10.team_stats_to_ovr(STATS) == _s10.team_stats_to_ovr(STATS, None)

    def test_each_half_moves_by_the_coefficient_per_point(self):
        off0, def0, adj0 = _s10.team_stats_split(STATS)
        off, dfn, adj = _s10.team_stats_split(STATS, (2.0, -1.0))
        assert off == pytest.approx(off0 + _s10.SCHED_COEF * 2.0)
        assert dfn == pytest.approx(def0 - _s10.SCHED_COEF * 1.0)
        assert adj == adj0, "the turnover adjustment is not a schedule effect"

    def test_the_same_box_score_against_a_tougher_schedule_rates_higher(self):
        hard = _s10.team_stats_to_ovr(STATS, (3.0, 3.0))
        easy = _s10.team_stats_to_ovr(STATS, (-3.0, -3.0))
        assert hard > easy
        assert hard - easy == pytest.approx(_s10.SCHED_COEF * 6.0)

    def test_an_average_schedule_changes_nothing(self):
        assert _s10.team_stats_to_ovr(STATS, (0.0, 0.0)) == pytest.approx(_s10.team_stats_to_ovr(STATS))

    def test_a_missing_half_is_not_invented(self):
        only_def = {"yards_allowed_pg": 340.0}
        off, dfn, _ = _s10.team_stats_split(only_def, (2.0, 2.0))
        assert off is None and dfn is not None

    def test_the_coefficient_is_inside_the_measured_band(self):
        # fitted 1.56 (offense) and 1.68 (defense); leave-one-season-out 1.52-1.71; pooled 1.62
        assert 1.45 <= _s10.SCHED_COEF <= 1.65


class TestPublishedHalves:
    def _splits(self, sched):
        return _s10.compute_team_splits(1, None, dict(ROSTER), dict(STATS), SP_MEANS, None, 75.0, sched)

    def test_the_halves_add_up_to_the_headline(self):
        s = self._splits((1.5, -0.5))
        want = 0.6 * s["roster_half"] + 0.4 * s["stats_half"]
        assert s["overall_rating"] == pytest.approx(want, abs=0.02)

    def test_the_raw_half_is_kept_beside_the_adjusted_one(self):
        s = self._splits((2.0, 2.0))
        assert s["stats_half"] - s["stats_half_raw"] == pytest.approx(_s10.SCHED_COEF * 2.0, abs=0.02)
        assert s["sched_faced"] == [2.0, 2.0]

    def test_without_a_schedule_the_halves_agree_and_none_is_recorded(self):
        s = self._splits(None)
        assert s["stats_half"] == s["stats_half_raw"]
        assert s["sched_faced"] is None

    def test_a_tougher_schedule_raises_offense_and_defense_ratings(self):
        hard, easy = self._splits((3.0, 3.0)), self._splits((-3.0, -3.0))
        assert hard["overall_rating"] > easy["overall_rating"]
        assert hard["offense_rating"] > easy["offense_rating"]
        assert hard["defense_rating"] > easy["defense_rating"]

    def test_the_roster_half_is_untouched_by_the_schedule(self):
        assert self._splits((3.0, 3.0))["roster_half"] == self._splits((-3.0, -3.0))["roster_half"]
