"""Tests for Trajectory Readiness and HOLD Batch Audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.interpretation.trajectory_readiness import audit_durable_trajectories


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_audit_durable_trajectories_scans_and_reports(repo_root: Path) -> None:
    """Verify batch audit scans real durable runs, produces valid counts, and generates Markdown."""
    report = audit_durable_trajectories(repo_root=repo_root)

    assert report.total_trials_scanned >= 5
    assert report.analysis_ready_count > 0
    assert 0.0 <= report.analysis_ready_ratio <= 1.0
    assert len(report.trial_records) == report.total_trials_scanned

    # Markdown rendering
    md = report.render_markdown()
    assert "# Batch Trajectory Readiness & HOLD Report" in md
    assert "## Detailed Trial Inventory" in md
    assert "READY" in md

    # JSON serialization
    d = report.to_dict()
    assert d["total_trials_scanned"] == report.total_trials_scanned
    assert "trial_records" in d
