"""Is the rating distribution still the shape we calibrated it to?

Read-only. Writes nothing, changes nothing — run it after any weight, anchor or
input change to scripts 06/07 and read the table before shipping.

Two questions, which are not the same question:

1. **Shape.** Per position: how many players, where the mass sits, and how many
   clear 85 and 90. Distribution shape is a hard gate on this project — a tweak
   that helps one player but lifts every running back with him is wrong, and the
   only way to see that is the whole distribution, every time.

2. **Agreement.** Within-position Spearman against EA Sports CFB 27, over the
   players both sides have. EA is a **reference, not a target**: it never supplies
   a number, it answers "are we too generous, too stingy, is the ceiling in the
   right place". Rank-matching to it every season would reintroduce exactly the
   pool-relative scaling AUDIT_FINDINGS §9 exists to forbid.

   The comparison is deliberately restricted to matched players and scored under
   *our* position groups, so both sides describe the same population. EA counts
   here are therefore lower than EA's published totals, which cover its whole
   roster including players we never matched.

   One caveat that cannot be engineered away: we hold exactly one season of EA
   data (CFB 27, 2026), so this is a cross-section against the most recent played
   season, never a backtest.

Usage:
    python scripts/validate_ratings.py                        # 2025, engine=edge
    python scripts/validate_ratings.py --season 2024
    python scripts/validate_ratings.py --season 2026 --engine projected
    python scripts/validate_ratings.py --season 2025 --no-ea
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


def ea_agreement(df: pd.DataFrame, ea: pd.DataFrame) -> pd.DataFrame:
    """Within-position rank agreement with EA, over players both sides have.

    Spearman, not Pearson: the two scales are not the same instrument, and only
    the ordering is meant to be comparable.
    """
    ea = ea[ea["player_id"].notna()][["player_id", "ovr"]].copy()
    ea["player_id"] = ea["player_id"].astype("int64")
    # EA ships one row per player; ours is per player-season. Best rating wins so
    # a player who changed teams mid-season is not counted twice.
    mine = df[df["player_id"].notna()].copy()
    mine["player_id"] = mine["player_id"].astype("int64")
    mine = mine.sort_values("overall_rating", ascending=False) \
               .drop_duplicates(subset=["player_id"], keep="first")

    m = mine.merge(ea, on="player_id", how="inner")
    out = []
    for pg in POSITION_ORDER:
        g = m[m["position_group"] == pg]
        if len(g) < 10:
            continue
        out.append({
            "pos":       pg,
            "matched":   len(g),
            "spearman":  round(float(g["overall_rating"].corr(g["ovr"], method="spearman")), 4),
            "ours 85+":  int((g["overall_rating"] >= 85).sum()),
            "EA 85+":    int((g["ovr"] >= 85).sum()),
            "ours 90+":  int((g["overall_rating"] >= 90).sum()),
            "EA 90+":    int((g["ovr"] >= 90).sum()),
        })
    frame = pd.DataFrame(out)
    if not frame.empty:
        overall = m["overall_rating"].corr(m["ovr"], method="spearman")
        frame.attrs["pooled"] = round(float(overall), 4)
        frame.attrs["n"] = len(m)
    return frame


def main() -> None:
    ap = argparse.ArgumentParser(description="Rating distribution + EA agreement report")
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--engine", default="edge")
    ap.add_argument("--no-ea", action="store_true", help="skip the EA comparison")
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

    if args.no_ea:
        return
    ea = read_raw("ea_ratings")
    if ea.empty or "player_id" not in ea.columns:
        print("\n(no EA ratings on file — run script 08)")
        return

    agree = ea_agreement(season, ea)
    if agree.empty:
        print("\n(too few matched players for an EA comparison)")
        return
    print(f"\n=== vs EA CFB 27 · {agree.attrs['n']} matched players ===")
    print("EA counts cover matched players only, under our position groups.\n")
    print(agree.to_string(index=False))
    print(f"\npooled Spearman (all positions): {agree.attrs['pooled']}")
    print("Within-position is the number that matters — pooling one is inflated by "
          "position-level differences in scale.\n")


if __name__ == "__main__":
    main()
