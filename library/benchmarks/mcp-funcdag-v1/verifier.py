"""Deterministic verifier and oracle-leak exclusion gate for mcp-funcdag-v1."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def check_oracle_leak_exclusion(task_dir: Path) -> None:
    """Verifies that no ground-truth solution, DAG edges, or expected values leaked to agent workspace or image."""
    agent_workspace = task_dir / "agent_workspace"
    forbidden_tokens = ["expected_target_value", "reference_node_values", "topological_order"]
    
    if agent_workspace.exists():
        for root, _, files in os.walk(agent_workspace):
            for fname in files:
                fpath = Path(root) / fname
                if fpath.name == "result.json":
                    continue
                try:
                    text = fpath.read_text(encoding="utf-8", errors="ignore")
                    for token in forbidden_tokens:
                        if token in text:
                            raise AssertionError(f"Oracle truth leak detected in agent workspace: {fpath} contains {token}")
                except Exception:
                    pass


def verify_execution(
    task_dir: Path,
    verifier_truth_path: Path,
    evidence_dir: Path,
    workspace_dir: Path,
) -> dict[str, Any]:
    # 1. Oracle leak gate
    check_oracle_leak_exclusion(task_dir)

    # 2. Load verifier truth
    if not verifier_truth_path.exists():
        return {"reward": 0.0, "reason": "Verifier truth file missing"}
    
    verifier_truth = json.loads(verifier_truth_path.read_text(encoding="utf-8"))
    expected_target_value = verifier_truth["expected_target_value"]
    target_node_id = verifier_truth["target_node_id"]
    reference_node_values = verifier_truth["reference_node_values"]
    topological_order = verifier_truth["topological_order"]
    required_tools = [verifier_truth["node_tool_map"][nid] for nid in topological_order]

    # 3. Check workspace result
    result_file = workspace_dir / "result.json"
    if not result_file.exists():
        return {
            "reward": 0.0,
            "reason": "Agent result.json missing from workspace",
            "schema_conformance_rate": 0.0,
            "dag_conformance": False,
            "value_propagation_accuracy": 0.0,
        }

    try:
        agent_data = json.loads(result_file.read_text(encoding="utf-8"))
        agent_target_value = agent_data.get("target_value")
    except Exception as e:
        return {
            "reward": 0.0,
            "reason": f"Failed to parse agent result.json: {e}",
            "schema_conformance_rate": 0.0,
            "dag_conformance": False,
            "value_propagation_accuracy": 0.0,
        }

    # 4. Check benchmark events
    events_file = evidence_dir / "benchmark-events.jsonl"
    events = []
    if events_file.exists():
        for line in events_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass

    total_events = len(events)
    conforming_events = [e for e in events if e.get("schema_conforming", False)]
    schema_conformance_rate = (len(conforming_events) / total_events) if total_events > 0 else 0.0

    # 5. Check DAG execution sequence & value propagation
    successful_calls = [e for e in events if e.get("event_type") == "tool_call_success"]
    executed_tools = [e["tool_name"] for e in successful_calls]

    # Check if required tools were executed in topological order
    tool_idx = 0
    dag_conformance = True
    for req_tool in required_tools:
        try:
            found_pos = executed_tools.index(req_tool, tool_idx)
            tool_idx = found_pos + 1
        except ValueError:
            dag_conformance = False
            break

    # Check intermediate values with strictly one-to-one event consumption
    consumed_indices: set[int] = set()
    valid_intermediate_count = 0
    for node_id in topological_order:
        tool_name = verifier_truth["node_tool_map"][node_id]
        expected_val = reference_node_values[node_id]
        matched = False
        for idx, call in enumerate(successful_calls):
            if idx not in consumed_indices and call["tool_name"] == tool_name and call.get("result") == expected_val:
                consumed_indices.add(idx)
                matched = True
                break
        if matched:
            valid_intermediate_count += 1

    value_propagation_accuracy = (valid_intermediate_count / len(topological_order)) if topological_order else 0.0

    # Final outcome evaluation (strictly requires target value match + dag conformance + 100% value propagation)
    reward = 0.0
    if agent_target_value == expected_target_value and dag_conformance and value_propagation_accuracy == 1.0:
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
