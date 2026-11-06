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

  (c) postseason placebo    for a FINISHED season (v4.25.1): the week-N snapshot is built twice with 17b's own snapshot(), once on the
                            real data and once after every postseason score has been replaced with a random one. The
                            snapshot is a regular-season object, so no team reading (headline, power, results, roster, stats,
                            record) may move. Checks (a) and (b) run the player chain only and cut games by regular-season
                            week, which is why a leak through script 10's results reading (it solves strengths from every
                            scored game on file) went unseen until the pre-playoff marker test: it let the bowls and the
                            playoff into every week of the 2024 and 2025 replays.

    python scripts/validate_snapshot_invariance.py --season 2026 --weeks 3 6 9 --placebo-week 3
    python scripts/validate_snapshot_invariance.py --season 2025 --weeks 3 --placebo-week 3 --postseason-weeks 5 15

Exit code 1 if any check fails. Takes a few minutes (one 06 run per week, two 06+07 runs for the placebo, two full snapshots per
postseason week).
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
    """games.json as it stood after `week`: the scores of later regular-season games and of every postseason game removed (17b's rule)."""
    out = []
    for g in games:
        g = dict(g)
        if g.get("season") == season and (g.get("season_type") != "regular" or (g.get("week") or 99) > week):
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
    finished = any(g.get("season") == season and g.get("season_type") == "postseason"
                   and (g.get("home_score") is not None or g.get("away_score") is not None) for g in games)   # 17b's rule
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
        t17b.limit_to_week(sandbox, season, masked, week, None if finished else latest_week)
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


TEAM_FIELDS = ("overall_rating", "power_rating", "roster_half", "stats_half", "results_half", "wins", "losses")


def scramble_postseason(season: int, seed: int = 11):
    """A snapshot() `mutate` hook: every postseason game of `season` gets random scores (so who won, and by how much, is noise)."""
    rng = np.random.default_rng(seed)

    def mutate(games: list) -> None:
        for g in games:
            if g.get("season") == season and g.get("season_type") == "postseason":
                g["home_score"], g["away_score"] = (int(x) for x in rng.integers(0, 60, 2))
    return mutate


def check_team_postseason(season: int, weeks: list) -> bool:
    """Check (c): a finished season's week-N team readings must not move when its postseason is scrambled; True if none does."""
    live17 = t17b._module("17_live_season.py", "live17")
    t10 = t17b._module("10_compute_team_ratings.py", "t10")
    t07 = t17b._module("07_compute_player_ratings.py", "t07")
    ok = True
    for wk in weeks:
        base = t17b.snapshot(season, wk, wk, live17, t10, t07)
        alt = t17b.snapshot(season, wk, wk, live17, t10, t07, mutate=scramble_postseason(season))
        a = {t["team_id"]: t for t in base["export"]["teams"]}
        b = {t["team_id"]: t for t in alt["export"]["teams"]}
        moved: dict = {f: [] for f in TEAM_FIELDS}
        for tid in set(a) & set(b):
            for f in TEAM_FIELDS:
                x, y = a[tid].get(f), b[tid].get(f)
                if (x is None) != (y is None) or (x is not None and abs(float(x) - float(y)) > MOVE_TOLERANCE):
                    moved[f].append((abs(float(x or 0) - float(y or 0)), a[tid].get("school") or tid, x, y))
        only = len(set(a) ^ set(b))
        status = "PASS" if not any(moved.values()) and not only else "FAIL"
        ok &= status == "PASS"
        summary = "; ".join(f"{f} {len(v)} teams (largest {max(v)[0]:.2f})" for f, v in moved.items() if v) or "none moved"
        print(f"  {status}  (c) week {wk}, postseason scrambled: {len(set(a) & set(b))} teams in both runs; {summary}; {only} in one run only")
        for f in ("overall_rating", "results_half"):
            for d, name, x, y in sorted(moved[f], reverse=True)[:4]:
                print(f"        {f}: {name}: {x} -> {y}")
    return ok


def main() -> None:
    """Run checks (a) and (b) in one private copy of the data, and (c) when asked; exit 1 on any failure."""
    ap = argparse.ArgumentParser(description="Does a week-N snapshot see only weeks 1..N?")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--weeks", type=int, nargs="+", default=[3, 6, 9], help="weeks for the games-played check (a)")
    ap.add_argument("--placebo-week", type=int, default=3, help="week for the future-scramble placebo (b)")
    ap.add_argument("--postseason-weeks", type=int, nargs="*", default=[],
                    help="weeks for the postseason placebo (c), for a finished season; omitted, (c) is not run")
    ap.add_argument("--only-postseason", action="store_true", help="run only check (c)")
    args = ap.parse_args()
    if args.only_postseason:
        sys.exit(0 if check_team_postseason(args.season, args.postseason_weeks) else 1)

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
    ok_c = check_team_postseason(args.season, args.postseason_weeks) if args.postseason_weeks else True
    sys.exit(0 if ok_a and ok_b and ok_c else 1)


if __name__ == "__main__":
    main()
