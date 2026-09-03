"""Focused test suite for the Durable Trajectory Quality Ledger.

Tests:
1. Clean ATIF pass (is_ingestable=True, is_analysis_ready=True, status="pass")
2. Warning ATIF (is_ingestable=True, is_analysis_ready=True, status="warn")
3. Malformed ATIF (catalog-visible but analysis-ineligible, is_analysis_ready=False, status="fail")
4. Infrastructure exception quarantined with reason code (not reward 0, is_analysis_ready=False)
5. AnalysisWorker admission gate defers missing report as "quality_not_evaluated"
6. AnalysisWorker admission gate rejects quarantined/failed quality trials
7. Idempotency: identical reruns produce deterministic, byte-stable Parquet tables
8. DuckDB attach: registers and queries trajectory_quality_reports and trajectory_quality_findings
9. Status generator: renders quality ledger counts and reason codes without content leaks
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest

from evallab.analysis_worker import (
    AdmissionContext,
    AnalysisRequest,
    RequestStore,
    admit,
)
from evallab.interpretation.trajectory_quality import (
    FindingSeverity,
    QualityStatus,
    TrajectoryQualityFinding,
    TrajectoryQualityReport,
    evaluate_trial_quality,
    load_quality_report_for_trial,
    persist_quality_ledger,
    register_quality_tables_in_duckdb,
)
from evallab.profiles import AgentProfile
from evallab.schemas import StandingApprovalsPolicy
from evallab.status_generator import StatusReportData, render_status_markdown


@pytest.fixture
def sample_trial_dir(tmp_path: Path) -> Path:
    """Fixture creating a standard valid trial directory."""
    trial_dir = tmp_path / "runs" / "job-1" / "trial-1"
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    result_data = {
        "id": "trial-1-id",
        "task_name": "sample-task",
        "agent_info": {"name": "codex", "model_info": {"name": "gpt-5.6"}},
        "verifier_result": {"rewards": {"reward": 1.0}},
    }
    (trial_dir / "result.json").write_text(json.dumps(result_data), encoding="utf-8")
    (trial_dir / "lock.json").write_text("{}", encoding="utf-8")

    atif_data = {
        "schema_version": "ATIF-v1.7",
        "session_id": "session-123",
        "agent": {"name": "codex", "version": "0.146.0"},
        "steps": [
            {
                "step_id": 0,
                "timestamp": "2026-08-25T12:00:00Z",
                "tool_calls": [{"name": "bash", "arguments": {"cmd": "ls"}}],
                "observations": [{"output": "file.txt", "exit_code": 0}],
            },
            {
                "step_id": 1,
                "timestamp": "2026-08-25T12:00:05Z",
                "tool_calls": [{"name": "finish", "arguments": {}}],
                "observations": [{"output": "done", "exit_code": 0}],
            },
        ],
    }
    (agent_dir / "trajectory.json").write_text(json.dumps(atif_data), encoding="utf-8")
    return trial_dir


def test_clean_atif_pass(sample_trial_dir: Path) -> None:
    """Clean ATIF passes all checks and is marked analysis ready."""
    report, findings = evaluate_trial_quality(sample_trial_dir)
    assert report.status == QualityStatus.PASS
    assert report.is_ingestable is True
    assert report.is_analysis_ready is True
    assert report.errors_count == 0
    assert report.quarantine_reason is None
    assert report.raw_atif_digest is not None
    assert report.raw_result_digest is not None


def test_warning_atif_remains_ingestable_and_analysis_ready(sample_trial_dir: Path) -> None:
    """Non-fatal anomalies (like empty steps or non-monotonic ids) produce warnings but remain analysis ready."""
    traj_path = sample_trial_dir / "agent" / "trajectory.json"
    atif_data = {
        "schema_version": "ATIF-v1.7",
        "steps": [
            {
                "step_id": 1,
                "tool_calls": [{"name": "read", "arguments": {}}],
                "observations": [],  # unpaired observation generates warning
            }
        ],
    }
    traj_path.write_text(json.dumps(atif_data), encoding="utf-8")

    report, findings = evaluate_trial_quality(sample_trial_dir)
    assert report.status == QualityStatus.WARN
    assert report.is_ingestable is True
    assert report.is_analysis_ready is True
    assert report.warnings_count >= 1
    assert any(f.severity == FindingSeverity.WARN for f in findings)


def test_malformed_atif_is_catalog_visible_but_analysis_ineligible(
    sample_trial_dir: Path,
) -> None:
    """Malformed ATIF JSON remains catalog ingestable but is strictly ineligible for semantic analysis."""
    traj_path = sample_trial_dir / "agent" / "trajectory.json"
    traj_path.write_text("{broken json syntax... [", encoding="utf-8")

    report, findings = evaluate_trial_quality(sample_trial_dir)
    assert report.status == QualityStatus.FAIL
    assert report.is_ingestable is True
    assert report.is_analysis_ready is False
    assert report.errors_count >= 1
    assert report.quarantine_reason is not None
    assert "atif_parse_error" in report.quarantine_reason


def test_infrastructure_exception_quarantined_not_reward_zero(
    sample_trial_dir: Path,
) -> None:
    """Infrastructure crash is quarantined with an explicit reason code, not recorded as an agent failure."""
    (sample_trial_dir / "exception.txt").write_text(
        "DockerContainerTimeoutError: container timed out after 300s\nTraceback...",
        encoding="utf-8",
    )

    report, findings = evaluate_trial_quality(sample_trial_dir)
    assert report.status == QualityStatus.QUARANTINE
    assert report.is_ingestable is True
    assert report.is_analysis_ready is False
    assert "infrastructure_exception" in str(report.quarantine_reason)
    assert any(f.code == "INFRA_EXCEPTION" for f in findings)


def test_null_agent_result_payload_is_quarantined_not_crash(
    sample_trial_dir: Path,
) -> None:
    """Failed Harbor trials record explicit null payloads ("agent_result": null).

    The quality audit must quarantine them via the exception.txt reason,
    not raise AttributeError while staging the nightly corpus.
    """
    result_path = sample_trial_dir / "result.json"
    result_data = json.loads(result_path.read_text(encoding="utf-8"))
    result_data["agent_result"] = None
    result_data["verifier_result"] = None
    result_data["exception_info"] = {"exception_type": "NonZeroAgentExitCodeError"}
    result_path.write_text(json.dumps(result_data), encoding="utf-8")
    (sample_trial_dir / "exception.txt").write_text(
        "harbor.trial.trial.NonZeroAgentExitCodeError: agent setup failed",
        encoding="utf-8",
    )

    report, findings = evaluate_trial_quality(sample_trial_dir)
    assert report.status == QualityStatus.QUARANTINE
    assert report.is_analysis_ready is False
    assert "infrastructure_exception" in str(report.quarantine_reason)
    assert any(f.code == "INFRA_EXCEPTION" for f in findings)

def test_control_run_without_atif_is_valid_but_not_analysis_ready(
    tmp_path: Path,
) -> None:
    """Oracle and Nop controls do not write ATIF; they are catalog-valid but excluded from agent analysis."""
    ctrl_dir = tmp_path / "runs" / "job-oracle" / "trial-oracle"
    ctrl_dir.mkdir(parents=True)
    res = {
        "id": "oracle-trial",
        "agent_name": "oracle",
        "verifier_result": {"rewards": {"reward": 1.0}},
    }
    (ctrl_dir / "result.json").write_text(json.dumps(res), encoding="utf-8")

    report, findings = evaluate_trial_quality(ctrl_dir)
    assert report.status == QualityStatus.PASS
    assert report.is_ingestable is True
    assert report.is_analysis_ready is False  # Control run not sent to agent analysis
    assert any(f.code == "CONTROL_NON_ATIF" for f in findings)


def test_idempotent_and_deterministic_persistence(sample_trial_dir: Path, tmp_path: Path) -> None:
    """Evaluating and persisting the same trials multiple times is deterministic and byte-stable."""
    derived_root = tmp_path / "derived" / "parquet"
    report1, findings1 = evaluate_trial_quality(
        sample_trial_dir, evaluated_at="2026-08-25T12:00:00Z"
    )
    rep_p1, find_p1 = persist_quality_ledger([report1], findings1, derived_root)

    bytes_rep1 = rep_p1.read_bytes()
    bytes_find1 = find_p1.read_bytes()

    # Re-run evaluation over same trial
    report2, findings2 = evaluate_trial_quality(
        sample_trial_dir, evaluated_at="2026-08-25T12:00:00Z"
    )
    rep_p2, find_p2 = persist_quality_ledger([report2], findings2, derived_root)

    bytes_rep2 = rep_p2.read_bytes()
    bytes_find2 = find_p2.read_bytes()

    assert bytes_rep1 == bytes_rep2
    assert bytes_find1 == bytes_find2

    # Load report back
    loaded = load_quality_report_for_trial("trial-1-id", derived_root)
    assert loaded is not None
    assert loaded.status == QualityStatus.PASS
    assert loaded.is_analysis_ready is True


def test_analysis_worker_admission_fails_closed_on_missing_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AnalysisWorker defers as quality_not_evaluated when no quality ledger record exists."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    trial_path = repo_root / "runs" / "job-1" / "trial-1"
    agent_dir = trial_path / "agent"
    agent_dir.mkdir(parents=True)

    res_bytes = b'{"id": "trial-1"}'
    traj_bytes = b'{"steps": []}'
    lock_bytes = b"{}"
    (trial_path / "result.json").write_bytes(res_bytes)
    (agent_dir / "trajectory.json").write_bytes(traj_bytes)
    (trial_path / "lock.json").write_bytes(lock_bytes)

    # Empty derived directory with no quality reports
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(repo_root / "derived" / "parquet"))

    request = AnalysisRequest.model_validate(
        {
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
            "result_sha256": "sha256:" + hashlib.sha256(res_bytes).hexdigest(),
            "trajectory_sha256": "sha256:" + hashlib.sha256(traj_bytes).hexdigest(),
            "lock_sha256": "sha256:" + hashlib.sha256(lock_bytes).hexdigest(),
            "task_digest": "sha256:1111",
            "verifier_digest": "sha256:2222",
            "rubric_sha256": "sha256:3333",
            "prompt_sha256": "sha256:4444",
            "profile_digest": "sha256:5555",
        }
    )
    policy = StandingApprovalsPolicy(
        version=1,
        daily_cost_ceiling_usd=20.0,
        per_job_cost_ceiling_usd=3.0,
        quiet_failure_rule=3,
        auto_run=[
            {"name": "local-controls", "agents": ["oracle", "nop"]},
            {
                "name": "researcher-followups",
                "agents": ["test-adapter", "codex"],
                "max_attempts": 5,
                "requires": [],
            },
        ],
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
    assert decision.kind == "defer"
    assert decision.reason == "quality_not_evaluated"


def test_analysis_worker_quarantines_quarantined_quality_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AnalysisWorker strictly quarantines trials whose quality ledger status is quarantine or failed."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    trial_path = repo_root / "runs" / "job-1" / "trial-1"
    agent_dir = trial_path / "agent"
    agent_dir.mkdir(parents=True)

    res_bytes = b'{"id": "trial-1"}'
    traj_bytes = b'{"steps": []}'
    lock_bytes = b"{}"
    (trial_path / "result.json").write_bytes(res_bytes)
    (agent_dir / "trajectory.json").write_bytes(traj_bytes)
    (trial_path / "lock.json").write_bytes(lock_bytes)

    derived_root = repo_root / "derived" / "parquet"
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived_root))

    # Persist a quarantined quality report
    q_rep = TrajectoryQualityReport(
        job_id="job-1",
        trial_id="trial-1",
        document_id="doc-1",
        raw_atif_digest="sha256:abc",
        raw_result_digest="sha256:def",
        check_version="v1",
        check_digest="sha256:123",
        status=QualityStatus.QUARANTINE,
        is_ingestable=True,
        is_analysis_ready=False,
        quarantine_reason="infrastructure_exception:docker_timeout",
        findings_count=1,
        warnings_count=0,
        errors_count=1,
    )
    persist_quality_ledger([q_rep], [], derived_root)

    request = AnalysisRequest.model_validate(
        {
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
            "result_sha256": "sha256:" + hashlib.sha256(res_bytes).hexdigest(),
            "trajectory_sha256": "sha256:" + hashlib.sha256(traj_bytes).hexdigest(),
            "lock_sha256": "sha256:" + hashlib.sha256(lock_bytes).hexdigest(),
            "task_digest": "sha256:1111",
            "verifier_digest": "sha256:2222",
            "rubric_sha256": "sha256:3333",
            "prompt_sha256": "sha256:4444",
            "profile_digest": "sha256:5555",
        }
    )
    policy = StandingApprovalsPolicy(
        version=1,
        daily_cost_ceiling_usd=20.0,
        per_job_cost_ceiling_usd=3.0,
        quiet_failure_rule=3,
        auto_run=[
            {"name": "local-controls", "agents": ["oracle", "nop"]},
            {
                "name": "researcher-followups",
                "agents": ["test-adapter", "codex"],
                "max_attempts": 5,
                "requires": [],
            },
        ],
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
    assert "quality_quarantined:infrastructure_exception:docker_timeout" in decision.reason


def test_duckdb_attach_and_queries(tmp_path: Path) -> None:
    """DuckDB attach correctly queries trajectory_quality_reports and trajectory_quality_findings."""
    derived_root = tmp_path / "derived" / "parquet"
    rep = TrajectoryQualityReport(
        job_id="job-duck",
        trial_id="trial-duck",
        document_id="doc-duck",
        raw_atif_digest="sha256:a1",
        raw_result_digest="sha256:r1",
        check_version="v1",
        check_digest="sha256:c1",
        status=QualityStatus.PASS,
        is_ingestable=True,
        is_analysis_ready=True,
        quarantine_reason=None,
        findings_count=1,
        warnings_count=1,
        errors_count=0,
    )
    fnd = TrajectoryQualityFinding(
        finding_id="fnd-duck-1",
        job_id="job-duck",
        trial_id="trial-duck",
        document_id="doc-duck",
        severity=FindingSeverity.WARN,
        category="atif",
        code="ATIF_SCHEMA_INVALID",
        message="Minor schema version warning",
    )
    persist_quality_ledger([rep], [fnd], derived_root)

    conn = duckdb.connect()
    has_rep, has_fnd = register_quality_tables_in_duckdb(conn, derived_root)
    assert has_rep is True
    assert has_fnd is True

    # Query reports
    rows = conn.execute(
        "SELECT trial_id, status, is_analysis_ready FROM trajectory_quality_reports"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("trial-duck", "pass", True)

    # Query findings
    f_rows = conn.execute(
        "SELECT finding_id, code, severity FROM trajectory_quality_findings"
    ).fetchall()
    assert len(f_rows) == 1
    assert f_rows[0] == ("fnd-duck-1", "ATIF_SCHEMA_INVALID", "warn")


def test_status_generator_renders_quality_ledger_without_leaks() -> None:
    """Status generator presents aggregate quality ledger counts and reason codes without content leaks."""
    data = StatusReportData(
        target_date=date(2026, 8, 25),
        reporting_date=date(2026, 8, 24),
        quality_summary={
            "total": 10,
            "pass": 7,
            "warn": 1,
            "fail": 1,
            "quarantine": 1,
            "reasons": {
                "infrastructure_exception:timeout": 1,
                "missing_trajectory_file": 1,
            },
        },
    )
    md = render_status_markdown(data)
    assert "### Evidence Quality Ledger" in md
    assert "- **Evaluated Trials:** 10 (Passed: 7, Warnings: 1, Failed: 1, Quarantined: 1)" in md
    assert "`infrastructure_exception:timeout`: 1" in md
    assert "`missing_trajectory_file`: 1" in md
