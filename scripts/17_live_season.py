"""Earned-to-date ratings for a season that is being played, kept out of everything else.

A season half-way through has results but no final numbers. Rating it with the
normal chain would write partial-season rows into the files every other script
trusts: script 15 trains its career curves on engine="edge", and a 2026 row built
from eight games would be read as a finished season. So this script does not.

It copies data/raw and data/computed into a private folder, points the store at it
(CFB_DATA_DIR), and runs the ordinary chain there:

    06 --in-progress   EDGE, each team rescaled to full-season pace (see 06's
                       team_pace; validate_pace.py measures the correction)
    07                 player ratings, engine="edge"
    10                 team ratings

then reads the private results, joins them to the preseason projection that
script 16 wrote, and exports ONE file — data/computed/live_{season}.json, also
copied to the app's data/. Production ratings.json, team_ratings.json and
player_edge.json are never opened for writing. Run the harvest first:

    CFB_REFRESH_YEARS=2026 python scripts/01_harvest_games_players_stats.py --year 2026
    CFB_REFRESH_YEARS=2026 python scripts/09_harvest_supplemental.py --dataset team_advanced_season --seasons 2026
    CFB_REFRESH_YEARS=2026 python scripts/17_live_season.py --season 2026

What the numbers are: players with a production or blended rating (at least 3
games with a countable stat) and every team, as of the last completed week. A
player with fewer games has no earned rating yet and is simply absent. Part-season
ratings are provisional: week-8 rank agrees with the final rank at Spearman
0.86 to 0.95 depending on position, and the pace rescale leaves paced EDGE about
7% above the eventual value at the median (data/computed/pace_validation.json).
"""

import argparse
import json
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

APP_DATA = ROOT.parent / "cfb-analytics-app" / "data"
EARNED_BASES = ("production", "blended")

# What a rostered player's live rating is, and why when it is absent. Written to
# live_roster_{season}.json for script 12, which puts it on every roster row so a
# card can say "2 games - not yet rated" instead of showing a bare dash.
#   rated          earned-to-date rating (production or blended basis)
#   too_few_games  has played, but not enough countable games for an EDGE yet
#   no_games       no game on record this season
#   not_rated      a position we do not rate per player (offensive line)
LIVE_STATUSES = ("rated", "too_few_games", "no_games", "not_rated")


def _run(script: str, args: list, data_dir: Path, season: int) -> None:
    """Run one pipeline script against the private data folder.

    The child sees CFB_DATA_DIR (so it reads and writes only the copy) and
    CFB_REFRESH_YEARS=<season> (so SP+ and team stats are fetched fresh rather than
    served from a cache written before the games were played). Raises on a
    non-zero exit, with the tail of its output.
    """
    env = dict(os.environ, CFB_DATA_DIR=str(data_dir), CFB_REFRESH_YEARS=str(season), PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, "-B", "-W", "ignore", str(ROOT / "scripts" / script)] + args
    print(f"  {script} {' '.join(args)}")
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    if out.returncode != 0:
        raise RuntimeError(f"{script} failed:\n{(out.stdout + out.stderr)[-800:]}")


def _load(path: Path) -> pd.DataFrame:
    """Read a JSON list file into a DataFrame (empty if the file is missing)."""
    if not path.exists():
        return pd.DataFrame()
    return pd.DataFrame(json.loads(path.read_text(encoding="utf-8")))


def season_progress(games: pd.DataFrame, season: int) -> dict:
    """Where a season stands: games scheduled and completed, last completed week,
    and each team's regular-season record so far. Postseason games are ignored.
    """
    g = games[(games["season"] == season) & (games["season_type"] == "regular")]
    done = g[g["home_score"].notna() & g["away_score"].notna()]
    rec = {}
    for _, r in done.iterrows():
        hw = r["home_score"] > r["away_score"]
        for tid, won in ((r["home_team_id"], hw), (r["away_team_id"], not hw)):
            if pd.isna(tid):          # an FCS opponent with no team row
                continue
            w, l = rec.get(int(tid), (0, 0))
            rec[int(tid)] = (w + won, l + (not won))
    return {"scheduled": len(g), "completed": len(done),
            "through_week": int(done["week"].max()) if len(done) else 0, "records": rec}


