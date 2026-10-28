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
from collections import Counter
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
        offensive linemen are exported so a lineman stays on his own roster beside our
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
        valid = {"engine_d", "carry", "recruiting"}
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


@pytest.mark.parametrize("season", range(2016, 2027))
class TestRosterCardContract:
    """What the menu's roster and player card need from the data, every season 2016-2026.

    The card opens from a roster row; if that row lacks an id, a name or a position, or the ratings
    file holds a player no roster lists, a card is blank or unreachable. Measured 2026-10-28 over
    170,805 roster rows: no gaps, no duplicates, no orphans. These keep it that way. A roster row with
    no ratings row is legitimate (3,528 in 2026; 4,278 in 2025) and is explained by the screen.
    """

    @pytest.fixture
    def pair(self, season):
        if not (APP_DATA / f"rosters_{season}.json").exists():
            pytest.skip(f"no roster file for {season}")
        return _load(f"rosters_{season}.json"), _load(f"players_{season}.json")

    def test_every_roster_row_has_an_id_a_name_and_a_position(self, pair, season):
        rosters, _ = pair
        bad = [(t, p.get("player_id"), p.get("name")) for t, rows in rosters.items() for p in rows
               if p.get("player_id") is None or not p.get("name") or not p.get("position_group")]
        assert not bad, f"{season}: {len(bad)} roster rows lack an id, a name or a position, e.g. {bad[:3]}"

    def test_no_player_is_listed_twice_on_a_roster(self, pair, season):
        rosters, _ = pair
        dupes = [(t, pid) for t, rows in rosters.items()
                 for pid, n in Counter(p.get("player_id") for p in rows).items() if n > 1]
        assert not dupes, f"{season}: players listed twice on one roster, e.g. {dupes[:3]}"

    def test_every_ratings_row_is_on_its_teams_roster(self, pair, season):
        rosters, players = pair
        on = {t: {p.get("player_id") for p in rows} for t, rows in rosters.items()}
        orphans = [(p["id"], p.get("name"), p.get("team_id")) for p in players
                   if p["id"] not in on.get(str(p.get("team_id")), set())]
        assert not orphans, f"{season}: {len(orphans)} ratings rows no roster lists, e.g. {orphans[:3]}"


class TestPostseason:
    """Bowls and the playoff are in the schedules, labelled, and shown apart from what power counts.

    Measured 2026-10-28: games.json holds 889 postseason games (34-54 a season, 139 in 2023 when
    FCS playoff games are in it); schedules already listed them but the menu showed them as 'W1'
    and 'W2'. Team stats (CFBD /stats/season) already include them: in 2025, 74 of 74 teams that
    played a postseason game have games == all games, none have games == regular games only.
    """

    @pytest.mark.parametrize("season", range(2008, 2026))
    def test_schedules_carry_the_postseason_and_say_which_it_is(self, season):
        sched = _load(f"schedules_{season}.json")
        rows = [g for games in sched.values() for g in games]
        kinds = {g["season_type"] for g in rows}
        assert kinds <= {"regular", "postseason"}, f"{season}: unknown season types {kinds}"
        assert any(g["season_type"] == "postseason" for g in rows), f"{season}: no postseason games in the schedules"
        assert all("venue" in g and (g["venue"] is None or isinstance(g["venue"], str)) for g in rows)

    @pytest.mark.parametrize("season", range(2016, 2026))
    def test_a_postseason_game_between_two_teams_is_on_both_schedules(self, season):
        sched = _load(f"schedules_{season}.json")
        seen = {}
        for tid, games in sched.items():
            for g in games:
                if g["season_type"] == "postseason" and g["opp_is_fbs"]:
                    seen.setdefault(g["game_id"], set()).add(tid)
        lonely = [gid for gid, tids in seen.items() if len(tids) != 2]
        assert not lonely, f"{season}: postseason games on only one team's schedule: {lonely[:5]}"

    @pytest.mark.parametrize("season", range(2016, 2026))
    def test_power_lists_postseason_games_after_the_counted_ones_and_does_not_count_them(self, season):
        pg = _load(f"power_games_{season}.json")
        tr = {r["team_id"]: r for r in _load("team_ratings.json") if r["season"] == season and r.get("results_games") is not None}
        for tid, games in pg.items():
            flags = [bool(g.get("post")) for g in games]
            assert flags == sorted(flags), f"{season} team {tid}: a counted game follows a postseason game"
            if int(tid) in tr:
                counted = sum(1 for g in games if not g.get("post"))
                assert tr[int(tid)]["results_games"] == counted, \
                    f"{season} team {tid}: results part counts {tr[int(tid)]['results_games']} games, table lists {counted}"


