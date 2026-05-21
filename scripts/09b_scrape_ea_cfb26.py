"""Scrape EA CFB 26 player ratings from ea.com/games/ea-sports-college-football/ratings.

The EA ratings page is a React SPA. We use Selenium to drive it:
  1. Load the page, wait for player rows to render.
  2. Optionally filter by position to paginate in smaller sets.
  3. Extract player cards: name, team, position, OVR, attributes.
  4. Fuzzy-match to our player DB → upsert to ea_ratings (source='ea_cfb26').

Usage:
    python scripts/09b_scrape_ea_cfb26.py
    python scripts/09b_scrape_ea_cfb26.py --limit 500   # dev/test
    python scripts/09b_scrape_ea_cfb26.py --no-headless  # visible browser
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

EA_RATINGS_URL = "https://www.ea.com/games/ea-sports-college-football/ratings"
EA_SEASON      = 2025   # CFB 26 covers the 2025 season roster
SLEEP_SEC      = 2.5
PAGE_LOAD_WAIT = 20


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
# Scraping — EA.com ratings page
# ---------------------------------------------------------------------------

def scrape_all_players(limit: int | None = None, headless: bool = True) -> list[dict]:
    """
    Collect all ~11k EA CFB 26 players from ea.com ratings page.
    The page renders in React; we scroll/paginate to load all rows.
    Returns list of dicts: {name, team, position, ovr, attributes}.
    """
    from bs4 import BeautifulSoup
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    driver = _make_driver(headless=headless)
    all_players: list[dict] = []

    try:
        print(f"  Loading {EA_RATINGS_URL}")
        driver.get(EA_RATINGS_URL)

        # Wait for the page to render player data
        try:
            WebDriverWait(driver, PAGE_LOAD_WAIT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR,
                    "[class*='player'], [class*='Player'], table tbody tr, [data-testid*='player']"
                ))
            )
        except Exception:
            print("  Initial render wait timed out — trying anyway")

        time.sleep(3)

        # Save initial HTML for debugging
        debug_dir = Path("data")
        debug_dir.mkdir(exist_ok=True)

        page_num = 1
        prev_count = -1

        while True:
            soup = BeautifulSoup(driver.page_source, "html.parser")
            players = _parse_player_list(soup)

            if len(players) == prev_count:
                # No new players loaded after scroll — try clicking next page
                has_next = _click_next_page(driver)
                if not has_next:
                    print(f"  No more pages — done. Total: {len(all_players)}")
                    break
                time.sleep(SLEEP_SEC)
                page_num += 1
                soup = BeautifulSoup(driver.page_source, "html.parser")
                players = _parse_player_list(soup)

            # Collect only new players not already seen (by index)
            new_start = len(all_players)
            for p in players[new_start:]:
                all_players.append(p)

            if len(all_players) == prev_count:
                # Still nothing new after page turn — bail
                break

            prev_count = len(all_players)
            print(f"  Page {page_num}: {len(players)} visible, total collected: {len(all_players)}")

            if limit and len(all_players) >= limit:
                print(f"  Limit {limit} reached.")
                break

            # Scroll down to trigger lazy loading if paginated by infinite scroll
            _scroll_to_bottom(driver)
            time.sleep(SLEEP_SEC)

    finally:
        driver.quit()

    # Save raw dump for inspection
    raw_path = debug_dir / "ea_cfb26_raw.json"
    raw_path.write_text(json.dumps(all_players[:500], indent=2), encoding="utf-8")
    print(f"  Saved first 500 raw entries to {raw_path}")

    return all_players[:limit] if limit else all_players


def _parse_player_list(soup) -> list[dict]:
    """Parse currently visible player rows from the EA ratings page.

    EA.com has changed layouts over time. We try multiple selectors:
    1. Table rows (most structured)
    2. Card-based divs with player data attributes
    3. Generic divs with name + OVR pattern
    """
    players = []

    # Try table layout
    table = soup.select_one("table")
    if table:
        headers = [th.get_text(strip=True).lower() for th in table.select("thead th")]
        for row in table.select("tbody tr"):
            cells = [td.get_text(strip=True) for td in row.select("td")]
            if not cells:
                continue
            p = _table_row_to_player(headers, cells)
            if p:
                players.append(p)
        if players:
            return players

    # Try EA-specific card/row selectors
    row_selectors = [
        "[class*='PlayerListItem']",
        "[class*='player-item']",
        "[class*='ratings-row']",
        "[class*='RatingRow']",
        "[data-testid*='player']",
    ]
    for sel in row_selectors:
        cards = soup.select(sel)
        if cards:
            for card in cards:
                p = _card_to_player(card)
                if p:
                    players.append(p)
            if players:
                return players

    return players


def _table_row_to_player(headers: list[str], cells: list[str]) -> dict | None:
    """Map table row to player dict."""
    if not cells:
        return None
    data = {h: cells[i] for i, h in enumerate(headers) if i < len(cells)}

    name = data.get("name") or data.get("player") or cells[0] if cells else None
    if not name:
        return None

    team = data.get("team") or data.get("school") or data.get("college")
    position = data.get("pos") or data.get("position")
    ovr = data.get("ovr") or data.get("overall")

    skip_keys = {"name", "player", "team", "school", "college", "pos", "position", "ovr", "overall", "#"}
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
    except (ValueError, TypeError):
        ovr_int = None

    return {"name": name.strip(), "team": team.strip() if team else None,
            "position": position.strip() if position else None, "ovr": ovr_int, "attributes": attributes}


def _card_to_player(card) -> dict | None:
    """Parse a card/div-based player element from EA's SPA."""
    # Look for OVR number first — if no number found, skip
    ovr = None
    ovr_els = card.select("[class*='ovr'], [class*='Ovr'], [class*='overall'], [class*='Overall'], [class*='rating']")
    for el in ovr_els:
        try:
            ovr = int(el.get_text(strip=True))
            break
        except ValueError:
            pass

    name_el = (
        card.select_one("[class*='name'], [class*='Name']")
        or card.select_one("h2, h3, h4, strong")
    )
    if not name_el:
        return None
    name = name_el.get_text(strip=True)
    if not name or len(name) > 60:
        return None

    team_el = card.select_one("[class*='team'], [class*='Team'], [class*='school'], [class*='School']")
    team = team_el.get_text(strip=True) if team_el else None

    pos_el = card.select_one("[class*='pos'], [class*='Pos'], [class*='position']")
    position = pos_el.get_text(strip=True) if pos_el else None

    # Extract named attribute bars (spd, agi, str, etc.)
    attributes = {}
    for attr_el in card.select("[class*='attr'], [class*='Attr'], [class*='stat-bar']"):
        label_el = attr_el.select_one("[class*='label'], [class*='Label'], abbr")
        val_el   = attr_el.select_one("[class*='value'], [class*='Value']")
        if label_el and val_el:
            try:
                attributes[label_el.get_text(strip=True).lower()] = int(val_el.get_text(strip=True))
            except ValueError:
                pass

    return {"name": name, "team": team, "position": position, "ovr": ovr, "attributes": attributes}


