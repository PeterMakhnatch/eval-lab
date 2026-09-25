"""Paired harness-gate planning in src/evallab/power.py.

Validation anchors:
- exp04-check's recorded ``gate_table``
  (research-context/reef/experiments/work/04-check/summary.json), computed by
  ``04_gate_aa.py``'s exact ``publish_probability`` at the tutorial grader
  pass rates 32/60, 34/60, 39/60 for [sieve], [fib], [csv];
- HAR-72's DeepSeek A/A at ceiling pass rates (0 publishes in 30 trials),
  which must come out of the calculator as a near-zero false-publish rate.
"""

from __future__ import annotations

import pytest

from evallab.power import (
    PairedGateRule,
    paired_gate_plan_grid,
    paired_gate_publish_probability,
    sign_test_p_value,
)

#: 04-check's per-task pass rates: [sieve] 32/60, [fib] 34/60, [csv] 39/60.
EXP04_CHECK_RATES = [32 / 60, 34 / 60, 39 / 60]

#: 04-check summary.json gate_table rows: (candidate label, rule, repeats, p_publish).
EXP04_CHECK_GATE_TABLE = [
    ("null (A/A)", "wins>losses", 1, 0.34),
    ("null (A/A)", "wins>losses", 5, 0.426),
    ("null (A/A)", "sign test p<0.05", 5, 0.019),
    ("null (A/A)", "sign test p<0.05", 10, 0.03),
    ("+0.2 per task", "wins>losses", 1, 0.543),
    ("+0.2 per task", "wins>losses", 5, 0.845),
    ("+0.2 per task", "sign test p<0.05", 5, 0.169),
    ("+0.2 per task", "sign test p<0.05", 10, 0.404),
    ("+0.4 per task", "wins>losses", 1, 0.761),
    ("+0.4 per task", "wins>losses", 5, 0.996),
    ("+0.4 per task", "sign test p<0.05", 5, 0.681),
    ("+0.4 per task", "sign test p<0.05", 10, 0.977),
]

#: HAR-72's DeepSeek A/A pooled pass rates (5 repeats x 3 tasks, 0/30 publishes).
HAR72_CEILING_RATES = [1.0, 0.98, 1.0]


def _outcome(current: list[float], candidate: list[float], repeats: int, rule: PairedGateRule):
    return paired_gate_publish_probability(
        current_pass_rates=current,
        candidate_pass_rates=candidate,
        repeats=repeats,
        rule=rule,
    )


def test_sign_test_p_value_matches_exact_binomial_tail() -> None:
    """The p-value is the exact upper binomial tail with ties dropped."""
    assert sign_test_p_value(0, 0) == 1.0
    assert sign_test_p_value(5, 0) == pytest.approx(1 / 32)
    assert sign_test_p_value(6, 0) == pytest.approx(1 / 64)
    assert sign_test_p_value(6, 4) == pytest.approx(386 / 1024)
    assert sign_test_p_value(3, 3) == pytest.approx(42 / 64)


def test_sign_test_p_value_rejects_invalid_counts() -> None:
    """Negative, boolean, and non-integer counts are refused."""
    with pytest.raises(ValueError):
        sign_test_p_value(-1, 0)
    with pytest.raises(ValueError):
        sign_test_p_value(0, 2.5)
    with pytest.raises(ValueError):
        sign_test_p_value(True, 0)


def test_exp04_check_gate_table_reproduces_exactly() -> None:
    """Both live gate rules reproduce every recorded exp04-check gate_table row."""
    for label, gate, repeats, expected in EXP04_CHECK_GATE_TABLE:
        lift = {"null (A/A)": 0.0, "+0.2 per task": 0.2, "+0.4 per task": 0.4}[label]
        rule = (
            PairedGateRule.reef_default()
            if gate.startswith("wins>losses")
            else PairedGateRule.sign_gate(veto=None)
        )
        outcome = _outcome(
            EXP04_CHECK_RATES,
            [min(1.0, rate + lift) for rate in EXP04_CHECK_RATES],
            repeats,
            rule,
        )
        assert outcome.publish_probability == pytest.approx(expected, abs=1e-3), (label, gate)


def test_har72_ceiling_aa_false_publish_is_near_zero() -> None:
    """At ceiling pass rates the sign gate almost never publishes under H0."""
    outcome = _outcome(
        HAR72_CEILING_RATES,
        HAR72_CEILING_RATES,
        5,
        PairedGateRule.sign_gate(alpha=0.05, veto=1),
    )
    # Publish needs five decisive pairs from the one non-saturated task, so
    # the exact rate is ~2.9e-9: consistent with 0 publishes in 30 trials.
    assert outcome.publish_probability == pytest.approx(2.892546549760012e-09, rel=1e-9)
    # The only non-saturated task can still trip the regression veto: the
    # current side passes all five repeats while the candidate misses one.
    veto = 0.98**5 * (1 - 0.98**5)
    assert outcome.veto_probability == pytest.approx(veto)


def test_midrange_pool_has_nonzero_false_publish_for_majority_rule() -> None:
    """Reef's default majority rule has a material false-publish rate at mid rates."""
    rates = [0.5, 0.5, 0.5]
    outcome = _outcome(rates, rates, 1, PairedGateRule.reef_default())
    # Symmetric pairs: P(wins > losses) = (1 - P(wins == losses)) / 2, and
    # P(wins == losses) = 0.5**3 + 6 * 0.25 * 0.25 * 0.5 = 0.3125.
    assert outcome.publish_probability == pytest.approx(11 / 32)


