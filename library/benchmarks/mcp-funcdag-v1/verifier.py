"""Deterministic verifier and oracle-leak exclusion gate for mcp-funcdag-v1."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def check_oracle_leak_exclusion(task_dir: Path) -> None:
    agent_workspace = task_dir / "agent_workspace"
    forbidden_tokens = ["expected_target_value", "reference_node_values", "topological_order"]
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
    required_tools = [verifier_truth["node_tool_map"][nid] for nid in topological_order]

    result_file = workspace_dir / "result.json"
    if not result_file.exists():
        alt = Path("/app/output/result.json")
        result_file = alt if alt.exists() else result_file
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
    executed_tools = [e["tool_name"] for e in successful_calls]

    tool_idx = 0
    dag_conformance = True
    for req_tool in required_tools:
        try:
            found_pos = executed_tools.index(req_tool, tool_idx)
            tool_idx = found_pos + 1
        except ValueError:
            dag_conformance = False
            break

    consumed: set[int] = set()
    valid_intermediate_count = 0
    for node_id in topological_order:
        tool_name = verifier_truth["node_tool_map"][node_id]
        expected_val = reference_node_values[node_id]
        matched = False
        for idx, call in enumerate(successful_calls):
            if idx in consumed:
                continue
            if call.get("tool_name") == tool_name and _result_value(call.get("result")) == expected_val:
                consumed.add(idx)
                matched = True
                break
        if matched:
            valid_intermediate_count += 1
    value_propagation_accuracy = (
        valid_intermediate_count / len(topological_order) if topological_order else 0.0
    )
    reward = 0.0
    if (
        agent_target_value == expected_target_value
        and dag_conformance
        and value_propagation_accuracy == 1.0
    ):
        reward = 1.0
    return {
        "reward": reward,
        "expected_target_value": expected_target_value,
        "agent_target_value": agent_target_value,
        "schema_conformance_rate": schema_conformance_rate,
        "dag_conformance": dag_conformance,
        "value_propagation_accuracy": value_propagation_accuracy,
        "total_calls": total_events,
        "successful_calls": len(successful_calls),
    }
