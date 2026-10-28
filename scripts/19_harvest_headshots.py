"""Match our 2026 roster players to ESPN athletes, for the player card's headshot.

The CFBD data carries no photo, so the only source is ESPN's public roster pages. This reads
ESPN's college-football team list (one request) and one roster per matched team (about 138
requests, no key, no CFBD spend), matches each ESPN athlete to our player by NAME WITHIN THE
SAME TEAM, and writes cfb-analytics-app/data/headshots.json:

    { "source": "ESPN", "season": 2026, "image": "<url pattern>",
      "players": { "<our player_id>": "<ESPN athlete id>" } }

Only athletes ESPN lists with a headshot are mapped. The card builds the image URL from the id;
nothing is copied into the repo.

MATCHING IS DELIBERATELY STRICT. A wrong match shows the wrong face, so:
  - the school must agree (we only compare within one team's two rosters);
  - the normalised full name must agree (accents, punctuation and Jr/Sr/II/III dropped);
  - if two of our players share a name on that team, the ESPN position must be compatible with
    exactly one of them, else the athlete is left unmatched;
  - an ESPN athlete that would map onto a player another ESPN athlete already claimed is dropped
    along with the first (a conflict is not guessed at).
An unmatched player simply has no photo; the card shows his monogram.

The report it prints is the evidence for whether photos are worth showing: match rate over our
2026 roster, over the players the app rates, by position group and by team level, and every
team ESPN could not be matched to. Raw ESPN responses are cached in data/espn/ so a re-run
(or a change to the matching) costs no requests; --refresh fetches again.

Usage:
    python scripts/19_harvest_headshots.py            # uses the cache where it has it
    python scripts/19_harvest_headshots.py --refresh  # asks ESPN again
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw

ROOT = Path(__file__).parent.parent
APP_DATA = ROOT.parent / "cfb-analytics-app" / "data"
CACHE = ROOT / "data" / "espn"
BASE = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
IMAGE = "https://a.espncdn.com/i/headshots/college-football/players/full/{id}.png"
SEASON = 2026
DELAY = 0.3                       # seconds between requests: a polite pace for a public endpoint
UA = "Mozilla/5.0 (personal CFB analytics project; contact via repo owner)"

# ESPN position abbreviation -> the position groups of ours it can be.
POS_COMPAT = {
    "QB": {"QB"}, "RB": {"RB"}, "FB": {"RB", "TE"}, "WR": {"WR"}, "TE": {"TE"},
    "OL": {"OL"}, "OT": {"OL"}, "OG": {"OL"}, "C": {"OL"},
    "DL": {"DL", "EDGE"}, "DE": {"DL", "EDGE"}, "DT": {"DL"}, "NT": {"DL"},
    "LB": {"LB", "EDGE"}, "ILB": {"LB"}, "OLB": {"LB", "EDGE"},
    "DB": {"CB", "S", "DB"}, "CB": {"CB", "DB"}, "S": {"S", "DB"}, "SS": {"S", "DB"}, "FS": {"S", "DB"},
    "PK": {"K"}, "K": {"K"}, "P": {"P"}, "LS": set(), "ATH": None,
}
POWER_CONFS = {"SEC", "Big Ten", "Big 12", "ACC"}
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm(name: str) -> str:
    """A name reduced to what two sources agree on: no accents, punctuation or generational suffix."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"['.]", "", s)                       # De'Andre and DeAndre are one name; a hyphen is a space
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(w for w in s.split() if w not in SUFFIX)


def get(url: str, cache_file: Path, refresh: bool):
    """One JSON document, from the cache if present, else from ESPN (and then cached)."""
    if cache_file.exists() and not refresh:
        return json.loads(cache_file.read_text(encoding="utf-8")), False
    last = None
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": UA}), timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data), encoding="utf-8")
            time.sleep(DELAY)
            return data, True
        except Exception as e:  # noqa: BLE001 - report and retry any network or parse failure
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def candidates_for(index: dict, athlete: dict) -> list:
    """Our players an ESPN athlete could be: the same normalised name on the same team, narrowed by
    position only when the name alone is ambiguous. Exactly one is a match; none or several is not.
    """
    cands = index.get(norm(athlete.get("fullName") or athlete.get("displayName")), [])
    if len(cands) > 1:
        compat = POS_COMPAT.get((athlete.get("position") or {}).get("abbreviation"))
        cands = [p for p in cands if compat is None or p.get("position_group") in compat]
    return cands


def espn_team_ids(teams: list, refresh: bool):
    """{our team id: ESPN team id}, matched by school name, plus the schools that did not match."""
    data, fetched = get(f"{BASE}/teams?limit=1000", CACHE / "teams.json", refresh)
    espn = data["sports"][0]["leagues"][0]["teams"]
    by_name = {}
    for t in espn:
        x = t["team"]
        for key in (x.get("location"), x.get("displayName"), x.get("shortDisplayName")):
            if key:
                by_name.setdefault(norm(key), x["id"])
    out, missing = {}, []
    for t in teams:
        school = t["school"]
        hit = by_name.get(norm(school)) or by_name.get(norm(school.replace("State", "St"))) or by_name.get(norm(school.replace("&", "and")))
        if hit:
            out[t["id"]] = hit
        else:
            missing.append(school)
    return out, missing, int(fetched)


def main() -> None:
    """Fetch (or read from the cache) ESPN's team list and rosters, match athletes to our 2026 roster players, print the
    report and write data/headshots.json."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refresh", action="store_true", help="ask ESPN again instead of using the cache")
    args = ap.parse_args()

    rosters = json.loads((APP_DATA / f"rosters_{SEASON}.json").read_text(encoding="utf-8"))
    teams_df = read_raw("teams")
    teams = [{"id": int(r.id), "school": r.school, "conference": r.conference} for r in teams_df.itertuples() if str(int(r.id)) in rosters]
    ids, missing, requests = espn_team_ids(teams, args.refresh)

    mapping: dict = {}
    claimed: dict = {}                 # our player id -> ESPN athlete ids that claimed him
    stats = Counter()
    per_group = defaultdict(lambda: [0, 0])      # position group -> [ours, matched]
    per_level = defaultdict(lambda: [0, 0])
    for t in teams:
        espn_id = ids.get(t["id"])
        ours = rosters[str(t["id"])]
        level = "Power 4" if t["conference"] in POWER_CONFS else "Group of 6 / Ind."
        for p in ours:
            per_group[p.get("position_group") or "?"][0] += 1
            per_level[level][0] += 1
        if not espn_id:
            continue
        data, fetched = get(f"{BASE}/teams/{espn_id}/roster", CACHE / f"roster_{espn_id}.json", args.refresh)
        requests += int(fetched)
        index = defaultdict(list)
        for p in ours:
            index[norm(p.get("name"))].append(p)
        for grp in data.get("athletes", []):
            for a in grp.get("items", []):
                stats["espn_athletes"] += 1
                if not (a.get("headshot") or {}).get("href"):
                    stats["espn_no_headshot"] += 1
                    continue
                cands = candidates_for(index, a)
                if len(cands) != 1:
                    stats["unmatched_ambiguous" if len(cands) > 1 else "unmatched_no_name"] += 1
                    continue
                pid = cands[0]["player_id"]
                claimed.setdefault(pid, []).append((str(a["id"]), t["id"], cands[0].get("position_group"), level))
    # A player two athletes claimed is dropped, as is an athlete id claimed by two players.
    by_athlete = Counter(c[0] for cs in claimed.values() for c in cs)
    for pid, cs in claimed.items():
        if len(cs) != 1 or by_athlete[cs[0][0]] != 1:
            stats["dropped_conflict"] += 1
            continue
        mapping[str(pid)] = cs[0][0]
        per_group[cs[0][2] or "?"][1] += 1
        per_level[cs[0][3]][1] += 1

    total = sum(len(v) for v in rosters.values())
    rated = [p for v in rosters.values() for p in v if p.get("live_rating") is not None or p.get("overall_rating") is not None or p.get("rating_prev") is not None]
    rated_hit = sum(1 for p in rated if str(p["player_id"]) in mapping)
    print(f"ESPN requests made this run: {requests}  (cached files are free)")
    print(f"Teams: {len(teams)} ours, {len(ids)} matched to ESPN, unmatched: {missing}")
    print(f"ESPN athletes {stats['espn_athletes']}, without headshot {stats['espn_no_headshot']}")
    print(f"Not matched: no name on that team {stats['unmatched_no_name']}, ambiguous {stats['unmatched_ambiguous']}, conflicts dropped {stats['dropped_conflict']}")
    print(f"MATCHED: {len(mapping)} of our {total} {SEASON} roster players ({100 * len(mapping) / total:.1f}%)")
    print(f"  of the {len(rated)} players with any rating: {rated_hit} ({100 * rated_hit / len(rated):.1f}%)")
    for k, (n, m) in sorted(per_level.items()):
        print(f"  {k:20} {m:5d} / {n:5d}  {100 * m / n:5.1f}%")
    for k, (n, m) in sorted(per_group.items(), key=lambda kv: -kv[1][0]):
        print(f"  {k:6} {m:5d} / {n:5d}  {100 * m / max(n, 1):5.1f}%")

    out = {"source": "ESPN", "season": SEASON, "fetched": date.today().isoformat(), "image": IMAGE, "players": mapping}
    (APP_DATA / "headshots.json").write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {APP_DATA / 'headshots.json'} ({(APP_DATA / 'headshots.json').stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
