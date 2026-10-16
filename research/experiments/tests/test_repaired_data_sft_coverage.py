"""Coverage and uncertainty reporting of the repaired-data SFT scorer (`analyze.py`).

Regression for the Verifier's 2026-10-16 offline uncertainty check (research-context
5013313): 13 synthetic B/C fixtures over 33 tasks. Three reporting defects were found:
- an observed subset printed as a bare 1.0;
- zero common tasks printed p = 1;
- tiny p-values were rounded to 0.0.
Expected values are the Verifier's independent calculations.
"""

from __future__ import annotations

import runpy
from fractions import Fraction
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ANALYZE = runpy.run_path(
    str(REPO_ROOT / "research" / "experiments" / "repaired-data-sft-v1" / "analyze.py")
)
coverage = ANALYZE["arm_coverage"]
paired = ANALYZE["paired_comparison"]
incorrect_pass = ANALYZE["incorrect_pass_summary"]

TASKS = [f"synthetic-task-{i:03d}" for i in range(1, 34)]
Values = dict[str, list[bool | None]]


def _trials(arm: str, values: Values) -> dict[str, list[tuple[str, bool | None]]]:
    return {
        t: [(f"synthetic-{arm}-{t}-a{k}", v) for k, v in enumerate(items, 1)]
        for t, items in values.items()
    }


ONCE = {
    "B": {t: [i < 24] for i, t in enumerate(TASKS)},
    "C": {t: [i < 12] for i, t in enumerate(TASKS)},
}
TWICE = {arm: {t: v * 2 for t, v in items.items()} for arm, items in ONCE.items()}
FIXTURES: dict[str, dict[str, Values]] = {
    "balanced_once": ONCE,
    "balanced_correlated_twice": TWICE,
    "balanced_correlated_twice_swapped": {"B": TWICE["C"], "C": TWICE["B"]},
    "identical_arms": {"B": TWICE["B"], "C": TWICE["B"]},
    "sign_majority_opposes_mean": {
        "B": {t: ([True, False] if i < 18 else [False, False]) for i, t in enumerate(TASKS)},
        "C": {t: ([True, True] if 18 <= i < 28 else [False, False]) for i, t in enumerate(TASKS)},
    },
    "unequal_attempt_membership": {
        "B": {t: ([True, True] if i < 16 else [False]) for i, t in enumerate(TASKS)},
        "C": {t: ([True] if i < 16 else [False, False]) for i, t in enumerate(TASKS)},
    },
    "partial_error_rows": {
        "B": {t: ([True, True] if i < 2 else [None, None]) for i, t in enumerate(TASKS)},
        "C": {t: [False, False] for t in TASKS},
    },
    "partial_missing_task_rows": {
        "B": {t: [True, True] for t in TASKS[:2]},
        "C": {t: [False, False] for t in TASKS},
    },
    "all_B_errored": {"B": {t: [None, None] for t in TASKS}, "C": TWICE["C"]},
    "empty_B": {"B": {}, "C": TWICE["C"]},
    "disjoint_measured_tasks": {
        "B": {t: ([True, True] if i < 16 else [None, None]) for i, t in enumerate(TASKS)},
        "C": {t: ([None, None] if i < 16 else [True, True]) for i, t in enumerate(TASKS)},
    },
    "unanimous_B_better": {
        "B": {t: [True, True] for t in TASKS},
        "C": {t: [False, False] for t in TASKS},
    },
    "unanimous_C_better": {
        "B": {t: [False, False] for t in TASKS},
        "C": {t: [True, True] for t in TASKS},
    },
}

# Verifier's expected B coverage: (coverage, measured tasks, measured attempts, missing
# task-attempts, solve rate, CP interval).
EXPECTED_B = {
    "balanced_once": ("complete", "33/33", "33/33", 0, 0.7273, (0.5448, 0.867)),
    "balanced_correlated_twice": ("complete", "33/33", "66/66", 0, 0.7273, (0.6036, 0.8297)),
    "partial_error_rows": ("observed_subset", "2/33", "4/66", 62, 1.0, (0.3976, 1.0)),
    "partial_missing_task_rows": ("observed_subset", "2/33", "4/66", 62, 1.0, (0.3976, 1.0)),
    "unequal_attempt_membership": (
        "observed_subset",
        "33/33",
        "49/66",
        17,
        0.6531,
        (0.5036, 0.7833),
    ),
    "all_B_errored": ("unmeasured", "0/33", "0/66", 66, None, None),
    "empty_B": ("unmeasured", "0/33", "0/66", 66, None, None),
    "disjoint_measured_tasks": ("observed_subset", "16/33", "32/66", 34, 1.0, (0.8911, 1.0)),
}

