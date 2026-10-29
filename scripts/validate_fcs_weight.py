"""What is a game against an FCS opponent worth? Reproduces the measurement behind OPP_FCS_MULT in scripts/06_compute_edge_scores.py.

For every offensive skill player 2016-2025 with at least one FCS game and at least two FBS games, compares his mean per-game
composite against FCS opponents with his mean against FBS opponents (the same players, so talent cancels), weighted by his FCS
games. A ratio above 1 means FCS games flatter a player; the multiplier that equalises them is its inverse. Then applies each
candidate multiplier (1.0, the pooled-FCS-team value used until v4.19, and the shipped constant) to the FCS games, keeping the
real strength multiplier on the FBS ones, and reports the ratio of the ADJUSTED means, which should be 1.0 when the weight is right.
A placebo relabels the same number of each player's FBS games as FCS at random and must give about 1.0 before any adjustment.

Offense only: the paired composite is the per-game score script 06 already computes; the defensive composite needs the team context
map and is not paired here, so the constant is applied to defense on the offensive evidence (stated as a limit in METHODS).

Usage:
    python scripts/validate_fcs_weight.py
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("_t06f", ROOT / "scripts" / "06_compute_edge_scores.py")
t06 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t06)

SEASONS = range(2016, 2026)
POSITIONS = {"QB", "RB", "WR", "TE"}


def old_pooled_multiplier(sp_map: dict, means: tuple) -> float:
    """The multiplier an FCS opponent got through v4.19: the pooled FCS team's defense read through the FBS curve."""
    entry = sp_map[t06.OPP_FCS_KEY]
    z = max(-2.0, min(2.0, (means[1] - entry["defense"]) / max(means[1] * 0.26, 1.0)))
    return 1.0 + z * (t06.OPP_K_HI if z >= 0 else t06.OPP_K_LO)


def season_rows(season: int) -> pd.DataFrame:
    """One row per offensive player-game: player, position, raw composite, FCS flag and the real FBS strength multiplier."""
    sp_map = t06.build_opponent_map(season)
    means = t06._season_means(sp_map)
    st = t06._load_game_stats(season, POSITIONS)
    recs = []
    for r in st.itertuples():
        opp = r.away_team_id if r.player_team_id == r.home_team_id else r.home_team_id
        opp = int(opp) if opp is not None and opp == opp else None
        raw = t06._off_stat_composite(r.position_group, r.data)
        if raw <= 0:
            continue
        fcs = opp is None or opp not in sp_map
        mult = 1.0 if fcs else t06.opponent_multiplier(opp, sp_map, "defense", means)
        recs.append((season, r.player_id, r.position_group, raw, fcs, mult, old_pooled_multiplier(sp_map, means)))
    return pd.DataFrame(recs, columns=["season", "player_id", "pg", "raw", "fcs", "mult", "old_fcs_mult"])


def paired(df: pd.DataFrame, fcs_mult) -> float:
    """Ratio of the adjusted per-game means, FCS over FBS, over players with both, weighted by their FCS games."""
    d = df.copy()
    d["adj"] = d["raw"] * np.where(d["fcs"], fcs_mult if not isinstance(fcs_mult, str) else d["old_fcs_mult"], d["mult"])
    g = d.groupby(["season", "player_id", "fcs"])["adj"].agg(["mean", "count"]).unstack("fcs")
    g.columns = [f"{a}_{'fcs' if b else 'fbs'}" for a, b in g.columns]
    g = g.dropna()
    g = g[(g["count_fcs"] >= 1) & (g["count_fbs"] >= 2)]
    w = g["count_fcs"]
    return float((g["mean_fcs"] * w).sum() / (g["mean_fbs"] * w).sum())


def main() -> int:
    """Print the paired ratios by position and under each candidate multiplier, and the placebo; return 1 if the shipped value is far off."""
    df = pd.concat([season_rows(s) for s in SEASONS], ignore_index=True)
    print(f"{len(df):,} player-games, {df['fcs'].mean():.1%} against a non-FBS opponent\n")
    print("raw ratio of per-game composite, FCS games over FBS games (same players):")
    for pg, z in df.groupby("pg"):
        r = paired(z.assign(mult=1.0), 1.0)
        print(f"   {pg}: {r:.2f}   multiplier that equalises {1 / r:.2f}")
    raw = paired(df.assign(mult=1.0), 1.0)
    print(f"   all offense: {raw:.3f}   multiplier that equalises {1 / raw:.3f}\n")
    shipped = t06.OPP_FCS_MULT
    print("ratio after the opponent adjustment (1.0 means FCS and FBS games now count alike):")
    for label, m in (("FCS at 1.0 (live until v4.20)", 1.0), ("pooled FCS team (finished seasons to v4.19)", "old"), (f"fixed {shipped} (v4.20)", shipped)):
        print(f"   {label:46s} {paired(df, m):.3f}")
    print(f"   multiplier that equalises once FBS games carry their strength multiplier: {1 / paired(df, 1.0):.3f}"
          f"   (the shipped {shipped} is the owner's setting and is harsher)")
    rng = np.random.default_rng(0)
    pl = df.copy()
    pl["fcs"] = False
    for (_, _), idx in pl.groupby(["season", "player_id"]).groups.items():
        n_fcs = int(df.loc[idx, "fcs"].sum())
        if n_fcs:
            pl.loc[rng.choice(np.array(idx), size=n_fcs, replace=False), "fcs"] = True
    placebo = paired(pl.assign(mult=1.0), 1.0)
    print(f"\nplacebo (the same number of each player's games relabelled FCS at random), raw ratio: {placebo:.3f}  (real: {raw:.3f})")
    return 1 if abs(1 / raw - shipped) > 0.10 else 0


if __name__ == "__main__":
    sys.exit(main())
