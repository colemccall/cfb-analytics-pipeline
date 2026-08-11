"""Team Ratings v2 — three-signal composite with position-weighted splits.

Blends three signals into five split ratings (pass_off, run_off, pass_def,
run_def, special_teams) stored in sub_ratings JSONB, plus overall/offense/defense:

  1. SP+ (45%) — schedule-adjusted; sigmoid-scaled so SP+~0 → 50, ±30 → ±35.
  2. Position-weighted player talent (30%) — top-N starter ratings per position,
     blended into pass/run/pass-def/run-def composites.
  3. Raw team stats + advanced metrics (25%) — net passing/rushing yards,
     3rd-down conversion rates, and havoc (TFL + 2×INTs + fumbles recovered).

Ratings use fixed absolute anchors (like EDGE player ratings) for cross-season
comparability. A team that rates 89 in 2019 genuinely compares to an 89 in 2025.

Usage:
    python scripts/10_compute_team_ratings.py              # 2025
    python scripts/10_compute_team_ratings.py --season 2024
    python scripts/10_compute_team_ratings.py --all-seasons
"""

import argparse
import json
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.store import read_raw, read_computed, write_computed
from utils.api_client import load_api_key, fetch_sp_ratings_breakdown, fetch_team_stats

MODEL_VERSION = "v2.0-team"

# ---------------------------------------------------------------------------
# Weight constants
# ---------------------------------------------------------------------------

W_SP        = 0.45
W_ROSTER    = 0.30
W_RAW       = 0.25

# Split weights within roster signal
W_QB   = 0.45;  W_WR_TE = 0.35;  W_OL = 0.20   # pass_off
W_RB   = 0.40;  W_OL_RUN = 0.40; W_QB_RUN = 0.10; W_WR_TE_RUN = 0.10  # run_off
W_DB   = 0.45;  W_LB_PDEF = 0.30; W_DL_PDEF = 0.25  # pass_def
W_DL   = 0.40;  W_LB_RDEF = 0.35; W_DB_RDEF = 0.25  # run_def

# Overall from splits
W_PASS_OFF = 0.25;  W_RUN_OFF = 0.25
W_PASS_DEF = 0.25;  W_RUN_DEF = 0.20;  W_SPEC = 0.05

# Offense/defense rollup
W_OFF_SPLIT = 0.50  # pass_off:run_off split
W_DEF_SPLIT = 0.55  # pass_def:run_def split (pass-heavy)

# Raw stat blend (raw × 0.60 + 3rd-down × 0.40, with havoc for defense)
W_RAW_BASE  = 0.60;  W_3RD = 0.40
W_RAW_D_BASE = 0.60; W_3RD_D = 0.20; W_HAVOC = 0.20

# Recruiting constants
REC_MIN = 0.80;  REC_MAX = 1.00
RECRUITING_WINDOW = 5

# Starter tier threshold
STARTER_TIER = "starter"

# Fuzzy match threshold for SP+ name lookup
FUZZY_THRESHOLD = 0.80

# Position group sets
OFF_POSITIONS = {"QB", "RB", "WR", "TE", "OL"}
DEF_POSITIONS = {"EDGE", "DL", "LB", "CB", "S", "DB"}

# ---------------------------------------------------------------------------
# SP+-anchored team rating curves (Madden/CFB-style spread).
#
# SP+ is a schedule-adjusted points-margin metric — the best single team signal.
# We map it directly to a 0-99 OVR with fixed anchors so ratings are cross-season
# comparable and have a proper spread (elite ~96-99, average ~72, cellar ~50).
#
# Calibrated from 2025 SP+ distribution:
#   sp_overall: min=-37 p25=-9 p50=+1.6 p90=+18 max=+32
#   sp_offense: 8..43, p50=27 (higher = better)
#   sp_defense: 8..43, p50=27 (LOWER = better — points allowed)
# ---------------------------------------------------------------------------

