"""Oracle solver, NOP control, and mutant controls for mcp-funcdag-v1."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from runtime import MCPRuntime


def run_oracle_solve(runtime: MCPRuntime, spec_data: dict[str, Any], workspace: Path) -> Any:
    """Executes the exact topological DAG sequence with required intermediate value propagation."""
    nodes = {n["node_id"]: n for n in spec_data["nodes"]}
    initial_inputs = spec_data["initial_inputs"]
    node_values: dict[str, Any] = dict(initial_inputs)
    topological_order = spec_data["topological_order"]

    for node_id in topological_order:
        node = nodes[node_id]
        tool_name = node["tool_name"]
        args = {}
        for param_name, src_id in node["input_bindings"].items():
            args[param_name] = node_values[src_id]
        
        call_out = runtime.call_tool(tool_name, args)
        if "error" in call_out:
            raise RuntimeError(f"Oracle tool call failed for {tool_name}: {call_out['error']}")
        val = call_out["result"]["value"]
        node_values[node_id] = val

    target_val = node_values[spec_data["target_node_id"]]
    workspace.mkdir(parents=True, exist_ok=True)
    ans_file = workspace / "result.json"
    ans_file.write_text(json.dumps({"target_value": target_val}, indent=2) + "\n", encoding="utf-8")
    return target_val


def run_nop_solve(runtime: MCPRuntime, spec_data: dict[str, Any], workspace: Path) -> None:
    """NOP does nothing."""
    workspace.mkdir(parents=True, exist_ok=True)


def run_wrong_order_mutant(runtime: MCPRuntime, spec_data: dict[str, Any], workspace: Path) -> None:
    """Calls tools in reversed topological order with uninitialized values."""
    nodes = {n["node_id"]: n for n in spec_data["nodes"]}
    initial_inputs = spec_data["initial_inputs"]
    node_values: dict[str, Any] = dict(initial_inputs)
    reversed_order = list(reversed(spec_data["topological_order"]))

    for node_id in reversed_order:
        node = nodes[node_id]
        tool_name = node["tool_name"]
        args = {}
        for param_name, src_id in node["input_bindings"].items():
            args[param_name] = node_values.get(src_id, 0)
        call_out = runtime.call_tool(tool_name, args)
        if "result" in call_out:
            node_values[node_id] = call_out["result"]["value"]

    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "result.json").write_text(json.dumps({"target_value": node_values.get(spec_data["target_node_id"], 0)}) + "\n", encoding="utf-8")


def run_wrong_value_mutant(runtime: MCPRuntime, spec_data: dict[str, Any], workspace: Path) -> None:
    """Follows topological order but corrupts inputs to tools."""
    nodes = {n["node_id"]: n for n in spec_data["nodes"]}
    initial_inputs = spec_data["initial_inputs"]
    node_values: dict[str, Any] = dict(initial_inputs)
    topological_order = spec_data["topological_order"]

    for node_id in topological_order:
        node = nodes[node_id]
        tool_name = node["tool_name"]
        args = {}
        for param_name, src_id in node["input_bindings"].items():
            args[param_name] = int(node_values[src_id]) + 999  # Corrupt value
        call_out = runtime.call_tool(tool_name, args)
        if "result" in call_out:
            node_values[node_id] = call_out["result"]["value"]

    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "result.json").write_text(json.dumps({"target_value": node_values.get(spec_data["target_node_id"], 0)}) + "\n", encoding="utf-8")


def run_distractor_trace_mutant(runtime: MCPRuntime, spec_data: dict[str, Any], workspace: Path) -> None:
    """Only calls distractor tools and outputs a dummy answer."""
    distractors = [t for t in spec_data["tools"] if t.get("is_distractor", False)]
    if not distractors:
        # Fallback to first tool if no distractors in cell
        distractors = spec_data["tools"][:1]
    for d in distractors:
        if d.get("is_distractor", False):
            runtime.call_tool(d["name"], {"input_payload": "noise_test", "flag": True})
        else:
            runtime.call_tool(d["name"], {})

    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "result.json").write_text(json.dumps({"target_value": -999999}) + "\n", encoding="utf-8")


def run_answer_only_mutant(runtime: MCPRuntime, spec_data: dict[str, Any], workspace: Path) -> None:
    """Writes the expected target value directly without executing ANY tools in the DAG."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "result.json").write_text(json.dumps({"target_value": 42}) + "\n", encoding="utf-8")


def get_mutants() -> dict[str, Callable[[MCPRuntime, dict[str, Any], Path], None]]:
    return {
        "wrong_order": run_wrong_order_mutant,
        "wrong_value": run_wrong_value_mutant,
        "distractor_trace": run_distractor_trace_mutant,
        "answer_only": run_answer_only_mutant,
    }
