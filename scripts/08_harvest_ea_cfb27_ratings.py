"""Harvest EA CFB 27 player ratings from ea.com -> data/raw/ea_ratings.json.

ea.com/games/ea-sports-college-football/ratings is a Next.js app. Rather than
driving a browser, we hit the Next.js data route the page itself uses for
client-side navigation:

    /_next/data/{buildId}/en/games/ea-sports-college-football/ratings.json?team={id}

That returns the full team roster as JSON — every player, every attribute — so
no Selenium/Playwright is needed. The buildId changes on each EA deploy, so it
is re-extracted from the live page HTML on every run.

Output: data/raw/ea_ratings.json
        One row per EA player, all 50+ attributes, linked to our player_id /
        player_season_id where a name+team match exists. True freshmen have no
        prior player_season and stay unmatched (expected).

Usage:
    python scripts/08_harvest_ea_cfb27_ratings.py
    python scripts/08_harvest_ea_cfb27_ratings.py --teams 12,1     # subset (testing)
    python scripts/08_harvest_ea_cfb27_ratings.py --limit-teams 5  # first N teams
"""

import argparse
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.json_utils import write_json
from utils.matching import build_player_index, match_player, resolve_collisions
from utils.store import RAW_DIR

RATINGS_URL = "https://www.ea.com/games/ea-sports-college-football/ratings"
DATA_ROUTE  = ("https://www.ea.com/_next/data/{build_id}/en/games/"
               "ea-sports-college-football/ratings.json")
FRANCHISE_SLUG = "ea-sports-college-football"

EA_GAME    = "ea_cfb27"
EA_SEASON  = 2026          # CFB 27 ships the 2026 season roster

# Seasons searched (most recent first) when linking an EA player to one of ours.
# 2026 player_seasons do not exist yet, so returning players link via 2025.
MATCH_SEASONS = [2025, 2024]

SLEEP_SEC = 0.4
TIMEOUT   = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

OUTPUT = RAW_DIR / "ea_ratings.json"


# ---------------------------------------------------------------------------
# EA fetching
# ---------------------------------------------------------------------------

