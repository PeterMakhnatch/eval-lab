"""Strict verifier for mcp-funcdag-v1 trusted MCP event evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

EVENT_SCHEMA_VERSION = "mcp-tool-event-v1"
EVENT_TYPES = frozenset({"tool_call_success", "tool_call_error"})


def check_oracle_leak_exclusion(task_dir: Path) -> None:
    agent_workspace = task_dir / "agent_workspace"
    forbidden_tokens = [
        "expected_target_value",
        "reference_node_values",
        "topological_order",
        "node_expected_calls",
        "node_tool_map",
        "candidate_tool_names",
    ]
    if not agent_workspace.exists():
        return
    for root, _, files in os.walk(agent_workspace):
        for fname in files:
            fpath = Path(root) / fname
            if fpath.name == "result.json":
                continue
            try:
                text = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for token in forbidden_tokens:
                if token in text:
                    raise AssertionError(
                        f"Oracle truth leak detected in agent workspace: {fpath} contains {token}"
                    )


def _result_value(raw: Any) -> Any:
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"]
    return raw


def _event_is_valid(event: Any, allowed_tools: set[str]) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("schema_version") != EVENT_SCHEMA_VERSION:
        return False
    if event.get("event_type") not in EVENT_TYPES:
        return False
    if not isinstance(event.get("event_ordinal"), int) or event["event_ordinal"] <= 0:
        return False
    if not isinstance(event.get("tool_name"), str) or event["tool_name"] not in allowed_tools:
        return False
    if not isinstance(event.get("arguments"), dict):
        return False
    if event["event_type"] == "tool_call_success":
        return event.get("is_error") is False and isinstance(event.get("result"), dict) and event.get("error") is None
    error = event.get("error")
    return (
        event.get("is_error") is True
        and isinstance(error, dict)
        and isinstance(error.get("type"), str)
        and isinstance(error.get("message"), str)
        and event.get("result") is None
    )


def parse_result_artifact(workspace_dir: Path) -> tuple[bool, str | None, Any]:
    """Parse the agent result.json deliverable and return (format_ok, error_tag, parsed_value).

    Distinguishes artifact-format defects (missing file, invalid JSON, wrong type)
    from value / DAG-structure failures.
    """
    result_file = workspace_dir / "result.json"
    if not result_file.exists():
        alt = Path("/app/result.json")
        result_file = alt if alt.exists() else result_file
    if not result_file.exists():
        return False, "missing_result_file", None
    try:
        raw = json.loads(result_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"invalid_json:{exc}", None
    if not isinstance(raw, dict):
        return False, "not_a_json_object", None
    if "target_value" not in raw:
        return False, "missing_target_value_key", None
    val = raw["target_value"]
    if not isinstance(val, int) or isinstance(val, bool):
        return False, "non_integer_target_value", None
    return True, None, val


def verify_execution(
    task_dir: Path,
    verifier_truth_path: Path,
    evidence_dir: Path,
    workspace_dir: Path,
) -> dict[str, Any]:
    check_oracle_leak_exclusion(task_dir)
    if not verifier_truth_path.exists():
        return {
            "reward": 0.0,
            "reason": "Verifier truth file missing",
            "artifact_format_ok": False,
            "artifact_format_error": "verifier_truth_missing",
            "value_match": False,
            "dag_structure_ok": False,
        }

    verifier_truth = json.loads(verifier_truth_path.read_text(encoding="utf-8"))
    expected_target_value = verifier_truth["expected_target_value"]
    reference_node_values = verifier_truth["reference_node_values"]
    topological_order = verifier_truth["topological_order"]
    node_tool_map = verifier_truth["node_tool_map"]
    node_expected_calls = verifier_truth["node_expected_calls"]
    allowed_tools = set(verifier_truth["candidate_tool_names"])

    # 1. Parse and validate result.json artifact format
    artifact_format_ok, artifact_format_error, agent_target_value = parse_result_artifact(
        workspace_dir
    )
    if not artifact_format_ok:
        return {
            "reward": 0.0,
            "reason": f"Artifact format error: {artifact_format_error}",
            "artifact_format_ok": False,
            "artifact_format_error": artifact_format_error,
            "expected_target_value": expected_target_value,
            "agent_target_value": agent_target_value,
            "value_match": False,
            "dag_structure_ok": False,
            "dag_conformance": False,
            "value_propagation_accuracy": 0.0,
            "schema_conformance_rate": 0.0,
            "contiguous_ordinals": False,
            "total_calls": 0,
            "successful_calls": 0,
        }

    # 2. Parse and validate evidence events
    events_file = evidence_dir / "benchmark-events.jsonl"
    alt_events = Path("/app/output/benchmark-events.jsonl")
    if not events_file.exists() and alt_events.exists():
        events_file = alt_events
    events = [
        json.loads(line)
        for line in events_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if events_file.exists() else []

    schema_conformance_rate = (
        sum(_event_is_valid(event, allowed_tools) for event in events) / len(events)
        if events else 0.0
    )
    contiguous_ordinals = bool(events) and [
        event.get("event_ordinal") for event in events
    ] == list(range(1, len(events) + 1))
    successful_calls = [
        event for event in events if event.get("event_type") == "tool_call_success"
    ]

    # Only exact, bound-success calls satisfy the required DAG sequence: no extras may precede or follow.
    dag_conformance = len(successful_calls) == len(topological_order)
    valid_intermediate_count = 0
    if dag_conformance:
        for node_id, call in zip(topological_order, successful_calls, strict=True):
            expected_tool = node_tool_map[node_id]
            expected_val = reference_node_values[node_id]
            expected_args = node_expected_calls[node_id]["expected_args"]
            if (
                call["tool_name"] != expected_tool
                or call["arguments"] != expected_args
                or _result_value(call["result"]) != expected_val
            ):
                dag_conformance = False
                break
            valid_intermediate_count += 1

    value_propagation_accuracy = valid_intermediate_count / len(topological_order)
    value_match = agent_target_value == expected_target_value
    dag_structure_ok = (
        dag_conformance
        and value_propagation_accuracy == 1.0
        and schema_conformance_rate == 1.0
        and contiguous_ordinals
    )

    reward = float(artifact_format_ok and value_match and dag_structure_ok)
    return {
        "reward": reward,
        "artifact_format_ok": artifact_format_ok,
        "artifact_format_error": artifact_format_error,
        "value_match": value_match,
        "dag_structure_ok": dag_structure_ok,
        "expected_target_value": expected_target_value,
        "agent_target_value": agent_target_value,
        "schema_conformance_rate": schema_conformance_rate,
        "dag_conformance": dag_conformance,
        "value_propagation_accuracy": value_propagation_accuracy,
        "contiguous_ordinals": contiguous_ordinals,
        "total_calls": len(events),
        "successful_calls": len(successful_calls),
    }