# Overall: sp_overall (centered near 0) → OVR
SP_OVERALL_ANCHORS = [
    (-37, 48), (-20, 58), (-10, 65), (-3, 70), (2, 73),
    (10, 80), (18, 87), (25, 93), (33, 99),
]
# Offense: sp_offense (higher = better)
SP_OFFENSE_ANCHORS = [
    (5, 48), (15, 60), (22, 68), (27, 73), (33, 82), (39, 90), (44, 97),
]
# Defense: sp_defense (LOWER = better) — note descending OVR as value rises
SP_DEFENSE_ANCHORS = [
    (7, 97), (14, 90), (20, 82), (27, 73), (33, 66), (39, 58), (44, 50),
]


def _interp_anchors(val: float, anchors: list) -> float:
    xs = [a[0] for a in anchors]
    ys = [a[1] for a in anchors]
    return float(np.interp(val, xs, ys))


def sp_overall_to_ovr(sp: float) -> float:
    return _interp_anchors(sp, SP_OVERALL_ANCHORS)


def sp_offense_to_ovr(sp: float) -> float:
    return _interp_anchors(sp, SP_OFFENSE_ANCHORS)


def sp_defense_to_ovr(sp: float) -> float:
    return _interp_anchors(sp, SP_DEFENSE_ANCHORS)


# Roster-talent fallback anchors — mean of a team's top-22 starter OVRs → team OVR.
# Used to blend with SP+ (and as the sole signal when SP+ is missing, e.g. FCS).
ROSTER_OVR_ANCHORS = [
    (50, 50), (60, 62), (66, 70), (72, 80), (78, 88), (84, 95), (90, 99),
]


def roster_to_ovr(mean_top: float) -> float:
    return _interp_anchors(mean_top, ROSTER_OVR_ANCHORS)


# Headline OVR blend: SP+ drives 75% (schedule-adjusted), roster talent 25%.
# Sum must equal 1.0. Increase W_ROSTER_BLEND to weight raw talent more heavily.
W_SP_BLEND = 0.75
W_ROSTER_BLEND = 0.25


def _clip(val: float) -> float:
    return max(10.0, min(99.0, val))


# ---------------------------------------------------------------------------
# SP+ helpers
# ---------------------------------------------------------------------------

def _fuzzy_match(name: str, candidates: list, threshold: float = FUZZY_THRESHOLD):
    best_score = 0.0
    best_match = None
    for c in candidates:
        s = SequenceMatcher(None, name, c).ratio()
        if s > best_score:
            best_score = s
            best_match = c
    return best_match if best_score >= threshold else None


def build_sp_map(season: int, api_key: str) -> dict:
    """Return {team_db_id: {overall, offense, defense}} for a season."""
    sp_by_name = fetch_sp_ratings_breakdown(api_key, season)
    if not sp_by_name:
        print(f"  Warning: no SP+ data returned for {season}")
        return {}

    sp_keys = list(sp_by_name.keys())

    teams_df = read_raw("teams")
    teams = list(zip(teams_df["id"], teams_df["school"])) if not teams_df.empty else []

    result = {}
    exact = fuzzy = unmatched = 0
    for db_id, school in teams:
        lower = school.lower()
        if lower in sp_by_name:
            result[db_id] = sp_by_name[lower]
            exact += 1
        else:
            m = _fuzzy_match(lower, sp_keys)
            if m:
                result[db_id] = sp_by_name[m]
                fuzzy += 1
            else:
                unmatched += 1

    print(f"  SP+ matched: {exact} exact, {fuzzy} fuzzy, {unmatched} unmatched")
    return result


def sp_scaled(raw, mean: float = 0.0) -> float:
    """Sigmoid-scale SP+ to 0-100. SP+~0→50, ±30→±35."""
    val = raw if raw is not None else mean
    return 50.0 + 35.0 * float(np.tanh(float(val) / 25.0))


def sp_season_means(sp_map: dict) -> tuple:
    """Return (mean_overall, mean_offense, mean_defense_raw) — defense is raw (higher=worse)."""
    if not sp_map:
        return 0.0, 25.0, 25.0
    overs = [v["overall"] for v in sp_map.values()]
    offs  = [v["offense"] for v in sp_map.values()]
    defs  = [v["defense"] for v in sp_map.values()]
    return (
        float(np.mean(overs)) if overs else 0.0,
        float(np.mean(offs))  if offs  else 25.0,
        float(np.mean(defs))  if defs  else 25.0,  # raw mean; caller negates for sigmoid
    )


