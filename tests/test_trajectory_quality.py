"""Focused test suite for the Durable Trajectory Quality Ledger.

Evaluates:
- Correct classification of clean, warning, and malformed ATIF trajectories.
- Handling of infrastructure errors and un-evaluated controls.
- Deterministic per-trial Parquet settlement through authenticated ingest_and_project.
- AnalysisWorker admission gating directly from exact frozen source bytes.
- Canonical storage.attach.attach queries, typed readiness, and byte-capture immutability.
- Absence of content leaks in status reporting.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from evallab.analysis_worker import (
    AdmissionContext,
    AnalysisRequest,
    RequestStore,
    admit,
    freeze_request,
)
from evallab.evidence.atif import ingest_and_project
from evallab.evidence_store import archive_evidence, evidence_locator
from evallab.interpretation.trajectory_quality import (
    FindingSeverity,
    QualityStatus,
    evaluate_trial_quality,
)
from evallab.profiles import AgentProfile, ProbeResult
from evallab.results import load_job
from evallab.schemas import StandingApprovalsPolicy
from evallab.status_generator import StatusReportData, render_status_markdown
from evallab.storage.attach import attach


@pytest.fixture
def sample_trial_dir(tmp_path: Path) -> Path:
    """Fixture creating a standard valid trial directory."""
    trial_dir = tmp_path / "job-1" / "trial-1"
    agent_dir = trial_dir / "agent"
    verifier_dir = trial_dir / "verifier"
    agent_dir.mkdir(parents=True)
    verifier_dir.mkdir(parents=True)

    result_payload = {
        "job_id": "job-1",
        "trial_id": "trial-1",
        "trial_name": "trial-1",
        "task_name": "event-summary",
        "agent_name": "mini-swe-agent",
        "started_at": "2026-08-25T12:00:00Z",
        "finished_at": "2026-08-25T12:05:00Z",
        "primary_reward": 1.0,
    }
    (trial_dir / "result.json").write_text(json.dumps(result_payload), encoding="utf-8")

    atif_payload = {
        "schema_version": "ATIF-1.0.0",
        "session_id": "session-1",
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "message": "Starting task",
                "tool_calls": [
                    {
                        "call_id": "call-1",
                        "tool_name": "bash",
                        "arguments": {"command": "ls -la"},
                    }
                ],
                "observations": [{"output": "total 0", "tool_call_id": "call-1"}],
            },
            {
                "step_id": 2,
                "source": "environment",
                "message": "total 0",
                "observations": [{"output": "total 0"}],
            },
        ],
    }
    (agent_dir / "trajectory.json").write_text(json.dumps(atif_payload), encoding="utf-8")
    (trial_dir / "lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "job-1" / "result.json").write_text(
        json.dumps(
            {"id": "job-1", "n_total_trials": 1, "stats": {}, "finished_at": "2026-08-25T12:05:00Z"}
        ),
        encoding="utf-8",
    )
    return trial_dir


def test_clean_atif_pass(sample_trial_dir: Path) -> None:
    """Clean ATIF passes all checks and is marked analysis ready."""
    report, findings = evaluate_trial_quality(sample_trial_dir)
    assert report.status == QualityStatus.PASS
    assert report.is_ingestable is True
    assert report.is_analysis_ready is True
    assert report.quarantine_reason is None
    assert report.findings_count == 0
    assert report.raw_atif_digest is not None
    assert report.raw_result_digest is not None


def test_warning_atif_remains_ingestable_and_analysis_ready(sample_trial_dir: Path) -> None:
    """Non-fatal anomalies produce warnings but remain analysis ready."""
    traj_path = sample_trial_dir / "agent" / "trajectory.json"
    atif_data = json.loads(traj_path.read_text(encoding="utf-8"))
    atif_data["schema_version"] = "custom-v99"
    traj_path.write_text(json.dumps(atif_data), encoding="utf-8")

    report, findings = evaluate_trial_quality(sample_trial_dir)
    assert report.status == QualityStatus.WARN
    assert report.is_ingestable is True
    assert report.is_analysis_ready is True
    assert report.quarantine_reason is None
    assert report.warnings_count > 0
    assert any(f.severity == FindingSeverity.WARN for f in findings)


def test_malformed_atif_is_catalog_visible_but_analysis_ineligible(
    sample_trial_dir: Path,
) -> None:
    """Malformed ATIF JSON remains catalog ingestable but is strictly ineligible for semantic analysis."""
    traj_path = sample_trial_dir / "agent" / "trajectory.json"
    traj_path.write_text("{ broken json", encoding="utf-8")

    report, findings = evaluate_trial_quality(sample_trial_dir)
    assert report.status == QualityStatus.FAIL
    assert report.is_ingestable is True
    assert report.is_analysis_ready is False
    assert report.quarantine_reason is not None
    assert "atif_parse_error" in report.quarantine_reason


def test_infrastructure_exception_quarantined_not_reward_zero(
    sample_trial_dir: Path,
) -> None:
    """Infrastructure crash is quarantined with an explicit reason code, not recorded as an agent failure."""
    exc_path = sample_trial_dir / "exception.txt"
    exc_path.write_text(
        "DockerTimeoutException: container timed out after 300s\n", encoding="utf-8"
    )

    report, findings = evaluate_trial_quality(sample_trial_dir)
    assert report.status == QualityStatus.QUARANTINE
    assert report.is_ingestable is True
    assert report.is_analysis_ready is False
    assert (
        report.quarantine_reason
        == "infrastructure_exception:DockerTimeoutException: container timed out after 300s"
    )
    assert any(f.code == "INFRA_EXCEPTION" for f in findings)


def test_control_run_without_atif_is_valid_but_not_analysis_ready(
    tmp_path: Path,
) -> None:
    """Oracle and Nop controls do not write ATIF; they are catalog-valid but excluded from agent analysis."""
    trial_dir = tmp_path / "job-c" / "trial-c"
    trial_dir.mkdir(parents=True)
    result_payload = {
        "job_id": "job-c",
        "trial_id": "trial-c",
        "task_name": "event-summary",
        "agent_name": "oracle",
        "primary_reward": 1.0,
    }
    (trial_dir / "result.json").write_text(json.dumps(result_payload), encoding="utf-8")
    (trial_dir / "lock.json").write_text("{}", encoding="utf-8")

    report, findings = evaluate_trial_quality(trial_dir)
    assert report.status == QualityStatus.PASS
    assert report.is_ingestable is True
    assert report.is_analysis_ready is False
    assert any(f.code == "CONTROL_NON_ATIF" for f in findings)


def test_authenticated_ingest_settles_quality_tables(
    sample_trial_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab import database
    from evallab.evidence import facts as facts_module

    monkeypatch.setattr(database, "initialize", lambda _url: None)
    monkeypatch.setattr(database, "ingest", lambda _url, jobs, root: len(jobs))
    monkeypatch.setattr(
        facts_module, "ingest_catalog", lambda _url, _jobs, root, derived_root: None
    )

    """ingest_and_project with EvidenceLocator settles deterministic quality tables."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    derived_root = repo_root / "derived"
    derived_root.mkdir(parents=True)
    store_root = repo_root / "cas"

    from evallab.results import load_job

    job_dir = sample_trial_dir.parent
    job = load_job(job_dir)

    archive = archive_evidence(job_dir, store_root, kind="job", record_id=job.id)
    locator = evidence_locator(store_root, archive)

    # Ingest and project through canonical settlement
    result = ingest_and_project(
        "postgresql://test",
        [job],
        root=repo_root,
        output_root=derived_root,
        source_locators={job.id: locator},
        settlement_recorder=lambda *_args: None,
    )
    assert len(result.failures) == 0

    # Verify settled quality tables exist at exact relative paths
    rep_parquet = derived_root / "job_id=job-1/trial_id=trial-1/trajectory_quality_reports.parquet"
    find_parquet = (
        derived_root / "job_id=job-1/trial_id=trial-1/trajectory_quality_findings.parquet"
    )
    assert rep_parquet.is_file()
    assert find_parquet.is_file()

    # Re-running identical source is idempotent
    result2 = ingest_and_project(
        "postgresql://test",
        [job],
        root=repo_root,
        output_root=derived_root,
        source_locators={job.id: locator},
        settlement_recorder=lambda *_args: None,
    )
    assert len(result2.failures) == 0
    assert rep_parquet.read_bytes()


