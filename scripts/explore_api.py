"""Survey the CFB Data API and write down what it actually has.

Read-only. Touches nothing under data/raw. Its only output is documentation:
docs/API_INVENTORY.md and data/computed/api_inventory.json.

Why this exists: an opportunistic twenty-minute probe overturned two conclusions
this project had recorded as blocked — the coaching event study (there is a
/coaches endpoint carrying full tenure) and the offensive-line unit rating
(/stats/season/advanced carries lineYards, stuffRate and powerSuccess). We use
13 of 74 endpoints. Guessing at the other 61 is how "the API doesn't have that"
becomes folklore, so this asks once and writes the answer down.

It also records the *absences* with their evidence, which are just as expensive
to rediscover: there is no blocking data, no per-play tackle attribution, no QB
hurry attribution, and no NIL anywhere.

Three phases, each independently runnable:

  schema    one probe per endpoint -> status, record count, full key schema
  coverage  priority endpoints across four seasons -> where history begins
  join      do the ids in a response actually match ours, and at what rate

Every response goes through the same .cache/ the pipeline uses, so a second run
costs zero requests. /info/usage is read before and after so the cost of the
survey is measured rather than assumed.

Usage:
    python scripts/explore_api.py                 # all phases
    python scripts/explore_api.py --phase schema
    python scripts/explore_api.py --offline       # cache only, no new requests
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.api_client import BASE_URL, CACHE_DIR, _cache_key, _headers, load_api_key, VERIFY_TLS
from utils.store import COMPUTED_DIR, read_raw

DOCS_DIR = Path(__file__).parent.parent / "docs"
APP_DATA = Path(__file__).parent.parent.parent / "cfb-analytics-app" / "data"
SPEC_PATH = "/api-docs.json"

# What the pipeline actually calls today, from utils/api_client.py. The gap
# between this and the 74 available endpoints is the point of the whole survey.
IN_PIPELINE = {
    "/teams/fbs", "/roster", "/stats/player/season", "/ppa/players/season",
    "/stats/season", "/ratings/sp", "/talent", "/recruiting/players",
    "/player/usage", "/awards", "/games/teams", "/games", "/player/portal",
    "/drives", "/plays",
}

# Back-to-back probing trips the limiter where a normal harvest does not.
RATE_LIMIT_RETRIES = 5
RATE_LIMIT_BASE_WAIT = 8      # 8, 16, 32, 64, 128s
PROBE_SPACING = 1.0           # polite gap between fresh requests

# Our own count of live requests. /info/usage is the authority on the rolling
# window, but it is itself rate-limited and returned nothing on one run — so the
# cost of the survey is counted here too, where it cannot fail.
_LIVE_REQUESTS = 0

# Fixture values for required parameters. Every one of these is a real object
# verified to exist, so a 404 means "no data for this shape", not "bad fixture".
FIXTURE = {
    "year": 2024,
    "week": 5,
    "team": "Boise State",
    "team1": "Boise State",
    "team2": "Fresno State",
    "gameId": 401628469,      # Boise State at Oregon, 2024 week 2
    "id": 401628469,
    "coachId": 1781,          # Spencer Danielson
    "playerId": 4890973,      # Ashton Jeanty
    "athleteId": 4890973,
    "searchTerm": "Jeanty",
    "down": 3,
    "distance": 8,
    "conference": "MWC",
    "seasonType": "regular",
}

# Endpoints that are pointless or hostile to survey: live feeds, and the full
# play-by-play dump, which is enormous and whose shape /plays/stats already tells
# us. Skipping them is a decision, not an oversight, so it is recorded.
SKIP = {
    "/live/plays": "live in-game feed; nothing to inventory",
    "/scoreboard": "live scoreboard; nothing to inventory",
    "/plays": "full play-by-play; very large, and /plays/stats covers attribution",
}

# The endpoints worth knowing the history of. Everything else can be probed for
# shape alone.
PRIORITY = [
    "/stats/season/advanced", "/stats/game/advanced", "/stats/game/havoc",
    "/stats/player/success", "/stats/player/season",
    "/coaches", "/draft/picks", "/player/returning", "/player/usage",
    "/plays/stats", "/ppa/players/season", "/wepa/players/rushing",
    "/ratings/sp", "/ratings/srs", "/ratings/elo", "/ratings/fpi", "/ratings/core",
    "/metrics/wp/pregame", "/lines", "/playoffs/cfp/participants",
    "/games/weather", "/venues", "/talent", "/recruiting/players",
]
COVERAGE_YEARS = [2010, 2016, 2021, 2024]

# Ids we could join a foreign row to one of ours on.
JOIN_HINTS = ["athleteId", "collegeAthleteId", "playerId", "player_id",
              "teamId", "team_id", "school", "team", "id", "name"]


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def probe(api_key: str, path: str, params: dict, offline: bool = False) -> dict:
    """One request, cached, recording the status rather than raising on it.

    The pipeline's _get() calls raise_for_status(), which is right for a harvest
    and wrong for a survey — a 404 here is a finding, not a failure.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = _cache_key(path, params)
    cache_file = os.path.join(CACHE_DIR, f"{key}.json")
    # Negative results are cached separately, under a name the pipeline's own
    # cache lookup will never collide with. Without this, every 400 is re-issued
    # on every run and "re-running is free" is simply untrue.
    miss_file = os.path.join(CACHE_DIR, f"probe-miss-{key}.json")

    if os.path.exists(cache_file):
        with open(cache_file, encoding="utf-8") as f:
            try:
                return {"status": 200, "cached": True, "body": json.load(f)}
            except Exception:
                pass
    if os.path.exists(miss_file):
        with open(miss_file, encoding="utf-8") as f:
            try:
                rec = json.load(f)
                return {"status": rec.get("status"), "cached": True,
                        "body": None, "note": rec.get("note", "")}
            except Exception:
                pass
    if offline:
        return {"status": None, "cached": False, "body": None, "note": "not cached, offline"}

    # A survey walks 74 endpoints back to back, which trips the rate limiter far
    # sooner than a harvest does. Without this the run "succeeds" and records 40
    # endpoints as 429 — which reads exactly like "the endpoint is broken" and is
    # the most misleading thing an inventory could possibly say.
    global _LIVE_REQUESTS
    r = None
    for attempt in range(RATE_LIMIT_RETRIES):
        _LIVE_REQUESTS += 1
        try:
            r = requests.get(f"{BASE_URL}{path}", headers=_headers(api_key),
                             params=params, timeout=45, verify=VERIFY_TLS)
        except Exception as e:
            return {"status": None, "cached": False, "body": None,
                    "note": f"{type(e).__name__}: {e}"}
        if r.status_code != 429:
            break
        wait = RATE_LIMIT_BASE_WAIT * (2 ** attempt)
        print(f"    rate limited on {path}; waiting {wait}s "
              f"({attempt + 1}/{RATE_LIMIT_RETRIES})")
        time.sleep(wait)

    if r is None or r.status_code != 200:
        status = None if r is None else r.status_code
        note = "rate limited after retries" if status == 429 else ""
        # A 429 is a transient condition, not a fact about the endpoint — never
        # cache it, or one bad run poisons the inventory permanently.
        if status is not None and status != 429:
            with open(miss_file, "w", encoding="utf-8") as f:
                json.dump({"status": status, "note": note}, f)
        return {"status": status, "cached": False, "body": None, "note": note}
    try:
        body = r.json()
    except Exception:
        return {"status": 200, "cached": False, "body": None, "note": "non-JSON response"}
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(body, f)
    time.sleep(PROBE_SPACING)   # only after a live request; cached reads are free
    return {"status": 200, "cached": False, "body": body}


