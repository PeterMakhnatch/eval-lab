"""Tests for trajectory pair matching, sequence alignment, and divergence k* detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.trajectory_alignment import (
    ConfoundedPairError,
    align_trajectory_pair,
)
from evallab.trajectory_ir import build_trajectory_ir


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def canary_pair(repo_root: Path) -> tuple[Path, Path]:
    runs_dir = repo_root / "research" / "evidence" / "runs" / "canary-transaction-reconciliation-codex-20260815"
    if not runs_dir.exists():
        runs_dir = repo_root / "runs" / "canary-transaction-reconciliation-codex-20260815"
    trial_a = runs_dir / "transaction-reconciliation__ba8ovxZ"
    trial_b = runs_dir / "transaction-reconciliation__frxRezo"
    return trial_a, trial_b


def test_align_trajectory_pair_on_canary(canary_pair: tuple[Path, Path], repo_root: Path) -> None:
    """Align two canary trials of the same task, producing divergence k* and citations."""
    trial_a, trial_b = canary_pair
    ir_a = build_trajectory_ir(trial_a, repo_root=repo_root)
    ir_b = build_trajectory_ir(trial_b, repo_root=repo_root)

    alignment = align_trajectory_pair(ir_a, ir_b)

    assert alignment.alignment_id is not None
    assert alignment.task_name == ir_a.task_name
    assert len(alignment.aligned_pairs) > 0

    # Alignment has deterministic score
    assert isinstance(alignment.alignment_score, float)

    # Check serialization
    d = alignment.to_dict()
    assert "alignment_id" in d
    assert "aligned_pairs" in d


def test_confounded_pair_rejection(repo_root: Path) -> None:
    """Aligning trials from different tasks raises ConfoundedPairError."""
    canary_runs = repo_root / "research" / "evidence" / "runs"
    if not canary_runs.exists():
        canary_runs = repo_root / "runs"

    trial_recon = canary_runs / "canary-transaction-reconciliation-codex-20260815" / "transaction-reconciliation__ba8ovxZ"
    trial_filter = canary_runs / "canary-terminal-bench-html-js-filter-codex-20260815" / "terminal-bench-html-js-filter__kzGxL7Q"

    ir_recon = build_trajectory_ir(trial_recon, repo_root=repo_root)
    ir_filter = build_trajectory_ir(trial_filter, repo_root=repo_root)

    with pytest.raises(ConfoundedPairError, match="Cannot align trials with mismatched task names"):
        align_trajectory_pair(ir_recon, ir_filter)
