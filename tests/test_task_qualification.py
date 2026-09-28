"""Backend qualification behavior: classification, cost, broken export (HAR-88)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from evallab import cli
from evallab.evidence.facts import extract_trial_fact, trial_environment_type
from evallab.results import load_job
from evallab.task_qualification import (
    BROKEN_EXPORT_SCHEMA,
    TABLE_FILENAME,
    classify_trial,
    collect_jobs,
    detect_grader_collection_failure,
    estimate_cost_usd,
    export_broken,
    is_backend_quota_failure,
    read_task_qualification_parquet,
    status_for,
    summarize_qualification,
    write_task_qualification_parquet,
)

TASK_TOML = """\
schema_version = "1.4"

[task]
name = "mimo-v2.6-rl/qual-task"
description = "qualification fixture"

[metadata]
domain = "terminal"
source_id = "qual-task"

[agent]
timeout_sec = 100.0

[verifier]
timeout_sec = 60.0

[environment]
cpus = 2
memory_mb = 2048
storage_mb = 10240
"""


def _write_staged_task(root: Path, *, toml: str = TASK_TOML) -> Path:
    staged = root / "staged-task"
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "task.toml").write_text(toml)
    return staged


def _timing(start: str, end: str | None) -> dict[str, str | None]:
    return {"started_at": start, "finished_at": end}


def _write_trial(
    job: Path,
    name: str,
    *,
    agent: str = "nop",
    env_type: str = "docker",
    import_path: str | None = None,
    reward: float | None = 0.0,
    exception: dict | None = None,
    agent_execution: bool = True,
    stability_runs: list | None = None,
    staged: Path | None = None,
    trial_id: str = "11111111-1111-1111-1111-111111111111",
) -> Path:
    trial = job / name
    trial.mkdir(parents=True, exist_ok=True)
    (trial / "trial.log").write_text("log")
    (trial / "config.json").write_text(json.dumps({
        "environment": {"type": env_type, "import_path": import_path},
        "agent": {"name": agent},
        "trial_name": name,
    }))
    lock_task: dict = {"name": "qual-task"}
    if staged is not None:
        lock_task["path"] = str(staged)
    (trial / "lock.json").write_text(json.dumps({
        "task": lock_task,
        "environment": {"type": env_type, "import_path": import_path},
        "agent": {"name": agent},
    }))
    verifier_rewards = {} if reward is None else {"reward": reward}
    (trial / "result.json").write_text(json.dumps({
        "id": trial_id,
        "task_name": "mimo-v2.6-rl/qual-task",
        "trial_name": name,
        "config": {
            "environment": {"type": env_type, "import_path": import_path},
            "agent": {"name": agent},
        },
        "agent_info": {"name": agent, "version": "1.0.0"},
        "agent_result": {},
        "verifier_result": {"rewards": verifier_rewards},
        "exception_info": exception,
        "started_at": "2026-09-28T00:00:00Z",
        "finished_at": "2026-09-28T00:01:00Z",
        "environment_setup": _timing("2026-09-28T00:00:00Z", "2026-09-28T00:00:05Z"),
        "agent_setup": _timing("2026-09-28T00:00:05Z", "2026-09-28T00:00:06Z"),
        "agent_execution": _timing("2026-09-28T00:00:06Z", "2026-09-28T00:00:50Z")
        if agent_execution
        else None,
        "verifier": _timing("2026-09-28T00:00:50Z", "2026-09-28T00:01:00Z"),
    }))
    if reward is not None:
        verifier_dir = trial / "verifier"
        verifier_dir.mkdir(exist_ok=True)
        (verifier_dir / "reward.txt").write_text(f"{reward}\n")
    if stability_runs is not None:
        verifier_dir = trial / "verifier"
        verifier_dir.mkdir(exist_ok=True)
        (verifier_dir / "stability.json").write_text(
            json.dumps({"runs": [{"reward": value} for value in stability_runs]})
        )
    return trial


def _write_job(
    root: Path,
    name: str,
    trials: list[dict],
    *,
    version_digest: str = "sha256:" + "a" * 64,
    harbor_digest: str = "sha256:" + "b" * 64,
    staged: Path | None = None,
) -> Path:
    job = root / name
    job.mkdir(parents=True, exist_ok=True)
    (job / "result.json").write_text(json.dumps({
        "id": "22222222-2222-2222-2222-222222222222",
        "n_total_trials": len(trials),
        "stats": {},
        "finished_at": "2026-09-28T00:02:00Z",
    }))
    (job / "config.json").write_text("{}")
    (job / "lock.json").write_text("{}")
    if staged is None:
        staged = _write_staged_task(root / f"{name}-staged")
    for index, kwargs in enumerate(trials):
        trial_id = f"11111111-1111-1111-1111-{index:012d}"
        _write_trial(job, kwargs.pop("name"), staged=staged, trial_id=trial_id, **kwargs)
    (job / "lab-metadata.json").write_text(json.dumps({
        "task_staging": {
            "source_package_digest": version_digest,
            "source_harbor_digest": harbor_digest,
            "source_task_basename": "qual-task",
        }
    }))
    return job


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    monkeypatch.setattr(cli, "load_local_env", lambda _path: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")


# ---------------------------------------------------------------- environment type


def test_environment_type_from_config_and_wrappers() -> None:
    assert trial_environment_type({"environment": {"type": "docker"}}) == "docker"
    assert trial_environment_type({"environment": {"type": "daytona"}}) == "daytona"
    assert (
        trial_environment_type(
            {"environment": {
                "type": "docker",
                "import_path": "evallab.harbor_daytona:BoundedDaytonaEnvironment",
            }}
        )
        == "daytona"
    )
    assert (
        trial_environment_type(
            {"environment": {
                "type": "docker",
                "import_path": "evallab.harbor_daytona:SecretSafeDaytonaEnvironment",
            }}
        )
        == "daytona"
    )
    # Config wins; lock is the fallback.
    assert (
        trial_environment_type(
            {"environment": {"type": "docker"}}, {"environment": {"type": "daytona"}}
        )
        == "docker"
    )
    assert (
        trial_environment_type({}, {"environment": {"type": "daytona"}}) == "daytona"
    )
    assert trial_environment_type({}, {}) is None
    assert trial_environment_type(None) is None


def test_extract_trial_fact_records_environment_type(tmp_path: Path) -> None:
    staged = _write_staged_task(tmp_path)
    job = _write_job(
        tmp_path, "job1", [{"name": "t1__a", "env_type": "docker"}], staged=staged
    )
    record = load_job(job)
    assert len(record.trials) == 1
    fact = extract_trial_fact(record, record.trials[0])
    assert fact.environment_type == "docker"


def test_extract_trial_fact_maps_daytona_wrapper(tmp_path: Path) -> None:
    staged = _write_staged_task(tmp_path)
    job = _write_job(
        tmp_path,
        "job2",
        [{
            "name": "t1__a",
            "env_type": "docker",
            "import_path": "evallab.harbor_daytona:BoundedDaytonaEnvironment",
        }],
        staged=staged,
    )
    fact = extract_trial_fact(load_job(job), load_job(job).trials[0])
    assert fact.environment_type == "daytona"


# ---------------------------------------------------------------- classification


def test_classify_synthetic_job_statuses_and_reasons(tmp_path: Path) -> None:
    staged = _write_staged_task(tmp_path)
    job = _write_job(tmp_path, "jobq", [
        {"name": "t__setup", "exception": {
            "exception_type": "EnvironmentSetupError",
            "exception_message": "docker build failed",
            "occurred_at": "2026-09-28T00:00:01Z",
        }, "agent_execution": False, "reward": None},
        {"name": "t__verifier_timeout", "exception": {
            "exception_type": "VerifierTimeoutError",
            "exception_message": "verifier timed out after 60s",
            "occurred_at": "2026-09-28T00:00:55Z",
        }, "reward": None},
        {"name": "t__nop_pass", "reward": 1.0},
        {"name": "t__ok", "reward": 0.0},
        {"name": "t__unstable", "reward": 0.0, "stability_runs": [0.0, 1.0]},
        {"name": "t__quota", "exception": {
            "exception_type": "DaytonaError",
            "exception_message": "Total CPU limit exceeded for organization tier (quota)",
            "occurred_at": "2026-09-28T00:00:01Z",
        }, "agent_execution": False, "reward": None},
    ], staged=staged)
    rows = {row["trial_name"]: row for row in collect_jobs([job])}
    assert len(rows) == 6

    assert rows["t__setup"]["status"] == "broken"
    assert rows["t__setup"]["reasons"] == ["setup_failed"]
    assert rows["t__setup"]["infra_error_phase"] == "environment"
    assert rows["t__setup"]["setup_ok"] is False

    assert rows["t__verifier_timeout"]["status"] == "broken"
    assert rows["t__verifier_timeout"]["reasons"] == ["verifier_timeout"]

    assert rows["t__nop_pass"]["status"] == "broken"
    assert rows["t__nop_pass"]["reasons"] == ["nop_passes"]

    assert rows["t__ok"]["status"] == "ok"
    assert rows["t__ok"]["reasons"] == []
    assert rows["t__ok"]["verifier_completed"] is True

    assert rows["t__unstable"]["status"] == "broken"
    assert rows["t__unstable"]["reasons"] == ["unstable_verifier"]
    assert rows["t__unstable"]["repeat_rewards"] == [0.0, 1.0]

    assert rows["t__quota"]["status"] == "inconclusive"
    assert rows["t__quota"]["reasons"] == ["backend_quota"]

    for row in rows.values():
        assert row["backend"] == "docker"
        assert row["domain"] == "terminal"
        assert row["task_id"] == "qual-task"
        assert row["est_cost_usd"] is None  # docker is never priced


def test_backend_quota_matcher_cases() -> None:
    assert is_backend_quota_failure("DaytonaRateLimitError", "rate limited") is True
    assert is_backend_quota_failure("DaytonaError", "memory limit reached") is True
    assert is_backend_quota_failure("DaytonaError", "disk limit exceeded") is True
    assert is_backend_quota_failure("ProviderError", "quota exhausted") is True
    assert is_backend_quota_failure("HTTPError", "HTTP 429 quota exceeded") is True
    assert is_backend_quota_failure("HTTPError", "HTTP 403 rate limited") is True
    assert is_backend_quota_failure("EnvironmentSetupError", "docker build failed") is False
    assert is_backend_quota_failure(None, None) is False
    # A bare line number in a traceback is not a quota signal.
    assert is_backend_quota_failure("ValueError", 'File "x.py", line 429') is False


def test_status_for_quota_only_is_inconclusive() -> None:
    assert status_for([]) == "ok"
    assert status_for(["backend_quota"]) == "inconclusive"
    assert status_for(["setup_failed"]) == "broken"
    assert status_for(["backend_quota", "nop_passes"]) == "broken"


def test_classify_verifier_error_and_reward_missing() -> None:
    base = {
        "agent_name": "nop",
        "verifier_completed": True,
        "exception_message": None,
        "agent_execution_started": True,
        "repeat_rewards": None,
    }
    assert classify_trial(
        reward=None, exception_type="VerifierError", **base
    ) == ["verifier_error"]
    assert classify_trial(
        reward=None, exception_type=None, **base
    ) == ["reward_missing"]
    assert classify_trial(
        reward=0.0, exception_type=None, **base
    ) == []
    # Repeats that agree add no reason; stable repeats keep an ok trial ok.
    assert classify_trial(
        reward=0.0, exception_type=None, repeat_rewards=[0.0, 0.0], **{
            key: value for key, value in base.items() if key != "repeat_rewards"
        }
    ) == []


# ---------------------------------------------------------------- cost model


def test_cost_formula_on_known_resources() -> None:
    cost = estimate_cost_usd(
        backend="daytona",
        sandbox_seconds=3600.0,
        cpus=4,
        memory_mb=8192,
        storage_mb=30720,
        rate_backend="daytona",
    )
    assert cost == pytest.approx(4 * 0.0504 + 8 * 0.0162 + 25 * 0.000108)
    assert cost == pytest.approx(0.3339)


def test_cost_null_off_rate_card_and_missing_inputs() -> None:
    assert estimate_cost_usd(
        backend="docker", sandbox_seconds=3600.0, cpus=4,
        memory_mb=8192, storage_mb=30720,
    ) is None
    assert estimate_cost_usd(
        backend="daytona", sandbox_seconds=None, cpus=4,
        memory_mb=8192, storage_mb=30720,
    ) is None
    # Storage under the 5 GiB free tier bills nothing for disk.
    small = estimate_cost_usd(
        backend="daytona", sandbox_seconds=3600.0, cpus=1,
        memory_mb=1024, storage_mb=1024,
    )
    assert small == pytest.approx(1 * 0.0504 + 1 * 0.0162)
    # Unset resources count as 0, never invented.
    assert estimate_cost_usd(
        backend="daytona", sandbox_seconds=3600.0, cpus=None,
        memory_mb=None, storage_mb=None,
    ) == pytest.approx(0.0)


def test_collect_prices_daytona_trial(tmp_path: Path) -> None:
    staged = _write_staged_task(tmp_path)
    job = _write_job(
        tmp_path, "jobd", [{"name": "t__a", "env_type": "daytona", "reward": 0.0}],
        staged=staged,
    )
    (row,) = collect_jobs([job])
    assert row["backend"] == "daytona"
    assert row["status"] == "ok"
    expected = (60.0 / 3600.0) * (2 * 0.0504 + 2 * 0.0162 + 5 * 0.000108)
    assert row["est_cost_usd"] == pytest.approx(expected)


# ---------------------------------------------------------------- report + parquet round-trip


def test_report_counts_and_totals(tmp_path: Path) -> None:
    staged = _write_staged_task(tmp_path)
    job = _write_job(tmp_path, "jobr", [
        {"name": "t__ok", "reward": 0.0},
        {"name": "t__bad", "reward": 1.0},
        {"name": "t__quota", "exception": {
            "exception_type": "DaytonaError",
            "exception_message": "quota exceeded",
            "occurred_at": "2026-09-28T00:00:01Z",
        }, "agent_execution": False, "reward": None},
    ], staged=staged)
    rows = collect_jobs([job])
    report = summarize_qualification(rows)
    assert "tasks=1 trials=3 ok=1 broken=1 inconclusive (backend_quota)=1" in report
    assert "nop_passes=1" in report
    assert "sandbox_hours=" in report and "est_cost_usd=" in report
    assert "median=" in report and "p90=" in report


def test_parquet_round_trip_contract_schema(tmp_path: Path) -> None:
    staged = _write_staged_task(tmp_path)
    job = _write_job(tmp_path, "jobp", [{"name": "t__a", "reward": 0.0}], staged=staged)
    rows = collect_jobs([job], produced_at="2026-09-28T00:00:00+00:00")
    out = tmp_path / TABLE_FILENAME
    write_task_qualification_parquet(rows, out)
    back = read_task_qualification_parquet(out)
    assert len(back) == 1
    assert back[0]["status"] == "ok"
    assert back[0]["repeat_rewards"] is None
    assert set(pq.read_table(out).schema.names) == {
        "task_version_digest", "harbor_digest", "task_id", "domain", "backend",
        "environment_import_path", "job_name", "trial_name", "agent_name",
        "started_at", "finished_at", "environment_setup_seconds",
        "agent_setup_seconds", "verifier_seconds", "trial_seconds", "setup_ok",
        "verifier_completed", "reward", "repeat_rewards", "infra_error_class",
        "infra_error_phase", "grader_error", "cpus", "memory_mb", "storage_mb",
        "sandbox_seconds", "est_cost_usd", "status", "reasons", "produced_at",
    }


# ---------------------------------------------------------------- export-broken


def _write_catalog_table(derived: Path, rows: list[dict]) -> Path:
    catalog = derived / "external/task_catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    out = catalog / TABLE_FILENAME
    write_task_qualification_parquet(rows, out)
    return out


def test_export_broken_digest_stable_and_content_addressed(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    staged = _write_staged_task(tmp_path)
    job = _write_job(tmp_path, "jobe", [
        {"name": "t__bad", "reward": 1.0},
        {"name": "t__ok", "reward": 0.0},
    ], staged=staged)
    rows = collect_jobs([job])
    for row in rows:
        row["backend"] = "daytona"
        if row["trial_name"] == "t__bad":
            row["finished_at"] = "2026-09-28T00:02:00Z"
    _write_catalog_table(derived, rows)

    first = export_broken(
        tmp_path / "broken.json", backend="daytona",
        repo_root=tmp_path, derived_root=derived,
    )
    assert first.n_broken == 1
    payload = json.loads(first.path.read_text())
    assert payload["schema"] == BROKEN_EXPORT_SCHEMA
    assert payload["backend"] == "daytona"
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["reasons"] == ["nop_passes"]
    assert item["task_id"] == "qual-task"
    assert len(item["trials"]) == 2
    canonical = json.dumps(
        {key: payload[key] for key in ("schema", "backend", "items")},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    assert payload["sha256"] == f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    again = export_broken(
        tmp_path / "broken-again.json", backend="daytona",
        repo_root=tmp_path, derived_root=derived,
    )
    assert again.sha256 == first.sha256

    other = export_broken(
        tmp_path / "broken-docker.json", backend="docker",
        repo_root=tmp_path, derived_root=derived,
    )
    assert other.n_broken == 0
    assert other.sha256 != first.sha256


def test_export_broken_uses_latest_trial_and_skips_inconclusive(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    staged = _write_staged_task(tmp_path)
    old = _write_job(
        tmp_path, "jobold", [
            {"name": "t__bad", "reward": 1.0},
        ],
        staged=staged,
        version_digest="sha256:" + "c" * 64,
    )
    rows = collect_jobs([old])
    for row in rows:
        row["backend"] = "daytona"
    # A newer ok trial for the same version heals it.
    rows.append({
        **rows[0],
        "job_name": "jobnew",
        "trial_name": "t__healed",
        "finished_at": "2026-10-01T00:00:00Z",
        "status": "ok",
        "reasons": [],
        "reward": 0.0,
    })
    # An inconclusive-only version is unrun at this tier, not broken.
    quota_row = {
        **rows[0],
        "task_version_digest": "sha256:" + "d" * 64,
        "job_name": "jobquota",
        "trial_name": "t__quota",
        "finished_at": "2026-10-01T00:00:00Z",
        "status": "inconclusive",
        "reasons": ["backend_quota"],
    }
    rows.append(quota_row)
    _write_catalog_table(derived, rows)

    result = export_broken(
        tmp_path / "broken.json", backend="daytona",
        repo_root=tmp_path, derived_root=derived,
    )
    assert result.n_broken == 0


def test_export_broken_refuses_without_table(tmp_path: Path) -> None:
    from evallab.task_catalog import CatalogError

    with pytest.raises(CatalogError):
        export_broken(
            tmp_path / "broken.json", backend="daytona",
            repo_root=tmp_path, derived_root=tmp_path / "derived",
        )


# ---------------------------------------------------------------- CLI wiring


def test_cli_qualify_collect_writes_table_and_summary(tmp_path: Path, capsys) -> None:
    staged = _write_staged_task(tmp_path)
    job = _write_job(tmp_path, "jobc", [{"name": "t__a", "reward": 0.0}], staged=staged)
    derived = tmp_path / "derived"
    code = cli.run_cli(
        ["tasks", "qualify-collect", str(job), "--output", str(
            derived / "external/task_catalog" / TABLE_FILENAME)],
        workspace=tmp_path,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "wrote 1 rows" in out
    assert "ok=1" in out
    back = read_task_qualification_parquet(
        derived / "external/task_catalog" / TABLE_FILENAME
    )
    assert back[0]["task_version_digest"] == "sha256:" + "a" * 64


def test_cli_export_broken_prints_sha(tmp_path: Path, capsys) -> None:
    derived = tmp_path / "derived"
    staged = _write_staged_task(tmp_path)
    job = _write_job(tmp_path, "jobx", [{"name": "t__a", "reward": 1.0}], staged=staged)
    rows = collect_jobs([job])
    for row in rows:
        row["backend"] = "daytona"
    _write_catalog_table(derived, rows)
    code = cli.run_cli(
        ["tasks", "catalog", "export-broken", "--backend", "daytona",
         "--out", str(tmp_path / "broken.json"),
         "--derived-root", str(derived)],
        workspace=tmp_path,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "1 broken on daytona" in out
    assert "sha256: " in out

# ---------------------------------------------------------------- disk-cap quota


def test_backend_quota_matches_disk_capacity_markers() -> None:
    assert is_backend_quota_failure("DaytonaError", "no space left on device") is True
    assert is_backend_quota_failure("DaytonaError", "failed: disk quota exceeded") is True
    assert is_backend_quota_failure("DaytonaError", "insufficient disk space") is True
    assert is_backend_quota_failure("DaytonaError", "insufficient storage for volume") is True
    assert is_backend_quota_failure("DaytonaError", "storage limit reached") is True
    assert is_backend_quota_failure("DaytonaError", "disk size exceeds the allowed limit") is True
    assert is_backend_quota_failure("DaytonaError", "storage capacity exceeded for tier") is True


def test_backend_quota_rejects_bare_disk_storage() -> None:
    assert is_backend_quota_failure("EnvironmentSetupError", "disk I/O error") is False
    assert is_backend_quota_failure("EnvironmentSetupError", "storage backend down") is False
    assert is_backend_quota_failure("EnvironmentSetupError", "low disk warning") is False
    assert is_backend_quota_failure("EnvironmentSetupError", "retry budget exceeded") is False


def test_classify_disk_cap_setup_failure_is_inconclusive() -> None:
    reasons = classify_trial(
        agent_name="nop", reward=None, verifier_completed=False,
        exception_type="DaytonaError",
        exception_message="create sandbox: no space left on device",
        agent_execution_started=False, repeat_rewards=None,
    )
    assert reasons == ["backend_quota"]
    assert status_for(reasons) == "inconclusive"


# ---------------------------------------------------------------- resource overrides


def test_collect_uses_environment_resource_overrides(tmp_path: Path) -> None:
    staged = _write_staged_task(tmp_path)
    job = _write_job(
        tmp_path, "jobo", [{"name": "t__a", "env_type": "daytona", "reward": 0.0}],
        staged=staged,
    )
    trial = job / "t__a"
    config = json.loads((trial / "config.json").read_text())
    config["environment"].update({
        "override_cpus": 4, "override_memory_mb": 8192, "override_storage_mb": 30720,
    })
    (trial / "config.json").write_text(json.dumps(config))
    (row,) = collect_jobs([job])
    assert (row["cpus"], row["memory_mb"], row["storage_mb"]) == (4, 8192, 30720)
    expected = (60.0 / 3600.0) * (4 * 0.0504 + 8 * 0.0162 + 25 * 0.000108)
    assert row["est_cost_usd"] == pytest.approx(expected)


# ---------------------------------------------------------------- grader_broken

GRADER_BROKEN_STDOUT = """\
==================================== ERRORS ====================================
_______________________ ERROR collecting test_outputs.py _______________________
ImportError while importing test module '/tests/test_outputs.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/tests/test_outputs.py:10: in <module>
    from bandit.core.issue import Issue