def schema_of(obj, prefix: str = "", depth: int = 0) -> dict:
    """Flatten one record into {dotted.key: type}. Nested objects matter here —
    the whole point of /stats/season/advanced is what is inside `offense`."""
    out: dict = {}
    if depth > 3 or not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out[key] = "object"
            out.update(schema_of(v, f"{key}.", depth + 1))
        elif isinstance(v, list):
            out[key] = "array"
            if v and isinstance(v[0], dict):
                out.update(schema_of(v[0], f"{key}[].", depth + 1))
        else:
            out[key] = type(v).__name__
    return out


def summarize(body) -> dict:
    if body is None:
        return {"count": 0, "schema": {}, "sample": None}
    if isinstance(body, dict):
        return {"count": 1, "schema": schema_of(body), "sample": body}
    if isinstance(body, list):
        return {
            "count": len(body),
            "schema": schema_of(body[0]) if body and isinstance(body[0], dict) else {},
            "sample": body[0] if body else None,
        }
    return {"count": 0, "schema": {}, "sample": None}


def usage(api_key: str) -> int | None:
    """Rolling 7-day request count, or None if it could not be read.

    Retries a 429 — this is called immediately before and after a burst of
    probing, which is exactly when the limiter is most likely to refuse it, and
    silently returning None loses the only measurement of what the survey cost.
    """
    for attempt in range(3):
        try:
            r = requests.get(f"{BASE_URL}/info/usage", headers=_headers(api_key),
                             timeout=30, verify=VERIFY_TLS)
        except Exception:
            return None
        if r.status_code == 429:
            time.sleep(RATE_LIMIT_BASE_WAIT * (2 ** attempt))
            continue
        if r.status_code != 200:
            return None
        try:
            return int(r.json()["totals"]["requests"])
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def load_spec(api_key: str, offline: bool) -> dict:
    res = probe(api_key, SPEC_PATH, {}, offline)
    if res.get("status") != 200 or not res.get("body"):
        print("  could not load the OpenAPI spec; cannot enumerate endpoints")
        return {}
    return (res["body"] or {}).get("paths", {}) or {}


