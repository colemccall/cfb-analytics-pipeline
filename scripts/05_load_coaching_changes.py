"""Scrape ESPN coaching changes + load seed CSV → upsert to coaching_changes table.

Two data sources:
  1. Seed CSV  — historical data (pre-2024), always loaded first
  2. ESPN scraper — current cycle HC/OC/DC hires, Selenium-based

Usage:
    python scripts/05_coaching_changes.py               # seed CSV + ESPN scrape
    python scripts/05_coaching_changes.py --csv-only    # seed CSV only
    python scripts/05_coaching_changes.py --espn-only   # ESPN scrape only
    python scripts/05_coaching_changes.py --csv data/my.csv  # custom CSV path
"""

import argparse
import csv
import difflib
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.db import bulk_upsert, get_connection

DEFAULT_CSV = Path(__file__).parent.parent / "data" / "coaching_changes_seed.csv"

# 2024-25 coaching carousel tracker
ESPN_URL = "https://www.espn.com/college-football/story/_/id/38866719/college-football-coaching-changes-tracker-2024-25"

SLEEP_SEC = 2.0
PAGE_LOAD_WAIT = 12

VALID_ROLES = {"HC", "OC", "DC", "ST"}

ESPN_ROLE_MAP = {
    "head coach": "HC",
    "offensive coordinator": "OC",
    "defensive coordinator": "DC",
    "special teams": "ST",
    " oc ": "OC",
    " dc ": "DC",
    " hc ": "HC",
}


# ---------------------------------------------------------------------------
# Selenium driver
# ---------------------------------------------------------------------------

def _make_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
    except ImportError:
        service = Service()

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


# ---------------------------------------------------------------------------
# Team index
# ---------------------------------------------------------------------------

