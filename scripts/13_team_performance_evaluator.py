"""Team Performance Evaluator — scores teams by over/underperformance vs talent.

For each team-season, computes:
  - 3-year rolling avg recruiting composite (classes t-3 to t-1)
  - Normalized talent score (0-100), dynamically calibrated from full dataset
  - Expected SP+ from multiple regression on (talent, conference tier)
  - Performance residual = actual SP+ - expected SP+
  - Performance percentile vs all other team-seasons (tie-corrected)

Improvements vs v1:
  - Dynamic normalization bounds (1st–99th pctile of full dataset)
  - Conference tier covariate (P5 vs G5) in multiple regression
  - R² reported per record
  - Missing talent imputed with conference-season median (flagged)
  - Tied-rank percentile using mean method

Output: cfb-analytics-app/data/team_performance.json

Usage:
    python scripts/13_team_performance_evaluator.py
"""

import sys
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw, read_computed
from utils.json_utils import write_json

OUTPUT_PATH = (
    Path(__file__).parent.parent.parent
    / "cfb-analytics-app" / "data" / "team_performance.json"
)

# Power 4 + legacy Pac-12 treated as P5 for the covariate
P5_CONFERENCES = {"SEC", "Big Ten", "Big 12", "ACC", "Pac-12"}


def _normalize_composite(x: float, c_min: float, c_max: float) -> float:
    if c_max == c_min:
        return 50.0
    return round((x - c_min) / (c_max - c_min) * 100, 2)


def _percentile_of_score(scores: list, score: float) -> float:
    """Mean-method percentile — handles ties by averaging strict and weak ranks."""
    n = len(scores)
    n_less  = sum(1 for x in scores if x < score)
    n_equal = sum(1 for x in scores if x == score)
    pctile  = (n_less + (n_less + n_equal)) / 2 / n * 100
    return round(pctile, 1)


