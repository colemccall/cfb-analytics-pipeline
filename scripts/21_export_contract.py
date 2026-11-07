"""The export contract for sibling apps: cfb-analytics-app/data/exports/v1/ — teams, players, a schema and a manifest.

Two projects will read this one's numbers without reading its code: a rating-driven play-calling sim with a dynasty manager
(cfb-analytics-sim) and a controller-playable football prototype (cfb-analytics-game). They need a small, stable, documented
surface instead of the app's internal files, whose shape changes whenever a screen does. This script writes that surface from
the files the site already publishes (so it can never disagree with the site), and nothing game-related lives here.

The rules the contract keeps (schema.json says them again for the reader on the other side):
  - Every field is described in schema.json: type, meaning, source file and field, unit or range, and its null semantics.
  - A missing value is null AND carries a reason in the row's `nulls` map ({field: reason code}). Never a default number:
    a lineman's rating is null with "withheld_ol", not a 50; a player with no box-score rows has null games, not 0.
  - Within v1 a field is never removed, renamed or redefined. New fields and new reason codes may be added (the minor version
    rises); a consumer treats an unknown reason code as "unknown". A breaking change is v2, written beside v1.
  - Three player ratings are never merged: last season's (earned), this season's projection, and this season to date (earned).

Reads (cfb-analytics-app/data): teams.json, team_ratings.json, live_{season}.json, rosters_{season}.json, players_{season}.json.
Writes (cfb-analytics-app/data/exports/v1): manifest.json, schema.json, teams.json, players.json.

    python scripts/21_export_contract.py                 the current season (CONFIG.CURRENT_SEASON's live file)
    python scripts/21_export_contract.py --check         build in memory and validate; write nothing
"""

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils import model_version  # noqa: E402

APP_DATA = ROOT.parent / "cfb-analytics-app" / "data"
OUT = APP_DATA / "exports" / "v1"
CONTRACT_VERSION = "1.0.0"

# ── the vocabulary of reasons a value is null ─────────────────────────────────
NULL_REASONS = {
    "withheld_ol": "An offensive lineman: no public source has individual blocking data, so no individual rating or projection "
                   "is made (a standing decision since v4.3; the team's line is rated as a unit instead). Not missing: withheld.",
    "no_prev_season": "Not on a roster of ours last season (a freshman, or new to FBS), so there is no last-season rating.",
    "prev_not_rated": "On a roster last season but carries no rating of ours for it.",
    "not_projected": "No projection was made for this season (a late arrival or a walk-on who has since played).",
    "no_box_score_rows": "No box-score row this season yet. Not the same as zero games: box scores list only players who "
                         "recorded a statistic.",
    "no_rating_this_season": "The team has no rating for this season (it has no games or no roster reading on file).",
    "no_recruiting_record": "No recruiting record matched to this player (unrecruited, or a record that could not be matched).",
    "class_year_unknown": "Neither a recruiting class nor a first FBS season is on file, so the class year cannot be derived.",
    "not_in_source": "The source file has no value for this field.",
}

