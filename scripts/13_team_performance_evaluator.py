"""Team Performance Evaluator — scores teams by over/underperformance vs talent.

For each team-season, computes:
  - 3-year rolling avg recruiting composite (classes t-3 to t-1)
  - Normalized talent score (0-100)
  - Expected SP+ from linear regression on talent
  - Performance residual = actual SP+ - expected SP+
  - Performance percentile vs all other team-seasons

Output: cfb-analytics-app/data/team_performance.json

Usage:
    python scripts/13_team_performance_evaluator.py
"""

import sys
from collections import defaultdict
from pathlib import Path

import math

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw, read_computed
from utils.json_utils import write_json

OUTPUT_PATH = (
    Path(__file__).parent.parent.parent
    / "cfb-analytics-app" / "data" / "team_performance.json"
)

# Observed composite_score range across full dataset (0.4311–1.0000).
# Used for normalization; kept fixed so residuals are comparable across runs.
COMPOSITE_MIN = 0.4311
COMPOSITE_MAX = 1.0000


def _normalize_composite(x: float) -> float:
    return round((x - COMPOSITE_MIN) / (COMPOSITE_MAX - COMPOSITE_MIN) * 100, 2)


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
    # Uses recruiting classes from years [season-3, season-2, season-1]
    # -------------------------------------------------------------------------
    print("Building recruiting composite by team-season...")
    rec_by_team_year: dict = defaultdict(list)
    if not recruiting.empty:
        for _, r in recruiting.iterrows():
            tid = r.get("committed_team_id")
            yr  = r.get("recruit_year")
            cs  = r.get("composite_score")
            try:
                if tid is None or yr is None or not cs:
                    continue
                rec_by_team_year[(int(tid), int(yr))].append(float(cs))
            except (ValueError, TypeError):
                continue

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

        # 3-year rolling recruiting composite
        comps: list[float] = []
        for yr in range(szn - 3, szn):
            comps.extend(rec_by_team_year.get((tid, yr), []))

        talent_raw  = sum(comps) / len(comps) if comps else None
        talent_norm = _normalize_composite(talent_raw) if talent_raw is not None else None

        info = team_info.get(tid, {})
        records.append({
            "team_id":             tid,
            "season":              szn,
            "school":              info.get("school", "Unknown"),
            "conference":          info.get("conference", ""),
            "talent_normalized":   talent_norm,
            "sp_overall":          round(sp, 2),
            "sp_predicted":        None,   # filled below
            "performance_residual":   None,
            "performance_percentile": None,
            "overall_rating":      round(float(tr.get("overall_rating") or 0), 2),
            "coaching_change_flag": bool(tr.get("coaching_change", False)),
        })

    # -------------------------------------------------------------------------
    # Linear regression: talent_normalized → sp_overall
    # Only rows with non-null talent_normalized enter the regression.
    # -------------------------------------------------------------------------
    valid = [(r["talent_normalized"], r["sp_overall"]) for r in records if r["talent_normalized"] is not None]
    if len(valid) >= 10:
        X = np.array([v[0] for v in valid], dtype=float)
        Y = np.array([v[1] for v in valid], dtype=float)
        mask = ~(np.isnan(X) | np.isnan(Y) | np.isinf(X) | np.isinf(Y))
        X, Y = X[mask], Y[mask]
        slope, intercept = np.polyfit(X, Y, 1)
        print(f"  Regression: SP+ = {slope:.4f} × talent + {intercept:.4f}  (n={len(valid)})")

        for r in records:
            if r["talent_normalized"] is not None:
                pred = slope * r["talent_normalized"] + intercept
                r["sp_predicted"]         = round(float(pred), 2)
                r["performance_residual"] = round(r["sp_overall"] - r["sp_predicted"], 2)

        # Performance percentile vs all team-seasons with a residual
        residuals = [r["performance_residual"] for r in records if r["performance_residual"] is not None]
        n_res = len(residuals)
        for r in records:
            if r["performance_residual"] is not None:
                pctile = sum(1 for x in residuals if x < r["performance_residual"]) / n_res * 100
                r["performance_percentile"] = round(pctile, 1)
    else:
        print(f"  WARNING: only {len(valid)} rows with talent data — skipping regression")

    # Sort: season desc, residual desc (overachievers first)
    records.sort(key=lambda r: (-(r["season"] or 0), -(r["performance_residual"] or 0)))

    write_json(OUTPUT_PATH, records)
    print(f"Done. {len(records)} team-seasons written to team_performance.json")


if __name__ == "__main__":
    main()