class TestCardShards:
    """cards/NNNN.json: one player's whole career in one ~150 KB file, equal to the grid's rows.

    Opening a card used to download 82-93 MB (every season's players file). The shards must hold
    exactly what the grid holds, or a card disagrees with the table it was opened from.
    """

    @pytest.fixture(scope="class")
    def cards(self):
        d = APP_DATA / "cards"
        if not d.exists():
            pytest.skip("no cards/ export")
        return d

    def test_shard_files_are_numbered_within_the_shard_count(self, cards):
        n = int(re.search(r"^CARD_SHARDS = (\d+)", (Path(__file__).parent.parent / "scripts" / "12_export_frontend_json.py")
                          .read_text(encoding="utf-8"), re.M).group(1))
        nums = [int(p.stem) for p in cards.glob("*.json")]
        assert nums and max(nums) < n and all(re.fullmatch(r"\d{4}", p.stem) for p in cards.glob("*.json"))

    @pytest.mark.parametrize("season", [2026, 2025, 2021, 2016])
    def test_every_grid_row_is_in_its_shard_unchanged(self, cards, season):
        n = int(re.search(r"^CARD_SHARDS = (\d+)", (Path(__file__).parent.parent / "scripts" / "12_export_frontend_json.py")
                          .read_text(encoding="utf-8"), re.M).group(1))
        loaded = {}
        missing = []
        for p in _load(f"players_{season}.json"):
            k = p["id"] % n
            shard = loaded.get(k) or loaded.setdefault(k, json.loads((cards / f"{k:04d}.json").read_text(encoding="utf-8")))
            rows = [r for r in shard.get(str(p["id"]), []) if r["season"] == season]
            want = {a: b for a, b in p.items() if a != "logo_url"}
            if len(rows) != 1 or rows[0] != want:
                missing.append(p["id"])
        assert not missing, f"{season}: {len(missing)} grid rows missing or different in their shard, e.g. {missing[:5]}"

    def test_a_players_seasons_are_oldest_first_and_unique(self, cards):
        for path in list(cards.glob("*.json"))[:40]:
            for pid, rows in json.loads(path.read_text(encoding="utf-8")).items():
                seasons = [r["season"] for r in rows]
                assert seasons == sorted(set(seasons)), f"{path.name} player {pid}: seasons {seasons}"


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


class TestRatingBasis:
    """Every rated row says what its number is built from (v4.5).

    Two thirds of any roster has no production to measure, and for those players
    the rating IS `position average + STARS_OVR_DELTA[stars]` — a six-valued step
    function. It was published as the same kind of object as a starter's
    production rating, which is the same category error as the withdrawn OL
    rating differing only in degree.

    The tier that decides this already existed; these tests pin that it is now
    visible, and that it is derived rather than invented — a row labelled
    `production` must not be a recruiting grade in disguise.
    """

    VALID = {"production", "blended", "recruiting", "withheld"}

    @pytest.fixture(scope="class")
    def players(self, manifest):
        season = manifest["last_played_season"]
        path = APP_DATA / f"players_{season}.json"
        if not path.exists():
            pytest.skip(f"players_{season}.json not exported yet")
        return _load(f"players_{season}.json")

    def test_every_row_carries_a_basis(self, players):
        missing = [p for p in players if not p.get("rating_basis")]
        assert not missing, (
            f"{len(missing)} of {len(players)} rows carry no rating_basis. A number "
            "whose nature is unstated is the defect this field exists to close.")

    def test_basis_values_are_from_the_contract(self, players):
        bad = sorted({p.get("rating_basis") for p in players} - self.VALID)
        assert not bad, f"unexpected rating_basis values: {bad}"

    def test_withheld_is_exactly_the_unrated(self, players):
        for p in players:
            if p.get("rating_basis") == "withheld":
                assert p.get("overall_rating") is None, (
                    f"{p.get('name')} is labelled withheld but carries a rating")
            if p.get("rating_status") == "not_rated":
                assert p.get("rating_basis") == "withheld"

    def test_recruiting_basis_rows_exist_and_are_not_rare(self, players):
        """If this ever reads zero, the derivation has silently broken.

        Two thirds of a roster having no production is the fact the field exists
        to expose; a version that labels everything `production` would pass every
        other test here and publish the original lie.
        """
        rated = [p for p in players if p.get("overall_rating") is not None]
        rec = [p for p in rated if p.get("rating_basis") == "recruiting"]
        assert len(rec) > 0.05 * len(rated), (
            f"only {len(rec)} of {len(rated)} rated rows are recruiting-based. "
            "Expected a substantial share — check the tier derivation.")


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


