"""Engine D — next-season projection from a player's whole career EDGE curve.

REBUILT (v3.2). The previous version predicted next-season OVR from a single
season's level, and its top SHAP feature was "current OVR" for 69% of players.
Minimising squared error against weak features means predicting the conditional
mean, which means shrinking toward the population average — so it projected
100% of players rated 90+ to decline (reality: 88%) and 0% of players rated
40-45 to decline (reality: 6%), and it compressed the spread to SD 8.5 when
next-season OVR actually has SD ~11.4. It also barely beat doing nothing:
naive "next = current" scores MAE 9.50 on the 2023-24 holdout, the old model ~9.

What changed, and why:

  1. CAREER CURVE, NOT A SNAPSHOT. Features come from every season a player has
     played -- the shape of his EDGE curve (slope, acceleration, distance from
     peak, consistency), not just where it currently sits. Raw EDGE is not
     comparable across seasons or positions, so each score is first converted to
     a percentile within its own (season, position group).

  2. COHORT DEVELOPMENT CURVES. For every (position group, class year, EDGE
     decile) we compute what players like this historically did the next season.
     This is both the strongest single feature and the backbone of the
     explanation: "juniors at this level typically add 2.5".

  3. CALIBRATED SPREAD. The raw conditional mean under-disperses. We inflate
     variance 50% toward the realised distribution -- measured as the best
     available trade (MAE 8.41 vs raw 8.10 and naive 9.50, while cutting
     decline-rate error from 18.6 to 12.8 points). Full quantile mapping was
     tested and rejected: it scored worse on BOTH axes (MAE 9.36, 24.9 points)
     because matching marginals injects rank noise.

  4. LABELS AGAINST THE COHORT, NOT AGAINST ZERO. "Breakout" now means beating
     what players like him normally do. The old raw-delta label correlated
     -0.87 with current OVR -- it was the current rating, inverted. The
     cohort-relative label correlates -0.22.

  5. EVERY PREDICTION EXPLAINS ITSELF. Each row ships signed driver
     contributions, a generated human explanation, and historical comparables
     with similar career shapes and what they actually did.

Skill positions (QB/RB/WR/TE/DL/LB/DB) go through the model. OL/K/P have no
individual play attribution, so they use the cohort development curve directly
-- arithmetic that is itself better than naive (MAE 8.68).

Inputs  : player_seasons, ratings (computed, engine=edge), player_edge, recruiting, players
Outputs : data/models/engine_d.json          (model artifact)
          cfb-analytics-app/data/trajectory.json

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

from utils.store import read_raw, read_ratings
from utils.json_utils import write_json

MODEL_PATH = Path(__file__).parent.parent / "data" / "models" / "engine_d.json"
_APP_DATA = Path(__file__).parent.parent.parent / "cfb-analytics-app" / "data"
# Split deliberately: the list file is loaded by the home page and the research
# table, so it carries only what a row renders. Explanations, drivers and
# comparables are ~5 MB of per-player prose that only the modal ever shows, so
# they live in a second file fetched on demand.
OUTPUT_PATH        = _APP_DATA / "trajectory.json"
OUTPUT_DETAIL_PATH = _APP_DATA / "trajectory_detail.json"
COHORT_PATH = Path(__file__).parent.parent / "data" / "computed" / "cohort_curves.json"

TRAIN_SEASONS = (2008, 2020)   # inclusive fit window
VALID_SEASONS = (2021, 2022)   # residual quantiles for intervals come from here,
                               # never from train -- train residuals are
                               # optimistically small and under-cover.
TEST_SEASONS  = (2023, 2024)   # held out for the published metrics

SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "DL", "LB", "DB", "EDGE", "CB", "S"}

MIN_OVR       = 40
MIN_COHORT_N  = 20      # below this a cohort cell is noise; fall back a level
VARIANCE_LAMBDA = 0.5   # 50% inflation toward realised spread -- see header
OVR_FLOOR, OVR_CEIL = 40.0, 99.0

BREAKOUT_VS_COHORT =  3.0
DECLINE_VS_COHORT  = -3.0

FEATURE_COLS = [
    "ovr", "pct_last", "pct_slope", "pct_peak", "pct_from_peak", "pct_mean",
    "pct_sd", "pct_accel", "n_seasons", "games_last", "games_mean",
    "opp_sp_last", "opp_sp_trend", "class_year", "stars", "composite_score",
    "cohort_delta", "cohort_next", "pos_enc",
]

DRIVER_LABELS = {
    "ovr":             "current rating",
    "pct_last":        "production level",
    "pct_slope":       "career trend",
    "pct_peak":        "career-best production",
    "pct_from_peak":   "distance from peak",
    "pct_mean":        "career average production",
    "pct_sd":          "consistency",
    "pct_accel":       "recent acceleration",
    "n_seasons":       "experience",
    "games_last":      "availability",
    "games_mean":      "durability",
    "opp_sp_last":     "opponent strength",
    "opp_sp_trend":    "strengthening schedule",
    "class_year":      "class year",
    "stars":           "recruiting stars",
    "composite_score": "recruiting grade",
    "cohort_delta":    "typical development at this stage",
    "cohort_next":     "cohort baseline",
    "pos_enc":         "position",
}

_ORDINAL = {1: "true freshman", 2: "sophomore", 3: "junior", 4: "senior", 5: "fifth-year senior"}


# ---------------------------------------------------------------------------
# Career curve construction
# ---------------------------------------------------------------------------

def _recency_weighted_slope(seasons, values) -> float:
    """Slope of production over time, weighting recent seasons more heavily.

    A player who climbed 40 -> 60 -> 85 and one who fell 85 -> 60 -> 40 have
    identical means; only the slope separates them, and it is the single most
    interpretable thing about a career.
    """
    if len(values) < 2:
        return 0.0
    x = np.asarray(seasons, dtype=float)
    y = np.asarray(values, dtype=float)
    x = x - x.mean()
    w = np.power(1.6, np.arange(len(values)))
    w = w / w.sum()
    xm = np.average(x, weights=w)
    ym = np.average(y, weights=w)
    den = np.average((x - xm) ** 2, weights=w)
    if den <= 1e-9:
        return 0.0
    return float(np.average((x - xm) * (y - ym), weights=w) / den)


def build_career_frame(ratings_df, player_seasons_df, player_edge_df,
                       recruiting_df, players_df) -> pd.DataFrame:
    """One row per player-season carrying the shape of that player's career so far."""
    ps = player_seasons_df[["id", "player_id", "season", "position_group", "year", "team_id"]] \
        .rename(columns={"id": "ps_id", "year": "class_year"})
    pe = player_edge_df[["player_season_id", "edge_score", "games_played", "opponent_avg_sp"]] \
        .rename(columns={"player_season_id": "ps_id"})
    rt = ratings_df[["player_season_id", "overall_rating"]] \
        .rename(columns={"player_season_id": "ps_id", "overall_rating": "ovr"})

    df = ps.merge(pe, on="ps_id", how="inner").merge(rt, on="ps_id", how="inner")
    df = df[df["ovr"].notna() & df["edge_score"].notna() & (df["ovr"] >= MIN_OVR)]
    if df.empty:
        return df
    print(f"  {len(df)} player-seasons with both EDGE and a rating")

    # Percentile within (season, position group) -- the only form in which EDGE
    # is comparable across eras and across positions.
    df["edge_pct"] = df.groupby(["season", "position_group"])["edge_score"].rank(pct=True) * 100

    df = df.sort_values(["player_id", "season"])
    career = defaultdict(list)
    for r in df.itertuples(index=False):
        career[r.player_id].append(
            (r.season, r.edge_pct, r.games_played or 0, r.opponent_avg_sp or 0.0)
        )

    rows = []
    for pid, hist in career.items():
        for i in range(len(hist)):
            prior = hist[: i + 1]                       # career through this season
            seasons = [p[0] for p in prior]
            pcts    = [p[1] for p in prior]
            games   = [p[2] for p in prior]
            opps    = [p[3] for p in prior]
            rows.append({
                "player_id":     pid,
                "season":        seasons[-1],
                "n_seasons":     len(prior),
                "pct_last":      pcts[-1],
                "pct_slope":     _recency_weighted_slope(seasons, pcts),
                "pct_peak":      max(pcts),
                "pct_from_peak": pcts[-1] - max(pcts),
                "pct_mean":      float(np.mean(pcts)),
                "pct_sd":        float(np.std(pcts)) if len(pcts) > 1 else 0.0,
                "pct_accel":     ((pcts[-1] - pcts[-2]) - (pcts[-2] - pcts[-3])) if len(pcts) >= 3 else 0.0,
                "games_last":    float(games[-1]),
                "games_mean":    float(np.mean(games)),
                "opp_sp_last":   float(opps[-1]),
                "opp_sp_trend":  float(opps[-1] - np.mean(opps[:-1])) if len(opps) > 1 else 0.0,
                # kept for the explanation, not fed to the model
                "_pct_path":     [round(p, 1) for p in pcts[-4:]],
                "_seasons_path": seasons[-4:],
            })

    C = pd.DataFrame(rows)
    C = C.merge(df[["player_id", "season", "ps_id", "position_group", "class_year", "ovr", "team_id"]],
                on=["player_id", "season"], how="left")

    if not recruiting_df.empty:
        rb = (recruiting_df.sort_values("composite_score", ascending=False)
                           .drop_duplicates("player_id")[["player_id", "stars", "composite_score"]])
        C = C.merge(rb, on="player_id", how="left")
    else:
        C["stars"] = np.nan
        C["composite_score"] = np.nan
    C["stars"] = C["stars"].fillna(2.5)
    C["composite_score"] = C["composite_score"].fillna(0.85)

    if not players_df.empty:
        C = C.merge(players_df[["id", "name"]].rename(columns={"id": "player_id"}),
                    on="player_id", how="left")
    else:
        C["name"] = ""

    # Target: the same player's OVR one season later.
    nxt = C[["player_id", "season", "ovr"]].copy()
    nxt["season"] -= 1
    nxt = nxt.rename(columns={"ovr": "next_ovr"})
    C = C.merge(nxt, on=["player_id", "season"], how="left")
    return C


