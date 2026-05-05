"""Fetch teams, rosters, games, and player stats from CFB Data API → upsert to Supabase.

Run order:
    python scripts/01_fetch_cfb_api.py                  # primary seasons (2021-2025)
    python scripts/01_fetch_cfb_api.py --historical      # backfill 2005-2020
    python scripts/01_fetch_cfb_api.py --year 2024       # single year

Name-matching helpers ported from cfb-analytics-v1-archived/data-pipeline/fetch_and_rate.py.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.api_client import (
    load_api_key,
    fetch_teams,
    fetch_all_rosters,
    fetch_player_stats,
    fetch_player_stats_postseason,
    fetch_ppa,
    fetch_team_stats,
    fetch_sp_ratings,
    fetch_player_usage,
    fetch_awards,
    fetch_games,
    fetch_all_game_player_stats,
    fetch_transfer_portal,
    fetch_plays,
)
from utils.db import bulk_upsert, get_connection

SEASONS_PRIMARY    = list(range(2021, 2026))   # 2021–2025
SEASONS_HISTORICAL = list(range(2005, 2021))   # 2005–2020 backfill

# Position group normalization (raw API position → canonical group)
POSITION_GROUP_MAP = {
    "QB": "QB", "RB": "RB", "FB": "RB",
    "WR": "WR", "TE": "TE",
    "OL": "OL", "OT": "OL", "OG": "OL", "C": "OL",
    "DL": "DL", "DE": "DL", "DT": "DL", "NT": "DL",
    "LB": "LB", "ILB": "LB", "OLB": "LB",
    "DB": "DB", "CB": "DB", "S": "DB", "SAF": "DB", "FS": "DB", "SS": "DB",
    "K": "K", "P": "P", "LS": "LS",
    "ATH": "ATH",
}


# ---------------------------------------------------------------------------
# Name-matching helpers (ported from v1 fetch_and_rate.py)
# ---------------------------------------------------------------------------

def build_stat_lookup(player_stats_raw: list) -> dict:
    """Build {(name_lower, team_lower): {stat_name: value}} for fast lookup."""
    lookup: dict = {}
    for entry in player_stats_raw:
        player_name = entry.get("player", "")
        team = entry.get("team", "")
        key = (player_name.lower(), team.lower())
        if key not in lookup:
            lookup[key] = {}
        cat = entry.get("category", "")
        stat_type = entry.get("statType", "")
        stat_name = cat + stat_type[0].upper() + stat_type[1:] if stat_type else cat
        lookup[key][stat_name] = entry.get("stat", 0)
    return lookup


def find_player_stats(stat_lookup: dict, first_name: str, last_name: str, team: str) -> dict:
    """Try multiple name variants to find a player's stats."""
    team_l = team.lower()
    full = f"{first_name} {last_name}".lower()
    if (full, team_l) in stat_lookup:
        return stat_lookup[(full, team_l)]
    # Nickname/prefix match on first name
    for (name, t), stats in stat_lookup.items():
        if t != team_l:
            continue
        parts = name.split(" ", 1)
        if len(parts) == 2 and parts[1] == last_name.lower():
            rf = first_name.lower()
            sf = parts[0]
            if rf.startswith(sf) or sf.startswith(rf):
                return stats
    return {}


def build_ppa_lookup(ppa_raw: list) -> dict:
    lookup: dict = {}
    for entry in ppa_raw:
        name = entry.get("name", "")
        team = entry.get("team", "")
        avg_ppa = entry.get("averagePPA", {})
        val = avg_ppa.get("all", 0) if isinstance(avg_ppa, dict) else (avg_ppa or 0)
        try:
            lookup[(name.lower(), team.lower())] = float(val)
        except (TypeError, ValueError):
            lookup[(name.lower(), team.lower())] = 0.0
    return lookup


