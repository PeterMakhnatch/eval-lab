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
from collections.abc import Sequence
from dataclasses import dataclass

from evallab.cohort import (
    minimum_detectable_effect,
    pass_at_k_probability,
    power_requirements,
    required_tasks_for_effect,
)
from evallab.schemas import PowerSpec

__all__ = [
    "PairedGateGridRow",
    "PairedGateOutcome",
    "PairedGateRule",
    "minimum_detectable_effect",
    "paired_gate_plan_grid",
    "paired_gate_publish_probability",
    "pass_at_k_probability",
    "plan_power_spec",
    "power_requirements",
    "required_tasks_for_effect",
    "sign_test_p_value",
]


def plan_power_spec(
    *,
    n_tasks: int,
    k: int = 1,
    baseline: float = 0.0,
    alpha: float = 0.05,
    target_power: float = 0.8,
    pair_correlation: float = 0.0,
) -> PowerSpec:
    """Construct a validated PowerSpec with planned_n and minimum detectable difference."""
    if n_tasks < 2:
        return PowerSpec(
            mdd=None,
            planned_n=n_tasks,
        )

    try:
        mde = minimum_detectable_effect(
            n_tasks=n_tasks,
            k=k,
            baseline=baseline,
            alpha=alpha,
            target_power=target_power,
            pair_correlation=pair_correlation,
        )
    except (ValueError, ZeroDivisionError):
        mde = None

    return PowerSpec(
        mdd=round(mde, 4) if mde is not None else None,
        planned_n=n_tasks,
    )


# -- paired harness-gate planning ---------------------------------------------------


@dataclass(frozen=True)
class PairedGateRule:
    """One harness publication gate's decision rule, in planning form.

    ``decision`` selects the tally the gate reads:

    - ``"wins_margin"`` publishes when ``wins - losses`` exceeds
      ``min_win_margin`` (0 is Reef's default ``score_comparison`` majority:
      Reef's ``ScoreComparisonMixin.decide`` selects on exactly that tally).
    - ``"sign_test"`` publishes when the exact one-sided sign-test p-value on
      decisive pairs (ties dropped) is strictly below ``alpha``, the rule the
      Reef gate adapter's ``decide_pairs`` applies to its valid pairs.

    ``regression_failure_threshold`` adds that adapter's per-task regression
    veto: a task whose current side passed every repeat contributes a veto
    once at least this many of its candidate episodes failed. ``None``
    disables the veto (Reef's default rule has none).
    """

    decision: str = "sign_test"
    alpha: float = 0.05
    min_win_margin: int = 0
    regression_failure_threshold: int | None = None

    def __post_init__(self) -> None:
        if self.decision not in ("wins_margin", "sign_test"):
            raise ValueError("decision must be 'wins_margin' or 'sign_test'")
        if not 0.0 < self.alpha < 1.0:
            raise ValueError("alpha must lie strictly between 0 and 1")
        if isinstance(self.min_win_margin, bool) or not isinstance(self.min_win_margin, int):
            raise ValueError("min_win_margin must be an integer")
        if self.min_win_margin < 0:
            raise ValueError("min_win_margin must be non-negative")
        if self.regression_failure_threshold is not None:
            if isinstance(self.regression_failure_threshold, bool) or not isinstance(
                self.regression_failure_threshold, int
            ):
                raise ValueError("regression_failure_threshold must be an integer when set")
            if self.regression_failure_threshold < 1:
                raise ValueError("regression_failure_threshold must be at least 1 when set")

    @classmethod
    def reef_default(cls) -> PairedGateRule:
        """Reef's default ``score_comparison``: publish when wins exceed losses."""
        return cls(decision="wins_margin", min_win_margin=0)

    @classmethod
    def sign_gate(cls, *, alpha: float = 0.05, veto: int | None = 1) -> PairedGateRule:
        """Exact sign-test gate; ``veto`` sets the per-task regression threshold."""
        return cls(
            decision="sign_test",
            alpha=alpha,
            regression_failure_threshold=veto,
        )


