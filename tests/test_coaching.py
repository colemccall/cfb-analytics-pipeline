"""Who was the head coach — the definition behind the coaching event study.

"Changed coach" is a definition, not a lookup, and the two ways it goes wrong are
both silent: a midseason interim displacing the man who coached the other ten
games, and the first season of our record reading as a change for all 120 teams.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import coaching  # noqa: E402


def fake_coaches(rows):
    return pd.DataFrame(rows)


@pytest.fixture
def two_teams(monkeypatch):
    rows = [
        # Team 1: Alpha 2019-2020, Beta 2021-2022
        {"coach_id": 1, "coach_name": "Alpha", "team_id": 1, "season": 2019, "games": 12},
        {"coach_id": 1, "coach_name": "Alpha", "team_id": 1, "season": 2020, "games": 11},
        {"coach_id": 2, "coach_name": "Beta",  "team_id": 1, "season": 2021, "games": 12},
        {"coach_id": 2, "coach_name": "Beta",  "team_id": 1, "season": 2022, "games": 13},
        # Team 2: Beta arrives in 2023 after leaving team 1 — a second stint
        {"coach_id": 2, "coach_name": "Beta",  "team_id": 2, "season": 2023, "games": 12},
        {"coach_id": 2, "coach_name": "Beta",  "team_id": 2, "season": 2024, "games": 12},
    ]
    monkeypatch.setattr(coaching, "read_raw", lambda t: fake_coaches(rows))
    return rows


class TestHeadCoachOfRecord:
    def test_plurality_of_games_wins(self, monkeypatch):
        """A two-game interim must not displace the man who coached the other ten."""
        monkeypatch.setattr(coaching, "read_raw", lambda t: fake_coaches([
            {"coach_id": 1, "coach_name": "Starter", "team_id": 1, "season": 2022, "games": 10},
            {"coach_id": 2, "coach_name": "Interim", "team_id": 1, "season": 2022, "games": 2},
        ]))
        hc = coaching.head_coach_by_team_season()
        assert hc[(1, 2022)]["coach_name"] == "Starter"

    def test_one_entry_per_team_season(self, two_teams):
        hc = coaching.head_coach_by_team_season()
        assert len(hc) == 6
        assert hc[(1, 2021)]["coach_name"] == "Beta"

    def test_missing_table_is_empty_not_an_error(self, monkeypatch):
        monkeypatch.setattr(coaching, "read_raw", lambda t: pd.DataFrame())
        assert coaching.head_coach_by_team_season() == {}


class TestCoachingChanges:
    def test_detects_a_real_change(self, two_teams):
        assert coaching.coaching_changes(2021) == {1}

    def test_a_continuing_coach_is_not_a_change(self, two_teams):
        assert coaching.coaching_changes(2022) == set()

    def test_the_first_season_on_record_is_not_a_change(self, two_teams):
        """Otherwise every team in 2008 is flagged, and the event study is noise."""
        assert coaching.coaching_changes(2019) == set()

    def test_a_new_school_is_not_a_change_for_that_school(self, two_teams):
        """Team 2's 2023 is its first season on record, not a coaching change."""
        assert 2 not in coaching.coaching_changes(2023)

    def test_falls_back_to_the_legacy_table(self, monkeypatch):
        """A clone without script 09 still runs — it just knows less."""
        def fake(table):
            if table == "coaches":
                return pd.DataFrame()
            return pd.DataFrame([
                {"team_id": 7, "role": "HC", "start_season": 2024},
                {"team_id": 8, "role": "S&C", "start_season": 2024},
            ])
        monkeypatch.setattr(coaching, "read_raw", fake)
        assert coaching.coaching_changes(2024) == {7}


class TestTenures:
    def test_splits_stints_by_school(self, two_teams):
        t = coaching.coach_tenures()
        beta = sorted([s for s in t if s["coach"] == "beta"],
                      key=lambda s: s["first_season"])
        assert len(beta) == 2, "the same coach at two schools is two stints"
        assert beta[0]["team_id"] == 1 and beta[0]["seasons"] == 2
        assert beta[1]["team_id"] == 2 and beta[1]["first_season"] == 2023

    def test_a_gap_breaks_a_stint(self, monkeypatch):
        """A coach who returns after time away is two stints. Averaging across
        the gap would blur the very transition the event study looks for."""
        monkeypatch.setattr(coaching, "read_raw", lambda t: fake_coaches([
            {"coach_id": 1, "coach_name": "A", "team_id": 1, "season": 2010, "games": 12},
            {"coach_id": 1, "coach_name": "A", "team_id": 1, "season": 2011, "games": 12},
            {"coach_id": 1, "coach_name": "A", "team_id": 1, "season": 2020, "games": 12},
        ]))
        stints = coaching.coach_tenures()
        assert len(stints) == 2
        assert {s["seasons"] for s in stints} == {1, 2}

    def test_first_and_last_season_bracket_the_stint(self, two_teams):
        alpha = [s for s in coaching.coach_tenures() if s["coach"] == "alpha"][0]
        assert alpha["first_season"] == 2019
        assert alpha["last_season"] == 2020
