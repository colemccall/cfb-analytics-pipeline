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
    fetch_ppa,
    fetch_team_stats,
    fetch_sp_ratings,
    fetch_player_usage,
    fetch_awards,
    fetch_games,
    fetch_all_game_player_stats,
    fetch_transfer_portal,
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


def upsert_players(rosters_by_team: dict, team_id_map: dict) -> dict:
    """Upsert all players for a season; return {cfb_api_id: db_id}."""
    rows = []
    for team_name, players in rosters_by_team.items():
        team_id = team_id_map.get(team_name.lower())
        for p in players:
            raw_pos = p.get("position", "") or ""
            rows.append({
                "cfb_api_id":     p.get("id"),
                "name":           f"{p.get('firstName', '')} {p.get('lastName', '')}".strip(),
                "team_id":        team_id,
                "position":       raw_pos,
                "position_group": POSITION_GROUP_MAP.get(raw_pos.upper(), "ATH"),
                "year":           p.get("year"),
                "height_in":      p.get("height"),
                "weight_lbs":     p.get("weight"),
                "hometown":       p.get("homeCity"),
                "hometown_state": p.get("homeState"),
                "hometown_country": p.get("homeCountry"),
            })
    valid_rows = [r for r in rows if r.get("cfb_api_id") and r.get("name")]
    # Deduplicate by cfb_api_id — a player can appear on multiple rosters (transfers)
    seen = {}
    for r in valid_rows:
        seen[r["cfb_api_id"]] = r
    valid_rows = list(seen.values())
    if valid_rows:
        bulk_upsert("players", valid_rows, "cfb_api_id")
        print(f"  Upserted {len(valid_rows)} players")

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, cfb_api_id FROM players WHERE cfb_api_id IS NOT NULL")
        return {row[1]: row[0] for row in cur.fetchall()}


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


def upsert_season_stats(
    player_stats_raw: list,
    ppa_raw: list,
    usage_raw: list,
    awards_raw: list,
    player_id_map: dict,
    rosters_by_team: dict,
    season: int,
) -> None:
    """Build season-aggregate stat rows per player and upsert into stats table."""
    stat_lookup  = build_stat_lookup(player_stats_raw)
    ppa_lookup   = build_ppa_lookup(ppa_raw)
    usage_lookup = build_usage_lookup(usage_raw)
    awards_lookup = build_awards_lookup(awards_raw)

    rows = []
    for team_name, players in rosters_by_team.items():
        for p in players:
            pid = p.get("id")
            try:
                pid_int = int(pid) if pid is not None else None
            except (ValueError, TypeError):
                pid_int = None
            db_id = player_id_map.get(pid_int)
            if not db_id:
                continue
            first = p.get("firstName", "")
            last = p.get("lastName", "")
            stats = find_player_stats(stat_lookup, first, last, team_name)
            ppa_val = find_player_ppa(ppa_lookup, first, last, team_name)
            usage = usage_lookup.get(pid_int, {})
            award_tier = awards_lookup.get((f"{first} {last}".lower(), team_name.lower()), 0)

            if stats or ppa_val or usage:
                data = {
                    **stats,
                    "ppa": ppa_val,
                    "snap_pct": usage.get("overall", 0),
                    "snap_pct_pass": usage.get("pass", 0),
                    "snap_pct_rush": usage.get("rush", 0),
                    "games_played": usage.get("games", 0),
                    "award_tier": award_tier,
                }
                rows.append({
                    "player_id": db_id,
                    "game_id":   None,
                    "season":    season,
                    "stat_type": "season_aggregate",
                    "data":      json.dumps(data),
                })

    if rows:
        # Deduplicate by (player_id, season, stat_type) — transfers can put a player on 2 rosters
        seen: dict = {}
        for r in rows:
            seen[(r["player_id"], r["season"], r["stat_type"])] = r
        rows = list(seen.values())

        sql = """
            INSERT INTO stats (player_id, game_id, season, stat_type, data)
            VALUES %s
            ON CONFLICT (player_id, season, stat_type) WHERE game_id IS NULL
            DO UPDATE SET data = EXCLUDED.data, updated_at = now()
        """
        template = "(%(player_id)s, %(game_id)s, %(season)s, %(stat_type)s, %(data)s)"
        import psycopg2.extras
        with get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, rows, template=template, page_size=500)
        print(f"  Upserted {len(rows)} season-aggregate stat rows")


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

    # Rosters
    print("Rosters...")
    rosters_by_team = fetch_all_rosters(api_key, team_names, season)
    player_id_map = upsert_players(rosters_by_team, team_id_map)

    # Games
    print("Games...")
    games_raw = fetch_games(api_key, season)
    game_id_map = upsert_games(games_raw, team_id_map, season)

    # Season stats
    print("Player stats...")
    player_stats_raw = fetch_player_stats(api_key, season)
    ppa_raw = fetch_ppa(api_key, season)
    usage_raw = fetch_player_usage(api_key, season)
    awards_raw = fetch_awards(api_key, season)
    upsert_season_stats(player_stats_raw, ppa_raw, usage_raw, awards_raw, player_id_map, rosters_by_team, season)

    # Transfer portal (raw entries; player linkage done in script 03)
    print("Transfer portal...")
    portal_raw = fetch_transfer_portal(api_key, season)
    upsert_transfers(portal_raw, player_id_map, team_id_map, season)

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
