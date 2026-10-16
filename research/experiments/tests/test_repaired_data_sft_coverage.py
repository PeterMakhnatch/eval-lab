"""Coverage and uncertainty reporting of the repaired-data SFT scorer (`analyze.py`).

Regression for the Verifier's 2026-10-16 offline uncertainty check (research-context
5013313): 13 synthetic B/C fixtures over 33 tasks. Three reporting defects were found:
- an observed subset printed as a bare 1.0;
- zero common tasks printed p = 1;
- tiny p-values were rounded to 0.0.
Expected values are the Verifier's independent calculations.

Its follow-up check added two cell-membership fixtures on the authorized 33-task x {1,2}
schedule. An extra a3 reported 67/66 measured attempts, and a missing a2 plus an extra
a3 reported a false complete 66/66. Both must now be refused.
"""

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCORER = REPO_ROOT / "research" / "experiments" / "repaired-data-sft-v1" / "analyze.py"
ANALYZE = runpy.run_path(str(SCORER))
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
        paired(b, c, TASKS, ("B", "C"), attempts),
    )


def _full(value: bool = True) -> dict[str, list[tuple[str, bool | None]]]:
    return _trials("B", {t: [value, value] for t in TASKS})


def _extra_a3() -> dict[str, list[tuple[str, bool | None]]]:
    trials = _full()
    trials[TASKS[0]].append((f"synthetic-B-{TASKS[0]}-a3", True))
    return trials


def _missing_a2_extra_a3() -> dict[str, list[tuple[str, bool | None]]]:
    trials = _full()
    trials[TASKS[0]] = [(f"synthetic-B-{TASKS[0]}-a1", True), (f"synthetic-B-{TASKS[0]}-a3", True)]
    return trials


def _duplicate_a1() -> dict[str, list[tuple[str, bool | None]]]:
    trials = _full()
    trials[TASKS[0]].append((f"synthetic-B-{TASKS[0]}-a1", False))
    return trials


def _unauthorized_task() -> dict[str, list[tuple[str, bool | None]]]:
    trials = _full()
    trials["synthetic-task-999"] = [("synthetic-B-synthetic-task-999-a1", True)]
    return trials


INVALID = {
    "extra_a3": _extra_a3,  # was 67/66
    "missing_a2_extra_a3": _missing_a2_extra_a3,  # was a false complete 66/66
    "duplicate_a1": _duplicate_a1,
    "unauthorized_task": _unauthorized_task,
}


def test_full_authorized_schedule_is_complete() -> None:
    out = coverage(_full(), TASKS, 2)
    assert out["solve_coverage"] == "complete"
    assert (out["measured_attempts"], out["missing_task_attempts"]) == ("66/66", 0)


@pytest.mark.parametrize("case", sorted(INVALID))
def test_cells_outside_the_schedule_are_refused(case: str) -> None:
    trials = INVALID[case]()
    with pytest.raises(ValueError, match="authorized 33-task x attempts 1..2"):
        coverage(trials, TASKS, 2)
    with pytest.raises(ValueError, match="authorized"):
        paired(trials, _full(False), TASKS, ("B", "C"), 2)
    with pytest.raises(ValueError, match="authorized"):
        paired(_full(False), trials, TASKS, ("B", "C"), 2)


def _cli(
    tmp_path: Path, b_trials: dict[str, list[tuple[str, bool | None]]]
) -> subprocess.CompletedProcess:
    digest = {t: hashlib.sha256(t.encode()).hexdigest() for t in TASKS}
    (tmp_path / "heldout.json").write_text(json.dumps({"scoring_allowed": True, "heldout": TASKS}))
    (tmp_path / "exports.json").write_text(
        json.dumps({"source_manifest": {"tasks": ["training-only"]}, "arms": {}})
    )
    (tmp_path / "package-pairs.csv").write_text(
        "task_id,v6_package_digest\n" + "".join(f"{t},sha256:{digest[t]}\n" for t in TASKS)
    )
    receipt = {
        "remote": {
            "train_rows": 17,
            "trained_tokens": 100,
            "parameter_update": {"optimizer_steps": 20},
        },
        "train": {"sft": {"num_epochs": 4}},
    }
    (tmp_path / "receipt.json").write_text(json.dumps(receipt))
    command = [sys.executable, str(SCORER), "--attempts", "2", "--out", str(tmp_path / "out.json")]
    for flag in ("heldout", "exports", "package-pairs"):
        suffix = ".csv" if flag == "package-pairs" else ".json"
        command += [f"--{flag}", str(tmp_path / f"{flag}{suffix}")]
    for arm, trials in (("B", b_trials), ("C", _full(False))):
        rows = [
            {"task": f"/store/fixture__{t}/{digest.get(t, '0' * 12)[:12]}", "name": n, "passed": v}
            for t, items in trials.items()
            for n, v in items
        ]
        (tmp_path / f"{arm}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        command += ["--arm", f"{arm}={tmp_path / f'{arm}.jsonl'}"]
        command += ["--receipt", f"{arm}={tmp_path / 'receipt.json'}"]
    return subprocess.run(command, capture_output=True, text=True, timeout=60)


def test_cli_refuses_the_verifier_cell_fixtures_without_a_report(tmp_path: Path) -> None:
    ok = _cli(tmp_path, _full())
    assert ok.returncode == 0, ok.stderr
    assert (
        json.loads((tmp_path / "out.json").read_text())["arms"]["B"]["solve_coverage"] == "complete"
    )
    for case in ("extra_a3", "missing_a2_extra_a3"):
        (tmp_path / "out.json").unlink(missing_ok=True)
        run = _cli(tmp_path, INVALID[case]())
        assert run.returncode == 2, case
        assert "REFUSED: arm B" in run.stderr and "a3" in run.stderr
        assert not (tmp_path / "out.json").exists()


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
