"""Scrape EA CFB 25 player ratings from TeamCrafters → upsert to ea_ratings table.

Strategy: paginate TeamCrafters /players/CFB25 to bulk-collect all ~11k players,
then fuzzy-match to our player DB by name + team. No per-player searches needed.

Usage:
    python scripts/09_scrape_ea_cfb25.py
    python scripts/09_scrape_ea_cfb25.py --limit 500   # dev/test run
    python scripts/09_scrape_ea_cfb25.py --no-headless  # visible browser for debugging
"""

import argparse
import difflib
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.db import bulk_upsert, get_connection

BASE_URL = "https://www.teamcrafters.net/players/CFB25"
SLEEP_SEC = 2.0
PAGE_LOAD_WAIT = 15
EA_SEASON = 2024  # CFB 25 covers the 2024 season roster


# ---------------------------------------------------------------------------
# Selenium driver
# ---------------------------------------------------------------------------

def _make_driver(headless: bool = True):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
    except ImportError:
        service = Service()

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    return webdriver.Chrome(service=service, options=opts)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def scrape_all_players(limit: int | None = None, headless: bool = True) -> list[dict]:
    """
    Paginate TeamCrafters player list, collecting all players.
    Returns list of raw dicts: {name, team, position, ovr, attributes}.
    """
    from bs4 import BeautifulSoup
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    driver = _make_driver(headless=headless)
    all_players: list[dict] = []
    page = 1

    try:
        while True:
            url = f"{BASE_URL}?page={page}" if page > 1 else BASE_URL
            print(f"  Page {page}: {url}")
            driver.get(url)

            # Wait for player rows to appear
            try:
                WebDriverWait(driver, PAGE_LOAD_WAIT).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr, [class*='player-row'], [class*='PlayerRow']"))
                )
            except Exception:
                # Try waiting for any table
                try:
                    WebDriverWait(driver, PAGE_LOAD_WAIT).until(
                        EC.presence_of_element_located((By.TAG_NAME, "table"))
                    )
                except Exception:
                    print(f"  Timed out on page {page} — stopping.")
                    break

            time.sleep(1)
            soup = BeautifulSoup(driver.page_source, "html.parser")

            players, has_next = _parse_page(soup, page)

            if not players:
                print(f"  No players found on page {page} — stopping.")
                # Save page source for debugging
                debug_path = Path("data") / f"tc_debug_p{page}.html"
                debug_path.parent.mkdir(exist_ok=True)
                debug_path.write_text(driver.page_source, encoding="utf-8")
                print(f"  Saved debug HTML to {debug_path}")
                break

            all_players.extend(players)
            print(f"    {len(players)} players (total: {len(all_players)})")

            if limit and len(all_players) >= limit:
                print(f"  Limit {limit} reached.")
                break

            if not has_next:
                print("  No next page — done.")
                break

            page += 1
            time.sleep(SLEEP_SEC)

    finally:
        driver.quit()

    return all_players[:limit] if limit else all_players


def _parse_page(soup, page: int) -> tuple[list[dict], bool]:
    """
    Parse a TeamCrafters player listing page.
    Returns (players_list, has_next_page).
    Handles both table-based and card-based layouts.
    """
    players = []

    # --- Table layout (most likely) ---
    table = soup.select_one("table")
    if table:
        headers = [th.get_text(strip=True).lower() for th in table.select("thead th")]
        for row in table.select("tbody tr"):
            cells = [td.get_text(strip=True) for td in row.select("td")]
            if not cells:
                continue
            player = _cells_to_player(headers, cells)
            if player:
                players.append(player)
    else:
        # --- Card layout fallback ---
        cards = (
            soup.select("[class*='player-card']")
            or soup.select("[class*='PlayerCard']")
            or soup.select("[class*='player-row']")
        )
        for card in cards:
            player = _card_to_player(card)
            if player:
                players.append(player)

    # Detect next page — look for pagination next button or page number links
    has_next = False
    next_btn = soup.select_one("a[rel='next'], [class*='next']:not([disabled]), [aria-label='Next']")
    if next_btn and next_btn.get("href"):
        has_next = True
    # Also check if a page link higher than current page exists
    page_links = soup.select("a[href*='page=']")
    for link in page_links:
        href = link.get("href", "")
        import re
        m = re.search(r"page=(\d+)", href)
        if m and int(m.group(1)) > page:
            has_next = True
            break

    return players, has_next


