"""Train XGBoost player ratings model → upsert results to Supabase ratings table.

Approach:
  - Per-position models (QB, RB, WR, TE, OL, DL, LB, DB, K, P)
  - Train on starter-tier players only (sufficient stat volume per position)
  - Stars-anchored fallback ratings for low-snap / backup players (from v1 logic)
  - SHAP values stored as JSONB for frontend explainability
  - Trajectory score = YoY change in overall_rating
  - Breakout probability = probability of top-quartile performance next season

Features by position group (all pulled from stats.data JSONB + recruiting + transfers):

  QB:  comp_pct, yards_per_att, td_int_ratio, ppa, snap_pct, recruit_composite, transfer_flag, sp_quality
  RB:  yards_per_carry, yards_per_game, rec_per_game, ppa, snap_pct, recruit_composite, transfer_flag
  WR:  yards_per_rec, catch_rate, rec_per_game, ppa, snap_pct, recruit_composite, transfer_flag
  TE:  yards_per_rec, catch_rate, rec_per_game, ppa, snap_pct, recruit_composite, transfer_flag
  OL:  team_rush_ypa, team_sack_rate, award_tier, recruit_composite, draft_round_proxy
  DL:  tackles_per_game, sacks_per_game, tfl_per_game, ppa, recruit_composite, transfer_flag
  LB:  tackles_per_game, sacks_per_game, tfl_per_game, ints_per_game, ppa, recruit_composite
  DB:  tackles_per_game, ints_per_game, pbu_per_game, ppa, recruit_composite, transfer_flag
  K:   fg_pct, fg_long, xp_pct
  P:   avg_yards, inside_20_pct

Usage:
    python scripts/06_train_ratings.py              # current season (2025)
    python scripts/06_train_ratings.py --season 2024
    python scripts/06_train_ratings.py --all-seasons # train on 2021-2025, rate 2025
"""

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.preprocessing import MinMaxScaler
import xgboost as xgb
import shap

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.db import bulk_upsert
from utils.supabase_client import get_client

OUTPUT_DIR = Path(__file__).parent.parent / "output" / "models"
MODEL_VERSION = "v1.0-xgb"

# Minimum stat volume to qualify for ML rating (otherwise use fallback)
STARTER_THRESHOLDS = {
    "QB":  {"passingAtt": 100},
    "RB":  {"rushingCar": 60},
    "WR":  {"receivingRec": 20},
    "TE":  {"receivingRec": 10},
    "OL":  {},         # No individual stats — always use team proxy
    "DL":  {"defensiveTot": 10},
    "LB":  {"defensiveTot": 20},
    "DB":  {"defensiveTot": 15},
    "K":   {"kickingFGM": 5},
    "P":   {"puntingPunts": 10},
}

# Stars → baseline rating offset from team starter average (from v1 rating_engine.py)
STARS_FALLBACK_OFFSET = {5: -3, 4: -8, 3: -15, 2: -22, 1: -28, 0: -33}


# ---------------------------------------------------------------------------
# Data loading from Supabase
# ---------------------------------------------------------------------------

def load_players_with_stats(season: int, position_group: str) -> pd.DataFrame:
    """Pull players + season stats + recruiting + transfers for one position group."""
    client = get_client()

    # Players
    players_res = (
        client.table("players")
        .select("id, name, cfb_api_id, position_group, team_id, year")
        .eq("position_group", position_group)
        .execute()
    )
    if not players_res.data:
        return pd.DataFrame()

    player_ids = [r["id"] for r in players_res.data]
    players_df = pd.DataFrame(players_res.data).set_index("id")

    # Season-aggregate stats
    stats_res = (
        client.table("stats")
        .select("player_id, data")
        .in_("player_id", player_ids)
        .eq("season", season)
        .eq("stat_type", "season_aggregate")
        .execute()
    )
    stats_map = {}
    for row in stats_res.data or []:
        data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
        stats_map[row["player_id"]] = data

    # Recruiting (most recent class within 5 years of current season)
    rec_res = (
        client.table("recruiting")
        .select("player_id, stars, composite_score, recruit_year")
        .in_("player_id", player_ids)
        .gte("recruit_year", season - 5)
        .execute()
    )
    rec_map = {}
    for row in rec_res.data or []:
        pid = row["player_id"]
        # Keep highest composite score if multiple classes
        if pid not in rec_map or (row.get("composite_score") or 0) > (rec_map[pid].get("composite_score") or 0):
            rec_map[pid] = row

    # Transfer flag
    xfer_res = (
        client.table("transfers")
        .select("player_id")
        .in_("player_id", player_ids)
        .eq("transfer_year", season)
        .execute()
    )
    transfer_set = {r["player_id"] for r in xfer_res.data or []}

    # Assemble rows
    rows = []
    for pid, player in players_df.iterrows():
        stats = stats_map.get(pid, {})
        rec = rec_map.get(pid, {})
        row = {
            "player_id":   pid,
            "name":        player["name"],
            "team_id":     player["team_id"],
            "year":        player.get("year", 0) or 0,
            "stars":       rec.get("stars", 0) or 0,
            "composite_score": rec.get("composite_score", 0.8) or 0.8,
            "recruit_composite": _composite_to_100(rec.get("composite_score")),
            "transfer_flag": 1 if pid in transfer_set else 0,
            "games_played": stats.get("games_played", 0) or 0,
            "snap_pct":    stats.get("snap_pct", 0) or 0,
            "award_tier":  stats.get("award_tier", 0) or 0,
            "ppa":         stats.get("ppa", 0) or 0,
            # Raw stats (position-specific; undefined ones stay 0)
            **_extract_stats(stats, position_group),
        }
        rows.append(row)

    return pd.DataFrame(rows).set_index("player_id")