def phase_schema(api_key: str, paths: dict, offline: bool) -> list[dict]:
    print(f"\n=== schema: {len(paths)} endpoints ===")
    rows = []
    for path in sorted(paths):
        get = (paths[path] or {}).get("get") or {}
        if path in SKIP:
            rows.append({"path": path, "status": "skipped", "note": SKIP[path],
                         "count": 0, "schema": {}, "params": []})
            continue
        params_spec = get.get("parameters", []) or []
        required = [p["name"] for p in params_spec if p.get("required")]
        missing = [n for n in required if n not in FIXTURE]
        if missing:
            rows.append({"path": path, "status": "no fixture", "count": 0, "schema": {},
                         "note": f"needs {missing}", "params": required})
            continue

        params = {n: FIXTURE[n] for n in required}
        # Bound the probe to one season where the endpoint supports it. Do NOT
        # also narrow by team: /draft/picks filters on the player's *college*, and
        # Boise State had no 2024 picks, so adding team=Boise State made the
        # inventory report "0 records" for an endpoint that returns ~255 a year.
        # A survey that under-reports availability is worse than no survey.
        if "year" not in params and any(p.get("name") == "year" for p in params_spec):
            params["year"] = FIXTURE["year"]

        res = probe(api_key, path, params, offline)

        # Some endpoints need a narrowing parameter the spec does not mark
        # required — /games/teams and /ppa/players/games reject year alone. Retry
        # with team, then with week, so the inventory records what the endpoint
        # *has* rather than a 400 that reads as "broken".
        if res.get("status") == 400:
            for extra in ("team", "week"):
                if extra in params or not any(p.get("name") == extra for p in params_spec):
                    continue
                retry = dict(params, **{extra: FIXTURE[extra]})
                res = probe(api_key, path, retry, offline)
                if res.get("status") == 200:
                    params = retry
                    break

        s = summarize(res.get("body"))
        rows.append({
            "path": path,
            "status": res.get("status"),
            "cached": res.get("cached"),
            "count": s["count"],
            "capped": s["count"] in (1000, 2000, 5000),
            "schema": s["schema"],
            "sample": s["sample"],
            "params": sorted(params),
            "note": res.get("note", ""),
            "join_keys": sorted({k for k in s["schema"] if k.split(".")[-1] in JOIN_HINTS}),
        })
        flag = "" if res.get("status") == 200 else f"  [{res.get('status')}]"
        cap = "  CAPPED" if rows[-1]["capped"] else ""
        print(f"  {path:42} n={s['count']:<6}{flag}{cap}")
    return rows


def phase_coverage(api_key: str, offline: bool) -> dict:
    print(f"\n=== coverage: {len(PRIORITY)} endpoints x {len(COVERAGE_YEARS)} seasons ===")
    out: dict = {}
    for path in PRIORITY:
        per_year = {}
        for y in COVERAGE_YEARS:
            params = {"year": y}
            if path in ("/plays/stats", "/stats/game/advanced", "/stats/game/havoc",
                        "/metrics/wp/pregame", "/lines"):
                params["team"] = FIXTURE["team"]
            res = probe(api_key, path, params, offline)
            per_year[y] = summarize(res.get("body"))["count"] if res.get("status") == 200 else None
        out[path] = per_year
        print(f"  {path:34} " + "  ".join(
            f"{y}:{'-' if per_year[y] is None else per_year[y]}" for y in COVERAGE_YEARS))
    return out


