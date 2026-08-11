"""EDGE — Efficiency-Driven Grade per Event (v4.1).

Formula (all positions):
    edge_score = sum_over_games(stat_composite_i × opp_mult_i) / sqrt(games_played)

Offensive (QB/RB/WR/TE):
    stat_composite = weighted sum of per-game stats (yards, TDs, INTs).
    opp_mult scales by the opponent's *defensive* SP+ rating for that game.

Defensive (EDGE/DL/LB/CB/S):
    stat_composite = weighted sum of per-game defensive stats.
    Pre-2016: only INTs available — sacks/hurries/TFLs/PBUs are absent from the
    CFB Data API for those seasons. Defensive EDGE scores pre-2016 will be sparse.
    opp_mult scales by the opponent's *offensive* SP+ rating for that game.

All data is read from data/raw/*.json and written to data/raw/player_edge.json.

Usage:
    python scripts/06_compute_edge_scores.py              # 2025 only
    python scripts/06_compute_edge_scores.py --season 2024
    python scripts/06_compute_edge_scores.py --all-seasons  # 2008-2025
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.store import read_raw, RAW_DIR
from utils.api_client import load_api_key, fetch_sp_ratings_all, fetch_sp_ratings_breakdown, fetch_game_team_stats

MODEL_VERSION = "v4.1-local"

OFF_EDGE_POSITIONS = {"QB", "RB", "WR", "TE"}
DEF_EDGE_POSITIONS = {"EDGE", "DL", "LB", "CB", "S", "DB"}
EDGE_POSITIONS = OFF_EDGE_POSITIONS | DEF_EDGE_POSITIONS
MIN_GAMES = 3

# ---------------------------------------------------------------------------
# Stat composite weights
# ---------------------------------------------------------------------------

OFFENSIVE_COMPOSITE = {
    "QB": {"passingYDS": 1.0, "rushingYDS": 0.7, "passingTD": 25.0, "rushingTD": 20.0, "passingINT": -20.0},
    "RB": {"rushingYDS": 1.0, "receivingYDS": 0.9, "rushingTD": 20.0, "receivingTD": 20.0},
    "WR": {"receivingYDS": 1.0, "receivingTD": 25.0, "receivingREC": 2.0},
    "TE": {"receivingYDS": 1.0, "receivingTD": 25.0, "receivingREC": 2.5},
}

OFFENSE_PRIMARY_STATS = {
    "QB": ["passingATT", "rushingCAR"],
    "RB": ["rushingCAR", "receivingREC"],
    "WR": ["receivingREC"],
    "TE": ["receivingREC"],
}

DEF_STAT_WEIGHTS = {
    "EDGE": {"sacks": 7.0, "hur": 2.5, "tfl": 4.0, "tot": 0.3, "ints": 4.0,  "pbu": 1.5},
    "DL":   {"sacks": 6.0, "hur": 2.0, "tfl": 4.0, "tot": 0.4, "ints": 3.0,  "pbu": 0.5},
    "LB":   {"sacks": 5.5, "hur": 1.5, "tfl": 4.0, "tot": 0.6, "ints": 7.0,  "pbu": 2.0},
    "CB":   {"sacks": 2.5, "hur": 1.0, "tfl": 2.0, "tot": 0.3, "ints": 12.0, "pbu": 2.0},
    "S":    {"sacks": 3.0, "hur": 1.5, "tfl": 3.0, "tot": 0.4, "ints": 10.0, "pbu": 2.0},
    "DB":   {"sacks": 2.5, "hur": 1.0, "tfl": 2.0, "tot": 0.3, "ints": 11.0, "pbu": 2.0},
}

# ---------------------------------------------------------------------------
# Stat coercion
# ---------------------------------------------------------------------------

def _coerce_float(val) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return 0.0
    if "/" in s:
        s = s.split("/", 1)[0]
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0

def _coerce_int(val) -> int:
    return int(_coerce_float(val))


def _parse_stat_value(d: dict, key: str) -> float | None:
    """Parse a stat value from a game-stats dict, handling 'made-att' strings.

    The CFB Data API sometimes encodes stats like completions as "25-38"
    (completions-attempts). This function takes the value before the dash
    so that integer stats parse correctly.
    """
    v = d.get(key)
    if v is None:
        return None
    try:
        return float(str(v).split("-")[0])
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# SP+ opponent quality map — built from API (cached) + local teams.json
# ---------------------------------------------------------------------------

def build_opponent_sp_map(season: int, api_key: str) -> dict:
    """Return {team_db_id: {"overall", "offense", "defense"}} for a season."""
    sp_by_name = fetch_sp_ratings_breakdown(api_key, season)
    if not sp_by_name:
        flat = fetch_sp_ratings_all(api_key, season)
        sp_by_name = {k: {"overall": v, "offense": 0.0, "defense": 0.0} for k, v in flat.items()}

    teams_df = read_raw("teams")
    result = {}
    for _, row in teams_df.iterrows():
        school = str(row.get("school") or "")
        db_id = row.get("id")
        if not db_id or not school:
            continue
        sp = sp_by_name.get(school.lower())
        if sp is not None:
            result[int(db_id)] = sp
    return result


def _season_means(sp_map: dict) -> tuple[float, float, float]:  # (mean_off_sp, mean_def_sp, mean_overall_sp)
    if not sp_map:
        return (25.0, 25.0, 0.0)
    offs  = [v["offense"] for v in sp_map.values() if isinstance(v, dict)]
    defs  = [v["defense"] for v in sp_map.values() if isinstance(v, dict)]
    overs = [v["overall"] for v in sp_map.values() if isinstance(v, dict)]
    return (
        sum(offs)  / len(offs)  if offs  else 25.0,
        sum(defs)  / len(defs)  if defs  else 25.0,
        sum(overs) / len(overs) if overs else 0.0,
    )


def opponent_multiplier(opp_team_id, sp_map: dict, side: str, season_means: tuple) -> float:
    """Asymmetric opponent quality multiplier.

    Bonus (hard opponent):   up to 1.70 (z * 0.35)
    Penalty (weak opponent): down to 0.76 (z * 0.12)
    """
    if not opp_team_id or opp_team_id not in sp_map:
        return 1.0
    sp_entry = sp_map[opp_team_id]
    mean_off, mean_def, mean_ovr = season_means

    if isinstance(sp_entry, dict):
        if side == "defense":
            val = sp_entry.get("defense", mean_def)
            std = max(mean_def * 0.26, 1.0)
            z = (mean_def - val) / std
        else:
            val = sp_entry.get("offense", mean_off)
            std = max(mean_off * 0.26, 1.0)
            z = (val - mean_off) / std
    else:
        sp = float(sp_entry)
        z = (sp - mean_ovr) / 30.0

    z = max(-2.0, min(2.0, z))
    if z >= 0:
        return 1.0 + z * 0.35
    else:
        return 1.0 + z * 0.12


# ---------------------------------------------------------------------------
# Team defensive context (game-level yards/points allowed)
# ---------------------------------------------------------------------------

def build_game_context_map(api_key: str, season: int) -> dict:
    """Return {(db_game_id, def_team_db_id): {pass_yds, rush_yds, points}}."""
    raw = fetch_game_team_stats(api_key, season)
    if not raw:
        return {}

    games_df = read_raw("games")
    teams_df = read_raw("teams")

    game_cfb_to_db = {}
    for _, r in games_df[games_df["season"] == season].iterrows():
        cid = r.get("cfb_api_id")
        if cid is not None:
            game_cfb_to_db[int(cid)] = int(r["id"])

    team_cfb_to_db = {}
    for _, r in teams_df.iterrows():
        cid = r.get("cfb_api_id")
        if cid is not None:
            team_cfb_to_db[int(cid)] = int(r["id"])

    result = {}
    for cfb_game_id, teams_dict in raw.items():
        db_game_id = game_cfb_to_db.get(cfb_game_id)
        if not db_game_id:
            continue
        team_ids = list(teams_dict.keys())
        if len(team_ids) != 2:
            continue
        for i, def_cfb_tid in enumerate(team_ids):
            off_cfb_tid = team_ids[1 - i]
            def_db_tid = team_cfb_to_db.get(def_cfb_tid)
            if not def_db_tid:
                continue
            opp_stats = teams_dict[off_cfb_tid]
            pass_yds = _parse_stat_value(opp_stats, "netPassingYards")
            rush_yds = _parse_stat_value(opp_stats, "rushingYards")
            points   = opp_stats.get("_points")
            if points is not None:
                try:
                    points = float(points)
                except (ValueError, TypeError):
                    points = None
            result[(db_game_id, def_db_tid)] = {"pass_yds": pass_yds, "rush_yds": rush_yds, "points": points}
    return result


# ---------------------------------------------------------------------------
# Coverage-denial credit (CB / S / DB)
#
# A defensive back's best games leave no trace. Quarterbacks stop throwing at a
# corner who covers, so the counting stats that drive every other position's
# score measure the opposite of what we want for this one — volume accrues to
# the DBs who get picked on, and to safeties on defenses that are on the field
# all day. Caleb Downs, whom no credible top-five safety list omits, rated 22nd.
#
# def_context_modifier already knows which defenses are good, but it is
# MULTIPLICATIVE, and 1.1 x a suppressed composite is still suppressed. Credit
# has to be ADDITIVE to survive the suppression it is correcting.
#
# So: a DB who plays full-time on a defense that genuinely denies the pass is
# credited for that denial, whether or not the ball ever came near him. This is
# the same reasoning OL already runs on — when individual data does not exist,
# a team proxy is the honest measure — and it carries the same humility.
#
# Tuned against EA CFB 27 as an independent scouting consensus (Spearman of our
# DB score vs EA's OVR, 2025, n=954): 0.639 uncredited -> 0.658 at this scale,
# with a broad optimum from x3 to x5. A placebo that credits the same amount to
# RANDOMLY chosen defenses scores 0.629 — worse than no credit at all — so the
# gain is the denial signal, not the extra magnitude.
# ---------------------------------------------------------------------------

COVERAGE_POSITIONS = {"CB", "S", "DB"}

# Max per-game credit in stat-composite units, for a full-time starter on the
# most suppressive pass defense in the country. A typical starting corner earns
# ~3.2/game from the counting stats, so this roughly doubles the elite case.
# Safeties are scaled down: run support keeps their volume closer to honest.
COVERAGE_CREDIT = {"CB": 4.2, "DB": 3.7, "S": 3.0}

# A defense fields five defensive backs. The five who do the most work in a
# team's secondary are treated as full-time; the next two taper off.
COVERAGE_ROOM_SIZE = 5
COVERAGE_TAPER = {6: 0.6, 7: 0.3}

# Guard against a thin secondary handing a token contributor a starter's credit:
# a player still needs this fraction of a typical starter's tackle volume.
COVERAGE_MIN_VOLUME = 0.25


# Minimum pass attempts faced before a defense has demonstrated anything.
DENIAL_MIN_ATTEMPTS = 100
# Credit runs from the 75th percentile of yards-per-attempt allowed (some credit
# for three quarters of defenses) down to the 8th (full credit).
DENIAL_HI_PCT, DENIAL_LO_PCT = 75, 8


def _pass_attempts(stats: dict) -> int:
    """Attempts from a game row.

    Game rows encode passing as `passingC/ATT` — "25/38". `_coerce_float` splits
    on "/" and keeps the FIRST field, which is completions, so reading attempts
    needs its own parse. Getting this wrong silently yields zero attempts for
    every team and the whole denial signal disappears.
    """
    v = stats.get("passingC/ATT")
    if v is None:
        return _coerce_int(stats.get("passingATT"))
    try:
        return int(float(str(v).split("/")[-1]))
    except (ValueError, TypeError):
        return 0


def build_team_pass_denial(season: int) -> dict:
    """{team_db_id: 0..1} — how much better than par a defense is per pass thrown.

    Yards ALLOWED PER ATTEMPT, not total yards. Total yards confounds coverage
    with pace and game script: a defense that leads, or that stuffs the run,
    faces more throws and "allows" more yards for identical coverage. Switching
    to per-attempt raised agreement with an independent consensus from 0.6511 to
    0.6630, and widening the band from a 2-SD cut to the 75th->8th percentile
    added another 0.002 — under the 2-SD rule half the defenses in the country
    scored exactly zero, so most defensive backs got no coverage signal at all.

    Taken across a whole season, never per game: one game's passing line is
    mostly noise, a season's is the defense.

    Credit only, never penalty. A porous pass defense scores 0 rather than
    negative; the multiplicative def_context_modifier already carries downside,
    and double-charging it would bury the DBs who are the reason a bad defense
    is not worse.
    """
    off_rows = _load_game_stats(season, OFF_EDGE_POSITIONS)
    if off_rows.empty:
        return {}

    faced: dict[int, list[float]] = {}
    for _, r in off_rows.iterrows():
        stats = r["data"] if isinstance(r["data"], dict) else {}
        att = _pass_attempts(stats)
        yds = _coerce_int(stats.get("passingYDS"))
        if att <= 0 and yds <= 0:
            continue
        my_team = r["player_team_id"]
        if my_team is None or pd.isna(my_team):
            continue
        # The offense's line is charged to the DEFENSE it was thrown against.
        opp = r["away_team_id"] if my_team == r["home_team_id"] else r["home_team_id"]
        if opp is None or pd.isna(opp):
            continue
        e = faced.setdefault(int(opp), [0.0, 0.0])
        e[0] += att
        e[1] += yds

    return denial_from_ypa({t: y / a for t, (a, y) in faced.items()
                            if a >= DENIAL_MIN_ATTEMPTS})


def denial_from_ypa(ypa: dict) -> dict:
    """{team: yards per attempt allowed} -> {team: 0..1 credit}. Pure; testable."""
    if len(ypa) < 10:
        return {}
    vals = np.array(list(ypa.values()), dtype=float)
    hi = float(np.percentile(vals, DENIAL_HI_PCT))
    lo = float(np.percentile(vals, DENIAL_LO_PCT))
    if hi <= lo:
        return {}
    return {t: float(np.clip((hi - v) / (hi - lo), 0.0, 1.0)) for t, v in ypa.items()}


# ---------------------------------------------------------------------------
# Defensive-back archetypes
#
# "Defensive back" is three jobs wearing one label, and a single composite makes
# them compete on one axis they do not share. These sub-scores say WHICH job a
# player does well, so a lockdown corner is not read as a failed ball hawk.
#
# They describe, they do not rate. Combining them into the rating (best skill
# plus partial credit for the rest) was tried and scored 0.6431 against an
# external consensus versus 0.6613 for the additive composite — taking the max
# throws away the information that a player is good at two things. So the
# composite stays the rating and these ride alongside it.
#
# Tackles appear ONLY in run support. For ball skills and coverage they are not
# production at all — see build_coverage_participation, where they are used as
# evidence of playing time instead.
# ---------------------------------------------------------------------------

ARCHETYPE_WEIGHTS = {
    "ball_hawk":   {"ints": 12.0, "pbu": 3.5, "def_td": 8.0},
    "run_support": {"tot": 0.6, "tfl": 4.0, "sacks": 6.0, "hur": 1.5},
    # coverage has no box-score inputs at all; it is playing time x denial
}

# Fixed divisors putting the three on one 0-10 axis: each is the 90th percentile
# of that archetype's raw score among rated defensive backs, so a 10 means the
# same thing in all three. Frozen constants, like the OVR anchors — recomputing
# them per season would make a weak year's best look elite.
#
# These must be re-measured whenever the inputs change. They were first set from
# a prototype that used a different denial signal, which left coverage topping
# out at 7.1 on a 0-10 axis while run support reached 20 — coverage could not win
# a comparison it was supposed to be able to win, and 25 defensive backs out of
# 2,026 typed as coverage players.
ARCHETYPE_SCALE = {"ball_hawk": 12.9, "coverage": 8.5, "run_support": 14.8}

# How much each job counts toward the overall, by position. A corner is paid to
# cover and take the ball away; a safety is the last line and plays the run.
#
# This makes a defensive back's rating literally the sum of the three
# sub-ratings shown on his card, which is worth something on its own. It is
# marginally less predictive than the flat stat composite it replaces —
# Spearman against EA CFB 27 goes 0.660 -> 0.655 (2025) and 0.555 -> 0.540
# (2024) — a cost accepted deliberately in exchange for a rating that explains
# itself. Reverting is a one-line change: drop back to _def_stat_composite.
SECONDARY_ARCHETYPE_WEIGHTS = {
    "CB": {"coverage": 0.40, "ball_hawk": 0.40, "run_support": 0.20},
    "S":  {"run_support": 0.50, "ball_hawk": 0.30, "coverage": 0.20},
    "DB": {"run_support": 1 / 3, "ball_hawk": 1 / 3, "coverage": 1 / 3},
}


def _archetype_raws(pg: str, stats: dict, credit: float) -> dict:
    """Per-game archetype contributions for one defensive back."""
    ints   = _coerce_int(stats.get("interceptionsINT")) or _coerce_int(stats.get("defensiveINT"))
    pbu    = _coerce_int(stats.get("defensivePD")) or _coerce_int(stats.get("defensivePBU"))
    def_td = _coerce_int(stats.get("defensiveTD")) + _coerce_int(stats.get("interceptionsTD"))
    b = ARCHETYPE_WEIGHTS["ball_hawk"]
    r = ARCHETYPE_WEIGHTS["run_support"]
    return {
        "ball_hawk":   ints * b["ints"] + pbu * b["pbu"] + def_td * b["def_td"],
        "run_support": (_coerce_int(stats.get("defensiveTOT")) * r["tot"]
                        + _coerce_int(stats.get("defensiveTFL")) * r["tfl"]
                        + _coerce_int(stats.get("defensiveSACKS")) * r["sacks"]
                        + _coerce_int(stats.get("defensiveQB HUR")) * r["hur"]),
        "coverage":    credit,
    }


def build_coverage_participation(rows: pd.DataFrame) -> dict:
    """{(team_id, player_id): 0..1} — is he one of his secondary's regulars?

    Playing time has to be inferred from tackles, because no snap counts exist
    for defenders. But *share* of tackles cannot be the measure: the corner this
    credit exists for is avoided, so he tackles less than his teammates, and
    scaling his credit by that share would hand back exactly the suppression we
    are correcting (a covered corner scored 0.68 of a full-timer on a plain
    share rule).

    So rank, not share. The five defensive backs who do the most work are the
    five on the field, whatever the spread between them, and a corner is not
    docked for tackles he never had to make. COVERAGE_MIN_VOLUME then stops a
    thin secondary from promoting a token contributor into that group: the bar
    is a quarter of what a typical starter in that room records.
    """
    dbs = rows[rows["position_group"].isin(COVERAGE_POSITIONS)]
    if dbs.empty:
        return {}

    tot = dbs["data"].apply(lambda d: _coerce_int(d.get("defensiveTOT")) if isinstance(d, dict) else 0)
    room = pd.DataFrame({
        "team_id":   dbs["player_team_id"].values,
        "player_id": dbs["player_id"].values,
        "tot":       tot.values,
    }).dropna(subset=["team_id"])
    if room.empty:
        return {}

    by_player = room.groupby(["team_id", "player_id"], as_index=False)["tot"].sum()
    by_player["rank"] = by_player.groupby("team_id")["tot"].rank(ascending=False, method="first")

    # A typical starter's volume in this room, taken from the top of it so one
    # thin rotation cannot define its own baseline.
    ref = (by_player[by_player["rank"] <= COVERAGE_ROOM_SIZE - 1]
           .groupby("team_id")["tot"].median().rename("ref"))
    by_player = by_player.merge(ref, on="team_id", how="left")

    out = {}
    for t, p, tot_v, rk, rf in zip(by_player["team_id"], by_player["player_id"],
                                   by_player["tot"], by_player["rank"], by_player["ref"]):
        if tot_v <= 0:
            continue
        base = 1.0 if rk <= COVERAGE_ROOM_SIZE else COVERAGE_TAPER.get(int(rk), 0.0)
        if base <= 0:
            continue
        floor = max(float(rf or 0) * COVERAGE_MIN_VOLUME, 1.0)
        out[(int(t), int(p))] = float(base * np.clip(tot_v / floor, 0.0, 1.0))
    return out


def _compute_season_defensive_means(ctx_map: dict) -> dict:
    pass_vals = [v["pass_yds"] for v in ctx_map.values() if v["pass_yds"] is not None]
    rush_vals = [v["rush_yds"] for v in ctx_map.values() if v["rush_yds"] is not None]
    pt_vals   = [v["points"]   for v in ctx_map.values() if v["points"]   is not None]
    def _stats(vals):
        if not vals:
            return (200.0, 60.0)
        return (float(np.mean(vals)), max(float(np.std(vals)), 1.0))
    return {"pass": _stats(pass_vals), "rush": _stats(rush_vals), "pts": _stats(pt_vals)}


DEF_CONTEXT_WEIGHTS = {
    "CB":   {"pass": 0.55, "rush": 0.05, "pts": 0.40},
    "S":    {"pass": 0.45, "rush": 0.15, "pts": 0.40},
    "DB":   {"pass": 0.50, "rush": 0.10, "pts": 0.40},
    "DL":   {"pass": 0.15, "rush": 0.55, "pts": 0.30},
    "EDGE": {"pass": 0.25, "rush": 0.45, "pts": 0.30},
    "LB":   {"pass": 0.30, "rush": 0.40, "pts": 0.30},
}
DEF_CONTEXT_BLEND = 0.35  # weight applied to team defensive context modifier (vs raw EDGE). See docs/AUDIT_FINDINGS.md §5.


def def_context_modifier(pg: str, game_db_id: int, player_team_db_id: int,
                          ctx_map: dict, ctx_means: dict) -> float:
    ctx = ctx_map.get((game_db_id, player_team_db_id))
    if not ctx:
        return 1.0
    weights = DEF_CONTEXT_WEIGHTS.get(pg)
    if not weights:
        return 1.0
    z_pass = z_rush = z_pts = 0.0
    pass_yds = ctx.get("pass_yds")
    if pass_yds is not None:
        mean_p, std_p = ctx_means["pass"]
        z_pass = (mean_p - pass_yds) / std_p
    rush_yds = ctx.get("rush_yds")
    if rush_yds is not None:
        mean_r, std_r = ctx_means["rush"]
        z_rush = (mean_r - rush_yds) / std_r
    pts = ctx.get("points")
    if pts is not None:
        mean_pts, std_pts = ctx_means["pts"]
        z_pts = (mean_pts - pts) / std_pts
    z_total = max(-1.5, min(1.5, z_pass * weights["pass"] + z_rush * weights["rush"] + z_pts * weights["pts"]))
    raw_mod = 1.0 + z_total * 0.20
    return 1.0 - DEF_CONTEXT_BLEND + DEF_CONTEXT_BLEND * raw_mod


# ---------------------------------------------------------------------------
# Load per-game stats from local raw JSON
# ---------------------------------------------------------------------------

def _load_game_stats(season: int, positions: set) -> pd.DataFrame:
    """Load game_aggregate stat rows from data/raw/stats.json for given positions."""
    stats_df = read_raw("stats")
    ps_df    = read_raw("player_seasons")
    games_df = read_raw("games")

    if stats_df.empty or ps_df.empty or games_df.empty:
        return pd.DataFrame()

    # Filter stats
    mask = (
        (stats_df["stat_type"] == "game_aggregate") &
        (stats_df["game_id"].notna())
    )
    s = stats_df[mask].copy()

    # Filter player_seasons to this season + positions
    ps = ps_df[
        (ps_df["season"] == season) &
        (ps_df["position_group"].isin(positions))
    ][["id", "player_id", "position_group", "team_id"]].copy()
    ps = ps.rename(columns={"id": "ps_id"})

    # Parse data field
    def _parse_data(v):
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return {}
        return {}

    s["data"] = s["data"].apply(_parse_data)

    # stats also has a player_id column (Supabase internal) — drop it before merging
    s = s.drop(columns=["player_id"], errors="ignore")

    # Join stats -> player_seasons
    merged = s.merge(ps, left_on="player_season_id", right_on="ps_id", how="inner")
    merged = merged.rename(columns={"player_id": "player_id", "team_id": "player_team_id"})

    # Join games for home/away team IDs
    g = games_df[["id", "home_team_id", "away_team_id"]].rename(columns={"id": "game_db_id"})
    merged = merged.merge(g, left_on="game_id", right_on="game_db_id", how="left")

    return merged[["player_id", "position_group", "player_team_id", "game_id", "data", "home_team_id", "away_team_id"]]


# ---------------------------------------------------------------------------
# Offensive EDGE
# ---------------------------------------------------------------------------

def _off_stat_composite(pg: str, stats: dict) -> float:
    weights = OFFENSIVE_COMPOSITE.get(pg, {})
    return sum(_coerce_float(stats.get(k)) * w for k, w in weights.items())


def _off_stats_measured(pg: str, stats: dict) -> float:
    if pg == "QB":
        catt = stats.get("passingC/ATT") or stats.get("passingATT")
        if catt and "/" in str(catt):
            att = _coerce_int(str(catt).split("/")[-1])
        else:
            att = _coerce_int(catt)
        return float(att + _coerce_int(stats.get("rushingCAR")))
    return sum(_coerce_float(stats.get(k)) for k in OFFENSE_PRIMARY_STATS.get(pg, []))


def compute_offensive_edge(season: int, sp_map: dict) -> pd.DataFrame:
    rows = _load_game_stats(season, OFF_EDGE_POSITIONS)
    if rows.empty:
        return pd.DataFrame()

    season_means = _season_means(sp_map)
    records = []

    for _, r in rows.iterrows():
        pg      = r["position_group"]
        my_team = r["player_team_id"]
        opp_team = r["away_team_id"] if my_team == r["home_team_id"] else r["home_team_id"]
        stats   = r["data"] if isinstance(r["data"], dict) else {}

        raw = _off_stat_composite(pg, stats)
        if raw <= 0:
            continue

        try:
            opp_id = int(opp_team) if opp_team is not None and not pd.isna(opp_team) else None
        except (TypeError, ValueError):
            opp_id = None

        opp_mult = opponent_multiplier(opp_id, sp_map, side="defense", season_means=season_means)
        records.append({
            "player_id":      int(r["player_id"]),
            "position_group": pg,
            "adj_score":      raw * opp_mult,
            "opp_mult":       opp_mult,
            "primary_vol":    _off_stats_measured(pg, stats),
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    def agg(grp):
        n         = len(grp)
        edge      = grp["adj_score"].sum() / max(np.sqrt(n), 1.0)
        avg_mult  = grp["opp_mult"].mean()
        return pd.Series({
            "edge_score":      edge,
            "games_played":    n,
            "stats_measured":  grp["primary_vol"].sum(),
            "opponent_avg_sp": (avg_mult - 1.0) * 60.0,
            "position_group":  grp["position_group"].iloc[0],
        })

    result = df.groupby("player_id").apply(agg, include_groups=False).reset_index()
    result.loc[result["games_played"] < MIN_GAMES, "edge_score"] = None
    return result


# ---------------------------------------------------------------------------
# Defensive EDGE
# ---------------------------------------------------------------------------

def _def_stat_composite(pg: str, stats: dict) -> tuple[float, float]:
    weights = DEF_STAT_WEIGHTS.get(pg)
    if not weights:
        return 0.0, 0.0
    tot   = _coerce_int(stats.get("defensiveTOT"))
    sacks = _coerce_int(stats.get("defensiveSACKS"))
    tfl   = _coerce_int(stats.get("defensiveTFL"))
    hur   = _coerce_int(stats.get("defensiveQB HUR")) or _coerce_int(stats.get("defensiveQBH"))
    pbu   = _coerce_int(stats.get("defensivePD")) or _coerce_int(stats.get("defensivePBU"))
    ints  = _coerce_int(stats.get("interceptionsINT")) or _coerce_int(stats.get("defensiveINT"))
    score = (tot * weights["tot"] + sacks * weights["sacks"] + tfl * weights["tfl"] +
             hur * weights["hur"] + pbu * weights["pbu"] + ints * weights["ints"])
    vol = tot + sacks + tfl + hur + pbu + ints
    return score, float(vol)


def compute_defensive_edge(season: int, sp_map: dict, ctx_map: dict | None = None) -> pd.DataFrame:
    rows = _load_game_stats(season, DEF_EDGE_POSITIONS)
    if rows.empty:
        return pd.DataFrame()

    season_means = _season_means(sp_map)
    ctx_means = _compute_season_defensive_means(ctx_map) if ctx_map else {}
    pass_denial   = build_team_pass_denial(season)
    participation = build_coverage_participation(rows)
    records = []

    for _, r in rows.iterrows():
        pg      = r["position_group"]
        my_team = r["player_team_id"]
        game_id = r["game_id"]
        opp_team = r["away_team_id"] if my_team == r["home_team_id"] else r["home_team_id"]
        stats   = r["data"] if isinstance(r["data"], dict) else {}

        raw, vol = _def_stat_composite(pg, stats)

        # Coverage denial — see COVERAGE_CREDIT. Additive, so it survives the
        # stat suppression that is the whole reason it exists.
        credit = 0.0
        if pg in COVERAGE_POSITIONS and my_team is not None and not pd.isna(my_team):
            credit = (COVERAGE_CREDIT[pg]
                      * participation.get((int(my_team), int(r["player_id"])), 0.0)
                      * pass_denial.get(int(my_team), 0.0))

        # A defensive back's score IS his three sub-ratings, weighted by what his
        # position is actually paid to do. Scaling per game is equivalent to
        # scaling the aggregate — the whole chain is linear — so the archetypes
        # a card displays and the number beside them cannot drift apart.
        arch = None
        if pg in COVERAGE_POSITIONS:
            arch = _archetype_raws(pg, stats, credit)
            w = SECONDARY_ARCHETYPE_WEIGHTS[pg]
            raw = sum(w[k] * (arch[k] / ARCHETYPE_SCALE[k] * 10.0) for k in w)

        # A game where nothing was thrown at him is not an absent game. Keep it
        # when coverage credit is owed — dropping it shrinks the sqrt(games)
        # denominator and quietly rewards the suppression instead.
        if raw <= 0 and credit <= 0:
            continue

        try:
            opp_id = int(opp_team) if opp_team is not None and not pd.isna(opp_team) else None
        except (TypeError, ValueError):
            opp_id = None

        opp_mult = opponent_multiplier(opp_id, sp_map, side="offense", season_means=season_means)

        ctx_mult = 1.0
        if ctx_map and ctx_means and my_team is not None and game_id is not None:
            try:
                ctx_mult = def_context_modifier(pg, int(game_id), int(my_team), ctx_map, ctx_means)
            except (TypeError, ValueError):
                ctx_mult = 1.0

        # For the secondary the credit is already inside `raw` as the coverage
        # archetype; adding it again would pay for it twice.
        score = raw if arch is not None else raw + credit
        rec = {
            "player_id":      int(r["player_id"]),
            "position_group": pg,
            "adj_score":      score * opp_mult * ctx_mult,
            "opp_mult":       opp_mult,
            "raw_vol":        vol,
            # What the coverage archetype contributes, in the same units as the
            # score, so coverage_share below stays meaningful.
            "cov_credit":     (SECONDARY_ARCHETYPE_WEIGHTS[pg]["coverage"]
                               * credit / ARCHETYPE_SCALE["coverage"] * 10.0)
                              if arch is not None else credit,
        }
        if arch is not None:
            for k, v in arch.items():
                rec[f"arch_{k}"] = v * opp_mult
        records.append(rec)

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    def agg(grp):
        n         = len(grp)
        edge      = grp["adj_score"].sum() / max(np.sqrt(n), 1.0)
        avg_mult  = grp["opp_mult"].mean()
        # What fraction of the score is coverage denial rather than counting
        # stats. Carried through so the rating can say so out loud instead of
        # leaving a DB's number unexplained.
        total     = float(grp["adj_score"].sum())
        credited  = float((grp["cov_credit"] * grp["opp_mult"]).sum())
        out = {
            "edge_score":      edge,
            "games_played":    n,
            "stats_measured":  grp["raw_vol"].sum(),
            "opponent_avg_sp": (avg_mult - 1.0) * 60.0,
            "coverage_share":  (credited / total) if total > 0 else 0.0,
            "position_group":  grp["position_group"].iloc[0],
        }
        # Archetypes on a shared 0-10 axis, aggregated the same way the rating is
        # so a part-season player is not flattered by the sum.
        for k, scale in ARCHETYPE_SCALE.items():
            col = f"arch_{k}"
            if col in grp.columns:
                per = grp[col].sum() / max(np.sqrt(n), 1.0)
                out[col] = float(np.clip(per / scale * 10.0, 0.0, 20.0))
        return pd.Series(out)

    result = df.groupby("player_id").apply(agg, include_groups=False).reset_index()
    result.loc[result["games_played"] < MIN_GAMES, "edge_score"] = None
    return result


# ---------------------------------------------------------------------------
# Write results to data/raw/player_edge.json (upsert by player_season_id)
# ---------------------------------------------------------------------------

def save_edge(agg: pd.DataFrame, season: int) -> None:
    ps_df = read_raw("player_seasons")
    ps_season = ps_df[ps_df["season"] == season][["id", "player_id"]].copy()
    ps_map = {int(r["player_id"]): int(r["id"]) for _, r in ps_season.iterrows()}

    new_rows = {}
    skipped = 0
    for _, r in agg.iterrows():
        player_id = int(r["player_id"])
        ps_id = ps_map.get(player_id)
        if not ps_id:
            skipped += 1
            continue
        new_rows[ps_id] = {
            "player_season_id": ps_id,
            "player_id":        player_id,
            "season":           season,
            "edge_score":       float(r["edge_score"]) if pd.notna(r.get("edge_score")) else None,
            "stats_measured":   int(r["stats_measured"]) if pd.notna(r.get("stats_measured")) else 0,
            "games_played":     int(r["games_played"]) if pd.notna(r.get("games_played")) else 0,
            "opponent_avg_sp":  float(r["opponent_avg_sp"]) if pd.notna(r.get("opponent_avg_sp")) else None,
            "coverage_share":   float(r["coverage_share"]) if pd.notna(r.get("coverage_share")) else 0.0,
            "model_version":    MODEL_VERSION,
        }
        # Archetype sub-scores, and which job this player actually does. Only
        # defensive backs carry them.
        arch = {k: float(r[f"arch_{k}"]) for k in ARCHETYPE_SCALE
                if f"arch_{k}" in r.index and pd.notna(r.get(f"arch_{k}"))}
        if arch and max(arch.values()) > 0:
            new_rows[ps_id]["archetypes"] = {k: round(v, 2) for k, v in arch.items()}
            new_rows[ps_id]["archetype"]  = max(arch, key=arch.get)

    if skipped:
        print(f"  Skipped {skipped} players with no player_seasons row for {season}")

    # Merge with existing player_edge.json — replace rows for this season
    path = RAW_DIR / "player_edge.json"
    existing = []
    if path.exists():
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)

    # Keep rows from other seasons, replace this season
    kept = [r for r in existing if r.get("season") != season]
    combined = kept + list(new_rows.values())

    with open(path, "w", encoding="utf-8") as f:
        json.dump(combined, f, separators=(",", ":"))

    print(f"  Saved {len(new_rows)} EDGE rows for {season} ({len(combined)} total in file)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_season(season: int, api_key: str) -> None:
    print(f"\n{'='*60}")
    print(f"Computing EDGE — Season {season}")
    print(f"{'='*60}")

    print("Loading SP+ ratings...")
    sp_map = build_opponent_sp_map(season, api_key)
    print(f"  {len(sp_map)} teams with SP+ ratings")

    print("Computing offensive EDGE...")
    off_agg = compute_offensive_edge(season, sp_map)
    if off_agg.empty:
        print("  No offensive game stats found — check data/raw/stats.json")
    else:
        valid = off_agg["edge_score"].notna().sum()
        print(f"  {valid} offensive players with valid EDGE (>={MIN_GAMES} games)")
        for pg in sorted(off_agg["position_group"].unique()):
            sub = off_agg[off_agg["position_group"] == pg]["edge_score"].dropna()
            if len(sub):
                p = np.percentile(sub, [50, 90])
                print(f"    {pg}: n={len(sub)} p50={p[0]:.1f} p90={p[1]:.1f} max={sub.max():.1f}")

    print("Building team defensive context map...")
    try:
        ctx_map = build_game_context_map(api_key, season)
        print(f"  {len(ctx_map)} game-team context entries")
    except Exception as e:
        print(f"  Warning: {e}. Skipping context signal.")
        ctx_map = None

    print("Computing defensive EDGE...")
    def_agg = compute_defensive_edge(season, sp_map, ctx_map=ctx_map)
    if def_agg.empty:
        print(f"  No defensive game stats found for {season} (expected pre-2016)")
    else:
        valid = def_agg["edge_score"].notna().sum()
        print(f"  {valid} defensive players with valid EDGE (>={MIN_GAMES} games)")
        for pg in sorted(def_agg["position_group"].unique()):
            sub = def_agg[def_agg["position_group"] == pg]["edge_score"].dropna()
            if len(sub):
                p = np.percentile(sub, [50, 90])
                print(f"    {pg}: n={len(sub)} p50={p[0]:.1f} p90={p[1]:.1f} max={sub.max():.1f}")

    frames = [f for f in [off_agg, def_agg] if not f.empty]
    if not frames:
        print("  No EDGE scores computed.")
        return

    agg = pd.concat(frames, ignore_index=True)
    save_edge(agg, season)
    print(f"Season {season} EDGE complete.")


def main():
    parser = argparse.ArgumentParser(description="Compute EDGE scores from local JSON stats")
    parser.add_argument("--season", type=int, default=2025)
    parser.add_argument("--all-seasons", action="store_true", help="Run 2008-2026")
    args = parser.parse_args()

    api_key = load_api_key()
    seasons = list(range(2008, 2027)) if args.all_seasons else [args.season]
    for s in seasons:
        run_season(s, api_key)
    print("\nDone.")


if __name__ == "__main__":
    main()
