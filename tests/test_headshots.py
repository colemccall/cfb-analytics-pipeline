"""Tests for the headshot lookup (scripts/19_harvest_headshots.py) and its exported mapping."""

import importlib.util
import json
from pathlib import Path

import pytest

APP_DATA = Path(__file__).parent.parent.parent / "cfb-analytics-app" / "data"
_spec = importlib.util.spec_from_file_location("headshots", Path(__file__).parent.parent / "scripts" / "19_harvest_headshots.py")
hs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hs)


def _index(players):
    idx = {}
    for p in players:
        idx.setdefault(hs.norm(p["name"]), []).append(p)
    return idx


class TestMatching:
    def test_names_agree_through_accents_punctuation_and_suffixes(self):
        assert hs.norm("José De'Andre O’Neil Jr.") == hs.norm("Jose DeAndre ONeil")
        assert hs.norm("Marvin Harrison II") == hs.norm("marvin harrison")

    def test_a_unique_name_on_the_team_matches(self):
        idx = _index([{"player_id": 1, "name": "Kaden Anderson", "position_group": "QB"}])
        assert [p["player_id"] for p in hs.candidates_for(idx, {"fullName": "Kaden Anderson"})] == [1]

    def test_a_name_that_is_not_on_the_team_does_not_match(self):
        idx = _index([{"player_id": 1, "name": "Kaden Anderson", "position_group": "QB"}])
        assert hs.candidates_for(idx, {"fullName": "Kaden Andersen"}) == []

    def test_two_players_with_one_name_are_told_apart_by_position(self):
        idx = _index([{"player_id": 1, "name": "Chris Jones", "position_group": "WR"},
                      {"player_id": 2, "name": "Chris Jones", "position_group": "CB"}])
        got = hs.candidates_for(idx, {"fullName": "Chris Jones", "position": {"abbreviation": "DB"}})
        assert [p["player_id"] for p in got] == [2]

    def test_two_players_with_one_name_and_a_position_that_fits_both_are_left_unmatched(self):
        idx = _index([{"player_id": 1, "name": "Chris Jones", "position_group": "CB"},
                      {"player_id": 2, "name": "Chris Jones", "position_group": "S"}])
        got = hs.candidates_for(idx, {"fullName": "Chris Jones", "position": {"abbreviation": "DB"}})
        assert len(got) == 2          # not exactly one: the caller leaves him unmatched


@pytest.mark.skipif(not (APP_DATA / "headshots.json").exists(), reason="no headshots.json")
class TestExportedMapping:
    @pytest.fixture(scope="class")
    def doc(self):
        return json.loads((APP_DATA / "headshots.json").read_text(encoding="utf-8"))

    def test_shape(self, doc):
        assert doc["source"] == "ESPN" and "{id}" in doc["image"] and doc["season"] == 2026
        assert all(k.isdigit() and v.isdigit() for k, v in doc["players"].items())

    def test_every_mapped_player_is_on_a_2026_roster(self, doc):
        on = {str(p["player_id"]) for v in json.loads((APP_DATA / "rosters_2026.json").read_text(encoding="utf-8")).values() for p in v}
        assert set(doc["players"]) <= on

    def test_no_photo_belongs_to_two_players(self, doc):
        ids = list(doc["players"].values())
        assert len(ids) == len(set(ids))

    def test_most_rated_current_players_have_a_photo(self, doc):
        """The gate for showing photos: below 75% of the players the app rates, the card would be mostly blanks."""
        rosters = json.loads((APP_DATA / "rosters_2026.json").read_text(encoding="utf-8"))
        rated = [p for v in rosters.values() for p in v
                 if p.get("live_rating") is not None or p.get("overall_rating") is not None or p.get("rating_prev") is not None]
        share = sum(1 for p in rated if str(p["player_id"]) in doc["players"]) / len(rated)
        assert share >= 0.75, f"only {share:.1%} of rated 2026 players have a photo"