# ── the schema: one entry per field, in the order of `fields` in the data file ─
P = "players_{season}.json"
R = "rosters_{season}.json"
L = "live_{season}.json"
TEAM_FIELDS = [
    ("team_id", "integer", False, "CollegeFootballData team id; the key players.team_id refers to", "teams.json id", None),
    ("school", "string", False, "School name", "teams.json school", None),
    ("abbreviation", "string", True, "Short name", "teams.json abbreviation", None),
    ("conference", "string", True, "Conference this season", "teams.json conference", None),
    ("color", "string", True, "Primary colour, #rrggbb", "teams.json color", None),
    ("alt_color", "string", True, "Secondary colour, #rrggbb", "teams.json alt_color", None),
    ("logo_url", "string", True, "Logo image URL (an outside host)", "teams.json logo_url", None),
    ("rating_prev", "number", True, "Last completed season's team rating (the headline: 1/6 roster, 1/6 stats, 2/3 results)",
     "team_ratings.json overall_rating, season = prev_season", "about 30 to 100+, absolute anchors"),
    ("projection", "number", True, "This season's preseason team rating (built from the projected roster)", f"{L} teams[].preseason_overall",
     "about 30 to 100"),
    ("rating_to_date", "number", True, "This season's team rating from its games so far", f"{L} teams[].overall_rating", "about 30 to 100+"),
    ("offense_to_date", "number", True, "Offensive half of rating_to_date", f"{L} teams[].offense_rating", "about 30 to 100+"),
    ("defense_to_date", "number", True, "Defensive half of rating_to_date", f"{L} teams[].defense_rating", "about 30 to 100+"),
    ("power_to_date", "number", True, "Power, what teams are ranked by: points per game above an average FBS team; while a season is "
     "being played it is the games alone", f"{L} teams[].power_rating", "points per game, signed"),
    ("roster_to_date", "number", True, "The roster reading of rating_to_date", f"{L} teams[].roster_half", "about 30 to 100"),
    ("stats_to_date", "number", True, "The team-stats reading (schedule-adjusted)", f"{L} teams[].stats_half", "about 30 to 100+"),
    ("results_to_date", "number", True, "The results reading (points scored and prevented against the schedule)", f"{L} teams[].results_half",
     "about 30 to 100+"),
    ("wins", "integer", True, "Wins this season so far (regular season)", f"{L} teams[].wins", ">= 0"),
    ("losses", "integer", True, "Losses this season so far", f"{L} teams[].losses", ">= 0"),
    ("nulls", "object", False, "For every null field in this row, why: {field: reason code}. Empty when nothing is null.", "this export",
     "keys are field names; values are codes from null_reasons"),
]
PLAYER_FIELDS = [
    ("player_id", "integer", False, "CollegeFootballData athlete id, stable across seasons", f"{R} player_id", None),
    ("name", "string", False, "Name", f"{R} name", None),
    ("team_id", "integer", False, "The team whose roster he is on this season; joins teams.team_id", f"{R} (the key of each roster)", None),
    ("position", "string", True, "Position as the source roster lists it: the API's coarse label (OL, DB, LS, ATH ...), not a "
     "depth-chart slot; often absent", f"{P} position", None),
    ("position_group", "string", False, "The group he is rated in", f"{R} position_group",
     "QB RB WR TE OL DL EDGE LB CB S DB K P (and ATH, LS where the roster says so)"),
    ("class_year", "integer", True, "Year in college (1 = first year), derived from his recruiting class or first FBS season",
     f"{R} class_year", "1 to 6"),
    ("class_year_basis", "string", True, "What class_year was derived from", f"{P} class_year_basis", "recruiting | first_fbs"),
    ("height_in", "integer", True, "Height", f"{R} height_in", "inches"),
    ("weight_lbs", "integer", True, "Weight", f"{R} weight_lbs", "pounds"),
    ("stars", "integer", True, "Recruiting stars", f"{R} stars", "1 to 5"),
    ("recruiting_composite", "number", True, "Recruiting composite grade", f"{R} composite_score", "0 to 1"),
    ("rating_prev", "number", True, "Last completed season's earned rating (box-score production, opponent-adjusted, on fixed anchors)",
     f"{R} rating_prev", "30 to 99"),
    ("rating_prev_basis", "string", True, "What rating_prev is built from", f"{R} rating_prev_basis", "production | blended | recruiting"),
    ("projection", "number", True, "This season's preseason projection (Engine D career curve, or recruiting / carry-forward where it has "
     "no history)", f"{R} overall_rating", "30 to 99"),
    ("projection_low", "number", True, "Low end of the projection's 80% interval", f"{R} projection_low", "30 to 99"),
    ("projection_high", "number", True, "High end of the projection's 80% interval", f"{R} projection_high", "30 to 99"),
    ("projection_source", "string", True, "How the projection was made", f"{R} projection_source", "engine_d | recruiting | carry"),
    ("projection_confidence", "string", True, "The projection's stated confidence", f"{R} projection_confidence", "low | medium | high"),
    ("rating_to_date", "number", True, "This season's earned rating from the games played so far (the level he has played at, through "
     "the part-season curve)", f"{R} live_rating", "30 to 99"),
    ("rating_to_date_basis", "string", True, "What rating_to_date is built from", f"{R} live_basis", "production | blended"),
    ("games_to_date", "integer", True, "Games with a box-score row this season. Not counted for kickers and punters, whose rating "
     "comes from season totals (null, not_in_source)", f"{R} live_games", ">= 1"),
    ("nulls", "object", False, "For every null field in this row, why: {field: reason code}. Empty when nothing is null.", "this export",
     "keys are field names; values are codes from null_reasons"),
]
ENUMS = {
    "class_year_basis": {"recruiting", "first_fbs"},
    "rating_prev_basis": {"production", "blended", "recruiting"},
    "projection_source": {"engine_d", "recruiting", "carry"},
    "projection_confidence": {"low", "medium", "high"},
    "rating_to_date_basis": {"production", "blended"},
}


