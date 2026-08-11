"""Tests for the coverage-denial credit (script 06, CB/S/DB).

A defensive back's best games leave no stat line: quarterbacks stop throwing at a
corner who covers. The counting stats every other position's score is built from
therefore measure the opposite of what we want here, and Caleb Downs — whom no credible
top-five safety list omits — rated 22nd among safeties.

The credit that fixes it has three properties worth locking down, because each one
was arrived at by getting it wrong first.
"""

import importlib.util
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_spec = importlib.util.spec_from_file_location(
    "_s06", Path(__file__).parent.parent / "scripts" / "06_compute_edge_scores.py")
_s06 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s06)

denial_from_ypa              = _s06.denial_from_ypa
_pass_attempts               = _s06._pass_attempts
build_coverage_participation = _s06.build_coverage_participation
COVERAGE_CREDIT              = _s06.COVERAGE_CREDIT
COVERAGE_POSITIONS           = _s06.COVERAGE_POSITIONS
ARCHETYPE_SCALE              = _s06.ARCHETYPE_SCALE
_archetype_raws              = _s06._archetype_raws
SECONDARY_ARCHETYPE_WEIGHTS  = _s06.SECONDARY_ARCHETYPE_WEIGHTS


def _ypa(n=40, lo=5.5, hi=9.0):
    return {i: lo + (hi - lo) * i / (n - 1) for i in range(n)}


class TestPassDenial:
    def test_stingy_defense_earns_credit_and_porous_one_earns_none(self):
        d = denial_from_ypa(_ypa())
        assert d[0] > 0.0          # best YPA allowed
        assert d[39] == 0.0        # worst

    def test_most_defenses_earn_something(self):
        """The 2-SD rule this replaced left half the country at exactly zero, so
        most defensive backs got no coverage signal at all."""
        d = denial_from_ypa(_ypa())
        assert sum(v > 0 for v in d.values()) / len(d) >= 0.6

    def test_credit_never_goes_negative(self):
        """Credit only. The multiplicative context modifier already carries downside;
        charging a DB twice would bury the players holding a bad defense together."""
        assert all(0.0 <= v <= 1.0 for v in denial_from_ypa(_ypa()).values())

    def test_needs_enough_teams_to_standardize(self):
        assert denial_from_ypa({0: 5.5, 1: 9.0}) == {}

    def test_a_better_defense_never_earns_less(self):
        d = denial_from_ypa(_ypa())
        vals = [d[i] for i in sorted(d)]
        assert all(a >= b for a, b in zip(vals, vals[1:]))


class TestAttemptParsing:
    def test_reads_attempts_not_completions(self):
        """Game rows store "25/38" — completions/attempts. Taking the first field
        (which the generic coercion does) silently zeroed the whole signal."""
        assert _pass_attempts({"passingC/ATT": "25/38"}) == 38

    def test_falls_back_to_a_plain_attempts_key(self):
        assert _pass_attempts({"passingATT": 31}) == 31

    def test_garbage_is_zero_not_an_exception(self):
        assert _pass_attempts({"passingC/ATT": "—"}) == 0
        assert _pass_attempts({}) == 0


class TestSecondaryComposite:
    """A defensive back's overall IS his three archetypes, weighted by position."""

    def test_every_secondary_position_has_weights(self):
        assert set(SECONDARY_ARCHETYPE_WEIGHTS) == COVERAGE_POSITIONS

    def test_weights_sum_to_one(self):
        for pg, w in SECONDARY_ARCHETYPE_WEIGHTS.items():
            assert sum(w.values()) == pytest.approx(1.0), f"{pg} weights sum to {sum(w.values())}"

    def test_weights_cover_exactly_the_three_archetypes(self):
        for pg, w in SECONDARY_ARCHETYPE_WEIGHTS.items():
            assert set(w) == set(ARCHETYPE_SCALE), f"{pg} weights {set(w)}"

    def test_corners_are_paid_to_cover_safeties_to_tackle(self):
        cb, s = SECONDARY_ARCHETYPE_WEIGHTS["CB"], SECONDARY_ARCHETYPE_WEIGHTS["S"]
        assert cb["coverage"] > cb["run_support"]
        assert s["run_support"] > s["coverage"]

    def test_the_three_share_one_axis(self):
        """Scale constants must be the same order of magnitude or one archetype
        cannot influence the overall. Stale constants once capped coverage at 7.1
        on a 0-10 axis while run support reached 20."""
        vals = list(ARCHETYPE_SCALE.values())
        assert max(vals) / min(vals) < 2.5, f"archetype scales are lopsided: {ARCHETYPE_SCALE}"


class TestArchetypes:
    def test_tackles_count_only_for_run_support(self):
        """A corner nobody throws at makes few tackles. They are evidence of
        playing time, not of ball skills or coverage."""
        a = _archetype_raws("CB", {"defensiveTOT": 60}, credit=0.0)
        assert a["run_support"] > 0
        assert a["ball_hawk"] == 0
        assert a["coverage"] == 0

    def test_ball_hawk_is_interceptions_and_breakups(self):
        a = _archetype_raws("CB", {"interceptionsINT": 4, "defensivePD": 10}, credit=0.0)
        assert a["ball_hawk"] > 0 and a["run_support"] == 0

    def test_coverage_is_the_credit_and_nothing_else(self):
        a = _archetype_raws("CB", {"defensiveTOT": 40, "interceptionsINT": 2}, credit=3.1)
        assert a["coverage"] == pytest.approx(3.1)

    def test_all_three_exist_and_are_positive(self):
        assert set(ARCHETYPE_SCALE) == {"ball_hawk", "coverage", "run_support"}
        assert all(v > 0 for v in ARCHETYPE_SCALE.values())


