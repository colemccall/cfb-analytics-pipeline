"""Shared name→player matching for scraped sources (EA ratings, On3 NIL, ...).

External sources give us a name and a school, never our ids. Matching is the
step where bad data gets in, so the rules here are deliberately strict:

  * exact name + school agreeing        -> match ("team")
  * exact name, unique in our data      -> match ("unique") — covers players who
                                            transferred since their last season
  * fuzzy name                          -> ONLY with school agreement. "Chaden
                                            Sullivan" and "Caden Sullivan" are one
                                            edit apart and are different people.
  * anything else                       -> no match

Two people can share a name, so resolve_collisions() is a required second pass:
it unmatches rows when several scraped players claim the same player of ours.

Usage:
    from utils.matching import build_player_index, match_player, resolve_collisions

    index = build_player_index(seasons=[2025, 2024])
    pid, psid, how = match_player("Jayden Virgin-Morgan", "Boise State", index)
"""

import difflib

from utils.store import read_raw

# Scraped-source school labels that differ from our teams.json school names.
TEAM_ALIASES = {
    "appalachian state":       "app state",
    "cal":                     "california",
    "connecticut":             "uconn",
    "fau":                     "florida atlantic",
    "fiu":                     "florida international",
    "hawaii":                  "hawai'i",
    "miami (ohio)":            "miami (oh)",
    "miami (fl)":              "miami",
    "middle tennessee state":  "middle tennessee",
    "ole miss":                "ole miss",
    "san jose state":          "san josé state",
    "umass":                   "massachusetts",
    "usf":                     "south florida",
}

_NAME_SUFFIXES = [" jr.", " jr", " sr.", " sr", " ii", " iii", " iv"]


def strip_suffix(name: str) -> str:
    name = (name or "").lower().strip()
    for suffix in _NAME_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)].strip()
    return name


def normalize_school(school: str | None) -> str:
    s = (school or "").lower().strip()
    return TEAM_ALIASES.get(s, s)


def build_player_index(seasons: list[int] | None = None) -> dict:
    """{name_key: [(player_id, player_season_id, school, season), ...]}.

    seasons limits which player_seasons are considered — pass the recent ones
    when matching a current roster, since older seasons add name-collision risk
    without adding matches. One candidate per player (most recent season wins);
    keeping a row per season would make every multi-season player look like a
    name collision and defeat the unique-name rule in match_player().
    """
    players_df = read_raw("players")
    ps_df      = read_raw("player_seasons")
    teams_df   = read_raw("teams")
    if players_df.empty or ps_df.empty:
        return {}

    school_by_team_id = {int(r["id"]): (r.get("school") or "").lower()
                         for _, r in teams_df.iterrows() if r.get("id") is not None}
    name_by_player_id = {int(r["id"]): (r.get("name") or "").strip()
                         for _, r in players_df.iterrows() if r.get("id") is not None}

    rows = ps_df[ps_df["season"].isin(seasons)] if seasons else ps_df

    best_by_player: dict = {}
    for row in rows.to_dict("records"):
        pid = row.get("player_id")
        if pid is None:
            continue
        pid    = int(pid)
        season = int(row.get("season") or 0)
        if pid in best_by_player and best_by_player[pid][3] >= season:
            continue
        tid    = row.get("team_id")
        school = school_by_team_id.get(int(tid), "") if tid is not None else ""
        best_by_player[pid] = (pid, row.get("id"), school, season)

    index: dict = {}
    for pid, candidate in best_by_player.items():
        name = name_by_player_id.get(pid, "")
        if name:
            index.setdefault(strip_suffix(name), []).append(candidate)
    return index


def match_player(name: str, team: str | None, player_index: dict,
                 threshold: float = 0.88) -> tuple[int | None, int | None, str | None]:
    """Return (player_id, player_season_id, match_type) — see module docstring."""
    key    = strip_suffix(name)
    school = normalize_school(team)

    def _team_hit(candidates):
        for pid, psid, cand_school, _season in candidates:
            if cand_school == school:
                return pid, psid
        return None

    exact = player_index.get(key)
    if exact:
        hit = _team_hit(exact)
        if hit:
            return hit[0], hit[1], "team"
        if len(exact) == 1:
            return exact[0][0], exact[0][1], "unique"

    for close in difflib.get_close_matches(key, player_index.keys(), n=3, cutoff=threshold):
        hit = _team_hit(player_index[close])
        if hit:
            return hit[0], hit[1], "team"
    return None, None, None


def resolve_collisions(rows: list[dict]) -> int:
    """Drop matches where several scraped rows claim the same player of ours.

    Expects each row to carry player_id / player_season_id / match_type as set by
    match_player. Keeps the one school-confirmed row; if none or several are
    confirmed, unmatches them all rather than guess. Returns rows unmatched.
    """
    by_player: dict = {}
    for r in rows:
        if r.get("player_id") is not None:
            by_player.setdefault(r["player_id"], []).append(r)

    dropped = 0
    for claims in by_player.values():
        if len(claims) < 2:
            continue
        confirmed = [r for r in claims if r.get("match_type") == "team"]
        keep = confirmed[0] if len(confirmed) == 1 else None
        for r in claims:
            if r is keep:
                continue
            r["player_id"] = None
            r["player_season_id"] = None
            r["match_type"] = None
            dropped += 1
    return dropped
