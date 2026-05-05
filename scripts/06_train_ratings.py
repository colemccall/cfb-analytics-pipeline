"""CFB player ratings — Engine A (current-season performance).

Rating architecture:
  ENGINE A — "What did this player do this season?"
    QB/RB/WR: EDGE score (opponent-adjusted, situation-weighted EPA from
      play-by-play) is the PRIMARY input (~42-55%). Traditional stats fill the rest.
    TE/OL/DL/LB/DB/K/P: Stat-only composites (EDGE attribution too sparse
      for these positions to be meaningful).

  Cross-season normalization: percentile ranks are computed across ALL seasons
  (2021–2025) combined so a rating of 85 means the same thing in every year.

  Recruiting composite is used as an ANCHOR — not a primary input:
    - Starters: recruiting weight is 5-10%
    - Sub-threshold: blended fallback (70% recruiting, 30% efficiency-implied)
    - No stats / true freshmen: 100% recruiting fallback

Formula weights by position:
  QB:  edge_scaled 55%, yards_per_att 15%, td_int_ratio 15%, comp_pct 10%, recruit 5%
  RB:  edge_scaled 55%, yards_per_carry 20%, yards_total 15%, rec_versatility 5%, recruit 5%
  WR:  td_score 35%, yards_per_rec 28%, yards_total 22%, rec_volume 10%, recruit 5%
  TE:  td_score 22%, yards_per_rec 18%, yards_total 13%, rec_volume 5% (no EDGE)
  OL:  recruit 30%, team_rush_ypa 30%, team_sack_rate_inv 25%, experience 10%, award 5%
  DL:  pass_rush 38%, run_stop 28%, disruption 19%, volume 5%, recruit 10%
  LB:  tackling 33%, pass_rush 22%, coverage 20%, instinct 15%, recruit 10%
  DB:  coverage 38%, tackling 22%, instinct 20%, pass_rush 10%, recruit 10%
  K:   fg_pct 50%, fg_long 25%, xp_pct 15%, volume 10%
  P:   avg_yards 55%, inside_20_pct 30%, volume 15%

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
from sklearn.preprocessing import MinMaxScaler

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.db import bulk_upsert, get_connection

MODEL_VERSION = "v3.1-multiseason"

# ---------------------------------------------------------------------------
# Starter thresholds — determines how much we trust stats vs recruiting
# ---------------------------------------------------------------------------

STARTER_THRESHOLDS = {
    "QB":  ("passingATT",  100),
    "RB":  ("rushingCAR",   60),
    "WR":  ("receivingREC", 20),
    "TE":  ("receivingREC", 10),
    "OL":  (None,            0),   # team proxy only
    "DL":  ("defensiveTOT", 10),
    "LB":  ("defensiveTOT", 20),
    "DB":  ("defensiveTOT", 15),
    "K":   ("kickingFGM",    5),
    "P":   ("puntingNO",    10),
}

# Recruiting fallback: how much the overall rating shifts from position average
# based on recruiting stars when a player has NO usable stats.
STARS_FALLBACK = {5: -3, 4: -8, 3: -15, 2: -22, 1: -28, 0: -33}

# Positions where EDGE is the primary rating driver.
# QB/RB: EDGE attribution is comprehensive (~60-70% of starters get edge_scaled).
# WR: only ~25% of starters get attribution — mixing EDGE and non-EDGE players in
#   the same normalized pool creates systematic bias against non-EDGE players.
# TE/DL/LB/DB: attribution near-zero (<1% of starters). Stat-only for all.
EDGE_POSITIONS = {"QB", "RB"}

# Hard ceiling on overall_rating by position (K/P have inflated stats by nature)
POSITION_CEILING = {"K": 78, "P": 78}

# All seasons used for cross-season normalization
ALL_SEASONS = list(range(2021, 2026))


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

    if pg == "DB":
        tot   = max(_f(stats, "defensiveTOT"), 1)
        sacks = _f(stats, "defensiveSACKS")
        tfl   = _f(stats, "defensiveTFL")
        ints  = _f(stats, "interceptionsINT")
        pbu   = _f(stats, "defensivePD")
        return {
            "coverage_score":   ints * 3.0 + pbu * 1.5,                 # ball skills
            "tackling_score":   tot * 0.5 + tfl * 2.0,                  # run support
            "pass_rush_score":  sacks * 4.0 + tfl * 1.5,                # blitz value
            "instinct_score":   (ints + pbu + tfl * 0.5) / tot,         # playmaking rate (coverage + disruption)
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
# EDGE positions: edge_scaled is primary; stat features fill the rest.
# Non-EDGE positions: stat-only composite.
# Recruiting weight = 5% for starters, 15% for non-EDGE positions.
# ---------------------------------------------------------------------------

WEIGHTS = {
    "QB": {
        "edge_scaled":     0.55,
        "yards_per_att":   0.15,
        "td_int_ratio":    0.15,
        "comp_pct":        0.10,
        "recruit_composite": 0.05,
    },
    "RB": {
        "edge_scaled":     0.55,
        "yards_per_carry": 0.20,
        "yards_total":     0.15,
        "rec_versatility": 0.05,
        "recruit_composite": 0.05,
    },
    "WR": {
        "td_score":        0.35,
        "yards_per_rec":   0.28,
        "yards_total":     0.22,
        "rec_volume":      0.10,
        "recruit_composite": 0.05,
    },
    "TE": {
        "td_score":        0.35,
        "yards_per_rec":   0.28,
        "yards_total":     0.22,
        "rec_volume":      0.10,
        "recruit_composite": 0.05,
    },
    # Non-EDGE positions: no edge_scaled, higher recruit weight
    "OL": {
        "team_rush_ypa":      0.30,
        "team_sack_rate_inv": 0.25,
        "recruit_composite":  0.30,   # primary individual differentiator
        "experience":         0.10,
        "award_tier":         0.05,
    },
    # DL/LB/DB: no edge_scaled — defender attribution in play-by-play is too
    # sparse (~4-14 players/season with edge_scaled vs ~800 starters).
    # Weights reflect the composite scores defined in extract_features().
    "DL": {
        "pass_rush_score":  0.38,
        "run_stop_score":   0.28,
        "disruption_rate":  0.19,
        "volume_score":     0.05,
        "recruit_composite": 0.10,
    },
    "LB": {
        "tackling_score":   0.33,
        "pass_rush_score":  0.22,
        "coverage_score":   0.20,
        "instinct_score":   0.15,
        "recruit_composite": 0.10,
    },
    "DB": {
        "coverage_score":   0.32,
        "tackling_score":   0.22,
        "instinct_score":   0.20,
        "pass_rush_score":  0.16,
        "recruit_composite": 0.10,
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

# When EDGE is missing for an EDGE-position starter, fall back to stat-only weights
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
    "DB": {
        "coverage_score":   0.27,
        "tackling_score":   0.20,
        "pass_rush_score":  0.17,
        "instinct_score":   0.16,
        "volume_score":     0.05,
        "recruit_composite": 0.15,
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
            """SELECT player_season_id, season, edge_scaled, plays_counted, crunch_epa
               FROM player_edge
               WHERE player_season_id = ANY(%s)""",
            (all_ps_ids,)
        )
        edge_map = {ps_id: {"edge_scaled": es, "plays_counted": pc, "crunch_epa": ce}
                    for ps_id, s, es, pc, ce in cur.fetchall()}

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
        feats["edge_scaled"]       = edge_info.get("edge_scaled")
        plays_counted              = edge_info.get("plays_counted") or 0
        feats["plays_counted"]     = plays_counted
        feats["crunch_epa"]        = edge_info.get("crunch_epa") or 0.0
        feats["games_played"]      = min(plays_counted / 15.0, 15.0)
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
    key, threshold = STARTER_THRESHOLDS.get(pg, (None, 0))
    if key is None:
        return True
    return (row.get("volume_score", 0) or 0) >= threshold


def has_edge(row: pd.Series) -> bool:
    v = row.get("edge_scaled")
    return v is not None and not pd.isna(v)


# ---------------------------------------------------------------------------
# Rating computation
# ---------------------------------------------------------------------------

def percentile_rank_cols(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    result = df.copy()
    for c in cols:
        if c not in df.columns:
            result[c] = 0.5
            continue
        vals = df[c].fillna(0)
        result[c] = vals.rank(pct=True, method="average")
    return result


def compute_ratings(df: pd.DataFrame, pg: str) -> tuple[np.ndarray, list[dict]]:
    """
    Returns:
      scores   — np.ndarray of composite scores (0–1)
      contribs — list of {feature: contribution_fraction} dicts for shap_values storage
    """
    contribs = []
    scores   = np.zeros(len(df))

    for i, (pid, row) in enumerate(df.iterrows()):
        # Choose weights based on whether EDGE data exists
        if pg in EDGE_POSITIONS and has_edge(row):
            weights = WEIGHTS[pg]
        elif pg in EDGE_POSITIONS:
            weights = WEIGHTS_NO_EDGE.get(pg, WEIGHTS[pg])
        else:
            weights = WEIGHTS.get(pg, {})

        feature_cols = list(weights.keys())
        # We'll compute percentile ranks below — store raw values per player per iteration
        # NOTE: percentile ranks are computed at the full-population level below (not per-player)
        contribs.append({"_weights": weights, "_feature_cols": feature_cols})

    # Population-level percentile ranking
    # First pass: compute for all features across all players
    all_weights = WEIGHTS[pg] if pg not in EDGE_POSITIONS else WEIGHTS[pg]
    all_feature_cols = list(all_weights.keys())

    # Also need no-edge cols for players missing EDGE
    no_edge_cols = list(WEIGHTS_NO_EDGE.get(pg, {}).keys()) if pg in EDGE_POSITIONS else []
    all_cols = list(set(all_feature_cols + no_edge_cols))

    ranked = percentile_rank_cols(df, all_cols)

    final_scores  = []
    final_contribs = []

    for i, (pid, row) in enumerate(df.iterrows()):
        if pg in EDGE_POSITIONS and has_edge(row):
            weights = WEIGHTS[pg]
        elif pg in EDGE_POSITIONS:
            weights = WEIGHTS_NO_EDGE.get(pg, WEIGHTS[pg])
        else:
            weights = WEIGHTS.get(pg, {})

        feature_cols = list(weights.keys())
        w_arr = np.array([weights[c] for c in feature_cols])
        total_w = w_arr.sum()

        x = np.array([ranked.loc[pid, c] if c in ranked.columns else 0.5
                       for c in feature_cols])
        score = float(x @ w_arr)

        contrib = {}
        for j, feat in enumerate(feature_cols):
            deviation = x[j] - 0.5
            contrib[feat] = round(float(deviation * w_arr[j] / total_w), 4)

        final_scores.append(score)
        final_contribs.append(contrib)

    return np.array(final_scores), final_contribs


# Conference discount — only applied to stat-only positions (non-EDGE).
# EDGE positions (QB/RB) are opponent-adjusted at the play level via SP+,
# so a G5 conference label would double-penalize what the model already handles.
# For stat-only positions (WR/TE/OL/DL/LB/DB), raw counting stats don't carry
# opponent context, so a modest discount still applies.
P4_CONFERENCES = {"SEC", "Big Ten", "Big 12", "ACC", "Pac-12"}
G5_DISCOUNT = 0.95   # reduced — stat-only positions only, lighter touch


def scale_to_range(scores: np.ndarray, low=30, high=99, pg: str = "") -> np.ndarray:
    """Z-score sigmoid normalization across the full multi-season population.

    Sigmoid shifted so median starter (z=0) -> ~65. Per-position steepness
    controls spread: higher steepness = more separation between best and average.

    Because scores are computed against ALL seasons combined, the scale is
    consistent year-over-year: a 90 in 2021 means the same thing as a 90 in 2025.
    """
    if len(scores) < 2:
        return np.full(len(scores), 65.0)
    mu  = np.mean(scores)
    std = np.std(scores)
    if std < 1e-9:
        return np.full(len(scores), 65.0)
    z = (scores - mu) / std
    # Single steepness value for all positions.
    # With a cross-season pool of 750–4500 starters, the natural z-score
    # variance is already wide — no need to amplify it per-position.
    # 0.9 maps median starter -> ~65 and top 1% -> ~90-95 naturally.
    steepness = 0.9
    raw = 100.0 / (1.0 + np.exp(-steepness * (z + 0.75))) * 1.08 - 4.0
    return np.clip(raw, low, high).round(2)


def apply_conference_discount(scores: np.ndarray, df: pd.DataFrame, pg: str = "") -> np.ndarray:
    """Apply a modest discount to G5 players at stat-only positions.

    EDGE positions (QB/RB) skip this entirely — their raw scores are already
    opponent-adjusted at the play level via SP+. Applying a conference penalty
    on top would double-penalize G5 players for the same competition factor
    that EDGE already accounts for.

    For stat-only positions (WR/TE/OL/DL/LB/DB), counting stats carry no
    opponent context, so a small conference-level adjustment still applies.
    """
    if pg in EDGE_POSITIONS:
        return scores.copy()   # EDGE handles opponent quality — no extra penalty

    result = scores.copy()
    for i, (_, row) in enumerate(df.iterrows()):
        conf = row.get("conference") or ""
        if conf not in P4_CONFERENCES:
            result[i] = scores[i] * G5_DISCOUNT
    return result


def apply_games_confidence(scaled: np.ndarray, df: pd.DataFrame) -> np.ndarray:
    """Damp ratings toward position average only for low-game-count players.

    Players with 8+ games: untouched (full confidence).
    Players with fewer games: rating pulled toward position average proportionally.
    Prevents a 2-game wonder from rating 95 but doesn't compress full-season players.
    """
    avg = float(np.mean(scaled))
    result = scaled.copy()
    for i, (_, row) in enumerate(df.iterrows()):
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

def compute_trajectory(ratings_map: dict, prev_season: int) -> dict:
    if not ratings_map:
        return {}
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT player_id, overall_rating FROM ratings WHERE season = %s AND player_id = ANY(%s)",
            (prev_season, list(ratings_map.keys()))
        )
        prev = {pid: float(r) for pid, r in cur.fetchall()}
    return {pid: round(float(curr) - prev[pid], 2) if pid in prev else 0.0
            for pid, curr in ratings_map.items()}


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

    edge_count = all_starter_df["edge_scaled"].notna().sum() if "edge_scaled" in all_starter_df.columns else 0

    if len(all_starter_df) >= 5:
        raw_scores, contribs = compute_ratings(all_starter_df, pg)
        discounted = apply_conference_discount(raw_scores, all_starter_df, pg=pg)
        scaled = scale_to_range(discounted, pg=pg)
        if pg in EDGE_POSITIONS:
            scaled = apply_games_confidence(scaled, all_starter_df)
        for i, rkey in enumerate(all_starter_df.index):
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

    # Compute an efficiency percentile across all starters for blending
    eff_col = {
        "QB": "yards_per_att", "RB": "yards_per_carry",
        "WR": "yards_per_rec", "TE": "yards_per_rec",
        "DL": "disruption_rate", "LB": "instinct_score", "DB": "instinct_score",
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
    season_pid_rating: dict[int, float] = {}
    for rkey, row in season_df.iterrows():
        pid = int(row["player_id"])
        season_pid_rating[pid] = float(ratings_map.get(rkey, 50.0))

    trajectory = compute_trajectory(season_pid_rating, season - 1)

    # Breakout probability — use this season's subset only
    season_pids  = list(season_pid_rating.keys())
    season_rats  = np.array([season_pid_rating[p] for p in season_pids])
    # Build a sub-df indexed by player_id for compute_breakout
    sub_df = season_df.copy()
    sub_df.index = sub_df["player_id"].astype(int)
    breakout_arr = compute_breakout(sub_df.loc[sub_df.index.isin(season_pids)], season_rats)
    breakout_map = dict(zip(season_pids, breakout_arr))

    ceiling = POSITION_CEILING.get(pg)
    starters_this_season = season_df.apply(lambda r: is_starter(r, pg), axis=1).sum()
    edge_this_season = (
        season_df["edge_scaled"].notna().sum()
        if "edge_scaled" in season_df.columns else 0
    )

    rows = []
    for rkey, row in season_df.iterrows():
        pid    = int(row["player_id"])
        ps_id  = int(row["player_season_id"])
        ovr    = float(ratings_map.get(rkey, 50.0))
        if ceiling:
            ovr = min(ovr, ceiling)
        rows.append({
            "player_season_id":     ps_id,
            "season":               int(season),
            "overall_rating":       ovr,
            "position_rating":      ovr,
            "trajectory_score":     float(trajectory.get(pid, 0.0)),
            "breakout_probability": float(breakout_map.get(pid, 0.05)),
            "shap_values":          json.dumps(contrib_map.get(rkey, {})),
            "model_version":        MODEL_VERSION,
        })

    edge_info = f", EDGE: {edge_this_season}" if pg in EDGE_POSITIONS else ""
    print(f"{len(rows)} players this season (starters: {starters_this_season}{edge_info}, total pool: {len(all_starter_df)} starters across all seasons)")
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--season",      type=int, default=2025)
    parser.add_argument("--all-seasons", action="store_true")
    parser.add_argument("--position",    type=str)
    args = parser.parse_args()

    seasons = list(range(2021, 2026)) if args.all_seasons else [args.season]
    positions = [args.position.upper()] if args.position else list(WEIGHTS.keys())

    for season in seasons:
        print(f"\n-- Season {season} --")
        all_rows = []
        for pg in positions:
            all_rows.extend(rate_position(season, pg))
        if all_rows:
            bulk_upsert("ratings", all_rows, "player_season_id")
            print(f"  Upserted {len(all_rows)} rows")

    print("\nDone.")


if __name__ == "__main__":
    main()