class TestParticipation:
    def _rows(self, spec):
        return pd.DataFrame([
            {"position_group": pg, "player_team_id": t, "player_id": p,
             "data": {"defensiveTOT": tot}}
            for t, p, pg, tot in spec
        ])

    def test_full_time_starter_saturates_at_one(self):
        rows = self._rows([(1, 10, "CB", 60), (1, 11, "CB", 55), (1, 12, "S", 70),
                           (1, 13, "S", 50), (1, 14, "DB", 40)])
        part = build_coverage_participation(rows)
        assert part[(1, 10)] == pytest.approx(1.0)

    def test_deep_reserve_earns_almost_nothing(self):
        rows = self._rows([(1, 10, "CB", 90), (1, 11, "CB", 85), (1, 12, "S", 80),
                           (1, 13, "S", 75), (1, 14, "DB", 2)])
        part = build_coverage_participation(rows)
        assert part[(1, 14)] < 0.15
        assert part[(1, 10)] > part[(1, 14)]

    def test_a_covered_corner_is_not_docked_for_tackles_he_never_had_to_make(self):
        """Tackle share saturates well below an even split, so a corner nobody
        throws at still counts as full-time. Using raw share would re-import the
        exact suppression this credit exists to correct."""
        rows = self._rows([(1, 10, "CB", 30), (1, 11, "CB", 75), (1, 12, "S", 85),
                           (1, 13, "S", 80), (1, 14, "DB", 70)])
        part = build_coverage_participation(rows)
        assert part[(1, 10)] == pytest.approx(1.0)

    def test_only_defensive_backs_participate(self):
        rows = self._rows([(1, 10, "CB", 50), (1, 20, "LB", 120), (1, 21, "EDGE", 40)])
        part = build_coverage_participation(rows)
        assert (1, 20) not in part and (1, 21) not in part


class TestCoverageShareSurvivesToTheUI:
    """The credit is applied inside script 06's composite, so the *rating* is right
    whether or not coverage_share travels. What breaks silently is the explanation:
    a defensive back gets a bump his box score doesn't justify and the modal has
    nothing to say about it. Script 07 rebuilds a fresh feature dict per row, so a
    column that isn't explicitly copied vanishes with no error anywhere.
    """

    def test_script_07_carries_coverage_share_into_its_feature_dict(self):
        src = (Path(__file__).parent.parent / "scripts"
               / "07_compute_player_ratings.py").read_text(encoding="utf-8")
        assert 'feats["coverage_share"]' in src, \
            "script 07 drops coverage_share when rebuilding features per row"

    def test_script_07_puts_it_in_the_contributions(self):
        src = (Path(__file__).parent.parent / "scripts"
               / "07_compute_player_ratings.py").read_text(encoding="utf-8")
        assert 'contrib["coverage_share"]' in src

    def test_script_12_exports_it(self):
        src = (Path(__file__).parent.parent / "scripts"
               / "12_export_frontend_json.py").read_text(encoding="utf-8")
        assert '"coverage_share":' in src, \
            "script 12 merges coverage_share but never emits it"


class TestTheFrontendPrintsTheSameWeights:
    """A defensive back's overall IS his three archetypes, so the modal prints the
    weights beside the sub-ratings. Those weights are a hand-kept copy in the other
    repo — nothing at runtime reconciles them, and a UI that itemises a rating with
    the wrong weights is worse than one that doesn't itemise it at all.
    """

    APP_JS = (Path(__file__).parent.parent.parent
              / "cfb-analytics-app" / "js" / "playerSearch.js")

    def test_weights_match_script_06(self):
        if not self.APP_JS.exists():
            pytest.skip("cfb-analytics-app not present in this checkout")
        src = self.APP_JS.read_text(encoding="utf-8")
        block = re.search(r"ARCHETYPE_WEIGHTS\s*=\s*\{(.*?)\n\};", src, re.S)
        assert block, "playerSearch.js has no ARCHETYPE_WEIGHTS"

        for pg, want in SECONDARY_ARCHETYPE_WEIGHTS.items():
            row = re.search(rf"\b{pg}:\s*\{{([^}}]*)\}}", block.group(1))
            assert row, f"playerSearch.js ARCHETYPE_WEIGHTS has no {pg}"
            got = {}
            for k, v in re.findall(r"(\w+):\s*([\d./\s]+?)(?:,|$)", row.group(1)):
                got[k] = eval(v.strip(), {"__builtins__": {}})  # noqa: S307 — "1 / 3"
            assert set(got) == set(want), f"{pg}: UI lists {set(got)}, script 06 {set(want)}"
            for k in want:
                assert got[k] == pytest.approx(want[k], abs=1e-6), (
                    f"{pg}.{k}: UI says {got[k]}, script 06 says {want[k]}")


class TestCreditShape:
    def test_corners_are_credited_most_and_safeties_least(self):
        """Run support keeps a safety's volume closer to honest, so less of his
        value is invisible."""
        assert COVERAGE_CREDIT["CB"] > COVERAGE_CREDIT["DB"] > COVERAGE_CREDIT["S"]

    def test_only_the_secondary_is_credited(self):
        assert COVERAGE_POSITIONS == {"CB", "S", "DB"}
        assert not COVERAGE_POSITIONS & {"EDGE", "DL", "LB"}

    def test_credit_is_material_against_a_typical_starters_composite(self):
        """A starting corner earns roughly 3.2/game from counting stats. If the
        credit cannot move that, it cannot fix the suppression it targets — the
        multiplicative modifier that preceded it topped out at +10%."""
        assert COVERAGE_CREDIT["CB"] >= 3.0