def phase_join(api_key: str, offline: bool) -> dict:
    """Do foreign ids actually match ours, and at what rate?

    An endpoint we cannot join is not usable however good it looks, and
    "plausibly the same id" has burned this project before.
    """
    print("\n=== join-key match rates ===")
    players = read_raw("players")
    ps = read_raw("player_seasons")
    ours = set()
    for col in ("cfb_api_id", "id"):
        if col in players.columns:
            ours |= {int(v) for v in players[col].dropna().tolist()}
    teams = read_raw("teams")
    our_schools = {str(s).lower() for s in teams["school"].dropna()} if not teams.empty else set()
    out = {}

    checks = [
        ("/draft/picks", {"year": 2024}, "collegeAthleteId", ours, "our player ids"),
        ("/stats/player/success", {"year": 2024, "team": FIXTURE["team"]}, "id", ours, "our player ids"),
        ("/plays/stats", {"year": 2024, "team": FIXTURE["team"]}, "athleteId", ours, "our player ids"),
        ("/coaches", {"year": 2024}, None, our_schools, "our school names"),
    ]
    for path, params, key, universe, label in checks:
        res = probe(api_key, path, params, offline)
        body = res.get("body")
        if res.get("status") != 200 or not isinstance(body, list) or not body:
            out[path] = {"matched": None, "n": 0, "note": f"status {res.get('status')}"}
            print(f"  {path:30} no data")
            continue
        if key:
            vals = [r.get(key) for r in body if r.get(key) is not None]
            hit = sum(1 for v in vals if _as_int(v) in universe)
        else:  # coaches: match on school inside seasons[]
            vals, hit = [], 0
            for r in body:
                for s in (r.get("seasons") or []):
                    school = str(s.get("school", "")).lower()
                    vals.append(school)
                    hit += school in universe
        rate = hit / len(vals) if vals else None
        out[path] = {"key": key or "seasons[].school", "n": len(vals),
                     "matched": hit, "rate": rate, "against": label}
        print(f"  {path:30} {hit}/{len(vals)}"
              f"{'' if rate is None else f'  ({rate:.1%})'}  vs {label}")
    return out


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

# Absences verified by direct probe. Recorded with their evidence because the
# cost of rediscovering them is a day each.
VERIFIED_ABSENT = [
    ("Per-lineman blocking data (pancakes, sacks allowed, pressures allowed)",
     "Full key scan of every stat category in the feed: passing, rushing, receiving, "
     "defensive, fumbles, interceptions, kicking, punting, kickReturns, puntReturns. "
     "No blocking category exists."),
    ("Per-play tackle attribution",
     "/plays/stats returns 0 records of statType 'Tackle' in 2014, 2019 and 2024 samples, "
     "though the type is defined in /plays/stats/types."),
    ("Per-play QB hurry attribution",
     "Same probe: 0 records of statType 'QB Hurry'."),
    ("Defender-side target data (targets allowed by a corner)",
     "statType 'Target' is attributed to the RECEIVER and appears only on incompletions "
     "(80 of 80 Target plays in a 2024 sample also carry an Incompletion, 0 carry a Reception)."),
    ("Missed tackles", "No such field in any endpoint."),
    ("NIL valuations or compensation", "No endpoint in the 74-path spec exposes NIL."),
]


