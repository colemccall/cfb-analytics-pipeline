"""Opponent strength from our own game results: an offense and a defense rating per team, no outside numbers.

Used by script 06 to adjust every game a player played for how good his opponent was, in place of SP+. A
team's offense and defense are solved together, for all teams at once, from the points scored in every game of
the season, by a ridge regression with a conference level underneath:

    points(team i vs opponent j) = mu + off_i + dfn_j + home edge          (one row per team per game)
    off_i = conference offense level + team offense deviation             (and the same for defense)

  - OFFENSE and DEFENSE are separate, because a quarterback is judged against the opposing defense and a
    linebacker against the opposing offense. Both are in points per game above an average team.
  - The CONFERENCE LEVEL is estimated from how a conference's teams did against everyone else (cross-conference
    games carry weight CROSS_WEIGHT because they are what separates one league's level from another's), and a
    team's deviation from its conference is ridge-shrunk, so a very good team in a weak league keeps its place
    above a poor team in a strong one but the league's level still counts. The spread inside a conference is
    not flattened.
  - Every game counts, the postseason at POST_WEIGHT (bowls and the playoff are the densest cross-conference
    games, but opt-outs make them noisier). An opponent that is not an FBS team of ours is one pooled FCS team,
    estimated from the FBS-vs-FCS games, instead of an average team.
  - A season in progress is solved on the games played so far; the ridge keeps thin samples near their league.

The constants come from scripts/validate_opponent_strength.py (week-8 cut, 2016-2025, scored on every later
FBS game, leave-one-season-out). Nothing here reads a player rating, so the player ratings can use it without a loop.
"""

import numpy as np

LAM_TEAM = 3.0        # ridge on a team's deviation from its conference, in games of prior
LAM_CONF = 0.5        # ridge on a conference level (weak: leagues really differ)
POST_WEIGHT = 1.0     # weight of a postseason game relative to a regular-season game
CROSS_WEIGHT = 1.5    # weight of a game between two different conferences
FCS = -1              # the pooled team that stands for every non-FBS opponent


def _rows(games, conf_of, post_weight, cross_weight):
    """(home, away, home points, away points, neutral, weight) for each scored game involving at least one FBS team."""
    out = []
    for g in games:
        h, a = g.get("home_team_id"), g.get("away_team_id")
        hs, as_ = g.get("home_score"), g.get("away_score")
        if hs is None or as_ is None or hs != hs or as_ != as_:
            continue
        h = int(h) if h is not None and h == h else None
        a = int(a) if a is not None and a == a else None
        h = h if h is not None and conf_of(h) else FCS
        a = a if a is not None and conf_of(a) else FCS
        if h == FCS and a == FCS:
            continue
        w = post_weight if g.get("season_type") == "postseason" else 1.0
        if h != FCS and a != FCS and conf_of(h) != conf_of(a):
            w *= cross_weight
        out.append((h, a, float(hs), float(as_), bool(g.get("neutral_site") == True), w))   # noqa: E712
    return out


