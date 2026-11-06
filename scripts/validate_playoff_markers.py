"""The pre-playoff marker test: what did our ratings say the day the 12-team field was picked, and how did the playoff go? Read-only.

2024 and 2025 are the owner's markers of "the current state of football". This asks what each rating said at the end of the REGULAR
season only (the weekly replay's week-15 snapshot, script 17b, which sees no game after that week and no postseason total) and
scores it against what happened:

  (1) Selection   how many of the committee's 12 were in each reading's top 12; where the champion ranked; how closely the reading
                  orders the 12 the way the committee did (Spearman on the committee's own rank). Then the same under the format's
                  rule: the five best conference champions by the reading get in automatically, then its seven best other teams
                  (the committee's rule, with the reading in place of the committee's ranking).
  (2) The playoff  each of the 22 games between two playoff teams (every CFP game): did the higher-rated team win?
  (3) The bowls    the same for every other postseason game between two rated FBS teams, and for the games played at a neutral site
                  the error of our power difference as a margin against the closing spread's. Secondary: opt-outs and the portal make
                  a bowl a worse test than a playoff game, and n is small.

Five readings, as shipped: the headline rating (head), power, and the three readings the headline is built from (results, roster,
stats). Three baselines that use no rating of ours: the committee's seed (higher seed wins), the regular-season record, and the
closing spread's favourite. Two placebos: ratings dealt to the wrong teams (exact for selection, a permutation for games).

24 playoff team-seasons and 22 games are an anecdote, not a gate. This reports them with their intervals and their misses beside
their hits; any pass or fail rule is the owner's to set. Nothing here changes a rating or a constant.

    python scripts/validate_playoff_markers.py
    python scripts/validate_playoff_markers.py --write-doc docs/PLAYOFF_MARKERS_2024_2025.md
"""

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import hypergeom, spearmanr

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.store import fill_shutouts  # noqa: E402

SEASONS = (2024, 2025)
SNAPSHOT_WEEK = 15                      # the conference championships; the committee picks the next day. Army-Navy (week 16) is after.
READINGS = ("head", "power", "results", "roster", "stats")
LABEL = {"head": "headline", "power": "power", "results": "results", "roster": "roster", "stats": "stats",
         "record": "record", "prior": "last season", "seed": "committee seed", "market": "closing spread"}
FIELD = 12
AUTO_BIDS = 5                           # the five highest-ranked conference champions get in (2024 and 2025 formats)
PERMUTATIONS = 5000
RNG_SEED = 20241207


# ── data ──────────────────────────────────────────────────────────────────────

def weekly_teams(season: int) -> dict:
    """The season's weekly team snapshots ({team_id: {school, weeks: [...]}}), from this repo's computed copy or the app's export."""
    for base in (ROOT / "data" / "computed", ROOT.parent / "cfb-analytics-app" / "data"):
        p = base / f"weekly_teams_{season}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))["teams"]
    raise FileNotFoundError(f"weekly_teams_{season}.json: run scripts/17b_weekly_progression.py --season {season} --through-week 15")


def snapshot(season: int, week: int = SNAPSHOT_WEEK) -> pd.DataFrame:
    """One row per team at `week`: record and the five readings. A team with no row that week is absent."""
    rows = []
    for tid, t in weekly_teams(season).items():
        w = next((x for x in t["weeks"] if x["week"] == week), None)
        if w is None:
            continue
        rows.append({"team_id": int(tid), "school": t["school"], "w": w.get("w"), "l": w.get("l"),
                     **{r: w.get(r) for r in READINGS}})
    df = pd.DataFrame(rows)
    df["record"] = [(w - l) / (w + l) + 1e-6 * w if pd.notna(w) and (w + l) else np.nan for w, l in zip(df["w"], df["l"])]
    return df


def prior_ratings(season: int) -> dict:
    """{team_id: last season's published team rating}: what the preseason knew. Empty when there is no earlier season."""
    p = ROOT / "data" / "computed" / "team_ratings.json"
    if not p.exists():
        p = ROOT.parent / "cfb-analytics-app" / "data" / "team_ratings.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    rows = rows if isinstance(rows, list) else list(rows.values())
    return {int(r["team_id"]): r.get("overall_rating") for r in rows if r.get("season") == season - 1 and r.get("overall_rating") is not None}


