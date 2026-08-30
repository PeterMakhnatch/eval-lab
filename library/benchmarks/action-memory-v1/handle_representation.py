"""E0b handle-representation intervention: deterministic matched axis over chunk reference modes.

Materializes matched Action Memory cells that differ ONLY in how context chunk
references (handles) are represented to the agent. Content, dose, target, arm,
seed, and the required read set are held byte-identical across representations;
only the handle encoding and the range/batch retrieval surface differ.

Representations:
- ``opaque``     : non-semantic hashed handles (``ctx_<sha256>``); single-handle retrieval.
- ``indexed``    : positional index handles (``chunk_<ordinal>``); single-handle retrieval.
- ``range_batch``: positional index handles plus a ``get_context_chunks``
                   range/batch retrieval surface and a range descriptor on list.

The representation is recorded as a declared manipulation with a stable digest.
Undeclared or mixed modes are rejected fail-closed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

HANDLE_AXIS_VERSION = "am-handle-rep-v1"
HANDLE_REPRESENTATIONS = ("opaque", "indexed", "range_batch")
HANDLE_SEEDS = (42, 1337, 2026)
HANDLE_DOSE_BYTES = (4096, 16384)
HANDLE_ARMS = ("neutral_padding", "semantic_distractor")
HANDLE_PADDING_POSITION = "suffix"
HANDLE_INVERSION_COUNT = 1
HANDLE_SLOT_BYTES = 256
DECLARED_DELTA = "handle_reference_representation"
STEP_BUDGET = 3  # list_context_chunks, retrieval loop (single or range/batch), execute_mutation

# Tool surfaces per representation. ``range_batch`` adds the batch/range read tool.
REPRESENTATION_TOOL_SURFACES: dict[str, tuple[str, ...]] = {
    "opaque": ("list_context_chunks", "get_context_chunk", "execute_mutation"),
    "indexed": ("list_context_chunks", "get_context_chunk", "execute_mutation"),
    "range_batch": (
        "list_context_chunks",
        "get_context_chunk",
        "get_context_chunks",
        "execute_mutation",
    ),
}

# Canonical declared manipulation label per representation (for digest stability).
REPRESENTATION_DELTAS: dict[str, str] = {
    "opaque": "opaque_reference_handles",
    "indexed": "indexed_reference_handles",
    "range_batch": "range_batch_reference_surface",
}

# Field names that are permitted to differ between matched representation twins.
# Everything else (content, dose, target, arm, seed, required read set) is frozen.
_REPRESENTATION_VARYING_FIELDS = frozenset({"cell_id", "representation", "chunk_id"})


def _state_module():
    mod_name = "action_memory_state_module"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / "action_memory_state.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _utf8_exact(text: str, size: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) == size:
        return text
    if len(raw) > size:
        trimmed = raw[:size]
        while trimmed:
            try:
                decoded = trimmed.decode("utf-8")
                if len(decoded.encode("utf-8")) == size:
                    return decoded
                trimmed = trimmed[:-1]
            except UnicodeDecodeError:
                trimmed = trimmed[:-1]
        return "a" * size
    return text + ("." * (size - len(raw)))


def normalize_representation(representation: str) -> str:
    """Validate a declared representation; reject undeclared or mixed modes."""
    if not isinstance(representation, str) or representation not in HANDLE_REPRESENTATIONS:
        allowed = ", ".join(HANDLE_REPRESENTATIONS)
        raise ValueError(f"undeclared handle representation {representation!r}; allowed: {allowed}")
    return representation


def representation_digest(representation: str) -> str:
    """Return a stable digest over the declared representation manipulation."""
    normalize_representation(representation)
    canonical = json.dumps(
        {
            "axis_version": HANDLE_AXIS_VERSION,
            "representation": representation,
            "declared_delta": DECLARED_DELTA,
            "delta": REPRESENTATION_DELTAS[representation],
            "tool_surface": list(REPRESENTATION_TOOL_SURFACES[representation]),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def base_task_pair_id(seed: int, dose_bytes: int, arm: str) -> str:
    """Identity shared by all representations of the same (seed, dose, arm) scenario."""
    return f"{HANDLE_AXIS_VERSION}-s{seed}-d{dose_bytes}-{arm}"


def cell_id_for(representation: str, seed: int, dose_bytes: int, arm: str) -> str:
    normalize_representation(representation)
    return f"rep-{representation}-{arm.replace('_', '-')}-{dose_bytes}-s{seed}"


def handle_for(representation: str, pair_id: str, ordinal: int) -> str:
    """Deterministic reference handle for chunk ``ordinal`` under ``representation``."""
    normalize_representation(representation)
    if representation == "opaque":
        digest = hashlib.sha256(f"{pair_id}:opaque-handle:{ordinal}".encode("utf-8")).hexdigest()
        return f"ctx_{digest[:24]}"
    return f"chunk_{ordinal:03d}"


def range_descriptor(chunk_count: int) -> dict[str, Any]:
    """Return the canonical contiguous range covering ``chunk_count`` chunks."""
    return {"start": 0, "end": chunk_count - 1, "unit": "chunk"}


def _generate_canonical_scenario(seed: int, dose_bytes: int, arm: str) -> Any:
    """Deterministic content generation independent of representation.

    Content, dose, target, and required read set are keyed only by (seed, dose, arm),
    so every representation of the same base yields byte-identical content.
    """
    state = _state_module()
    if arm not in HANDLE_ARMS:
        raise ValueError(f"unsupported handle-representation arm: {arm}")
    if dose_bytes not in HANDLE_DOSE_BYTES:
        raise ValueError(f"unsupported handle-representation dose: {dose_bytes}")

    pair_id = base_task_pair_id(seed, dose_bytes, arm)
    identity_rng = random.Random(
        f"action_memory_handle_rep:{HANDLE_AXIS_VERSION}:{seed}:{dose_bytes}:{arm}"
    )
    target_entity = f"entity_{identity_rng.randint(100, 999)}"
    target_attribute = "routing_key"
    val_prefix = hashlib.sha256(
        f"{HANDLE_AXIS_VERSION}:{seed}:{dose_bytes}:{arm}:{target_entity}".encode("utf-8")
    ).hexdigest()[:8]
    values = [f"{val_prefix}_v{i}" for i in range(1, HANDLE_INVERSION_COUNT + 2)]
    initial_value = values[0]
    latest_value = values[-1]
    inversion_steps = values[1:]

    init_text = (
        f"[SYSTEM LOG 001]: Initializing configuration record for {target_entity}.\n"
        f"Entity attribute {target_attribute} is permanently bound to '{initial_value}'.\n"
        "All subsequent telemetry routing must reference this active token unless explicitly updated.\n"
    )
    inv_text = (
        f"[CRITICAL STATE INVERSION 091]: Configuration OVERRIDE event for {target_entity}.\n"
        f"Superceding prior assignments, attribute {target_attribute} is now mutated to '{latest_value}'.\n"
        f"Mandatory action execution REQUIREMENT: Final mutation must supply latest bound token '{latest_value}'.\n"
    )
    identity_chunks = [
        {
            "chunk_id": handle_for("indexed", pair_id, 0),
            "content": init_text,
            "chunk_type": "initial_fact",
            "byte_count": len(init_text.encode("utf-8")),
        },
        {
            "chunk_id": handle_for("indexed", pair_id, 1),
            "content": inv_text,
            "chunk_type": "inversion_fact",
            "byte_count": len(inv_text.encode("utf-8")),
        },
    ]
    needed = max(0, dose_bytes - sum(chunk["byte_count"] for chunk in identity_chunks))
    sizes = [HANDLE_SLOT_BYTES] * (needed // HANDLE_SLOT_BYTES)
    if needed % HANDLE_SLOT_BYTES:
        sizes.append(needed % HANDLE_SLOT_BYTES)

    fill_rng = random.Random(f"{pair_id}:fill")
    fill_chunks: list[dict[str, Any]] = []
    for i, size in enumerate(sizes):
        if arm == "semantic_distractor":
            distractor_entity = f"entity_{1000 + fill_rng.randint(0, 8999)}"
            distractor_value = f"distractor_val_{i}_{fill_rng.randint(10000, 99999)}"
            raw = (
                f"[SYSTEM LOG {100 + i:03d}]: Configuration heartbeat for {distractor_entity}. "
                f"Active routing token for {distractor_entity}.{target_attribute} is confirmed as '{distractor_value}'. "
                f"Verify downstream consumers propagate {distractor_value} for cluster partition #{i}."
            )
            chunk_type = "distractor"
        else:
            fill_rng.randint(0, 8999)
            fill_rng.randint(10000, 99999)
            raw = f"[DIAGNOSTIC NOISE {i:04d}] "
            chunk_type = "padding"
        content = _utf8_exact(raw, size)
        encoded = content.encode("utf-8")
        if len(encoded) != size:
            content = ("a" * (size - 1)) + "\n"
            encoded = content.encode("utf-8")
        fill_chunks.append(
            {
                "chunk_id": handle_for("indexed", pair_id, i + len(identity_chunks)),
                "content": content,
                "chunk_type": chunk_type,
                "byte_count": len(encoded),
            }
        )

    all_chunks = identity_chunks + fill_chunks
    realized = sum(chunk["byte_count"] for chunk in all_chunks)
    if realized != dose_bytes:
        raise AssertionError(
            f"handle-representation dose mismatch pair={pair_id} arm={arm}: {realized} != {dose_bytes}"
        )

    return state.ScenarioSpec(
        seed=seed,
        cell_id=cell_id_for("indexed", seed, dose_bytes, arm),
        arm=arm,
        target_entity=target_entity,
        target_attribute=target_attribute,
        initial_value=initial_value,
        latest_value=latest_value,
        inversion_steps=inversion_steps,
        inversion_count=HANDLE_INVERSION_COUNT,
        update_opportunity_count=HANDLE_INVERSION_COUNT,
        read_opportunity_count=len(all_chunks),
        mutation_opportunity_count=1,
        dose_bytes=realized,
        padding_position=HANDLE_PADDING_POSITION,
        chunks=all_chunks,
        expected_mutation_call={
            "action": "execute_mutation",
            "parameters": {
                "entity_id": target_entity,
                "attribute": target_attribute,
                "bound_value": latest_value,
            },
        },
    )


def generate_matched_handle_arm(
    seed: int, dose_bytes: int, arm: str, representation: str
) -> Any:
    """Return a matched handle-representation twin of the canonical scenario.

    Only the handle encoding (and, for ``range_batch``, the retrieval surface)
    differs from the canonical base; content, dose, target, arm, seed, and the
    required read set are identical.
    """
    normalize_representation(representation)
    pair_id = base_task_pair_id(seed, dose_bytes, arm)
    base = _generate_canonical_scenario(seed, dose_bytes, arm)
    chunks = [
        dict(chunk, chunk_id=handle_for(representation, pair_id, i))
        for i, chunk in enumerate(base.chunks)
    ]
    realized = sum(chunk["byte_count"] for chunk in chunks)
    if realized != base.dose_bytes:
        raise AssertionError(
            f"representation re-encode changed dose pair={pair_id} rep={representation}: "
            f"{realized} != {base.dose_bytes}"
        )
    return base.__class__(
        seed=base.seed,
        cell_id=cell_id_for(representation, seed, dose_bytes, arm),
        arm=base.arm,
        target_entity=base.target_entity,
        target_attribute=base.target_attribute,
        initial_value=base.initial_value,
        latest_value=base.latest_value,
        inversion_steps=list(base.inversion_steps),
        inversion_count=base.inversion_count,
        update_opportunity_count=base.update_opportunity_count,
        read_opportunity_count=base.read_opportunity_count,
        mutation_opportunity_count=base.mutation_opportunity_count,
        dose_bytes=base.dose_bytes,
        padding_position=base.padding_position,
        chunks=chunks,
        expected_mutation_call=dict(base.expected_mutation_call),
    )


def _content_digest(chunks: list[dict[str, Any]]) -> str:
    """Stable digest over the ordered chunk content set (independent of handles)."""
    joined = b"".join(str(chunk["content"]).encode("utf-8") for chunk in chunks)
    return "sha256:" + hashlib.sha256(joined).hexdigest()


def enumerate_handle_rep_cells() -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for seed in HANDLE_SEEDS:
        for dose in HANDLE_DOSE_BYTES:
            for arm in HANDLE_ARMS:
                pair_id = base_task_pair_id(seed, dose, arm)
                canonical = _generate_canonical_scenario(seed, dose, arm)
                base_content_digest = _content_digest(canonical.chunks)
                base_required_reads = len(canonical.chunks)
                for representation in HANDLE_REPRESENTATIONS:
                    spec = generate_matched_handle_arm(seed, dose, arm, representation)
                    cells.append(
                        {
                            "cell_id": spec.cell_id,
                            "representation": representation,
                            "representation_digest": representation_digest(representation),
                            "declared_delta": DECLARED_DELTA,
                            "handle_axis_version": HANDLE_AXIS_VERSION,
                            "base_task_pair_id": pair_id,
                            "dose_bytes": dose,
                            "arm": arm,
                            "seed": seed,
                            "padding_position": HANDLE_PADDING_POSITION,
                            "inversion_count": HANDLE_INVERSION_COUNT,
                            "step_budget": STEP_BUDGET,
                            "tool_schema": list(REPRESENTATION_TOOL_SURFACES[representation]),
                            "content_digest": _content_digest(spec.chunks),
                            "required_read_set_size": len(spec.chunks),
                            "update_opportunity_count": spec.update_opportunity_count,
                            "read_opportunity_count": spec.read_opportunity_count,
                            "mutation_opportunity_count": spec.mutation_opportunity_count,
                        }
                    )
                    assert spec.dose_bytes == dose
                    assert _content_digest(spec.chunks) == base_content_digest
                    assert len(spec.chunks) == base_required_reads
    return cells


def write_contract(path: Path | None = None) -> dict[str, Any]:
    cells = enumerate_handle_rep_cells()
    contract = {
        "benchmark_family": "action-memory-v1",
        "version": "1.1.0",
        "handle_axis_version": HANDLE_AXIS_VERSION,
        "construct": "actionable_entity_memory_and_value_binding",
        "declared_delta": DECLARED_DELTA,
        "padding_position": HANDLE_PADDING_POSITION,
        "inversion_count": HANDLE_INVERSION_COUNT,
        "step_budget": STEP_BUDGET,
        "representations": list(HANDLE_REPRESENTATIONS),
        "representation_digests": {
            rep: representation_digest(rep) for rep in HANDLE_REPRESENTATIONS
        },
        "representation_deltas": dict(REPRESENTATION_DELTAS),
        "tool_surfaces": {rep: list(surface) for rep, surface in REPRESENTATION_TOOL_SURFACES.items()},
        "doses_bytes": list(HANDLE_DOSE_BYTES),
        "seeds": list(HANDLE_SEEDS),
        "arms": list(HANDLE_ARMS),
        "cells": cells,
        "artifact_paths": {
            "benchmark_events": "/app/output/benchmark-events.jsonl",
            "final_state": "/app/output/final-state.json",
            "verifier_reward": "/logs/verifier/reward.txt",
        },
    }
    target = path or (ROOT / "handle_representation_contract.json")
    target.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return contract


if __name__ == "__main__":
    print(json.dumps(write_contract(), indent=2, sort_keys=True))
