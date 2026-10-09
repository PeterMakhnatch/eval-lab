"""Behavioural tests for the mimo-clean-census fleet grader.

Covers the pure census helpers (task selection, JUnit evidence, cell grades,
ladder summary, verify taxonomy, spend fence, results.csv contract) on
fabricated job dirs in tmp homes. No Docker, no Harbor runs, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab import mimo_census
from evallab.mimo_census import (
    RESULTS_COLUMNS,
    VERIFY_CLEAN,
    VERIFY_ENV_BROKEN,
    VERIFY_GRADER_HOLE,
    VERIFY_NEEDS_TRIAGE,
    VERIFY_OPEN_LEAK,
    VERIFY_ORACLE_WRONG,
    VERIFY_UNVERIFIED,
    acceptance_matches,
    amortized_cost,
    append_spend_record,
    blank_row,
    census_row_pass,
    fence_allows,
    ladder_summary,
    load_results,
    next_free_name,
    nop_cell_grade,
    oracle_cell_grade,
    parse_junit_grade,
    parse_task_list,
    slice_spent_usd,
    summarize_results,
    trial_tests_executed,
    verify_grade_for,
    write_results,
)

TASK_ID = "format-code-task-000000"


def _write_result(trial_dir: Path, mean: float | None) -> None:
    trial_dir.mkdir(parents=True, exist_ok=True)
    if mean is None:
        payload: dict[str, Any] = {"stats": {}}
    else:
        payload = {"stats": {"evals": {"nop__adhoc": {"metrics": [{"mean": mean}]}}}}
    (trial_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_junit(trial_dir: Path, line: str = "rc=1 cases=4 bad=1 named=4 missing=[]") -> None:
    verifier = trial_dir / "verifier"
    verifier.mkdir(parents=True, exist_ok=True)
    (verifier / "junit-grade.log").write_text(line + "\n", encoding="utf-8")


def _write_attempts(
    trial_dir: Path, attacks: list[tuple[str, str]], version: str = "1.2.0"
) -> None:
    cheat_dir = trial_dir / "agent" / "cheat"
    cheat_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "evallab.cheat.attempts/v1",
        "agent": "cheat",
        "version": version,
        "cwd": "/testbed",
        "attacks": [
            {"name": name, "status": status, "detail": "", "evidence": []}
            for name, status in attacks
        ],
    }
    (cheat_dir / "attempts.json").write_text(json.dumps(payload), encoding="utf-8")


def test_parse_task_list_dedupes_and_trims() -> None:
    assert parse_task_list(None) == ()
    assert parse_task_list("  ") == ()
    assert parse_task_list("b,a,b,, a") == ("b", "a")


def test_parse_junit_grade_round_trip() -> None:
    grade = parse_junit_grade("rc=1 cases=4 bad=1 named=4 missing=[]")
    assert grade == {"rc": 1, "cases": 4, "bad": 1, "named": 4, "missing": []}
    assert parse_junit_grade("not a grade line") is None
    assert parse_junit_grade("") is None


def test_nop_grade_zero_with_executed_tests(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "trial-a"
    _write_result(trial, 0.0)
    _write_junit(trial)
    assert nop_cell_grade(job) == "0"


def test_nop_grade_flags_missing_test_evidence(tmp_path: Path) -> None:
    job = tmp_path / "job"
    _write_result(job / "trial-a", 0.0)
    assert nop_cell_grade(job) == "0-noexec"


def test_nop_grade_failure_modes(tmp_path: Path) -> None:
    assert nop_cell_grade(tmp_path / "absent") == "missing"
    unscored = tmp_path / "unscored"
    _write_result(unscored / "t", None)
    assert nop_cell_grade(unscored) == "unscored"
    failed = tmp_path / "failed"
    trial = failed / "t"
    _write_result(trial, 1.0)
    _write_junit(trial)
    assert nop_cell_grade(failed) == "fail:1"


def test_trial_tests_executed_requires_named_cases(tmp_path: Path) -> None:
    trial = tmp_path / "t"
    _write_junit(trial, "rc=0 cases=0 bad=0 named=0 missing=[]")
    assert not trial_tests_executed(trial)
    _write_junit(trial, "rc=0 cases=9 bad=0 named=9 missing=[]")
    assert trial_tests_executed(trial)


def test_oracle_grades(tmp_path: Path) -> None:
    assert oracle_cell_grade(None, has_reference_fix=False) == "n/a"
    assert oracle_cell_grade(tmp_path / "absent", has_reference_fix=True) == "missing"
    good = tmp_path / "good"
    trial = good / "t"
    _write_result(trial, 1.0)
    _write_junit(trial, "rc=0 cases=9 bad=0 named=9 missing=[]")
    assert oracle_cell_grade(good, has_reference_fix=True) == "1"
    bad = tmp_path / "bad"
    _write_result(bad / "t", 0.0)
    assert oracle_cell_grade(bad, has_reference_fix=True) == "fail:0"


def test_ladder_summary_clean_full_ladder(tmp_path: Path) -> None:
    from evallab.cheat_ladder import ATTACKS

    job = tmp_path / "cheat"
    trial = job / "t1"
    _write_result(trial, 0.0)
    _write_attempts(trial, [(name, "executed") for name in ATTACKS])
    summary = ladder_summary(job)
    assert summary["verdict"] == "clean"
    assert summary["cracking"] == []
    assert summary["executed"] == sorted(ATTACKS)
    assert summary["trials"] == 1


def test_ladder_summary_partial_when_attack_missing(tmp_path: Path) -> None:
    from evallab.cheat_ladder import ATTACKS

    job = tmp_path / "cheat"
    trial = job / "t1"
    _write_result(trial, 0.0)
    _write_attempts(trial, [(name, "executed") for name in ATTACKS[:-1]])
    assert ladder_summary(job)["verdict"] == "partial"


def test_ladder_summary_attributes_single_attack_crack(tmp_path: Path) -> None:
    from evallab.cheat_ladder import ATTACKS

    job = tmp_path / "cheat"
    full = job / "full"
    _write_result(full, 1.0)
    _write_attempts(full, [(name, "executed") for name in ATTACKS])
    single = job / "single-skip_plant"
    _write_result(single, 1.0)
    _write_attempts(single, [("skip_plant", "executed")])
    summary = ladder_summary(job)
    assert summary["verdict"] == "cracked"
    assert "skip_plant" in summary["cracking"]
    assert "full-ladder-unattributed" in summary["cracking"]


def test_ladder_summary_unscored_and_missing(tmp_path: Path) -> None:
    assert ladder_summary(tmp_path / "absent")["verdict"] == "missing"
    job = tmp_path / "job"
    trial = job / "t"
    _write_result(trial, None)
    _write_attempts(trial, [("git_history", "executed")])
    assert ladder_summary(job)["verdict"] == "unscored"


def test_verify_grade_taxonomy() -> None:
    clean = {"nop": "0", "oracle": "1", "ladder_verdict": "clean", "census_locations": 0}
    assert verify_grade_for(**clean) == VERIFY_CLEAN
    assert (
        verify_grade_for(nop="0", oracle="n/a", ladder_verdict="clean", census_locations=None)
        == VERIFY_CLEAN
    )
    assert (
        verify_grade_for(nop="0", oracle="1", ladder_verdict="clean", census_locations=3)
        == VERIFY_OPEN_LEAK
    )
    assert (
        verify_grade_for(nop="0", oracle="1", ladder_verdict="cracked", census_locations=0)
        == VERIFY_GRADER_HOLE
    )
    assert (
        verify_grade_for(nop="fail:1", oracle="1", ladder_verdict="clean", census_locations=0)
        == VERIFY_ENV_BROKEN
    )
    assert (
        verify_grade_for(nop="0", oracle="fail:0", ladder_verdict="clean", census_locations=0)
        == VERIFY_ORACLE_WRONG
    )
    assert (
        verify_grade_for(nop="0-noexec", oracle="1", ladder_verdict="clean", census_locations=0)
        == VERIFY_NEEDS_TRIAGE
    )
    assert (
        verify_grade_for(nop="missing", oracle="1", ladder_verdict="clean", census_locations=0)
        == VERIFY_UNVERIFIED
    )
    assert (
        verify_grade_for(nop="0", oracle="1", ladder_verdict="partial", census_locations=0)
        == VERIFY_UNVERIFIED
    )


def test_census_row_pass_requires_all_checks() -> None:
    base = blank_row(TASK_ID, manifest_version="mimo-clean-v1", backend="docker")
    base.update(
        {"nop": "0", "oracle": "1", "ladder_verdict": "clean", "ladder_cracking_attacks": ""}
    )
    assert census_row_pass(base)
    assert census_row_pass({**base, "oracle": "n/a"})
    assert not census_row_pass({**base, "nop": "0-noexec"})
    assert not census_row_pass({**base, "ladder_cracking_attacks": "skip_plant"})
    assert not census_row_pass({**base, "census_locations": "2"})
    assert census_row_pass({**base, "census_locations": "0"})


def test_acceptance_matches_mimo_clean_predicate() -> None:
    assert acceptance_matches(
        oracle_rewards=[1.0], has_reference_fix=True, nop_rewards=[0.0], cracked=0
    )
    assert not acceptance_matches(
        oracle_rewards=[0.0], has_reference_fix=True, nop_rewards=[0.0], cracked=0
    )


def test_fence_and_amortized_cost() -> None:
    assert fence_allows(spent_usd=0.0, projected_usd=15.00)
    assert not fence_allows(spent_usd=0.01, projected_usd=15.00)
    assert fence_allows(spent_usd=3.0, projected_usd=5.0, cap_usd=10.0)
    assert amortized_cost(2.0, 4) == 0.5
    assert amortized_cost(2.0, 0) == 0.0


def test_results_csv_round_trip_sorted(tmp_path: Path) -> None:
    rows = [
        {**blank_row("b-task", manifest_version="v", backend="docker"), "nop": "0"},
        {**blank_row("a-task", manifest_version="v", backend="docker"), "nop": "0"},
    ]
    path = write_results(rows, tmp_path / "results.csv")
    loaded = load_results(path)
    assert [row["task_id"] for row in loaded] == ["a-task", "b-task"]
    assert set(loaded[0]) == set(RESULTS_COLUMNS)


def test_summarize_results_buckets_failures() -> None:
    passing = {
        **blank_row("a", manifest_version="v", backend="docker"),
        "nop": "0",
        "oracle": "1",
        "ladder_verdict": "clean",
    }
    failing = {
        **blank_row("b", manifest_version="v", backend="docker"),
        "nop": "fail:1",
        "oracle": "n/a",
        "ladder_verdict": "clean",
        "census_locations": "2",
    }
    summary = summarize_results([passing, failing])
    assert (summary["total"], summary["passed"], summary["failed"]) == (2, 1, 1)
    assert summary["failures"][0]["task_id"] == "b"
    assert "nop=fail:1" in summary["failures"][0]["reasons"]


def test_spend_record_and_slice_total(tmp_path: Path) -> None:
    assert slice_spent_usd(tmp_path) == 0.0
    append_spend_record(tmp_path, {"batch_id": "pilot", "actual_usd": 1.25})
    append_spend_record(tmp_path, {"batch_id": "extra", "actual_usd": 0.75})
    assert slice_spent_usd(tmp_path) == 2.0


def test_cell_job_naming_and_next_free(tmp_path: Path) -> None:
    name = mimo_census.cell_job_name(task_id=TASK_ID, backend="modal", cell="nop")
    assert name == "mimo-census-v1-modal-000000-nop"
    assert next_free_name(tmp_path, name) == name
    (tmp_path / name).mkdir()
    assert next_free_name(tmp_path, name) == f"{name}-attempt2"