vendor/pycqa-bandit/bandit/core/extension_loader.py:6: in <module>
    from stevedore import extension
E   ModuleNotFoundError: No module named 'stevedore'
=========================== short test summary info ============================
ERROR ../tests/test_outputs.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.11s
"""

HEALTHY_STDOUT = """\
FAILED ../tests/test_outputs.py::test_core_case - assert 0 == 1
FAILED ../tests/test_outputs.py::test_edge_case - assert 0 == 2
========================= 5 failed in 0.05s ==========================
"""


def test_detect_grader_flags_collection_import_failure() -> None:
    assert detect_grader_collection_failure(
        [GRADER_BROKEN_STDOUT], instruction_text="Repair the scan pipeline.\n"
    ) == "ModuleNotFoundError: No module named 'stevedore'"


def test_detect_grader_ignores_healthy_stdout() -> None:
    assert detect_grader_collection_failure(
        [HEALTHY_STDOUT], instruction_text="Repair the scan pipeline.\n"
    ) is None


def test_detect_grader_requires_collection_marker() -> None:
    runtime = "FAILED test_x.py::test_y - E ModuleNotFoundError: No module named 'pandas'\n"
    assert detect_grader_collection_failure(
        [runtime], instruction_text="Analyze the data.\n"
    ) is None


def test_detect_grader_flags_syntax_error() -> None:
    stdout = (
        "ERROR collecting tests/test_bad.py\n"
        "E   SyntaxError: invalid syntax (test_bad.py, line 3)\n"
        "!!!! Interrupted: 1 error during collection !!!!\n"
    )
    assert detect_grader_collection_failure(
        [stdout], instruction_text="Fix it.\n"
    ) == "SyntaxError: invalid syntax (test_bad.py, line 3)"


def test_detect_grader_nop_guard_for_expected_module() -> None:
    stdout = (
        "ERROR collecting tests/test_out.py\n"
        "ImportError while importing test module 'tests/test_out.py'.\n"
        "E   ModuleNotFoundError: No module named 'solution'\n"
        "Interrupted: 1 error during collection\n"
    )
    guarded = "Create /app/solution.py implementing the pipeline.\n"
    assert detect_grader_collection_failure(
        [stdout], instruction_text=guarded
    ) is None
    assert detect_grader_collection_failure(
        [stdout], instruction_text="Repair the vendored scanner.\n"
    ) == "ModuleNotFoundError: No module named 'solution'"
    # A missing instruction never guards: the defect still flags.
    assert detect_grader_collection_failure(
        [stdout], instruction_text=None
    ) == "ModuleNotFoundError: No module named 'solution'"


def test_detect_grader_guards_dotted_module_by_top_level() -> None:
    stdout = (
        "ERROR collecting tests/test_out.py\n"
        "E   ModuleNotFoundError: No module named 'pkg.submodule'\n"
        "Interrupted: 1 error during collection\n"
    )
    assert detect_grader_collection_failure(
        [stdout], instruction_text="Implement pkg from scratch.\n"
    ) is None


def test_collect_trial_flags_grader_broken_for_controls(tmp_path: Path) -> None:
    staged = _write_staged_task(tmp_path)
    job = _write_job(tmp_path, "jobg", [
        {"name": "t__nop", "agent": "nop", "reward": 0.0},
        {"name": "t__model", "agent": "agentx", "reward": 0.0},
    ], staged=staged)
    for name in ("t__nop", "t__model"):
        (job / name / "verifier" / "test-stdout.txt").write_text(GRADER_BROKEN_STDOUT)
    rows = {row["trial_name"]: row for row in collect_jobs([job])}
    nop = rows["t__nop"]
    assert nop["reasons"] == ["grader_broken"]
    assert nop["status"] == "broken"
    assert nop["grader_error"] == "ModuleNotFoundError: No module named 'stevedore'"
    model = rows["t__model"]
    assert model["reasons"] == []
    assert model["status"] == "ok"
    assert model["grader_error"] is None


def test_collect_trial_grader_guard_from_staged_instruction(tmp_path: Path) -> None:
    staged = _write_staged_task(tmp_path)
    (staged / "instruction.md").write_text("Create /app/solution.py now.\n")
    job = _write_job(tmp_path, "jobh", [
        {"name": "t__nop", "agent": "nop", "reward": 0.0},
    ], staged=staged)
    stdout = (
        "ERROR collecting tests/test_out.py\n"
        "E   ModuleNotFoundError: No module named 'solution'\n"
        "Interrupted: 1 error during collection\n"
    )
    (job / "t__nop" / "verifier" / "test-stdout.txt").write_text(stdout)
    (row,) = collect_jobs([job])
    assert row["reasons"] == []
    assert row["status"] == "ok"
    assert row["grader_error"] is None


def test_collect_trial_reads_repeat_stdout(tmp_path: Path) -> None:
    staged = _write_staged_task(tmp_path)
    job = _write_job(tmp_path, "jobr2", [
        {"name": "t__nop", "agent": "nop", "reward": 0.0},
    ], staged=staged)
    top = job / "t__nop" / "verifier" / "test-stdout.txt"
    top.unlink(missing_ok=True)
    repeat_stdout = job / "t__nop" / "verifier" / "repeat" / "0" / "test-stdout.txt"
    repeat_stdout.parent.mkdir(parents=True)
    repeat_stdout.write_text(GRADER_BROKEN_STDOUT)
    (row,) = collect_jobs([job])
    assert row["reasons"] == ["grader_broken"]
    assert row["grader_error"] == "ModuleNotFoundError: No module named 'stevedore'"


# ---------------------------------------------------------------- export-broken findings


def _write_findings_table(derived: Path, rows: list[dict]) -> None:
    import pyarrow as pa

    from evallab.task_catalog import TASK_FINDINGS_SCHEMA

    catalog = derived / "external/task_catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(rows, schema=TASK_FINDINGS_SCHEMA),
        catalog / "task_findings.parquet",
    )


def test_export_broken_lists_error_findings_on_any_backend(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    staged = _write_staged_task(tmp_path)
    job = _write_job(tmp_path, "jobf", [{"name": "t__ok", "reward": 0.0}], staged=staged)
    rows = collect_jobs([job])
    for row in rows:
        row["backend"] = "docker"
    _write_catalog_table(derived, rows)
    _write_findings_table(derived, [{
        "task_version_digest": "sha256:" + "a" * 64,
        "task_id": "qual-task", "domain": "terminal",
        "rule": "grader-broken", "severity": "error",
        "message": "collection imports stevedore",
    }])
    for backend in ("docker", "daytona"):
        result = export_broken(
            tmp_path / f"broken-{backend}.json", backend=backend,
            repo_root=tmp_path, derived_root=derived,
        )
        assert result.n_broken == 1
        payload = json.loads(result.path.read_text())
        (item,) = payload["items"]
        assert item["reasons"] == ["finding:grader-broken"]
        assert item["source"] == "finding"
        assert item["trials"] == []
    again = export_broken(
        tmp_path / "broken-docker-again.json", backend="docker",
        repo_root=tmp_path, derived_root=derived,
    )
    first = export_broken(
        tmp_path / "broken-docker-first.json", backend="docker",
        repo_root=tmp_path, derived_root=derived,
    )
    assert again.sha256 == first.sha256


def test_export_broken_lists_findings_before_any_qualification_run(tmp_path: Path) -> None:
    """A curated defect is exportable before the backend has been qualified."""
    derived = tmp_path / "derived"
    _write_findings_table(derived, [{
        "task_version_digest": "sha256:" + "a" * 64,
        "task_id": "qual-task", "domain": "terminal",
        "rule": "grader-broken", "severity": "error",
        "message": "collection imports stevedore",
    }])
    result = export_broken(
        tmp_path / "broken.json", backend="daytona",
        repo_root=tmp_path, derived_root=derived,
    )
    payload = json.loads(result.path.read_text())
    (item,) = payload["items"]
    assert item["task_id"] == "qual-task"
    assert item["reasons"] == ["finding:grader-broken"]
    assert payload["meta"]["table_digest"] is None
    assert payload["meta"]["n_considered"] == 0


def test_export_broken_refuses_with_only_warning_findings(tmp_path: Path) -> None:
    from evallab.task_catalog import CatalogError

    derived = tmp_path / "derived"
    _write_findings_table(derived, [{
        "task_version_digest": "sha256:" + "b" * 64,
        "task_id": "warn-task", "domain": "terminal",
        "rule": "mimo-no-oracle", "severity": "warning",
        "message": "no oracle",
    }])
    with pytest.raises(CatalogError):
        export_broken(
            tmp_path / "broken.json", backend="daytona",
            repo_root=tmp_path, derived_root=derived,
        )


def test_export_broken_merges_finding_into_qualification_item(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    staged = _write_staged_task(tmp_path)
    job = _write_job(tmp_path, "jobm", [{"name": "t__bad", "reward": 1.0}], staged=staged)
    rows = collect_jobs([job])
    for row in rows:
        row["backend"] = "daytona"
    _write_catalog_table(derived, rows)
    _write_findings_table(derived, [{
        "task_version_digest": "sha256:" + "a" * 64,
        "task_id": "qual-task", "domain": "terminal",
        "rule": "grader-broken", "severity": "error",
        "message": "collection imports stevedore",
    }])
    result = export_broken(
        tmp_path / "broken.json", backend="daytona",
        repo_root=tmp_path, derived_root=derived,
    )
    assert result.n_broken == 1
    (item,) = json.loads(result.path.read_text())["items"]
    assert item["reasons"] == ["finding:grader-broken", "nop_passes"]
    assert item["source"] == "finding"


def test_export_broken_ignores_warning_findings(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    staged = _write_staged_task(tmp_path)
    job = _write_job(tmp_path, "jobw", [{"name": "t__ok", "reward": 0.0}], staged=staged)
    rows = collect_jobs([job])
    for row in rows:
        row["backend"] = "docker"
    _write_catalog_table(derived, rows)
    _write_findings_table(derived, [{
        "task_version_digest": "sha256:" + "a" * 64,
        "task_id": "qual-task", "domain": "terminal",
        "rule": "mimo-no-oracle", "severity": "warning",
        "message": "no oracle recorded",
    }])
    result = export_broken(
        tmp_path / "broken.json", backend="docker",
        repo_root=tmp_path, derived_root=derived,
    )
    assert result.n_broken == 0
