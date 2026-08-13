"""The offensive line rating, measured at the level the data actually supports.

There is no per-lineman blocking data anywhere in the CFB Data API — no pancakes,
no sacks allowed by player, no pressures allowed. That was verified by a full key
scan and is recorded in docs/API_INVENTORY.md, so it is a fact about the world
rather than a gap in our harvest.

The old OL player rating pretended otherwise. It read two team keys that were
never written into the payload it read them from, so 55% of the formula silently
collapsed — one term to zero, the other to a constant — and what shipped was
`0.25 + 0.30·recruiting + 0.10·class + 0.05·award`. Its correlation with the
recruiting composite was 0.877 and its rank agreement with EA was **negative**
(−0.274). Twenty percent of rated linemen landed on exactly 80.0.

So the player number is withdrawn, and this replaces it with a measurement of the
thing that can actually be measured: the unit. Five standard line metrics, all
confirmed present back to 2008, on fixed absolute anchors like every other rating
in this project.

    run blocking    line yards, stuff rate, power success, second-level yards
    pass protection sack rate allowed

What this is NOT: a per-player rating with the team's name on it. It is attached
to the team-season. Allocating it back to individuals by snaps or starts was
considered and rejected — it would invent per-player variance that does not exist
and would look exactly like a measurement.
"""

from __future__ import annotations

import numpy as np

# Weights across the five inputs. Run blocking carries 75% because four of the
# five metrics describe it and because the run-game metrics isolate the line far
# better than sack rate does — a sack is a quarterback holding the ball as often
# as it is a lineman losing a rep.
LINE_UNIT_WEIGHTS: dict[str, float] = {
    "line_yards":            0.30,
    "sack_rate_allowed_inv": 0.25,
    "stuff_rate_inv":        0.20,
    "power_success":         0.15,
    "second_level_yards":    0.10,
}

# Bounds are ERA-BUCKETED, and that is not a stylistic choice — it is a fix for a
# bug this rating shipped with for exactly one run.
#
# Pooling all eighteen seasons produced a rating that drifted from a median of 52
# in 2008 to 77 in 2023, which would have said that almost every modern line
# outblocks almost every old one. It is not improvement. The underlying metrics
# have two visible step changes, and they are step changes rather than trends:
#
#   median line yards   2.775 (2008) → 2.885 (2020) → 3.095 (2021) → 3.123 (2023)
#   median stuff rate   0.218 (2008) → 0.199 (2020) → 0.165 (2021)
#   median power succ.  0.648 (2008) → 0.664 (2013) → 0.717 (2014)
#
# A 7% jump in line yards and a 17% drop in stuff rate between two consecutive
# seasons is the provider changing a definition, not 130 teams simultaneously
# learning to block. Bounds pooled across that produce a rating that mostly
# encodes what year it is.
#
# So: three era buckets, each calibrated on its own p10/p90. Within an era the
# bounds are still fixed absolute constants — if no line in the country blocks
# well in a given season, none of them gets a 90, which is the guarantee
# docs/AUDIT_FINDINGS.md §9 exists to protect. Only the era boundary moves.
#
# The breaks are at 2014 and 2021, which are NOT the same as script 07's
# ERA_ANCHORS (2013 and 2018). They are different phenomena and should not be
# forced to agree: EDGE's buckets track when defensive stats became available,
# these track when the advanced-stats endpoint changed how it computes line play.
LINE_UNIT_ERAS: list[tuple[str, int, int]] = [
    ("classic",    2008, 2013),
    ("transition", 2014, 2020),
    ("modern",     2021, 2100),
]

# p10/p90 within each era, over 2,295 FBS team-seasons. Sack rate comes from a
# different endpoint (/stats/season `sacksOpponent`) and only exists from 2016;
# the classic bucket reuses the transition bounds so the four run metrics still
# renormalise correctly when it is absent.
LINE_UNIT_BOUNDS_BY_ERA: dict[str, dict[str, tuple[float, float]]] = {
    "classic": {
        "line_yards":         (2.4471, 3.2132),
        "stuff_rate":         (0.1658, 0.2521),   # inverted below — lower is better
        "power_success":      (0.5531, 0.7532),
        "second_level_yards": (0.8358, 1.2352),
        "sack_rate_allowed":  (0.0377, 0.1006),   # not published before 2016
    },
    "transition": {
        "line_yards":         (2.5220, 3.2780),
        "stuff_rate":         (0.1567, 0.2384),
        "power_success":      (0.6191, 0.7977),
        "second_level_yards": (0.9172, 1.3460),
        "sack_rate_allowed":  (0.0377, 0.1006),
    },
    "modern": {
        "line_yards":         (2.7059, 3.3714),
        "stuff_rate":         (0.1334, 0.2157),
        "power_success":      (0.6350, 0.8095),
        "second_level_yards": (0.9037, 1.3136),
        "sack_rate_allowed":  (0.0354, 0.0937),
    },
}