def live_roster(earned: pd.DataFrame, ps_all: pd.DataFrame, edge: pd.DataFrame) -> list:
    """One row per rostered player: games played, live rating (or None) and why.

    The 5,468 rated players are a third of a 16,882-player roster; the rest are
    reserves, players with 1 or 2 games, and linemen. Without a row for each, a
    roster card cannot tell "not rated yet" from "missing".
    """
    games = dict(zip(edge["player_season_id"], edge["games_played"]))
    rating = {int(r.player_season_id): (float(r.overall_rating), r.rating_basis) for r in earned.itertuples()}
    out = []
    for r in ps_all.itertuples():
        psid = int(r.player_season_id)
        g = games.get(psid)
        # None = no per-game rows for him (a player rated from his season aggregate):
        # the count is unknown, which is not the same as zero.
        g = int(g) if g is not None and pd.notna(g) else None
        if r.position_group == "OL":
            status, val, basis = "not_rated", None, None
        elif psid in rating:
            status = "rated"
            val, basis = rating[psid]
            val = round(val, 1)
        else:
            status, val, basis = ("too_few_games" if (g or 0) > 0 else "no_games"), None, None
        out.append({"player_id": int(r.player_id), "player_season_id": psid, "games": g,
                    "live_rating": val, "live_basis": basis, "status": status})
    return out


