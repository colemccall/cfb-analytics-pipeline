"""Fetch recruiting rankings from CFB Data API → upsert to Supabase.

Uses /recruiting/players endpoint (stars, composite score, ranks, committedTo).
Player linkage uses fuzzy name matching against the `players` table.

Falls back to 247Sports scraping if API returns no data for a year (pre-2013).

Usage:
    python scripts/02_scrape_recruiting.py              # 2005-2025
    python scripts/02_scrape_recruiting.py --year 2024  # single year
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

from utils.api_client import load_api_key, fetch_recruiting
from utils.db import bulk_upsert, get_connection

YEARS_DEFAULT = list(range(2005, 2026))

# 247Sports fallback config (used only when API returns nothing)
BASE_247_URL = "https://247sports.com/Season/{year}-Football/Recruits/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# CFB Data API source
# ---------------------------------------------------------------------------

def fetch_recruiting_api(api_key: str, year: int) -> list[dict]:
    """Fetch recruiting class from CFB Data API. Returns normalized dicts."""
    raw = fetch_recruiting(api_key, year)
    results = []
    for r in raw:
        # API rating is 0–1 composite (247-style); convert national_rank from ranking field
        results.append({
            "name":           r.get("name", ""),
            "recruit_year":   year,
            "stars":          r.get("stars"),
            "composite_score": r.get("rating"),  # 0.7–1.0 range
            "national_rank":  r.get("ranking"),
            "position":       r.get("position"),
            "committed_team": r.get("committedTo"),
            "position_rank":  None,  # not provided by API
            "state_rank":     None,  # not provided by API
        })
    return results


# ---------------------------------------------------------------------------
# 247Sports scrape fallback
# ---------------------------------------------------------------------------

def scrape_247_class(year: int, max_pages: int = 35, slow: bool = False) -> list[dict]:
    recruits = []
    delay = 8.0 if slow else 4.0

    for page in range(1, max_pages + 1):
        url = BASE_247_URL.format(year=year)
        if page > 1:
            url = f"{url}?Page={page}"
        print(f"  [247 fallback] Page {page}: {url}")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 403:
                print("  403 Forbidden — scrape blocked. Stopping.")
                break
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  Request failed: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("li.rankings-page__list-item") or soup.select(".ri-page__list-item")
        if not rows:
            break

        for row in rows:
            r = _parse_247_row(row, year)
            if r:
                recruits.append(r)

        time.sleep(delay)

    return recruits


def _parse_247_row(row, year: int) -> dict | None:
    try:
        name_el = row.select_one(".ri-page__name-block a") or row.select_one(".rankings-page__name-block a")
        if not name_el:
            return None
        name = name_el.get_text(strip=True)

        stars_els = row.select(".ri-page__star-and-score .yellow") or row.select(".icon-starsolid.yellow")
        stars = len(stars_els)

        score_el = row.select_one(".ri-page__star-and-score .score") or row.select_one(".comp_score")
        composite_score = None
        if score_el:
            try:
                composite_score = float(score_el.get_text(strip=True))
            except ValueError:
                pass

        nat_rank_el = row.select_one(".rankings-page__list-item .primary") or row.select_one(".natrank")
        national_rank = None
        if nat_rank_el:
            try:
                national_rank = int(nat_rank_el.get_text(strip=True).replace(",", ""))
            except ValueError:
                pass

        pos_rank_el = row.select_one(".posrank") or row.select_one(".rankings-page__list-item .posrank")
        position_rank = None
        if pos_rank_el:
            try:
                position_rank = int(pos_rank_el.get_text(strip=True).replace(",", ""))
            except ValueError:
                pass

        pos_el = row.select_one(".position") or row.select_one(".ri-page__position")
        position = pos_el.get_text(strip=True) if pos_el else None

        team_el = row.select_one(".ri-page__school a") or row.select_one(".rankings-page__list-item .ist")
        committed_team = team_el.get_text(strip=True) if team_el else None

        return {
            "name": name, "recruit_year": year, "stars": stars,
            "composite_score": composite_score, "national_rank": national_rank,
            "position_rank": position_rank, "state_rank": None,
            "position": position, "committed_team": committed_team,
        }
    except Exception as e:
        print(f"  Row parse error: {e}")
        return None


# ---------------------------------------------------------------------------
# Player and team indexes (psycopg2 — no 1000-row REST limit)
# ---------------------------------------------------------------------------

def build_player_name_index() -> dict:
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


def build_team_name_index() -> dict:
    """Build {school_lower: team_id}."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, school FROM teams")
        return {school.lower(): tid for tid, school in cur.fetchall()}