def fetch_build_id(session: requests.Session) -> str:
    """Extract the current Next.js buildId — it changes on every EA deploy."""
    resp = session.get(RATINGS_URL, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    match = re.search(r'"buildId":"([^"]+)"', resp.text)
    if not match:
        raise RuntimeError("Could not find buildId in ratings page — EA page structure changed")
    return match.group(1)


def fetch_ratings_json(session: requests.Session, build_id: str, team_id: int | None = None) -> dict:
    params = {"franchiseSlug": FRANCHISE_SLUG}
    if team_id is not None:
        params["team"] = str(team_id)
    resp = session.get(DATA_ROUTE.format(build_id=build_id), headers=HEADERS,
                       params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def extract_teams(payload: dict) -> list[dict]:
    """Pull the full team directory (id, label, conference) out of the filter data."""
    groups = payload.get("pageProps", {}).get("ratingsFilters", {}).get("teamGroups", [])
    teams = []
    for group in groups:
        for team in group.get("teams", []):
            teams.append({
                "ea_team_id": team.get("id"),
                "ea_team":    team.get("label"),
                "conference": group.get("label") or group.get("id"),
            })
    return teams


def parse_players(payload: dict) -> list[dict]:
    """Flatten ratingDetails.items into one row per player with all attributes."""
    items = payload.get("pageProps", {}).get("ratingDetails", {}).get("items", [])
    players = []
    for item in items:
        # stats values are {"value": N, "diff": N} — keep the value, drop the diff
        attributes = {
            key: stat.get("value") if isinstance(stat, dict) else stat
            for key, stat in (item.get("stats") or {}).items()
        }
        team     = item.get("team") or {}
        position = item.get("position") or {}
        conf     = item.get("conference") or {}
        first    = (item.get("firstName") or "").strip()
        last     = (item.get("lastName") or "").strip()
        players.append({
            "ea_player_id":  item.get("id"),
            "name":          f"{first} {last}".strip(),
            "first_name":    first,
            "last_name":     last,
            "ea_team_id":    team.get("id"),
            "ea_team":       team.get("label"),
            "conference":    conf.get("label") or conf.get("id"),
            "position":      position.get("shortLabel") or position.get("id"),
            "position_full": position.get("label"),
            "position_type": (position.get("positionType") or {}).get("name"),
            "ovr":           item.get("overallRating"),
            "height_in":     item.get("height"),
            "weight_lb":     item.get("weight"),
            "jersey":        item.get("jerseyNum"),
            "class_year":    item.get("schoolYear"),
            "redshirt":      item.get("redShirtStatus"),
            "home_town":     item.get("homeTown"),
            "home_state":    item.get("homeState"),
            "attributes":    attributes,
        })
    return players


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Harvest EA CFB 27 ratings -> data/raw/ea_ratings.json")
    parser.add_argument("--teams",       help="Comma-separated EA team ids (testing)")
    parser.add_argument("--limit-teams", type=int, help="Only harvest the first N teams")
    parser.add_argument("--no-match",    action="store_true",
                        help="Skip player matching (raw EA data only)")
    args = parser.parse_args()

    session = requests.Session()

    print("Resolving EA build id...")
    build_id = fetch_build_id(session)
    print(f"  buildId={build_id}")

    print("Fetching team directory...")
    directory = fetch_ratings_json(session, build_id)
    teams     = extract_teams(directory)
    if args.teams:
        wanted = {int(t) for t in args.teams.split(",")}
        teams  = [t for t in teams if t["ea_team_id"] in wanted]
    if args.limit_teams:
        teams = teams[: args.limit_teams]
    print(f"  {len(teams)} teams")

    player_index = {}
    if not args.no_match:
        print("Building player index from local JSON...")
        player_index = build_player_index(seasons=MATCH_SEASONS)
        print(f"  {len(player_index)} names across seasons {MATCH_SEASONS}")

    all_players: list[dict] = []
    failed: list[str] = []

    print(f"\nHarvesting {len(teams)} team rosters...")
    for i, team in enumerate(teams, 1):
        try:
            payload = fetch_ratings_json(session, build_id, team["ea_team_id"])
            roster  = parse_players(payload)
        except (requests.RequestException, ValueError) as e:
            print(f"  [{i}/{len(teams)}] {team['ea_team']}: FAILED ({e})")
            failed.append(team["ea_team"])
            continue

        for p in roster:
            p["ea_game"]   = EA_GAME
            p["ea_season"] = EA_SEASON
            if not p.get("conference"):
                p["conference"] = team["conference"]
        all_players.extend(roster)
        print(f"  [{i}/{len(teams)}] {team['ea_team']}: {len(roster)} players")
        time.sleep(SLEEP_SEC)

    if not all_players:
        print("\nNo players harvested — aborting without writing.")
        sys.exit(1)

    if player_index:
        print("\nMatching to our players...")
        for p in all_players:
            pid, psid, match_type = match_player(p["name"], p["ea_team"], player_index)
            p["player_id"]        = pid
            p["player_season_id"] = psid
            p["match_type"]       = match_type
        dropped = resolve_collisions(all_players)
        if dropped:
            print(f"  dropped {dropped} ambiguous matches (shared names)")
    else:
        for p in all_players:
            p["player_id"] = None
            p["player_season_id"] = None
            p["match_type"] = None

    write_json(OUTPUT, all_players)

    matched = sum(1 for p in all_players if p.get("player_id") is not None)
    pct = (matched / len(all_players) * 100) if all_players else 0
    print(f"\n{len(all_players)} EA players from {len(teams) - len(failed)} teams")
    print(f"  matched to our players: {matched} ({pct:.1f}%) - unmatched are mostly true freshmen")
    if failed:
        print(f"  failed teams ({len(failed)}): {', '.join(failed)}")
    print("Done.")


if __name__ == "__main__":
    main()