def test_all_tied_pool_never_publishes() -> None:
    """A pool where both sides always pass produces no decisive pairs."""
    rates = [1.0, 1.0]
    for rule in (PairedGateRule.reef_default(), PairedGateRule.sign_gate()):
        outcome = _outcome(rates, rates, 3, rule)
        assert outcome.publish_probability == 0.0
        assert outcome.expected_wins == 0.0
        assert outcome.expected_losses == 0.0
        assert outcome.expected_ties == 6.0


def test_regression_veto_reduces_publish_probability() -> None:
    """The veto removes decisions the sign test alone would publish on."""
    current = [0.9] * 6
    candidate = [0.99] * 6
    without_veto = _outcome(current, candidate, 2, PairedGateRule.sign_gate(veto=None))
    with_veto = _outcome(current, candidate, 2, PairedGateRule.sign_gate(veto=1))
    assert with_veto.veto_probability > 0.0
    assert with_veto.publish_probability < without_veto.publish_probability
    # Raising the threshold to the repeat count makes the veto require a
    # full sweep of candidate failures on a protected task; task vetoes
    # combine as independent events, not as a plain sum.
    strict = _outcome(current, candidate, 2, PairedGateRule.sign_gate(veto=2))
    per_task = 0.9**2 * (1 - 0.99) ** 2
    assert strict.veto_probability == pytest.approx(1 - (1 - per_task) ** 6)


def test_expectations_are_linear_in_repeats_and_rates() -> None:
    """Expected tallies equal repeats times the summed per-task pair law."""
    current = [0.5, 0.2, 0.9]
    candidate = [0.7, 0.4, 0.6]
    repeats = 4
    outcome = _outcome(current, candidate, repeats, PairedGateRule.reef_default())
    win = sum(c * (1 - p) for p, c in zip(current, candidate, strict=True))
    loss = sum(p * (1 - c) for p, c in zip(current, candidate, strict=True))
    assert outcome.expected_wins == pytest.approx(repeats * win)
    assert outcome.expected_losses == pytest.approx(repeats * loss)
    assert outcome.expected_ties == pytest.approx(repeats * (3 - win - loss))


def test_probability_mass_is_conserved_by_the_veto() -> None:
    """The veto can only remove publish decisions, never add them."""
    current = [0.8, 0.3]
    candidate = [0.95, 0.5]
    outcome = _outcome(current, candidate, 3, PairedGateRule.sign_gate(veto=1))
    no_veto = _outcome(current, candidate, 3, PairedGateRule.sign_gate(veto=None))
    assert outcome.publish_probability <= no_veto.publish_probability + 1e-12
    assert 0.0 <= outcome.veto_probability <= 1.0
    assert outcome.publish_probability + outcome.veto_probability <= 1.0


def test_min_win_margin_shifts_the_majority_rule() -> None:
    """A margin of one demands wins exceed losses by more than one."""
    rates = [0.5] * 4
    plain = _outcome(rates, rates, 1, PairedGateRule(decision="wins_margin", min_win_margin=0))
    margined = _outcome(rates, rates, 1, PairedGateRule(decision="wins_margin", min_win_margin=1))
    assert margined.publish_probability < plain.publish_probability


def test_grid_flags_requirement_and_counts_episodes() -> None:
    """Grid rows carry episodes per side and the requirement flag consistently."""
    rows = paired_gate_plan_grid(
        pass_rates=[0.5, 0.6],
        per_task_lift=0.2,
        rule=PairedGateRule.sign_gate(veto=None),
        max_tasks=3,
        max_repeats=2,
    )
    assert len(rows) == 6
    for row in rows:
        assert row.episodes_per_side == row.tasks * row.repeats
        recomputed = _outcome(
            [0.5 if i % 2 == 0 else 0.6 for i in range(row.tasks)],
            [0.7 if i % 2 == 0 else 0.8 for i in range(row.tasks)],
            row.repeats,
            PairedGateRule.sign_gate(veto=None),
        )
        assert row.power_at_lift == pytest.approx(recomputed.publish_probability)
        assert row.meets_requirements == (
            row.false_publish_rate <= 0.05 and row.power_at_lift >= 0.8
        )


def test_rule_and_input_validation() -> None:
    """Malformed rules and rate vectors are refused."""
    with pytest.raises(ValueError):
        PairedGateRule(decision="bogus")
    with pytest.raises(ValueError):
        PairedGateRule(decision="sign_test", alpha=0.0)
    with pytest.raises(ValueError):
        PairedGateRule(decision="wins_margin", min_win_margin=-1)
    with pytest.raises(ValueError):
        PairedGateRule(decision="sign_test", regression_failure_threshold=0)
    with pytest.raises(ValueError):
        paired_gate_publish_probability(
            current_pass_rates=[0.5],
            candidate_pass_rates=[0.5, 0.5],
            repeats=1,
            rule=PairedGateRule.sign_gate(),
        )
    with pytest.raises(ValueError):
        paired_gate_publish_probability(
            current_pass_rates=[],
            candidate_pass_rates=[],
            repeats=1,
            rule=PairedGateRule.sign_gate(),
        )
    with pytest.raises(ValueError):
        paired_gate_publish_probability(
            current_pass_rates=[1.5],
            candidate_pass_rates=[0.5],
            repeats=1,
            rule=PairedGateRule.sign_gate(),
        )
    with pytest.raises(ValueError):
        paired_gate_publish_probability(
            current_pass_rates=[0.5],
            candidate_pass_rates=[0.5],
            repeats=0,
            rule=PairedGateRule.sign_gate(),
        )
    with pytest.raises(ValueError):
        paired_gate_plan_grid(
            pass_rates=[0.5],
            per_task_lift=1.5,
            rule=PairedGateRule.sign_gate(),
            max_tasks=2,
            max_repeats=1,
        )
