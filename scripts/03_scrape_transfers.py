"""Scrape transfer portal data → upsert to Supabase.

Sources (try in order):
  1. On3 transfer portal tracker
  2. 247Sports transfer portal

Also resolves player_id linkages for transfer rows inserted by script 01.

Usage:
    python scripts/03_scrape_transfers.py              # 2021-2025
    python scripts/03_scrape_transfers.py --year 2024  # single year
    python scripts/03_scrape_transfers.py --link-only  # just re-run player linkage
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
# Player and team indexes (psycopg2 — no 1000-row REST limit)
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
    name_l = name.lower().strip()
    for suffix in [" jr.", " jr", " sr.", " sr", " ii", " iii", " iv"]:
        if name_l.endswith(suffix):
            name_l = name_l[: -len(suffix)].strip()

    if name_l in player_index:
        candidates = player_index[name_l]
        if from_school:
            school_l = from_school.lower()
            for pid, team in candidates:
                if team == school_l:
                    return pid
        return candidates[0][0]

    matches = difflib.get_close_matches(name_l, player_index.keys(), n=3, cutoff=threshold)
    for match in matches:
        candidates = player_index[match]
        if from_school:
            school_l = from_school.lower()
            for pid, team in candidates:
                if team == school_l:
                    return pid
        if len(candidates) == 1:
            return candidates[0][0]

    return None


# ---------------------------------------------------------------------------
# On3 scraper
# ---------------------------------------------------------------------------

def scrape_on3(year: int) -> list[dict]:
    url = ON3_URL.format(year=year)
    print(f"  Fetching {url}")
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
        from_school = from_el.get_text(strip=True) if from_el else None

        to_el = row.select_one("[class*='ToSchool']") or row.select_one(".to-school")
        to_school = to_el.get_text(strip=True) if to_el else None

        pos_el = row.select_one("[class*='Position']") or row.select_one(".position")
        position = pos_el.get_text(strip=True) if pos_el else None

        date_el = row.select_one("[class*='Date']") or row.select_one(".portal-date")
        portal_date = date_el.get_text(strip=True) if date_el else None

        return {
            "name": name,
            "from_school": from_school,
            "to_school": to_school,
            "position": position,
            "portal_date": portal_date,
            "transfer_year": year,
        }
    except Exception as e:
        print(f"  Row parse error: {e}")
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

        # Parse portal_date — keep only YYYY-MM-DD portion if present
        raw_date = (e.get("portal_date") or "").strip()
        portal_date = raw_date[:10] if len(raw_date) >= 10 else (raw_date or None)

        rows.append({
            "player_id":     player_id,
            "from_team_id":  from_id,
            "to_team_id":    to_id,
            "transfer_year": e["transfer_year"],
            "portal_date":   portal_date,
            "source":        "on3_scrape",
        })

    # Compute portal_entry_count per player across this batch
    from collections import Counter
    pid_counts = Counter(r["player_id"] for r in rows)
    for r in rows:
        r["portal_entry_count"] = pid_counts[r["player_id"]]

    # Dedup by (player_id, transfer_year) — keep last seen
    seen: dict = {}
    for r in rows:
        seen[(r["player_id"], r["transfer_year"])] = r
    rows = list(seen.values())

    if rows:
        bulk_upsert("transfers", rows, ["player_id", "transfer_year"])
    print(f"  Upserted {len(rows)} transfers ({unmatched} unmatched players)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scrape transfer portal → Supabase")
    parser.add_argument("--year", type=int, help="Single year")
    parser.add_argument("--link-only", action="store_true", help="Only re-run player linkage (no-op placeholder)")
    args = parser.parse_args()

    years = [args.year] if args.year else YEARS_DEFAULT
    print("Building player and team indexes...")
    player_index = build_player_index()
    team_index = build_team_index()
    print(f"  {len(player_index)} player names, {len(team_index)} teams loaded")

    if args.link_only:
        print("--link-only: no scrape source to re-link without names. Exiting.")
        return

    for year in years:
        print(f"\n--- Transfer portal {year} ---")
        entries = scrape_on3(year)
        print(f"  Scraped {len(entries)} portal entries")
        if entries:
            upsert_transfers(entries, player_index, team_index)

    print("\nDone.")


if __name__ == "__main__":
    main()
