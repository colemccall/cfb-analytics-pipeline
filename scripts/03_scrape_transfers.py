"""Fetch transfer portal data from CFB Data API → upsert to Supabase.

Uses /player/portal endpoint (origin, destination, transferDate, rating, stars).
Player linkage uses fuzzy name matching against the `players` table.

Falls back to On3 scraping if API returns no data for a year.

Usage:
    python scripts/03_scrape_transfers.py              # 2021-2025
    python scripts/03_scrape_transfers.py --year 2024  # single year
"""

import argparse
import difflib
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.api_client import load_api_key, fetch_transfer_portal
from utils.db import bulk_upsert, get_connection

YEARS_DEFAULT = list(range(2021, 2026))
SLEEP_SEC = 3.0

ON3_URL = "https://www.on3.com/transfer-portal/wire/football/{year}/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ---------------------------------------------------------------------------
# CFB Data API source
# ---------------------------------------------------------------------------

def fetch_transfers_api(api_key: str, year: int) -> list[dict]:
    """Fetch transfer portal from CFB Data API. Returns normalized dicts."""
    raw = fetch_transfer_portal(api_key, year)
    results = []
    for r in raw:
        name = f"{r.get('firstName', '')} {r.get('lastName', '')}".strip()
        raw_date = r.get("transferDate", "")
        portal_date = raw_date[:10] if raw_date and len(raw_date) >= 10 else None
        results.append({
            "name":          name,
            "from_school":   r.get("origin"),
            "to_school":     r.get("destination"),
            "position":      r.get("position"),
            "portal_date":   portal_date,
            "transfer_year": r.get("season", year),
            "stars":         r.get("stars"),
            "rating":        r.get("rating"),
            "eligibility":   r.get("eligibility"),
        })
    return results


# ---------------------------------------------------------------------------
# On3 scrape fallback
# ---------------------------------------------------------------------------

def scrape_on3(year: int) -> list[dict]:
    url = ON3_URL.format(year=year)
    print(f"  [On3 fallback] Fetching {url}")
    entries = []
    page = 1

    while True:
        page_url = f"{url}?page={page}" if page > 1 else url
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=20)
            if resp.status_code in (403, 404):
                print(f"  {resp.status_code} on page {page} — stopping.")
                break
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  Request failed: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select(".transfer-portal-player-card") or soup.select("[class*='PlayerCard']")
        if not rows:
            break

        for row in rows:
            entry = _parse_on3_row(row, year)
            if entry:
                entries.append(entry)

        page += 1
        time.sleep(SLEEP_SEC)

    return entries


def _parse_on3_row(row, year: int) -> dict | None:
    try:
        name_el = row.select_one("[class*='PlayerName']") or row.select_one(".player-name")
        if not name_el:
            return None
        name = name_el.get_text(strip=True)
        from_el = row.select_one("[class*='FromSchool']") or row.select_one(".from-school")
        to_el = row.select_one("[class*='ToSchool']") or row.select_one(".to-school")
        pos_el = row.select_one("[class*='Position']") or row.select_one(".position")
        date_el = row.select_one("[class*='Date']") or row.select_one(".portal-date")
        raw_date = date_el.get_text(strip=True) if date_el else None
        return {
            "name":          name,
            "from_school":   from_el.get_text(strip=True) if from_el else None,
            "to_school":     to_el.get_text(strip=True) if to_el else None,
            "position":      pos_el.get_text(strip=True) if pos_el else None,
            "portal_date":   (raw_date or "")[:10] or None,
            "transfer_year": year,
            "stars":         None,
            "rating":        None,
            "eligibility":   None,
        }
    except Exception as e:
        print(f"  Row parse error: {e}")
        return None


# ---------------------------------------------------------------------------
# Player and team indexes (psycopg2 — no 1000-row REST limit)
# ---------------------------------------------------------------------------

def build_player_index() -> dict:
    """Build {name_lower: [(player_id, team_lower)]} using player_seasons.

    Each (player_id, team) pair comes from an actual player_seasons row,
    so the index reflects every school a player has ever been rostered at.
    This means two 'Sammy Brown' LBs at different schools are distinct entries.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT p.id, p.name, t.school
            FROM players p
            JOIN player_seasons ps ON ps.player_id = p.id
            LEFT JOIN teams t ON t.id = ps.team_id
        """)
        index: dict = {}
        for pid, name, school in cur.fetchall():
            key = name.lower().strip()
            team = (school or "").lower()
            entry = (pid, team)
            lst = index.setdefault(key, [])
            if entry not in lst:
                lst.append(entry)
    return index


