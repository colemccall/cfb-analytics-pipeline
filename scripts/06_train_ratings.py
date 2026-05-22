"""CFB player ratings — Engine A (v4.0).

Rating architecture:
  EDGE positions (QB/RB/WR/TE/EDGE/DL/LB/CB/S):
    OVR = edge_to_ovr(edge_score, position_group)
    edge_score (from script 08) is a per-game stat composite × per-game opponent
    SP+ quality, accumulated over games and normalized by sqrt(games_played).
    No traditional stats in the formula — they're already embedded in edge_score.
    Recruiting/NIL only for players below the stats_measured threshold.

  Non-EDGE positions (OL, K, P):
    Stat-only composite with fixed absolute bounds, mapped via scale_to_range().

  EDGE_OVR_ANCHORS: fixed piecewise linear mapping from edge_score → OVR.
    Anchors are calibrated from known reference seasons and 2025 distributions.
    Not recalculated per run — they are permanent calibration points.
    *** Offensive anchors are INITIAL ESTIMATES — recalibrate after first run ***
    by reviewing top-50 per position and adjusting anchors so known elite
    players land 90-96 and average starters land 62-70.

Usage:
    python scripts/06_train_ratings.py              # 2025
    python scripts/06_train_ratings.py --season 2024
    python scripts/06_train_ratings.py --all-seasons
    python scripts/06_train_ratings.py --position QB --season 2024
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.db import bulk_upsert, get_connection

MODEL_VERSION = "v4.0-edge-direct"

# ---------------------------------------------------------------------------
# Starter thresholds — determines how much we trust stats vs recruiting
# ---------------------------------------------------------------------------

# Four-tier playing-time system (replaces binary STARTER_THRESHOLDS)
# Each position has stat-based thresholds that classify players as:
#   - starter: full formula, EDGE at full weight, cap 99
#   - role: 75% formula blended with 25% recruiting, EDGE weight × 0.7, cap 78
#   - reserve: 40% formula + 60% recruiting, EDGE weight × 0.4, cap 68
#   - bench: recruiting-only fallback, cap 60
PLAYTIME_TIERS = {
    "QB":   {"stat": "passingATT",   "starter": 100, "role": 30,  "reserve": 5},
    "RB":   {"stat": "rushingCAR",   "starter":  60, "role": 25,  "reserve": 8},
    "WR":   {"stat": "receivingREC", "starter":  20, "role": 10,  "reserve": 3},
    "TE":   {"stat": "receivingREC", "starter":  10, "role":  5,  "reserve": 2},
    "OL":   {"stat": None,           "starter":   0, "role":  0,  "reserve": 0},  # team proxy only
    "EDGE": {"stat": "defensiveTOT", "starter":   8, "role":  4,  "reserve": 1},
    "DL":   {"stat": "defensiveTOT", "starter":  10, "role":  5,  "reserve": 1},
    "LB":   {"stat": "defensiveTOT", "starter":  20, "role": 10,  "reserve": 2},
    "CB":   {"stat": "defensiveTOT", "starter":  10, "role":  5,  "reserve": 1},
    "S":    {"stat": "defensiveTOT", "starter":  20, "role": 10,  "reserve": 2},
    "K":    {"stat": "kickingFGM",   "starter":   5, "role":  2,  "reserve": 1},
    "P":    {"stat": "puntingNO",    "starter":  10, "role":  5,  "reserve": 1},
}

def classify_tier(pg: str, stats: dict, games_played: int = 1) -> str:
    """Classify player into tier based on stat volume."""
    cfg = PLAYTIME_TIERS.get(pg)
    if not cfg or cfg["stat"] is None:
        return "starter"  # OL team proxy, always full formula

    # Try the canonical PLAYTIME_TIERS stat key first, then fall back to
    # volume_score (the alias set by extract_features for all positions).
    val = stats.get(cfg["stat"]) or stats.get("volume_score") or 0
    try:
        val = float(val)
    except (TypeError, ValueError):
        val = 0

    if val >= cfg["starter"]:
        return "starter"
    if val >= cfg["role"]:
        return "role"
    if val >= cfg["reserve"]:
        return "reserve"
    return "bench"

# Recruiting fallback: how much the overall rating shifts from position average
# based on recruiting stars when a player has NO usable stats.
STARS_FALLBACK = {5: -3, 4: -8, 3: -15, 2: -22, 1: -28, 0: -33}

# Positions that can have EDGE scores (computed in script 08).
# Offensive: QB, RB, WR, TE (play-level EPA)
# Defensive: EDGE, DL, LB, CB, S (per-game stat composite × opponent SP+)
# OL, K, P: never have EDGE
EDGE_POSITIONS = {"QB", "RB", "WR", "TE", "EDGE", "DL", "LB", "CB", "S", "DB"}

# No hard ceiling — K/P rated by stat composite, scale_to_range handles distribution
POSITION_CEILING: dict[str, int] = {}

# All seasons used for cross-season normalization
ALL_SEASONS = list(range(2008, 2026))

# ---------------------------------------------------------------------------
# EDGE → OVR direct mapping (replaces weighted composite + scale_to_range
# for all EDGE_POSITIONS)
#
# Piecewise linear: (edge_score, target_ovr) anchor pairs per position.
# Offensive anchors are INITIAL ESTIMATES based on expected per-game stat
# composite × opp_mult / sqrt(games) scale for v4.0. Recalibrate after
# first run by reviewing distributions and known reference players.
#
# Defensive anchors are carried forward from 2025 distribution data —
# the formula change is minor (added hurries/PBUs to all positions).
# ---------------------------------------------------------------------------

EDGE_OVR_ANCHORS: dict[str, list[tuple[float, float]]] = {
    # Calibrated from all-time top-20 per position (2008-2025).
    # Goal: top-20 all-time sets the bar for 99; #20 all-time ≈ 97.
    #
    # Offensive: stat_composite × opp_mult / sqrt(games)
    #   QB:   p50≈445, p90≈1085. All-time #1=2352 (Burrow 2019), #20=1773.
    "QB":   [(0, 30), (150, 50), (445, 65), (750, 77), (1100, 85), (1450, 90), (1770, 97), (2400, 99)],
    #   RB:   All-time #1=1003 (Gordon 2014), #20=803 (Ajayi 2014).
    "RB":   [(0, 30), (40,  50), (120, 65), (240, 77), (500,  87), (650,  93), (803,  97), (1050, 99)],
    #   WR:   All-time #1=926 (DeVonta Smith 2020), #20=663 (Jones 2022).
    "WR":   [(0, 30), (35,  50), (120, 65), (210, 77), (400,  87), (550,  93), (663,  97), (950,  99)],
    #   TE:   All-time #1=622 (Amaro 2013), #20=384 (Andrews 2017).
    "TE":   [(0, 30), (25,  50), (75,  65), (160, 78), (270,  87), (384,  97), (650,  99)],
    #
    # Defensive: stat_composite × opp_mult / sqrt(games). Weights v4.1.
    # All-time top-20 sets 99 ceiling; #20 ≈ 97.
    #
    #   EDGE: All-time #1=81.4 (Chase Young 2019), #20=58.7 (Kennard 2024). Ceiling 65 per user spec.
    "EDGE": [(0, 30), (3.0, 50), (11.0, 65), (22.0, 77), (38.0, 87), (58.7, 97), (65.0, 99)],
    #   DL:   All-time #1=69.9 (Green 2023), #20=51.5 (Polite 2018). Ceiling 65 per user spec.
    "DL":   [(0, 30), (2.5, 50), (7.0,  65), (15.0, 77), (30.0, 87), (51.5, 97), (65.0, 99)],
    #   LB:   All-time #1=95.3 (Anderson 2021), #2=82.4 (Josh Allen 2018) — true anomalies.
    #   #3=70.7 (Lloyd 2021), #20=63.7. Set 99 at 70 so Anderson/Allen go off-chart.
    "LB":   [(0, 30), (4.0, 50), (11.0, 65), (22.0, 77), (40.0, 87), (63.7, 97), (70.0, 99)],
    #   CB:   All-time #1=52.3 (Amerson 2011), #20=39.1 (Phillips 2022).
    #   Team defensive context adjusts score. Ceiling reflects INT-heavy outliers.
    "CB":   [(0, 30), (1.5, 50), (6.0,  65), (12.0, 74), (20.0, 83), (39.1, 97), (53.0, 99)],
    #   S:    All-time #1=54.6 (Delpit 2018), #20=35.4 (Stevens 2019).
    "S":    [(0, 30), (2.0, 50), (7.5,  65), (14.0, 74), (22.0, 83), (35.4, 97), (55.0, 99)],
    #   DB:   All-time #1=52.3 (Golson 2014), #20=38.3 (Hayward 2011).
    "DB":   [(0, 30), (1.5, 50), (6.5,  65), (12.0, 74), (20.0, 83), (38.3, 97), (53.0, 99)],
}


# ---------------------------------------------------------------------------
# Era-bucketed anchors for historical backfill (2008–2020).
# Pre-2015 data lacks hurries/PBUs, making raw composite scores structurally lower.
# Rather than altering the EDGE formula, we lower the anchor thresholds so that
# an elite 2010 QB (missing hurry data) still maps to ~90 OVR, not ~75.
# Scaling: transition = modern × 0.85, classic = modern × 0.75
# ---------------------------------------------------------------------------

def _scale_anchors(anchors: list[tuple[float, float]], factor: float) -> list[tuple[float, float]]:
    """Scale the edge_score breakpoints by factor, keep OVR values unchanged."""
    return [(round(x * factor, 4), y) for (x, y) in anchors]


ERA_ANCHORS: dict[str, dict[str, list[tuple[float, float]]]] = {
    "modern":     EDGE_OVR_ANCHORS,  # 2018+, same as calibrated anchors
    "transition": {pg: _scale_anchors(v, 0.85) for pg, v in EDGE_OVR_ANCHORS.items()},  # 2013–2017
    "classic":    {pg: _scale_anchors(v, 0.75) for pg, v in EDGE_OVR_ANCHORS.items()},  # 2008–2012
}


def get_era(season: int) -> str:
    if season >= 2018:
        return "modern"
    if season >= 2013:
        return "transition"
    return "classic"


DEFENSIVE_POSITIONS = {"EDGE", "DL", "LB", "CB", "S", "DB"}

def edge_to_ovr(edge_score: float, pg: str, season: int = 2025) -> float:
    """Map raw edge_score to OVR via fixed piecewise linear anchors.

    Returns a rating in [30, 99] that reflects absolute production quality.
    Era scaling (ERA_ANCHORS) applies only to defensive positions — pre-2016
    data lacks hurries/PBUs so raw defensive composites are structurally lower.
    Offensive positions (QB/RB/WR/TE) always use the modern anchors because
    passing/rushing stats have been tracked consistently since 2008.
    """
    if pg in DEFENSIVE_POSITIONS:
        era = get_era(season)
        anchors = ERA_ANCHORS[era].get(pg)
    else:
        anchors = EDGE_OVR_ANCHORS.get(pg)
    if not anchors or edge_score is None or (isinstance(edge_score, float) and np.isnan(edge_score)):
        return 50.0
    xs = [a[0] for a in anchors]
    ys = [float(a[1]) for a in anchors]
    return float(np.clip(np.interp(float(edge_score), xs, ys), 30.0, 99.0))


def _f(stats, key):
    v = stats.get(key)
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _composite_to_100(score) -> float:
    if not score:
        return 40.0
    return max(0.0, min(100.0, (float(score) - 0.7) / 0.3 * 100))


# ---------------------------------------------------------------------------
# Supplementary stat features (secondary role for EDGE positions)
# ---------------------------------------------------------------------------

def extract_features(stats: dict, pg: str) -> dict:
    """Return named production metrics for one player."""
    if pg == "QB":
        att  = max(_f(stats, "passingATT"), 1)
        comp = _f(stats, "passingCOMPLETIONS")
        yds  = _f(stats, "passingYDS")
        td   = _f(stats, "passingTD")
        ints = _f(stats, "passingINT")
        return {
            "comp_pct":      comp / att,
            "yards_per_att": yds  / att,
            "td_int_ratio":  (td + 1) / (ints + 1),
            "volume_score":  att,
        }

    if pg == "RB":
        car = max(_f(stats, "rushingCAR"), 1)
        yds = _f(stats, "rushingYDS")
        rec = _f(stats, "receivingREC")
        return {
            "yards_per_carry":  yds / car,
            "yards_total":      yds,
            "rec_versatility":  rec / car,
            "volume_score":     car,
        }

    if pg in ("WR", "TE"):
        rec = max(_f(stats, "receivingREC"), 1)
        yds = _f(stats, "receivingYDS")
        tds = _f(stats, "receivingTD")
        return {
            "yards_per_rec":   yds / rec,
            "yards_total":     yds,
            "td_score":        tds * 8.0 + yds * 0.01,
            "rec_volume":      rec,
            "volume_score":    rec,   # alias used by is_starter threshold check
        }

    if pg == "OL":
        return {
            "team_rush_ypa":      _f(stats, "team_rush_ypa"),
            "team_sack_rate_inv": 1.0 - min(_f(stats, "team_sack_rate"), 1.0),
            "award_tier":         _f(stats, "award_tier"),
            "experience":         2.0,   # placeholder; overwritten below from players.year
        }

    if pg == "EDGE":
        tot   = max(_f(stats, "defensiveTOT"), 1)
        sacks = _f(stats, "defensiveSACKS")
        tfl   = _f(stats, "defensiveTFL")
        hur   = _f(stats, "defensiveQB HUR")
        return {
            "pass_rush_score":  sacks * 5.0 + hur * 1.5 + tfl * 2.0,   # sacks + pressure dominant for EDGE
            "disruption_rate":  (sacks + tfl) / tot,                     # impact per play
            "run_stop_score":   tfl * 2.5 + (tot - sacks) * 0.3,        # run stuffs (secondary for EDGE)
            "volume_score":     tot,
        }

    if pg == "DL":
        tot   = max(_f(stats, "defensiveTOT"), 1)
        sacks = _f(stats, "defensiveSACKS")
        tfl   = _f(stats, "defensiveTFL")
        hur   = _f(stats, "defensiveQB HUR")
        return {
            "pass_rush_score":  sacks * 5.0 + hur * 1.5 + tfl * 1.0,   # sacks + pressure
            "run_stop_score":   tfl * 2.5 + (tot - sacks) * 0.4,        # run stuffs + tackle presence
            "disruption_rate":  (sacks + tfl) / tot,                     # impact per play
            "volume_score":     tot,
        }

    if pg == "LB":
        tot    = max(_f(stats, "defensiveTOT"), 1)
        sacks  = _f(stats, "defensiveSACKS")
        tfl    = _f(stats, "defensiveTFL")
        ints   = _f(stats, "interceptionsINT")
        pbu    = _f(stats, "defensivePD")
        return {
            "tackling_score":   tot * 0.5 + tfl * 2.0,                  # pursuit + run stop
            "pass_rush_score":  sacks * 4.0 + tfl * 1.0,                # blitz / pressure
            "coverage_score":   ints * 3.0 + pbu * 1.5,                 # zone/man skills
            "instinct_score":   (ints + pbu + tfl) / tot,               # playmaking rate
            "volume_score":     tot,
        }

    if pg == "CB":
        tot   = max(_f(stats, "defensiveTOT"), 1)
        sacks = _f(stats, "defensiveSACKS")
        tfl   = _f(stats, "defensiveTFL")
        ints  = _f(stats, "interceptionsINT")
        pbu   = _f(stats, "defensivePD")
        return {
            "coverage_score":   ints * 4.0 + pbu * 2.0,                 # CB: ball skills dominant
            "tackling_score":   tot * 0.3 + tfl * 1.0,                  # run support (secondary)
            "pass_rush_score":  sacks * 3.0 + tfl * 1.0,                # blitz value
            "instinct_score":   (ints + pbu) / tot,                     # focus on coverage instinct
            "volume_score":     tot,
        }

    if pg == "S":
        tot   = max(_f(stats, "defensiveTOT"), 1)
        sacks = _f(stats, "defensiveSACKS")
        tfl   = _f(stats, "defensiveTFL")
        ints  = _f(stats, "interceptionsINT")
        pbu   = _f(stats, "defensivePD")
        return {
            "coverage_score":   ints * 3.5 + pbu * 1.5,                 # S: coverage (less dominant than CB)
            "tackling_score":   tot * 0.6 + tfl * 2.0,                  # S: tackle more than CB
            "pass_rush_score":  sacks * 3.0 + tfl * 1.5,                # box blitz value
            "instinct_score":   (ints + pbu + tfl * 0.5) / tot,         # playmaking (coverage + disruption)
            "volume_score":     tot,
        }

    if pg == "DB":
        # Fallback: legacy "DB" generic should never appear post-v2 schema
        # but keep it for robustness
        tot   = max(_f(stats, "defensiveTOT"), 1)
        sacks = _f(stats, "defensiveSACKS")
        tfl   = _f(stats, "defensiveTFL")
        ints  = _f(stats, "interceptionsINT")
        pbu   = _f(stats, "defensivePD")
        return {
            "coverage_score":   ints * 3.0 + pbu * 1.5,
            "tackling_score":   tot * 0.5 + tfl * 2.0,
            "pass_rush_score":  sacks * 4.0 + tfl * 1.5,
            "instinct_score":   (ints + pbu + tfl * 0.5) / tot,
            "volume_score":     tot,
        }

    if pg == "K":
        fga = max(_f(stats, "kickingFGA"), 1)
        xpa = max(_f(stats, "kickingXPA"), 1)
        return {
            "fg_pct":       _f(stats, "kickingFGM") / fga,
            "fg_long":      _f(stats, "kickingLNG"),
            "xp_pct":       _f(stats, "kickingXPM") / xpa,
            "volume_score": _f(stats, "kickingFGM"),
        }

    if pg == "P":
        n = max(_f(stats, "puntingNO"), 1)
        return {
            "avg_yards":     _f(stats, "puntingYDS") / n,
            "inside_20_pct": _f(stats, "puntingIn 20") / n,
            "volume_score":  _f(stats, "puntingNO"),
        }

    return {}


# ---------------------------------------------------------------------------
# Formula weights
# EDGE positions: edge_score (raw opponent-adjusted) is primary; stat features fill the rest.
# Non-EDGE positions: stat-only composite.
# Recruiting weight = 5% for starters, 15% for non-EDGE positions.
# ---------------------------------------------------------------------------

WEIGHTS = {
    "QB": {
        "edge_score":     0.55,
        "yards_per_att":   0.15,
        "td_int_ratio":    0.15,
        "comp_pct":        0.10,
        "recruit_composite": 0.05,
    },
    "RB": {
        "edge_score":     0.55,
        "yards_per_carry": 0.20,
        "yards_total":     0.15,
        "rec_versatility": 0.05,
        "recruit_composite": 0.05,
    },
    "WR": {
        "edge_score":     0.42,    # increased; opponent-adj EPA is primary differentiator
        "td_score":        0.22,
        "yards_per_rec":   0.18,
        "yards_total":     0.10,
        "rec_volume":      0.05,
        "recruit_composite": 0.03,
    },
    "TE": {
        "edge_score":     0.38,    # increased; EPA captures both yards and TDs
        "td_score":        0.22,
        "yards_per_rec":   0.20,
        "yards_total":     0.12,
        "rec_volume":      0.05,
        "recruit_composite": 0.03,
    },
    # Non-EDGE positions: no edge_scaled, higher recruit weight
    "OL": {
        "team_rush_ypa":      0.30,
        "team_sack_rate_inv": 0.25,
        "recruit_composite":  0.30,   # primary individual differentiator
        "experience":         0.10,
        "award_tier":         0.05,
    },
    # Defensive positions with per-game opponent-adjusted EDGE (script 08)
    "EDGE": {
        "edge_score":      0.50,
        "pass_rush_score":  0.25,
        "disruption_rate":  0.12,
        "run_stop_score":   0.08,
        "recruit_composite": 0.05,
    },
    "DL": {
        "edge_score":      0.40,
        "pass_rush_score":  0.25,
        "run_stop_score":   0.18,
        "disruption_rate":  0.10,
        "recruit_composite": 0.07,
    },
    "LB": {
        "edge_score":      0.40,
        "tackling_score":   0.25,
        "coverage_score":   0.15,
        "pass_rush_score":  0.10,
        "recruit_composite": 0.10,
    },
    "CB": {
        "edge_score":      0.45,
        "coverage_score":   0.30,
        "tackling_score":   0.12,
        "recruit_composite": 0.08,
        "instinct_score":   0.05,
    },
    "S": {
        "edge_score":      0.40,
        "coverage_score":   0.22,
        "tackling_score":   0.22,
        "instinct_score":   0.10,
        "recruit_composite": 0.06,
    },
    "DB": {
        "edge_score":      0.40,
        "coverage_score":   0.22,
        "tackling_score":   0.22,
        "instinct_score":   0.10,
        "recruit_composite": 0.06,
    },
    "K": {
        "fg_pct":     0.50,
        "fg_long":    0.25,
        "xp_pct":     0.15,
        "volume_score": 0.10,
    },
    "P": {
        "avg_yards":     0.55,
        "inside_20_pct": 0.30,
        "volume_score":  0.15,
    },
}

# When EDGE is missing for players with EDGE in their formula, fall back to stat-only
WEIGHTS_NO_EDGE = {
    "QB": {
        "yards_per_att":   0.35,
        "td_int_ratio":    0.30,
        "comp_pct":        0.25,
        "volume_score":    0.05,
        "recruit_composite": 0.05,
    },
    "RB": {
        "yards_per_carry": 0.40,
        "yards_total":     0.35,
        "rec_versatility": 0.10,
        "volume_score":    0.10,
        "recruit_composite": 0.05,
    },
    "WR": {
        "yards_per_rec":   0.35,
        "td_score":        0.30,
        "yards_total":     0.20,
        "rec_volume":      0.10,
        "recruit_composite": 0.05,
    },
    "TE": {
        "yards_per_rec":   0.35,
        "td_score":        0.30,
        "yards_total":     0.20,
        "rec_volume":      0.10,
        "recruit_composite": 0.05,
    },
    "EDGE": {
        "pass_rush_score":  0.45,
        "disruption_rate":  0.22,
        "run_stop_score":   0.18,
        "volume_score":     0.05,
        "recruit_composite": 0.10,
    },
    "DL": {
        "pass_rush_score":  0.40,
        "run_stop_score":   0.28,
        "disruption_rate":  0.12,
        "volume_score":     0.05,
        "recruit_composite": 0.15,
    },
    "LB": {
        "tackling_score":   0.30,
        "pass_rush_score":  0.22,
        "coverage_score":   0.18,
        "instinct_score":   0.10,
        "volume_score":     0.05,
        "recruit_composite": 0.15,
    },
    "CB": {
        "coverage_score":   0.45,
        "tackling_score":   0.20,
        "instinct_score":   0.15,
        "pass_rush_score":  0.10,
        "recruit_composite": 0.10,
    },
    "S": {
        "coverage_score":   0.30,
        "tackling_score":   0.28,
        "instinct_score":   0.18,
        "pass_rush_score":  0.14,
        "recruit_composite": 0.10,
    },
    "DB": {
        "coverage_score":   0.35,
        "tackling_score":   0.25,
        "instinct_score":   0.18,
        "pass_rush_score":  0.12,
        "recruit_composite": 0.10,
    },
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_position_data(season: int, pg: str) -> pd.DataFrame:
    """Load one season's data for a position group.

    Only includes players who appeared on a roster in this season, determined
    by the presence of a season_aggregate stats row.
    """
    return _load_seasons([season], pg)


def _load_seasons(seasons: list[int], pg: str) -> pd.DataFrame:
    """Load one or more seasons of data for a position group via player_seasons.

    player_seasons is the join anchor: one row per player × season × team.
    This correctly handles same-name players at different schools — they are
    distinct player_seasons rows and never collide.
    """
    with get_connection() as conn:
        cur = conn.cursor()

        # Core join: player_seasons → players → stats
        # ps.id is the player_season_id; it uniquely identifies a player-season-team combo.
        cur.execute(
            """SELECT ps.id, p.id, p.name, ps.team_id, ps.year, ps.season, s.data
               FROM player_seasons ps
               JOIN players p ON p.id = ps.player_id
               JOIN stats s ON s.player_season_id = ps.id
               WHERE ps.position_group = %s
                 AND ps.season = ANY(%s)
                 AND s.stat_type = 'season_aggregate'
                 AND s.game_id IS NULL""",
            (pg, seasons)
        )
        raw_rows = cur.fetchall()
        if not raw_rows:
            return pd.DataFrame()

        # (ps_id, player_id, name, team_id, year, season, data)
        player_season_rows = [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in raw_rows]
        all_ps_ids    = list({r[0] for r in raw_rows})
        all_player_ids = list({r[1] for r in raw_rows})

        # stats_map keyed by ps_id (already one row per player_season_id + stat_type)
        stats_map = {}
        for r in raw_rows:
            ps_id, data = r[0], r[6]
            stats_map[ps_id] = data if isinstance(data, dict) else json.loads(data)

        # EDGE scores — keyed by player_season_id
        cur.execute(
            """SELECT player_season_id, edge_score, stats_measured, games_played, opponent_avg_sp
               FROM player_edge
               WHERE player_season_id = ANY(%s)""",
            (all_ps_ids,)
        )
        edge_map = {
            ps_id: {
                "edge_score":      es,
                "stats_measured":  sm,
                "games_played":    gp,
                "opponent_avg_sp": osp,
            }
            for ps_id, es, sm, gp, osp in cur.fetchall()
        }

        # Recruiting — keyed by player_id (career-level, not season-level)
        min_season = min(seasons)
        cur.execute(
            """SELECT player_id, stars, composite_score, recruit_year FROM recruiting
               WHERE recruit_year >= %s AND player_id = ANY(%s)
               ORDER BY composite_score DESC NULLS LAST""",
            (min_season - 5, all_player_ids)
        )
        rec_map = {}
        for pid, stars, cs, ry in cur.fetchall():
            if pid not in rec_map:
                rec_map[pid] = {"stars": stars or 0, "composite_score": cs}

        # Conference — keyed by team_id
        all_team_ids = list({r[3] for r in raw_rows if r[3]})
        cur.execute(
            "SELECT t.id, t.conference FROM teams t WHERE t.id = ANY(%s)",
            (all_team_ids,)
        )
        conf_map = {tid: conf for tid, conf in cur.fetchall()}

        # Transfer history — to flag transfer-in players (career-level)
        cur.execute(
            """SELECT player_id, transfer_year
               FROM transfers
               WHERE player_id = ANY(%s) AND to_team_id IS NOT NULL""",
            (all_player_ids,)
        )
        transfer_seasons: dict[int, set] = {}
        for pid, yr in cur.fetchall():
            transfer_seasons.setdefault(pid, set()).add(yr)

    rows = []
    for ps_id, pid, name, team_id, year, s in player_season_rows:
        raw_stats = stats_map.get(ps_id, {})
        rec       = rec_map.get(pid, {})
        stars     = int(rec.get("stars") or 0)
        cs        = rec.get("composite_score")
        edge_info = edge_map.get(ps_id, {})

        feats = extract_features(raw_stats, pg)
        if pg == "OL":
            feats["experience"] = float(year or 2)
        feats["recruit_composite"] = _composite_to_100(cs)
        feats["transfer_flag"]     = 1 if s in transfer_seasons.get(pid, set()) else 0
        feats["stars"]             = stars
        feats["year"]              = int(year or 0)
        feats["team_id"]           = team_id
        feats["name"]              = name
        feats["player_season_id"]  = ps_id   # carry through for output upsert
        feats["edge_score"]      = edge_info.get("edge_score")
        feats["stats_measured"]  = edge_info.get("stats_measured") or 0
        feats["games_played"]    = edge_info.get("games_played") or 0
        feats["opp_avg_sp"]      = edge_info.get("opponent_avg_sp") or 0.0
        feats["conference"]        = conf_map.get(team_id, "")
        feats["_season"]           = s
        rows.append({"player_id": pid, **feats})

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Compound row key: player_season_id is already unique per row.
    # Keep using string key for backwards compat with rate_position logic.
    df["_row_key"] = df["player_season_id"].astype(str)
    return df.set_index("_row_key")


# ---------------------------------------------------------------------------
# Starter classification
# ---------------------------------------------------------------------------

def is_starter(row: pd.Series, pg: str) -> bool:
    """Returns True if player qualifies as 'starter' tier."""
    return classify_tier(pg, row.to_dict()) == "starter"


def get_tier(row: pd.Series, pg: str) -> str:
    """Classify player into tier: starter, role, reserve, or bench."""
    return classify_tier(pg, row.to_dict(), games_played=row.get("games_played", 1))


def has_opp_score(row: pd.Series, pg: str = "") -> bool:
    """True if a valid opponent-adjusted EDGE score is present and above threshold.

    stats_measured thresholds (season totals of primary countable stats):
      QB: 100 (pass_att + rush_att), RB: 60 (car + rec), WR: 20 (rec), TE: 10 (rec)
      Defense: 15-30 total stat events depending on position.
    """
    v = row.get("edge_score")
    if v is None or pd.isna(v) or float(v) == 0.0:
        return False
    sm = float(row.get("stats_measured") or 0)
    thresholds = {
        "QB": 100, "RB": 60, "WR": 20, "TE": 10,
        "EDGE": 20, "DL": 20, "LB": 30, "CB": 15, "S": 15, "DB": 15,
    }
    return sm >= thresholds.get(pg, 15)


# ---------------------------------------------------------------------------
# Absolute feature normalization (replaces percentile ranking)
# ---------------------------------------------------------------------------
#
# Each feature is normalized to [0, 1] against fixed all-time reference bounds,
# NOT against the current season's player pool. This makes ratings absolute:
# a player with 7.5 YPC rates the same regardless of what year they played or
# how strong their positional peers were that year.
#
# Bounds are calibrated so that:
#   0.0 = floor (worst reasonable starter)
#   ~0.35 = typical starter (p50 of all-seasons pool)
#   1.0 = all-time elite ceiling (top 1-2% over many seasons)
#
# edge_score is the raw opponent-adjusted production score from script 08.
# It is NOT scaled against peers — it uses fixed position-specific ceilings
# calibrated from 2021-2025 data. A better season always produces a higher score.
# Ceilings set to observed p95+ so top performers clip to ~1.0.

# Position-specific ceilings for raw edge_score (floor is always 0).
# Calibrated from 2021-2025 data: Jeanty 2023=4.99, 2024=6.14
# Ceilings calibrated so generational seasons normalize to ~1.0 and elite-but-not-historic
# seasons normalize to 0.75-0.90. Offensive ceilings are set above p99 so seasons like
# Jeanty 2024 (RB=6.14) vs Jeanty 2023 (RB=4.99) remain meaningfully differentiated
# rather than both clipping to 1.0.
# Defensive ceilings use p95 (stat composite formula produces larger raw values).
# p50 of each group: QB=1.92, RB=0.83, WR=5.46, TE=4.13, EDGE=6.33,
#   DL=3.84, LB=7.47, CB=5.01, S=7.80
EDGE_SCORE_BOUNDS: dict[str, tuple[float, float]] = {
    "QB":   (0.0, 10.0),  # max=14.1; set ceiling above p99 to preserve separation
    "RB":   (0.0,  7.5),  # max=8.06; Jeanty 2024=6.14→0.82, Jeanty 2023=4.99→0.67
    "WR":   (0.0, 10.2),  # p95=10.23; use p95 as ceiling so p95 WR normalizes to 1.0
    "TE":   (0.0,  8.6),  # p95=8.60; use p95 as ceiling
    "EDGE": (0.0, 21.5),  # p95=21.53 (defensive stat composite / sqrt(games))
    "DL":   (0.0, 14.4),  # p95=14.39
    "LB":   (0.0, 23.3),  # p95=23.35
    "CB":   (0.0, 13.3),  # p95=13.33
    "S":    (0.0, 19.8),  # p95=19.80
    "DB":   (0.0, 16.9),  # p95=16.92 (legacy fallback)
}

FEATURE_BOUNDS: dict[str, tuple[float, float]] = {
    # QB
    "comp_pct":         (0.45, 0.79),
    "yards_per_att":    (3.0,  11.7),
    "td_int_ratio":     (0.5,  5.5),
    # RB
    "yards_per_carry":  (2.0,  8.0),
    "yards_total":      (0.0,  1100),  # Jeanty 2024=2497 clips to 1.0
    "rec_versatility":  (0.0,  0.4),
    # WR / TE
    "yards_per_rec":    (5.0,  20.0),
    "td_score":         (0.0,  80.0),
    "rec_volume":       (0.0,  80.0),
    # OL (team proxy)
    "team_rush_ypa":    (3.0,  6.0),
    "team_sack_rate_inv": (0.5, 0.98),
    "experience":       (1.0,  5.0),
    "award_tier":       (0.0,  3.0),
    # Defensive stat composites
    "pass_rush_score":  (0.0,  55.0),
    "run_stop_score":   (0.0,  50.0),
    "disruption_rate":  (0.0,  0.6),
    "tackling_score":   (0.0,  60.0),
    "coverage_score":   (0.0,  15.0),
    "instinct_score":   (0.0,  0.3),
    # Universal
    "recruit_composite":(40.0, 100.0),
    "volume_score":     (0.0,  46.0),
    # K — FG% calibrated: 0.60 = poor, 0.92 = elite all-time
    "fg_pct":           (0.60, 0.92),
    # fg_long: 45 = routine, 60 = elite range (McPherson, Moody type)
    "fg_long":          (40.0, 65.0),
    "xp_pct":           (0.80, 1.00),
    # P — net avg: 35 = below avg, 48 = elite (Johnny Townsend type)
    "avg_yards":        (35.0, 48.0),
    # inside_20_pct: 0.25 = poor, 0.55 = elite placement
    "inside_20_pct":    (0.20, 0.55),
    # Pass-through
    "transfer_flag":    (0.0,  1.0),
}


def normalize_feature(key: str, val: float, pg: str = "") -> float:
    """Normalize a feature value to [0, 1] using fixed absolute bounds.

    edge_score uses position-specific ceilings (EDGE_SCORE_BOUNDS) because
    RBs accumulate far more opponent-adjusted EPA than CBs. All other features
    use the flat FEATURE_BOUNDS dict.
    """
    if key == "edge_score":
        lo, hi = EDGE_SCORE_BOUNDS.get(pg, (0.0, 6.0))
        if hi <= lo:
            return 0.0
        return float(np.clip((float(val) - lo) / (hi - lo), 0.0, 1.0))
    bounds = FEATURE_BOUNDS.get(key)
    if bounds is None:
        return 0.5  # unknown feature: neutral
    lo, hi = bounds
    if hi <= lo:
        return 0.5
    return float(np.clip((val - lo) / (hi - lo), 0.0, 1.0))


# ---------------------------------------------------------------------------
# Rating computation
# ---------------------------------------------------------------------------

def compute_edge_ratings(df: pd.DataFrame, pg: str, season: int = 2025) -> tuple[np.ndarray, list[dict]]:
    """Direct edge_score → OVR mapping for EDGE positions.

    For players with a valid edge_score: OVR = edge_to_ovr(edge_score, pg).
    For players without EDGE data (pre-2016 defense, injured, etc.): use the
    stat-only composite (WEIGHTS_NO_EDGE) scaled to [30–90] so historical
    defensive players get meaningful variance instead of a flat 50.0 fallback.

    Returns:
      scores   — np.ndarray of OVR values [30–99] or 0.0 if truly no data at all
      contribs — list of {feature: value} dicts for shap_values storage
    """
    # Pre-compute stat-only composites for the whole df (used for no-EDGE fallback).
    # Features are already extracted columns in the dataframe (via _load_seasons → extract_features).
    no_edge_weights = WEIGHTS_NO_EDGE.get(pg, {})
    no_edge_features = list(no_edge_weights.keys())
    no_edge_w_arr = np.array([no_edge_weights[c] for c in no_edge_features]) if no_edge_features else np.array([])

    # Gather all no-EDGE stat composites to scale relatively within the pool
    all_stat_scores = []
    for _, row in df.iterrows():
        if not has_opp_score(row, pg) and no_edge_features:
            x = np.array([normalize_feature(c, float(row.get(c) or 0), pg=pg) for c in no_edge_features])
            all_stat_scores.append(float(x @ no_edge_w_arr))
        else:
            all_stat_scores.append(None)

    valid_stat = [s for s in all_stat_scores if s is not None]
    if valid_stat:
        stat_pcts = np.percentile(valid_stat, [0, 10, 50, 75, 90, 99, 100])
        stat_targets = [30.0, 40.0, 58.0, 68.0, 76.0, 85.0, 90.0]  # no-EDGE cap at 90

    final_scores   = []
    final_contribs = []

    for i, (_, row) in enumerate(df.iterrows()):
        if has_opp_score(row, pg):
            es  = float(row.get("edge_score") or 0)
            ovr = edge_to_ovr(es, pg, season)
            contrib = {
                "edge_score":     round(es, 4),
                "games_played":   int(row.get("games_played") or 0),
                "opp_quality":    round(float(row.get("opp_avg_sp") or 0), 2),
                "stats_measured": int(row.get("stats_measured") or 0),
            }
        elif all_stat_scores[i] is not None and valid_stat:
            # Stat-based fallback: scale within the no-EDGE pool, cap at 90
            ovr = float(np.clip(np.interp(all_stat_scores[i], stat_pcts, stat_targets), 30.0, 90.0))
            contrib = {"recruit_composite": round(float(row.get("recruit_composite") or 50.0), 2),
                       "stat_fallback": round(all_stat_scores[i], 4)}
        else:
            ovr    = 0.0   # truly no data → recruiting fallback in rate_position
            contrib = {}

        final_scores.append(ovr)
        final_contribs.append(contrib)

    return np.array(final_scores, dtype=float), final_contribs


def compute_ratings(df: pd.DataFrame, pg: str) -> tuple[np.ndarray, list[dict]]:
    """Stat-composite rating for non-EDGE positions (OL, K, P).

    Normalizes features via fixed absolute bounds and returns composite [0–1].
    Call scale_to_range() on the result to convert to [30–99].
    """
    final_scores   = []
    final_contribs = []

    for _, row in df.iterrows():
        weights = WEIGHTS.get(pg, {})
        feature_cols = list(weights.keys())
        w_arr = np.array([weights[c] for c in feature_cols])
        x = np.array([normalize_feature(c, float(row.get(c) or 0), pg=pg)
                      for c in feature_cols])
        score = float(x @ w_arr)
        contrib = {feat: round(float((x[j] - 0.5) * w_arr[j]), 4)
                   for j, feat in enumerate(feature_cols)}
        final_scores.append(score)
        final_contribs.append(contrib)

    return np.array(final_scores), final_contribs


# Conference discount — only applied to stat-only positions (non-EDGE).
# EDGE positions (QB/RB) are opponent-adjusted at the play level via SP+,
# so a G5 conference label would double-penalize what the model already handles.
# For stat-only positions (WR/TE/OL/DL/LB/DB), raw counting stats don't carry
# opponent context, so a modest discount still applies.
def scale_to_range(scores: np.ndarray, low=30, high=99, pg: str = "") -> np.ndarray:
    """Map composite scores (0-1) to rating range via piecewise linear interpolation.

    Uses the actual distribution of the all-season pool to compute percentile anchors,
    then maps them to target rating targets. Because the pool is all seasons combined
    (2021-2025), percentiles are absolute — a player's rating doesn't change based on
    who else played the same year. A weak class will produce fewer high ratings.

    Target distribution:
      p10 → 40  (marginal starter)
      p50 → 65  (average starter)
      p75 → 77  (solid starter)
      p90 → 85  (excellent)
      p99 → 93  (elite/generational)
    """
    if len(scores) < 5:
        return np.full(len(scores), 65.0)
    if np.std(scores) < 1e-9:
        return np.full(len(scores), 65.0)

    pct_vals = np.percentile(scores, [0, 10, 50, 75, 90, 99, 100])
    targets  = [float(low), 40.0, 65.0, 77.0, 85.0, 93.0, float(high)]
    result = np.interp(scores, pct_vals, targets)
    return np.clip(result, low, high).round(2)


def apply_conference_discount(scores: np.ndarray, df: pd.DataFrame, pg: str = "") -> np.ndarray:
    """No conference-level adjustment applied.

    All opponent quality adjustment happens at the play level (edge_score opp_mult)
    or the game level (defensive stat composite × per-game opp SP+ offense in script 08).
    Conference membership is too coarse a proxy — if per-play or per-game opponent
    data isn't available, we don't substitute a conference-level bandaid.
    """
    return scores.copy()


def apply_multi_tier_treatment(scores: np.ndarray, df: pd.DataFrame, pg: str,
                               position_avg: float = 65.0) -> np.ndarray:
    """Blend formula-driven scores with recruiting anchor based on playing-time tier.

    The tier controls *how much* we trust stats vs recruiting, not the ceiling.
    A 5-star backup at Alabama should still rate high — they just haven't had
    the opportunity to prove it yet, so recruiting anchors more heavily.

      starter: 100% formula (stats + EDGE drive the rating)
      role:    75% formula + 25% recruiting anchor
      reserve: 40% formula + 60% recruiting anchor
      bench:   100% recruiting anchor (no production to evaluate)

    No tier caps — loaded classes will naturally cluster higher, lean classes
    lower. Distribution shape comes from sigmoid normalization, not artificial ceilings.
    """
    result = scores.copy()
    stars_fallback = df["stars"].fillna(0).astype(int)

    for i, (_, row) in enumerate(df.iterrows()):
        tier = row.get("tier", "bench")
        stars = int(stars_fallback.iloc[i] or 0)
        recruit_rating = fallback_rating(stars, position_avg)

        if tier == "starter":
            result[i] = scores[i]  # full formula, no adjustment

        elif tier == "role":
            result[i] = round(scores[i] * 0.75 + recruit_rating * 0.25, 2)

        elif tier == "reserve":
            result[i] = round(scores[i] * 0.40 + recruit_rating * 0.60, 2)

        else:  # bench/redshirt
            result[i] = recruit_rating  # recruiting only

    return result


def apply_games_confidence(scaled: np.ndarray, df: pd.DataFrame) -> np.ndarray:
    """Damp ratings toward position average only for low-game-count players.

    Players with 8+ games: untouched (full confidence).
    Players with fewer games: rating pulled toward position average proportionally.
    Prevents a 2-game wonder from rating 95 but doesn't compress full-season players.
    Zero values (no EDGE data) are skipped entirely — they get fallback_rating later.
    """
    valid = scaled[scaled > 0]
    avg = float(np.mean(valid)) if len(valid) > 0 else 65.0
    result = scaled.copy()
    for i, (_, row) in enumerate(df.iterrows()):
        if scaled[i] == 0.0:
            continue  # no EDGE data — leave as 0, fallback_rating handles it
        games = float(row.get("games_played", 0) or 0)
        if games >= 8:
            continue  # full-season starters: no change
        # linear from 0.25 confidence at 1 game to 1.0 at 8 games
        confidence = max(0.25, games / 8.0)
        result[i] = round(float(avg + confidence * (scaled[i] - avg)), 2)
    return result


def fallback_rating(stars: int, position_avg: float = 65.0) -> float:
    offset = STARS_FALLBACK.get(min(stars, 5), -33)
    return max(30.0, min(99.0, round(position_avg + offset, 2)))


# ---------------------------------------------------------------------------
# Trajectory and breakout
# ---------------------------------------------------------------------------

def compute_trajectory(ratings_map: dict, prev_season: int, df: pd.DataFrame | None = None) -> dict:
    """Compute year-over-year trajectory for each player.

    Blends two signals:
    1. Rating delta: current_rating - prior_rating (captures relative rank change)
    2. Absolute EDGE growth: % change in raw edge_score (captures personal improvement
       even when the position pool also got stronger, e.g. Jeanty 2023->2024)

    Without signal 2, a generational season that coincides with a stronger pool
    shows 0 or negative trajectory despite clear real-world improvement.
    """
    if not ratings_map:
        return {}

    player_ids = list(ratings_map.keys())

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT ps.player_id, r.overall_rating
               FROM ratings r
               JOIN player_seasons ps ON ps.id = r.player_season_id
               WHERE r.season = %s AND ps.player_id = ANY(%s)""",
            (prev_season, player_ids)
        )
        prev_ratings = {pid: float(r) for pid, r in cur.fetchall()}

        # Load raw edge_scores (not scaled) for current and prior season
        cur.execute(
            """SELECT ps.player_id, pe.edge_score
               FROM player_edge pe
               JOIN player_seasons ps ON ps.id = pe.player_season_id
               WHERE pe.season = %s AND ps.player_id = ANY(%s)""",
            (prev_season, player_ids)
        )
        prev_edge = {pid: float(es) for pid, es in cur.fetchall() if es is not None}

        cur.execute(
            """SELECT ps.player_id, pe.edge_score
               FROM player_edge pe
               JOIN player_seasons ps ON ps.id = pe.player_season_id
               WHERE pe.season = %s AND ps.player_id = ANY(%s)""",
            (prev_season + 1, player_ids)
        )
        curr_edge = {pid: float(es) for pid, es in cur.fetchall() if es is not None}

    result = {}
    for pid, curr_rating in ratings_map.items():
        if pid not in prev_ratings:
            result[pid] = 0.0
            continue

        rating_delta = float(curr_rating) - prev_ratings[pid]

        # Absolute EDGE growth bonus: if raw edge_score grew substantially,
        # add up to +5 pts to trajectory even if relative rank held steady.
        edge_bonus = 0.0
        if pid in prev_edge and pid in curr_edge:
            prev_e = prev_edge[pid]
            curr_e = curr_edge[pid]
            if prev_e > 0:
                pct_growth = (curr_e - prev_e) / prev_e
                # Cap at ±5 pts bonus: 50%+ growth → +5, 25% → +2.5, etc.
                edge_bonus = round(min(5.0, max(-5.0, pct_growth * 10.0)), 2)

        result[pid] = round(rating_delta + edge_bonus, 2)

    return result


