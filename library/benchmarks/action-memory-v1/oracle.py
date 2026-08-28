"""Oracle solver interacting via MCP protocol for action-memory-v1."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from runtime import MCPClient


def solve_via_mcp(mcp_url: str = "http://mcp-server:8080/mcp", target_entity: str | None = None) -> dict[str, Any]:
    client = MCPClient(mcp_url)
    
    # Initialize session
    client.initialize()
    tools = client.list_tools()
    
    # Fetch list of context chunk IDs
    res = client.call_tool("list_context_chunks", {})
    text_content = res["content"][0]["text"]
    chunk_ids = json.loads(text_content)["chunk_ids"]
    
    current_value = None
    resolved_entity = target_entity
    resolved_attr = "routing_key"

    # Read each chunk and detect state transitions
    for cid in chunk_ids:
        chunk_res = client.call_tool("get_context_chunk", {"chunk_id": cid})
        chunk_text = chunk_res["content"][0]["text"]
        
        # If target_entity is not provided, detect from first init log
        if resolved_entity is None:
            m_ent = re.search(r"for\s+(entity_\d+)", chunk_text)
            if m_ent:
                resolved_entity = m_ent.group(1)
        
        if resolved_entity and resolved_entity in chunk_text:
            m_val = re.search(r"'(?P<val>[^']+)'", chunk_text)
            if m_val:
                current_value = m_val.group("val")

    if not resolved_entity or not current_value:
        raise RuntimeError(f"Could not resolve entity/value: {resolved_entity=}, {current_value=}")

    # Call final mutation
    mutation_res = client.call_tool("execute_mutation", {
        "entity_id": resolved_entity,
        "attribute": resolved_attr,
        "bound_value": current_value,
    })
    return mutation_res


def solve_direct(task_dir: Path, evidence_dir: Path) -> dict[str, Any]:
    """Offline solve directly generating evidence for fast control checks."""
    scenario_file = task_dir / "scenario.json"
    if not scenario_file.exists() and (task_dir / "task_state" / "scenario.json").exists():
        scenario_file = task_dir / "task_state" / "scenario.json"
    scenario = json.loads(scenario_file.read_text(encoding="utf-8"))

    target_entity = scenario["target_entity"]
    target_attribute = scenario["target_attribute"]
    latest_val = scenario["latest_value"]

    evidence_events = []
    for idx, chunk in enumerate(scenario["chunks"], start=1):
        evidence_events.append({
            "event_index": idx,
            "event_type": "read_chunk",
            "payload": {"chunk_id": chunk["chunk_id"], "byte_count": chunk["byte_count"]}
        })

    evidence_events.append({
        "event_index": len(evidence_events) + 1,
        "event_type": "execute_mutation",
        "payload": {
            "entity_id": target_entity,
            "attribute": target_attribute,
            "bound_value": latest_val,
        }
    })

    evidence_dir.mkdir(parents=True, exist_ok=True)
    with (evidence_dir / "benchmark-events.jsonl").open("w", encoding="utf-8") as f:
        for ev in evidence_events:
            f.write(json.dumps(ev, sort_keys=True) + "\n")

    final_state = {
        "status": "executed",
        "target_entity": target_entity,
        "target_attribute": target_attribute,
        "bound_value": latest_val,
    }
    with (evidence_dir / "final-state.json").open("w", encoding="utf-8") as f:
        json.dump(final_state, f, indent=2, sort_keys=True)

    return final_state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-url", type=str, default=os.getenv("MCP_SERVER_URL", "http://mcp-server:8080/mcp"))
    parser.add_argument("--task-dir", type=Path, default=None)
    parser.add_argument("--evidence-dir", type=Path, default=None)
    parser.add_argument("--target-entity", type=str, default=None)
    args = parser.parse_args()

    if args.task_dir and args.evidence_dir:
        solve_direct(args.task_dir, args.evidence_dir)
    else:
        solve_via_mcp(args.mcp_url, args.target_entity)
