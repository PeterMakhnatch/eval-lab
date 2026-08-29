"""Oracle solver interacting via MCP protocol for action-memory-v1."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


def _get_client():
    mod_name = "action_memory_client_module"
    if mod_name in sys.modules:
        return sys.modules[mod_name].McpHttpSession
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / "client.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod.McpHttpSession


def solve_via_mcp(
    mcp_url: str = "http://mcp-service:8080/mcp", target_entity: str | None = None
) -> dict[str, Any]:
    client_cls = _get_client()
    client = client_cls(url=mcp_url)
    status, _raw = client.initialize()
    if status != 200:
        raise RuntimeError(f"mcp initialize failed: {status}")
    listed = client.call_tool("list_context_chunks", {})
    chunk_ids = listed["chunk_ids"] if isinstance(listed, dict) else []
    current_value = None
    resolved_entity = target_entity
    resolved_attr = "routing_key"
    for chunk_id in chunk_ids:
        chunk = client.call_tool("get_context_chunk", {"chunk_id": chunk_id})
        chunk_text = json.dumps(chunk) if isinstance(chunk, dict) else str(chunk)
        if resolved_entity is None:
            entity_match = re.search(r"for\s+(entity_\d+)", chunk_text)
            if entity_match:
                resolved_entity = entity_match.group(1)
        if resolved_entity and re.search(rf"\b{re.escape(resolved_entity)}\b", chunk_text):
            value_match = re.search(r"'(?P<val>[^']+)'", chunk_text)
            if value_match:
                current_value = value_match.group("val")
    if not resolved_entity or not current_value:
        raise RuntimeError(f"Could not resolve entity/value: {resolved_entity=}, {current_value=}")
    return client.call_tool(
        "execute_mutation",
        {
            "entity_id": resolved_entity,
            "attribute": resolved_attr,
            "bound_value": current_value,
        },
    )


def _event(ordinal: int, tool_name: str, arguments: dict[str, Any], result: Any) -> dict[str, Any]:
    """Mirror the generated FastMCP state-journal format for offline controls."""
    return {
        "schema_version": "mcp-tool-event-v1",
        "event_ordinal": ordinal,
        "event_type": "tool_call_success",
        "tool_name": tool_name,
        "arguments": arguments,
        "result": {"status": "ok", "value": result},
        "is_error": False,
        "is_distractor": False,
    }


def solve_direct(task_dir: Path, evidence_dir: Path) -> dict[str, Any]:
    """Offline control that emits the same evidence shape as the FastMCP sidecar."""
    scenario_file = task_dir / "scenario.json"
    if not scenario_file.exists() and (task_dir / "task_state" / "scenario.json").exists():
        scenario_file = task_dir / "task_state" / "scenario.json"
    scenario = json.loads(scenario_file.read_text(encoding="utf-8"))

    target_entity = scenario["target_entity"]
    target_attribute = scenario["target_attribute"]
    latest_value = scenario["latest_value"]
    chunk_ids = [chunk["chunk_id"] for chunk in scenario["chunks"]]

    evidence_events = [_event(1, "list_context_chunks", {}, {"chunk_ids": chunk_ids})]
    for ordinal, chunk in enumerate(scenario["chunks"], start=2):
        evidence_events.append(
            _event(
                ordinal,
                "get_context_chunk",
                {"chunk_id": chunk["chunk_id"]},
                {"chunk_id": chunk["chunk_id"], "content": chunk["content"]},
            )
        )
    final_state = {
        "status": "executed",
        "target_entity": target_entity,
        "target_attribute": target_attribute,
        "bound_value": latest_value,
    }
    evidence_events.append(
        _event(
            len(evidence_events) + 1,
            "execute_mutation",
            {
                "entity_id": target_entity,
                "attribute": target_attribute,
                "bound_value": latest_value,
            },
            final_state,
        )
    )

    evidence_dir.mkdir(parents=True, exist_ok=True)
    with (evidence_dir / "benchmark-events.jsonl").open("w", encoding="utf-8") as file:
        for event in evidence_events:
            file.write(json.dumps(event, sort_keys=True) + "\n")
    (evidence_dir / "final-state.json").write_text(
        json.dumps(final_state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return final_state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-url", type=str, default=os.getenv("MCP_SERVER_URL", "http://mcp-service:8080/mcp"))
    parser.add_argument("--task-dir", type=Path, default=None)
    parser.add_argument("--evidence-dir", type=Path, default=None)
    parser.add_argument("--target-entity", type=str, default=None)
    args = parser.parse_args()
    if args.task_dir and args.evidence_dir:
        solve_direct(args.task_dir, args.evidence_dir)
    else:
        solve_via_mcp(args.mcp_url, args.target_entity)