def _cells_to_player(headers: list[str], cells: list[str]) -> dict | None:
    """Map table cells to player dict using header names."""
    if not cells:
        return None

    data = {}
    for i, h in enumerate(headers):
        if i < len(cells):
            data[h] = cells[i]

    # Flexible header matching
    name = (
        data.get("name") or data.get("player") or data.get("player name")
        or (cells[0] if cells else None)
    )
    if not name:
        return None

    team = data.get("team") or data.get("school") or data.get("college")
    position = data.get("pos") or data.get("position")
    ovr = data.get("ovr") or data.get("overall") or data.get("rating")

    # Collect all numeric attribute columns as the attributes dict
    skip_keys = {"name", "player", "player name", "team", "school", "college", "pos", "position", "ovr", "overall", "rating", "#", "rank"}
    attributes = {}
    for k, v in data.items():
        if k in skip_keys:
            continue
        try:
            attributes[k] = int(v)
        except (ValueError, TypeError):
            pass

    try:
        ovr_int = int(ovr) if ovr else None
    except ValueError:
        ovr_int = None

    return {
        "name":       name.strip(),
        "team":       team.strip() if team else None,
        "position":   position.strip() if position else None,
        "ovr":        ovr_int,
        "attributes": attributes,
    }


def _card_to_player(card) -> dict | None:
    """Parse a card-layout player element."""
    name_el = (
        card.select_one("[class*='name']")
        or card.select_one("[class*='Name']")
        or card.select_one("h3, h4")
    )
    if not name_el:
        return None
    name = name_el.get_text(strip=True)

    team_el = card.select_one("[class*='team'], [class*='Team'], [class*='school']")
    team = team_el.get_text(strip=True) if team_el else None

    pos_el = card.select_one("[class*='pos'], [class*='Pos'], [class*='position']")
    position = pos_el.get_text(strip=True) if pos_el else None

    ovr_el = card.select_one("[class*='ovr'], [class*='Ovr'], [class*='overall'], [class*='rating']")
    ovr = None
    if ovr_el:
        try:
            ovr = int(ovr_el.get_text(strip=True))
        except ValueError:
            pass

    return {"name": name, "team": team, "position": position, "ovr": ovr, "attributes": {}}


# ---------------------------------------------------------------------------
# Player matching
# ---------------------------------------------------------------------------

def build_player_index() -> dict:
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


def _strip_suffix(name: str) -> str:
    for suffix in [" jr.", " jr", " sr.", " sr", " ii", " iii", " iv"]:
        if name.endswith(suffix):
            return name[: -len(suffix)].strip()
    return name


def match_player(name: str, team: str | None, player_index: dict, threshold: float = 0.85) -> int | None:
    name_l = _strip_suffix(name.lower().strip())
    team_l = team.lower().strip() if team else None

    candidates = player_index.get(name_l, [])
    if candidates:
        if team_l:
            for pid, t in candidates:
                if t == team_l:
                    return pid
        return candidates[0][0]

    matches = difflib.get_close_matches(name_l, player_index.keys(), n=3, cutoff=threshold)
    for match in matches:
        cands = player_index[match]
        if team_l:
            for pid, t in cands:
                if t == team_l:
                    return pid
        if len(cands) == 1:
            return cands[0][0]
    return None


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert_ea_ratings(players: list[dict], player_index: dict) -> None:
    rows = []
    unmatched = 0

    for p in players:
        player_id = match_player(p["name"], p.get("team"), player_index)
        if player_id is None:
            unmatched += 1
            continue

        rows.append({
            "player_id":   player_id,
            "source":      "ea_cfb25",
            "ea_season":   EA_SEASON,
            "ovr":         p.get("ovr"),
            "position":    p.get("position"),
            "attributes":  json.dumps(p.get("attributes") or {}),
        })

    # Dedup by (player_id, source, ea_season)
    seen: set = set()
    deduped = []
    for r in rows:
        key = (r["player_id"], r["source"], r["ea_season"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    if deduped:
        bulk_upsert("ea_ratings", deduped, ["player_id", "source", "ea_season"])
    print(f"  Upserted {len(deduped)} EA CFB 25 ratings ({unmatched} unmatched)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Max players to scrape (for testing)")
    parser.add_argument("--no-headless", action="store_true", help="Show browser window (debugging)")
    args = parser.parse_args()

    print("Building player index...")
    player_index = build_player_index()
    print(f"  {len(player_index)} player names loaded")

    print("Scraping TeamCrafters EA CFB 25 ratings...")
    players = scrape_all_players(limit=args.limit, headless=not args.no_headless)
    print(f"Scraped {len(players)} players from TeamCrafters")

    if players:
        upsert_ea_ratings(players, player_index)

    print("Done.")


if __name__ == "__main__":
    main()