def build_team_index() -> dict:
    """Build {school_lower: team_id}."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, school FROM teams")
        return {school.lower(): tid for tid, school in cur.fetchall()}


def fuzzy_match_player(
    name: str,
    from_school: str | None,
    player_index: dict,
    threshold: float = 0.85,
) -> int | None:
    """Match a transfer portal entry to a player_id.

    Critically: if from_school is provided and the name matches multiple players,
    we ONLY accept a match where the player has a player_seasons row at from_school.
    If no team-gated match is found, return None (don't fall back to the wrong player).
    This prevents two players sharing a name at different schools from colliding.
    """
    name_l = name.lower().strip()
    for suffix in [" jr.", " jr", " sr.", " sr", " ii", " iii", " iv"]:
        if name_l.endswith(suffix):
            name_l = name_l[: -len(suffix)].strip()

    def _team_match(candidates: list, school_l: str) -> int | None:
        for pid, team in candidates:
            if team == school_l:
                return pid
        return None

    # Exact name match
    if name_l in player_index:
        candidates = player_index[name_l]
        if from_school:
            school_l = from_school.lower()
            hit = _team_match(candidates, school_l)
            if hit:
                return hit
            # Name matches but no team match — ambiguous, do NOT guess
            if len(candidates) > 1:
                return None
            # Only one candidate with this name; accept if no from_school conflict
            return candidates[0][0]
        return candidates[0][0] if len(candidates) == 1 else None

    # Fuzzy name match — only accept with team confirmation
    matches = difflib.get_close_matches(name_l, player_index.keys(), n=3, cutoff=threshold)
    for match in matches:
        candidates = player_index[match]
        if from_school:
            school_l = from_school.lower()
            hit = _team_match(candidates, school_l)
            if hit:
                return hit
        elif len(candidates) == 1:
            return candidates[0][0]

    return None


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert_transfers(entries: list[dict], player_index: dict, team_index: dict) -> None:
    rows = []
    unmatched = 0

    for e in entries:
        player_id = fuzzy_match_player(e["name"], e.get("from_school"), player_index)
        if player_id is None:
            unmatched += 1
            continue

        from_id = team_index.get((e.get("from_school") or "").lower())
        to_id = team_index.get((e.get("to_school") or "").lower())

        rows.append({
            "player_id":     player_id,
            "from_team_id":  from_id,
            "to_team_id":    to_id,
            "transfer_year": e["transfer_year"],
            "portal_date":   e.get("portal_date"),
            "source":        "cfb_api",
        })

    # Compute portal_entry_count per player across this batch
    from collections import Counter
    pid_counts = Counter(r["player_id"] for r in rows)
    for r in rows:
        r["portal_entry_count"] = pid_counts[r["player_id"]]

    # Dedup by (player_id, transfer_year, from_team_id) — matches new unique constraint
    seen: dict = {}
    for r in rows:
        key = (r["player_id"], r["transfer_year"], r["from_team_id"])
        existing = seen.get(key)
        if existing is None or (r.get("portal_date") and not existing.get("portal_date")):
            seen[key] = r
    rows = list(seen.values())

    if rows:
        bulk_upsert("transfers", rows, ["player_id", "transfer_year", "from_team_id"])
    print(f"  Upserted {len(rows)} transfers ({unmatched} unmatched players)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch transfer portal → Supabase")
    parser.add_argument("--year", type=int, help="Single year")
    args = parser.parse_args()

    api_key = load_api_key()
    years = [args.year] if args.year else YEARS_DEFAULT

    print("Building player and team indexes...")
    player_index = build_player_index()
    team_index = build_team_index()
    print(f"  {len(player_index)} player names, {len(team_index)} teams loaded")

    for year in years:
        print(f"\n--- Transfer portal {year} ---")
        entries = fetch_transfers_api(api_key, year)

        if not entries:
            print(f"  API returned nothing for {year} — trying On3 scrape fallback")
            entries = scrape_on3(year)

        print(f"  Got {len(entries)} portal entries")
        if entries:
            upsert_transfers(entries, player_index, team_index)

    print("\nDone.")


if __name__ == "__main__":
    main()