# ---------------------------------------------------------------------------
# Player matching
# ---------------------------------------------------------------------------

def fuzzy_match_player(
    name: str,
    committed_team: str | None,
    player_index: dict,
    threshold: float = 0.85,
) -> int | None:
    name_l = name.lower().strip()
    for suffix in [" jr.", " jr", " sr.", " sr", " ii", " iii", " iv"]:
        if name_l.endswith(suffix):
            name_l = name_l[: -len(suffix)].strip()

    if name_l in player_index:
        candidates = player_index[name_l]
        if len(candidates) == 1:
            return candidates[0][0]
        if committed_team:
            team_l = committed_team.lower()
            for pid, team in candidates:
                if team == team_l:
                    return pid
        return candidates[0][0]

    matches = difflib.get_close_matches(name_l, player_index.keys(), n=3, cutoff=threshold)
    for match in matches:
        candidates = player_index[match]
        if committed_team:
            team_l = committed_team.lower()
            for pid, team in candidates:
                if team == team_l:
                    return pid
        if len(candidates) == 1:
            return candidates[0][0]

    return None


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert_recruiting(recruits: list[dict], player_index: dict, team_index: dict) -> None:
    rows = []
    unmatched = 0

    for r in recruits:
        player_id = fuzzy_match_player(r["name"], r.get("committed_team"), player_index)
        if player_id is None:
            unmatched += 1
            continue

        committed_team_id = team_index.get((r.get("committed_team") or "").lower())

        rows.append({
            "player_id":         player_id,
            "recruit_year":      r["recruit_year"],
            "stars":             r.get("stars"),
            "national_rank":     r.get("national_rank"),
            "position_rank":     r.get("position_rank"),
            "state_rank":        r.get("state_rank"),
            "composite_score":   r.get("composite_score"),
            "committed_team_id": committed_team_id,
            "source":            "247sports",
        })

    # Dedup by (player_id, recruit_year) — keep highest national_rank (lowest number = better)
    seen: dict = {}
    for r in rows:
        key = (r["player_id"], r["recruit_year"])
        existing = seen.get(key)
        if existing is None:
            seen[key] = r
        else:
            # prefer row with a non-None national_rank, or lower rank number
            cur_rank = r.get("national_rank")
            ex_rank = existing.get("national_rank")
            if cur_rank is not None and (ex_rank is None or cur_rank < ex_rank):
                seen[key] = r
    rows = list(seen.values())

    if rows:
        bulk_upsert("recruiting", rows, ["player_id", "recruit_year"])
    print(f"  Upserted {len(rows)} recruiting rows ({unmatched} unmatched players)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch recruiting data → Supabase")
    parser.add_argument("--year", type=int, help="Single year to fetch")
    args = parser.parse_args()

    api_key = load_api_key()
    years = [args.year] if args.year else YEARS_DEFAULT

    print("Building player index...")
    player_index = build_player_name_index()
    team_index = build_team_name_index()
    print(f"  {len(player_index)} player names, {len(team_index)} teams loaded")

    for year in years:
        print(f"\n--- Recruiting class {year} ---")
        recruits = fetch_recruiting_api(api_key, year)

        if not recruits:
            print(f"  API returned nothing for {year} — trying 247Sports scrape fallback")
            recruits = scrape_247_class(year)

        print(f"  Got {len(recruits)} recruits")
        if recruits:
            upsert_recruiting(recruits, player_index, team_index)

    print("\nDone.")


if __name__ == "__main__":
    main()