def build_export(season: int, sandbox: Path, progress: dict) -> dict:
    """Join the sandbox's earned-to-date results to the real preseason projection.

    Teams: earned overall/offense/defense beside the preseason projected overall.
    Players: only those with an earned (production or blended) rating, with the
    preseason projection where one exists for the same player-season.
    """
    teams_raw = _load(ROOT / "data" / "raw" / "teams.json")
    school = dict(zip(teams_raw["id"], teams_raw["school"]))
    conf = dict(zip(teams_raw["id"], teams_raw.get("conference", pd.Series(dtype=object))))

    s_tr = _load(sandbox / "computed" / "team_ratings.json")
    r_tr = _load(ROOT / "data" / "computed" / "team_ratings.json")
    live_t = s_tr[(s_tr["season"] == season) & (s_tr["engine"] == "edge")]
    pre_t = r_tr[(r_tr["season"] == season) & (r_tr["engine"] == "projected")]
    pre_ovr = dict(zip(pre_t["team_id"], pre_t["overall_rating"]))

    teams = []
    for _, t in live_t.iterrows():
        tid = int(t["team_id"])
        w, l = progress["records"].get(tid, (0, 0))
        pre = pre_ovr.get(tid)
        ovr = float(t["overall_rating"]) if pd.notna(t["overall_rating"]) else None
        teams.append({
            "team_id": tid, "school": school.get(tid), "conference": conf.get(tid),
            "wins": int(w), "losses": int(l),
            "overall_rating": ovr,
            "offense_rating": float(t["offense_rating"]) if pd.notna(t["offense_rating"]) else None,
            "defense_rating": float(t["defense_rating"]) if pd.notna(t["defense_rating"]) else None,
            "preseason_overall": float(pre) if pre is not None and pd.notna(pre) else None,
            "change": round(ovr - float(pre), 1) if ovr is not None and pre is not None and pd.notna(pre) else None,
        })
    teams.sort(key=lambda x: x["overall_rating"] or 0, reverse=True)

    s_r = _load(sandbox / "computed" / "ratings.json")
    r_r = _load(ROOT / "data" / "computed" / "ratings.json")
    live_p = s_r[(s_r["season"] == season) & (s_r["engine"] == "edge")]
    earned = live_p[live_p["rating_basis"].isin(EARNED_BASES) & live_p["overall_rating"].notna()]
    pre_p = r_r[(r_r["season"] == season) & (r_r["engine"] == "projected") & r_r["overall_rating"].notna()]
    pre_by_ps = dict(zip(pre_p["player_season_id"], pre_p["overall_rating"]))

    ps = _load(sandbox / "raw" / "player_seasons.json")
    ps = ps[ps["season"] == season][["id", "player_id", "team_id", "position_group"]].rename(columns={"id": "player_season_id"})
    ps_all = ps.copy()          # every rostered player, for live_roster()
    names = _load(sandbox / "raw" / "players.json")[["id", "name"]].rename(columns={"id": "player_id"})
    edge = _load(sandbox / "raw" / "player_edge.json")
    edge = edge[edge["season"] == season][["player_season_id", "games_played", "pace_factor"]]
    roster = live_roster(earned, ps_all, edge)
    m = earned.merge(ps, on="player_season_id").merge(names, on="player_id", how="left").merge(edge, on="player_season_id", how="left")

    players = []
    for _, p in m.iterrows():
        pre = pre_by_ps.get(p["player_season_id"])
        ovr = float(p["overall_rating"])
        players.append({
            "player_id": int(p["player_id"]), "name": p["name"],
            "team_id": int(p["team_id"]) if pd.notna(p["team_id"]) else None,
            "team": school.get(p["team_id"]), "position_group": p["position_group"],
            "overall_rating": round(ovr, 1), "rating_basis": p["rating_basis"],
            "games_played": int(p["games_played"]) if pd.notna(p["games_played"]) else None,
            "preseason_overall": round(float(pre), 1) if pre is not None and pd.notna(pre) else None,
            "change": round(ovr - float(pre), 1) if pre is not None and pd.notna(pre) else None,
        })
    players.sort(key=lambda x: x["overall_rating"], reverse=True)

    pace = edge["pace_factor"].dropna()
    validation = {}
    vpath = ROOT / "data" / "computed" / "pace_validation.json"
    if vpath.exists():
        validation = json.loads(vpath.read_text(encoding="utf-8"))
    return {
        "season": season, "through_week": progress["through_week"],
        "games_completed": progress["completed"], "games_scheduled": progress["scheduled"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "median_pace_factor": round(float(pace.median()), 3) if len(pace) else None,
        "players_rated": len(players),
        "players_on_rosters": int(len(ps)),
        "validation": validation,
        "teams": teams, "players": players,
        "_roster": roster,          # popped by main(): written to its own computed-only file
    }


def main() -> None:
    """Run the live chain in a private data copy and write live_{season}.json.

    Nothing in the real data/raw or data/computed is modified. The private copy is
    deleted afterwards unless --keep-sandbox is given (for inspection).
    """
    ap = argparse.ArgumentParser(description="Earned-to-date ratings for a season in progress")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--keep-sandbox", action="store_true")
    args = ap.parse_args()

    games = _load(ROOT / "data" / "raw" / "games.json")
    progress = season_progress(games, args.season)
    if not progress["completed"]:
        sys.exit(f"No completed regular-season games for {args.season} — harvest first (script 01).")
    if progress["completed"] >= progress["scheduled"]:
        print(f"Note: every {args.season} regular-season game is complete; the normal chain (06, 07, 10) applies.")
    print(f"{args.season}: {progress['completed']:,} of {progress['scheduled']:,} regular-season games "
          f"complete, through week {progress['through_week']}")

    sandbox = Path(tempfile.mkdtemp(prefix="live_"))
    try:
        print(f"private data copy: {sandbox}")
        for sub in ("raw", "computed"):
            shutil.copytree(ROOT / "data" / sub, sandbox / sub)
        _run("06_compute_edge_scores.py", ["--season", str(args.season), "--in-progress"], sandbox, args.season)
        _run("07_compute_player_ratings.py", ["--season", str(args.season)], sandbox, args.season)
        _run("10_compute_team_ratings.py", ["--season", str(args.season)], sandbox, args.season)
        export = build_export(args.season, sandbox, progress)
    finally:
        if not args.keep_sandbox:
            shutil.rmtree(sandbox, ignore_errors=True)

    roster = export.pop("_roster")
    roster_doc = {"season": args.season, "through_week": export["through_week"],
                  "generated_at": export["generated_at"], "players": roster}
    (ROOT / "data" / "computed" / f"live_roster_{args.season}.json").write_text(
        json.dumps(roster_doc, separators=(",", ":")), encoding="utf-8")
    by = {k: sum(1 for p in roster if p["status"] == k) for k in LIVE_STATUSES}
    print(f"roster statuses: {by}")
    text = json.dumps(export, separators=(",", ":"), ensure_ascii=False)
    for dest in (ROOT / "data" / "computed", APP_DATA):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"live_{args.season}.json").write_text(text, encoding="utf-8")
    print(f"\nwrote live_{args.season}.json: {len(export['teams'])} teams, {export['players_rated']:,} rated players "
          f"of {export['players_on_rosters']:,} on rosters ({len(text) / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
