"""Scrape On3 NIL valuations → data/raw/nil_valuations.json.

On3 is JS-rendered — uses Selenium headless Chrome to fetch pages.
Re-run periodically (monthly or start of season) to refresh valuations.
Only current-year data is fetched.

STATUS: On3 actively blocks scraping, so this currently returns 0 rows and
nil_valuations.json stays empty. Engine B (script 11) runs recruiting-only until
a working NIL source lands. The scrape/parse path below is kept because the
matching and storage half is source-agnostic — a replacement source only needs
to produce {name, team, position, valuation_usd} dicts.

Usage:
    python scripts/04_harvest_nil_valuations.py
    python scripts/04_harvest_nil_valuations.py --pages 10
"""

import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.json_utils import write_json
from utils.matching import build_player_index, match_player, resolve_collisions
from utils.store import read_raw, RAW_DIR

ON3_URL = "https://www.on3.com/nil/rankings/player/college/football/"
SLEEP_SEC = 3.0
MAX_PAGES = 20  # ~50 players/page → top ~1000 NIL earners
PAGE_LOAD_WAIT = 10  # seconds to wait for JS render

OUTPUT = RAW_DIR / "nil_valuations.json"

# NIL rankings are current-roster; match against recent seasons only.
MATCH_SEASONS = [2025, 2024]


# ---------------------------------------------------------------------------
# Selenium helpers
# ---------------------------------------------------------------------------

def _make_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
    except ImportError:
        service = Service()  # assumes chromedriver on PATH

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    return webdriver.Chrome(service=service, options=opts)


def _wait_for_players(driver, timeout: int = PAGE_LOAD_WAIT):
    """Wait until at least one player row is present in the DOM."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    # On3 uses React — player rows appear inside a ranked list
    selectors = [
        "[class*='NilRankingPlayer']",
        "[class*='RankingPlayer']",
        "[class*='nil-player']",
        "li[class*='Player']",
    ]
    combined = ", ".join(selectors)
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, combined))
    )


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def scrape_nil_rankings(max_pages: int = MAX_PAGES) -> list[dict]:
    from bs4 import BeautifulSoup

    driver = _make_driver()
    entries = []

    try:
        for page in range(1, max_pages + 1):
            url = f"{ON3_URL}?page={page}" if page > 1 else ON3_URL
            print(f"  Page {page}: {url}")
            driver.get(url)

            try:
                _wait_for_players(driver)
            except Exception:
                print(f"  Timed out waiting for players on page {page} — stopping.")
                break

            soup = BeautifulSoup(driver.page_source, "html.parser")

            # Try multiple selector patterns — On3 uses hashed class names
            rows = (
                soup.select("[class*='NilRankingPlayer']")
                or soup.select("[class*='RankingPlayer__Item']")
                or soup.select("[class*='nil-player-row']")
                or soup.select("li[class*='Player']")
            )

            if not rows:
                print(f"  No player rows found on page {page} — stopping.")
                break

            page_entries = [_parse_nil_row(r) for r in rows]
            page_entries = [e for e in page_entries if e]
            entries.extend(page_entries)
            print(f"    {len(page_entries)} players parsed")

            time.sleep(SLEEP_SEC)
    finally:
        driver.quit()

    return entries


def _parse_nil_row(row) -> dict | None:
    try:
        name_el = (
            row.select_one("[class*='PlayerName']")
            or row.select_one("[class*='player-name']")
            or row.select_one("a[href*='/player/']")
        )
        if not name_el:
            return None
        name = name_el.get_text(strip=True)
        if not name:
            return None

        val_el = (
            row.select_one("[class*='NILValue']")
            or row.select_one("[class*='NilValue']")
            or row.select_one("[class*='nil-value']")
            or row.select_one("[class*='Valuation']")
        )
        valuation_usd = None
        if val_el:
            raw = val_el.get_text(strip=True).replace("$", "").replace(",", "").strip()
            if raw.upper().endswith("M"):
                try:
                    valuation_usd = int(float(raw[:-1]) * 1_000_000)
                except ValueError:
                    pass
            elif raw.upper().endswith("K"):
                try:
                    valuation_usd = int(float(raw[:-1]) * 1_000)
                except ValueError:
                    pass
            else:
                try:
                    valuation_usd = int(float(raw))
                except ValueError:
                    pass

        team_el = (
            row.select_one("[class*='School']")
            or row.select_one("[class*='school-name']")
            or row.select_one("[class*='Team']")
        )
        team = team_el.get_text(strip=True) if team_el else None

        pos_el = (
            row.select_one("[class*='Position']")
            or row.select_one("[class*='position']")
        )
        position = pos_el.get_text(strip=True) if pos_el else None

        return {"name": name, "team": team, "position": position, "valuation_usd": valuation_usd}
    except Exception as e:
        print(f"  Row parse error: {e}")
        return None


# ---------------------------------------------------------------------------
# Save to local JSON
# ---------------------------------------------------------------------------

def save_nil(entries: list[dict], player_index: dict) -> None:
    """Match scraped entries to our players and merge into nil_valuations.json."""
    today = date.today().isoformat()

    for e in entries:
        pid, psid, how = match_player(e["name"], e.get("team"), player_index)
        e["player_id"]        = pid
        e["player_season_id"] = psid
        e["match_type"]       = how
    dropped = resolve_collisions(entries)
    if dropped:
        print(f"  Dropped {dropped} ambiguous matches (shared names)")

    rows = [{
        "player_id":     e["player_id"],
        "valuation_usd": e.get("valuation_usd"),
        "source":        "on3",
        "as_of_date":    today,
    } for e in entries if e.get("player_id") is not None]
    unmatched = len(entries) - len(rows)

    # Dedup by (player_id, as_of_date)
    new_rows = {(r["player_id"], r["as_of_date"]): r for r in rows}

    existing_df = read_raw("nil_valuations")
    merged: dict = {}
    if not existing_df.empty:
        for r in existing_df.to_dict("records"):
            merged[(r.get("player_id"), r.get("as_of_date"))] = r
    merged.update(new_rows)

    write_json(OUTPUT, list(merged.values()))
    print(f"  Saved {len(new_rows)} NIL valuations ({unmatched} unmatched). Total: {len(merged)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="On3 NIL valuations -> data/raw/nil_valuations.json")
    parser.add_argument("--pages", type=int, default=MAX_PAGES, help="Max pages to scrape (default 20)")
    args = parser.parse_args()

    print("Building player index...")
    player_index = build_player_index(seasons=MATCH_SEASONS)
    print(f"  {len(player_index)} player names loaded")

    print(f"Scraping On3 NIL rankings (up to {args.pages} pages)...")
    entries = scrape_nil_rankings(max_pages=args.pages)
    print(f"Scraped {len(entries)} NIL entries")

    if entries:
        save_nil(entries, player_index)
    else:
        print("  No entries scraped - On3 blocks automated requests. "
              "nil_valuations.json left unchanged.")

    print("Done.")


if __name__ == "__main__":
    main()
