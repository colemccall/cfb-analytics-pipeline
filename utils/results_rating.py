"""A results rating and the power rating that ranks teams.

The team rating (script 10) says how good a team's players and stats are. It never looks at a
scoreboard, so on its own it will rank a team with two losses above one that has not lost. A ranking
has to answer both "how good are they" and "what have they done", so this module adds the second:

  results (SRS)  an opponent-adjusted scoring-margin rating from the season's games. Each game says
                 margin = rating(home) - rating(away) + home edge, margins capped at +-CAP so a 60-point
                 blowout counts as a 28-point one, and the system is solved by ridge least squares, which
                 pulls a team with few games toward the average. It is schedule-aware by construction:
                 beating a good team moves a rating more than beating a bad one.

  power          points per game above an average FBS team:
                     power = W_QUALITY * (team rating - mean team rating) + W_RESULTS * results
                 W_QUALITY and W_RESULTS are fitted by scripts/validate_rankings.py on 2016-2025
                 (leave-one-season-out, cut after week 8, scored on every later FBS game), with the
                 PREVIOUS season's rating as the quality reading. They are constants here, derived once.

Only regular-season games between two FBS teams are used. Nothing here reads SP+.
"""

import numpy as np

CAP = 28.0            # margin cap, points
LAM = 4.0             # ridge strength: roughly "four games of prior" per team
W_QUALITY = 0.408     # points of margin per point of team rating
W_RESULTS = 1.303     # points of margin per point of results rating


def srs_ratings(games, team_ids, cap: float = CAP, lam: float = LAM):
    """Opponent-adjusted margin ratings for the teams in `games`.

    `games` is any iterable of dicts (or a DataFrame's .to_dict("records")) with home_team_id,
    away_team_id, home_score, away_score and neutral_site. Games involving a team outside
    `team_ids`, or without both scores, are ignored. Returns ({team_id: rating}, home_edge,
    {team_id: games_counted}). A team with no games is absent from the first and third.
    """
    ids_ok = set(int(t) for t in team_ids)
    gs = []
    for g in games:
        h, a = g.get("home_team_id"), g.get("away_team_id")
        hs, as_ = g.get("home_score"), g.get("away_score")
        if h is None or a is None or hs is None or as_ is None:
            continue
        try:
            if hs != hs or as_ != as_:      # NaN
                continue
            h, a = int(h), int(a)
        except (TypeError, ValueError):
            continue
        if h in ids_ok and a in ids_ok:
            gs.append((h, a, float(hs) - float(as_), bool(g.get("neutral_site") == True)))  # noqa: E712
    if not gs:
        return {}, 0.0, {}
    ids = sorted({g[0] for g in gs} | {g[1] for g in gs})
    ix = {t: i for i, t in enumerate(ids)}
    n = len(ids)
    A = np.zeros((len(gs), n + 1))
    y = np.zeros(len(gs))
    count = {t: 0 for t in ids}
    for k, (h, a, m, neutral) in enumerate(gs):
        A[k, ix[h]] = 1.0
        A[k, ix[a]] = -1.0
        A[k, n] = 0.0 if neutral else 1.0
        y[k] = max(-cap, min(cap, m))
        count[h] += 1
        count[a] += 1
    reg = lam * np.eye(n + 1)
    reg[n, n] = 1e-6                      # the home edge is not shrunk (a hair of ridge keeps an all-neutral set solvable)
    sol = np.linalg.solve(A.T @ A + reg, A.T @ y)
    return {t: float(sol[ix[t]]) for t in ids}, float(sol[n]), count


def power_rating(quality, results, mean_quality):
    """Points per game above an average FBS team, or None when there is no team rating.

    A team with no results yet (a projected season, or none counted) is ranked on quality alone,
    which is the honest reading of "we know how good its players are and nothing else".
    """
    if quality is None or mean_quality is None:
        return None
    return W_QUALITY * (float(quality) - float(mean_quality)) + W_RESULTS * (float(results) if results is not None else 0.0)