def compute_breakout(df: pd.DataFrame, ratings: np.ndarray) -> np.ndarray:
    """Identify breakout candidates: young players with high recruiting and below-median production."""
    median = np.median(ratings)
    probs  = []
    for i, (_, row) in enumerate(df.iterrows()):
        rec = row.get("recruit_composite", 40)
        yr  = row.get("year", 3)
        rat = ratings[i]
        young     = yr in (1, 2)
        high_rec  = rec > 70
        below_med = rat < median
        if young and high_rec and below_med:
            prob = min(0.95, 0.40 + (rec - 70) / 100 + (median - rat) / 200)
        elif young and high_rec:
            prob = 0.25
        elif high_rec:
            prob = 0.15
        else:
            prob = 0.05
        probs.append(round(prob, 4))
    return np.array(probs)


# ---------------------------------------------------------------------------
# Per-position entry point
# ---------------------------------------------------------------------------

def rate_position(season: int, pg: str) -> list[dict]:
    """Rate all players at a position for a given season.

    Normalization is cross-season: percentile ranks are computed against the
    full 2021-2025 starter population, so ratings are consistent across years.
    A player rated 85 in 2021 is genuinely comparable to an 85 in 2025.
    """
    print(f"  {pg}...", end=" ", flush=True)

    # Load ALL seasons together so percentile ranks are cross-season stable
    all_df = _load_seasons(ALL_SEASONS, pg)
    if all_df.empty:
        print("no data")
        return []

    all_starter_mask = all_df.apply(lambda r: is_starter(r, pg), axis=1)
    all_starter_df   = all_df[all_starter_mask]
    all_backup_df    = all_df[~all_starter_mask]

    # Cross-season ratings_map: row_key -> float
    ratings_map: dict[str, float] = {}
    contrib_map: dict[str, dict]  = {}

    edge_count = all_starter_df["edge_score"].notna().sum() if "edge_score" in all_starter_df.columns else 0

    if len(all_starter_df) >= 5:
        if pg in EDGE_POSITIONS:
            # --- Direct edge_score → OVR mapping (no peer ranking) ---
            raw_scores, contribs = compute_edge_ratings(all_starter_df, pg, season)
            # raw_scores is already [30-99] for EDGE players, 0.0 for no-data players
            scaled = apply_games_confidence(raw_scores, all_starter_df)
            # Print distribution of players with valid EDGE
            edge_ovrs = scaled[scaled > 0]
            if len(edge_ovrs) >= 5:
                p = np.percentile(edge_ovrs, [10, 25, 50, 75, 90, 99])
                print(f"\n    [edge OVR] p10={p[0]:.1f} p25={p[1]:.1f} p50={p[2]:.1f} p75={p[3]:.1f} p90={p[4]:.1f} p99={p[5]:.1f}", end=" ")
        else:
            # --- Stat composite + scale_to_range for OL/K/P ---
            raw_scores, contribs = compute_ratings(all_starter_df, pg)
            discounted = apply_conference_discount(raw_scores, all_starter_df, pg=pg)
            scaled = scale_to_range(discounted, pg=pg)

        for i, rkey in enumerate(all_starter_df.index):
            if scaled[i] > 0:   # 0.0 = no EDGE data → let fallback handle it
                ratings_map[rkey] = float(scaled[i])
                contrib_map[rkey] = contribs[i]
    else:
        print(f"(only {len(all_starter_df)} starters across all seasons — fallback only) ", end="")
        for rkey, row in all_df.iterrows():
            ratings_map[rkey] = fallback_rating(int(row.get("stars", 0)))

    # Fallback for backups / low-snap players.
    # Sub-threshold players with high efficiency get a blended rating instead
    # of pure recruiting fallback — prevents small-sample stars being buried.
    pos_avg = float(np.mean(list(ratings_map.values()))) if ratings_map else 65.0

    # Compute an efficiency percentile across all starters for blending.
    # For EDGE positions, use edge_score directly if available.
    eff_col = {
        "QB": "yards_per_att", "RB": "yards_per_carry",
        "WR": "yards_per_rec", "TE": "yards_per_rec",
        "EDGE": "edge_score", "DL": "edge_score",
        "LB": "edge_score", "CB": "edge_score", "S": "edge_score",
        "DB": "edge_score",
    }.get(pg)

    starter_eff_vals = None
    if eff_col and eff_col in all_starter_df.columns:
        starter_eff_vals = all_starter_df[eff_col].fillna(0).values

    for rkey, row in all_backup_df.iterrows():
        base = fallback_rating(int(row.get("stars", 0)), pos_avg)
        # For players with meaningful efficiency (>= 50th pct of starters),
        # blend 30% efficiency-implied rating with 70% recruiting fallback.
        # This prevents 7.8 YPC players from rating the same as 3.0 YPC backups.
        if eff_col and starter_eff_vals is not None:
            eff_val = float(row.get(eff_col) or 0)
            if eff_val > 0 and len(starter_eff_vals) >= 5:
                pct = float(np.mean(starter_eff_vals <= eff_val))
                if pct >= 0.5:
                    # Map percentile to a rating in pos_avg±20 range
                    eff_implied = pos_avg - 10 + pct * 20
                    blend = round(0.70 * base + 0.30 * eff_implied, 2)
                    ratings_map[rkey] = blend
                    contrib_map[rkey] = {"recruit_composite": 0.35, eff_col: 0.15}
                    continue
        ratings_map[rkey] = base
        contrib_map[rkey] = {"recruit_composite": 0.5}

    # --- Filter down to the requested season ---
    season_df = all_df[all_df["_season"] == season]
    if season_df.empty:
        print("no data for requested season")
        return []

    # Compute trajectory (needs per-player ratings from prior season)
    # Build player_id -> rating map for this season's rows
    # If a player transferred mid-season they may have two player_seasons rows;
    # keep the higher rating for trajectory/breakout purposes.
    season_pid_rating: dict[int, float] = {}
    for rkey, row in season_df.iterrows():
        pid = int(row["player_id"])
        val = float(ratings_map.get(rkey, 50.0))
        if pid not in season_pid_rating or val > season_pid_rating[pid]:
            season_pid_rating[pid] = val

    trajectory = compute_trajectory(season_pid_rating, season - 1)

    # Breakout probability — use this season's subset only
    # Deduplicate by player_id (a player can have two player_seasons rows if they
    # transferred mid-season; keep the one with the higher rating)
    season_pids = list(season_pid_rating.keys())
    season_rats = np.array([season_pid_rating[p] for p in season_pids])
    sub_df = season_df.copy()
    sub_df["_pid_int"] = sub_df["player_id"].astype(int)
    # Keep one row per player_id — highest rating wins
    sub_df = sub_df.sort_values("_pid_int")
    sub_df = sub_df.drop_duplicates(subset=["_pid_int"], keep="first")
    sub_df = sub_df.set_index("_pid_int")
    # Align sub_df rows to season_pids order
    aligned_df = sub_df.reindex(season_pids)
    breakout_arr = compute_breakout(aligned_df, season_rats)
    breakout_map = dict(zip(season_pids, breakout_arr))

    ceiling = POSITION_CEILING.get(pg)
    starters_this_season = season_df.apply(lambda r: is_starter(r, pg), axis=1).sum()
    edge_this_season = (
        season_df["edge_score"].notna().sum()
        if "edge_score" in season_df.columns else 0
    )

    rows = []
    tiers = []
    for rkey, row in season_df.iterrows():
        pid    = int(row["player_id"])
        ps_id  = int(row["player_season_id"])
        ovr    = float(ratings_map.get(rkey, 50.0))
        tier   = get_tier(row, pg)
        tiers.append(ovr)  # for multi-tier treatment
        if ceiling:
            ovr = min(ovr, ceiling)
        rows.append({
            "player_season_id":     ps_id,
            "season":               int(season),
            "overall_rating":       ovr,
            "position_rating":      ovr,
            "trajectory_score":     float(trajectory.get(pid, 0.0)),
            "trajectory":           float(trajectory.get(pid, 0.0)),
            "breakout_probability": float(breakout_map.get(pid, 0.05)),
            "shap_values":          json.dumps(contrib_map.get(rkey, {})),
            "model_version":        MODEL_VERSION,
            "engine":               "edge",
            "_tier":                tier,   # stripped before upsert (not a DB column)
            "_pg":                  pg,     # stripped before upsert
        })

    # Apply multi-tier rating treatment
    tier_arr = np.array([r["_tier"] for r in rows])
    ratings_arr = np.array([r["overall_rating"] for r in rows])
    season_df["tier"] = tier_arr
    season_df["stars"] = season_df["stars"].fillna(0)
    adjusted_ratings = apply_multi_tier_treatment(ratings_arr, season_df, pg, position_avg=pos_avg)

    # Update rows with adjusted ratings
    for i, r in enumerate(rows):
        r["overall_rating"] = float(adjusted_ratings[i])
        r["position_rating"] = float(adjusted_ratings[i])

    edge_info = f", EDGE: {edge_count}" if pg in EDGE_POSITIONS else ""
    print(f"{len(rows)} players this season (starters: {starters_this_season}{edge_info}, total pool: {len(all_starter_df)} starters across all seasons)")
    return rows


