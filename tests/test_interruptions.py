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
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.class_year import derive_class_year          # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_s15", Path(__file__).parent.parent / "scripts" / "15_predict_trajectories.py")
_s15 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s15)

flag_interruptions = _s15.flag_interruptions
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
    career for 84.0% of players with 3+ seasons that carry a plausible value,
    never increments, and holds an outright calendar year for 114,612 of 269,552
    rows. Cohort cells keyed on it were mixing a player's freshman, sophomore and
    junior seasons together.

    The derivation moved to `utils/class_year.py` in v4.6 so scripts 07, 12, 15
    and 16 read one value, and its contract changed in one deliberate way: it
    now returns **NaN for a class year it cannot determine**, and the anchor that
    produced each value beside it. It used to clip the floor anchor into [1, 6]
    unconditionally, which handed every left-censored career a class year of 1 —
    the true-freshman cell, and the one with the largest recruiting prior.
    """

    def test_recruit_year_anchors_a_real_progression(self):
        """Jaden Mickey: recruited 2022, played 2022-2025, stored as a junior in
        every one of them. He is a fourth-year senior in 2025."""
        cy, basis = derive_class_year([2022, 2023, 2024, 2025], [2022] * 4, [2022] * 4)
        assert list(cy) == [1, 2, 3, 4]
        assert list(basis) == ["recruiting"] * 4

    def test_whit_weeks_was_a_true_freshman_in_2023(self):
        cy, _ = derive_class_year([2023, 2024, 2025], [2023] * 3, [2023] * 3)
        assert list(cy) == [1, 2, 3]

    def test_first_observed_season_is_the_fallback_and_says_so(self):
        """A floor, not a truth — so it is labelled, not silently mixed in with
        the anchored values. Where both anchors apply they agree exactly 74.0% of
        the time, and the floor is too YOUNG 23.9% of the time against too old
        2.1%."""
        cy, basis = derive_class_year([2019, 2020, 2021], [None] * 3, [2019] * 3)
        assert list(cy) == [1, 2, 3]
        assert list(basis) == ["first_fbs"] * 3

    def test_an_implausible_recruit_year_falls_back(self):
        """A recruit year 12 seasons before the season is a bad join, not a
        12th-year senior."""
        cy, basis = derive_class_year([2020], [2008], [2019])
        assert list(cy) == [2]
        assert list(basis) == ["first_fbs"]

    def test_the_stored_value_is_never_consulted(self):
        """It carries no per-season information, so nothing should depend on it.
        The signature has no parameter for it."""
        a, _ = derive_class_year([2022, 2023], [2022, 2022], [2022, 2022])
        b, _ = derive_class_year([2022, 2023], [2022, 2022], [2022, 2022])
        assert list(a) == list(b) == [1, 2]

    def test_it_never_exceeds_a_plausible_class_year(self):
        cy, _ = derive_class_year([2019, 2020, 2021], [2016] * 3, [2016] * 3)
        assert list(cy) == [4, 5, 6]
        cy, _ = derive_class_year([2022], [2016], [2016])
        assert np.all(np.isnan(cy)), "a seventh year is a bad join, not a class year"

    # ---- the unknown branch (v4.6) ----------------------------------------

    def test_a_left_censored_career_is_unknown_not_a_freshman(self):
        """His first observed season is the first season in the archive, so we
        cannot tell a true freshman from a fifth-year senior who enrolled in 2004.
        Answering 1 would hand him the full true-freshman recruiting prior."""
        cy, basis = derive_class_year([2008, 2009], [None, None], [2008, 2008])
        assert np.all(np.isnan(cy))
        assert list(basis) == ["unknown", "unknown"]

    def test_a_recruiting_anchor_beats_left_censoring(self):
        """Censoring only blocks the FLOOR anchor. A real enrolment date still
        dates him, even at the edge of the archive."""
        cy, basis = derive_class_year([2008], [2006], [2008])
        assert list(cy) == [3]
        assert list(basis) == ["recruiting"]

    def test_a_season_before_the_first_observed_one_is_unknown(self):
        """A negative class year is a broken join. It used to clip to 1."""
        cy, basis = derive_class_year([2019], [None], [2020])
        assert np.all(np.isnan(cy))
        assert list(basis) == ["unknown"]

    def test_a_career_longer_than_six_years_is_unknown_not_clipped(self):
        """Clipping produced a sixth-year senior out of a player whose first
        observed season is nine years back, which is a data problem wearing a
        plausible number."""
        cy, basis = derive_class_year([2020], [None], [2010])
        assert np.all(np.isnan(cy))
        assert list(basis) == ["unknown"]

    def test_unknown_is_nan_and_never_zero(self):
        """Missing means unknown, never zero — the rule that outranks the rest of
        this module. A 0 would sort below every real class year and read as a
        value."""
        cy, _ = derive_class_year([2019], [None], [2025])
        assert np.isnan(cy[0])


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