def _load(name: str):
    return json.loads((APP_DATA / name).read_text(encoding="utf-8"))


def _num(v):
    """A finite number or None (a NaN or a missing value is never passed through as a number)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _int(v):
    f = _num(v)
    return None if f is None else int(round(f))


def _color(v):
    """A #rrggbb colour or None ("#null", empty and malformed values from the source are not colours)."""
    return v if isinstance(v, str) and len(v) == 7 and v.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in v[1:]) else None


def build_teams(season: int, live: dict) -> list:
    """Rows of teams.json, one per team in teams.json, with this season's and last season's ratings and the null reasons."""
    prev = {int(r["team_id"]): r for r in _load("team_ratings.json") if r.get("season") == season - 1}
    now = {int(t["team_id"]): t for t in live["teams"]}
    rows = []
    for t in sorted(_load("teams.json"), key=lambda x: int(x["id"])):
        tid = int(t["id"])
        lv, pv = now.get(tid), prev.get(tid)
        row = {
            "team_id": tid, "school": t.get("school"), "abbreviation": t.get("abbreviation") or None, "conference": t.get("conference") or None,
            "color": _color(t.get("color")), "alt_color": _color(t.get("alt_color")), "logo_url": t.get("logo_url") or None,
            "rating_prev": _num(pv.get("overall_rating")) if pv else None,
            "projection": _num(lv.get("preseason_overall")) if lv else None,
            "rating_to_date": _num(lv.get("overall_rating")) if lv else None,
            "offense_to_date": _num(lv.get("offense_rating")) if lv else None,
            "defense_to_date": _num(lv.get("defense_rating")) if lv else None,
            "power_to_date": _num(lv.get("power_rating")) if lv else None,
            "roster_to_date": _num(lv.get("roster_half")) if lv else None,
            "stats_to_date": _num(lv.get("stats_half")) if lv else None,
            "results_to_date": _num(lv.get("results_half")) if lv else None,
            "wins": _int(lv.get("wins")) if lv else None,
            "losses": _int(lv.get("losses")) if lv else None,
        }
        nulls = {}
        for f in ("abbreviation", "conference", "color", "alt_color", "logo_url"):
            if row[f] is None:
                nulls[f] = "not_in_source"
        if row["rating_prev"] is None:
            nulls["rating_prev"] = "no_prev_season" if not pv else "not_in_source"
        for f in ("projection", "rating_to_date", "offense_to_date", "defense_to_date", "power_to_date", "roster_to_date", "stats_to_date",
                  "results_to_date", "wins", "losses"):
            if row[f] is None:
                nulls[f] = "no_rating_this_season" if not lv else "not_in_source"
        row["nulls"] = nulls
        rows.append(row)
    return rows


