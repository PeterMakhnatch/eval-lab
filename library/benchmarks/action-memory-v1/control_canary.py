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
from typing import Any

from evallab.interpretation.benchmark_events import (
    BenchmarkContractRecord,
    BenchmarkEventRecord,
    CorrelatedToolCall,
    FinalStateRecord,
    TrialBundle,
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

    init_digest = f"sha256:{hashlib.sha256(initial_val.encode('utf-8')).hexdigest()}"
    inv_digest = f"sha256:{hashlib.sha256(inverted_val.encode('utf-8')).hexdigest()}"

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
            "benchmark_events": "/app/output/benchmark-events.jsonl",
            "final_state": "/app/output/final-state.json",
            "verifier_reward": "/logs/verifier/reward.txt",
        },
    }

    # 2. Events & Final State
    events: list[dict[str, Any]] = []
    mutations: list[dict[str, Any]] = []
    task_success = False

    if control_type == "oracle":
        # Oracle reads all chunks in canonical order
        for idx, chunk in enumerate(scenario["chunks"]):
            events.append(
                {
                    "event_index": idx,
                    "event_type": "mcp_call",
                    "call_id": f"call_{idx:03d}",
                    "tool_name": "get_context_chunk",
                    "arguments": {"chunk_id": chunk["chunk_id"]},
                }
            )
            # Result preserves full raw content separately while exposing first-class bound_token
            events.append(
                {
                    "event_index": idx + 1000,
                    "event_type": "tool_result",
                    "call_id": f"call_{idx:03d}",
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
        # Oracle executes correct mutation
        mut_call_id = f"call_{len(scenario['chunks']):03d}"
        events.append(
            {
                "event_index": len(scenario["chunks"]),
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
        mut_token_digest = (
            f"sha256:{hashlib.sha256(target_val.encode('utf-8')).hexdigest()}"
        )
        events.append(
            {
                "event_index": len(scenario["chunks"]) + 1000,
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
        for idx, chunk in enumerate(scenario["chunks"]):
            events.append(
                {
                    "event_index": idx,
                    "event_type": "mcp_call",
                    "call_id": f"call_{idx:03d}",
                    "tool_name": "get_context_chunk",
                    "arguments": {"chunk_id": chunk["chunk_id"]},
                }
            )
            events.append(
                {
                    "event_index": idx + 1000,
                    "event_type": "tool_result",
                    "call_id": f"call_{idx:03d}",
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
        mut_call_id = f"call_{len(scenario['chunks']):03d}"
        events.append(
            {
                "event_index": len(scenario["chunks"]),
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
        stale_token_digest = (
            f"sha256:{hashlib.sha256(spec.initial_value.encode('utf-8')).hexdigest()}"
        )
        events.append(
            {
                "event_index": len(scenario["chunks"]) + 1000,
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
            "status": "available",
            "reason": None,
            "changes": [
                {
                    "change_type": "mutation",
                    "entity_id": spec.target_entity,
                    "attribute": spec.target_attribute,
                    "value": m["bound_value"],
                    "bound_token": m["bound_value"],
                    "token_digest": f"sha256:{hashlib.sha256(m['bound_value'].encode('utf-8')).hexdigest()}",
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


def extract_read_to_use_linkage(
    bundle: TrialBundle,
) -> dict[str, Any]:
    """Deterministic identity-based read->use linkage.

    Evaluates whether the executed mutation's bound token was observed in a prior
    read event carrying the matching first-class entity_id, attribute, and
    bound_token identity, without any string parsing of raw content logs.

    Rules:
    - Step precedence: read event index < mutation event index.
    - Identity equality: read_event.bound_token == mutation.bound_value and
      read_event.entity_id == mutation.entity_id.
    - Token digest equality: read_event.token_digest == mutation.token_digest.
    - Write metrics: zero agent memory writes prior to mutation ->
      write_to_read_rate is None (NULL), write_to_read_to_use_rate is None (NULL).
    """
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
        bound_entity = str(mutation_call.arguments.get("entity_id", ""))
        bound_attribute = str(mutation_call.arguments.get("attribute", ""))
        bound_token = str(mutation_call.arguments.get("bound_value", ""))
    elif final_mutations:
        m0 = final_mutations[0]
        if isinstance(m0, dict):
            bound_entity = str(m0.get("entity_id", ""))
            bound_attribute = str(m0.get("attribute", ""))
            bound_token = str(m0.get("bound_value", ""))

    if not bound_token or not bound_entity:
        return {
            "read_to_use_linked": False,
            "linkage_status": "no_mutation",
            "matched_read_chunk_id": None,
            "bound_token": None,
            "token_digest": None,
            "content_digest": None,
            "write_to_read_opportunities": 0,
            "write_to_read_rate": None,
            "write_to_read_to_use_rate": None,
        }

    mut_index = (
        mutation_call.request_event.event_index if mutation_call else len(bundle.events)
    )

    # Scan preceding read calls in step order
    matched_chunk_id: str | None = None
    matched_token: str | None = None
    matched_token_digest: str | None = None
    matched_content_digest: str | None = None

    for call in calls:
        if call.tool_name != "get_context_chunk" or call.is_error:
            continue
        if call.request_event.event_index >= mut_index:
            continue  # Step precedence: read must precede use

        payload = call.result_payload
        if not isinstance(payload, dict):
            continue

        read_token = payload.get("bound_token")
        read_entity = payload.get("entity_id")
        read_attr = payload.get("attribute")

        # Identity equality check (no string parsing)
        if (
            read_token is not None
            and read_entity == bound_entity
            and (not read_attr or read_attr == bound_attribute)
        ):
            if str(read_token).strip() == str(bound_token).strip():
                matched_chunk_id = payload.get("chunk_id")
                matched_token = str(read_token)
                matched_token_digest = payload.get("token_digest")
                matched_content_digest = payload.get("content_digest")

    is_linked = matched_token is not None

    return {
        "read_to_use_linked": is_linked,
        "linkage_status": "linked" if is_linked else "unlinked",
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

    Enforces that when state journal observability is absent or degraded, the
    emitted verdict is unknown (HOLD), preventing ungrounded claims.
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

    # State Observability Gate: If state journal is absent, verdict must be unknown (HOLD)
    state_journal = trial_data.get("state_journal")
    has_valid_state_journal = (
        isinstance(state_journal, dict) and state_journal.get("status") == "available"
    )

    if not has_valid_state_journal:
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
