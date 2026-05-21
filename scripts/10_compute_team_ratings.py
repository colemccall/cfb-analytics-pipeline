"""Team Ratings - composite per-team quality score per season.

Blends four signals into a single 0-100 overall_rating, plus split
offense/defense ratings:

  1. SP+ (schedule-adjusted performance): sigmoid-scaled so SP+ ~0 -> ~50,
     +30 (elite) -> ~85, -30 (basement) -> ~15.
  2. Roster quality: average overall_rating of starter-tier players, split
     by side of ball. Requires script 06 to have run first.
  3. Recruiting: 5-year rolling average of composite_score for players who
     joined this team, scaled from the 0.80-1.00 range to 0-100.
  4. Stability anchor: 5% weight toward the national mean (50) so teams with
     no data don't collapse to 0.

Offense/defense ratings use the same weights but substitute the side-specific
SP+ and starter averages.

SP+ team matching: exact lowercase, then fuzzy (SequenceMatcher >= 0.80).
If no match is found, substitute the national mean SP+ for that season.

Coaching-change flag: TRUE if any HC/OC/DC row in coaching_changes has
start_season = the computed season.

Usage:
    python scripts/10_compute_team_ratings.py              # 2025
    python scripts/10_compute_team_ratings.py --season 2024
    python scripts/10_compute_team_ratings.py --all-seasons
"""

import argparse
import json
import sys
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.db import bulk_upsert, get_connection
from utils.api_client import load_api_key, fetch_sp_ratings_breakdown

MODEL_VERSION = "v1.0-team"

# Composite weights
W_SP        = 0.45
W_ROSTER    = 0.35
W_RECRUITING = 0.15
W_ANCHOR    = 0.05   # pulls toward 50

# Split weights (offense_rating / defense_rating)
W_SP_SPLIT      = 0.50
W_ROSTER_SPLIT  = 0.45
W_REC_SPLIT     = 0.05

# Recruiting composite score range to normalize (0-100)
REC_MIN = 0.80
REC_MAX = 1.00

# Fuzzy match threshold for SP+ team name lookup
FUZZY_THRESHOLD = 0.80

# Tier that counts as "starters"
STARTER_TIER = "starter"

# Position groups for each side
OFF_POSITIONS = {"QB", "RB", "WR", "TE", "OL"}
DEF_POSITIONS = {"EDGE", "DL", "LB", "CB", "S", "DB"}

# Recruiting window in years (rolling)
RECRUITING_WINDOW = 5


# ---------------------------------------------------------------------------
# SP+ helpers
# ---------------------------------------------------------------------------

def _fuzzy_match(name: str, candidates: list[str], threshold: float = FUZZY_THRESHOLD) -> str | None:
    """Return the best-matching candidate string, or None if below threshold."""
    best_score = 0.0
    best_match = None
    for candidate in candidates:
        score = SequenceMatcher(None, name, candidate).ratio()
        if score > best_score:
            best_score = score
            best_match = candidate
    if best_score >= threshold:
        return best_match
    return None


def build_sp_map(season: int, api_key: str) -> dict:
    """Return {team_db_id: {overall, offense, defense}} for a season.

    Exact match by lowercased school name first; fuzzy fallback at 0.80.
    Teams with no SP+ data are excluded - callers substitute the season mean.
    """
    sp_by_name = fetch_sp_ratings_breakdown(api_key, season)
    if not sp_by_name:
        print(f"  Warning: no SP+ data returned for {season}")
        return {}

    sp_keys = list(sp_by_name.keys())   # already lowercase

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, school FROM teams")
        teams = cur.fetchall()

    result = {}
    matched_exact = 0
    matched_fuzzy = 0
    unmatched = 0

    for db_id, school in teams:
        lower = school.lower()
        if lower in sp_by_name:
            result[db_id] = sp_by_name[lower]
            matched_exact += 1
        else:
            fuzzy = _fuzzy_match(lower, sp_keys)
            if fuzzy:
                result[db_id] = sp_by_name[fuzzy]
                matched_fuzzy += 1
            else:
                unmatched += 1

    print(f"  SP+ matched: {matched_exact} exact, {matched_fuzzy} fuzzy, {unmatched} unmatched")
    return result


