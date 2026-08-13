"""Recruiting Class ROI — what each class became, GIVEN what it was.

For each (team, recruit_year): what share of the class became high-OVR
contributors (peak OVR >= HIT_OVR_THRESHOLD across all rated seasons), and —
the part that actually answers the question the page asks — how that compares to
what recruits of that calibre normally become.

## The problem this version fixes

The page said hit rate separated programs that recruit well from programs that
develop well. Measured, it did not. Hit rate correlates **+0.266** with the
class's own recruiting composite (+0.328 on the all-recruits denominator);
the strongest third of classes hit 39.0% against 28.8% for the weakest. So the
metric was close to a restatement of the star ratings — a leaderboard of who
signs the best players, presented as a leaderboard of who develops them.

## What replaces it

Expected peak OVR is fitted per RECRUIT from his own composite score, and the
class is scored on the residual: did these specific players outrun what players
with their rankings normally do. A program that turns three-stars into 80s scores
well; one that turns five-stars into 80s does not. This is script 13's trick, one
level down.

Both numbers ship. The raw hit rate is what most people mean by the phrase and
removing it would be a different kind of dishonesty; the residual is the one the
UI leads with, and each carries an interval.

## Shrinkage

Classes are small — the median is around twenty recruits and the minimum here is
five — so one player moves a raw rate by several points. Every rate and residual
is shrunk toward the population (utils/shrinkage.py) and shipped with an 80%
interval, because ranking 2,000-odd noisy classes puts luck at the top of the
list by construction.

Output: cfb-analytics-app/data/recruiting_roi.json

Usage:
    python scripts/14_recruiting_roi.py
"""

import sys
import datetime
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw, read_computed, read_ratings
from utils.json_utils import write_json
from utils.shrinkage import population_stats, residualize, shrink_mean, shrink_rate

OUTPUT_PATH = (
    Path(__file__).parent.parent.parent
    / "cfb-analytics-app" / "data" / "recruiting_roi.json"
)

HIT_OVR_THRESHOLD    = 75
BLUE_CHIP_STARS      = 4
MIN_CLASS_SIZE       = 5
BC_MIN_SAMPLE        = 3    # bc_hit_rate_pct is unreliable below this
MATURING_THRESHOLD   = 3    # classes with < 3 development seasons flagged
CURRENT_YEAR         = datetime.date.today().year

# The prior is worth this many recruits. A five-man class lands close to the
# national rate; a forty-man class is mostly its own evidence. Deliberately
# generous — see the shrinkage note in the module docstring.
ROI_PRIOR_STRENGTH   = 25.0

# A recruit with no composite score still has stars. Values are the MEDIAN
# composite actually observed at each star level over all 64,906 graded recruits
# on disk (5★ 0.9914, 4★ 0.9196, 3★ 0.8450, 2★ 0.7667), so this is a measurement
# rather than a guess. In practice it covers only the 189 recruits of 65,095 with
# stars but no grade — the services grade almost everyone — but the fallback has
# to exist or those recruits silently score as the worst in the country.
STARS_TO_COMPOSITE   = {5: 0.9914, 4: 0.9196, 3: 0.8450, 2: 0.7667, 1: 0.7400, 0: 0.7300}