class TestClassYear:
    """The exported class year is derived, and unknown is allowed to be unknown.

    `player_seasons.year` is not a class year. Measured on the store 2026-08-18:
    constant across the whole career for 24,972 of 29,722 players with three or
    more seasons that carry a plausible value (84.0%), never incrementing, an
    outright calendar year in 114,612 of 269,552 rows, and — over rated rows with
    a plausible value — saying "year 4" for 46,610 of 71,320 (65%).

    It was exported under the name `year` and the site rendered it as FR/SO/JR/SR
    anyway. `cohort_curves.json` is keyed on the *derived* class year that script
    15 computes, and script 16 looked that table up with the raw one, so the
    projection carry path asked "what do juniors do next" of a table that had
    never been told who the juniors were.

    These tests pin the three things that fix has to keep true.
    """

    @pytest.fixture(scope="class")
    def players(self, manifest):
        season = manifest["last_played_season"]
        path = APP_DATA / f"players_{season}.json"
        if not path.exists():
            pytest.skip(f"players_{season}.json not exported yet")
        return _load(f"players_{season}.json")

    def test_no_consumer_reads_the_raw_year_field(self):
        """The raw column is not exported at all, under any name a page could
        mistake for a class year. Leaving it beside the derived one is how a
        consumer picks the wrong one — which is the defect, not a risk of it."""
        offenders = []
        for path in sorted(APP_DATA.glob("players_*.json")):
            rows = _load(path.name)
            if rows and isinstance(rows[0], dict) and "year" in rows[0]:
                offenders.append(path.name)
        for name in ("rosters_2025.json", "top_players.json"):
            p = APP_DATA / name
            if not p.exists():
                continue
            rows = _load(name)
            sample = rows[0] if isinstance(rows, list) and rows else None
            if isinstance(rows, dict):
                first = next(iter(rows.values()), None)
                sample = first[0] if isinstance(first, list) and first else None
            if isinstance(sample, dict) and "year" in sample:
                offenders.append(name)
        assert not offenders, (
            f"these exports still carry a raw `year` key: {offenders}. "
            "It is not a class year; export `class_year` from utils/class_year.py.")

    def test_class_year_is_plausible_or_null(self, players):
        bad = [p for p in players
               if p.get("class_year") is not None
               and not (1 <= p["class_year"] <= 6)]
        assert not bad, f"{len(bad)} rows carry an out-of-range class year"

    def test_unknown_renders_as_unknown_not_as_a_freshman(self, players):
        """The branch this whole design exists for. A defaulted class year hands
        every unmatched transfer the full true-freshman recruiting prior, which
        is precisely backwards — so undeterminable must be null, and it must
        actually occur. If this reads zero the default has crept back in."""
        unknown = [p for p in players if p.get("class_year") is None]
        assert unknown, (
            "no row has an unknown class year. 1.7% of rated player-seasons are "
            "left-censored with no recruiting anchor; a run with none of them "
            "means the unknown branch has been filled with a default.")
        for p in unknown:
            assert p.get("class_year_basis") in (None, "unknown"), (
                f"{p.get('name')} has no class year but claims basis "
                f"{p.get('class_year_basis')}")

    def test_the_distribution_is_not_a_spike(self, players):
        """The stored field's tell was that 65% of it said one value. Whatever
        the derived distribution's exact shape, no single class year may hold a
        majority of the rows."""
        from collections import Counter
        c = Counter(p["class_year"] for p in players
                    if p.get("class_year") is not None)
        assert c, "no class years exported at all"
        top, n = c.most_common(1)[0]
        assert n < 0.5 * sum(c.values()), (
            f"class year {top} holds {n}/{sum(c.values())} rows — that is a "
            "default, not a distribution")

    def test_the_basis_is_named_when_the_year_is(self, players):
        named = [p for p in players if p.get("class_year") is not None]
        assert named
        bad = [p for p in named
               if p.get("class_year_basis") not in ("recruiting", "first_fbs")]
        assert not bad, (
            f"{len(bad)} rows carry a class year with no anchor named. "
            "`first_fbs` is a floor and has to be labelled as one.")


