"""The power rating that ranks teams: our own team rating, a measured conference level, and results.

The team rating (script 10) says how good a team's players and stats are. It never looks at a scoreboard,
so ranked alone it put LSU first with two losses and Kansas State eighth at 3-3. A ranking has to answer
"how good are they" and "what have they done", so power has three parts, each a number of points per
game above an average FBS team, and they add:

  rating part      BETA * (team rating - mean team rating). What the team's players and stats say it should do.
  conference part  how much better or worse the team's CONFERENCE has played than its ratings say, from its
                   out-of-conference results. It is switched OFF (LAM_CONF): scored, it changes the result by
                   0.1 point of accuracy and 0.06 points of margin error, and a conference has too few
                   out-of-conference games early in a season to measure it steadily. The part stays in the
                   output, always 0, so turning it on is a constant, not a rewrite.
  results part     how much better or worse the team has played than its rating and conference predict. Each
                   game is worth its margin (capped at +-CAP) plus BONUS for a win or minus BONUS for a loss, and
                   is read against what the two teams' ratings and conferences expected. A win over a team the
                   ratings call weak, or a loss to one they call strong, moves the result part by what that
                   surprise was, not by the final score alone. Shrunk toward 0 for a team with few games (LAM).

So the schedule enters only through our own ratings and what the conferences have really done. A loss counts
(minus BONUS and the margin against the expectation), and an unbeaten team is not ranked above a team that has
played better. The constants are derived once by scripts/validate_rankings.py on 2016-2025 (leave-one-season-out,
cut after week 8, scored on every later FBS game).

Only regular-season games between two FBS teams are used.
"""

import numpy as np

CAP = 28.0            # margin cap, points
BONUS = 8.0           # points added to a win's value and taken from a loss's
LAM = 2.0             # ridge on a team's results part: about two games of prior
LAM_CONF = 1e9        # a conference level is OFF: measured, it adds nothing the ratings and results do not (see validate_rankings.py)
BETA = 0.98           # points per game per rating point (the slope of margin on rating gap, fitted)


def _clean(games, team_ids):
    """Games as (home, away, margin, neutral, week) for finished games between two teams in `team_ids`."""
    ids_ok = set(int(t) for t in team_ids)
    out = []
    for g in games:
        h, a = g.get("home_team_id"), g.get("away_team_id")
        hs, as_ = g.get("home_score"), g.get("away_score")
        if h is None or a is None or hs is None or as_ is None:
            continue
        try:
            if hs != hs or as_ != as_ or h != h or a != a:      # NaN
                continue
            h, a = int(h), int(a)
        except (TypeError, ValueError):
            continue
        if h in ids_ok and a in ids_ok:
            wk = g.get("week")
            out.append((h, a, float(hs) - float(as_), bool(g.get("neutral_site") == True),  # noqa: E712
                        int(wk) if wk is not None and wk == wk else None))
    return out


def game_value(margin: float, cap: float = CAP, bonus: float = BONUS) -> float:
    """What a game is worth to the home team: the margin capped at +-cap, plus bonus for a win, minus it for a loss."""
    sign = 1.0 if margin > 0 else -1.0 if margin < 0 else 0.0
    return max(-cap, min(cap, margin)) + bonus * sign


def power_components(games, quality, conf_of, cap=CAP, bonus=BONUS, lam=LAM, lam_conf=LAM_CONF, beta=BETA):
    """Power and its parts for every team that has a rating.

    `quality` is {team_id: team rating}; `conf_of(team_id)` gives its conference (or None). Returns
    (teams, info) where teams[team_id] has power, rating_part, conf_part, results_part, games, wins,
    losses; and info has home_edge and the conference levels. A team with no games has results_part 0.
    The keyword arguments exist so the validation script can score variants; production uses the constants.
    """
    ids = sorted(int(t) for t in quality)
    qm = float(np.mean([quality[t] for t in ids])) if ids else 0.0
    prior = {t: beta * (float(quality[t]) - qm) for t in ids}
    gs = _clean(games, ids)
    confs = sorted({conf_of(t) for t in ids if conf_of(t)})
    n, nc = len(ids), len(confs)
    ix = {t: i for i, t in enumerate(ids)}
    cx = {c: n + i for i, c in enumerate(confs)}
    teams = {t: dict(rating_part=prior[t], conf_part=0.0, results_part=0.0, games=0, wins=0, losses=0) for t in ids}
    home_edge = 0.0
    conf_level = {}
    if gs:
        A = np.zeros((len(gs), n + nc + 1))
        y = np.zeros(len(gs))
        for k, (h, a, m, neutral, _wk) in enumerate(gs):
            A[k, ix[h]] += 1.0
            A[k, ix[a]] -= 1.0
            ch, ca = conf_of(h), conf_of(a)
            if ch in cx:
                A[k, cx[ch]] += 1.0
            if ca in cx:
                A[k, cx[ca]] -= 1.0
            A[k, n + nc] = 0.0 if neutral else 1.0
            y[k] = game_value(m, cap, bonus) - (prior[h] - prior[a])
            for t, won in ((h, m > 0), (a, m < 0)):
                teams[t]["games"] += 1
                teams[t]["wins"] += int(won)
                teams[t]["losses"] += int(m != 0 and not won)
        reg = np.zeros(n + nc + 1)
        reg[:n] = lam
        reg[n:n + nc] = lam_conf
        reg[n + nc] = 1e-6            # the home edge is not shrunk (a hair keeps an all-neutral set solvable)
        sol = np.linalg.solve(A.T @ A + np.diag(reg), A.T @ y)
        home_edge = float(sol[n + nc])
        conf_level = {c: float(sol[cx[c]]) for c in confs}
        for t in ids:
            teams[t]["results_part"] = float(sol[ix[t]])
            teams[t]["conf_part"] = conf_level.get(conf_of(t), 0.0)
    for t in ids:
        p = teams[t]
        p["power"] = p["rating_part"] + p["conf_part"] + p["results_part"]
    return teams, {"home_edge": home_edge, "conference_levels": conf_level, "mean_rating": qm}


def expected_value(parts_home: dict, parts_away: dict, home_edge: float, neutral: bool) -> float:
    """What a game was expected to be worth to the home team from the two teams' rating and conference parts alone."""
    return (parts_home["rating_part"] + parts_home["conf_part"]) - (parts_away["rating_part"] + parts_away["conf_part"]) \
        + (0.0 if neutral else home_edge)
