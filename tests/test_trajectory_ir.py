"""Tests for canonical TrajectoryIR lossless representation, normalization, and outlines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.evidence_store import load_blob
from evallab.trajectory_ir import (
    build_trajectory_ir,
    trajectory_ir_from_file,
    trajectory_ir_to_dict,
    trajectory_ir_to_outline,
)


def test_trajectory_ir_build_and_roundtrip(tmp_path: Path) -> None:
    """Verify building TrajectoryIR from standard ATIF dictionary and serializing back."""
    raw_atif = {
        "schema_version": "ATIF-v1.7",
        "session_id": "01a00436-eb1e-7573-a972-86c9f7fb0508",
        "agent": {
            "name": "codex",
            "version": "2.4.0",
            "model_name": "gpt-5.6-terra",
            "tools": [{"name": "bash", "description": "Execute bash commands"}],
            "extra": {"temperature": 0.2},
        },
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-08-25T10:00:00Z",
                "source": "agent",
                "message": "Let me list directory contents.",
                "reasoning_content": "Chain-of-thought analysis for directory inspection.",
                "temperature": 0.2,
                "top_p": 0.95,
                "sample_index": 0,
                "tool_calls": [
                    {
                        "tool_call_id": "tc-101",
                        "function_name": "bash",
                        "arguments": {"command": "ls -la"},
                    }
                ],
                "observation_results": [
                    {
                        "source_call_id": "tc-101",
                        "content": "file1.txt\nfile2.txt",
                        "extra": {"exit_code": 0, "status": "success"},
                    }
                ],
                "metrics": {
                    "prompt_tokens": 120,
                    "completion_tokens": 40,
                    "cached_tokens": 10,
                    "reasoning_tokens": 25,
                    "cost_usd": 0.00045,
                },
            }
        ],
        "final_metrics": {
            "total_prompt_tokens": 120,
            "total_completion_tokens": 40,
            "total_cached_tokens": 10,
            "total_cost_usd": 0.00045,
            "total_steps": 1,
        },
        "notes": "Test canary trajectory",
    }

    ir = build_trajectory_ir(raw_atif, store_root=tmp_path)
    assert ir.schema_version == "ATIF-v1.7"
    assert ir.agent_name == "codex"
    assert ir.model_name == "gpt-5.6-terra"
    assert len(ir.steps) == 1

    step0 = ir.steps[0]
    assert step0.step_id == 1
    assert step0.reasoning_content == "Chain-of-thought analysis for directory inspection."
    assert step0.reasoning_content_ref is not None
    assert step0.reasoning_content_ref.startswith("cas://sha256/")
    assert step0.metrics.reasoning_tokens == 25
    assert step0.sampling_params is not None
    assert step0.sampling_params.temperature == 0.2
    assert step0.sampling_params.top_p == 0.95
    assert step0.sample_index == 0

    assert len(step0.tool_calls) == 1
    assert step0.tool_calls[0].function_name == "bash"
    assert step0.tool_calls[0].arguments == {"command": "ls -la"}

    assert len(step0.observation_results) == 1
    assert step0.observation_results[0].content == "file1.txt\nfile2.txt"

    # Verify CAS retrieval
    stored_reasoning = load_blob(tmp_path, step0.reasoning_content_ref)
    assert stored_reasoning.decode("utf-8") == "Chain-of-thought analysis for directory inspection."

    # Verify serialization
    d = trajectory_ir_to_dict(ir)
    assert d["schema_version"] == "ATIF-v1.7"
    assert d["agent_name"] == "codex"
    assert len(d["steps"]) == 1
    assert d["loss_report"]["is_fully_declared"] is True


def test_trajectory_ir_from_file_and_outline(tmp_path: Path) -> None:
    """Verify reading TrajectoryIR from file on disk and converting to TrajectoryOutline."""
    traj_path = tmp_path / "trajectory.json"
    raw_data = {
        "schema_version": "ATIF-v1.7",
        "session_id": "test-session-file",
        "agent": {"name": "reviewer", "model_name": "claude-3.7-sonnet"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "message": "Writing test patch.",
                "tool_calls": [
                    {
                        "tool_call_id": "call-1",
                        "function_name": "write",
                        "arguments": {"path": "src/patch.py", "content": "print('ok')"},
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "call-1",
                            "content": "Wrote 15 bytes",
                            "extra": {"exit_code": 0},
                        }
                    ]
                },
                "metrics": {
                    "prompt_tokens": 50,
                    "completion_tokens": 20,
                    "cost_usd": 0.0001,
                },
            }
        ],
    }
    traj_path.write_text(json.dumps(raw_data), encoding="utf-8")

    ir = trajectory_ir_from_file(traj_path, store_root=tmp_path)
    assert ir.agent_name == "reviewer"
    assert ir.model_name == "claude-3.7-sonnet"
    assert ir.source_path == str(traj_path)
    assert ir.source_sha256 is not None

    # Strengthen observable assertions on nested observation retention
    assert len(ir.steps) == 1
    step0 = ir.steps[0]
    assert len(step0.observation_results) == 1
    obs0 = step0.observation_results[0]
    assert obs0.source_call_id == "call-1"
    assert obs0.content == "Wrote 15 bytes"
    assert obs0.extra == {"exit_code": 0}
    assert obs0.content_bytes == 14
    assert obs0.content_digest is not None
    assert obs0.content_ref is not None
    stored_obs = load_blob(tmp_path, obs0.content_ref)
    assert stored_obs.decode("utf-8") == "Wrote 15 bytes"
    outline = trajectory_ir_to_outline(
        ir,
        trial_id="trial-101",
        job_id="job-202",
        trial_name="trial-name",
        job_name="job-name",
        task_name="task-name",
    )
    assert outline.trial_id == "trial-101"
    assert outline.agent_name == "reviewer"
    assert outline.model_name == "claude-3.7-sonnet"
    assert outline.total_steps == 1
    assert outline.total_tool_calls == 1
    assert outline.step_to_first_tool == 1
    assert outline.step_to_first_edit == 1  # write is an edit tool
    assert outline.status == "featured"


def test_trajectory_ir_invalid_file_handling(tmp_path: Path) -> None:
    """Verify proper error handling for missing and malformed trajectory files."""
    missing = tmp_path / "nonexistent.json"
    with pytest.raises(FileNotFoundError):
        trajectory_ir_from_file(missing)

    malformed = tmp_path / "malformed.json"
    malformed.write_text("invalid json {{{", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid JSON"):
        trajectory_ir_from_file(malformed)

    non_object = tmp_path / "array.json"
    non_object.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        trajectory_ir_from_file(non_object)


def test_trajectory_ir_native_atif_observation_ingestion() -> None:
    """Verify native ATIF step.observation.results is ingested into step.observation_results."""
    raw_data = {
        "schema_version": "ATIF-v1.7",
        "session_id": "test-native-atif",
        "agent": {"name": "agent-native", "model_name": "gpt-5.6"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_call_id": "call-x",
                        "function_name": "bash",
                        "arguments": {"command": "echo 'hello'"},
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "call-x",
                            "content": "hello\n",
                            "extra": {"exit_code": 0},
                        }
                    ]
                },
            }
        ],
    }
    ir = build_trajectory_ir(raw_data)
    assert len(ir.steps) == 1
    assert len(ir.steps[0].observation_results) == 1
    obs = ir.steps[0].observation_results[0]
    assert obs.source_call_id == "call-x"
    assert obs.content == "hello\n"
    assert obs.extra == {"exit_code": 0}
    assert obs.content_bytes == 6

    # Verify serialization roundtrip preserves observation results
    d = trajectory_ir_to_dict(ir)
    step_d = d["steps"][0]
    assert len(step_d["observation_results"]) == 1
    assert step_d["observation_results"][0]["source_call_id"] == "call-x"
    assert step_d["observation_results"][0]["content"] == "hello\n"


def test_trajectory_ir_observation_dual_representation_equal_not_duplicated() -> None:
    """Verify that when both flattened and nested representations are equal, data is not duplicated."""
    raw_data = {
        "schema_version": "ATIF-v1.7",
        "session_id": "test-dual-equal",
        "agent": {"name": "agent-dual", "model_name": "gpt-5.6"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "observation_results": [
                    {
                        "source_call_id": "call-1",
                        "content": "output text",
                        "extra": {"exit_code": 0},
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "call-1",
                            "content": "output text",
                            "extra": {"exit_code": 0},
                        }
                    ]
                },
            }
        ],
    }
    ir = build_trajectory_ir(raw_data)
    assert len(ir.steps[0].observation_results) == 1
    assert ir.steps[0].observation_results[0].content == "output text"
    assert ir.steps[0].observation_results[0].source_call_id == "call-1"


def test_trajectory_ir_observation_dual_representation_conflict_rejected() -> None:
    """Verify that contradictory dual representations raise ValueError rather than silently discarding."""
    raw_data_content_mismatch = {
        "schema_version": "ATIF-v1.7",
        "session_id": "test-dual-conflict",
        "agent": {"name": "agent-dual", "model_name": "gpt-5.6"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "observation_results": [{"source_call_id": "call-1", "content": "flattened text"}],
                "observation": {
                    "results": [
                        {"source_call_id": "call-1", "content": "nested contradictory text"}
                    ]
                },
            }
        ],
    }
    with pytest.raises(ValueError, match="Contradictory observation data"):
        build_trajectory_ir(raw_data_content_mismatch)

    raw_data_count_mismatch = {
        "schema_version": "ATIF-v1.7",
        "session_id": "test-dual-conflict-count",
        "agent": {"name": "agent-dual", "model_name": "gpt-5.6"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "observation_results": [{"source_call_id": "call-1", "content": "text"}],
                "observation": {"results": []},
            }
        ],
    }
    with pytest.raises(ValueError, match="Contradictory observation data"):
        build_trajectory_ir(raw_data_count_mismatch)


def test_trajectory_ir_observation_malformed_nested_does_not_erase_evidence() -> None:
    """Verify malformed nested representation does not erase valid flattened evidence."""
    # Malformed non-dict observation container
    raw_data_string_obs = {
        "schema_version": "ATIF-v1.7",
        "session_id": "test-malformed-string",
        "agent": {"name": "agent", "model_name": "gpt-5.6"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "observation": "malformed string value",
                "observation_results": [{"source_call_id": "call-1", "content": "good evidence"}],
            }
        ],
    }
    ir = build_trajectory_ir(raw_data_string_obs)
    assert len(ir.steps[0].observation_results) == 1
    assert ir.steps[0].observation_results[0].content == "good evidence"

    # Malformed non-list results field inside observation container
    raw_data_non_list_results = {
        "schema_version": "ATIF-v1.7",
        "session_id": "test-malformed-results",
        "agent": {"name": "agent", "model_name": "gpt-5.6"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "observation": {"results": "not-a-list"},
                "observation_results": [{"source_call_id": "call-1", "content": "good evidence"}],
            }
        ],
    }
    ir2 = build_trajectory_ir(raw_data_non_list_results)
    assert len(ir2.steps[0].observation_results) == 1
    assert ir2.steps[0].observation_results[0].content == "good evidence"


def test_trajectory_ir_observation_missing_inline_content_preserves_content_ref() -> None:
    """Verify explicit raw content_ref is preserved when content is absent, without empty CAS hash."""
    raw_ref = "cas://sha256/a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
    raw_data = {
        "schema_version": "ATIF-v1.7",
        "session_id": "test-ref-preservation",
        "agent": {"name": "agent", "model_name": "gpt-5.6"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "observation": {
                    "results": [
                        {
                            "source_call_id": "call-ref",
                            "content_ref": raw_ref,
                            "content_bytes": 1024,
                            "extra": {"status": "external_blob"},
                        }
                    ]
                },
            }
        ],
    }
    ir = build_trajectory_ir(raw_data)
    obs = ir.steps[0].observation_results[0]
    assert obs.content is None
    assert obs.content_ref == raw_ref
    assert obs.content_bytes == 1024
    assert obs.content_digest == "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert obs.source_call_id == "call-ref"
    assert obs.extra == {"status": "external_blob"}

    # When content is absent and content_ref is absent, content_ref remains None (no synthesized hash)
    raw_data_no_ref = {
        "schema_version": "ATIF-v1.7",
        "session_id": "test-no-ref",
        "agent": {"name": "agent", "model_name": "gpt-5.6"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "observation": {
                    "results": [
                        {
                            "source_call_id": "call-empty",
                        }
                    ]
                },
            }
        ],
    }
    ir_no_ref = build_trajectory_ir(raw_data_no_ref)
    obs_no_ref = ir_no_ref.steps[0].observation_results[0]
    assert obs_no_ref.content is None
    assert obs_no_ref.content_ref is None
    assert obs_no_ref.content_digest is None
    assert obs_no_ref.content_bytes == 0