class TestThreeRatings:
    """A season being played carries three ratings per player, never combined.

    Last season earned (rating_prev), the preseason projection (overall_rating) and
    earned-to-date (live_rating), each with the reason when it is absent. Script 12
    writes the fields only for a season with a live file from script 17; every
    played season must export exactly as before.
    """

    PREV = {"rated", "not_rated", "unrated", "no_season"}
    LIVE = {"rated", "too_few_games", "no_games", "not_rated"}

    @pytest.fixture(scope="class")
    def season(self, manifest):
        live = sorted(int(p.stem.split("_")[1]) for p in APP_DATA.glob("live_*.json"))
        if not live:
            pytest.skip("no live_{season}.json exported — run script 17 then 12")
        return live[-1]

    @pytest.fixture(scope="class")
    def roster(self, season):
        by_team = _load(f"rosters_{season}.json")
        return [p for rows in by_team.values() for p in rows]

    def test_every_roster_row_carries_the_fields(self, roster):
        missing = [p["player_id"] for p in roster if "live_status" not in p or "rating_prev_status" not in p]
        assert not missing, f"{len(missing)} roster rows without the three-ratings fields"

    def test_statuses_are_from_the_vocabulary(self, roster):
        assert {p["live_status"] for p in roster} <= self.LIVE
        assert {p["rating_prev_status"] for p in roster} <= self.PREV

    def test_a_number_exists_only_where_its_status_says_so(self, roster):
        for p in roster:
            assert (p["live_rating"] is not None) == (p["live_status"] == "rated"), p["player_id"]
            assert (p["rating_prev"] is not None) == (p["rating_prev_status"] == "rated"), p["player_id"]
            if p["live_rating"] is not None:
                assert p["live_basis"] in ("production", "blended"), "a recruiting-basis number was published as earned"

    def test_the_roster_reconciles_with_the_live_file(self, roster, season):
        live = _load(f"live_{season}.json")
        assert len(roster) == live["players_on_rosters"], (
            f"{len(roster)} roster rows vs {live['players_on_rosters']} on rosters in live_{season}.json")
        rated = sum(1 for p in roster if p["live_status"] == "rated")
        assert rated == live["players_rated"]

    def test_the_player_grid_lists_every_live_rated_player(self, season):
        grid = {p["id"] for p in _load(f"players_{season}.json")}
        missing = [p["player_id"] for p in _load(f"live_{season}.json")["players"] if p["player_id"] not in grid]
        assert not missing, f"{len(missing)} live-rated players are absent from players_{season}.json"

    def test_the_three_numbers_are_not_merged(self, roster):
        """Live and projection are separate numbers: where both exist they must differ somewhere."""
        both = [p for p in roster if p["live_rating"] is not None and p["overall_rating"] is not None]
        assert any(p["live_rating"] != p["overall_rating"] for p in both), "live equals projection everywhere: suspicious"

    def test_played_seasons_gain_no_extra_fields(self, manifest):
        last = manifest["last_played_season"]
        rows = _load(f"players_{last}.json")[:200]
        assert not any("live_status" in r or "rating_prev" in r for r in rows)