def _composite_to_100(score: float | None) -> float:
    """247 composite is 0.7000–1.0000; scale to 0–100."""
    if not score:
        return 40.0
    return max(0.0, min(100.0, (score - 0.7) / 0.3 * 100))


def _extract_stats(stats: dict, pg: str) -> dict:
    """Extract position-relevant numeric stats from the JSONB blob."""
    s = {k: float(v) if v is not None else 0.0 for k, v in stats.items() if isinstance(v, (int, float))}
    games = max(s.get("games_played", 1), 1)

    if pg == "QB":
        att = max(s.get("passingAtt", 0), 1)
        return {
            "comp_pct":       s.get("passingComp", 0) / att,
            "yards_per_att":  s.get("passingYds", 0) / att,
            "td_int_ratio":   (s.get("passingTd", 0) + 1) / (s.get("passingInt", 0) + 1),
        }
    if pg == "RB":
        car = max(s.get("rushingCar", 0), 1)
        return {
            "yards_per_carry": s.get("rushingYds", 0) / car,
            "yards_per_game":  s.get("rushingYds", 0) / games,
            "rec_per_game":    s.get("receivingRec", 0) / games,
        }
    if pg in ("WR", "TE"):
        rec = max(s.get("receivingRec", 0), 1)
        return {
            "yards_per_rec":  s.get("receivingYds", 0) / rec,
            "catch_rate":     s.get("receivingRec", 0) / max(s.get("receivingRec", 1), 1),
            "rec_per_game":   s.get("receivingRec", 0) / games,
        }
    if pg == "OL":
        return {
            "team_rush_ypa":  s.get("team_rush_ypa", 0),
            "team_sack_rate": s.get("team_sack_rate", 0),
        }
    if pg in ("DL", "LB", "DB"):
        return {
            "tackles_per_game": s.get("defensiveTot", 0) / games,
            "sacks_per_game":   s.get("defensiveSacks", 0) / games,
            "tfl_per_game":     s.get("defensiveTfl", 0) / games,
            "ints_per_game":    s.get("defensiveInt", 0) / games,
            "pbu_per_game":     s.get("defensivePd", 0) / games,
        }
    if pg == "K":
        att = max(s.get("kickingFGA", 0), 1)
        return {
            "fg_pct":  s.get("kickingFGM", 0) / att,
            "fg_long": s.get("kickingLng", 0),
            "xp_pct":  s.get("kickingXPM", 0) / max(s.get("kickingXPA", 1), 1),
        }
    if pg == "P":
        punts = max(s.get("puntingPunts", 0), 1)
        return {
            "avg_yards":     s.get("puntingYds", 0) / punts,
            "inside_20_pct": s.get("puntingIn20", 0) / punts,
        }
    return {}


def is_starter_tier(row: pd.Series, position_group: str) -> bool:
    thresholds = STARTER_THRESHOLDS.get(position_group, {})
    if not thresholds:
        return True  # OL: always use fallback path
    stats = {}  # We only have extracted stats here, not raw
    # Use games_played + snap_pct as a proxy for starter tier
    return (row.get("games_played", 0) >= 6) and (row.get("snap_pct", 0) >= 0.10)


# ---------------------------------------------------------------------------
# Model training
# ---------------------------------------------------------------------------

