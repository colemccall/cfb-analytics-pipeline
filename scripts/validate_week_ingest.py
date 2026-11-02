"""Check a staged in-season refresh against the weeks already stored, before anything is merged. Read-only.

Two modes:

    python scripts/validate_week_ingest.py --season 2026 --audit
        Coverage of the weeks already on file: for each regular-season week, the FBS-involved games that are scored,
        how many of those have player box-score rows in stats.json, and how many have a team box score in the cache.
        Run before a refresh, so only weeks with a gap are re-fetched.

    python scripts/validate_week_ingest.py --season 2026 --weeks 9
        Validates data/staging/{season}/ (written by scripts/01b_refresh_in_season.py --stage). Every check prints
        PASS, WARN or FAIL with its numbers, and the exit code is 1 if any check FAILs:

        schema       the staged payloads carry the keys the save functions read; stat keys not seen in earlier weeks
        counts       scored share of the week's FBS-involved games; player rows per scored game against the median
                     of the earlier complete weeks; team box scores for every scored FBS-involved game
        unlinked     box-score entries whose player the stored roster does not know, against the earlier weeks' rate
        uniqueness   one row per game id and per (player_season, game, season, stat_type), staged and stored
        consistency  games of the week already stored (scores and player rows) match the staged payload;
                     score changes in earlier weeks are listed
        conferences  /records conferences against the teams table the rating scripts fall back to

A score is read with the shutout rule (utils/store.fill_shutouts): a game with exactly one score is a shutout, so its
missing score is 0. "FBS-involved" means at least one team is in data/raw/teams.json.
"""

import argparse
import importlib.util
import json
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.api_client import cache_read  # noqa: E402
from utils.store import RAW_DIR  # noqa: E402

_spec = importlib.util.spec_from_file_location("harvest01v", ROOT / "scripts" / "01_harvest_games_players_stats.py")
h01 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h01)

MIN_SCORED_SHARE = 0.90       # of a week's FBS-involved games; complete weeks of 2026 run 0.95-1.0 after shutouts
ROWS_PER_GAME_TOLERANCE = 0.25  # the week's median player rows per scored game, against earlier weeks' median
MAX_CHANGED_ROWS = 0.05       # share of already-stored rows of the week the staged payload may differ on
GAME_KEYS = {"id", "week", "homeTeam", "awayTeam", "homePoints", "awayPoints", "neutralSite", "startDate", "venue"}

_results: list[tuple[str, str, str]] = []


def report(check: str, status: str, message: str) -> None:
    """Record and print one check result."""
    _results.append((check, status, message))
    print(f"  {status:<4}  {check:<12} {message}")


