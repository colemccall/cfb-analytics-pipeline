"""Is the rating distribution still the shape we calibrated it to?

Read-only. Writes nothing, changes nothing — run it after any weight, anchor or
input change to scripts 06/07 and read the table before shipping.

The question it answers:

1. **Shape.** Per position: how many players, where the mass sits, and how many
   clear 85 and 90. Distribution shape is a hard gate on this project — a tweak
   that helps one player but lifts every running back with him is wrong, and the
   only way to see that is the whole distribution, every time.

Usage:
    python scripts/validate_ratings.py                        # 2025, engine=edge
    python scripts/validate_ratings.py --season 2024
    python scripts/validate_ratings.py --season 2026 --engine projected
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw, read_ratings

POSITION_ORDER = ["QB", "RB", "WR", "TE", "OL", "EDGE", "DL", "LB", "CB", "S", "DB", "K", "P"]


def _pct(s: pd.Series, q: float) -> float:
    return float(s.quantile(q)) if len(s) else float("nan")


def distribution(df: pd.DataFrame) -> pd.DataFrame:
    """One row per position group: where the mass sits and who clears the top."""
    out = []
    for pg in POSITION_ORDER:
        v = df.loc[df["position_group"] == pg, "overall_rating"].dropna()
        if v.empty:
            continue
        out.append({
            "pos":   pg,
            "n":     len(v),
            "mean":  round(float(v.mean()), 1),
            "p50":   round(_pct(v, 0.50), 1),
            "p90":   round(_pct(v, 0.90), 1),
            "p99":   round(_pct(v, 0.99), 1),
            "max":   round(float(v.max()), 1),
            "85+":   int((v >= 85).sum()),
            "90+":   int((v >= 90).sum()),
        })
    return pd.DataFrame(out)


def main() -> None:
    """Print a season's rating distribution per position.

    A readout, not a gate: it exits 1 only when there are no ratings for the
    requested engine/season. Position group and player id are joined from
    player_seasons, the join anchor, because rating rows do not carry them.
    """
    ap = argparse.ArgumentParser(description="Rating distribution report")
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--engine", default="edge")
    args = ap.parse_args()

    ratings = read_ratings(args.engine)
    if ratings.empty:
        print(f"No ratings for engine={args.engine}")
        sys.exit(1)
    season = ratings[ratings["season"] == args.season].copy()
    if season.empty:
        have = sorted(ratings["season"].dropna().unique().tolist())
        print(f"No engine={args.engine} ratings for {args.season}. Have: {have}")
        sys.exit(1)

    # position_group and player_id live on player_seasons, which is the join
    # anchor; a ratings row carries neither for every engine.
    want = [c for c in ("position_group", "player_id") if c not in season.columns]
    if want:
        ps = read_raw("player_seasons")[["id"] + want] \
            .rename(columns={"id": "player_season_id"})
        season = season.merge(ps, on="player_season_id", how="left")

    print(f"\n=== {args.season} · engine={args.engine} · {len(season)} rated player-seasons ===\n")
    print(distribution(season).to_string(index=False))


if __name__ == "__main__":
    main()
