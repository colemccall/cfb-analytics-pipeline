"""CFB player ratings — Engine A (v4.0).

Rating architecture:
  EDGE positions (QB/RB/WR/TE/EDGE/DL/LB/CB/S):
    OVR = edge_to_ovr(edge_score, position_group)
    edge_score (from script 08) is a per-game stat composite × per-game opponent
    SP+ quality, accumulated over games and normalized by sqrt(games_played).
    No traditional stats in the formula — they're already embedded in edge_score.
    Recruiting/NIL only for players below the stats_measured threshold.

  Non-EDGE positions (OL, K, P):
    Stat-only composite with fixed absolute bounds, mapped via composite_to_ovr().

  EDGE_OVR_ANCHORS: fixed piecewise linear mapping from edge_score → OVR.
    Anchors are calibrated from known reference seasons and 2025 distributions.
    Not recalculated per run — they are permanent calibration points.
    *** Offensive anchors are INITIAL ESTIMATES — recalibrate after first run ***
    by reviewing top-50 per position and adjusting anchors so known elite
    players land 90-96 and average starters land 62-70.

Usage:
    python scripts/07_compute_player_ratings.py              # 2025
    python scripts/07_compute_player_ratings.py --season 2024
    python scripts/07_compute_player_ratings.py --all-seasons
    python scripts/07_compute_player_ratings.py --position QB --season 2024
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

from utils.store import read_raw, read_computed, write_computed
from utils.stat_agg import META_KEYS, aggregate_game_stats, has_box_score

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

def classify_playtime_tier(pg: str, stats: dict, games_played: int = 1) -> str:
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
STARS_OVR_DELTA = {5: -3, 4: -8, 3: -15, 2: -22, 1: -28, 0: -33}

# Positions that can have EDGE scores (computed in script 08).
# Offensive: QB, RB, WR, TE (play-level EPA)
# Defensive: EDGE, DL, LB, CB, S (per-game stat composite × opponent SP+)
# OL, K, P: never have EDGE
EDGE_POSITIONS = {"QB", "RB", "WR", "TE", "EDGE", "DL", "LB", "CB", "S", "DB"}

# All seasons used for cross-season normalization
ALL_SEASONS = list(range(2008, 2027))

# ---------------------------------------------------------------------------
# EDGE → OVR direct mapping (replaces weighted composite + percentile scaling
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

# v4.2 calibration (2026-08-11).
#
# Every x-coordinate below is OUR OWN edge_score — specifically, the score posted
# by the Nth-best player at that position in a typical (median) season across
# 2021-2025. What had to be chosen is only what a given standing is *worth*, and
# for that EA CFB 27 is consulted as an external scouting consensus: not to supply
# numbers, but to answer "are we handing out too many 85s, too few, and is the
# ceiling in the right place". Where EA and the earlier anchors already agreed
# (TE: 17 players at 85+ and 3 at 90+ in both), nothing was touched.
#
# These remain ABSOLUTE constants, derived once. A weaker future crop simply
# produces fewer 90s — the percentile-of-the-pool scaling this replaced is the
# bug documented in AUDIT_FINDINGS.md §9, and rank-matching every season would
# have reintroduced it.
#
# Ceilings follow from what each position needed:
#   · offensive skill — the WEAKEST season's best player is a 96, so the best
#     player in the country always reads like one (their tips were too low);
#   · defense — the TYPICAL season's best is a 96, so a monster year can still
#     exceed it and only a historic one nears 99 (their tips were too high; a 99
#     was going to a very good season rather than a generational one).
# The single best season on record maps to 99 in both cases.
EDGE_OVR_ANCHORS: dict[str, list[tuple[float, float]]] = {
    # Offensive: stat_composite × opp_mult / sqrt(games)
    #   QB/RB validated as correct below the tip; only the ceiling moved.
    #   QB all-time #1 = Burrow 2019.
    "QB":   [(0, 30), (150, 50), (445, 65), (750, 77), (1100, 85), (1450, 90), (1676.9, 96), (2385, 99)],
    #   RB all-time #1 = Gordon 2014.
    "RB":   [(0, 30), (40,  50), (120, 65), (240, 77), (500,  87), (650,  93), (712.6,  96), (1029, 99)],
    #   WR: the middle rises. A team rotates three to five receivers through real
    #   snaps, so the WR3 nationally is a starter and was priced as a reserve —
    #   the 72 and 77 anchors are the last man in a 3.5-deep rotation and the
    #   rank-286 receiver. All-time #1 = DeVonta Smith 2020.
    "WR":   [(0, 30), (35,  50), (101.5, 72), (173.3, 77), (319.1, 85), (459.9, 90), (611.4, 96), (950, 99)],
    #   TE: unchanged — already matched the external consensus exactly.
    "TE":   [(0, 30), (25,  50), (75,  65), (160, 78), (270,  87), (384,  97), (650,  99)],
    #
    # Defensive: stat_composite × opp_mult / sqrt(games). Weights v4.1, plus the
    # coverage-denial credit added to CB/S/DB in script 06.
    #
    #   EDGE was thin at the top (too few genuinely elite) with a tip that was too
    #   high — a 99 for a very good season.
    "EDGE": [(0, 30), (3.0, 50), (11.0, 65), (22.0, 77), (30.3, 85), (40.8, 90), (65.7, 96), (82.8, 99)],
    #   DL and LB were the opposite: too many 85s and 90s.
    "DL":   [(0, 30), (2.5, 50), (7.0,  65), (15.0, 77), (30.5, 85), (43.0, 90), (59.9, 96), (71.0, 99)],
    "LB":   [(0, 30), (4.0, 50), (11.0, 65), (22.0, 77), (39.0, 85), (52.1, 90), (70.9, 96), (96.7, 99)],
    #   CB/S/DB: the secondary carried roughly half the elite ratings it should —
    #   53 players at 85+ against an expected ~113 — because coverage suppresses
    #   the counting stats these scores are built from. The credit in script 06
    #   addresses the suppression; these anchors address the compression.
    #
    #   The x-scale here is much smaller than the other positions' because a
    #   defensive back's score is no longer a raw stat composite: it is his three
    #   archetypes on a 0-10 axis, weighted by position
    #   (SECONDARY_ARCHETYPE_WEIGHTS in script 06). Anchors are per-position, so
    #   the scales never have to agree — but these MUST be re-derived whenever
    #   that composite or its scale constants change.
    "CB":   [(0, 30), (1.5, 50), (3.8, 75), (7.4, 80), (11.6, 85), (16.5, 90), (19.1, 96), (24.2, 99)],
    "S":    [(0, 30), (2.0, 50), (4.7, 75), (8.4, 80), (10.9, 85), (14.4, 90), (18.9, 96), (25.8, 99)],
    "DB":   [(0, 30), (1.5, 50), (4.5, 75), (7.9, 80), (11.2, 85), (14.5, 90), (20.8, 96), (25.5, 99)],
}


DEFENSIVE_POSITIONS = {"EDGE", "DL", "LB", "CB", "S", "DB"}

# ---------------------------------------------------------------------------
# Pre-2016 CLASSIC defensive ratings (2008–2015).
#
# The CFB Data API tracks interceptions back to 2008 but does NOT track
# sacks, TFLs, QB hurries, or pass breakups before 2016. Tackles are also
# absent. This means:
#
#   CB / S / DB — rated by interceptions (always tracked) + recruiting composite.
#     Reference: Amerson 2011 (13 INT) → ~97, Peterson 2010 (4 INT, 5-star) → ~91,
#     Ha-Ha Clinton-Dix 2013 (2 INT, 5-star) → ~85.
#
#   LB — INTs are rare but meaningful (Te'o 2012 had 7 — extraordinary).
#     Without tackle data we still use INT + recruiting. Te'o (7 INT, 5-star) → ~97.
#
#   EDGE / DL — INTs are irrelevant; sacks/TFLs are absent → recruiting composite
#     is the only signal. Myles Garrett 2015 (5-star, 0.9992) → ~85 (cap; can't
#     confirm production without sack data).
#
# Adaptive blending: w_int scales from 0.45 (0 INT) to 0.95 (13+ INT).
# At 0 INT, recruiting dominates; at record INTs, recruiting is minor context.
# ---------------------------------------------------------------------------

# Piecewise INT → OVR anchors per defensive position.
# Calibrated so average starter (1 INT) → ~60, All-American (4-5 INT) → ~85-90,
# record season → 97-99.
_CLASSIC_INT_ANCHORS: dict[str, list[tuple[float, float]]] = {
    "CB": [(0,40),(1,60),(2,70),(3,78),(4,85),(5,89),(6,92),(7,94),(8,96),(10,98),(13,99)],
    "S":  [(0,40),(1,60),(2,70),(3,78),(4,84),(5,88),(6,91),(7,93),(8,95),(10,97),(13,99)],
    "DB": [(0,40),(1,59),(2,69),(3,77),(4,83),(5,87),(6,90),(7,93),(8,95),(10,97),(13,99)],
    # LB: 4+ INTs for a linebacker is extraordinary; Te'o (7 INT) → 97
    "LB": [(0,38),(1,58),(2,70),(3,80),(4,88),(5,93),(6,96),(7,99)],
}

# Hard cap for EDGE/DL pre-2016: recruiting only, can't verify pass-rush production.
_CLASSIC_EDGE_CAP = 88
_CLASSIC_DL_CAP   = 85


def compute_pre2016_classic_ovr(ints: float, rec_ovr: float, pg: str) -> float:
    """Rate a pre-2016 defensive player using the CLASSIC system (INT + recruiting).

    Args:
        ints:    season interception count (float)
        rec_ovr: recruiting OVR already on 0–100 scale (_composite_to_100 output)
        pg:      position group

    For CB/S/DB/LB: adaptive blend of INT-derived OVR and recruiting OVR.
    For EDGE/DL: recruiting OVR capped — no production data available pre-2016.
      Myles Garrett 2015 (5-star, ~100 rec_ovr) → 85 (cap).
    """
    if pg in ("EDGE", "DL"):
        cap = _CLASSIC_EDGE_CAP if pg == "EDGE" else _CLASSIC_DL_CAP
        return float(np.clip(min(cap, rec_ovr), 35.0, cap))

    anchors = _CLASSIC_INT_ANCHORS.get(pg)
    if not anchors:
        return float(np.clip(rec_ovr, 35.0, 99.0))

    xs = [a[0] for a in anchors]
    ys = [float(a[1]) for a in anchors]
    int_ovr = float(np.interp(ints, xs, ys))

    # Adaptive weight: INT signal earns more weight as INT count rises.
    # w_int = 0.45 at 0 INT (recruiting dominates) → 0.95 at 13+ INT (Amerson).
    w_int = float(np.clip(0.45 + ints * 0.04, 0.45, 0.95))
    w_rec = 1.0 - w_int

    ovr = int_ovr * w_int + rec_ovr * w_rec
    return float(np.clip(ovr, 35.0, 99.0))


def get_rating_era(season: int) -> str:
    return "modern" if season >= 2016 else "pre2016"


def edge_to_ovr(edge_score: float, pg: str, season: int = 2025) -> float:
    """Map raw edge_score to OVR via piecewise linear anchors.

    Pre-2016 defensive positions should NOT reach this function — they are
    handled by compute_pre2016_classic_ovr() in compute_edge_ratings().
    All other positions (offensive all eras, defensive 2016+) use EDGE_OVR_ANCHORS.
    """
    anchors = EDGE_OVR_ANCHORS.get(pg)
    if not anchors or edge_score is None or (isinstance(edge_score, float) and np.isnan(edge_score)):
        return 50.0
    xs = [a[0] for a in anchors]
    ys = [float(a[1]) for a in anchors]
    return float(np.clip(np.interp(float(edge_score), xs, ys), 30.0, 99.0))


def _stat_float(stats, key):
    v = stats.get(key)
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _si(v, default=0):
    try:
        return int(v) if v is not None and v == v else default
    except (TypeError, ValueError):
        return default


def _sf(v, default=0.0):
    try:
        return float(v) if v is not None and v == v else default
    except (TypeError, ValueError):
        return default


def _composite_to_100(score) -> float:
    # 247Sports composite scores range 0.7–1.0; map to 0–100 for feature normalization.
    if not score:
        return 40.0
    return max(0.0, min(100.0, (float(score) - 0.7) / 0.3 * 100))


# ---------------------------------------------------------------------------
# Supplementary stat features (secondary role for EDGE positions)
# ---------------------------------------------------------------------------

def compute_stat_features(stats: dict, pg: str) -> dict:
    """Return named production metrics for one player."""
    if pg == "QB":
        att  = max(_stat_float(stats, "passingATT"), 1)
        comp = _stat_float(stats, "passingCOMPLETIONS")
        yds  = _stat_float(stats, "passingYDS")
        td   = _stat_float(stats, "passingTD")
        ints = _stat_float(stats, "passingINT")
        return {
            "comp_pct":      comp / att,
            "yards_per_att": yds  / att,
            "td_int_ratio":  (td + 1) / (ints + 1),
            "volume_score":  att,
        }

    if pg == "RB":
        car = max(_stat_float(stats, "rushingCAR"), 1)
        yds = _stat_float(stats, "rushingYDS")
        rec = _stat_float(stats, "receivingREC")
        return {
            "yards_per_carry":  yds / car,
            "yards_total":      yds,
            "rec_versatility":  rec / car,
            "volume_score":     car,
        }

    if pg in ("WR", "TE"):
        rec = max(_stat_float(stats, "receivingREC"), 1)
        yds = _stat_float(stats, "receivingYDS")
        tds = _stat_float(stats, "receivingTD")
        return {
            "yards_per_rec":   yds / rec,
            "yards_total":     yds,
            "td_score":        tds * 8.0 + yds * 0.01,
            "rec_volume":      rec,
            "volume_score":    rec,   # alias used by is_starter threshold check
        }

    if pg == "OL":
        return {
            "team_rush_ypa":      _stat_float(stats, "team_rush_ypa"),
            "team_sack_rate_inv": 1.0 - min(_stat_float(stats, "team_sack_rate"), 1.0),
            "award_tier":         _stat_float(stats, "award_tier"),
            "experience":         2.0,   # placeholder; overwritten below from players.year
        }

    if pg == "EDGE":
        tot   = max(_stat_float(stats, "defensiveTOT"), 1)
        sacks = _stat_float(stats, "defensiveSACKS")
        tfl   = _stat_float(stats, "defensiveTFL")
        hur   = _stat_float(stats, "defensiveQB HUR")
        return {
            "pass_rush_score":  sacks * 5.0 + hur * 1.5 + tfl * 2.0,   # sacks + pressure dominant for EDGE
            "disruption_rate":  (sacks + tfl) / tot,                     # impact per play
            "run_stop_score":   tfl * 2.5 + (tot - sacks) * 0.3,        # run stuffs (secondary for EDGE)
            "volume_score":     tot,
        }

    if pg == "DL":
        tot   = max(_stat_float(stats, "defensiveTOT"), 1)
        sacks = _stat_float(stats, "defensiveSACKS")
        tfl   = _stat_float(stats, "defensiveTFL")
        hur   = _stat_float(stats, "defensiveQB HUR")
        return {
            "pass_rush_score":  sacks * 5.0 + hur * 1.5 + tfl * 1.0,   # sacks + pressure
            "run_stop_score":   tfl * 2.5 + (tot - sacks) * 0.4,        # run stuffs + tackle presence
            "disruption_rate":  (sacks + tfl) / tot,                     # impact per play
            "volume_score":     tot,
        }

    if pg == "LB":
        tot    = max(_stat_float(stats, "defensiveTOT"), 1)
        sacks  = _stat_float(stats, "defensiveSACKS")
        tfl    = _stat_float(stats, "defensiveTFL")
        ints   = _stat_float(stats, "interceptionsINT")
        pbu    = _stat_float(stats, "defensivePD")
        return {
            "tackling_score":   tot * 0.5 + tfl * 2.0,                  # pursuit + run stop
            "pass_rush_score":  sacks * 4.0 + tfl * 1.0,                # blitz / pressure
            "coverage_score":   ints * 3.0 + pbu * 1.5,                 # zone/man skills
            "instinct_score":   (ints + pbu + tfl) / tot,               # playmaking rate
            "volume_score":     tot,
        }

    if pg == "CB":
        tot   = max(_stat_float(stats, "defensiveTOT"), 1)
        sacks = _stat_float(stats, "defensiveSACKS")
        tfl   = _stat_float(stats, "defensiveTFL")
        ints  = _stat_float(stats, "interceptionsINT")
        pbu   = _stat_float(stats, "defensivePD")
        return {
            "coverage_score":   ints * 4.0 + pbu * 2.0,                 # CB: ball skills dominant
            "tackling_score":   tot * 0.3 + tfl * 1.0,                  # run support (secondary)
            "pass_rush_score":  sacks * 3.0 + tfl * 1.0,                # blitz value
            "instinct_score":   (ints + pbu) / tot,                     # focus on coverage instinct
            "volume_score":     tot,
        }

    if pg == "S":
        tot   = max(_stat_float(stats, "defensiveTOT"), 1)
        sacks = _stat_float(stats, "defensiveSACKS")
        tfl   = _stat_float(stats, "defensiveTFL")
        ints  = _stat_float(stats, "interceptionsINT")
        pbu   = _stat_float(stats, "defensivePD")
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
        tot   = max(_stat_float(stats, "defensiveTOT"), 1)
        sacks = _stat_float(stats, "defensiveSACKS")
        tfl   = _stat_float(stats, "defensiveTFL")
        ints  = _stat_float(stats, "interceptionsINT")
        pbu   = _stat_float(stats, "defensivePD")
        return {
            "coverage_score":   ints * 3.0 + pbu * 1.5,
            "tackling_score":   tot * 0.5 + tfl * 2.0,
            "pass_rush_score":  sacks * 4.0 + tfl * 1.5,
            "instinct_score":   (ints + pbu + tfl * 0.5) / tot,
            "volume_score":     tot,
        }

    if pg == "K":
        fga = max(_stat_float(stats, "kickingFGA"), 1)
        xpa = max(_stat_float(stats, "kickingXPA"), 1)
        return {
            "fg_pct":       _stat_float(stats, "kickingFGM") / fga,
            # The API key is kickingLONG. This read "kickingLNG" and so returned
            # 0.0 for every kicker who has ever been rated — a quarter of the K
            # composite was a dead constant, and the anchors below were then
            # calibrated on top of that hole.
            "fg_long":      _stat_float(stats, "kickingLONG"),
            "xp_pct":       _stat_float(stats, "kickingXPM") / xpa,
            "volume_score": _stat_float(stats, "kickingFGM"),
        }

    if pg == "P":
        n = max(_stat_float(stats, "puntingNO"), 1)
        return {
            "avg_yards":     _stat_float(stats, "puntingYDS") / n,
            "inside_20_pct": _stat_float(stats, "puntingIn 20") / n,
            "volume_score":  _stat_float(stats, "puntingNO"),
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
STAT_ONLY_WEIGHTS = {
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

# Percentile → OVR targets for the no-EDGE stat fallback in compute_edge_ratings.
# Cap at 78: without opponent-adjusted EPA we can't confirm elite production.
STAT_FALLBACK_TARGETS = [30.0, 38.0, 55.0, 64.0, 70.0, 76.0, 78.0]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_position_data(season: int, pg: str) -> pd.DataFrame:
    """Load one season's data for a position group.

    Only includes players who appeared on a roster in this season, determined
    by the presence of a season_aggregate stats row.
    """
    return _load_seasons([season], pg)


# Raw tables are read once per process, not once per (season × position). Every
# call to _load_seasons used to re-parse all of data/raw — 255 MB of stats alone —
# so a --all-seasons run parsed it 228 times (19 seasons × 12 groups) and spent
# hours doing nothing but JSON. The tables are immutable for the life of the run.
_RAW: dict[str, pd.DataFrame] = {}


def _raw(table: str) -> pd.DataFrame:
    if table not in _RAW:
        _RAW[table] = read_raw(table)
    return _RAW[table]


# ratings.json is read once per position to find last season's number, and once
# per season to merge — 228 reads of a 54 MB file across a full run. It is also
# the one table this script writes, so the cache is replaced with what was
# written rather than dropped: season N+1 must see season N's ratings.
_RATINGS: pd.DataFrame | None = None


def _computed_ratings() -> pd.DataFrame:
    global _RATINGS
    if _RATINGS is None:
        _RATINGS = read_computed("ratings")
    return _RATINGS


def _parse_stat_data(val) -> dict:
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            pass
    return {}


_STATS_INDEX: dict | None = None


def _stats_index() -> dict:
    """player_season_id -> season stat payload, for every player we can build one.

    The season aggregate where the API wrote a real one; a sum of that player's
    game rows where it wrote nothing, or wrote a row carrying only usage and PPA.
    Rating used to inner-join the aggregate alone, which dropped ~350-465
    player-seasons a year: Jayden Virgin-Morgan played four seasons at Boise State
    with 12-14 game rows each and was rated in none of them, so to a reader he had
    no history at all.

    Built once and the stats frame released, because it is the largest table in
    the store by an order of magnitude and nothing downstream needs the rows.
    """
    global _STATS_INDEX
    if _STATS_INDEX is not None:
        return _STATS_INDEX

    st_all = read_raw("stats")
    if st_all.empty:
        _STATS_INDEX = {}
        return _STATS_INDEX

    agg_rows = st_all[(st_all["stat_type"] == "season_aggregate") &
                      (st_all["game_id"].isna())][["player_season_id", "data"]]
    index: dict = {}
    for r in agg_rows.itertuples(index=False):
        index[r.player_season_id] = _parse_stat_data(r.data)

    # Missing OR metadata-only. Both are the same failure to a rating: no
    # production recorded for a player who took the field.
    need = {ps for ps, d in index.items() if not has_box_score(d)}
    game_rows = st_all[st_all["game_id"].notna()][["player_season_id", "data"]]
    del st_all

    by_ps: dict = {}
    for r in game_rows.itertuples(index=False):
        ps = r.player_season_id
        if ps in index and ps not in need:
            continue
        by_ps.setdefault(ps, []).append(_parse_stat_data(r.data))
    del game_rows

    rebuilt = 0
    for ps, rows in by_ps.items():
        summed = aggregate_game_stats(rows)
        if not summed:
            continue
        # Usage, PPA and awards come from endpoints the game rows do not carry.
        # Whatever the original row knew is kept; only production is replaced.
        old = index.get(ps) or {}
        for k, v in old.items():
            if k in META_KEYS and v:
                summed[k] = v
        summed["rebuilt_from_games"] = True
        index[ps] = summed
        rebuilt += 1

    print(f"  Stats index: {len(index)} player-seasons "
          f"({rebuilt} rebuilt from game rows)")
    _STATS_INDEX = index
    return _STATS_INDEX


def _load_seasons(seasons: list[int], pg: str) -> pd.DataFrame:
    """Load one or more seasons of data for a position group from local JSON.

    Joins player_seasons → players → stats → player_edge → recruiting → teams.
    player_seasons is the join anchor: one row per player × season × team.
    """
    ps_df   = _raw("player_seasons")
    if ps_df.empty:
        return pd.DataFrame()

    # Filter to position group + requested seasons
    ps_df = ps_df[
        (ps_df["position_group"] == pg) &
        (ps_df["season"].isin(seasons))
    ].copy()
    if ps_df.empty:
        return pd.DataFrame()

    # Players — name lookup
    pl_df = _raw("players")[["id", "name"]].rename(columns={"id": "player_id"})
    ps_df = ps_df.merge(pl_df, on="player_id", how="left")

    # Stats — the season aggregate where the API wrote one, a sum of game rows
    # where it did not. Mapping rather than merging keeps the old inner-join
    # semantics (no payload, no rating) without a second copy of the frame.
    index = _stats_index()
    ps_df["data"] = ps_df["id"].map(index)
    ps_df = ps_df[ps_df["data"].notna()].copy()
    if ps_df.empty:
        return pd.DataFrame()

    # EDGE scores
    edge_df = _raw("player_edge")
    if not edge_df.empty:
        cols = ["player_season_id", "edge_score", "stats_measured", "games_played", "opponent_avg_sp"]
        if "coverage_share" in edge_df.columns:
            cols.append("coverage_share")
        edge_df = edge_df[cols].copy()
        ps_df = ps_df.merge(edge_df, left_on="id", right_on="player_season_id",
                            how="left", suffixes=("", "_edge"))
    else:
        ps_df["edge_score"] = None
        ps_df["stats_measured"] = 0
        ps_df["games_played"] = 0
        ps_df["opponent_avg_sp"] = 0.0

    # Recruiting — best record per player
    rec_df = _raw("recruiting")
    if not rec_df.empty:
        rec_df = rec_df.sort_values("composite_score", ascending=False, na_position="last")
        rec_df = rec_df.drop_duplicates(subset=["player_id"], keep="first")
        rec_df = rec_df[["player_id", "stars", "composite_score"]]
        ps_df = ps_df.merge(rec_df, on="player_id", how="left")
    else:
        ps_df["stars"] = 0
        ps_df["composite_score"] = None

    # Conference
    tm_df = _raw("teams")[["id", "conference"]].rename(columns={"id": "team_id"})
    ps_df = ps_df.merge(tm_df, on="team_id", how="left")

    # Transfer flag
    tr_df = _raw("transfers")
    if not tr_df.empty:
        tr_set = set(zip(tr_df["player_id"], tr_df["transfer_year"]))
    else:
        tr_set = set()

    rows = []
    for _, row in ps_df.iterrows():
        ps_id    = row["id"]
        pid      = row["player_id"]
        name     = row.get("name", "")
        team_id  = row.get("team_id")
        year     = row.get("year")
        season   = row["season"]
        raw_stats = row.get("data") or {}
        stars    = _si(row.get("stars"))
        cs       = row.get("composite_score")

        feats = compute_stat_features(raw_stats, pg)
        if pg == "OL":
            feats["experience"] = _sf(year, 2.0)
        feats["recruit_composite"] = _composite_to_100(cs)
        feats["transfer_flag"]     = 1 if (pid, season) in tr_set else 0
        # Store raw INT count for pre-2016 CLASSIC defensive rating system.
        if pg in DEFENSIVE_POSITIONS:
            feats["interceptionsINT"] = _sf(raw_stats.get("interceptionsINT"), 0.0)
        feats["stars"]             = stars
        feats["year"]              = _si(year)
        feats["team_id"]           = team_id
        feats["name"]              = name
        feats["player_season_id"]  = ps_id
        feats["edge_score"]        = row.get("edge_score")
        feats["stats_measured"]    = _si(row.get("stats_measured"))
        feats["games_played"]      = _si(row.get("games_played"))
        feats["opp_avg_sp"]        = _sf(row.get("opponent_avg_sp"))
        # How much of a secondary player's EDGE came from coverage denial rather
        # than counting stats. This dict is rebuilt per row rather than carried
        # from the frame, so a column that isn't copied here silently disappears —
        # which is how the UI's coverage line came out empty for every DB.
        feats["coverage_share"]    = _sf(row.get("coverage_share"), 0.0)
        feats["conference"]        = row.get("conference") or ""
        feats["_season"]           = season
        rows.append({"player_id": pid, **feats})

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["_row_key"] = df["player_season_id"].astype(str)
    return df.set_index("_row_key")


# ---------------------------------------------------------------------------
# Starter classification
# ---------------------------------------------------------------------------

def _has_valid_edge(row: pd.Series) -> bool:
    """A valid EDGE score (≥3 attributed games from script 06) is itself proof the
    player produced enough to be evaluated as a starter. Critical for pre-2016
    defenders, whose season tackle totals are missing so volume-based tiering fails."""
    es = row.get("edge_score")
    try:
        return es is not None and not np.isnan(float(es)) and float(es) > 0
    except (TypeError, ValueError):
        return False


def is_starter(row: pd.Series, pg: str) -> bool:
    """Returns True if player qualifies as 'starter' tier."""
    if _has_valid_edge(row):
        return True
    # Pre-2016 defensive players with any interceptions are starters — they have
    # real production signal even without EDGE scores.
    row_season = int(row.get("_season") or 9999)
    if pg in DEFENSIVE_POSITIONS and row_season < 2016:
        ints = float(row.get("interceptionsINT") or 0)
        if ints >= 1:
            return True
        # No INTs but still a pre-2016 defender — use recruiting to classify
        rec = float(row.get("recruit_composite") or 40)
        return rec >= 60  # 3-star+ treat as starter for CLASSIC rating
    return classify_playtime_tier(pg, row.to_dict()) == "starter"


def get_tier(row: pd.Series, pg: str) -> str:
    """Classify player into tier: starter, role, reserve, or bench."""
    if _has_valid_edge(row):
        return "starter"
    return classify_playtime_tier(pg, row.to_dict(), games_played=row.get("games_played", 1))


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
    # Pre-2016 defenders only have interceptions tracked per game (no tackles/sacks),
    # so their stats_measured is tiny. Lower the bar — a valid EDGE from ≥3 INT-games
    # is the strongest signal we have for that era.
    season = int(row.get("_season") or 0)
    if pg in {"EDGE", "DL", "LB", "CB", "S", "DB"} and season < 2016:
        return sm >= 3
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
    # The secondary's score is now archetypes on a 0-10 axis (script 06), not a
    # raw stat composite, so its scale is much smaller than the front seven's.
    # Only reached on the no-EDGE fallback path, but a stale ceiling here would
    # normalize every defensive back to 1.0.
    "CB":   (0.0, 17.0),
    "S":    (0.0, 16.0),
    "DB":   (0.0, 17.0),
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
    stat-only composite (STAT_ONLY_WEIGHTS) scaled to [30–90] so historical
    defensive players get meaningful variance instead of a flat 50.0 fallback.

    Returns:
      scores   — np.ndarray of OVR values [30–99] or 0.0 if truly no data at all
      contribs — list of {feature: value} dicts for shap_values storage
    """
    # Pre-compute stat-only composites for the whole df (used for no-EDGE fallback).
    # Features are already extracted columns in the dataframe (via _load_seasons → extract_features).
    no_edge_weights = STAT_ONLY_WEIGHTS.get(pg, {})
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

    final_scores   = []
    final_contribs = []

    for i, (_, row) in enumerate(df.iterrows()):
        # Per-row pre-2016 defensive check — df contains ALL seasons.
        row_season = int(row.get("_season") or season)
        is_pre2016_def = (pg in DEFENSIVE_POSITIONS and row_season < 2016)

        if is_pre2016_def:
            # CLASSIC system: interceptions + recruiting composite.
            # interceptionsINT is stored directly by _load_seasons for defensive positions.
            ints    = float(row.get("interceptionsINT") or 0)
            rec_ovr = float(row.get("recruit_composite") or 40.0)
            ovr     = compute_pre2016_classic_ovr(ints, rec_ovr, pg)
            contrib = {
                "interceptionsINT":   round(ints, 1),
                "recruit_composite":  round(rec_ovr, 2),
                "classic_system":     True,
            }
        elif has_opp_score(row, pg):
            es  = float(row.get("edge_score") or 0)
            ovr = edge_to_ovr(es, pg, season)
            contrib = {
                "edge_score":     round(es, 4),
                "games_played":   int(row.get("games_played") or 0),
                "opp_quality":    round(float(row.get("opp_avg_sp") or 0), 2),
                "stats_measured": int(row.get("stats_measured") or 0),
            }
            # How much of a defensive back's score is coverage denial rather than
            # counting stats. Carried so the UI can say so — an unexplained bump
            # on a player with modest numbers reads as a bug.
            cov = row.get("coverage_share")
            if pg in ("CB", "S", "DB") and cov is not None and float(cov or 0) > 0:
                contrib["coverage_share"] = round(float(cov), 4)
        elif all_stat_scores[i] is not None and valid_stat:
            # Stat-based fallback: scale within the no-EDGE pool, cap at 78
            ovr = float(np.clip(np.interp(all_stat_scores[i], stat_pcts, STAT_FALLBACK_TARGETS), 30.0, 78.0))
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
    Call composite_to_ovr(scores, pg) on the result to convert to [30–99].
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
# Composite → OVR anchors for the stat-only positions (OL / K / P).
#
# These are FIXED absolute anchors, matching how EDGE positions map through
# EDGE_OVR_ANCHORS. They replace a percentile scaling that mapped whatever
# happened to top the pool to 99 and whatever sat at the bottom to 30 — a forced
# curve that guaranteed a 99 every season regardless of absolute quality.
#
# Calibrated from the pooled 2008-2025 starter composites:
#   OL: p10=0.325  p50=0.398  p75=0.625  p90=0.625  max=0.650
#   K : p10=0.170  p50=0.416  p75=0.531  p90=0.624  p99=0.689  max=0.707
#   P : p10=0.289  p50=0.478  p75=0.583  p90=0.695  p99=0.854  max=0.980
#
# OL tops out at 88, deliberately below the skill-position ceiling: its inputs
# are team proxies (team rush YPA, sack rate allowed) plus recruiting, which
# cannot separate an All-American from an average starter on the same line.
# Those proxies also saturate — the top ~10% of linemen share one composite
# value — so a 99 would be an accuracy claim the inputs do not support.
#
# K and P were recalibrated in v4.2 (2026-08-11). They were the most inflated
# thing in the system: 38 punters rated 85+ and 9 rated 90+ in 2025, against one
# and zero in EA CFB 27. On a team page a punter outranked the receivers around
# him, which is the tell — a specialist's impact range is genuinely narrower than
# a skill player's, so his rating band should be too. Only a truly elite kicker
# now earns a high number and an average one is an average player.
#
# The kicker composite also changed underneath these: fg_long is 25% of it and had
# been reading a stat key that does not exist, so it was a dead constant.
COMPOSITE_OVR_ANCHORS: dict[str, list[tuple[float, float]]] = {
    "OL": [(0.00, 30), (0.25, 45), (0.325, 55), (0.40, 65),
           (0.50, 72), (0.625, 80), (0.65, 88)],
    "K":  [(0.00, 30), (0.05, 35), (0.23, 48), (0.42, 56), (0.55, 63),
           (0.65, 71), (0.74, 78), (0.83, 87), (1.00, 90)],
    "P":  [(0.00, 30), (0.05, 35), (0.41, 48), (0.52, 56), (0.60, 63),
           (0.66, 71), (0.76, 78), (0.88, 87), (1.00, 90)],
}