def main() -> None:
    print("Loading data...")
    recruiting   = read_raw("recruiting")
    team_ratings = read_computed("team_ratings")
    teams        = read_raw("teams")

    if team_ratings.empty:
        print("ERROR: data/computed/team_ratings.json is empty — run script 10 first")
        return

    # Team name / conference lookup
    team_info: dict = {}
    for _, t in teams.iterrows():
        tid = t.get("id")
        if tid is not None:
            team_info[int(tid)] = {
                "school":     t.get("school", "Unknown"),
                "conference": t.get("conference") or "",
            }

    # -------------------------------------------------------------------------
    # Build 3-year rolling recruiting composite per (team_id, season)
    # -------------------------------------------------------------------------
    print("Building recruiting composite by team-season...")
    rec_by_team_year: dict = defaultdict(list)
    all_composites: list[float] = []

    if not recruiting.empty:
        for _, r in recruiting.iterrows():
            tid = r.get("committed_team_id")
            yr  = r.get("recruit_year")
            cs  = r.get("composite_score")
            try:
                if tid is None or yr is None or not cs:
                    continue
                cs_f = float(cs)
                rec_by_team_year[(int(tid), int(yr))].append(cs_f)
                all_composites.append(cs_f)
            except (ValueError, TypeError):
                continue

    # Dynamic normalization bounds — 1st/99th pctile clips outliers
    c_min = float(np.percentile(all_composites, 1))  if all_composites else 0.43
    c_max = float(np.percentile(all_composites, 99)) if all_composites else 1.00
    print(f"  Composite bounds: [{c_min:.4f}, {c_max:.4f}] (dynamic 1st–99th pctile)")

    # -------------------------------------------------------------------------
    # Build output records
    # -------------------------------------------------------------------------
    print("Computing performance metrics...")
    records: list[dict] = []

    for _, tr in team_ratings.iterrows():
        tid = tr.get("team_id")
        szn = tr.get("season")
        sp  = tr.get("sp_overall")
        try:
            tid = int(tid); szn = int(szn); sp = float(sp)
        except (ValueError, TypeError):
            continue
        if not math.isfinite(sp):
            continue

        comps: list[float] = []
        for yr in range(szn - 3, szn):
            comps.extend(rec_by_team_year.get((tid, yr), []))

        talent_raw  = sum(comps) / len(comps) if comps else None
        talent_norm = _normalize_composite(talent_raw, c_min, c_max) if talent_raw is not None else None

        info  = team_info.get(tid, {})
        conf  = info.get("conference", "")
        is_p5 = conf in P5_CONFERENCES

        records.append({
            "team_id":                tid,
            "season":                 szn,
            "school":                 info.get("school", "Unknown"),
            "conference":             conf,
            "is_p5":                  is_p5,
            "talent_normalized":      talent_norm,
            "talent_imputed":         False,
            "sp_overall":             round(sp, 2),
            "sp_predicted":           None,
            "performance_residual":   None,
            "performance_percentile": None,
            "regression_r2":          None,
            "overall_rating":         round(float(tr.get("overall_rating") or 0), 2),
            "coaching_change_flag":   bool(tr.get("coaching_change", False)),
        })

    # -------------------------------------------------------------------------
    # Impute missing talent with conference-season median; fall back to season median
    # -------------------------------------------------------------------------
    print("Imputing missing talent scores...")
    conf_season_talent: dict = defaultdict(list)
    for r in records:
        if r["talent_normalized"] is not None:
            conf_season_talent[(r["conference"], r["season"])].append(r["talent_normalized"])

    imputed = 0
    for r in records:
        if r["talent_normalized"] is None:
            pool = conf_season_talent.get((r["conference"], r["season"]), [])
            if not pool:
                pool = [x["talent_normalized"] for x in records
                        if x["season"] == r["season"] and x["talent_normalized"] is not None]
            if pool:
                r["talent_normalized"] = round(float(np.median(pool)), 2)
                r["talent_imputed"]    = True
                imputed += 1

    print(f"  Imputed {imputed} team-seasons with conference/season median talent")

    # -------------------------------------------------------------------------
    # Multiple regression: [talent_normalized, is_p5] → sp_overall
    # -------------------------------------------------------------------------
    valid = [r for r in records if r["talent_normalized"] is not None]
    if len(valid) >= 10:
        X_raw = np.array(
            [[r["talent_normalized"], 1.0 if r["is_p5"] else 0.0, 1.0] for r in valid],
            dtype=float,
        )
        Y = np.array([r["sp_overall"] for r in valid], dtype=float)
        mask = ~(np.isnan(X_raw).any(axis=1) | np.isnan(Y) | np.isinf(Y))
        X_fit, Y_fit = X_raw[mask], Y[mask]

        coeffs, _, _, _ = np.linalg.lstsq(X_fit, Y_fit, rcond=None)
        slope_talent, slope_p5, intercept = coeffs

        y_pred_fit = X_fit @ coeffs
        ss_res = float(np.sum((Y_fit - y_pred_fit) ** 2))
        ss_tot = float(np.sum((Y_fit - Y_fit.mean()) ** 2))
        r2     = round(1.0 - ss_res / ss_tot, 4) if ss_tot > 0 else None

        print(
            f"  Regression: SP+ = {slope_talent:.4f}×talent + {slope_p5:.4f}×is_p5 + {intercept:.4f}  "
            f"(n={len(Y_fit)}, R²={r2:.3f})"
        )

        for r in records:
            if r["talent_normalized"] is not None:
                p5   = 1.0 if r["is_p5"] else 0.0
                pred = slope_talent * r["talent_normalized"] + slope_p5 * p5 + intercept
                r["sp_predicted"]         = round(float(pred), 2)
                r["performance_residual"] = round(r["sp_overall"] - r["sp_predicted"], 2)
                r["regression_r2"]        = r2

        residuals = [r["performance_residual"] for r in records if r["performance_residual"] is not None]
        for r in records:
            if r["performance_residual"] is not None:
                r["performance_percentile"] = _percentile_of_score(residuals, r["performance_residual"])
    else:
        print(f"  WARNING: only {len(valid)} rows with talent data — skipping regression")

    records.sort(key=lambda r: (-(r["season"] or 0), -(r["performance_residual"] or 0)))

    write_json(OUTPUT_PATH, records)
    print(f"Done. {len(records)} team-seasons written to team_performance.json")


if __name__ == "__main__":
    main()
