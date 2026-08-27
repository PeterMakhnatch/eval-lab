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
    assert isinstance(alignment.normalized_edit_distance, float)
    assert 0.0 <= alignment.normalized_edit_distance <= 1.0

    # Check serialization
    d = alignment.to_dict()
    assert "alignment_id" in d
    assert "aligned_pairs" in d
    assert "normalized_edit_distance" in d
    assert d["normalized_edit_distance"] == alignment.normalized_edit_distance

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


def test_confounded_pair_rejection_on_missing_vs_present_digest(canary_pair: tuple[Path, Path], repo_root: Path) -> None:
    """Trials with asymmetric task/verifier digests are rejected as confounded pairs."""
    from dataclasses import replace
    trial_a, trial_b = canary_pair
    ir_a = build_trajectory_ir(trial_a, repo_root=repo_root)
    ir_b = build_trajectory_ir(trial_b, repo_root=repo_root)

    # Create asymmetric task digest (one present, one None)
    ir_a_mod = replace(ir_a, task_digest="sha256:present_digest")
    ir_b_mod = replace(ir_b, task_digest=None)

    with pytest.raises(ConfoundedPairError, match="Task digest mismatch"):
        align_trajectory_pair(ir_a_mod, ir_b_mod)

    # Create asymmetric verifier digest (one present, one None)
    ir_a_v = replace(ir_a, verifier_digest="sha256:present_verifier")
    ir_b_v = replace(ir_b, verifier_digest=None)

    with pytest.raises(ConfoundedPairError, match="Verifier digest mismatch"):
        align_trajectory_pair(ir_a_v, ir_b_v)


def test_k_star_citation_matches_exact_call_index_on_multi_call_step() -> None:
    """k* citation resolves the exact diverging call_index on multi-call steps."""
    from dataclasses import replace

    from evallab.traj import outline_trajectory
    from evallab.traj_baseline import compute_trace_baseline
    from evallab.trajectory_ir import IREvent, TrajectoryIR
    cit_0 = create_citation_handle(source_path="trajectory.json", step_id=2, call_index=0, tool_call_id="call_0", target_type="tool_call")
    cit_1 = create_citation_handle(source_path="trajectory.json", step_id=2, call_index=1, tool_call_id="call_1", target_type="tool_call")
    cit_diff = create_citation_handle(source_path="trajectory.json", step_id=2, call_index=1, tool_call_id="call_diff", target_type="tool_call")

    ev_a0 = IREvent(
        event_id="ea0", event_ordinal=0, event_type="tool_call", actor="agent",
        timestamp=None, phase="work", episode_id=1, step_index=2, call_index=0,
        action_family="file_read", status_owning_program="cat", argument_skeleton="cat file.txt",
        exit_code=0, exit_semantics="success", is_error=False, payload_digest="d0",
        payload_bytes=10, source_citation=cit_0, summary="cat file",
    )
    ev_a1 = IREvent(
        event_id="ea1", event_ordinal=1, event_type="tool_call", actor="agent",
        timestamp=None, phase="work", episode_id=1, step_index=2, call_index=1,
        action_family="file_edit", status_owning_program="edit", argument_skeleton="edit file.txt",
        exit_code=0, exit_semantics="success", is_error=False, payload_digest="d1",
        payload_bytes=10, source_citation=cit_1, summary="edit file",
    )
    ev_b1 = IREvent(
        event_id="eb1", event_ordinal=1, event_type="tool_call", actor="agent",
        timestamp=None, phase="work", episode_id=1, step_index=2, call_index=1,
        action_family="command_execution", status_owning_program="rm", argument_skeleton="rm file.txt",
        exit_code=0, exit_semantics="success", is_error=False, payload_digest="d2",
        payload_bytes=10, source_citation=cit_diff, summary="rm file",
    )

    ir_a = TrajectoryIR(
        ir_version="1.0", ir_digest="sha256:ir_a", trial_id="t_a", job_id="j", trial_name="t_a",
        job_name="j", task_name="common_task", task_digest="sha256:task", verifier_digest="sha256:ver",
        agent_scaffold="ag", agent_version=None, model_name="m", status="featured", unavailable_reason=None,
        final_verdict="PASS", primary_reward=1.0, exception_class=None, exception_message=None, duration_seconds=10.0,
        total_tokens=100, cost_usd=0.01, quality_status="pass", quality_findings=(), unpaired_tool_calls_count=0,
        linkage_coverage="complete", is_production_cas=False, events=(ev_a0, ev_a1), episodes=(),
        opportunity_windows=(), unknowns=(), baseline_metrics=compute_trace_baseline(outline_trajectory(Path(__file__))),
        evidence_coverage={}, source_digests={}, created_at="2026-08-26",
    )
    ir_b = replace(ir_a, ir_digest="sha256:ir_b", trial_id="t_b", trial_name="t_b", events=(ev_a0, ev_b1))

    alignment = align_trajectory_pair(ir_a, ir_b)
    assert alignment.divergence_step_a == 2
    assert alignment.divergence_step_b == 2
    assert alignment.citation_a is not None
    assert alignment.citation_b is not None
    assert alignment.citation_a.call_index == 1
    assert alignment.citation_a.tool_call_id == "call_1"
    assert alignment.citation_b.call_index == 1
    assert alignment.citation_b.tool_call_id == "call_diff"

def test_same_task_pass_vs_fail_counterfactual_alignment_allowed(canary_pair: tuple[Path, Path], repo_root: Path) -> None:
    """PASS vs FAIL trials of the same task align successfully without confounding."""
    from dataclasses import replace
    trial_a, trial_b = canary_pair
    ir_a = build_trajectory_ir(trial_a, repo_root=repo_root)
    ir_b = build_trajectory_ir(trial_b, repo_root=repo_root)

    # Modify ir_b to simulate a counterfactual FAIL outcome while keeping task/verifier identity identical
    ir_b_fail = replace(
        ir_b,
        final_verdict="FAIL",
        primary_reward=0.0,
    )

    alignment = align_trajectory_pair(ir_a, ir_b_fail)
    assert alignment.task_name == ir_a.task_name
    assert "PASS" in alignment.outcome_delta
    assert "FAIL" in alignment.outcome_delta