# Verifier's expected B-vs-C pair: (status, cohort, B wins, C wins, mean difference, p).
EXPECTED_PAIR = {
    "balanced_once": ("measured", "33/33", 12, 0, 0.3636, Fraction(1, 2048)),
    "balanced_correlated_twice": ("measured", "33/33", 12, 0, 0.3636, Fraction(1, 2048)),
    "balanced_correlated_twice_swapped": ("measured", "33/33", 0, 12, -0.3636, Fraction(1, 2048)),
    "identical_arms": ("measured", "33/33", 0, 0, 0.0, Fraction(1)),
    "unequal_attempt_membership": ("measured", "33/33", 0, 0, 0.0, Fraction(1)),
    "partial_error_rows": ("measured", "2/33", 2, 0, 1.0, Fraction(1, 2)),
    "partial_missing_task_rows": ("measured", "2/33", 2, 0, 1.0, Fraction(1, 2)),
    "all_B_errored": ("unmeasured", "0/33", 0, 0, None, None),
    "empty_B": ("unmeasured", "0/33", 0, 0, None, None),
    "disjoint_measured_tasks": ("unmeasured", "0/33", 0, 0, None, None),
    "unanimous_B_better": ("measured", "33/33", 33, 0, 1.0, Fraction(1, 2**32)),
    "unanimous_C_better": ("measured", "33/33", 0, 33, -1.0, Fraction(1, 2**32)),
}


def _run(case: str) -> tuple[dict, dict, dict]:
    values = FIXTURES[case]
    b, c = _trials("B", values["B"]), _trials("C", values["C"])
    attempts = 1 if case == "balanced_once" else 2
    return (
        coverage(b, TASKS, attempts),
        coverage(c, TASKS, attempts),
        paired(b, c, TASKS, ("B", "C")),
    )


@pytest.mark.parametrize("case", sorted(EXPECTED_B))
def test_arm_coverage_states_the_observed_subset(case: str) -> None:
    status, tasks, attempts, missing, rate, ci = EXPECTED_B[case]
    b, _, _ = _run(case)
    assert b["solve_coverage"] == status
    assert (b["measured_tasks"], b["measured_attempts"]) == (tasks, attempts)
    assert b["missing_task_attempts"] == missing
    assert b["solve_rate"] == rate
    assert b["solve_ci95"] == ci


def test_partial_coverage_names_the_missing_tasks() -> None:
    # 4 passes on 2 tasks, 31 tasks with no rows at all: never a bare 1.0.
    b, _, _ = _run("partial_missing_task_rows")
    assert b["missing_task_ids"] == TASKS[2:]
    assert b["unmeasured_task_ids"] == TASKS[2:]
    errored, _, _ = _run("partial_error_rows")
    assert errored["missing_task_ids"] == []
    assert errored["unmeasured_task_ids"] == TASKS[2:]


@pytest.mark.parametrize("case", sorted(EXPECTED_PAIR))
def test_paired_comparison_states_its_cohort_and_never_fakes_p(case: str) -> None:
    status, cohort, wins, losses, diff, p = EXPECTED_PAIR[case]
    _, _, pair = _run(case)
    assert pair["status"] == status
    assert pair["common_task_cohort"] == cohort
    assert (pair["B_better"], pair["C_better"]) == (wins, losses)
    assert pair["mean_difference"] == diff
    if p is None:
        assert pair["exact_sign_p"] is None and pair["exact_sign_fraction"] is None
    else:
        assert pair["exact_sign_fraction"] == str(p)
        assert pair["exact_sign_p"] == float(p)


def test_tiny_p_is_kept_at_full_precision() -> None:
    _, _, pair = _run("unanimous_B_better")
    assert pair["exact_sign_p"] == pytest.approx(2.3283064365e-10, rel=1e-10)
    assert pair["exact_sign_p"] > 0


def test_unequal_attempts_are_flagged_not_hidden() -> None:
    b, c, pair = _run("unequal_attempt_membership")
    assert (b["solve_rate"], c["solve_rate"]) == (0.6531, 0.32)
    assert pair["mean_difference"] == 0.0
    assert pair["equal_attempts_per_task"] is False
    assert (pair["B_attempts_in_cohort"], pair["C_attempts_in_cohort"]) == (49, 50)
    assert "unequal attempts" in pair["attempts_caveat"]


def test_arm_swap_reverses_the_pair_and_keeps_p() -> None:
    _, _, normal = _run("balanced_correlated_twice")
    _, _, swapped = _run("balanced_correlated_twice_swapped")
    assert normal["mean_difference"] == -swapped["mean_difference"]
    assert (normal["B_better"], normal["C_better"]) == (swapped["C_better"], swapped["B_better"])
    assert normal["exact_sign_p"] == swapped["exact_sign_p"]


def test_empty_replay_population_is_unmeasured_not_complete() -> None:
    out = incorrect_pass([], {})
    assert out["incorrect_pass_status"] == "unmeasured"
    assert out["incorrect_pass_rate"] is None
    assert out["incorrect_pass_ci95"] is None