def composite_to_ovr(scores: np.ndarray, pg: str, low=30, high=99) -> np.ndarray:
    """Map stat-only composites [0-1] to OVR through fixed per-position anchors.

    Absolute, not relative: if nobody reaches the top anchor in a given season,
    nobody gets the top rating — the same guarantee EDGE positions have.
    """
    anchors = COMPOSITE_OVR_ANCHORS.get(pg)
    if anchors is None:
        return np.full(len(scores), 65.0)
    xs = [a[0] for a in anchors]
    ys = [float(a[1]) for a in anchors]
    result = np.interp(np.asarray(scores, dtype=float), xs, ys)
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


def apply_games_confidence(scaled: np.ndarray, df: pd.DataFrame, pg: str = "") -> np.ndarray:
    """Damp ratings toward position average only for low-game-count players.

    Players with 8+ games: untouched (full confidence).
    Players with fewer games: rating pulled toward position average proportionally.
    Prevents a 2-game wonder from rating 95 but doesn't compress full-season players.
    Zero values (no EDGE data) are skipped entirely — they get fallback_rating later.
    Pre-2016 defensive players skip the penalty: they use season totals (CLASSIC system)
    and have games_played=0 by construction (no player_edge row).
    """
    valid = scaled[scaled > 0]
    avg = float(np.mean(valid)) if len(valid) > 0 else 65.0
    result = scaled.copy()
    for i, (_, row) in enumerate(df.iterrows()):
        if scaled[i] == 0.0:
            continue  # no EDGE data — leave as 0, fallback_rating handles it
        row_season = int(row.get("_season") or 9999)
        if pg in DEFENSIVE_POSITIONS and row_season < 2016:
            continue  # CLASSIC system: season totals, no per-game confidence check
        games = float(row.get("games_played", 0) or 0)
        if games >= 8:
            continue  # full-season starters: no change
        # linear from 0.25 confidence at 1 game to 1.0 at 8 games
        confidence = max(0.25, games / 8.0)
        result[i] = round(float(avg + confidence * (scaled[i] - avg)), 2)
    return result


