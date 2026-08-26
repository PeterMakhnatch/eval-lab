"""Tests for EvidencePack v1: hierarchical compression, token budgeting, citation reopening, and coverage metrics."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.evidence_pack import (
    EvidenceCoverageMetrics,
    build_evidence_pack,
    compute_evidence_coverage_metrics,
    reopen_omitted_range,
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

def test_evidence_coverage_metrics_categories(canary_trial_dir: Path, repo_root: Path) -> None:
    """Verify complete category-wise coverage metrics computation."""
    ir = build_trajectory_ir(canary_trial_dir, repo_root=repo_root)
    pack = build_evidence_pack(ir, trial_dir=canary_trial_dir)
    assert pack.pack_digest.startswith("sha256:")

    metrics = compute_evidence_coverage_metrics(ir, trial_dir=canary_trial_dir)
    assert isinstance(metrics, EvidenceCoverageMetrics)
    assert metrics.has_atif is True
    assert metrics.has_result is True
    assert metrics.total_events > 0
    assert metrics.total_episodes > 0
    assert metrics.analysis_ready is True
    assert len(metrics.hold_reasons) == 0

    # Test dictionary serialization
    cov_dict = metrics.to_dict()
    assert cov_dict["has_atif"] is True
    assert cov_dict["analysis_ready"] is True
    assert "user_messages_count" in cov_dict
    assert "state_mutations_count" in cov_dict
    assert "verifier_executed" in cov_dict


def test_omitted_range_digest_verification_and_tamper_rejection(tmp_path: Path, repo_root: Path) -> None:
    """Mandatory test: omitted ranges carry canonical digest, reopen losslessly, and reject tampered digests."""
    import json
    from dataclasses import replace

    from evallab.evidence_pack import OmittedRange

    trial_dir = tmp_path / "t_long_omitted"
    (trial_dir / "agent").mkdir(parents=True)
    # Create trajectory with 10 steps (steps 1-3 instruction boundary, steps 4-7 routine inspection omitted, steps 8-10 terminal boundary)
    steps = [
        {"step_id": 1, "source": "user", "message": "initial task prompt"},
        {"step_id": 2, "source": "agent", "message": "planning"},
        {"step_id": 3, "source": "agent", "tool_calls": [{"name": "bash", "arguments": {"command": "pwd"}}]},
        {"step_id": 4, "source": "agent", "tool_calls": [{"name": "bash", "arguments": {"command": "ls -la"}}]},
        {"step_id": 5, "source": "agent", "tool_calls": [{"name": "bash", "arguments": {"command": "ls src/"}}]},
        {"step_id": 6, "source": "agent", "tool_calls": [{"name": "bash", "arguments": {"command": "cat README.md"}}]},
        {"step_id": 7, "source": "agent", "tool_calls": [{"name": "bash", "arguments": {"command": "cat config.json"}}]},
        {"step_id": 8, "source": "agent", "message": "finishing up"},
        {"step_id": 9, "source": "agent", "message": "all done"},
        {"step_id": 10, "source": "verifier", "message": "verifier pass"},
    ]
    (trial_dir / "agent" / "trajectory.json").write_text(json.dumps({"schema_version": "ATIF-v1.4", "steps": steps}))
    (trial_dir / "result.json").write_text(json.dumps({"id": "t_long", "trial_name": "t_long", "task_name": "synthetic/omitted-test", "verifier_result": {"rewards": {"reward": 1.0}}}))

    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path)
    pack = build_evidence_pack(ir, trial_dir=trial_dir, repo_root=tmp_path)

    assert len(pack.omitted_ranges) > 0, "Omitted ranges must be non-empty for long routine sequence"
    om = pack.omitted_ranges[0]
    assert len(om.event_ids) > 0
    assert om.omitted_content_digest.startswith("sha256:")

    # 1. Lossless reopening succeeds with digest match
    reopened = reopen_omitted_range(pack, om.range_id, ir=ir, trial_dir=trial_dir, repo_root=tmp_path)
    assert reopened.event_count == om.event_count
    assert len(reopened.events) == om.event_count

    # 2. Tampered digest raises ValueError
    tampered_ranges = list(pack.omitted_ranges)
    tampered_ranges[0] = OmittedRange(
        range_id=om.range_id,
        step_start=om.step_start,
        step_end=om.step_end,
        event_count=om.event_count,
        event_ids=om.event_ids,
        action_families=om.action_families,
        summary=om.summary,
        omitted_content_digest="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        reopening_citation=om.reopening_citation,
    )
    tampered_pack = replace(pack, omitted_ranges=tuple(tampered_ranges))
    with pytest.raises(ValueError, match="Omitted content digest mismatch"):
        reopen_omitted_range(tampered_pack, om.range_id, ir=ir, trial_dir=trial_dir, repo_root=tmp_path)
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


def test_reopen_omitted_range_invalid_id_raises_value_error(canary_trial_dir: Path, repo_root: Path) -> None:
    """Reopening a nonexistent omitted range id raises ValueError."""
    ir = build_trajectory_ir(canary_trial_dir, repo_root=repo_root)
    pack = build_evidence_pack(ir, trial_dir=canary_trial_dir)

    with pytest.raises(ValueError, match="not found in pack"):
        reopen_omitted_range(pack, 9999, trial_dir=canary_trial_dir, repo_root=repo_root)


def test_multi_call_citation_handle_hydration_identity(tmp_path: Path, repo_root: Path) -> None:
    """CitationHandle target_type='tool_call' hydrates specific tool call and sibling observation."""
    import json

    from evallab.trajectory_hydration import create_citation_handle, hydrate_citation

    trial_dir = tmp_path / "t_multi_cit"
    (trial_dir / "agent").mkdir(parents=True)
    raw_atif = {
        "schema_version": "ATIF-v1.4",
        "session_id": "sess-cit-test",
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [
                    {"name": "bash", "arguments": {"command": "cat foo.txt"}, "tool_call_id": "call_a"},
                    {"name": "edit", "arguments": {"file": "bar.txt"}, "tool_call_id": "call_b"},
                ],
                "observations": [
                    {"source_call_id": "call_a", "content": "hello foo"},
                    {"source_call_id": "call_b", "content": "edited bar"},
                ],
            }
        ],
    }
    (trial_dir / "agent" / "trajectory.json").write_text(json.dumps(raw_atif, indent=2))

    cit_a = create_citation_handle(
        source_path="agent/trajectory.json",
        step_id=1,
        call_index=0,
        tool_call_id="call_a",
        target_type="tool_call",
    )
    assert cit_a.tool_call_id == "call_a"
    assert cit_a.call_index == 0

    hydrated_a = hydrate_citation(cit_a, trial_dir=trial_dir, repo_root=tmp_path)
    parsed_a = json.loads(hydrated_a.redacted_content)
    assert "tool_call" in parsed_a
    assert parsed_a["tool_call"]["name"] == "bash"
    assert "observation" in parsed_a
    assert parsed_a["observation"]["content"] == "hello foo"
    # Does not duplicate the other tool call
    assert parsed_a["tool_call"]["tool_call_id"] == "call_a"


def test_five_tb3_cas_packs_determinism_and_rebuild() -> None:
    """Verify all five PR187 TB3 CAS packs rebuild byte-identically without exception fallbacks."""
    import json
    manifest_path = Path("/Users/petermakhnatch/Developer/eval-lab/research/experiments/manifests/terminal-bench-v3-k1-gemini-low-campaign-manifest.json")
    assert manifest_path.is_file(), f"Manifest missing: {manifest_path}"
    cas_store = Path("/Users/petermakhnatch/Developer/eval-lab/derived/evidence-cas")
    assert cas_store.is_dir(), f"Central CAS store missing: {cas_store}"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tb3_entries = [e for e in manifest["entries"] if e.get("atif_steps_count", 0) > 0 and e.get("role") != "free_control"]
    assert len(tb3_entries) >= 5, f"Expected at least 5 TB3 entries, got {len(tb3_entries)}"

    for entry in tb3_entries:
        trial_name = entry["trial_name"]
        ir1 = build_trajectory_ir(entry, store_root=cas_store)
        pack1 = build_evidence_pack(ir1, store_root=cas_store)

        ir2 = build_trajectory_ir(entry, store_root=cas_store)
        pack2 = build_evidence_pack(ir2, store_root=cas_store)
        assert ir1.ir_digest == ir2.ir_digest, f"IR digest mismatch for {trial_name}"
        assert pack1.pack_digest == pack2.pack_digest, f"Pack digest mismatch for {trial_name}"
        assert pack1.is_model_callable is True
        assert len(pack1.selected_windows) > 0
