"""Fetch recruiting rankings from CFB Data API -> data/raw/recruiting.json.

Usage:
    python scripts/02_harvest_recruiting.py              # 2005-2025
    python scripts/02_harvest_recruiting.py --year 2024  # single year
"""

import argparse
import difflib
import json
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.api_client import load_api_key, fetch_recruiting
from utils.store import read_raw, RAW_DIR

YEARS_DEFAULT = list(range(2005, 2026))

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
    raw = fetch_recruiting(api_key, year)
    results = []
    for r in raw:
        results.append({
            "name":            r.get("name", ""),
            "recruit_year":    year,
            "stars":           r.get("stars"),
            "composite_score": r.get("rating"),
            "national_rank":   r.get("ranking"),
            "position":        r.get("position"),
            "committed_team":  r.get("committedTo"),
            "position_rank":   None,
            "state_rank":      None,
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
                print("  403 Forbidden. Stopping.")
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
# Player/team indexes built from local JSON
# ---------------------------------------------------------------------------

def build_player_name_index() -> dict:
    """Build {name_lower: [(player_id, team_lower)]} from local players + player_seasons."""
    players_df = read_raw("players")
    ps_df      = read_raw("player_seasons")
    teams_df   = read_raw("teams")

    team_id_to_school = {int(r["id"]): (r.get("school") or "").lower()
                         for _, r in teams_df.iterrows()}

    ps_map = {}  # player_id -> set of school names
    for _, r in ps_df.iterrows():
        pid = r.get("player_id")
        tid = r.get("team_id")
        if pid is None:
            continue
        school = team_id_to_school.get(int(tid) if tid is not None else -1, "")
        ps_map.setdefault(int(pid), set()).add(school)

    index: dict = {}
    for _, r in players_df.iterrows():
        pid = r.get("id")
        name = (r.get("name") or "").strip()
        if not pid or not name:
            continue
        key = name.lower()
        for school in ps_map.get(int(pid), {""}):
            entry = (int(pid), school)
            lst = index.setdefault(key, [])
            if entry not in lst:
                lst.append(entry)
    return index


def build_team_name_index() -> dict:
    teams_df = read_raw("teams")
    return {(r.get("school") or "").lower(): int(r["id"])
            for _, r in teams_df.iterrows() if r.get("id") and r.get("school")}


# ---------------------------------------------------------------------------
# Player matching
# ---------------------------------------------------------------------------

def fuzzy_match_player(name: str, committed_team: str | None,
                        player_index: dict, threshold: float = 0.85) -> int | None:
    name_l = name.lower().strip()
    for suffix in [" jr.", " jr", " sr.", " sr", " ii", " iii", " iv"]:
        if name_l.endswith(suffix):
            name_l = name_l[:-len(suffix)].strip()

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
# Save to local JSON
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

    # Dedup by (player_id, recruit_year) — keep best national_rank
    seen: dict = {}
    for r in rows:
        key = (r["player_id"], r["recruit_year"])
        existing = seen.get(key)
        if existing is None:
            seen[key] = r
        else:
            cur_rank = r.get("national_rank")
            ex_rank  = existing.get("national_rank")
            if cur_rank is not None and (ex_rank is None or cur_rank < ex_rank):
                seen[key] = r
    new_rows = dict(seen)

    # Load existing and merge
    path = RAW_DIR / "recruiting.json"
    existing_data = []
    if path.exists():
        with open(path, encoding="utf-8") as f:
            existing_data = json.load(f)

    # Build existing index, replace for matching (player_id, recruit_year)
    existing_map = {(r.get("player_id"), r.get("recruit_year")): r for r in existing_data}
    existing_map.update(new_rows)
    all_recruits = list(existing_map.values())

    with open(path, "w", encoding="utf-8") as f:
        json.dump(all_recruits, f, separators=(",", ":"))

    print(f"  Saved {len(new_rows)} recruiting rows ({unmatched} unmatched). Total: {len(all_recruits)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch recruiting data -> local JSON")
    parser.add_argument("--year", type=int, help="Single year to fetch")
    args = parser.parse_args()

    api_key = load_api_key()
    years = [args.year] if args.year else YEARS_DEFAULT

    print("Building player/team indexes from local JSON...")
    player_index = build_player_name_index()
    team_index   = build_team_name_index()
    print(f"  {len(player_index)} player names, {len(team_index)} teams")

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
