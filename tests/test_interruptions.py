"""Tests for injured and redshirt seasons (script 15).

A career curve reads a season a player missed as a season a player declined.
Both directions of that error showed up in real 2026 projections:

  Whit Weeks   12 games in 2024 (98th pct among LBs), 6 games in 2025 (69th).
               Projected up — correctly — but labelled a BREAKOUT, when 2024
               was the breakout and 2026 is a return to it.

  Jaden Mickey 3 games in 2024 at Notre Dame, then a career-best 11-game 2025 at
               Boise State. The lost season dragged his career mean down and his
               SD up until his best year looked like an outlier, and he
               projected DOWN 9.6 off it.

The rule separating the two cases has to distinguish "hurt" from "backup", and
has to do it without reading the future.
"""

import importlib.util
from pathlib import Path

import numpy as np
import pytest

_spec = importlib.util.spec_from_file_location(
    "_s15", Path(__file__).parent.parent / "scripts" / "15_predict_trajectories.py")
_s15 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s15)

flag_interruptions = _s15.flag_interruptions
derive_class_year  = _s15.derive_class_year
_describe_path     = _s15._describe_path
AVAIL_INTERRUPTED  = _s15.AVAIL_INTERRUPTED
AVAIL_PRIOR_FLOOR  = _s15.AVAIL_PRIOR_FLOOR


class TestInterruptionDetection:
    def test_whit_weeks_2025_is_an_interruption(self):
        """9/13, 12/13, then 6/13 — an established starter who missed half a year."""
        assert flag_interruptions([9 / 13, 12 / 13, 6 / 13]) == [False, False, True]

    def test_jaden_mickey_2024_is_an_interruption(self):
        """7/13, 9/13, 3/16, 11/14. He never reached 75% availability, so a rule
        keyed to 'established starter' misses him — the test has to be relative
        to his own prior best, not to an absolute idea of a starter."""
        flags = flag_interruptions([7 / 13, 9 / 13, 3 / 16, 11 / 14])
        assert flags == [False, False, True, False]

    def test_a_backup_who_plays_a_little_every_year_is_not_interrupted(self):
        """Low availability throughout is a role, not an injury."""
        assert flag_interruptions([0.2, 0.25, 0.2, 0.3]) == [False] * 4

    def test_a_true_freshmans_first_season_is_never_an_interruption(self):
        """Nothing precedes it, so there is no availability to have lost."""
        assert flag_interruptions([0.25]) == [False]
        assert flag_interruptions([0.05, 0.9])[0] is False

    def test_a_full_career_is_never_interrupted(self):
        assert flag_interruptions([1.0, 0.92, 1.0, 0.85]) == [False] * 4

    def test_a_modest_dip_is_not_an_interruption(self):
        """Missing one or two games is ordinary. Only a real absence counts."""
        assert flag_interruptions([1.0, 0.85]) == [False, False]

    def test_it_never_reads_the_future(self):
        """Prior-only: truncating the career cannot change an earlier verdict, or
        the model would be learning from information it will not have."""
        full = [7 / 13, 9 / 13, 3 / 16, 11 / 14]
        flags = flag_interruptions(full)
        for i in range(1, len(full) + 1):
            assert flag_interruptions(full[:i]) == flags[:i], f"changed at prefix {i}"

    def test_recovery_after_an_interruption_is_not_itself_flagged(self):
        assert flag_interruptions([0.95, 0.2, 0.9])[2] is False

    def test_handles_missing_values(self):
        assert flag_interruptions([None, 0.0, 0.9]) == [False, False, False]