def solve(games, conf_of, team_ids, lam_team=LAM_TEAM, lam_conf=LAM_CONF, post_weight=POST_WEIGHT, cross_weight=CROSS_WEIGHT):
    """Offense and defense strength of every team, in points per game above an average team.

    `games` are dicts with home_team_id, away_team_id, home_score, away_score, neutral_site, season_type;
    `conf_of(team_id)` is a conference name or None (None = not an FBS team of ours); `team_ids` are the teams to
    rate. Returns {team_id: {"off": x, "dfn": y, "conf": name}} where dfn is how many points a team PREVENTS
    (positive = stingy), plus FCS under the key -1. Teams with no games get their conference's level.
    """
    ids = sorted(int(t) for t in team_ids)
    rows = _rows(games, conf_of, post_weight, cross_weight)
    confs = sorted({conf_of(t) for t in ids if conf_of(t)})
    n, nc = len(ids), len(confs)
    ix = {t: i for i, t in enumerate(ids)}
    ix[FCS] = n
    cx = {c: i for i, c in enumerate(confs)}
    nt = n + 1
    # columns: team offense deviation (nt), team defense deviation (nt), conference offense (nc), conference defense (nc), home edge, mean
    p = 2 * nt + 2 * nc + 2
    A = np.zeros((len(rows) * 2, p))
    y = np.zeros(len(rows) * 2)
    w = np.zeros(len(rows) * 2)
    for k, (h, a, hs, as_, neutral, wt) in enumerate(rows):
        for side, (me, opp, pts, home) in enumerate(((h, a, hs, 0 if neutral else 1), (a, h, as_, 0))):
            r = 2 * k + side
            A[r, ix[me]] = 1.0                              # my offense deviation
            A[r, nt + ix[opp]] = 1.0                        # his defense deviation (negative = stingy)
            if me != FCS and conf_of(me) in cx:
                A[r, 2 * nt + cx[conf_of(me)]] = 1.0
            if opp != FCS and conf_of(opp) in cx:
                A[r, 2 * nt + nc + cx[conf_of(opp)]] = 1.0
            A[r, 2 * nt + 2 * nc] = home
            A[r, 2 * nt + 2 * nc + 1] = 1.0
            y[r] = pts
            w[r] = wt
    reg = np.zeros(p)
    reg[:2 * nt] = lam_team
    reg[2 * nt:2 * nt + 2 * nc] = lam_conf
    reg[2 * nt + 2 * nc] = 1e-6
    reg[2 * nt + 2 * nc + 1] = 1e-6
    # The league average is the intercept; the level parameters are penalised toward it.
    if len(rows):
        W = w[:, None]
        sol = np.linalg.solve(A.T @ (A * W) + np.diag(reg), A.T @ (y * w))
    else:
        sol = np.zeros(p)
    off_dev, dfn_dev = sol[:nt], sol[nt:2 * nt]
    c_off, c_dfn = sol[2 * nt:2 * nt + nc], sol[2 * nt + nc:2 * nt + 2 * nc]
    out = {}
    for t in ids:
        c = conf_of(t)
        co = c_off[cx[c]] if c in cx else 0.0
        cd = c_dfn[cx[c]] if c in cx else 0.0
        out[t] = {"off": float(co + off_dev[ix[t]]), "dfn": float(-(cd + dfn_dev[ix[t]])), "conf": c}
    out[FCS] = {"off": float(off_dev[ix[FCS]]), "dfn": float(-dfn_dev[ix[FCS]]), "conf": None}
    # Centre on the FBS teams: an average FBS team is 0 on both sides.
    mo = np.mean([out[t]["off"] for t in ids]) if ids else 0.0
    md = np.mean([out[t]["dfn"] for t in ids]) if ids else 0.0
    for v in out.values():
        v["off"] -= mo
        v["dfn"] -= md
    return out


def schedule_faced(games, conf_of, team_ids, **solve_kw):
    """How hard each team's schedule was, from the same strengths solve() gives, in points per game.

    Returns {team_id: (defenses, offenses)}: the mean `dfn` of the defenses a team faced and the mean `off` of the
    offenses it faced, each CENTRED on the average of `team_ids` that played a game, so 0 is an average schedule and
    +3 is three points per game tougher. A non-FBS opponent (a missing or unknown id) is the pooled FCS team, the
    same as in solve(). A team with no scored game is absent: no schedule is known, and none is invented.
    """
    ids = [int(t) for t in team_ids]
    sol = solve(games, conf_of, ids, **solve_kw)
    faced = {t: ([], []) for t in ids}
    for g in games:
        hs, as_ = g.get("home_score"), g.get("away_score")
        if hs is None or as_ is None or hs != hs or as_ != as_:
            continue
        h, a = g.get("home_team_id"), g.get("away_team_id")
        h = int(h) if h is not None and h == h else FCS
        a = int(a) if a is not None and a == a else FCS
        for me, opp in ((h, a), (a, h)):
            if me in faced:
                v = sol[opp] if opp != FCS and conf_of(opp) and opp in sol else sol[FCS]
                faced[me][0].append(v["dfn"])
                faced[me][1].append(v["off"])
    mean = {t: (float(np.mean(d)), float(np.mean(o))) for t, (d, o) in faced.items() if d}
    if not mean:
        return {}
    cd = float(np.mean([v[0] for v in mean.values()]))
    co = float(np.mean([v[1] for v in mean.values()]))
    return {t: (d - cd, o - co) for t, (d, o) in mean.items()}


def strength_gap(out, home_id, away_id):
    """Expected margin of the home side from the two teams' strengths alone (points): (off_h + dfn_h) - (off_a + dfn_a)."""
    h, a = out.get(home_id), out.get(away_id)
    if h is None or a is None:
        return None
    return (h["off"] + h["dfn"]) - (a["off"] + a["dfn"])
