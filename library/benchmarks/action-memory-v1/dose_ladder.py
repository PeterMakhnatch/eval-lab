"""Versioned Action Memory dose ladder: matched neutral vs semantic arms.

Does not mutate merged v1 cell identities. Shared identity is keyed by
dose_axis_version + seed + dose only. The declared single delta is fill-chunk
semantics at identical agent-visible serialized byte counts, with opaque shared
chunk handles, inversion, padding position, and tool schema.
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

DOSE_AXIS_VERSION = "am-dose-ladder-v1"
DOSE_LADDER_BYTES = (4096, 16384, 65536, 131072)
DOSE_LADDER_SEEDS = (42, 1337, 2026)
DOSE_LADDER_ARMS = ("neutral_padding", "semantic_distractor")
DOSE_LADDER_PADDING_POSITION = "suffix"
DOSE_LADDER_INVERSION_COUNT = 1
DOSE_LADDER_SLOT_BYTES = 256
DECLARED_DELTA = "fill_chunk_semantics"
STEP_BUDGET = 3  # list_context_chunks, get_context_chunk loop, execute_mutation


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


def base_task_pair_id(seed: int, dose_bytes: int) -> str:
    return f"{DOSE_AXIS_VERSION}-s{seed}-d{dose_bytes}"


def cell_id_for(arm: str, dose_bytes: int, seed: int) -> str:
    return f"dl-{arm.replace('_', '-')}-{dose_bytes}-s{seed}"


def _opaque_chunk_id(pair_id: str, ordinal: int) -> str:
    """Return a stable, non-semantic handle shared by both matched arms."""
    digest = hashlib.sha256(f"{pair_id}:opaque-handle:{ordinal}".encode("utf-8")).hexdigest()
    return f"ctx_{digest[:24]}"


def generate_matched_dose_arm(seed: int, dose_bytes: int, arm: str) -> Any:
    if arm not in DOSE_LADDER_ARMS:
        raise ValueError(f"unsupported dose-ladder arm: {arm}")
    if dose_bytes not in DOSE_LADDER_BYTES:
        raise ValueError(f"unsupported dose-ladder dose: {dose_bytes}")

    state = _state_module()
    pair_id = base_task_pair_id(seed, dose_bytes)
    cell_id = cell_id_for(arm, dose_bytes, seed)
    identity_rng = random.Random(f"action_memory_dose_ladder:{DOSE_AXIS_VERSION}:{seed}:{dose_bytes}")

    target_entity = f"entity_{identity_rng.randint(100, 999)}"
    target_attribute = "routing_key"
    val_prefix = hashlib.sha256(
        f"{DOSE_AXIS_VERSION}:{seed}:{dose_bytes}:{target_entity}".encode("utf-8")
    ).hexdigest()[:8]
    values = [f"{val_prefix}_v{i}" for i in range(1, DOSE_LADDER_INVERSION_COUNT + 2)]
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
            "chunk_id": _opaque_chunk_id(pair_id, 0),
            "content": init_text,
            "chunk_type": "initial_fact",
            "byte_count": len(init_text.encode("utf-8")),
        },
        {
            "chunk_id": _opaque_chunk_id(pair_id, 1),
            "content": inv_text,
            "chunk_type": "inversion_fact",
            "byte_count": len(inv_text.encode("utf-8")),
        },
    ]
    needed = max(0, dose_bytes - sum(chunk["byte_count"] for chunk in identity_chunks))
    sizes = [DOSE_LADDER_SLOT_BYTES] * (needed // DOSE_LADDER_SLOT_BYTES)
    if needed % DOSE_LADDER_SLOT_BYTES:
        sizes.append(needed % DOSE_LADDER_SLOT_BYTES)

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
                "chunk_id": _opaque_chunk_id(pair_id, i + len(identity_chunks)),
                "content": content,
                "chunk_type": chunk_type,
                "byte_count": len(encoded),
            }
        )

    all_chunks = identity_chunks + fill_chunks
    realized = sum(chunk["byte_count"] for chunk in all_chunks)
    if realized != dose_bytes:
        raise AssertionError(f"dose mismatch pair={pair_id} arm={arm}: {realized} != {dose_bytes}")

    return state.ScenarioSpec(
        seed=seed,
        cell_id=cell_id,
        arm=arm,
        target_entity=target_entity,
        target_attribute=target_attribute,
        initial_value=initial_value,
        latest_value=latest_value,
        inversion_steps=inversion_steps,
        inversion_count=DOSE_LADDER_INVERSION_COUNT,
        update_opportunity_count=DOSE_LADDER_INVERSION_COUNT,
        read_opportunity_count=len(all_chunks),
        mutation_opportunity_count=1,
        dose_bytes=realized,
        padding_position=DOSE_LADDER_PADDING_POSITION,
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


def enumerate_dose_ladder_cells() -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for seed in DOSE_LADDER_SEEDS:
        for dose in DOSE_LADDER_BYTES:
            pair_id = base_task_pair_id(seed, dose)
            for arm in DOSE_LADDER_ARMS:
                spec = generate_matched_dose_arm(seed=seed, dose_bytes=dose, arm=arm)
                cells.append(
                    {
                        "cell_id": spec.cell_id,
                        "dose_bytes": dose,
                        "arm": arm,
                        "seed": seed,
                        "base_task_pair_id": pair_id,
                        "padding_position": DOSE_LADDER_PADDING_POSITION,
                        "inversion_count": DOSE_LADDER_INVERSION_COUNT,
                        "declared_delta": DECLARED_DELTA,
                        "dose_axis_version": DOSE_AXIS_VERSION,
                        "step_budget": STEP_BUDGET,
                        "update_opportunity_count": spec.update_opportunity_count,
                        "read_opportunity_count": spec.read_opportunity_count,
                        "mutation_opportunity_count": spec.mutation_opportunity_count,
                    }
                )
    return cells


def write_contract(path: Path | None = None) -> dict[str, Any]:
    cells = enumerate_dose_ladder_cells()
    contract = {
        "benchmark_family": "action-memory-v1",
        "version": "1.1.0",
        "dose_axis_version": DOSE_AXIS_VERSION,
        "construct": "actionable_entity_memory_and_value_binding",
        "declared_delta": DECLARED_DELTA,
        "padding_position": DOSE_LADDER_PADDING_POSITION,
        "inversion_count": DOSE_LADDER_INVERSION_COUNT,
        "step_budget": STEP_BUDGET,
        "tool_schema": ["list_context_chunks", "get_context_chunk", "execute_mutation"],
        "doses_bytes": list(DOSE_LADDER_BYTES),
        "seeds": list(DOSE_LADDER_SEEDS),
        "arms": list(DOSE_LADDER_ARMS),
        "cells": cells,
        "artifact_paths": {
            "benchmark_events": "/app/output/benchmark-events.jsonl",
            "final_state": "/app/output/final-state.json",
            "verifier_reward": "/logs/verifier/reward.txt",
        },
    }
    target = path or (ROOT / "dose_ladder_contract.json")
    target.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return contract
