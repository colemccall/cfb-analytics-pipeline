"""Season team totals rebuilt from per-game box scores, so a season can be read as it stood after any week.

/stats/season returns one cumulative row per team per statistic. For the weekly progression (scripts/17b_weekly_progression.py)
the stats half of the headline has to be built from the games played through week N only, and the API does not offer that.
It does offer every game's box score (/games/teams, one call per week, already in the cache), and the season totals are
exactly their sum: for 2026 through week 9, 138 of 138 FBS teams match /stats/season on games, total yards, sacks, tackles
for loss, turnovers, third downs made and attempted and opponent yards (tests/test_box_scores.py checks the rule on a fixture;
the full comparison is in the v4.22 CHANGES entry).

The output has the shape of /stats/season, [{team, statName, statValue}], so the consumers in scripts/10 need no second path.
"""

from collections import defaultdict

# Box-score categories that are plain numbers and keep their name in the season table.
PLAIN = ("totalYards", "netPassingYards", "rushingYards", "rushingAttempts", "firstDowns", "sacks", "tacklesForLoss", "turnovers",
         "interceptions", "fumblesRecovered", "fumblesLost", "passesIntercepted", "passingTDs", "rushingTDs",
         "kickReturns", "kickReturnYards", "puntReturns", "puntReturnYards")
# "made-attempted" categories: box-score name -> (made name, attempted name) in the season table.
RATIO = {"thirdDownEff": ("thirdDownConversions", "thirdDowns"),
         "fourthDownEff": ("fourthDownConversions", "fourthDowns"),
         "completionAttempts": ("passCompletions", "passAttempts"),
         "totalPenaltiesYards": ("penalties", "penaltyYards")}


def _num(v) -> float | None:
    """A box-score value as a number, None when it is not one (a missing stat is not a zero)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _seconds(v) -> float | None:
    """'36:46' as 2206 seconds."""
    try:
        m, s = str(v).split(":")
        return int(m) * 60 + int(s)
    except (TypeError, ValueError):
        return None


def _split(v) -> tuple[float, float] | None:
    """'6-13' as (6, 13)."""
    try:
        a, b = str(v).split("-")
        return float(a), float(b)
    except (TypeError, ValueError):
        return None


def _team_values(team: dict) -> dict:
    """One team's game as {statName: number} with the made/attempted and clock categories split out."""
    raw = {s.get("category"): s.get("stat") for s in team.get("stats", [])}
    out = {}
    for name in PLAIN:
        v = _num(raw.get(name))
        if v is not None:
            out[name] = v
    for name, (made, att) in RATIO.items():
        pair = _split(raw.get(name))
        if pair is not None:
            out[made], out[att] = pair
    clock = _seconds(raw.get("possessionTime"))
    if clock is not None:
        out["possessionTime"] = clock
    return out


def season_rows(games: list) -> list:
    """Season totals, in /stats/season's long shape, from a list of box-score games ({id, teams: [two team blocks]}).

    A team's own statistics sum under their names and the opponent's under name + "Opponent"; `games` counts the box scores a
    team appears in. A game that does not have exactly two teams is skipped, not guessed at.
    """
    total = defaultdict(lambda: defaultdict(float))
    for g in games:
        teams = g.get("teams") or []
        if len(teams) != 2:
            continue
        vals = [_team_values(t) for t in teams]
        for i, t in enumerate(teams):
            name = t.get("team")
            if not name:
                continue
            total[name]["games"] += 1
            for k, v in vals[i].items():
                total[name][k] += v
            for k, v in vals[1 - i].items():
                total[name][k + "Opponent"] += v
    return [{"team": team, "statName": k, "statValue": v} for team, d in total.items() for k, v in d.items()]


def fetch_season_rows(api_key: str, season: int, through_week: int, get_list) -> list:
    """season_rows() over the regular-season weeks 1..through_week, reading each week through `get_list` (api_client._get_list)."""
    games = []
    for wk in range(1, through_week + 1):
        games.extend(get_list(api_key, "/games/teams", {"year": season, "week": wk, "seasonType": "regular"}))
    return season_rows(games)
