"""Engine B — NIL + Recruiting composite rating.

Produces an overall_rating (30–99) based on recruiting prestige and NIL valuation.
Independent of on-field stats; useful for freshmen, transfers, and cross-season projections.

Formula:
    engine_b_ovr = 0.60 * recruiting_ovr + 0.40 * nil_ovr   (if NIL available)
    engine_b_ovr = recruiting_ovr                             (if no NIL data)

    recruiting_ovr: 247Sports composite (0–1.0 scale) mapped to [30, 99] via piecewise anchors.
    nil_ovr:        On3 NIL valuation (USD/year) log-scaled vs position median, mapped to [30, 99].

Outputs: upserts to ratings table with engine='engine_b'.

Usage:
    python scripts/11_compute_engine_b_ratings.py
    python scripts/11_compute_engine_b_ratings.py --season 2025
    python scripts/11_compute_engine_b_ratings.py --season 2024 --season 2025
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.db import bulk_upsert, get_connection

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
# Data fetch
# ---------------------------------------------------------------------------

def fetch_player_seasons(season: int, conn) -> list[dict]:
    """Return all player_seasons for the given season with recruiting + NIL data."""
    cur = conn.cursor()
    cur.execute("""
        SELECT
            ps.id            AS player_season_id,
            ps.player_id,
            ps.season,
            ps.team_id,
            ps.position_group,
            r.composite_score AS recruit_composite,
            r.stars           AS recruit_stars,
            r.recruit_year,
            n.valuation_usd   AS nil_valuation
        FROM player_seasons ps
        LEFT JOIN recruiting r
            ON r.player_id = ps.player_id
            AND r.recruit_year BETWEEN ps.season - 5 AND ps.season
        LEFT JOIN nil_valuations n
            ON n.player_id = ps.player_id
        WHERE ps.season = %s
        ORDER BY ps.id
    """, (season,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def compute_nil_position_medians(rows: list[dict]) -> dict[str, float]:
    """Compute NIL valuation median per position group for ratio scaling."""
    vals_by_pos: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        v = r.get("nil_valuation")
        pg = r.get("position_group") or "ATH"
        if v and v > 0:
            vals_by_pos[pg].append(float(v))

    medians = {}
    for pg, vals in vals_by_pos.items():
        medians[pg] = float(np.median(vals)) if vals else 0.0
    return medians


# ---------------------------------------------------------------------------
# Rating computation
# ---------------------------------------------------------------------------

def compute_engine_b(row: dict, nil_medians: dict[str, float]) -> dict | None:
    ps_id = row["player_season_id"]
    pg    = row.get("position_group") or "ATH"

    rec_ovr = recruit_to_ovr(row.get("recruit_composite"))
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

    return {
        "player_season_id": ps_id,
        "player_id":        row["player_id"],
        "season":           row["season"],
        "team_id":          row.get("team_id"),
        "overall_rating":   round(float(np.clip(ovr, 30.0, 99.0)), 2),
        "engine":           "engine_b",
        "stars":            row.get("recruit_stars"),
        "composite_score":  row.get("recruit_composite"),
        "shap_values":      json.dumps(shap),
        "model_version":    "engine_b_v1.0",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_season(season: int, conn) -> int:
    print(f"\n[Engine B] Season {season}")
    rows = fetch_player_seasons(season, conn)
    print(f"  {len(rows)} player-seasons loaded")

    nil_medians = compute_nil_position_medians(rows)
    has_nil = sum(1 for r in rows if r.get("nil_valuation"))
    print(f"  {has_nil} players have NIL data; position medians: {nil_medians}")

    ratings = []
    skipped = 0
    for row in rows:
        r = compute_engine_b(row, nil_medians)
        if r is None:
            skipped += 1
        else:
            ratings.append(r)

    print(f"  Computed {len(ratings)} Engine B ratings ({skipped} skipped — no recruiting/NIL data)")

    if not ratings:
        return 0

    # Dedup by player_season_id + engine
    seen: set = set()
    deduped = []
    for r in ratings:
        key = (r["player_season_id"], r["engine"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    bulk_upsert("ratings", deduped, ["player_season_id", "engine"])
    print(f"  Upserted {len(deduped)} rows to ratings (engine_b)")
    return len(deduped)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int, action="append", dest="seasons",
                        help="Season to process (default: 2021–2026). Repeatable.")
    args = parser.parse_args()

    seasons = args.seasons or list(range(2021, 2027))
    print(f"Engine B: processing seasons {seasons}")

    with get_connection() as conn:
        total = 0
        for season in seasons:
            total += run_season(season, conn)

    print(f"\nDone. Total rows upserted: {total}")


if __name__ == "__main__":
    main()