# ---------------------------------------------------------------------------
# Position-weighted roster quality
# ---------------------------------------------------------------------------

def avg_top(ratings: list, n: int) -> float:
    """Average top-N ratings from list; return 50.0 if empty."""
    if not ratings:
        return 50.0
    return float(np.mean(sorted(ratings, reverse=True)[:n]))


def load_starter_ratings_by_position(season: int, engine: str = "edge") -> dict:
    """Return {team_id: {QB: [r1,r2,...], RB: [...], ...}} for starter-tier players.

    `engine` selects which ratings to read: "edge" for played seasons, "projected"
    for an upcoming one. Reading unfiltered would mix engines and double-count
    every player who has rows in more than one.
    """
    rat_df = read_computed("ratings")
    ps_df  = read_raw("player_seasons")[["id", "team_id", "position_group"]].rename(columns={"id": "ps_id"})

    if rat_df.empty or ps_df.empty:
        return {}

    rat_df = rat_df[
        (rat_df["season"] == season) &
        (rat_df["engine"] == engine) &
        (rat_df["overall_rating"] >= 55) &
        rat_df["overall_rating"].notna()
    ]
    merged = rat_df.merge(ps_df, left_on="player_season_id", right_on="ps_id", how="left")
    merged = merged[merged["team_id"].notna()]

    result: dict = defaultdict(lambda: defaultdict(list))
    for _, row in merged.iterrows():
        result[int(row["team_id"])][row["position_group"]].append(float(row["overall_rating"]))
    return result


def compute_roster_splits(by_pos: dict) -> dict:
    """Compute pass_off, run_off, pass_def, run_def, special_teams from position ratings."""
    def g(pos):
        return by_pos.get(pos, [])

    # Merge DB/CB/S into a DB bucket
    db_all = g("DB") + g("CB") + g("S")
    dl_all = g("DL") + g("EDGE")

    pass_off = (avg_top(g("QB"),   2) * W_QB
              + avg_top(g("WR") + g("TE"), 5) * W_WR_TE
              + avg_top(g("OL"),   5) * W_OL)

    run_off  = (avg_top(g("RB"),   3) * W_RB
              + avg_top(g("OL"),   5) * W_OL_RUN
              + avg_top(g("QB"),   2) * W_QB_RUN
              + avg_top(g("WR") + g("TE"), 5) * W_WR_TE_RUN)

    pass_def = (avg_top(db_all,    5) * W_DB
              + avg_top(g("LB"),   4) * W_LB_PDEF
              + avg_top(dl_all,    4) * W_DL_PDEF)

    run_def  = (avg_top(dl_all,    4) * W_DL
              + avg_top(g("LB"),   4) * W_LB_RDEF
              + avg_top(db_all,    5) * W_DB_RDEF)

    special  = avg_top(g("K") + g("P"), 2)

    return {
        "pass_off": pass_off,
        "run_off":  run_off,
        "pass_def": pass_def,
        "run_def":  run_def,
        "special":  special,
    }


# ---------------------------------------------------------------------------
# Team season stats — pivot the /stats/season long format into per-team dicts
# and persist display-ready per-game metrics for the frontend stats panel.
# ---------------------------------------------------------------------------

def build_team_stat_table(season: int, api_key: str) -> dict:
    """Pivot /stats/season long rows ({team, statName, statValue}) into
    {team_name_lower: {statName: float}}."""
    raw = fetch_team_stats(api_key, season)
    if not raw:
        return {}
    table: dict = defaultdict(dict)
    for row in raw:
        name = (row.get("team") or "").lower().strip()
        stat = row.get("statName")
        if not name or not stat:
            continue
        try:
            table[name][stat] = float(row.get("statValue") or 0)
        except (TypeError, ValueError):
            continue
    return dict(table)