def find_player_ppa(ppa_lookup: dict, first_name: str, last_name: str, team: str) -> float:
    team_l = team.lower()
    full = f"{first_name} {last_name}".lower()
    if (full, team_l) in ppa_lookup:
        return ppa_lookup[(full, team_l)]
    for (name, t), val in ppa_lookup.items():
        if t != team_l:
            continue
        parts = name.split(" ", 1)
        if len(parts) == 2 and parts[1] == last_name.lower():
            rf = first_name.lower()
            sf = parts[0]
            if rf.startswith(sf) or sf.startswith(rf):
                return val
    return 0.0


def build_usage_lookup(usage_raw: list) -> dict:
    lookup: dict = {}
    for entry in usage_raw:
        pid = entry.get("id")
        if not pid:
            continue
        usage = entry.get("usage") or {}
        lookup[int(pid)] = {
            "overall": float(usage.get("overall") or 0),
            "pass":    float(usage.get("pass") or 0),
            "rush":    float(usage.get("rush") or 0),
            "games":   int(entry.get("games") or 0),
        }
    return lookup


def build_awards_lookup(awards_raw: list) -> dict:
    """Map (name_lower, team_lower) → award tier (3=All-American, 2=All-Conf1st, 1=All-Conf2nd)."""
    lookup: dict = {}
    for entry in awards_raw:
        name = (entry.get("player") or entry.get("name") or "").lower().strip()
        team = (entry.get("team") or "").lower().strip()
        award = (entry.get("award") or entry.get("category") or "").lower()
        if not name or not award:
            continue
        if any(x in award for x in ["all-american", "outland", "rimington", "lombardi", "bednarik"]):
            tier = 3
        elif "all-" in award and any(x in award for x in ["first", "1st"]):
            tier = 2
        elif "all-" in award and any(x in award for x in ["second", "2nd", "honorable"]):
            tier = 1
        else:
            continue
        key = (name, team)
        if key not in lookup or lookup[key] < tier:
            lookup[key] = tier
    return lookup


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------

def upsert_teams(teams_raw: list) -> dict:
    """Upsert teams, return {school_lower: db_id} mapping."""
    rows = []
    for t in teams_raw:
        rows.append({
            "cfb_api_id":   t.get("id"),
            "school":       t.get("school"),
            "mascot":       t.get("mascot"),
            "abbreviation": t.get("abbreviation"),
            "conference":   t.get("conference"),
            "division":     t.get("classification"),
            "color":        t.get("color"),
            "alt_color":    t.get("alt_color"),
            "logo_url":     (t.get("logos") or [None])[0],
            "stadium_name": (t.get("location") or {}).get("name"),
            "city":         (t.get("location") or {}).get("city"),
            "state":        (t.get("location") or {}).get("state"),
            "capacity":     (t.get("location") or {}).get("capacity"),
        })
    if rows:
        bulk_upsert("teams", rows, "school")
        print(f"  Upserted {len(rows)} teams")

    from utils.supabase_client import get_client
    client = get_client()
    result = client.table("teams").select("id, school").execute()
    return {r["school"].lower(): r["id"] for r in result.data}