def build_team_index() -> dict:
    """Return {school_lower: team_id}."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, school FROM teams")
        return {school.lower().strip(): tid for tid, school in cur.fetchall()}


def match_team(name: str, team_index: dict, threshold: float = 0.80) -> int | None:
    name_l = name.lower().strip()
    if name_l in team_index:
        return team_index[name_l]
    matches = difflib.get_close_matches(name_l, team_index.keys(), n=1, cutoff=threshold)
    if matches:
        return team_index[matches[0]]
    return None


# ---------------------------------------------------------------------------
# Source 1: seed CSV
# ---------------------------------------------------------------------------

def load_seed_csv(csv_path: Path, team_index: dict) -> list[dict]:
    rows = []
    unmatched = []

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for line in reader:
            team_name = line.get("team", "").strip()
            team_id = match_team(team_name, team_index)
            if team_id is None:
                unmatched.append(team_name)
                continue

            role = line.get("role", "").strip().upper()
            if role not in VALID_ROLES:
                print(f"  Unknown role '{role}' for {line.get('coach_name')} — skipping")
                continue

            end_season = line.get("end_season", "").strip()
            rows.append({
                "team_id":      team_id,
                "coach_name":   line.get("coach_name", "").strip(),
                "role":         role,
                "start_season": int(line["start_season"]) if line.get("start_season") else None,
                "end_season":   int(end_season) if end_season else None,
                "prior_team":   line.get("prior_team", "").strip() or None,
            })

    if unmatched:
        print(f"  Unmatched teams in CSV: {unmatched}")

    return rows


# ---------------------------------------------------------------------------
# Source 2: ESPN coaching tracker (Selenium)
# ---------------------------------------------------------------------------

def scrape_espn_coaching(team_index: dict) -> list[dict]:
    """
    Scrape ESPN coaching changes tracker article.
    Article uses <h2>/<h3> headings per team, <p>/<li> for coach entries.
    """
    from bs4 import BeautifulSoup
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    print(f"  Loading: {ESPN_URL}")
    driver = _make_driver()
    rows = []

    try:
        driver.get(ESPN_URL)
        try:
            WebDriverWait(driver, PAGE_LOAD_WAIT).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "article, .article-body, [class*='story'], [class*='article']")
                )
            )
        except Exception:
            print("  Timed out waiting for ESPN article — page may have moved.")
            return rows

        time.sleep(2)
        soup = BeautifulSoup(driver.page_source, "html.parser")

        article = (
            soup.select_one("article")
            or soup.select_one(".article-body")
            or soup.select_one("[class*='story__body']")
            or soup.select_one("[class*='article__body']")
            or soup.select_one("main")
        )

        if not article:
            print("  Could not find article body — page structure may have changed.")
            return rows

        rows = _parse_espn_article(article, team_index)
    finally:
        driver.quit()

    return rows


def _parse_espn_article(article, team_index: dict) -> list[dict]:
    rows = []
    current_team_id = None

    for el in article.find_all(["h2", "h3", "p", "li"]):
        text = el.get_text(separator=" ", strip=True)
        if not text:
            continue

        if el.name in ("h2", "h3"):
            tid = match_team(text, team_index, threshold=0.75)
            if tid:
                current_team_id = tid
            continue

        if current_team_id is None:
            continue

        entry = _parse_espn_coach_line(text, current_team_id)
        if entry:
            rows.append(entry)

    print(f"  Parsed {len(rows)} coaching entries from ESPN article")
    return rows


def _parse_espn_coach_line(text: str, team_id: int) -> dict | None:
    text_l = " " + text.lower() + " "

    role = None
    for keyword, r in ESPN_ROLE_MAP.items():
        if keyword in text_l:
            role = r
            break
    if role is None:
        return None

    years = re.findall(r"\b(202[0-9])\b", text)
    start_season = int(years[0]) if years else 2024

    is_departure = any(w in text_l for w in [" fired ", " resigned ", " left ", " departed ", " stepping down "])
    end_season = start_season if is_departure else None
    start_season = None if is_departure else start_season

    name_match = re.match(r"^([A-Z][a-z]+(?: [A-Z][a-z]+){1,3})", text)
    if not name_match:
        return None
    coach_name = name_match.group(1).strip()

    from_match = re.search(r"\bfrom\s+([A-Z][A-Za-z &'()\-]+?)(?:\)|,|\.|$)", text)
    prior_team = from_match.group(1).strip() if from_match else None

    return {
        "team_id":      team_id,
        "coach_name":   coach_name,
        "role":         role,
        "start_season": start_season,
        "end_season":   end_season,
        "prior_team":   prior_team,
    }


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert_coaching(rows: list[dict]) -> None:
    if not rows:
        print("  No rows to upsert.")
        return

    valid = [r for r in rows if r.get("coach_name") and r.get("role") and r.get("team_id")]
    skipped = len(rows) - len(valid)
    if skipped:
        print(f"  Skipped {skipped} rows missing required fields")

    # Dedup by conflict key
    seen: set = set()
    deduped = []
    for r in valid:
        key = (r["team_id"], r["coach_name"], r["role"], r.get("start_season"))
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    bulk_upsert("coaching_changes", deduped, ["team_id", "coach_name", "role", "start_season"])
    print(f"  Upserted {len(deduped)} coaching change rows")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--csv-only", action="store_true", help="Skip ESPN scrape")
    parser.add_argument("--espn-only", action="store_true", help="Skip seed CSV")
    args = parser.parse_args()

    print("Building team index...")
    team_index = build_team_index()
    print(f"  {len(team_index)} teams loaded")

    all_rows: list[dict] = []

    if not args.espn_only:
        if args.csv.exists():
            print(f"Loading seed CSV: {args.csv}")
            csv_rows = load_seed_csv(args.csv, team_index)
            print(f"  {len(csv_rows)} rows from CSV")
            all_rows.extend(csv_rows)
        else:
            print(f"  Seed CSV not found: {args.csv} — skipping")

    if not args.csv_only:
        print("Scraping ESPN coaching changes tracker...")
        espn_rows = scrape_espn_coaching(team_index)
        print(f"  {len(espn_rows)} rows from ESPN")
        all_rows.extend(espn_rows)

    print(f"Total: {len(all_rows)} coaching change rows")
    upsert_coaching(all_rows)
    print("Done.")


if __name__ == "__main__":
    main()