def store_team_season_stats(season: int, api_key: str, teams: list) -> None:
    """Compute display-ready per-game team metrics and persist to
    data/computed/team_season_stats.json (one row per team_id × season)."""
    table = build_team_stat_table(season, api_key)
    if not table:
        print(f"  No team stats to store for {season}")
        return

    table_keys = list(table.keys())
    rows = []
    for team_id, school, _conf in teams:
        lower = school.lower()
        stats = table.get(lower) or table.get(_fuzzy_match(lower, table_keys) or "")
        if not stats:
            continue
        g = max(stats.get("games", 0) or 0, 1)

        def pg(key):  # per-game
            return round(stats.get(key, 0) / g, 1)

        def rate(num, den):
            d = stats.get(den, 0)
            return round(100.0 * stats.get(num, 0) / d, 1) if d else None

        rows.append({
            "team_id": team_id, "season": season, "games": int(g),
            # Offense per game
            "yards_pg":        pg("totalYards"),
            "pass_yards_pg":   pg("netPassingYards"),
            "rush_yards_pg":   pg("rushingYards"),
            "first_downs_pg":  pg("firstDowns"),
            "third_down_pct":  rate("thirdDownConversions", "thirdDowns"),
            "fourth_down_pct": rate("fourthDownConversions", "fourthDowns"),
            # Defense per game (opponent output allowed)
            "yards_allowed_pg":      pg("totalYardsOpponent"),
            "pass_allowed_pg":       pg("netPassingYardsOpponent"),
            "rush_allowed_pg":       pg("rushingYardsOpponent"),
            "third_down_def_pct":    rate("thirdDownConversionsOpponent", "thirdDownsOpponent"),
            # Disruption / takeaways
            "sacks_pg":      pg("sacks"),
            "tfl_pg":        pg("tacklesForLoss"),
            "takeaways":     int((stats.get("interceptions", 0) or 0) + (stats.get("fumblesRecovered", 0) or 0)),
            "giveaways":     int(stats.get("turnovers", 0) or 0),
            "turnover_margin": int(((stats.get("interceptions", 0) or 0) + (stats.get("fumblesRecovered", 0) or 0))
                                   - (stats.get("turnovers", 0) or 0)),
            "possession_pg_sec": int((stats.get("possessionTime", 0) or 0) / g),
        })

    if not rows:
        return

    new_df = pd.DataFrame(rows)
    existing = read_computed("team_season_stats")
    if not existing.empty:
        mask = ~(existing["team_id"].isin(new_df["team_id"]) & existing["season"].isin(new_df["season"]))
        combined = pd.concat([existing[mask], new_df], ignore_index=True)
    else:
        combined = new_df
    write_computed("team_season_stats", combined)