def cfp_field(season: int) -> pd.DataFrame:
    """The 12 selected teams with seed, the committee's rank and the outcome (data/raw/cfp_participants.json)."""
    rows = json.loads((ROOT / "data" / "raw" / "cfp_participants.json").read_text(encoding="utf-8"))
    return pd.DataFrame([r for r in rows if r["season"] == season])


def conference_champions(season: int) -> set:
    """Winners of the season's conference championship games: week-15 regular-season games between two teams of one conference."""
    g = fill_shutouts(pd.DataFrame(json.loads((ROOT / "data" / "raw" / "games.json").read_text(encoding="utf-8"))))
    rec = pd.DataFrame(json.loads((ROOT / "data" / "raw" / "team_records.json").read_text(encoding="utf-8")))
    rec = rec[rec["season"] == season].dropna(subset=["team_id", "conference"])
    conf = {int(t): c for t, c in zip(rec["team_id"], rec["conference"])}
    g = g[(g["season"] == season) & (g["season_type"] == "regular") & (g["week"] == SNAPSHOT_WEEK)
          & g["home_score"].notna() & g["away_score"].notna() & g["home_team_id"].notna() & g["away_team_id"].notna()]
    out = set()
    for r in g.itertuples():
        h, a = int(r.home_team_id), int(r.away_team_id)
        if conf.get(h) and conf.get(h) == conf.get(a) and r.home_score != r.away_score:
            out.add(h if r.home_score > r.away_score else a)
    return out


def postseason_games(season: int) -> pd.DataFrame:
    """Decided postseason games between two teams with ids, scores restored for shutouts (utils.store.fill_shutouts)."""
    g = fill_shutouts(pd.DataFrame(json.loads((ROOT / "data" / "raw" / "games.json").read_text(encoding="utf-8"))))
    g = g[(g["season"] == season) & (g["season_type"] == "postseason") & g["home_team_id"].notna() & g["away_team_id"].notna()
          & g["home_score"].notna() & g["away_score"].notna() & (g["home_score"] != g["away_score"])].copy()
    for c in ("home_team_id", "away_team_id"):
        g[c] = g[c].astype(int)
    g["margin"] = g["home_score"] - g["away_score"]
    g["winner"] = np.where(g["margin"] > 0, g["home_team_id"], g["away_team_id"])
    return g.reset_index(drop=True)


def closing_spreads(season: int) -> dict:
    """{cfbd game id: median spread across providers}. The spread is the HOME team's: negative means the home team is favoured."""
    b = pd.DataFrame(json.loads((ROOT / "data" / "raw" / "betting_lines.json").read_text(encoding="utf-8")))
    b = b[(b["season"] == season) & b["spread"].notna()]
    return b.groupby("game_cfb_id")["spread"].median().to_dict()


# ── scoring (pure: these are what the tests exercise) ─────────────────────────

def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    """The Wilson 95% interval for k of n. (0, 1) for n = 0."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def rank_of(values: pd.Series) -> pd.Series:
    """1 = highest; ties share the better rank; missing stays missing."""
    return values.rank(ascending=False, method="min")


def selection(snap: pd.DataFrame, field_ids: set, reading: str, k: int = FIELD) -> dict:
    """How many of the selected teams are in the reading's top k, and which teams are on each side of the line."""
    v = snap.dropna(subset=[reading]).copy()
    v["rk"] = rank_of(v[reading])
    top = set(v.loc[v["rk"] <= k, "team_id"])
    return {"hits": len(top & field_ids), "top": top, "missed": sorted(field_ids - top), "extra": sorted(top - field_ids),
            "rank": dict(zip(v["team_id"], v["rk"]))}


def format_selection(snap: pd.DataFrame, field_ids: set, champions: set, reading: str, k: int = FIELD, auto: int = AUTO_BIDS) -> dict:
    """The field the format would pick with this reading as its ranking: its `auto` best conference champions, then its best others.

    Ties are broken by team id, so this is not used for the record (whose values tie often)."""
    v = snap.dropna(subset=[reading]).sort_values([reading, "team_id"], ascending=[False, True], kind="mergesort")
    order = [int(t) for t in v["team_id"]]
    champs = [t for t in order if t in champions][:auto]
    rest = [t for t in order if t not in champs][:k - len(champs)]
    picked = set(champs) | set(rest)
    return {"hits": len(picked & field_ids), "missed": sorted(field_ids - picked), "extra": sorted(picked - field_ids), "auto": champs}


