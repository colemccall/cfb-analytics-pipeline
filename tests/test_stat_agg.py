"""Tests for rebuilding a season aggregate from per-game rows.

~350-465 player-seasons a year have game rows and no season aggregate. Script 07
inner-joined on the aggregate, so those players were dropped from the ratings
outright — Jayden Virgin-Morgan played four seasons at Boise State with 12-14
game rows each and was rated in none of them, which is why he looked to a reader
like a player with no history.

The two API shapes do not agree, and the ways they disagree are exactly the ways
naive summing goes wrong.
"""

import pytest

from utils.stat_agg import META_KEYS, aggregate_game_stats, has_box_score


class TestCounts:
    def test_counting_stats_sum(self):
        out = aggregate_game_stats([
            {"receivingYDS": 80, "receivingREC": 5, "receivingTD": 1},
            {"receivingYDS": 45, "receivingREC": 3, "receivingTD": 0},
        ])
        assert out["receivingYDS"] == 125
        assert out["receivingREC"] == 8
        assert out["receivingTD"] == 1

    def test_games_played_is_the_row_count(self):
        out = aggregate_game_stats([{"defensiveTOT": 5}] * 11)
        assert out["games_played"] == 11

    def test_defensive_lines_sum(self):
        out = aggregate_game_stats([
            {"defensiveTOT": 7, "defensiveTFL": 1.5, "defensiveSACKS": 1.0},
            {"defensiveTOT": 4, "defensiveTFL": 0.5, "defensiveSACKS": 0.0},
        ])
        assert out["defensiveTOT"] == 11
        assert out["defensiveTFL"] == 2.0
        assert out["defensiveSACKS"] == 1.0


class TestPairedStrings:
    def test_completions_over_attempts_splits(self):
        """Game rows carry "25/38"; the season shape carries the two separately.
        The generic float coercion keeps the first field, which silently turns
        38 attempts into 25."""
        out = aggregate_game_stats([{"passingC/ATT": "25/38"}, {"passingC/ATT": "18/29"}])
        assert out["passingCOMPLETIONS"] == 43
        assert out["passingATT"] == 67

    def test_field_goals_split(self):
        out = aggregate_game_stats([{"kickingFG": "2/3"}, {"kickingFG": "1/1"}])
        assert out["kickingFGM"] == 3
        assert out["kickingFGA"] == 4

    def test_extra_points_split(self):
        out = aggregate_game_stats([{"kickingXP": "3/3"}, {"kickingXP": "2/3"}])
        assert out["kickingXPM"] == 5
        assert out["kickingXPA"] == 6

    def test_unparseable_pairs_are_zero_not_a_guess(self):
        out = aggregate_game_stats([{"passingC/ATT": "—"}, {"passingC/ATT": "12/20"}])
        assert out["passingCOMPLETIONS"] == 12
        assert out["passingATT"] == 20


class TestNonSummables:
    def test_long_is_a_maximum(self):
        out = aggregate_game_stats([
            {"rushingLONG": 12}, {"rushingLONG": 47}, {"rushingLONG": 8}])
        assert out["rushingLONG"] == 47

    def test_per_game_averages_are_dropped_not_summed(self):
        """A sum of averages is not a statistic, and 4.2 + 6.1 yards per carry
        would read as an 10.3 YPC season."""
        out = aggregate_game_stats([
            {"rushingYDS": 42, "rushingCAR": 10, "rushingAVG": 4.2},
            {"rushingYDS": 61, "rushingCAR": 10, "rushingAVG": 6.1},
        ])
        assert out.get("rushingAVG") is None
        assert out["rushingYPC"] == pytest.approx(5.15)

    def test_rates_are_recomputed_from_totals(self):
        """Weighting matters: a 2-carry game must not count as much as a 30-carry
        one, which is what averaging the per-game averages would do."""
        out = aggregate_game_stats([
            {"rushingYDS": 100, "rushingCAR": 10},
            {"rushingYDS": 4, "rushingCAR": 2},
        ])
        assert out["rushingYPC"] == pytest.approx(104 / 12)

    def test_completion_percentage_is_a_percentage(self):
        out = aggregate_game_stats([{"passingC/ATT": "15/20"}])
        assert out["passingPCT"] == pytest.approx(75.0)

    def test_a_rate_with_no_denominator_is_absent(self):
        out = aggregate_game_stats([{"defensiveTOT": 3}])
        assert "rushingYPC" not in out