def build_perf_scores(season: int, api_key: str) -> dict:
    """Fetch team season stats and normalize 0-1 across FBS teams.

    Returns {team_name_lower: {pass_off, run_off, pass_def, run_def}} — all 0-1.
    """
    raw = fetch_team_stats(api_key, season)
    if not raw:
        print(f"  Warning: no team stats returned for {season}")
        return {}

    # Extract needed fields per team
    teams_data = {}
    for row in raw:
        name = (row.get("team") or "").lower().strip()
        if not name:
            continue
        teams_data[name] = {
            "net_pass_off":  row.get("netPassingYards") or 0,
            "rush_off":      row.get("rushingYards") or 0,
            "net_pass_def":  row.get("netPassingYardsAllowed") or row.get("netPassingYardsOpponent") or 0,
            "rush_def":      row.get("rushingYardsAllowed") or row.get("rushingYardsOpponent") or 0,
            "td3_att":       row.get("thirdDownConversions") or 0,
            "td3_total":     max(row.get("thirdDowns") or 1, 1),
            "td3d_att":      row.get("thirdDownConversionsAllowed") or row.get("thirdDownConversionsOpponent") or 0,
            "td3d_total":    max(row.get("thirdDownsOpponent") or row.get("thirdDownsAllowed") or 1, 1),
            "tfl":           row.get("tacklesForLoss") or 0,
            "ints":          row.get("interceptions") or 0,
            "fum_rec":       row.get("fumblesRecovered") or 0,
        }

    if not teams_data:
        return {}

    # Compute derived metrics
    for d in teams_data.values():
        d["3rd_off_rate"] = d["td3_att"] / d["td3_total"]
        d["3rd_def_rate"] = d["td3d_att"] / d["td3d_total"]
        d["havoc"] = d["tfl"] + d["ints"] * 2 + d["fum_rec"]

    def normalize(key: str, vals: dict, invert: bool = False) -> dict:
        """Normalize a metric 0-1 across all teams."""
        v_list = [d[key] for d in vals.values()]
        lo, hi = min(v_list), max(v_list)
        if hi == lo:
            return {n: 0.5 for n in vals}
        result = {}
        for n, d in vals.items():
            norm = (d[key] - lo) / (hi - lo)
            result[n] = 1.0 - norm if invert else norm
        return result

    n_pass_off  = normalize("net_pass_off",  teams_data)
    n_rush_off  = normalize("rush_off",      teams_data)
    n_pass_def  = normalize("net_pass_def",  teams_data, invert=True)
    n_rush_def  = normalize("rush_def",      teams_data, invert=True)
    n_3rd_off   = normalize("3rd_off_rate",  teams_data)
    n_3rd_def   = normalize("3rd_def_rate",  teams_data, invert=True)
    n_havoc     = normalize("havoc",         teams_data)

    result = {}
    for name in teams_data:
        raw_pass_off_adv = n_pass_off[name] * W_RAW_BASE + n_3rd_off[name] * W_3RD
        raw_run_off_adv  = n_rush_off[name] * W_RAW_BASE + n_3rd_off[name] * W_3RD
        raw_pass_def_adv = n_pass_def[name] * W_RAW_D_BASE + n_3rd_def[name] * W_3RD_D + n_havoc[name] * W_HAVOC
        raw_run_def_adv  = n_rush_def[name] * W_RAW_D_BASE + n_3rd_def[name] * W_3RD_D + n_havoc[name] * W_HAVOC
        result[name] = {
            "pass_off": raw_pass_off_adv * 100.0,
            "run_off":  raw_run_off_adv  * 100.0,
            "pass_def": raw_pass_def_adv * 100.0,
            "run_def":  raw_run_def_adv  * 100.0,
        }
    return result


# ---------------------------------------------------------------------------
# Match raw stats to DB team ids
# ---------------------------------------------------------------------------

def match_raw_to_teams(perf_scores: dict, teams: list) -> dict:
    """Return {team_db_id: {pass_off, run_off, pass_def, run_def}} using fuzzy name match."""
    perf_keys = list(perf_scores.keys())
    result = {}
    for db_id, school, _ in teams:
        lower = school.lower()
        if lower in perf_scores:
            result[db_id] = perf_scores[lower]
        else:
            m = _fuzzy_match(lower, perf_keys)
            if m:
                result[db_id] = perf_scores[m]
    return result


# ---------------------------------------------------------------------------
# Recruiting (5-year rolling)
# ---------------------------------------------------------------------------

def load_recruiting_scores(season: int) -> dict:
    """Return {team_id: recruiting_scaled (0-100)}."""
    start_year = season - RECRUITING_WINDOW + 1
    rec_df = read_raw("recruiting")
    ps_df  = read_raw("player_seasons")[["player_id", "team_id", "season"]].rename(columns={"season": "ps_season"})

    if rec_df.empty or ps_df.empty:
        return {}

    rec_df = rec_df[
        rec_df["recruit_year"].between(start_year, season) &
        rec_df["composite_score"].notna()
    ]
    merged = rec_df.merge(
        ps_df,
        left_on=["player_id", "recruit_year"],
        right_on=["player_id", "ps_season"],
        how="inner"
    )
    merged = merged[merged["team_id"].notna()]

    scores: dict = defaultdict(list)
    for _, row in merged.iterrows():
        scores[int(row["team_id"])].append(float(row["composite_score"]))

    return {
        tid: max(0.0, min(100.0, (float(np.mean(vals)) - REC_MIN) / (REC_MAX - REC_MIN) * 100.0))
        for tid, vals in scores.items()
    }


# ---------------------------------------------------------------------------
# Coaching-change flag
# ---------------------------------------------------------------------------

