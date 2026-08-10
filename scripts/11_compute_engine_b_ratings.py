"""Engine B — NIL + Recruiting composite rating.

Produces an overall_rating (30–99) based on recruiting prestige and NIL valuation.
Independent of on-field stats; useful for freshmen, transfers, and cross-season projections.

Formula:
    engine_b_ovr = 0.60 * recruiting_ovr + 0.40 * nil_ovr   (if NIL available)
    engine_b_ovr = recruiting_ovr                             (if no NIL data)

    recruiting_ovr: 247Sports composite (0–1.0 scale) mapped to [30, 99] via piecewise anchors.
    nil_ovr:        On3 NIL valuation (USD/year) log-scaled vs position median, mapped to [30, 99].

NIL data is currently empty (On3 blocks scraping — see script 04), so this runs
recruiting-only until a working NIL source lands.

Reads:  data/raw/{player_seasons,recruiting,nil_valuations}.json
Writes: data/computed/ratings.json  (rows with engine='engine_b')

Usage:
    python scripts/11_compute_engine_b_ratings.py
    python scripts/11_compute_engine_b_ratings.py --season 2025
    python scripts/11_compute_engine_b_ratings.py --season 2024 --season 2025
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw, read_computed, write_computed

ENGINE        = "engine_b"
MODEL_VERSION = "engine_b_v1.1"

# Recruiting classes are matched to a season within this lookback window —
# a 2021 recruit is still "a 2021 recruit" in their 2025 senior season.
RECRUIT_LOOKBACK_YEARS = 5

# ---------------------------------------------------------------------------
# Recruiting composite → OVR anchors
# 247Sports composite is 0–1.0 scale (0.98+ = elite 5-star, 0.7 = average 3-star).
# Historical distributions: p50 ≈ 0.82 (3-star), p90 ≈ 0.91 (4-star+)
# ---------------------------------------------------------------------------
RECRUIT_ANCHORS: list[tuple[float, float]] = [
    (0.0,  30),   # unranked / no composite
    (0.70, 45),   # low 3-star / lightly recruited
    (0.82, 60),   # median 3-star
    (0.86, 70),   # high 3-star
    (0.89, 78),   # low 4-star
    (0.91, 83),   # solid 4-star
    (0.94, 88),   # high 4-star / borderline 5-star
    (0.96, 92),   # 5-star fringe
    (0.98, 96),   # elite 5-star
    (1.00, 99),   # all-time elite (Trevor Lawrence territory)
]

# NIL valuation → OVR anchors
# Mapped on log scale. Values in USD/year (On3 estimates).
# Position medians vary widely; we scale relative to position median first.
# Ratio: player_nil / position_median → log-scaled to [30, 99].
NIL_RATIO_ANCHORS: list[tuple[float, float]] = [
    (0.0,   30),   # no NIL / zero valuation
    (0.1,   40),
    (0.5,   55),
    (1.0,   65),   # at position median
    (2.0,   73),
    (5.0,   82),
    (10.0,  88),
    (25.0,  93),
    (50.0,  97),
    (100.0, 99),
]


def piecewise_interp(x: float, anchors: list[tuple[float, float]]) -> float:
    xs = [a[0] for a in anchors]
    ys = [float(a[1]) for a in anchors]
    return float(np.clip(np.interp(x, xs, ys), 30.0, 99.0))


def recruit_to_ovr(composite: float | None) -> float | None:
    if composite is None or composite <= 0:
        return None
    return piecewise_interp(float(composite), RECRUIT_ANCHORS)


def nil_to_ovr(nil_val: float | None, position_median: float) -> float | None:
    if nil_val is None or nil_val <= 0 or position_median <= 0:
        return None
    ratio = nil_val / position_median
    return piecewise_interp(ratio, NIL_RATIO_ANCHORS)


# ---------------------------------------------------------------------------
# Data assembly (local JSON — replaces the old player_seasons/recruiting/NIL SQL join)
# ---------------------------------------------------------------------------

def _best_recruiting_by_player(recruiting: pd.DataFrame) -> dict[int, list[dict]]:
    """{player_id: [{recruit_year, composite_score, stars}, ...]} sorted newest first."""
    by_player: dict[int, list[dict]] = defaultdict(list)
    if recruiting.empty:
        return by_player
    for row in recruiting.to_dict("records"):
        pid = row.get("player_id")
        if pid is None or pd.isna(pid):
            continue
        by_player[int(pid)].append({
            "recruit_year":    row.get("recruit_year"),
            "composite_score": row.get("composite_score"),
            "stars":           row.get("stars"),
        })
    for pid in by_player:
        by_player[pid].sort(key=lambda r: -(r["recruit_year"] or 0))
    return by_player


def _nil_by_player(nil_df: pd.DataFrame) -> dict[int, float]:
    if nil_df.empty or "player_id" not in nil_df.columns:
        return {}
    col = "valuation_usd" if "valuation_usd" in nil_df.columns else None
    if col is None:
        return {}
    out: dict[int, float] = {}
    for row in nil_df.to_dict("records"):
        pid = row.get("player_id")
        val = row.get(col)
        if pid is None or val is None or pd.isna(pid) or pd.isna(val):
            continue
        out[int(pid)] = float(val)
    return out


def fetch_player_seasons(season: int, player_seasons: pd.DataFrame,
                         recruiting_idx: dict, nil_idx: dict) -> list[dict]:
    """Player-seasons for one season, joined to their recruiting profile and NIL value."""
    if player_seasons.empty:
        return []
    season_rows = player_seasons[player_seasons["season"] == season]

    rows = []
    for ps in season_rows.to_dict("records"):
        pid = ps.get("player_id")
        pid = int(pid) if pid is not None and not pd.isna(pid) else None

        recruit = None
        for candidate in recruiting_idx.get(pid, []):
            year = candidate.get("recruit_year")
            if year is None:
                continue
            if season - RECRUIT_LOOKBACK_YEARS <= year <= season:
                recruit = candidate
                break

        rows.append({
            "player_season_id":  ps.get("id"),
            "player_id":         pid,
            "season":            season,
            "position_group":    ps.get("position_group"),
            "recruit_composite": (recruit or {}).get("composite_score"),
            "recruit_stars":     (recruit or {}).get("stars"),
            "nil_valuation":     nil_idx.get(pid),
        })
    return rows


def compute_nil_position_medians(rows: list[dict]) -> dict[str, float]:
    """Compute NIL valuation median per position group for ratio scaling."""
    vals_by_pos: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        v = r.get("nil_valuation")
        pg = r.get("position_group") or "ATH"
        if v and v > 0:
            vals_by_pos[pg].append(float(v))

    return {pg: float(np.median(vals)) for pg, vals in vals_by_pos.items() if vals}


# ---------------------------------------------------------------------------
# Rating computation
# ---------------------------------------------------------------------------

def compute_engine_b(row: dict, nil_medians: dict[str, float]) -> dict | None:
    pg = row.get("position_group") or "ATH"

    composite = row.get("recruit_composite")
    if composite is not None and pd.isna(composite):
        composite = None
    rec_ovr = recruit_to_ovr(composite)

    nil_med = nil_medians.get(pg, 0.0)
    nil_ovr = nil_to_ovr(row.get("nil_valuation"), nil_med)

    if rec_ovr is None and nil_ovr is None:
        return None  # not enough data for this player

    if nil_ovr is not None:
        ovr = 0.60 * rec_ovr + 0.40 * nil_ovr if rec_ovr is not None else nil_ovr
    else:
        ovr = rec_ovr

    shap = {}
    if rec_ovr is not None:
        shap["recruit_composite"] = round(rec_ovr - 65.0, 3)  # deviation from neutral
    if nil_ovr is not None:
        shap["nil_valuation"] = round(nil_ovr - 65.0, 3)

    # Columns Engine B does not produce (position_rating, trajectory_*, breakout_*)
    # are left out entirely rather than set to None — concat fills them as NaN and
    # keeps the existing float dtypes instead of forcing object columns.
    return {
        "player_season_id": row["player_season_id"],
        "season":           row["season"],
        "overall_rating":   round(float(np.clip(ovr, 30.0, 99.0)), 2),
        "shap_values":      json.dumps(shap),
        "model_version":    MODEL_VERSION,
        "engine":           ENGINE,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_season(season: int, player_seasons: pd.DataFrame,
               recruiting_idx: dict, nil_idx: dict) -> list[dict]:
    print(f"\n[Engine B] Season {season}")
    rows = fetch_player_seasons(season, player_seasons, recruiting_idx, nil_idx)
    print(f"  {len(rows)} player-seasons loaded")
    if not rows:
        return []

    nil_medians = compute_nil_position_medians(rows)
    has_nil = sum(1 for r in rows if r.get("nil_valuation"))
    print(f"  {has_nil} players have NIL data" +
          (f"; position medians: {nil_medians}" if nil_medians else " (recruiting-only mode)"))

    ratings, skipped = [], 0
    for row in rows:
        r = compute_engine_b(row, nil_medians)
        if r is None:
            skipped += 1
        else:
            ratings.append(r)

    print(f"  Computed {len(ratings)} Engine B ratings ({skipped} skipped - no recruiting/NIL data)")

    # Dedup by player_season_id (one Engine B rating per player-season)
    seen: set = set()
    deduped = []
    for r in ratings:
        if r["player_season_id"] in seen:
            continue
        seen.add(r["player_season_id"])
        deduped.append(r)
    return deduped


def merge_into_ratings(new_rows: list[dict]) -> None:
    """Replace this engine's rows for the affected seasons, keep every other engine."""
    new_df   = pd.DataFrame(new_rows)
    existing = read_computed("ratings")

    if not existing.empty:
        stale = (
            (existing["engine"] == ENGINE) &
            (existing["season"].isin(new_df["season"].unique()))
        )
        combined = pd.concat([existing[~stale], new_df], ignore_index=True)
    else:
        combined = new_df

    write_computed("ratings", combined)


def main():
    parser = argparse.ArgumentParser(description="Engine B ratings -> data/computed/ratings.json")
    parser.add_argument("--season", type=int, action="append", dest="seasons",
                        help="Season to process (default: 2021–2026). Repeatable.")
    args = parser.parse_args()

    seasons = args.seasons or list(range(2021, 2027))
    print(f"Engine B: processing seasons {seasons}")

    print("Loading local JSON...")
    player_seasons = read_raw("player_seasons")
    recruiting_idx = _best_recruiting_by_player(read_raw("recruiting"))
    nil_idx        = _nil_by_player(read_raw("nil_valuations"))
    print(f"  {len(player_seasons)} player-seasons, {len(recruiting_idx)} recruits, "
          f"{len(nil_idx)} NIL valuations")

    all_rows: list[dict] = []
    for season in seasons:
        all_rows.extend(run_season(season, player_seasons, recruiting_idx, nil_idx))

    if not all_rows:
        print("\nNo Engine B ratings computed — nothing written.")
        return

    merge_into_ratings(all_rows)
    print(f"\nDone. {len(all_rows)} engine_b rows written to data/computed/ratings.json")


if __name__ == "__main__":
    main()
