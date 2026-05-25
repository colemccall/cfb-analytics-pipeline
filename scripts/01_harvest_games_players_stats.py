"""Fetch teams, rosters, games, and player stats from CFB Data API -> local JSON.

Writes to data/raw/:
    teams.json, players.json, player_seasons.json,
    games.json, stats.json

Usage:
    python scripts/01_harvest_games_players_stats.py              # 2021-2025
    python scripts/01_harvest_games_players_stats.py --historical  # 2008-2020
    python scripts/01_harvest_games_players_stats.py --year 2024   # single year
    python scripts/01_harvest_games_players_stats.py --all-seasons # 2008-2025
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
)
from utils.store import read_raw, RAW_DIR

SEASONS_PRIMARY    = list(range(2021, 2026))
SEASONS_HISTORICAL = list(range(2008, 2021))

AWARD_TIER_ALL_AMERICAN = 3  # All-American, Outland, Rimington, Lombardi, Bednarik
AWARD_TIER_FIRST_TEAM   = 2  # All-Conference first team
AWARD_TIER_HONORABLE    = 1  # All-Conference second team / honorable mention

POSITION_GROUP_MAP = {
    "QB":  "QB",
    "RB":  "RB",  "HB":  "RB",  "FB":  "RB",
    "WR":  "WR",  "FL":  "WR",  "SE":  "WR",
    "TE":  "TE",  "H":   "TE",
    "OL":  "OL",  "OT":  "OL",  "OG":  "OL",
    "G":   "OL",  "T":   "OL",  "C":   "OL",
    "LT":  "OL",  "RT":  "OL",  "LG":  "OL",  "RG":  "OL",
    "LS":  "OL",
    "EDGE": "EDGE", "OLB": "EDGE", "DE": "EDGE",
    "DL":  "DL",  "DT":  "DL",  "NT":  "DL",  "NG":  "DL",
    "LB":  "LB",  "ILB": "LB",  "MLB": "LB",
    "CB":  "CB",  "NB":  "CB",
    "DB":  "DB",
    "S":   "S",   "FS":  "S",   "SS":  "S",   "SAF": "S",
    "K":   "K",   "PK":  "K",
    "P":   "P",
    "ATH": "ATH",
}


# ---------------------------------------------------------------------------
# Stat lookup helpers (unchanged from original)
# ---------------------------------------------------------------------------

def build_stat_lookup(player_stats_raw: list) -> dict:
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
    team_l = team.lower()
    full = f"{first_name} {last_name}".lower()
    if (full, team_l) in stat_lookup:
        return stat_lookup[(full, team_l)]
    for (name, t), stats in stat_lookup.items():
        if t != team_l:
            continue
        parts = name.split(" ", 1)
        if len(parts) == 2 and parts[1] == last_name.lower():
            query_first = first_name.lower()
            stored_first = parts[0]
            if query_first.startswith(stored_first) or stored_first.startswith(query_first):
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
            query_first = first_name.lower()
            stored_first = parts[0]
            if query_first.startswith(stored_first) or stored_first.startswith(query_first):
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
    lookup: dict = {}
    for entry in awards_raw:
        name  = (entry.get("player") or entry.get("name") or "").lower().strip()
        team  = (entry.get("team") or "").lower().strip()
        award = (entry.get("award") or entry.get("category") or "").lower()
        if not name or not award:
            continue
        if any(x in award for x in ["all-american", "outland", "rimington", "lombardi", "bednarik"]):
            tier = AWARD_TIER_ALL_AMERICAN
        elif "all-" in award and any(x in award for x in ["first", "1st"]):
            tier = AWARD_TIER_FIRST_TEAM
        elif "all-" in award and any(x in award for x in ["second", "2nd", "honorable"]):
            tier = AWARD_TIER_HONORABLE
        else:
            continue
        key = (name, team)
        if key not in lookup or lookup[key] < tier:
            lookup[key] = tier
    return lookup


# ---------------------------------------------------------------------------
# JSON load helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _save_json(path: Path, data: list) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

def upsert_teams(teams_raw: list) -> dict:
    """Upsert teams into teams.json. Returns {school_lower: db_id}."""
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

    path = RAW_DIR / "teams.json"
    existing = {r["school"]: r for r in _load_json(path) if r.get("school")}

    # Assign stable integer IDs based on cfb_api_id or school
    # Use existing IDs where possible; new teams get next available int
    existing_by_api = {r.get("cfb_api_id"): r for r in existing.values() if r.get("cfb_api_id")}
    max_id = max((r.get("id", 0) or 0 for r in existing.values()), default=0)

    for r in rows:
        cfb_id = r.get("cfb_api_id")
        school = r.get("school", "")
        if cfb_id and cfb_id in existing_by_api:
            r["id"] = existing_by_api[cfb_id]["id"]
        elif school in existing:
            r["id"] = existing[school]["id"]
        else:
            max_id += 1
            r["id"] = max_id
        existing[school] = r

    all_teams = list(existing.values())
    _save_json(path, all_teams)
    print(f"  Saved {len(rows)} teams ({len(all_teams)} total)")
    return {(r["school"] or "").lower(): r["id"] for r in all_teams if r.get("school")}


# ---------------------------------------------------------------------------
# Players + player_seasons
# ---------------------------------------------------------------------------

def upsert_players_and_seasons(rosters_by_team: dict, team_id_map: dict, season: int) -> tuple[dict, dict]:
    """Upsert players and player_seasons. Returns (cfb_api_id->db_id, player_id->ps_id)."""
    # Load existing
    players_path = RAW_DIR / "players.json"
    ps_path      = RAW_DIR / "player_seasons.json"
    existing_players = {r["cfb_api_id"]: r for r in _load_json(players_path) if r.get("cfb_api_id")}
    existing_ps = {(r["player_id"], r["season"], r["team_id"]): r
                   for r in _load_json(ps_path)
                   if r.get("player_id") and r.get("season") and r.get("team_id")}

    max_player_id = max((r.get("id", 0) or 0 for r in existing_players.values()), default=0)
    max_ps_id     = max((r.get("id", 0) or 0 for r in existing_ps.values()), default=0)

    # Build identity rows
    for team_name, players in rosters_by_team.items():
        for p in players:
            api_id = p.get("id")
            name   = f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()
            if not api_id or not name:
                continue
            api_id = int(api_id)
            if api_id not in existing_players:
                max_player_id += 1
                existing_players[api_id] = {"id": max_player_id, "cfb_api_id": api_id}
            existing_players[api_id].update({
                "name":             name,
                "height_in":        p.get("height"),
                "weight_lbs":       p.get("weight"),
                "hometown":         p.get("homeCity"),
                "hometown_state":   p.get("homeState"),
                "hometown_country": p.get("homeCountry"),
            })

    players_list = list(existing_players.values())
    _save_json(players_path, players_list)
    print(f"  Saved {len(players_list)} players")

    # Build player_id_map: cfb_api_id -> db id
    player_id_map = {r["cfb_api_id"]: r["id"] for r in players_list if r.get("cfb_api_id")}

    # Build player_seasons
    for team_name, players in rosters_by_team.items():
        team_id = team_id_map.get(team_name.lower())
        for p in players:
            api_id = p.get("id")
            db_id  = player_id_map.get(int(api_id)) if api_id else None
            if not db_id or not team_id:
                continue
            raw_pos = p.get("position", "") or ""
            key = (db_id, season, team_id)
            if key not in existing_ps:
                max_ps_id += 1
                existing_ps[key] = {"id": max_ps_id}
            existing_ps[key].update({
                "player_id":      db_id,
                "season":         season,
                "team_id":        team_id,
                "position":       raw_pos,
                "position_group": POSITION_GROUP_MAP.get(raw_pos.upper(), "ATH"),
                "year":           p.get("year"),
            })

    ps_list = list(existing_ps.values())
    _save_json(ps_path, ps_list)
    print(f"  Saved {len(ps_list)} player_seasons")

    # ps_id_map: player_id -> ps_id for this season
    ps_id_map: dict = {}
    for (pid, s, _tid), r in existing_ps.items():
        if s == season:
            ps_id_map[pid] = r["id"]

    return player_id_map, ps_id_map


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------

def save_games(games_raw: list, team_id_map: dict, season: int) -> dict:
    """Upsert games. Returns {cfb_api_id: db_id}."""
    path = RAW_DIR / "games.json"
    existing = {r["cfb_api_id"]: r for r in _load_json(path) if r.get("cfb_api_id")}
    max_id = max((r.get("id", 0) or 0 for r in existing.values()), default=0)

    for g in games_raw:
        api_id = g.get("id")
        if not api_id:
            continue
        home = (g.get("homeTeam") or g.get("home_team") or "").lower()
        away = (g.get("awayTeam") or g.get("away_team") or "").lower()
        if api_id not in existing:
            max_id += 1
            existing[api_id] = {"id": max_id}
        existing[api_id].update({
            "cfb_api_id":   api_id,
            "season":       season,
            "week":         g.get("week"),
            "season_type":  g.get("_seasonType", "regular"),
            "home_team_id": team_id_map.get(home),
            "away_team_id": team_id_map.get(away),
            "home_score":   g.get("homePoints") or g.get("home_points"),
            "away_score":   g.get("awayPoints") or g.get("away_points"),
            "neutral_site": g.get("neutralSite", False),
            "game_date":    (g.get("startDate") or g.get("start_date") or "")[:10] or None,
            "venue":        g.get("venue"),
        })

    combined = list(existing.values())
    _save_json(path, combined)
    print(f"  Saved {len(combined)} games total")
    return {r["cfb_api_id"]: r["id"] for r in combined if r.get("cfb_api_id")}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def _build_ps_lookup_local(season: int) -> dict:
    """Return {(player_id, team_id): ps_id} for a given season."""
    ps_list = _load_json(RAW_DIR / "player_seasons.json")
    return {(r["player_id"], r["team_id"]): r["id"]
            for r in ps_list if r.get("season") == season}


def _save_stat_rows(new_rows: list) -> None:
    """Append/replace stat rows in data/raw/stats.json."""
    path = RAW_DIR / "stats.json"
    existing = _load_json(path)

    # Index existing: season_agg by (ps_id, season, stat_type) where game_id IS NULL
    #                 game_agg by (ps_id, game_id, season, stat_type)
    existing_season = {
        (r["player_season_id"], r["season"], r["stat_type"]): r
        for r in existing
        if r.get("game_id") is None
    }
    existing_game = {
        (r["player_season_id"], r["game_id"], r["season"], r["stat_type"]): r
        for r in existing
        if r.get("game_id") is not None
    }

    added = 0
    for r in new_rows:
        if r.get("game_id") is None:
            k = (r["player_season_id"], r["season"], r["stat_type"])
            existing_season[k] = r
        else:
            k = (r["player_season_id"], r["game_id"], r["season"], r["stat_type"])
            existing_game[k] = r
        added += 1

    combined = list(existing_season.values()) + list(existing_game.values())
    _save_json(path, combined)
    print(f"  Saved {added} stat rows ({len(combined)} total in file)")


def save_season_stats(
    player_stats_raw, ppa_raw, usage_raw, awards_raw,
    player_id_map, ps_id_map, rosters_by_team, team_id_map, season,
) -> None:
    stat_lookup   = build_stat_lookup(player_stats_raw)
    ppa_lookup    = build_ppa_lookup(ppa_raw)
    usage_lookup  = build_usage_lookup(usage_raw)
    awards_lookup = build_awards_lookup(awards_raw)
    ps_lookup     = _build_ps_lookup_local(season)

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
            stats     = find_player_stats(stat_lookup, first, last, team_name)
            ppa_val   = find_player_ppa(ppa_lookup, first, last, team_name)
            usage     = usage_lookup.get(pid_int, {})
            award_tier = awards_lookup.get((f"{first} {last}".lower(), team_name.lower()), 0)

            if stats or ppa_val or usage:
                data = {
                    **stats,
                    "ppa":           ppa_val,
                    "snap_pct":      usage.get("overall", 0),
                    "snap_pct_pass": usage.get("pass", 0),
                    "snap_pct_rush": usage.get("rush", 0),
                    "games_played":  usage.get("games", 0),
                    "award_tier":    award_tier,
                }
                rows.append({
                    "player_season_id": ps_id,
                    "game_id":          None,
                    "season":           season,
                    "stat_type":        "season_aggregate",
                    "data":             data,
                })

    seen = {}
    for r in rows:
        seen[(r["player_season_id"], r["season"], r["stat_type"])] = r
    _save_stat_rows(list(seen.values()))


def save_postseason_stats(postseason_stats_raw, player_id_map, ps_id_map,
                           rosters_by_team, team_id_map, season) -> None:
    stat_lookup = build_stat_lookup(postseason_stats_raw)
    ps_lookup   = _build_ps_lookup_local(season)
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
                    "data":             stats,
                })
    seen = {}
    for r in rows:
        seen[(r["player_season_id"], r["season"], r["stat_type"])] = r
    _save_stat_rows(list(seen.values()))


def save_game_stats(game_stats_raw, player_id_map, team_id_map, game_id_map, season) -> None:
    ps_lookup = _build_ps_lookup_local(season)
    rows_dict: dict = {}

    for game in game_stats_raw:
        gid_api = game.get("id")
        gid_db  = game_id_map.get(gid_api)
        if not gid_db:
            continue
        for team_block in game.get("teams", []):
            school = team_block.get("team") or team_block.get("school", "")
            tid = team_id_map.get(school.lower())
            if not tid:
                continue
            for cat in team_block.get("categories", []):
                cat_name = cat.get("name", "")
                for stype in cat.get("types", []):
                    stat_label = stype.get("name", "")
                    stat_key = f"{cat_name}{stat_label}"
                    for ath in stype.get("athletes", []):
                        api_pid = ath.get("id")
                        try:
                            api_pid_int = int(api_pid) if api_pid is not None else None
                        except (ValueError, TypeError):
                            api_pid_int = None
                        if api_pid_int is None:
                            continue
                        db_id = player_id_map.get(api_pid_int)
                        if not db_id:
                            continue
                        ps_id = ps_lookup.get((db_id, tid))
                        if not ps_id:
                            continue
                        key = (ps_id, gid_db)
                        if key not in rows_dict:
                            rows_dict[key] = {}
                        rows_dict[key][stat_key] = ath.get("stat", 0)

    rows = [
        {
            "player_season_id": ps_id,
            "game_id":          gid,
            "season":           season,
            "stat_type":        "game_aggregate",
            "data":             stats,
        }
        for (ps_id, gid), stats in rows_dict.items()
        if stats
    ]
    if not rows:
        print("  No game-aggregate stat rows to save")
        return
    seen = {}
    for r in rows:
        seen[(r["player_season_id"], r["game_id"], r["season"], r["stat_type"])] = r
    _save_stat_rows(list(seen.values()))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_year(api_key: str, season: int) -> None:
    print(f"\n{'='*60}")
    print(f"Season {season}")
    print(f"{'='*60}")

    print("Teams...")
    teams_raw  = fetch_teams(api_key, season)
    team_id_map = upsert_teams(teams_raw)
    team_names  = [t.get("school") for t in teams_raw if t.get("school")]

    print("Rosters -> players + player_seasons...")
    rosters_by_team = fetch_all_rosters(api_key, team_names, season)
    player_id_map, ps_id_map = upsert_players_and_seasons(rosters_by_team, team_id_map, season)

    print("Games...")
    games_raw   = fetch_games(api_key, season)
    game_id_map = save_games(games_raw, team_id_map, season)

    print("Player stats (season aggregate)...")
    player_stats_raw = fetch_player_stats(api_key, season)
    ppa_raw          = fetch_ppa(api_key, season)
    usage_raw        = fetch_player_usage(api_key, season)
    awards_raw       = fetch_awards(api_key, season)
    save_season_stats(
        player_stats_raw, ppa_raw, usage_raw, awards_raw,
        player_id_map, ps_id_map, rosters_by_team, team_id_map, season,
    )

    print("Player stats (postseason)...")
    postseason_stats_raw = fetch_player_stats_postseason(api_key, season)
    if postseason_stats_raw:
        save_postseason_stats(
            postseason_stats_raw, player_id_map, ps_id_map,
            rosters_by_team, team_id_map, season,
        )

    print("Per-game player stats...")
    game_stats_raw = fetch_all_game_player_stats(api_key, team_names, season)
    if game_stats_raw:
        save_game_stats(game_stats_raw, player_id_map, team_id_map, game_id_map, season)

    print(f"Season {season} complete.")


def main():
    parser = argparse.ArgumentParser(description="Fetch CFB Data API -> local JSON")
    parser.add_argument("--year",        type=int,       help="Single year")
    parser.add_argument("--historical",  action="store_true", help="2008-2020 backfill")
    parser.add_argument("--all-seasons", action="store_true", help="2008-2025")
    args = parser.parse_args()

    api_key = load_api_key()

    if args.year:
        seasons = [args.year]
    elif args.all_seasons:
        seasons = list(range(2008, 2026))
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
