"""Week-by-week ratings for a season being played: the live chain re-run as it stood after each week.

Script 17 rates a season to date. This script runs the same chain once per completed week N, in a private copy of the data in
which every game after week N is unscored and every per-game statistic after week N is out of reach:

    games.json   scores of games after week N removed (the schedule stays, so the pace scale and the "season in progress" test
                 see the season as it stood)
    06           --in-progress --through-week N    player EDGE from the games through N
    07                                             player ratings
    10           --through-week N                  team readings; team statistics summed from the box scores of weeks 1..N
                                                   (utils/box_scores.py), no line unit (see limits)

and exports two files, beside live_{season}.json in data/computed/ and the app's data/:

    weekly_teams_{season}.json     per team and week: record, headline, power, the three readings, both ranks, that week's game
                                   and its single-game grade
    weekly_players_{season}.json   per player and week: rating, games, basis, that week's game and its single-game grade

Single-game grades (what the progression shows beside the cumulative number):

    team    the game as a strength: margin (capped at +-28, the cap power uses) less the home edge, plus the opponent's strength at
            that week (points above an average team, the same solve as the results reading), through the same absolute anchors
            as the results reading (script 10 results_to_ovr). It answers "how good would a team have to be to do this to
            this opponent", on the headline scale.
    player  the game's adjusted score through the player's position anchors as if the whole season looked like it
            (edge_to_ovr(sqrt(12) x score)): one game of a full 12-game schedule. Only for players who hold a rating that week.

Offline by design: the children get a dead proxy, any request they attempt is counted and the run fails if there is one.
Nothing in the real data/raw or data/computed is modified.

    python scripts/17b_weekly_progression.py --season 2026
"""

import argparse
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils import opponent_strength as ostr  # noqa: E402
from utils.store import fill_shutouts  # noqa: E402

APP_DATA = ROOT.parent / "cfb-analytics-app" / "data"
MARGIN_CAP = 28.0        # the cap power uses on a game's margin (utils/results_rating.CAP)
SEASON_GAMES = 12        # a single game is graded as one game of this many


def _module(file_name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, ROOT / "scripts" / file_name)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def team_game_grade(margin: float, opp_strength: float, home_edge: float, venue: str, results_to_ovr) -> float:
    """One game as a team grade on the headline scale.

    margin is the team's points scored less allowed; venue is "home", "away" or "neutral"; home_edge is power's home edge in
    points; opp_strength is the opponent's off + dfn from the results solve at that week. The margin is capped at +-28, the home
    edge is taken off a home team's margin and added to an away team's, and the opponent's strength is added back.
    """
    m = max(-MARGIN_CAP, min(MARGIN_CAP, float(margin)))
    m += {"home": -home_edge, "away": home_edge}.get(venue, 0.0)
    return float(results_to_ovr(m + opp_strength))


def player_game_grade(adj_score: float, position_group: str, edge_to_ovr) -> float:
    """One game's adjusted score as a rating: the whole season looking like it (sqrt(12) x the score) through the position anchors."""
    return float(edge_to_ovr(math.sqrt(SEASON_GAMES) * float(adj_score), position_group))


def _conf_lookup(sandbox: Path, season: int, teams_raw: pd.DataFrame):
    """conf_of(team_id) as script 10 builds it: the season's harvested conference, else the team's current one; FCS has none."""
    cur = dict(zip(teams_raw["id"], teams_raw["conference"]))
    season_conf = {}
    path = sandbox / "raw" / "team_records.json"
    if path.exists():
        rec = pd.DataFrame(json.loads(path.read_text(encoding="utf-8")))
        if not rec.empty:
            for r in rec[rec["season"] == season].dropna(subset=["team_id", "conference"]).itertuples():
                season_conf[int(r.team_id)] = r.conference
    return lambda t: None if t == ostr.FCS else (season_conf.get(int(t)) or cur.get(int(t)))