FEATURE_COLS = {
    "QB":  ["comp_pct", "yards_per_att", "td_int_ratio", "ppa", "snap_pct", "recruit_composite", "transfer_flag"],
    "RB":  ["yards_per_carry", "yards_per_game", "rec_per_game", "ppa", "snap_pct", "recruit_composite", "transfer_flag"],
    "WR":  ["yards_per_rec", "catch_rate", "rec_per_game", "ppa", "snap_pct", "recruit_composite", "transfer_flag"],
    "TE":  ["yards_per_rec", "catch_rate", "rec_per_game", "ppa", "snap_pct", "recruit_composite", "transfer_flag"],
    "OL":  ["team_rush_ypa", "team_sack_rate", "award_tier", "recruit_composite"],
    "DL":  ["tackles_per_game", "sacks_per_game", "tfl_per_game", "ppa", "recruit_composite", "transfer_flag"],
    "LB":  ["tackles_per_game", "sacks_per_game", "tfl_per_game", "ints_per_game", "ppa", "recruit_composite"],
    "DB":  ["tackles_per_game", "ints_per_game", "pbu_per_game", "ppa", "recruit_composite", "transfer_flag"],
    "K":   ["fg_pct", "fg_long", "xp_pct"],
    "P":   ["avg_yards", "inside_20_pct"],
}

XGB_PARAMS = {
    "n_estimators": 200,
    "max_depth": 4,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "random_state": 42,
    "n_jobs": -1,
}


def build_target(df: pd.DataFrame, position_group: str) -> pd.Series:
    """Construct a pseudo-target as weighted combination of production metrics."""
    weights = {
        "QB":  {"ppa": 0.35, "yards_per_att": 0.25, "td_int_ratio": 0.25, "comp_pct": 0.15},
        "RB":  {"ppa": 0.40, "yards_per_carry": 0.35, "yards_per_game": 0.25},
        "WR":  {"ppa": 0.40, "yards_per_rec": 0.35, "rec_per_game": 0.25},
        "TE":  {"ppa": 0.40, "yards_per_rec": 0.35, "rec_per_game": 0.25},
        "OL":  {"team_rush_ypa": 0.50, "award_tier": 0.30, "recruit_composite": 0.20},
        "DL":  {"ppa": 0.35, "tfl_per_game": 0.35, "sacks_per_game": 0.30},
        "LB":  {"ppa": 0.30, "tackles_per_game": 0.30, "tfl_per_game": 0.25, "ints_per_game": 0.15},
        "DB":  {"ppa": 0.35, "ints_per_game": 0.35, "pbu_per_game": 0.30},
        "K":   {"fg_pct": 0.60, "fg_long": 0.25, "xp_pct": 0.15},
        "P":   {"avg_yards": 0.70, "inside_20_pct": 0.30},
    }
    w = weights.get(position_group, {})
    target = pd.Series(0.0, index=df.index)
    for col, weight in w.items():
        if col in df.columns:
            col_vals = df[col].fillna(0)
            col_min, col_max = col_vals.min(), col_vals.max()
            if col_max > col_min:
                target += weight * (col_vals - col_min) / (col_max - col_min)
    return target


def train_position_model(df: pd.DataFrame, position_group: str) -> tuple[xgb.XGBRegressor, list[str]]:
    feature_cols = [c for c in FEATURE_COLS.get(position_group, []) if c in df.columns]
    if not feature_cols:
        raise ValueError(f"No feature columns available for {position_group}")

    X = df[feature_cols].fillna(0)
    y = build_target(df, position_group)

    model = xgb.XGBRegressor(**XGB_PARAMS)
    model.fit(X, y)
    return model, feature_cols


def compute_shap_values(model: xgb.XGBRegressor, X: pd.DataFrame) -> list[dict]:
    explainer = shap.TreeExplainer(model)
    shap_matrix = explainer.shap_values(X)
    results = []
    for i, row in enumerate(shap_matrix):
        results.append({col: round(float(row[j]), 4) for j, col in enumerate(X.columns)})
    return results


def scale_to_100(scores: np.ndarray) -> np.ndarray:
    scaler = MinMaxScaler(feature_range=(30, 99))
    return scaler.fit_transform(scores.reshape(-1, 1)).flatten().round(2)


def fallback_rating(stars: int, team_avg: float = 65.0) -> float:
    """Stars-anchored fallback for backup/low-snap players (from v1 logic)."""
    offset = STARS_FALLBACK_OFFSET.get(stars, STARS_FALLBACK_OFFSET[0])
    return max(30.0, min(99.0, round(team_avg + offset, 2)))


# ---------------------------------------------------------------------------
# Trajectory and breakout
# ---------------------------------------------------------------------------

def compute_trajectory(current_ratings: dict, prev_season: int, current_season: int) -> dict:
    """Compare current ratings to previous season; return {player_id: trajectory_score}."""
    client = get_client()
    result = (
        client.table("ratings")
        .select("player_id, overall_rating")
        .eq("season", prev_season)
        .execute()
    )
    prev_map = {r["player_id"]: r["overall_rating"] for r in result.data or []}
    trajectory = {}
    for pid, curr_rating in current_ratings.items():
        prev_rating = prev_map.get(pid)
        if prev_rating is not None:
            trajectory[pid] = round(curr_rating - prev_rating, 2)
        else:
            trajectory[pid] = 0.0
    return trajectory