class TestRecruitingBoard:
    """recruiting_{year}.json (script 18): the menu's prospect board and class ranks."""

    def _classes(self):
        files = sorted(APP_DATA.glob("recruiting_[0-9]*.json"))
        if not files:
            pytest.skip("no recruiting_{year}.json exported — run script 18")
        return [json.loads(p.read_text(encoding="utf-8")) for p in files]

    def test_class_ranks_are_dense_and_only_on_full_classes(self):
        for c in self._classes():
            ranks = sorted(t["class_rank"] for t in c["teams"] if t["class_rank"] is not None)
            assert ranks == list(range(1, len(ranks) + 1)), f"{c['year']}: class ranks are not 1..n"
            for t in c["teams"]:
                if t["class_rank"] is not None:
                    assert t["top_avg"] is not None, "a ranked class has no average"
                else:
                    assert t["top_avg"] is None, "an unranked class carries a ranking average"

    def test_a_missing_value_is_null_not_zero(self):
        for c in self._classes():
            for p in c["prospects"]:
                assert p["composite"] is None or p["composite"] > 0, f"{p['name']}: composite 0"
                assert p["stars"] is None or 1 <= p["stars"] <= 5

    def test_prospects_are_ordered_by_national_rank(self):
        for c in self._classes():
            ranks = [p["national_rank"] for p in c["prospects"] if p["national_rank"] is not None]
            assert ranks == sorted(ranks), f"{c['year']}: prospects are not in rank order"


class TestConferencePerSeason:
    """A team's conference is the one it was in THAT season (v4.11).

    teams.json carries only the current conference, so before this every season showed it:
    Boise State read as Pac-12 in 2008. The truth is in the harvested season records.
    """

    RAW = Path(__file__).parent.parent / "data" / "raw" / "team_records.json"

    def test_every_team_season_matches_its_season_record(self):
        if not self.RAW.exists():
            pytest.skip("team_records not harvested")
        records = {(r["team_id"], r["season"]): r["conference"] for r in json.loads(self.RAW.read_text(encoding="utf-8"))
                   if r.get("team_id") is not None and r.get("conference")}
        rows = _load("team_ratings.json")
        checked = wrong = 0
        for r in rows:
            want = records.get((r["team_id"], r["season"]))
            if want is None:
                continue
            checked += 1
            if r["conference"] != want:
                wrong += 1
        assert checked > 1000, "too few team-seasons checked to mean anything"
        assert wrong == 0, f"{wrong} of {checked} team-seasons carry a conference other than their season's"

    def test_boise_state_was_not_always_in_the_pac_12(self):
        rows = {r["season"]: r["conference"] for r in _load("team_ratings.json") if r["school"] == "Boise State"}
        assert rows[2009] == "Western Athletic"
        assert rows[2015] == "Mountain West"
        assert rows[2026] == "Pac-12", "the season being played falls back to the current conference"


class TestRecruitingClassDetail:
    """recruiting_{year}.json carries each team's class and where it came from (for the map)."""

    def test_state_counts_cover_the_matched_commits_and_the_list_is_best_first(self):
        files = sorted(APP_DATA.glob("recruiting_[0-9]*.json"))
        if not files:
            pytest.skip("no recruiting_{year}.json exported")
        for p in files:
            c = json.loads(p.read_text(encoding="utf-8"))
            for t in c["teams"]:
                assert sum(t["states"].values()) <= t["commits"], f"{c['year']} {t['team']}: more states than commits"
                comps = [x["composite"] for x in t["players"] if x["composite"] is not None]
                assert comps == sorted(comps, reverse=True), f"{c['year']} {t['team']}: class list not best first"
                assert len(t["players"]) <= min(t["commits"], 45)


class TestTransferHomeState:
    def test_every_transfer_row_carries_a_home_state_field(self):
        files = sorted(APP_DATA.glob("transfers_[0-9]*.json"))
        if not files:
            pytest.skip("no transfers exported")
        for p in files:
            for rows in json.loads(p.read_text(encoding="utf-8")).values():
                for t in rows:
                    assert "hometown_state" in t, f"{p.name}: a transfer row lacks hometown_state"
