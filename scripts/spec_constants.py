"""The ratings spec's constants table, generated from the code so it cannot be typed wrong. Read-only.

docs/RATINGS_SPEC.md (and the METHODS entry `system-spec` that mirrors it) carries one table of the constants that set a rating:
name, value, file, who set it, and whether it is frozen. This script builds that table by importing the scripts and reading
the values, and tests/test_ratings_spec.py fails if the spec's copy differs from what the code says now, so a constant cannot
change without the spec changing with it ("change both or neither", CLAUDE.md section 2).

  who   `owner`   set by the owner's judgement (labelled as such everywhere it is shown)
        `data`    fitted or chosen by a measurement a validator reproduces
        `design`  a structural choice (a count, a list, a scale)
  frozen constants are the ones CLAUDE.md section 3 says need a written case to change.

    python scripts/spec_constants.py                 print the table
    python scripts/spec_constants.py --update-spec   rewrite the table inside docs/RATINGS_SPEC.md
"""

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

SPEC = ROOT / "docs" / "RATINGS_SPEC.md"
START, END = "<!-- constants:start -->", "<!-- constants:end -->"


def _module(file_name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, ROOT / "scripts" / file_name)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _short(v) -> str:
    """A value as a short, stable string: floats trimmed, dicts and lists summarised by shape when long."""
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, dict):
        if all(isinstance(x, (int, float)) for x in v.values()):
            return "{" + ", ".join(f"{k}: {_short(x)}" for k, x in v.items()) + "}"
        return "{" + ", ".join(f"{k}: {_short(x)}" for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_short(x) for x in v) + "]"
    return str(v)


