"""Export pipeline data → static JSON files for cfb-analytics-app/data/.

Reads from data/raw/ (API harvest) and data/computed/ (ratings engine output).
No database required.

Every per-season file is written per season — there are no season-agnostic
players/rosters/schedules blobs. Earlier versions wrote both; the bare copies
were stale duplicates with no consumers and have been deleted.

Seasons that have not been played carry engine="projected" ratings from script
16 instead of engine="edge". ratings_for() picks the right frame per season, and
every projected row exports provenance/projection_source/projection_confidence
/projection_low/projection_high so the UI cannot render a projection as
an earned number.

Exports (season-agnostic):
  teams.json                — all teams with conference, colors, logo
  team_ratings.json         — team OVR + sub-score splits, all seasons
  team_history.json         — year-by-year progression    {team_id: [{season,...}]}
  player_transfers.json     — portal moves keyed by player
  manifest.json             — seasons, last_played_season, projected_seasons
  research/index.json       — published findings index

Exports (per season):
  players_{season}.json     — rated players (ratings, SHAP, recruiting, team, stats)
  rosters_{season}.json     — team rosters            {team_id: [...]}
  schedules_{season}.json   — games by team           {team_id: [...]}
  transfers_{season}.json   — portal moves by team    {team_id: [...]}
  ratings_by_position_{season}.json — top 50 per position group
  similar_players_{season}.json     — precomputed z-score similarity (OVR >= 55)
  team_stats_{season}.json  — per-game team metrics

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
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw, read_computed, read_ratings, COMPUTED_DIR
from utils.class_year import attach_class_year
from utils.stat_agg import META_KEYS, aggregate_game_stats, has_box_score

DEFAULT_OUTPUT = Path(__file__).parent.parent.parent / "cfb-analytics-app" / "data"
CURRENT_SEASON = 2026
TOP_N_PER_POSITION = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_stat_data(v) -> dict:
    """Return a stat blob as a dict whether stored as a dict or a JSON string; anything unparseable → {}."""
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v)
    except Exception:
        return {}


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
    """Write `data` to `path` as compact JSON after _clean_nan(), creating parent
    directories. allow_nan=False makes a surviving NaN raise here rather than
    ship as the bare `NaN` token browsers refuse to parse (the contract suite
    guards the same seam).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_clean_nan(data), f, separators=(",", ":"), default=_json_default, allow_nan=False)
    size_kb = path.stat().st_size / 1024
    n = len(data) if isinstance(data, (list, dict)) else "?"
    print(f"  Wrote {path.name} ({size_kb:.1f} KB, {n} items)")


def _json_default(o):
    """json.dump fallback: Decimal → float, numpy scalars → Python via .item(); anything else raises."""
    import decimal
    if isinstance(o, decimal.Decimal):
        return float(o)
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"Object of type {type(o)} is not JSON serializable")


def _parse_shap(val) -> dict:
    """Return stored SHAP values as a dict whether stored as a dict or a JSON string; anything else → {}."""
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