def upsert_players(rosters_by_team: dict, team_id_map: dict, season: int) -> tuple[dict, dict]:
    """Upsert player identity rows and player_seasons rows.

    Returns:
        player_id_map: {cfb_api_id: db_player_id}
        ps_id_map:     {db_player_id: player_season_id}  (for this season)
    """
    import psycopg2.extras

    # --- Step 1: upsert identity rows (no team, no year) ---
    identity_rows = []
    for team_name, players in rosters_by_team.items():
        for p in players:
            api_id = p.get("id")
            name = f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()
            if not api_id or not name:
                continue
            identity_rows.append({
                "cfb_api_id":        api_id,
                "name":              name,
                "height_in":         p.get("height"),
                "weight_lbs":        p.get("weight"),
                "hometown":          p.get("homeCity"),
                "hometown_state":    p.get("homeState"),
                "hometown_country":  p.get("homeCountry"),
            })

    # Deduplicate by cfb_api_id — keep last occurrence (most recent data)
    seen_identity: dict = {}
    for r in identity_rows:
        seen_identity[r["cfb_api_id"]] = r
    identity_rows = list(seen_identity.values())

    if identity_rows:
        bulk_upsert("players", identity_rows, "cfb_api_id")
        print(f"  Upserted {len(identity_rows)} players (identity)")

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, cfb_api_id FROM players WHERE cfb_api_id IS NOT NULL")
        player_id_map = {row[1]: row[0] for row in cur.fetchall()}

    # --- Step 2: upsert player_seasons rows ---
    ps_rows = []
    for team_name, players in rosters_by_team.items():
        team_id = team_id_map.get(team_name.lower())
        for p in players:
            api_id = p.get("id")
            db_id  = player_id_map.get(int(api_id)) if api_id else None
            if not db_id or not team_id:
                continue
            raw_pos = p.get("position", "") or ""
            ps_rows.append({
                "player_id":      db_id,
                "season":         season,
                "team_id":        team_id,
                "position":       raw_pos,
                "position_group": POSITION_GROUP_MAP.get(raw_pos.upper(), "ATH"),
                "year":           p.get("year"),
            })

    # A player can appear on multiple teams in one season (mid-season transfers are rare
    # but possible). Keep all unique (player_id, season, team_id) combos.
    seen_ps: dict = {}
    for r in ps_rows:
        seen_ps[(r["player_id"], r["season"], r["team_id"])] = r
    ps_rows = list(seen_ps.values())

    if ps_rows:
        sql = """
            INSERT INTO player_seasons (player_id, season, team_id, position, position_group, year)
            VALUES %s
            ON CONFLICT (player_id, season, team_id) DO UPDATE
                SET position       = EXCLUDED.position,
                    position_group = EXCLUDED.position_group,
                    year           = EXCLUDED.year,
                    updated_at     = now()
        """
        tmpl = "(%(player_id)s, %(season)s, %(team_id)s, %(position)s, %(position_group)s, %(year)s)"
        with get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, ps_rows, template=tmpl, page_size=500)
        print(f"  Upserted {len(ps_rows)} player_seasons rows")

    # Return ps_id_map: {player_id: player_season_id} for this season
    # For players on multiple teams this season, pick the one with the most stats (resolved later);
    # here we just return the first/only match per player for this season.
    ps_id_map: dict = {}
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT player_id, id FROM player_seasons WHERE season = %s",
            (season,)
        )
        for player_id, ps_id in cur.fetchall():
            # If a player transferred mid-season and has two rows, last one wins here.
            # upsert_season_stats will resolve correctly via the ps lookup.
            ps_id_map[player_id] = ps_id

    return player_id_map, ps_id_map


def upsert_games(games_raw: list, team_id_map: dict, season: int) -> dict:
    """Upsert games; return {cfb_api_id: db_id}."""
    rows = []
    for g in games_raw:
        home = (g.get("homeTeam") or g.get("home_team") or "").lower()
        away = (g.get("awayTeam") or g.get("away_team") or "").lower()
        rows.append({
            "cfb_api_id":    g.get("id"),
            "season":        season,
            "week":          g.get("week"),
            "season_type":   g.get("_seasonType", "regular"),
            "home_team_id":  team_id_map.get(home),
            "away_team_id":  team_id_map.get(away),
            "home_score":    g.get("homePoints") or g.get("home_points"),
            "away_score":    g.get("awayPoints") or g.get("away_points"),
            "neutral_site":  g.get("neutralSite", False),
            "game_date":     (g.get("startDate") or g.get("start_date") or "")[:10] or None,
            "venue":         g.get("venue"),
        })
    valid_rows = [r for r in rows if r.get("cfb_api_id")]
    if valid_rows:
        bulk_upsert("games", valid_rows, "cfb_api_id")
        print(f"  Upserted {len(valid_rows)} games")

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, cfb_api_id FROM games WHERE season = %s AND cfb_api_id IS NOT NULL", (season,))
        return {row[1]: row[0] for row in cur.fetchall()}


