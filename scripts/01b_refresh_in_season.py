"""Refresh a season that is being played with the newest week only — not the whole season again.

Script 01 harvests a season end to end: every roster and every team's box scores, about 400 requests. In the middle of
a season only the newest week and the season totals have changed, so this script asks for exactly those:

    /games                  year, regular          every result of the season in one call (late corrections included)
    /games/players          year, week, regular    the week's player box scores, all teams in one call
    /games/teams            year, week, regular    the week's team box scores (script 10 sums them through a week)
    /stats/player/season    year, regular          season totals: the API cannot slice these by week, so they are
    /ppa/players/season     year                     refreshed whole (one call each)
    /player/usage           year, regular
    /stats/season           year                   team season totals (script 10's live stats half)
    /stats/season/advanced  year                   team advanced totals (script 09 → the line unit)
    /records                year                   conferences and records (scripts 06, 10, 17b)
    /ratings/sp             year                   SP+, a reference column only

Rosters and /teams are NOT fetched. Rosters come from the cache (the last full harvest); a week whose box scores name
players the roster does not know shows up in the validation as unlinked entries, which is the signal to re-harvest
rosters. /teams is left alone because a fresh team harvest once silently changed 95 colours and the logo host.

Two steps, so nothing reaches data/raw or the cache before it has been checked:

    python scripts/01b_refresh_in_season.py --season 2026 --weeks 9 --stage    # fetch into data/staging/2026/
    python scripts/validate_week_ingest.py  --season 2026 --weeks 9            # schema, counts, uniqueness, consistency
    python scripts/01b_refresh_in_season.py --season 2026 --weeks 9 --merge    # into the cache and data/raw/

--stage makes the requests and writes only data/staging/{season}/. --merge makes no requests: it installs each staged
response in the cache (so scripts 06, 09 and 10 read it) and upserts games, the week's per-game player stats and the
season totals with script 01's own save functions. Running --merge twice changes nothing; it prints the hashes of
games.json and stats.json so that can be checked. Then re-derive the supplemental tables from the cache (0 requests):

    python scripts/09_harvest_supplemental.py --dataset team_advanced_season team_records --seasons 2026
"""

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.api_client import cache_read, cache_write, fetch_fresh, load_api_key  # noqa: E402
from utils.store import RAW_DIR  # noqa: E402

_spec = importlib.util.spec_from_file_location("harvest01", ROOT / "scripts" / "01_harvest_games_players_stats.py")
h01 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h01)


def staging_dir(season: int) -> Path:
    """Where --stage writes and --merge and the validator read: data/staging/{season}/."""
    return ROOT / "data" / "staging" / str(season)


def planned_calls(season: int, weeks: list[int]) -> list[tuple[str, str, dict]]:
    """(name, path, params) for every request a refresh makes. The params are exactly those the reading scripts use,
    so a staged response lands on the cache key they look up."""
    calls = [("games_regular", "/games", {"year": season, "seasonType": "regular"})]
    for w in weeks:
        calls.append((f"games_players_w{w}", "/games/players", {"year": season, "week": w, "seasonType": "regular"}))
        calls.append((f"games_teams_w{w}", "/games/teams", {"year": season, "week": w, "seasonType": "regular"}))
    calls += [
        ("player_season_stats", "/stats/player/season", {"year": season, "seasonType": "regular"}),
        ("ppa", "/ppa/players/season", {"year": season}),
        ("usage", "/player/usage", {"year": season, "seasonType": "regular"}),
        ("team_season_stats", "/stats/season", {"year": season}),
        ("team_advanced_season", "/stats/season/advanced", {"year": season}),
        ("records", "/records", {"year": season}),
        ("sp", "/ratings/sp", {"year": season}),
    ]
    return calls


def stage(season: int, weeks: list[int]) -> None:
    """Fetch every planned call fresh and write it to the staging folder with a manifest. Touches nothing else."""
    api_key = load_api_key()
    out = staging_dir(season)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"season": season, "weeks": weeks, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "calls": []}
    for name, path, params in planned_calls(season, weeks):
        data = fetch_fresh(api_key, path, params)
        (out / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
        manifest["calls"].append({"name": name, "path": path, "params": params,
                                  "records": len(data) if isinstance(data, list) else None})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"\nstaged {len(manifest['calls'])} responses in {out} ({len(manifest['calls'])} requests)")


def load_staged(season: int) -> tuple[dict, dict]:
    """(manifest, {name: payload}) from the staging folder."""
    folder = staging_dir(season)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    payloads = {c["name"]: json.loads((folder / f"{c['name']}.json").read_text(encoding="utf-8")) for c in manifest["calls"]}
    return manifest, payloads


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def merge(season: int, weeks: list[int]) -> None:
    """Install the staged responses in the cache and upsert them into data/raw/ (no requests)."""
    manifest, staged = load_staged(season)
    if manifest["season"] != season or sorted(manifest["weeks"]) != sorted(weeks):
        sys.exit(f"staging holds season {manifest['season']} weeks {manifest['weeks']}, not {season} {weeks}")

    for c in manifest["calls"]:
        cache_write(c["path"], c["params"], staged[c["name"]])
    print(f"cache: {len(manifest['calls'])} keys replaced")

    teams = json.loads((RAW_DIR / "teams.json").read_text(encoding="utf-8"))
    team_id_map = {t["school"].lower(): t["id"] for t in teams if t.get("school")}
    players = json.loads((RAW_DIR / "players.json").read_text(encoding="utf-8"))
    player_id_map = {p["cfb_api_id"]: p["id"] for p in players if p.get("cfb_api_id")}

    print("games ...")
    game_id_map = h01.save_games(staged["games_regular"], team_id_map, season)

    for w in weeks:
        print(f"per-game player stats, week {w} ...")
        h01.save_game_stats(staged[f"games_players_w{w}"], player_id_map, team_id_map, game_id_map, season)

    print("season totals (rosters from the cache) ...")
    rosters = {}
    for t in teams:
        roster = cache_read("/roster", {"team": t["school"], "year": season})
        if roster is not None:
            rosters[t["school"]] = roster
    ps = json.loads((RAW_DIR / "player_seasons.json").read_text(encoding="utf-8"))
    ps_id_map = {r["player_id"]: r["id"] for r in ps if r.get("season") == season}
    # /awards is not refreshed: it has no 2026 response (never cached) and awards are a season-end fact, so award_tier
    # stays 0 exactly as the last full harvest wrote it.
    h01.save_season_stats(staged["player_season_stats"], staged["ppa"], staged["usage"], [],
                          player_id_map, ps_id_map, rosters, team_id_map, season)

    print(f"\nrosters read from the cache: {len(rosters)} teams")
    print(f"games.json  sha256 {_sha256(RAW_DIR / 'games.json')}")
    print(f"stats.json  sha256 {_sha256(RAW_DIR / 'stats.json')}")


def main() -> None:
    """--stage or --merge for one season's newest week(s); see the module docstring for the order."""
    ap = argparse.ArgumentParser(description="Refresh a season in progress with its newest week(s)")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--weeks", type=int, nargs="+", required=True, help="the regular-season week(s) to refresh")
    step = ap.add_mutually_exclusive_group(required=True)
    step.add_argument("--stage", action="store_true", help="fetch into data/staging/{season}/ (makes the requests)")
    step.add_argument("--merge", action="store_true", help="install the staged responses (no requests)")
    args = ap.parse_args()
    if args.stage:
        stage(args.season, args.weeks)
    else:
        merge(args.season, args.weeks)


if __name__ == "__main__":
    main()
