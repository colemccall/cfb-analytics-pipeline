"""Scrape EA CFB 26 player ratings from ea.com/games/ea-sports-college-football/ratings.

The EA ratings page is a React SPA. Playwright intercepts XHR responses to
capture raw player JSON without DOM parsing (primary strategy). Falls back to
DOM scraping if XHR capture yields no players.

Output: data/ea_cfb26_raw.json (raw scraped records)
        data/computed/ea_ratings.json (matched to local player IDs)

Usage:
    python scripts/08b_harvest_ea_cfb26_ratings.py
    python scripts/08b_harvest_ea_cfb26_ratings.py --limit 500
    python scripts/08b_harvest_ea_cfb26_ratings.py --no-headless
    python scripts/08b_harvest_ea_cfb26_ratings.py --fallback-dom
"""

import argparse
import asyncio
import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw
from utils.json_utils import write_json

EA_RATINGS_URL = "https://www.ea.com/games/ea-sports-college-football/ratings"
EA_SEASON      = 2025   # CFB 26 covers the 2025 season roster

RAW_OUTPUT     = Path(__file__).parent.parent / "data" / "ea_cfb26_raw.json"
COMPUTED_OUTPUT = (
    Path(__file__).parent.parent.parent
    / "cfb-analytics-app" / "data" / "ea_ratings.json"
)


# ---------------------------------------------------------------------------
# XHR network interception
# ---------------------------------------------------------------------------

def _extract_players_from_json(data, limit: int | None) -> list[dict]:
    """Recursively search JSON payload for objects that look like player records."""
    found: list[dict] = []

    def _walk(node):
        if isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, dict):
            name = node.get("name") or node.get("fullName") or node.get("playerName")
            ovr  = node.get("ovr") or node.get("overall") or node.get("overallRating")
            if name and isinstance(name, str) and ovr and str(ovr).isdigit():
                found.append({
                    "name":       name.strip(),
                    "team":       node.get("team") or node.get("school") or node.get("teamName"),
                    "position":   node.get("position") or node.get("pos"),
                    "ovr":        int(ovr),
                    "attributes": {
                        k: v for k, v in node.items()
                        if k not in ("name", "fullName", "playerName", "team", "school",
                                     "teamName", "position", "pos", "ovr", "overall",
                                     "overallRating")
                        and isinstance(v, int) and 1 <= v <= 99
                    },
                })
                if limit and len(found) >= limit:
                    return
            else:
                for v in node.values():
                    _walk(v)
                    if limit and len(found) >= limit:
                        return

    _walk(data)
    return found


async def _scrape_via_xhr(headless: bool, limit: int | None) -> list[dict]:
    """Primary strategy: intercept XHR responses from the EA SPA."""
    from playwright.async_api import async_playwright

    captured: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-http2",
                "--ignore-certificate-errors",
                "--ignore-ssl-errors",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(ignore_https_errors=True)
        page    = await context.new_page()

        async def handle_response(response):
            url = response.url.lower()
            if not any(kw in url for kw in ["player", "ratings", "roster", "cfb", "madden"]):
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                data = await response.json()
                players = _extract_players_from_json(data, limit)
                if players:
                    captured.extend(players)
                    print(f"  XHR captured {len(players)} players from {response.url[:80]}")
            except Exception:
                pass

        page.on("response", handle_response)

        print(f"  Loading {EA_RATINGS_URL}")
        try:
            await page.goto(EA_RATINGS_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(5_000)
        except Exception as e:
            print(f"  Page load warning: {e}")

        # Trigger position dropdown clicks to force additional XHR loads
        try:
            for sel in ["[class*='position']", "[class*='filter']", "select"]:
                dropdowns = await page.query_selector_all(sel)
                for dd in dropdowns[:3]:
                    try:
                        await dd.click()
                        await page.wait_for_timeout(1500)
                    except Exception:
                        pass
        except Exception:
            pass

        await context.close()
        await browser.close()

    return captured[:limit] if limit and captured else captured


# ---------------------------------------------------------------------------
# DOM fallback scraping (carried over from Selenium version)
# ---------------------------------------------------------------------------

async def _scrape_via_dom(headless: bool, limit: int | None) -> list[dict]:
    """Fallback: parse rendered DOM when XHR interception yields nothing."""
    from bs4 import BeautifulSoup
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--disable-http2", "--ignore-certificate-errors"],
        )
        context = await browser.new_context(ignore_https_errors=True)
        page    = await context.new_page()

        print(f"  [fallback] Loading {EA_RATINGS_URL}")
        try:
            await page.goto(EA_RATINGS_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(5_000)
        except Exception as e:
            print(f"  Page load warning: {e}")

        try:
            html = await page.content()
        except Exception:
            html = ""
        await context.close()
        await browser.close()

    soup    = BeautifulSoup(html, "html.parser")
    players = _parse_player_list(soup)
    return players[:limit] if limit else players


def _parse_player_list(soup) -> list[dict]:
    players = []

    table = soup.select_one("table")
    if table:
        headers = [th.get_text(strip=True).lower() for th in table.select("thead th")]
        for row in table.select("tbody tr"):
            cells = [td.get_text(strip=True) for td in row.select("td")]
            p = _table_row_to_player(headers, cells)
            if p:
                players.append(p)
        if players:
            return players

    for sel in ["[class*='PlayerListItem']", "[class*='player-item']",
                "[class*='ratings-row']", "[class*='RatingRow']", "[data-testid*='player']"]:
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
    if not cells:
        return None
    data = {h: cells[i] for i, h in enumerate(headers) if i < len(cells)}
    name = data.get("name") or data.get("player") or (cells[0] if cells else None)
    if not name:
        return None
    team     = data.get("team") or data.get("school") or data.get("college")
    position = data.get("pos") or data.get("position")
    ovr      = data.get("ovr") or data.get("overall")
    skip     = {"name", "player", "team", "school", "college", "pos", "position", "ovr", "overall", "#"}
    attributes = {}
    for k, v in data.items():
        if k in skip:
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
            "position": position.strip() if position else None,
            "ovr": ovr_int, "attributes": attributes}


