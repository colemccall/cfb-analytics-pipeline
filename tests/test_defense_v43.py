"""The v4.3 defensive changes: solo/assist, fumble recoveries, opportunity, havoc.

Each of these was checked against measured data before it shipped, and each has a
specific way of going wrong that these tests pin down:

  solo split          zeroing every tackle in a season that never recorded solos
  fumble recoveries   crediting `fumblesFUM`, which is a fumble a defender COMMITTED
  opportunity index   getting the direction backwards (fewer plays faced is better)
  havoc share         being scored at all — it failed its ablation and must stay at 0
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_spec = importlib.util.spec_from_file_location(
    "s06", Path(__file__).parent.parent / "scripts" / "06_compute_edge_scores.py")
s06 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s06)


def rows(*payloads) -> pd.DataFrame:
    return pd.DataFrame({"data": list(payloads)})


class TestSoloAssistSplit:
    def test_solo_is_worth_more_than_an_assist(self):
        assert s06.SOLO_MULT > s06.ASSIST_MULT

    def test_split_is_near_neutral_in_aggregate(self):
        """Calibrated so the average defender's tackle credit does not move.

        Solo tackles are 56.4% of all recorded tackles 2013-2026. If this drifts
        far from 1.0 the change stops being a re-weighting and becomes a
        league-wide inflation or deflation of every defensive rating.
        """
        share = 0.564
        effective = share * s06.SOLO_MULT + (1 - share) * s06.ASSIST_MULT
        assert effective == pytest.approx(1.0, abs=0.03)

    def test_all_solo_beats_all_assisted_at_equal_volume(self):
        solo = s06._tackle_credit({"defensiveTOT": 10, "defensiveSOLO": 10}, 1.0, True)
        assist = s06._tackle_credit({"defensiveTOT": 10, "defensiveSOLO": 0}, 1.0, True)
        assert solo > assist

    def test_split_is_skipped_when_the_season_never_recorded_it(self):
        """Pre-2013 has no defensiveSOLO at all. Applying the split would read
        every classic-era tackle as an assist and deflate fifteen seasons."""
        stats = {"defensiveTOT": 10}
        assert s06._tackle_credit(stats, 1.0, solo_known=False) == pytest.approx(10.0)
        # With the split on and no SOLO field, the same row is all assists.
        assert s06._tackle_credit(stats, 1.0, solo_known=True) < 10.0

    def test_season_records_solo_detects_presence(self):
        assert s06.season_records_solo(rows({"defensiveSOLO": 3})) is True
        assert s06.season_records_solo(rows({"defensiveTOT": 9})) is False
        assert s06.season_records_solo(pd.DataFrame()) is False

    def test_solo_cannot_exceed_total(self):
        """A bad row must not create negative assists."""
        solo, assist = s06._tackle_split({"defensiveTOT": 4, "defensiveSOLO": 9})
        assert solo == 4
        assert assist == 0


class TestFumbles:
    def test_recoveries_are_credited(self):
        assert s06._fumble_recoveries({"fumblesREC": 2}) == 2.0

    def test_fumbles_committed_are_not_credited(self):
        """`fumblesFUM` on a defensive row is a fumble the player COMMITTED — 84%
        of the 974 such rows are games where he also had a return, an INT or a
        recovery, and 455 also carry fumblesLOST. Crediting it would pay a corner
        for coughing up an interception return."""
        assert s06._fumble_recoveries({"fumblesFUM": 3, "fumblesLOST": 1}) == 0.0

    def test_recovery_raises_the_composite(self):
        without, _ = s06._def_stat_composite("CB", {"defensiveTOT": 5}, True)
        with_rec, _ = s06._def_stat_composite("CB", {"defensiveTOT": 5, "fumblesREC": 1}, True)
        assert with_rec > without

    def test_every_position_has_a_fumble_weight(self):
        for pg, w in s06.DEF_STAT_WEIGHTS.items():
            assert "fum_rec" in w, f"{pg} has no fum_rec weight"

    def test_ball_hawk_counts_recoveries(self):
        base = s06._archetype_raws("CB", {"defensiveTOT": 4}, 0.0, True)
        withrec = s06._archetype_raws("CB", {"defensiveTOT": 4, "fumblesREC": 1}, 0.0, True)
        assert withrec["ball_hawk"] > base["ball_hawk"]


class TestOpportunityIndex:
    def test_clip_range_is_gentle_and_centred(self):
        lo, hi = s06.OPPORTUNITY_CLIP
        assert lo < 1.0 < hi
        assert hi - lo <= 0.5, "a wide clip makes snaps-faced the dominant term"

    def test_absent_table_degrades_to_no_adjustment(self, monkeypatch):
        """Without script 09's harvest the index must vanish, not misfire."""
        monkeypatch.setattr(s06, "read_raw", lambda t: pd.DataFrame())
        assert s06.build_opportunity_index(2025) == {}

    def test_index_rewards_facing_fewer_plays(self, monkeypatch):
        """Above 1.0 means fewer plays faced than the median — each counting stat
        represents more per opportunity. Backwards here would reward defences
        that never get off the field."""
        adv = pd.DataFrame([
            {"season": 2025, "team_id": t, "def_plays": plays}
            for t, plays in [(i, 800) for i in range(1, 30)] + [(99, 600), (98, 1000)]
        ])
        games = pd.DataFrame(
            [{"season": 2025, "home_team_id": t, "away_team_id": 0} for t in
             list(range(1, 30)) + [99, 98] for _ in range(12)])

        def fake(table):
            return adv if table == "team_advanced_season" else games
        monkeypatch.setattr(s06, "read_raw", fake)

        idx = s06.build_opportunity_index(2025)
        assert idx[99] > 1.0, "faced fewer plays — should be scaled up"
        assert idx[98] < 1.0, "faced more plays — should be scaled down"

    def test_partial_seasons_are_excluded(self, monkeypatch):
        adv = pd.DataFrame([{"season": 2025, "team_id": 1, "def_plays": 100}])
        monkeypatch.setattr(s06, "read_raw",
                            lambda t: adv if t == "team_advanced_season" else pd.DataFrame())
        assert s06.build_opportunity_index(2025) == {}


