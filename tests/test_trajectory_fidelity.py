"""Tests for ATIF Fidelity & Loss Manifest (P1).

Verifies declared per-field loss manifest, CAS reasoning preservation,
token ID persistence, sampling parameter extraction, and zero undeclared field loss.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from evallab.evidence_store import load_blob, store_blob
from evallab.traj import outline_trajectory
from evallab.trajectory_ir import (
    TrajectoryIR,
    build_trajectory_ir,
    trajectory_ir_to_dict,
)
from evallab.trajectory_loss_manifest import (
    FieldLossEntry,
    LossManifest,
    TrajectoryFieldAudit,
    TrajectoryLossReport,
    audit_trajectory_loss,
    get_declared_loss_manifest,
)


def test_declared_loss_manifest_completeness() -> None:
    """Verify declared loss manifest contains all core ATIF and Harbor fields."""
    manifest = get_declared_loss_manifest()
    fields = manifest.declared_fields
    assert len(fields) >= 40

    # Key fields must be declared
    assert "root.schema_version" in fields
    assert fields["root.schema_version"].status == "preserved"

    assert "step.reasoning_content" in fields
    assert fields["step.reasoning_content"].status == "preserved"
    assert fields["step.reasoning_content"].storage_tier in {"in_memory_ir", "cas_blob"}

    assert "step.metrics.reasoning_tokens" in fields
    assert fields["step.metrics.reasoning_tokens"].status == "preserved"

    assert "step.metrics.prompt_token_ids" in fields
    assert fields["step.metrics.prompt_token_ids"].status == "preserved"

    assert "step.sampling_params" in fields
    assert fields["step.sampling_params"].status == "preserved"

    assert "step.sample_index" in fields
    assert fields["step.sample_index"].status == "preserved"

    # Every digested or dropped entry must have non-empty reason
    for field_path, entry in fields.items():
        if entry.status in {"digested", "dropped"}:
            assert entry.reason is not None and entry.reason != "", f"Missing reason for non-preserved field {field_path}"


def test_audit_trajectory_fidelity_detects_undeclared_fields() -> None:
    """Audit should pass for standard ATIF and flag unexpected fields."""
    clean_atif = {
        "schema_version": "ATIF-v1.7",
        "session_id": "test-session-001",
        "agent": {
            "name": "codex",
            "version": "1.0.0",
            "model_name": "gpt-5.6-terra",
        },
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-08-25T12:00:00Z",
                "source": "agent",
                "message": "Let me inspect the environment.",
                "reasoning_content": "Detailed multi-step chain of thought planning.",
                "tool_calls": [
                    {
                        "tool_call_id": "tc-1",
                        "function_name": "bash",
                        "arguments": {"command": "ls -la"},
                    }
                ],
                "observation_results": [
                    {
                        "source_call_id": "tc-1",
                        "content": "total 0",
                        "type": "text",
                        "status": "success",
                    }
                ],
                "metrics": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "reasoning_tokens": 30,
                    "cost_usd": 0.001,
                },
            }
        ],
    }
    report = audit_trajectory_loss(clean_atif)
    assert report.is_fully_declared
    assert len(report.undeclared_fields) == 0
    assert report.preserved_fields_count > 0

    # Add an undeclared field
    dirty_atif = dict(clean_atif)
    dirty_atif["steps"] = [dict(clean_atif["steps"][0])]
    dirty_atif["steps"][0]["unknown_synthetic_mutation_field"] = 12345

    dirty_report = audit_trajectory_loss(dirty_atif)
    assert not dirty_report.is_fully_declared
    assert "step.unknown_synthetic_mutation_field" in dirty_report.undeclared_fields


def test_build_trajectory_ir_cas_reasoning_and_tokens(tmp_path: Path) -> None:
    """Verify build_trajectory_ir stores reasoning content and tokens into CAS."""
    raw_atif = {
        "schema_version": "ATIF-v1.7",
        "session_id": "01a00436-eb1e-7573-a972-86c9f7fb0508",
        "agent": {
            "name": "test-agent",
            "model_name": "claude-3.7-sonnet",
        },
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-08-25T14:00:00Z",
                "source": "agent",
                "message": "Executing task step 1.",
                "reasoning_content": "Deep analytical thinking string with private thoughts.",
                "temperature": 0.2,
                "top_p": 0.95,
                "sample_index": 0,
                "tool_calls": [
                    {
                        "tool_call_id": "call_1",
                        "function_name": "execute",
                        "arguments": {"cmd": "pytest"},
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "content": "tests passed",
                            "status": "success",
                            "extra": {"exit_code": 0},
                        }
                    ]
                },
                "metrics": {
                    "prompt_tokens": 500,
                    "completion_tokens": 120,
                    "extra": {
                        "reasoning_output_tokens": 80,
                    },
                    "prompt_token_ids": [101, 2043, 2003],
                    "completion_token_ids": [102, 3001],
                    "logprobs": [-0.01, -0.05],
                },
            }
        ],
    }

    store_root = tmp_path / "cas_store"
    ir = build_trajectory_ir(raw_atif, store_root=store_root)

    assert ir.schema_version == "ATIF-v1.7"
    assert ir.agent_name == "test-agent"
    assert len(ir.steps) == 1

    step = ir.steps[0]
    assert step.step_id == 1
    assert step.reasoning_content == "Deep analytical thinking string with private thoughts."
    assert step.reasoning_content_ref is not None
    assert step.reasoning_content_ref.startswith("cas://sha256/")

    # Read back from CAS store directly
    restored_reasoning = load_blob(store_root, step.reasoning_content_ref).decode("utf-8")
    assert restored_reasoning == "Deep analytical thinking string with private thoughts."

    # Verify reasoning tokens and sampling params
    assert step.metrics is not None
    assert step.metrics.reasoning_tokens == 80
    assert step.metrics.prompt_token_ids_ref is not None
    assert step.sampling_params is not None
    assert step.sampling_params.temperature == 0.2
    assert step.sampling_params.top_p == 0.95
    assert step.sample_index == 0

    # Verify serialization to dict
    ir_dict = trajectory_ir_to_dict(ir)
    assert ir_dict["schema_version"] == "ATIF-v1.7"
    assert ir_dict["steps"][0]["reasoning_content_ref"] == step.reasoning_content_ref
    assert ir_dict["loss_report"]["is_fully_declared"] is True


def test_outline_trajectory_preserves_fidelity_refs(tmp_path: Path) -> None:
    """Verify outline_trajectory integrates reasoning refs and metadata."""
    trial_dir = tmp_path / "trial_run"
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True)

    traj_content = {
        "schema_version": "ATIF-v1.7",
        "agent": {"name": "reviewer", "model_name": "gemini-3.7-flash"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "reasoning_content": "Chain-of-thought hypothesis testing.",
                "tool_calls": [
                    {
                        "function_name": "bash",
                        "arguments": {"command": "cargo test"},
                    }
                ],
                "metrics": {
                    "prompt_tokens": 100,
                    "completion_tokens": 40,
                    "reasoning_tokens": 25,
                },
                "temperature": 0.0,
            }
        ],
    }
    (agent_dir / "trajectory.json").write_text(json.dumps(traj_content), encoding="utf-8")
    (trial_dir / "result.json").write_text(
        json.dumps({"id": "trial_123", "trial_name": "trial_123", "task_name": "test_task"}),
        encoding="utf-8",
    )

    store_root = tmp_path / "cas"
    outline = outline_trajectory(
        target=trial_dir,
        repo_root=tmp_path,
        explicit_runs_root=tmp_path,
        store_root=store_root,
    )

    assert outline.status == "featured"
    assert len(outline.steps) == 1
    step = outline.steps[0]
    assert step.reasoning_content == "Chain-of-thought hypothesis testing."
    assert step.reasoning_content_ref is not None
    assert step.reasoning_tokens == 25
    assert step.sampling_params is not None
    assert step.sampling_params.get("temperature") == 0.0