def test_analysis_worker_admission_fails_closed_on_missing_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AnalysisWorker defers as quality_not_evaluated when evidence cannot be evaluated."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    trial_path = repo_root / "runs" / "job-1" / "trial-1"
    trial_path.mkdir(parents=True)

    # Missing result.json -> unreadable evidence
    request = AnalysisRequest.model_validate(
        {
            "schema_version": 2,
            "request_id": "0123456789abcdef",
            "created_at": datetime.now(UTC),
            "experiment_id": "exp-1",
            "job_id": "job-1",
            "trial_id": "trial-1",
            "job_name": "job-1",
            "trial_name": "trial-1",
            "trial_path": "runs/job-1/trial-1",
            "profile_id": "profile-1",
            "adapter": "test-adapter",
            "model": "test-model",
            "result_sha256": "sha256:1111222233334444555566667777888899990000111122223333444455556666",
            "trajectory_sha256": "sha256:1111222233334444555566667777888899990000111122223333444455556666",
            "lock_sha256": "sha256:1111222233334444555566667777888899990000111122223333444455556666",
            "task_digest": "sha256:1111",
            "verifier_digest": "sha256:2222",
            "rubric_sha256": "sha256:3333",
            "prompt_sha256": "sha256:4444",
            "profile_digest": "sha256:5555",
            "quality_status": "pass",
            "quality_check_version": "v1.0.0",
            "quality_check_digest": "sha256:0000",
            "quality_quarantine_reason": None,
            "quality_report_digest": "sha256:0000",
            "quality_inputs_digest": "sha256:0000",
            "source_snapshot_digest": "sha256:0000",
        }
    )
    policy = StandingApprovalsPolicy(
        version=1,
        daily_cost_ceiling_usd=20.0,
        per_job_cost_ceiling_usd=3.0,
        quiet_failure_rule=3,
        auto_run=[{"name": "local-controls", "agents": ["oracle", "nop"]}],
        escalate_to_human=[],
    )
    ctx = AdmissionContext(
        stop_present=lambda: False,
        policy=policy,
        profile=AgentProfile(
            profile_id="p1",
            adapter="test-adapter",
            model="gpt-5.6",
            auth_mode="subscription-keychain",
            secret_source="keychain:eval-lab",
            verified_facts=("2026-08-06: proven run",),
        ),
        probe=None,
        spent_today_usd=lambda: 0.0,
        est_call_cost_usd=0.01,
        services_healthy=lambda: True,
        requirement_checks={},
    )
    store = RequestStore(repo_root / "derived" / "analyses" / "worker")
    decision = admit(request, store, ctx, repo_root)
    assert decision.kind == "quarantine"
    assert "evidence_missing:result.json" in decision.reason


