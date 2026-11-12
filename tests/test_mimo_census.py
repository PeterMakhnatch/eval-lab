"""Behavior tests for the retained fleet census log readers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.mimo_census import parse_junit_grade


@pytest.mark.parametrize(
    "line",
    [
        "rc=1 cases=4 bad=1 named=4 missing=[]",
        "runner=pytest rc=1\nrc=1 cases=4 bad=1 skipped=0 named=4 missing=[]",
    ],
)
def test_parse_junit_grade_round_trip(line: str) -> None:
    grade = parse_junit_grade(line)
    assert grade == {"rc": 1, "cases": 4, "bad": 1, "named": 4, "missing": []}
    assert parse_junit_grade("not a grade line") is None
    assert parse_junit_grade("") is None


def _write_job_result(job: Path, *, errored: int = 0, eval_errors: int = 0) -> None:
    job.mkdir(parents=True, exist_ok=True)
    (job / "result.json").write_text(
        json.dumps(
            {
                "stats": {
                    "n_errored_trials": errored,
                    "evals": {"nop__adhoc": {"n_errors": eval_errors, "metrics": []}},
                }
            }
        ),
        encoding="utf-8",
    )


def test_setup_fail_bucket_for_errored_jobs(tmp_path: Path) -> None:
    from evallab.mimo_census import job_has_trial_errors

    errored = tmp_path / "errored"
    (errored / "t").mkdir(parents=True)
    (errored / "t" / "result.json").write_text("{}", encoding="utf-8")
    _write_job_result(errored, errored=1)
    assert job_has_trial_errors(errored)
    quiet = tmp_path / "quiet"
    (quiet / "t").mkdir(parents=True)
    (quiet / "t" / "result.json").write_text("{}", encoding="utf-8")
    _write_job_result(quiet)
    assert not job_has_trial_errors(quiet)


def _write_verifier_trial(
    trial: Path, *, output: str = "", grade: str = "", runner: str | None = None
) -> Path:
    verifier = trial / "verifier"
    verifier.mkdir(parents=True, exist_ok=True)
    (verifier / "test_output.log").write_text(output, encoding="utf-8")
    (verifier / "junit-grade.log").write_text(grade, encoding="utf-8")
    if runner is not None:
        (verifier / "runner.txt").write_text(f"RUNNER={runner}\n", encoding="utf-8")
    return trial


def test_trial_runner_identity_prefers_runner_txt(tmp_path: Path) -> None:
    from evallab.mimo_census import trial_runner

    trial = _write_verifier_trial(tmp_path / "t1", runner="go-test", grade="runner=jest rc=0\n")
    assert trial_runner(trial) == "go-test"
    trial2 = _write_verifier_trial(tmp_path / "t2", grade="runner=mocha rc=1\n")
    assert trial_runner(trial2) == "mocha"
    trial3 = _write_verifier_trial(tmp_path / "t3", grade="rc=1 cases=0 bad=0 named=0")
    assert trial_runner(trial3) == "pytest"