class TestHavocShareIsNotScored:
    """It failed its own ablation: replacing each unit's havoc with one shared
    constant scored BETTER (+0.0019) than the real denominator (+0.0011), which
    means the credit was re-weighting stats the composite already counts. It is
    computed and published, never added to the number."""

    def test_credit_is_empty(self):
        assert s06.HAVOC_CREDIT == {}, (
            "Havoc share is published, not scored. Re-enabling it needs a fresh "
            "ablation against EA, not just a value.")

    def test_share_is_still_computed(self):
        r = pd.DataFrame({
            "player_id": [1, 2],
            "player_team_id": [10, 10],
            "position_group": ["EDGE", "CB"],
            "data": [{"defensiveTFL": 10}, {"defensivePD": 5}],
        })
        share = s06.build_havoc_share(r, {10: (100.0, 50.0)})
        assert (10, 1) in share and (10, 2) in share
        assert 0.0 <= share[(10, 1)] <= 1.0

    def test_front_seven_and_secondary_use_their_own_denominators(self):
        r = pd.DataFrame({
            "player_id": [1, 2],
            "player_team_id": [10, 10],
            "position_group": ["EDGE", "CB"],
            "data": [{"defensiveTFL": 10}, {"defensivePD": 10}],
        })
        # Same event count, very different unit totals: the corner's share must
        # be larger because his unit did less.
        share = s06.build_havoc_share(r, {10: (200.0, 50.0)})
        assert share[(10, 2)] > share[(10, 1)]

    def test_player_havoc_does_not_double_count_sacks(self):
        """A sack is already a tackle for loss. Adding both would pay twice."""
        ev = s06._player_havoc_events({"defensiveTFL": 3, "defensiveSACKS": 2})
        assert ev == 3.0
