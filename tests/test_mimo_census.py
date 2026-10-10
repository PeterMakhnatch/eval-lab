"""Behavioural tests for the mimo-clean-census fleet grader.

Covers the pure census helpers (task selection, JUnit evidence, cell grades,
ladder summary, verify taxonomy, spend fence, results.csv contract) on
fabricated job dirs in tmp homes. No Docker, no Harbor runs, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab import mimo_census
from evallab.mimo_census import (
    RESULTS_COLUMNS,
    VERIFY_ENV_BROKEN,
    VERIFY_FAIL_GRADER_HOLE,
    VERIFY_FAIL_OPEN_LEAK,
    VERIFY_FAIL_ORACLE_WRONG,
    VERIFY_PASS,
    VERIFY_UNVERIFIED,
    acceptance_matches,
    amortized_cost,
    append_spend_record,
    blank_row,
    cell_reusable,
    census_row_pass,
    census_task,
    combine_task_rows,
    expected_resource_kwargs,
    fence_allows,
    ladder_summary,
    load_results,
    merge_census_rows,
    next_free_name,
    nop_cell_grade,
    normalize_modal_resource_policy,
    oracle_cell_grade,
    parse_check_list,
    parse_junit_grade,
    parse_task_list,
    policy_row_value,
    probe_completion,
    probe_evidence,
    recorded_resource_kwargs,
    report_row_key,
    resolve_worker_count,
    run_task_record,
    scannable_cell_dirs,
    slice_spent_usd,
    summarize_results,
    trial_disk_exhausted,
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


def test_trial_tests_executed_counts_cases_not_names(tmp_path: Path) -> None:
    trial = tmp_path / "t"
    _write_junit(trial, "rc=0 cases=0 bad=0 named=0 missing=[]")
    assert not trial_tests_executed(trial)
    # Suites may report cases without parsable names (observed cases=10 bad=6
    # named=0 on a genuine nop failure): cases>0 is the execution signal.
    _write_junit(trial, "rc=1 cases=10 bad=6 named=0 missing=[]")
    assert trial_tests_executed(trial)


def test_exit_code_graded_nop_scores_zero(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "t1"
    _write_result(trial, 0.0)
    (trial / "verifier").mkdir(parents=True)
    (trial / "verifier" / "junit-grade.log").write_text(
        "rc=1 no junit: exit-code grading\n", encoding="utf-8"
    )
    (trial / "verifier" / "test_output.log").write_text(
        "FAILED test_a.py::test_x\nFAILED test_a.py::test_y\n"
        "============================== 2 failed in 0.79s ===============================\n"
        "REWARD=0 rc=1\n",
        encoding="utf-8",
    )
    assert trial_tests_executed(trial)
    assert nop_cell_grade(job) == "0"


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
    assert nop_cell_grade(errored) == "setup-fail"
    assert (
        verify_grade_for(nop="setup-fail", oracle="n/a", ladder_verdict="clean", census_locations=0)
        == VERIFY_ENV_BROKEN
    )
    quiet = tmp_path / "quiet"
    (quiet / "t").mkdir(parents=True)
    (quiet / "t" / "result.json").write_text("{}", encoding="utf-8")
    _write_job_result(quiet)
    assert not job_has_trial_errors(quiet)
    assert nop_cell_grade(quiet) == "unscored"


def test_backend_unsupported_bucket_for_disk_exhaustion(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "t"
    _write_result(trial, None)
    (trial / "trial.log").write_text(
        "setup: writing layer: no space left on device\n", encoding="utf-8"
    )
    _write_job_result(job, errored=1)
    assert trial_disk_exhausted(trial)
    # Disk exhaustion wins over the generic setup-fail bucket.
    assert nop_cell_grade(job) == "backend-unsupported"
    assert (
        verify_grade_for(
            nop="backend-unsupported",
            oracle="n/a",
            ladder_verdict="missing",
            census_locations=None,
        )
        == VERIFY_UNVERIFIED
    )
    passing = {
        **blank_row("a", manifest_version="v", backend="daytona"),
        "nop": "0",
        "oracle": "n/a",
        "ladder_verdict": "clean",
        "census_locations": "n/a",
    }
    unsupported = {
        **blank_row("b", manifest_version="v", backend="daytona"),
        "nop": "backend-unsupported",
        "oracle": "backend-unsupported",
        "ladder_verdict": "missing",
    }
    summary = summarize_results([passing, unsupported], has_reference_fix={"a": False, "b": False})
    assert (summary["passed"], summary["failed"], summary["backend_unsupported"]) == (1, 0, 1)
    assert summary["failures"] == []


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


def test_ladder_summary_clean_with_legitimate_skips(tmp_path: Path) -> None:
    from evallab.cheat_ladder import ATTACKS

    job = tmp_path / "cheat"
    trial = job / "t1"
    _write_result(trial, 0.0)
    _write_attempts(
        trial,
        [
            (
                name,
                "executed"
                if name not in ("tamper_source_exit", "tamper_source_pytest_patch")
                else "skipped",
            )
            for name in ATTACKS
        ],
    )
    summary = ladder_summary(job)
    assert summary["verdict"] == "clean"
    assert summary["cracking"] == []
    assert summary["skipped"] == ["tamper_source_exit", "tamper_source_pytest_patch"]


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


def test_cell_job_naming_and_next_free(tmp_path: Path) -> None:
    name = mimo_census.cell_job_name(task_id=TASK_ID, backend="modal", cell="nop")
    assert name == "mimo-census-v1-modal-000000-nop"
    assert (
        mimo_census.cell_job_name(task_id=TASK_ID, backend="daytona", cell="cheat")
        == "mimo-census-v1-daytona-000000-cheat"
    )
    assert next_free_name(tmp_path, name) == name
    (tmp_path / name).mkdir()
    assert next_free_name(tmp_path, name) == f"{name}-attempt2"


def test_verify_grade_taxonomy() -> None:
    clean = {"nop": "0", "oracle": "1", "ladder_verdict": "clean", "census_locations": 0}
    assert verify_grade_for(**clean) == VERIFY_PASS
    assert (
        verify_grade_for(
            nop="0",
            oracle="n/a",
            ladder_verdict="clean",
            census_locations=None,
            has_reference_fix=False,
        )
        == VERIFY_PASS
    )
    assert (
        verify_grade_for(
            nop="0",
            oracle="n/a",
            ladder_verdict="clean",
            census_locations="n/a",
            has_reference_fix=False,
        )
        == VERIFY_PASS
    )
    # Known-patch default is strict: the fix phase must complete first.
    assert (
        verify_grade_for(nop="0", oracle="n/a", ladder_verdict="clean", census_locations="")
        == VERIFY_UNVERIFIED
    )
    assert (
        verify_grade_for(nop="0", oracle="n/a", ladder_verdict="clean", census_locations=None)
        == VERIFY_UNVERIFIED
    )
    assert (
        verify_grade_for(nop="0", oracle="1", ladder_verdict="clean", census_locations=3)
        == VERIFY_FAIL_OPEN_LEAK
    )
    assert (
        verify_grade_for(nop="0", oracle="1", ladder_verdict="clean", census_locations="2")
        == VERIFY_FAIL_OPEN_LEAK
    )
    assert (
        verify_grade_for(nop="0", oracle="1", ladder_verdict="cracked", census_locations=0)
        == VERIFY_FAIL_GRADER_HOLE
    )
    assert (
        verify_grade_for(nop="fail:1", oracle="1", ladder_verdict="clean", census_locations=0)
        == VERIFY_ENV_BROKEN
    )
    assert (
        verify_grade_for(nop="0", oracle="fail:0", ladder_verdict="clean", census_locations=0)
        == VERIFY_FAIL_ORACLE_WRONG
    )
    # Ambiguous and unexecuted signals stay unverified; per-check columns
    # carry the explicit classification for the receipt taxonomy.
    assert (
        verify_grade_for(nop="0-noexec", oracle="1", ladder_verdict="clean", census_locations=0)
        == VERIFY_UNVERIFIED
    )
    assert (
        verify_grade_for(nop="missing", oracle="1", ladder_verdict="clean", census_locations=0)
        == VERIFY_UNVERIFIED
    )
    assert (
        verify_grade_for(nop="0", oracle="1", ladder_verdict="partial", census_locations=0)
        == VERIFY_UNVERIFIED
    )
    assert (
        verify_grade_for(
            nop="0", oracle="1", ladder_verdict="clean", census_locations="probe-blind"
        )
        == VERIFY_UNVERIFIED
    )
    assert (
        verify_grade_for(nop="", oracle="", ladder_verdict="", census_locations="")
        == VERIFY_UNVERIFIED
    )
    assert verify_grade_for(**{**clean, "census_locations": "bogus"}) == VERIFY_UNVERIFIED


def test_census_row_pass_requires_all_checks() -> None:
    base = blank_row(TASK_ID, manifest_version="mimo-clean-v1", backend="docker")
    base.update(
        {"nop": "0", "oracle": "1", "ladder_verdict": "clean", "ladder_cracking_attacks": ""}
    )
    # Known-patch default is strict: a blank fix cell never passes.
    assert not census_row_pass(base)
    assert census_row_pass({**base, "oracle": "n/a", "census_locations": "0"})
    assert not census_row_pass({**base, "nop": "0-noexec", "census_locations": "0"})
    assert not census_row_pass(
        {**base, "ladder_cracking_attacks": "skip_plant", "census_locations": "0"}
    )
    assert not census_row_pass({**base, "census_locations": "2"})
    assert census_row_pass({**base, "census_locations": "0"})
    assert not census_row_pass({**base, "census_locations": "probe-blind"})
    # No reference fix: explicit n/a (or legacy blank) permits pass.
    assert census_row_pass({**base, "census_locations": "n/a"}, has_reference_fix=False)
    assert census_row_pass(base, has_reference_fix=False)
    assert not census_row_pass({**base, "census_locations": "2"}, has_reference_fix=False)


def test_acceptance_matches_mimo_clean_predicate() -> None:
    assert acceptance_matches(
        oracle_rewards=[1.0], has_reference_fix=True, nop_rewards=[0.0], cracked=0
    )
    assert not acceptance_matches(
        oracle_rewards=[0.0], has_reference_fix=True, nop_rewards=[0.0], cracked=0
    )


def test_fence_and_amortized_cost() -> None:
    assert fence_allows(spent_usd=0.0, projected_usd=13.00)
    assert not fence_allows(spent_usd=0.01, projected_usd=13.00)
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
        "census_locations": "n/a",
    }
    failing = {
        **blank_row("b", manifest_version="v", backend="docker"),
        "nop": "fail:1",
        "oracle": "n/a",
        "ladder_verdict": "clean",
        "census_locations": "2",
    }
    summary = summarize_results([passing, failing], has_reference_fix={"a": False, "b": True})
    assert (summary["total"], summary["passed"], summary["failed"]) == (2, 1, 1)
    assert summary["failures"][0]["task_id"] == "b"
    assert "nop=fail:1" in summary["failures"][0]["reasons"]


def test_summarize_blank_fix_blocks_known_patch_pass() -> None:
    row = {
        **blank_row("a", manifest_version="v", backend="docker"),
        "nop": "0",
        "oracle": "1",
        "ladder_verdict": "clean",
        "census_locations": "",
    }
    strict = summarize_results([row], has_reference_fix={"a": True})
    assert (strict["passed"], strict["failed"]) == (0, 1)
    assert "census=pending" in strict["failures"][0]["reasons"]
    lenient = summarize_results([row], has_reference_fix={"a": False})
    assert (lenient["passed"], lenient["failed"]) == (1, 0)


def test_spend_record_and_slice_total(tmp_path: Path) -> None:
    assert slice_spent_usd(tmp_path) == 0.0
    append_spend_record(tmp_path, {"batch_id": "pilot", "actual_usd": 1.25})
    append_spend_record(tmp_path, {"batch_id": "extra", "actual_usd": 0.75})
    assert slice_spent_usd(tmp_path) == 2.0


def test_daytona_hourly_rate_matches_known_stratum() -> None:
    from evallab.mimo_census import daytona_hourly_usd

    # 2 CPU / 8 GiB / 10 GiB (5 free) == HAR-88 $0.23094/h reference.
    assert daytona_hourly_usd(cpus=2.0, mem_gib=8.0, disk_gib=10.0) == pytest.approx(0.23094)
    assert daytona_hourly_usd(cpus=2.0, mem_gib=8.0, disk_gib=3.0) == pytest.approx(
        2 * 0.0504 + 8 * 0.0162
    )


def test_trial_wall_hours_and_batch_cost(tmp_path: Path) -> None:
    from evallab.mimo_census import daytona_batch_cost_usd, trial_wall_hours

    trial = tmp_path / "task-a" / "job-daytona-x" / "t1"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {"started_at": "2026-10-10T00:00:00+00:00", "finished_at": "2026-10-10T01:00:00+00:00"}
        ),
        encoding="utf-8",
    )
    assert trial_wall_hours(trial) == pytest.approx(1.0)
    assert trial_wall_hours(tmp_path / "absent") is None
    package = tmp_path / "pkg"
    (package).mkdir()
    (package / "task.toml").write_text(
        "[environment]\ncpus = 2\nmemory_mb = 8192\n", encoding="utf-8"
    )
    manifest = {"task-a": {"package_path": "pkg"}}
    result = daytona_batch_cost_usd(
        jobs_root=tmp_path, task_ids=("task-a", "task-b"), primary=tmp_path, manifest=manifest
    )
    assert result["trials"] == 1
    assert result["per_task_usd"]["task-a"] == pytest.approx(0.23094)
    assert result["batch_usd"] == pytest.approx(0.23094)
    assert result["unscored"] == []


def test_fix_census_reuses_completed_probes(tmp_path: Path) -> None:
    import hashlib

    from evallab.mimo_census import census_fix_content

    clean = tmp_path / "clean"
    (clean / "environment" / "setup").mkdir(parents=True)
    (clean / "environment" / "setup" / "setup.sh").write_text("# ship\n", encoding="utf-8")
    run = tmp_path / "runpkg"
    (run).mkdir()
    (run / "task.toml").write_text(
        '[environment]\ndocker_image = "img:1"\nworkdir = "/testbed"\n', encoding="utf-8"
    )
    scratch = tmp_path / "scratch" / "task-x"
    scratch.mkdir(parents=True)
    sha = hashlib.sha256(b"# ship\n").hexdigest()
    (scratch / "census.meta.json").write_text(
        json.dumps(
            {
                "image": "img:1",
                "clean_setup_sha256": sha,
                "fix_sha": "a" * 40,
                "clean": {
                    "hits_total": 0,
                    "setup_rc": "0",
                    "ready": "yes",
                    "probe_rc": "0",
                    "scan_complete": True,
                    "scan_rc": "0",
                },
            }
        ),
        encoding="utf-8",
    )
    # No Docker needed: reuse short-circuits before any container call.
    result = census_fix_content(
        task_id="task-x",
        clean_package=clean,
        run_package=run,
        language="python",
        scratch_root=tmp_path / "scratch",
    )
    assert result["census_locations"] == 0


def test_fix_census_rejects_unrecoverable_fix(tmp_path: Path, monkeypatch: Any) -> None:
    import evallab.fix_content_census as fcc
    from evallab.mimo_census import census_fix_content

    clean = tmp_path / "clean"
    (clean / "environment" / "setup").mkdir(parents=True)
    (clean / "environment" / "setup" / "setup.sh").write_text("# ship\n", encoding="utf-8")
    run = tmp_path / "runpkg"
    (run).mkdir()
    (run / "task.toml").write_text(
        '[environment]\ndocker_image = "img:1"\nworkdir = "/testbed"\n', encoding="utf-8"
    )
    scratch = tmp_path / "scratch"

    def fake_copy(image: str, workdir: str, dest: object) -> bool:
        target = Path(str(dest)) / ".git"
        target.mkdir(parents=True, exist_ok=True)
        return True

    monkeypatch.setattr(fcc, "copy_git_from_image", fake_copy)
    monkeypatch.setattr(
        fcc,
        "leak_oracle_extract",
        lambda *args: {"status": "no-identifiable-fix", "fix": None, "rationale": " stories"},
    )

    def fake_lite(*args: Any) -> dict[str, Any]:
        raise ValueError("lite refused")

    monkeypatch.setattr(fcc, "recover_fix_lite", fake_lite)
    # A null fix must never become the string "None" feeding vacuous probes.
    result = census_fix_content(
        task_id="task-y",
        clean_package=clean,
        run_package=run,
        language="python",
        scratch_root=scratch,
    )
    assert result["census_locations"] is None
    assert str(result["reason"]).startswith("no recoverable fix")


KNOWN_PATCH = """\
diff --git a/pkg/core.py b/pkg/core.py
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1,2 +1,3 @@
+from pkg.newdep import promote_batch_shape
 def run(x):
