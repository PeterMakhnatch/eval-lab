"""Tests for EvidencePack v1: hierarchical compression, token budgeting, and citation reopening."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.evidence_pack import (
    build_evidence_pack,
)
from evallab.trajectory_hydration import RedactionPolicy
from evallab.trajectory_ir import build_trajectory_ir


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def canary_trial_dir(repo_root: Path) -> Path:
    canary = repo_root / "research" / "evidence" / "runs" / "canary-transaction-reconciliation-codex-20260815" / "transaction-reconciliation__ba8ovxZ"
    if not canary.exists():
        canary = repo_root / "runs" / "canary-transaction-reconciliation-codex-20260815" / "transaction-reconciliation__ba8ovxZ"
    return canary


def test_build_evidence_pack_hierarchical_structure(canary_trial_dir: Path, repo_root: Path) -> None:
    """EvidencePack produces global outline, episode summaries, and prioritized windows."""
    ir = build_trajectory_ir(canary_trial_dir, repo_root=repo_root)
    pack = build_evidence_pack(ir, trial_dir=canary_trial_dir, budget_tokens=16000)

    assert pack.pack_version == "1.0"
    assert pack.pack_digest.startswith("sha256:")
    assert pack.trial_name == "transaction-reconciliation__ba8ovxZ"
    assert pack.budget_tokens == 16000
    assert pack.consumed_tokens_est <= pack.budget_tokens

    # Global outline assertions
    assert "step_count" in pack.global_outline
    assert "tool_call_count" in pack.global_outline

    # Episode summaries present
    assert len(pack.episodes) > 0

    # Selected windows and omitted ranges have valid citations
    for w in pack.selected_windows:
        assert w.reopening_citation.source_path is not None
        assert w.reopening_citation.step_index is not None
        assert len(w.events) > 0

    # Markdown rendering
    md = pack.render_markdown()
    assert "# Evidence Pack: transaction-reconciliation__ba8ovxZ" in md
    assert "## Global Outline & Telemetry" in md
    assert "## Execution Episodes" in md

    # Pack digest determinism
    pack2 = build_evidence_pack(ir, trial_dir=canary_trial_dir, budget_tokens=16000)
    assert pack.pack_digest == pack2.pack_digest

def test_budget_overflow_marks_pack_uncallable(canary_trial_dir: Path, repo_root: Path) -> None:
    """When mandatory windows exceed token budget, pack is marked uncallable with tiered_pack_required."""
    ir = build_trajectory_ir(canary_trial_dir, repo_root=repo_root)
    # Intentionally tiny budget (10 tokens)
    pack = build_evidence_pack(ir, trial_dir=canary_trial_dir, budget_tokens=10)

    assert pack.is_model_callable is False
    assert pack.tiered_pack_required is True
    assert "mandatory_window_budget_overflow" in (pack.overflow_reason or "")


def test_redaction_policy_digest_mints_distinct_pack_digest(canary_trial_dir: Path, repo_root: Path) -> None:
    """Changing redaction policy produces a new deterministic pack digest."""
    ir = build_trajectory_ir(canary_trial_dir, repo_root=repo_root)

    pol1 = RedactionPolicy(redact_secrets=True)
    pol2 = RedactionPolicy(redact_secrets=False)

    pack1 = build_evidence_pack(ir, trial_dir=canary_trial_dir, policy=pol1)
    pack2 = build_evidence_pack(ir, trial_dir=canary_trial_dir, policy=pol2)

    assert pack1.redaction_profile_digest != pack2.redaction_profile_digest
    assert pack1.pack_digest != pack2.pack_digest