def order_agreement(snap: pd.DataFrame, field: pd.DataFrame, reading: str) -> float:
    """Spearman between the reading's rank and the committee's rank over the selected teams (1.0 = the committee's order)."""
    rk = selection(snap, set(field["team_id"]), reading)["rank"]
    pairs = [(rk[t], c) for t, c in zip(field["team_id"], field["committee_rank"]) if t in rk and pd.notna(c)]
    if len(pairs) < 3:
        return float("nan")
    return float(spearmanr([a for a, _ in pairs], [b for _, b in pairs]).statistic)


def pick(a, b, home_id: int, away_id: int):
    """The home or away id the values favour, or None when either is missing or they are equal. Higher is better."""
    if a is None or b is None or pd.isna(a) or pd.isna(b) or a == b:
        return None
    return home_id if a > b else away_id


def accuracy(games: pd.DataFrame, value: dict) -> tuple:
    """(correct, picked): the higher `value` (a {team_id: number}) picks the winner."""
    ok = n = 0
    for r in games.itertuples():
        p = pick(value.get(r.home_team_id), value.get(r.away_team_id), r.home_team_id, r.away_team_id)
        if p is not None:
            n += 1
            ok += int(p == r.winner)
    return ok, n


def market_accuracy(games: pd.DataFrame, spreads: dict) -> tuple:
    """(correct, picked): the closing spread's favourite wins. A pick'em or a game with no line is not a pick."""
    ok = n = 0
    for r in games.itertuples():
        s = spreads.get(r.cfb_api_id)
        if s is None or s == 0 or pd.isna(s):
            continue
        n += 1
        ok += int((r.home_team_id if s < 0 else r.away_team_id) == r.winner)
    return ok, n


def placebo_games(games_by_season: dict, value_by_season: dict, trials: int = PERMUTATIONS, seed: int = RNG_SEED) -> np.ndarray:
    """Correct picks over all the games when each season's ratings are dealt at random among the teams that played in them."""
    rng = np.random.default_rng(seed)
    out = np.zeros(trials, dtype=int)
    prepared = []
    for s, g in games_by_season.items():
        ids = sorted(set(g["home_team_id"]) | set(g["away_team_id"]))
        vals = np.array([value_by_season[s].get(t, np.nan) for t in ids], dtype=float)
        prepared.append((g, ids, vals))
    for i in range(trials):
        total = 0
        for g, ids, vals in prepared:
            deal = dict(zip(ids, rng.permutation(vals)))
            total += accuracy(g, deal)[0]
        out[i] = total
    return out


def margin_errors(games: pd.DataFrame, power: dict, spreads: dict) -> dict:
    """On neutral-site games with both: the error of our power difference as a margin, and of the closing spread, against the real one."""
    rows = []
    for r in games[games["neutral_site"] == True].itertuples():  # noqa: E712
        s = spreads.get(r.cfb_api_id)
        ph, pa = power.get(r.home_team_id), power.get(r.away_team_id)
        if s is None or pd.isna(s) or ph is None or pa is None or pd.isna(ph) or pd.isna(pa):
            continue
        rows.append((r.margin, ph - pa, -s))
    if not rows:
        return {}
    m, o, k = (np.array(c, dtype=float) for c in zip(*rows))
    corr = lambda a, b: float(np.corrcoef(a, b)[0, 1]) if len(a) >= 3 and a.std() > 0 and b.std() > 0 else float("nan")   # noqa: E731
    return {"n": len(rows), "ours_rmse": float(np.sqrt(np.mean((o - m) ** 2))), "mkt_rmse": float(np.sqrt(np.mean((k - m) ** 2))),
            "ours_mae": float(np.mean(np.abs(o - m))), "mkt_mae": float(np.mean(np.abs(k - m))),
            "ours_bias": float(np.mean(o - m)), "mkt_bias": float(np.mean(k - m)),
            "ours_corr": corr(o, m), "mkt_corr": corr(k, m),
            "closer": int(np.sum(np.abs(o - m) < np.abs(k - m)))}


# ── the test ──────────────────────────────────────────────────────────────────

