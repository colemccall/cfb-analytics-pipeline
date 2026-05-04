"""CFB player ratings — Engine A (current-season performance).

Rating architecture:
  ENGINE A — "What did this player do this season?"
    Offensive skill (QB/RB/WR/TE): EDGE score (opponent-adjusted, situation-weighted
      EPA from play-by-play) is the PRIMARY input. Traditional season stats serve as
      supplementary features to fill gaps when EDGE data is thin.
    Defensive / OL / Special Teams: Season stat percentile composite (no EPA
      attribution available in play-by-play for these positions).

  Recruiting composite is used as an ANCHOR — not a primary input:
    - Starters (≥ threshold plays/attempts): recruiting weight is 0–5%
    - Rotation (partial data): 10–15%
    - No stats / true freshmen: 100% recruiting fallback

  This is honest: if a player played, we trust what they did on the field.
  If they didn't play, we fall back to what scouts expected of them.

Formula weights by position:
  QB:  edge_scaled 55%, yards_per_att 15%, td_int_ratio 15%, comp_pct 10%, recruit 5%
  RB:  edge_scaled 55%, yards_per_carry 20%, yards_total 15%, rec_versatility 5%, recruit 5%
  WR:  edge_scaled 55%, yards_per_rec 20%, yards_total 15%, rec_volume 5%, recruit 5%
  TE:  edge_scaled 55%, yards_per_rec 20%, yards_total 15%, rec_volume 5%, recruit 5%
  OL:  team_rush_ypa 40%, team_sack_rate_inv 35%, award_tier 10%, recruit 15%
  DL:  tfl_total 30%, sacks_total 30%, tackles_total 15%, volume 10%, recruit 15%
  LB:  tackles_total 30%, tfl_total 20%, sacks_total 15%, ints_total 10%, volume 10%, recruit 15%
  DB:  ints_total 30%, pbu_total 25%, tackles_total 15%, volume 10%, recruit 20%
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

MODEL_VERSION = "v3.0-edge"

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

# Positions where EDGE (play-by-play EPA) is available and trusted
EDGE_POSITIONS = {"QB", "RB", "WR", "TE", "DL", "LB", "DB"}


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
        return {
            "yards_per_rec": yds / rec,
            "yards_total":   yds,
            "rec_volume":    _f(stats, "receivingREC"),
            "volume_score":  _f(stats, "receivingREC"),
        }

    if pg == "OL":
        return {
            "team_rush_ypa":      _f(stats, "team_rush_ypa"),
            "team_sack_rate_inv": 1.0 - min(_f(stats, "team_sack_rate"), 1.0),
            "award_tier":         _f(stats, "award_tier"),
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
            "instinct_score":   (ints + pbu) / tot,                     # playmaking rate
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
        "edge_scaled":     0.55,
        "yards_per_rec":   0.20,
        "yards_total":     0.15,
        "rec_volume":      0.05,
        "recruit_composite": 0.05,
    },
    "TE": {
        "edge_scaled":     0.55,
        "yards_per_rec":   0.20,
        "yards_total":     0.15,
        "rec_volume":      0.05,
        "recruit_composite": 0.05,
    },
    # Non-EDGE positions: no edge_scaled, higher recruit weight
    "OL": {
        "team_rush_ypa":      0.40,
        "team_sack_rate_inv": 0.35,
        "award_tier":         0.10,
        "recruit_composite":  0.15,
    },
    "DL": {
        "edge_scaled":      0.50,
        "tfl_total":        0.18,
        "sacks_total":      0.17,
        "tackles_total":    0.07,
        "recruit_composite": 0.08,
    },
    "LB": {
        "edge_scaled":      0.50,
        "tackles_total":    0.18,
        "tfl_total":        0.12,
        "sacks_total":      0.08,
        "ints_total":       0.05,
        "recruit_composite": 0.07,
    },
    "DB": {
        "edge_scaled":      0.50,
        "ints_total":       0.18,
        "pbu_total":        0.14,
        "tackles_total":    0.08,
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
        "yards_per_rec":   0.40,
        "yards_total":     0.40,
        "rec_volume":      0.10,
        "volume_score":    0.05,
        "recruit_composite": 0.05,
    },
    "TE": {
        "yards_per_rec":   0.40,
        "yards_total":     0.40,
        "rec_volume":      0.10,
        "volume_score":    0.05,
        "recruit_composite": 0.05,
    },
    "DL": {
        "tfl_total":        0.35,
        "sacks_total":      0.30,
        "tackles_total":    0.15,
        "volume_score":     0.05,
        "recruit_composite": 0.15,
    },
    "LB": {
        "tackles_total":    0.30,
        "tfl_total":        0.22,
        "sacks_total":      0.15,
        "ints_total":       0.10,
        "volume_score":     0.08,
        "recruit_composite": 0.15,
    },
    "DB": {
        "ints_total":       0.30,
        "pbu_total":        0.28,
        "tackles_total":    0.15,
        "volume_score":     0.07,
        "recruit_composite": 0.20,
    },
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_position_data(season: int, pg: str) -> pd.DataFrame:
    """Load players, stats, EDGE scores, and recruiting for a position group.

    Only includes players who appeared on a roster in this season, determined
    by the presence of a season_aggregate stats row. This prevents ghost ratings
    for players who transferred, graduated, or turned pro in prior seasons.
    """
    with get_connection() as conn:
        cur = conn.cursor()

        # Only players who have a stats row this season — they were on a roster
        cur.execute(
            """SELECT p.id, p.name, p.team_id, p.year, s.data
               FROM players p
               JOIN stats s ON s.player_id = p.id
               WHERE p.position_group = %s
                 AND s.season = %s
                 AND s.stat_type = 'season_aggregate'
                 AND s.game_id IS NULL""",
            (pg, season)
        )
        raw_rows = cur.fetchall()
        if not raw_rows:
            return pd.DataFrame()

        player_rows = [(r[0], r[1], r[2], r[3]) for r in raw_rows]
        player_ids = [r[0] for r in player_rows]

        stats_map = {}
        for r in raw_rows:
            pid, data = r[0], r[4]
            stats_map[pid] = data if isinstance(data, dict) else json.loads(data)

        # EDGE scores (NULL if not enough plays)
        cur.execute(
            """SELECT player_id, edge_scaled, plays_counted, crunch_epa
               FROM player_edge
               WHERE season = %s AND player_id = ANY(%s)""",
            (season, player_ids)
        )
        edge_map = {pid: {"edge_scaled": es, "plays_counted": pc, "crunch_epa": ce}
                    for pid, es, pc, ce in cur.fetchall()}

        # Recruiting (most recent composite within 5 years)
        cur.execute(
            """SELECT player_id, stars, composite_score FROM recruiting
               WHERE recruit_year >= %s AND player_id = ANY(%s)
               ORDER BY composite_score DESC NULLS LAST""",
            (season - 5, player_ids)
        )
        rec_map = {}
        for pid, stars, cs in cur.fetchall():
            if pid not in rec_map:
                rec_map[pid] = {"stars": stars or 0, "composite_score": cs}

        # Conference for G5 discount
        cur.execute(
            "SELECT t.id, t.conference FROM teams t WHERE t.id = ANY(%s)",
            (list({r[2] for r in player_rows if r[2]}),)
        )
        conf_map = {tid: conf for tid, conf in cur.fetchall()}

        # Transfer history — used to resolve correct team per season
        # For a player who transferred in year Y, to_team_id is their team for seasons >= Y.
        # Take the most recent transfer at or before this season.
        cur.execute(
            """SELECT player_id, transfer_year, to_team_id
               FROM transfers
               WHERE player_id = ANY(%s) AND transfer_year <= %s AND to_team_id IS NOT NULL
               ORDER BY transfer_year DESC""",
            (player_ids, season)
        )
        # Build {player_id: team_id_for_this_season}
        transfer_team_map: dict = {}
        transfer_set: set = set()
        for pid, yr, to_tid in cur.fetchall():
            if pid not in transfer_team_map:
                transfer_team_map[pid] = to_tid
            if yr == season:
                transfer_set.add(pid)

    rows = []
    for pid, name, team_id, year in player_rows:
        raw_stats = stats_map.get(pid, {})
        rec       = rec_map.get(pid, {})
        stars     = int(rec.get("stars") or 0)
        cs        = rec.get("composite_score")
        edge_info = edge_map.get(pid, {})

        feats = extract_features(raw_stats, pg)
        feats["recruit_composite"] = _composite_to_100(cs)
        feats["transfer_flag"]     = 1 if pid in transfer_set else 0
        feats["stars"]             = stars
        feats["year"]              = int(year or 0)
        # Use transfer-resolved team for this season; fall back to players.team_id
        feats["team_id"]           = transfer_team_map.get(pid, team_id)
        feats["name"]              = name
        # EDGE — may be None if insufficient plays
        feats["edge_scaled"]       = edge_info.get("edge_scaled")
        plays_counted              = edge_info.get("plays_counted") or 0
        feats["plays_counted"]     = plays_counted
        feats["crunch_epa"]        = edge_info.get("crunch_epa") or 0.0
        # Estimate games played from attributed plays (~15 plays/game for skill positions)
        feats["games_played"]      = min(plays_counted / 15.0, 15.0)
        # Conference — used for G5 discount (resolved via season-correct team_id)
        resolved_tid = transfer_team_map.get(pid, team_id)
        feats["conference"]        = conf_map.get(resolved_tid, "")
        rows.append({"player_id": pid, **feats})

    return pd.DataFrame(rows).set_index("player_id")


# ---------------------------------------------------------------------------
# Starter classification
# ---------------------------------------------------------------------------

def is_starter(row: pd.Series, pg: str) -> bool:
    key, threshold = STARTER_THRESHOLDS.get(pg, (None, 0))
    if key is None:
        return True
    return (row.get("volume_score", 0) or 0) >= threshold


def has_edge(row: pd.Series) -> bool:
    return row.get("edge_scaled") is not None and not pd.isna(row.get("edge_scaled"))


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


# P4 conferences — players in other conferences get a G5 discount
P4_CONFERENCES = {"SEC", "Big Ten", "Big 12", "ACC", "Pac-12"}
G5_DISCOUNT = 0.93   # G5 raw scores multiplied by this before normalization


def scale_to_range(scores: np.ndarray, low=30, high=99) -> np.ndarray:
    """Z-score → sigmoid normalization.

    Maps raw composite scores to 0–100 using the population's mean and std.
    The sigmoid shape means a player needs to be genuinely elite (+3σ) to
    reach 95+. Most seasons top out at 90–94; generational seasons hit 97–99.
    A few 94–99 players per position per season is by design.
    """
    if len(scores) < 2:
        return np.full(len(scores), 55.0)
    mu  = np.mean(scores)
    std = np.std(scores)
    if std < 1e-9:
        return np.full(len(scores), 55.0)
    z = (scores - mu) / std
    # Sigmoid: output ≈ 55 at z=0, 75 at z=1, 88 at z=2, 95 at z=3, 98 at z=4
    raw = 100.0 / (1.0 + np.exp(-0.85 * z)) * 1.06 - 3.0
    return np.clip(raw, low, high).round(2)


def apply_conference_discount(scores: np.ndarray, df: pd.DataFrame) -> np.ndarray:
    """Apply a small discount to G5 players before final scaling.

    G5 players face weaker competition on average; their raw production
    numbers are slightly inflated relative to P4 peers. Jeanty-level
    seasons still rate elite — the discount only affects average G5 starters.
    """
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


def fallback_rating(stars: int, position_avg: float = 60.0) -> float:
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
    print(f"  {pg}...", end=" ", flush=True)
    df = load_position_data(season, pg)
    if df.empty:
        print("no data")
        return []

    starter_mask = df.apply(lambda r: is_starter(r, pg), axis=1)
    starter_df   = df[starter_mask]
    backup_df    = df[~starter_mask]

    edge_count = starter_df["edge_scaled"].notna().sum() if "edge_scaled" in starter_df.columns else 0

    ratings_map: dict[int, float] = {}
    contrib_map: dict[int, dict]  = {}

    if len(starter_df) >= 5:
        raw_scores, contribs = compute_ratings(starter_df, pg)
        # Apply G5 discount before z-score normalization so the sigmoid
        # naturally places G5 players slightly below equivalent P4 output
        discounted = apply_conference_discount(raw_scores, starter_df)
        scaled = scale_to_range(discounted)
        # Only apply sample-size damping for EDGE positions (QB/RB/WR/TE)
        # where plays_counted is meaningful; skip for stat-only positions
        if pg in EDGE_POSITIONS:
            scaled = apply_games_confidence(scaled, starter_df)
        for i, pid in enumerate(starter_df.index):
            ratings_map[pid] = float(scaled[i])
            contrib_map[pid] = contribs[i]
    else:
        print(f"(only {len(starter_df)} starters — fallback only) ", end="")
        for pid, row in df.iterrows():
            ratings_map[pid] = fallback_rating(int(row.get("stars", 0)))

    # Fallback for backups / low-snap players
    pos_avg = float(np.mean(list(ratings_map.values()))) if ratings_map else 60.0
    for pid, row in backup_df.iterrows():
        ratings_map[pid] = fallback_rating(int(row.get("stars", 0)), pos_avg)
        contrib_map[pid] = {"recruit_composite": 0.5}  # 100% recruit-driven

    trajectory   = compute_trajectory(ratings_map, season - 1)
    all_pids     = [pid for pid in df.index if pid in ratings_map]
    all_ratings  = np.array([ratings_map[pid] for pid in all_pids])
    breakout_arr = compute_breakout(df.loc[df.index.isin(all_pids)], all_ratings)
    breakout_map = dict(zip(all_pids, breakout_arr))

    rows = []
    for pid in df.index:
        rows.append({
            "player_id":            int(pid),
            "season":               int(season),
            "overall_rating":       float(ratings_map.get(pid, 50.0)),
            "position_rating":      float(ratings_map.get(pid, 50.0)),
            "trajectory_score":     float(trajectory.get(pid, 0.0)),
            "breakout_probability": float(breakout_map.get(pid, 0.05)),
            "shap_values":          json.dumps(contrib_map.get(pid, {})),
            "team_id":              int(df.loc[pid, "team_id"]) if df.loc[pid, "team_id"] else None,
            "model_version":        MODEL_VERSION,
        })

    edge_info = f", EDGE: {edge_count}" if pg in EDGE_POSITIONS else ""
    print(f"{len(rows)} players (starters: {len(starter_df)}{edge_info}, fallback: {len(backup_df)})")
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
            bulk_upsert("ratings", all_rows, ["player_id", "season"])
            print(f"  Upserted {len(all_rows)} rows")

    print("\nDone.")


if __name__ == "__main__":
    main()