def build_players(season: int) -> list:
    """Rows of players.json: every player on a roster this season, with the three ratings kept apart and the null reasons."""
    rosters = _load(f"rosters_{season}.json")
    extra = {int(p["id"]): p for p in _load(f"players_{season}.json")}
    rows = []
    for team_key, members in rosters.items():
        for r in members:
            pid = int(r["player_id"])
            x = extra.get(pid, {})
            pg = r.get("position_group")
            ol = pg == "OL"
            row = {
                "player_id": pid, "name": r.get("name"), "team_id": int(team_key), "position": x.get("position") or None, "position_group": pg,
                "class_year": _int(r.get("class_year")),
                "class_year_basis": x.get("class_year_basis") if x.get("class_year_basis") in ENUMS["class_year_basis"] else None,
                "height_in": _int(r.get("height_in")), "weight_lbs": _int(r.get("weight_lbs")),
                "stars": _int(r.get("stars")), "recruiting_composite": _num(r.get("composite_score")),
                "rating_prev": _num(r.get("rating_prev")), "rating_prev_basis": r.get("rating_prev_basis") or None,
                "projection": _num(r.get("overall_rating")), "projection_low": _num(r.get("projection_low")),
                "projection_high": _num(r.get("projection_high")), "projection_source": r.get("projection_source") or None,
                "projection_confidence": r.get("projection_confidence") or None,
                "rating_to_date": _num(r.get("live_rating")), "rating_to_date_basis": r.get("live_basis") or None,
                "games_to_date": _int(r.get("live_games")),
            }
            if row["stars"] == 0:            # 0 stars in the source means "no recruiting record", not a zero-star player
                row["stars"] = None
            nulls = {}
            if row["position"] is None:
                nulls["position"] = "not_in_source"
            if row["class_year"] is None:
                nulls["class_year"] = "class_year_unknown"
            if row["class_year_basis"] is None:
                nulls["class_year_basis"] = "class_year_unknown" if row["class_year"] is None else "not_in_source"
            for f in ("height_in", "weight_lbs"):
                if row[f] is None:
                    nulls[f] = "not_in_source"
            for f in ("stars", "recruiting_composite"):
                if row[f] is None:
                    nulls[f] = "no_recruiting_record"
            st = r.get("rating_prev_status")
            prev_reason = "withheld_ol" if (ol or st == "not_rated") else "prev_not_rated" if st == "unrated" else "no_prev_season" \
                if st == "no_season" else "not_in_source"
            for f in ("rating_prev", "rating_prev_basis"):
                if row[f] is None:
                    nulls[f] = prev_reason
            proj_reason = "withheld_ol" if ol else "not_projected"
            for f in ("projection", "projection_low", "projection_high", "projection_source", "projection_confidence"):
                if row[f] is None:
                    nulls[f] = proj_reason
            ls = r.get("live_status")
            live_reason = "withheld_ol" if (ol or ls == "not_rated") else "no_box_score_rows" if ls == "no_games" else "not_in_source"
            for f in ("rating_to_date", "rating_to_date_basis", "games_to_date"):
                if row[f] is None:
                    nulls[f] = live_reason
            row["nulls"] = nulls
            rows.append(row)
    rows.sort(key=lambda r: (r["team_id"], r["player_id"]))
    return rows


def schema(season: int) -> dict:
    """schema.json: the contract, field by field, with the null vocabulary and the versioning rules."""
    def fields(spec):
        return [{"name": n, "type": t, "nullable": nb, "meaning": m, "source": s.replace("{season}", str(season)),
                 **({"range": rg} if rg else {}), **({"enum": sorted(ENUMS[n])} if n in ENUMS else {})} for n, t, nb, m, s, rg in spec]
    return {
        "contract": "cfb-analytics export", "version": CONTRACT_VERSION,
        "layout": "Each data file is {\"fields\": [names], \"rows\": [[values in that order]]}. Zip a row with `fields` to get a record.",
        "null_semantics": "A null value always has a reason: the row's `nulls` field maps every null field to a code in `null_reasons`. "
                          "No field carries a default number in place of a missing one. A consumer must treat an unknown code as 'unknown'.",
        "versioning": "Within v1 no field is removed, renamed or redefined and no reason code changes meaning. New fields (appended to "
                      "`fields`) and new reason codes may be added, and the minor version rises. A breaking change ships as exports/v2 "
                      "beside v1.",
        "ratings": "Player ratings are on a fixed 30-99 scale with absolute anchors (the same production earns the same number in any "
                   "season); a 99 is not guaranteed to exist in a season. Three player ratings are never merged: rating_prev (last "
                   "season, earned), projection (this season, preseason), rating_to_date (this season, earned so far). Ratings measure "
                   "box-score production adjusted for the opponent; they are not physical attributes.",
        "not_in_v1": ["individual offensive-line ratings (withheld everywhere, not only here)", "per-game or per-play data",
                      "physical or skill attributes (speed, strength, accuracy)", "NIL values", "past seasons other than last season's "
                      "final rating"],
        "null_reasons": NULL_REASONS,
        "files": {
            "teams.json": {"rows": "one per FBS team in the app's teams.json", "key": "team_id", "fields": fields(TEAM_FIELDS)},
            "players.json": {"rows": "one per player on a roster this season", "key": "player_id", "fields": fields(PLAYER_FIELDS)},
            "manifest.json": {"rows": "one object: what was exported, from which model, when, and each file's sha256"},
        },
    }


