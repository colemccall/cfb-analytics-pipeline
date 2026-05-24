"""Export pipeline data → static JSON files for cfb-analytics-app/data/.

Reads from data/raw/ (API harvest) and data/computed/ (ratings engine output).
No database required.

Exports:
  players.json              — all rated players (ratings, SHAP, recruiting, team)
  teams.json                — all teams with avg rating, player count
  team_ratings.json         — team OVR + sub-score splits
  ratings_by_position.json  — top 50 per position group
  similar_players.json      — precomputed cosine similarity (OVR >= 55)
  rosters.json              — all team rosters by season  {team_id: {season: [...]}}
  schedules.json            — all games by team/season    {team_id: {season: [...]}}
  transfers.json            — all portal moves by team    {team_id: [...]}
  team_history.json         — year-by-year progression    {team_id: [{season,...}]}
  research/index.json       — published findings index

Usage:
    python scripts/07_export_static_json.py
    python scripts/07_export_static_json.py --season 2024
    python scripts/07_export_static_json.py --output /custom/path
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw, read_computed

DEFAULT_OUTPUT = Path(__file__).parent.parent.parent / "cfb-analytics-app" / "data"
CURRENT_SEASON = 2025
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
        json.dump(_clean_nan(data), f, separators=(",", ":"), default=_default, allow_nan=False)
    size_kb = path.stat().st_size / 1024
    n = len(data) if isinstance(data, (list, dict)) else "?"
    print(f"  Wrote {path.name} ({size_kb:.1f} KB, {n} items)")


def _default(o):
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
        "ratings":          read_computed("ratings"),
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
    rat_s = rat[rat["season"] == season].copy()
    if rat_s.empty:
        print(f"  players.json: no ratings for season {season}")
        return

    # ratings.player_id is the Supabase internal PK — NOT the CFB API player id.
    # The correct CFB API player id lives in player_seasons.player_id.
    # Join path: ratings.player_season_id → player_seasons.id → player_seasons.player_id → players.id
    ps_slim = ps[["id", "player_id", "position_group", "year", "team_id"]] \
                .rename(columns={"id": "ps_id", "player_id": "cfb_player_id", "team_id": "ps_team_id"})
    rat_s = rat_s.merge(ps_slim, left_on="player_season_id", right_on="ps_id", how="left")
    rat_s = rat_s.rename(columns={"ps_team_id": "team_id"})

    # players — join on cfb_player_id (= players.id)
    pl_slim = pl[["id", "name", "position", "height_in", "weight_lbs", "hometown_state"]] \
                .rename(columns={"id": "pl_id"})
    rat_s = rat_s.merge(pl_slim, left_on="cfb_player_id", right_on="pl_id", how="left")

    # teams — school/conference/color (use resolved team_id)
    tm_slim = tm[["id", "school", "abbreviation", "conference", "color", "logo_url"]] \
                .rename(columns={"id": "tm_id"})
    rat_s = rat_s.merge(tm_slim, left_on="team_id", right_on="tm_id", how="left")

    # Recruiting — pull stars/composite from recruiting table (ratings.stars is often None)
    if not rec.empty:
        rec_best = rec.sort_values("composite_score", ascending=False, na_position="last") \
                      .drop_duplicates(subset=["player_id"], keep="first") \
                      [["player_id", "recruit_year", "stars", "composite_score"]] \
                      .rename(columns={"player_id": "rec_pid", "stars": "rec_stars",
                                       "composite_score": "rec_composite"})
        rat_s = rat_s.merge(rec_best, left_on="cfb_player_id", right_on="rec_pid", how="left")
        # ratings has no stars/composite — use recruiting values directly
        rat_s["stars"] = rat_s["rec_stars"]
        rat_s["composite_score"] = rat_s["rec_composite"]
    else:
        rat_s["recruit_year"] = None

    # EDGE — only need stats_measured/games_played (edge_score already in ratings)
    if not edge.empty:
        edge_slim = edge[["player_season_id", "stats_measured", "games_played"]] \
                        .rename(columns={"player_season_id": "edge_ps_id"})
        rat_s = rat_s.merge(edge_slim, left_on="player_season_id", right_on="edge_ps_id", how="left")
    else:
        rat_s["stats_measured"] = None
        rat_s["games_played"] = None

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
        rat_s = rat_s.merge(
            st_agg.rename(columns={"player_season_id": "st_ps_id", "data": "stats_season"}),
            left_on="player_season_id", right_on="st_ps_id", how="left")

        st_post = st[(st["stat_type"] == "postseason_aggregate") & st["game_id"].isna()] \
                    [["player_season_id", "data"]].copy()
        st_post["data"] = st_post["data"].apply(_parse_data)
        rat_s = rat_s.merge(
            st_post.rename(columns={"player_season_id": "stp_id", "data": "stats_postseason"}),
            left_on="player_season_id", right_on="stp_id", how="left")
    else:
        rat_s["stats_season"] = None
        rat_s["stats_postseason"] = None

    players = []
    for _, row in rat_s.iterrows():
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
        rat_s = rat[rat["season"] == season].merge(ps_slim, on="player_season_id", how="left")
        rat_s = rat_s[rat_s["team_id"].notna()]
        count_by_team = rat_s.groupby("team_id").size().to_dict()

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
    rat_s = rat[rat["season"] == season].copy()
    rat_s = rat_s.merge(ps[["id", "player_id", "team_id", "position_group", "year"]]
                        .rename(columns={"id": "ps_id", "player_id": "cfb_player_id"}),
                        left_on="player_season_id", right_on="ps_id", how="left")
    rat_s = rat_s.merge(pl[["id", "name"]].rename(columns={"id": "pl_id"}),
                        left_on="cfb_player_id", right_on="pl_id", how="left")
    rat_s = rat_s.merge(tm[["id", "school", "abbreviation", "conference", "color"]]
                        .rename(columns={"id": "tm_id"}),
                        left_on="team_id", right_on="tm_id", how="left")

    rat_s = rat_s.sort_values("overall_rating", ascending=False, na_position="last")

    by_position: dict = {}
    for _, row in rat_s.iterrows():
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
# Export: similar_players.json
# ---------------------------------------------------------------------------

def export_similar_players(T: dict, output_dir: Path) -> None:
    rat  = T["ratings"]
    ps   = T["player_seasons"]
    pl   = T["players"]
    tm   = T["teams"]
    edge = T["player_edge"]
    rec  = T["recruiting"]

    if rat.empty:
        return

    # ratings.player_id is Supabase internal; use player_seasons.player_id (CFB API id)
    print("  Building similarity matrix...")
    df = rat[rat["overall_rating"] >= 55].copy()
    df = df.merge(ps[["id", "player_id", "team_id", "position_group"]]
                  .rename(columns={"id": "ps_id", "player_id": "cfb_player_id"}),
                  left_on="player_season_id", right_on="ps_id", how="left")
    df = df.merge(pl[["id", "name"]].rename(columns={"id": "pl_id"}),
                  left_on="cfb_player_id", right_on="pl_id", how="left")
    df = df.merge(tm[["id", "school"]].rename(columns={"id": "tm_id"}),
                  left_on="team_id", right_on="tm_id", how="left")

    CONF_TIER = {"SEC": 1.0, "Big Ten": 1.0, "ACC": 0.9, "Big 12": 0.9,
                 "Pac-12": 0.85, "Sun Belt": 0.5, "MAC": 0.5, "C-USA": 0.5,
                 "Mountain West": 0.55, "American": 0.55}

    def make_vector(row) -> list:
        ovr        = _f(row.get("overall_rating"), 50)
        edge_s     = _f(row.get("edge_score"), 0)
        composite  = _f(row.get("composite_score"), 0)
        trajectory = _f(row.get("trajectory_score"), 0)
        conf       = CONF_TIER.get(row.get("school") or "", 0.6)
        recency    = max(0.0, (int(row["season"]) - 2008) / 17)
        return [ovr, edge_s, composite, trajectory, conf, recency]

    import numpy as np

    by_pos: dict = defaultdict(list)
    for _, row in df.iterrows():
        by_pos[row.get("position_group") or "ATH"].append(row.to_dict())

    similar: dict = {}

    for pg, group in by_pos.items():
        if len(group) < 2:
            continue
        vectors = np.array([make_vector(r) for r in group], dtype=float)

        # Min-max normalize each column
        col_min = vectors.min(axis=0)
        col_max = vectors.max(axis=0)
        rng = col_max - col_min
        rng[rng == 0] = 1.0
        vectors = (vectors - col_min) / rng

        # Cosine similarity matrix via numpy
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        normed = vectors / norms
        sim_matrix = normed @ normed.T  # shape (n, n)

        for i, row in enumerate(group):
            scores = sim_matrix[i]
            top_idx = np.argsort(scores)[::-1]
            sims = []
            for j in top_idx:
                if j == i:
                    continue
                other = group[j]
                sims.append({
                    "id":         _i(other.get("cfb_player_id")),
                    "ps_id":      _i(other.get("player_season_id")),
                    "name":       other.get("name"),
                    "season":     other.get("season"),
                    "team":       other.get("school"),
                    "ovr":        round(_f(other.get("overall_rating"), 0), 1),
                    "similarity": round(float(scores[j]), 3),
                })
                if len(sims) == 5:
                    break
            ps_id = _i(row.get("player_season_id"))
            if ps_id is not None:
                similar[ps_id] = sims

    write_json(output_dir / "similar_players.json", similar)
    print(f"  {len(similar)} player-seasons with similarity data")


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
        rat_s = rat[rat["season"] == season][["player_season_id", "overall_rating",
                     "trajectory_score", "breakout_probability", "shap_values"]].copy()
        merged = merged.merge(rat_s, left_on="id", right_on="player_season_id",
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
    schedules: dict = {}

    for _, g in games_s.iterrows():
        home_id   = _i(g.get("home_team_id"))
        away_id   = _i(g.get("away_team_id"))
        home_sc   = _i(g.get("home_score"))
        away_sc   = _i(g.get("away_score"))
        game_date = str(g["game_date"]) if g.get("game_date") else None

        for is_home in (True, False):
            tid        = str(home_id if is_home else away_id)
            opponent   = tm_map.get(away_id if is_home else home_id)
            team_score = home_sc if is_home else away_sc
            opp_score  = away_sc if is_home else home_sc
            result     = ("W" if team_score > opp_score else ("L" if team_score < opp_score else "T")) \
                         if (team_score is not None and opp_score is not None) else None
            schedules.setdefault(tid, []).append({
                "game_id":     _i(g.get("id")),
                "week":        _i(g.get("week")),
                "game_date":   game_date,
                "season_type": g.get("season_type"),
                "is_home":     is_home,
                "neutral_site": bool(g.get("neutral_site")),
                "opponent":    opponent,
                "team_score":  team_score,
                "opp_score":   opp_score,
                "result":      result,
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
            "from_school":    row.get("from_school"),
            "to_school":      row.get("to_school"),
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

    print("similar_players.json...")
    export_similar_players(T, output_dir)

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