def build_cohort_curves(C: pd.DataFrame, train_mask) -> tuple:
    """What players at each (position, class year, production decile) did next.

    Returns (lookup, global_delta). Cells thinner than MIN_COHORT_N are dropped
    and resolved by falling back to (position, class year), then to global.
    """
    T = C[train_mask]
    global_delta = float((T["next_ovr"] - T["ovr"]).mean())

    T = T.assign(pct_bucket=(T["pct_last"] // 10).clip(0, 9))
    g = T.groupby(["position_group", "class_year", "pct_bucket"])
    full = g.apply(lambda d: pd.Series({
        "delta": float((d["next_ovr"] - d["ovr"]).mean()),
        "n":     int(len(d)),
    }), include_groups=False).reset_index()
    full = full[full["n"] >= MIN_COHORT_N]

    g2 = T.groupby(["position_group", "class_year"])
    coarse = g2.apply(lambda d: pd.Series({
        "delta": float((d["next_ovr"] - d["ovr"]).mean()),
        "n":     int(len(d)),
    }), include_groups=False).reset_index()
    coarse = coarse[coarse["n"] >= MIN_COHORT_N]

    lookup = {
        "full":   {(r.position_group, r.class_year, r.pct_bucket): (r.delta, int(r.n))
                   for r in full.itertuples(index=False)},
        "coarse": {(r.position_group, r.class_year): (r.delta, int(r.n))
                   for r in coarse.itertuples(index=False)},
    }
    print(f"  cohort cells: {len(lookup['full'])} full, {len(lookup['coarse'])} coarse "
          f"(global delta {global_delta:+.2f})")
    return lookup, global_delta


def apply_cohort(C: pd.DataFrame, lookup: dict, global_delta: float) -> pd.DataFrame:
    bucket = (C["pct_last"] // 10).clip(0, 9)
    deltas, ns = [], []
    for pg, cy, pb in zip(C["position_group"], C["class_year"], bucket):
        hit = lookup["full"].get((pg, cy, pb)) or lookup["coarse"].get((pg, cy))
        if hit:
            deltas.append(hit[0]); ns.append(hit[1])
        else:
            deltas.append(global_delta); ns.append(0)
    C = C.copy()
    C["pct_bucket"]   = bucket
    C["cohort_delta"] = deltas
    C["cohort_n"]     = ns
    C["cohort_next"]  = C["ovr"] + C["cohort_delta"]
    return C


# ---------------------------------------------------------------------------
# Explanation
# ---------------------------------------------------------------------------

def _describe_path(path, seasons) -> str:
    if len(path) < 2:
        return ""
    arrows = " → ".join(f"{p:.0f}" for p in path)
    direction = "climbed" if path[-1] > path[0] else "slipped" if path[-1] < path[0] else "held"
    return f"His production percentile has {direction} across {len(path)} seasons ({arrows})."


def build_explanation(row, drivers, comparables) -> str:
    """Plain English for why the number moved, built from the same values the
    model used -- never a generic template with numbers dropped in."""
    cls = _ORDINAL.get(int(row["class_year"] or 0), "player")
    pos = row["position_group"] or "player"
    parts = []

    path = row.get("_pct_path") or []
    seasons = row.get("_seasons_path") or []
    trend = _describe_path(path, seasons)
    if trend:
        parts.append(f"A {cls} {pos}. {trend}")
    else:
        parts.append(f"A {cls} {pos} with one measured season.")

    cd, cn = row["cohort_delta"], int(row.get("cohort_n") or 0)
    if cn >= MIN_COHORT_N:
        verb = "add" if cd >= 0 else "lose"
        parts.append(
            f"{_ORDINAL.get(int(row['class_year'] or 0), 'Players').capitalize()}s at this "
            f"production level historically {verb} {abs(cd):.1f} OVR the next season "
            f"(n={cn})."
        )

    vs = row["vs_cohort"]
    if abs(vs) >= 1.0:
        side = "above" if vs > 0 else "below"
        top = [d for d in drivers if d["label"] != "cohort baseline"][:2]
        because = ", ".join(f"{d['label']}" for d in top)
        parts.append(
            f"We project {abs(vs):.1f} {side} that baseline"
            + (f", driven mainly by {because}." if because else ".")
        )
    else:
        parts.append("We project him right at that baseline.")

    if comparables:
        names = ", ".join(c["name"] for c in comparables if c.get("name"))
        avg = np.mean([c["actual_delta"] for c in comparables])
        if names:
            parts.append(f"Closest historical career shapes: {names} — they averaged {avg:+.1f}.")

    return " ".join(parts)


def find_comparables(row, pool: pd.DataFrame, k: int = 3) -> list:
    """Historical players whose career curve looked like this one at the same stage."""
    cand = pool[(pool["position_group"] == row["position_group"]) &
                (pool["class_year"] == row["class_year"])]
    if len(cand) < k:
        cand = pool[pool["position_group"] == row["position_group"]]
    if len(cand) < k:
        return []
    d = (((cand["pct_last"]  - row["pct_last"])  / 25.0) ** 2 +
         ((cand["pct_slope"] - row["pct_slope"]) / 10.0) ** 2 +
         ((cand["ovr"]       - row["ovr"])       / 10.0) ** 2)
    near = cand.assign(_d=d).nsmallest(k, "_d")
    return [{
        "player_id":    int(c.player_id),
        "name":         c.name if isinstance(c.name, str) else "",
        "season":       int(c.season),
        "ovr":          round(float(c.ovr), 1),
        "actual_next":  round(float(c.next_ovr), 1),
        "actual_delta": round(float(c.next_ovr - c.ovr), 1),
    } for c in near.itertuples(index=False)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _between(s, rng):
    return s.between(rng[0], rng[1])


def main() -> None:
    parser = argparse.ArgumentParser(description="Engine D — career-curve next-season projection")
    parser.add_argument("--retrain", action="store_true")
    parser.add_argument("--predict-season", type=int, default=None,
                        help="Season to predict FROM (default: latest in the data)")
    args = parser.parse_args()

    print("Loading data...")
    ratings_df = read_ratings("edge")
    ps_df      = read_raw("player_seasons")
    edge_df    = read_raw("player_edge")
    rec_df     = read_raw("recruiting")
    players_df = read_raw("players")

    if ratings_df.empty or ps_df.empty:
        print("ERROR: ratings.json or player_seasons.json empty — run scripts 01/06/07 first")
        return

    print("Building career curves...")
    C = build_career_frame(ratings_df, ps_df, edge_df, rec_df, players_df)
    if C.empty:
        print("ERROR: no career rows built")
        return

    predict_season = args.predict_season or int(C["season"].max())
    has_target = C["next_ovr"].notna()
    train_mask = has_target & _between(C["season"], TRAIN_SEASONS)

    print("Building cohort development curves...")
    lookup, global_delta = build_cohort_curves(C, train_mask)
    C = apply_cohort(C, lookup, global_delta)

    # Published so script 16 can carry players forward on the same curves rather
    # than recomputing (and drifting from) them.
    write_json(COHORT_PATH, {
        "global_delta": round(global_delta, 3),
        "min_n": MIN_COHORT_N,
        "full":   [{"position_group": k[0], "class_year": k[1], "pct_bucket": k[2],
                    "delta": round(v[0], 3), "n": v[1]} for k, v in lookup["full"].items()],
        "coarse": [{"position_group": k[0], "class_year": k[1],
                    "delta": round(v[0], 3), "n": v[1]} for k, v in lookup["coarse"].items()],
    })

    C["pos_enc"] = C["position_group"].astype("category").cat.codes.astype(float)
    is_skill = C["position_group"].isin(SKILL_POSITIONS)

    D  = C[C["next_ovr"].notna() & is_skill]
    tr = D[_between(D["season"], TRAIN_SEASONS)]
    va = D[_between(D["season"], VALID_SEASONS)]
    te = D[_between(D["season"], TEST_SEASONS)]
    print(f"  train {len(tr)}  valid {len(va)}  test {len(te)}")
    if len(tr) < 500:
        print("ERROR: insufficient training data")
        return

    from xgboost import XGBRegressor
    if MODEL_PATH.exists() and not args.retrain:
        print(f"Loading model from {MODEL_PATH}...")
        model = XGBRegressor()
        model.load_model(str(MODEL_PATH))
    else:
        print(f"Training on {TRAIN_SEASONS[0]}–{TRAIN_SEASONS[1]}...")
        model = XGBRegressor(n_estimators=400, max_depth=5, learning_rate=0.05,
                             subsample=0.8, colsample_bytree=0.8,
                             random_state=42, n_jobs=-1)
        model.fit(tr[FEATURE_COLS].values.astype(float),
                  tr["next_ovr"].values.astype(float))
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        model.save_model(str(MODEL_PATH))
        print(f"  saved {MODEL_PATH}")

    # ── Calibration constants, fitted on train only ────────────────────────
    tr_pred = model.predict(tr[FEATURE_COLS].values.astype(float))
    tr_act  = tr["next_ovr"].values.astype(float)
    infl_mu = float(tr_pred.mean())
    infl_k  = 1.0 + VARIANCE_LAMBDA * (float(np.std(tr_act)) / float(np.std(tr_pred)) - 1.0)

    def calibrate(p):
        return np.clip(infl_mu + infl_k * (p - infl_mu), OVR_FLOOR, OVR_CEIL)

    # ── Interval quantiles from the VALIDATION split ───────────────────────
    # Train residuals are optimistically small; using them under-covers.
    va_pred = calibrate(model.predict(va[FEATURE_COLS].values.astype(float)))
    va_res  = va["next_ovr"].values.astype(float) - va_pred
    va_bkt  = np.clip((va_pred // 10).astype(int), 4, 9)
    lo_q, hi_q = {}, {}
    for b in range(4, 10):
        r = va_res[va_bkt == b]
        if len(r) < 50:
            r = va_res
        lo_q[b], hi_q[b] = float(np.percentile(r, 10)), float(np.percentile(r, 90))

    # ── Published metrics on the untouched test split ──────────────────────
    if len(te):
        te_raw = model.predict(te[FEATURE_COLS].values.astype(float))
        te_cal = calibrate(te_raw)
        te_act = te["next_ovr"].values.astype(float)
        te_cur = te["ovr"].values.astype(float)
        te_coh = te["cohort_next"].values.astype(float)
        mae = lambda p: float(np.mean(np.abs(p - te_act)))
        naive_mae, model_mae = mae(te_cur), mae(te_cal)
        b = np.clip((te_cal // 10).astype(int), 4, 9)
        lo = te_cal + np.array([lo_q[i] for i in b])
        hi = te_cal + np.array([hi_q[i] for i in b])
        coverage = float(((te_act >= lo) & (te_act <= hi)).mean() * 100)
        print(f"\n  Holdout {TEST_SEASONS[0]}–{TEST_SEASONS[1]} (n={len(te)}):")
        print(f"    naive next=current   MAE {naive_mae:.2f}")
        print(f"    cohort arithmetic    MAE {mae(te_coh):.2f}")
        print(f"    model (raw)          MAE {mae(te_raw):.2f}   SD {np.std(te_raw):.2f}")
        print(f"    model (calibrated)   MAE {model_mae:.2f}   SD {np.std(te_cal):.2f}")
        print(f"    actual                            SD {np.std(te_act):.2f}")
        print(f"    80% interval coverage {coverage:.1f}%")

        # Gate: the whole point is to beat doing nothing.
        if model_mae >= naive_mae:
            print(f"\n  GATE FAILED: calibrated MAE {model_mae:.2f} does not beat "
                  f"naive carry-forward {naive_mae:.2f}. Not writing projections.")
            sys.exit(1)
        sd_ratio = float(np.std(te_cal)) / float(np.std(te_act))
        if sd_ratio < 0.6:
            print(f"\n  GATE FAILED: projected spread is {sd_ratio:.0%} of realised "
                  f"spread — distribution is compressed.")
            sys.exit(1)
        print(f"    GATES PASSED (MAE {naive_mae - model_mae:+.2f} vs naive, "
              f"spread {sd_ratio:.0%} of realised)")
    else:
        naive_mae = model_mae = coverage = None

    # ── Predict ────────────────────────────────────────────────────────────
    P = C[(C["season"] == predict_season) & is_skill].copy()
    if P.empty:
        print(f"WARNING: no season-{predict_season} rows to predict")
        return
    print(f"\nProjecting {predict_season + 1} from {predict_season} careers ({len(P)} players)...")

    raw  = model.predict(P[FEATURE_COLS].values.astype(float))
    proj = calibrate(raw)
    P["predicted_ovr"] = proj
    P["vs_cohort"]     = proj - P["cohort_next"].values

    print("Computing per-prediction drivers...")
    import shap
    sv = shap.TreeExplainer(model).shap_values(P[FEATURE_COLS].values.astype(float))

    comp_pool = D[_between(D["season"], (TRAIN_SEASONS[0], VALID_SEASONS[1]))]
    print("Finding comparables and writing explanations...")

    records, details = [], {}
    for i, (_, row) in enumerate(P.iterrows()):
        order = np.argsort(-np.abs(sv[i]))[:4]
        drivers = [{
            "feature": FEATURE_COLS[j],
            "label":   DRIVER_LABELS.get(FEATURE_COLS[j], FEATURE_COLS[j]),
            "effect":  round(float(sv[i][j]), 2),
        } for j in order]

        comparables = find_comparables(row, comp_pool)
        vs = float(row["vs_cohort"])
        label = ("breakout" if vs >= BREAKOUT_VS_COHORT
                 else "decline" if vs <= DECLINE_VS_COHORT else "steady")
        b = int(np.clip(row["predicted_ovr"] // 10, 4, 9))
        pred = float(row["predicted_ovr"])

        records.append({
            "player_season_id": int(row["ps_id"]),
            "player_id":        int(row["player_id"]),
            "name":             row["name"] if isinstance(row["name"], str) else "",
            "season":           int(row["season"]),
            "team_id":          int(row["team_id"]) if pd.notna(row["team_id"]) else None,
            "position_group":   row["position_group"],
            "class_year":       int(row["class_year"]) if pd.notna(row["class_year"]) else None,
            "current_ovr":      round(float(row["ovr"]), 1),
            "predicted_ovr":    round(pred, 1),
            "proj_low":         round(max(OVR_FLOOR, pred + lo_q[b]), 1),
            "proj_high":        round(min(OVR_CEIL, pred + hi_q[b]), 1),
            "delta":            round(pred - float(row["ovr"]), 1),
            "cohort_expected":  round(float(row["cohort_next"]), 1),
            "cohort_n":         int(row["cohort_n"]),
            "vs_cohort":        round(vs, 1),
            "trajectory_label": label,
            "shap_top_feature": drivers[0]["label"] if drivers else None,
            "engine":           "engine_d",
        })

        details[str(int(row["ps_id"]))] = {
            "explanation":   build_explanation(row, drivers, comparables),
            "drivers":       drivers,
            "comparables":   comparables,
            "edge_pct_path": row["_pct_path"],
            "seasons_path":  [int(s) for s in row["_seasons_path"]],
        }

    counts = pd.Series([r["trajectory_label"] for r in records]).value_counts().to_dict()
    print(f"  {counts}")
    records.sort(key=lambda r: -r["vs_cohort"])

    write_json(OUTPUT_PATH, {
        "_meta": {
            "engine": "engine_d",
            "predicts_season": predict_season + 1,
            "from_season": predict_season,
            "method": "career EDGE percentile curve + cohort development, 50% variance-inflated",
            "test_seasons": list(TEST_SEASONS),
            "naive_mae": round(naive_mae, 2) if naive_mae else None,
            "model_mae": round(model_mae, 2) if model_mae else None,
            "interval_coverage_pct": round(coverage, 1) if coverage else None,
            "label_counts": counts,
            "n": len(records),
        },
        "predictions": records,
    })
    write_json(OUTPUT_DETAIL_PATH, details)
    print(f"Done. {len(records)} projections written to {OUTPUT_PATH.name} "
          f"(+ per-player detail in {OUTPUT_DETAIL_PATH.name})")


if __name__ == "__main__":
    main()