def write_outputs(spec_rows, coverage, joins, usage_delta):
    COMPUTED_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "endpoints": [{k: v for k, v in r.items() if k != "sample"} for r in spec_rows],
        "coverage": coverage,
        "joins": joins,
        "verified_absent": [{"what": w, "evidence": e} for w, e in VERIFIED_ABSENT],
        "requests_used_by_survey": usage_delta,
    }
    out_json = COMPUTED_DIR / "api_inventory.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"\n  Wrote {out_json} ({len(spec_rows)} endpoints)")

    lines = [
        "# API inventory",
        "",
        "*Generated by `scripts/explore_api.py`. Read-only survey — regenerate with*",
        "*`python scripts/explore_api.py`; a second run is served entirely from `.cache/`.*",
        "",
        "## What the API does not have",
        "",
        "Recorded with evidence, because rediscovering an absence costs as much as finding a",
        "feature and leaves nothing behind.",
        "",
        "| Not available | How we know |",
        "|---|---|",
    ]
    lines += [f"| {w} | {e} |" for w, e in VERIFIED_ABSENT]
    lines += ["", "## Endpoints", "",
              "| Endpoint | Status | Records | Join keys | Notes |", "|---|---|---:|---|---|"]
    for r in sorted(spec_rows, key=lambda x: x["path"]):
        jk = ", ".join(f"`{k}`" for k in (r.get("join_keys") or [])[:4]) or "—"
        note = r.get("note") or ("CAPPED — needs slicing" if r.get("capped") else "")
        lines.append(f"| `{r['path']}` | {r.get('status')} | {r.get('count')} | {jk} | {note} |")

    if coverage:
        lines += ["", "## Season coverage", "",
                  "Where each priority endpoint's history actually begins. This is the question",
                  "that has bitten twice — usage data starts in 2013, hurries in 2015.", "",
                  "| Endpoint | " + " | ".join(str(y) for y in COVERAGE_YEARS) + " |",
                  "|---" * (len(COVERAGE_YEARS) + 1) + "|"]
        for path, per in coverage.items():
            cells = " | ".join("—" if per[y] is None else str(per[y]) for y in COVERAGE_YEARS)
            lines.append(f"| `{path}` | {cells} |")

    if joins:
        lines += ["", "## Join-key match rates", "",
                  "An endpoint we cannot join to our players is not usable, however good it looks.",
                  "", "| Endpoint | Key | Matched | Rate |", "|---|---|---|---|"]
        for path, j in joins.items():
            rate = "—" if j.get("rate") is None else f"{j['rate']:.1%}"
            lines.append(f"| `{path}` | `{j.get('key','—')}` | "
                         f"{j.get('matched')}/{j.get('n')} | {rate} |")

    lines += ["", "## Full field schemas", ""]
    for r in sorted(spec_rows, key=lambda x: x["path"]):
        if not r.get("schema"):
            continue
        lines += [f"### `{r['path']}`", "", "```text"]
        lines += [f"{k:44} {v}" for k, v in sorted(r["schema"].items())]
        lines += ["```", ""]

    out_md = DOCS_DIR / "API_INVENTORY.md"
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  Wrote {out_md}")

    # A slim copy for methods.html, so the data-availability table on the site is
    # rendered from the survey rather than hand-copied out of it and left to rot.
    # Scripts 13-15 already write straight into the app's data/ — same pattern.
    slim = {
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(timespec="seconds"),
        "endpoints": sorted(
            ({"path": e["path"], "records": e.get("count"),
              "capped": bool(e.get("capped")),
              "join_keys": (e.get("join_keys") or [])[:3],
              "in_pipeline": e["path"] in IN_PIPELINE}
             for e in spec_rows if e.get("status") == 200),
            key=lambda e: e["path"]),
        "coverage": coverage,
        "joins": joins,
        "verified_absent": [{"what": w, "evidence": e} for w, e in VERIFIED_ABSENT],
    }
    if APP_DATA.exists():
        out_app = APP_DATA / "api_availability.json"
        with open(out_app, "w", encoding="utf-8") as f:
            json.dump(slim, f, separators=(",", ":"))
        print(f"  Wrote {out_app}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Survey the CFB Data API (read-only)")
    ap.add_argument("--phase", choices=["all", "schema", "coverage", "join"], default="all")
    ap.add_argument("--offline", action="store_true", help="cache only; issue no requests")
    args = ap.parse_args()

    api_key = load_api_key()
    before = None if args.offline else usage(api_key)

    paths = load_spec(api_key, args.offline)
    spec_rows = phase_schema(api_key, paths, args.offline) if args.phase in ("all", "schema") else []
    coverage = phase_coverage(api_key, args.offline) if args.phase in ("all", "coverage") else {}
    joins = phase_join(api_key, args.offline) if args.phase in ("all", "join") else {}

    after = None if args.offline else usage(api_key)
    delta = (after - before) if (before is not None and after is not None) else None
    print(f"\n  Live requests issued by this survey: {_LIVE_REQUESTS} "
          f"(the rest were served from .cache/)")
    if delta is not None:
        print(f"  Rolling 7-day total: {before} -> {after}  (+{delta})")
    else:
        print("  /info/usage did not answer, so the rolling total is unknown "
              "for this run — it is rate-limited like everything else.")

    if spec_rows:
        write_outputs(spec_rows, coverage, joins, delta)
    print("\nDone.")


if __name__ == "__main__":
    main()
