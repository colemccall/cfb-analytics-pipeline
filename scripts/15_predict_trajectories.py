"""Engine D — XGBoost player trajectory predictor.

Trains an XGBoost regressor on historical player seasons (2008–2022) to
predict next-season OVR. Produces predicted_ovr, trajectory_label
(breakout / steady / decline), and the top SHAP feature driving each
prediction.

Predictions are made FROM the most recent season we have data for, FOR the
season after it — so with data through 2025, players' 2025 rows predict their
2026 OVR. The source season is derived from the data rather than hardcoded
(override with --predict-season); a hardcoded constant silently predicts nothing
the moment it runs ahead of the harvest.

Only skill positions with EDGE scores are modeled (QB/RB/WR/TE/DL/LB/DB).
OL/K/P excluded — their ratings lack individual play attribution.

Data inputs (local JSON):
  player_seasons, ratings (computed), player_edge, recruiting, transfers, players

Outputs:
  data/models/engine_d.json               (trained XGBoost model artifact)
  cfb-analytics-app/data/trajectory.json  (predictions for the upcoming season)

Usage:
    python scripts/15_predict_trajectories.py
    python scripts/15_predict_trajectories.py --retrain
    python scripts/15_predict_trajectories.py --predict-season 2024
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw, read_computed, read_ratings
from utils.json_utils import write_json

MODEL_PATH = Path(__file__).parent.parent / "data" / "models" / "engine_d.json"
OUTPUT_PATH = (
    Path(__file__).parent.parent.parent
    / "cfb-analytics-app" / "data" / "trajectory.json"
)

TRAIN_SEASONS  = range(2008, 2023)   # 2008–2022 for training
TEST_SEASONS   = range(2023, 2025)   # 2023–2024 held out for evaluation
# Prediction source season defaults to the latest season present in the data —
# see --predict-season.

SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "DL", "LB", "DB"}
POS_ENC = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "DL": 4, "LB": 5, "DB": 6}

BREAKOUT_DELTA = 5    # delta >= 5 → breakout
DECLINE_DELTA  = -5   # delta <= -5 → decline
MIN_GAMES      = 5    # minimum games_played to include in training
MIN_OVR        = 40   # drop placeholder/zero ratings

FEATURE_COLS = [
    "current_ovr", "trajectory_score", "edge_score", "games_played",
    "opponent_avg_sp", "stars", "composite_score", "class_year",
    "era_bucket", "transfer_flag", "position_group_enc",
]

SHAP_LABELS = {
    "current_ovr":       "current OVR",
    "trajectory_score":  "trajectory trend",
    "edge_score":        "EDGE score",
    "games_played":      "games played",
    "opponent_avg_sp":   "opponent strength",
    "stars":             "recruiting stars",
    "composite_score":   "recruiting composite",
    "class_year":        "class year",
    "era_bucket":        "era",
    "transfer_flag":     "transfer",
    "position_group_enc": "position",
}


def _era_bucket(season: int) -> int:
    if season <= 2012: return 0
    if season <= 2017: return 1
    return 2


def _build_lookups(player_seasons_df, player_edge_df, recruiting_df, transfers_df, players_df):
    pid_name: dict = {}
    if not players_df.empty:
        for _, row in players_df.iterrows():
            pid = row.get("id")
            name = row.get("name")
            if pid is not None and name:
                try:
                    pid_name[int(pid)] = str(name)
                except (ValueError, TypeError):
                    pass

    psid_info: dict = {}
    if not player_seasons_df.empty:
        for _, row in player_seasons_df.iterrows():
            psid = row.get("id")
            if psid is None:
                continue
            try:
                psid_info[int(psid)] = {
                    "player_id":   int(row.get("player_id") or 0),
                    "season":      int(row.get("season") or 0),
                    "pos_group":   str(row.get("position_group") or ""),
                    "class_year":  float(row.get("year") or 0),
                }
            except (ValueError, TypeError):
                pass

    psid_edge: dict = {}
    if not player_edge_df.empty:
        for _, row in player_edge_df.iterrows():
            psid = row.get("player_season_id")
            if psid is None:
                continue
            try:
                psid_edge[int(psid)] = {
                    "edge_score":      float(row.get("edge_score") or 0),
                    "games_played":    int(row.get("games_played") or 0),
                    "opponent_avg_sp": float(row.get("opponent_avg_sp") or 0),
                }
            except (ValueError, TypeError):
                pass

    pid_recruiting: dict = {}
    if not recruiting_df.empty:
        for _, row in recruiting_df.iterrows():
            pid = row.get("player_id")
            if pid is None:
                continue
            try:
                pid_int = int(pid)
                cs = float(row.get("composite_score") or 0)
                st = float(row.get("stars") or 0)
                if cs > pid_recruiting.get(pid_int, {}).get("composite_score", 0):
                    pid_recruiting[pid_int] = {"stars": st, "composite_score": cs}
            except (ValueError, TypeError):
                pass

    transfer_keys: set = set()
    if not transfers_df.empty:
        for _, row in transfers_df.iterrows():
            pid = row.get("player_id")
            yr  = row.get("transfer_year")
            if pid is None or yr is None:
                continue
            try:
                transfer_keys.add((int(pid), int(yr)))
            except (ValueError, TypeError):
                pass

    return pid_name, psid_info, psid_edge, pid_recruiting, transfer_keys


def build_dataset(ratings_df, player_seasons_df, player_edge_df,
                  recruiting_df, transfers_df, players_df) -> pd.DataFrame:
    print("  Building lookup tables...")
    pid_name, psid_info, psid_edge, pid_recruiting, transfer_keys = _build_lookups(
        player_seasons_df, player_edge_df, recruiting_df, transfers_df, players_df
    )

    # pid+season → OVR (for next-season target)
    pid_season_ovr: dict = {}
    for _, row in ratings_df.iterrows():
        psid = row.get("player_season_id")
        ovr  = row.get("overall_rating")
        if psid is None or ovr is None:
            continue
        try:
            ps = psid_info.get(int(psid))
            if ps:
                pid_season_ovr[(ps["player_id"], ps["season"])] = float(ovr)
        except (ValueError, TypeError):
            pass

    print("  Building feature rows...")
    rows = []
    for _, row in ratings_df.iterrows():
        psid = row.get("player_season_id")
        ovr  = row.get("overall_rating")
        traj = row.get("trajectory_score")
        if psid is None or ovr is None:
            continue
        try:
            psid_int = int(psid)
            ovr_f    = float(ovr)
        except (ValueError, TypeError):
            continue

        if ovr_f < MIN_OVR:
            continue

        ps = psid_info.get(psid_int)
        if not ps:
            continue
        pos = ps["pos_group"]
        if pos not in SKILL_POSITIONS:
            continue

        pid    = ps["player_id"]
        season = ps["season"]

        edge  = psid_edge.get(psid_int, {})
        games = edge.get("games_played", 0)
        if games < MIN_GAMES:
            continue

        e_scr  = edge.get("edge_score", 0.0)
        opp_sp = edge.get("opponent_avg_sp", 0.0)

        rec = pid_recruiting.get(pid, {})
        st  = rec.get("stars", 2.5)
        cs  = rec.get("composite_score", 0.85)
        traj_f = float(traj) if traj is not None else 0.0

        rows.append({
            "player_season_id":   psid_int,
            "player_id":          pid,
            "season":             season,
            "name":               pid_name.get(pid, ""),
            "position_group":     pos,
            "current_ovr":        ovr_f,
            "trajectory_score":   traj_f,
            "edge_score":         e_scr,
            "games_played":       float(games),
            "opponent_avg_sp":    opp_sp,
            "stars":              st,
            "composite_score":    cs,
            "class_year":         ps["class_year"],
            "era_bucket":         float(_era_bucket(season)),
            "transfer_flag":      1.0 if (pid, season) in transfer_keys else 0.0,
            "position_group_enc": float(POS_ENC.get(pos, 0)),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    print(f"  {len(df)} player-season rows before next-season join")

    df["next_season_ovr"] = df.apply(
        lambda r: pid_season_ovr.get((r["player_id"], r["season"] + 1)),
        axis=1,
    )
    return df


def train_model(df_train: pd.DataFrame):
    from xgboost import XGBRegressor

    X = df_train[FEATURE_COLS].values.astype(float)
    y = df_train["next_season_ovr"].values.astype(float)
    print(f"  Training on {len(y)} rows...")
    model = XGBRegressor(
        n_estimators=300, max_depth=5, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, n_jobs=-1,
    )
    model.fit(X, y)
    return model


def evaluate_model(model, df_test: pd.DataFrame) -> float:
    X = df_test[FEATURE_COLS].values.astype(float)
    y = df_test["next_season_ovr"].values.astype(float)
    preds = model.predict(X)
    mae  = float(np.mean(np.abs(preds - y)))
    rmse = float(np.sqrt(np.mean((preds - y) ** 2)))
    print(f"  Test MAE={mae:.2f}  RMSE={rmse:.2f}  (n={len(y)})")
    return mae


def get_shap_top_features(model, X: np.ndarray) -> list[str]:
    import shap
    explainer  = shap.TreeExplainer(model)
    shap_vals  = explainer.shap_values(X)
    top_idxs   = np.argmax(np.abs(shap_vals), axis=1)
    return [SHAP_LABELS.get(FEATURE_COLS[i], FEATURE_COLS[i]) for i in top_idxs]


def main() -> None:
    parser = argparse.ArgumentParser(description="Engine D — XGBoost trajectory predictor")
    parser.add_argument("--retrain", action="store_true", help="Force retrain even if model exists")
    parser.add_argument("--predict-season", type=int, default=None,
                        help="Season to predict FROM (default: latest season in the data)")
    args = parser.parse_args()

    print("Loading data...")
    ratings_df        = read_ratings("edge")
    player_seasons_df = read_raw("player_seasons")
    player_edge_df    = read_raw("player_edge")
    recruiting_df     = read_raw("recruiting")
    transfers_df      = read_raw("transfers")
    players_df        = read_raw("players")

    if ratings_df.empty:
        print("ERROR: ratings.json empty — run script 06 first")
        return
    if player_seasons_df.empty:
        print("ERROR: player_seasons.json empty — run script 01 first")
        return

    print("Building dataset...")
    df = build_dataset(ratings_df, player_seasons_df, player_edge_df,
                       recruiting_df, transfers_df, players_df)

    if df.empty:
        print("ERROR: no rows built — check data files")
        return

    predict_season = args.predict_season or int(df["season"].max())

    df_has_target = df[df["next_season_ovr"].notna()].copy()
    df_train = df_has_target[df_has_target["season"].isin(TRAIN_SEASONS)]
    df_test  = df_has_target[df_has_target["season"].isin(TEST_SEASONS)]
    df_pred  = df[df["season"] == predict_season].copy()

    print(f"  Train: {len(df_train)}  |  Test: {len(df_test)}  |  "
          f"Predict(from {predict_season}): {len(df_pred)}")

    if len(df_train) < 100:
        print("ERROR: insufficient training data (< 100 rows)")
        return

    # Train or load existing model
    from xgboost import XGBRegressor
    model_exists = MODEL_PATH.exists() and not args.retrain

    if model_exists:
        print(f"Loading model from {MODEL_PATH}...")
        model = XGBRegressor()
        model.load_model(str(MODEL_PATH))
    else:
        print("Training model (2008–2022)...")
        model = train_model(df_train)
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        model.save_model(str(MODEL_PATH))
        print(f"  Saved to {MODEL_PATH}")

    if len(df_test) > 0:
        print("Evaluating on held-out test seasons (2023–2024)...")
        evaluate_model(model, df_test)

    if df_pred.empty:
        print(f"WARNING: no {predict_season} rows to predict - run "
              f"script 01 --season {predict_season} first")
        return

    print(f"Predicting {predict_season + 1} OVR from {predict_season} production...")
    X_pred = df_pred[FEATURE_COLS].values.astype(float)
    preds  = np.clip(model.predict(X_pred), 40, 99)

    print("Computing SHAP top features...")
    shap_features = get_shap_top_features(model, X_pred)

    records = []
    for i, (_, row) in enumerate(df_pred.iterrows()):
        curr  = float(row["current_ovr"])
        pred  = round(float(preds[i]), 1)
        delta = round(pred - curr, 1)
        label = "breakout" if delta >= BREAKOUT_DELTA else "decline" if delta <= DECLINE_DELTA else "steady"
        records.append({
            "player_season_id": int(row["player_season_id"]),
            "player_id":        int(row["player_id"]),
            "name":             row["name"],
            "season":           int(row["season"]),
            "position_group":   row["position_group"],
            "current_ovr":      round(curr, 1),
            "predicted_ovr":    pred,
            "delta":            delta,
            "trajectory_label": label,
            "shap_top_feature": shap_features[i],
            "engine":           "engine_d",
        })

    labels = [r["trajectory_label"] for r in records]
    print(f"  Breakout: {labels.count('breakout')}  Steady: {labels.count('steady')}  Decline: {labels.count('decline')}")

    # Sort breakout candidates first
    records.sort(key=lambda r: -r["delta"])

    write_json(OUTPUT_PATH, records)
    print(f"Done. {len(records)} predictions written to trajectory.json")


if __name__ == "__main__":
    main()