@dataclass(frozen=True)
class PairedGateOutcome:
    """Exact operating characteristics of one gate over one pool design.

    ``publish_probability`` is the exact probability the gate publishes,
    computed over every decisive-pair configuration, never simulated.
    ``veto_probability`` is the chance at least one protected task vetoes
    (``None`` when the rule has no veto). Expectations are per decision.
    """

    rule: PairedGateRule
    tasks: int
    repeats: int
    publish_probability: float
    veto_probability: float | None
    expected_wins: float
    expected_losses: float
    expected_ties: float


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


def _task_group_law(
    current_rate: float, candidate_rate: float, repeats: int, veto_threshold: int | None
) -> dict[tuple[int, int, int], float]:
    """Exact law of ``(wins, losses, veto)`` for one task's ``repeats`` pairings.

    Each repeat is an independent pairing of one current and one candidate
    episode. The per-repeat outcomes carry just enough state for the veto:
    protection requires every current episode to pass, so the tracked flag
    survives only through loss (current passed, candidate failed) and
    both-passed tie outcomes — a both-failed tie breaks the current side's
    sweep and must not keep the task protected.
    """
    win = candidate_rate * (1.0 - current_rate)
    loss = current_rate * (1.0 - candidate_rate)
    tie_both_pass = current_rate * candidate_rate
    tie_both_fail = (1.0 - current_rate) * (1.0 - candidate_rate)
    # (wins, losses, protected_flag) -> probability.
    states: dict[tuple[int, int, int], float] = {(0, 0, 1): 1.0}
    for _ in range(repeats):
        nxt: dict[tuple[int, int, int], float] = {}
        for (wins_i, losses_i, protected), mass in states.items():
            for (dw, dl, dp), q in (
                ((1, 0, 0), win),
                ((0, 1, protected), loss),
                ((0, 0, protected), tie_both_pass),
                ((0, 0, 0), tie_both_fail),
            ):
                if q > 0.0:
                    key = (wins_i + dw, losses_i + dl, dp)
                    nxt[key] = nxt.get(key, 0.0) + mass * q
        states = nxt
    law: dict[tuple[int, int, int], float] = {}
    for (wins_i, losses_i, protected), mass in states.items():
        veto = (
            1 if (protected and veto_threshold is not None and losses_i >= veto_threshold) else 0
        )
        law[(wins_i, losses_i, veto)] = law.get((wins_i, losses_i, veto), 0.0) + mass
    return law


def paired_gate_publish_probability(
    *,
    current_pass_rates: Sequence[float],
    candidate_pass_rates: Sequence[float],
    repeats: int,
    rule: PairedGateRule,
) -> PairedGateOutcome:
    """Exact publish probability of one gate over a paired task pool.

    ``current_pass_rates`` and ``candidate_pass_rates`` give each task's
    per-episode pass probability; every task runs ``repeats`` times per side,
    paired positionally — the layout both live gates read. Episodes are
    independent Bernoulli draws, and the calculation assumes every episode is
    valid, so infra failures (which only shrink the gate's evidence) reduce
    power in practice but are not modelled here.
    """
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        raise ValueError("repeats must be an integer of at least 1")
    if len(current_pass_rates) != len(candidate_pass_rates):
        raise ValueError("current and candidate pass-rate sequences must have equal length")
    if not current_pass_rates:
        raise ValueError("at least one task is required")
    for label, rates in (("current", current_pass_rates), ("candidate", candidate_pass_rates)):
        for rate in rates:
            if (
                isinstance(rate, bool)
                or not isinstance(rate, (int, float))
                or not 0.0 <= rate <= 1.0
            ):
                raise ValueError(f"{label} pass rates must be numbers in [0, 1]")

    joint: dict[tuple[int, int, int], float] = {(0, 0, 0): 1.0}
    expected_wins = expected_losses = expected_ties = 0.0
    veto_probability = 0.0 if rule.regression_failure_threshold is not None else None
    for current_rate, candidate_rate in zip(current_pass_rates, candidate_pass_rates, strict=True):
        win = candidate_rate * (1.0 - current_rate)
        loss = current_rate * (1.0 - candidate_rate)
        expected_wins += repeats * win
        expected_losses += repeats * loss
        expected_ties += repeats * (1.0 - win - loss)
        law = _task_group_law(
            current_rate, candidate_rate, repeats, rule.regression_failure_threshold
        )
        nxt: dict[tuple[int, int, int], float] = {}
        for (w0, l0, v0), mass0 in joint.items():
            for (dw, dl, dv), mass1 in law.items():
                key = (w0 + dw, l0 + dl, v0 or dv)
                nxt[key] = nxt.get(key, 0.0) + mass0 * mass1
        joint = nxt

    publish = 0.0
    for (wins, losses, vetoed), mass in joint.items():
        if vetoed:
            if veto_probability is not None:
                veto_probability += mass
            continue
        if rule.decision == "wins_margin":
            selected = wins - losses > rule.min_win_margin
        else:
            selected = sign_test_p_value(wins, losses) < rule.alpha
        if selected:
            publish += mass

    return PairedGateOutcome(
        rule=rule,
        tasks=len(current_pass_rates),
        repeats=repeats,
        publish_probability=publish,
        veto_probability=veto_probability,
        expected_wins=expected_wins,
        expected_losses=expected_losses,
        expected_ties=expected_ties,
    )