def load_live_files() -> tuple:
    """Read script 17's outputs: (live_roster, live_teams) as {season: ...}.

    live_roster[season] = {player_id: {games, live_rating, live_basis, status}} for EVERY
    rostered player; live_teams[season] = {team_id: team row} from live_{season}.json,
    plus the week they run through. Both empty for a season script 17 has not rated.
    """
    roster, teams = {}, {}
    for path in sorted(COMPUTED_DIR.glob("live_roster_*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        roster[int(doc["season"])] = {
            "through_week": doc.get("through_week"),
            "players": {int(r["player_id"]): r for r in doc["players"]},
        }
    for path in sorted(COMPUTED_DIR.glob("live_[0-9]*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        teams[int(doc["season"])] = {
            "through_week": doc.get("through_week"),
            "teams": {int(t["team_id"]): t for t in doc.get("teams", [])},
        }
    return roster, teams


def season_trio(T: dict, season: int) -> dict:
    """{player_id: fields} for a season with earned-to-date ratings on file; {} otherwise.

    The three ratings a 2026 card shows side by side, each with the reason when it
    is absent, and NEVER combined into one number:

      rating_prev      the season before, EARNED (engine=edge). A player keeps his rating
                       if he changed teams; if two rows, the higher. Status says why not:
                       rated | not_rated (offensive line) | unrated (on a roster, no
                       rating) | no_season (not on any FBS roster that year)
      overall_rating   the preseason projection (written by the caller, unchanged)
      live_*           earned so far this season, rescaled to full-season pace (script
                       17). live_status: rated | too_few_games | no_games | not_rated.

    A season with no live file gets no extra fields, so every played season exports
    exactly as before.
    """
    live = T["live_roster"].get(season)
    if not live:
        return {}
    ps, rat = T["player_seasons"], T["ratings"]
    prev_ps = ps[ps["season"] == season - 1][["id", "player_id", "position_group"]]
    prev_ids = set(prev_ps["player_id"].dropna().astype("int64"))
    ol_prev = set(prev_ps.loc[prev_ps["position_group"] == "OL", "player_id"].dropna().astype("int64"))
    prev = {}
    if not rat.empty:
        r = rat[(rat["season"] == season - 1) & rat["overall_rating"].notna()][
            ["player_season_id", "overall_rating", "rating_basis"]]
        m = r.merge(prev_ps[["id", "player_id"]], left_on="player_season_id", right_on="id")
        m = m.sort_values("overall_rating", ascending=False).drop_duplicates("player_id")
        prev = {int(a): (float(b), c) for a, b, c in zip(m["player_id"], m["overall_rating"], m["rating_basis"])}
    out = {}
    for pid, lv in live["players"].items():
        if pid in prev:
            rv, basis, status = prev[pid][0], prev[pid][1], "rated"
        else:
            rv, basis = None, None
            status = "not_rated" if pid in ol_prev else "unrated" if pid in prev_ids else "no_season"
        out[pid] = {
            "rating_prev": round(rv, 1) if rv is not None else None,
            "rating_prev_basis": basis,
            "rating_prev_status": status,
            "live_rating": lv.get("live_rating"),
            "live_basis": lv.get("live_basis"),
            "live_games": lv.get("games"),
            "live_status": lv.get("status"),
            "live_through_week": live.get("through_week"),
        }
    return out


# {(team_id, season): conference} from the harvested season records. teams.json carries only a
# team's CURRENT conference, so before v4.11 every season showed it there: Boise State was a
# Pac-12 team in 2008. Filled by load_tables(); seasons with no record (the one being played)
# fall back to the team's current conference, which is correct for them.
_CONF_BY_SEASON: dict = {}


def season_conf(team_id, season, fallback):
    """The conference a team was in during `season`, else `fallback` (its current one)."""
    return _CONF_BY_SEASON.get((_i(team_id), _i(season))) or fallback


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
        # Seasons that have not been played have no earned ratings at all; their
        # numbers come from script 16 under engine="projected". Kept as a
        # separate frame so a projected rating can never be silently mixed into
        # an earned season's export.
        "ratings_projected": read_ratings("projected"),
        "team_ratings":     read_computed("team_ratings"),
        "team_season_stats": read_computed("team_season_stats"),
    }
    t["live_roster"], t["live_teams"] = load_live_files()
    _recs = read_raw("team_records")
    _CONF_BY_SEASON.clear()
    if not _recs.empty:
        for _r in _recs.dropna(subset=["team_id", "season", "conference"]).itertuples():
            _CONF_BY_SEASON[(int(_r.team_id), int(_r.season))] = _r.conference
    for name, df in t.items():
        if isinstance(df, pd.DataFrame):
            print(f"  {name:<20} {len(df):>8} rows")
    for _s, _lv in t["live_roster"].items():
        print(f"  live_roster {_s:<8} {len(_lv['players']):>8} players, through week {_lv['through_week']}")
    # Class year, derived once here so every export below reads the same value.
    #
    # `player_seasons.year` is not a class year - see utils/class_year.py. It was
    # exported as one anyway, under the name `year`, and the site rendered it as
    # FR/SO/JR/SR: 46,610 of 71,320 rated rows with a plausible stored value said
    # "year 4", so a player card could call a true freshman a senior. The raw
    # column is dropped from the exported frame entirely rather than left beside
    # the derived one, so no consumer can pick the wrong one; a contract test
    # asserts the exports carry no `year` key.
    if not t["player_seasons"].empty:
        t["player_seasons"] = attach_class_year(
            t["player_seasons"], t["recruiting"])
        _cy = t["player_seasons"]["class_year"]
        print(f"  class_year derived for {len(_cy)} player-seasons "
              f"({int(_cy.isna().sum())} undeterminable, exported as null)")

    t["stats_index"] = build_stats_index(t["stats"])
    return t


def build_stats_index(st: pd.DataFrame) -> dict:
    """player_season_id -> season stat payload, on the same rule script 07 rates by.

    Script 07 rates a player whose season aggregate is missing or holds nothing but
    usage, by summing his game rows. Exporting only true aggregates would ship those
    players to the site with a rating and a blank stat line — a player card asserting
    an 89.9 next to no evidence for it.

    Rebuilt payloads are marked `rebuilt_from_games` so a reader (and the next phase,
    which reads usage) can tell a summed line from a reported one.
    """
    if st.empty:
        return {}

    # Keys are coerced to plain int on both sides of this lookup. A merge upstream
    # can leave player_season_id as float64, and a float key silently matches
    # nothing — which would ship every player with a blank stat line rather than
    # erroring.
    agg = st[(st["stat_type"] == "season_aggregate") & st["game_id"].isna()]
    index: dict = {}
    for r in agg[["player_season_id", "data"]].itertuples(index=False):
        ps = _i(r.player_season_id)
        if ps is not None:
            index[ps] = _parse_stat_data(r.data)

    need = {ps for ps, d in index.items() if not has_box_score(d)}
    by_ps: dict = {}
    for r in st[st["game_id"].notna()][["player_season_id", "data"]].itertuples(index=False):
        ps = _i(r.player_season_id)
        if ps is None or (ps in index and ps not in need):
            continue
        by_ps.setdefault(ps, []).append(_parse_stat_data(r.data))

    rebuilt = 0
    for ps, rows in by_ps.items():
        summed = aggregate_game_stats(rows)
        if not summed:
            continue
        # Usage, PPA and awards ride on endpoints the game rows never carried.
        for k, v in (index.get(ps) or {}).items():
            if k in META_KEYS and v:
                summed[k] = v
        summed["rebuilt_from_games"] = True
        index[ps] = summed
        rebuilt += 1

    print(f"  stats_index          {len(index):>8} player-seasons ({rebuilt} rebuilt from game rows)")
    return index


def is_projected_season(T: dict, season: int) -> bool:
    """True when a season's ratings come from the projection engine.

    Derived from the data rather than a constant, so the app cannot drift out of
    step with what the pipeline actually produced.
    """
    rp = T.get("ratings_projected")
    if rp is None or rp.empty:
        return False
    return bool((rp["season"] == season).any())


def ratings_for(T: dict, season: int) -> pd.DataFrame:
    """The rating frame that applies to a season — projected or earned."""
    return T["ratings_projected"] if is_projected_season(T, season) else T["ratings"]


# ---------------------------------------------------------------------------
# Export: players.json
# ---------------------------------------------------------------------------

def export_players(T: dict, output_dir: Path, season: int) -> None:
    """Write players_{season}.json — one row per rated player-season, sorted by OVR.

    The rating frame is ratings_for(season): projected for an unplayed season,
    earned otherwise, never both. Joins, in order: player_seasons (the ONLY
    correct route from a rating to a player — ratings.player_id is a legacy
    internal key), players, teams, the best recruiting row per player (highest
    composite, whichever class it came from), player_edge (EDGE, archetypes,
    coverage_share) and season/postseason stat blobs.

    Rows with no player name are dropped. `class_year` is the DERIVED class
    year from utils/class_year.py, not player_seasons.year. Projected rows
    carry provenance, projection_source/confidence/low/high so the
    UI cannot present them as earned.
    """
    rat = ratings_for(T, season)
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

    # A season with earned-to-date ratings also lists anyone who has a live or a
    # previous-season rating but no projection (a late arrival, a walk-on who has
    # played): the grid is "everyone with a rating of any of the three kinds".
    trio = season_trio(T, season)
    if trio:
        have = set(ratings_season["player_season_id"].dropna().astype("int64"))
        extra = [int(r.id) for r in ps[ps["season"] == season].itertuples()
                 if int(r.id) not in have and pd.notna(r.player_id)
                 and (trio.get(int(r.player_id), {}).get("rating_prev") is not None
                      or trio.get(int(r.player_id), {}).get("live_rating") is not None)]
        if extra:
            # No projection was made for these players, and no rating of ours is attached
            # to that absence: provenance "not_projected", the same stated non-projection
            # the offensive line carries.
            ratings_season = pd.concat(
                [ratings_season, pd.DataFrame({"player_season_id": extra, "season": season,
                                               "provenance": "not_projected"})],
                ignore_index=True)

    # ratings.player_id is the Supabase internal PK — NOT the CFB API player id.
    # The correct CFB API player id lives in player_seasons.player_id.
    # Join path: ratings.player_season_id → player_seasons.id → player_seasons.player_id → players.id
    ps_slim = ps[["id", "player_id", "position_group", "class_year", "class_year_basis", "team_id"]] \
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

    # EDGE — edge_score lives in player_edge.json, NOT in the ratings table. It
    # was previously omitted here, so every exported player had edge_score=null
    # and the frontend's EDGE column rendered an em-dash for all 8,437 rows.
    if not edge.empty:
        _ecols = ["player_season_id", "edge_score", "stats_measured", "games_played"]
        # A defensive back's overall IS his three archetypes, so the card has to
        # be able to show them beside it.
        _ecols += [c for c in ("archetypes", "archetype", "coverage_share")
                   if c in edge.columns]
        edge_slim = edge[_ecols] \
                        .rename(columns={"player_season_id": "edge_ps_id"})
        ratings_season = ratings_season.merge(edge_slim, left_on="player_season_id", right_on="edge_ps_id", how="left")
    else:
        ratings_season["edge_score"] = None
        ratings_season["stats_measured"] = None
        ratings_season["games_played"] = None

    # Stats — the aggregate the API reported, or the sum of game rows script 07
    # rated the player on. Built once in load_tables().
    st = T["stats"]
    if not st.empty:
        _sidx = T.get("stats_index") or {}
        ratings_season["stats_season"] = [
            _sidx.get(_i(v)) for v in ratings_season["player_season_id"]]

        st_post = st[(st["stat_type"] == "postseason_aggregate") & st["game_id"].isna()] \
                    [["player_season_id", "data"]].copy()
        st_post["data"] = st_post["data"].apply(_parse_stat_data)
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
            # The DERIVED class year, null where undeterminable. Never the raw
            # `player_seasons.year`, which is not a class year at all: it is
            # constant across the career for 84.0% of multi-season players and
            # says year 4 for 65% of the rated rows that carry a plausible value.
            "class_year":       _i(row.get("class_year")),
            "class_year_basis": row.get("class_year_basis"),
            "height_in":        _i(row.get("height_in")),
            "weight_lbs":       _i(row.get("weight_lbs")),
            "hometown_state":   row.get("hometown_state"),
            "team_id":          _i(row.get("team_id")),
            "team":             row.get("school"),
            "team_abbr":        row.get("abbreviation"),
            "conference":       season_conf(row.get("team_id"), season, row.get("conference")),
            "team_color":       row.get("color"),
            "logo_url":         row.get("logo_url"),
            "overall_rating":   _f(row.get("overall_rating")),
            "position_rating":  _f(row.get("position_rating")),
            # A withheld rating is not a missing one. OL carries rating_status
            # "not_rated" with the reason attached, so the UI renders a stated
            # refusal instead of a blank that reads as a bug.
            "rating_status":    row.get("rating_status"),
            # What the number is BUILT from, derived from the playing-time tier
            # (v4.5). production / blended / recruiting / withheld. A backup's 54
            # and a starter's 54 are different claims — the backup's IS
            # `pos_avg + stars_delta`, a six-valued step function — and until this
            # shipped nothing on the page distinguished them.
            "rating_basis":     row.get("rating_basis"),
            "not_rated_reason": row.get("not_rated_reason"),
            "trajectory":       _f(row.get("trajectory_score")),
            "breakout_prob":    _f(row.get("breakout_probability")),
            "shap":             _parse_shap(row.get("shap_values")),
            "stars":            _i(row.get("stars")),
            "composite_score":  _f(row.get("composite_score")),
            "recruit_year":     _i(row.get("recruit_year")),
            "season":           season,
            "edge_score":       _f(row.get("edge_score")),
            "archetypes":       row.get("archetypes") if isinstance(row.get("archetypes"), dict) else None,
            "archetype":        row.get("archetype") if isinstance(row.get("archetype"), str) else None,
            "coverage_share":   _f(row.get("coverage_share")),
            "stats_measured":   _i(row.get("stats_measured")),
            "games_played":     _i(row.get("games_played")),
            "stats_season":     row.get("stats_season") if isinstance(row.get("stats_season"), dict) else None,
            "stats_postseason": row.get("stats_postseason") if isinstance(row.get("stats_postseason"), dict) else None,
            # Provenance travels with the rating so the UI can never render a
            # projection as an earned number. Null on played seasons.
            "provenance":            row.get("provenance"),
            "projection_source":     row.get("projection_source"),
            "projection_confidence": row.get("projection_confidence"),
            "projection_low":        _f(row.get("projection_low")),
            "projection_high":       _f(row.get("projection_high")),
            **trio.get(_i(row.get("cfb_player_id")), {}),
        })

    players.sort(key=lambda p: p["overall_rating"] or 0, reverse=True)
    write_json(output_dir / f"players_{season}.json", players)