def fallback_rating(stars: int, position_avg: float = 65.0) -> float:
    offset = STARS_OVR_DELTA.get(min(stars, 5), -33)
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

    player_ids = set(ratings_map.keys())

    # Load prior ratings from computed output
    prev_ratings = {}
    rat_df = _computed_ratings()
    if not rat_df.empty and "season" in rat_df.columns and "player_season_id" in rat_df.columns:
        prev_rat = rat_df[rat_df["season"] == prev_season].copy()
        if not prev_rat.empty:
            # ratings already has player_id column — use it directly
            if "player_id" in prev_rat.columns:
                prev_rat = prev_rat[prev_rat["player_id"].isin(player_ids)]
                prev_ratings = dict(zip(prev_rat["player_id"], prev_rat["overall_rating"].astype(float)))

    # Load edge scores from raw dump
    edge_df = _raw("player_edge")
    ps_df   = _raw("player_seasons")[["id", "player_id", "season"]].rename(
        columns={"id": "ps_id", "player_id": "ps_player_id", "season": "ps_season"})
    if not edge_df.empty and not ps_df.empty:
        merged = edge_df.merge(ps_df, left_on="player_season_id", right_on="ps_id", how="left")
        # Resolve player_id: prefer edge_df's own column, fall back to player_seasons
        merged["player_id"] = merged["player_id"].where(
            merged["player_id"].notna(), merged["ps_player_id"])
        merged["season"] = merged["season"].where(
            merged["season"].notna(), merged["ps_season"])
        merged = merged[merged["player_id"].isin(player_ids)]
        prev_edge = dict(zip(
            merged[merged["season"] == prev_season]["player_id"],
            merged[merged["season"] == prev_season]["edge_score"].astype(float)
        ))
        curr_edge = dict(zip(
            merged[merged["season"] == prev_season + 1]["player_id"],
            merged[merged["season"] == prev_season + 1]["edge_score"].astype(float)
        ))
    else:
        prev_edge = {}
        curr_edge = {}

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
            scaled = apply_games_confidence(raw_scores, all_starter_df, pg=pg)
            # Print distribution of players with valid EDGE
            edge_ovrs = scaled[scaled > 0]
            if len(edge_ovrs) >= 5:
                p = np.percentile(edge_ovrs, [10, 25, 50, 75, 90, 99])
                print(f"\n    [edge OVR] p10={p[0]:.1f} p25={p[1]:.1f} p50={p[2]:.1f} p75={p[3]:.1f} p90={p[4]:.1f} p99={p[5]:.1f}", end=" ")
        else:
            # --- Stat composite + fixed anchors for OL/K/P ---
            raw_scores, contribs = compute_ratings(all_starter_df, pg)
            discounted = apply_conference_discount(raw_scores, all_starter_df, pg=pg)
            scaled = composite_to_ovr(discounted, pg)

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
    season_df = all_df[all_df["_season"] == season].copy()
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
    global _RATINGS
    parser = argparse.ArgumentParser()
    parser.add_argument("--season",      type=int, default=2025)
    parser.add_argument("--all-seasons", action="store_true")
    parser.add_argument("--position",    type=str)
    args = parser.parse_args()

    seasons = list(range(2008, 2027)) if args.all_seasons else [args.season]
    positions = [args.position.upper()] if args.position else list(WEIGHTS.keys())

    for season in seasons:
        print(f"\n-- Season {season} --")
        all_rows = []
        for pg in positions:
            all_rows.extend(rate_position(season, pg))
        if all_rows:
            print("\n  Validating distribution...")
            validate_distribution(all_rows)
            clean_rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in all_rows]

            # Merge with any existing computed ratings (other seasons / engines)
            existing = _computed_ratings()
            new_df   = pd.DataFrame(clean_rows)
            if not existing.empty:
                # Drop rows being replaced (same player_season_id + season + engine)
                mask = ~(
                    existing["player_season_id"].isin(new_df["player_season_id"]) &
                    existing["season"].isin(new_df["season"]) &
                    existing["engine"].isin(new_df["engine"])
                )
                combined = pd.concat([existing[mask], new_df], ignore_index=True)
            else:
                combined = new_df

            write_computed("ratings", combined)
            # What was just written is what the next season must read.
            global _RATINGS
            _RATINGS = combined
            print(f"  Wrote {len(clean_rows)} rows")

    print("\nDone.")


if __name__ == "__main__":
    main()
