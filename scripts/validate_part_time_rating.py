"""Should a part-season rating count the games a player missed? The Sire Gaines question, measured. Read-only.

A live rating is EDGE = sum of game scores / sqrt(games), carried to a full season by sqrt(team games scheduled /
completed) and a calibration k by share of team games played (scripts/06 pace_factor_for). For the same per-game
level that is level x sqrt(his share of his team's games x the schedule) x k: a player who misses games rates lower
than his games say, by design. The owner asked whether missed games should lower a rating, and for a sample-size
rule instead (v4.24). Nothing here changes a rating; it measures, so the definition can be chosen with numbers.

Data: 2021-2025. Script 06 is run on each full season in a private copy (offline) for every player's per-game
adjusted scores, regular season only. Each season is cut after weeks 4, 6, 8 and 10; one row per player-cut with at
least 3 games at the cut. Every value is mapped through the frozen anchors (script 07 edge_to_ovr).

  (1) Ceiling   how reliable is a mean of n games? Within-player ICC of game scores by position family, and the
                Spearman-Brown curve R(n) = n ICC / (1 + (n - 1) ICC), checked against odd/even split halves.
  (2) Candidates
        L0  today:          k(share) x sqrt(scheduled / completed) x sum / sqrt(n)     availability counted
        L1  level:          sqrt(12) x mean                                           availability ignored
        L2  level, shrunk:  sqrt(12) x (mu + R(n) (mean - mu)), mu = the family's mean at that cut (never preseason)
        L3  level, curve by games played: the level rating mapped through a line a + b x level fitted for each family and
            each count of games he has PLAYED (3, 4, 5, 6, 7, 8-9, 10+), on the other seasons (leave-one-season-out), so
            few games give a stricter curve (b below 1) and the player's absence from his team's other games is ignored
  (3) Targets   T1 the season's final rating as defined today (all regular-season games, sum / sqrt(games));
                T2 the level he played at after the cut (sqrt(12) x mean of his remaining games, at least 3)
  (4) Placebo   L0 with each player's (share, schedule) dealt to another player of the same family and cut. Breaks
                which player missed games, not only how many: if availability really informs a target, the dealt
                version is worse on it.

    python scripts/validate_part_time_rating.py                       # 2021-2025, ~10 minutes
    python scripts/validate_part_time_rating.py --gaines-sandbox DIR  # also Sire Gaines and Dante Moore from a kept 17 sandbox

Limits: T1 and T2 are EDGE through the anchors, without the tiers, the low-game rule or the games damp that the
published rating adds, so they describe the EDGE layer, which is where the availability term lives. The per-game
scores are adjusted with the season's final opponent strengths at every cut (the same for every candidate).
"""

import argparse
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.seasons import REGULAR_SEASON_GAMES  # noqa: E402
from utils.store import fill_shutouts  # noqa: E402


def _module(file_name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, ROOT / "scripts" / file_name)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


t06 = _module("06_compute_edge_scores.py", "pt06")
t07 = _module("07_compute_player_ratings.py", "pt07")

SEASONS = [2021, 2022, 2023, 2024, 2025]
CUTS = [4, 6, 8, 10]
MIN_GAMES = 3
FAMILY = {"QB": "QB", "RB": "RB", "WR": "WR/TE", "TE": "WR/TE", "DL": "DL/EDGE", "EDGE": "DL/EDGE",
          "LB": "LB", "CB": "DB", "S": "DB", "DB": "DB"}
SHARE_BUCKETS = ((0.5, "<=0.5"), (0.75, "0.5-0.75"), (0.9, "0.75-0.9"), (9.9, ">0.9"))
SQ = math.sqrt(REGULAR_SEASON_GAMES)


def bucket(share: float) -> str:
    """The share-of-team-games bucket a player-cut falls in."""
    return next(label for top, label in SHARE_BUCKETS if share <= top)


