"""Tests for TrajectoryIR v1: normalized events, action classification, and episode segmentation."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.trajectory_ir import (
    _classify_exit_semantics,
    _extract_status_owning_program,
    _normalize_argument_skeleton,
    build_trajectory_ir,
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def canary_trial_dir(repo_root: Path) -> Path:
    canary = repo_root / "research" / "evidence" / "runs" / "canary-transaction-reconciliation-codex-20260815" / "transaction-reconciliation__ba8ovxZ"
    if not canary.exists():
        canary = repo_root / "runs" / "canary-transaction-reconciliation-codex-20260815" / "transaction-reconciliation__ba8ovxZ"
    return canary


def test_status_owning_program_extraction() -> None:
    """Pipeline and command wrappers extract the right status-owning program."""
    assert _extract_status_owning_program("cat file.txt | grep 'pattern'", "bash") == "grep"
    assert _extract_status_owning_program("pytest tests/test_foo.py -v", "bash") == "pytest"
    assert _extract_status_owning_program("python3 -m unittest discover", "bash") == "python3_unittest"
    assert _extract_status_owning_program("sudo git diff --cached", "bash") == "git_diff"
    assert _extract_status_owning_program("cargo test --workspace", "bash") == "cargo_test"
    assert _extract_status_owning_program(None, "read_file") == "read_file"


def test_argument_skeleton_normalization() -> None:
    """Argument skeletons abstract file paths and hashes while retaining flags."""
    cmd1 = "pytest tests/unit/test_foo.py -v -k test_bar"
    skel1 = _normalize_argument_skeleton(cmd1, None)
    assert skel1 == "pytest <PATH> -v -k test_bar"

    cmd2 = "git checkout 01a0359a67d97491883ad6cb5de15153"
    skel2 = _normalize_argument_skeleton(cmd2, None)
    assert skel2 == "git checkout <DIGEST>"


def test_expected_negative_posix_exit_semantics() -> None:
    """grep/diff exit 1 is classified as expected_negative, not a fatal failure."""
    # Normal failing command
    sem_fail, is_err_fail = _classify_exit_semantics(1, "python", True)
    assert sem_fail == "error"
    assert is_err_fail is True

    # grep exit 1 (normal non-match)
    sem_grep, is_err_grep = _classify_exit_semantics(1, "grep", False)
    assert sem_grep == "expected_negative"
    assert is_err_grep is False

    # diff exit 1 (differences found)
    sem_diff, is_err_diff = _classify_exit_semantics(1, "diff", False)
    assert sem_diff == "expected_negative"
    assert is_err_diff is False

    # timeout exit code 124
    sem_to, is_err_to = _classify_exit_semantics(124, "pytest", True)
    assert sem_to == "timeout"
    assert is_err_to is True


def test_build_trajectory_ir_on_canary_trial(canary_trial_dir: Path, repo_root: Path) -> None:
    """Build deterministic TrajectoryIR for a real canary trial."""
    ir = build_trajectory_ir(canary_trial_dir, repo_root=repo_root)

    assert ir.ir_version == "1.0"
    assert ir.ir_digest.startswith("sha256:")
    assert ir.trial_name == "transaction-reconciliation__ba8ovxZ"
    assert ir.status == "featured"
    assert len(ir.events) > 0
    assert len(ir.episodes) > 0

    # Test event integrity
    for ev in ir.events:
        assert ev.event_id is not None
        assert ev.source_citation.step_index == ev.step_index
        assert ev.action_family in (
            "file_read",
            "file_write",
            "file_edit",
            "command_execution",
            "verification",
            "context_control",
            "model_reasoning",
            "other",
        )

    # Test digest determinism on re-run
    ir2 = build_trajectory_ir(canary_trial_dir, repo_root=repo_root)
    assert ir.ir_digest == ir2.ir_digest