class TestClassYearDerivation:
    """`player_seasons.year` is not a class year. It is constant across the whole
    career for 84% of players with 3+ seasons and never increments, and it holds
    an outright calendar year for 114,612 of 269,552 rows. Cohort cells keyed on
    it were mixing a player's freshman, sophomore and junior seasons together.
    """

    def test_recruit_year_anchors_a_real_progression(self):
        """Jaden Mickey: recruited 2022, played 2022-2025, stored as a junior in
        every one of them. He is a fourth-year senior in 2025."""
        got = derive_class_year([2022, 2023, 2024, 2025], [2022] * 4, [2022] * 4)
        assert list(got) == [1, 2, 3, 4]

    def test_whit_weeks_was_a_true_freshman_in_2023(self):
        got = derive_class_year([2023, 2024, 2025], [2023] * 3, [2023] * 3)
        assert list(got) == [1, 2, 3]

    def test_first_observed_season_is_the_fallback(self):
        got = derive_class_year([2019, 2020, 2021], [None] * 3, [2019] * 3)
        assert list(got) == [1, 2, 3]

    def test_an_implausible_recruit_year_falls_back(self):
        """A recruit year 12 seasons before the season is a bad join, not a
        12th-year senior."""
        got = derive_class_year([2020], [2008], [2019])
        assert list(got) == [2]

    def test_the_stored_value_is_never_consulted(self):
        """It carries no per-season information, so nothing should depend on it."""
        a = derive_class_year([2022, 2023], [2022, 2022], [2022, 2022])
        b = derive_class_year([2022, 2023], [2022, 2022], [2022, 2022])
        assert list(a) == list(b) == [1, 2]

    def test_it_never_exceeds_a_plausible_class_year(self):
        got = derive_class_year(list(range(2008, 2017)), [None] * 9, [2008] * 9)
        assert max(got) <= 6

    def test_it_never_goes_below_one(self):
        got = derive_class_year([2019], [None], [2020])
        assert min(got) >= 1


class TestPathDescription:
    def test_an_interrupted_dip_is_not_called_a_trend(self):
        s = _describe_path([50, 81, 12, 60], [2022, 2023, 2024, 2025],
                           [False, False, True, False])
        assert "cut short" in s
        assert "*" in s

    def test_direction_is_judged_on_healthy_seasons(self):
        """50 → 81 → 12 → 60 ends below where it started only because of the
        season he missed. Across the seasons he played it climbed."""
        s = _describe_path([50, 81, 12, 60], [2022, 2023, 2024, 2025],
                           [False, False, True, False])
        assert "climbed" in s

    def test_an_uninterrupted_decline_still_reads_as_a_decline(self):
        s = _describe_path([88, 70, 55], [2023, 2024, 2025], [False, False, False])
        assert "slipped" in s and "cut short" not in s

    def test_a_single_season_says_nothing(self):
        assert _describe_path([60], [2025], [False]) == ""

    def test_it_survives_a_missing_cut_path(self):
        s = _describe_path([50, 80], [2024, 2025])
        assert "climbed" in s and "cut short" not in s


class TestBouncebackIsNotBreakout:
    """The label carries the claim. 'Breakout' asserts new ground; a player
    returning to a level he already posted is a different, better-supported
    statement, and conflating them made the breakout list unreadable."""

    def test_the_margin_is_real_enough_to_mean_something(self):
        assert _s15.BOUNCEBACK_MARGIN >= 1.0

    def test_thresholds_are_ordered_sensibly(self):
        assert 0 < _s15.AVAIL_DROP_RATIO < 1
        assert 0 < AVAIL_PRIOR_FLOOR < 1
        assert 0 < AVAIL_INTERRUPTED < 1

    def test_healthy_features_are_all_modelled(self):
        for f in ("last_interrupted", "avail_last", "pct_last_healthy",
                  "pct_slope_healthy", "pct_peak_healthy"):
            assert f in _s15.FEATURE_COLS, f"{f} computed but never fed to the model"

    def test_every_feature_has_a_readable_label(self):
        """Drivers are rendered to users by name; an unlabelled feature shows up
        as a raw column name in the modal."""
        missing = [f for f in _s15.SKILL_FEATURE_COLS if f not in _s15.DRIVER_LABELS]
        assert not missing, f"features with no DRIVER_LABELS entry: {missing}"