# ---------------------------------------------------------------------------
# Export: teams.json
# ---------------------------------------------------------------------------

def export_teams(T: dict, output_dir: Path, season: int) -> None:
    """Write teams.json — every team's identity (school, colours, logo, stadium)
    plus `player_count`, the number of rated player-seasons in `season`.
    `conference` here is the team's CURRENT conference; per-season conference
    lives in team_history.json.
    """
    tm  = T["teams"]
    rat = ratings_for(T, season)
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
            # Ranking inputs (utils/results_rating.py): how well the team has done against its
            # schedule, and the power rating that combines that with the team rating.
            "results_rating":   _f(r.get("results_rating")),
            "results_games":    _i(r.get("results_games")),
            "power_rating":     _f(r.get("power_rating")),
            "rating_part":      _f(r.get("rating_part")),
            "conf_part":        _f(r.get("conf_part")),
            "home_edge":        _f(r.get("home_edge")),
            "school":           r.get("school"),
            "conference":       season_conf(r.get("team_id"), r.get("season"), r.get("conference")),
            "color":            r.get("color"),
            "logo_url":         r.get("logo_url"),
            "sub_ratings":      sub,
        }
        # Hoist split keys for frontend. line_unit is here rather than only
        # inside sub_ratings because the team page reads it directly — it is the
        # replacement for the withdrawn per-lineman rating and needs to be as
        # easy to reach as any other split.
        for k in ("pass_off", "run_off", "pass_def", "run_def", "special_teams",
                  "line_unit", "line_unit_inputs"):
            row[k] = sub.get(k)
        row["sp_plus"]       = sub.get("sp_offense_scaled") or sub.get("sp_overall_scaled")
        row["recruit_score"] = sub.get("recruiting_scaled")
        # A season being played: the earned-to-date team rating beside the projection.
        lt = T["live_teams"].get(_i(r.get("season")), {})
        lrow = lt.get("teams", {}).get(_i(r.get("team_id")))
        if lrow:
            row["live_overall_rating"] = _f(lrow.get("overall_rating"))
            row["live_offense_rating"] = _f(lrow.get("offense_rating"))
            row["live_defense_rating"] = _f(lrow.get("defense_rating"))
            row["live_power_rating"]   = _f(lrow.get("power_rating"))
            row["live_results_rating"] = _f(lrow.get("results_rating"))
            row["live_results_games"]  = _i(lrow.get("results_games"))
            row["live_rating_part"]    = _f(lrow.get("rating_part"))
            row["live_conf_part"]      = _f(lrow.get("conf_part"))
            row["live_home_edge"]      = _f(lrow.get("home_edge"))
            row["live_through_week"]   = lt.get("through_week")
        rows.append(row)

    write_json(output_dir / "team_ratings.json", rows)
    print(f"  {len(rows)} team_ratings rows exported")


