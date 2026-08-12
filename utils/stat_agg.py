"""Rebuild a season aggregate from per-game stat rows.

The API gives us two shapes and they do not agree. A `season_aggregate` row
carries split-out counts (`passingCOMPLETIONS`, `kickingFGM`) and derived rates
(`passingYPA`, `rushingYPC`); a `game_aggregate` row carries paired strings
(`passingC/ATT` = "25/38", `kickingFG` = "2/3") and per-game averages.

That matters because ~350-465 player-seasons a year have game rows and no season
aggregate at all. Script 07 inner-joined on the season aggregate, so those players
were dropped from the ratings entirely even when they had a full EDGE score —
Jayden Virgin-Morgan played four seasons at Boise State with 12-14 game rows each
and was rated in none of them.

Summing is only correct for counts. `LONG` is a maximum, rates have to be
recomputed from their components once totals exist, and the paired strings have to
be split before any of it works.
"""

from __future__ import annotations

# Paired "made/attempted" strings in game rows -> the two count keys a season
# aggregate would have carried.
_PAIRS = {
    "passingC/ATT": ("passingCOMPLETIONS", "passingATT"),
    "kickingFG":    ("kickingFGM", "kickingFGA"),
    "kickingXP":    ("kickingXPM", "kickingXPA"),
}

# Recomputed from totals once summed — never averaged from per-game averages,
# which would weight a 2-carry game the same as a 30-carry one.
_RATES = {
    "passingYPA":   ("passingYDS", "passingATT"),
    "passingPCT":   ("passingCOMPLETIONS", "passingATT"),
    "rushingYPC":   ("rushingYDS", "rushingCAR"),
    "receivingYPR": ("receivingYDS", "receivingREC"),
    "kickingPCT":   ("kickingFGM", "kickingFGA"),
    "puntingYPP":   ("puntingYDS", "puntingNO"),
    "kickReturnsAVG": ("kickReturnsYDS", "kickReturnsNO"),
    "puntReturnsAVG": ("puntReturnsYDS", "puntReturnsNO"),
    "interceptionsAVG": ("interceptionsYDS", "interceptionsINT"),
}

# Per-game averages that exist only in the game shape. Dropped rather than
# summed — a sum of averages is not a statistic.
_DROP = {"passingAVG", "rushingAVG", "receivingAVG", "puntingAVG"}

_PCT_SCALE = {"passingPCT", "kickingPCT"}   # reported 0-100, not 0-1

# Attached to a season aggregate by the harvest whether or not the box score came
# back: usage, PPA and awards are separate endpoints, so a row can hold these and
# no production at all. See script 01's save_season_stats.
META_KEYS = frozenset({"ppa", "snap_pct", "snap_pct_pass", "snap_pct_rush",
                       "games_played", "award_tier"})


def _num(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s or s in {"-", "--", "—"}:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _split_pair(v) -> tuple[float, float]:
    """"25/38" -> (25.0, 38.0). Anything unparseable is (0, 0), never a guess."""
    if v is None:
        return 0.0, 0.0
    s = str(v).strip()
    if "/" not in s:
        return _num(s), 0.0
    a, _, b = s.partition("/")
    return _num(a), _num(b)


def has_box_score(d) -> bool:
    """True when a season-aggregate payload records actual production.

    "The row exists" is not "the stats came back". The API answered with usage or
    PPA and nothing else for 176 offensive skill players in 2025 alone; counted as
    present, they read as zero production and drag their whole position room's
    shares down with them. Script 15 has always treated missing and empty alike —
    this is that rule, shared, so 07 and 12 cannot drift from it.
    """
    if not isinstance(d, dict) or not d:
        return False
    for k, v in d.items():
        if k in META_KEYS or k in _DROP:
            continue
        if k in _PAIRS:
            made, att = _split_pair(v)
            if made or att:
                return True
            continue
        if _num(v):
            return True
    return False


def aggregate_game_stats(game_dicts) -> dict:
    """Sum a player's per-game rows into a season-aggregate-shaped dict.

    Returns {} when there is nothing to aggregate, so callers can tell "no data"
    from "played and produced zero".
    """
    rows = [d for d in game_dicts if isinstance(d, dict) and d]
    if not rows:
        return {}

    out: dict[str, float] = {}
    longs: dict[str, float] = {}

    for d in rows:
        for k, v in d.items():
            if k in _DROP:
                continue
            if k in _PAIRS:
                made, att = _split_pair(v)
                a, b = _PAIRS[k]
                out[a] = out.get(a, 0.0) + made
                out[b] = out.get(b, 0.0) + att
                continue
            if k.endswith("LONG"):
                longs[k] = max(longs.get(k, 0.0), _num(v))
                continue
            if k.endswith("PCT") or k.endswith("AVG"):
                continue          # recomputed below from totals
            out[k] = out.get(k, 0.0) + _num(v)

    out.update(longs)
    out["games_played"] = float(len(rows))

    for rate, (num, den) in _RATES.items():
        d = out.get(den, 0.0)
        if d > 0:
            val = out.get(num, 0.0) / d
            out[rate] = val * 100.0 if rate in _PCT_SCALE else val

    return out