# What a caller gets with no season: the modern bucket, which is what "now" means.
LINE_UNIT_BOUNDS = LINE_UNIT_BOUNDS_BY_ERA["modern"]


def era_for(season: int | None) -> str:
    """Which calibration bucket a season belongs to."""
    if season is None:
        return "modern"
    for name, lo, hi in LINE_UNIT_ERAS:
        if lo <= int(season) <= hi:
            return name
    return "modern"


def bounds_for(season: int | None) -> dict:
    return LINE_UNIT_BOUNDS_BY_ERA[era_for(season)]

# Composite [0–1] → rating. The ceiling is 95 rather than 99: this is five
# blockers, a scheme, a quarterback's internal clock and a set of running backs
# measured together, so the very top of the scale would be claiming a precision
# about the LINE that a team-level measurement cannot have. It is well above the
# 88 the old player rating could reach, because unlike that number this one is
# measuring something.
LINE_UNIT_ANCHORS: list[tuple[float, float]] = [
    (0.00, 30), (0.15, 42), (0.30, 52), (0.45, 62),
    (0.55, 70), (0.70, 80), (0.85, 89), (1.00, 95),
]


def _norm(val, lo: float, hi: float, invert: bool = False) -> float | None:
    """Scale to [0,1] against fixed bounds. None in, None out — never a zero.

    A missing input must not be scored as the worst possible value. The composite
    below renormalises around whatever is present instead, which is the same rule
    script 10 applies when a whole signal is absent for a team-season.
    """
    if val is None or (isinstance(val, float) and val != val):
        return None
    if hi <= lo:
        return 0.5
    x = float(np.clip((float(val) - lo) / (hi - lo), 0.0, 1.0))
    return 1.0 - x if invert else x


def line_unit_composite(m: dict, season: int | None = None) -> tuple[float | None, dict]:
    """{metric: value} → (composite 0–1, per-input normalised contributions).

    Accepts any subset of the five inputs and renormalises the weights over the
    ones present, so a season missing sack data still produces a run-blocking
    composite rather than a silently deflated one. Returns (None, {}) when
    nothing usable is present.

    `season` selects the era bucket. Omitting it uses the modern calibration,
    which is right for a current-season caller and wrong for a 2009 one — pass it.
    """
    b = bounds_for(season)
    parts = {
        "line_yards":            _norm(m.get("line_yards"), *b["line_yards"]),
        "stuff_rate_inv":        _norm(m.get("stuff_rate"), *b["stuff_rate"], invert=True),
        "power_success":         _norm(m.get("power_success"), *b["power_success"]),
        "second_level_yards":    _norm(m.get("second_level_yards"), *b["second_level_yards"]),
        "sack_rate_allowed_inv": _norm(m.get("sack_rate_allowed"), *b["sack_rate_allowed"],
                                       invert=True),
    }
    present = {k: v for k, v in parts.items() if v is not None}
    if not present:
        return None, {}
    total_w = sum(LINE_UNIT_WEIGHTS[k] for k in present)
    composite = sum(LINE_UNIT_WEIGHTS[k] * v for k, v in present.items()) / total_w
    return float(composite), {k: round(v, 4) for k, v in present.items()}


def line_unit_rating(m: dict, season: int | None = None) -> tuple[float | None, dict]:
    """{metric: value} → (rating 30–95, contributions). The public entry point."""
    composite, parts = line_unit_composite(m, season)
    if composite is None:
        return None, {}
    xs = [a[0] for a in LINE_UNIT_ANCHORS]
    ys = [float(a[1]) for a in LINE_UNIT_ANCHORS]
    rating = float(np.clip(np.interp(composite, xs, ys), 30.0, 95.0))
    parts["composite"] = round(composite, 4)
    parts["era"] = era_for(season)
    return round(rating, 2), parts