# ---------------------------------------------------------------------------
# Export: ratings_by_position.json
# ---------------------------------------------------------------------------

def export_ratings_by_position(T: dict, output_dir: Path, season: int) -> None:
    """Write ratings_by_position_{season}.json — {position_group: top 50 by OVR}.

    Uses the same projected-or-earned frame as export_players(). Rows with a
    null OVR sort last but still occupy slots, so a group with fewer than 50
    rated players can carry unrated rows at the bottom.
    """
    rat = ratings_for(T, season)
    ps  = T["player_seasons"]
    pl  = T["players"]
    tm  = T["teams"]
    rec = T["recruiting"]

    if rat.empty:
        return

    # ratings.player_id is Supabase internal; use player_seasons.player_id (CFB API id)
    ratings_season = rat[rat["season"] == season].copy()
    ratings_season = ratings_season.merge(ps[["id", "player_id", "team_id", "position_group", "class_year"]]
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
            "class_year":      _i(row.get("class_year")),
            "team":            row.get("school"),
            "team_abbr":       row.get("abbreviation"),
            "conference":      season_conf(row.get("team_id"), season, row.get("conference")),
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
    """Write rosters_{season}.json — {team_id: [player, …]} for EVERY player on a
    roster that season, rated or not, sorted by OVR.

    Starts from player_seasons (not ratings), so reserves and withheld OL
    appear with a null OVR and their rating_status / rating_basis. Projection
    columns are selected only if the frame has them — they exist only on the
    projected engine. Players with no team_id are dropped.
    """
    ps  = T["player_seasons"]
    pl  = T["players"]
    rat = ratings_for(T, season)
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
        # Projection columns only exist on the projected engine, so select them
        # conditionally — omitting them here silently exported every projected
        # roster row with a null source.
        want = ["player_season_id", "overall_rating", "trajectory_score",
                "breakout_probability", "shap_values",
                "provenance", "projection_source", "projection_confidence",
                "projection_low", "projection_high"]
        cols = [c for c in want if c in rat.columns]
        ratings_season = rat[rat["season"] == season][cols].copy()
        merged = merged.merge(ratings_season, left_on="id", right_on="player_season_id",
                              how="left", suffixes=("", "_rat"))

    edge = T.get("player_edge", pd.DataFrame())
    if not edge.empty and "edge_score" in edge.columns:
        _rcols = ["player_season_id", "edge_score"]
        _rcols += [c for c in ("archetypes", "archetype") if c in edge.columns]
        edge_s = edge[_rcols].rename(
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
    trio = season_trio(T, season)

    rosters: dict = {}
    for _, row in merged.iterrows():
        tid = str(_i(row["team_id"]))
        rosters.setdefault(tid, []).append({
            "player_id":        _i(row["player_id"]),
            "player_season_id": _i(row["id"]),
            "name":             row.get("name"),
            "position_group":   row.get("position_group"),
            "class_year":       _i(row.get("class_year")),
            "height_in":        _i(row.get("height_in")),
            "weight_lbs":       _i(row.get("weight_lbs")),
            "hometown_state":   row.get("hometown_state"),
            "overall_rating":   _f(row.get("overall_rating")),
            "rating_status":    row.get("rating_status"),
            # What the number is BUILT from, derived from the playing-time tier
            # (v4.5). production / blended / recruiting / withheld. A backup's 54
            # and a starter's 54 are different claims — the backup's IS
            # `pos_avg + stars_delta`, a six-valued step function — and until this
            # shipped nothing on the page distinguished them.
            "rating_basis":     row.get("rating_basis"),
            "not_rated_reason": row.get("not_rated_reason"),
            "trajectory":       _f(row.get("trajectory_score")),
            "breakout_prob":    _f(row.get("breakout_probability")),
            "shap":             _parse_shap(row.get("shap_values")),
            "stars":            _i(row.get("rec_stars")),
            "composite_score":  _f(row.get("rec_composite")),
            "edge_score":       _f(row.get("edge_score")),
            "provenance":            row.get("provenance"),
            "projection_source":     row.get("projection_source"),
            "projection_confidence": row.get("projection_confidence"),
            "projection_low":        _f(row.get("projection_low")),
            "projection_high":       _f(row.get("projection_high")),
            **trio.get(_i(row["player_id"]), {}),
        })

    for tid in rosters:
        rosters[tid].sort(key=lambda p: p["overall_rating"] or 0, reverse=True)

    write_json(output_dir / f"rosters_{season}.json", rosters)


# ---------------------------------------------------------------------------
# Export: schedules.json
# ---------------------------------------------------------------------------

def export_schedules(T: dict, output_dir: Path, season: int) -> None:
    """Write schedules_{season}.json — {team_id: [game, …]}, each game from that
    team's side.

    An opponent with no team_id of ours (FCS) keeps the name harvested with
    the game and gets opp_is_fbs=False so the UI names it without linking it.
    A null opponent score counts as 0 when deciding W/L but is exported as
    null. Unplayed games have result=None.
    """
    games = T["games"]
    teams = T["teams"]

    if games.empty:
        return

    games_s = games[games["season"] == season]
    if games_s.empty:
        print(f"  schedules_{season}.json: no games for this season")
        return

    tm_map = dict(zip(teams["id"], teams["school"])) if not teams.empty else {}

    def _team_name(tid, stored_name):
        """Resolve an opponent, falling back to the name harvested with the game.

        FCS opponents have no row in our teams table, so tid is None for them.
        The name harvested alongside the game is what keeps them from rendering
        as "TBD" — they are known opponents, just not rated ones.
        """
        if tid is not None and tm_map.get(tid):
            return tm_map[tid]
        return stored_name or None

    schedules: dict = {}

    for _, g in games_s.iterrows():
        home_id   = _i(g.get("home_team_id"))
        away_id   = _i(g.get("away_team_id"))
        home_sc   = _i(g.get("home_score"))
        away_sc   = _i(g.get("away_score"))
        game_date = str(g["game_date"]) if g.get("game_date") else None
        venue     = g.get("venue") if isinstance(g.get("venue"), str) and g.get("venue") else None

        # For result calc: FCS opponent scores may be null — treat as 0 for W/L but
        # still store null in opp_score so UI can display "—" rather than "0".
        for is_home in (True, False):
            team_id_val = home_id if is_home else away_id
            if team_id_val is None:
                continue  # skip games where this side has no known team_id
            tid        = str(team_id_val)
            opp_id     = away_id if is_home else home_id
            opp_name   = g.get("away_team_name") if is_home else g.get("home_team_name")
            opponent   = _team_name(opp_id, opp_name)
            # An opponent with no id of ours is FCS (or otherwise unrated); the
            # UI needs to know not to try linking it to a team page.
            opp_is_fbs = opp_id is not None and opp_id in tm_map
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
                "venue":        venue,
                "is_home":      is_home,
                "neutral_site": bool(g.get("neutral_site")),
                "opponent":     opponent,
                "opp_is_fbs":   opp_is_fbs,
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
    """Write transfers_{season}.json — {team_id: [transfer, …]}, each move listed
    under BOTH schools with direction "out" (origin) and "in" (destination).

    ovr_previous is the EARNED rating at the origin school the season before;
    ovr_current is the earned rating at the destination this season. Only the
    earned frame is read, so for an unplayed season ovr_current is always null —
    a projection is never shown as a transfer's result. Unresolved schools
    export as "Unknown school"; unlinked transfers are skipped.
    """
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

    merged = tr_s.merge(pl[["id", "name", "hometown_state"]].rename(columns={"id": "pl_id"}),
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
            "hometown_state": row.get("hometown_state") if isinstance(row.get("hometown_state"), str) else None,
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
    """Write team_history.json — {team_id: [season row, …]} newest first, across
    every season with a team rating.

    W/L is counted from every game with both scores (postseason included; ties
    count for neither). Conference is resolved per season via
    _build_conf_history(), whose realignment overrides are a frozen constant.
    """
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
            "conference":       season_conf(tid, sea, get_conf(tid, sea)),
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
    """Write team_stats_{season}.json — {team_id: per-game team metrics} from
    data/computed/team_season_stats, NaN → null. Not written for a season with
    no rows, which includes every unplayed season.
    """
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

def export_manifest(T: dict, output_dir: Path) -> None:
    """One source of truth for which seasons exist and which are projected.

    The frontend previously hardcoded its own CURRENT_SEASON, which drifted from
    the pipeline's (2025 vs 2026). A contract test asserts these agree.

    Always describes EVERY season on disk, never just the subset this run
    exported — a `--season 2026` run once rewrote the manifest as
    first_season=2026, which would have told the frontend that eighteen seasons
    of data did not exist.
    """
    seasons = sorted(int(p.stem.split("_")[-1]) for p in output_dir.glob("players_*.json"))
    if not seasons:
        seasons = list(ALL_SEASONS)
    projected = sorted(s for s in seasons if is_projected_season(T, s))
    played = [s for s in seasons if s not in projected]
    manifest = {
        "generated_at":       datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "first_season":       min(seasons),
        "last_played_season": max(played) if played else max(seasons),
        "current_season":     max(seasons),
        "projected_seasons":  projected,
        "seasons":            sorted(seasons),
    }
    write_json(output_dir / "manifest.json", manifest)
    print(f"    first={manifest['first_season']} "
          f"last_played={manifest['last_played_season']} "
          f"current={manifest['current_season']} projected={projected}")


def export_research(T: dict, output_dir: Path) -> None:
    """Write data/research/index.json plus one research/{key}.json per
    research_cache entry. research_cache is currently empty, so this writes
    `[]` — see the backlog item in CLAUDE.md §6 (wire it or delete it).
    """
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


CARD_SHARDS = 1024                # cards/NNNN.json holds the players whose id % CARD_SHARDS == NNNN (js/config.js CARD_SHARDS agrees)
CARD_DROP = {"logo_url"}          # the only field a card does not need; everything else ships as the grid has it
POWER_GAMES_FIRST_SEASON = 2016   # power_games_{season}.json is exported from here on (and for a season in progress)


def export_power_games(T: dict, output_dir: Path, season: int) -> None:
    """Write power_games_{season}.json: for each team, every game that went into its power rating.

    This is the explanation of the ranking. Each entry is one game between two FBS teams, from the
    team's side. Regular-season games are what power counts; postseason games (bowls and the
    playoff) are listed after them with `post: true` and are shown, not counted, until the
    strength model decides how they should weigh. Each entry has what it was worth (the margin
    capped at +-28 plus 8 for a win or minus 8 for a loss), what the two teams' ratings expected it
    to be worth, and the difference (`over`). A team's results part is roughly the average of its `over` values, shrunk toward 0 for
    few games. Seasons before POWER_GAMES_FIRST_SEASON are not exported.
    """
    from utils.results_rating import expected_value, game_value

    if season < POWER_GAMES_FIRST_SEASON and season not in T["live_teams"]:
        return
    parts: dict = {}
    home_edge = 0.0
    lt = T["live_teams"].get(season)
    if lt:
        for tid, lrow in lt.get("teams", {}).items():
            if lrow.get("rating_part") is not None:
                parts[int(tid)] = {"rating_part": lrow["rating_part"], "conf_part": lrow.get("conf_part") or 0.0}
                home_edge = lrow.get("home_edge") or home_edge
    else:
        tr = T["team_ratings"]
        rows = tr[(tr["season"] == season) & (tr["engine"].fillna("edge") != "projected")] if "engine" in tr.columns else tr[tr["season"] == season]
        for _, r in rows.iterrows():
            if _f(r.get("rating_part")) is not None:
                parts[int(r["team_id"])] = {"rating_part": float(r["rating_part"]), "conf_part": float(r.get("conf_part") or 0.0)}
                home_edge = _f(r.get("home_edge")) or home_edge
    games = T["games"]
    if not parts or games.empty:
        return
    names = dict(zip(T["teams"]["id"], T["teams"]["school"]))
    g = games[(games["season"] == season) & (games["season_type"].isin(["regular", "postseason"]))]
    out: dict = {}
    for _, x in g.iterrows():
        h, a = _i(x.get("home_team_id")), _i(x.get("away_team_id"))
        hs, as_ = _i(x.get("home_score")), _i(x.get("away_score"))
        if h not in parts or a not in parts or hs is None or as_ is None:
            continue
        neutral = bool(x.get("neutral_site") == True)   # noqa: E712
        exp_home = expected_value(parts[h], parts[a], home_edge, neutral)
        m = float(hs - as_)
        for tid, opp, sign, is_home, sc, osc in ((h, a, 1.0, True, hs, as_), (a, h, -1.0, False, as_, hs)):
            val = game_value(sign * m)
            exp = sign * exp_home
            out.setdefault(str(tid), []).append({
                "week": _i(x.get("week")), "opp_id": opp, "opp": names.get(opp), "home": is_home and not neutral,
                "neutral": neutral, "score": sc, "opp_score": osc, "margin": int(sign * m),
                "value": round(val, 1), "expected": round(exp, 1), "over": round(val - exp, 1),
                **({"post": True} if x.get("season_type") == "postseason" else {}),
            })
    for tid in out:
        out[tid].sort(key=lambda e: (bool(e.get("post")), e["week"] is None, e["week"] or 0))
    write_json(output_dir / f"power_games_{season}.json", out)


def export_player_cards(output_dir: Path) -> None:
    """Write cards/{shard:04d}.json: every season of each rated player's ratings row, grouped by player id.

    A player card needs one player's whole career (the season stats, the rating history, the
    career table). Reading that from players_{season}.json costs every season's file: measured
    2026-10-28, opening one card downloaded 82-93 MB across 15 requests, and the classic modal
    19 files (about 200 MB). A shard is {player_id: [row per season, oldest first]} for the players
    whose id % CARD_SHARDS equals the file's number, about 140 KB. The rows are the grid's rows
    unchanged (read back from the files just written, so a card cannot disagree with the grid)
    minus CARD_DROP. A rostered player with no ratings row has no entry; the card is drawn from
    the roster row instead.
    """
    shards: dict = defaultdict(dict)
    n_rows = 0
    for season in ALL_SEASONS:
        f = output_dir / f"players_{season}.json"
        if not f.exists():
            continue
        for p in json.loads(f.read_text(encoding="utf-8")):
            pid = p.get("id")
            if pid is None:
                continue
            shards[int(pid) % CARD_SHARDS].setdefault(str(pid), []).append({k: v for k, v in p.items() if k not in CARD_DROP})
            n_rows += 1
    cards_dir = output_dir / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    for old in cards_dir.glob("*.json"):      # this directory belongs to this function; a shard that empties is removed
        old.unlink()
    total = 0
    for shard, players in shards.items():
        path = cards_dir / f"{shard:04d}.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_clean_nan(players), fh, separators=(",", ":"), default=_json_default, allow_nan=False)
        total += path.stat().st_size
    print(f"  Wrote cards/: {len(shards)} shards, {sum(len(v) for v in shards.values())} players, {n_rows} player-seasons, {total / 1e6:.0f} MB")


def main():
    """CLI entry point: export every season (ALL_SEASONS) or --season N into
    --output (default: the sibling app's data/).

    Season-agnostic files first (teams, team ratings, history, similar players,
    player transfers, manifest, research), then the six per-season
    files. Tables are loaded once. Note that teams.json's player_count reflects
    the LAST season in the run.
    """
    parser = argparse.ArgumentParser(description="Export local data -> static JSON for frontend")
    parser.add_argument("--season", type=int, default=None,
                        help="Single season to export (default: all seasons)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cards", action="store_true",
                        help="also rebuild cards/ on a single-season run (a full run always does)")
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

    print("manifest.json...")
    export_manifest(T, output_dir)

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

        print(f"  power_games_{season}.json...")
        export_power_games(T, output_dir, season)

    if args.season is None or args.cards:
        print("\ncards/ (one shard file per player-id group)...")
        export_player_cards(output_dir)

    print("\nDone.")


if __name__ == "__main__":
    main()
