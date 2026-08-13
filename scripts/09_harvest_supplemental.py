"""Harvest every supplemental endpoint the API survey found and we were not using.

`scripts/explore_api.py` found 74 endpoints answering and 13 in use. Three of the
unused ones overturned conclusions this project had written down as blocked:
`/coaches` (the coaching event study), `/draft/picks` (external validation and
survivorship), and `/stats/season/advanced` (a real offensive-line measurement).
The rest were not blocked either — nobody had asked.

So this harvests all of them, whether or not anything consumes them yet. The
cost of holding data we have not used is a few MB on disk; the cost of NOT
holding it is that every future question starts with a week of "can we even get
that", which is exactly the loop the survey was written to break. Everything
lands in `data/raw/` in the same shape as the rest of the pipeline, so a later
script can just `read_raw("draft_picks")`.

What each dataset is FOR, so this does not become a junk drawer:

  coaches                who was in charge, with per-season SP+ — event study (script 13)
  draft_picks            did the players we rate highly get drafted — external validation
  team_advanced_season   line yards / stuff rate / havoc — OL unit rating, defensive denominator
  team_advanced_games    the same per game — per-game opponent adjustment
  team_havoc_games       per-game havoc, front seven vs DB
  returning_production   returning PPA and usage share — projection and playoff features
  team_ratings_external  Elo / FPI / SRS / core — independent checks on our team rating
  betting_lines          the market baseline any predictive model has to beat
  pregame_wp             a ready-made win-probability benchmark
  cfp_participants       structured playoff ground truth for a backtest
  player_success         per-player play COUNTS — honest denominators, offence only
  player_wepa            opponent-adjusted EPA — a benchmark, never an input
  team_talent            composite talent per team-season
  team_records           home/away/neutral/conference splits
  team_ats               against-the-spread record and cover margin
  game_weather           per-game conditions, with a dome flag
  venues                 capacity, dome, elevation, lat/long (season-agnostic)
  play_stats             per-play per-athlete events — OPT-IN, see the cap note

`play_stats` is excluded from `--all` on purpose. The endpoint caps every
response at 2,000 records, so it has to be sliced by team, which is ~130 calls
per season and ~2,450 for the archive. Ask for it explicitly when a model needs
it; do not let it ride along with a routine refresh.

Usage:
    python scripts/09_harvest_supplemental.py --list
    python scripts/09_harvest_supplemental.py --all
    python scripts/09_harvest_supplemental.py --dataset coaches draft_picks
    python scripts/09_harvest_supplemental.py --all --seasons 2024 2025
    python scripts/09_harvest_supplemental.py --dataset play_stats --seasons 2025
    python scripts/09_harvest_supplemental.py --all --dry-run     # plan only, no requests
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from utils import api_client as api
from utils.api_client import load_api_key
from utils.json_utils import flatten_keys
from utils.store import read_raw, write_raw

FIRST_SEASON = 2008
LAST_SEASON = 2026

# Politeness between live requests. Cache hits do not sleep — a full re-harvest
# off cache would otherwise take an hour for no reason.
REQUEST_SPACING = 0.35


# ---------------------------------------------------------------------------
# Team-name → our team_id
#
# Almost every supplemental endpoint identifies a team by school name and not by
# id. Resolving it here, once, is what makes these tables joinable to the rest of
# the pipeline; an unresolvable name is left as null rather than guessed at,
# because a wrong team_id is far worse than a missing one.
# ---------------------------------------------------------------------------

_TEAM_IDS: dict[str, int] | None = None


def team_id_map() -> dict[str, int]:
    """{lowercased school: team_id} from data/raw/teams.json. Built once."""
    global _TEAM_IDS
    if _TEAM_IDS is None:
        df = read_raw("teams")
        _TEAM_IDS = {}
        if not df.empty and "school" in df.columns:
            for _, r in df.iterrows():
                school = str(r.get("school") or "").strip().lower()
                tid = r.get("id")
                if school and tid is not None:
                    try:
                        _TEAM_IDS[school] = int(tid)
                    except (TypeError, ValueError):
                        pass
    return _TEAM_IDS


def resolve_team(name) -> int | None:
    if not name:
        return None
    return team_id_map().get(str(name).strip().lower())


def _with_team(row: dict, season: int, name_key: str = "team") -> dict:
    """Stamp season and our team_id onto a flattened row."""
    row["season"] = season
    row["team_id"] = resolve_team(row.get(name_key))
    return row


# ---------------------------------------------------------------------------
# Normalizers
#
# Each returns a flat list of dicts carrying `season` and, where the row is about
# a team, `team_id`. Flat because three levels of nesting is not a DataFrame, and
# every consumer downstream reads DataFrames.
# ---------------------------------------------------------------------------

def norm_passthrough(rows: list, season: int) -> list[dict]:
    return [_with_team(flatten_keys(r), season) for r in rows if isinstance(r, dict)]


def norm_coaches(rows: list, season: int) -> list[dict]:
    """One row per coach-season, not per coach.

    The endpoint returns a coach with a nested `seasons` array covering his whole
    career, so asking for 2024 hands back 2011 rows too. Exploding to coach-season
    and filtering to the requested year is what makes the per-season replace in
    `write_raw` correct — without it, re-harvesting one season would delete every
    other season's rows for that coach.
    """
    out = []
    for c in rows:
        if not isinstance(c, dict):
            continue
        first = (c.get("firstName") or "").strip()
        last = (c.get("lastName") or "").strip()
        for s in c.get("seasons") or []:
            if not isinstance(s, dict) or int(s.get("year") or 0) != season:
                continue
            out.append({
                "coach_id": c.get("id"),
                "coach_name": f"{first} {last}".strip(),
                "first_name": first,
                "last_name": last,
                "hire_date": c.get("hireDate"),
                "season": season,
                "school": s.get("school"),
                "team_id": resolve_team(s.get("school")),
                "conference": s.get("conference"),
                "games": s.get("games"),
                "wins": s.get("wins"),
                "losses": s.get("losses"),
                "ties": s.get("ties"),
                "win_pct": s.get("winPercentage"),
                "preseason_rank": s.get("preseasonRank"),
                "postseason_rank": s.get("postseasonRank"),
                "srs": s.get("srs"),
                "sp_overall": s.get("spOverall"),
                "sp_offense": s.get("spOffense"),
                "sp_defense": s.get("spDefense"),
            })
    return out


def norm_draft(rows: list, season: int) -> list[dict]:
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        flat = flatten_keys(r)
        flat["season"] = season
        flat["draft_year"] = r.get("year")
        # collegeAthleteId is the CFBD athlete id — the same id space as our
        # players.cfb_api_id. Measured at 94.5% match; the misses are players who
        # never appear in an FBS box score.
        flat["college_athlete_id"] = r.get("collegeAthleteId")
        flat["team_id"] = resolve_team(r.get("collegeTeam"))
        out.append(flat)
    return out


def norm_advanced(rows: list, season: int) -> list[dict]:
    """Advanced team stats — flattened, with the line metrics promoted to short names.

    The promoted keys are the ones the OL unit rating and the defensive
    denominator read. They are aliases, not replacements: the full flattened
    payload stays, so nothing is lost for a later question.
    """
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        flat = _with_team(flatten_keys(r), int(r.get("season") or season))
        o, d = r.get("offense") or {}, r.get("defense") or {}
        flat.update({
            "off_line_yards": o.get("lineYards"),
            "off_stuff_rate": o.get("stuffRate"),
            "off_power_success": o.get("powerSuccess"),
            "off_second_level_yards": o.get("secondLevelYards"),
            "off_open_field_yards": o.get("openFieldYards"),
            "off_plays": o.get("plays"),
            "off_success_rate": o.get("successRate"),
            "off_explosiveness": o.get("explosiveness"),
            "off_ppa": o.get("ppa"),
            "def_line_yards": d.get("lineYards"),
            "def_stuff_rate": d.get("stuffRate"),
            "def_power_success": d.get("powerSuccess"),
            "def_plays": d.get("plays"),
            "def_drives": d.get("drives"),
            "def_success_rate": d.get("successRate"),
            "def_explosiveness": d.get("explosiveness"),
            "def_ppa": d.get("ppa"),
            "def_havoc_total": (d.get("havoc") or {}).get("total"),
            "def_havoc_front_seven": (d.get("havoc") or {}).get("frontSeven"),
            "def_havoc_db": (d.get("havoc") or {}).get("db"),
        })
        out.append(flat)
    return out


def norm_havoc(rows: list, season: int) -> list[dict]:
    """Per-game havoc. Different shape from the advanced endpoints — rates, not yards."""
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        flat = _with_team(flatten_keys(r), int(r.get("season") or season))
        d = r.get("defense") or {}
        flat.update({
            "game_cfb_id": r.get("gameId"),
            "def_havoc_rate": d.get("havocRate"),
            "def_havoc_front_seven_rate": d.get("frontSevenHavocRate"),
            "def_havoc_db_rate": d.get("dbHavocRate"),
            "def_havoc_events": d.get("totalHavocEvents"),
            "def_total_plays": d.get("totalPlays"),
        })
        out.append(flat)
    return out


def norm_lines(rows: list, season: int) -> list[dict]:
    """One row per game per book. The consensus is left to the reader.

    Averaging the providers here would throw away the disagreement between them,
    which is the only handle on how confident the market was.
    """
    out = []
    for g in rows:
        if not isinstance(g, dict):
            continue
        base = {
            "season": int(g.get("season") or season),
            "game_cfb_id": g.get("id"),
            "week": g.get("week"),
            "season_type": g.get("seasonType"),
            "home_team": g.get("homeTeam"),
            "home_team_id": resolve_team(g.get("homeTeam")),
            "home_score": g.get("homeScore"),
            "away_team": g.get("awayTeam"),
            "away_team_id": resolve_team(g.get("awayTeam")),
            "away_score": g.get("awayScore"),
        }
        for ln in g.get("lines") or []:
            if not isinstance(ln, dict):
                continue
            out.append({**base,
                        "provider": ln.get("provider"),
                        "spread": ln.get("spread"),
                        "spread_open": ln.get("spreadOpen"),
                        "over_under": ln.get("overUnder"),
                        "home_moneyline": ln.get("homeMoneyline"),
                        "away_moneyline": ln.get("awayMoneyline")})
        if not (g.get("lines") or []):
            out.append({**base, "provider": None, "spread": None, "spread_open": None,
                        "over_under": None, "home_moneyline": None, "away_moneyline": None})
    return out


def norm_cfp(rows: list, season: int) -> list[dict]:
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = r.get("team") or {}
        out.append({
            "season": season,
            "school": t.get("school"),
            "team_id": resolve_team(t.get("school")),
            "conference": t.get("conference"),
            "seed": r.get("seed"),
            "committee_rank": r.get("committeeRank"),
            "bid_type": r.get("bidType"),
            "qualification_reason": r.get("qualificationReason"),
            "conference_champion": r.get("conferenceChampion"),
            "first_round_bye": r.get("firstRoundBye"),
            "outcome": r.get("outcome"),
            "eliminated_round": r.get("eliminatedRound"),
        })
    return out


def norm_player_success(rows: list, season: int) -> list[dict]:
    """Per-player play counts. `id` here is the CFBD athlete id, as a string."""
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        flat = _with_team(flatten_keys(r), int(r.get("season") or season))
        flat["athlete_id"] = r.get("id")
        out.append(flat)
    return out


def norm_wepa(rows: list, season: int, kind: str) -> list[dict]:
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        flat = _with_team(flatten_keys(r), int(r.get("year") or season))
        flat["athlete_id"] = r.get("athleteId")
        flat["wepa_kind"] = kind
        out.append(flat)
    return out


def norm_ratings_external(rows: list, season: int, source: str) -> list[dict]:
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        flat = _with_team(flatten_keys(r), int(r.get("year") or season))
        flat["source"] = source
        out.append(flat)
    return out


def norm_games_by_id(rows: list, season: int, id_key: str = "id") -> list[dict]:
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        flat = flatten_keys(r)
        flat["season"] = int(r.get("season") or season)
        flat["game_cfb_id"] = r.get(id_key)
        flat["home_team_id"] = resolve_team(r.get("homeTeam"))
        flat["away_team_id"] = resolve_team(r.get("awayTeam"))
        out.append(flat)
    return out


# ---------------------------------------------------------------------------
# The registry
#
# `first` is the season the survey found data, not the season the endpoint was
# introduced. Asking below it is a wasted request that returns [] and then gets
# cached as an empty answer, which is indistinguishable from a real absence later.
# ---------------------------------------------------------------------------

DATASETS: dict[str, dict] = {
    "coaches": {
        "endpoint": "/coaches", "first": 2008, "fetch": api.fetch_coaches,
        "norm": norm_coaches,
        "why": "coach tenure with per-season SP+ — the coaching-change event study",
    },
    "draft_picks": {
        "endpoint": "/draft/picks", "first": 2008, "fetch": api.fetch_draft_picks,
        "norm": norm_draft,
        "why": "external validation of our ratings; NFL-departure modelling",
    },
    "team_advanced_season": {
        "endpoint": "/stats/season/advanced", "first": 2008,
        "fetch": api.fetch_advanced_season_stats, "norm": norm_advanced,
        "why": "line yards / stuff rate / havoc — OL unit rating, defensive denominator",
    },
    "team_advanced_games": {
        "endpoint": "/stats/game/advanced", "first": 2008,
        "fetch": api.fetch_advanced_game_stats, "norm": norm_advanced,
        "why": "the same splits per game — per-game opponent context",
    },
    "team_havoc_games": {
        "endpoint": "/stats/game/havoc", "first": 2008,
        "fetch": api.fetch_game_havoc, "norm": norm_havoc,
        "why": "per-game havoc, front seven vs DB",
    },
    "returning_production": {
        "endpoint": "/player/returning", "first": 2014,
        "fetch": api.fetch_returning_production, "norm": norm_passthrough,
        "why": "returning PPA and usage share — projections, playoff model",
    },
    "betting_lines": {
        "endpoint": "/lines", "first": 2008, "fetch": api.fetch_lines,
        "norm": norm_lines,
        "why": "the market baseline any predictive model has to beat",
    },
    "pregame_wp": {
        "endpoint": "/metrics/wp/pregame", "first": 2014,
        "fetch": api.fetch_pregame_wp, "norm": norm_games_by_id,
        "why": "an independent pregame win-probability benchmark",
    },
    "cfp_participants": {
        "endpoint": "/playoffs/cfp/participants", "first": 2024,
        "fetch": api.fetch_cfp_participants, "norm": norm_cfp,
        "why": "structured playoff ground truth (12-team era only)",
    },
    "player_success": {
        "endpoint": "/stats/player/success", "first": 2014,
        "fetch": api.fetch_player_success_rates, "norm": norm_player_success,
        "why": "per-player play COUNTS — honest rate denominators, offence only",
    },
    "team_talent": {
        "endpoint": "/talent", "first": 2015, "fetch": api.fetch_talent,
        "norm": norm_ratings_external, "norm_args": {"source": "talent"},
        "why": "composite talent per team-season",
    },
    "team_records": {
        "endpoint": "/records", "first": 2008, "fetch": api.fetch_team_records,
        "norm": norm_passthrough,
        "why": "home/away/neutral/conference/postseason splits",
    },
    "team_ats": {
        "endpoint": "/teams/ats", "first": 2008, "fetch": api.fetch_team_ats,
        "norm": norm_passthrough,
        "why": "against-the-spread record and average cover margin",
    },
    "game_weather": {
        "endpoint": "/games/weather", "first": 2008, "fetch": api.fetch_game_weather,
        "norm": norm_games_by_id,
        "why": "per-game conditions, with a dome flag",
    },
    "venues": {
        "endpoint": "/venues", "first": None, "fetch": api.fetch_venues,
        "norm": norm_passthrough, "season_agnostic": True,
        "why": "capacity, dome, elevation, lat/long",
    },
}

# Multi-call datasets: several endpoints landing in one table, distinguished by a
# column. Kept separate from DATASETS so the single-endpoint path stays simple.
MULTI: dict[str, dict] = {
    "team_ratings_external": {
        "first": 2008,
        "parts": {
            "elo": api.fetch_ratings_elo,
            "fpi": api.fetch_ratings_fpi,
            "srs": api.fetch_ratings_srs,
            "core": api.fetch_ratings_core,
        },
        "norm": norm_ratings_external, "key": "source",
        "why": "independent team ratings — validation and blend inputs",
    },
    "player_wepa": {
        "first": 2014,
        "parts": {
            "passing": lambda k, y: api.fetch_player_wepa(k, y, "passing"),
            "rushing": lambda k, y: api.fetch_player_wepa(k, y, "rushing"),
            "kicking": lambda k, y: api.fetch_player_wepa(k, y, "kicking"),
        },
        "norm": norm_wepa, "key": "kind",
        "why": "opponent-adjusted EPA — a benchmark, never a rating input",
    },
}

# Sliced by team because the endpoint caps at 2,000 records. Opt-in only.
PLAY_STATS = {
    "table": "play_stats", "first": 2013,
    "why": "per-play per-athlete events with down/distance/score — leverage weighting",
}


def all_dataset_names() -> list[str]:
    return sorted(list(DATASETS) + list(MULTI))


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------

def harvest_one(name: str, seasons: list[int], api_key: str, dry_run: bool = False) -> int:
    spec = DATASETS[name]
    if spec.get("season_agnostic"):
        print(f"\n[{name}] {spec['endpoint']} — season-agnostic")
        if dry_run:
            print("  would issue 1 request")
            return 0
        rows = spec["norm"](spec["fetch"](api_key), 0)
        return write_raw(name, rows)

    years = [y for y in seasons if spec["first"] is None or y >= spec["first"]]
    skipped = [y for y in seasons if y not in years]
    print(f"\n[{name}] {spec['endpoint']} — {len(years)} season(s)"
          + (f", skipping {min(skipped)}–{max(skipped)} (no data before {spec['first']})" if skipped else ""))
    if dry_run:
        print(f"  would issue up to {len(years)} requests")
        return 0

    out: list[dict] = []
    for y in years:
        raw = spec["fetch"](api_key, y)
        rows = spec["norm"](raw, y, **spec.get("norm_args", {}))
        out.extend(rows)
        print(f"  {y}: {len(raw)} raw -> {len(rows)} rows")
        time.sleep(REQUEST_SPACING)
    return write_raw(name, out, season_key="season", seasons=years)


def harvest_multi(name: str, seasons: list[int], api_key: str, dry_run: bool = False) -> int:
    spec = MULTI[name]
    years = [y for y in seasons if y >= spec["first"]]
    parts = spec["parts"]
    print(f"\n[{name}] {len(parts)} endpoints x {len(years)} season(s)")
    if dry_run:
        print(f"  would issue up to {len(parts) * len(years)} requests")
        return 0

    out: list[dict] = []
    for part, fn in parts.items():
        n = 0
        for y in years:
            rows = spec["norm"](fn(api_key, y), y, part)
            out.extend(rows)
            n += len(rows)
            time.sleep(REQUEST_SPACING)
        print(f"  {part}: {n} rows")
    return write_raw(name, out, season_key="season", seasons=years)


def harvest_play_stats(seasons: list[int], api_key: str, dry_run: bool = False) -> int:
    """Sliced by team, because a bare year request silently truncates at 2,000.

    Teams come from our own teams.json rather than the API, so the slice list is
    the set of teams we actually track.
    """
    years = [y for y in seasons if y >= PLAY_STATS["first"]]
    teams = sorted(team_id_map())
    print(f"\n[play_stats] /plays/stats — {len(years)} season(s) x {len(teams)} teams")
    if dry_run:
        print(f"  would issue up to {len(years) * len(teams)} requests — this is the expensive one")
        return 0
    if not teams:
        print("  no teams on disk — run script 01 first")
        return 0

    out: list[dict] = []
    for y in years:
        n = 0
        for i, school in enumerate(teams):
            rows = api.fetch_play_stats(api_key, y, team=school)
            if len(rows) >= 2000:
                print(f"    WARNING {y} {school}: {len(rows)} rows — at the cap, slice finer")
            for r in rows:
                if not isinstance(r, dict):
                    continue
                flat = flatten_keys(r)
                flat["season"] = y
                flat["team_id"] = resolve_team(r.get("team"))
                out.append(flat)
            n += len(rows)
            if (i + 1) % 25 == 0:
                print(f"    {y}: {i+1}/{len(teams)} teams, {n} rows")
            time.sleep(REQUEST_SPACING)
        print(f"  {y}: {n} rows")
    return write_raw("play_stats", out, season_key="season", seasons=years)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", nargs="+", help="dataset names (see --list)")
    p.add_argument("--all", action="store_true", help="every dataset except play_stats")
    p.add_argument("--seasons", nargs="+", type=int)
    p.add_argument("--list", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="print the plan, issue no requests")
    args = p.parse_args()

    if args.list:
        print("Supplemental datasets\n")
        for n in all_dataset_names():
            spec = DATASETS.get(n) or MULTI[n]
            first = spec.get("first")
            print(f"  {n:24s} {'from ' + str(first) if first else 'season-agnostic':18s} {spec['why']}")
        print(f"  {'play_stats':24s} {'from 2013':18s} {PLAY_STATS['why']}")
        print("\nplay_stats is opt-in: ~130 requests per season (2,000-record cap).")
        return

    names = list(args.dataset or [])
    if args.all:
        names = all_dataset_names()
    if not names:
        p.error("pass --dataset, --all, or --list")

    unknown = [n for n in names if n not in DATASETS and n not in MULTI and n != "play_stats"]
    if unknown:
        p.error(f"unknown dataset(s): {', '.join(unknown)}. Try --list.")

    seasons = args.seasons or list(range(FIRST_SEASON, LAST_SEASON + 1))
    api_key = "" if args.dry_run else load_api_key()

    print(f"Seasons: {min(seasons)}–{max(seasons)}   Datasets: {len(names)}")
    total = 0
    for n in names:
        try:
            if n == "play_stats":
                total += harvest_play_stats(seasons, api_key, args.dry_run)
            elif n in MULTI:
                total += harvest_multi(n, seasons, api_key, args.dry_run)
            else:
                total += harvest_one(n, seasons, api_key, args.dry_run)
        except Exception as e:
            # One dead endpoint must not cost the other fifteen. The failure is
            # printed rather than raised so a --all run finishes and says what
            # it could not get.
            print(f"  ERROR harvesting {n}: {type(e).__name__}: {e}")

    print(f"\nDone. {total} rows written across {len(names)} dataset(s).")


if __name__ == "__main__":
    main()
