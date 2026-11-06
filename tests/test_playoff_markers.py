"""The pre-playoff marker test (scripts/validate_playoff_markers.py): its scoring is right, and the data it scores is what it assumes.

The scoring functions are small and pure, so they are tested on made-up leagues where the answer is known. The data checks guard the
assumptions the report rests on (twelve selected teams and one champion a season, eleven playoff games a season each between two
selected teams, a week-15 snapshot with every reading for every selected team, the spread's sign), because a report that quietly
scored the wrong games would still print a plausible table.
"""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("markers", ROOT / "scripts" / "validate_playoff_markers.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


M = _load()


def league(n=20, selected=12):
    """n teams rated n..1 by every reading; the first `selected` of them are the field."""
    ids = list(range(1, n + 1))
    snap = pd.DataFrame({"team_id": ids, "school": [f"T{i}" for i in ids]})
    for r in (*M.READINGS, "record", "prior"):
        snap[r] = [float(n - i) for i in range(n)]
    field = pd.DataFrame({"team_id": ids[:selected], "committee_rank": list(range(1, selected + 1)), "seed": list(range(1, selected + 1))})
    return snap, field


class TestWilson:
    def test_an_empty_sample_says_nothing(self):
        assert M.wilson(0, 0) == (0.0, 1.0)

    def test_is_centred_near_the_rate_and_inside_zero_to_one(self):
        lo, hi = M.wilson(11, 22)
        assert 0.0 < lo < 0.5 < hi < 1.0
        assert abs((lo + hi) / 2 - 0.5) < 0.01

    def test_a_perfect_record_still_has_a_floor(self):
        lo, hi = M.wilson(22, 22)
        assert hi == pytest.approx(1.0) and lo < 0.9


class TestRanks:
    def test_ties_share_the_better_rank_and_missing_stays_missing(self):
        r = M.rank_of(pd.Series([5.0, 5.0, 3.0, np.nan]))
        assert list(r[:3]) == [1.0, 1.0, 3.0] and np.isnan(r.iloc[3])


class TestSelection:
    def test_a_reading_that_orders_the_field_first_holds_all_of_it(self):
        snap, field = league()
        x = M.selection(snap, set(field["team_id"]), "head")
        assert x["hits"] == 12 and x["missed"] == [] and x["extra"] == []

    def test_a_reversed_reading_holds_only_the_overlap(self):
        snap, field = league()
        snap["head"] = snap["head"][::-1].values              # now the field is the BOTTOM 12 of 20
        x = M.selection(snap, set(field["team_id"]), "head")
        assert x["hits"] == 4                                  # teams 9..12 sit in both the reversed top 12 and the field
        assert len(x["missed"]) == 8 and len(x["extra"]) == 8

    def test_order_agreement_is_plus_one_for_the_committees_order_and_minus_one_reversed(self):
        snap, field = league()
        assert M.order_agreement(snap, field, "head") == pytest.approx(1.0)
        field["committee_rank"] = field["committee_rank"][::-1].values
        assert M.order_agreement(snap, field, "head") == pytest.approx(-1.0)


class TestFormatSelection:
    def test_low_rated_champions_get_in_and_push_out_the_lowest_at_large_teams(self):
        snap, _ = league(20)
        champions = {16, 17, 18, 19, 20}                         # the five lowest-rated teams won their conferences
        field = set(range(1, 8)) | champions                     # the format admits them, then the seven best others
        x = M.format_selection(snap, field, champions, "head")
        assert x["hits"] == 12 and x["missed"] == [] and x["extra"] == []
        assert x["auto"] == [16, 17, 18, 19, 20]
        assert M.selection(snap, field, "head")["hits"] == 7      # the plain top 12 holds only the seven at-large teams

    def test_with_champions_already_at_the_top_it_is_the_plain_top_twelve(self):
        snap, field = league(20)
        x = M.format_selection(snap, set(field["team_id"]), {1, 2, 3, 4, 5, 6}, "head")
        assert x["hits"] == 12 and x["auto"] == [1, 2, 3, 4, 5]   # only five champions are automatic


class TestPicks:
    def test_the_higher_value_is_picked_and_ties_and_gaps_are_not_picks(self):
        assert M.pick(3, 2, 10, 20) == 10 and M.pick(2, 3, 10, 20) == 20
        assert M.pick(2, 2, 10, 20) is None and M.pick(None, 2, 10, 20) is None and M.pick(np.nan, 2, 10, 20) is None

    def games(self):
        return pd.DataFrame({"home_team_id": [1, 3, 5], "away_team_id": [2, 4, 6], "winner": [1, 4, 5], "cfb_api_id": [101, 102, 103]})

    def test_accuracy_counts_correct_picks_and_the_games_picked(self):
        value = {1: 9, 2: 1, 3: 8, 4: 2, 5: 7, 6: 7}           # game 3 is a tie: no pick
        assert M.accuracy(self.games(), value) == (1, 2)

    def test_the_market_favourite_is_the_negative_spread_side_and_a_pickem_is_no_pick(self):
        spreads = {101: -3.5, 102: -7.0, 103: 0.0}             # home favoured by 3.5 and by 7, then a pick'em
        assert M.market_accuracy(self.games(), spreads) == (1, 2)    # game 1 right (home wins), game 2 wrong (home favoured, away won)


class TestMargins:
    def test_a_perfect_prediction_has_no_error_and_unlisted_games_are_skipped(self):
        g = pd.DataFrame({"home_team_id": [1], "away_team_id": [2], "margin": [7.0], "neutral_site": [True], "cfb_api_id": [1]})
        out = M.margin_errors(g, {1: 10.0, 2: 3.0}, {1: -7.0})
        assert out["n"] == 1 and out["ours_rmse"] == pytest.approx(0.0) and out["mkt_rmse"] == pytest.approx(0.0)
        assert M.margin_errors(g, {1: 10.0}, {1: -7.0}) == {}


# ── the data the report scores ────────────────────────────────────────────────

def _have_data():
    return all((ROOT / "data" / "computed" / f"weekly_teams_{s}.json").exists() for s in M.SEASONS) and \
        (ROOT / "data" / "raw" / "cfp_participants.json").exists()


needs_data = pytest.mark.skipif(not _have_data(), reason="weekly replays or the CFP field are not generated in this checkout")


@needs_data
@pytest.mark.parametrize("season", M.SEASONS)
class TestTheData:
    def test_the_field_is_twelve_teams_with_one_champion(self, season):
        f = M.cfp_field(season)
        assert len(f) == 12 and f["team_id"].nunique() == 12
        assert (f["outcome"] == "champion").sum() == 1
        assert sorted(f["seed"]) == list(range(1, 13))

    def test_there_are_eleven_playoff_games_each_between_two_selected_teams(self, season):
        ids = set(M.cfp_field(season)["team_id"])
        g = M.postseason_games(season)
        both = g[g["home_team_id"].isin(ids) & g["away_team_id"].isin(ids)]
        assert len(both) == 11, f"{len(both)} playoff games found for {season}"
        # a selected team plays only playoff games after the field is named: none of them is in a bowl against an outsider
        outside = g[(g["home_team_id"].isin(ids) ^ g["away_team_id"].isin(ids))]
        assert outside.empty, f"{len(outside)} postseason games pair a selected team with an outsider"

    def test_every_automatic_bid_won_a_conference_title_game(self, season):
        f = M.cfp_field(season)
        champions = M.conference_champions(season)
        auto = set(f.loc[f["bid_type"] == "automatic", "team_id"].astype(int))
        assert len(auto) == M.AUTO_BIDS and auto <= champions, f"automatic bids not among the title-game winners: {auto - champions}"

    def test_the_snapshot_holds_every_reading_for_every_selected_team(self, season):
        snap = M.snapshot(season).set_index("team_id")
        for t in M.cfp_field(season)["team_id"]:
            assert t in snap.index, f"team {t} has no week-{M.SNAPSHOT_WEEK} snapshot"
            assert snap.loc[t, list(M.READINGS)].notna().all()

    def test_the_spread_is_the_home_teams_so_a_negative_spread_means_a_home_win(self, season):
        g = M.postseason_games(season)
        sp = M.closing_spreads(season)
        rows = [(r.margin, -sp[r.cfb_api_id]) for r in g.itertuples() if r.cfb_api_id in sp]
        assert len(rows) > 30
        m, s = (np.array(c, dtype=float) for c in zip(*rows))
        assert np.corrcoef(m, s)[0, 1] > 0.2, "the spread does not predict the margin with this sign: the convention is wrong"