def _card_to_player(card) -> dict | None:
    ovr = None
    for el in card.select("[class*='ovr'],[class*='Ovr'],[class*='overall'],[class*='rating']"):
        try:
            ovr = int(el.get_text(strip=True))
            break
        except ValueError:
            pass
    name_el = card.select_one("[class*='name'],[class*='Name']") or card.select_one("h2,h3,h4,strong")
    if not name_el:
        return None
    name = name_el.get_text(strip=True)
    if not name or len(name) > 60:
        return None
    team_el = card.select_one("[class*='team'],[class*='Team'],[class*='school']")
    team    = team_el.get_text(strip=True) if team_el else None
    pos_el  = card.select_one("[class*='pos'],[class*='Pos'],[class*='position']")
    pos     = pos_el.get_text(strip=True) if pos_el else None
    attributes = {}
    for attr_el in card.select("[class*='attr'],[class*='Attr'],[class*='stat-bar']"):
        lbl = attr_el.select_one("[class*='label'],[class*='Label'],abbr")
        val = attr_el.select_one("[class*='value'],[class*='Value']")
        if lbl and val:
            try:
                attributes[lbl.get_text(strip=True).lower()] = int(val.get_text(strip=True))
            except ValueError:
                pass
    return {"name": name, "team": team, "position": pos, "ovr": ovr, "attributes": attributes}


# ---------------------------------------------------------------------------
# Player matching — uses local JSON (no DB required)
# ---------------------------------------------------------------------------

def build_player_index() -> dict:
    """Build {name_lower: [(player_id, team_lower), ...]} from local JSON."""
    players        = read_raw("players")
    player_seasons = read_raw("player_seasons")
    teams          = read_raw("teams")

    # Build team_id → school lookup
    school_by_id: dict = {}
    if not teams.empty:
        for _, t in teams.iterrows():
            tid = t.get("id")
            if tid is not None:
                school_by_id[int(tid)] = (t.get("school") or "").lower()

    # Use player_seasons to get team for each player
    player_team: dict = {}
    if not player_seasons.empty:
        for _, ps in player_seasons.iterrows():
            pid = ps.get("player_id")
            tid = ps.get("team_id")
            if pid is not None and tid is not None:
                player_team[int(pid)] = school_by_id.get(int(tid), "")

    index: dict = {}
    if not players.empty:
        for _, p in players.iterrows():
            pid  = p.get("id")
            name = p.get("name")
            if pid is None or not name:
                continue
            key  = _strip_suffix(name.lower().strip())
            team = player_team.get(int(pid), "")
            index.setdefault(key, []).append((int(pid), team))
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
# Main
# ---------------------------------------------------------------------------

async def _run(args) -> None:
    headless     = not args.no_headless
    limit        = args.limit
    force_dom    = args.fallback_dom

    print("Building player index from local JSON...")
    player_index = build_player_index()
    print(f"  {len(player_index)} player names loaded")

    players: list[dict] = []

    if not force_dom:
        print("Scraping via XHR interception (Playwright)...")
        players = await _scrape_via_xhr(headless=headless, limit=limit)
        print(f"  XHR strategy: {len(players)} players captured")

    if not players:
        print("XHR capture empty — falling back to DOM scrape...")
        players = await _scrape_via_dom(headless=headless, limit=limit)
        print(f"  DOM strategy: {len(players)} players scraped")

    if not players:
        print("WARNING: No players captured. Run with --no-headless to debug.")
        return

    # Save raw dump
    RAW_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    RAW_OUTPUT.write_text(
        json.dumps(players[:500], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  Saved first 500 raw records to {RAW_OUTPUT}")

    # Match to local player IDs and save computed output
    matched: list[dict] = []
    unmatched = 0
    seen: set = set()

    for p in players:
        player_id = match_player(p["name"], p.get("team"), player_index)
        if player_id is None:
            unmatched += 1
            continue
        key = (player_id, EA_SEASON)
        if key in seen:
            continue
        seen.add(key)
        matched.append({
            "player_id": player_id,
            "source":    "ea_cfb26",
            "ea_season": EA_SEASON,
            "ovr":       p.get("ovr"),
            "position":  p.get("position"),
            "name":      p.get("name"),
            "team":      p.get("team"),
            "attributes": p.get("attributes") or {},
        })

    write_json(COMPUTED_OUTPUT, matched)
    print(f"  Matched {len(matched)} players ({unmatched} unmatched) → ea_ratings.json")
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Scrape EA CFB 26 ratings via Playwright")
    parser.add_argument("--limit",       type=int, default=None, help="Max players to scrape (testing)")
    parser.add_argument("--no-headless", action="store_true",    help="Show browser window")
    parser.add_argument("--fallback-dom", action="store_true",   help="Skip XHR, go straight to DOM scrape")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