def test_analysis_worker_quarantines_quarantined_quality_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AnalysisWorker strictly quarantines trials whose quality status is quarantine."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    trial_path = repo_root / "runs" / "job-1" / "trial-1"
    agent_dir = trial_path / "agent"
    agent_dir.mkdir(parents=True)

    res_bytes = json.dumps(
        {
            "job_id": "job-1",
            "trial_id": "trial-1",
            "trial_name": "trial-1",
            "task_name": "task",
            "agent_name": "agent",
            "exception": "runner timeout error",
        }
    ).encode()
    traj_bytes = b'{"steps": []}'
    lock_bytes = b"{}"
    (trial_path / "result.json").write_bytes(res_bytes)
    (agent_dir / "trajectory.json").write_bytes(traj_bytes)
    (trial_path / "lock.json").write_bytes(lock_bytes)
    (repo_root / "runs" / "job-1" / "result.json").write_text(
        json.dumps(
            {"id": "job-1", "n_total_trials": 1, "stats": {}, "finished_at": "2026-08-25T12:05:00Z"}
        ),
        encoding="utf-8",
    )

    prompt = repo_root / "prompt.txt"
    prompt.write_text("{source_trial_path}\n{rubric}\n{output_schema}")
    rubric = repo_root / "rubric.json"
    rubric.write_text("{}")
    prof = AgentProfile(
        profile_id="profile-1",
        adapter="test-adapter",
        model="test-model",
        auth_mode="subscription-keychain",
        secret_source="keychain:eval-lab",
        verified_facts=("2026-08-06: proven run",),
    )
    job = load_job(repo_root / "runs/job-1")
    request = freeze_request(
        job,
        job.trials[0],
        profile=prof,
        prompt_path=prompt,
        rubric_path=rubric,
        repo_root=repo_root,
    )
    assert request is not None
    assert request.quality_status == "quarantine"
    policy = StandingApprovalsPolicy(
        version=1,
        daily_cost_ceiling_usd=20.0,
        per_job_cost_ceiling_usd=3.0,
        quiet_failure_rule=3,
        auto_run=[{"name": "local-controls", "agents": ["oracle", "nop"]}],
        escalate_to_human=[],
    )
    ctx = AdmissionContext(
        stop_present=lambda: False,
        policy=policy,
        profile=AgentProfile(
            profile_id="p1",
            adapter="test-adapter",
            model="gpt-5.6",
            auth_mode="subscription-keychain",
            secret_source="keychain:eval-lab",
            verified_facts=("2026-08-06: proven run",),
        ),
        probe=None,
        spent_today_usd=lambda: 0.0,
        est_call_cost_usd=0.01,
        services_healthy=lambda: True,
        requirement_checks={},
    )
    store = RequestStore(repo_root / "derived" / "analyses" / "worker")
    decision = admit(request, store, ctx, repo_root)
    assert decision.kind == "quarantine"
    assert "runner_exception:runner timeout error" in decision.reason


