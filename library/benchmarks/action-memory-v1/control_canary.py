"""Deterministic control canary and paired-condition certification for action-memory-v1.

NOTE: This module provides deterministic unit test fixtures and paired contrast
specifications for the action-memory state-inversion control lane. Real task
packaging, container execution, and registry admission land in follow-up lanes
once runtime profile qualification is complete.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.interpretation.benchmark_events import (
    CorrelatedToolCall,
    TrialBundle,
    load_trial_bundle,
)
from evallab.semantic_facts import PairedConditionFact


CANARY_PAIR_ID = "am-state-inversion-canary-s42-4k"
CANARY_SEED = 42
CANARY_DOSE_BYTES = 4096
CANARY_TASK_NON_INVERTED = "am-canary-non-inverted-4k-s42"
CANARY_TASK_INVERTED = "am-canary-state-inverted-4k-s42"

# Canonical FastMCP tool inventory and schema definition
CANARY_TOOL_INVENTORY = [
    {
        "name": "list_context_chunks",
        "description": "List all available context chunk identifiers in canonical order.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_context_chunk",
        "description": "Retrieve content for a specific context chunk handle.",
        "inputSchema": {
            "type": "object",
            "properties": {"chunk_id": {"type": "string"}},
            "required": ["chunk_id"],
        },
    },
    {
        "name": "execute_mutation",
        "description": "Execute final state mutation with target entity attribute binding.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "attribute": {"type": "string"},
                "bound_value": {"type": "string"},
            },
            "required": ["entity_id", "attribute", "bound_value"],
        },
    },
]


def compute_tool_inventory_digest(inventory: list[dict[str, Any]]) -> str:
    """Compute deterministic SHA-256 digest of tool schemas."""
    serialized = json.dumps(inventory, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


CANARY_TOOL_SCHEMA_DIGEST = compute_tool_inventory_digest(CANARY_TOOL_INVENTORY)


def compute_token_digest(token: str) -> str:
    """Compute canonical deterministic SHA-256 digest of an exact bound token string."""
    return f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"


def _pad_text_to_exact_bytes(text: str, target_bytes: int) -> str:
    """Pad or trim UTF-8 text to exact byte length."""
    encoded = text.encode("utf-8")
    if len(encoded) == target_bytes:
        return text
    if len(encoded) < target_bytes:
        padding = " " * (target_bytes - len(encoded))
        return text + padding
    # If longer, truncate cleanly
    trimmed = encoded[:target_bytes]
    while trimmed:
        try:
            return trimmed.decode("utf-8")
        except UnicodeDecodeError:
            trimmed = trimmed[:-1]
    return " " * target_bytes


@dataclass(frozen=True)
class CanaryPairSpec:
    """Paired contrast specification for state inversion control."""

    pair_id: str
    seed: int
    dose_bytes: int
    target_entity: str
    target_attribute: str
    initial_value: str
    inverted_value: str
    non_inverted_task_id: str
    inverted_task_id: str
    non_inverted_scenario: dict[str, Any]
    inverted_scenario: dict[str, Any]
    verifier_truth_digest: str
    tool_inventory_digest: str
    total_realized_context_bytes: int


def build_canary_pair_spec(
    seed: int = CANARY_SEED,
    dose_bytes: int = CANARY_DOSE_BYTES,
) -> CanaryPairSpec:
    """Build matched non-inverted vs state-inverted canary scenario pair with exact byte parity."""
    pair_id = CANARY_PAIR_ID
    rng = random.Random(f"action_memory_canary:{seed}:{dose_bytes}")

    target_entity = f"entity_{rng.randint(100, 999)}"
    target_attribute = "routing_key"
    val_prefix = hashlib.sha256(
        f"canary:{seed}:{dose_bytes}:{target_entity}".encode("utf-8")
    ).hexdigest()[:8]
    initial_val = f"{val_prefix}_v1"
    inverted_val = f"{val_prefix}_v2"

    init_digest = compute_token_digest(initial_val)
    inv_digest = compute_token_digest(inverted_val)

    # Chunk 0: Initial Fact (exact 256 bytes) with first-class bound token
    c0_raw = (
        f"[SYSTEM LOG 001]: Initializing configuration record for {target_entity}.\n"
        f"Entity attribute {target_attribute} is permanently bound to '{initial_val}'.\n"
        "All subsequent telemetry routing must reference this active token unless explicitly updated.\n"
    )
    c0_text = _pad_text_to_exact_bytes(c0_raw, 256)
    c0_content_digest = f"sha256:{hashlib.sha256(c0_text.encode('utf-8')).hexdigest()}"
    init_chunk = {
        "chunk_id": "ctx_000_init",
        "content": c0_text,
        "chunk_type": "initial_fact",
        "byte_count": 256,
        "entity_id": target_entity,
        "attribute": target_attribute,
        "bound_token": initial_val,
        "token_digest": init_digest,
        "content_digest": c0_content_digest,
    }

    # Chunk 1 for Arm 0: Neutral filler (exact 256 bytes, no bound token)
    c1_arm0_raw = (
        f"[SYSTEM LOG 002]: Configuration verification for {target_entity}.\n"
        f"Active routing key confirms binding to initial token '{initial_val}'.\n"
        "No state overrides registered in current deployment epoch.\n"
    )
    c1_arm0_text = _pad_text_to_exact_bytes(c1_arm0_raw, 256)
    c1_arm0_content_digest = (
        f"sha256:{hashlib.sha256(c1_arm0_text.encode('utf-8')).hexdigest()}"
    )
    chunk1_arm0 = {
        "chunk_id": "ctx_001_fill",
        "content": c1_arm0_text,
        "chunk_type": "padding",
        "byte_count": 256,
        "entity_id": None,
        "attribute": None,
        "bound_token": None,
        "token_digest": None,
        "content_digest": c1_arm0_content_digest,
    }

    # Chunk 1 for Arm 1: Critical State Inversion Override (exact 256 bytes) with first-class inverted token
    c1_arm1_raw = (
        f"[CRITICAL STATE INVERSION 091]: Configuration OVERRIDE event for {target_entity}.\n"
        f"Superceding prior assignments, attribute {target_attribute} is now mutated to '{inverted_val}'.\n"
        f"Mandatory action execution REQUIREMENT: Final mutation must supply latest bound token '{inverted_val}'.\n"
    )
    c1_arm1_text = _pad_text_to_exact_bytes(c1_arm1_raw, 256)
    c1_arm1_content_digest = (
        f"sha256:{hashlib.sha256(c1_arm1_text.encode('utf-8')).hexdigest()}"
    )
    chunk1_arm1 = {
        "chunk_id": "ctx_001_inv",
        "content": c1_arm1_text,
        "chunk_type": "inversion_fact",
        "byte_count": 256,
        "entity_id": target_entity,
        "attribute": target_attribute,
        "bound_token": inverted_val,
        "token_digest": inv_digest,
        "content_digest": c1_arm1_content_digest,
    }

    # Chunks 2-6: Padding chunks identical across both arms (4 x 716 bytes + 1 x 720 bytes = 3584 bytes)
    # Total context = 256 + 256 + 3584 = 4096 bytes exactly!
    padding_chunks: list[dict[str, Any]] = []
    pad_sizes = [716, 716, 716, 716, 720]
    for idx, size in enumerate(pad_sizes, start=2):
        pad_raw = (
            f"[SYSTEM TELEMETRY]: Partition heartbeat check passed for cluster node {idx - 2}.\n"
            f"Latency envelope: 12ms p50, 45ms p99. Network interfaces active.\n"
            f"Sequence verification token: #{idx:04d}.\n"
        )
        pad_text = _pad_text_to_exact_bytes(pad_raw, size)
        pad_content_digest = (
            f"sha256:{hashlib.sha256(pad_text.encode('utf-8')).hexdigest()}"
        )
        padding_chunks.append(
            {
                "chunk_id": f"ctx_{idx:03d}_fill",
                "content": pad_text,
                "chunk_type": "padding",
                "byte_count": size,
                "entity_id": None,
                "attribute": None,
                "bound_token": None,
                "token_digest": None,
                "content_digest": pad_content_digest,
            }
        )

    chunks_arm0 = [init_chunk, chunk1_arm0] + padding_chunks
    chunks_arm1 = [init_chunk, chunk1_arm1] + padding_chunks

    total_bytes_arm0 = sum(c["byte_count"] for c in chunks_arm0)
    total_bytes_arm1 = sum(c["byte_count"] for c in chunks_arm1)
    assert total_bytes_arm0 == total_bytes_arm1 == 4096, (
        "Realized context bytes must equal exactly 4096"
    )

    scenario_arm0 = {
        "seed": seed,
        "cell_id": "canary-non-inverted-4k",
        "arm": "clean_non_inverted",
        "target_entity": target_entity,
        "target_attribute": target_attribute,
        "initial_value": initial_val,
        "latest_value": initial_val,
        "inversion_steps": [],
        "inversion_count": 0,
        "update_opportunity_count": 0,
        "read_opportunity_count": len(chunks_arm0),
        "mutation_opportunity_count": 1,
        "dose_bytes": dose_bytes,
        "chunks": chunks_arm0,
        "expected_mutation_call": {
            "entity_id": target_entity,
            "attribute": target_attribute,
            "bound_value": initial_val,
        },
    }

    scenario_arm1 = {
        "seed": seed,
        "cell_id": "canary-state-inverted-4k",
        "arm": "state_inverted",
        "target_entity": target_entity,
        "target_attribute": target_attribute,
        "initial_value": initial_val,
        "latest_value": inverted_val,
        "inversion_steps": [inverted_val],
        "inversion_count": 1,
        "update_opportunity_count": 1,
        "read_opportunity_count": len(chunks_arm1),
        "mutation_opportunity_count": 1,
        "dose_bytes": dose_bytes,
        "chunks": chunks_arm1,
        "expected_mutation_call": {
            "entity_id": target_entity,
            "attribute": target_attribute,
            "bound_value": inverted_val,
        },
    }

    truth_bytes = json.dumps(
        {
            "pair_id": pair_id,
            "target_entity": target_entity,
            "target_attribute": target_attribute,
            "arm0_value": initial_val,
            "arm1_value": inverted_val,
        },
        sort_keys=True,
    ).encode("utf-8")
    truth_digest = f"sha256:{hashlib.sha256(truth_bytes).hexdigest()}"

    return CanaryPairSpec(
        pair_id=pair_id,
        seed=seed,
        dose_bytes=dose_bytes,
        target_entity=target_entity,
        target_attribute=target_attribute,
        initial_value=initial_val,
        inverted_value=inverted_val,
        non_inverted_task_id=CANARY_TASK_NON_INVERTED,
        inverted_task_id=CANARY_TASK_INVERTED,
        non_inverted_scenario=scenario_arm0,
        inverted_scenario=scenario_arm1,
        verifier_truth_digest=truth_digest,
        tool_inventory_digest=CANARY_TOOL_SCHEMA_DIGEST,
        total_realized_context_bytes=4096,
    )


def synthesize_canary_trial_artifacts(
    spec: CanaryPairSpec,
    arm: str,
    control_type: str = "oracle",  # "oracle", "nop", "stale_mutant"
    include_state_journal: bool = True,
) -> dict[str, Any]:
    """Synthesize deterministic trial bundle artifacts for a canary control run."""
    is_arm0 = arm in ("non_inverted", "arm0", "clean_non_inverted")
    scenario = spec.non_inverted_scenario if is_arm0 else spec.inverted_scenario
    task_id = spec.non_inverted_task_id if is_arm0 else spec.inverted_task_id
    trial_id = f"{task_id}-{control_type}"
    target_val = scenario["latest_value"]

    # 1. Benchmark Contract
    contract_data = {
        "family": "action-memory-v1",
        "version": "1.0.0",
        "construct": "actionable_entity_memory_and_value_binding",
        "seed": spec.seed,
        "task_id": task_id,
        "cell_factors": {
            "cell_id": scenario["cell_id"],
            "arm": scenario["arm"],
            "dose_bytes": spec.dose_bytes,
            "inversion_count": scenario["inversion_count"],
            "target_entity": spec.target_entity,
            "target_attribute": spec.target_attribute,
            "initial_value": spec.initial_value,
            "latest_value": target_val,
            "expected_chunk_ids": [c["chunk_id"] for c in scenario["chunks"]],
        },
        "opportunity_counts": {
            "read_opportunity_count": scenario["read_opportunity_count"],
            "mutation_opportunity_count": scenario["mutation_opportunity_count"],
            "update_opportunity_count": scenario["update_opportunity_count"],
            "raw_binding_opportunities": 1,
            "raw_conflicting_opportunities": 1 if not is_arm0 else 0,
            "memory_write_opportunities": 0,  # Zero agent writes prior to final mutation
        },
        "verifier_truth_digest": spec.verifier_truth_digest,
        "tool_inventory_digest": spec.tool_inventory_digest,
        "artifact_paths": {
            "benchmark_events": "benchmark-events.jsonl",
            "final_state": "final-state.json",
            "verifier_reward": "reward.txt",
        },
    }

    # 2. Sequential Event Stream (Canonical 1-based indices without gaps)
    events: list[dict[str, Any]] = []
    mutations: list[dict[str, Any]] = []
    task_success = False

    current_event_idx = 1

    if control_type == "oracle":
        # Oracle reads all chunks in canonical order: sequential request, result pairs
        for chunk in scenario["chunks"]:
            call_id = f"call_read_{chunk['chunk_id']}"
            events.append(
                {
                    "event_index": current_event_idx,
                    "event_type": "mcp_call",
                    "call_id": call_id,
                    "tool_name": "get_context_chunk",
                    "arguments": {"chunk_id": chunk["chunk_id"]},
                }
            )
            current_event_idx += 1
            # Result preserves full raw content separately while exposing first-class bound_token
            events.append(
                {
                    "event_index": current_event_idx,
                    "event_type": "tool_result",
                    "call_id": call_id,
                    "result": {
                        "status": "ok",
                        "chunk_id": chunk["chunk_id"],
                        "content": chunk[
                            "content"
                        ],  # Full raw read payload preserved separately
                        "content_digest": chunk["content_digest"],
                        "entity_id": chunk.get("entity_id"),
                        "attribute": chunk.get("attribute"),
                        "bound_token": chunk.get(
                            "bound_token"
                        ),  # First-class bound token identity!
                        "token_digest": chunk.get("token_digest"),
                    },
                }
            )
            current_event_idx += 1

        # Oracle executes correct mutation
        mut_call_id = "call_mut_001"
        events.append(
            {
                "event_index": current_event_idx,
                "event_type": "mcp_call",
                "call_id": mut_call_id,
                "tool_name": "execute_mutation",
                "arguments": {
                    "entity_id": spec.target_entity,
                    "attribute": spec.target_attribute,
                    "bound_value": target_val,
                },
            }
        )
        current_event_idx += 1

        mut_token_digest = compute_token_digest(target_val)
        events.append(
            {
                "event_index": current_event_idx,
                "event_type": "tool_result",
                "call_id": mut_call_id,
                "result": {
                    "status": "executed",
                    "entity_id": spec.target_entity,
                    "attribute": spec.target_attribute,
                    "bound_value": target_val,
                    "bound_token": target_val,
                    "token_digest": mut_token_digest,
                },
            }
        )
        current_event_idx += 1

        mutations.append(
            {
                "entity_id": spec.target_entity,
                "attribute": spec.target_attribute,
                "bound_value": target_val,
            }
        )
        task_success = True
    elif control_type == "stale_mutant":
        # Stale mutant reads all chunks, but executes mutation with stale initial value
        for chunk in scenario["chunks"]:
            call_id = f"call_read_{chunk['chunk_id']}"
            events.append(
                {
                    "event_index": current_event_idx,
                    "event_type": "mcp_call",
                    "call_id": call_id,
                    "tool_name": "get_context_chunk",
                    "arguments": {"chunk_id": chunk["chunk_id"]},
                }
            )
            current_event_idx += 1
            events.append(
                {
                    "event_index": current_event_idx,
                    "event_type": "tool_result",
                    "call_id": call_id,
                    "result": {
                        "status": "ok",
                        "chunk_id": chunk["chunk_id"],
                        "content": chunk["content"],
                        "content_digest": chunk["content_digest"],
                        "entity_id": chunk.get("entity_id"),
                        "attribute": chunk.get("attribute"),
                        "bound_token": chunk.get("bound_token"),
                        "token_digest": chunk.get("token_digest"),
                    },
                }
            )
            current_event_idx += 1

        mut_call_id = "call_mut_001"
        events.append(
            {
                "event_index": current_event_idx,
                "event_type": "mcp_call",
                "call_id": mut_call_id,
                "tool_name": "execute_mutation",
                "arguments": {
                    "entity_id": spec.target_entity,
                    "attribute": spec.target_attribute,
                    "bound_value": spec.initial_value,  # stale!
                },
            }
        )
        current_event_idx += 1

        stale_token_digest = compute_token_digest(spec.initial_value)
        events.append(
            {
                "event_index": current_event_idx,
                "event_type": "tool_result",
                "call_id": mut_call_id,
                "result": {
                    "status": "executed",
                    "entity_id": spec.target_entity,
                    "attribute": spec.target_attribute,
                    "bound_value": spec.initial_value,
                    "bound_token": spec.initial_value,
                    "token_digest": stale_token_digest,
                },
            }
        )
        current_event_idx += 1

        mutations.append(
            {
                "entity_id": spec.target_entity,
                "attribute": spec.target_attribute,
                "bound_value": spec.initial_value,
            }
        )
        task_success = False
    elif control_type == "nop":
        # Nop emits no calls or mutations
        task_success = False

    final_state_data = {
        "status": "executed"
        if control_type in ("oracle", "stale_mutant")
        else "unattempted",
        "step_count": len(events),
        "mutations": mutations,
        "invariants_passed": task_success,
        "details": {"control_type": control_type, "target_entity": spec.target_entity},
    }

    # 3. State Journal
    state_journal_data: dict[str, Any] | None = None
    if include_state_journal:
        state_journal_data = {
            "schema_version": 1,
            "status": "available",
            "reason": None,
            "changes": [
                {
                    "change_type": "mutation",
                    "entity_id": spec.target_entity,
                    "attribute": spec.target_attribute,
                    "value": m["bound_value"],
                    "bound_token": m["bound_value"],
                    "token_digest": compute_token_digest(m["bound_value"]),
                }
                for m in mutations
            ],
        }

    return {
        "trial_id": trial_id,
        "pair_id": spec.pair_id,
        "task_id": task_id,
        "arm": arm,
        "control_type": control_type,
        "contract": contract_data,
        "events": events,
        "final_state": final_state_data,
        "state_journal": state_journal_data,
        "task_success": task_success,
    }


def materialize_canary_trial_bundle(
    trial_data: dict[str, Any],
    target_dir: Path,
) -> TrialBundle:
    """Materialize synthesized artifacts to disk and load via canonical parser."""
    target_dir.mkdir(parents=True, exist_ok=True)

    # Write benchmark-contract.json
    (target_dir / "benchmark-contract.json").write_text(
        json.dumps(trial_data["contract"], indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # Write final-state.json
    (target_dir / "final-state.json").write_text(
        json.dumps(trial_data["final_state"], indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # Write benchmark-events.jsonl (line-delimited)
    events_lines = [json.dumps(e, sort_keys=True) for e in trial_data["events"]]
    (target_dir / "benchmark-events.jsonl").write_text(
        "\n".join(events_lines) + ("\n" if events_lines else ""),
        encoding="utf-8",
    )

    # Write state journal if present
    if trial_data.get("state_journal"):
        sj_dir = target_dir / "state-journal"
        sj_dir.mkdir(exist_ok=True)
        sj_data = trial_data["state_journal"]
        (sj_dir / "status.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": sj_data.get("status", "available"),
                    "reason": sj_data.get("reason"),
                }
            ),
            encoding="utf-8",
        )
        (sj_dir / "state-diff.json").write_text(
            json.dumps({"schema_version": 1, "changes": sj_data.get("changes", [])}),
            encoding="utf-8",
        )

    return load_trial_bundle(target_dir, trial_id=trial_data["trial_id"])


def extract_read_to_use_linkage(
    bundle: TrialBundle,
) -> dict[str, Any]:
    """Deterministic identity-based read->use linkage.

    Evaluates whether the executed mutation's bound token was observed in a prior
    read result event carrying the matching first-class entity_id, attribute,
    and bound_token identity with verified token digest equality, without any
    string parsing of raw content logs.

    Enforces:
    1. Opportunity Binding: Validates memory_write_opportunities == 0 from contract.
    2. Step Precedence: read result event index < mutation request event index.
    3. Mandatory Identity: Exact non-empty match on entity_id AND attribute.
    4. Exact Token Match: read bound_token == mutation bound_value (no whitespace normalization).
    5. Token Digest Integrity: read token_digest == compute_token_digest(mut_token).
    """
    contract_opps = bundle.contract.opportunity_counts
    if "memory_write_opportunities" not in contract_opps:
        return {
            "read_to_use_linked": False,
            "linkage_status": "missing_contract_write_opportunities",
            "matched_read_chunk_id": None,
            "bound_token": None,
            "token_digest": None,
            "content_digest": None,
            "write_to_read_opportunities": None,
            "write_to_read_rate": None,
            "write_to_read_to_use_rate": None,
        }

    write_opps = contract_opps["memory_write_opportunities"]
    if write_opps != 0:
        return {
            "read_to_use_linked": False,
            "linkage_status": "nonzero_contract_write_opportunities_unsupported",
            "matched_read_chunk_id": None,
            "bound_token": None,
            "token_digest": None,
            "content_digest": None,
            "write_to_read_opportunities": int(write_opps),
            "write_to_read_rate": None,
            "write_to_read_to_use_rate": None,
        }

    calls = bundle.correlated_calls
    final_mutations = bundle.final_state.mutations

    # Find mutation call
    mutation_call: CorrelatedToolCall | None = None
    for call in calls:
        if call.tool_name == "execute_mutation" and not call.is_error:
            mutation_call = call
            break

    bound_entity: str | None = None
    bound_attribute: str | None = None
    bound_token: str | None = None

    if mutation_call and isinstance(mutation_call.arguments, dict):
        bound_entity = mutation_call.arguments.get("entity_id")
        bound_attribute = mutation_call.arguments.get("attribute")
        bound_token = mutation_call.arguments.get("bound_value")
    elif final_mutations:
        m0 = final_mutations[0]
        if isinstance(m0, dict):
            bound_entity = m0.get("entity_id")
            bound_attribute = m0.get("attribute")
            bound_token = m0.get("bound_value")

    # Both entity and attribute must be non-empty strings
    if not bound_token or not bound_entity or not bound_attribute:
        return {
            "read_to_use_linked": False,
            "linkage_status": "no_mutation_or_incomplete_mutation_identity",
            "matched_read_chunk_id": None,
            "bound_token": None,
            "token_digest": None,
            "content_digest": None,
            "write_to_read_opportunities": 0,
            "write_to_read_rate": None,
            "write_to_read_to_use_rate": None,
        }

    expected_token_digest = compute_token_digest(bound_token)

    # Validate mutation call execution result and token digest integrity (B2)
    if not mutation_call or not mutation_call.result_event or mutation_call.is_error:
        return {
            "read_to_use_linked": False,
            "linkage_status": "mutation_result_integrity_failure",
            "matched_read_chunk_id": None,
            "bound_token": None,
            "token_digest": None,
            "content_digest": None,
            "write_to_read_opportunities": 0,
            "write_to_read_rate": None,
            "write_to_read_to_use_rate": None,
        }

    mut_res_payload = mutation_call.result_payload
    if not isinstance(mut_res_payload, dict):
        return {
            "read_to_use_linked": False,
            "linkage_status": "mutation_result_integrity_failure",
            "matched_read_chunk_id": None,
            "bound_token": None,
            "token_digest": None,
            "content_digest": None,
            "write_to_read_opportunities": 0,
            "write_to_read_rate": None,
            "write_to_read_to_use_rate": None,
        }

    mut_res_entity = mut_res_payload.get("entity_id")
    mut_res_attr = mut_res_payload.get("attribute")
    mut_res_token = mut_res_payload.get("bound_token") or mut_res_payload.get(
        "bound_value"
    )
    mut_res_digest = mut_res_payload.get("token_digest")

    if (
        mut_res_entity != bound_entity
        or mut_res_attr != bound_attribute
        or mut_res_token != bound_token
        or mut_res_digest != expected_token_digest
    ):
        return {
            "read_to_use_linked": False,
            "linkage_status": "mutation_token_digest_mismatch",
            "matched_read_chunk_id": None,
            "bound_token": None,
            "token_digest": None,
            "content_digest": None,
            "write_to_read_opportunities": 0,
            "write_to_read_rate": None,
            "write_to_read_to_use_rate": None,
        }

    mut_request_idx = (
        mutation_call.request_event.event_index
        if mutation_call.request_event
        else len(bundle.events) + 1
    )

    # Scan preceding read calls in step order
    matched_chunk_id: str | None = None
    matched_token: str | None = None
    matched_token_digest: str | None = None
    matched_content_digest: str | None = None
    failure_reason: str = "no_matching_read_observation"

    for call in calls:
        if call.tool_name != "get_context_chunk" or call.is_error:
            continue

        # Step precedence: result event must exist and precede mutation request
        if not call.result_event:
            continue
        if call.result_event.event_index >= mut_request_idx:
            failure_reason = "read_observed_after_mutation_request"
            continue

        payload = call.result_payload
        if not isinstance(payload, dict):
            continue

        read_token = payload.get("bound_token")
        read_digest = payload.get("token_digest")
        read_entity = payload.get("entity_id")
        read_attr = payload.get("attribute")

        # Mandatory non-empty entity and attribute identity match
        if not read_entity or not read_attr:
            continue
        if read_entity != bound_entity or read_attr != bound_attribute:
            continue

        # Exact token match
        if read_token is None:
            continue
        if read_token != bound_token:
            continue

        # Token digest integrity check
        if not read_digest or read_digest != expected_token_digest:
            failure_reason = "token_digest_mismatch"
            continue

        # Valid linkage established
        matched_chunk_id = payload.get("chunk_id")
        matched_token = read_token
        matched_token_digest = read_digest
        matched_content_digest = payload.get("content_digest")

    is_linked = matched_token is not None

    return {
        "read_to_use_linked": is_linked,
        "linkage_status": "linked" if is_linked else failure_reason,
        "matched_read_chunk_id": matched_chunk_id,
        "bound_token": matched_token,
        "token_digest": matched_token_digest,
        "content_digest": matched_content_digest,
        "write_to_read_opportunities": 0,
        "write_to_read_rate": None,  # strict NULL on 0 write opportunities
        "write_to_read_to_use_rate": None,  # strict NULL on 0 write opportunities
    }


def emit_canary_paired_condition_fact(
    trial_data: dict[str, Any],
) -> PairedConditionFact:
    """Emit verified canonical PairedConditionFact for a canary trial.

    Enforces that when state journal observability is absent, degraded, or its
    mutation record fails token digest validation, the emitted verdict is
    unknown (HOLD), preventing ungrounded claims.
    """
    is_arm0 = trial_data["arm"] in ("non_inverted", "arm0", "clean_non_inverted")
    variant = "non_inverted" if is_arm0 else "state_inverted"
    condition = "baseline_clean" if is_arm0 else "stale_value_override"
    trigger = "initial_fact_binding" if is_arm0 else "inversion_override_binding"
    target_ent = trial_data["contract"]["cell_factors"]["target_entity"]
    target_attr = trial_data["contract"]["cell_factors"]["target_attribute"]
    bound_val = (
        trial_data["final_state"]["mutations"][0]["bound_value"]
        if trial_data["final_state"]["mutations"]
        else "unbound"
    )
    diff = f"{target_ent}.{target_attr}={bound_val}"

    # Source digest is the canonical SHA-256 digest of the contract evidence
    contract_bytes = json.dumps(
        trial_data["contract"], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    source_digest = f"sha256:{hashlib.sha256(contract_bytes).hexdigest()}"

    # State Observability & Journal Token Integrity Gate
    state_journal = trial_data.get("state_journal")
    has_valid_state_journal = (
        isinstance(state_journal, dict) and state_journal.get("status") == "available"
    )

    is_journal_mutation_verified = False
    if has_valid_state_journal and bound_val != "unbound":
        expected_digest = compute_token_digest(bound_val)
        changes = state_journal.get("changes", [])
        # Collect all changes matching target entity and attribute
        target_changes = [
            c
            for c in changes
            if isinstance(c, dict)
            and c.get("entity_id") == target_ent
            and c.get("attribute") == target_attr
        ]
        # Must have exactly one unambiguous change for the target entity/attribute (B6)
        if len(target_changes) == 1:
            c = target_changes[0]
            # Mandatory first-class bound_token matching bound_val
            has_valid_bound_token = c.get("bound_token") == bound_val
            # If legacy 'value' field is also present, it must equal bound_val (cannot conflict)
            has_valid_legacy_value = "value" not in c or c.get("value") == bound_val
            # Token digest must match canonical expected digest
            has_valid_digest = c.get("token_digest") == expected_digest

            if has_valid_bound_token and has_valid_legacy_value and has_valid_digest:
                is_journal_mutation_verified = True
    elif has_valid_state_journal and not trial_data["final_state"]["mutations"]:
        # Nop control with valid available journal (0 mutations)
        changes = state_journal.get("changes", [])
        target_changes = [
            c
            for c in changes
            if isinstance(c, dict)
            and c.get("entity_id") == target_ent
            and c.get("attribute") == target_attr
        ]
        if len(target_changes) == 0:
            is_journal_mutation_verified = True
    if not has_valid_state_journal or not is_journal_mutation_verified:
        verdict = "unknown"
    else:
        verdict = "satisfied" if trial_data["task_success"] else "violated"

    return PairedConditionFact(
        source_ref=f"benchmark_contract:{trial_data['task_id']}",
        source_digest=source_digest,
        provenance_kind="mechanical",
        trial_id=trial_data["trial_id"],
        pair_id=trial_data["pair_id"],
        task_id=trial_data["task_id"],
        variant=variant,
        condition=condition,
        trigger=trigger,
        critical_action="execute_mutation",
        state_diff=diff,
        primary_verdict=verdict,
        secondary_verdict=verdict,
    )
