"""VerifierCheck core: contracts, controls, hypothesis lifecycle, waves.

Offline: every grade goes through an injected fake ``grade_fn``. No Docker,
no model, no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evallab.vcheck import (
    ControlReport,
    GradeResult,
    Submission,
    blank_submission,
    oracle_submission,
    run_controls,
)


def _grade(reward: float | None, status: str = "ok", script: str = "fake.sh") -> GradeResult:
    return GradeResult(
        reward=reward,
        status=status,
        verifier_outputs={},
        job_dir=None,
        script=script,  # type: ignore[arg-type]
    )


def _fake_grade(
    oracle_reward: float | None = 1.0,
    blank_reward: float | None = 0.0,
    honest_reward: float | None = None,
    oracle_status: str = "ok",
    blank_status: str = "ok",
) -> Any:
    """Fake grade_fn keyed on submission shape: oracle+no-op, env+empty, else honest."""

    def grade(package: str | Path, submission: Submission) -> GradeResult:
        if submission.base == "oracle" and not submission.content:
            return _grade(oracle_reward, oracle_status, "oracle.sh")
        if submission.base == "environment" and not submission.content:
            return _grade(blank_reward, blank_status, "blank.sh")
        return _grade(
            honest_reward if honest_reward is not None else oracle_reward, script="honest.sh"
        )

    return grade


# --------------------------------------------------------------------------- #
# Hypothesis lifecycle
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Harness controls
# --------------------------------------------------------------------------- #


def test_controls_ok_when_oracle_scores_1_and_empty_scores_0() -> None:
    report = run_controls("pkg", _fake_grade(), task="t1")
    assert isinstance(report, ControlReport)
    assert report.verdict == "ok"
    assert report.task == "t1"
    assert report.baseline.reward == 1.0
    assert report.negative.reward == 0.0
    assert report.legit is report.baseline  # no honest solve: legit reuses oracle


def test_legit_grades_honest_solve_when_available() -> None:
    honest = Submission(kind="file", path="/app/out.txt", content=b"honest", base="oracle")
    report = run_controls("pkg", _fake_grade(honest_reward=1.0), honest=honest)
    assert report.verdict == "ok"
    assert report.legit.script == "honest.sh"


@pytest.mark.parametrize(
    ("oracle_reward", "blank_reward", "oracle_status", "blank_status", "match"),
    [
        (0.0, 0.0, "ok", "ok", "oracle earned 0"),
        (0.5, 0.0, "ok", "ok", "oracle earned 0.5"),
        (1.0, 1.0, "ok", "ok", "empty submission earned 1"),
        (1.0, 0.5, "ok", "ok", "empty submission earned 0.5"),
        (None, 0.0, "infra", "ok", "baseline unscored"),
        (None, 0.0, "ok", "ok", "baseline unscored"),
        (1.0, None, "ok", "infra", "negative control unscored"),
        # task_broken takes precedence: both controls bad still reports task_broken.
        (0.0, 1.0, "ok", "ok", "oracle earned 0"),
        (None, None, "infra", "infra", "baseline unscored"),
    ],
)
def test_task_broken_precedence(
    oracle_reward: float | None,
    blank_reward: float | None,
    oracle_status: str,
    blank_status: str,
    match: str,
) -> None:
    report = run_controls(
        "pkg",
        _fake_grade(
            oracle_reward=oracle_reward,
            blank_reward=blank_reward,
            oracle_status=oracle_status,
            blank_status=blank_status,
        ),
    )
    assert report.verdict == "task_broken"
    assert match in report.reason


def test_control_submission_shapes() -> None:
    assert oracle_submission().base == "oracle" and oracle_submission().content == b""
    assert blank_submission().base == "environment" and blank_submission().content == b""


# --------------------------------------------------------------------------- #
# Broadcast synthesis
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Manifest pinning
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Waves
# --------------------------------------------------------------------------- #


