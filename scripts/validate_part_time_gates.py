"""Do the volume gate and the playing-time tier belong on a part-season rating? Measured on a replay of a finished season.

In a season in progress a player with three or more games and an EDGE score should be rated by his EDGE level through
PART_SEASON_CURVE (v4.24). Two older gates in script 07 stand in front of that: has_opp_score (a count of stat events, e.g. 20 for a DL)
sends a player below it to a stat-only fallback, and the playing-time tier blends a role or reserve player with his recruiting
prior. Both are volume tests, so a player who played well in few games is rated for how little he played. In 2026, 1,868 of the
5,281 players with 3+ games and an EDGE score are below the gate and sit 11.5 points under their curve rating on average.

This runs the real replay (scripts/17b) of one finished season twice, as shipped and with CFB_EDGE_FOR_PART_TIME=1 (script 07
`part_time_edge`), and scores both on how the player went on to play:

  Target  T2 = edge_to_ovr(sqrt(12) x mean adjusted game score over the games AFTER the cut), at least 3 later games.
  Placebo the change each player gets (switch minus shipped) dealt to another player of the same family and cut; if the gates
          carried information about who plays well, dealing the change away costs nothing, and if the switch reads real
          production, the dealt version is much worse than the real one.
  Cost    the high end: players whose later level is 85+, where a shrinking curve lowers a good short sample.

The weekly files are overwritten by the replay, so they are backed up and restored, also on failure.

    python scripts/validate_part_time_gates.py --season 2025 --weeks 6 8
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import validate_part_time_rating as vp  # noqa: E402

APP_DATA = ROOT.parent / "cfb-analytics-app" / "data"
SQ = math.sqrt(12)


def replay(season: int, weeks: list, switch: bool) -> dict:
    """17b for the weeks, returning {player_id: {week: (rating, games, basis)}}."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9")
    env.pop("CFB_REFRESH_YEARS", None)
    env["CFB_EDGE_FOR_PART_TIME"] = "1" if switch else "0"
    out = subprocess.run([sys.executable, "-B", "-W", "ignore", str(ROOT / "scripts" / "17b_weekly_progression.py"), "--season", str(season),
                          "--weeks", *map(str, weeks), "--through-week", "15"], env=env, capture_output=True, text=True, cwd=ROOT)
    if out.returncode:
        raise RuntimeError((out.stdout + out.stderr)[-1500:])
    doc = json.loads((ROOT / "data" / "computed" / f"weekly_players_{season}.json").read_text(encoding="utf-8"))
    res = {}
    for pid, rows in doc["players"].items():
        for r in rows:
            res.setdefault(int(pid), {})[r[0]] = (r[1], r[2], r[3])
    return res


def main() -> None:
    """A/B-replay the part-time volume gate on 2025 weeks 6 and 8, with a placebo; restores the weekly files."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--weeks", type=int, nargs="+", default=[6, 8])
    args = ap.parse_args()

    names = [f"weekly_players_{args.season}.json", f"weekly_teams_{args.season}.json"]
    places = [ROOT / "data" / "computed", APP_DATA]
    backup = Path(tempfile.mkdtemp(prefix="gatesexp_"))
    for i, p in enumerate(places):
        for n in names:
            shutil.copy2(p / n, backup / f"{i}_{n}")
    try:
        print("replay as shipped ...", flush=True)
        shipped = replay(args.season, args.weeks, False)
        print("replay with the part-time switch ...", flush=True)
        switched = replay(args.season, args.weeks, True)
    finally:
        for i, p in enumerate(places):
            for n in names:
                shutil.copy2(backup / f"{i}_{n}", p / n)
        shutil.rmtree(backup, ignore_errors=True)
        print("weekly files restored")

    df = vp.game_scores([args.season])
    ps = pd.DataFrame(json.loads((ROOT / "data" / "raw" / "player_seasons.json").read_text(encoding="utf-8")))
    ps = ps[ps["season"] == args.season][["id", "player_id"]].rename(columns={"id": "player_season_id"})
    pid_of = dict(zip(ps["player_season_id"], ps["player_id"]))
    rows = []
    for psid, grp in df.groupby("player_season_id"):
        pid, pg, fam = pid_of.get(psid), grp["position_group"].iloc[0], grp["family"].iloc[0]
        if pid is None:
            continue
        a, w = grp["adj_score"].to_numpy(), grp["week"].to_numpy()
        for c in args.weeks:
            later = a[w > c]
            ship, sw = shipped.get(pid, {}).get(c), switched.get(pid, {}).get(c)
            if not ship or not sw or len(later) < vp.MIN_GAMES or not (vp.MIN_GAMES <= ship[1] <= 9):
                continue
            rows.append({"cut": c, "family": fam, "n": ship[1], "shipped": ship[0], "switch": sw[0], "basis": ship[2],
                         "t2": vp.t07.edge_to_ovr(SQ * later.mean(), pg)})
    d = pd.DataFrame(rows)
    d["moved"] = d["switch"] - d["shipped"]
    d["changed"] = d["moved"].abs() > 0.05
    print(f"\n{len(d)} player-cuts of {args.season}, weeks {args.weeks}, 3-9 games at the cut and 3+ games after")
    print(f"the switch changes {d['changed'].mean():.1%} of them: mean change {d.loc[d['changed'], 'moved'].mean():+.2f}, "
          f"up {(d['moved'] > 0.05).sum()}, down {(d['moved'] < -0.05).sum()}")

    def line(label, sub):
        if sub.empty:
            return
        e1, e2 = sub["shipped"] - sub["t2"], sub["switch"] - sub["t2"]
        print(f"  {label:<24} n={len(sub):5}  shipped MAE {e1.abs().mean():5.2f} bias {e1.mean():+6.2f}   switch MAE {e2.abs().mean():5.2f} bias {e2.mean():+6.2f}")

    line("all", d)
    line("the ones it changes", d[d["changed"]])
    line("the ones it leaves", d[~d["changed"]])
    print("the ones it changes, by games played at the cut:")
    for n, sub in d[d["changed"]].groupby("n"):
        line(f"{n} games", sub)
    print("by family:")
    for f, sub in d[d["changed"]].groupby("family"):
        line(f, sub)
    print("by the level he went on to play at (T2), the ones it changes:")
    for lo, hi in ((0, 60), (60, 75), (75, 85), (85, 100)):
        line(f"T2 {lo}-{hi}", d[d["changed"] & (d["t2"] >= lo) & (d["t2"] < hi)])
    print("the ones it raises by more than 10 points:")
    line("raised > 10", d[d["moved"] > 10])
    # Placebo: deal each player's change to another player of the same family and cut.
    rng = np.random.default_rng(7)
    dealt = d.copy()
    for _, idx in d.groupby(["cut", "family"]).groups.items():
        idx = list(idx)
        dealt.loc[idx, "moved"] = rng.permutation(d.loc[idx, "moved"].to_numpy())
    dealt["placebo"] = dealt["shipped"] + dealt["moved"]
    print(f"placebo (changes dealt to other players): MAE {(dealt['placebo'] - dealt['t2']).abs().mean():.2f}   "
          f"shipped {(d['shipped'] - d['t2']).abs().mean():.2f}   switch {(d['switch'] - d['t2']).abs().mean():.2f}")


if __name__ == "__main__":
    main()