def rows() -> list:
    """[(name, value, file, who, frozen)] for the constants the spec documents, read from the live modules."""
    s06 = _module("06_compute_edge_scores.py", "s06")
    s07 = _module("07_compute_player_ratings.py", "s07")
    s10 = _module("10_compute_team_ratings.py", "s10")
    from utils import opponent_strength as ost, results_rating as rr, seasons

    anchors = {pg: [(x, y) for x, y in v] for pg, v in s07.EDGE_OVR_ANCHORS.items()}
    qb = s06.OFFENSIVE_COMPOSITE["QB"]
    L = []
    add = lambda name, value, file, who, frozen="": L.append((name, _short(value), file, who, frozen))   # noqa: E731
    # player ratings: the opponent adjustment
    add("OPP_FCS_MULT", s06.OPP_FCS_MULT, "06", "owner")
    add("OPP_K_HI / OPP_K_LO", f"{s06.OPP_K_HI} / {s06.OPP_K_LO}", "06", "data")
    add("LAM_TEAM / LAM_CONF", f"{ost.LAM_TEAM} / {ost.LAM_CONF}", "utils/opponent_strength", "data")
    add("CROSS_WEIGHT", ost.CROSS_WEIGHT, "utils/opponent_strength", "data")
    add("POST_WEIGHT", ost.POST_WEIGHT, "utils/opponent_strength", "data")
    # the QB score
    add("QB composite (pass yd, rush yd, pass TD, rush TD, INT)", [qb["passingYDS"], qb["rushingYDS"], qb["passingTD"], qb["rushingTD"], qb["passingINT"]], "06", "owner")
    add("QB_EFFICIENCY_BLEND / QB_ATT_REF / QB_ATT_FLOOR", f"{s06.QB_EFFICIENCY_BLEND} / {s06.QB_ATT_REF} / {s06.QB_ATT_FLOOR}", "06", "data")
    add("SOLO_MULT / ASSIST_MULT", f"{s06.SOLO_MULT} / {s06.ASSIST_MULT}", "06", "data")
    add("COVERAGE_CREDIT", s06.COVERAGE_CREDIT, "06", "data")
    add("ARCHETYPE_SCALE", s06.ARCHETYPE_SCALE, "06", "data")
    add("SECONDARY_ARCHETYPE_WEIGHTS", s06.SECONDARY_ARCHETYPE_WEIGHTS, "06", "design")
    add("OPPORTUNITY_CLIP / OPPORTUNITY_MIN_PLAYS", f"{list(s06.OPPORTUNITY_CLIP)} / {s06.OPPORTUNITY_MIN_PLAYS}", "06", "data")
    # the score to a rating (frozen)
    for pg in ("QB", "RB", "WR", "TE", "EDGE", "DL", "LB", "CB", "S", "DB"):
        top = ", ".join(f"{y:g}: {x:g}" for x, y in anchors[pg] if y >= 85)
        add(f"EDGE_OVR_ANCHORS[{pg}] (the score that earns each rating from 85 up)", "{" + top + "}", "07", "data", "frozen")
    add("PLAYTIME_TIERS (QB starter / role / reserve)", [s07.PLAYTIME_TIERS["QB"][k] for k in ("starter", "role", "reserve")], "07", "design", "frozen")
    add("COMPOSITE_OVR_ANCHORS (OL, K, P: composite 0-1 to a rating, first and last anchor)", {pg: f"{a[0][1]:g} at {a[0][0]:g} to {a[-1][1]:g} at {a[-1][0]:g}" for pg, a in sorted(s07.COMPOSITE_OVR_ANCHORS.items())}, "07", "data", "frozen")
    add("STARS_OVR_DELTA", s07.STARS_OVR_DELTA, "07", "design")
    # in-season and low-game ratings
    add("LOW_GAME_WEIGHT (games, busy)", s07.LOW_GAME_WEIGHT, "07", "data")
    add("LOW_GAME_CEILING", s07.LOW_GAME_CEILING, "07", "design")
    add("PART_SEASON_CURVE[DL/EDGE, 8-9] (a, b)", s07.PART_SEASON_CURVE[("DL/EDGE", "8-9")], "07", "data")
    add("PART_SEASON_CURVE[QB, 8-9] (a, b)", s07.PART_SEASON_CURVE[("QB", "8-9")], "07", "data")
    add("PART_SEASON_CURVE[RB, 8-9] (a, b)", s07.PART_SEASON_CURVE[("RB", "8-9")], "07", "data")
    add("PACE_SHARE_EDGES / PACE_SHARE_K (pre-v4.24 pace scale, still the paced EDGE of 06)", f"{list(s06.PACE_SHARE_EDGES)} / {list(s06.PACE_SHARE_K)}", "06", "data")
    add("_CLASSIC_EDGE_CAP / _CLASSIC_DL_CAP (pre-2016 EDGE and DL)", f"{s07._CLASSIC_EDGE_CAP} / {s07._CLASSIC_DL_CAP}", "07", "design")
    add("REGULAR_SEASON_GAMES", seasons.REGULAR_SEASON_GAMES, "utils/seasons", "design")
    # team ratings
    add("W_ROSTER_BLEND / W_STATS_BLEND / W_RESULTS_BLEND (the headline)", f"{s10.W_ROSTER_BLEND:.4g} / {s10.W_STATS_BLEND:.4g} / {s10.W_RESULTS_BLEND:.4g}", "10", "owner")
    add("W_ROSTER_PLAY / W_STATS_PLAY (the play rating)", f"{s10.W_ROSTER_PLAY} / {s10.W_STATS_PLAY}", "10", "design")
    add("SLOT_STARTERS", s10.SLOT_STARTERS, "10", "design")
    add("SLOT_WEIGHTS", s10.SLOT_WEIGHTS, "10", "data")
    add("STARTER_SHARE", s10.STARTER_SHARE, "10", "data")
    add("SCHED_COEF", s10.SCHED_COEF, "10", "data")
    add("SHRINK_GAMES_K", s10.SHRINK_GAMES_K, "10", "data")
    add("IN_PROGRESS_MIN_FBS / IN_PROGRESS_MIN_ALL (games still to come for a season in progress)", f"{s10.IN_PROGRESS_MIN_FBS} / {s10.IN_PROGRESS_MIN_ALL}", "10", "design")
    add("CAP / BONUS / LAM / BETA (the results reading and power)", f"{rr.CAP} / {rr.BONUS} / {rr.LAM} / {rr.BETA}", "utils/results_rating", "data")
    add("EARNED_CAP / EARNED_BONUS (the earned order)", f"{rr.EARNED_CAP} / {rr.EARNED_BONUS}", "utils/results_rating", "design")
    return L


def table() -> str:
    """The constants as a markdown table."""
    out = ["| Constant | Value | Where | Set by | Frozen |", "|---|---|---|---|---|"]
    for name, value, file, who, frozen in rows():
        out.append(f"| {name} | {value.replace('|', '/')} | {file} | {who} | {frozen} |")
    return "\n".join(out)


def block() -> str:
    """The table between its markers, as it sits in the spec."""
    return f"{START}\n{table()}\n{END}"


def main() -> int:
    """Print the table; with --update-spec rewrite the one inside the spec."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--update-spec", action="store_true")
    args = ap.parse_args()
    if args.update_spec:
        text = SPEC.read_text(encoding="utf-8")
        a, b = text.index(START), text.index(END) + len(END)
        SPEC.write_text(text[:a] + block() + text[b:], encoding="utf-8")
        print(f"updated the constants table in {SPEC}")
    else:
        print(table())
    return 0


if __name__ == "__main__":
    sys.exit(main())