def _run(script: str, args: list, sandbox: Path) -> int:
    """Run one pipeline script against the sandbox with no network; return the number of requests it tried to make."""
    env = dict(os.environ, CFB_DATA_DIR=str(sandbox), PYTHONIOENCODING="utf-8",
               HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9")
    env.pop("CFB_REFRESH_YEARS", None)
    cmd = [sys.executable, "-B", "-W", "ignore", str(ROOT / "scripts" / script)] + args
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    text = out.stdout + out.stderr
    if out.returncode != 0:
        raise RuntimeError(f"{script} {' '.join(args)} failed:\n{text[-1200:]}")
    return sum(1 for ln in text.splitlines() if ln.startswith("  GET"))


def snapshot(season: int, week: int, live17, t10, t07) -> dict:
    """Run the chain as of `week` and return that week's teams, players and game grades."""
    sandbox = Path(tempfile.mkdtemp(prefix=f"wk{week}_"))
    try:
        for sub in ("raw", "computed"):
            shutil.copytree(ROOT / "data" / sub, sandbox / sub)
        gpath = sandbox / "raw" / "games.json"
        games = json.loads(gpath.read_text(encoding="utf-8"))
        for g in games:
            if g.get("season") == season and g.get("season_type") == "regular" and (g.get("week") or 99) > week:
                g["home_score"] = g["away_score"] = None
        gpath.write_text(json.dumps(games, separators=(",", ":")), encoding="utf-8")
        gdf = fill_shutouts(pd.DataFrame(games))     # a game's 0 is stored as missing; later weeks' scores are both missing and stay so

        gets = _run("06_compute_edge_scores.py", ["--season", str(season), "--in-progress", "--through-week", str(week)], sandbox)
        gets += _run("07_compute_player_ratings.py", ["--season", str(season)], sandbox)
        gets += _run("10_compute_team_ratings.py", ["--season", str(season), "--through-week", str(week)], sandbox)
        if gets:
            raise RuntimeError(f"week {week}: the children made {gets} request(s); the weekly run must be cache-only")

        progress = live17.season_progress(gdf, season)
        export = live17.build_export(season, sandbox, progress)
        export.pop("_roster", None)

        scores = pd.DataFrame(json.loads((sandbox / "raw" / "player_game_scores.json").read_text(encoding="utf-8")))
        ps = pd.DataFrame(json.loads((sandbox / "raw" / "player_seasons.json").read_text(encoding="utf-8")))
        ps = ps[ps["season"] == season][["id", "player_id"]].rename(columns={"id": "player_season_id"})
        teams_raw = pd.DataFrame(json.loads((ROOT / "data" / "raw" / "teams.json").read_text(encoding="utf-8")))
        conf_of = _conf_lookup(sandbox, season, teams_raw)
        scored = gdf[(gdf["season"] == season) & gdf["home_score"].notna() & gdf["away_score"].notna()].to_dict("records")
        # every team script 10 knows (not only the ones with a row this week): a game's opponent may have results and no rating yet
        strength = ostr.solve(scored, conf_of, [int(t) for t in teams_raw["id"]])
        return {"week": week, "export": export, "progress": progress, "strength": strength,
                "scores": scores.merge(ps, on="player_season_id"), "games": gdf[(gdf["season"] == season) & (gdf["week"] == week)]}
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def weekly_team_rows(snap: dict, t10) -> dict:
    """{team_id: row} for one snapshot: record, readings, ranks and the week's game with its grade."""
    ex, week = snap["export"], snap["week"]
    teams = ex["teams"]
    rk_head = {t["team_id"]: i + 1 for i, t in enumerate(sorted(teams, key=lambda t: -(t["overall_rating"] or -1e9)))}
    rk_pow = {t["team_id"]: i + 1 for i, t in enumerate(sorted(teams, key=lambda t: -(t["power_rating"] if t["power_rating"] is not None else -1e9)))}
    by_id = {t["team_id"]: t for t in teams}
    strength = snap["strength"]
    played = {}
    for g in snap["games"].to_dict("records"):
        if pd.isna(g["home_score"]) or pd.isna(g["away_score"]):
            continue
        for me, opp, mine, theirs, venue in ((g["home_team_id"], g["away_team_id"], g["home_score"], g["away_score"], "home"),
                                             (g["away_team_id"], g["home_team_id"], g["away_score"], g["home_score"], "away")):
            if pd.isna(me):
                continue
            me = int(me)
            opp_key = int(opp) if not pd.isna(opp) and int(opp) in strength else ostr.FCS
            opp_s = strength[opp_key]["off"] + strength[opp_key]["dfn"]
            home_edge = (by_id.get(me) or {}).get("home_edge") or 0.0
            v = "neutral" if g.get("neutral_site") == True else venue   # noqa: E712
            played[me] = {"gid": int(g["id"]), "pf": int(mine), "pa": int(theirs),
                          "opp": int(opp) if not pd.isna(opp) and int(opp) in by_id else None,
                          "venue": v,
                          "grade": round(team_game_grade(mine - theirs, opp_s, home_edge, v, t10.results_to_ovr), 1)}
    out = {}
    for t in teams:
        tid = t["team_id"]
        row = {"week": week, "w": t["wins"], "l": t["losses"],
               "head": _r(t["overall_rating"]), "power": _r(t["power_rating"], 2),
               "roster": _r(t["roster_half"]), "stats": _r(t["stats_half"]), "results": _r(t["results_half"]),
               "rk_head": rk_head[tid], "rk_power": rk_pow[tid]}
        row.update(played.get(tid, {}))
        out[tid] = row
    return out


def _r(v, nd=1):
    return None if v is None else round(float(v), nd)


def weekly_player_rows(snap: dict, t07) -> dict:
    """{player_id: [week, rating, games, basis, game_id, grade]} for the players who hold a rating that week."""
    sc = snap["scores"]
    gids = set(int(g) for g in snap["games"]["id"])
    this_week = sc[sc["game_id"].isin(gids)]
    adj = {}
    for r in this_week.itertuples():
        adj.setdefault(int(r.player_id), {})[int(r.game_id)] = float(r.adj_score)
    out = {}
    for p in snap["export"]["players"]:
        pid = p["player_id"]
        game = next(iter(adj.get(pid, {}).items()), None)
        grade = round(player_game_grade(game[1], p["position_group"], t07.edge_to_ovr), 1) if game else None
        out[pid] = [snap["week"], p["overall_rating"], p["games_played"], "P" if p["rating_basis"] == "production" else "B",
                    game[0] if game else None, grade]
    return out


def main() -> None:
    """Snapshot every completed week (or --weeks), assemble the two weekly files and write them beside live_{season}.json.

    Prints how the last snapshot compares with the live file (players should be identical; teams differ by the line unit).
    """
    ap = argparse.ArgumentParser(description="Week-by-week ratings for a season in progress")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--weeks", type=int, nargs="*", help="only these weeks (default: every week with a scored game)")
    args = ap.parse_args()

    live17 = _module("17_live_season.py", "live17")
    t10 = _module("10_compute_team_ratings.py", "t10")
    t07 = _module("07_compute_player_ratings.py", "t07")
    games = fill_shutouts(pd.DataFrame(json.loads((ROOT / "data" / "raw" / "games.json").read_text(encoding="utf-8"))))
    g = games[(games["season"] == args.season) & (games["season_type"] == "regular") & games["home_score"].notna() & games["away_score"].notna()]
    weeks = sorted(int(w) for w in g["week"].dropna().unique() if w >= 1)
    if args.weeks:
        weeks = [w for w in weeks if w in args.weeks]
    if not weeks:
        sys.exit(f"No completed regular-season weeks for {args.season}.")
    print(f"{args.season}: weeks {weeks[0]}-{weeks[-1]}")

    team_weeks: dict = {}
    player_weeks: dict = {}
    snaps = {}
    for wk in weeks:
        print(f"  week {wk} ...", flush=True)
        snap = snapshot(args.season, wk, live17, t10, t07)
        snaps[wk] = snap
        for tid, row in weekly_team_rows(snap, t10).items():
            team_weeks.setdefault(tid, []).append(row)
        for pid, row in weekly_player_rows(snap, t07).items():
            player_weeks.setdefault(pid, []).append(row)
        print(f"    {len(snap['export']['teams'])} teams, {len(snap['export']['players'])} rated players", flush=True)

    # How far the last snapshot sits from the live file: players should be identical (same games, same chain), teams differ
    # only by the line unit, which the weekly run leaves out.
    live_path = ROOT / "data" / "computed" / f"live_{args.season}.json"
    check = {}
    if live_path.exists():
        live = json.loads(live_path.read_text(encoding="utf-8"))
        if live["through_week"] == weeks[-1]:
            lt = {t["team_id"]: t["overall_rating"] for t in live["teams"]}
            d = [abs(r[-1]["head"] - lt[tid]) for tid, r in team_weeks.items() if tid in lt and r[-1]["head"] is not None and lt[tid] is not None]
            lp = {p["player_id"]: p["overall_rating"] for p in live["players"]}
            pd_ = [abs(r[-1][1] - lp[pid]) for pid, r in player_weeks.items() if pid in lp]
            check = {"live_week": live["through_week"], "teams_compared": len(d),
                     "headline_mean_abs_diff": round(sum(d) / len(d), 3), "headline_max_abs_diff": round(max(d), 2),
                     "players_compared": len(pd_), "player_max_abs_diff": round(max(pd_), 2) if pd_ else None}
            print("  against live_%d.json: %s" % (args.season, check))

    names = {t["team_id"]: t["school"] for t in snaps[weeks[-1]]["export"]["teams"]}
    doc_t = {"season": args.season, "weeks": weeks, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "check_vs_live": check,
             "teams": {str(tid): {"school": names.get(tid), "weeks": rows} for tid, rows in sorted(team_weeks.items())}}
    doc_p = {"season": args.season, "weeks": weeks, "generated_at": doc_t["generated_at"],
             "fields": ["week", "rating", "games", "basis", "game_id", "grade"],
             "players": {str(pid): rows for pid, rows in sorted(player_weeks.items())}}
    for name, doc in ((f"weekly_teams_{args.season}.json", doc_t), (f"weekly_players_{args.season}.json", doc_p)):
        text = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
        for dest in (ROOT / "data" / "computed", APP_DATA):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / name).write_text(text, encoding="utf-8")
        print(f"wrote {name} ({len(text) / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
