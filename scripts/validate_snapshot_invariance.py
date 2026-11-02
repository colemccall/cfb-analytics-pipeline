"""Does a week-N snapshot see only what was known after week N?  Read-only against real data (runs in a private copy).

The weekly progression (scripts/17b_weekly_progression.py) rates a season as it stood after each week. A snapshot that
reads anything from later in the season is silently wrong: a week-6 rating built with week-10 numbers looks fine and
cannot be told apart from a real one. Two checks:

  (a) games-played counts   for each week N, script 06 --in-progress --through-week N is run with the scores of later
                            games removed (as 17b does). Every EDGE row must say through_week == N, and its team_games
                            (the count the in-season scale divides by) must equal that team's completed regular-season
                            games through week N, counted here independently from games.json.

  (b) future-scramble placebo   the week-N chain (06 then 07) is run twice, both times cut to week N the way 17b cuts it
                            (17b limit_to_week): once on the real data, once on data in which everything dated after
                            week N has first been scrambled — the per-game rows after week N deleted, the season
                            totals (cumulative to the day they were fetched) shuffled across players of the same
                            position with half of them dropped, and the team advanced stats dealt to other teams.
                            Shuffling alone would keep WHICH players have a row; dropping half breaks that too. A
                            snapshot that uses only weeks 1..N cannot move: every rating that does move was read from a
                            channel the cut does not cover, and the players are listed.

    python scripts/validate_snapshot_invariance.py --season 2026 --weeks 3 6 9 --placebo-week 3

Exit code 1 if either check fails. Takes a few minutes (one 06 run per week, two 06+07 runs for the placebo).
"""

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.store import fill_shutouts  # noqa: E402

_spec = importlib.util.spec_from_file_location("weekly17b", ROOT / "scripts" / "17b_weekly_progression.py")
t17b = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t17b)

MOVE_TOLERANCE = 0.005     # rating points; ratings are stored to two decimals


def _read(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, rows: list) -> None:
    path.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")


def mask_games(games: list, season: int, week: int) -> list:
    """games.json as it stood after `week`: later regular-season scores removed (17b's rule)."""
    out = []
    for g in games:
        g = dict(g)
        if g.get("season") == season and g.get("season_type") == "regular" and (g.get("week") or 99) > week:
            g["home_score"] = g["away_score"] = None
        out.append(g)
    return out


def completed_through(games: list, season: int, week: int) -> Counter:
    """{team_id: completed regular-season games through `week`}, with the shutout rule."""
    df = fill_shutouts(pd.DataFrame(games))
    df = df[(df["season"] == season) & (df["season_type"] == "regular") & (df["week"] <= week)
            & df["home_score"].notna() & df["away_score"].notna()]
    return Counter([int(t) for t in pd.concat([df["home_team_id"], df["away_team_id"]]).dropna()])


def scramble_future(stats: list, season: int, week: int, week_of_game: dict, pg_of_ps: dict, seed: int = 7) -> tuple[list, dict]:
    """stats.json with everything a week-`week` snapshot must not see scrambled (see module docstring, check b)."""
    rng = np.random.default_rng(seed)
    kept, aggs = [], []
    deleted = 0
    for r in stats:
        if r.get("season") != season:
            kept.append(r)
        elif r.get("game_id") is not None:
            if (week_of_game.get(r["game_id"]) or 99) > week:
                deleted += 1
            else:
                kept.append(r)
        elif r.get("stat_type") == "season_aggregate":
            aggs.append(r)
        else:
            kept.append(r)
    by_pg: dict = {}
    for r in aggs:
        by_pg.setdefault(pg_of_ps.get(r["player_season_id"]), []).append(r)
    dropped = 0
    for rows in by_pg.values():
        payloads = [r["data"] for r in rows]
        order = rng.permutation(len(rows))
        for r, j in zip(rows, order):
            if rng.random() < 0.5:
                dropped += 1
                continue
            kept.append({**r, "data": payloads[j]})
    return kept, {"future_game_rows_deleted": deleted, "aggregates_shuffled": len(aggs) - dropped, "aggregates_dropped": dropped}


def ratings_of(sandbox: Path, season: int) -> dict:
    """{player_season_id: overall_rating} for the season's earned rows (production or blended basis, the ones the live and weekly files publish) that the sandbox's script 07 wrote."""
    df = pd.DataFrame(_read(sandbox / "computed" / "ratings.json"))
    df = df[(df["season"] == season) & (df["engine"] == "edge") & df["overall_rating"].notna()
            & df["rating_basis"].isin(["production", "blended"])]
    return dict(zip(df["player_season_id"].astype(int), df["overall_rating"].astype(float)))


def run(script: str, args: list, sandbox: Path) -> None:
    """One pipeline script in the sandbox, offline; any attempted request is a failure."""
    gets = t17b._run(script, args, sandbox)
    if gets:
        raise RuntimeError(f"{script}: {gets} request(s) attempted; this check must run from the cache")


