"""EDGE — Efficiency-Driven Grade per Event.

Our custom opponent-adjusted, situation-weighted player performance metric.
Stored in the player_edge table; consumed by Engine A in script 06.

What EDGE is:
  Raw EPA (Expected Points Added) per play is a solid baseline but has two
  problems for player rating: (1) garbage-time stats inflate totals for teams
  up/down 28+, and (2) a 0.3 EPA pass against a 70th-percentile defense is
  worth less than the same play against a 95th-percentile defense.

  EDGE fixes both:
    1. Situation weighting — crunch-time plays (see CRUNCH_WINDOW) count 2x.
       Garbage-time plays (see GARBAGE_WINDOW) count 0.25x.
    2. Opponent quality multiplier — each play's EPA is multiplied by
       (opponent_sp / median_sp). A play against a top-25 defense counts more.
    3. Aggregation — sum of situation-weighted, opponent-adjusted EPA,
       then divide by sqrt(plays_counted) to penalize tiny samples less
       harshly than a straight per-play average.
    4. Scale to 0–100 within each position group × season using MinMaxScaler.

Crunch time definition (CRUNCH_WINDOW):
  Score differential ≤ 8 points AND (period = 4 OR (period = 3 AND clock ≤ 120s))
  — equivalent to a one-possession game in the 4th quarter or end of 3rd.

Garbage time definition (GARBAGE_WINDOW):
  Score differential ≥ 28 points in period 3 or 4 with ≥ 8 minutes left.

Player attribution (offensive skill positions only):
  QB  — passer_player_id (passing plays) + rusher_player_id (scrambles/designed runs)
  RB  — rusher_player_id (run plays)
  WR/TE — receiver_player_id (pass receptions/targets)

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
from sklearn.preprocessing import MinMaxScaler

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.db import bulk_upsert, get_connection
from utils.api_client import load_api_key, fetch_sp_ratings_all

MODEL_VERSION = "v1.0-edge"

# Situation weight multipliers
CRUNCH_WEIGHT  = 2.0
NEUTRAL_WEIGHT = 1.0
GARBAGE_WEIGHT = 0.25

MEDIAN_SP = 0.0  # SP+ is centered around 0 by design; adjust multiplier accordingly

# Positions we compute EDGE for — includes defenders (negated EPA on sack/INT/TFL)
EDGE_POSITIONS = {"QB", "RB", "WR", "TE", "DL", "LB", "DB"}

# Minimum plays for a "valid" EDGE score (below this → NULL, use formula fallback)
MIN_PLAYS = 15


# ---------------------------------------------------------------------------
# Situation classification
# ---------------------------------------------------------------------------

def classify_situation(row) -> str:
    """Return 'crunch', 'garbage', or 'neutral' for a play row."""
    period    = row.get("period") or 0
    clock     = row.get("clock_seconds")   # seconds remaining in period (may be None)
    off_score = row.get("offense_score") or 0
    def_score = row.get("defense_score") or 0
    diff      = abs(off_score - def_score)

    # Crunch: one-possession game late
    if diff <= 8:
        if period >= 4:
            return "crunch"
        if period == 3 and clock is not None and clock <= 120:
            return "crunch"

    # Garbage: blowout late with plenty of time
    if diff >= 28 and period >= 3:
        if clock is None or clock >= 480:   # 8+ minutes left
            return "garbage"

    return "neutral"


# ---------------------------------------------------------------------------
# Opponent quality multiplier
# ---------------------------------------------------------------------------

def build_opponent_sp_map(season: int, api_key: str) -> dict:
    """Return {team_db_id: sp_rating} for a season."""
    sp_by_name = fetch_sp_ratings_all(api_key, season)
    if not sp_by_name:
        return {}

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


def opponent_multiplier(defense_team_id, sp_map: dict) -> float:
    """
    Scale factor based on how strong the opponent's defense is.
    SP+ is centered on 0 with std ~15. We add a base of 1.0 and scale
    so that a +15 SP defense gives ~1.5x and -15 gives ~0.5x.
    """
    if not defense_team_id or defense_team_id not in sp_map:
        return 1.0
    sp = sp_map[defense_team_id]
    # Clamp to [-30, 30] to prevent extreme outliers
    sp = max(-30.0, min(30.0, float(sp)))
    return 1.0 + (sp / 30.0) * 0.5   # range [0.5, 1.5]


# ---------------------------------------------------------------------------
# Load plays from DB
# ---------------------------------------------------------------------------

def load_plays(season: int) -> pd.DataFrame:
    """Load all plays for a season with team and player attribution columns."""
    sql = """
        SELECT
            p.id,
            p.game_id,
            p.offense_team_id,
            p.defense_team_id,
            p.period,
            p.clock_seconds,
            p.down,
            p.distance,
            p.yards_to_goal,
            p.offense_score,
            p.defense_score,
            p.play_type,
            p.yards_gained,
            p.epa,
            p.ppa,
            p.passer_player_id,
            p.rusher_player_id,
            p.receiver_player_id,
            p.defender_player_id
        FROM plays p
        WHERE p.season = %s
          AND p.play_type NOT IN ('Kickoff', 'Punt', 'Extra Point', 'Two Point Conversion',
                                   'Penalty', 'Timeout', 'End Period', 'End of Half',
                                   'End of Game', 'Kickoff Return (Offense)')
    """
    with get_connection() as conn:
        return pd.read_sql(sql, conn, params=(season,))


# ---------------------------------------------------------------------------
# Load player position map
# ---------------------------------------------------------------------------

def load_player_positions(season: int) -> dict:
    """Return {player_id: position_group} for players with a player_seasons row this season."""
    sql = """
        SELECT DISTINCT ps.player_id, ps.position_group
        FROM player_seasons ps
        WHERE ps.season = %s AND ps.position_group IS NOT NULL
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (season,))
        return {row[0]: row[1] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Core EDGE computation
# ---------------------------------------------------------------------------

def compute_edge(plays_df: pd.DataFrame, sp_map: dict, player_positions: dict) -> pd.DataFrame:
    """
    Compute EDGE for every player × season.

    Returns a DataFrame with columns:
      player_id, position_group, edge_score, crunch_epa, garbage_epa, plays_counted, opponent_avg_sp
    """
    if plays_df.empty:
        return pd.DataFrame()

    # Add situation column
    plays_df = plays_df.copy()
    plays_df["situation"] = plays_df.apply(classify_situation, axis=1)

    # Map situation → weight
    weight_map = {"crunch": CRUNCH_WEIGHT, "neutral": NEUTRAL_WEIGHT, "garbage": GARBAGE_WEIGHT}
    plays_df["sit_weight"] = plays_df["situation"].map(weight_map).fillna(NEUTRAL_WEIGHT)

    # Opponent multiplier
    plays_df["opp_mult"] = plays_df["defense_team_id"].apply(
        lambda d: opponent_multiplier(d, sp_map)
    )

    # Use API EPA if available; fall back to yards_gained / 10 as a rough proxy
    plays_df["raw_epa"] = plays_df["epa"].fillna(plays_df["ppa"]).fillna(
        plays_df["yards_gained"].fillna(0) / 10.0
    )

    # Weighted, opponent-adjusted EPA per play
    plays_df["adj_epa"] = plays_df["raw_epa"] * plays_df["sit_weight"] * plays_df["opp_mult"]

    # Build player-play attribution for EDGE_POSITIONS
    records = []

    # Helper to attribute plays to a player
    def attribute(sub_df, id_col, play_type_filter=None):
        df = sub_df.copy()
        if play_type_filter:
            df = df[df["play_type"].isin(play_type_filter)]
        df = df.dropna(subset=[id_col])
        df = df[df[id_col] > 0]
        df["player_id"] = df[id_col].astype(int)
        return df

    # QB: all pass plays (passer) + QB run plays (rusher where position=QB)
    qb_ids = {pid for pid, pg in player_positions.items() if pg == "QB"}
    pass_plays = attribute(plays_df, "passer_player_id",
                           ["Pass Completion", "Pass Reception", "Pass Incompletion",
                            "Interception", "Interception Return", "Sack"])
    qb_rush = attribute(plays_df, "rusher_player_id",
                        ["Rush", "Rushing Touchdown"])
    qb_rush = qb_rush[qb_rush["player_id"].isin(qb_ids)]
    records.extend(pass_plays.assign(player_id=pass_plays["player_id"])[
        ["player_id", "adj_epa", "situation", "opp_mult"]].to_dict("records"))
    records.extend(qb_rush[["player_id", "adj_epa", "situation", "opp_mult"]].to_dict("records"))

    # RB: rush plays only (non-QB rushers)
    rb_rush = attribute(plays_df, "rusher_player_id", ["Rush", "Rushing Touchdown"])
    rb_rush = rb_rush[~rb_rush["player_id"].isin(qb_ids)]
    records.extend(rb_rush[["player_id", "adj_epa", "situation", "opp_mult"]].to_dict("records"))

    # WR/TE: reception plays
    rec_plays = attribute(plays_df, "receiver_player_id",
                          ["Pass Completion", "Pass Reception", "Receiving Touchdown"])
    records.extend(rec_plays[["player_id", "adj_epa", "situation", "opp_mult"]].to_dict("records"))

    # DL/LB/DB: defensive plays — use offense_team_id SP+ for opponent quality
    # For defenders, a sack/INT is *good* — negate EPA so positive = good for defender
    # Opponent multiplier uses offense_team's SP+ (they faced a strong offense = multiplied reward)
    if "defender_player_id" in plays_df.columns:
        def_plays = attribute(plays_df, "defender_player_id",
                              ["Sack", "Interception", "Pass Interception Return",
                               "Interception Return Touchdown", "Fumble", "Fumble Recovery (Opponent)",
                               "Fumble Return Touchdown"])
        if not def_plays.empty:
            # Use offense_team SP+ as opponent quality for defenders
            def_plays = def_plays.copy()
            def_plays["def_opp_mult"] = def_plays["offense_team_id"].apply(
                lambda oid: opponent_multiplier(oid, sp_map)
            )
            # Negate EPA: a sack with EPA=-3 means defender contributed +3 value
            def_plays["def_adj_epa"] = -def_plays["raw_epa"] * def_plays["sit_weight"] * def_plays["def_opp_mult"]
            def_records = def_plays[["player_id", "def_adj_epa", "situation", "def_opp_mult"]].copy()
            def_records = def_records.rename(columns={"def_adj_epa": "adj_epa", "def_opp_mult": "opp_mult"})
            records.extend(def_records.to_dict("records"))

    if not records:
        return pd.DataFrame()

    attr_df = pd.DataFrame(records)

    # Aggregate per player
    def agg_player(grp):
        total_adj_epa  = grp["adj_epa"].sum()
        n              = len(grp)
        crunch_epa     = grp[grp["situation"] == "crunch"]["adj_epa"].sum()
        garbage_epa    = grp[grp["situation"] == "garbage"]["adj_epa"].sum()
        avg_opp_sp     = sp_map.get(0, 0)   # placeholder; filled below

        # Penalize tiny samples: divide by sqrt(n) to reward volume without
        # over-indexing on a handful of big plays
        edge = total_adj_epa / max(np.sqrt(n), 1.0)

        return pd.Series({
            "edge_score":    edge,
            "crunch_epa":    crunch_epa,
            "garbage_epa":   garbage_epa,
            "plays_counted": n,
        })

    agg = attr_df.groupby("player_id").apply(agg_player).reset_index()

    # Add opponent avg SP (mean opp_mult → back-convert to SP for display)
    opp_avg = attr_df.groupby("player_id")["opp_mult"].mean().reset_index()
    opp_avg.columns = ["player_id", "avg_opp_mult"]
    opp_avg["opponent_avg_sp"] = (opp_avg["avg_opp_mult"] - 1.0) * 60.0  # inverse of our formula
    agg = agg.merge(opp_avg[["player_id", "opponent_avg_sp"]], on="player_id", how="left")

    # Attach position group
    agg["position_group"] = agg["player_id"].map(player_positions)

    # Null out tiny samples — not enough data to trust
    agg.loc[agg["plays_counted"] < MIN_PLAYS, "edge_score"] = None

    return agg


# ---------------------------------------------------------------------------
# Scale EDGE to 0–100 within position group
# ---------------------------------------------------------------------------

def scale_edge(agg: pd.DataFrame) -> pd.DataFrame:
    agg = agg.copy()
    agg["edge_scaled"] = None

    for pg in EDGE_POSITIONS:
        mask = (agg["position_group"] == pg) & agg["edge_score"].notna()
        if mask.sum() < 3:
            continue
        vals = agg.loc[mask, "edge_score"].values.reshape(-1, 1)
        scaler = MinMaxScaler(feature_range=(0, 100))
        agg.loc[mask, "edge_scaled"] = scaler.fit_transform(vals).flatten()

    return agg


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert_edge(agg: pd.DataFrame, season: int) -> None:
    # Resolve player_season_id: {player_id: player_season_id} for this season
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
            "edge_scaled":      float(r["edge_scaled"]) if pd.notna(r.get("edge_scaled")) else None,
            "crunch_epa":       float(r["crunch_epa"]) if pd.notna(r.get("crunch_epa")) else None,
            "garbage_epa":      float(r["garbage_epa"]) if pd.notna(r.get("garbage_epa")) else None,
            "plays_counted":    int(r["plays_counted"]) if pd.notna(r.get("plays_counted")) else 0,
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
    bulk_upsert("player_edge", rows, conflict_col="player_season_id")
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

    print("Loading plays from DB...")
    plays_df = load_plays(season)
    print(f"  {len(plays_df)} plays loaded")

    if plays_df.empty:
        print("  No plays found. Run script 01 with --plays first.")
        return

    print("Loading player positions...")
    player_positions = load_player_positions(season)
    print(f"  {len(player_positions)} players with stats")

    print("Computing EDGE scores...")
    agg = compute_edge(plays_df, sp_map, player_positions)
    if agg.empty:
        print("  No EDGE scores computed (check player attribution in plays table)")
        return
    print(f"  {agg['edge_score'].notna().sum()} players with valid EDGE (>={MIN_PLAYS} plays)")

    agg = scale_edge(agg)
    upsert_edge(agg, season)
    print(f"Season {season} EDGE complete.")


def main():
    parser = argparse.ArgumentParser(description="Compute EDGE scores from play-by-play")
    parser.add_argument("--season", type=int, default=2025)
    parser.add_argument("--all-seasons", action="store_true")
    args = parser.parse_args()

    api_key = load_api_key()

    seasons = list(range(2021, 2026)) if args.all_seasons else [args.season]
    for s in seasons:
        run_season(s, api_key)

    print("\nDone.")


if __name__ == "__main__":
    main()
