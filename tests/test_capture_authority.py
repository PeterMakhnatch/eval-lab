"""Focused regression tests for the deterministic capture-authority contract.

Tests:
1. Normal direct-call control: 1:1 direct ATIF tool calls matching benchmark events
   -> concordant, trajectory ordering admissible, benchmark events admissible.
2. Discordant shape 1 (action-64k-semantic_distractor-s__6vDNEHZ regression fixture):
   264 benchmark reads (266 total MCP tool calls) vs 17 ATIF direct tool calls (child curl loop in bash)
   -> discordant_indirect_execution, trajectory ordering inadmissible, benchmark events admissible.
3. Discordant shape 2 (action-64k-semantic_distractor-s__8aYeUds regression fixture):
   522 benchmark reads (525 total MCP tool calls, two passes, 1 typo handle error) vs 16 ATIF direct tool calls
   -> discordant_indirect_execution, trajectory ordering inadmissible, benchmark events admissible.
4. Live E0b range_batch shape with get_context_chunks:
   Direct batch tool call with explicit chunk_ids expanding to match benchmark events
   -> concordant_batch_capture, has_batch_tool_representation=True, trajectory ordering admissible.
5. Unexpandable batch representation:
   Batch tool called without deterministically expandable arguments or result
   -> discordant_batch_unexpandable, trajectory ordering inadmissible.
6. Missing benchmark events:
   -> no_benchmark_events, benchmark events inadmissible, retrieval authority atif_trajectory.
7. Missing ATIF trajectory:
   -> no_trajectory, trajectory ordering inadmissible, retrieval authority benchmark_events.
8. Schema-invalid benchmark events:
   -> schema invalid, retrieval authority unresolved.
9. Direct handle extraction:
   extract_direct_atif_handles extracts singular and batch handles without inventing fake calls.
10. Assessment digest determinism:
    Identical facts produce byte-identical SHA-256 digests.
11. Real promoted trial directory evaluation:
    Verifies 6vDNEHZ and 8aYeUds directly against promoted repository artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from evallab.evidence.capture_authority import (
    CaptureAuthority,
    CaptureConcordanceStatus,
    CaptureReasonCode,
    assess_capture_concordance,
    evaluate_capture_authority_from_dir,
    extract_direct_atif_handles,
    extract_direct_atif_tool_calls,
)


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_normal_direct_call_control_is_concordant_and_admissible(tmp_path: Path) -> None:
    """Normal direct-call control where ATIF calls match benchmark events 1:1."""
    trial_dir = tmp_path / "normal_direct_control"

    atif_payload = {
        "schema_version": "ATIF-v1.7",
        "session_id": "ses_control_1",
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_call_id": "call_1",
                        "function_name": "memory_mcp_list_context_chunks",
                        "arguments": {},
                    }
                ],
            },
            {
                "step_id": 2,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_call_id": "call_2",
                        "function_name": "memory_mcp_get_context_chunk",
                        "arguments": {"chunk_id": "ctx_alpha"},
                    },
                    {
                        "tool_call_id": "call_3",
                        "function_name": "memory_mcp_get_context_chunk",
                        "arguments": {"chunk_id": "ctx_beta"},
                    },
                ],
            },
            {
                "step_id": 3,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_call_id": "call_4",
                        "function_name": "memory_mcp_execute_mutation",
                        "arguments": {"entity_id": "e1", "attribute": "k", "value": "v"},
                    }
                ],
            },
        ],
    }

    benchmark_events = [
        {
            "event_ordinal": 1,
            "event_type": "tool_call_success",
            "tool_name": "list_context_chunks",
            "arguments": {},
            "result": {"status": "ok"},
        },
        {
            "event_ordinal": 2,
            "event_type": "tool_call_success",
            "tool_name": "get_context_chunk",
            "arguments": {"chunk_id": "ctx_alpha"},
            "result": {"status": "ok"},
        },
        {
            "event_ordinal": 3,
            "event_type": "tool_call_success",
            "tool_name": "get_context_chunk",
            "arguments": {"chunk_id": "ctx_beta"},
            "result": {"status": "ok"},
        },
        {
            "event_ordinal": 4,
            "event_type": "tool_call_success",
            "tool_name": "execute_mutation",
            "arguments": {"entity_id": "e1"},
            "result": {"status": "ok"},
        },
    ]

    _write_json(trial_dir / "agent" / "trajectory.json", atif_payload)
    _write_jsonl(
        trial_dir / "artifacts" / "app" / "output" / "benchmark-events.jsonl", benchmark_events
    )

    assessment = evaluate_capture_authority_from_dir(trial_dir)

    assert assessment.is_concordant is True
    assert assessment.has_indirect_child_execution is False
    assert assessment.concordance_status == CaptureConcordanceStatus.CONCORDANT
    assert assessment.retrieval_authority == CaptureAuthority.BENCHMARK_EVENTS
    assert assessment.trajectory_ordering_admissible is True
    assert assessment.benchmark_events_admissible is True
    assert CaptureReasonCode.CONCORDANT_DIRECT_CAPTURE in assessment.reason_codes
    assert assessment.atif_tool_call_count == 4
    assert assessment.benchmark_tool_call_count == 4


def test_discordant_shape_1_6vdnehz_indirect_shell_execution(tmp_path: Path) -> None:
    """Regression fixture exactly matching action-64k-semantic_distractor-s__6vDNEHZ:

    266 benchmark events (264 get_context_chunk + 1 list + 1 mutation) vs 17 ATIF direct tool calls
    (agent invoked bash to run a curl loop over 257 handles).
    """
    trial_dir = tmp_path / "action-64k-semantic_distractor-s__6vDNEHZ"

    # In ATIF: 1 list + 6 direct gets + 6 bash + 1 direct get + 2 bash + 1 mutation = 17 calls
    atif_calls = [
        {
            "tool_call_id": "call_list",
            "function_name": "memory_mcp_list_context_chunks",
            "arguments": {},
        },
        *(
            {
                "tool_call_id": f"call_direct_{i}",
                "function_name": "memory_mcp_get_context_chunk",
                "arguments": {"chunk_id": f"ctx_{i:04d}"},
            }
            for i in range(6)
        ),
        {
            "tool_call_id": "call_bash_init",
            "function_name": "bash",
            "arguments": {"command": "curl initialize"},
        },
        {
            "tool_call_id": "call_bash_notif",
            "function_name": "bash",
            "arguments": {"command": "curl notif"},
        },
        {
            "tool_call_id": "call_bash_file",
            "function_name": "bash",
            "arguments": {"command": "cat > chunks.txt"},
        },
        {
            "tool_call_id": "call_bash_loop",
            "function_name": "bash",
            "arguments": {"command": "while read cid; do curl ...; done"},
        },
        {
            "tool_call_id": "call_bash_grep",
            "function_name": "bash",
            "arguments": {"command": "grep -v distractor"},
        },
        {
            "tool_call_id": "call_bash_parse",
            "function_name": "bash",
            "arguments": {"command": "python3 parse.py"},
        },
        {
            "tool_call_id": "call_direct_last",
            "function_name": "memory_mcp_get_context_chunk",
            "arguments": {"chunk_id": "ctx_0007"},
        },
        {
            "tool_call_id": "call_bash_check",
            "function_name": "bash",
            "arguments": {"command": "cat result"},
        },
        {
            "tool_call_id": "call_bash_echo",
            "function_name": "bash",
            "arguments": {"command": "echo done"},
        },
        {
            "tool_call_id": "call_mutate",
            "function_name": "memory_mcp_execute_mutation",
            "arguments": {"value": "v2"},
        },
    ]
    assert len(atif_calls) == 17

    # In benchmark events: 1 list + 264 get_context_chunk + 1 mutation = 266 events
    benchmark_events = [
        {
            "event_ordinal": 1,
            "event_type": "tool_call_success",
            "tool_name": "list_context_chunks",
            "arguments": {},
            "result": {},
        },
        *(
            {
                "event_ordinal": 2 + i,
                "event_type": "tool_call_success",
                "tool_name": "get_context_chunk",
                "arguments": {"chunk_id": f"ctx_{i}"},
                "result": {"status": "ok"},
            }
            for i in range(264)
        ),
        {
            "event_ordinal": 266,
            "event_type": "tool_call_success",
            "tool_name": "execute_mutation",
            "arguments": {"value": "v2"},
            "result": {},
        },
    ]
    assert len(benchmark_events) == 266

    _write_json(
        trial_dir / "agent" / "trajectory.json",
        {"steps": [{"step_id": 1, "tool_calls": atif_calls}]},
    )
    _write_jsonl(
        trial_dir / "artifacts" / "app" / "output" / "benchmark-events.jsonl", benchmark_events
    )

    assessment = evaluate_capture_authority_from_dir(trial_dir)

    assert assessment.is_concordant is False
    assert assessment.has_indirect_child_execution is True
    assert assessment.concordance_status == CaptureConcordanceStatus.DISCORDANT_INDIRECT_EXECUTION
    assert assessment.retrieval_authority == CaptureAuthority.BENCHMARK_EVENTS
    assert assessment.trajectory_ordering_admissible is False
    assert assessment.benchmark_events_admissible is True
    assert CaptureReasonCode.INDIRECT_CHILD_EXECUTION in assessment.reason_codes
    assert assessment.atif_tool_call_count == 17
    assert assessment.benchmark_tool_call_count == 266


def test_discordant_shape_2_8ayeuds_indirect_shell_with_duplicate_passes(tmp_path: Path) -> None:
    """Regression fixture exactly matching action-64k-semantic_distractor-s__8aYeUds:

    525 benchmark events (522 get_context_chunk + 2 list + 1 mutation) vs 16 ATIF tool calls.
    """
    trial_dir = tmp_path / "action-64k-semantic_distractor-s__8aYeUds"

    # In ATIF: 1 list + 4 direct gets + 10 bash + 1 mutation = 16 calls
    atif_calls = [
        {
            "tool_call_id": "call_list",
            "function_name": "memory_mcp_list_context_chunks",
            "arguments": {},
        },
        *(
            {
                "tool_call_id": f"call_direct_{i}",
                "function_name": "memory_mcp_get_context_chunk",
                "arguments": {"chunk_id": f"ctx_{i}"},
            }
            for i in range(4)
        ),
        {
            "tool_call_id": "call_bash_init",
            "function_name": "bash",
            "arguments": {"command": "curl init"},
        },
        {
            "tool_call_id": "call_bash_fetch_sh",
            "function_name": "bash",
            "arguments": {"command": "./fetch.sh"},
        },
        {
            "tool_call_id": "call_bash_parse",
            "function_name": "bash",
            "arguments": {"command": "python3 parse.py"},
        },
        {
            "tool_call_id": "call_bash_list_server",
            "function_name": "bash",
            "arguments": {"command": "curl list"},
        },
        {
            "tool_call_id": "call_bash_fetch_single",
            "function_name": "bash",
            "arguments": {"command": "curl single"},
        },
        {
            "tool_call_id": "call_bash_loop2",
            "function_name": "bash",
            "arguments": {"command": "while read ...; do curl; done"},
        },
        {
            "tool_call_id": "call_bash_analyze",
            "function_name": "bash",
            "arguments": {"command": "python3 analyze.py"},
        },
        {
            "tool_call_id": "call_bash_debug",
            "function_name": "bash",
            "arguments": {"command": "python3 debug.py"},
        },
        {
            "tool_call_id": "call_bash_verify",
            "function_name": "bash",
            "arguments": {"command": "cat out.txt"},
        },
        {
            "tool_call_id": "call_bash_clean",
            "function_name": "bash",
            "arguments": {"command": "rm tmp"},
        },
        {
            "tool_call_id": "call_mutate",
            "function_name": "memory_mcp_execute_mutation",
            "arguments": {"value": "f3e822e6_v2"},
        },
    ]
    assert len(atif_calls) == 16

    # In benchmark events: 2 list + 522 get_context_chunk + 1 mutation = 525 events
    benchmark_events = [
        {
            "event_ordinal": 1,
            "event_type": "tool_call_success",
            "tool_name": "list_context_chunks",
            "arguments": {},
            "result": {},
        },
        *(
            {
                "event_ordinal": 2 + i,
                "event_type": "tool_call_success",
                "tool_name": "get_context_chunk",
                "arguments": {"chunk_id": f"ctx_{i}"},
                "result": {"status": "ok"},
            }
            for i in range(522)
        ),
        {
            "event_ordinal": 524,
            "event_type": "tool_call_success",
            "tool_name": "list_context_chunks",
            "arguments": {},
            "result": {},
        },
        {
            "event_ordinal": 525,
            "event_type": "tool_call_success",
            "tool_name": "execute_mutation",
            "arguments": {"value": "v2"},
            "result": {},
        },
    ]
    assert len(benchmark_events) == 525

    _write_json(
        trial_dir / "agent" / "trajectory.json",
        {"steps": [{"step_id": 1, "tool_calls": atif_calls}]},
    )
    _write_jsonl(
        trial_dir / "artifacts" / "app" / "output" / "benchmark-events.jsonl", benchmark_events
    )

    assessment = evaluate_capture_authority_from_dir(trial_dir)

    assert assessment.is_concordant is False
    assert assessment.has_indirect_child_execution is True
    assert assessment.concordance_status == CaptureConcordanceStatus.DISCORDANT_INDIRECT_EXECUTION
    assert assessment.retrieval_authority == CaptureAuthority.BENCHMARK_EVENTS
    assert assessment.trajectory_ordering_admissible is False
    assert assessment.benchmark_events_admissible is True
    assert CaptureReasonCode.INDIRECT_CHILD_EXECUTION in assessment.reason_codes
    assert assessment.atif_tool_call_count == 16
    assert assessment.benchmark_tool_call_count == 525


def test_live_e0b_shape_get_context_chunks_batch_expansion(tmp_path: Path) -> None:
    """Live E0b range_batch shape: get_context_chunks batch tool call with chunk_ids.

    Valid batch representation expands deterministically and is concordant,
    not classified as indirect child execution.
    """
    trial_dir = tmp_path / "e0b_batch_trial"

    chunk_ids = [f"ctx_{i:03d}" for i in range(10)]

    # In ATIF: 1 direct get_context_chunks tool call with explicit chunk_ids
    atif_payload = {
        "steps": [
            {
                "step_id": 1,
                "tool_calls": [
                    {
                        "tool_call_id": "call_batch_1",
                        "function_name": "memory_mcp_get_context_chunks",
                        "arguments": {"chunk_ids": chunk_ids},
                    }
                ],
            }
        ]
    }

    # In benchmark events: 10 get_context_chunk events corresponding to the 10 requested chunks
    benchmark_events = [
        {
            "event_ordinal": 1 + i,
            "event_type": "tool_call_success",
            "tool_name": "get_context_chunk",
            "arguments": {"chunk_id": cid},
            "result": {"status": "ok"},
        }
        for i, cid in enumerate(chunk_ids)
    ]

    _write_json(trial_dir / "agent" / "trajectory.json", atif_payload)
    _write_jsonl(
        trial_dir / "artifacts" / "app" / "output" / "benchmark-events.jsonl", benchmark_events
    )

    assessment = evaluate_capture_authority_from_dir(trial_dir)

    assert assessment.has_batch_tool_representation is True
    assert assessment.has_indirect_child_execution is False
    assert assessment.is_concordant is True
    assert assessment.concordance_status == CaptureConcordanceStatus.CONCORDANT
    assert assessment.trajectory_ordering_admissible is True
    assert assessment.benchmark_events_admissible is True
    assert CaptureReasonCode.CONCORDANT_BATCH_CAPTURE in assessment.reason_codes


def test_unexpandable_batch_tool_representation_refuses_trajectory_ordering(tmp_path: Path) -> None:
    """When a batch tool is called without deterministically expandable arguments,

    it is reason-coded as BATCH_TOOL_REPRESENTATION rather than false direct concordance.
    """
    trial_dir = tmp_path / "unexpandable_batch"

    atif_payload = {
        "steps": [
            {
                "step_id": 1,
                "tool_calls": [
                    {
                        "tool_call_id": "call_b1",
                        "function_name": "memory_mcp_get_context_chunks",
                        "arguments": {},  # Empty arguments, unexpandable
                    }
                ],
            }
        ]
    }

    benchmark_events = [
        {
            "event_ordinal": 1,
            "event_type": "tool_call_success",
            "tool_name": "get_context_chunk",
            "result": {"status": "ok"},
        },
        {
            "event_ordinal": 2,
            "event_type": "tool_call_success",
            "tool_name": "get_context_chunk",
            "result": {"status": "ok"},
        },
    ]

    _write_json(trial_dir / "agent" / "trajectory.json", atif_payload)
    _write_jsonl(
        trial_dir / "artifacts" / "app" / "output" / "benchmark-events.jsonl", benchmark_events
    )

    assessment = evaluate_capture_authority_from_dir(trial_dir)

    assert assessment.has_batch_tool_representation is True
    assert assessment.concordance_status == CaptureConcordanceStatus.DISCORDANT_BATCH_UNEXPANDABLE
    assert assessment.trajectory_ordering_admissible is False
    assert assessment.benchmark_events_admissible is True
    assert CaptureReasonCode.BATCH_TOOL_REPRESENTATION in assessment.reason_codes


def test_missing_benchmark_events_sets_appropriate_authority_and_status(tmp_path: Path) -> None:
    """When benchmark events are absent, retrieval authority defaults to ATIF if available."""
    trial_dir = tmp_path / "missing_benchmark_events"

    atif_payload = {
        "steps": [
            {
                "step_id": 1,
                "tool_calls": [
                    {
                        "tool_call_id": "call_1",
                        "function_name": "get_context_chunk",
                        "arguments": {"chunk_id": "ctx_1"},
                    }
                ],
            }
        ]
    }

    _write_json(trial_dir / "agent" / "trajectory.json", atif_payload)

    assessment = evaluate_capture_authority_from_dir(trial_dir)

    assert assessment.concordance_status == CaptureConcordanceStatus.NO_BENCHMARK_EVENTS
    assert assessment.retrieval_authority == CaptureAuthority.ATIF_TRAJECTORY
    assert assessment.benchmark_events_admissible is False
    assert CaptureReasonCode.MISSING_BENCHMARK_EVENTS in assessment.reason_codes


def test_missing_atif_trajectory_sets_no_trajectory_and_authority(tmp_path: Path) -> None:
    """When ATIF trajectory is absent, retrieval authority remains benchmark events."""
    trial_dir = tmp_path / "missing_atif_trajectory"

    benchmark_events = [
        {
            "event_ordinal": 1,
            "event_type": "tool_call_success",
            "tool_name": "get_context_chunk",
            "result": {"status": "ok"},
        }
    ]

    _write_jsonl(
        trial_dir / "artifacts" / "app" / "output" / "benchmark-events.jsonl", benchmark_events
    )

    assessment = evaluate_capture_authority_from_dir(trial_dir)

    assert assessment.concordance_status == CaptureConcordanceStatus.NO_TRAJECTORY
    assert assessment.retrieval_authority == CaptureAuthority.BENCHMARK_EVENTS
    assert assessment.trajectory_ordering_admissible is False
    assert assessment.benchmark_events_admissible is True
    assert CaptureReasonCode.MISSING_ATIF_TRAJECTORY in assessment.reason_codes


def test_schema_invalid_benchmark_events_unresolves_authority(tmp_path: Path) -> None:
    """When benchmark events are corrupted, authority becomes UNRESOLVED."""
    trial_dir = tmp_path / "corrupted_events"

    # Write corrupt non-JSON lines to benchmark events
    events_file = trial_dir / "artifacts" / "app" / "output" / "benchmark-events.jsonl"
    events_file.parent.mkdir(parents=True, exist_ok=True)
    events_file.write_text("CORRUPT_NON_JSON\n", encoding="utf-8")

    assessment = evaluate_capture_authority_from_dir(trial_dir)

    assert assessment.concordance_status == CaptureConcordanceStatus.NO_BENCHMARK_EVENTS
    assert assessment.retrieval_authority == CaptureAuthority.UNRESOLVED
    assert assessment.benchmark_events_admissible is False
    assert CaptureReasonCode.BENCHMARK_EVENT_SCHEMA_INVALID in assessment.reason_codes


def test_extract_direct_atif_handles_and_expansion() -> None:
    """Verify handle extraction helper supports singular and batch forms without fake calls."""
    payload = {
        "steps": [
            {
                "step_id": 1,
                "tool_calls": [
                    {
                        "tool_call_id": "c1",
                        "function_name": "memory_mcp_get_context_chunk",
                        "arguments": {"chunk_id": "ctx_singular"},
                    },
                    {
                        "tool_call_id": "c2",
                        "function_name": "memory_mcp_get_context_chunks",
                        "arguments": {"chunk_ids": ["ctx_b1", "ctx_b2", "ctx_b3"]},
                    },
                    {
                        "tool_call_id": "c3",
                        "function_name": "bash",
                        "arguments": {"command": "curl http://service/ctx_fake"},
                    },
                ],
            }
        ]
    }

    handles = extract_direct_atif_handles(payload)
    # Bash tool calls are NOT parsed as direct handles (never invent fake ATIF calls)
    assert handles == ["ctx_singular", "ctx_b1", "ctx_b2", "ctx_b3"]

    calls = extract_direct_atif_tool_calls(payload)
    assert len(calls) == 3


def test_assessment_digest_is_deterministic() -> None:
    """Assessment digest produces identical SHA-256 for identical facts."""
    a1 = assess_capture_concordance(
        [{"function_name": "get_context_chunk", "arguments": {"chunk_id": "c1"}}],
        [{"event_type": "tool_call_success", "tool_name": "get_context_chunk"}],
        trial_id="trial_1",
    )
    a2 = assess_capture_concordance(
        [{"function_name": "get_context_chunk", "arguments": {"chunk_id": "c1"}}],
        [{"event_type": "tool_call_success", "tool_name": "get_context_chunk"}],
        trial_id="trial_1",
    )

    assert a1.assessment_digest == a2.assessment_digest
    assert a1.assessment_digest.startswith("sha256:")
    assert len(a1.assessment_digest) == 71


def test_real_promoted_phase_a_discordant_trials_if_present() -> None:
    """If real promoted trial directories exist in repo, verify their capture assessments."""
    promoted_base = Path("research/evidence/runs/zai-overnight-action-phase-a-v2-20260830")
    t1 = promoted_base / "action-64k-semantic_distractor-s__6vDNEHZ"
    t2 = promoted_base / "action-64k-semantic_distractor-s__8aYeUds"

    if t1.is_dir():
        a1 = evaluate_capture_authority_from_dir(t1)
        assert a1.has_indirect_child_execution is True
        assert a1.concordance_status == CaptureConcordanceStatus.DISCORDANT_INDIRECT_EXECUTION
        assert a1.retrieval_authority == CaptureAuthority.BENCHMARK_EVENTS
        assert a1.trajectory_ordering_admissible is False
        assert a1.benchmark_events_admissible is True
        assert a1.atif_tool_call_count == 17
        assert a1.benchmark_tool_call_count == 266
        assert a1.benchmark_event_count == 266

    if t2.is_dir():
        a2 = evaluate_capture_authority_from_dir(t2)
        assert a2.has_indirect_child_execution is True
        assert a2.concordance_status == CaptureConcordanceStatus.DISCORDANT_INDIRECT_EXECUTION
        assert a2.retrieval_authority == CaptureAuthority.BENCHMARK_EVENTS
        assert a2.trajectory_ordering_admissible is False
        assert a2.benchmark_events_admissible is True
        assert a2.atif_tool_call_count == 16
        assert a2.benchmark_tool_call_count == 525
        assert a2.benchmark_event_count == 525
