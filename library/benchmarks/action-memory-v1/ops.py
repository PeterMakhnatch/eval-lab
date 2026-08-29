"""Canonical Action Memory FastMCP operation registry.

Imported by generated sidecar ``server.py`` as ``from ops import OP_REGISTRY``.
The only chunk representation exposed to the agent contains an opaque handle and
its content. Internal chunk classification and byte accounting stay private to
the scenario/verifier boundary.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCENARIO_PATH = Path(os.environ.get("AM_SCENARIO", "/app/scenario.json"))
OUTPUT_DIR = Path(os.environ.get("AM_OUTPUT", "/app/output"))


def _load_scenario() -> dict[str, Any]:
    return json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))


def agent_visible_chunk(chunk: dict[str, Any]) -> dict[str, str]:
    """Return the complete chunk representation available to an agent."""
    return {"chunk_id": str(chunk["chunk_id"]), "content": str(chunk["content"])}


def canonical_agent_chunk_bytes(chunk: dict[str, Any]) -> bytes:
    """Serialize the complete public chunk response for dose matching."""
    return json.dumps(
        agent_visible_chunk(chunk), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def list_context_chunks() -> dict[str, list[str]]:
    scenario = _load_scenario()
    return {"chunk_ids": [str(chunk["chunk_id"]) for chunk in scenario.get("chunks", [])]}


def get_context_chunk(chunk_id: str) -> dict[str, str]:
    scenario = _load_scenario()
    chunk = next((item for item in scenario.get("chunks", []) if item["chunk_id"] == chunk_id), None)
    if chunk is None:
        return {"error": "not_found", "chunk_id": chunk_id}
    return agent_visible_chunk(chunk)


def execute_mutation(entity_id: str, attribute: str, bound_value: str) -> dict[str, str]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    final_state = {
        "status": "executed",
        "target_entity": entity_id,
        "target_attribute": attribute,
        "bound_value": bound_value,
    }
    (OUTPUT_DIR / "final-state.json").write_text(
        json.dumps(final_state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return final_state


OP_REGISTRY = {
    "list_context_chunks": list_context_chunks,
    "get_context_chunk": get_context_chunk,
    "execute_mutation": execute_mutation,
}
