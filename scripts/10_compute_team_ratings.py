"""Team Ratings v2.1 — three-signal composite with position-weighted splits.

The headline OVR blends three independent readings of the same team:

  1. SP+ (50%) — schedule-adjusted, external, the best single predictor.
  2. Player ratings (30%) — our own, as a 60/40 blend of the top-22 starter mean
     and a position-weighted composite, so a star-driven roster and a uniformly
     solid one stay distinguishable.
  3. Team stats (20%) — yards, third downs, havoc and turnover margin, mapped
     through fixed absolute anchors. Opponent-blind, which is exactly why SP+
     carries the majority.

Until v2.1 the third signal did not exist: this script harvested team stats into
team_season_stats.json for the frontend and never read them back, while the
W_SP/W_ROSTER/W_RAW constants sat declared and unused.

An unplayed season has neither SP+ nor team stats, so 2026 runs on the roster
alone (`--engine projected`) — the weights renormalize rather than treating the
missing signals as zero.

Ratings use fixed absolute anchors (like EDGE player ratings) for cross-season
comparability. A team that rates 89 in 2019 genuinely compares to an 89 in 2025.

Usage:
    python scripts/10_compute_team_ratings.py              # 2025
    python scripts/10_compute_team_ratings.py --season 2024
    python scripts/10_compute_team_ratings.py --all-seasons
    python scripts/10_compute_team_ratings.py --season 2026 --engine projected
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
from utils.line_unit import line_unit_rating
from utils.results_rating import power_components

MODEL_VERSION = "v2.0-team"

# ---------------------------------------------------------------------------
# Weight constants
# ---------------------------------------------------------------------------

# The headline blend is W_ROSTER_BLEND / W_STATS_BLEND, below: a team is rated on its
# own players' ratings and its own team stats from THAT season, nothing external.
# Until v4.11 it was 0.50 SP+ / 0.30 roster / 0.20 stats; SP+ is now published beside
# the rating as a reference column (sp_overall) and no longer feeds it.

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


# Headline OVR blend. Three independent readings of the same team:
#   SP+     — schedule-adjusted, external, the single best predictor
#   roster  — our own player ratings, which is the thing this site actually builds
#   stats   — what the team did on the field, unadjusted for opponent
#
# The third was missing entirely: team stats were harvested into
# team_season_stats.json by this very script and then never read, while
# W_SP/W_ROSTER/W_RAW sat declared and unused. Sum must equal 1.0.
W_ROSTER_BLEND = 0.60
W_STATS_BLEND  = 0.40

# Team-stat anchors. Absolute values, not percentiles of the season, so a weak
# year does not manufacture a great defense — the same rule the player anchors
# follow. Per-game figures.
# Defensive anchors read "more allowed = worse", so their OVR descends while x
# ascends. x MUST ascend regardless — np.interp requires it and returns silent
# nonsense otherwise.
YARDS_OFF_ANCHORS  = [(250, 45), (320, 58), (380, 70), (430, 80), (490, 89), (550, 96)]
YARDS_DEF_ANCHORS  = [(260, 97), (300, 90), (340, 80), (390, 68), (450, 55), (520, 40)]
THIRD_OFF_ANCHORS  = [(28, 45), (34, 58), (40, 70), (45, 82), (52, 93)]
THIRD_DEF_ANCHORS  = [(28, 95), (34, 84), (39, 70), (44, 56), (50, 42)]
HAVOC_ANCHORS      = [(3.5, 45), (5.0, 58), (6.5, 72), (8.0, 85), (9.5, 95)]   # sacks+TFL per game
TO_MARGIN_ANCHORS  = [(-15, -4.0), (-6, -2.0), (0, 0.0), (6, 2.0), (15, 4.0)]  # OVR points

# Within the roster signal: a slot score (below) vs a position-weighted composite. Until v4.16 the
# first half was a flat mean of the 22 best ratings of any position.
W_ROSTER_SLOTS, W_ROSTER_WEIGHTED = 0.60, 0.40

# THE SLOT SCORE. Each position group is scored from its starters with some depth behind them, and the
# groups are weighted. SLOT_STARTERS is how many starters a group has (DL includes EDGE, DB includes CB and S,
# K and P are one group); the next SLOT_DEPTH_N players are the depth; a group scores
# STARTER_SHARE x (mean of its starters) + (1 - STARTER_SHARE) x (mean of its depth), or its starters alone
# when it has no depth. A group nobody is rated in drops out and the weights renormalise (never a default).
# SLOT_WEIGHTS are 0.75 x the weights fitted by non-negative least squares of season scoring margin on the
# group scores (2016-2025, leave-one-season-out, 1,261 team-seasons) + 0.25 x a football prior
# (QB .21 RB .07 WR .14 TE .06 DL .21 LB .11 DB .18 K/P .02), because the pure fit gives WR and TE zero
# (their ratings overlap the QB's) and a team rating should not say receivers do not matter. Fitted, then
# shrunk: roster alone Spearman 0.764 against 0.735 for the top-22 mean; blended with team stats 0.879
# against 0.872 (pure fit 0.882). OL is NOT a slot: it enters only through the line unit inside the
# position-weighted half, as before.
SLOT_STARTERS = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DL": 4, "LB": 3, "DB": 5, "KP": 2}
SLOT_DEPTH_N = 3
STARTER_SHARE = 0.80
SLOT_WEIGHTS = {"QB": 0.194, "RB": 0.189, "WR": 0.036, "TE": 0.016, "DL": 0.150, "LB": 0.123, "DB": 0.286, "KP": 0.008}


def slot_score(by_pos: dict) -> float | None:
    """The slot score of a roster: weighted mean of its position groups' starter-plus-depth scores.

    `by_pos` is {position_group: [ratings]}. None when no group has a rated player.
    """
    groups = {
        "QB": by_pos.get("QB", []), "RB": by_pos.get("RB", []), "WR": by_pos.get("WR", []), "TE": by_pos.get("TE", []),
        "DL": by_pos.get("DL", []) + by_pos.get("EDGE", []), "LB": by_pos.get("LB", []),
        "DB": by_pos.get("DB", []) + by_pos.get("CB", []) + by_pos.get("S", []),
        "KP": by_pos.get("K", []) + by_pos.get("P", []),
    }
    parts = []
    for g, n in SLOT_STARTERS.items():
        xs = sorted(groups[g], reverse=True)
        if not xs:
            continue
        start = float(np.mean(xs[:n]))
        depth = xs[n:n + SLOT_DEPTH_N]
        score = start if not depth else STARTER_SHARE * start + (1 - STARTER_SHARE) * float(np.mean(depth))
        parts.append((score, SLOT_WEIGHTS[g]))
    return blend(parts)

W_STAT_OFF, W_STAT_DEF = 0.50, 0.50
W_YDS, W_THIRD = 0.65, 0.35          # within offense
W_YDS_D, W_THIRD_D, W_HAVOC_D = 0.50, 0.25, 0.25   # within defense


def team_stats_split(stats: dict | None):
    """A team's own box score as (offense OVR, defense OVR, turnover adjustment).

    Each is None when its inputs are missing, and the adjustment is 0.0 without a turnover
    margin. Opponent-blind by construction. team_stats_to_ovr() averages the two halves
    and adds the adjustment; the offense and defense ratings read the halves directly.
    """
    if not stats:
        return None, None, 0.0

    def val(key):
        v = stats.get(key)
        try:
            return float(v) if v is not None and v == v else None
        except (TypeError, ValueError):
            return None

    yo, yd = val("yards_pg"), val("yards_allowed_pg")
    to, td = val("third_down_pct"), val("third_down_def_pct")
    sk, tf = val("sacks_pg"), val("tfl_pg")
    if yo is None and yd is None:
        return None, None, 0.0

    off = dfn = None
    if yo is not None or to is not None:
        tot, w = 0.0, 0.0
        if yo is not None: tot += _interp_anchors(yo, YARDS_OFF_ANCHORS) * W_YDS;   w += W_YDS
        if to is not None: tot += _interp_anchors(to, THIRD_OFF_ANCHORS) * W_THIRD; w += W_THIRD
        if w > 0: off = tot / w
    if yd is not None or td is not None or sk is not None:
        tot, w = 0.0, 0.0
        if yd is not None: tot += _interp_anchors(yd, YARDS_DEF_ANCHORS) * W_YDS_D;   w += W_YDS_D
        if td is not None: tot += _interp_anchors(td, THIRD_DEF_ANCHORS) * W_THIRD_D; w += W_THIRD_D
        if sk is not None or tf is not None:
            tot += _interp_anchors((sk or 0) + (tf or 0), HAVOC_ANCHORS) * W_HAVOC_D
            w += W_HAVOC_D
        if w > 0: dfn = tot / w
    tm = val("turnover_margin")
    adj = _interp_anchors(tm, TO_MARGIN_ANCHORS) if tm is not None else 0.0
    return off, dfn, adj


def team_stats_to_ovr(stats: dict | None) -> float | None:
    """What a team's own box score says about it, on the OVR scale.

    Opponent-blind by construction, which is why it is one input among several and the
    roster's opponent-adjusted player ratings carry the schedule.
    """
    off, dfn, adj = team_stats_split(stats)
    parts, weights = [], []
    if off is not None: parts.append(off); weights.append(W_STAT_OFF)
    if dfn is not None: parts.append(dfn); weights.append(W_STAT_DEF)
    if not parts:
        return None
    return float(np.average(parts, weights=weights)) + adj


def _clip(val: float) -> float:
    return max(10.0, min(99.0, val))


# ---------------------------------------------------------------------------
# SP+ helpers
# ---------------------------------------------------------------------------

def _fuzzy_match(name: str, candidates: list, threshold: float = FUZZY_THRESHOLD):
    """Return the candidate with the highest SequenceMatcher ratio to `name`, or None if the best is under `threshold`."""
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

def avg_top(ratings: list, n: int) -> float | None:
    """Average of the top N ratings, or None when there are none.

    This used to return a hard-coded 50.0, which was a trap rather than a
    default: a position with nobody rated became an average position, and OL is
    40% of run offence. With the OL player rating withdrawn in v4.3 that would
    have made 40% of every team's run offence an identical constant. None forces
    the caller to renormalise, which is what the headline blend already does when
    a whole signal is missing.
    """
    if not ratings:
        return None
    return float(np.mean(sorted(ratings, reverse=True)[:n]))


def blend(parts: list) -> float | None:
    """Weighted mean over the [(value, weight)] pairs that have a value.

    Renormalises across what is present. None when nothing is.
    """
    present = [(v, w) for v, w in parts if v is not None]
    if not present:
        return None
    return sum(v * w for v, w in present) / sum(w for _, w in present)


def build_line_unit_ratings(season: int, stat_table: dict, teams: list) -> dict:
    """{team_id: (rating, contributions)} — the offensive line, rated as a unit.

    This is what replaces the withdrawn per-lineman number. Four run-blocking
    metrics come from /stats/season/advanced (harvested by script 09, confirmed
    back to 2008); sack rate allowed comes from /stats/season, where
    `sacksOpponent` is sacks taken BY this team's offence.

    Empty when script 09 has not been run, in which case the OL term simply drops
    out of the roster splits and the remaining weights renormalise — a smaller
    claim, not a wrong one.
    """
    adv = read_raw("team_advanced_season")
    if adv.empty or "season" not in adv.columns:
        return {}
    sub = adv[adv["season"] == season]
    if sub.empty:
        return {}

    keys = list(stat_table.keys())
    out: dict = {}
    school_by_id = {tid: school for tid, school, _ in teams}

    for _, r in sub.iterrows():
        tid = r.get("team_id")
        if tid is None or pd.isna(tid):
            continue
        tid = int(tid)

        sack_rate = None
        school = school_by_id.get(tid)
        if school:
            s = stat_table.get(school.lower()) or stat_table.get(_fuzzy_match(school.lower(), keys) or "")
            if s:
                sacks_allowed = s.get("sacksOpponent")
                att = s.get("passAttempts")
                # Dropbacks, not attempts: a sack is a pass play that ended in a
                # sack, so it belongs in its own denominator or the rate is
                # understated for exactly the lines that allow the most.
                if sacks_allowed is not None and att:
                    denom = float(att) + float(sacks_allowed)
                    if denom > 0:
                        sack_rate = float(sacks_allowed) / denom

        # Season is passed, not defaulted: the line metrics have two definitional
        # step changes (2014, 2021) and pooled bounds made the rating drift 25
        # points across the archive. See LINE_UNIT_BOUNDS_BY_ERA.
        rating, parts = line_unit_rating({
            "line_yards":         r.get("off_line_yards"),
            "stuff_rate":         r.get("off_stuff_rate"),
            "power_success":      r.get("off_power_success"),
            "second_level_yards": r.get("off_second_level_yards"),
            "sack_rate_allowed":  sack_rate,
        }, season=season)
        if rating is not None:
            out[tid] = (rating, parts)
    return out


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


def compute_roster_splits(by_pos: dict, line_rating: float | None = None) -> dict:
    """pass_off, run_off, pass_def, run_def, special_teams from position ratings.

    The OL term is the LINE-UNIT rating (utils/line_unit.py), not the average of
    five linemen's overalls — those no longer exist, and when they did they were
    the average of five recruiting ranks. When no line rating is available the
    term drops out and the remaining weights renormalise; the same rule now
    applies to every position, so a team with no rated kicker no longer gets a
    silent 50.

    Returns None for a split with no inputs at all. Callers must handle it.
    """
    def g(pos):
        return by_pos.get(pos, [])

    # Merge DB/CB/S into a DB bucket
    db_all = g("DB") + g("CB") + g("S")
    dl_all = g("DL") + g("EDGE")

    qb    = avg_top(g("QB"), 2)
    wrte  = avg_top(g("WR") + g("TE"), 5)
    rb    = avg_top(g("RB"), 3)
    lb    = avg_top(g("LB"), 4)
    dbs   = avg_top(db_all, 5)
    dls   = avg_top(dl_all, 4)

    return {
        "pass_off": blend([(qb, W_QB), (wrte, W_WR_TE), (line_rating, W_OL)]),
        "run_off":  blend([(rb, W_RB), (line_rating, W_OL_RUN),
                           (qb, W_QB_RUN), (wrte, W_WR_TE_RUN)]),
        "pass_def": blend([(dbs, W_DB), (lb, W_LB_PDEF), (dls, W_DL_PDEF)]),
        "run_def":  blend([(dls, W_DL), (lb, W_LB_RDEF), (dbs, W_DB_RDEF)]),
        "special":  avg_top(g("K") + g("P"), 2),
        "line_unit": line_rating,
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
    """Team ids with an HC, OC or DC whose tenure STARTS in `season`. Used only
    to set the `coaching_change` flag on a team row; it does not move a rating.
    """
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
    team_stats: dict | None,
    sp_means: tuple,
    recruiting_scaledcaled: float | None,
    line_rating: float | None = None,
) -> dict | None:
    """Overall/offense/defense (0-99) from the season's own player ratings and team stats.

    Headline = 0.60 roster talent + 0.40 the team's own stats; offense and defense are the
    same blend over the offensive and defensive halves. `sp` (SP+) is accepted but no longer
    an input: it is published beside the rating as a reference. The same function rates a
    played season, a season in progress and, with no stats, a projected one.

    sub_ratings (pass_off/run_off/pass_def/run_def) come from position-weighted
    roster splits — these drive the detail bars, not the headline OVR.

    Returns None when the team has no roster rating and no team stats — there is
    no rating to give, and saying 50.0 is a claim we cannot support.
    """
    # --- Headline ratings: two readings of the same season, blended ---
    # Roster talent, two views of the same roster. The slot score takes the starters of every position
    # group with some depth behind them, weighted by what each group has been worth (see SLOT_WEIGHTS); the
    # position-weighted composite below knows a quarterback outweighs a fourth safety and carries the
    # offensive line (as the line unit). Blending both keeps a star-driven roster and a uniformly solid one
    # distinguishable.
    slots = slot_score(roster_by_pos)
    ros = compute_roster_splits(roster_by_pos, line_rating)
    # Every split can now be None — a position group with nobody rated no longer
    # contributes a fabricated 50 — so the rollup renormalises the same way.
    weighted = blend([(ros["pass_off"], W_PASS_OFF), (ros["run_off"], W_RUN_OFF),
                      (ros["pass_def"], W_PASS_DEF), (ros["run_def"], W_RUN_DEF),
                      (ros["special"],  W_SPEC)])
    if weighted is None:
        slots = None

    if slots is not None:
        roster_ovr = (roster_to_ovr(slots) * W_ROSTER_SLOTS
                      + roster_to_ovr(weighted) * W_ROSTER_WEIGHTED)
    else:
        roster_ovr = None
    stats_ovr = team_stats_to_ovr(team_stats)

    def _mix(pairs):
        pairs = [(v, w) for v, w in pairs if v is not None]
        return sum(v * w for v, w in pairs) / sum(w for _, w in pairs) if pairs else None

    overall = _mix([(roster_ovr, W_ROSTER_BLEND), (stats_ovr, W_STATS_BLEND)])
    if overall is None:
        # No roster rating and no team stats: nothing of THIS season to rate. SP+ is not a
        # substitute any more, and 50.0 would be a placeholder indistinguishable from a
        # real average team.
        return None

    # Offense and defense are the same two readings, split: the roster's offensive and
    # defensive position groups (through the roster scale) and the team's own offensive
    # and defensive stats. When one side has no input the headline stands in for it.
    st_off, st_def, _ = team_stats_split(team_stats)
    r_off = blend([(ros["pass_off"], W_PASS_OFF), (ros["run_off"], W_RUN_OFF)])
    r_def = blend([(ros["pass_def"], W_PASS_DEF), (ros["run_def"], W_RUN_DEF)])
    off = _mix([(roster_to_ovr(r_off) if r_off is not None else None, W_ROSTER_BLEND), (st_off, W_STATS_BLEND)])
    def_rating = _mix([(roster_to_ovr(r_def) if r_def is not None else None, W_ROSTER_BLEND), (st_def, W_STATS_BLEND)])
    off = overall if off is None else off
    def_rating = overall if def_rating is None else def_rating

    # --- Detail splits: position-weighted roster quality (drives the bars) ---
    # Blend roster splits toward the SP+ offense/defense headline so bars track
    # OVR. A split with no roster behind it falls back to the headline rather
    # than to a constant, because the headline is a real reading of this team.
    def _bar(split, anchor):
        return anchor if split is None else split * 0.6 + anchor * 0.4

    pass_off = _bar(ros["pass_off"], off)
    run_off  = _bar(ros["run_off"],  off)
    pass_def = _bar(ros["pass_def"], def_rating)
    run_def  = _bar(ros["run_def"],  def_rating)
    special  = ros["special"]

    def r2(v): return round(float(v), 2)

    return {
        "pass_off":       r2(_clip(pass_off)),
        "run_off":        r2(_clip(run_off)),
        "pass_def":       r2(_clip(pass_def)),
        "run_def":        r2(_clip(run_def)),
        "special_teams":  r2(_clip(special)) if special is not None else None,
        "line_unit":      r2(_clip(line_rating)) if line_rating is not None else None,
        "overall_rating": r2(_clip(overall)),
        "offense_rating": r2(_clip(off)),
        "defense_rating": r2(_clip(def_rating)),
        "composite":      r2(overall),
    }


# ---------------------------------------------------------------------------
# Main per-season computation
# ---------------------------------------------------------------------------

def attach_power(rows: list, season: int, teams, with_results: bool = True) -> None:
    """Adds the power rating and its parts to each team row, in place.

    power = rating part + conference part + results part (utils/results_rating.py), in points per
    game above an average FBS team. `teams` is load_teams()'s (id, school, conference) list; a team's
    conference for the season comes from the harvested season records, falling back to its current
    one. A projected season has no games, so it passes with_results=False and is ranked on its rating
    part alone.
    """
    cur_conf = {int(t[0]): t[2] for t in teams}
    recs = read_raw("team_records")
    season_conf = {}
    if not recs.empty:
        for r in recs[recs["season"] == season].dropna(subset=["team_id", "conference"]).itertuples():
            season_conf[int(r.team_id)] = r.conference
    conf_of = lambda t: season_conf.get(int(t)) or cur_conf.get(int(t))   # noqa: E731
    quality = {int(r["team_id"]): r["overall_rating"] for r in rows if r["overall_rating"] is not None}
    games = []
    if with_results:
        g = read_raw("games")
        if not g.empty:
            games = g[(g["season"] == season) & (g["season_type"] == "regular")].to_dict("records")
    parts, info = power_components(games, quality, conf_of)
    for r in rows:
        p = parts.get(int(r["team_id"]))
        r["power_rating"] = round(p["power"], 2) if p else None
        r["rating_part"] = round(p["rating_part"], 2) if p else None
        r["conf_part"] = round(p["conf_part"], 2) if p else None
        r["results_rating"] = round(p["results_part"], 2) if p else None     # the results part
        r["results_games"] = int(p["games"]) if p else 0
        r["results_wins"] = int(p["wins"]) if p else 0
        r["results_losses"] = int(p["losses"]) if p else 0
        r["home_edge"] = round(info["home_edge"], 2)


def run_season(season: int, api_key: str) -> None:
    """Compute and write earned team ratings for one played season.

    Loads SP+ (and its season means), starter-tier player ratings by position,
    raw team stats, per-game team season stats, the offensive-line unit
    ratings, a RECRUITING_WINDOW-year recruiting score and coaching changes;
    then compute_team_splits() per team → overall / offense / defense plus five
    sub-ratings, the line unit and its inputs. A team with no SP+, no roster
    and no stats gets no row.

    Write: every non-projected row for `season` is dropped from
    data/computed/team_ratings.json before the new rows are added, so a team
    that no longer qualifies disappears instead of lingering as a stale row.
    Projected rows (engine="projected") are left alone.
    """
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

    # Per-game team stats, written by store_team_season_stats above. These feed
    # the headline blend directly — they used to be persisted for the frontend
    # and never read back into the rating.
    # Refreshed BEFORE they are read: this was the other way round until v4.11, so a season's
    # first run blended no per-game stats at all and later runs blended the previous run's.
    print("Storing display team stats...")
    store_team_season_stats(season, api_key, teams)
    _tss = read_computed("team_season_stats")
    tss_map = ({int(r["team_id"]): r.to_dict()
                for _, r in _tss[_tss["season"] == season].iterrows()}
               if not _tss.empty else {})
    print(f"  {len(tss_map)} teams have per-game season stats")

    print("Rating offensive lines as units...")
    line_map = build_line_unit_ratings(season, build_team_stat_table(season, api_key), teams)
    if line_map:
        vals = sorted((v[0] for v in line_map.values()), reverse=True)
        print(f"  {len(line_map)} lines rated  (top {vals[0]:.1f}, median "
              f"{vals[len(vals)//2]:.1f}, bottom {vals[-1]:.1f})")
    else:
        print("  none — run scripts/09_harvest_supplemental.py "
              "--dataset team_advanced_season; the OL term will renormalise out")

    print(f"Loading recruiting ({season - RECRUITING_WINDOW + 1}–{season})...")
    recruiting_map = load_recruiting_scores(season)
    print(f"  {len(recruiting_map)} teams have recruiting data")

    print("Loading coaching changes...")
    coaching_change_teams = load_coaching_changes(season)
    print(f"  {len(coaching_change_teams)} teams with HC/OC/DC changes")

    rows = []
    skipped: list[str] = []
    school_map = {t[0]: t[1] for t in teams}

    for team_id, school, conference in teams:
        sp      = sp_map.get(team_id)
        by_pos  = dict(roster_map.get(team_id, {}))
        raw     = raw_map.get(team_id)
        tstats  = tss_map.get(team_id)
        recruiting_scaled   = recruiting_map.get(team_id)

        line_rating, line_parts = line_map.get(team_id, (None, {}))

        splits = compute_team_splits(team_id, sp, by_pos, tstats, sp_means,
                                     recruiting_scaled, line_rating)
        if splits is None:
            skipped.append(school)
            continue

        sub_ratings = {
            "pass_off":           splits["pass_off"],
            "run_off":            splits["run_off"],
            "pass_def":           splits["pass_def"],
            "run_def":            splits["run_def"],
            "special_teams":      splits["special_teams"],
            # The offensive line, rated as a unit. It replaces the per-lineman
            # number entirely — see utils/line_unit.py — and it ships with its
            # inputs so a team page can show what the rating is made of.
            "line_unit":          splits["line_unit"],
            "line_unit_inputs":   line_parts or None,
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
            "engine":             "edge",
            "provenance":         "earned",
            "sub_ratings":        json.dumps(sub_ratings),
        })

    if skipped:
        print(f"  {len(skipped)} teams have no SP+, no roster and no stats — "
              f"no row written ({', '.join(sorted(skipped)[:6])}"
              f"{', …' if len(skipped) > 6 else ''})")

    # Dedup
    seen: dict = {}
    for r in rows:
        seen[(r["team_id"], r["season"])] = r
    rows = list(seen.values())
    attach_power(rows, season, teams)

    new_df = pd.DataFrame(rows)
    existing = read_computed("team_ratings")
    if not existing.empty:
        # Drop every earned row for this season before re-adding, rather than only
        # the teams we happen to be rewriting. A team that used to get a row and no
        # longer earns one must actually disappear — otherwise a stale placeholder
        # survives forever and collides with the projected row written later.
        eng = (existing["engine"] if "engine" in existing.columns
               else pd.Series(["edge"] * len(existing), index=existing.index))
        eng = eng.fillna("edge")
        mask = ~((existing["season"] == season) & (eng != "projected"))
        combined = (pd.concat([existing[mask], new_df], ignore_index=True)
                    if not new_df.empty else existing[mask].copy())
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
            # No line rating for an unplayed season — the line metrics are
            # measurements of games that have not happened. The OL term
            # renormalises out rather than being projected, which matches the
            # decision not to project individual linemen either.
            "line_unit":         None,
            "line_unit_inputs":  None,
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

    attach_power(rows, season, teams, with_results=False)
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
    """CLI entry point. --engine edge (default) rates played seasons from SP+,
    rosters and stats (--season N or --all-seasons 2008–2026). --engine
    projected rates an unplayed season from projected player ratings with no
    API calls — run it after script 16, per the documented order.
    """
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
