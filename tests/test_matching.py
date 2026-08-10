"""Tests for utils.matching — name→player matching for scraped sources.

These lock in two real false-positive classes found while building the EA CFB 27
harvest (script 08):

  1. A fuzzy name hit with no school confirmation matched different people —
     "Chaden Sullivan" (Tulsa) was matched to Caden Sullivan, "Nate Johnson" to
     Tate Johnson, "Evan Hampton" to Ethan Hampton.
  2. Two different people who share a name both matched our single record —
     CFB 27 has two Brandon Whites; both claimed the same player_id.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.matching import (  # noqa: E402
    match_player,
    normalize_school,
    resolve_collisions,
    strip_suffix,
)


def idx(entries):
    """Build an index: {name_key: [(player_id, player_season_id, school, season)]}."""
    out = {}
    for pid, psid, name, school, season in entries:
        out.setdefault(strip_suffix(name), []).append((pid, psid, school, season))
    return out


class TestExactMatching:
    def test_name_and_school_agree(self):
        index = idx([(1, 10, "Jayden Virgin-Morgan", "boise state", 2025)])
        assert match_player("Jayden Virgin-Morgan", "Boise State", index) == (1, 10, "team")

    def test_unique_name_matches_despite_new_school(self):
        """Transfers: EA lists the new school, our latest season has the old one."""
        index = idx([(2, 20, "Juelz Goff", "pittsburgh", 2025)])
        assert match_player("Juelz Goff", "Boise State", index) == (2, 20, "unique")

    def test_ambiguous_name_without_school_confirmation_is_no_match(self):
        index = idx([
            (3, 30, "John Smith", "alabama", 2025),
            (4, 40, "John Smith", "georgia", 2025),
        ])
        assert match_player("John Smith", "Ohio State", index) == (None, None, None)

    def test_ambiguous_name_resolved_by_school(self):
        index = idx([
            (3, 30, "John Smith", "alabama", 2025),
            (4, 40, "John Smith", "georgia", 2025),
        ])
        assert match_player("John Smith", "Georgia", index) == (4, 40, "team")


class TestFuzzyRequiresSchool:
    def test_near_miss_name_at_other_school_is_rejected(self):
        """The Chaden/Caden Sullivan case — one edit apart, different people."""
        index = idx([(5, 50, "Caden Sullivan", "iowa state", 2025)])
        assert match_player("Chaden Sullivan", "Tulsa", index) == (None, None, None)

    def test_near_miss_name_at_same_school_is_accepted(self):
        """Same school makes a one-character difference a spelling variant."""
        index = idx([(5, 50, "Caden Sullivan", "iowa state", 2025)])
        assert match_player("Chaden Sullivan", "Iowa State", index) == (5, 50, "team")

    def test_unrelated_name_never_matches(self):
        index = idx([(6, 60, "Marvin Harrison", "ohio state", 2025)])
        assert match_player("Bijan Robinson", "Texas", index) == (None, None, None)


class TestSuffixAndSchoolNormalization:
    def test_suffixes_are_stripped(self):
        index = idx([(7, 70, "Harry Stewart III", "kansas", 2025)])
        assert match_player("Harry Stewart III", "Kansas", index)[0] == 7

    def test_suffix_mismatch_still_matches(self):
        index = idx([(7, 70, "Harry Stewart III", "kansas", 2025)])
        assert match_player("Harry Stewart", "Kansas", index)[0] == 7

    def test_school_aliases(self):
        assert normalize_school("UMass") == "massachusetts"
        assert normalize_school("Miami (Ohio)") == "miami (oh)"
        assert normalize_school("Cal") == "california"
        assert normalize_school("Connecticut") == "uconn"

    def test_aliased_school_confirms_a_match(self):
        index = idx([(8, 80, "Some Player", "massachusetts", 2025)])
        assert match_player("Some Player", "UMass", index)[2] == "team"


class TestResolveCollisions:
    def test_school_confirmed_row_wins(self):
        rows = [
            {"player_id": 9, "player_season_id": 90, "match_type": "team"},
            {"player_id": 9, "player_season_id": 90, "match_type": "unique"},
        ]
        assert resolve_collisions(rows) == 1
        assert rows[0]["player_id"] == 9
        assert rows[1]["player_id"] is None

    def test_all_unmatched_when_none_confirmed(self):
        """Two Brandon Whites, neither confirmed — refuse to guess."""
        rows = [
            {"player_id": 9, "player_season_id": 90, "match_type": "unique"},
            {"player_id": 9, "player_season_id": 90, "match_type": "unique"},
        ]
        assert resolve_collisions(rows) == 2
        assert all(r["player_id"] is None for r in rows)

    def test_all_unmatched_when_several_confirmed(self):
        rows = [
            {"player_id": 9, "player_season_id": 90, "match_type": "team"},
            {"player_id": 9, "player_season_id": 90, "match_type": "team"},
        ]
        assert resolve_collisions(rows) == 2
        assert all(r["player_id"] is None for r in rows)

    def test_single_claim_is_untouched(self):
        rows = [{"player_id": 9, "player_season_id": 90, "match_type": "unique"}]
        assert resolve_collisions(rows) == 0
        assert rows[0]["player_id"] == 9
