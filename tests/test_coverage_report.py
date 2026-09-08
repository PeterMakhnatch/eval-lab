"""Behavioral tests for coverage and reconciliation report (M029 / Data Engineer)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from evallab.coverage_report import build_coverage_report
from evallab.evidence.atif import project_jobs
from evallab.results import load_job

FIXTURES = (Path(__file__).parent / "fixtures" / "explorer" / "jobs").resolve()


def test_coverage_report_projected_passing_job(tmp_path: Path) -> None:
    """A projected passing job shows under projected with correct trajectory availability."""
    job = load_job(FIXTURES / "job-pass")
    derived_root = tmp_path / "derived"
    project_jobs([job], derived_root)

    report = build_coverage_report(
        root=tmp_path,
        derived_root=derived_root,
        database_url="postgresql://invalid:5432/none",
    )

    assert report.projected.count == 1
    assert "job-pass" in report.projected.jobs
    assert report.projected.truncated is False

    agent_stats = report.trajectory_availability_by_agent["codex"]
    assert agent_stats.trials == 1
    assert agent_stats.with_trajectory == 1
    assert agent_stats.with_usage == 1
    assert agent_stats.reason is None


def test_coverage_report_non_atif_agent_expected(tmp_path: Path) -> None:
    """Oracle and nop trials legitimately have no model trajectory: with_trajectory=False."""
    job = load_job(FIXTURES / "job-notraj")
    derived_root = tmp_path / "derived"
    project_jobs([job], derived_root)

    report = build_coverage_report(
        root=tmp_path,
        derived_root=derived_root,
        database_url="postgresql://invalid:5432/none",
    )

    assert report.projected.count == 1
    assert "job-notraj" in report.projected.jobs

    agent_stats = report.trajectory_availability_by_agent["oracle"]
    assert agent_stats.trials == 1
    assert agent_stats.with_trajectory is False
    assert agent_stats.with_usage is False
    assert agent_stats.reason == "non_atif_agent_expected"


def test_coverage_report_unfinished_and_excluded_jobs(tmp_path: Path) -> None:
    """An unfinished or excluded job appears under excepted with reason and repair path."""
    # 1. Unfinished job on disk (has started_at but no finished_at)
    runs_dir = tmp_path / "runs"
    unfinished_dir = runs_dir / "job-unfinished-1"
    unfinished_dir.mkdir(parents=True)
    (unfinished_dir / "result.json").write_text(
        json.dumps({"id": "j-unf", "started_at": "2026-09-01T00:00:00Z"})
    )

    # 2. Excluded job outside this checkout via catalog loader
    def mock_catalog(db_url: str | None) -> tuple[dict, dict]:
        jobs = {
            "j-unf": {
                "id": "j-unf",
                "name": "job-unfinished-1",
                "path": "runs/job-unfinished-1",
            },
            "j-ext": {
                "id": "j-ext",
                "name": "job-external-1",
                "path": "/private/tmp/other-workspace/runs/job-external-1",
            },
        }
        return jobs, {}

    report = build_coverage_report(
        root=tmp_path,
        derived_root=tmp_path / "derived",
        database_url="postgresql://invalid:5432/none",
        catalog_loader=mock_catalog,
    )

    assert "job-unfinished-1" in report.excepted.jobs
    assert "job-external-1" in report.excepted.jobs
    assert report.reasons.get("job_unfinished", 0) >= 1
    assert report.reasons.get("outside_checkout", 0) >= 1

    repairs = {entry.job_name: entry for entry in report.repair_path}
    assert "job-unfinished-1" in repairs
    assert repairs["job-unfinished-1"].reason == "job_unfinished"
    assert repairs["job-unfinished-1"].origin == "catalog"
    assert repairs["job-unfinished-1"].resumable_command == (
        "unfinishable-by-ingest: re-run the job to completion, "
        "then evallab ingest runs/job-unfinished-1; or abandon"
    )

    assert "job-external-1" in repairs
    assert repairs["job-external-1"].reason == "outside_checkout"
    assert repairs["job-external-1"].origin == "catalog"
    assert repairs["job-external-1"].resumable_command.startswith("unresolvable-from-this-checkout")
    assert "/private/tmp/other-workspace/runs/job-external-1" in repairs["job-external-1"].resumable_command


def test_coverage_report_unfinished_fixture_yields_unfinishable_marker(tmp_path: Path) -> None:
    """An unfinished fixture job yields the unfinishable marker, never an evallab ingest command."""
    # Copy a real fixture job to scratch runs directory, but remove finished_at from result.json
    fixture_job_dir = FIXTURES / "job-fail"
    runs_dir = tmp_path / "runs"
    unfinished_job_dir = runs_dir / "fixture-unfinished"
    runs_dir.mkdir(parents=True)
    shutil.copytree(fixture_job_dir, unfinished_job_dir)

    result_json_path = unfinished_job_dir / "result.json"
    result_data = json.loads(result_json_path.read_text())
    result_data.pop("finished_at", None)
    result_json_path.write_text(json.dumps(result_data))

    report = build_coverage_report(
        root=tmp_path,
        derived_root=tmp_path / "derived",
        database_url="postgresql://invalid:5432/none",
    )

    assert "fixture-unfinished" in report.excepted.jobs
    repairs = {entry.job_name: entry for entry in report.repair_path}
    assert "fixture-unfinished" in repairs

    entry = repairs["fixture-unfinished"]
    assert entry.reason == "job_unfinished"
    assert entry.origin == "disk-only"

    # Must yield the unfinishable marker, NEVER a bare evallab ingest command
    assert not entry.resumable_command.startswith("evallab ingest")
    assert entry.resumable_command == (
        "unfinishable-by-ingest: re-run the job to completion, "
        "then evallab ingest runs/fixture-unfinished; or abandon"
    )


def test_coverage_report_as_dict_json_roundtrip(tmp_path: Path) -> None:
    """CoverageReport.as_dict() produces clean JSON that round-trips identically."""
    job = load_job(FIXTURES / "job-pass")
    derived_root = tmp_path / "derived"
    project_jobs([job], derived_root)

    report = build_coverage_report(
        root=tmp_path,
        derived_root=derived_root,
        database_url="postgresql://invalid:5432/none",
    )

    data = report.as_dict()
    assert isinstance(data, dict)

    # Required top-level keys
    expected_keys = {
        "native_jobs_present",
        "catalogued",
        "projected",
        "excepted",
        "failed",
        "trajectory_availability_by_agent",
        "reasons",
        "repair_path",
    }
    assert set(data.keys()) == expected_keys

    # Each repair path entry includes origin
    assert all("origin" in entry for entry in data["repair_path"])

    # JSON round-trip
    serialized = json.dumps(data)
    deserialized = json.loads(serialized)
    assert deserialized == data