def sp_scaled(raw: float | None, mean: float = 0.0) -> float:
    """Sigmoid-scale SP+ to 0-100. SP+~0 -> 50, +30 -> ~85, -30 -> ~15."""
    val = raw if raw is not None else mean
    return 50.0 + 35.0 * float(np.tanh(val / 25.0))


def sp_season_means(sp_map: dict) -> tuple[float, float, float]:
    """Return (mean_overall, mean_offense, mean_defense) across teams with SP+ data."""
    if not sp_map:
        return 0.0, 25.0, 25.0
    overs = [v["overall"] for v in sp_map.values()]
    offs  = [v["offense"] for v in sp_map.values()]
    defs  = [v["defense"] for v in sp_map.values()]
    return (
        float(np.mean(overs)) if overs else 0.0,
        float(np.mean(offs))  if offs  else 25.0,
        float(np.mean(defs))  if defs  else 25.0,
    )


# ---------------------------------------------------------------------------
# Roster quality from player ratings
# ---------------------------------------------------------------------------

def load_starter_ratings(season: int) -> dict:
    """Return {team_id: {offense_avg, defense_avg, overall_avg, starter_count}}
    using starter-tier player ratings for the season.
    """
    sql = """
        SELECT
            ps.team_id,
            ps.position_group,
            r.overall_rating
        FROM ratings r
        JOIN player_seasons ps ON ps.id = r.player_season_id
        WHERE r.season = %s
          AND r.tier = %s
          AND r.overall_rating IS NOT NULL
          AND ps.team_id IS NOT NULL
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (season, STARTER_TIER))
        rows = cur.fetchall()

    # Aggregate per team
    from collections import defaultdict
    off_ratings: dict  = defaultdict(list)
    def_ratings: dict  = defaultdict(list)

    for team_id, pos_group, rating in rows:
        if pos_group in OFF_POSITIONS:
            off_ratings[team_id].append(float(rating))
        elif pos_group in DEF_POSITIONS:
            def_ratings[team_id].append(float(rating))

    all_team_ids = set(off_ratings) | set(def_ratings)
    result = {}
    for tid in all_team_ids:
        off_avg = float(np.mean(off_ratings[tid])) if off_ratings[tid] else None
        def_avg = float(np.mean(def_ratings[tid])) if def_ratings[tid] else None
        parts = [x for x in [off_avg, def_avg] if x is not None]
        team_avg = float(np.mean(parts)) if parts else None
        total_count = len(off_ratings[tid]) + len(def_ratings[tid])
        result[tid] = {
            "offense_avg":    off_avg,
            "defense_avg":    def_avg,
            "overall_avg":    team_avg,
            "starter_count":  total_count,
        }
    return result


# ---------------------------------------------------------------------------
# Recruiting (5-year rolling)
# ---------------------------------------------------------------------------

def load_recruiting_scores(season: int) -> dict:
    """Return {team_id: recruiting_scaled (0-100)} for the 5-year window ending
    at `season`. Joins recruiting -> player_seasons to get team context.
    """
    start_year = season - RECRUITING_WINDOW + 1
    sql = """
        SELECT
            ps.team_id,
            r.composite_score
        FROM recruiting r
        JOIN player_seasons ps
            ON ps.player_id = r.player_id
           AND ps.season     = r.recruit_year
        WHERE r.recruit_year BETWEEN %s AND %s
          AND r.composite_score IS NOT NULL
          AND ps.team_id IS NOT NULL
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (start_year, season))
        rows = cur.fetchall()

    from collections import defaultdict
    scores: dict = defaultdict(list)
    for team_id, composite in rows:
        scores[team_id].append(float(composite))

    result = {}
    for tid, vals in scores.items():
        avg = float(np.mean(vals))
        scaled = max(0.0, min(100.0, (avg - REC_MIN) / (REC_MAX - REC_MIN) * 100.0))
        result[tid] = scaled
    return result


# ---------------------------------------------------------------------------
# Coaching-change flag
# ---------------------------------------------------------------------------

