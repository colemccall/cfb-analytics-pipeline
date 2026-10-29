"""How far should a part-season team reading be pulled toward the league mean?

Reproduces the measurement behind SHRINK_GAMES_K in scripts/10_compute_team_ratings.py. For each team-season 2016-2025
(no 2020), take the mean of a per-game measure over the first g regular-season games and over the remaining games. The slope of
the later mean on the early mean (both z-scored within the season) is the weight an early mean deserves: 1.0 would mean an early
deviation from the league persists in full, 0.5 that half of it does.

Measures: line yards per game (the line unit's input); success rate and PPA on offense and defense (efficiency, standing in for the
box-score rates, because per-game yards and third downs are not on file); points for and against.

Usage:
    python scripts/validate_early_reliability.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw  # noqa: E402

CUTS = (4, 7, 9)
EFFICIENCY = ["offense_lineYards", "offense_successRate", "offense_ppa", "defense_successRate", "defense_ppa"]


def per_game_table() -> pd.DataFrame:
    """One row per team per regular-season game 2016-2025 (no 2020): the advanced measures and the points for and against."""
    adv = read_raw("team_advanced_games")
    for c in ("season", "week"):
        adv[c] = pd.to_numeric(adv[c], errors="coerce")
    adv = adv[(adv["seasonType"] == "regular") & adv["season"].between(2016, 2025) & (adv["season"] != 2020)].copy()
    for c in EFFICIENCY:
        adv[c] = pd.to_numeric(adv[c], errors="coerce")
    games = read_raw("games")
    games = games[(games["season_type"] == "regular") & games["season"].between(2016, 2025) & games["home_score"].notna() & games["away_score"].notna()]
    name = read_raw("teams").set_index("id")["school"]
    rows = []
    for r in games.itertuples():
        wk = float(r.week) if r.week == r.week else 99.0
        for me, pf, pa in ((r.home_team_id, r.home_score, r.away_score), (r.away_team_id, r.away_score, r.home_score)):
            if me == me and me in name.index:
                rows.append((int(r.season), name[me], wk, float(pf), float(pa)))
    pts = pd.DataFrame(rows, columns=["season", "team", "week", "pf", "pa"])
    return adv[["season", "team", "week"] + EFFICIENCY].merge(pts, on=["season", "team", "week"], how="outer")


def slope(table: pd.DataFrame, measure: str, cut: int) -> tuple:
    """(slope of later on early, number of team-seasons) for `measure`, early = a team's first `cut` games."""
    xs, ys, ss = [], [], []
    for (s, _), d in table.groupby(["season", "team"]):
        d = d.sort_values("week").dropna(subset=[measure])
        if len(d) < cut + 3:
            continue
        ss.append(s)
        xs.append(d[measure].iloc[:cut].mean())
        ys.append(d[measure].iloc[cut:].mean())
    df = pd.DataFrame({"s": ss, "x": xs, "y": ys})
    for c in ("x", "y"):
        df[c] = df.groupby("s")[c].transform(lambda v: (v - v.mean()) / v.std())
    return float(np.polyfit(df["x"], df["y"], 1)[0]), len(df)


def main() -> int:
    """Print the slope of later on early for each measure and cut, and the means the shrink constant is read from."""
    table = per_game_table()
    print("%-24s" % "measure" + "".join("   g=%d" % g for g in CUTS))
    res = {}
    for c in EFFICIENCY + ["pf", "pa"]:
        res[c] = [slope(table, c, g)[0] for g in CUTS]
        print("%-24s" % c + "".join("   %.3f" % b for b in res[c]))
    for i, g in enumerate(CUTS):
        print("mean slope at g=%d: efficiency and line %.3f | points %.3f" %
              (g, np.mean([res[c][i] for c in EFFICIENCY]), np.mean([res[c][i] for c in ("pf", "pa")])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
