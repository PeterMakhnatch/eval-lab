"""Deterministic verifier and oracle-leak exclusion gate for mcp-funcdag-v1."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def check_oracle_leak_exclusion(task_dir: Path) -> None:
    agent_workspace = task_dir / "agent_workspace"
    forbidden_tokens = [
        "expected_target_value",
        "reference_node_values",
        "topological_order",
        "node_expected_calls",
        "node_tool_map",
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


def verify_execution(
    task_dir: Path,
    verifier_truth_path: Path,
    evidence_dir: Path,
    workspace_dir: Path,
) -> dict[str, Any]:
    check_oracle_leak_exclusion(task_dir)
    if not verifier_truth_path.exists():
        return {"reward": 0.0, "reason": "Verifier truth file missing"}

    verifier_truth = json.loads(verifier_truth_path.read_text(encoding="utf-8"))
    expected_target_value = verifier_truth["expected_target_value"]
    reference_node_values = verifier_truth["reference_node_values"]
    topological_order = verifier_truth["topological_order"]
    node_tool_map = verifier_truth["node_tool_map"]
    node_expected_calls = verifier_truth.get("node_expected_calls", {})

    result_file = workspace_dir / "result.json"
    if not result_file.exists():
        alt = Path("/app/result.json")
        result_file = alt if alt.exists() else result_file
    if not result_file.exists():
        alt2 = Path("/app/output/result.json")
        result_file = alt2 if alt2.exists() else result_file
    if not result_file.exists():
        return {
            "reward": 0.0,
            "reason": "Agent result.json missing from workspace",
            "schema_conformance_rate": 0.0,
            "dag_conformance": False,
            "value_propagation_accuracy": 0.0,
        }
    try:
        agent_target_value = json.loads(result_file.read_text(encoding="utf-8")).get("target_value")
    except Exception as exc:
        return {
            "reward": 0.0,
            "reason": f"Failed to parse agent result.json: {exc}",
            "schema_conformance_rate": 0.0,
            "dag_conformance": False,
            "value_propagation_accuracy": 0.0,
        }

    events_file = evidence_dir / "benchmark-events.jsonl"
    alt_events = Path("/app/output/benchmark-events.jsonl")
    if not events_file.exists() and alt_events.exists():
        events_file = alt_events
    events = []
    if events_file.exists():
        for line in events_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))

    total_events = len(events)
    conforming_events = [e for e in events if e.get("schema_conforming", True)]
    schema_conformance_rate = (len(conforming_events) / total_events) if total_events else 0.0
    successful_calls = [e for e in events if e.get("event_type") == "tool_call_success"]

    # Verify contiguous ordinals
    ordinals = [e.get("event_index") for e in events if "event_index" in e]
    contiguous_ordinals = (ordinals == list(range(len(ordinals)))) if ordinals else True

    # Exact per-node verification: matching tool, input arguments, and output result in topological order
    tool_idx = 0
    dag_conformance = True
    valid_intermediate_count = 0

    for node_id in topological_order:
        expected_tool = node_tool_map[node_id]
        expected_val = reference_node_values[node_id]
        expected_call_meta = node_expected_calls.get(node_id, {})
        expected_args = expected_call_meta.get("expected_args")

        found_match = False
        while tool_idx < len(successful_calls):
            call = successful_calls[tool_idx]
            tool_idx += 1
            call_tool = call.get("tool_name")
            call_res = _result_value(call.get("result"))
            call_args = call.get("arguments", {})

            # Match tool name
            if call_tool == expected_tool:
                # Match result value
                if call_res == expected_val:
                    # Match input arguments if present in truth
                    if expected_args is None or call_args == expected_args:
                        found_match = True
                        valid_intermediate_count += 1
                        break
        if not found_match:
            dag_conformance = False
            break

    value_propagation_accuracy = (
        valid_intermediate_count / len(topological_order) if topological_order else 0.0
    )

    reward = 0.0
    if (
        agent_target_value == expected_target_value
        and dag_conformance
        and value_propagation_accuracy == 1.0
        and contiguous_ordinals
    ):
        reward = 1.0

    return {
        "reward": reward,
        "expected_target_value": expected_target_value,
        "agent_target_value": agent_target_value,
        "schema_conformance_rate": schema_conformance_rate,
        "dag_conformance": dag_conformance,
        "value_propagation_accuracy": value_propagation_accuracy,
        "contiguous_ordinals": contiguous_ordinals,
        "total_calls": total_events,
        "successful_calls": len(successful_calls),
    }
