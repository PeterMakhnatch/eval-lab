"""Deterministic state generator for action-memory-v1 benchmark."""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class EntityFact:
    entity_id: str
    attribute: str
    value: str
    version: int


@dataclass(frozen=True)
class ContextChunk:
    chunk_id: str
    content: str
    chunk_type: str  # 'initial_fact', 'padding', 'distractor', 'inversion_fact'
    byte_count: int


@dataclass(frozen=True)
class ScenarioSpec:
    seed: int
    cell_id: str
    arm: str
    target_entity: str
    target_attribute: str
    initial_value: str
    latest_value: str
    inversion_steps: list[str]
    inversion_count: int
    update_opportunity_count: int
    read_opportunity_count: int
    mutation_opportunity_count: int
    dose_bytes: int
    padding_position: str | None
    chunks: list[dict[str, Any]]
    expected_mutation_call: dict[str, Any]


def generate_scenario(
    seed: int = 42,
    cell_id: str = "clean-baseline-4k",
    arm: str = "clean",
    dose_bytes: int = 4096,
    inversion_count: int = 1,
    padding_position: str | None = None,
    distractor_count: int = 4,
) -> ScenarioSpec:
    rng = random.Random(f"action_memory:{seed}:{cell_id}:{arm}:{dose_bytes}")

    target_entity = f"entity_{rng.randint(100, 999)}"
    target_attribute = "routing_key"
    val_prefix = hashlib.sha256(f"{seed}:{target_entity}".encode("utf-8")).hexdigest()[:8]
    
    values = [f"{val_prefix}_v{i}" for i in range(1, inversion_count + 2)]
    initial_value = values[0]
    latest_value = values[-1]
    inversion_steps = values[1:]

    chunks: list[dict[str, Any]] = []

    # Initial target fact
    c0_text = (
        f"[SYSTEM LOG 001]: Initializing configuration record for {target_entity}.\n"
        f"Entity attribute {target_attribute} is permanently bound to '{initial_value}'.\n"
        f"All subsequent telemetry routing must reference this active token unless explicitly updated.\n"
    )
    chunks.append({
        "chunk_id": "chunk_000_init",
        "content": c0_text,
        "chunk_type": "initial_fact",
        "byte_count": len(c0_text.encode("utf-8")),
    })

    # Inversion facts (updates to latest_value)
    inversion_chunks: list[dict[str, Any]] = []
    for step_i, step_val in enumerate(inversion_steps, start=1):
        inv_text = (
            f"[CRITICAL STATE INVERSION 09{step_i}]: Configuration OVERRIDE event for {target_entity}.\n"
            f"Superceding prior assignments, attribute {target_attribute} is now mutated to '{step_val}'.\n"
            f"Mandatory action execution REQUIREMENT: Final mutation must supply latest bound token '{step_val}'.\n"
        )
        inversion_chunks.append({
            "chunk_id": f"chunk_inv_{step_i:03d}",
            "content": inv_text,
            "chunk_type": "inversion_fact",
            "byte_count": len(inv_text.encode("utf-8")),
        })

    # Distractors
    distractor_chunks: list[dict[str, Any]] = []
    if arm == "semantic_distractor":
        for d in range(distractor_count):
            d_ent = f"entity_{rng.randint(1000, 9999)}"
            d_val = f"distractor_val_{d}_{rng.randint(10000, 99999)}"
            d_text = (
                f"[SYSTEM LOG {100+d:03d}]: Configuration heartbeat for {d_ent}.\n"
                f"Active routing token for {d_ent}.{target_attribute} is confirmed as '{d_val}'.\n"
                f"Verify downstream consumers propagate {d_val} for cluster partition #{d}.\n"
            )
            distractor_chunks.append({
                "chunk_id": f"chunk_distractor_{d:03d}",
                "content": d_text,
                "chunk_type": "distractor",
                "byte_count": len(d_text.encode("utf-8")),
            })

    # Padding to reach exact target dose_bytes
    current_bytes = (
        sum(c["byte_count"] for c in chunks)
        + sum(c["byte_count"] for c in inversion_chunks)
        + sum(c["byte_count"] for c in distractor_chunks)
    )
    needed_bytes = max(0, dose_bytes - current_bytes)

    padding_chunks: list[dict[str, Any]] = []
    p_idx = 0
    while needed_bytes > 0:
        p_len = min(needed_bytes, 1024)
        if p_len < 40:
            noise_core = "." * max(0, p_len - 11)
            noise = f"[PAD {p_idx:03d}] {noise_core}\n"
        else:
            noise_core = "." * (p_len - 30)
            noise = f"[DIAGNOSTIC NOISE {p_idx:04d}] {noise_core}\n"
        
        b_noise = noise.encode("utf-8")
        if len(b_noise) > needed_bytes:
            b_noise = b_noise[:needed_bytes]
            noise = b_noise.decode("utf-8", errors="ignore")
            b_noise = noise.encode("utf-8")

        b_len = len(b_noise)
        if b_len == 0:
            break

        padding_chunks.append({
            "chunk_id": f"chunk_pad_{p_idx:03d}",
            "content": noise,
            "chunk_type": "padding",
            "byte_count": b_len,
        })
        needed_bytes -= b_len
        p_idx += 1

    # Exact single-byte finish if any rounding occurred
    actual_current = (
        sum(c["byte_count"] for c in chunks)
        + sum(c["byte_count"] for c in inversion_chunks)
        + sum(c["byte_count"] for c in distractor_chunks)
        + sum(c["byte_count"] for c in padding_chunks)
    )
    if actual_current < dose_bytes:
        diff = dose_bytes - actual_current
        filler = ("#" * (diff - 1)) + "\n" if diff > 1 else "\n"
        padding_chunks.append({
            "chunk_id": f"chunk_pad_exact_{p_idx:03d}",
            "content": filler,
            "chunk_type": "padding",
            "byte_count": len(filler.encode("utf-8")),
        })

    # Assemble chunks based on arm and padding_position
    if padding_position == "prefix":
        all_chunks = padding_chunks + chunks + distractor_chunks + inversion_chunks
    elif padding_position == "middle":
        half = len(distractor_chunks) // 2
        all_chunks = chunks + distractor_chunks[:half] + padding_chunks + distractor_chunks[half:] + inversion_chunks
    else:
        all_chunks = chunks + distractor_chunks + inversion_chunks + padding_chunks

    realized_dose = sum(c["byte_count"] for c in all_chunks)

    expected_mutation = {
        "action": "execute_mutation",
        "parameters": {
            "entity_id": target_entity,
            "attribute": target_attribute,
            "bound_value": latest_value,
        }
    }

    return ScenarioSpec(
        seed=seed,
        cell_id=cell_id,
        arm=arm,
        target_entity=target_entity,
        target_attribute=target_attribute,
        initial_value=initial_value,
        latest_value=latest_value,
        inversion_steps=inversion_steps,
        inversion_count=inversion_count,
        update_opportunity_count=inversion_count,
        read_opportunity_count=len(all_chunks),
        mutation_opportunity_count=1,
        dose_bytes=realized_dose,
        padding_position=padding_position,
        chunks=all_chunks,
        expected_mutation_call=expected_mutation,
    )
