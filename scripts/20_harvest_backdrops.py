"""Find freely licensed photos for the menu's background slideshow, from Wikimedia Commons.

For each team: its stadium (the team's stadium name from our teams table), and for the home page a set of the
sport's most famous stadiums. Only images with a free licence (public domain, CC0, CC BY, CC BY-SA) and a size
and shape that suit a full-screen background are kept, each with its licence, author and page so the app can
credit it. Images are LINKED from Commons (upload.wikimedia.org thumbnails), never copied into the repo.

Writes cfb-analytics-app/data/backdrops.json:
    { "source": "Wikimedia Commons", "fetched": date,
      "home":  [ {url, title, credit, license, page} ... ],
      "teams": { "<our team id>": [ {url, title, credit, license, page} ... ] } }

What this cannot supply: photos of players in action, uniforms, locker rooms or cities for a given team are
rarely freely licensed, so most of a team's set is its stadium. A team with no qualifying photo has an empty
list and the app uses its colours. Raw responses are cached in data/commons/ so a re-run costs no requests.

Usage:
    python scripts/20_harvest_backdrops.py            # uses the cache
    python scripts/20_harvest_backdrops.py --refresh  # asks Commons again
"""

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw

ROOT = Path(__file__).parent.parent
APP_DATA = ROOT.parent / "cfb-analytics-app" / "data"
CACHE = ROOT / "data" / "commons"
API = "https://commons.wikimedia.org/w/api.php"
UA = "cfb-analytics personal project (colemccall22@gmail.com)"
DELAY = 0.4
PER_TEAM = 5
MIN_WIDTH = 1400
ALLOWED = re.compile(r"^(public domain|cc0|cc[ -]by(-sa)?[ -]\d|cc[ -]by(-sa)?$|pd[ -])", re.I)
FAMOUS = ["Rose Bowl stadium Pasadena", "Michigan Stadium Ann Arbor", "Notre Dame Stadium", "Ohio Stadium Columbus",
          "Tiger Stadium Baton Rouge", "Kyle Field College Station", "Bryant-Denny Stadium", "Sanford Stadium Athens",
          "Autzen Stadium Eugene", "Neyland Stadium Knoxville", "Beaver Stadium State College", "Darrell K Royal Texas Memorial Stadium",
          "Memorial Stadium Lincoln Nebraska", "Orange Bowl Miami", "Cotton Bowl Dallas", "Camp Randall Stadium"]


def query(search: str, refresh: bool) -> list:
    """Commons image hits for a search: [{url, title, w, h, license, author, page}], cached by the query."""
    f = CACHE / (re.sub(r"[^a-z0-9]+", "_", search.lower())[:90] + ".json")
    if f.exists() and not refresh:
        pages = json.loads(f.read_text(encoding="utf-8"))
        fetched = False
    else:
        url = (f"{API}?action=query&format=json&generator=search&gsrsearch={quote(search + ' filetype:bitmap')}&gsrnamespace=6&gsrlimit=15"
               f"&prop=imageinfo&iiprop=url|extmetadata|size&iiurlwidth=1920&iiextmetadatafilter=LicenseShortName|Artist")
        pages = None
        for attempt in range(3):
            try:
                with urlopen(Request(url, headers={"User-Agent": UA}), timeout=30) as r:
                    pages = json.loads(r.read().decode("utf-8")).get("query", {}).get("pages", {})
                break
            except Exception:  # noqa: BLE001 - retry any network or parse failure
                time.sleep(2 * (attempt + 1))
        if pages is None:
            return []
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(pages), encoding="utf-8")
        time.sleep(DELAY)
    out = []
    for p in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        i = (p.get("imageinfo") or [{}])[0]
        m = i.get("extmetadata", {})
        out.append({"url": i.get("thumburl") or i.get("url"), "title": p["title"].replace("File:", ""), "w": i.get("width", 0), "h": i.get("height", 0),
                    "license": (m.get("LicenseShortName", {}).get("value") or ""), "author": re.sub(r"<[^>]+>", "", m.get("Artist", {}).get("value") or "").strip(),
                    "page": f"https://commons.wikimedia.org/wiki/{quote(p['title'].replace(' ', '_'))}"})
    return out


def good(hit: dict, words: list) -> bool:
    """A free licence, a wide enough landscape picture, and a title that names the place (not a logo, map or diagram)."""
    t = hit["title"].lower()
    return (bool(hit["url"]) and ALLOWED.match(hit["license"] or "") is not None and hit["w"] >= MIN_WIDTH and hit["w"] / max(hit["h"], 1) >= 1.3
            and hit["w"] / max(hit["h"], 1) >= 1.45 and hit["w"] >= 1800
            and not re.search(r"logo|map|seal|diagram|flag|chart|plan|campus|\.svg|\.png|tailgat|fans|crowd|parking|band|student|people|cheer|mascot|player|coach|team |huddle|kick|vs\.? |versus|at.*vs", t)
            and "stadium" in t or ("field" in t and any(w in t for w in words)) and ALLOWED.match(hit["license"] or "") is not None
            and any(w in t for w in words))


def credit(hit: dict) -> dict:
    """What the app shows and links for one photo."""
    return {"url": hit["url"], "title": hit["title"], "credit": hit["author"][:80] or "Wikimedia Commons contributor", "license": hit["license"], "page": hit["page"]}


def main() -> None:
    """Search Commons for every team's stadium and the famous stadiums, keep the free-licensed hits and write backdrops.json."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    teams = read_raw("teams")
    out = {"source": "Wikimedia Commons", "fetched": date.today().isoformat(), "home": [], "teams": {}}
    for q in FAMOUS:
        words = [w.lower() for w in q.split()[:2]]
        hits = [h for h in query(q, args.refresh) if good(h, words)][:2]
        out["home"] += [credit(h) for h in hits]
    have = 0
    for r in teams.itertuples():
        stadium = (r.stadium_name or "").strip()
        words = [w.lower() for w in re.sub(r"[^A-Za-z ]", " ", stadium).split() if len(w) > 3][:2] or [str(r.school).lower().split()[0]]
        hits = [h for h in query(f"{stadium} {r.school}" if stadium else f"{r.school} football stadium", args.refresh) if good(h, words)]
        seen, keep = set(), []
        for h in hits:
            if h["url"] not in seen:
                seen.add(h["url"]); keep.append(credit(h))
        out["teams"][str(int(r.id))] = keep[:PER_TEAM]
        have += bool(keep)
        print(f"{r.school:24s} {len(keep[:PER_TEAM])}", flush=True)
    (APP_DATA / "backdrops.json").write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"\nhome photos: {len(out['home'])}; teams with at least one photo: {have} of {len(out['teams'])}; photos: {sum(len(v) for v in out['teams'].values())}")


if __name__ == "__main__":
    main()