def _build_ps_lookup(season: int, team_id_map: dict) -> dict:
    """Return {(player_id, team_id): player_season_id} for a given season."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT player_id, team_id, id FROM player_seasons WHERE season = %s",
            (season,)
        )
        return {(pid, tid): ps_id for pid, tid, ps_id in cur.fetchall()}


def upsert_season_stats(
    player_stats_raw: list,
    ppa_raw: list,
    usage_raw: list,
    awards_raw: list,
    player_id_map: dict,
    ps_id_map: dict,
    rosters_by_team: dict,
    team_id_map: dict,
    season: int,
) -> None:
    """Build season-aggregate stat rows per player and upsert into stats table."""
    import psycopg2.extras

    stat_lookup   = build_stat_lookup(player_stats_raw)
    ppa_lookup    = build_ppa_lookup(ppa_raw)
    usage_lookup  = build_usage_lookup(usage_raw)
    awards_lookup = build_awards_lookup(awards_raw)

    # ps_lookup keyed by (player_id, team_id) for precise resolution
    ps_lookup = _build_ps_lookup(season, team_id_map)

    rows = []
    for team_name, players in rosters_by_team.items():
        team_id = team_id_map.get(team_name.lower())
        for p in players:
            pid = p.get("id")
            try:
                pid_int = int(pid) if pid is not None else None
            except (ValueError, TypeError):
                pid_int = None
            db_id = player_id_map.get(pid_int)
            if not db_id:
                continue

            # Resolve player_season_id — prefer (player, team) exact match
            ps_id = ps_lookup.get((db_id, team_id)) or ps_id_map.get(db_id)
            if not ps_id:
                continue

            first = p.get("firstName", "")
            last  = p.get("lastName", "")
            stats     = find_player_stats(stat_lookup, first, last, team_name)
            ppa_val   = find_player_ppa(ppa_lookup, first, last, team_name)
            usage     = usage_lookup.get(pid_int, {})
            award_tier = awards_lookup.get((f"{first} {last}".lower(), team_name.lower()), 0)

            if stats or ppa_val or usage:
                data = {
                    **stats,
                    "ppa":          ppa_val,
                    "snap_pct":     usage.get("overall", 0),
                    "snap_pct_pass": usage.get("pass", 0),
                    "snap_pct_rush": usage.get("rush", 0),
                    "games_played": usage.get("games", 0),
                    "award_tier":   award_tier,
                }
                rows.append({
                    "player_season_id": ps_id,
                    "game_id":          None,
                    "season":           season,
                    "stat_type":        "season_aggregate",
                    "data":             json.dumps(data),
                })

    if rows:
        seen: dict = {}
        for r in rows:
            seen[(r["player_season_id"], r["season"], r["stat_type"])] = r
        rows = list(seen.values())

        sql = """
            INSERT INTO stats (player_season_id, game_id, season, stat_type, data)
            VALUES %s
            ON CONFLICT (player_season_id, season, stat_type) WHERE game_id IS NULL
            DO UPDATE SET data = EXCLUDED.data, updated_at = now()
        """
        template = "(%(player_season_id)s, %(game_id)s, %(season)s, %(stat_type)s, %(data)s)"
        with get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, rows, template=template, page_size=500)
        print(f"  Upserted {len(rows)} season-aggregate stat rows")


def upsert_postseason_stats(
    postseason_stats_raw: list,
    player_id_map: dict,
    ps_id_map: dict,
    rosters_by_team: dict,
    team_id_map: dict,
    season: int,
) -> None:
    """Build postseason-aggregate stat rows and upsert as stat_type='postseason_aggregate'."""
    import psycopg2.extras

    stat_lookup = build_stat_lookup(postseason_stats_raw)
    ps_lookup   = _build_ps_lookup(season, team_id_map)

    rows = []
    for team_name, players in rosters_by_team.items():
        team_id = team_id_map.get(team_name.lower())
        for p in players:
            pid = p.get("id")
            try:
                pid_int = int(pid) if pid is not None else None
            except (ValueError, TypeError):
                pid_int = None
            db_id = player_id_map.get(pid_int)
            if not db_id:
                continue
            ps_id = ps_lookup.get((db_id, team_id)) or ps_id_map.get(db_id)
            if not ps_id:
                continue
            first = p.get("firstName", "")
            last  = p.get("lastName", "")
            stats = find_player_stats(stat_lookup, first, last, team_name)
            if stats:
                rows.append({
                    "player_season_id": ps_id,
                    "game_id":          None,
                    "season":           season,
                    "stat_type":        "postseason_aggregate",
                    "data":             json.dumps(stats),
                })

    if rows:
        seen: dict = {}
        for r in rows:
            seen[(r["player_season_id"], r["season"], r["stat_type"])] = r
        rows = list(seen.values())

        sql = """
            INSERT INTO stats (player_season_id, game_id, season, stat_type, data)
            VALUES %s
            ON CONFLICT (player_season_id, season, stat_type) WHERE game_id IS NULL
            DO UPDATE SET data = EXCLUDED.data, updated_at = now()
        """
        template = "(%(player_season_id)s, %(game_id)s, %(season)s, %(stat_type)s, %(data)s)"
        with get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, rows, template=template, page_size=500)
        print(f"  Upserted {len(rows)} postseason-aggregate stat rows")


def _parse_clock(clock_str) -> int | None:
    """'13:24' → seconds remaining. Returns None on bad input."""
    if not clock_str:
        return None
    try:
        parts = str(clock_str).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return None


import re as _re

_PASS_RE    = _re.compile(r'^(.+?)\s+pass\s+(?:complete|incomplete)', _re.IGNORECASE)
_RUSH_RE    = _re.compile(r'^(.+?)\s+(?:run|rush|scramble)', _re.IGNORECASE)
_RECV_RE    = _re.compile(r'pass\s+(?:complete|incomplete)\s+to\s+(.+?)\s+for', _re.IGNORECASE)
_SACK_RE    = _re.compile(r'sacked by (.+?)(?:\s+for|\s+at|$)', _re.IGNORECASE)
_DEF_SACK_RE = _re.compile(r'sacked by (.+?)(?:\s+for|\s+at|\s*$)', _re.IGNORECASE)
_DEF_INT_RE  = _re.compile(r'intercepted by (.+?)(?:\s+at|\s+for|\s+return|\s*$)', _re.IGNORECASE)
_DEF_TFL_RE  = _re.compile(r'(?:run|rush) for a loss.*?(?:tackle by|tackled by) (.+?)(?:\s+for|\s*$)', _re.IGNORECASE)


def _parse_play_names(play_text: str, play_type: str) -> tuple:
    """Extract (passer, rusher, receiver, defender) names from play text."""
    pt = (play_text or "").strip()
    ptype = (play_type or "").lower()
    passer = rusher = receiver = defender = ""

    if "pass" in ptype or "sack" in ptype or "interception" in ptype:
        m = _PASS_RE.match(pt)
        if m:
            passer = m.group(1).strip()
        m2 = _RECV_RE.search(pt)
        if m2:
            receiver = m2.group(1).strip()
        if "sack" in ptype:
            m3 = _DEF_SACK_RE.search(pt)
            if m3:
                defender = m3.group(1).strip()
            if not passer:
                # play starts with QB name before "sacked"
                m4 = _re.match(r'^(.+?)\s+sacked', pt, _re.IGNORECASE)
                if m4:
                    passer = m4.group(1).strip()
        if "interception" in ptype:
            m5 = _DEF_INT_RE.search(pt)
            if m5:
                defender = m5.group(1).strip()
    elif "rush" in ptype or "run" in ptype:
        m = _RUSH_RE.match(pt)
        if m:
            rusher = m.group(1).strip()
        # TFL — defender made the stop
        m2 = _DEF_TFL_RE.search(pt)
        if m2:
            defender = m2.group(1).strip()

    return passer.lower(), rusher.lower(), receiver.lower(), defender.lower()


def upsert_plays(plays_raw: list, game_id_map: dict, team_id_map: dict, player_id_map: dict, season: int) -> None:
    """Upsert play-by-play rows. Player attribution is parsed from playText."""
    # Build {name_lower: db_id} from players with stats this season.
    name_to_db_id: dict = {}
    with get_connection() as conn:
        cur = conn.cursor()
        # Use ALL players in DB (not just those with stats) so defenders
        # with 0 counting stats can still be attributed on sack/INT plays.
        cur.execute("SELECT id, name FROM players LIMIT 100000")
        for db_id, name in cur.fetchall():
            name_to_db_id[name.lower()] = db_id

    rows = []
    for play in plays_raw:
        game_api_id = play.get("game_id") or play.get("gameId")
        game_db_id  = game_id_map.get(game_api_id)

        offense_raw = (play.get("offense") or "").lower()
        defense_raw = (play.get("defense") or "").lower()

        play_text  = play.get("play_text") or play.get("playText") or ""
        play_type  = play.get("play_type") or play.get("playType") or ""

        passer_name, rusher_name, receiver_name, defender_name = _parse_play_names(play_text, play_type)

        rows.append({
            "cfb_api_id":          play.get("id"),
            "game_id":             game_db_id,
            "season":              season,
            "week":                play.get("week"),
            "offense_team_id":     team_id_map.get(offense_raw),
            "defense_team_id":     team_id_map.get(defense_raw),
            "period":              play.get("period"),
            "clock_seconds":       _parse_clock(play.get("clock") or play.get("clock_time")),
            "down":                play.get("down"),
            "distance":            play.get("distance"),
            "yards_to_goal":       play.get("yards_to_goal") or play.get("yardsToGoal"),
            "home_score":          play.get("home_score") or play.get("homeScore"),
            "away_score":          play.get("away_score") or play.get("awayScore"),
            "offense_score":       play.get("offense_score") or play.get("offenseScore"),
            "defense_score":       play.get("defense_score") or play.get("defenseScore"),
            "play_type":           play.get("play_type") or play.get("playType"),
            "yards_gained":        play.get("yards_gained") or play.get("yardsGained"),
            "epa":                 play.get("epa"),
            "ppa":                 play.get("ppa"),
            "passer_player_id":    name_to_db_id.get(passer_name)   if passer_name   else None,
            "rusher_player_id":    name_to_db_id.get(rusher_name)   if rusher_name   else None,
            "receiver_player_id":  name_to_db_id.get(receiver_name) if receiver_name else None,
            "defender_player_id":  name_to_db_id.get(defender_name) if defender_name else None,
            "play_text":           (play.get("play_text") or play.get("playText") or "")[:500],
        })

    valid_rows = [r for r in rows if r.get("cfb_api_id")]
    if not valid_rows:
        print("  No plays to upsert")
        return

    # Deduplicate by cfb_api_id
    seen: dict = {}
    for r in valid_rows:
        seen[r["cfb_api_id"]] = r
    valid_rows = list(seen.values())

    import psycopg2.extras
    cols = list(valid_rows[0].keys())
    col_str = ", ".join(cols)
    placeholder = "(" + ", ".join(f"%({c})s" for c in cols) + ")"
    sql = f"""
        INSERT INTO plays ({col_str}) VALUES %s
        ON CONFLICT (cfb_api_id) DO UPDATE SET
            epa = EXCLUDED.epa,
            ppa = EXCLUDED.ppa,
            passer_player_id   = COALESCE(EXCLUDED.passer_player_id,   plays.passer_player_id),
            rusher_player_id   = COALESCE(EXCLUDED.rusher_player_id,   plays.rusher_player_id),
            receiver_player_id = COALESCE(EXCLUDED.receiver_player_id, plays.receiver_player_id),
            defender_player_id = COALESCE(EXCLUDED.defender_player_id, plays.defender_player_id),
            updated_at = now()
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, valid_rows, template=placeholder, page_size=500)
    print(f"  Upserted {len(valid_rows)} plays")