def validate(teams: list, players: list) -> list:
    """Every way a row can break the contract; empty when it holds."""
    problems = []
    for name, rows, spec in (("teams", teams, TEAM_FIELDS), ("players", players, PLAYER_FIELDS)):
        names = [f[0] for f in spec]
        types = {f[0]: f[1] for f in spec}
        nullable = {f[0]: f[2] for f in spec}
        keys = set()
        for r in rows:
            if list(r.keys()) != names:
                problems.append(f"{name}: fields out of order or missing in {r.get(names[0])}")
                continue
            k = r[names[0]]
            if k in keys:
                problems.append(f"{name}: duplicate key {k}")
            keys.add(k)
            for f in names[:-1]:
                v = r[f]
                if v is None:
                    if not nullable[f]:
                        problems.append(f"{name} {k}: {f} is null and not nullable")
                    elif f not in r["nulls"]:
                        problems.append(f"{name} {k}: {f} is null with no reason")
                    continue
                if f in r["nulls"]:
                    problems.append(f"{name} {k}: {f} has a value and a null reason")
                t = types[f]
                if (t == "integer" and (not isinstance(v, int) or isinstance(v, bool))) or (t == "number" and not isinstance(v, (int, float))) \
                        or (t == "string" and not isinstance(v, str)):
                    problems.append(f"{name} {k}: {f}={v!r} is not {t}")
                if f in ENUMS and v not in ENUMS[f]:
                    problems.append(f"{name} {k}: {f}={v!r} is not one of {sorted(ENUMS[f])}")
            for f, code in r["nulls"].items():
                if code not in NULL_REASONS:
                    problems.append(f"{name} {k}: reason {code!r} for {f} is not in the vocabulary")
    team_ids = {t["team_id"] for t in teams}
    orphans = sum(1 for p in players if p["team_id"] not in team_ids)
    if orphans:
        problems.append(f"players: {orphans} rows name a team_id that teams.json does not have")
    return problems


def _columnar(rows: list) -> dict:
    return {"fields": list(rows[0].keys()) if rows else [], "rows": [list(r.values()) for r in rows]}


def main() -> int:
    """Build, validate and (unless --check) write the v1 export for the current season's live file."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--season", type=int, help="the season to export (default: the one the app's live file is for)")
    ap.add_argument("--check", action="store_true", help="build and validate in memory; write nothing")
    args = ap.parse_args()
    season = args.season or json.loads((APP_DATA / "manifest.json").read_text(encoding="utf-8"))["current_season"]
    live = _load(f"live_{season}.json")
    teams, players = build_teams(season, live), build_players(season)
    problems = validate(teams, players)
    for p in problems[:20]:
        print("  CONTRACT:", p)
    if problems:
        print(f"{len(problems)} contract problem(s); nothing written")
        return 1
    reasons = {}
    for r in players + teams:
        for code in r["nulls"].values():
            reasons[code] = reasons.get(code, 0) + 1
    print(f"v1 export, {season}: {len(teams)} teams, {len(players)} players; null reasons used: {dict(sorted(reasons.items()))}")
    if args.check:
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    files = {"teams.json": _columnar(teams), "players.json": _columnar(players), "schema.json": schema(season)}
    digests = {}
    for name, obj in files.items():
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        (OUT / name).write_text(text, encoding="utf-8")
        digests[name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    manifest = {
        "contract": "cfb-analytics export", "version": CONTRACT_VERSION, "season": season, "prev_season": season - 1,
        "through_week": live.get("through_week"), "games_completed": live.get("games_completed"), "games_scheduled": live.get("games_scheduled"),
        "source_generated_at": live.get("generated_at"), "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "release": model_version.release(), "model_fingerprint": model_version.fingerprint(),
        "rows": {"teams.json": len(teams), "players.json": len(players)}, "sha256": digests,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
