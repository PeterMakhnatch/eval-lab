"""Tests for trajectory pair matching, sequence alignment, and divergence k* detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.trajectory_alignment import (
    ConfoundedPairError,
    align_action_sequences,
    align_trajectory_pair,
)
from evallab.trajectory_hydration import create_citation_handle
from evallab.trajectory_ir import IREvent, build_trajectory_ir


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


def test_symmetric_unmatched_ranges_on_gap_pairs() -> None:
    """Symmetric alignment populates unmatched_ranges_a for gap_b and unmatched_ranges_b for gap_a."""
    cit = create_citation_handle(source_path="trajectory.json", step_id=1)
    
    # Create mock event sequence A with extra steps at the beginning
    ev_a1 = IREvent(
        event_id="ev_a1", event_ordinal=0, event_type="tool_call", actor="agent",
        timestamp=None, phase="work", episode_id=1, step_index=1, call_index=0,
        action_family="file_read", status_owning_program="cat", argument_skeleton="cat <PATH>",
        exit_code=0, exit_semantics="success", is_error=False, payload_digest="d1",
        payload_bytes=10, source_citation=cit, summary="cat file",
    )
    ev_a2 = IREvent(
        event_id="ev_a2", event_ordinal=1, event_type="tool_call", actor="agent",
        timestamp=None, phase="work", episode_id=1, step_index=2, call_index=0,
        action_family="verification", status_owning_program="pytest", argument_skeleton="pytest <PATH>",
        exit_code=0, exit_semantics="success", is_error=False, payload_digest="d2",
        payload_bytes=10, source_citation=cit, summary="pytest",
    )

    # Create mock event sequence B with extra steps at the end
    ev_b1 = IREvent(
        event_id="ev_b1", event_ordinal=0, event_type="tool_call", actor="agent",
        timestamp=None, phase="work", episode_id=1, step_index=1, call_index=0,
        action_family="verification", status_owning_program="pytest", argument_skeleton="pytest <PATH>",
        exit_code=0, exit_semantics="success", is_error=False, payload_digest="d2",
        payload_bytes=10, source_citation=cit, summary="pytest",
    )
    ev_b2 = IREvent(
        event_id="ev_b2", event_ordinal=1, event_type="tool_call", actor="agent",
        timestamp=None, phase="work", episode_id=1, step_index=2, call_index=0,
        action_family="file_write", status_owning_program="echo", argument_skeleton="echo <STR>",
        exit_code=0, exit_semantics="success", is_error=False, payload_digest="d3",
        payload_bytes=10, source_citation=cit, summary="echo",
    )

    seq_a = [(1, 0, ("file_read", "cat", "cat <PATH>"), ev_a1), (2, 0, ("verification", "pytest", "pytest <PATH>"), ev_a2)]
    seq_b = [(1, 0, ("verification", "pytest", "pytest <PATH>"), ev_b1), (2, 0, ("file_write", "echo", "echo <STR>"), ev_b2)]

    aligned, score = align_action_sequences(seq_a, seq_b)

    # Verify gap_b (unmatched in A) and gap_a (unmatched in B) both exist
    gap_b = [p for p in aligned if p.match_quality == "gap_b"]
    gap_a = [p for p in aligned if p.match_quality == "gap_a"]
    assert len(gap_b) >= 1
    assert len(gap_a) >= 1
