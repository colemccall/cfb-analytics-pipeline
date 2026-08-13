"""Contract tests for everything the pipeline writes into cfb-analytics-app/data/.

These guard the seam between the two repos, which has broken twice in ways no
unit test could see:

  · a literal NaN token reaching disk, which is invalid JSON and kills the
    browser's fetch().json() for the whole file;
  · the frontend's CURRENT_SEASON drifting from the pipeline's (2025 vs 2026),
    so pages asked for a season that had never been exported;
  · orphaned exports (rosters.json, schedules.json — 71 MB, zero consumers)
    sitting in the repo long after their consumers were deleted.

The suite is skipped rather than failed when the app directory is absent, so a
pipeline-only checkout still runs green.
"""

import json
import re
from pathlib import Path

import pytest

APP_DATA = Path(__file__).parent.parent.parent / "cfb-analytics-app" / "data"
APP_JS = Path(__file__).parent.parent.parent / "cfb-analytics-app" / "js" / "config.js"

pytestmark = pytest.mark.skipif(
    not APP_DATA.exists(), reason="cfb-analytics-app/data not present in this checkout"
)


def _load(name):
    with open(APP_DATA / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def manifest():
    path = APP_DATA / "manifest.json"
    if not path.exists():
        pytest.skip("manifest.json not exported yet — run script 12")
    return _load("manifest.json")


class TestStrictParse:
    """Every exported file must be valid, strictly-parseable JSON."""

    def test_all_files_parse(self):
        bad = []
        for p in sorted(APP_DATA.rglob("*.json")):
            try:
                with open(p, encoding="utf-8") as f:
                    json.load(f)
            except Exception as e:
                bad.append(f"{p.name}: {e}")
        assert not bad, "unparseable exports:\n" + "\n".join(bad)

    def test_no_nan_token(self):
        """`NaN` is what Python emits and JavaScript rejects."""
        offenders = []
        for p in sorted(APP_DATA.rglob("*.json")):
            text = p.read_text(encoding="utf-8")
            if re.search(r"(?<![\"\w])(NaN|Infinity|-Infinity)(?![\"\w])", text):
                offenders.append(p.name)
        assert not offenders, f"literal NaN/Infinity in: {offenders}"


class TestNoOrphans:
    """Season-agnostic duplicates of per-season files were pure dead weight."""

    @pytest.mark.parametrize("name", ["rosters.json", "schedules.json", "players.json",
                                      "transfers.json", "similar_players.json"])
    def test_bare_duplicates_absent(self, name):
        assert not (APP_DATA / name).exists(), (
            f"{name} is a season-agnostic duplicate with no consumer — "
            f"the per-season files are authoritative"
        )


class TestManifest:
    def test_required_keys(self, manifest):
        for k in ["generated_at", "first_season", "last_played_season",
                  "current_season", "projected_seasons", "seasons"]:
            assert k in manifest, f"manifest.json missing {k}"

    def test_seasons_are_contiguous(self, manifest):
        s = manifest["seasons"]
        assert s == list(range(manifest["first_season"], manifest["current_season"] + 1))

    def test_projected_seasons_are_after_last_played(self, manifest):
        for s in manifest["projected_seasons"]:
            assert s > manifest["last_played_season"], (
                f"season {s} is marked projected but is not after "
                f"last_played_season {manifest['last_played_season']}"
            )

    def test_every_season_has_a_players_file(self, manifest):
        missing = [s for s in manifest["seasons"]
                   if not (APP_DATA / f"players_{s}.json").exists()]
        assert not missing, f"seasons in manifest with no players file: {missing}"

    def test_frontend_season_constants_agree(self, manifest):
        """The split-brain guard.

        config.js is loaded synchronously before any fetch, so it cannot read
        manifest.json at runtime. A test is what keeps the two in step.
        """
        if not APP_JS.exists():
            pytest.skip("config.js not present")
        src = APP_JS.read_text(encoding="utf-8")

        def const(name):
            m = re.search(rf"{name}:\s*(\d{{4}})", src)
            return int(m.group(1)) if m else None

        assert const("FIRST_SEASON") == manifest["first_season"], (
            "config.js FIRST_SEASON disagrees with manifest.json"
        )
        assert const("CURRENT_SEASON") == manifest["current_season"], (
            "config.js CURRENT_SEASON disagrees with manifest.json"
        )
        assert const("LAST_PLAYED_SEASON") == manifest["last_played_season"], (
            "config.js LAST_PLAYED_SEASON disagrees with manifest.json"
        )

        m = re.search(r"PROJECTED_SEASONS:\s*\[([^\]]*)\]", src)
        assert m, "config.js has no PROJECTED_SEASONS"
        js_projected = [int(x) for x in re.findall(r"\d{4}", m.group(1))]
        assert js_projected == manifest["projected_seasons"], (
            f"config.js PROJECTED_SEASONS {js_projected} != "
            f"manifest {manifest['projected_seasons']}"
        )


class TestProvenance:
    """A projected rating must never be able to pass as an earned one."""

    def test_projected_seasons_carry_provenance(self, manifest):
        """Every row says what it is. "not_projected" is a legitimate answer —
        offensive linemen are exported so EA's opinion can be shown beside our
        refusal — but it must come with no rating of ours attached."""
        for s in manifest["projected_seasons"]:
            players = _load(f"players_{s}.json")
            assert players, f"players_{s}.json is empty"
            unmarked = [p for p in players
                        if p.get("provenance") not in ("projected", "not_projected")]
            assert not unmarked, (
                f"{len(unmarked)} rows in players_{s}.json lack a provenance "
                f'(e.g. {unmarked[0].get("name")})'
            )
            leaked = [p for p in players
                      if p.get("provenance") == "not_projected"
                      and p.get("overall_rating") is not None]
            assert not leaked, (
                f'{len(leaked)} rows are marked "not_projected" yet carry a '
                f'rating (e.g. {leaked[0].get("name")} = {leaked[0].get("overall_rating")})'
            )

    def test_projected_rows_name_their_source(self, manifest):
        valid = {"engine_d", "carry", "recruiting", "ea_cfb27"}
        for s in manifest["projected_seasons"]:
            for p in _load(f"players_{s}.json"):
                if p.get("overall_rating") is None:
                    continue          # nothing was projected, so nothing to source
                assert p.get("projection_source") in valid, (
                    f"{p.get('name')} has projection_source={p.get('projection_source')!r}"
                )

    def test_played_seasons_are_not_marked_projected(self, manifest):
        s = manifest["last_played_season"]
        marked = [p for p in _load(f"players_{s}.json") if p.get("provenance") == "projected"]
        assert not marked, f"{len(marked)} rows in played season {s} marked projected"

    def test_intervals_bracket_the_estimate(self, manifest):
        for s in manifest["projected_seasons"]:
            for p in _load(f"players_{s}.json"):
                lo, hi, ovr = p.get("projection_low"), p.get("projection_high"), p.get("overall_rating")
                if lo is None or hi is None or ovr is None:
                    continue
                assert lo <= ovr <= hi, f"{p.get('name')}: {lo} <= {ovr} <= {hi} violated"


class TestRosterIntegrity:
    def test_no_player_on_two_teams_in_one_season(self, manifest):
        """A player plays for one team in a season.

        Violated for 4,121 players until Aug 2026: the harvest upsert was keyed
        on (player, season, team) and only ever added, so a corrected team left
        the wrong row behind forever.
        """
        for s in [manifest["last_played_season"], manifest["current_season"]]:
            rosters = _load(f"rosters_{s}.json")
            seen = {}
            dupes = []
            for team_id, players in rosters.items():
                for p in players:
                    pid = p.get("player_id")
                    if pid is None:
                        continue
                    if pid in seen and seen[pid] != team_id:
                        dupes.append((p.get("name"), seen[pid], team_id))
                    seen[pid] = team_id
            assert not dupes, f"season {s}: players on multiple rosters: {dupes[:5]}"


class TestSchedules:
    def test_opponents_are_named(self, manifest):
        """FCS opponents have no team_id of ours, but they do have names.

        Rendering them as "TBD" told the user a known fixture was undecided.
        """
        s = manifest["current_season"]
        unnamed = []
        for team_id, games in _load(f"schedules_{s}.json").items():
            for g in games:
                if not g.get("opponent"):
                    unnamed.append((team_id, g.get("week")))
        assert not unnamed, f"{len(unnamed)} schedule entries with no opponent name"

    def test_non_fbs_opponents_are_flagged(self, manifest):
        """The UI needs to know not to link an opponent with no team page."""
        s = manifest["current_season"]
        for games in _load(f"schedules_{s}.json").values():
            for g in games:
                assert "opp_is_fbs" in g, "schedule entry missing opp_is_fbs"


class TestTrajectory:
    @pytest.fixture(scope="class")
    def traj(self):
        p = APP_DATA / "trajectory.json"
        if not p.exists():
            pytest.skip("trajectory.json not exported")
        return _load("trajectory.json")

    def test_shape(self, traj):
        assert isinstance(traj, dict) and "_meta" in traj and "predictions" in traj

    def test_beats_naive_baseline(self, traj):
        """The whole justification for the model.

        The previous version scored ~9 MAE against a naive carry-forward of
        9.32 — it barely beat doing nothing while claiming to be a projection.
        """
        m = traj["_meta"]
        assert m["model_mae"] < m["naive_mae"], (
            f"model MAE {m['model_mae']} does not beat naive {m['naive_mae']}"
        )

    def test_interval_coverage_is_honest(self, traj):
        """Bands are published as 80%; they should actually cover ~80%."""
        cov = traj["_meta"]["interval_coverage_pct"]
        assert 72.0 <= cov <= 88.0, f"80% interval covers {cov}% of outcomes"

    def test_labels_are_not_just_current_rating(self, traj):
        """The old labels correlated -0.87 with current OVR — 'breakout' meant
        'was bad last year'. Labels now compare against the player's cohort."""
        rows = traj["predictions"]
        n = len(rows)
        cur = [r["current_ovr"] for r in rows]
        vs = [r["vs_cohort"] for r in rows]
        mc, mv = sum(cur) / n, sum(vs) / n
        cov = sum((c - mc) * (v - mv) for c, v in zip(cur, vs)) / n
        sc = (sum((c - mc) ** 2 for c in cur) / n) ** 0.5
        sv = (sum((v - mv) ** 2 for v in vs) / n) ** 0.5
        corr = cov / (sc * sv)
        assert abs(corr) < 0.40, (
            f"label driver correlates {corr:+.2f} with current OVR — "
            f"labels are re-encoding the current rating"
        )

    def test_spread_is_not_compressed(self, traj):
        """The defect that made every starter project downward."""
        rows = traj["predictions"]
        n = len(rows)
        pred = [r["predicted_ovr"] for r in rows]
        cur = [r["current_ovr"] for r in rows]
        sd = lambda xs: (sum((x - sum(xs) / n) ** 2 for x in xs) / n) ** 0.5
        assert sd(pred) / sd(cur) > 0.60, (
            f"projected SD is {sd(pred):.1f} vs current {sd(cur):.1f} — compressed"
        )

    def test_every_prediction_is_explainable(self, traj):
        p = APP_DATA / "trajectory_detail.json"
        if not p.exists():
            pytest.skip("trajectory_detail.json not exported")
        detail = _load("trajectory_detail.json")
        missing = [r["player_season_id"] for r in traj["predictions"]
                   if str(r["player_season_id"]) not in detail]
        assert not missing, f"{len(missing)} predictions with no explanation"
        for k, d in list(detail.items())[:200]:
            assert d.get("explanation"), f"{k} has an empty explanation"
            assert d.get("drivers"), f"{k} has no drivers"


class TestOffensiveLineIsWithheld:
    """OL carries no earned player rating from v4.3, and that has to be visible.

    The number that used to be here read two team keys absent from the payload it
    read them from, so 55% of the formula silently collapsed and what shipped was
    `0.25 + 0.30·recruiting + 0.10·class + 0.05·award`: r = 0.877 with the
    recruiting composite, 20% of rated linemen on exactly 80.0, and agreement
    with EA of −0.274. It is withdrawn.

    "Withdrawn" has to mean two things at once, and these tests pin both: no
    lineman carries a number, AND every lineman still carries a row with a
    reason. A missing row would make him vanish from his own roster.
    """

    @pytest.fixture(scope="class")
    def players(self, manifest):
        season = manifest["last_played_season"]
        path = APP_DATA / f"players_{season}.json"
        if not path.exists():
            pytest.skip(f"players_{season}.json not exported yet")
        return _load(f"players_{season}.json")

    def test_no_lineman_carries_an_earned_rating(self, players):
        rated = [p for p in players
                 if p.get("position_group") == "OL" and p.get("overall_rating") is not None]
        assert not rated, (
            f"{len(rated)} OL rows carry a rating. The player rating is withdrawn — "
            "see NOT_RATED_POSITIONS in script 07 and utils/line_unit.py.")

    def test_linemen_are_still_present(self, players):
        ol = [p for p in players if p.get("position_group") == "OL"]
        assert len(ol) > 100, (
            "Linemen have disappeared from the export. Withholding a rating must "
            "not remove the player — he still has a page and a roster spot.")

    def test_every_withheld_row_says_why(self, players):
        ol = [p for p in players if p.get("position_group") == "OL"]
        missing = [p for p in ol if not p.get("not_rated_reason")]
        assert not missing, (
            f"{len(missing)} OL rows have no reason attached. An unexplained blank "
            "reads as a bug; a stated refusal reads as a judgement.")

    def test_status_is_marked(self, players):
        ol = [p for p in players if p.get("position_group") == "OL"]
        assert all(p.get("rating_status") == "not_rated" for p in ol)


class TestLineUnitRating:
    """The constructive half of the withdrawal: the line is rated as a unit."""

    @pytest.fixture(scope="class")
    def team_ratings(self):
        path = APP_DATA / "team_ratings.json"
        if not path.exists():
            pytest.skip("team_ratings.json not exported yet")
        return _load("team_ratings.json")

    def test_played_seasons_carry_a_line_rating(self, team_ratings, manifest):
        played = [r for r in team_ratings
                  if r.get("season") == manifest["last_played_season"]]
        assert played, "no rows for the last played season"
        rated = [r for r in played if r.get("line_unit") is not None]
        assert len(rated) > 100, (
            f"only {len(rated)} of {len(played)} teams have a line rating. Run "
            "scripts/09_harvest_supplemental.py --dataset team_advanced_season, "
            "then script 10.")

    def test_line_rating_is_on_the_ovr_scale(self, team_ratings):
        vals = [r["line_unit"] for r in team_ratings if r.get("line_unit") is not None]
        assert vals
        assert all(30.0 <= v <= 95.0 for v in vals), "line rating outside its anchors"

    def test_line_rating_does_not_drift_across_eras(self, team_ratings):
        """The bug this shipped with once: pooled bounds made the median climb
        from 52 in 2008 to 77 in 2023, because the provider changed how it
        computes line yards in 2014 and again in 2021. Era-bucketed bounds fix
        it, and this is what stops them being pooled again."""
        import statistics
        by_season = {}
        for r in team_ratings:
            if r.get("line_unit") is not None and r.get("season"):
                by_season.setdefault(r["season"], []).append(r["line_unit"])
        medians = {s: statistics.median(v) for s, v in by_season.items() if len(v) >= 50}
        if len(medians) < 5:
            pytest.skip("not enough seasons exported to test drift")
        early = statistics.mean([m for s, m in medians.items() if s <= 2013])
        late = statistics.mean([m for s, m in medians.items() if s >= 2021])
        assert abs(late - early) < 10, (
            f"median line rating is {early:.1f} in the classic era and {late:.1f} in "
            "the modern one — the era buckets are not holding")

    def test_inputs_ship_with_the_rating(self, team_ratings):
        withrating = [r for r in team_ratings if r.get("line_unit") is not None]
        assert any(r.get("line_unit_inputs") for r in withrating), (
            "the team page renders the inputs as bars; without them it has "
            "a number and no explanation")
