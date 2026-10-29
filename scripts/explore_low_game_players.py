"""Exploration (v4.18, not shipped): players with 1-2 productive games.

Usage: python scripts/explore_low_game_players.py

Players with 1-2 productive games: how much does a short, good stretch say about a player?

A rating needs MIN_GAMES = 3 productive games (a game counts only if the box-score composite is positive). This measures, per game,
the raw composite (no opponent adjustment) for 2016-2025 (no 2020), and asks two questions:
  1. How many rostered players sit under the line, and do they look better or worse per game than the players above it?
  2. For players with 6+ productive games, how well does the mean of their FIRST m games predict their mean after game 3?
     The slope (both z-scored within season and position group) is the weight an m-game stretch deserves.
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np, pandas as pd
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from utils.store import read_raw
spec = importlib.util.spec_from_file_location("t06", ROOT / "scripts" / "06_compute_edge_scores.py")
t06 = importlib.util.module_from_spec(spec); spec.loader.exec_module(t06)

games = read_raw("games")[["id", "week", "season"]].rename(columns={"id": "gid"})
frames = []
for season in range(2016, 2026):
    if season == 2020:
        continue
    for side, positions in (("off", t06.OFF_EDGE_POSITIONS), ("def", t06.DEF_EDGE_POSITIONS)):
        rows = t06._load_game_stats(season, positions)
        if rows.empty:
            continue
        if side == "off":
            comp = [t06._off_stat_composite(r.position_group, r.data if isinstance(r.data, dict) else {}) for r in rows.itertuples()]
        else:
            comp = [t06._def_stat_composite(r.position_group, r.data if isinstance(r.data, dict) else {})[0] for r in rows.itertuples()]
        d = rows[["player_id", "position_group", "game_id"]].copy()
        d["comp"] = comp; d["season"] = season; d["side"] = side
        d = d.merge(games[["gid", "week"]], left_on="game_id", right_on="gid", how="left")
        frames.append(d[d.comp > 0])
D = pd.concat(frames, ignore_index=True)
D["week"] = D.week.fillna(99)
print("player-games with a positive composite:", len(D))

g = D.groupby(["season", "side", "player_id", "position_group"]).comp.agg(["count", "mean"]).reset_index()
print("\n1. Rostered players by productive-game count (all positions, 2016-2025 average per season):")
n_season = g.season.nunique()
for lab, m in (("1 game", g["count"] == 1), ("2 games", g["count"] == 2), ("3-5 games", g["count"].between(3, 5)), ("6+ games", g["count"] >= 6)):
    print("   %-10s %6.0f players a season" % (lab, m.sum() / n_season))
g["z"] = g.groupby(["season", "position_group"])["mean"].transform(lambda v: (v - v.mean()) / v.std())
base = g[g["count"] >= 3]
for lab, m in (("1 game", g["count"] == 1), ("2 games", g["count"] == 2)):
    x = g[m]
    print("   %-8s mean per-game composite z %+.2f vs %+.2f for 3+ games; share above the 3+-game 75th percentile %.0f%%" % (
        lab, x.z.mean(), base.z.mean(), 100 * (x.z > base.z.quantile(.75)).mean()))

print("\n2. Slope of the later mean (games 4 on) on the mean of the first m productive games, players with 6+ games:")
res = {}
for side in ("off", "def"):
    for m in (1, 2, 3):
        xs, ys, keys = [], [], []
        for (s, sd, pid, pg), d in D[D.side == side].groupby(["season", "side", "player_id", "position_group"]):
            if len(d) < 6:
                continue
            d = d.sort_values("week")
            xs.append(d.comp.iloc[:m].mean()); ys.append(d.comp.iloc[3:].mean()); keys.append((s, pg))
        df = pd.DataFrame({"x": xs, "y": ys, "k": keys})
        for c in ("x", "y"):
            df[c] = df.groupby("k")[c].transform(lambda v: (v - v.mean()) / (v.std() or 1))
        b = float(np.polyfit(df.x, df.y, 1)[0])
        res[(side, m)] = (b, len(df))
        print("   %s  first %d game(s): slope %.3f  (n=%d)" % (side, m, b, len(df)))

# 3. next-season outcome
from utils.store import read_computed
rat = read_computed("ratings"); rat = rat[(rat.engine == "edge") & rat.overall_rating.notna()]
ps = read_raw("player_seasons")[["id", "player_id", "season"]].rename(columns={"id": "player_season_id"})
rt = rat.merge(ps, on="player_season_id", how="left", suffixes=("", "_ps"))
rt = rt.groupby(["player_id", "season"]).overall_rating.max().reset_index()
rt["rz"] = rt.groupby("season").overall_rating.transform(lambda v: (v - v.mean()) / v.std())
nxt = rt.rename(columns={"season": "ns", "rz": "next_rz", "overall_rating": "next_rating"})[["player_id", "ns", "next_rz", "next_rating"]]
g2 = g.copy(); g2["ns"] = g2.season + 1
j = g2.merge(nxt, on=["player_id", "ns"], how="inner")
print("\n3. Next-season rating (z within season) against this season's per-game composite z, by productive games now:")
for lab, m in (("1-2 games", j["count"].between(1, 2)), ("3-5 games", j["count"].between(3, 5)), ("6+ games", j["count"] >= 6)):
    x = j[m]
    b = np.polyfit(x.z, x.next_rz, 1)[0]
    print("   %-9s n=%5d  of %5d such players (%.0f%% have a rating next year) | slope %.3f corr %.3f | mean next-year rating %.1f (z %+.2f)" % (
        lab, len(x), int(g2[(g2["count"].between(1, 2)) if lab == "1-2 games" else (g2["count"].between(3, 5)) if lab == "3-5 games" else (g2["count"] >= 6)].shape[0]),
        100 * len(x) / max(int(g2[(g2["count"].between(1, 2)) if lab == "1-2 games" else (g2["count"].between(3, 5)) if lab == "3-5 games" else (g2["count"] >= 6)].shape[0]), 1),
        b, x.z.corr(x.next_rz), x.next_rating.mean(), x.next_rz.mean()))
x = j[j["count"].between(1, 2)]
top = x[x.z > base.z.quantile(.75)]
print("   1-2 games AND above the regulars' 75th percentile per game (n=%d): next-year rating z %+.2f vs %+.2f for the other low-game players" % (len(top), top.next_rz.mean(), x[x.z <= base.z.quantile(.75)].next_rz.mean()))