def compute_breakout_prob(df: pd.DataFrame, ratings: np.ndarray, position_group: str) -> np.ndarray:
    """Proxy breakout probability: young players with below-median rating but above-median recruit composite."""
    median_rating = np.median(ratings)
    probs = []
    for i, (pid, row) in enumerate(df.iterrows()):
        rating = ratings[i]
        rec = row.get("recruit_composite", 50)
        yr = row.get("year", 3)
        # Young (FR/SO), high recruit composite, below current median = breakout candidate
        is_young = yr in (1, 2)
        high_rec = rec > 70
        below_median = rating < median_rating
        if is_young and high_rec and below_median:
            prob = min(0.95, 0.40 + (rec - 70) / 100 + (median_rating - rating) / 200)
        elif is_young and high_rec:
            prob = 0.25
        elif high_rec:
            prob = 0.15
        else:
            prob = 0.05
        probs.append(round(prob, 4))
    return np.array(probs)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def rate_position(season: int, position_group: str) -> list[dict]:
    print(f"  {position_group}...")
    df = load_players_with_stats(season, position_group)
    if df.empty:
        print(f"    No data for {position_group} in {season}")
        return []

    # Split starter-tier vs backup
    starter_mask = df.apply(lambda r: is_starter_tier(r, position_group), axis=1)
    starter_df = df[starter_mask]
    backup_df = df[~starter_mask]

    ratings_map: dict = {}
    shap_map: dict = {}

    # ML ratings for starters
    if len(starter_df) >= 10:
        try:
            model, feature_cols = train_position_model(starter_df, position_group)
            X = starter_df[feature_cols].fillna(0)
            raw_scores = model.predict(X)
            scaled = scale_to_100(raw_scores)
            shap_vals = compute_shap_values(model, X)

            for i, pid in enumerate(starter_df.index):
                ratings_map[pid] = float(scaled[i])
                shap_map[pid] = shap_vals[i]

            # Save model artifact
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            model.save_model(str(OUTPUT_DIR / f"{position_group}_{season}.json"))

        except Exception as e:
            print(f"    Model training failed for {position_group}: {e}")
            # Fallback to formula-based for all
            for pid, row in df.iterrows():
                ratings_map[pid] = fallback_rating(int(row.get("stars", 0)))
    else:
        print(f"    Insufficient starters ({len(starter_df)}) for {position_group} — using fallback")
        for pid, row in starter_df.iterrows():
            ratings_map[pid] = fallback_rating(int(row.get("stars", 0)))

    # Fallback ratings for backups
    team_avg = np.mean(list(ratings_map.values())) if ratings_map else 65.0
    for pid, row in backup_df.iterrows():
        ratings_map[pid] = fallback_rating(int(row.get("stars", 0)), team_avg)

    # Trajectory
    trajectory = compute_trajectory(ratings_map, season - 1, season)

    # Breakout probability (starters only)
    if not starter_df.empty and starter_df.index.isin(ratings_map.keys()).any():
        rated_starter_df = starter_df[starter_df.index.isin(ratings_map.keys())]
        starter_ratings = np.array([ratings_map[pid] for pid in rated_starter_df.index])
        breakout_probs = compute_breakout_prob(rated_starter_df, starter_ratings, position_group)
        breakout_map = dict(zip(rated_starter_df.index, breakout_probs))
    else:
        breakout_map = {}

    # Assemble output rows
    rows = []
    for pid in df.index:
        rows.append({
            "player_id":           pid,
            "season":              season,
            "overall_rating":      ratings_map.get(pid, 50.0),
            "position_rating":     ratings_map.get(pid, 50.0),  # same until cross-position normalization
            "trajectory_score":    trajectory.get(pid, 0.0),
            "breakout_probability": breakout_map.get(pid, 0.05),
            "shap_values":         json.dumps(shap_map.get(pid, {})),
            "model_version":       MODEL_VERSION,
        })

    print(f"    Rated {len(rows)} players (ML: {len(starter_df)}, fallback: {len(backup_df)})")
    return rows


def main():
    parser = argparse.ArgumentParser(description="Train ratings model → Supabase")
    parser.add_argument("--season", type=int, default=2025)
    parser.add_argument("--position", type=str, help="Single position group (e.g. QB)")
    args = parser.parse_args()

    positions = [args.position.upper()] if args.position else list(FEATURE_COLS.keys())

    all_rows = []
    for pg in positions:
        rows = rate_position(args.season, pg)
        all_rows.extend(rows)

    if all_rows:
        bulk_upsert("ratings", all_rows, ["player_id", "season"])
        print(f"\nUpserted {len(all_rows)} rating rows for season {args.season}")

    print("Done.")


if __name__ == "__main__":
    main()
