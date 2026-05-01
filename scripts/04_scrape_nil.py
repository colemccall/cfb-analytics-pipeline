"""Scrape On3 NIL valuations → upsert to Supabase nil_valuations table.

Re-run this periodically (monthly or start of season) to refresh valuations.
Only current-year data is fetched — On3 is the primary NIL source.

Usage:
    python scripts/04_scrape_nil.py
"""

import difflib
import sys
import time
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.db import bulk_upsert, get_connection

ON3_URL = "https://www.on3.com/nil/rankings/player/college/football/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}
SLEEP_SEC = 3.0
MAX_PAGES = 20  # ~50 players/page → top ~1000 NIL earners


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def scrape_nil_rankings(max_pages: int = MAX_PAGES) -> list[dict]:
    entries = []
    for page in range(1, max_pages + 1):
        url = f"{ON3_URL}?page={page}" if page > 1 else ON3_URL
        print(f"  Page {page}: {url}")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code in (403, 404):
                print(f"  {resp.status_code} — stopping.")
                break
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  Request failed: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("[class*='NilRankingPlayer']") or soup.select(".nil-player-row")
        if not rows:
            break

        for row in rows:
            entry = _parse_nil_row(row)
            if entry:
                entries.append(entry)

        time.sleep(SLEEP_SEC)

    return entries


def _parse_nil_row(row) -> dict | None:
    try:
        name_el = row.select_one("[class*='PlayerName']") or row.select_one(".player-name")
        if not name_el:
            return None
        name = name_el.get_text(strip=True)

        val_el = row.select_one("[class*='NILValue']") or row.select_one(".nil-value")
        valuation_usd = None
        if val_el:
            raw = val_el.get_text(strip=True).replace("$", "").replace(",", "").strip()
            # Handle "1.2M" and "500K" notations
            if raw.endswith("M"):
                try:
                    valuation_usd = int(float(raw[:-1]) * 1_000_000)
                except ValueError:
                    pass
            elif raw.endswith("K"):
                try:
                    valuation_usd = int(float(raw[:-1]) * 1_000)
                except ValueError:
                    pass
            else:
                try:
                    valuation_usd = int(float(raw))
                except ValueError:
                    pass

        team_el = row.select_one("[class*='School']") or row.select_one(".school-name")
        team = team_el.get_text(strip=True) if team_el else None

        pos_el = row.select_one("[class*='Position']") or row.select_one(".position")
        position = pos_el.get_text(strip=True) if pos_el else None

        return {
            "name": name,
            "team": team,
            "position": position,
            "valuation_usd": valuation_usd,
        }
    except Exception as e:
        print(f"  Row parse error: {e}")
        return None


# ---------------------------------------------------------------------------
# Player matching (psycopg2 — no 1000-row REST limit)
# ---------------------------------------------------------------------------

def build_player_index() -> dict:
    """Build {name_lower: [(player_id, team_lower)]} from all players."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.id, p.name, t.school
            FROM players p
            LEFT JOIN teams t ON t.id = p.team_id
        """)
        index: dict = {}
        for pid, name, school in cur.fetchall():
            key = name.lower().strip()
            team = (school or "").lower()
            index.setdefault(key, []).append((pid, team))
    return index


def match_player(name: str, team: str | None, player_index: dict, threshold: float = 0.85) -> int | None:
    name_l = name.lower().strip()
    for suffix in [" jr.", " jr", " sr.", " sr", " ii", " iii", " iv"]:
        if name_l.endswith(suffix):
            name_l = name_l[: -len(suffix)].strip()

    if name_l in player_index:
        candidates = player_index[name_l]
        if team:
            team_l = team.lower()
            for pid, t in candidates:
                if t == team_l:
                    return pid
        return candidates[0][0]

    matches = difflib.get_close_matches(name_l, player_index.keys(), n=3, cutoff=threshold)
    for match in matches:
        candidates = player_index[match]
        if team:
            team_l = team.lower()
            for pid, t in candidates:
                if t == team_l:
                    return pid
        if len(candidates) == 1:
            return candidates[0][0]
    return None


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert_nil(entries: list[dict], player_index: dict) -> None:
    today = date.today().isoformat()
    rows = []
    unmatched = 0

    for e in entries:
        player_id = match_player(e["name"], e.get("team"), player_index)
        if player_id is None:
            unmatched += 1
            continue
        rows.append({
            "player_id":     player_id,
            "valuation_usd": e.get("valuation_usd"),
            "source":        "on3",
            "as_of_date":    today,
        })

    if rows:
        bulk_upsert("nil_valuations", rows, ["player_id", "as_of_date"])
    print(f"  Upserted {len(rows)} NIL valuations ({unmatched} unmatched)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Building player index...")
    player_index = build_player_index()
    print(f"  {len(player_index)} player names loaded")
    print("Scraping On3 NIL rankings...")
    entries = scrape_nil_rankings()
    print(f"Scraped {len(entries)} NIL entries")
    if entries:
        upsert_nil(entries, player_index)
    print("Done.")


if __name__ == "__main__":
    main()