def run_06(sandbox: Path, season: int) -> None:
    """Script 06 on a full season in the sandbox, offline (any attempted request fails the run). --in-progress only makes it keep
    the per-game scores; on a complete season the pace ratio is 1, so nothing is rescaled."""
    env = dict(os.environ, CFB_DATA_DIR=str(sandbox), PYTHONIOENCODING="utf-8",
               HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9")
    env.pop("CFB_REFRESH_YEARS", None)
    out = subprocess.run([sys.executable, "-B", "-W", "ignore", str(ROOT / "scripts" / "06_compute_edge_scores.py"),
                          "--season", str(season), "--in-progress"], env=env, capture_output=True, text=True, cwd=ROOT)
    text = out.stdout + out.stderr
    if out.returncode or any(ln.startswith("  GET") for ln in text.splitlines()):
        raise RuntimeError(f"06 --season {season} failed or went online:\n{text[-800:]}")


def game_scores(seasons: list) -> pd.DataFrame:
    """Per-game adjusted scores for the seasons, regular season, with week, position family and team."""
    sandbox = Path(tempfile.mkdtemp(prefix="parttime_"))
    try:
        for sub in ("raw", "computed"):
            shutil.copytree(ROOT / "data" / sub, sandbox / sub)
        for s in seasons:
            run_06(sandbox, s)
            print(f"  06 {s} done", flush=True)
        scores = pd.DataFrame(json.loads((sandbox / "raw" / "player_game_scores.json").read_text(encoding="utf-8")))
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    games = pd.DataFrame(json.loads((ROOT / "data" / "raw" / "games.json").read_text(encoding="utf-8")))
    games = games[(games["season_type"] == "regular")][["id", "week"]].rename(columns={"id": "game_id"})
    ps = pd.DataFrame(json.loads((ROOT / "data" / "raw" / "player_seasons.json").read_text(encoding="utf-8")))
    ps = ps[["id", "team_id", "position_group"]].rename(columns={"id": "player_season_id"})
    df = scores[scores["season"].isin(seasons)].merge(games, on="game_id").merge(ps, on="player_season_id")
    df["family"] = df["position_group"].map(FAMILY)
    return df[df["family"].notna()]


def team_schedule(seasons: list) -> tuple[dict, dict]:
    """({(season, team): regular-season games scheduled}, {(season, team): {week: completed games through it}})."""
    g = fill_shutouts(pd.DataFrame(json.loads((ROOT / "data" / "raw" / "games.json").read_text(encoding="utf-8"))))
    g = g[g["season"].isin(seasons) & (g["season_type"] == "regular")]
    sched, done = defaultdict(int), defaultdict(lambda: defaultdict(int))
    for r in g.itertuples():
        played = pd.notna(r.home_score) and pd.notna(r.away_score)
        for t in (r.home_team_id, r.away_team_id):
            if pd.isna(t):
                continue
            sched[(r.season, int(t))] += 1
            if played:
                done[(r.season, int(t))][int(r.week)] += 1
    through = {}
    for key, by_week in done.items():
        run, acc = {}, 0
        for w in range(1, 17):
            acc += by_week.get(w, 0)
            run[w] = acc
        through[key] = run
    return dict(sched), through


def reliability(df: pd.DataFrame) -> dict:
    """{family: (icc, split-half r, players)} from one-way ANOVA on game scores, players with 6+ games."""
    out = {}
    for fam, grp in df.groupby("family"):
        by_p = [v.to_numpy() for _, v in grp.groupby(["season", "player_season_id"])["adj_score"] if len(v) >= 6]
        if len(by_p) < 30:
            continue
        n_i = np.array([len(v) for v in by_p])
        means = np.array([v.mean() for v in by_p])
        grand = np.concatenate(by_p).mean()
        msb = (n_i * (means - grand) ** 2).sum() / (len(by_p) - 1)
        msw = sum(((v - v.mean()) ** 2).sum() for v in by_p) / (n_i.sum() - len(by_p))
        n0 = (n_i.sum() - (n_i ** 2).sum() / n_i.sum()) / (len(by_p) - 1)
        icc = max(0.0, (msb - msw) / (msb + (n0 - 1) * msw))
        odd = np.array([v[0::2].mean() for v in by_p])
        even = np.array([v[1::2].mean() for v in by_p])
        out[fam] = (icc, float(np.corrcoef(odd, even)[0, 1]), len(by_p), float(np.mean(n_i)))
    return out


def spearman_brown(icc: float, n: float) -> float:
    """Reliability of the mean of n games when one game's is icc."""
    return n * icc / (1 + (n - 1) * icc) if icc > 0 else 0.0


def player_cuts(df: pd.DataFrame, sched: dict, through: dict) -> pd.DataFrame:
    """One row per player-cut: games, mean and sum through the cut, share and pace, and both targets."""
    rows = []
    for (season, psid), grp in df.sort_values("week").groupby(["season", "player_season_id"]):
        team, pg, fam = int(grp["team_id"].iloc[0]), grp["position_group"].iloc[0], grp["family"].iloc[0]
        a, w = grp["adj_score"].to_numpy(), grp["week"].to_numpy()
        final = t07.edge_to_ovr(a.sum() / math.sqrt(len(a)), pg)
        for c in CUTS:
            seen, later = a[w <= c], a[w > c]
            comp = through.get((season, team), {}).get(c, 0)
            if len(seen) < MIN_GAMES or not comp or not sched.get((season, team)):
                continue
            rows.append({"season": season, "cut": c, "family": fam, "pg": pg, "n": len(seen), "comp": comp,
                         "sched": sched[(season, team)], "share": min(1.0, len(seen) / comp),
                         "mean": seen.mean(), "sum": seen.sum(), "t1": final,
                         "t2": t07.edge_to_ovr(SQ * later.mean(), pg) if len(later) >= MIN_GAMES else np.nan})
    return pd.DataFrame(rows)


def games_bucket(n: int) -> str:
    """The count of games a player has played, in the buckets L3 is fitted for."""
    return str(n) if n <= 7 else "8-9" if n <= 9 else "10+"


def fit_curves(d: pd.DataFrame, target: str = "t2") -> dict:
    """{(family, games bucket): (a, b)} for rating = a + b x level, fitted on the rows of `d` that have the target."""
    out = {}
    rows = d[d[target].notna()].assign(gb=lambda x: x["n"].map(games_bucket))
    for (fam, gb), g in rows.groupby(["family", "gb"]):
        if len(g) >= 50:
            b, a = np.polyfit(g["L1"], g[target], 1)
            out[(fam, gb)] = (float(a), float(b))
    return out


def apply_curve(curves: dict, fam: str, n: int, level: float) -> float:
    """The L3 rating for a level: the fitted line for (family, games bucket), the level itself if none was fitted."""
    a, b = curves.get((fam, games_bucket(n)), (0.0, 1.0))
    return float(np.clip(a + b * level, 30.0, 99.0))


def candidates(d: pd.DataFrame, rel: dict, rng: np.random.Generator) -> pd.DataFrame:
    """Add L0, L1, L2, L3 and the placebo L0 (rating points) to the player-cut rows."""
    d = d.copy()
    raw = d["sum"] / np.sqrt(d["n"])
    d["L0"] = [t07.edge_to_ovr(t06.pace_factor_for(x, s / c, n, c), pg)
               for x, s, c, n, pg in zip(raw, d["sched"], d["comp"], d["n"], d["pg"])]
    d["L1"] = [t07.edge_to_ovr(SQ * m, pg) for m, pg in zip(d["mean"], d["pg"])]
    mu = d.groupby(["family", "cut", "season"])["mean"].transform("mean")
    r_n = [spearman_brown(rel.get(f, (0,))[0], n) for f, n in zip(d["family"], d["n"])]
    d["L2"] = [t07.edge_to_ovr(SQ * (m0 + r * (m - m0)), pg) for m, m0, r, pg in zip(d["mean"], mu, r_n, d["pg"])]
    d["L3"] = np.nan
    for s in sorted(d["season"].unique()):                      # fitted on the other seasons, applied to this one
        curves = fit_curves(d[d["season"] != s])
        m = d["season"] == s
        d.loc[m, "L3"] = [apply_curve(curves, f, n, lv) for f, n, lv in zip(d.loc[m, "family"], d.loc[m, "n"], d.loc[m, "L1"])]
    # placebo: deal each row's (share, schedule) pair, together, to another row of the same family, cut and season
    idx = np.arange(len(d))
    for pos in d.groupby(["family", "cut", "season"]).indices.values():
        idx[pos] = rng.permutation(pos)
    dealt_share, dealt_sched = d["share"].to_numpy()[idx], d["sched"].to_numpy()[idx]
    k = np.array([t06.PACE_SHARE_K[sum(sh > e for e in t06.PACE_SHARE_EDGES)] for sh in dealt_share])
    d["L0p"] = [t07.edge_to_ovr(kk * m * math.sqrt(sh * sc), pg)
                for kk, m, sh, sc, pg in zip(k, d["mean"], dealt_share, dealt_sched, d["pg"])]
    d["bucket"] = d["share"].map(bucket)
    return d


def table(d: pd.DataFrame, target: str, by: str) -> None:
    """MAE / mean signed error (candidate minus target) of every candidate against one target, by a grouping."""
    rows = d[d[target].notna()]
    print(f"\n  against {target} ({'final rating' if target == 't1' else 'level after the cut'}), by {by}:   MAE  (bias)")
    print(f"    {'':>10} {'n':>7} " + "".join(f"{c:>16}" for c in ("L0 today", "L1 level", "L2 shrunk", "L3 n-curve", "L0 placebo")))
    for key, g in rows.groupby(by):
        cells = "".join(f"{np.mean(np.abs(g[c] - g[target])):>8.2f} ({np.mean(g[c] - g[target]):+5.2f})"
                        for c in ("L0", "L1", "L2", "L3", "L0p"))
        print(f"    {str(key):>10} {len(g):>7,} {cells}")


CASES = ((537401, "RB", "Sire Gaines (RB, Boise State)"), (545721, "QB", "Dante Moore (QB, Oregon)"),
         (541588, "DL", "Jamarrion Harkless (DL, Kentucky)"))


def cases(sandbox: Path, rel: dict, curves: dict) -> None:
    """The named 2026 players under each candidate, from the per-game scores of a kept script-17 sandbox."""
    scores = pd.DataFrame(json.loads((sandbox / "raw" / "player_game_scores.json").read_text(encoding="utf-8")))
    edge = pd.DataFrame(json.loads((sandbox / "raw" / "player_edge.json").read_text(encoding="utf-8")))
    ratings = pd.DataFrame(json.loads((sandbox / "computed" / "ratings.json").read_text(encoding="utf-8")))
    for psid, pg, label in CASES:
        a = scores[(scores["season"] == 2026) & (scores["player_season_id"] == psid)]["adj_score"].to_numpy()
        e = edge[(edge["season"] == 2026) & (edge["player_season_id"] == psid)].iloc[0]
        pub = ratings[(ratings["season"] == 2026) & (ratings["player_season_id"] == psid) & (ratings["engine"] == "edge")]["overall_rating"].iloc[0]
        comp, n = int(e["team_games"]), len(a)
        sched = round(float(e["pace_factor"]) * comp)
        raw = a.sum() / math.sqrt(n)
        share = min(1.0, n / comp)
        k = t06.PACE_SHARE_K[sum(share > x for x in t06.PACE_SHARE_EDGES)]
        level = t07.edge_to_ovr(SQ * a.mean(), pg)
        fam = FAMILY[pg]
        print(f"\n{label}, 2026: {n} games of his team's {comp} (share {share:.3f}), schedule {sched}; published rating {pub:.1f}")
        print(f"  game scores {np.round(a, 1).tolist()}; stored edge_raw {e['edge_raw']:.1f}, edge {e['edge_score']:.1f}")
        print(f"  L1 level            sqrt(12) x mean                 {SQ * a.mean():8.1f} -> {level:.1f}")
        print(f"  x sqrt(share)       what a season at his share scores {a.mean() * math.sqrt(share * sched):8.1f} -> "
              f"{t07.edge_to_ovr(a.mean() * math.sqrt(share * sched), pg):.1f}   (the availability term)")
        print(f"  x k {k:.4f}         today's EDGE (L0)                {k * raw * math.sqrt(sched / comp):8.1f} -> "
              f"{t07.edge_to_ovr(k * raw * math.sqrt(sched / comp), pg):.1f}")
        icc = rel.get(fam, (0,))[0]
        print(f"  L3 n-curve ({fam}, {games_bucket(n)} games)  {apply_curve(curves, fam, n, level):.1f}  (fitted line a, b = "
              f"{tuple(round(x, 2) for x in curves.get((fam, games_bucket(n)), (0, 1)))}); reliability R({n}) = {spearman_brown(icc, n):.3f}")


def main() -> None:
    """Measure the ceiling, then compare the candidates on every season and cut; print the tables."""
    ap = argparse.ArgumentParser(description="Should a part-season rating count missed games?")
    ap.add_argument("--seasons", type=int, nargs="+", default=SEASONS)
    ap.add_argument("--gaines-sandbox", type=Path, help="a script-17 --keep-sandbox folder for 2026")
    args = ap.parse_args()

    df = game_scores(args.seasons)
    sched, through = team_schedule(args.seasons)
    rel = reliability(df)
    print("\n(1) CEILING: one game's reliability (ICC) and the mean of n games (Spearman-Brown), players with 6+ games")
    print(f"    {'family':<9}{'players':>8}{'games':>7}{'ICC':>7}{'R(3)':>7}{'R(5)':>7}{'R(8)':>7}{'R(12)':>7}   odd/even r (check: ~R(n/2))")
    for fam, (icc, half, n_p, n_g) in sorted(rel.items()):
        print(f"    {fam:<9}{n_p:>8,}{n_g:>7.1f}{icc:>7.3f}" + "".join(f"{spearman_brown(icc, n):>7.3f}" for n in (3, 5, 8, 12))
              + f"   {half:.3f} (R({n_g / 2:.1f}) = {spearman_brown(icc, n_g / 2):.3f})")

    d = candidates(player_cuts(df, sched, through), rel, np.random.default_rng(11))
    print(f"\n(2-4) {len(d):,} player-cuts ({d['season'].nunique()} seasons x cuts {CUTS}); {d['t2'].notna().sum():,} with 3+ games left")
    for target in ("t1", "t2"):
        table(d, target, "bucket")
        table(d, target, "family")
    gl = d[(d["share"] > 0.5) & (d["share"] <= 0.75) & (d["n"] >= 4)]
    print(f"\n  Gaines-like players (share 0.5-0.75, 4+ games at the cut): {len(gl):,}")
    for target in ("t1", "t2"):
        g = gl[gl[target].notna()]
        print(f"    against {target}: " + "  ".join(f"{c} MAE {np.mean(np.abs(g[c] - g[target])):.2f} bias {np.mean(g[c] - g[target]):+.2f}"
                                             for c in ("L0", "L1", "L2", "L3", "L0p")))
    top = d[d["L0"] >= 85]
    print(f"\n  cost at the top: player-cuts rated 85+ today ({len(top):,}): later level minus today {np.nanmean(top['t2'] - top['L0']):+.2f}, "
          f"final minus today {np.mean(top['t1'] - top['L0']):+.2f}")
    curves = fit_curves(d)
    print()
    print("L3 curves fitted on all five seasons (family, games played) -> (a, b), the table scripts/07 PART_SEASON_CURVE holds:")
    print("PART_SEASON_CURVE = {")
    for (fam, gb), (a, b) in sorted(curves.items()):
        print(f'    ("{fam}", "{gb}"): ({a:.4f}, {b:.4f}),')
    print("}")
    if args.gaines_sandbox:
        cases(args.gaines_sandbox, rel, curves)


if __name__ == "__main__":
    main()
