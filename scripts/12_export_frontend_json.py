"""Export pipeline data → static JSON files for cfb-analytics-app/data/.

Reads from data/raw/ (API harvest) and data/computed/ (ratings engine output).
No database required.

Exports:
  players.json              — all rated players (ratings, SHAP, recruiting, team)
  teams.json                — all teams with avg rating, player count
  team_ratings.json         — team OVR + sub-score splits
  ratings_by_position.json  — top 50 per position group
  similar_players_{season}.json — precomputed z-score similarity (OVR >= 55, per season)
  rosters.json              — all team rosters by season  {team_id: {season: [...]}}
  schedules.json            — all games by team/season    {team_id: {season: [...]}}
  transfers.json            — all portal moves by team    {team_id: [...]}
  team_history.json         — year-by-year progression    {team_id: [{season,...}]}
  research/index.json       — published findings index

Usage:
    python scripts/12_export_frontend_json.py
    python scripts/12_export_frontend_json.py --season 2024
    python scripts/12_export_frontend_json.py --output /custom/path
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw, read_computed, read_ratings

DEFAULT_OUTPUT = Path(__file__).parent.parent.parent / "cfb-analytics-app" / "data"
CURRENT_SEASON = 2026
TOP_N_PER_POSITION = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_nan(o):
    """Recursively replace float NaN/inf with None — literal NaN is invalid JSON
    and breaks browser fetch().json(). pandas merges produce NaN for unmatched rows.
    Handles both python float and numpy float (np.float64) NaN."""
    if isinstance(o, dict):
        return {k: _clean_nan(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean_nan(v) for v in o]
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    # numpy scalar (np.float64 etc.) — unwrap then check
    if hasattr(o, "item"):
        try:
            v = o.item()
            if isinstance(v, float):
                return v if math.isfinite(v) else None
            return v
        except (ValueError, TypeError):
            return o
    return o


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_clean_nan(data), f, separators=(",", ":"), default=_json_default, allow_nan=False)
    size_kb = path.stat().st_size / 1024
    n = len(data) if isinstance(data, (list, dict)) else "?"
    print(f"  Wrote {path.name} ({size_kb:.1f} KB, {n} items)")


def _json_default(o):
    import decimal
    if isinstance(o, decimal.Decimal):
        return float(o)
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"Object of type {type(o)} is not JSON serializable")


def _parse_shap(val) -> dict:
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            pass
    return {}


def _f(val, default=None):
    """Safe float conversion."""
    try:
        return float(val) if val is not None and not (isinstance(val, float) and math.isnan(val)) else default
    except (TypeError, ValueError):
        return default


def _i(val, default=None):
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Load shared DataFrames once
# ---------------------------------------------------------------------------

def load_tables() -> dict:
    """Load all raw and computed tables into memory. Called once per run."""
    print("Loading source tables...")
    t = {
        "players":        read_raw("players"),
        "player_seasons": read_raw("player_seasons"),
        "teams":          read_raw("teams"),
        "games":          read_raw("games"),
        "stats":          read_raw("stats"),
        "recruiting":     read_raw("recruiting"),
        "transfers":      read_raw("transfers"),
        "player_edge":    read_raw("player_edge"),
        "research_cache": read_raw("research_cache"),
        "ratings":          read_ratings("edge"),   # engine-filtered: see read_ratings
        "team_ratings":     read_computed("team_ratings"),
        "team_season_stats": read_computed("team_season_stats"),
    }
    for name, df in t.items():
        print(f"  {name:<20} {len(df):>8} rows")
    return t


# ---------------------------------------------------------------------------
# Export: players.json
# ---------------------------------------------------------------------------

def export_players(T: dict, output_dir: Path, season: int) -> None:
    rat = T["ratings"]
    ps  = T["player_seasons"]
    pl  = T["players"]
    tm  = T["teams"]
    rec = T["recruiting"]
    edge = T["player_edge"]

    if rat.empty or ps.empty:
        print("  players.json: no ratings data — skipping")
        return

    # Filter to season
    ratings_season = rat[rat["season"] == season].copy()
    if ratings_season.empty:
        print(f"  players.json: no ratings for season {season}")
        return

    # ratings.player_id is the Supabase internal PK — NOT the CFB API player id.
    # The correct CFB API player id lives in player_seasons.player_id.
    # Join path: ratings.player_season_id → player_seasons.id → player_seasons.player_id → players.id
    ps_slim = ps[["id", "player_id", "position_group", "year", "team_id"]] \
                .rename(columns={"id": "ps_id", "player_id": "cfb_player_id", "team_id": "ps_team_id"})
    ratings_season = ratings_season.merge(ps_slim, left_on="player_season_id", right_on="ps_id", how="left")
    ratings_season = ratings_season.rename(columns={"ps_team_id": "team_id"})

    # players — join on cfb_player_id (= players.id)
    pl_slim = pl[["id", "name", "position", "height_in", "weight_lbs", "hometown_state"]] \
                .rename(columns={"id": "pl_id"})
    ratings_season = ratings_season.merge(pl_slim, left_on="cfb_player_id", right_on="pl_id", how="left")

    # teams — school/conference/color (use resolved team_id)
    tm_slim = tm[["id", "school", "abbreviation", "conference", "color", "logo_url"]] \
                .rename(columns={"id": "tm_id"})
    ratings_season = ratings_season.merge(tm_slim, left_on="team_id", right_on="tm_id", how="left")

    # Recruiting — pull stars/composite from recruiting table (ratings.stars is often None)
    if not rec.empty:
        rec_best = rec.sort_values("composite_score", ascending=False, na_position="last") \
                      .drop_duplicates(subset=["player_id"], keep="first") \
                      [["player_id", "recruit_year", "stars", "composite_score"]] \
                      .rename(columns={"player_id": "rec_pid", "stars": "rec_stars",
                                       "composite_score": "rec_composite"})
        ratings_season = ratings_season.merge(rec_best, left_on="cfb_player_id", right_on="rec_pid", how="left")
        # ratings has no stars/composite — use recruiting values directly
        ratings_season["stars"] = ratings_season["rec_stars"]
        ratings_season["composite_score"] = ratings_season["rec_composite"]
    else:
        ratings_season["recruit_year"] = None

    # EDGE — only need stats_measured/games_played (edge_score already in ratings)
    if not edge.empty:
        edge_slim = edge[["player_season_id", "stats_measured", "games_played"]] \
                        .rename(columns={"player_season_id": "edge_ps_id"})
        ratings_season = ratings_season.merge(edge_slim, left_on="player_season_id", right_on="edge_ps_id", how="left")
    else:
        ratings_season["stats_measured"] = None
        ratings_season["games_played"] = None

    # Stats (season aggregate)
    def _parse_data(v):
        if isinstance(v, dict): return v
        try: return json.loads(v)
        except: return {}

    st = T["stats"]
    if not st.empty:
        st_agg = st[(st["stat_type"] == "season_aggregate") & st["game_id"].isna()] \
                   [["player_season_id", "data"]].copy()
        st_agg["data"] = st_agg["data"].apply(_parse_data)
        ratings_season = ratings_season.merge(
            st_agg.rename(columns={"player_season_id": "st_ps_id", "data": "stats_season"}),
            left_on="player_season_id", right_on="st_ps_id", how="left")

        st_post = st[(st["stat_type"] == "postseason_aggregate") & st["game_id"].isna()] \
                    [["player_season_id", "data"]].copy()
        st_post["data"] = st_post["data"].apply(_parse_data)
        ratings_season = ratings_season.merge(
            st_post.rename(columns={"player_season_id": "stp_id", "data": "stats_postseason"}),
            left_on="player_season_id", right_on="stp_id", how="left")
    else:
        ratings_season["stats_season"] = None
        ratings_season["stats_postseason"] = None

    players = []
    for _, row in ratings_season.iterrows():
        if not row.get("name"):
            continue
        players.append({
            "id":               _i(row.get("cfb_player_id")),
            "player_season_id": _i(row.get("player_season_id")),
            "name":             row.get("name"),
            "position":         row.get("position"),
            "position_group":   row.get("position_group"),
            "year":             _i(row.get("year")),
            "height_in":        _i(row.get("height_in")),
            "weight_lbs":       _i(row.get("weight_lbs")),
            "hometown_state":   row.get("hometown_state"),
            "team_id":          _i(row.get("team_id")),
            "team":             row.get("school"),
            "team_abbr":        row.get("abbreviation"),
            "conference":       row.get("conference"),
            "team_color":       row.get("color"),
            "logo_url":         row.get("logo_url"),
            "overall_rating":   _f(row.get("overall_rating")),
            "position_rating":  _f(row.get("position_rating")),
            "trajectory":       _f(row.get("trajectory_score")),
            "breakout_prob":    _f(row.get("breakout_probability")),
            "shap":             _parse_shap(row.get("shap_values")),
            "stars":            _i(row.get("stars")),
            "composite_score":  _f(row.get("composite_score")),
            "recruit_year":     _i(row.get("recruit_year")),
            "season":           season,
            "edge_score":       _f(row.get("edge_score")),
            "stats_measured":   _i(row.get("stats_measured")),
            "games_played":     _i(row.get("games_played")),
            "stats_season":     row.get("stats_season") if isinstance(row.get("stats_season"), dict) else None,
            "stats_postseason": row.get("stats_postseason") if isinstance(row.get("stats_postseason"), dict) else None,
        })

    players.sort(key=lambda p: p["overall_rating"] or 0, reverse=True)
    write_json(output_dir / f"players_{season}.json", players)


# ---------------------------------------------------------------------------
# Export: teams.json
# ---------------------------------------------------------------------------

def export_teams(T: dict, output_dir: Path, season: int) -> None:
    tm  = T["teams"]
    rat = T["ratings"]
    ps  = T["player_seasons"]

    if tm.empty:
        return

    # Per-team player count — join ratings -> player_seasons to get team_id
    count_by_team = {}
    if not rat.empty and not ps.empty:
        ps_slim = ps[["id", "team_id"]].rename(columns={"id": "player_season_id"})
        ratings_season = rat[rat["season"] == season].merge(ps_slim, on="player_season_id", how="left")
        ratings_season = ratings_season[ratings_season["team_id"].notna()]
        count_by_team = ratings_season.groupby("team_id").size().to_dict()

    teams = []
    for _, t in tm.iterrows():
        teams.append({
            "id":           _i(t.get("id")),
            "school":       t.get("school"),
            "abbreviation": t.get("abbreviation"),
            "conference":   t.get("conference"),
            "color":        t.get("color"),
            "alt_color":    t.get("alt_color"),
            "logo_url":     t.get("logo_url"),
            "stadium_name": t.get("stadium_name"),
            "city":         t.get("city"),
            "state":        t.get("state"),
            "capacity":     _i(t.get("capacity")),
            "player_count": count_by_team.get(t["id"], 0),
            "season":       season,
        })

    teams.sort(key=lambda x: x["school"] or "")
    write_json(output_dir / "teams.json", teams)


# ---------------------------------------------------------------------------
# Export: team_ratings.json
# ---------------------------------------------------------------------------

def export_team_ratings(T: dict, output_dir: Path, season: int) -> None:
    """Export all seasons of team ratings (not just current) so the UI can look up any year."""
    tr = T["team_ratings"]
    tm = T["teams"]

    if tr.empty:
        print("  team_ratings.json: no computed team ratings — skipping")
        return

    tr_s = tr.copy() if "season" in tr.columns else tr.copy()
    if tr_s.empty:
        print(f"  team_ratings.json: no data")
        return

    if not tm.empty:
        tr_s = tr_s.merge(tm[["id", "school", "conference", "color", "logo_url"]]
                          .rename(columns={"id": "tm_id"}),
                          left_on="team_id", right_on="tm_id", how="left")

    rows = []
    for _, r in tr_s.sort_values("overall_rating", ascending=False, na_position="last").iterrows():
        sub = r.get("sub_ratings")
        if isinstance(sub, str):
            try:
                sub = json.loads(sub)
            except Exception:
                sub = {}
        sub = sub or {}

        row = {
            "team_id":          _i(r.get("team_id")),
            "season":           _i(r.get("season")),
            "overall_rating":   _f(r.get("overall_rating")),
            "offense_rating":   _f(r.get("offense_rating")),
            "defense_rating":   _f(r.get("defense_rating")),
            "sp_overall":       _f(r.get("sp_overall")),
            "sp_offense":       _f(r.get("sp_offense")),
            "sp_defense":       _f(r.get("sp_defense")),
            "recruiting_score": _f(r.get("recruiting_score")),
            "school":           r.get("school"),
            "conference":       r.get("conference"),
            "color":            r.get("color"),
            "logo_url":         r.get("logo_url"),
            "sub_ratings":      sub,
        }
        # Hoist split keys for frontend
        for k in ("pass_off", "run_off", "pass_def", "run_def", "special_teams"):
            row[k] = sub.get(k)
        row["sp_plus"]       = sub.get("sp_offense_scaled") or sub.get("sp_overall_scaled")
        row["recruit_score"] = sub.get("recruiting_scaled")
        rows.append(row)

    write_json(output_dir / "team_ratings.json", rows)
    print(f"  {len(rows)} team_ratings rows exported")


# ---------------------------------------------------------------------------
# Export: ratings_by_position.json
# ---------------------------------------------------------------------------

def export_ratings_by_position(T: dict, output_dir: Path, season: int) -> None:
    rat = T["ratings"]
    ps  = T["player_seasons"]
    pl  = T["players"]
    tm  = T["teams"]
    rec = T["recruiting"]

    if rat.empty:
        return

    # ratings.player_id is Supabase internal; use player_seasons.player_id (CFB API id)
    ratings_season = rat[rat["season"] == season].copy()
    ratings_season = ratings_season.merge(ps[["id", "player_id", "team_id", "position_group", "year"]]
                        .rename(columns={"id": "ps_id", "player_id": "cfb_player_id"}),
                        left_on="player_season_id", right_on="ps_id", how="left")
    ratings_season = ratings_season.merge(pl[["id", "name"]].rename(columns={"id": "pl_id"}),
                        left_on="cfb_player_id", right_on="pl_id", how="left")
    ratings_season = ratings_season.merge(tm[["id", "school", "abbreviation", "conference", "color"]]
                        .rename(columns={"id": "tm_id"}),
                        left_on="team_id", right_on="tm_id", how="left")

    ratings_season = ratings_season.sort_values("overall_rating", ascending=False, na_position="last")

    by_position: dict = {}
    for _, row in ratings_season.iterrows():
        pg = row.get("position_group") or "ATH"
        if pg not in by_position:
            by_position[pg] = []
        if len(by_position[pg]) >= TOP_N_PER_POSITION:
            continue
        by_position[pg].append({
            "id":              _i(row.get("cfb_player_id")),
            "name":            row.get("name"),
            "year":            _i(row.get("year")),
            "team":            row.get("school"),
            "team_abbr":       row.get("abbreviation"),
            "conference":      row.get("conference"),
            "team_color":      row.get("color"),
            "overall":         _f(row.get("overall_rating")),
            "position_rating": _f(row.get("position_rating")),
            "trajectory":      _f(row.get("trajectory_score")),
            "breakout_prob":   _f(row.get("breakout_probability")),
            "shap":            _parse_shap(row.get("shap_values")),
            "stars":           _i(row.get("stars")),
            "composite":       _f(row.get("composite_score")),
        })

    write_json(output_dir / f"ratings_by_position_{season}.json", by_position)


# ---------------------------------------------------------------------------
# Export: similar_players_{season}.json  (one file per season)
# ---------------------------------------------------------------------------

def _era_bucket(season: int) -> int:
    """0=classic(2008-12), 1=transition(2013-17), 2=modern(2018+)."""
    if season <= 2012:
        return 0
    if season <= 2017:
        return 1
    return 2


def export_similar_players(T: dict, output_dir: Path) -> None:
    """
    Build per-season similar_players_{year}.json files using weighted
    z-score Euclidean distance (replaces cosine on min-max normalized features).

    Fixes vs old algorithm:
    - Z-score standardization prevents score collapse in small position groups
    - OVR band filter (±15) prevents elite/mediocre cross-matching
    - games_played filter (>=5) excludes low-sample backups as comps
    - Era cap: max 2 of 5 slots can be cross-era
    - Stars default 2.5 (not 0) so missing recruiting data is neutral
    """
    import numpy as np

    rat  = T["ratings"]
    ps   = T["player_seasons"]
    pl   = T["players"]
    tm   = T["teams"]
    edge = T["player_edge"]
    rec  = T["recruiting"]

    if rat.empty:
        return

    print("  Building similarity matrix...")

    # Build base dataframe: rated players with OVR >= 55
    df = rat[rat["overall_rating"] >= 55].copy()
    df = df.merge(
        ps[["id", "player_id", "team_id", "position_group"]]
          .rename(columns={"id": "ps_id", "player_id": "cfb_player_id"}),
        left_on="player_season_id", right_on="ps_id", how="left")
    df = df.merge(
        pl[["id", "name"]].rename(columns={"id": "pl_id"}),
        left_on="cfb_player_id", right_on="pl_id", how="left")
    df = df.merge(
        tm[["id", "school"]].rename(columns={"id": "tm_id"}),
        left_on="team_id", right_on="tm_id", how="left")

    # Join edge data for games_played filter and raw edge_score
    if not edge.empty:
        edge_slim = edge[["player_season_id", "edge_score", "games_played"]].copy()
        df = df.merge(edge_slim, on="player_season_id", how="left")
    else:
        df["edge_score"] = 0.0
        df["games_played"] = 0

    # Join recruiting for stars (best match by player_id)
    if not rec.empty:
        best_stars = (
            rec[rec["stars"].notna()]
               .sort_values("stars", ascending=False)
               .drop_duplicates(subset=["player_id"], keep="first")
               [["player_id", "stars"]]
        )
        df = df.merge(
            best_stars.rename(columns={"player_id": "rec_pid"}),
            left_on="cfb_player_id", right_on="rec_pid", how="left")
    else:
        df["stars"] = None

    # Filter: require >= 5 games OR ATH/special (kickers often have fewer "games")
    SPARSE_POS = {"K", "P", "ATH"}
    mask_games = (
        df["games_played"].fillna(0) >= 5
    ) | df["position_group"].isin(SPARSE_POS)
    df = df[mask_games].copy()

    if df.empty:
        return

    # Feature weights: [ovr, edge_pctile, trajectory, stars, era_bucket]
    WEIGHTS = np.array([0.35, 0.30, 0.15, 0.10, 0.10])
    OVR_BAND = 15        # max OVR difference to consider as a comp
    MAX_CROSS_ERA = 2    # max cross-era slots per player's 5 comps
    SIM_SCALE = 0.5      # z-score distances are small; 0.5 spreads top-5 comps across 0.5–0.99
    MAX_SIMILARITY = 0.99  # two distinct players are never 100% similar

    # Group by position_group; compute all cross-season similarity within each group
    by_pos: dict = defaultdict(list)
    for _, row in df.iterrows():
        by_pos[row.get("position_group") or "ATH"].append(row.to_dict())

    # Accumulate results keyed by season: season_buckets[season][str(ps_id)] = [...]
    season_buckets: dict = defaultdict(dict)

    for pg, group in by_pos.items():
        if len(group) < 2:
            continue

        # Compute edge percentile rank within this position group
        raw_edges = np.array([_f(r.get("edge_score"), 0.0) for r in group])
        if raw_edges.max() > raw_edges.min():
            edge_pctiles = (
                pd.Series(raw_edges).rank(pct=True) * 100
            ).to_numpy()
        else:
            edge_pctiles = np.full(len(group), 50.0)

        # Build raw feature matrix
        raw = np.array([
            [
                _f(r.get("overall_rating"), 50.0),
                edge_pctiles[k],
                _f(r.get("trajectory_score"), 0.0),
                _f(r.get("stars"), 2.5),              # neutral default, not 0
                float(_era_bucket(int(r.get("season") or 2025))),
            ]
            for k, r in enumerate(group)
        ], dtype=float)

        # Z-score standardize per column within position group
        mean = raw.mean(axis=0)
        std  = raw.std(axis=0)
        std[std == 0] = 1.0    # zero-variance columns stay at 0; no collapse
        z = (raw - mean) / std

        # For each player: find top 5 comps with OVR band + era cap
        for i, row in enumerate(group):
            ovr_i  = _f(row.get("overall_rating"), 50.0)
            era_i  = _era_bucket(int(row.get("season") or 2025))
            z_i    = z[i]

            # Weighted Euclidean distances to all other players
            diffs = z - z_i                      # (n, 5)
            dists = np.sqrt((WEIGHTS * diffs**2).sum(axis=1))
            dists[i] = np.inf                    # exclude self

            # Apply OVR band filter
            for j, other in enumerate(group):
                if abs(_f(other.get("overall_rating"), 50.0) - ovr_i) > OVR_BAND:
                    dists[j] = np.inf

            sorted_idx = np.argsort(dists)

            sims = []
            cross_era_count = 0
            for j in sorted_idx:
                if len(sims) == 5:
                    break
                if np.isinf(dists[j]):
                    break
                era_j = _era_bucket(int(group[j].get("season") or 2025))
                is_cross_era = era_j != era_i
                if is_cross_era:
                    if cross_era_count >= MAX_CROSS_ERA:
                        continue
                    cross_era_count += 1
                similarity = round(
                    min(MAX_SIMILARITY, max(0.0, 1.0 - float(dists[j]) / SIM_SCALE)), 3)
                other = group[j]
                sims.append({
                    "id":         _i(other.get("cfb_player_id")),
                    "ps_id":      _i(other.get("player_season_id")),
                    "name":       other.get("name"),
                    "season":     other.get("season"),
                    "team":       other.get("school"),
                    "ovr":        round(_f(other.get("overall_rating"), 0), 1),
                    "similarity": similarity,
                })

            ps_id = _i(row.get("player_season_id"))
            season = row.get("season")
            if ps_id is not None and season is not None:
                season_buckets[int(season)][str(ps_id)] = sims

    # Write one file per season
    total = 0
    for season, bucket in sorted(season_buckets.items()):
        write_json(output_dir / f"similar_players_{season}.json", bucket)
        total += len(bucket)
    print(f"  {total} player-seasons with similarity data across {len(season_buckets)} seasons")


# ---------------------------------------------------------------------------
# Export: rosters.json
# ---------------------------------------------------------------------------

def export_rosters(T: dict, output_dir: Path, season: int) -> None:
    ps  = T["player_seasons"]
    pl  = T["players"]
    rat = T["ratings"]
    rec = T["recruiting"]

    if ps.empty:
        return

    ps_s = ps[ps["season"] == season].copy()
    if ps_s.empty:
        print(f"  rosters_{season}.json: no player_seasons for this season")
        return

    pl_cols = [c for c in ["id", "name", "height_in", "weight_lbs", "hometown_state"] if c in pl.columns]
    merged = ps_s.merge(pl[pl_cols].rename(columns={"id": "pl_id"}),
                        left_on="player_id", right_on="pl_id", how="left")

    if not rat.empty:
        ratings_season = rat[rat["season"] == season][["player_season_id", "overall_rating",
                     "trajectory_score", "breakout_probability", "shap_values"]].copy()
        merged = merged.merge(ratings_season, left_on="id", right_on="player_season_id",
                              how="left", suffixes=("", "_rat"))

    edge = T.get("player_edge", pd.DataFrame())
    if not edge.empty and "edge_score" in edge.columns:
        edge_s = edge[["player_season_id", "edge_score"]].rename(
            columns={"player_season_id": "edge_ps_id"})
        merged = merged.merge(edge_s, left_on="id", right_on="edge_ps_id", how="left")

    if not rec.empty:
        rec_best = rec.sort_values("composite_score", ascending=False, na_position="last") \
                      .drop_duplicates(subset=["player_id"], keep="first") \
                      [["player_id", "stars", "composite_score"]] \
                      .rename(columns={"player_id": "rec_pid",
                                       "stars": "rec_stars",
                                       "composite_score": "rec_composite"})
        merged = merged.merge(rec_best, left_on="player_id", right_on="rec_pid", how="left")
    else:
        merged["rec_stars"] = None
        merged["rec_composite"] = None

    merged = merged[merged["team_id"].notna()]

    rosters: dict = {}
    for _, row in merged.iterrows():
        tid = str(_i(row["team_id"]))
        rosters.setdefault(tid, []).append({
            "player_id":        _i(row["player_id"]),
            "player_season_id": _i(row["id"]),
            "name":             row.get("name"),
            "position_group":   row.get("position_group"),
            "year":             _i(row.get("year")),
            "height_in":        _i(row.get("height_in")),
            "weight_lbs":       _i(row.get("weight_lbs")),
            "hometown_state":   row.get("hometown_state"),
            "overall_rating":   _f(row.get("overall_rating")),
            "trajectory":       _f(row.get("trajectory_score")),
            "breakout_prob":    _f(row.get("breakout_probability")),
            "shap":             _parse_shap(row.get("shap_values")),
            "stars":            _i(row.get("rec_stars")),
            "composite_score":  _f(row.get("rec_composite")),
            "edge_score":       _f(row.get("edge_score")),
        })

    for tid in rosters:
        rosters[tid].sort(key=lambda p: p["overall_rating"] or 0, reverse=True)

    write_json(output_dir / f"rosters_{season}.json", rosters)


# ---------------------------------------------------------------------------
# Export: schedules.json
# ---------------------------------------------------------------------------

def _build_fcs_name_map() -> dict:
    """Build {cfb_api_id: team_name} from cached API game responses for FCS lookup."""
    cache_dir = Path(__file__).parent.parent / ".cache"
    id_to_name: dict = {}
    if not cache_dir.exists():
        return id_to_name
    for f in cache_dir.glob("*.json"):
        try:
            with open(f) as fp:
                data = json.load(fp)
            if not isinstance(data, list) or not data:
                continue
            first = data[0]
            if not isinstance(first, dict) or "homeTeam" not in first:
                continue
            for g in data:
                hid = g.get("homeId")
                aid = g.get("awayId")
                ht  = g.get("homeTeam")
                at  = g.get("awayTeam")
                if hid and ht:
                    id_to_name[int(hid)] = ht
                if aid and at:
                    id_to_name[int(aid)] = at
        except Exception:
            continue
    return id_to_name


def export_schedules(T: dict, output_dir: Path, season: int) -> None:
    games = T["games"]
    teams = T["teams"]

    if games.empty:
        return

    games_s = games[games["season"] == season]
    if games_s.empty:
        print(f"  schedules_{season}.json: no games for this season")
        return

    tm_map = dict(zip(teams["id"], teams["school"])) if not teams.empty else {}
    # Supplement with FCS team names from API cache
    fcs_map = _build_fcs_name_map()
    def _team_name(tid):
        if tid is None:
            return None
        return tm_map.get(tid) or fcs_map.get(tid) or f"FCS opponent"

    schedules: dict = {}

    for _, g in games_s.iterrows():
        home_id   = _i(g.get("home_team_id"))
        away_id   = _i(g.get("away_team_id"))
        home_sc   = _i(g.get("home_score"))
        away_sc   = _i(g.get("away_score"))
        game_date = str(g["game_date"]) if g.get("game_date") else None

        # For result calc: FCS opponent scores may be null — treat as 0 for W/L but
        # still store null in opp_score so UI can display "—" rather than "0".
        for is_home in (True, False):
            team_id_val = home_id if is_home else away_id
            if team_id_val is None:
                continue  # skip games where this side has no known team_id
            tid        = str(team_id_val)
            opp_id     = away_id if is_home else home_id
            opponent   = _team_name(opp_id)
            team_score = home_sc if is_home else away_sc
            opp_score  = away_sc if is_home else home_sc
            # Result: if opp_score is null but team_score exists, treat opp as 0 (FCS game)
            if team_score is not None:
                opp_for_result = opp_score if opp_score is not None else 0
                result = ("W" if team_score > opp_for_result
                          else ("L" if team_score < opp_for_result else "T"))
            else:
                result = None
            schedules.setdefault(tid, []).append({
                "game_id":      _i(g.get("id")),
                "week":         _i(g.get("week")),
                "game_date":    game_date,
                "season_type":  g.get("season_type"),
                "is_home":      is_home,
                "neutral_site": bool(g.get("neutral_site")),
                "opponent":     opponent,
                "team_score":   team_score,
                "opp_score":    opp_score,
                "result":       result,
                "home_team_id": home_id,
                "away_team_id": away_id,
            })

    write_json(output_dir / f"schedules_{season}.json", schedules)


# ---------------------------------------------------------------------------
# Export: transfers.json
# ---------------------------------------------------------------------------

def export_transfers(T: dict, output_dir: Path, season: int) -> None:
    tr  = T["transfers"]
    pl  = T["players"]
    ps  = T["player_seasons"]
    tm  = T["teams"]
    rat = T.get("ratings", pd.DataFrame())

    if tr.empty:
        return

    tr_s = tr[tr["transfer_year"] == season] if "transfer_year" in tr.columns else tr
    # Only export transfers linked to a known player
    if "player_id" in tr_s.columns:
        tr_s = tr_s[tr_s["player_id"].notna()]
    if tr_s.empty:
        print(f"  transfers_{season}.json: no linked transfers for this season")
        return

    merged = tr_s.merge(pl[["id", "name"]].rename(columns={"id": "pl_id"}),
                        left_on="player_id", right_on="pl_id", how="left")

    if not tm.empty:
        from_schools = tm[["id", "school"]].rename(columns={"id": "from_id", "school": "from_school"})
        to_schools   = tm[["id", "school"]].rename(columns={"id": "to_id",   "school": "to_school"})
        merged = merged.merge(from_schools, left_on="from_team_id", right_on="from_id", how="left")
        merged = merged.merge(to_schools,   left_on="to_team_id",   right_on="to_id",   how="left")

    if not ps.empty:
        pos_lookup = ps[ps["season"] == season].drop_duplicates(subset=["player_id"]) \
                       [["player_id", "position_group"]]
        merged = merged.merge(pos_lookup, on="player_id", how="left")

    # Build rating lookup: (player_season_id) → overall_rating
    # Also need (player_id, season, team_id) → player_season_id from player_seasons
    ps_ovr_map: dict = {}   # player_season_id → overall_rating
    ps_id_map: dict  = {}   # (player_id, season, team_id) → player_season_id
    if not rat.empty and not ps.empty:
        if "overall_rating" in rat.columns and "player_season_id" in rat.columns:
            for _, rr in rat.iterrows():
                psid = _i(rr.get("player_season_id"))
                ovr  = _f(rr.get("overall_rating"))
                if psid and ovr:
                    ps_ovr_map[psid] = ovr
        for _, pr in ps.iterrows():
            key = (_i(pr["player_id"]), _i(pr["season"]), _i(pr["team_id"]))
            if all(k is not None for k in key):
                ps_id_map[key] = _i(pr["id"])

    def _lookup_ovr(pid, seas, team_id):
        psid = ps_id_map.get((_i(pid), _i(seas), _i(team_id)))
        return ps_ovr_map.get(psid) if psid else None

    transfers: dict = {}
    for _, row in merged.iterrows():
        portal_date = str(row["portal_date"]) if row.get("portal_date") else None
        pid     = _i(row["player_id"])
        from_id = _i(row.get("from_team_id"))
        to_id   = _i(row.get("to_team_id"))
        # Current-season rating at to_team; previous-season rating at from_team
        ovr_current  = _lookup_ovr(pid, season,     to_id)
        ovr_previous = _lookup_ovr(pid, season - 1, from_id)
        entry = {
            "player_id":      pid,
            "name":           row.get("name"),
            "position_group": row.get("position_group"),
            "transfer_year":  season,
            "portal_date":    portal_date,
            "from_school":    row.get("from_school") or "Unknown school",
            "to_school":      row.get("to_school") or "Unknown school",
            "ovr_current":    round(ovr_current,  1) if ovr_current  else None,
            "ovr_previous":   round(ovr_previous, 1) if ovr_previous else None,
        }
        if from_id:
            transfers.setdefault(str(from_id), []).append({**entry, "direction": "out"})
        if to_id:
            transfers.setdefault(str(to_id), []).append({**entry, "direction": "in"})

    write_json(output_dir / f"transfers_{season}.json", transfers)


# ---------------------------------------------------------------------------
# Export: team_history.json
# ---------------------------------------------------------------------------

def _build_conf_history(teams: "pd.DataFrame") -> dict:
    """Build {team_id: {season: conference}} from teams table + override table."""
    if teams.empty:
        return {}
    base = dict(zip(teams["id"], teams["conference"]))

    # School-name based overrides for well-known realignment moves.
    # Keyed by EXACT lowercase school name as stored in the teams table.
    _by_school: dict[str, list[tuple[int, int, str]]] = {
        "usc":            [(2008, 2023, "Pac-12"),        (2024, 9999, "Big Ten")],
        "ucla":           [(2008, 2023, "Pac-12"),        (2024, 9999, "Big Ten")],
        "oregon":         [(2008, 2023, "Pac-12"),        (2024, 9999, "Big Ten")],
        "washington":     [(2008, 2023, "Pac-12"),        (2024, 9999, "Big Ten")],
        "texas":          [(2008, 2023, "Big 12"),        (2024, 9999, "SEC")],
        "oklahoma":       [(2008, 2023, "Big 12"),        (2024, 9999, "SEC")],
        "utah":           [(2008, 2023, "Pac-12"),        (2024, 9999, "Big 12")],
        "colorado":       [(2008, 2010, "Big 12"),  (2011, 2023, "Pac-12"), (2024, 9999, "Big 12")],
        "arizona":        [(2008, 2023, "Pac-12"),        (2024, 9999, "Big 12")],
        "arizona state":  [(2008, 2023, "Pac-12"),        (2024, 9999, "Big 12")],
        "maryland":       [(2008, 2013, "ACC"),           (2014, 9999, "Big Ten")],
        "rutgers":        [(2008, 2013, "Big East"),      (2014, 9999, "Big Ten")],
        "nebraska":       [(2008, 2010, "Big 12"),        (2011, 9999, "Big Ten")],
        "texas a&m":      [(2008, 2011, "Big 12"),        (2012, 9999, "SEC")],
        "missouri":       [(2008, 2011, "Big 12"),        (2012, 9999, "SEC")],
        "pittsburgh":     [(2008, 2012, "Big East"),      (2013, 9999, "ACC")],
        "syracuse":       [(2008, 2012, "Big East"),      (2013, 9999, "ACC")],
        "louisville":     [(2008, 2013, "Big East"),      (2014, 9999, "ACC")],
        "west virginia":  [(2008, 2011, "Big East"),      (2012, 9999, "Big 12")],
        "tcu":            [(2008, 2011, "Mountain West"), (2012, 9999, "Big 12")],
    }

    # Build id → school lookup
    id_to_school = dict(zip(teams["id"], teams["school"].str.lower())) if "school" in teams.columns else {}

    def _conf_for_team_season(tid: int, sea: int) -> str:
        school = id_to_school.get(tid, "")
        overrides = _by_school.get(school)
        if overrides:
            for (start, end, conf) in overrides:
                if start <= sea <= end:
                    return conf
        return base.get(tid, "")

    return _conf_for_team_season


def export_team_history(T: dict, output_dir: Path) -> None:
    tr    = T["team_ratings"]
    games = T["games"]
    teams = T["teams"]

    if tr.empty:
        return

    # Compute W/L from games
    wins_by: dict = defaultdict(lambda: defaultdict(int))
    losses_by: dict = defaultdict(lambda: defaultdict(int))
    if not games.empty:
        for _, g in games.iterrows():
            hs = _i(g.get("home_score"))
            as_ = _i(g.get("away_score"))
            if hs is None or as_ is None:
                continue
            sea = _i(g.get("season"))
            hid = _i(g.get("home_team_id"))
            aid = _i(g.get("away_team_id"))
            if hs > as_:
                wins_by[hid][sea]   += 1
                losses_by[aid][sea] += 1
            elif as_ > hs:
                wins_by[aid][sea]   += 1
                losses_by[hid][sea] += 1

    get_conf = _build_conf_history(teams)

    history: dict = {}
    for _, row in tr.sort_values("season", ascending=False).iterrows():
        tid = _i(row.get("team_id"))
        sea = _i(row.get("season"))
        history.setdefault(str(tid), []).append({
            "season":           sea,
            "wins":             wins_by[tid].get(sea),
            "losses":           losses_by[tid].get(sea),
            "conference":       get_conf(tid, sea),
            "sp_overall":       _f(row.get("sp_overall")),
            "overall_rating":   _f(row.get("overall_rating")),
            "offense_rating":   _f(row.get("offense_rating")),
            "defense_rating":   _f(row.get("defense_rating")),
            "recruiting_score": _f(row.get("recruiting_score")),
        })

    write_json(output_dir / "team_history.json", history)


# ---------------------------------------------------------------------------
# Export: team_stats_{season}.json — per-game team metrics for the stats panel
# ---------------------------------------------------------------------------

def export_team_stats(T: dict, output_dir: Path, season: int) -> None:
    ts = T.get("team_season_stats")
    if ts is None or ts.empty:
        return
    ts_s = ts[ts["season"] == season]
    if ts_s.empty:
        print(f"  team_stats_{season}.json: no data")
        return
    out: dict = {}
    for _, row in ts_s.iterrows():
        tid = _i(row.get("team_id"))
        if tid is None:
            continue
        d = {k: (None if (isinstance(v, float) and math.isnan(v)) else v)
             for k, v in row.to_dict().items() if k not in ("team_id", "season")}
        out[str(tid)] = d
    write_json(output_dir / f"team_stats_{season}.json", out)


# ---------------------------------------------------------------------------
# Export: player_transfers.json — cross-season player-centric transfer history
# ---------------------------------------------------------------------------

def export_player_transfers(T: dict, output_dir: Path) -> None:
    """Build player-centric transfer history.

    Output: {str(player_id): [{transfer_year, from_school, to_school, portal_date, portal_entry_count}]}
    Sorted by transfer_year asc within each player's list.
    """
    tr = T["transfers"]
    tm = T["teams"]

    if tr.empty:
        return

    # Only linked transfers
    linked = tr[tr["player_id"].notna()].copy()
    if linked.empty:
        return

    # Join school names
    if not tm.empty:
        from_schools = tm[["id", "school"]].rename(columns={"id": "from_id", "school": "from_school"})
        to_schools   = tm[["id", "school"]].rename(columns={"id": "to_id",   "school": "to_school"})
        linked = linked.merge(from_schools, left_on="from_team_id", right_on="from_id", how="left")
        linked = linked.merge(to_schools,   left_on="to_team_id",   right_on="to_id",   how="left")

    out: dict = {}
    for _, row in linked.iterrows():
        pid = _i(row.get("player_id"))
        if pid is None:
            continue
        entry = {
            "transfer_year":       _i(row.get("transfer_year")),
            "from_school":         row.get("from_school") or "Unknown school",
            "to_school":           row.get("to_school")   or "Unknown school",
            "portal_date":         str(row["portal_date"]) if row.get("portal_date") else None,
            "portal_entry_count":  _i(row.get("portal_entry_count")),
        }
        out.setdefault(str(pid), []).append(entry)

    # Sort each player's list chronologically
    for pid_key in out:
        out[pid_key].sort(key=lambda e: e.get("transfer_year") or 0)

    write_json(output_dir / "player_transfers.json", out)


# ---------------------------------------------------------------------------
# Export: research/index.json
# ---------------------------------------------------------------------------

def export_research(T: dict, output_dir: Path) -> None:
    rc = T["research_cache"]
    research_dir = output_dir / "research"

    if rc.empty:
        research_dir.mkdir(parents=True, exist_ok=True)
        write_json(research_dir / "index.json", [])
        print("  No research_cache entries yet — index.json written as empty array")
        return

    index = []
    for _, row in rc.iterrows():
        key = row.get("research_key")
        data = row.get("data") or {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        meta = data.get("_meta", {})
        if meta:
            index.append({
                "key":           key,
                "title":         meta.get("title", key),
                "category":      meta.get("category", ""),
                "summary":       meta.get("summary", ""),
                "headline_stat": meta.get("headline_stat", ""),
            })
        # Write individual finding file
        data["_generated_at"] = str(row.get("generated_at", ""))
        write_json(research_dir / f"{key}.json", data)

    write_json(research_dir / "index.json", index)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

ALL_SEASONS = list(range(2008, CURRENT_SEASON + 1))


def main():
    parser = argparse.ArgumentParser(description="Export local data -> static JSON for frontend")
    parser.add_argument("--season", type=int, default=None,
                        help="Single season to export (default: all seasons)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    output_dir: Path = args.output
    seasons = [args.season] if args.season else ALL_SEASONS

    print(f"\nExporting {len(seasons)} season(s) -> {output_dir}\n")
    output_dir.mkdir(parents=True, exist_ok=True)

    T = load_tables()
    print()

    # Season-independent exports (once, using latest season for display defaults)
    current = seasons[-1]

    print("teams.json...")
    export_teams(T, output_dir, current)

    print("team_ratings.json...")
    export_team_ratings(T, output_dir, current)

    print("team_history.json...")
    export_team_history(T, output_dir)

    print("similar_players_{season}.json (per season)...")
    export_similar_players(T, output_dir)

    print("player_transfers.json...")
    export_player_transfers(T, output_dir)

    print("research/...")
    export_research(T, output_dir)

    # Per-season exports
    for season in seasons:
        print(f"\n--- Season {season} ---")

        print(f"  players_{season}.json...")
        export_players(T, output_dir, season)

        print(f"  ratings_by_position_{season}.json...")
        export_ratings_by_position(T, output_dir, season)

        print(f"  rosters_{season}.json...")
        export_rosters(T, output_dir, season)

        print(f"  schedules_{season}.json...")
        export_schedules(T, output_dir, season)

        print(f"  transfers_{season}.json...")
        export_transfers(T, output_dir, season)

        print(f"  team_stats_{season}.json...")
        export_team_stats(T, output_dir, season)

    print("\nDone.")


if __name__ == "__main__":
    main()