@dataclass(frozen=True)
class PairedGateGridRow:
    """One cell of a tasks-x-repeats planning grid."""

    tasks: int
    repeats: int
    episodes_per_side: int
    false_publish_rate: float
    power_at_lift: float
    veto_probability: float | None
    meets_requirements: bool


def paired_gate_plan_grid(
    *,
    pass_rates: Sequence[float],
    per_task_lift: float,
    rule: PairedGateRule,
    max_tasks: int,
    max_repeats: int,
    false_publish_max: float = 0.05,
    power_min: float = 0.8,
) -> list[PairedGateGridRow]:
    """Tasks-x-repeats grid of exact false-publish rate and power for one pool.

    The null arm runs the same rates on both sides (identical trees), and the
    power arm adds ``per_task_lift`` to every task's pass rate, capped at 1.
    Task count grows by tiling ``pass_rates`` cyclically, so the pool's rate
    profile is preserved as it scales; a row's ``tasks`` is therefore a rate
    mix, not a claim that specific new tasks exist.
    """
    if not 0.0 <= per_task_lift <= 1.0:
        raise ValueError("per_task_lift must lie in [0, 1]")
    if max_tasks < 1 or max_repeats < 1:
        raise ValueError("max_tasks and max_repeats must be at least 1")
    rates = [float(rate) for rate in pass_rates]
    if not rates:
        raise ValueError("at least one pass rate is required")
    lifted = [min(1.0, rate + per_task_lift) for rate in rates]
    rows: list[PairedGateGridRow] = []
    for tasks in range(1, max_tasks + 1):
        current = [rates[i % len(rates)] for i in range(tasks)]
        candidate = [lifted[i % len(lifted)] for i in range(tasks)]
        for repeats in range(1, max_repeats + 1):
            null = paired_gate_publish_probability(
                current_pass_rates=current,
                candidate_pass_rates=current,
                repeats=repeats,
                rule=rule,
            )
            powered = paired_gate_publish_probability(
                current_pass_rates=current,
                candidate_pass_rates=candidate,
                repeats=repeats,
                rule=rule,
            )
            rows.append(
                PairedGateGridRow(
                    tasks=tasks,
                    repeats=repeats,
                    episodes_per_side=tasks * repeats,
                    false_publish_rate=null.publish_probability,
                    power_at_lift=powered.publish_probability,
                    veto_probability=powered.veto_probability,
                    meets_requirements=(
                        null.publish_probability <= false_publish_max
                        and powered.publish_probability >= power_min
                    ),
                )
            )
    return rows