def upsert_transfers(portal_raw: list, player_id_map: dict, team_id_map: dict, season: int) -> None:
    """Upsert transfer portal entries for a season."""
    rows = []
    for entry in portal_raw:
        name = (entry.get("firstName", "") + " " + entry.get("lastName", "")).strip().lower()
        # Try to find player by name lookup (best effort — portal entries may not have API ID)
        from_team = (entry.get("origin") or "").lower()
        to_team = (entry.get("destination") or "").lower()
        rows.append({
            "from_team_id":  team_id_map.get(from_team),
            "to_team_id":    team_id_map.get(to_team),
            "transfer_year": season,
            "portal_date":   (entry.get("transferDate") or "")[:10] or None,
            "source":        "cfb_api",
            # player_id left NULL here; scripts/03_scrape_transfers.py fills it via fuzzy match
        })
    # Only insert rows where we have at least a from-team
    valid_rows = [r for r in rows if r.get("from_team_id")]
    if valid_rows:
        import psycopg2.extras
        cols = list(valid_rows[0].keys())
        col_str = ", ".join(cols)
        placeholder = "(" + ", ".join(f"%({c})s" for c in cols) + ")"
        sql = f"INSERT INTO transfers ({col_str}) VALUES %s ON CONFLICT DO NOTHING"
        with get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, valid_rows, template=placeholder)
        print(f"  Inserted {len(valid_rows)} transfer portal entries (no player_id; run 03 to link)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_year(api_key: str, season: int) -> None:
    print(f"\n{'='*60}")
    print(f"Season {season}")
    print(f"{'='*60}")

    # Teams (fetch once, reuse for all seasons)
    print("Teams...")
    teams_raw = fetch_teams(api_key, season)
    team_id_map = upsert_teams(teams_raw)
    team_names = list(t.get("school") for t in teams_raw if t.get("school"))

    # Rosters → players (identity) + player_seasons
    print("Rosters...")
    rosters_by_team = fetch_all_rosters(api_key, team_names, season)
    player_id_map, ps_id_map = upsert_players(rosters_by_team, team_id_map, season)

    # Games
    print("Games...")
    games_raw = fetch_games(api_key, season)
    game_id_map = upsert_games(games_raw, team_id_map, season)

    # Season stats
    print("Player stats...")
    player_stats_raw = fetch_player_stats(api_key, season)
    ppa_raw   = fetch_ppa(api_key, season)
    usage_raw = fetch_player_usage(api_key, season)
    awards_raw = fetch_awards(api_key, season)
    upsert_season_stats(
        player_stats_raw, ppa_raw, usage_raw, awards_raw,
        player_id_map, ps_id_map, rosters_by_team, team_id_map, season,
    )

    # Postseason stats (bowl/CFP)
    postseason_stats_raw = fetch_player_stats_postseason(api_key, season)
    if postseason_stats_raw:
        upsert_postseason_stats(
            postseason_stats_raw, player_id_map, ps_id_map,
            rosters_by_team, team_id_map, season,
        )

    # Transfer portal (raw entries; player linkage done in script 03)
    print("Transfer portal...")
    portal_raw = fetch_transfer_portal(api_key, season)
    upsert_transfers(portal_raw, player_id_map, team_id_map, season)

    # Play-by-play (large — ~40k plays/season; feeds EDGE computation in script 08)
    print("Play-by-play...")
    plays_raw = fetch_plays(api_key, season)
    upsert_plays(plays_raw, game_id_map, team_id_map, player_id_map, season)

    print(f"Season {season} complete.")


def main():
    parser = argparse.ArgumentParser(description="Fetch CFB Data API → Supabase")
    parser.add_argument("--year", type=int, help="Single year to fetch")
    parser.add_argument("--historical", action="store_true", help="Fetch 2005-2020 backfill")
    args = parser.parse_args()

    api_key = load_api_key()

    if args.year:
        seasons = [args.year]
    elif args.historical:
        seasons = SEASONS_HISTORICAL
    else:
        seasons = SEASONS_PRIMARY

    for season in seasons:
        run_year(api_key, season)
        time.sleep(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