-    return x
+    return promote_batch_shape(x)
"""


def _write_patch_package(tmp_path: Path, name: str) -> Path:
    package = tmp_path / name
    (package / "environment" / "setup").mkdir(parents=True)
    (package / "environment" / "setup" / "setup.sh").write_text("# ship\n", encoding="utf-8")
    (package / "task.toml").write_text(
        '[environment]\ndocker_image = "img:1"\nworkdir = "/testbed"\n', encoding="utf-8"
    )
    return package


def _fake_probe_out(out: Path, *, hits: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "workdir").write_text("/testbed", encoding="utf-8")
    (out / "fix").write_text("", encoding="utf-8")
    (out / "fix_source").write_text("known-patch:task-k", encoding="utf-8")
    (out / "fix_present_pre").write_text("precomputed", encoding="utf-8")
    (out / "fix_present_post").write_text("no", encoding="utf-8")
    (out / "setup_rc").write_text("0", encoding="utf-8")
    (out / "ready").write_text("yes", encoding="utf-8")
    (out / "docker_rc").write_text("0", encoding="utf-8")
    (out / "scan_rc").write_text("0", encoding="utf-8")
    (out / "pattern_count").write_text("2", encoding="utf-8")
    (out / "pattern_raw_count").write_text("2", encoding="utf-8")
    (out / "blobs.txt").write_text("", encoding="utf-8")
    if hits:
        (out / "hit_detail.txt").write_text(
            "/testbed/pkg/installed.py\t1\tabc123  f\n", encoding="utf-8"
        )
    else:
        (out / "hit_detail.txt").write_text("", encoding="utf-8")
    (out / "fix_mtimes.txt").write_text("", encoding="utf-8")
    (out / "worktree_mtimes").write_text("1", encoding="utf-8")
    (out / "caches.txt").write_text("", encoding="utf-8")


def test_known_patch_probe_blind(tmp_path: Path, monkeypatch: Any) -> None:
    import evallab.fix_content_census as fcc
    from evallab.mimo_census import census_fix_content

    clean = _write_patch_package(tmp_path, "clean")
    run = _write_patch_package(tmp_path, "runpkg")
    patch = tmp_path / "fix.patch"
    patch.write_text(KNOWN_PATCH, encoding="utf-8")
    staged: list[str] = []

    def fake_stage(stage_dir: object, *args: Any, **kwargs: Any) -> None:
        staged.append(str(stage_dir))
        precomputed = kwargs.get("precomputed")
        assert precomputed and len(precomputed["patterns"]) >= 1

    def fake_run(
        image: str,
        workdir: str,
        fix: str,
        stage: object,
        out: object,
        *,
        backend: str = "docker",
        egress_lock: bool = True,
    ) -> None:
        _fake_probe_out(Path(str(out)), hits=0)

    monkeypatch.setattr(fcc, "stage_probe", fake_stage)
    monkeypatch.setattr(fcc, "run_probe", fake_run)
    result = census_fix_content(
        task_id="task-k",
        clean_package=clean,
        run_package=run,
        language="python",
        scratch_root=tmp_path / "scratch",
        reference_fix=patch,
    )
    # Both actual setups are scanned; a blind positive control still cannot
    # turn a completed clean zero into a cleanliness claim.
    assert result["census_locations"] == "probe-blind"
    assert [Path(path).name for path in staged] == ["stage-published", "stage-clean"]
    metadata = json.loads(
        (tmp_path / "scratch" / "task-k" / "census.meta.json").read_text(encoding="utf-8")
    )
    assert probe_completion(metadata["published_complete"]) == (True, "")
    assert probe_completion(metadata["clean"]) == (True, "")
    assert metadata["clean"]["hits_total"] == 0
    reused = census_fix_content(
        task_id="task-k",
        clean_package=clean,
        run_package=run,
        language="python",
        scratch_root=tmp_path / "scratch",
        reference_fix=patch,
    )
    assert reused["census_locations"] == "probe-blind"
    assert len(staged) == 2
    assert (
        verify_grade_for(
            nop="0", oracle="1", ladder_verdict="clean", census_locations="probe-blind"
        )
        == VERIFY_UNVERIFIED
    )


def test_known_patch_clean_leak_counted(tmp_path: Path, monkeypatch: Any) -> None:
    import evallab.fix_content_census as fcc
    from evallab.mimo_census import census_fix_content

    clean = _write_patch_package(tmp_path, "clean")
    run = _write_patch_package(tmp_path, "runpkg")
    patch = tmp_path / "fix.patch"
    patch.write_text(KNOWN_PATCH, encoding="utf-8")

    def fake_stage(stage_dir: object, *args: Any, **kwargs: Any) -> None:
        return None

    def fake_run(
        image: str,
        workdir: str,
        fix: str,
        stage: object,
        out: object,
        *,
        backend: str = "docker",
        egress_lock: bool = True,
    ) -> None:
        _fake_probe_out(Path(str(out)), hits=1 if "published" in str(out) else 0)

    monkeypatch.setattr(fcc, "stage_probe", fake_stage)
    monkeypatch.setattr(fcc, "run_probe", fake_run)
    result = census_fix_content(
        task_id="task-k",
        clean_package=clean,
        run_package=run,
        language="python",
        scratch_root=tmp_path / "scratch",
        reference_fix=patch,
    )
    assert result["census_locations"] == 0


def test_known_patch_reuse_by_patch_sha(tmp_path: Path, monkeypatch: Any) -> None:
    import hashlib

    import evallab.fix_content_census as fcc
    from evallab.mimo_census import census_fix_content

    clean = _write_patch_package(tmp_path, "clean")
    run = _write_patch_package(tmp_path, "runpkg")
    patch = tmp_path / "fix.patch"
    patch.write_text(KNOWN_PATCH, encoding="utf-8")
    scratch = tmp_path / "scratch" / "task-k"
    scratch.mkdir(parents=True)
    setup_sha = hashlib.sha256(b"# ship\n").hexdigest()
    patch_sha = hashlib.sha256(KNOWN_PATCH.encode()).hexdigest()
    (scratch / "census.meta.json").write_text(
        json.dumps(
            {
                "image": "img:1",
                "clean_setup_sha256": setup_sha,
                "patch_sha": patch_sha,
                "clean": {
                    "hits_total": 0,
                    "setup_rc": "0",
                    "ready": "yes",
                    "probe_rc": "0",
                    "scan_complete": True,
                    "scan_rc": "0",
                },
            }
        ),
        encoding="utf-8",
    )

    def fail_stage(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("reuse must skip staging")

    monkeypatch.setattr(fcc, "stage_probe", fail_stage)
    result = census_fix_content(
        task_id="task-k",
        clean_package=clean,
        run_package=run,
        language="python",
        scratch_root=tmp_path / "scratch",
        reference_fix=patch,
    )
    assert result["census_locations"] == 0


# --- Phased selection, worker bounds, digest-bound reuse, merges, forwarding ---


def test_parse_check_list_defaults_and_rejects() -> None:
    assert parse_check_list(None) == ("nop", "oracle", "ladder", "fix")
    assert parse_check_list("  ") == ("nop", "oracle", "ladder", "fix")
    assert parse_check_list("fix,nop,fix") == ("nop", "fix")
    with pytest.raises(ValueError):
        parse_check_list("nop,bogus")
    with pytest.raises(ValueError):
        parse_check_list(" , ,")


def test_resolve_worker_count_bounds() -> None:
    from evallab.mimo_census import (
        DEFAULT_WORKERS,
        MAX_DOCKER_WORKERS,
        MAX_REMOTE_WORKERS,
    )

    assert resolve_worker_count(backend="docker", requested=None) == DEFAULT_WORKERS
    assert resolve_worker_count(backend="modal", requested=10) == 10
    assert (
        resolve_worker_count(backend="docker", requested=MAX_DOCKER_WORKERS) == MAX_DOCKER_WORKERS
    )
    assert (
        resolve_worker_count(backend="daytona", requested=MAX_REMOTE_WORKERS) == MAX_REMOTE_WORKERS
    )
    with pytest.raises(ValueError):
        resolve_worker_count(backend="docker", requested=0)
    with pytest.raises(ValueError):
        resolve_worker_count(backend="docker", requested=MAX_DOCKER_WORKERS + 1)
    with pytest.raises(ValueError):
        resolve_worker_count(backend="modal", requested=MAX_REMOTE_WORKERS + 1)


def _write_scored_cell(
    cell: Path,
    *,
    digest: str,
    agent: str,
    backend: str,
    reward: float = 0.0,
    junit: bool = False,
    attacks: list[tuple[str, str]] | None = None,
    version: str = "9.9.9",
    resource_kwargs: dict[str, str] | None = None,
) -> Path:
    trial = cell / "trial-1"
    _write_result(trial, reward)
    if junit:
        _write_junit(trial)
    (cell / "lab-metadata.json").write_text(
        json.dumps(
            {
                "command": ["harbor", "run", "--env", backend],
                "task_staging": {"source_package_digest": digest},
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"agents": [{"name": agent}]}
    if resource_kwargs is not None:
        config["environment"] = {"kwargs": dict(resource_kwargs)}
    (cell / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if attacks is not None:
        _write_attempts(trial, attacks, version=version)
    return cell


def _manifest_row(**overrides: Any) -> dict[str, str]:
    row = {
        "package_path": "derived/task-store/variants/x/abc",
        "reference_fix": "none",
        "final_digest": "sha256:abc",
        "language": "python",
    }
    row.update(overrides)
    return row


def test_checks_selection_skips_unrequested_cells(tmp_path: Path, monkeypatch: Any) -> None:
    launched: list[str] = []

    def fake_run_cell(**kwargs: Any) -> Path:
        launched.append(str(kwargs["agent"]))
        cell = tmp_path / "jobs" / TASK_ID / str(kwargs["name"])
        cell.mkdir(parents=True, exist_ok=True)
        return cell

    monkeypatch.setattr(mimo_census, "run_cell", fake_run_cell)
    partial = census_task(
        task_id=TASK_ID,
        manifest_row=_manifest_row(),
        primary=tmp_path,
        jobs_root=tmp_path / "jobs",
        backend="docker",
        timeout_seconds=10,
        root=tmp_path,
        checks=("nop",),
    )
    # Only the nop cell launches; unselected checks report blank, never missing.
    assert launched == ["nop"]
    assert partial["nop"] == "missing"
    assert partial["oracle"] == ""
    assert partial["ladder_verdict"] == ""
    assert partial["run_ids"] != ""


def test_oracle_cell_skipped_without_reference_fix(tmp_path: Path, monkeypatch: Any) -> None:
    launched: list[str] = []

    def fake_run_cell(**kwargs: Any) -> Path:
        launched.append(str(kwargs["agent"]))
        cell = tmp_path / "jobs" / TASK_ID / str(kwargs["name"])
        cell.mkdir(parents=True, exist_ok=True)
        return cell

    monkeypatch.setattr(mimo_census, "run_cell", fake_run_cell)
    partial = census_task(
        task_id=TASK_ID,
        manifest_row=_manifest_row(reference_fix="none"),
        primary=tmp_path,
        jobs_root=tmp_path / "jobs",
        backend="docker",
        timeout_seconds=10,
        root=tmp_path,
    )
    assert "oracle" not in launched
    assert partial["oracle"] == "n/a"


def test_ladder_version_available_without_harbor_sdk(monkeypatch: Any) -> None:
    import builtins

    from evallab.cheat_ladder import CHEAT_AGENT_VERSION

    real_import = builtins.__import__

    def import_without_harbor(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "evallab.harbor_cheat" or name == "harbor" or name.startswith("harbor."):
            raise ImportError("optional Harbor SDK is unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_harbor)
    assert mimo_census.current_ladder_version() == CHEAT_AGENT_VERSION
    assert mimo_census.current_ladder_version() not in ("", "unknown")


def test_matching_generation_cells_reused_without_launch(tmp_path: Path, monkeypatch: Any) -> None:
    from evallab.cheat_ladder import ATTACKS

    digest = "sha256:abc"
    jobs = tmp_path / "jobs" / TASK_ID
    version = mimo_census.current_ladder_version()
    assert version not in ("", "unknown")
    _write_scored_cell(
        jobs / "mimo-census-v1-docker-000000-nop",
        digest=digest,
        agent="nop",
        backend="docker",
        reward=0.0,
        junit=True,
    )
    _write_scored_cell(
        jobs / "mimo-census-v1-docker-000000-oracle",
        digest=digest,
        agent="oracle",
        backend="docker",
        reward=1.0,
        junit=True,
    )
    _write_scored_cell(
        jobs / "mimo-census-v1-docker-000000-cheat",
        digest=digest,
        agent="cheat",
        backend="docker",
        reward=0.0,
        attacks=[(name, "executed") for name in ATTACKS],
        version=version,
    )

    def fail_launch(**kwargs: Any) -> Path:
        raise AssertionError(f"bound cells must reuse, not launch: {kwargs['name']}")

    monkeypatch.setattr(mimo_census, "run_cell", fail_launch)
    partial = census_task(
        task_id=TASK_ID,
        manifest_row=_manifest_row(reference_fix="/fixes/x.patch", final_digest=digest),
        primary=tmp_path,
        jobs_root=tmp_path / "jobs",
        backend="docker",
        timeout_seconds=10,
        root=tmp_path,
    )
    assert partial["nop"] == "0"
    assert partial["oracle"] != ""
    assert partial["ladder_verdict"] == "clean"
    assert partial["ladder_version"] == version


def test_stale_digest_cell_relaunches(tmp_path: Path, monkeypatch: Any) -> None:
    jobs = tmp_path / "jobs" / TASK_ID
    _write_scored_cell(
        jobs / "mimo-census-v1-docker-000000-nop",
        digest="sha256:old-generation",
        agent="nop",
        backend="docker",
        reward=0.0,
        junit=True,
    )
    launched: list[str] = []

    def fake_run_cell(**kwargs: Any) -> Path:
        launched.append(str(kwargs["name"]))
        cell = tmp_path / "jobs" / TASK_ID / str(kwargs["name"])
        cell.mkdir(parents=True, exist_ok=True)
        return cell

    monkeypatch.setattr(mimo_census, "run_cell", fake_run_cell)
    census_task(
        task_id=TASK_ID,
        manifest_row=_manifest_row(final_digest="sha256:new-generation"),
        primary=tmp_path,
        jobs_root=tmp_path / "jobs",
        backend="docker",
        timeout_seconds=10,
        root=tmp_path,
        checks=("nop",),
    )
    assert launched == ["mimo-census-v1-docker-000000-nop-attempt2"]


def test_cell_reusable_binds_digest_agent_backend(tmp_path: Path) -> None:
    cell = _write_scored_cell(
        tmp_path / "cell",
        digest="sha256:abc",
        agent="nop",
        backend="docker",
        reward=0.0,
        junit=True,
    )
    good = {
        "expected_digest": "sha256:abc",
        "expected_agent": "nop",
        "expected_backend": "docker",
    }
    assert cell_reusable(cell, **good)
    assert not cell_reusable(cell, **{**good, "expected_digest": "sha256:other"})
    assert not cell_reusable(cell, **{**good, "expected_agent": "oracle"})
    assert not cell_reusable(cell, **{**good, "expected_backend": "modal"})
    assert not cell_reusable(cell, **{**good, "expected_digest": ""})
    assert not cell_reusable(tmp_path / "absent", **good)


def test_ladder_reuse_gated_on_current_version(tmp_path: Path, monkeypatch: Any) -> None:
    from evallab.cheat_ladder import ATTACKS

    digest = "sha256:abc"
    jobs = tmp_path / "jobs" / TASK_ID
    version = mimo_census.current_ladder_version()
    stale = jobs / "mimo-census-v1-docker-000000-cheat"
    _write_scored_cell(
        stale,
        digest=digest,
        agent="cheat",
        backend="docker",
        reward=0.0,
        attacks=[(name, "executed") for name in ATTACKS],
        version="0.0.0-stale",
    )
    assert not cell_reusable(
        stale,
        expected_digest=digest,
        expected_agent="cheat",
        expected_backend="docker",
        expected_ladder_version=version,
    )
    launched: list[str] = []

    def fake_run_cell(**kwargs: Any) -> Path:
        launched.append(str(kwargs["name"]))
        cell = jobs / str(kwargs["name"])
        cell.mkdir(parents=True, exist_ok=True)
        return cell

    monkeypatch.setattr(mimo_census, "run_cell", fake_run_cell)
    partial = census_task(
        task_id=TASK_ID,
        manifest_row=_manifest_row(final_digest=digest),
        primary=tmp_path,
        jobs_root=tmp_path / "jobs",
        backend="docker",
        timeout_seconds=10,
        root=tmp_path,
        checks=("ladder",),
    )
    assert launched == ["mimo-census-v1-docker-000000-cheat-attempt2"]
    assert partial["ladder_version"] == ""


def test_merge_preserves_prior_evidence_on_ladder_only_row() -> None:
    digest = "sha256:abc"
    controls = {
        **blank_row(TASK_ID, manifest_version="mimo-clean-v1", backend="daytona"),
        "final_digest": digest,
        "nop": "0",
        "oracle": "n/a",
        "ladder_verdict": "",
        "ladder_cracking_attacks": "",
        "census_locations": "",
        "run_ids": "mimo-census-v1-daytona-000000-nop",
    }
    ladder_only = {
        **blank_row(TASK_ID, manifest_version="mimo-clean-v1", backend="daytona"),
        "final_digest": digest,
        "nop": "",
        "oracle": "",
        "ladder_verdict": "clean",
        "ladder_cracking_attacks": "",
        "census_locations": "0",
        "ladder_version": "1.3.0",
        "run_ids": "mimo-census-v1-daytona-000000-cheat",
    }
    merged = merge_census_rows(controls, ladder_only)
    assert merged["nop"] == "0"
    assert merged["oracle"] == "n/a"
    assert merged["ladder_verdict"] == "clean"
    assert merged["census_locations"] == "0"
    assert merged["ladder_version"] == "1.3.0"
    assert merged["run_ids"] == (
        "mimo-census-v1-daytona-000000-nop,mimo-census-v1-daytona-000000-cheat"
    )


def test_combine_task_rows_needs_matching_digest() -> None:
    digest = "sha256:abc"
    controls = {
        **blank_row(TASK_ID, manifest_version="mimo-clean-v1", backend="daytona"),
        "final_digest": digest,
        "nop": "0",
        "oracle": "n/a",
        "ladder_verdict": "",
        "run_ids": "mimo-census-v1-daytona-000000-nop",
    }
    ladder = {
        **blank_row(TASK_ID, manifest_version="mimo-clean-v1", backend="modal"),
        "final_digest": digest,
        "nop": "",
        "oracle": "",
        "ladder_verdict": "clean",
        "ladder_version": "1.3.0",
        "run_ids": "mimo-census-v1-modal-000000-cheat",
    }
    fix = {
        **blank_row(TASK_ID, manifest_version="mimo-clean-v1", backend="modal"),
        "final_digest": digest,
        "nop": "",
        "oracle": "",
        "ladder_verdict": "",
        "census_locations": "0",
        "run_ids": "",
    }
    stale = {
        **blank_row(TASK_ID, manifest_version="mimo-clean-v1", backend="modal"),
        "final_digest": "sha256:stale-generation",
        "nop": "fail:1",
        "oracle": "fail:0",
        "ladder_verdict": "cracked",
        "census_locations": "5",
        "run_ids": "stale",
    }
    combined = combine_task_rows([controls, ladder, fix, stale], final_digest=digest)
    assert combined is not None
    assert combined["final_digest"] == digest
    assert combined["nop"] == "0"
    assert combined["oracle"] == "n/a"
    assert combined["ladder_verdict"] == "clean"
    assert combined["census_locations"] == "0"
    assert combined["verify"] == VERIFY_PASS
    assert combined["backends"] == "daytona,modal"
    assert stale["run_ids"] not in combined["run_ids"].split(",")
    assert combine_task_rows([{**controls, "final_digest": ""}], final_digest=digest) is None
    # Without an exact digest, mixed generations refuse rather than crown
    # a most-evidence older winner.
    assert combine_task_rows([controls, stale]) is None
    assert combine_task_rows([controls, stale], final_digest="sha256:absent") is None


def test_combine_task_rows_grades_fail_classes() -> None:
    digest = "sha256:abc"

    def row(**overrides: Any) -> dict[str, Any]:
        base = {
            **blank_row(TASK_ID, manifest_version="mimo-clean-v1", backend="docker"),
            "final_digest": digest,
            "nop": "0",
            "oracle": "1",
            "ladder_verdict": "clean",
            "census_locations": "0",
        }
        base.update(overrides)
        return base

    assert combine_task_rows([row(census_locations="3")])["verify"] == VERIFY_FAIL_OPEN_LEAK  # type: ignore[index]
    assert combine_task_rows([row(ladder_verdict="cracked")])["verify"] == VERIFY_FAIL_GRADER_HOLE  # type: ignore[index]
    assert combine_task_rows([row(oracle="fail:0")])["verify"] == VERIFY_FAIL_ORACLE_WRONG  # type: ignore[index]
    assert combine_task_rows([row(nop="setup-fail")])["verify"] == VERIFY_ENV_BROKEN  # type: ignore[index]


def test_summarize_counts_partial_rows_separately() -> None:
    partial = {
        **blank_row("a", manifest_version="v", backend="daytona"),
        "nop": "0",
        "oracle": "n/a",
        "ladder_verdict": "",
    }
    summary = summarize_results([partial])
    assert summary["partial"] == 1
    assert summary["failed"] == 0
    assert summary["failures"] == []


def test_run_task_record_returns_single_partial_record(tmp_path: Path, monkeypatch: Any) -> None:
    def fake_census_task(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["checks"] == ["nop", "oracle"]
        return {
            "nop": "0",
            "oracle": "n/a",
            "ladder_verdict": "",
            "ladder_cracking_attacks": "",
            "ladder_executed": "",
            "ladder_version": "",
            "run_ids": "nop-job",
        }

    def fail_fix(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("fix unselected; must not run")

    monkeypatch.setattr(mimo_census, "census_task", fake_census_task)
    monkeypatch.setattr(mimo_census, "census_fix_content", fail_fix)
    record = run_task_record(
        {
            "task_id": TASK_ID,
            "manifest_row": _manifest_row(),
            "manifest_version": "mimo-clean-v1",
            "primary": str(tmp_path),
            "jobs_root": str(tmp_path / "jobs"),
            "backend": "daytona",
            "timeout_seconds": 10,
            "root": str(tmp_path),
            "checks": ["nop", "oracle"],
            "ledger_row": None,
        }
    )
    assert record["task_id"] == TASK_ID
    assert record["nop"] == "0"
    assert record["oracle"] == "n/a"
    assert record["ladder_verdict"] == ""
    assert record["census_locations"] == ""
    assert record["run_ids"] == "nop-job"
    assert set(record) == set(RESULTS_COLUMNS)


def test_fix_backend_forwarded_to_known_patch_probe(tmp_path: Path, monkeypatch: Any) -> None:
    import evallab.fix_content_census as fcc
    from evallab.mimo_census import census_fix_content

    clean = _write_patch_package(tmp_path, "clean")
    run = _write_patch_package(tmp_path, "runpkg")
    (clean / "task.toml").write_text(
        '[environment]\ndocker_image = "img:1"\nworkdir = "/testbed"\nnetwork_mode = "public"\n',
        encoding="utf-8",
    )
    patch = tmp_path / "fix.patch"
    patch.write_text(KNOWN_PATCH, encoding="utf-8")
    seen: list[Any] = []

    def fake_run(
        image: str,
        workdir: str,
        fix: str,
        stage: object,
        out: object,
        *,
        backend: str = "docker",
        egress_lock: bool = True,
    ) -> None:
        seen.append((backend, egress_lock))
        _fake_probe_out(Path(str(out)), hits=1 if "published" in str(out) else 0)

    monkeypatch.setattr(fcc, "stage_probe", lambda *args, **kwargs: None)
    monkeypatch.setattr(fcc, "run_probe", fake_run)
    result = census_fix_content(
        task_id="task-k",
        clean_package=clean,
        run_package=run,
        language="python",
        scratch_root=tmp_path / "scratch",
        reference_fix=patch,
        backend="modal",
    )
    assert result["census_locations"] == 0
    assert seen == [("modal", False), ("modal", False)]


def test_fix_probe_egress_lock_follows_package_policy(tmp_path: Path) -> None:
    from evallab.mimo_census import fix_probe_egress_lock

    public = tmp_path / "public"
    public.mkdir()
    (public / "task.toml").write_text('[environment]\nnetwork_mode = "public"\n', encoding="utf-8")
    assert fix_probe_egress_lock(public) is False
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "task.toml").write_text('[environment]\nnetwork_mode = "none"\n', encoding="utf-8")
    assert fix_probe_egress_lock(locked) is True
    # Unreadable or missing contracts fail closed (locked).
    assert fix_probe_egress_lock(tmp_path / "absent") is True
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "task.toml").write_text('[task]\nname = "x"\n', encoding="utf-8")
    assert fix_probe_egress_lock(bare) is True


def test_fix_probe_daytona_never_relaxes_policy(tmp_path: Path, monkeypatch: Any) -> None:
    import evallab.fix_content_census as fcc
    from evallab.mimo_census import census_fix_content

    clean = _write_patch_package(tmp_path, "clean")
    run = _write_patch_package(tmp_path, "runpkg")
    (clean / "task.toml").write_text(
        '[environment]\ndocker_image = "img:1"\nworkdir = "/testbed"\nnetwork_mode = "public"\n',
        encoding="utf-8",
    )
    patch = tmp_path / "fix.patch"
    patch.write_text(KNOWN_PATCH, encoding="utf-8")
    seen: list[Any] = []

    def fake_run(
        image: str,
        workdir: str,
        fix: str,
        stage: object,
        out: object,
        *,
        backend: str = "docker",
        egress_lock: bool = True,
    ) -> None:
        seen.append((backend, egress_lock))
        _fake_probe_out(Path(str(out)), hits=1 if "published" in str(out) else 0)

    monkeypatch.setattr(fcc, "stage_probe", lambda *args, **kwargs: None)
    monkeypatch.setattr(fcc, "run_probe", fake_run)
    result = census_fix_content(
        task_id="task-k",
        clean_package=clean,
        run_package=run,
        language="python",
        scratch_root=tmp_path / "scratch",
        reference_fix=patch,
        backend="daytona",
    )
    assert result["census_locations"] == 0
    assert seen == [("daytona", True), ("daytona", True)]


def test_fix_extractor_remote_reports_unavailable(tmp_path: Path, monkeypatch: Any) -> None:
    import evallab.fix_content_census as fcc
    from evallab.mimo_census import census_fix_content

    clean = _write_patch_package(tmp_path, "clean")
    run = _write_patch_package(tmp_path, "runpkg")

    def fail_copy(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("remote archaeology must not launch")

    monkeypatch.setattr(fcc, "copy_git_from_image", fail_copy)
    result = census_fix_content(
        task_id="task-k",
        clean_package=clean,
        run_package=run,
        language="python",
        scratch_root=tmp_path / "scratch",
        reference_fix=None,
        backend="modal",
    )
    assert result["census_locations"] is None
    assert "unavailable" in str(result["reason"])


def test_run_cell_modal_app_attribution_only_on_modal(tmp_path: Path, monkeypatch: Any) -> None:
    import evallab.execution_contracts as ec
    import evallab.queue as queue_mod
    from evallab.mimo_census import MODAL_CENSUS_APP_NAME, run_cell

    class FakeRequest(dict):  # type: ignore[type-arg]
        def __init__(self, *args: Any, modal_app_name: Any = None, **kwargs: Any):
            super().__init__(modal_app_name=modal_app_name, **kwargs)

    class FakeExecutor:
        @classmethod
        def from_repo(cls, root: object) -> FakeExecutor:
            return cls()

        def execute_direct(self, request: Any) -> Path:
            cell = Path(request["jobs_dir"]) / str(request["name"])
            cell.mkdir(parents=True, exist_ok=True)
            return cell

    monkeypatch.setattr(ec, "RunRequest", FakeRequest)
    monkeypatch.setattr(queue_mod, "Executor", FakeExecutor)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    run_cell(
        package=pkg,
        agent="nop",
        name="m1",
        jobs_dir=jobs,
        backend="modal",
        timeout_seconds=10,
        root=tmp_path,
    )
    assert (jobs / "m1").is_dir()
    # Re-run capturing the request kwargs via a recording executor.
    captured: dict[str, Any] = {}

    class RecordingExecutor(FakeExecutor):
        def execute_direct(self, request: Any) -> Path:
            captured.update(dict(request))
            return super().execute_direct(request)

    monkeypatch.setattr(queue_mod, "Executor", RecordingExecutor)
    run_cell(
        package=pkg,
        agent="nop",
        name="m2",
        jobs_dir=jobs,
        backend="modal",
        timeout_seconds=10,
        root=tmp_path,
    )
    assert captured.get("modal_app_name") == MODAL_CENSUS_APP_NAME
    captured.clear()
    run_cell(
        package=pkg,
        agent="nop",
        name="d1",
        jobs_dir=jobs,
        backend="docker",
        timeout_seconds=10,
        root=tmp_path,
    )
    assert "modal_app_name" not in captured or captured.get("modal_app_name") is None


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


def test_tests_executed_surefire_and_marker_grades(tmp_path: Path) -> None:
    passing = _write_verifier_trial(
        tmp_path / "surefire",
        grade="runner=mvn rc=0\nrc=0 mvn surefire=2 cases=3 bad=0\n",
    )
    assert trial_tests_executed(passing)
    empty = _write_verifier_trial(
        tmp_path / "surefire-empty",
        grade="runner=mvn rc=0\nrc=0 mvn surefire=2 cases=0 bad=0\n",
    )
    assert not trial_tests_executed(empty)
    marked = _write_verifier_trial(
        tmp_path / "marked",
        output="some output without countable cases\n",
        grade="runner=jest rc=0\nrc=0 jest markers=True\n",
        runner="jest",
    )
    assert trial_tests_executed(marked)
    unmarked = _write_verifier_trial(
        tmp_path / "unmarked",
        output="some output without countable cases\n",
        grade="runner=jest rc=0\nrc=0 jest markers=False\n",
        runner="jest",
    )
    assert not trial_tests_executed(unmarked)


def test_tests_executed_go_family(tmp_path: Path) -> None:
    ok = _write_verifier_trial(
        tmp_path / "go-ok",
        output="ok  \texample.com/mod/pkg\t1.234s\n",
        grade="runner=go-test rc=0\nrc=0 go-test markers=True\n",
        runner="go-test",
    )
    assert trial_tests_executed(ok)
    failed = _write_verifier_trial(
        tmp_path / "go-fail",
        output="--- FAIL: TestParse (0.01s)\nFAIL\texample.com/mod/pkg\t0.02s\n",
        grade="runner=go-test rc=1\nrc=1 go-test nonzero exit\n",
        runner="go-test",
    )
    # Failed cases still prove execution (reward stays 0; no noexec flag).
    assert trial_tests_executed(failed)
    nofiles = _write_verifier_trial(
        tmp_path / "go-nofiles",
        output="?   \texample.com/mod/pkg\t[no test files]\n",
        grade="runner=go-test rc=0\nrc=0 go-test markers=False\n",
        runner="go-test",
    )
    assert not trial_tests_executed(nofiles)
    buildfail = _write_verifier_trial(
        tmp_path / "go-buildfail",
        output="# example.com/mod/pkg\n./pkg.go:3: syntax error\nFAIL\texample.com/mod/pkg [build failed]\n",
        grade="runner=go-test rc=1\nrc=1 go-test nonzero exit\n",
        runner="go-test",
    )
    assert not trial_tests_executed(buildfail)


def test_tests_executed_js_families(tmp_path: Path) -> None:
    jest = _write_verifier_trial(
        tmp_path / "jest",
        output="Tests:       5 passed, 5 total\nTest Suites: 1 passed, 1 total\n",
        grade="runner=jest rc=0\n",
        runner="jest",
    )
    assert trial_tests_executed(jest)
    jest_fail = _write_verifier_trial(
        tmp_path / "jest-fail",
        output="Tests:       3 failed, 2 passed, 5 total\n",
        grade="runner=jest rc=1\nrc=1 jest nonzero exit\n",
        runner="jest",
    )
    assert trial_tests_executed(jest_fail)
    jest_none = _write_verifier_trial(
        tmp_path / "jest-none",
        output="No tests found, exiting with code 1\n",
        grade="runner=jest rc=1\nrc=1 jest nonzero exit\n",
        runner="jest",
    )
    assert not trial_tests_executed(jest_none)
    vitest = _write_verifier_trial(
        tmp_path / "vitest",
        output=" Test Files  1 passed (1)\n      Tests  5 passed (5)\n",
        grade="runner=vitest rc=0\n",
        runner="vitest",
    )
    assert trial_tests_executed(vitest)
    mocha = _write_verifier_trial(
        tmp_path / "mocha",
        output="  12 passing (30ms)\n",
        grade="runner=mocha rc=0\n",
        runner="mocha",
    )
    assert trial_tests_executed(mocha)
    mocha_none = _write_verifier_trial(
        tmp_path / "mocha-none",
        output="  0 passing (5ms)\n",
        grade="runner=mocha rc=0\nrc=0 mocha markers=False\n",
        runner="mocha",
    )
    assert not trial_tests_executed(mocha_none)


def test_tests_executed_other_runners(tmp_path: Path) -> None:
    unittest = _write_verifier_trial(
        tmp_path / "unittest",
        output="Ran 5 tests in 0.01s\n\nOK\n",
        grade="runner=unittest rc=0\n",
        runner="unittest",
    )
    assert trial_tests_executed(unittest)
    phpunit = _write_verifier_trial(
        tmp_path / "phpunit",
        output="OK (5 tests, 10 assertions)\n",
        grade="runner=phpunit rc=0\n",
        runner="phpunit",
    )
    assert trial_tests_executed(phpunit)
    phpunit_none = _write_verifier_trial(
        tmp_path / "phpunit-none",
        output="No tests executed!\n",
        grade="runner=phpunit rc=1\nrc=1 phpunit nonzero exit\n",
        runner="phpunit",
    )
    assert not trial_tests_executed(phpunit_none)
    rspec = _write_verifier_trial(
        tmp_path / "rspec",
        output="7 examples, 0 failures\n",
        grade="runner=rspec rc=0\n",
        runner="rspec",
    )
    assert trial_tests_executed(rspec)
    cargo = _write_verifier_trial(
        tmp_path / "cargo",
        output="test result: ok. 4 passed; 0 failed; 0 ignored\n",
        grade="runner=cargo-test rc=0\n",
        runner="cargo-test",
    )
    assert trial_tests_executed(cargo)
    tap = _write_verifier_trial(
        tmp_path / "tap",
        output="TAP version 13\nok 1 - first\nnot ok 2 - second\n1..2\n",
        grade="runner=tap rc=1\nrc=1 tap nonzero exit\n",
        runner="tap",
    )
    assert trial_tests_executed(tap)


def test_tests_executed_custom_fallback_rc_is_not_execution(tmp_path: Path) -> None:
    custom = _write_verifier_trial(
        tmp_path / "custom",
        output="script finished\nREWARD=0 rc=0 runner=RUNNER=custom\n",
        grade="runner=custom rc=0\nrc=0 custom: exit-code grading\n",
        runner="custom",
    )
    assert not trial_tests_executed(custom)
    node_run = _write_verifier_trial(
        tmp_path / "node-run",
        output="42\n",
        grade="runner=node-run rc=0\nrc=0 node-run: exit-code grading\n",
        runner="node-run",
    )
    assert not trial_tests_executed(node_run)


def test_tests_executed_pytest_collection_errors_stay_noexec(tmp_path: Path) -> None:
    collected_none = _write_verifier_trial(
        tmp_path / "collected-none",
        output="collected 0 items\n",
        grade="runner=pytest rc=0\n",
        runner="pytest",
    )
    assert not trial_tests_executed(collected_none)
    error_only = _write_verifier_trial(
        tmp_path / "error-only",
        output="ERROR test_x.py\n1 error in 0.42s\n",
        grade="runner=pytest rc=1\n",
        runner="pytest",
    )
    assert not trial_tests_executed(error_only)
    failed_summary = _write_verifier_trial(
        tmp_path / "failed-summary",
        output="2 failed in 0.79s\n",
        grade="runner=pytest rc=1\n",
        runner="pytest",
    )
    assert trial_tests_executed(failed_summary)


def test_combine_blank_fix_blocks_known_patch_pass() -> None:
    digest = "sha256:abc"
    controls = {
        **blank_row(TASK_ID, manifest_version="mimo-clean-v1", backend="daytona"),
        "final_digest": digest,
        "nop": "0",
        "oracle": "n/a",
        "ladder_verdict": "",
        "run_ids": "mimo-census-v1-daytona-000000-nop",
    }
    ladder = {
        **blank_row(TASK_ID, manifest_version="mimo-clean-v1", backend="modal"),
        "final_digest": digest,
        "nop": "",
        "oracle": "",
        "ladder_verdict": "clean",
        "ladder_version": "1.3.0",
        "run_ids": "mimo-census-v1-modal-000000-cheat",
    }
    # Fix phase not run: known-patch default stays unverified even though
    # controls+ladder are clean.
    pending = combine_task_rows([controls, ladder], final_digest=digest)
    assert pending is not None
    assert pending["verify"] == VERIFY_UNVERIFIED
    # Same evidence with no reference fix passes via explicit n/a.
    passing = combine_task_rows(
        [{**controls, "census_locations": "n/a"}, ladder],
        has_reference_fix=False,
        final_digest=digest,
    )
    assert passing is not None
    assert passing["verify"] == VERIFY_PASS


def test_run_task_record_marks_fix_na_without_reference(tmp_path: Path, monkeypatch: Any) -> None:
    def fail_fix(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("no reference fix; probe must not run")

    monkeypatch.setattr(mimo_census, "census_fix_content", fail_fix)
    record = run_task_record(
        {
            "task_id": TASK_ID,
            "manifest_row": _manifest_row(reference_fix="none"),
            "manifest_version": "mimo-clean-v1",
            "primary": str(tmp_path),
            "jobs_root": str(tmp_path / "jobs"),
            "backend": "docker",
            "timeout_seconds": 10,
            "root": str(tmp_path),
            "checks": ["fix"],
            "ledger_row": {"task_id": TASK_ID},
        }
    )
    assert record["census_locations"] == "n/a"
    assert record["nop"] == ""
    assert record["ladder_verdict"] == ""


def test_normalize_modal_resource_policy() -> None:
    assert normalize_modal_resource_policy(None) is None
    assert normalize_modal_resource_policy("auto") is None
    assert normalize_modal_resource_policy("AUTO") is None
    assert normalize_modal_resource_policy("limit") == "limit"
    assert policy_row_value(None) == "auto"
    assert policy_row_value("limit") == "limit"
    assert expected_resource_kwargs(None) == {}
    assert expected_resource_kwargs("limit") == {
        "cpu_enforcement_policy": "limit",
        "memory_enforcement_policy": "limit",
    }
    with pytest.raises(ValueError):
        normalize_modal_resource_policy("turbo")


def test_recorded_resource_kwargs_prefers_config_then_command(tmp_path: Path) -> None:
    cell = _write_scored_cell(
        tmp_path / "cell",
        digest="sha256:abc",
        agent="nop",
        backend="modal",
        reward=0.0,
        junit=True,
        resource_kwargs={
            "cpu_enforcement_policy": "limit",
            "memory_enforcement_policy": "limit",
        },
    )
    assert recorded_resource_kwargs(cell) == {
        "cpu_enforcement_policy": "limit",
        "memory_enforcement_policy": "limit",
    }
    plain = _write_scored_cell(
        tmp_path / "plain", digest="sha256:abc", agent="nop", backend="modal"
    )
    assert recorded_resource_kwargs(plain) == {}
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "config.json").write_text(json.dumps({"agents": [{"name": "nop"}]}))
    (tokens / "lab-metadata.json").write_text(
        json.dumps(
            {
                "command": [
                    "harbor",
                    "run",
                    "--env",
                    "modal",
                    "--environment-kwarg",
                    "cpu_enforcement_policy=limit",
                ],
                "task_staging": {"source_package_digest": "sha256:abc"},
            }
        )
    )
    assert recorded_resource_kwargs(tokens) == {"cpu_enforcement_policy": "limit"}


def test_cell_reusable_binds_resource_policy(tmp_path: Path) -> None:
    limit_cell = _write_scored_cell(
        tmp_path / "limit",
        digest="sha256:abc",
        agent="nop",
        backend="modal",
        reward=0.0,
        junit=True,
        resource_kwargs={
            "cpu_enforcement_policy": "limit",
            "memory_enforcement_policy": "limit",
        },
    )
    base = {
        "expected_digest": "sha256:abc",
        "expected_agent": "nop",
        "expected_backend": "modal",
    }
    assert cell_reusable(
        limit_cell, **base, expected_resource_kwargs=expected_resource_kwargs("limit")
    )
    assert not cell_reusable(
        limit_cell, **base, expected_resource_kwargs=expected_resource_kwargs(None)
    )
    auto_cell = _write_scored_cell(
        tmp_path / "auto",
        digest="sha256:abc",
        agent="nop",
        backend="modal",
        reward=0.0,
        junit=True,
    )
    assert cell_reusable(auto_cell, **base, expected_resource_kwargs=expected_resource_kwargs(None))
    assert not cell_reusable(
        auto_cell, **base, expected_resource_kwargs=expected_resource_kwargs("limit")
    )


def test_scannable_cell_dirs_newest_first(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    base = "mimo-census-v1-modal-000000-nop"
    (jobs / base).mkdir()
    (jobs / f"{base}-attempt2").mkdir()
    (jobs / f"{base}-attempt10").mkdir()
    (jobs / "mimo-census-v1-modal-000000-cheat").mkdir()
    (jobs / "unrelated.txt").write_text("x")
    names = [path.name for path in scannable_cell_dirs(jobs, base)]
    assert names == [f"{base}-attempt10", f"{base}-attempt2", base]
    assert scannable_cell_dirs(tmp_path / "absent", base) == []


def test_reuse_scans_attempt_cells_not_just_base(tmp_path: Path, monkeypatch: Any) -> None:
    jobs = tmp_path / "jobs" / TASK_ID
    _write_scored_cell(
        jobs / "mimo-census-v1-docker-000000-nop",
        digest="sha256:old-generation",
        agent="nop",
        backend="docker",
        reward=0.0,
        junit=True,
    )
    _write_scored_cell(
        jobs / "mimo-census-v1-docker-000000-nop-attempt2",
        digest="sha256:abc",
        agent="nop",
        backend="docker",
        reward=0.0,
        junit=True,
    )

    def fail_launch(**kwargs: Any) -> Path:
        raise AssertionError("valid attempt must reuse, not launch")

    monkeypatch.setattr(mimo_census, "run_cell", fail_launch)
    partial = census_task(
        task_id=TASK_ID,
        manifest_row=_manifest_row(final_digest="sha256:abc"),
        primary=tmp_path,
        jobs_root=tmp_path / "jobs",
        backend="docker",
        timeout_seconds=10,
        root=tmp_path,
        checks=("nop",),
    )
    assert partial["nop"] == "0"


def test_auto_and_limit_cells_never_share(tmp_path: Path, monkeypatch: Any) -> None:
    jobs = tmp_path / "jobs" / TASK_ID
    _write_scored_cell(
        jobs / "mimo-census-v1-modal-000000-nop",
        digest="sha256:abc",
        agent="nop",
        backend="modal",
        reward=0.0,
        junit=True,
    )
    launched: list[str] = []

    def fake_run_cell(**kwargs: Any) -> Path:
        launched.append(str(kwargs["name"]))
        assert kwargs["modal_resource_policy"] == "limit"
        cell = jobs / str(kwargs["name"])
        cell.mkdir(parents=True, exist_ok=True)
        return cell

    monkeypatch.setattr(mimo_census, "run_cell", fake_run_cell)
    census_task(
        task_id=TASK_ID,
        manifest_row=_manifest_row(final_digest="sha256:abc"),
        primary=tmp_path,
        jobs_root=tmp_path / "jobs",
        backend="modal",
        timeout_seconds=10,
        root=tmp_path,
        checks=("nop",),
        modal_resource_policy="limit",
    )
    # The auto base cell is not reusable under limit: a fresh attempt runs.
    assert launched == ["mimo-census-v1-modal-000000-nop-attempt2"]


def test_run_cell_policy_forwarding_and_refusals(tmp_path: Path, monkeypatch: Any) -> None:
    import evallab.execution_contracts as ec
    import evallab.queue as queue_mod
    from evallab.mimo_census import run_cell

    class FakeRequest(dict):  # type: ignore[type-arg]
        def __init__(
            self,
            *args: Any,
            modal_app_name: Any = None,
            modal_resource_policy: Any = None,
            **kwargs: Any,
        ):
            super().__init__(
                modal_app_name=modal_app_name,
                modal_resource_policy=modal_resource_policy,
                **kwargs,
            )

    class FakeExecutor:
        @classmethod
        def from_repo(cls, root: object) -> FakeExecutor:
            return cls()

        def execute_direct(self, request: Any) -> Path:
            cell = Path(request["jobs_dir"]) / str(request["name"])
            cell.mkdir(parents=True, exist_ok=True)
            return cell

    monkeypatch.setattr(ec, "RunRequest", FakeRequest)
    monkeypatch.setattr(queue_mod, "Executor", FakeExecutor)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    captured: dict[str, Any] = {}

    class RecordingExecutor(FakeExecutor):
        def execute_direct(self, request: Any) -> Path:
            captured.update(dict(request))
            return super().execute_direct(request)

    monkeypatch.setattr(queue_mod, "Executor", RecordingExecutor)
    run_cell(
        package=pkg,
        agent="nop",
        name="m1",
        jobs_dir=jobs,
        backend="modal",
        timeout_seconds=10,
        modal_resource_policy="limit",
        root=tmp_path,
    )
    assert captured.get("modal_resource_policy") == "limit"
    assert captured.get("modal_app_name") == "mimo-clean-census"
    with pytest.raises(ValueError):
        run_cell(
            package=pkg,
            agent="nop",
            name="d1",
            jobs_dir=jobs,
            backend="docker",
            timeout_seconds=10,
            modal_resource_policy="limit",
            root=tmp_path,
        )
    with pytest.raises(ValueError):
        run_cell(
            package=pkg,
            agent="nop",
            name="m2",
            jobs_dir=jobs,
            backend="modal",
            timeout_seconds=10,
            modal_resource_policy="turbo",
            root=tmp_path,
        )


def test_probe_completion_predicate() -> None:
    complete = {
        "setup_rc": "0",
        "ready": "yes",
        "probe_rc": "0",
        "scan_complete": True,
        "scan_rc": "0",
    }
    assert probe_completion(complete) == (True, "")
    assert probe_completion({**complete, "scan_complete": False, "scan_rc": "1"}) == (
        True,
        "",
    )
    assert probe_completion(None) == (False, "no probe result")
    assert probe_completion({}) == (False, "no probe result")
    done, why = probe_completion({**complete, "setup_rc": "1"})
    assert not done and "setup_rc" in why
    done, why = probe_completion({**complete, "ready": "no"})
    assert not done and "ready" in why
    done, why = probe_completion({**complete, "probe_rc": "unknown"})
    assert not done and "probe_rc" in why
    done, why = probe_completion({**complete, "scan_complete": False, "scan_rc": "2"})
    assert not done and "scan" in why
    assert probe_evidence(complete) == complete


def test_known_patch_incomplete_published_never_blind(tmp_path: Path, monkeypatch: Any) -> None:
    import evallab.fix_content_census as fcc
    from evallab.mimo_census import census_fix_content

    clean = _write_patch_package(tmp_path, "clean")
    run = _write_patch_package(tmp_path, "runpkg")
    patch = tmp_path / "fix.patch"
    patch.write_text(KNOWN_PATCH, encoding="utf-8")
    measured: list[str] = []

    def fake_stage(stage_dir: object, *args: Any, **kwargs: Any) -> None:
        return None

    def fake_run(
        image: str,
        workdir: str,
        fix: str,
        stage: object,
        out: object,
        *,
        backend: str = "docker",
        egress_lock: bool = True,
    ) -> None:
        assert egress_lock is True
        measured.append(Path(str(out)).name)
        _fake_probe_out(Path(str(out)), hits=0)
        if "published" in str(out):
            (Path(str(out)) / "setup_rc").write_text("1", encoding="utf-8")

    monkeypatch.setattr(fcc, "stage_probe", fake_stage)
    monkeypatch.setattr(fcc, "run_probe", fake_run)
    result = census_fix_content(
        task_id="task-k",
        clean_package=clean,
        run_package=run,
        language="python",
        scratch_root=tmp_path / "scratch",
        reference_fix=patch,
    )
    assert result["census_locations"] is None
    assert "published probe incomplete" in str(result["reason"])
    assert measured == ["out-published", "out-clean"]
    metadata = json.loads(
        (tmp_path / "scratch" / "task-k" / "census.meta.json").read_text(encoding="utf-8")
    )
    assert not probe_completion(metadata["published_complete"])[0]
    assert probe_completion(metadata["clean"]) == (True, "")


def test_known_patch_incomplete_clean_never_zero(tmp_path: Path, monkeypatch: Any) -> None:
    import evallab.fix_content_census as fcc
    from evallab.mimo_census import census_fix_content

    clean = _write_patch_package(tmp_path, "clean")
    run = _write_patch_package(tmp_path, "runpkg")
    patch = tmp_path / "fix.patch"
    patch.write_text(KNOWN_PATCH, encoding="utf-8")

    def fake_stage(stage_dir: object, *args: Any, **kwargs: Any) -> None:
        return None

    def fake_run(
        image: str,
        workdir: str,
        fix: str,
        stage: object,
        out: object,
        *,
        backend: str = "docker",
        egress_lock: bool = True,
    ) -> None:
        out_path = Path(str(out))
        _fake_probe_out(out_path, hits=1 if "published" in str(out) else 0)
        if "published" not in str(out):
            (out_path / "ready").write_text("no", encoding="utf-8")

    monkeypatch.setattr(fcc, "stage_probe", fake_stage)
    monkeypatch.setattr(fcc, "run_probe", fake_run)
    result = census_fix_content(
        task_id="task-k",
        clean_package=clean,
        run_package=run,
        language="python",
        scratch_root=tmp_path / "scratch",
        reference_fix=patch,
    )
    assert result["census_locations"] is None
    assert "clean probe incomplete" in str(result["reason"])


@pytest.mark.parametrize(
    "published_complete",
    [
        {},
        {"setup_rc": "0", "ready": "yes", "probe_rc": "0", "scan_rc": "0"},
    ],
)
def test_blind_reuse_needs_both_probe_completions(
    tmp_path: Path, monkeypatch: Any, published_complete: dict[str, Any]
) -> None:
    import hashlib

    import evallab.fix_content_census as fcc
    from evallab.mimo_census import census_fix_content

    clean = _write_patch_package(tmp_path, "clean")
    run = _write_patch_package(tmp_path, "runpkg")
    patch = tmp_path / "fix.patch"
    patch.write_text(KNOWN_PATCH, encoding="utf-8")
    patch_sha = hashlib.sha256(KNOWN_PATCH.encode()).hexdigest()
    setup_sha = hashlib.sha256(b"# ship\n").hexdigest()
    scratch = tmp_path / "scratch" / "task-k"
    scratch.mkdir(parents=True)
    (scratch / "census.meta.json").write_text(
        json.dumps(
            {
                "image": "img:1",
                "clean_setup_sha256": setup_sha,
                "patch_sha": patch_sha,
                "probe_blind": True,
                "published": {"hits_total": 0, "open_leak": "no"},
                "published_complete": published_complete,
            }
        ),
        encoding="utf-8",
    )

    def fail_stage(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("incomplete blind metadata must re-probe, not reuse")

    monkeypatch.setattr(fcc, "stage_probe", fail_stage)
    with pytest.raises(AssertionError):
        census_fix_content(
            task_id="task-k",
            clean_package=clean,
            run_package=run,
            language="python",
            scratch_root=tmp_path / "scratch",
            reference_fix=patch,
        )


def test_report_row_key_separates_generations_and_policies() -> None:
    base = blank_row(TASK_ID, manifest_version="mimo-clean-v1", backend="modal")
    row = {**base, "final_digest": "sha256:v2", "modal_resource_policy": "auto"}
    assert report_row_key(row) == (TASK_ID, "modal", "mimo-clean-v1", "sha256:v2", "auto")
    other_version = {**row, "manifest_version": "mimo-clean-v2"}
    assert report_row_key(other_version) != report_row_key(row)
    other_digest = {**row, "final_digest": "sha256:v3"}
    assert report_row_key(other_digest) != report_row_key(row)
    other_policy = {**row, "modal_resource_policy": "limit"}
    assert report_row_key(other_policy) != report_row_key(row)
    legacy = {k: v for k, v in row.items() if k != "modal_resource_policy"}
    assert report_row_key(legacy) == report_row_key(row)


def _write_manifest(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    from evallab.mimo_clean import MANIFEST_COLUMNS, write_manifest

    manifest = tmp_path / "manifest.csv"
    write_manifest(
        [{key: row.get(key, "") for key in MANIFEST_COLUMNS} | row for row in rows],
        manifest,
    )
    return manifest


def _manifest_row_full(task_id: str, **overrides: Any) -> dict[str, str]:
    row = {
        "task_id": task_id,
        "domain": "code",
        "language": "python",
        "chain": "strip-future-history@1",
        "final_digest": "sha256:v3",
        "package_path": "derived/x",
        "reference_fix": "/fixes/x.patch",
        "status": "built",
        "reason": "",
        "run_digest": "",
        "oracle_label": "",
        "verify": "unverified",
    }
    row.update(overrides)
    return row


def test_report_mixed_v2_controls_v3_ladder_must_not_pass(tmp_path: Path) -> None:
    import argparse

    from evallab.mimo_census import _report_command
    from evallab.mimo_census import load_manifest as _load_manifest

    manifest = _write_manifest(
        tmp_path,
        [
            _manifest_row_full(TASK_ID, final_digest="sha256:v3"),
            _manifest_row_full(
                "format-code-task-000001",
                final_digest="sha256:ok",
                reference_fix="none",
            ),
        ],
    )
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    v2_controls = {
        **blank_row(TASK_ID, manifest_version="mimo-clean-v1", backend="daytona"),
        "final_digest": "sha256:v2",
        "nop": "0",
        "oracle": "n/a",
        "ladder_verdict": "",
        "run_ids": "mimo-census-v1-daytona-000000-nop",
    }
    v3_ladder = {
        **blank_row(TASK_ID, manifest_version="mimo-clean-v2", backend="modal"),
        "final_digest": "sha256:v3",
        "nop": "",
        "oracle": "",
        "ladder_verdict": "clean",
        "ladder_version": "1.3.0",
        "run_ids": "mimo-census-v1-modal-000000-cheat",
    }
    passing_other = {
        **blank_row("format-code-task-000001", manifest_version="mimo-clean-v1", backend="docker"),
        "final_digest": "sha256:ok",
        "nop": "0",
        "oracle": "n/a",
        "ladder_verdict": "clean",
        "census_locations": "n/a",
        "run_ids": "x",
    }
    with (jobs / "census-rows.jsonl").open("w", encoding="utf-8") as handle:
        for row in (v2_controls, v3_ladder, passing_other):
            handle.write(json.dumps(row) + "\n")
    receipt = tmp_path / "receipt"
    receipt.mkdir()
    args = argparse.Namespace(
        manifest=manifest,
        manifest_version="mimo-clean-v2",
        receipt_dir=receipt,
        jobs_dir=jobs,
        cost_default_usd=0.0,
    )
    assert _report_command(args, tmp_path) == 0
    loaded = load_results(receipt / "results.csv")
    by_key = {(row["task_id"], row["manifest_version"], row["final_digest"]): row for row in loaded}
    # Generations stay separate rows with truthful original versions.
    assert by_key[(TASK_ID, "mimo-clean-v1", "sha256:v2")]["nop"] == "0"
    assert by_key[(TASK_ID, "mimo-clean-v2", "sha256:v3")]["ladder_verdict"] == "clean"
    assert by_key[(TASK_ID, "mimo-clean-v2", "sha256:v3")]["nop"] == ""
    manifest_rows = {row["task_id"]: row for row in _load_manifest(manifest)}
    # v3 has only ladder evidence: unverified, never pass.
    assert manifest_rows[TASK_ID]["verify"] == VERIFY_UNVERIFIED
    # The fully evidenced task on its exact digest passes.
    assert manifest_rows["format-code-task-000001"]["verify"] == VERIFY_PASS


@pytest.mark.parametrize("with_older_generation", [False, True])
def test_report_cost_sums_per_task_backend(tmp_path: Path, with_older_generation: bool) -> None:
    import argparse

    from evallab.mimo_census import _report_command

    manifest = _write_manifest(tmp_path, [_manifest_row_full(TASK_ID)])
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    row = {
        **blank_row(TASK_ID, manifest_version="mimo-clean-v1", backend="modal"),
        "final_digest": "sha256:v3",
        "nop": "0",
        "oracle": "n/a",
        "ladder_verdict": "clean",
        "census_locations": "0",
        "run_ids": "x",
    }
    with (jobs / "census-rows.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
        if with_older_generation:
            older = {**row, "final_digest": "sha256:v2"}
            handle.write(json.dumps(older) + "\n")
    receipt = tmp_path / "receipt"
    receipt.mkdir()
    with (receipt / "spend.jsonl").open("w", encoding="utf-8") as handle:
        for batch in ("b1", "b2"):
            handle.write(
                json.dumps(
                    {
                        "batch_id": batch,
                        "backend": "modal",
                        "tasks": TASK_ID,
                        "n_tasks": 1,
                        "actual_usd": 1.5,
                        "recorded_at": "2026-10-10T00:00:00+00:00",
                    }
                )
                + "\n"
            )
    args = argparse.Namespace(
        manifest=manifest,
        manifest_version="mimo-clean-v1",
        receipt_dir=receipt,
        jobs_dir=jobs,
        cost_default_usd=0.0,
    )
    assert _report_command(args, tmp_path) == 0
    loaded = load_results(receipt / "results.csv")
    assert sum(float(record["cost_usd"]) for record in loaded) == 3.0
    assert all(
        record["cost_usd"] == ("1.5000" if with_older_generation else "3.0000") for record in loaded
    )


def test_run_task_record_forwards_policy(tmp_path: Path, monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    def fake_census_task(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {
            "nop": "0",
            "oracle": "n/a",
            "ladder_verdict": "clean",
            "ladder_cracking_attacks": "",
            "ladder_executed": "",
            "ladder_version": "1.3.0",
            "run_ids": "nop-job",
        }

    monkeypatch.setattr(mimo_census, "census_task", fake_census_task)
    record = run_task_record(
        {
            "task_id": TASK_ID,
            "manifest_row": _manifest_row(),
            "manifest_version": "mimo-clean-v1",
            "primary": str(tmp_path),
            "jobs_root": str(tmp_path / "jobs"),
            "backend": "modal",
            "timeout_seconds": 10,
            "root": str(tmp_path),
            "checks": ["nop", "oracle", "ladder"],
            "ledger_row": None,
            "modal_resource_policy": "limit",
        }
    )
    assert seen.get("modal_resource_policy") == "limit"
    assert record["modal_resource_policy"] == "limit"