def run() -> dict:
    """Everything the report prints, as one dictionary (so a test can read it)."""
    res = {"seasons": {}, "games": [], "bowls": []}
    cfp_games, all_games, values = {}, {}, {}
    for s in SEASONS:
        snap = snapshot(s)
        field = cfp_field(s)
        ids = set(field["team_id"])
        prior = prior_ratings(s)
        snap["prior"] = snap["team_id"].map(prior)
        champion = int(field.loc[field["outcome"] == "champion", "team_id"].iloc[0])
        sel = {}
        for r in (*READINGS, "record", "prior"):
            x = selection(snap, ids, r)
            sel[r] = {"hits": x["hits"], "missed": x["missed"], "extra": x["extra"], "champion_rank": x["rank"].get(champion),
                      "order": order_agreement(snap, field, r) if r in READINGS else float("nan"), "n_rated": len(x["rank"])}
        champs = conference_champions(s)
        fmt = {r: format_selection(snap, ids, champs, r) for r in (*READINGS, "prior")}
        g = postseason_games(s)
        in_field = g[g["home_team_id"].isin(ids) & g["away_team_id"].isin(ids)]
        spreads = closing_spreads(s)
        value = {r: dict(zip(snap["team_id"], snap[r])) for r in (*READINGS, "record", "prior")}
        value["seed"] = {int(t): -float(sd) for t, sd in zip(field["team_id"], field["seed"])}
        cfp_games[s], all_games[s], values[s] = in_field, g, value
        res["seasons"][s] = {"snap": snap, "field": field, "selection": sel, "format": fmt, "champions": champs, "champion": champion, "spreads": spreads,
                             "n_teams": len(snap), "n_cfp_games": len(in_field), "n_postseason": len(g)}
    # the 22 games
    def total(games_by_season, key):
        ok = n = 0
        for s, g in games_by_season.items():
            a, b = accuracy(g, values[s][key])
            ok, n = ok + a, n + b
        return ok, n
    res["cfp"] = {k: total(cfp_games, k) for k in (*READINGS, "record", "prior", "seed")}
    mk = [market_accuracy(g, res["seasons"][s]["spreads"]) for s, g in cfp_games.items()]
    res["cfp"]["market"] = (sum(a for a, _ in mk), sum(b for _, b in mk))
    res["cfp_placebo"] = {k: placebo_games(cfp_games, {s: values[s][k] for s in SEASONS}) for k in READINGS}
    # every other postseason game (the bowls), same scoring
    bowls = {s: all_games[s][~all_games[s]["id"].isin(cfp_games[s]["id"])] for s in SEASONS}
    rated = {s: set(res["seasons"][s]["snap"]["team_id"]) for s in SEASONS}
    bowls = {s: g[g["home_team_id"].isin(rated[s]) & g["away_team_id"].isin(rated[s])] for s, g in bowls.items()}
    def tot_b(key):
        ok = n = 0
        for s, g in bowls.items():
            a, b = accuracy(g, values[s][key])
            ok, n = ok + a, n + b
        return ok, n
    res["bowls"] = {k: tot_b(k) for k in (*READINGS, "record", "prior")}
    mb = [market_accuracy(g, res["seasons"][s]["spreads"]) for s, g in bowls.items()]
    res["bowls"]["market"] = (sum(a for a, _ in mb), sum(b for _, b in mb))
    res["bowl_games"] = bowls
    res["cfp_games"] = cfp_games
    res["margin"] = {}
    for label, games in (("playoff", cfp_games), ("bowls", bowls)):
        parts = [margin_errors(g, values[s]["power"], res["seasons"][s]["spreads"]) for s, g in games.items()]
        parts = [p for p in parts if p]
        if parts:
            n = sum(p["n"] for p in parts)
            res["margin"][label] = {k: (sum(p[k] * p["n"] for p in parts) / n if k != "closer" else sum(p[k] for p in parts)) for k in parts[0] if k != "n"} | {"n": n}
    res["values"] = values
    return res


def pct(k: int, n: int) -> str:
    """'k/n (p%, 95% lo-hi%)' for a count of correct picks."""
    if n == 0:
        return "-"
    lo, hi = wilson(k, n)
    return f"{k}/{n} ({100 * k / n:.0f}%, {100 * lo:.0f}-{100 * hi:.0f}%)"


