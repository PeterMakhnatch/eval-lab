"""Focused test suite for the Durable Trajectory Quality Ledger.

Tests:
1. Clean ATIF pass (is_ingestable=True, is_analysis_ready=True, status="pass")
2. Warning ATIF (is_ingestable=True, is_analysis_ready=True, status="warn")
3. Malformed ATIF (catalog-visible but analysis-ineligible, is_analysis_ready=False, status="fail")
4. Infrastructure exception quarantined with reason code (not reward 0, is_analysis_ready=False)
5. Idempotency: identical reruns produce deterministic, byte-stable Parquet tables
6. DuckDB queries trajectory_quality_reports and trajectory_quality_findings
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import pytest

from evallab.interpretation.trajectory_quality import (
    FindingSeverity,
    QualityStatus,
    TrajectoryQualityFinding,
    TrajectoryQualityReport,
    evaluate_trial_quality,
    persist_quality_ledger,
)


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
                "tool_calls": [
                    {"tool_call_id": "call-1", "function_name": "bash", "arguments": {"cmd": "ls"}}
                ],
                "observation": {"results": [{"source_call_id": "call-1", "content": "file.txt"}]},
            },
            {
                "step_id": 1,
                "timestamp": "2026-08-25T12:00:05Z",
                "tool_calls": [
                    {"tool_call_id": "call-2", "function_name": "finish", "arguments": {}}
                ],
                "observation": {"results": [{"source_call_id": "call-2", "content": "done"}]},
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


def test_fallback_atif_quality_uses_inspected_digest(sample_trial_dir: Path) -> None:
    trajectory = sample_trial_dir / "agent" / "trajectory.json"
    fallback = trajectory.rename(sample_trial_dir / "trajectory.json")

    report, findings = evaluate_trial_quality(sample_trial_dir)

    assert report.status == QualityStatus.PASS
    assert report.is_analysis_ready is True
    assert report.raw_atif_digest == "sha256:" + hashlib.sha256(fallback.read_bytes()).hexdigest()
    assert not any(f.code == "ATIF_MISSING" for f in findings)


def test_malformed_preferred_atif_is_not_masked_by_valid_fallback(sample_trial_dir: Path) -> None:
    trajectory = sample_trial_dir / "agent" / "trajectory.json"
    (sample_trial_dir / "trajectory.json").write_bytes(trajectory.read_bytes())
    trajectory.write_text("{broken", encoding="utf-8")

    report, findings = evaluate_trial_quality(sample_trial_dir)

    assert report.status == QualityStatus.FAIL
    assert report.raw_atif_digest == "sha256:" + hashlib.sha256(trajectory.read_bytes()).hexdigest()
    assert any(f.code == "ATIF_PARSE_ERROR" for f in findings)


def test_warning_atif_remains_ingestable_and_analysis_ready(sample_trial_dir: Path) -> None:
    """Non-fatal anomalies (like empty steps or non-monotonic ids) produce warnings but remain analysis ready."""
    traj_path = sample_trial_dir / "agent" / "trajectory.json"
    atif_data = {
        "schema_version": "ATIF-v1.7",
        "steps": [
            {
                "step_id": 1,
                "tool_calls": [{"name": "read", "arguments": {}}],
                "observation": {"results": []},  # unpaired observation generates warning
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


@pytest.mark.parametrize("use_fallback", [False, True])
def test_malformed_atif_is_catalog_visible_but_analysis_ineligible(
    sample_trial_dir: Path, use_fallback: bool
) -> None:
    """Malformed ATIF JSON remains catalog ingestable but is strictly ineligible for semantic analysis."""
    traj_path = sample_trial_dir / "agent" / "trajectory.json"
    if use_fallback:
        traj_path = traj_path.rename(sample_trial_dir / "trajectory.json")
    traj_path.write_text("{broken json syntax... [", encoding="utf-8")

    report, findings = evaluate_trial_quality(sample_trial_dir)
    assert report.status == QualityStatus.FAIL
    assert report.is_ingestable is True
    assert report.is_analysis_ready is False
    assert report.errors_count >= 1
    assert report.quarantine_reason is not None
    assert "atif_parse_error" in report.quarantine_reason
    assert any(f.code == "ATIF_PARSE_ERROR" for f in findings)


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


def test_control_run_without_atif_is_valid_but_not_analysis_ready(
    tmp_path: Path,
) -> None:
    """Oracle and Nop controls do not write ATIF; they are catalog-valid but excluded from agent analysis."""
    ctrl_dir = tmp_path / "runs" / "job-oracle" / "trial-oracle"
    ctrl_dir.mkdir(parents=True)
    res = {
        "id": "oracle-trial",
        "agent_name": "oracle",
        "agent_result": None,
        "agent_info": None,
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

    # Query the persisted report through the retained Parquet reader.
    with duckdb.connect() as conn:
        loaded = conn.execute(
            "SELECT status, is_analysis_ready FROM read_parquet(?) WHERE trial_id = ?",
            [str(rep_p2), "trial-1-id"],
        ).fetchall()
    assert loaded == [(QualityStatus.PASS, True)]


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
    # Query reports
    rows = conn.execute(
        f"SELECT trial_id, status, is_analysis_ready FROM read_parquet('{derived_root}/trajectory_quality_reports.parquet')"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("trial-duck", "pass", True)

    # Query findings
    f_rows = conn.execute(
        f"SELECT finding_id, code, severity FROM read_parquet('{derived_root}/trajectory_quality_findings.parquet')"
    ).fetchall()
    assert len(f_rows) == 1
    assert f_rows[0] == ("fnd-duck-1", "ATIF_SCHEMA_INVALID", "warn")