def test_duckdb_attach_and_adversary_immutability(
    sample_trial_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab import database
    from evallab.evidence import facts as facts_module

    monkeypatch.setattr(database, "initialize", lambda _url: None)
    monkeypatch.setattr(database, "ingest", lambda _url, jobs, root: len(jobs))
    monkeypatch.setattr(
        facts_module, "ingest_catalog", lambda _url, _jobs, root, derived_root: None
    )

    """Proves DuckDB attach captures bytes: replacing Parquet file does not alter admitted query rows."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    derived_root = repo_root / "derived"
    derived_root.mkdir(parents=True)
    store_root = repo_root / "cas"

    from evallab.results import load_job

    job_dir = sample_trial_dir.parent
    job = load_job(job_dir)

    archive = archive_evidence(job_dir, store_root, kind="job", record_id=job.id)
    locator = evidence_locator(store_root, archive)

    from evallab.storage.settlement import write_settlement_manifest

    # Ingest and project
    result = ingest_and_project(
        "postgresql://test",
        [job],
        root=repo_root,
        output_root=derived_root,
        source_locators={job.id: locator},
        settlement_recorder=lambda _url, d_root, manifest: write_settlement_manifest(
            d_root, manifest
        ),
    )
    assert len(result.failures) == 0

    # Attach with DuckDB
    attach_res = attach(repo_root=repo_root, explicit_derived=derived_root, environ={})
    z3 = next(z for z in attach_res.zones if z.name == "z3")
    assert z3.state in ("ready", "partial")
    t_rep_before = next(t for t in z3.tables if t.table_name == "trajectory_quality_reports")
    assert t_rep_before.state == "ready"

    # Query quality reports and findings
    conn = attach_res.connection
    reports = conn.execute(
        "SELECT job_id, trial_id, status, is_analysis_ready FROM trajectory_quality_reports"
    ).fetchall()
    assert len(reports) == 1
    assert reports[0] == ("job-1", "trial-1", "pass", True)

    findings = conn.execute("SELECT count(*) FROM trajectory_quality_findings").fetchone()
    assert findings[0] == 0

    # ADVERSARY 1: Tamper with/replace the parquet file on disk after attach
    rep_parquet = derived_root / "job_id=job-1/trial_id=trial-1/trajectory_quality_reports.parquet"
    assert rep_parquet.is_file()
    rep_parquet.write_bytes(b"corrupted or replaced parquet bytes")

    # Second query on the SAME connection still returns the immutable captured bytes
    reports_again = conn.execute(
        "SELECT job_id, trial_id, status, is_analysis_ready FROM trajectory_quality_reports"
    ).fetchall()
    assert len(reports_again) == 1
    assert reports_again[0] == ("job-1", "trial-1", "pass", True)

    # ADVERSARY 2: New attach after file mutation fails closed at validation/capture
    attach_after = attach(repo_root=repo_root, explicit_derived=derived_root, environ={})
    z3 = next(z for z in attach_after.zones if z.name == "z3")
    t_rep = next((t for t in z3.tables if t.table_name == "trajectory_quality_reports"), None)
    assert t_rep is not None
    assert t_rep.state != "ready"

    conn.close()
    attach_after.connection.close()


def test_status_generator_renders_quality_ledger_without_leaks() -> None:
    """Status generator presents aggregate quality ledger counts and reason codes without content leaks."""
    data = StatusReportData(
        target_date=date(2026, 8, 25),
        reporting_date=date(2026, 8, 25),
        quality_summary={
            "total": 10,
            "pass": 6,
            "warn": 2,
            "fail": 0,
            "quarantine": 2,
            "quarantine_reasons": {
                "infrastructure_exception:docker_timeout": 1,
                "missing_trajectory_file": 1,
            },
        },
    )
    md = render_status_markdown(data)
    assert "### Evidence Quality Ledger" in md
    assert "- **Evaluated Trials:** 10 (Passed: 6, Warnings: 2, Failed: 0, Quarantined: 2)" in md


def test_staged_quarantine_drift_by_removing_exception_file_is_rejected_with_zero_calls(
    tmp_path: Path,
) -> None:
    """Architect adversary B1: staged quarantined trial with exception.txt removed before admit fails closed with 0 calls."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    trial_path = repo_root / "runs" / "job-1" / "trial-1"
    agent_dir = trial_path / "agent"
    agent_dir.mkdir(parents=True)

    res_bytes = json.dumps(
        {
            "job_id": "job-1",
            "trial_id": "trial-1",
            "trial_name": "trial-1",
            "task_name": "task",
            "agent_name": "agent",
        }
    ).encode()
    traj_bytes = json.dumps(
        {"schema_version": "ATIF-1.0.0", "session_id": "s1", "steps": []}
    ).encode()
    lock_bytes = b"{}"
    (trial_path / "result.json").write_bytes(res_bytes)
    (agent_dir / "trajectory.json").write_bytes(traj_bytes)
    (trial_path / "lock.json").write_bytes(lock_bytes)
    (repo_root / "runs" / "job-1" / "result.json").write_text(
        json.dumps(
            {"id": "job-1", "n_total_trials": 1, "stats": {}, "finished_at": "2026-08-25T12:05:00Z"}
        )
    )
    # Add exception.txt -> quality status will freeze as quarantine
    (trial_path / "exception.txt").write_text("DockerTimeoutException\n")

    prompt = repo_root / "prompt.txt"
    prompt.write_text("{source_trial_path}\n{rubric}\n{output_schema}")
    rubric = repo_root / "rubric.json"
    rubric.write_text("{}")
    prof = AgentProfile(
        profile_id="profile-1",
        adapter="test-adapter",
        model="test-model",
        auth_mode="subscription-keychain",
        secret_source="keychain:eval-lab",
        verified_facts=("2026-08-06: proven run",),
    )
    job = load_job(repo_root / "runs/job-1")
    request = freeze_request(
        job,
        job.trials[0],
        profile=prof,
        prompt_path=prompt,
        rubric_path=rubric,
        repo_root=repo_root,
    )
    assert request is not None
    assert request.quality_status == "quarantine"

    # ADVERSARY: Remove exception.txt without altering result.json / trajectory.json
    (trial_path / "exception.txt").unlink()

    policy = StandingApprovalsPolicy(
        version=1,
        daily_cost_ceiling_usd=20.0,
        per_job_cost_ceiling_usd=3.0,
        quiet_failure_rule=3,
        auto_run=[{"name": "local-controls", "agents": ["oracle", "nop"]}],
        escalate_to_human=[],
    )
    ctx = AdmissionContext(
        stop_present=lambda: False,
        policy=policy,
        profile=prof,
        probe=None,
        spent_today_usd=lambda: 0.0,
        est_call_cost_usd=0.01,
        services_healthy=lambda: True,
        requirement_checks={},
    )
    store = RequestStore(repo_root / "derived" / "analyses" / "worker")
    decision = admit(request, store, ctx, repo_root)

    # Must be quarantined due to quality inputs drift with zero model calls!
    assert decision.kind == "quarantine"
    assert decision.reason == "evidence_tampered:quality_inputs"


def test_post_admission_quality_tampering_of_original_path_quarantines_with_zero_calls_and_preserves_snapshot(
    tmp_path: Path,
) -> None:
    """Architect adversary B1: mutating original trial path post-admission quarantines with zero model calls and preserves snapshot isolation."""
    from evallab.analysis_worker import RESEARCHER_RULE, AnalysisWorker
    from evallab.evidence.facts import AnalyzerCallResult
    from evallab.evidence_store import evidence_tree_digest
    from evallab.results import JobRecord, TrialRecord

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    trial_path = repo_root / "runs" / "job-1" / "trial-1"
    agent_dir = trial_path / "agent"
    agent_dir.mkdir(parents=True)

    res_bytes = json.dumps(
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "job_id": "00000000-0000-0000-0000-000000000001",
            "trial_id": "00000000-0000-0000-0000-000000000002",
            "trial_name": "trial-1",
            "task_name": "task",
            "agent_name": "test-adapter",
            "primary_reward": 1.0,
        }
    ).encode()
    traj_bytes = json.dumps(
        {"schema_version": "ATIF-1.0.0", "session_id": "s1", "steps": []}
    ).encode()
    lock_bytes = b"{}"
    (trial_path / "result.json").write_bytes(res_bytes)
    (agent_dir / "trajectory.json").write_bytes(traj_bytes)
    (trial_path / "lock.json").write_bytes(lock_bytes)
    (repo_root / "runs" / "job-1" / "result.json").write_text(
        json.dumps(
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "n_total_trials": 1,
                "stats": {},
                "finished_at": "2026-08-25T12:05:00Z",
            }
        )
    )

    prompt = repo_root / "prompt.txt"
    prompt.write_text("{source_trial_path}\n{rubric}\n{output_schema}")
    rubric = repo_root / "rubric.json"
    rubric.write_text("{}")
    prof = AgentProfile(
        profile_id="profile-1",
        adapter="test-adapter",
        model="test-model",
        auth_mode="subscription-keychain",
        secret_source="keychain:eval-lab",
        verified_facts=("2026-08-06: proven run",),
    )
    job = load_job(repo_root / "runs/job-1")
    request = freeze_request(
        job,
        job.trials[0],
        profile=prof,
        prompt_path=prompt,
        rubric_path=rubric,
        repo_root=repo_root,
    )
    assert request is not None
    assert request.quality_status == "warn"

    store = RequestStore(repo_root / "derived" / "analyses" / "worker")
    assert store.freeze(request, trial_path=trial_path)

    snapshot_dir = store.request_dir(request.request_id) / "snapshot"
    assert snapshot_dir.is_dir()
    initial_snapshot_digest = evidence_tree_digest(snapshot_dir)
    assert initial_snapshot_digest == request.source_snapshot_digest

    policy = StandingApprovalsPolicy(
        version=1,
        daily_cost_ceiling_usd=20.0,
        per_job_cost_ceiling_usd=3.0,
        quiet_failure_rule=3,
        auto_run=[{"name": RESEARCHER_RULE, "agents": ["test-adapter"]}],
        escalate_to_human=[],
    )
    ctx = AdmissionContext(
        stop_present=lambda: False,
        policy=policy,
        profile=prof,
        probe=lambda _p: ProbeResult(ok=True),
        spent_today_usd=lambda: 0.0,
        est_call_cost_usd=0.01,
        services_healthy=lambda: True,
        requirement_checks={},
    )

    model_calls = 0

    def counting_adapter(p: str, s: dict) -> AnalyzerCallResult:
        nonlocal model_calls
        model_calls += 1
        return AnalyzerCallResult(raw_output="{}")

    def mutating_adapter_factory(_j: JobRecord, _t: TrialRecord, _req: AnalysisRequest):
        # Mutate the ORIGINAL mutable trial path AFTER admission
        (trial_path / "exception.txt").write_text("LateDockerTimeoutException\n")
        (trial_path / "result.json").write_text("CORRUPTED")
        return counting_adapter

    worker = AnalysisWorker(
        store=store,
        context=ctx,
        repo_root=repo_root,
        prompt_path=prompt,
        rubric_path=rubric,
        adapter=counting_adapter,
        adapter_factory=mutating_adapter_factory,
    )

    transition = worker.run_one(request.request_id)
    # Must quarantine with zero model calls!
    assert transition.state == "quarantined"
    assert transition.reason == "evidence_tampered:quality_inputs"
    assert model_calls == 0
    assert not store.sidecar_path(request.request_id).is_file()

    # Snapshot remained uncorrupted, exact, and isolated
    assert evidence_tree_digest(snapshot_dir) == request.source_snapshot_digest
    assert not (snapshot_dir / "exception.txt").exists()
    assert (snapshot_dir / "result.json").read_bytes() == res_bytes