def _scroll_to_bottom(driver) -> None:
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")


def _click_next_page(driver) -> bool:
    """Try to click a Next Page button. Returns True if found and clicked."""
    from selenium.webdriver.common.by import By

    next_selectors = [
        "[aria-label='Next page']",
        "[aria-label='Next']",
        "button[class*='next']",
        "a[class*='next']",
        "[data-testid='next-page']",
    ]
    for sel in next_selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn.is_displayed() and btn.is_enabled():
                driver.execute_script("arguments[0].click();", btn)
                return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Player matching (same logic as 09_scrape_ea_cfb25.py)
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
            "player_id":  player_id,
            "source":     "ea_cfb26",
            "ea_season":  EA_SEASON,
            "ovr":        p.get("ovr"),
            "position":   p.get("position"),
            "attributes": json.dumps(p.get("attributes") or {}),
        })

    seen: set = set()
    deduped = []
    for r in rows:
        key = (r["player_id"], r["source"], r["ea_season"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    if deduped:
        bulk_upsert("ea_ratings", deduped, ["player_id", "source", "ea_season"])
    print(f"  Upserted {len(deduped)} EA CFB 26 ratings ({unmatched} unmatched, {len(players) - unmatched - len(deduped)} deduped)")


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

    print("Scraping EA CFB 26 ratings from ea.com...")
    players = scrape_all_players(limit=args.limit, headless=not args.no_headless)
    print(f"Scraped {len(players)} players from EA.com")

    if players:
        upsert_ea_ratings(players, player_index)
    else:
        print("WARNING: No players scraped — the page layout may have changed.")
        print("Run with --no-headless to debug visually, or check data/ea_cfb26_raw.json")

    print("Done.")


if __name__ == "__main__":
    main()