def load_coaching_changes(season: int) -> set:
    df = read_raw("coaching_changes")
    if df.empty:
        return set()
    df = df[
        (df["start_season"] == season) &
        (df["role"].isin(["HC", "OC", "DC"])) &
        df["team_id"].notna()
    ]
    return set(df["team_id"].astype(int))


# ---------------------------------------------------------------------------
# Load all teams
# ---------------------------------------------------------------------------

def load_teams() -> list:
    df = read_raw("teams")
    if df.empty:
        return []
    return list(zip(df["id"], df["school"], df["conference"]))


# ---------------------------------------------------------------------------
# Three-signal blend → five splits → OVR
# ---------------------------------------------------------------------------

def compute_team_splits(
    team_id: int,
    sp: dict | None,
    roster_by_pos: dict,
    raw: dict | None,
    sp_means: tuple,
    recruiting_scaledcaled: float | None,
) -> dict:
    """SP+-anchored overall/offense/defense (0-99), blended with roster talent.

    sub_ratings (pass_off/run_off/pass_def/run_def) come from position-weighted
    roster splits — these drive the detail bars, not the headline OVR.
    """
    # --- Headline ratings: SP+ anchored, blended with roster talent ---
    # Roster talent = mean of the team's top-22 starter OVRs across all positions.
    all_starter_ovrs = [r for ratings in roster_by_pos.values() for r in ratings]
    mean_top22 = (float(np.mean(sorted(all_starter_ovrs, reverse=True)[:22]))
                  if all_starter_ovrs else None)
    roster_ovr = roster_to_ovr(mean_top22) if mean_top22 is not None else None

    if sp is not None:
        sp_ovr = sp_overall_to_ovr(sp["overall"])
        off    = sp_offense_to_ovr(sp["offense"])
        def_rating   = sp_defense_to_ovr(sp["defense"])
        if roster_ovr is not None:
            overall = sp_ovr * W_SP_BLEND + roster_ovr * W_ROSTER_BLEND
        else:
            overall = sp_ovr
    elif roster_ovr is not None:
        # No SP+ (e.g. FCS / unmatched) — fall back to roster talent only.
        overall = roster_ovr
        off = def_rating = roster_ovr
    else:
        overall = off = def_rating = 50.0

    # --- Detail splits: position-weighted roster quality (drives the bars) ---
    ros = compute_roster_splits(roster_by_pos)
    # Blend roster splits toward the SP+ offense/defense headline so bars track OVR.
    pass_off = ros["pass_off"] * 0.6 + off  * 0.4
    run_off  = ros["run_off"]  * 0.6 + off  * 0.4
    pass_def = ros["pass_def"] * 0.6 + def_rating * 0.4
    run_def  = ros["run_def"]  * 0.6 + def_rating * 0.4
    special  = ros["special"]

    def r2(v): return round(float(v), 2)

    return {
        "pass_off":       r2(_clip(pass_off)),
        "run_off":        r2(_clip(run_off)),
        "pass_def":       r2(_clip(pass_def)),
        "run_def":        r2(_clip(run_def)),
        "special_teams":  r2(_clip(special)),
        "overall_rating": r2(_clip(overall)),
        "offense_rating": r2(_clip(off)),
        "defense_rating": r2(_clip(def_rating)),
        "composite":      r2(overall),
    }


# ---------------------------------------------------------------------------
# Main per-season computation
# ---------------------------------------------------------------------------