def _load(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def scored(home, away) -> bool:
    """A game has a result when at least one score is recorded (the other, if missing, is a shutout's 0)."""
    return home is not None or away is not None


def stored_games(season: int) -> list:
    return [g for g in _load(RAW_DIR / "games.json") if g.get("season") == season and g.get("season_type") == "regular"]


def fbs_ids() -> set:
    return {t["id"] for t in _load(RAW_DIR / "teams.json")}


def fbs_involved(g: dict, fbs: set) -> bool:
    return g.get("home_team_id") in fbs or g.get("away_team_id") in fbs


def stored_game_rows(season: int) -> dict:
    """{game db id: [stat row, ...]} for the season's per-game player rows in stats.json (loads the whole file)."""
    by_game = defaultdict(list)
    for r in _load(RAW_DIR / "stats.json"):
        if r.get("season") == season and r.get("game_id") is not None and r.get("stat_type") == "game_aggregate":
            by_game[r["game_id"]].append(r)
    return by_game


def team_box_ids(season: int, week: int, payload=None) -> set:
    """API game ids with a team box score for the week: from `payload` if given, else from the cache."""
    rows = payload if payload is not None else (cache_read("/games/teams", {"year": season, "week": week, "seasonType": "regular"}) or [])
    return {g.get("id") for g in rows if g.get("teams")}


# ── audit ─────────────────────────────────────────────────────────────────────

def audit(season: int) -> None:
    """Coverage of the stored weeks: scored FBS-involved games, with player rows, with a cached team box score."""
    fbs = fbs_ids()
    games = stored_games(season)
    rows = stored_game_rows(season)
    weeks = sorted({g["week"] for g in games if g.get("week") is not None})
    print(f"{season} stored regular season: {len(games)} games, weeks {weeks[0]}-{weeks[-1]}")
    print("week | FBS-involved | scored | with player rows | with team box score | gaps (scored, missing either)")
    for w in weeks:
        wk = [g for g in games if g["week"] == w and fbs_involved(g, fbs)]
        sc = [g for g in wk if scored(g.get("home_score"), g.get("away_score"))]
        if not sc:
            print(f"{w:>4} | {len(wk):>12} | {0:>6} |")
            continue
        boxes = team_box_ids(season, w)
        with_rows = [g for g in sc if g["id"] in rows]
        with_box = [g for g in sc if g["cfb_api_id"] in boxes]
        gaps = [f"{g['home_team_name']} v {g['away_team_name']}" for g in sc if g["id"] not in rows or g["cfb_api_id"] not in boxes]
        print(f"{w:>4} | {len(wk):>12} | {len(sc):>6} | {len(with_rows):>15} | {len(with_box):>19} | {'; '.join(gaps[:6])}")


# ── staged validation ─────────────────────────────────────────────────────────

def check_schema(staged: dict, weeks: list, season: int, stored_rows: dict, games: list) -> None:
    """The staged payloads carry the keys the save functions read, and no stat key or category appears or vanishes against earlier weeks."""
    missing = GAME_KEYS - set().union(*(set(g) for g in staged["games_regular"]))
    report("schema", "FAIL" if missing else "PASS", f"/games rows carry {'all keys save_games reads' if not missing else 'no ' + str(sorted(missing))}")

    known = set()
    for g in games:
        if g["week"] not in weeks:
            for r in stored_rows.get(g["id"], []):
                known |= set(r["data"])
    for w in weeks:
        payload = staged[f"games_players_w{w}"]
        keys, shape_ok = set(), True
        for game in payload:
            for team in game.get("teams", []):
                if not {"team", "categories"} <= set(team):
                    shape_ok = False
                for cat in team.get("categories", []):
                    for t in cat.get("types", []):
                        keys.add(f"{cat.get('name', '')}{t.get('name', '')}")
                        if t.get("athletes") and not {"id", "stat"} <= set(t["athletes"][0]):
                            shape_ok = False
        new = sorted(keys - known)
        report("schema", "FAIL" if not shape_ok else ("WARN" if new else "PASS"),
               f"week {w} /games/players: game > team > category > type > athlete shape {'ok' if shape_ok else 'BROKEN'}; "
               f"{len(keys)} stat keys, {len(new)} not seen in earlier weeks {new[:8]}")

        cats_before = set()
        for wb in range(1, w):
            for g in cache_read("/games/teams", {"year": season, "week": wb, "seasonType": "regular"}) or []:
                for t in g.get("teams", []):
                    cats_before |= {s.get("category") for s in t.get("stats", [])}
        cats = {s.get("category") for g in staged[f"games_teams_w{w}"] for t in g.get("teams", []) for s in t.get("stats", [])}
        gone = sorted(c for c in cats_before - cats if c)
        report("schema", "WARN" if gone else "PASS",
               f"week {w} /games/teams: {len(cats)} stat categories; absent this week but present before: {gone[:8]}")


def check_counts(staged: dict, weeks: list, season: int, stored_rows: dict, games: list, fbs: set, maps: dict) -> None:
    """Scored share of each week's FBS-involved games, player rows per scored game, team box scores and unlinked entries, against the stored complete weeks."""
    by_api = {g["cfb_api_id"]: g for g in games}
    staged_games = {g["id"]: g for g in staged["games_regular"]}
    complete_weeks = [w for w in sorted({g["week"] for g in games}) if w not in weeks and w >= 2
                      and any(scored(g.get("home_score"), g.get("away_score")) for g in games if g["week"] == w)]
    base_counts = [len(stored_rows[g["id"]]) for g in games if g["week"] in complete_weeks and g["id"] in stored_rows]
    base_median = st.median(base_counts) if base_counts else None

    for w in weeks:
        wk = [g for g in games if g["week"] == w and fbs_involved(g, fbs)]
        live = [staged_games.get(g["cfb_api_id"]) for g in wk]
        sc = [g for g, s in zip(wk, live) if s and scored(s.get("homePoints"), s.get("awayPoints"))]
        share = len(sc) / len(wk) if wk else 0.0
        not_done = [f"{g['home_team_name']} v {g['away_team_name']} ({g.get('game_date')})" for g, s in zip(wk, live)
                    if not (s and scored(s.get("homePoints"), s.get("awayPoints")))]
        report("counts", "PASS" if share >= MIN_SCORED_SHARE else "FAIL",
               f"week {w}: {len(sc)} of {len(wk)} FBS-involved games scored ({share:.1%}; floor {MIN_SCORED_SHARE:.0%}); unscored: {not_done[:6]}")

        rows, counts = h01.build_game_stat_rows(staged[f"games_players_w{w}"], maps["player"], maps["team"], maps["game"], season)
        per_game = Counter(r["game_id"] for r in rows)
        scored_ids = {g["id"] for g in sc}
        with_rows = [per_game[i] for i in scored_ids if per_game.get(i)]
        med = st.median(with_rows) if with_rows else 0
        ratio = med / base_median if base_median else 0
        ok = base_median and abs(ratio - 1) <= ROWS_PER_GAME_TOLERANCE
        report("counts", "PASS" if ok else "FAIL",
               f"week {w}: player rows for {len(with_rows)} of {len(scored_ids)} scored games; median {med:.0f} rows per game "
               f"against {base_median:.0f} in weeks {complete_weeks[0]}-{complete_weeks[-1]} (ratio {ratio:.2f}, ±{ROWS_PER_GAME_TOLERANCE:.0%})")

        boxes = team_box_ids(season, w, staged[f"games_teams_w{w}"])
        missing_box = [f"{g['home_team_name']} v {g['away_team_name']}" for g in sc if g["cfb_api_id"] not in boxes]
        report("counts", "PASS" if not missing_box else "WARN",
               f"week {w}: team box scores for {len(sc) - len(missing_box)} of {len(sc)} scored FBS-involved games; missing {missing_box[:6]}")

        total = counts["linked"] + counts["no_player"] + counts["no_season"]
        rate = (counts["no_player"] + counts["no_season"]) / total if total else 0
        base = Counter()
        for t in _load(RAW_DIR / "teams.json"):
            payload = cache_read("/games/players", {"year": season, "seasonType": "regular", "team": t["school"]}) or []
            earlier = [g for g in payload if by_api.get(g.get("id"), {}).get("week") in complete_weeks]
            _, c = h01.build_game_stat_rows(earlier, maps["player"], maps["team"], maps["game"], season)
            base.update(c)
        btotal = base["linked"] + base["no_player"] + base["no_season"]
        brate = (base["no_player"] + base["no_season"]) / btotal if btotal else 0
        report("unlinked", "PASS" if rate <= 2 * brate + 0.01 else "WARN",
               f"week {w}: {counts['no_player']} entries with no player row, {counts['no_season']} with no player_season on that team "
               f"({rate:.2%} of {total}); earlier weeks {brate:.2%} of {btotal} — a rise means the roster is behind the box scores")


def check_uniqueness(staged: dict, weeks: list) -> None:
    """No duplicate game id in the staged games, and no duplicate key in the stored games and stats files."""
    ids = [g["id"] for g in staged["games_regular"]]
    dup = [i for i, n in Counter(ids).items() if n > 1]
    report("uniqueness", "FAIL" if dup else "PASS", f"/games: {len(ids)} rows, {len(dup)} duplicate ids")
    stored = _load(RAW_DIR / "stats.json")
    keys = Counter((r["player_season_id"], r.get("game_id"), r["season"], r["stat_type"]) for r in stored)
    dupk = sum(1 for n in keys.values() if n > 1)
    report("uniqueness", "FAIL" if dupk else "PASS", f"stats.json: {len(stored)} rows, {dupk} duplicate (player_season, game, season, type) keys")
    gids = Counter(g.get("cfb_api_id") for g in _load(RAW_DIR / "games.json") if g.get("cfb_api_id"))
    dupg = sum(1 for n in gids.values() if n > 1)
    report("uniqueness", "FAIL" if dupg else "PASS", f"games.json: {len(gids)} api ids, {dupg} duplicates")


def check_consistency(staged: dict, weeks: list, season: int, stored_rows: dict, games: list, maps: dict) -> None:
    """Stored scores of earlier weeks and the week's already-stored player rows against the staged payload."""
    staged_games = {g["id"]: g for g in staged["games_regular"]}
    changed = []
    for g in games:
        s = staged_games.get(g["cfb_api_id"])
        if not s or g["week"] in weeks or not scored(g.get("home_score"), g.get("away_score")):
            continue
        old = (g.get("home_score") or 0, g.get("away_score") or 0)
        new = (s.get("homePoints") or 0, s.get("awayPoints") or 0)
        if old != new:
            changed.append(f"wk{g['week']} {g['home_team_name']} v {g['away_team_name']} {old}->{new}")
    report("consistency", "WARN" if changed else "PASS",
           f"earlier weeks: {len(changed)} stored scores differ from the API (shutout zeros compared as 0) {changed[:6]}")
    restored = sum(1 for g in games if (g.get("home_score") is None) != (g.get("away_score") is None))
    report("consistency", "PASS", f"{restored} stored games carry a shutout's 0 as missing; the merge stores the API's 0 (no rating effect: readers apply fill_shutouts)")

    for w in weeks:
        rows, _ = h01.build_game_stat_rows(staged[f"games_players_w{w}"], maps["player"], maps["team"], maps["game"], season)
        new = {(r["player_season_id"], r["game_id"]): r["data"] for r in rows}
        before = [r for g in games if g["week"] == w for r in stored_rows.get(g["id"], [])]
        same = sum(1 for r in before if new.get((r["player_season_id"], r["game_id"])) == r["data"])
        diff = len(before) - same
        share = diff / len(before) if before else 0.0
        report("consistency", "PASS" if share <= MAX_CHANGED_ROWS else "FAIL",
               f"week {w}: {len(before)} player rows already stored for this week; {same} identical in the staged payload, {diff} differ ({share:.1%})")


def check_conferences(staged: dict, season: int) -> None:
    """The /records conferences against the teams table that scripts 06 and 10 fall back to."""
    teams = {t["id"]: t for t in _load(RAW_DIR / "teams.json")}
    by_school = {t["school"]: t for t in teams.values()}
    recs = staged["records"]
    differ = [f"{r.get('team')}: records {r.get('conference')} / teams {by_school[r['team']].get('conference')}"
              for r in recs if r.get("team") in by_school and r.get("conference") != by_school[r["team"]].get("conference")]
    unmatched = [r.get("team") for r in recs if r.get("team") not in by_school]
    report("conferences", "WARN" if differ else "PASS",
           f"/records: {len(recs)} teams; {len(differ)} conferences differ from teams.json {differ[:6]}; {len(unmatched)} not in teams.json")


def validate(season: int, weeks: list) -> int:
    """Run every check on the staged refresh; return the process exit code."""
    folder = ROOT / "data" / "staging" / str(season)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    staged = {c["name"]: json.loads((folder / f"{c['name']}.json").read_text(encoding="utf-8")) for c in manifest["calls"]}
    print(f"staged {manifest['fetched_at']}: {len(manifest['calls'])} responses for {season} weeks {manifest['weeks']}")

    fbs = fbs_ids()
    games = stored_games(season)
    stored_rows = stored_game_rows(season)
    maps = {
        "team": {t["school"].lower(): t["id"] for t in _load(RAW_DIR / "teams.json") if t.get("school")},
        "player": {p["cfb_api_id"]: p["id"] for p in _load(RAW_DIR / "players.json") if p.get("cfb_api_id")},
        "game": {g["cfb_api_id"]: g["id"] for g in _load(RAW_DIR / "games.json") if g.get("cfb_api_id")},
    }
    check_schema(staged, weeks, season, stored_rows, games)
    check_counts(staged, weeks, season, stored_rows, games, fbs, maps)
    check_uniqueness(staged, weeks)
    check_consistency(staged, weeks, season, stored_rows, games, maps)
    check_conferences(staged, season)

    fails = [r for r in _results if r[1] == "FAIL"]
    warns = [r for r in _results if r[1] == "WARN"]
    print(f"\n{len(_results)} checks: {len(_results) - len(fails) - len(warns)} PASS, {len(warns)} WARN, {len(fails)} FAIL")
    return 1 if fails else 0


def main() -> None:
    """--audit for the stored weeks, else validate the staged refresh of --weeks."""
    ap = argparse.ArgumentParser(description="Validate a staged in-season refresh, or audit the stored weeks")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--weeks", type=int, nargs="+")
    ap.add_argument("--audit", action="store_true")
    args = ap.parse_args()
    if args.audit:
        audit(args.season)
        return
    if not args.weeks:
        ap.error("--weeks is required unless --audit")
    sys.exit(validate(args.season, args.weeks))


if __name__ == "__main__":
    main()
