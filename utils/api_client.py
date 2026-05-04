"""CFB Data API client — fetches teams, rosters, stats, PPA, and more.
Includes file-based caching so re-runs don't re-fetch the same endpoints.

Adapted from cfb-analytics-v1-archived/data-pipeline/api_client.py.
Cache lives in cfb-analytics-pipeline/.cache/ (gitignored).
"""

import hashlib
import json
import os
import time

import requests
from dotenv import load_dotenv

BASE_URL = "https://api.collegefootballdata.com"
# Cache lives next to the pipeline root, not next to this file
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", ".cache")


def load_api_key() -> str:
    load_dotenv()
    key = os.getenv("CFB_API_KEY")
    if not key:
        raise RuntimeError("CFB_API_KEY not found in .env")
    return key


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def _cache_key(path: str, params: dict | None) -> str:
    key_str = path + json.dumps(params or {}, sort_keys=True)
    return hashlib.md5(key_str.encode()).hexdigest()


def _get(api_key: str, path: str, params: dict | None = None, retries: int = 5) -> list | dict:
    os.makedirs(CACHE_DIR, exist_ok=True)
    ck = _cache_key(path, params)
    cache_file = os.path.join(CACHE_DIR, f"{ck}.json")

    if os.path.exists(cache_file):
        print(f"  [cache] {path} {params or ''}")
        with open(cache_file) as f:
            return json.load(f)

    print(f"  GET {path} {params or ''}")
    for attempt in range(retries):
        resp = requests.get(
            f"{BASE_URL}{path}",
            headers=_headers(api_key),
            params=params,
            timeout=30,
        )
        if resp.status_code == 429:
            wait = 5 * (2**attempt)  # 5, 10, 20, 40, 80s
            print(f"  Rate limited, waiting {wait}s... (attempt {attempt+1}/{retries})")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        n = len(data) if isinstance(data, list) else "ok"
        print(f"  -> {n} records, cached.")
        with open(cache_file, "w") as f:
            json.dump(data, f)
        return data
    raise RuntimeError(f"Still rate-limited after {retries} retries: {path}")


def _safe(api_key: str, path: str, params: dict | None = None) -> list:
    """Like _get but returns [] on any error or non-list response."""
    try:
        result = _get(api_key, path, params)
        return result if isinstance(result, list) else []
    except Exception as e:
        print(f"  WARNING: {path} {params} failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Public fetch functions
# ---------------------------------------------------------------------------

def fetch_teams(api_key: str, year: int) -> list:
    return _get(api_key, "/teams/fbs", {"year": year})


def fetch_roster(api_key: str, team_name: str, year: int) -> list:
    return _safe(api_key, "/roster", {"team": team_name, "year": year})


def fetch_all_rosters(api_key: str, team_names: list[str], year: int) -> dict:
    rosters = {}
    for i, name in enumerate(team_names):
        print(f"  Roster {i+1}/{len(team_names)}: {name}")
        rosters[name] = fetch_roster(api_key, name, year)
        time.sleep(0.75)
    return rosters


def fetch_player_stats(api_key: str, year: int) -> list:
    """Season-level player stats (regular season only to avoid double-counting)."""
    return _safe(api_key, "/stats/player/season", {"year": year, "seasonType": "regular"})


def fetch_player_stats_postseason(api_key: str, year: int) -> list:
    """Postseason (bowl/CFP) player stats for a given year."""
    return _safe(api_key, "/stats/player/season", {"year": year, "seasonType": "postseason"})


def fetch_ppa(api_key: str, year: int) -> list:
    """Predicted Points Added per player per season."""
    return _safe(api_key, "/ppa/players/season", {"year": year})


def fetch_team_stats(api_key: str, year: int) -> list:
    return _safe(api_key, "/stats/season", {"year": year})


def fetch_sp_ratings(api_key: str, year: int) -> list:
    """SP+ schedule-adjusted team quality ratings."""
    return _safe(api_key, "/ratings/sp", {"year": year})


def fetch_talent(api_key: str, year: int) -> list:
    """Recruiting talent composite score per team."""
    return _safe(api_key, "/talent", {"year": year})


def fetch_recruiting(api_key: str, year: int) -> list:
    """Individual player recruiting data (stars, composite score, position, etc.)."""
    return _safe(api_key, "/recruiting/players", {"year": year})


def fetch_player_usage(api_key: str, year: int) -> list:
    """Snap percentages and games played per player."""
    return _safe(api_key, "/player/usage", {"year": year, "seasonType": "regular"})


def fetch_awards(api_key: str, year: int) -> list:
    """All-American, All-Conference, Heisman finalist awards."""
    return _safe(api_key, "/awards", {"year": year})


def fetch_games(api_key: str, year: int) -> list:
    """All games (regular + postseason) for a given year."""
    regular = _safe(api_key, "/games", {"year": year, "seasonType": "regular"})
    postseason = _safe(api_key, "/games", {"year": year, "seasonType": "postseason"})
    for g in postseason:
        g["_seasonType"] = "postseason"
    return regular + postseason


def fetch_game_player_stats(api_key: str, team: str, year: int) -> list:
    """Per-game box score stats for one team (regular + postseason)."""
    regular = _safe(api_key, "/games/players", {"year": year, "seasonType": "regular", "team": team})
    postseason = _safe(api_key, "/games/players", {"year": year, "seasonType": "postseason", "team": team})
    return regular + postseason


def fetch_all_game_player_stats(api_key: str, team_names: list[str], year: int) -> list:
    """Game player stats for all teams; deduplicates by gameId."""
    seen_game_ids: set = set()
    results = []
    for i, team in enumerate(team_names):
        print(f"  Game stats {i+1}/{len(team_names)}: {team}")
        for entry in fetch_game_player_stats(api_key, team, year):
            gid = entry.get("id")
            if gid not in seen_game_ids:
                seen_game_ids.add(gid)
                results.append(entry)
        time.sleep(0.5)
    return results


def fetch_transfer_portal(api_key: str, year: int) -> list:
    return _safe(api_key, "/player/portal", {"year": year})


def fetch_drives(api_key: str, year: int) -> list:
    """All drives for a year (regular + postseason), iterating by week."""
    all_drives = []
    for week in range(1, 17):
        all_drives.extend(_safe(api_key, "/drives", {"year": year, "week": week, "seasonType": "regular"}))
        time.sleep(0.3)
    for week in range(1, 7):
        chunk = _safe(api_key, "/drives", {"year": year, "week": week, "seasonType": "postseason"})
        if chunk:
            all_drives.extend(chunk)
        time.sleep(0.3)
    return all_drives


def fetch_plays(api_key: str, year: int) -> list:
    """All play-by-play for a year. Large payload — use only when needed."""
    all_plays = []
    for week in range(1, 17):
        all_plays.extend(_safe(api_key, "/plays", {"year": year, "week": week, "seasonType": "regular"}))
        time.sleep(0.3)
    for week in range(1, 7):
        chunk = _safe(api_key, "/plays", {"year": year, "week": week, "seasonType": "postseason"})
        if chunk:
            all_plays.extend(chunk)
        time.sleep(0.3)
    return all_plays


def fetch_sp_ratings_all(api_key: str, year: int) -> dict:
    """Return {team_lower: sp_rating} for a year. Used for opponent quality adjustment."""
    raw = fetch_sp_ratings(api_key, year)
    result = {}
    for r in raw:
        team = (r.get("team") or "").lower()
        rating = r.get("rating") or r.get("sp") or 0
        if team:
            result[team] = float(rating)
    return result
