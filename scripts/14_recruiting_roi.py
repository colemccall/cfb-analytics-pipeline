"""Recruiting Class ROI — hit-rate analysis for each team's recruiting classes.

For each (team, recruit_year), computes what % of recruits became high-OVR
contributors (peak OVR >= HIT_OVR_THRESHOLD across all rated seasons).

Output: cfb-analytics-app/data/recruiting_roi.json

Usage:
    python scripts/14_recruiting_roi.py
"""

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw, read_computed
from utils.json_utils import write_json

OUTPUT_PATH = (
    Path(__file__).parent.parent.parent
    / "cfb-analytics-app" / "data" / "recruiting_roi.json"
)

HIT_OVR_THRESHOLD = 75   # minimum peak OVR to count as a "contributor"
BLUE_CHIP_STARS   = 4    # minimum stars to be a blue-chip recruit
MIN_CLASS_SIZE    = 5    # skip classes too small to be meaningful


def main() -> None:
    print("Loading data...")
    recruiting     = read_raw("recruiting")
    player_seasons = read_raw("player_seasons")
    ratings        = read_computed("ratings")
    teams          = read_raw("teams")

    if recruiting.empty:
        print("ERROR: data/raw/recruiting.json is empty")
        return
    if ratings.empty:
        print("ERROR: data/computed/ratings.json is empty — run script 06 first")
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
    # Peak OVR per player (max overall_rating across all rated seasons)
    # -------------------------------------------------------------------------
    print("Computing peak OVR per player...")
    peak_ovr: dict = defaultdict(float)   # player_id → peak OVR

    if not player_seasons.empty and "player_season_id" in ratings.columns:
        # Build player_season_id → player_id lookup
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
    # Aggregate per (committed_team_id, recruit_year)
    # -------------------------------------------------------------------------
    print("Aggregating by class...")
    # class_data: (team_id, recruit_year) → {recruits, rated, contributors, bc_total, bc_contributors, stars_sum, composite_sum}
    class_data: dict = defaultdict(lambda: {
        "n_recruits": 0, "n_rated": 0, "n_contributors": 0,
        "n_bluechip": 0, "n_bc_contributors": 0,
        "stars_sum": 0.0, "composite_sum": 0.0,
    })

    for _, r in recruiting.iterrows():
        tid  = r.get("committed_team_id")
        yr   = r.get("recruit_year")
        pid  = r.get("player_id")
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
                d["n_rated"] += 1
                if p_ovr >= HIT_OVR_THRESHOLD:
                    d["n_contributors"] += 1
                    if is_bc:
                        d["n_bc_contributors"] += 1

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

        hit_rate     = round(n_cont / n_rated * 100, 1) if n_rated > 0 else None
        bc_hit_rate  = round(n_bc_c / n_bc   * 100, 1) if n_bc > 0 else None
        avg_stars    = round(d["stars_sum"] / n_rec, 2) if n_rec > 0 else None
        avg_composite = round(d["composite_sum"] / n_rec, 4) if n_rec > 0 else None

        info = team_info.get(tid, {})
        records.append({
            "team_id":         tid,
            "recruit_year":    yr,
            "school":          info.get("school", "Unknown"),
            "conference":      info.get("conference", ""),
            "n_recruits":      n_rec,
            "n_rated":         n_rated,
            "n_contributors":  n_cont,
            "hit_rate_pct":    hit_rate,
            "n_bluechip":      n_bc,
            "n_bc_contributors": n_bc_c,
            "bc_hit_rate_pct": bc_hit_rate,
            "avg_stars":       avg_stars,
            "avg_composite":   avg_composite,
        })

    # Sort: recruit_year desc, hit_rate desc
    records.sort(key=lambda r: (-(r["recruit_year"] or 0), -(r["hit_rate_pct"] or 0)))

    write_json(OUTPUT_PATH, records)
    print(f"Done. {len(records)} class-seasons written to recruiting_roi.json")


if __name__ == "__main__":
    main()
