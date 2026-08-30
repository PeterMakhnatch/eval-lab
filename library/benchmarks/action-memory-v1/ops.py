"""Canonical Action Memory FastMCP operation registry.

Imported by generated sidecar ``server.py`` as ``from ops import OP_REGISTRY``.
The only chunk representation exposed to the agent contains the declared handle
and its content. Internal chunk classification and byte accounting stay private
to the scenario/verifier boundary.

Supports opaque, indexed, and range/batch reference modes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCENARIO_PATH = Path(os.environ.get("AM_SCENARIO", "/app/scenario.json"))
OUTPUT_DIR = Path(os.environ.get("AM_OUTPUT", "/app/output"))
HANDLE_REP_PATH = Path(os.environ.get("AM_HANDLE_REP", "/app/handle_representation.json"))


def _load_scenario() -> dict[str, Any]:
    return json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))


def _load_representation() -> str | None:
    """Resolve the declared handle representation mode if declared."""
    env_rep = os.environ.get("AM_HANDLE_REPRESENTATION", "").strip()
    if env_rep:
        return env_rep
    if HANDLE_REP_PATH.exists():
        try:
            data = json.loads(HANDLE_REP_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("representation"), str):
                return data["representation"]
        except Exception:
            pass
    return None


def agent_visible_chunk(chunk: dict[str, Any]) -> dict[str, str]:
    """Return the complete chunk representation available to an agent."""
    return {"chunk_id": str(chunk["chunk_id"]), "content": str(chunk["content"])}


def canonical_agent_chunk_bytes(chunk: dict[str, Any]) -> bytes:
    """Serialize the complete public chunk response for dose matching."""
    return json.dumps(
        agent_visible_chunk(chunk), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def list_context_chunks() -> dict[str, Any]:
    """List handles for context records and declare the active representation mode."""
    scenario = _load_scenario()
    chunks = scenario.get("chunks", [])
    rep = _load_representation()
    result: dict[str, Any] = {
        "chunk_ids": [str(chunk["chunk_id"]) for chunk in chunks],
    }
    if rep is not None:
        result["representation"] = rep
        if rep == "range_batch":
            result["range"] = {
                "start": 0,
                "end": max(0, len(chunks) - 1),
                "unit": "chunk",
            }
    return result


def get_context_chunk(chunk_id: str) -> dict[str, str]:
    """Read the content for one context handle (single-chunk mode)."""
    scenario = _load_scenario()
    chunk = next((item for item in scenario.get("chunks", []) if item["chunk_id"] == chunk_id), None)
    if chunk is None:
        return {"error": "not_found", "chunk_id": chunk_id}
    return agent_visible_chunk(chunk)


def get_context_chunks(
    chunk_ids: list[str] | None = None,
    range: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Batch/range retrieval tool available in ``range_batch`` reference mode."""
    scenario = _load_scenario()
    all_chunks = scenario.get("chunks", [])
    selected: list[dict[str, Any]] = []

    if chunk_ids is not None:
        if not isinstance(chunk_ids, list):
            return {"error": "invalid_argument", "message": "chunk_ids must be a list of strings"}
        by_id = {str(item["chunk_id"]): item for item in all_chunks}
        for cid in chunk_ids:
            if str(cid) in by_id:
                selected.append(by_id[str(cid)])
            else:
                return {"error": "not_found", "chunk_id": str(cid)}
    elif range is not None:
        if not isinstance(range, dict) or "start" not in range or "end" not in range:
            return {
                "error": "invalid_argument",
                "message": "range must be an object with 'start' and 'end' integers",
            }
        start, end = range.get("start", 0), range.get("end", 0)
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
            return {"error": "invalid_range", "start": start, "end": end}
        selected = all_chunks[start : end + 1]
    else:
        return {
            "error": "missing_argument",
            "message": "must supply either 'chunk_ids' or 'range'",
        }

    return {"chunks": [agent_visible_chunk(c) for c in selected]}


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
    "get_context_chunks": get_context_chunks,
    "execute_mutation": execute_mutation,
}