def report(res: dict) -> list:
    """The report as markdown lines (printed, and written when --write-doc is given)."""
    out = []
    P = out.append
    P("## 1. Selection: how many of the committee's 12 were in each reading's top 12")
    P("")
    P("| Reading | " + " | ".join(f"{s} hits | {s} champion rank | {s} order (Spearman)" for s in SEASONS) + " |")
    P("|---|" + "---|---|---|" * len(SEASONS))
    for r in (*READINGS, "record", "prior"):
        cells = []
        for s in SEASONS:
            x = res["seasons"][s]["selection"][r]
            cells += [f"{x['hits']} of {FIELD}", "-" if x["champion_rank"] is None else f"{int(x['champion_rank'])} of {x['n_rated']}",
                      "-" if math.isnan(x["order"]) else f"{x['order']:+.2f}"]
        P(f"| {LABEL[r]} | " + " | ".join(cells) + " |")
    ns = " and ".join(str(res["seasons"][s]["n_teams"]) for s in SEASONS)
    n_mean = int(round(np.mean([res["seasons"][s]["n_teams"] for s in SEASONS])))
    chance = FIELD * FIELD / n_mean
    P("")
    P(f"The placebo is exact: dealing ratings to teams at random makes a reading's top {FIELD} a random {FIELD} of the {ns} teams rated, "
      f"which holds {chance:.1f} of the committee's {FIELD} on average (hypergeometric; 8 or more of {FIELD} has a probability of "
      f"{hypergeom(n_mean, FIELD, FIELD).sf(7):.1e} with {n_mean} teams). 'Order' is the Spearman correlation of the reading's rank with the committee's rank over the {FIELD} selected.")
    wide = [(LABEL[r], s, res["seasons"][s]["selection"][r]["hits"] + len(res["seasons"][s]["selection"][r]["extra"]))
            for r in (*READINGS, "record", "prior") for s in SEASONS]
    wide = [w for w in wide if w[2] != FIELD]
    if wide:
        P("")
        P(f"Ties share the better rank, so a top {FIELD} can hold more than {FIELD} teams, and a bigger set holds more of the field by chance: "
          + "; ".join(f"{lab} {s}: {n} teams" for lab, s, n in wide) + f". Every other top {FIELD} above is exactly {FIELD}.")
    P("")
    P(f"### Under the format: each reading's {AUTO_BIDS} best conference champions get in, then its {FIELD - AUTO_BIDS} best other teams")
    P("")
    P("The committee's rule with the reading in place of the committee's ranking, so a reading is not marked down for a champion the rule "
      "admits whatever anyone rates it. Conference champions are the winners of the week-15 title games ("
      + " and ".join(f"{len(res['seasons'][s]['champions'])} in {s}" for s in SEASONS)
      + "). The record is left out: its values tie too often for a top-12 cut to mean one thing.")
    P("")
    P("| Reading | " + " | ".join(f"{s} hits | {s} its automatic five | {s} missed | {s} picked instead" for s in SEASONS) + " |")
    P("|---|" + "---|---|---|---|" * len(SEASONS))
    for r in (*READINGS, "prior"):
        cells = []
        for s in SEASONS:
            info = res["seasons"][s]
            names = dict(zip(info["snap"]["team_id"], info["snap"]["school"]))
            x = info["format"][r]
            nm = lambda ids: ", ".join(names.get(t, str(t)) for t in ids) or "none"   # noqa: E731
            cells += [f"{x['hits']} of {FIELD}", nm(x["auto"]), nm(x["missed"]), nm(x["extra"])]
        P(f"| {LABEL[r]} | " + " | ".join(cells) + " |")
    P("")
    for s in SEASONS:
        info = res["seasons"][s]
        names = dict(zip(info["snap"]["team_id"], info["snap"]["school"]))
        P(f"### {s}: the misses, by reading")
        P("")
        for r in READINGS:
            x = info["selection"][r]
            P(f"- **{LABEL[r]}:** selected but outside its top {FIELD}: {', '.join(names.get(t, str(t)) for t in x['missed']) or 'none'}; "
              f"in its top {FIELD} but not selected: {', '.join(names.get(t, str(t)) for t in x['extra']) or 'none'}.")
        P("")
    P("## 2. The playoff: in the 22 games between two playoff teams, did the higher-rated team win?")
    P("")
    P("| Reading | Correct | vs 11 of 22 by chance (placebo mean, P of this many or more) |")
    P("|---|---|---|")
    for r in (*READINGS, "record", "prior", "seed", "market"):
        k, n = res["cfp"][r]
        plc = res["cfp_placebo"].get(r)
        extra = "" if plc is None else f"{plc.mean():.1f} of 22; {np.mean(plc >= k):.3f}"
        P(f"| {LABEL[r]}{' (baseline)' if r in ('record', 'prior', 'seed', 'market') else ''} | {pct(k, n)} | {extra} |")
    P("")
    P("Each game, with each reading's pick (a check mark is a correct pick, a cross a miss, a dash no pick):")
    P("")
    P("| Season | Date | Winner | Loser | Score | " + " | ".join(LABEL[r] for r in (*READINGS, "record", "seed")) + " | spread |")
    P("|---|---|---|---|---|" + "---|" * (len(READINGS) + 3))
    for s in SEASONS:
        info = res["seasons"][s]
        names = dict(zip(info["snap"]["team_id"], info["snap"]["school"]))
        for g in res["cfp_games"][s].sort_values("game_date").itertuples():
            win, lose = (g.home_team_id, g.away_team_id) if g.margin > 0 else (g.away_team_id, g.home_team_id)
            cells = []
            for r in (*READINGS, "record", "seed"):
                p = pick(res["values"][s][r].get(g.home_team_id), res["values"][s][r].get(g.away_team_id), g.home_team_id, g.away_team_id)
                cells.append("-" if p is None else ("✓" if p == g.winner else "✗"))
            sp = info["spreads"].get(g.cfb_api_id)
            fav = "-" if sp is None or sp == 0 else ("✓" if (g.home_team_id if sp < 0 else g.away_team_id) == g.winner else "✗")
            hs, as_ = int(max(g.home_score, g.away_score)), int(min(g.home_score, g.away_score))
            P(f"| {s} | {g.game_date[:10]} | {names.get(win, win)} | {names.get(lose, lose)} | {hs}-{as_} | " + " | ".join(cells) + f" | {fav} |")
    P("")
    P("## 3. The bowls: every other postseason game between two rated FBS teams")
    P("")
    P("| Reading | Correct |")
    P("|---|---|")
    for r in (*READINGS, "record", "prior", "market"):
        k, n = res["bowls"][r]
        P(f"| {LABEL[r]}{' (baseline)' if r in ('record', 'prior', 'market') else ''} | {pct(k, n)} |")
    P("")
    P("Opt-outs and transfers move a bowl's rosters after the snapshot, and no reading knows who is not playing: a bowl is a weaker test than a playoff game.")
    P("")
    P("## 4. Margins: our power difference against the closing spread, neutral-site games, with a line")
    P("")
    P("| Games | n | Our RMSE | Spread RMSE | Our MAE | Spread MAE | Our bias | Spread bias | Our corr. | Spread corr. | Ours closer |")
    P("|---|---|---|---|---|---|---|---|---|---|---|")
    for label in ("playoff", "bowls"):
        m = res["margin"].get(label)
        if m:
            P(f"| {label} | {m['n']} | {m['ours_rmse']:.1f} | {m['mkt_rmse']:.1f} | {m['ours_mae']:.1f} | {m['mkt_mae']:.1f} | {m['ours_bias']:+.1f} | {m['mkt_bias']:+.1f} | "
              f"{m['ours_corr']:.2f} | {m['mkt_corr']:.2f} | {int(m['closer'])} of {m['n']} |")
    P("")
    P("Power is in points per game above an average team, so the difference of two powers is a margin. A bias is the mean of prediction minus result for the home side as listed.")
    return out


def main() -> int:
    """Print the report; optionally write it as a document."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write-doc", help="also write the report as a markdown document at this path")
    args = ap.parse_args()
    res = run()
    lines = report(res)
    print("\n".join(lines))
    if args.write_doc:
        head = ["# The pre-playoff markers, 2024 and 2025", "",
                f"Generated by `scripts/validate_playoff_markers.py` from the weekly replay's week-{SNAPSHOT_WEEK} snapshot (regular season only), "
                "the CFP field, every postseason game and the closing spreads. Read-only: no rating or constant changed. "
                "24 playoff team-seasons and 22 games are an anecdote, not a gate; any pass or fail rule is the owner's to set.", "",
                "The replays were rebuilt in v4.25.1 after this test found each finished season's postseason visible to every week's team "
                "readings; `RATINGS_SPEC.md` §8 has these results beside the ones the leaky replays gave.", ""]
        Path(args.write_doc).write_text("\n".join(head + lines) + "\n", encoding="utf-8")
        print(f"\nwrote {args.write_doc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