def load_coaching_changes(season: int) -> set:
    """Return set of team_ids that had a head/offensive/defensive coordinator
    change starting in `season`.
    """
    sql = """
        SELECT DISTINCT team_id
        FROM coaching_changes
        WHERE start_season = %s
          AND role IN ('HC', 'OC', 'DC')
          AND team_id IS NOT NULL
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (season,))
        return {row[0] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Load all teams
# ---------------------------------------------------------------------------

def load_teams() -> list[tuple]:
    """Return list of (id, school, conference) for all teams."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, school, conference FROM teams ORDER BY school")
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Composite formula
# ---------------------------------------------------------------------------

def _clip(val: float) -> float:
    return max(10.0, min(99.0, val))


def compute_team_rating(
    sp: dict | None,
    roster: dict | None,
    recruiting_scaled: float | None,
    sp_means: tuple[float, float, float],
) -> dict:
    """Compute overall/offense/defense ratings from component signals.

    All missing components fall back to the national mean (50 for scaled metrics).
    Returns dict with keys: overall_rating, offense_rating, defense_rating,
    sp_overall_scaled, sp_offense_scaled, sp_defense_scaled.
    """
    mean_ovr, mean_off, mean_def = sp_means

    sp_raw_ovr  = sp["overall"]  if sp else None
    sp_raw_off  = sp["offense"]  if sp else None
    sp_raw_def  = sp["defense"]  if sp else None

    sp_ovr_s  = sp_scaled(sp_raw_ovr,  mean_ovr)
    sp_off_s  = sp_scaled(sp_raw_off,  mean_off)
    sp_def_s  = sp_scaled(sp_raw_def,  mean_def)

    off_avg  = roster["offense_avg"] if roster else None
    def_avg  = roster["defense_avg"] if roster else None
    team_avg = roster["overall_avg"] if roster else None

    # Fallback missing roster to 50 (midpoint)
    off_avg_f  = off_avg  if off_avg  is not None else 50.0
    def_avg_f  = def_avg  if def_avg  is not None else 50.0
    team_avg_f = team_avg if team_avg is not None else 50.0

    rec_f = recruiting_scaled if recruiting_scaled is not None else 50.0

    overall = (
        W_SP         * sp_ovr_s
        + W_ROSTER   * team_avg_f
        + W_RECRUITING * rec_f
        + W_ANCHOR   * 50.0
    )
    offense = (
        W_SP_SPLIT     * sp_off_s
        + W_ROSTER_SPLIT * off_avg_f
        + W_REC_SPLIT  * rec_f
    )
    defense = (
        W_SP_SPLIT     * sp_def_s
        + W_ROSTER_SPLIT * def_avg_f
        + W_REC_SPLIT  * rec_f
    )

    return {
        "overall_rating":   round(_clip(overall), 2),
        "offense_rating":   round(_clip(offense), 2),
        "defense_rating":   round(_clip(defense), 2),
        "sp_overall_scaled": round(sp_ovr_s, 2),
        "sp_offense_scaled": round(sp_off_s, 2),
        "sp_defense_scaled": round(sp_def_s, 2),
    }


# ---------------------------------------------------------------------------
# Main computation for one season
# ---------------------------------------------------------------------------