def main() -> None:
    print("Loading data...")
    recruiting     = read_raw("recruiting")
    player_seasons = read_raw("player_seasons")
    # EDGE only — engine_b ratings are derived FROM recruiting, so counting them
    # as "did this recruit pan out?" would make every blue-chip a hit by definition.
    ratings        = read_ratings("edge")
    teams          = read_raw("teams")

    if recruiting.empty:
        print("ERROR: data/raw/recruiting.json is empty")
        return
    if ratings.empty:
        print("ERROR: data/computed/ratings.json is empty — run script 06 first")
        return

    team_info: dict = {}
    for _, t in teams.iterrows():
        tid = t.get("id")
        if tid is not None:
            team_info[int(tid)] = {
                "school":     t.get("school", "Unknown"),
                "conference": t.get("conference") or "",
            }

    # -------------------------------------------------------------------------
    # Peak OVR per player (max overall_rating across all rated seasons)
    # -------------------------------------------------------------------------
    print("Computing peak OVR per player...")
    peak_ovr: dict = defaultdict(float)

    if not player_seasons.empty and "player_season_id" in ratings.columns:
        psid_to_pid: dict = {}
        for _, ps in player_seasons.iterrows():
            psid = ps.get("id")
            pid  = ps.get("player_id")
            if psid is not None and pid is not None:
                try:
                    psid_to_pid[int(psid)] = int(pid)
                except (ValueError, TypeError):
                    pass

        for _, r in ratings.iterrows():
            psid = r.get("player_season_id")
            ovr  = r.get("overall_rating")
            if psid is None or ovr is None:
                continue
            try:
                pid = psid_to_pid.get(int(psid))
                if pid is not None:
                    peak_ovr[pid] = max(peak_ovr[pid], float(ovr))
            except (ValueError, TypeError):
                continue

    print(f"  Peak OVR computed for {len(peak_ovr)} players")

    # -------------------------------------------------------------------------
    # The expectation model: what does a recruit of THIS calibre normally become?
    #
    # Fitted once over every graded recruit in the archive who was ever rated.
    # Two fits, because the two questions are different:
    #
    #   peak OVR   a continuous outcome — least squares on composite
    #   hit rate   a binary outcome — the observed hit rate within composite bands
    #
    # Banding rather than a logistic fit is deliberate. The relationship is not
    # monotone-linear in probability space, the bands are wide enough to be
    # stable (thousands of recruits each), and a table can be printed and argued
    # with, which a fitted coefficient cannot.
    # -------------------------------------------------------------------------
    print("Fitting the recruit-level expectation model...")

    def recruit_composite(row) -> float | None:
        c = row.get("composite_score")
        try:
            cf = float(c)
            if cf == cf and cf > 0:
                return cf
        except (ValueError, TypeError):
            pass
        try:
            return STARS_TO_COMPOSITE.get(int(float(row.get("stars") or 0)))
        except (ValueError, TypeError):
            return None

    fit_x, fit_y, fit_hit = [], [], []
    for _, r in recruiting.iterrows():
        pid = r.get("player_id")
        comp = recruit_composite(r)
        if pid is None or comp is None:
            continue
        try:
            p_ovr = peak_ovr.get(int(pid))
        except (ValueError, TypeError):
            continue
        if p_ovr is None:
            continue
        fit_x.append(comp)
        fit_y.append(float(p_ovr))
        fit_hit.append(1.0 if p_ovr >= HIT_OVR_THRESHOLD else 0.0)

    _resid, ovr_fit = residualize(fit_x, fit_y)
    print(f"  peak OVR = {ovr_fit['slope']:.2f} x composite + {ovr_fit['intercept']:.2f}  "
          f"(n={ovr_fit['n']}, R2={ovr_fit['r2']})")

    # Hit rate by composite band. Edges chosen at the star boundaries measured
    # above so a band means something a reader recognises.
    BANDS = [(0.0, 0.80), (0.80, 0.85), (0.85, 0.89), (0.89, 0.93),
             (0.93, 0.97), (0.97, 1.01)]

    def band_of(comp: float) -> int:
        for i, (lo, hi) in enumerate(BANDS):
            if lo <= comp < hi:
                return i
        return len(BANDS) - 1

    band_hit: dict[int, list] = defaultdict(lambda: [0.0, 0.0])
    for c, h in zip(fit_x, fit_hit):
        b = band_hit[band_of(c)]
        b[0] += h
        b[1] += 1
    band_rate = {i: (v[0] / v[1] if v[1] else 0.0) for i, v in band_hit.items()}
    overall_hit = sum(fit_hit) / len(fit_hit) if fit_hit else 0.0
    for i, (lo, hi) in enumerate(BANDS):
        n = band_hit.get(i, [0, 0])[1]
        if n:
            print(f"    composite {lo:.2f}-{hi:.2f}: n={int(n):6d}  "
                  f"hit rate {band_rate.get(i, 0.0)*100:5.1f}%")
    print(f"  population hit rate among rated recruits: {overall_hit*100:.1f}%")

    # -------------------------------------------------------------------------
    # Aggregate per (committed_team_id, recruit_year)
    # -------------------------------------------------------------------------
    print("Aggregating by class...")
    class_data: dict = defaultdict(lambda: {
        "n_recruits": 0, "n_rated": 0, "n_contributors": 0,
        "n_bluechip": 0, "n_bc_contributors": 0,
        "stars_sum": 0.0, "composite_sum": 0.0, "ovr_sum": 0.0,
        # The expectation side: what this exact set of recruits should have done.
        "exp_hits": 0.0, "exp_ovr_sum": 0.0, "ovr_resid_sum": 0.0,
    })

    for _, r in recruiting.iterrows():
        tid   = r.get("committed_team_id")
        yr    = r.get("recruit_year")
        pid   = r.get("player_id")
        stars = r.get("stars")
        comp  = r.get("composite_score")

        try:
            if tid is None or yr is None:
                continue
            tid = int(tid); yr = int(yr)
        except (ValueError, TypeError):
            continue

        key = (tid, yr)
        d   = class_data[key]
        d["n_recruits"] += 1

        if stars:
            try:
                d["stars_sum"]     += float(stars)
                d["composite_sum"] += float(comp or 0)
                is_bc = float(stars) >= BLUE_CHIP_STARS
                if is_bc:
                    d["n_bluechip"] += 1
            except (ValueError, TypeError):
                is_bc = False
        else:
            is_bc = False

        if pid is not None:
            try:
                pid_int = int(pid)
            except (ValueError, TypeError):
                continue
            p_ovr = peak_ovr.get(pid_int)
            if p_ovr is not None:
                d["n_rated"]  += 1
                d["ovr_sum"]  += p_ovr
                if p_ovr >= HIT_OVR_THRESHOLD:
                    d["n_contributors"] += 1
                    if is_bc:
                        d["n_bc_contributors"] += 1
                # Expectation is accumulated over the SAME denominator as the
                # outcome — rated recruits only. Charging a class for players who
                # never appear in a box score would measure attrition, which is a
                # different finding and one we cannot separate from transfers.
                rc = recruit_composite(r)
                if rc is not None:
                    d["exp_hits"] += band_rate.get(band_of(rc), overall_hit)
                    exp_ovr = ovr_fit["slope"] * rc + ovr_fit["intercept"]
                    d["exp_ovr_sum"]   += exp_ovr
                    d["ovr_resid_sum"] += p_ovr - exp_ovr

    # -------------------------------------------------------------------------
    # Build output records
    # -------------------------------------------------------------------------
    print("Building output records...")
    records: list[dict] = []

    for (tid, yr), d in class_data.items():
        if d["n_recruits"] < MIN_CLASS_SIZE:
            continue

        n_rec   = d["n_recruits"]
        n_rated = d["n_rated"]
        n_cont  = d["n_contributors"]
        n_bc    = d["n_bluechip"]
        n_bc_c  = d["n_bc_contributors"]
        dev_seasons = CURRENT_YEAR - yr

        hit_rate_rated = round(n_cont / n_rated * 100, 1) if n_rated > 0 else None
        hit_rate_class = round(n_cont / n_rec   * 100, 1)
        avg_peak_ovr   = round(d["ovr_sum"] / n_rated, 1) if n_rated > 0 else None
        # Only report bc_hit_rate when sample is meaningful
        bc_hit_rate    = round(n_bc_c / n_bc * 100, 1) if n_bc >= BC_MIN_SAMPLE else None
        avg_stars      = round(d["stars_sum"] / n_rec, 2) if n_rec > 0 else None
        avg_composite  = round(d["composite_sum"] / n_rec, 4) if n_rec > 0 else None

        # --- Development, as distinct from recruiting -------------------------
        # Expected hits for THIS set of recruits, from the band table. The
        # difference is the development claim; the raw rate above is not.
        exp_hits = d["exp_hits"]
        exp_hit_rate = (exp_hits / n_rated * 100) if n_rated > 0 else None
        hits_over_expected = round(n_cont - exp_hits, 2) if n_rated > 0 else None
        avg_exp_ovr = round(d["exp_ovr_sum"] / n_rated, 1) if n_rated > 0 else None
        ovr_over_expected = round(d["ovr_resid_sum"] / n_rated, 2) if n_rated > 0 else None

        info = team_info.get(tid, {})
        records.append({
            "team_id":                      tid,
            "recruit_year":                 yr,
            "school":                       info.get("school", "Unknown"),
            "conference":                   info.get("conference", ""),
            "n_recruits":                   n_rec,
            "n_rated":                      n_rated,
            "n_contributors":               n_cont,
            "hit_rate_pct":                 hit_rate_rated,    # n_contributors / n_rated
            "hit_rate_class_pct":           hit_rate_class,    # n_contributors / n_recruits
            "avg_peak_ovr":                 avg_peak_ovr,
            "n_bluechip":                   n_bc,
            "n_bc_contributors":            n_bc_c,
            "bc_hit_rate_pct":              bc_hit_rate,       # None when n_bluechip < 3
            "avg_stars":                    avg_stars,
            "avg_composite":                avg_composite,
            "expected_development_seasons": dev_seasons,
            "maturing":                     dev_seasons < MATURING_THRESHOLD,
            # --- what the class was expected to become ---
            "expected_hits":                round(exp_hits, 2) if n_rated > 0 else None,
            "expected_hit_rate_pct":        round(exp_hit_rate, 1) if exp_hit_rate is not None else None,
            "hits_over_expected":           hits_over_expected,
            "avg_expected_peak_ovr":        avg_exp_ovr,
            # THE development number: mean peak OVR above what recruits of this
            # calibre normally reach. Positive means the program got more out of
            # them than their rankings implied.
            "ovr_over_expected":            ovr_over_expected,
        })

    # -------------------------------------------------------------------------
    # Shrinkage — because ranking 2,000 small classes puts luck at the top
    # -------------------------------------------------------------------------
    print("Shrinking rates and residuals toward the population...")
    rated = [r for r in records if (r["n_rated"] or 0) > 0]
    pop_hit = (sum(r["n_contributors"] for r in rated) /
               max(sum(r["n_rated"] for r in rated), 1))
    resid_mu, resid_sd = population_stats(r["ovr_over_expected"] for r in rated)
    # Within-class spread of the per-recruit residual, which is what makes one
    # class's mean noisy. Approximated from the fit's own residual SD.
    _r, _f = residualize(fit_x, fit_y)
    obs_sd = (sum(v ** 2 for v in _r) / max(len(_r) - 1, 1)) ** 0.5 if _r else 0.0
    print(f"  population hit rate {pop_hit*100:.1f}%  |  residual SD across classes "
          f"{resid_sd:.2f}  ·  per-recruit residual SD {obs_sd:.2f}")

    for r in records:
        n_rated = r["n_rated"] or 0
        sr = shrink_rate(r["n_contributors"], n_rated, pop_hit, ROI_PRIOR_STRENGTH)
        r["hit_rate_shrunk_pct"] = round(sr["value"] * 100, 1) if sr["value"] is not None else None
        r["hit_rate_low_pct"]    = round(sr["low"] * 100, 1) if sr["low"] is not None else None
        r["hit_rate_high_pct"]   = round(sr["high"] * 100, 1) if sr["high"] is not None else None

        if r["ovr_over_expected"] is not None and n_rated > 0:
            sm = shrink_mean(r["ovr_over_expected"], n_rated, resid_mu, resid_sd, obs_sd)
            r["ovr_over_expected_shrunk"] = sm["value"]
            r["ovr_over_expected_low"]    = sm["low"]
            r["ovr_over_expected_high"]   = sm["high"]
            # How much of the shrunk number is this class's own evidence.
            r["evidence_weight"] = sm["weight"]
        else:
            r["ovr_over_expected_shrunk"] = None
            r["ovr_over_expected_low"] = None
            r["ovr_over_expected_high"] = None
            r["evidence_weight"] = 0.0

    # Sorted by the development number, not the raw rate — that is the change.
    records.sort(key=lambda r: (-(r["recruit_year"] or 0),
                                -(r["ovr_over_expected_shrunk"] or -99)))

    write_json(OUTPUT_PATH, records)
    print(f"Done. {len(records)} class-seasons written to recruiting_roi.json")

    # Did residualizing actually break the link to recruiting? That is the whole
    # point of the change, so it is measured here rather than assumed.
    import numpy as _np
    have = [r for r in records
            if r["avg_composite"] and r["hit_rate_pct"] is not None
            and r["ovr_over_expected_shrunk"] is not None
            and all(_np.isfinite([r["avg_composite"], r["hit_rate_pct"],
                                  r["ovr_over_expected_shrunk"]]))]
    if len(have) > 30:
        comp = _np.array([r["avg_composite"] for r in have])
        raw  = _np.array([r["hit_rate_pct"] for r in have])
        res  = _np.array([r["ovr_over_expected_shrunk"] for r in have])
        print(f"\n  corr(class recruiting, RAW hit rate)        = {_np.corrcoef(comp, raw)[0,1]:+.3f}")
        print(f"  corr(class recruiting, residual dev score)  = {_np.corrcoef(comp, res)[0,1]:+.3f}")
        print("  The second should be near zero. If it is not, the expectation "
              "model has not removed recruiting and the metric still measures it.")


if __name__ == "__main__":
    main()
