"""Should a player with two games be rated on how he played? Read-only. The owner's question after Max Cutforth (v4.27).

Today a player with one or two games is rated by the low-game rule (scripts/07 low_game_rating): a recruiting prior plus about a
fifth of how far his stretch sits from other low-game players, capped at 72. The proposal: in a season in progress a two-game
player is rated like a three-game player, by his per-game level through a PART_SEASON_CURVE line fitted for two games.

Data: 2021-2025, the per-game adjusted scores script 06 writes (run in a private copy, offline), regular season, cut after weeks
4, 6, 8 and 10. One row per player-cut with EXACTLY two games at the cut. Every value is on the rating scale (script 07 edge_to_ovr).

  Target    T2: the level he played at AFTER the cut, sqrt(12) x the mean of his remaining games, at least 3 of them. It is what
            scripts/validate_part_time_rating.py fits the curve on. It exists only for players who kept playing (stated in the output).
  Candidates
            today   the published low-game rating from the weekly replays (2024 and 2025 only: older seasons have no weekly file)
            level   his own level, believed entirely
            curve2  a + b x level, fitted for two games by position family on the other seasons (leave one season out)
            curve3  the shipped three-game line applied at two games, no new constants
            mean    the family's mean later level (the dumb baseline), from the other seasons
            placebo curve2 with each level dealt to another player of the same family, cut and season
  Pass      curve2 beats today on 2024-2025, overall and for players with no recruiting grade; beats the placebo; and is no worse than
            the dumb baseline. Both the error and the bias are printed, then the cost at the top end and the size of the jump at game 3.

    python scripts/validate_two_game_rating.py      # about 10 minutes, 0 requests

Limits: only players who played at least 3 more games have a target, so this cannot speak for one who never played again. The
per-game scores use the season's final opponent strengths at every cut (the same for every candidate).
"""

import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
APP = ROOT.parent / "cfb-analytics-app" / "data"


def _module(file_name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, ROOT / "scripts" / file_name)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


vp = _module("validate_part_time_rating.py", "vp2")
t07 = vp.t07
SEASONS = vp.SEASONS
CUTS = vp.CUTS
SQ = vp.SQ
WEEKLY_SEASONS = (2024, 2025)


def two_game_rows(df: pd.DataFrame, through: dict) -> pd.DataFrame:
    """One row per player-cut with exactly two games at the cut: his level, what came after, and his rating at his third game."""
    rows = []
    for (season, psid), grp in df.sort_values("week").groupby(["season", "player_season_id"]):
        team, pg, fam = int(grp["team_id"].iloc[0]), grp["position_group"].iloc[0], grp["family"].iloc[0]
        a, w = grp["adj_score"].to_numpy(), grp["week"].to_numpy()
        for c in CUTS:
            seen, later = a[w <= c], a[w > c]
            if len(seen) != 2 or not through.get((season, team), {}).get(c, 0):
                continue
            third = t07.part_season_curve(t07.edge_to_ovr(SQ * a[:3].mean(), pg), pg, 3) if len(a) >= 3 else np.nan
            rows.append({"season": season, "cut": c, "family": fam, "pg": pg, "psid": int(psid), "level": t07.edge_to_ovr(SQ * seen.mean(), pg),
                         "t2": t07.edge_to_ovr(SQ * later.mean(), pg) if len(later) >= 3 else np.nan, "third": third})
    return pd.DataFrame(rows)


def published(d: pd.DataFrame) -> pd.DataFrame:
    """Add `today` (the published rating at that cut, from the weekly replay) and `no_grade` (no recruiting grade on file)."""
    today, no_grade = {}, {}
    for season in WEEKLY_SEASONS:
        pl = json.loads((APP / f"players_{season}.json").read_text(encoding="utf-8"))
        wk = json.loads((APP / f"weekly_players_{season}.json").read_text(encoding="utf-8"))["players"]
        for p in pl:
            no_grade[(season, int(p["player_season_id"]))] = not p.get("stars")
            for c in CUTS:
                rows = [r for r in wk.get(str(p["id"]), []) if r[0] <= c]
                if rows and rows[-1][2] == 2 and rows[-1][1] is not None:
                    today[(season, c, int(p["player_season_id"]))] = float(rows[-1][1])
    d = d.copy()
    d["today"] = [today.get((s, c, p), np.nan) for s, c, p in zip(d["season"], d["cut"], d["psid"])]
    d["no_grade"] = [no_grade.get((s, p), False) for s, p in zip(d["season"], d["psid"])]
    return d


def fit(rows: pd.DataFrame) -> dict:
    """{family: (a, b)} for later level = a + b x level on rows that have a target."""
    out = {}
    for fam, g in rows[rows["t2"].notna()].groupby("family"):
        if len(g) >= 50:
            b, a = np.polyfit(g["level"], g["t2"], 1)
            out[fam] = (float(a), float(b))
    return out