def run_season(season: int, api_key: str) -> None:
    print(f"\n{'='*60}")
    print(f"Computing Team Ratings - Season {season}")
    print(f"{'='*60}")

    print("Loading teams...")
    teams = load_teams()
    print(f"  {len(teams)} teams")

    print("Loading SP+ ratings...")
    sp_map = build_sp_map(season, api_key)
    sp_means = sp_season_means(sp_map)
    print(f"  Season SP+ means: overall={sp_means[0]:.1f}, "
          f"offense={sp_means[1]:.1f}, defense={sp_means[2]:.1f}")

    print("Loading starter ratings from player ratings...")
    roster_map = load_starter_ratings(season)
    teams_with_ratings = len(roster_map)
    if teams_with_ratings == 0:
        print("  Warning: no starter ratings found. Run script 06 first. "
              "Roster quality will fall back to 50.")
    else:
        print(f"  {teams_with_ratings} teams have starter-tier player ratings")

    print(f"Loading recruiting scores ({season - RECRUITING_WINDOW + 1}-{season} window)...")
    recruiting_map = load_recruiting_scores(season)
    print(f"  {len(recruiting_map)} teams have recruiting data")

    print("Loading coaching changes...")
    coaching_change_teams = load_coaching_changes(season)
    print(f"  {len(coaching_change_teams)} teams with HC/OC/DC changes in {season}")

    # Build one row per team
    rows = []
    for team_id, school, conference in teams:
        sp      = sp_map.get(team_id)
        roster  = roster_map.get(team_id)
        rec_s   = recruiting_map.get(team_id)

        ratings = compute_team_rating(sp, roster, rec_s, sp_means)

        starter_count = roster["starter_count"] if roster else 0
        off_avg       = roster["offense_avg"]   if roster else None
        def_avg       = roster["defense_avg"]   if roster else None
        avg_starter   = roster["overall_avg"]   if roster else None

        # sub_ratings carries the intermediate inputs for auditability
        sub_ratings = {
            "sp_overall_scaled":  ratings["sp_overall_scaled"],
            "sp_offense_scaled":  ratings["sp_offense_scaled"],
            "sp_defense_scaled":  ratings["sp_defense_scaled"],
            "offense_roster_avg": round(off_avg, 2) if off_avg is not None else None,
            "defense_roster_avg": round(def_avg, 2) if def_avg is not None else None,
            "recruiting_scaled":  round(rec_s, 2)   if rec_s  is not None else None,
        }

        rows.append({
            "team_id":            team_id,
            "season":             season,
            "overall_rating":     ratings["overall_rating"],
            "offense_rating":     ratings["offense_rating"],
            "defense_rating":     ratings["defense_rating"],
            "sp_overall":         round(sp["overall"], 2) if sp else None,
            "sp_offense":         round(sp["offense"], 2) if sp else None,
            "sp_defense":         round(sp["defense"], 2) if sp else None,
            "recruiting_score":   round(rec_s, 2) if rec_s is not None else None,
            "avg_starter_rating": round(avg_starter, 2) if avg_starter is not None else None,
            "starter_count":      starter_count,
            "coaching_change":    team_id in coaching_change_teams,
            "sub_ratings":        json.dumps(sub_ratings),
        })

    # Dedup by (team_id, season) - should already be unique, but be safe
    seen: dict = {}
    for r in rows:
        key = (r["team_id"], r["season"])
        seen[key] = r
    rows = list(seen.values())

    print(f"\nUpserting {len(rows)} team rating rows...")
    bulk_upsert("team_ratings", rows, conflict_col=["team_id", "season"])
    print(f"  Done. {len(rows)} rows written.")

    # Print top-10 summary
    sorted_rows = sorted(rows, key=lambda r: r["overall_rating"] or 0, reverse=True)
    top10 = sorted_rows[:10]

    # Build school lookup
    school_map = {t[0]: t[1] for t in teams}

    print(f"\n{'='*52}")
    print(f"  Top 10 Teams - {season} Overall Rating")
    print(f"{'='*52}")
    print(f"  {'Rank':<5} {'Team':<28} {'Overall':>7} {'Off':>6} {'Def':>6}")
    print(f"  {'-'*4:<5} {'-'*27:<28} {'-'*7:>7} {'-'*5:>6} {'-'*5:>6}")
    for rank, row in enumerate(top10, 1):
        school = school_map.get(row["team_id"], f"team#{row['team_id']}")
        print(
            f"  {rank:<5} {school:<28} "
            f"{row['overall_rating']:>7.1f} "
            f"{row['offense_rating']:>6.1f} "
            f"{row['defense_rating']:>6.1f}"
        )
    print(f"{'-'*52}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute composite team ratings per season and upsert to team_ratings."
    )
    parser.add_argument("--season", type=int, default=2025,
                        help="Season to compute (default: 2025)")
    parser.add_argument("--all-seasons", action="store_true",
                        help="Compute for all seasons 2008-2025")
    args = parser.parse_args()

    api_key = load_api_key()

    seasons = list(range(2008, 2026)) if args.all_seasons else [args.season]
    for s in seasons:
        run_season(s, api_key)

    print("\nDone.")


if __name__ == "__main__":
    main()