def run_season(season: int, api_key: str) -> None:
    print(f"\n{'='*60}")
    print(f"Computing Team Ratings v2 - Season {season}")
    print(f"{'='*60}")

    teams = load_teams()
    print(f"  {len(teams)} teams")

    print("Loading SP+ ratings...")
    sp_map = build_sp_map(season, api_key)
    sp_means = sp_season_means(sp_map)
    print(f"  Season SP+ means: overall={sp_means[0]:.1f}, "
          f"offense={sp_means[1]:.1f}, defense={sp_means[2]:.1f}")

    print("Loading position-weighted roster ratings...")
    roster_map = load_starter_ratings_by_position(season)
    print(f"  {len(roster_map)} teams have starter-tier player ratings")

    print("Loading raw team stats...")
    perf_raw = build_perf_scores(season, api_key)
    raw_map = match_raw_to_teams(perf_raw, teams)
    print(f"  {len(raw_map)} teams have raw stat data")

    print("Storing display team stats...")
    store_team_season_stats(season, api_key, teams)

    print(f"Loading recruiting ({season - RECRUITING_WINDOW + 1}–{season})...")
    recruiting_map = load_recruiting_scores(season)
    print(f"  {len(recruiting_map)} teams have recruiting data")

    print("Loading coaching changes...")
    coaching_change_teams = load_coaching_changes(season)
    print(f"  {len(coaching_change_teams)} teams with HC/OC/DC changes")

    rows = []
    school_map = {t[0]: t[1] for t in teams}

    for team_id, school, conference in teams:
        sp      = sp_map.get(team_id)
        by_pos  = dict(roster_map.get(team_id, {}))
        raw     = raw_map.get(team_id)
        recruiting_scaled   = recruiting_map.get(team_id)

        splits = compute_team_splits(team_id, sp, by_pos, raw, sp_means, recruiting_scaled)

        sub_ratings = {
            "pass_off":           splits["pass_off"],
            "run_off":            splits["run_off"],
            "pass_def":           splits["pass_def"],
            "run_def":            splits["run_def"],
            "special_teams":      splits["special_teams"],
            "composite":          splits["composite"],
            "sp_offense_scaled":  round(sp_scaled(sp["offense"] if sp else None, sp_means[1]), 2),
            "sp_defense_scaled":  round(sp_scaled(sp["defense"] if sp else None, sp_means[2]), 2),
            "recruiting_scaled":  round(recruiting_scaled, 2) if recruiting_scaled is not None else None,
        }

        rows.append({
            "team_id":            team_id,
            "season":             season,
            "overall_rating":     splits["overall_rating"],
            "offense_rating":     splits["offense_rating"],
            "defense_rating":     splits["defense_rating"],
            "sp_overall":         round(sp["overall"], 2)  if sp else None,
            "sp_offense":         round(sp["offense"], 2)  if sp else None,
            "sp_defense":         round(sp["defense"], 2)  if sp else None,
            "recruiting_score":   round(recruiting_scaled, 2) if recruiting_scaled is not None else None,
            "starter_count":      sum(len(v) for v in by_pos.values()),
            "coaching_change":    team_id in coaching_change_teams,
            "sub_ratings":        json.dumps(sub_ratings),
        })

    # Dedup
    seen: dict = {}
    for r in rows:
        seen[(r["team_id"], r["season"])] = r
    rows = list(seen.values())

    new_df = pd.DataFrame(rows)
    existing = read_computed("team_ratings")
    if not existing.empty:
        mask = ~(
            existing["team_id"].isin(new_df["team_id"]) &
            existing["season"].isin(new_df["season"])
        )
        combined = pd.concat([existing[mask], new_df], ignore_index=True)
    else:
        combined = new_df

    print(f"\nWriting {len(rows)} team rating rows...")
    write_computed("team_ratings", combined)
    print(f"  Done. {len(rows)} rows written.")

    # Top-10 summary
    sorted_rows = sorted(rows, key=lambda r: r["overall_rating"] or 0, reverse=True)
    top10 = sorted_rows[:10]

    print(f"\n{'='*52}")
    print(f"  Top 10 Teams - {season} Overall Rating")
    print(f"{'='*52}")
    print(f"  {'Rank':<5} {'Team':<28} {'Overall':>7} {'Off':>6} {'Def':>6}")
    print(f"  {'-'*4:<5} {'-'*27:<28} {'-'*7:>7} {'-'*5:>6} {'-'*5:>6}")
    for rank, row in enumerate(top10, 1):
        school = school_map.get(row["team_id"], f"team#{row['team_id']}")
        sub = json.loads(row["sub_ratings"]) if isinstance(row["sub_ratings"], str) else row["sub_ratings"]
        po = sub.get("pass_off", "—")
        ro = sub.get("run_off",  "—")
        print(
            f"  {rank:<5} {school:<28} "
            f"{row['overall_rating']:>7.1f} "
            f"{row['offense_rating']:>6.1f} "
            f"{row['defense_rating']:>6.1f}"
        )
    print(f"{'-'*52}")