def check_counts(sandbox: Path, games: list, season: int, weeks: list, team_of_ps: dict) -> bool:
    """Check (a): every week's EDGE rows carry through_week == N and the team's completed games through N; True if all pass."""
    ok = True
    for wk in weeks:
        _write(sandbox / "raw" / "games.json", mask_games(games, season, wk))
        run("06_compute_edge_scores.py", ["--season", str(season), "--in-progress", "--through-week", str(wk)], sandbox)
        edge = [r for r in _read(sandbox / "raw" / "player_edge.json") if r.get("season") == season]
        truth = completed_through(games, season, wk)
        wrong_week = [r for r in edge if r.get("through_week") != wk]
        wrong_games = [r for r in edge if r.get("team_games") is not None
                       and r["team_games"] != truth.get(team_of_ps.get(r["player_season_id"]))]
        status = "PASS" if not wrong_week and not wrong_games else "FAIL"
        ok &= status == "PASS"
        print(f"  {status}  (a) week {wk}: {len(edge)} EDGE rows; through_week != {wk}: {len(wrong_week)}; "
              f"team_games != completed games through week {wk}: {len(wrong_games)}")
    return ok


def check_placebo(sandbox: Path, games: list, stats: list, season: int, week: int, week_of_game: dict, pg_of_ps: dict,
                  names: dict) -> bool:
    """Check (b): rate week N on the real data and on data scrambled after week N, one channel at a time; True if no earned rating moves."""
    masked = mask_games(games, season, week)
    _write(sandbox / "raw" / "games.json", masked)
    latest_week = max(g["week"] for g in games if g.get("season") == season and g.get("season_type") == "regular"
                      and (g.get("home_score") is not None or g.get("away_score") is not None))
    adv_path = sandbox / "raw" / "team_advanced_season.json"
    adv = _read(adv_path)

    def dealt_advanced():
        rng = np.random.default_rng(5)
        out = [dict(r) for r in adv]
        mine = [i for i, r in enumerate(out) if r.get("season") == season]
        ids = [out[i].get("team_id") for i in mine]
        for i, j in zip(mine, rng.permutation(len(mine))):
            out[i]["team_id"] = ids[j]
        return out, len(mine)

    arms = {}
    for label in ("real data", "stats scrambled", "advanced stats dealt"):
        stats_in, adv_in, note = stats, adv, ""
        if label == "stats scrambled":
            stats_in, info = scramble_future(stats, season, week, week_of_game, pg_of_ps)
            note = str(info)
        elif label == "advanced stats dealt":
            adv_in, n = dealt_advanced()
            note = f"{n} teams' season advanced stats dealt to other teams"
        _write(sandbox / "raw" / "stats.json", stats_in)
        _write(adv_path, adv_in)
        t17b.limit_to_week(sandbox, season, masked, week, latest_week)
        run("06_compute_edge_scores.py", ["--season", str(season), "--in-progress", "--through-week", str(week)], sandbox)
        run("07_compute_player_ratings.py", ["--season", str(season)], sandbox)
        arms[label] = ratings_of(sandbox, season)
        if note:
            print(f"      {label}: {note}")
    base = arms["real data"]
    ok = True
    for label in ("stats scrambled", "advanced stats dealt"):
        other = arms[label]
        both = set(base) & set(other)
        moved = sorted(((abs(base[k] - other[k]), k) for k in both if abs(base[k] - other[k]) > MOVE_TOLERANCE), reverse=True)
        only = len(set(base) ^ set(other))
        status = "PASS" if not moved and not only else "FAIL"
        ok &= status == "PASS"
        largest = f" (largest {moved[0][0]:.2f})" if moved else ""
        print(f"  {status}  (b) week {week}, {label}: {len(both)} earned ratings in both runs; {len(moved)} moved{largest}; "
              f"{only} earned in one run only")
        for _, k in moved[:6]:
            print(f"        {names.get(k, k)}: {base[k]:.2f} -> {other[k]:.2f}")
    return ok


def main() -> None:
    """Run checks (a) and (b) in one private copy of the data; exit 1 on any failure."""
    ap = argparse.ArgumentParser(description="Does a week-N snapshot see only weeks 1..N?")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--weeks", type=int, nargs="+", default=[3, 6, 9], help="weeks for the games-played check (a)")
    ap.add_argument("--placebo-week", type=int, default=3, help="week for the future-scramble placebo (b)")
    args = ap.parse_args()

    games = _read(ROOT / "data" / "raw" / "games.json")
    stats = _read(ROOT / "data" / "raw" / "stats.json")
    ps = _read(ROOT / "data" / "raw" / "player_seasons.json")
    players = {p["id"]: p.get("name") for p in _read(ROOT / "data" / "raw" / "players.json")}
    team_of_ps = {r["id"]: r.get("team_id") for r in ps}
    pg_of_ps = {r["id"]: r.get("position_group") for r in ps}
    names = {r["id"]: f"{players.get(r['player_id'])} ({r.get('position_group')})" for r in ps if r.get("season") == args.season}
    week_of_game = {g["id"]: g.get("week") if g.get("season_type") == "regular" else 99 for g in games}

    sandbox = Path(tempfile.mkdtemp(prefix="invariance_"))
    try:
        for sub in ("raw", "computed"):
            shutil.copytree(ROOT / "data" / sub, sandbox / sub)
        print(f"{args.season}: private copy {sandbox}")
        ok_a = check_counts(sandbox, games, args.season, args.weeks, team_of_ps)
        ok_b = check_placebo(sandbox, games, stats, args.season, args.placebo_week, week_of_game, pg_of_ps, names)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    sys.exit(0 if ok_a and ok_b else 1)


if __name__ == "__main__":
    main()
