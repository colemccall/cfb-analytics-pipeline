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
from utils.coaching import coach_tenures, head_coach_by_team_season
from utils.shrinkage import population_stats, shrink_mean

OUTPUT_PATH = (
    Path(__file__).parent.parent.parent
    / "cfb-analytics-app" / "data" / "team_performance.json"
)

COACHING_PATH = (
    Path(__file__).parent.parent.parent
    / "cfb-analytics-app" / "data" / "coaching_impact.json"
)

# A stint needs this many seasons on each side of a change before the before/after
# comparison says anything. Two is the minimum that is not a single season, and a
# single season of SP+ residual is mostly noise (SD 9.82).
MIN_STINT_SEASONS = 2

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
                # NaN is truthy, so `not cs` above lets it through. A single one
                # makes np.percentile return nan, which propagated all the way to
                # a TypeError formatting R² — 189 unrated recruits took the whole
                # script down.
                if cs_f != cs_f:
                    continue
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

    # -------------------------------------------------------------------------
    # Shrinkage — the residual leaderboard is 2,310 noisy numbers ranked
    # -------------------------------------------------------------------------
    resids = [r["performance_residual"] for r in records
              if r["performance_residual"] is not None]
    if resids:
        mu, sd = population_stats(resids)
        print(f"  Residual distribution: mean {mu:.2f}, SD {sd:.2f} over {len(resids)} team-seasons")
        # One team-season is one observation, so n=1 and the shrunk value is the
        # honest one: a single season tells you much less than the raw number
        # implies. Teams with several seasons get an aggregate below.
        for r in records:
            if r["performance_residual"] is None:
                continue
            sm = shrink_mean(r["performance_residual"], 1, mu, sd, sd)
            r["residual_shrunk"] = sm["value"]
            r["residual_low"]    = sm["low"]
            r["residual_high"]   = sm["high"]

    records.sort(key=lambda r: (-(r["season"] or 0), -(r["performance_residual"] or 0)))

    write_json(OUTPUT_PATH, records)
    print(f"Done. {len(records)} team-seasons written to team_performance.json")

    write_json(COACHING_PATH, coaching_event_study(records))


def coaching_event_study(records: list[dict]) -> list[dict]:
    """Does the performance residual move when the head coach does?

    This is the test the team-performance finding has needed since it shipped.
    The residual persists year over year (r = 0.607 at t+1, 0.280 at t+3), which
    the page presented as evidence of coaching. It is not: scheme, development
    pipelines, portal usage and systematic error in the talent proxy are all
    persistent by team too. Persistence says the residual is real. It says nothing
    about what causes it.

    A coaching change is the closest thing to a natural experiment available. If
    the residual steps at the change and the step travels with the coach to his
    next job, coaching is doing work. If it does not move, the residual is a
    property of the program.

    What this can support: a distribution of before/after steps across many
    changes, and a per-coach record. What it cannot: causation for any single
    hire. Programs fire coaches after bad seasons, so the "before" is selected
    for being low and mean reversion alone predicts an improvement — which is why
    the summary reports the *median* step and the share of changes that improved,
    rather than presenting a positive mean as proof that firing coaches works.
    """
    print("\nCoaching event study...")
    hc = head_coach_by_team_season()
    if not hc:
        print("  No coaches table — run scripts/09_harvest_supplemental.py "
              "--dataset coaches. Skipping.")
        return []

    resid = {(r["team_id"], r["season"]): r["performance_residual"]
             for r in records if r["performance_residual"] is not None}

    stints = [s for s in coach_tenures(hc) if s["seasons"] >= MIN_STINT_SEASONS]
    by_team: dict = defaultdict(list)
    for s in stints:
        by_team[s["team_id"]].append(s)
    for v in by_team.values():
        v.sort(key=lambda s: s["first_season"])

    def stint_mean(s: dict):
        vals = [resid[(s["team_id"], y)] for y in
                range(s["first_season"], s["last_season"] + 1)
                if (s["team_id"], y) in resid]
        return (float(np.mean(vals)), len(vals)) if vals else (None, 0)

    events: list[dict] = []
    for tid, seq in by_team.items():
        for prev, cur in zip(seq, seq[1:]):
            # Consecutive stints only. A gap means seasons we have no coach for,
            # and attributing the step across it would be inventing the transition.
            if cur["first_season"] != prev["last_season"] + 1:
                continue
            before, n_before = stint_mean(prev)
            after, n_after = stint_mean(cur)
            if before is None or after is None:
                continue
            if n_before < MIN_STINT_SEASONS or n_after < MIN_STINT_SEASONS:
                continue
            events.append({
                "team_id":        tid,
                "school":         next((r["school"] for r in records if r["team_id"] == tid), ""),
                "change_season":  cur["first_season"],
                "outgoing":       prev["coach_name"],
                "incoming":       cur["coach_name"],
                "residual_before": round(before, 2),
                "residual_after":  round(after, 2),
                "step":            round(after - before, 2),
                "seasons_before":  n_before,
                "seasons_after":   n_after,
            })

    if not events:
        print("  No coaching changes with enough seasons on both sides.")
        return []

    steps = np.array([e["step"] for e in events], dtype=float)
    print(f"  {len(events)} coaching changes with >= {MIN_STINT_SEASONS} rated seasons each side")
    print(f"    mean step   {steps.mean():+.2f} SP+ points")
    print(f"    median step {np.median(steps):+.2f}")
    print(f"    improved    {100*float((steps > 0).mean()):.1f}% of changes")
    print(f"    step SD     {steps.std(ddof=1):.2f}  (residual SD is 9.82 — "
          f"a step smaller than that is not a coaching effect)")

    # Does a coach carry his residual between jobs? This is the harder and more
    # interesting question, and the one persistence cannot answer at all.
    by_coach: dict = defaultdict(list)
    for s in stints:
        m, n = stint_mean(s)
        if m is not None and n >= MIN_STINT_SEASONS:
            by_coach[s["coach"]].append({"team_id": s["team_id"], "mean": m, "n": n,
                                         "first_season": s["first_season"],
                                         "coach_name": s["coach_name"]})
    movers = {c: v for c, v in by_coach.items() if len(v) >= 2}
    carry = None
    if len(movers) >= 10:
        first = [sorted(v, key=lambda x: x["first_season"])[0]["mean"] for v in movers.values()]
        later = [sorted(v, key=lambda x: x["first_season"])[1]["mean"] for v in movers.values()]
        carry = float(np.corrcoef(first, later)[0, 1])
        print(f"  {len(movers)} coaches with two rated stints: correlation between "
              f"their first job's residual and their second = {carry:+.3f}")
        print("    This is the number that separates 'good coach' from 'good program'.")

    events.sort(key=lambda e: -abs(e["step"]))
    return [{
        "_summary": True,
        "n_events": len(events),
        "mean_step": round(float(steps.mean()), 2),
        "median_step": round(float(np.median(steps)), 2),
        "pct_improved": round(100 * float((steps > 0).mean()), 1),
        "step_sd": round(float(steps.std(ddof=1)), 2),
        "residual_sd": 9.82,
        "n_coaches_with_two_stints": len(movers),
        "coach_carryover_r": round(carry, 3) if carry is not None else None,
        "min_stint_seasons": MIN_STINT_SEASONS,
    }] + events


if __name__ == "__main__":
    main()