# ---------------------------------------------------------------------------
# Projected seasons — roster only
# ---------------------------------------------------------------------------

def run_projected_season(season: int) -> None:
    """Team ratings for a season that has not been played.

    SP+, team stats and results do not exist for an unplayed season, so the only
    honest signal is the roster itself. compute_team_splits already falls back to
    roster-only when sp is None, so this path reuses it rather than forking the
    math — the difference is which ratings feed it and that no API is touched.
    """
    print(f"\n{'='*60}")
    print(f"Computing PROJECTED Team Ratings - Season {season}")
    print(f"{'='*60}")

    teams = load_teams()
    roster_map = load_starter_ratings_by_position(season, engine="projected")
    print(f"  {len(roster_map)} teams have projected starter ratings")
    if not roster_map:
        print(f"  ERROR: no projected player ratings for {season} — run script 16 first")
        sys.exit(1)

    recruiting_map = load_recruiting_scores(season)
    sp_means = (0.0, 0.0, 0.0)

    rows = []
    school_map = {t[0]: t[1] for t in teams}
    for team_id, school, conference in teams:
        by_pos = dict(roster_map.get(team_id, {}))
        if not by_pos:
            continue
        splits = compute_team_splits(team_id, None, by_pos, None, sp_means,
                                     recruiting_map.get(team_id))
        rec = recruiting_map.get(team_id)
        sub_ratings = {
            "pass_off":          splits["pass_off"],
            "run_off":           splits["run_off"],
            "pass_def":          splits["pass_def"],
            "run_def":           splits["run_def"],
            "special_teams":     splits["special_teams"],
            "composite":         splits["composite"],
            "sp_offense_scaled": None,
            "sp_defense_scaled": None,
            "recruiting_scaled": round(rec, 2) if rec is not None else None,
        }
        rows.append({
            "team_id":          team_id,
            "season":           season,
            "overall_rating":   splits["overall_rating"],
            "offense_rating":   splits["offense_rating"],
            "defense_rating":   splits["defense_rating"],
            "sp_overall":       None,
            "sp_offense":       None,
            "sp_defense":       None,
            "recruiting_score": round(rec, 2) if rec is not None else None,
            "starter_count":    sum(len(v) for v in by_pos.values()),
            "coaching_change":  False,
            "engine":           "projected",
            "provenance":       "projected",
            "sub_ratings":      json.dumps(sub_ratings),
        })

    new_df = pd.DataFrame(rows)
    existing = read_computed("team_ratings")
    if not existing.empty:
        # Replace only this season's projected rows; earned rows are untouched.
        eng = existing["engine"] if "engine" in existing.columns else pd.Series(
            ["edge"] * len(existing), index=existing.index)
        mask = ~((existing["season"] == season) & (eng == "projected"))
        combined = pd.concat([existing[mask], new_df], ignore_index=True)
    else:
        combined = new_df

    write_computed("team_ratings", combined)
    print(f"  {len(rows)} projected team rating rows written")

    top = sorted(rows, key=lambda r: r["overall_rating"] or 0, reverse=True)[:10]
    print(f"\n  Projected top 10 — {season}")
    for rank, r in enumerate(top, 1):
        print(f"   {rank:>2}. {school_map.get(r['team_id'], '?'):<28} {r['overall_rating']:>5.1f}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute v2 team ratings with position-weighted splits per season."
    )
    parser.add_argument("--season", type=int, default=2025)
    parser.add_argument("--all-seasons", action="store_true",
                        help="Compute for all seasons 2008-2026")
    parser.add_argument("--engine", choices=["edge", "projected"], default="edge",
                        help="'projected' computes from projected player ratings "
                             "with no API calls — for seasons not yet played")
    args = parser.parse_args()

    if args.engine == "projected":
        run_projected_season(args.season)
        print("\nDone.")
        return

    api_key = load_api_key()
    seasons = list(range(2008, 2027)) if args.all_seasons else [args.season]
    for s in seasons:
        run_season(s, api_key)
    print("\nDone.")


if __name__ == "__main__":
    main()