class TestEmptiness:
    def test_no_rows_is_empty(self):
        assert aggregate_game_stats([]) == {}

    def test_only_junk_rows_is_empty(self):
        assert aggregate_game_stats([None, {}, "nonsense"]) == {}

    def test_empty_is_distinguishable_from_zero_production(self):
        """Callers need to tell 'never played' from 'played and did nothing'."""
        assert aggregate_game_stats([]) == {}
        assert aggregate_game_stats([{"defensiveTOT": 0}])["games_played"] == 1


class TestHasBoxScore:
    """"The row exists" is not "the stats came back".

    The harvest writes a season aggregate whenever usage OR PPA OR box score came
    back, so a row can hold nothing but a snap share. Treated as present it counts
    as zero production, which is how 176 offensive skill players in 2025 dragged
    down their whole position room's shares. Scripts 07, 12 and 15 all have to
    agree on this or a player is rated on evidence the site never shows.
    """

    def test_usage_only_is_not_production(self):
        assert not has_box_score({"ppa": 0.42, "snap_pct": 61.0,
                                  "games_played": 12, "award_tier": 0})

    def test_every_meta_key_alone_is_still_empty(self):
        for k in META_KEYS:
            assert not has_box_score({k: 7}), f"{k} alone should not count as production"

    def test_all_zero_box_score_is_empty(self):
        assert not has_box_score({"passingYDS": 0, "rushingCAR": 0, "ppa": 0.3})

    def test_missing_and_malformed_are_empty(self):
        assert not has_box_score(None)
        assert not has_box_score({})
        assert not has_box_score("not a dict")

    def test_any_real_production_counts(self):
        assert has_box_score({"rushingYDS": 812, "ppa": 0.1})
        assert has_box_score({"defensiveTOT": 3})

    def test_a_long_alone_counts(self):
        """One 41-yard carry is a season with production in it."""
        assert has_box_score({"rushingLONG": 41})

    def test_a_pair_string_counts(self):
        """Game-shaped rows reach this too; "25/38" is not zero."""
        assert has_box_score({"passingC/ATT": "25/38"})
        assert not has_box_score({"passingC/ATT": "0/0"})

    def test_a_rebuilt_aggregate_reads_as_production(self):
        """The round trip: summing game rows must produce something this accepts,
        or 07 would rebuild the same player on every run."""
        assert has_box_score(aggregate_game_stats([{"receivingYDS": 40, "receivingREC": 3}]))

    def test_a_scoreless_game_row_is_still_not_production(self):
        """games_played is metadata, so an appearance with no stats stays empty."""
        assert not has_box_score(aggregate_game_stats([{"receivingYDS": 0}]))


class TestAgainstTheRealSchema:
    def test_output_keys_match_what_script_07_reads(self):
        """Script 07 reads the season-aggregate spelling. Producing the game
        spelling instead would leave every value at zero without erroring."""
        out = aggregate_game_stats([{
            "passingC/ATT": "20/30", "passingYDS": 250, "passingTD": 2, "passingINT": 1,
            "rushingCAR": 5, "rushingYDS": 30, "receivingREC": 0,
            "kickingFG": "1/2", "kickingXP": "3/3", "kickingLONG": 45,
            "puntingNO": 3, "puntingYDS": 120, "puntingIn 20": 1,
            "defensiveTOT": 4, "defensiveTFL": 1, "defensiveSACKS": 0.5,
            "defensivePD": 2, "interceptionsINT": 1,
        }])
        for k in ("passingCOMPLETIONS", "passingATT", "kickingFGM", "kickingFGA",
                  "kickingXPM", "kickingXPA", "kickingLONG", "puntingNO",
                  "puntingYDS", "puntingIn 20", "defensiveTOT", "defensivePD",
                  "interceptionsINT", "games_played"):
            assert k in out, f"{k} missing — script 07 would read 0"
        assert "passingC/ATT" not in out and "kickingFG" not in out
