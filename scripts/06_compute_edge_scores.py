"""EDGE — Efficiency-Driven Grade per Event  (v4.0).

Formula (all positions):
    edge_score = sum_over_games(stat_composite_i × opp_mult_i) / sqrt(games_played)

Offensive (QB/RB/WR/TE):
    stat_composite = weighted sum of per-game stats (yards, TDs, INTs).
    opp_mult scales by the opponent's *defensive* SP+ rating for that game.

Defensive (EDGE/DL/LB/CB/S):
    stat_composite = weighted sum of per-game defensive stats
                     (sacks, hurries, PBUs, INTs, tackles, TFLs).
    All positions use all 6 stat categories; weights differ by role.
    opp_mult scales by the opponent's *offensive* SP+ rating for that game.

stats_measured: season total of primary countable stats (used as minimum
    threshold in script 06 and for display). Offense: primary volume stat.
    Defense: sum of all tracked stats across the season.

games_played: count of distinct games where the player recorded any stats.

Usage:
    python scripts/08_compute_edge_score.py              # 2025
    python scripts/08_compute_edge_score.py --season 2024
    python scripts/08_compute_edge_score.py --all-seasons
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.db import bulk_upsert, get_connection
from utils.api_client import load_api_key, fetch_sp_ratings_all, fetch_sp_ratings_breakdown, fetch_game_team_stats

MODEL_VERSION = "v4.1-team-context"

# Offensive positions: per-game game_aggregate stats × opp defensive SP+
OFF_EDGE_POSITIONS = {"QB", "RB", "WR", "TE"}

# Defensive positions: per-game game_aggregate stats × opp offensive SP+
DEF_EDGE_POSITIONS = {"EDGE", "DL", "LB", "CB", "S", "DB"}

EDGE_POSITIONS = OFF_EDGE_POSITIONS | DEF_EDGE_POSITIONS

# Minimum games for a valid EDGE score (both offense and defense)
MIN_GAMES = 3

# ---------------------------------------------------------------------------
# Stat composite weights
# ---------------------------------------------------------------------------

# Offensive per-game stat composite weights.
# Yards are the base unit; TDs add a large bonus; INTs are penalized.
OFFENSIVE_COMPOSITE = {
    "QB": {
        "passingYDS":  1.0,
        "rushingYDS":  0.7,
        "passingTD":  25.0,
        "rushingTD":  20.0,
        "passingINT": -20.0,
    },
    "RB": {
        "rushingYDS":   1.0,
        "receivingYDS": 0.9,
        "rushingTD":   20.0,
        "receivingTD": 20.0,
    },
    "WR": {
        "receivingYDS": 1.0,
        "receivingTD": 25.0,
        "receivingREC": 2.0,
    },
    "TE": {
        "receivingYDS": 1.0,
        "receivingTD": 25.0,
        "receivingREC": 2.5,
    },
}

# Primary stat keys for computing stats_measured (season volume qualifier).
OFFENSE_PRIMARY_STATS = {
    "QB": ["passingATT", "rushingCAR"],
    "RB": ["rushingCAR", "receivingREC"],
    "WR": ["receivingREC"],
    "TE": ["receivingREC"],
}

# Defensive per-game stat composite weights.
# All positions use all 6 categories; weights reflect positional emphasis.
DEF_STAT_WEIGHTS = {
    # tot weight is intentionally low — raw tackle counts conflate "made the stop" with
    # "was standing there after teammates shed blockers". TFLs, sacks, and hurries represent
    # genuine disruption.
    # Coverage positions (CB/S/DB): INTs are elite plays worth ~3× a PBU. A PBU just means
    # the pass was incomplete (could be out-of-bounds, dropped, etc.); an INT means you
    # outplayed the receiver AND the QB. PBU=2.0 prevents tackle-volume DBs from dominating.
    "EDGE": {"sacks": 7.0, "hur": 2.5, "tfl": 4.0, "tot": 0.3, "ints":  4.0, "pbu": 1.5},
    "DL":   {"sacks": 6.0, "hur": 2.0, "tfl": 4.0, "tot": 0.4, "ints":  3.0, "pbu": 0.5},
    "LB":   {"sacks": 5.5, "hur": 1.5, "tfl": 4.0, "tot": 0.6, "ints":  7.0, "pbu": 2.0},
    "CB":   {"sacks": 2.5, "hur": 1.0, "tfl": 2.0, "tot": 0.3, "ints": 12.0, "pbu": 2.0},
    "S":    {"sacks": 3.0, "hur": 1.5, "tfl": 3.0, "tot": 0.4, "ints": 10.0, "pbu": 2.0},
    "DB":   {"sacks": 2.5, "hur": 1.0, "tfl": 2.0, "tot": 0.3, "ints": 11.0, "pbu": 2.0},
}


# ---------------------------------------------------------------------------
# Stat coercion helpers
# ---------------------------------------------------------------------------

def _coerce_float(val) -> float:
    """Parse a stat value that may be string, int, float, or None."""
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


# ---------------------------------------------------------------------------
# SP+ opponent quality map
# ---------------------------------------------------------------------------

def build_opponent_sp_map(season: int, api_key: str) -> dict:
    """Return {team_db_id: {"overall", "offense", "defense"}} for a season."""
    sp_by_name = fetch_sp_ratings_breakdown(api_key, season)
    if not sp_by_name:
        flat = fetch_sp_ratings_all(api_key, season)
        sp_by_name = {k: {"overall": v, "offense": 0.0, "defense": 0.0} for k, v in flat.items()}

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, school FROM teams")
        rows = cur.fetchall()

    result = {}
    for db_id, school in rows:
        sp = sp_by_name.get(school.lower())
        if sp is not None:
            result[db_id] = sp
    return result


def _season_means(sp_map: dict) -> tuple:
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


def opponent_multiplier(opp_team_id, sp_map: dict, side: str,
                        season_means: tuple) -> float:
    """
    Opponent quality multiplier — asymmetric, std-normalized.

    Normalization: z = (val - mean) / std  → clipped to [-2, +2]
    Bonus (hard opponent):  mult = 1.0 + z * 0.35   → up to 1.70
    Penalty (weak opponent): mult = 1.0 + z * 0.20  → down to 0.60

    Asymmetry rationale: a DB making an INT against a weak offense still
    demonstrates real skill — the defender can't choose who throws at them.
    But facing an elite offense should be rewarded more than facing a weak
    one is penalized. This prevents the symmetric 0.30 floor that destroys
    otherwise strong G5 seasons.

    side="defense": for offensive players — lower opp defense SP+ = harder.
    side="offense": for defensive players — higher opp offense SP+ = harder.
    """
    if not opp_team_id or opp_team_id not in sp_map:
        return 1.0
    sp_entry = sp_map[opp_team_id]
    mean_off, mean_def, mean_ovr = season_means

    if isinstance(sp_entry, dict):
        if side == "defense":
            val = sp_entry.get("defense", mean_def)
            # lower defense SP+ = stronger defense = harder for offense
            std = max(mean_def * 0.26, 1.0)   # ~std of SP+ defense
            z = (mean_def - val) / std
        else:
            val = sp_entry.get("offense", mean_off)
            # higher offense SP+ = stronger offense = harder for defense
            std = max(mean_off * 0.26, 1.0)   # ~std of SP+ offense (~7/27 ≈ 0.26)
            z = (val - mean_off) / std
    else:
        sp = float(sp_entry)
        z = (sp - mean_ovr) / 30.0

    z = max(-2.0, min(2.0, z))
    if z >= 0:
        return 1.0 + z * 0.35   # bonus: up to 1.70 for elite opponents
    else:
        return 1.0 + z * 0.20   # penalty: down to 0.60 for weakest opponents


# ---------------------------------------------------------------------------
# Team defensive context — yards/points allowed per game
# ---------------------------------------------------------------------------

def build_game_context_map(api_key: str, season: int) -> dict:
    """Build a lookup from (db_game_id, defending_team_db_id) → opp offensive stats.

    For each game, the "context" for a defensive player is how many yards/points
    their team allowed — i.e., the opponent's offensive stats in that game.

    Returns: {(game_db_id, def_team_db_id): {"pass_yds": ..., "rush_yds": ..., "points": ...}}
    """
    # Load game team stats from API (cached after first call)
    raw = fetch_game_team_stats(api_key, season)  # {cfb_game_id: {cfb_team_id: {stats}}}

    if not raw:
        return {}

    with get_connection() as conn:
        cur = conn.cursor()
        # Map cfb_api_id → db_id for games
        cur.execute("SELECT id, cfb_api_id FROM games WHERE season = %s", (season,))
        game_cfb_to_db = {r[1]: r[0] for r in cur.fetchall()}
        # Map cfb_api_id → db_id for teams
        cur.execute("SELECT id, cfb_api_id FROM teams WHERE cfb_api_id IS NOT NULL")
        team_cfb_to_db = {r[1]: r[0] for r in cur.fetchall()}

    def _int_stat(d, key):
        v = d.get(key)
        if v is None:
            return None
        try:
            return float(str(v).split("-")[0])
        except (ValueError, TypeError):
            return None

    result = {}
    for cfb_game_id, teams_dict in raw.items():
        db_game_id = game_cfb_to_db.get(cfb_game_id)
        if not db_game_id:
            continue
        # For each team, store OPPONENT'S offensive output as this team's defensive context
        team_ids = list(teams_dict.keys())
        if len(team_ids) != 2:
            continue
        for i, def_cfb_tid in enumerate(team_ids):
            off_cfb_tid = team_ids[1 - i]   # the other team = offense
            def_db_tid = team_cfb_to_db.get(def_cfb_tid)
            if not def_db_tid:
                continue
            opp_stats = teams_dict[off_cfb_tid]
            pass_yds = _int_stat(opp_stats, "netPassingYards")
            rush_yds = _int_stat(opp_stats, "rushingYards")
            points   = opp_stats.get("_points")
            if points is not None:
                try:
                    points = float(points)
                except (ValueError, TypeError):
                    points = None
            result[(db_game_id, def_db_tid)] = {
                "pass_yds": pass_yds,
                "rush_yds": rush_yds,
                "points":   points,
            }
    return result


def _compute_season_defensive_means(ctx_map: dict) -> dict:
    """Compute mean/std of pass_yds and rush_yds across all games (both teams)."""
    pass_vals = [v["pass_yds"] for v in ctx_map.values() if v["pass_yds"] is not None]
    rush_vals = [v["rush_yds"] for v in ctx_map.values() if v["rush_yds"] is not None]
    pt_vals   = [v["points"]   for v in ctx_map.values() if v["points"]   is not None]
    def _stats(vals):
        if not vals:
            return (200.0, 60.0)  # fallback means
        return (float(np.mean(vals)), max(float(np.std(vals)), 1.0))
    return {
        "pass": _stats(pass_vals),
        "rush": _stats(rush_vals),
        "pts":  _stats(pt_vals),
    }


# How much defensive context blends into the stat composite by position group.
# Range [0.0, 1.0]: 0 = no context adjustment, 0.25 = 25% of score modified by context.
# Context cap: max adjustment (bonus or penalty) of ±30% to individual game composite.
DEF_CONTEXT_WEIGHTS = {
    # Coverage positions: pass yards allowed is primary signal
    "CB":   {"pass": 0.55, "rush": 0.05, "pts": 0.40},
    "S":    {"pass": 0.45, "rush": 0.15, "pts": 0.40},
    "DB":   {"pass": 0.50, "rush": 0.10, "pts": 0.40},
    # Run stoppers: rush yards allowed is primary
    "DL":   {"pass": 0.15, "rush": 0.55, "pts": 0.30},
    "EDGE": {"pass": 0.25, "rush": 0.45, "pts": 0.30},
    # LB: balanced pass/rush
    "LB":   {"pass": 0.30, "rush": 0.40, "pts": 0.30},
}

# Blend fraction: how much the context modifier adjusts the game composite.
# 0.35 means context can adjust the per-game composite by up to ±10.5% (0.35 × 0.30).
# This rewards players on strong defensive units (e.g. SEC lockdown CBs) without
# over-weighting team performance over individual stats.
DEF_CONTEXT_BLEND = 0.35


def def_context_modifier(pg: str, game_db_id: int, player_team_db_id: int,
                          ctx_map: dict, ctx_means: dict) -> float:
    """
    Returns a context multiplier (near 1.0) for a defensive player's game.

    Good defensive game (low yards/pts allowed) → > 1.0 (bonus)
    Bad defensive game (high yards/pts allowed) → < 1.0 (penalty)
    Missing data → 1.0 (neutral)

    The modifier is blended with 1.0 at DEF_CONTEXT_BLEND so it adjusts
    the stat composite by at most ±30%.
    """
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
        # More yards allowed = worse = lower z (penalty)
        z_pass = (mean_p - pass_yds) / std_p

    rush_yds = ctx.get("rush_yds")
    if rush_yds is not None:
        mean_r, std_r = ctx_means["rush"]
        z_rush = (mean_r - rush_yds) / std_r

    pts = ctx.get("points")
    if pts is not None:
        mean_pts, std_pts = ctx_means["pts"]
        z_pts = (mean_pts - pts) / std_pts

    # Weighted z-score
    z_total = (z_pass * weights["pass"] + z_rush * weights["rush"] + z_pts * weights["pts"])
    # Clip to ±1.5 std, scale to ±0.30 range
    z_total = max(-1.5, min(1.5, z_total))
    context_adj = z_total * 0.20   # max ±0.30 modifier

    # Final modifier: blend with 1.0
    raw_mod = 1.0 + context_adj
    return 1.0 - DEF_CONTEXT_BLEND + DEF_CONTEXT_BLEND * raw_mod


# ---------------------------------------------------------------------------
# Shared game-stats query
# ---------------------------------------------------------------------------

def _load_game_stats(season: int, positions: set) -> pd.DataFrame:
    """Load per-game stat rows from stats table for a set of position groups."""
    sql = """
        SELECT
            ps.player_id,
            ps.position_group,
            ps.team_id           AS player_team_id,
            s.game_id,
            s.data,
            g.home_team_id,
            g.away_team_id
        FROM stats s
        JOIN player_seasons ps ON ps.id = s.player_season_id
        JOIN games g ON g.id = s.game_id
        WHERE ps.season = %s
          AND ps.position_group = ANY(%s)
          AND s.stat_type = 'game_aggregate'
          AND s.game_id IS NOT NULL
    """
    with get_connection() as conn:
        return pd.read_sql(sql, conn, params=(season, list(positions)))


# ---------------------------------------------------------------------------
# Offensive EDGE — per-game stat composite × opp defensive SP+
# ---------------------------------------------------------------------------

def _off_stat_composite(pg: str, stats: dict) -> float:
    weights = OFFENSIVE_COMPOSITE.get(pg, {})
    total = 0.0
    for key, w in weights.items():
        total += _coerce_float(stats.get(key)) * w
    return total


def _off_stats_measured(pg: str, stats: dict) -> float:
    """Season total of primary countable stats for minimum-threshold check."""
    if pg == "QB":
        # API stores attempts as "passingC/ATT" (e.g. "21/23") — extract the denominator
        catt = stats.get("passingC/ATT") or stats.get("passingATT")
        if catt and "/" in str(catt):
            att = _coerce_int(str(catt).split("/")[-1])
        else:
            att = _coerce_int(catt)
        return float(att + _coerce_int(stats.get("rushingCAR")))
    keys = OFFENSE_PRIMARY_STATS.get(pg, [])
    return sum(_coerce_float(stats.get(k)) for k in keys)


def compute_offensive_edge(season: int, sp_map: dict) -> pd.DataFrame:
    """
    Per-game opponent-adjusted EDGE for QB/RB/WR/TE.
    Returns DataFrame: player_id, position_group, edge_score, games_played,
                       stats_measured, opponent_avg_sp.
    """
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

        opp_mult = opponent_multiplier(opp_team, sp_map, side="defense",
                                       season_means=season_means)
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
        total_adj = grp["adj_score"].sum()
        edge      = total_adj / max(np.sqrt(n), 1.0)
        avg_mult  = grp["opp_mult"].mean()
        return pd.Series({
            "edge_score":      edge,
            "games_played":    n,
            "stats_measured":  grp["primary_vol"].sum(),
            "opponent_avg_sp": (avg_mult - 1.0) * 60.0,
            "position_group":  grp["position_group"].iloc[0],
        })

    result = df.groupby("player_id").apply(agg).reset_index()
    result.loc[result["games_played"] < MIN_GAMES, "edge_score"] = None
    return result


# ---------------------------------------------------------------------------
# Defensive EDGE — per-game stat composite × opp offensive SP+
# ---------------------------------------------------------------------------

def _def_stat_composite(pg: str, stats: dict) -> tuple[float, float]:
    """Returns (adj_composite, raw_vol) for one game.

    raw_vol is the sum of all tracked stat counts — used for stats_measured.
    """
    weights = DEF_STAT_WEIGHTS.get(pg)
    if not weights:
        return 0.0, 0.0

    tot   = _coerce_int(stats.get("defensiveTOT"))
    sacks = _coerce_int(stats.get("defensiveSACKS"))
    tfl   = _coerce_int(stats.get("defensiveTFL"))
    hur   = _coerce_int(stats.get("defensiveQB HUR")) or _coerce_int(stats.get("defensiveQBH"))
    pbu   = _coerce_int(stats.get("defensivePD")) or _coerce_int(stats.get("defensivePBU"))
    ints  = _coerce_int(stats.get("interceptionsINT")) or _coerce_int(stats.get("defensiveINT"))

    score = (
        tot   * weights["tot"]  +
        sacks * weights["sacks"] +
        tfl   * weights["tfl"] +
        hur   * weights["hur"] +
        pbu   * weights["pbu"] +
        ints  * weights["ints"]
    )
    vol = tot + sacks + tfl + hur + pbu + ints
    return score, float(vol)


def compute_defensive_edge(season: int, sp_map: dict,
                           ctx_map: dict | None = None) -> pd.DataFrame:
    """
    Per-game opponent-adjusted EDGE for EDGE/DL/LB/CB/S/DB.

    Two-signal adjustment per game:
    1. Opponent SP+ quality multiplier (existing) — rewards playing tougher offenses
    2. Team defensive context modifier (new) — uses actual yards/points allowed
       to credit lockdown defenders who play on good defenses even with low stats

    Returns DataFrame: player_id, position_group, edge_score, games_played,
                       stats_measured, opponent_avg_sp.
    """
    rows = _load_game_stats(season, DEF_EDGE_POSITIONS)
    if rows.empty:
        return pd.DataFrame()

    season_means = _season_means(sp_map)
    ctx_means = _compute_season_defensive_means(ctx_map) if ctx_map else {}
    records = []

    for _, r in rows.iterrows():
        pg       = r["position_group"]
        my_team  = r["player_team_id"]
        game_id  = r["game_id"]
        opp_team = r["away_team_id"] if my_team == r["home_team_id"] else r["home_team_id"]
        stats    = r["data"] if isinstance(r["data"], dict) else {}

        raw, vol = _def_stat_composite(pg, stats)
        if raw <= 0:
            continue

        # Signal 1: opponent offensive quality (SP+)
        opp_mult = opponent_multiplier(opp_team, sp_map, side="offense",
                                       season_means=season_means)

        # Signal 2: team defensive context — yards/points allowed this game
        ctx_mult = 1.0
        if ctx_map and ctx_means and my_team is not None and game_id is not None:
            try:
                ctx_mult = def_context_modifier(
                    pg, int(game_id), int(my_team), ctx_map, ctx_means)
            except (TypeError, ValueError):
                ctx_mult = 1.0

        records.append({
            "player_id":      int(r["player_id"]),
            "position_group": pg,
            "adj_score":      raw * opp_mult * ctx_mult,
            "opp_mult":       opp_mult,
            "raw_vol":        vol,
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    def agg(grp):
        n         = len(grp)
        total_adj = grp["adj_score"].sum()
        edge      = total_adj / max(np.sqrt(n), 1.0)
        avg_mult  = grp["opp_mult"].mean()
        return pd.Series({
            "edge_score":      edge,
            "games_played":    n,
            "stats_measured":  grp["raw_vol"].sum(),
            "opponent_avg_sp": (avg_mult - 1.0) * 60.0,
            "position_group":  grp["position_group"].iloc[0],
        })

    result = df.groupby("player_id").apply(agg).reset_index()
    result.loc[result["games_played"] < MIN_GAMES, "edge_score"] = None
    return result


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert_edge(agg: pd.DataFrame, season: int) -> None:
    player_ids = [int(r["player_id"]) for _, r in agg.iterrows()]
    ps_id_map: dict = {}
    if player_ids:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT player_id, id FROM player_seasons WHERE season = %s AND player_id = ANY(%s)",
                (season, player_ids)
            )
            for pid, ps_id in cur.fetchall():
                ps_id_map[pid] = ps_id

    rows = []
    skipped = 0
    for _, r in agg.iterrows():
        player_id = int(r["player_id"])
        ps_id = ps_id_map.get(player_id)
        if not ps_id:
            skipped += 1
            continue
        rows.append({
            "player_season_id": ps_id,
            "season":           season,
            "edge_score":       float(r["edge_score"]) if pd.notna(r.get("edge_score")) else None,
            "stats_measured":   int(r["stats_measured"]) if pd.notna(r.get("stats_measured")) else 0,
            "games_played":     int(r["games_played"]) if pd.notna(r.get("games_played")) else 0,
            "opponent_avg_sp":  float(r["opponent_avg_sp"]) if pd.notna(r.get("opponent_avg_sp")) else None,
            "model_version":    MODEL_VERSION,
        })

    if not rows:
        print("  No EDGE rows to upsert")
        return

    if skipped:
        print(f"  Skipped {skipped} players with no player_seasons row for {season}")

    seen = {r["player_season_id"]: r for r in rows}
    rows = list(seen.values())
    bulk_upsert("player_edge", rows, conflict_col="player_season_id",
                conflict_where="player_season_id IS NOT NULL")
    print(f"  Upserted {len(rows)} EDGE rows")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_season(season: int, api_key: str) -> None:
    print(f"\n{'='*60}")
    print(f"Computing EDGE — Season {season}")
    print(f"{'='*60}")

    print("Loading SP+ ratings for opponent quality...")
    sp_map = build_opponent_sp_map(season, api_key)
    print(f"  {len(sp_map)} teams have SP+ ratings")

    print("Computing offensive EDGE (per-game stat composite × opp defensive SP+)...")
    off_agg = compute_offensive_edge(season, sp_map)
    if off_agg.empty:
        print("  Warning: no offensive game_aggregate stats found. Run script 01 first.")
    else:
        valid = off_agg["edge_score"].notna().sum()
        print(f"  {valid} offensive players with valid EDGE (>={MIN_GAMES} games)")
        for pg in sorted(off_agg["position_group"].unique()):
            sub = off_agg[off_agg["position_group"] == pg]["edge_score"].dropna()
            if len(sub):
                p50 = np.percentile(sub, 50)
                p90 = np.percentile(sub, 90)
                print(f"    {pg}: n={len(sub)} p50={p50:.1f} p90={p90:.1f} max={sub.max():.1f}")

    print("Building team defensive context map (game-level yards/points allowed)...")
    try:
        ctx_map = build_game_context_map(api_key, season)
        print(f"  {len(ctx_map)} game-team context entries loaded")
    except Exception as e:
        print(f"  Warning: could not build context map: {e}. Skipping context signal.")
        ctx_map = None

    print("Computing defensive EDGE (per-game stat composite × opp offensive SP+ × team context)...")
    def_agg = compute_defensive_edge(season, sp_map, ctx_map=ctx_map)
    if def_agg.empty:
        print("  Warning: no defensive game_aggregate stats found. Run script 01 first.")
    else:
        valid = def_agg["edge_score"].notna().sum()
        print(f"  {valid} defensive players with valid EDGE (>={MIN_GAMES} games)")
        for pg in sorted(def_agg["position_group"].unique()):
            sub = def_agg[def_agg["position_group"] == pg]["edge_score"].dropna()
            if len(sub):
                p50 = np.percentile(sub, 50)
                p90 = np.percentile(sub, 90)
                print(f"    {pg}: n={len(sub)} p50={p50:.1f} p90={p90:.1f} max={sub.max():.1f}")

    frames = [f for f in [off_agg, def_agg] if not f.empty]
    if not frames:
        print("  No EDGE scores computed.")
        return

    agg = pd.concat(frames, ignore_index=True)
    upsert_edge(agg, season)
    print(f"Season {season} EDGE complete.")


def main():
    parser = argparse.ArgumentParser(description="Compute EDGE scores from per-game stats")
    parser.add_argument("--season", type=int, default=2025)
    parser.add_argument("--all-seasons", action="store_true")
    args = parser.parse_args()

    api_key = load_api_key()
    seasons = list(range(2008, 2026)) if args.all_seasons else [args.season]
    for s in seasons:
        run_season(s, api_key)

    print("\nDone.")


if __name__ == "__main__":
    main()