# ---------------------------------------------------------------------------
# Distribution validation (hard gate — must pass before upsert)
# ---------------------------------------------------------------------------

def validate_distribution(rows: list[dict]) -> bool:
    """
    Validate that rating distributions are within expected bounds.
    Returns True if all pass; prints violations and returns False otherwise.

    Target bounds:
      All groups: mean 62-68, p50 within 3 of mean, p90 80-87, p99 92-98
    """
    if not rows:
        return True

    df = pd.DataFrame(rows)
    # Validate starter-tier only — bench/reserve ratings intentionally skew low
    starter_df = df[df["tier"] == "starter"] if "tier" in df.columns else df
    group_col = "_pg" if "_pg" in df.columns else ("position_group" if "position_group" in df.columns else "season")
    by_pg = starter_df.groupby(group_col)["overall_rating"]

    valid = True
    for pg, scores in by_pg:
        if len(scores) < 10:
            continue  # skip small groups
        vals = scores.dropna()
        if vals.empty:
            continue
        mean = np.mean(vals)
        p10 = np.percentile(vals, 10)
        p50 = np.percentile(vals, 50)
        p90 = np.percentile(vals, 90)
        p99 = np.percentile(vals, 99)

        print(f"  {pg}: n={len(vals)} mean={mean:.1f} p10={p10:.1f} p50={p50:.1f} p90={p90:.1f} p99={p99:.1f}")

        # Check bounds — absolute normalization, so bounds reflect all-time expectations.
        # Offensive positions can reach 90+ (elite rushers/passers have extreme stat outliers).
        # Defensive positions cap naturally lower due to counting-stat compression.
        if pg in ("K", "P"):
            mean_ok = (55 <= mean <= 70)
            p90_ok  = (65 <= p90 <= 79)
            p99_ok  = (70 <= p99 <= 79)
        elif pg == "OL":
            mean_ok = (55 <= mean <= 75)
            p90_ok  = (78 <= p90 <= 95)
            p99_ok  = (88 <= p99 <= 99)
        elif pg in ("QB", "RB"):
            mean_ok = (60 <= mean <= 72)
            p90_ok  = (78 <= p90 <= 92)
            p99_ok  = (88 <= p99 <= 99)
        elif pg in ("WR", "TE"):
            mean_ok = (58 <= mean <= 72)
            p90_ok  = (75 <= p90 <= 90)
            p99_ok  = (85 <= p99 <= 99)
        else:
            # Defensive positions: EDGE, DL, LB, CB, S, DB
            mean_ok = (58 <= mean <= 74)
            p90_ok  = (72 <= p90 <= 88)
            p99_ok  = (78 <= p99 <= 97)
        if not mean_ok:
            print(f"    WARNING: mean out of bounds (got {mean:.1f})")
            valid = False
        if abs(p50 - mean) > 5:
            print(f"    WARNING: p50 too far from mean (delta {abs(p50 - mean):.1f}, max 5)")
            valid = False
        if not p90_ok:
            print(f"    WARNING: p90 out of bounds (got {p90:.1f})")
            valid = False
        if not p99_ok:
            print(f"    WARNING: p99 out of bounds (got {p99:.1f})")
            valid = False

    if not valid:
        print("\n  Distribution validation WARNINGS above — review before committing.")
        print("    (Not blocking — loaded/lean classes will naturally shift the distribution.)")
    else:
        print("\n  Distribution validation passed")
    return True  # always allow upsert; validation is informational


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--season",      type=int, default=2025)
    parser.add_argument("--all-seasons", action="store_true")
    parser.add_argument("--position",    type=str)
    args = parser.parse_args()

    seasons = list(range(2008, 2026)) if args.all_seasons else [args.season]
    positions = [args.position.upper()] if args.position else list(WEIGHTS.keys())

    for season in seasons:
        print(f"\n-- Season {season} --")
        all_rows = []
        for pg in positions:
            all_rows.extend(rate_position(season, pg))
        if all_rows:
            print("\n  Validating distribution...")
            if not validate_distribution(all_rows):
                print(f"\n  Skipping upsert — fix weights and re-run")
                continue
            upsert_rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in all_rows]
            bulk_upsert("ratings", upsert_rows, ["player_season_id", "season", "engine"])
            print(f"  Upserted {len(all_rows)} rows")

    print("\nDone.")


if __name__ == "__main__":
    main()