def candidates(d: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Add curve2, curve3, mean and the placebo, each fitted on the other seasons."""
    d = d.copy()
    d["curve3"] = [t07.part_season_curve(lv, pg, 3) for lv, pg in zip(d["level"], d["pg"])]
    idx = np.arange(len(d))
    for pos in d.groupby(["family", "cut", "season"]).indices.values():
        idx[pos] = rng.permutation(pos)
    d["dealt"] = d["level"].to_numpy()[idx]
    for name in ("curve2", "mean", "placebo"):
        d[name] = np.nan
    for s in sorted(d["season"].unique()):
        tr = d[d["season"] != s]
        lines, means = fit(tr), tr[tr["t2"].notna()].groupby("family")["t2"].mean()
        m = d["season"] == s
        line = lambda f, x: float(np.clip(lines[f][0] + lines[f][1] * x, 30.0, 99.0))   # noqa: E731
        d.loc[m, "curve2"] = [line(f, x) for f, x in zip(d.loc[m, "family"], d.loc[m, "level"])]
        d.loc[m, "placebo"] = [line(f, x) for f, x in zip(d.loc[m, "family"], d.loc[m, "dealt"])]
        d.loc[m, "mean"] = d.loc[m, "family"].map(means)
    return d


def cells(g: pd.DataFrame, cols) -> str:
    return "".join(f"{np.mean(np.abs(g[c] - g['t2'])):>8.2f} ({np.mean(g[c] - g['t2']):+6.2f})" for c in cols)


def table(rows: pd.DataFrame, cols, by=None) -> None:
    """Print mean error and bias per candidate, for every row and then by a grouping."""
    print(f"    {'':<14}{'n':>7} " + "".join(f"{c:>17}" for c in cols))
    print(f"    {'all':<14}{len(rows):>7,} {cells(rows, cols)}")
    if by is not None:
        for key, g in rows.groupby(by):
            print(f"    {str(key):<14}{len(g):>7,} {cells(g, cols)}")


def main() -> None:
    """Build the player-cuts, score every candidate and print the tables, the costs and the fitted lines."""
    df = vp.game_scores(SEASONS)
    _, through = vp.team_schedule(SEASONS)
    d = candidates(published(two_game_rows(df, through)), np.random.default_rng(31))
    t = d[d["t2"].notna()]
    print(f"\n{len(d):,} player-cuts with exactly two games, {len(t):,} of them with 3+ games left (the rest cannot be scored).")
    print("\nEvery season 2021-2025, against the level he played at afterwards: mean error (bias, candidate minus later level)")
    table(t, ["level", "curve2", "curve3", "mean", "placebo"], "family")
    w = t[t["today"].notna()]
    print(f"\n2024 and 2025, where the published rating exists ({len(w):,} player-cuts)")
    cols = ["today", "level", "curve2", "curve3", "mean", "placebo"]
    table(w, cols, "family")
    print("\n  by recruiting grade on file:")
    table(w, cols, "no_grade")
    print("\n  by the player's level over the two games:")
    table(w, cols, pd.cut(w["level"], [0, 60, 75, 99.9], labels=["under 60", "60 to 75", "75 and up"]).astype(str))
    print("\n  QBs whose two games were at a level of 75 or better (2024-2025):")
    table(w[(w["family"] == "QB") & (w["level"] >= 75)], cols)
    print("\nCost at the top end (every season): player-cuts a curve rates at or above a line, and how they then played")
    for c in ("curve2", "curve3"):
        for line in (72, 80, 85):
            x = t[t[c] >= line]
            print(f"    rated {line}+ by {c}: {len(x):>5,} ({len(x) / len(t):.1%}); later level minus rating {np.mean(x['t2'] - x[c]) if len(x) else float('nan'):+.2f}")
    j = d[d["third"].notna() & d["today"].notna()]
    print(f"\nThe jump at his third game ({len(j):,} player-cuts, 2024-2025): rating at three games minus rating at two")
    for c in ("today", "curve2", "curve3"):
        x = j["third"] - j[c]
        print(f"    {c:<8} mean {x.mean():+.2f}  mean size {x.abs().mean():.2f}  largest {x.abs().max():.1f}")
    print("\nWhat each would publish (mean, 10th / 50th / 90th percentile). Players with no later games have no target: they are the cost nobody can score")
    def dist(g, c):
        v = g[c].dropna()
        return f"{c} {v.mean():.1f} ({v.quantile(.1):.1f} / {v.quantile(.5):.1f} / {v.quantile(.9):.1f})" if len(v) else f"{c} -"
    for label, g in (("kept playing (3+ later games)", d[d["t2"].notna()]), ("did not (no target)", d[d["t2"].isna()])):
        print(f"    {label:<32} n {len(g):>6,}  " + "   ".join(dist(g, c) for c in ("today", "level", "curve2", "curve3")))
    low = d[d["level"] < 50]
    print(f"    {'level under 50, every player':<32} n {len(low):>6,}  " + "   ".join(dist(low, c) for c in ("today", "level", "curve2", "curve3")))
    print("\nThe lines to add to scripts/07 PART_SEASON_CURVE, fitted on all five seasons:")
    for fam, (a, b) in sorted(fit(d).items()):
        print(f'    ("{fam}", "2"): ({a:.4f}, {b:.4f}),')


if __name__ == "__main__":
    main()
