"""What is a one- or two-game stretch worth? Reproduces the weights in scripts/07_compute_player_ratings.py (LOW_GAME_WEIGHT).

For every player-season 2016-2025 with one or two productive games and an EDGE score kept by script 06 (edge_all), R_edge is the
full-season anchors applied to that score. The outcome is the player's EARNED (production-basis) rating the next season, which only
exists for the minority who go on to earn one. Both sides are measured against their position-and-games mean, and the slope of the
outcome on R_edge is the weight a stretch deserves: how many rating points of next season's rating each point of R_edge predicts.

Reports the slope by games, by quartile of counted events, and for the four games x busy cells; the leave-one-season-out margin error
of no stretch information, one weight and the four cells; and a placebo that shuffles R_edge within position and season.

Usage:
    python scripts/validate_low_game_weights.py
"""

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.store import RAW_DIR, read_computed, read_raw  # noqa: E402

_spec = importlib.util.spec_from_file_location("_t07w", ROOT / "scripts" / "07_compute_player_ratings.py")
t07 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t07)


def dataset() -> pd.DataFrame:
    """One row per low-game player-season with R_edge, counted events and the next season's earned rating (NaN if none)."""
    pe = pd.DataFrame(json.loads((RAW_DIR / "player_edge.json").read_text(encoding="utf-8")))
    pe = pe[pe["season"].between(2016, 2025) & pe["edge_all"].notna() & pe["games_played"].between(1, 2)]
    ps = read_raw("player_seasons")[["id", "player_id", "season", "position_group"]].rename(columns={"id": "player_season_id", "season": "ps_season"})
    rat = read_computed("ratings")
    rat = rat[(rat["engine"] == "edge") & rat["overall_rating"].notna()]
    prod = rat[rat["rating_basis"] == "production"].merge(ps, on="player_season_id")[["player_id", "season", "overall_rating"]]
    d = pe.merge(ps, on="player_season_id", suffixes=("", "_ps"))
    d = d[d["position_group"].isin(t07.EDGE_POSITIONS)].copy()
    d["r_edge"] = [t07.edge_to_ovr(e, pg) for e, pg in zip(d["edge_all"], d["position_group"])]
    nx = prod.rename(columns={"season": "ns", "overall_rating": "next"})
    d["ns"] = d["season"] + 1
    d = d.merge(nx, on=["player_id", "ns"], how="left")
    d = d[d["next"].notna()].copy()
    d["x"] = d["r_edge"] - d.groupby(["position_group", "games_played"])["r_edge"].transform("mean")
    d["t"] = d["next"] - d.groupby(["position_group", "games_played"])["next"].transform("mean")
    d["busy"] = d["stats_measured"] >= t07.LOW_GAME_BUSY_EVENTS
    return d


def slope(z: pd.DataFrame) -> float:
    """Least-squares slope through the origin of t on x (both already demeaned)."""
    x, t = z["x"].values, z["t"].values
    return float((x @ t) / (x @ x))


def main() -> int:
    """Print the slopes, the cross-validated error and the placebo; return 1 if a shipped weight is off its fit by more than 0.08."""
    d = dataset()
    print(f"{len(d):,} low-game player-seasons 2016-2025 with an earned rating the next season\n")
    print("slope of next season's earned rating on R_edge:")
    for g in (1, 2):
        print(f"   {g} game(s): {slope(d[d['games_played'] == g]):.3f}  (n={int((d['games_played'] == g).sum())})")
    d["vq"] = pd.qcut(d["stats_measured"].rank(method="first"), 4, labels=False)
    print("   by quartile of counted events:", [round(slope(z), 3) for _, z in d.groupby("vq")])
    cells = {(int(g), bool(b)): slope(z) for (g, b), z in d.groupby(["games_played", "busy"])}
    print("   games x busy cells:", {k: round(v, 3) for k, v in cells.items()})
    print("   shipped LOW_GAME_WEIGHT:", t07.LOW_GAME_WEIGHT)
    mae0, mae1, mae2 = [], [], []
    for s in sorted(d["season"].unique()):
        tr, te = d[d["season"] != s], d[d["season"] == s]
        wl = {(int(g), bool(b)): slope(z) for (g, b), z in tr.groupby(["games_played", "busy"])}
        pred = np.array([wl[(int(g), bool(b))] for g, b in zip(te["games_played"], te["busy"])]) * te["x"].values
        mae0 += list(np.abs(te["t"]))
        mae1 += list(np.abs(te["t"] - slope(tr) * te["x"].values))
        mae2 += list(np.abs(te["t"] - pred))
    print(f"\nleave-one-season-out margin error: no stretch information {np.mean(mae0):.3f} | one weight {np.mean(mae1):.3f} | four cells {np.mean(mae2):.3f}")
    rng = np.random.default_rng(0)
    pz = d.copy()
    pz["x"] = pz.groupby(["position_group", "season"])["x"].transform(lambda v: rng.permutation(v.values))
    print(f"placebo (R_edge shuffled within position and season): slope {slope(pz):.3f} against {slope(d):.3f}")
    off = max(abs(cells[k] - t07.LOW_GAME_WEIGHT[k]) for k in cells)
    print(f"largest gap between a fitted cell and the shipped weight: {off:.3f}")
    return 1 if off > 0.08 else 0


if __name__ == "__main__":
    sys.exit(main())
