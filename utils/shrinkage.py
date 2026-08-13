"""Empirical-Bayes shrinkage and intervals for ranked findings.

Every finding on the research page is a ranking, and none of them carried
uncertainty. Rank 2,310 noisy things and the top of the list is selected
substantially for noise: the team at number one is disproportionately likely to
be there because it got lucky, not because it is best. A platform whose pitch is
"we show our work" should not walk into the multiple-comparisons trap.

The fix is the standard one and it is a hundred years old. Each observation is
pulled toward the population mean by an amount that depends on how much evidence
is behind it — a lot for a five-recruit class, almost none for a fifty-recruit
one. What survives shrinkage is signal.

Two estimators here, because the findings come in two shapes:

    shrink_mean   a continuous measurement with a known sampling variance
                  (a team's SP+ residual over n seasons)
    shrink_rate   a proportion out of n trials
                  (a recruiting class's hit rate — k contributors from n recruits)

Both return the shrunk value, an interval, and the effective sample size, so the
UI can show a range instead of a false decimal point. Pure functions, no I/O.
"""

from __future__ import annotations

import math

# 1.2816 = the 90th percentile of the standard normal, so ±1.2816 SE is an 80%
# interval. Matched deliberately to the projection engine's 80% bands so the two
# do not mean different things in the same product.
Z80 = 1.2815515655446004


def _finite(xs) -> list[float]:
    out = []
    for x in xs:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def population_stats(values) -> tuple[float, float]:
    """(mean, standard deviation) over the finite values. SD is 0 when n < 2."""
    v = _finite(values)
    if not v:
        return 0.0, 0.0
    mu = sum(v) / len(v)
    if len(v) < 2:
        return mu, 0.0
    var = sum((x - mu) ** 2 for x in v) / (len(v) - 1)
    return mu, math.sqrt(max(var, 0.0))


def shrink_mean(observed: float, n: int, prior_mean: float, prior_sd: float,
                obs_sd: float) -> dict:
    """Shrink one observed mean toward the population mean.

    The weight is the textbook one: `w = tau² / (tau² + sigma²/n)`, where tau is
    how much teams genuinely differ and sigma is how noisy one observation is.
    When a team has one season of evidence and seasons are noisy, w is small and
    the estimate barely moves off the population mean — which is the honest
    statement, not a defect.

    Returns shrunk value, an 80% interval, and the weight actually applied.
    """
    n = max(int(n or 0), 0)
    if n <= 0 or prior_sd <= 0 or obs_sd <= 0:
        return {"value": round(float(prior_mean), 3), "low": None, "high": None,
                "weight": 0.0, "n": n}
    tau2 = prior_sd ** 2
    se2 = (obs_sd ** 2) / n
    w = tau2 / (tau2 + se2)
    value = w * float(observed) + (1 - w) * float(prior_mean)
    # Posterior SD of the shrunk estimate: sqrt(w · sigma²/n).
    post_sd = math.sqrt(max(w * se2, 0.0))
    return {
        "value":  round(value, 3),
        "low":    round(value - Z80 * post_sd, 3),
        "high":   round(value + Z80 * post_sd, 3),
        "weight": round(w, 4),
        "n":      n,
    }


def shrink_rate(successes: float, trials: float, prior_rate: float,
                prior_strength: float = 25.0) -> dict:
    """Shrink a proportion toward a prior rate, Beta-binomial style.

    `prior_strength` is in units of trials: 25 means the prior is worth 25
    recruits, so a five-recruit class lands near the population rate and a
    forty-recruit class is mostly its own. It is deliberately generous —
    recruiting classes are small and one player swings a raw hit rate by 4-20
    points.

    The interval is Wald on the posterior, which is adequate here because the
    posterior always has at least `prior_strength` pseudo-trials behind it and so
    never sits at the 0-or-1 boundary where Wald falls apart.
    """
    k = max(float(successes or 0), 0.0)
    n = max(float(trials or 0), 0.0)
    a = k + prior_strength * float(prior_rate)
    b = (n - k) + prior_strength * (1.0 - float(prior_rate))
    total = a + b
    if total <= 0:
        return {"value": None, "low": None, "high": None, "weight": 0.0, "n": int(n)}
    p = a / total
    se = math.sqrt(max(p * (1 - p) / total, 0.0))
    return {
        "value":  round(p, 4),
        "low":    round(max(p - Z80 * se, 0.0), 4),
        "high":   round(min(p + Z80 * se, 1.0), 4),
        "weight": round(n / (n + prior_strength), 4) if n + prior_strength > 0 else 0.0,
        "n":      int(n),
    }


def residualize(xs, ys) -> tuple[list[float], dict]:
    """Least-squares fit of y on x; return residuals and the fit.

    Used to turn "what did this class become" into "what did this class become
    GIVEN what it was", which is the difference between measuring recruiting and
    measuring development. Single-variable by design — the callers that need
    more covariates build their own design matrix with numpy.
    """
    pairs = [(float(a), float(b)) for a, b in zip(xs, ys)
             if a is not None and b is not None
             and math.isfinite(float(a)) and math.isfinite(float(b))]
    if len(pairs) < 3:
        return [0.0] * len(pairs), {"slope": 0.0, "intercept": 0.0, "r2": None, "n": len(pairs)}

    n = len(pairs)
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    slope = sxy / sxx if sxx > 0 else 0.0
    intercept = my - slope * mx

    resid = [p[1] - (slope * p[0] + intercept) for p in pairs]
    ss_res = sum(r ** 2 for r in resid)
    ss_tot = sum((p[1] - my) ** 2 for p in pairs)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    return resid, {"slope": slope, "intercept": intercept,
                   "r2": round(r2, 4) if r2 is not None else None, "n": n}
