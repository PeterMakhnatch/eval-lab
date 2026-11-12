"""Statistical power planning and sample size estimation for eval-lab comparisons.

Provides task-paired planning using ``pass_at_k_probability``, a model-based
independent-attempt transform ``1-(1-p)**k``. That function is not realized
first-k (any/all over ordered attempts) and not the Chen/Yao combinatorial
estimator from observed (n, c, k).

The paired-gate section answers a different question: how often a harness
publication gate that reads per-task pass/fail episodes would publish, given
per-task pass probabilities. It models the two live decision rules exactly —
Reef's default ``score_comparison`` and the exact sign-test gate with a
per-task regression veto — over independent Bernoulli episodes.
"""

from __future__ import annotations

import math

# -- paired harness-gate planning ---------------------------------------------------


def sign_test_p_value(wins: int, losses: int) -> float:
    """Exact one-sided sign test: ``P(X >= wins)`` for ``X ~ Bin(wins+losses, 1/2)``.

    Ties are not an input; the caller drops them from the sign sample before
    counting wins and losses. With no decisive pairs the statistic is vacuous
    and the p-value is 1, matching the Reef gate adapter's ``sign_test_p``.
    """
    for name, value in (("wins", wins), ("losses", losses)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    n = wins + losses
    if n == 0:
        return 1.0
    return sum(math.comb(n, i) for i in range(wins, n + 1)) / 2**n


