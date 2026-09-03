"""Runner-neutral write/read/use and context-boundary feature producer.

Write->read->use chains are counted only over explicit source-native memory
operations that share a first-class declared content digest. Chat logs, answer
overlap, and substring matches are never identity. A missing declared content
digest is refusal (missing_content_identity), never synthesized from content.
action-memory-v1 is a separate binding/handle family with no agent writes and
must not be used to validate these chains.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from evallab.semantic_facts import (
    ContextOperationFact,
    ContextOperationPayloadV1,
    context_operation_content_digest,
)

_MEMORY_OPERATIONS = frozenset({"memory_write", "memory_read", "memory_use"})
_BOUNDARY_OPERATIONS = frozenset({"compaction", "clear", "evict", "session_boundary"})
_CANONICAL_OPERATIONS = _MEMORY_OPERATIONS | _BOUNDARY_OPERATIONS
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOOL_NAME_KEYS = ("function_name", "tool_name", "name", "tool", "method")
_TOOL_PREFIXES = ("memory_mcp_", "mcp_", "functions.")

MemoryContinuityStatus = Literal[
    "observed",
    "missing_step_order",
    "missing_content_identity",
    "missing_operation_identity",
    "unavailable",
]

MEMORY_CONTINUITY_FACT_SET_DOMAIN = b"evallab.memory-continuity-fact-set.v1\x00"


@dataclass(frozen=True)
class MemoryContinuityFeatures:
    """Per-trial facts over explicit memory identities and boundary events."""

    trial_id: str
    source_digest: str
    fact_set_digest: str | None
    memory_write_count: int
    memory_read_count: int
    memory_use_count: int
    write_read_link_count: int | None
    write_read_use_link_count: int | None
    mean_write_to_read_latency_steps: float | None
    mean_read_to_use_latency_steps: float | None
    context_boundary_count: int
    boundary_carryover_opportunity_count: int | None
    boundary_carryover_success_count: int | None
    boundary_carryover_rate: float | None
    memory_read_context_position_observation_count: int
    memory_read_context_position_coverage: float | None
    mean_memory_read_context_position_tokens: float | None
    max_memory_read_context_position_tokens: int | None
    memory_continuity_status: MemoryContinuityStatus

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fact_set_digest(facts: Sequence[ContextOperationFact]) -> str:
    serialized_rows = [
        json.dumps(
            fact.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        for fact in facts
    ]
    # Complete canonical serialized fact value as representation-only tie-breaker
    canonical_json_bytes = ("[" + ",".join(sorted(serialized_rows)) + "]").encode("utf-8")
    digest_bytes = MEMORY_CONTINUITY_FACT_SET_DOMAIN + canonical_json_bytes
    return f"sha256:{hashlib.sha256(digest_bytes).hexdigest()}"


def _mean(values: Sequence[int]) -> float | None:
    return sum(values) / len(values) if values else None


def _empty_links(
    facts: Sequence[ContextOperationFact],
    *,
    status: MemoryContinuityStatus,
    trial_id: str | None = None,
    source_digest: str | None = None,
) -> MemoryContinuityFeatures:
    reads = [fact for fact in facts if fact.operation == "memory_read"]
    positions = [
        fact.context_position_tokens for fact in reads if fact.context_position_tokens is not None
    ]
    tid = trial_id or (facts[0].trial_id if facts else "unknown")
    src_digest = source_digest or (facts[0].source_digest if facts else "")
    return MemoryContinuityFeatures(
        trial_id=tid,
        source_digest=src_digest,
        fact_set_digest=_fact_set_digest(facts) if facts else None,
        memory_write_count=sum(fact.operation == "memory_write" for fact in facts),
        memory_read_count=len(reads),
        memory_use_count=sum(fact.operation == "memory_use" for fact in facts),
        write_read_link_count=None,
        write_read_use_link_count=None,
        mean_write_to_read_latency_steps=None,
        mean_read_to_use_latency_steps=None,
        context_boundary_count=sum(fact.operation in _BOUNDARY_OPERATIONS for fact in facts),
        boundary_carryover_opportunity_count=None,
        boundary_carryover_success_count=None,
        boundary_carryover_rate=None,
        memory_read_context_position_observation_count=len(positions),
        memory_read_context_position_coverage=(len(positions) / len(reads) if reads else None),
        mean_memory_read_context_position_tokens=_mean(positions),
        max_memory_read_context_position_tokens=max(positions) if positions else None,
        memory_continuity_status=status,
    )


def _unavailable_without_events(
    *,
    trial_id: str,
    source_digest: str,
) -> MemoryContinuityFeatures:
    """Typed unavailable row when a trial has no explicit source-native memory events."""
    return MemoryContinuityFeatures(
        trial_id=trial_id,
        source_digest=source_digest,
        fact_set_digest=None,
        memory_write_count=0,
        memory_read_count=0,
        memory_use_count=0,
        write_read_link_count=None,
        write_read_use_link_count=None,
        mean_write_to_read_latency_steps=None,
        mean_read_to_use_latency_steps=None,
        context_boundary_count=0,
        boundary_carryover_opportunity_count=0,
        boundary_carryover_success_count=0,
        boundary_carryover_rate=None,
        memory_read_context_position_observation_count=0,
        memory_read_context_position_coverage=None,
        mean_memory_read_context_position_tokens=None,
        max_memory_read_context_position_tokens=None,
        memory_continuity_status="unavailable",
    )


def _extract_trial(facts: Sequence[ContextOperationFact]) -> MemoryContinuityFeatures:
    if not facts:
        raise ValueError("Cannot extract memory continuity features from empty facts")

    # A2: Operation identity completeness & uniqueness check
    op_ids = [fact.operation_id for fact in facts]
    if len(op_ids) != len(set(op_ids)) or any(not fact.operation_id for fact in facts):
        return _empty_links(facts, status="missing_operation_identity")

    # A3: Total step_index ordering check
    if any(fact.step_index is None for fact in facts):
        return _empty_links(facts, status="missing_step_order")
    step_indices = [fact.step_index for fact in facts]
    if len(step_indices) != len(set(step_indices)):
        return _empty_links(facts, status="missing_step_order")

    # A1: Content identity check for memory operations
    if any(fact.operation in _MEMORY_OPERATIONS and fact.content_digest is None for fact in facts):
        return _empty_links(facts, status="missing_content_identity")

    # Total ordering strictly by step_index
    ordered = sorted(facts, key=lambda fact: fact.step_index if fact.step_index is not None else -1)
    writes = [fact for fact in ordered if fact.operation == "memory_write"]
    reads = [fact for fact in ordered if fact.operation == "memory_read"]
    uses = [fact for fact in ordered if fact.operation == "memory_use"]
    boundaries = [fact for fact in ordered if fact.operation in _BOUNDARY_OPERATIONS]

    writes_by_digest: dict[str, list[ContextOperationFact]] = defaultdict(list)
    for fact in writes:
        assert fact.content_digest is not None
        writes_by_digest[fact.content_digest].append(fact)

    # Exact digest equality only. Substring, log text, and action-memory
    # bound-target values are not content identity.
    linked_reads: list[tuple[ContextOperationFact, ContextOperationFact]] = []
    linked_reads_by_digest: dict[str, list[ContextOperationFact]] = defaultdict(list)
    for read in reads:
        assert read.content_digest is not None and read.step_index is not None
        candidates = [
            write
            for write in writes_by_digest[read.content_digest]
            if write.step_index is not None and write.step_index < read.step_index
        ]
        if not candidates:
            continue
        # Latest strictly preceding write
        write = max(candidates, key=lambda fact: fact.step_index)
        linked_reads.append((write, read))
        linked_reads_by_digest[read.content_digest].append(read)

    # A2: One-to-one read->use assignment. A read may contribute to at most one
    # positive read->use link. Ambiguous, duplicate, or unmatched reuse makes
    # link/latency metrics unavailable.
    linked_uses: list[tuple[ContextOperationFact, ContextOperationFact]] = []
    available_reads_by_digest: dict[str, list[ContextOperationFact]] = {
        digest: list(reads_list) for digest, reads_list in linked_reads_by_digest.items()
    }
    for use in uses:
        assert use.content_digest is not None and use.step_index is not None
        available = available_reads_by_digest.get(use.content_digest, [])
        eligible = [
            read
            for read in available
            if read.step_index is not None and read.step_index < use.step_index
        ]
        if len(eligible) != 1:
            # Unmatched (0 eligible reads, including digest absent from reads)
            # or ambiguous (>1 simultaneously eligible reads) -> unavailable
            return _empty_links(facts, status="unavailable")
        matched_read = eligible[0]
        available.remove(matched_read)
        linked_uses.append((matched_read, use))
    write_read_latencies = [
        read.step_index - write.step_index
        for write, read in linked_reads
        if write.step_index is not None and read.step_index is not None
    ]
    read_use_latencies = [
        use.step_index - read.step_index
        for read, use in linked_uses
        if read.step_index is not None and use.step_index is not None
    ]

    carryover_opportunities = 0
    carryover_successes = 0
    for boundary in boundaries:
        assert boundary.step_index is not None
        eligible_digests = {
            write.content_digest
            for write in writes
            if write.step_index is not None and write.step_index < boundary.step_index
        }
        for content_digest in eligible_digests:
            assert content_digest is not None
            carryover_opportunities += 1
            post_boundary_reads = [
                read
                for read in linked_reads_by_digest[content_digest]
                if read.step_index is not None and read.step_index > boundary.step_index
            ]
            used_post_boundary = any(
                use.content_digest == content_digest
                and any(
                    r.operation_id == read.operation_id
                    for r, u in linked_uses
                    if u.operation_id == use.operation_id
                )
                for read in post_boundary_reads
                for use in uses
            )
            if used_post_boundary:
                carryover_successes += 1

    positions = [
        read.context_position_tokens for read in reads if read.context_position_tokens is not None
    ]

    return MemoryContinuityFeatures(
        trial_id=facts[0].trial_id,
        source_digest=facts[0].source_digest,
        fact_set_digest=_fact_set_digest(facts),
        memory_write_count=len(writes),
        memory_read_count=len(reads),
        memory_use_count=len(uses),
        write_read_link_count=len(linked_reads),
        write_read_use_link_count=len(linked_uses),
        mean_write_to_read_latency_steps=_mean(write_read_latencies),
        mean_read_to_use_latency_steps=_mean(read_use_latencies),
        context_boundary_count=len(boundaries),
        boundary_carryover_opportunity_count=carryover_opportunities,
        boundary_carryover_success_count=carryover_successes,
        boundary_carryover_rate=(
            carryover_successes / carryover_opportunities if carryover_opportunities else None
        ),
        memory_read_context_position_observation_count=len(positions),
        memory_read_context_position_coverage=(len(positions) / len(reads) if reads else None),
        mean_memory_read_context_position_tokens=_mean(positions),
        max_memory_read_context_position_tokens=max(positions) if positions else None,
        memory_continuity_status="observed",
    )


def extract_memory_continuity_features(
    facts: Sequence[ContextOperationFact],
) -> tuple[MemoryContinuityFeatures, ...]:
    """Produce one deterministic feature row per trial without inferring memory use."""
    grouped: dict[str, list[ContextOperationFact]] = defaultdict(list)
    for fact in facts:
        grouped[fact.trial_id].append(fact)
    return tuple(_extract_trial(grouped[trial_id]) for trial_id in sorted(grouped))


def _canonical_operation_name(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    if name in _CANONICAL_OPERATIONS:
        return name
    for prefix in _TOOL_PREFIXES:
        if name.startswith(prefix):
            stripped = name[len(prefix) :]
            if stripped in _CANONICAL_OPERATIONS:
                return stripped
            return None
    return None


def _tool_name(call: Mapping[str, Any]) -> str:
    for key in _TOOL_NAME_KEYS:
        value = call.get(key)
        if isinstance(value, str):
            return value
    payload = call.get("payload")
    if isinstance(payload, Mapping):
        return _tool_name(payload)
    return ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _extract_payload_v1(arguments: Mapping[str, Any]) -> ContextOperationPayloadV1 | None:
    candidate = arguments.get("payload")
    if candidate is None:
        return None
    if isinstance(candidate, ContextOperationPayloadV1):
        return candidate
    if isinstance(candidate, str):
        try:
            return ContextOperationPayloadV1.model_validate_json(candidate)
        except Exception:
            return None
    if isinstance(candidate, Mapping):
        try:
            return ContextOperationPayloadV1.model_validate(dict(candidate))
        except Exception:
            return None
    return None


def _resolve_content_digest(arguments: Mapping[str, Any]) -> str | None:
    """Resolve and validate declared content_digest for first-class memory operations.

    A missing declared content_digest is refusal (returns None); we never
    synthesize one from content or observation to accept the operation. If
    declared, it must be canonical sha256:<64 lowercase hex>. The payload must
    be present and strictly valid as ContextOperationPayloadV1, and the declared
    digest must exactly equal the domain-separated context_operation_content_digest.
    """
    raw_digest = arguments.get("content_digest")
    if raw_digest is None or not isinstance(raw_digest, str):
        return None
    if not _SHA256.fullmatch(raw_digest):
        return None

    payload = _extract_payload_v1(arguments)
    if payload is None:
        return None

    try:
        expected_digest = context_operation_content_digest(payload)
        if raw_digest != expected_digest:
            return None
    except Exception:
        return None

    return raw_digest


def _call_arguments(call: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = call.get("arguments")
    if raw is None:
        payload = call.get("payload")
        if isinstance(payload, Mapping):
            raw = payload.get("arguments")
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, Mapping) else {}
        return {}
    return raw if isinstance(raw, Mapping) else {}


def extract_context_operation_facts_from_atif(
    payload: Mapping[str, Any],
    *,
    trial_id: str,
    source_ref: str,
    source_digest: str,
) -> tuple[ContextOperationFact, ...]:
    """Map explicit ATIF tool calls onto ContextOperationFact rows.

    Only canonical operation names are admitted after stripping one sanctioned
    prefix. Chat turns, questions, answer overlap, substring matches,
    unrecognized function names, copied-context flags, and token drops are not
    inferred as memory or boundary operations. Missing/malformed declared
    content_digest is refusal (content_digest=None). Missing or duplicate
    operation ID refuses fact emission (returns empty tuple).
    """
    session_id = payload.get("session_id")
    session_value = session_id if isinstance(session_id, str) and session_id else None
    admitted_calls: list[tuple[int | None, int | None, str, Mapping[str, Any]]] = []

    for step in payload.get("steps") or ():
        if not isinstance(step, Mapping):
            continue
        step_index = _optional_int(step.get("step_id"))
        metrics = _mapping(step.get("metrics"))
        prompt_tokens = _optional_int(metrics.get("prompt_tokens"))

        for call in step.get("tool_calls") or ():
            if not isinstance(call, Mapping):
                continue
            arguments = _call_arguments(call)
            declared = arguments.get("operation")
            if not isinstance(declared, str) or not declared.strip():
                declared = call.get("operation")
            candidate_name = declared if isinstance(declared, str) else _tool_name(call)
            operation_name = _canonical_operation_name(candidate_name)
            if operation_name is None:
                continue
            admitted_calls.append((step_index, prompt_tokens, operation_name, call))

    if not admitted_calls:
        return ()

    call_ids: list[str] = []
    for _, _, _, call in admitted_calls:
        raw_id = call.get("tool_call_id")
        if not isinstance(raw_id, str) or not raw_id.strip() or "\n" in raw_id:
            return ()
        call_ids.append(raw_id.strip())

    if len(call_ids) != len(set(call_ids)):
        return ()

    facts: list[ContextOperationFact] = []
    for (step_index, prompt_tokens, operation_name, call), call_id in zip(
        admitted_calls, call_ids, strict=True
    ):
        arguments = _call_arguments(call)
        content_digest = _resolve_content_digest(arguments)
        position = _optional_int(arguments.get("context_position_tokens"))
        if position is None:
            position = prompt_tokens

        facts.append(
            ContextOperationFact.model_validate(
                {
                    "source_ref": f"{source_ref}#{call_id}",
                    "source_digest": source_digest,
                    "provenance_kind": "mechanical",
                    "trial_id": trial_id,
                    "operation_id": call_id,
                    "operation": operation_name,
                    "prompt_tokens": prompt_tokens,
                    "content_digest": content_digest,
                    "session_id": session_value,
                    "step_index": step_index,
                    "context_position_tokens": position,
                }
            )
        )
    return tuple(facts)


def extract_memory_continuity_features_from_atif(
    payload: Mapping[str, Any],
    *,
    trial_id: str,
    source_ref: str,
    source_digest: str,
) -> tuple[tuple[ContextOperationFact, ...], MemoryContinuityFeatures]:
    """Return ATIF-derived context facts and a continuity row for the trial.

    Chat turns, questions, answer overlap, substring/log matches, copied-context
    flags, and token drops are never inferred as write/read/use. Read->use
    requires an explicit memory_use with the same first-class content digest as a
    prior linked read. Absent explicit source-native memory events yield a typed
    unavailable row with zero opportunity. Incomplete/duplicate operation
    identities yield typed missing_operation_identity with no positive facts.
    """
    admitted_calls: list[Mapping[str, Any]] = []
    for step in payload.get("steps") or ():
        if not isinstance(step, Mapping):
            continue
        for call in step.get("tool_calls") or ():
            if not isinstance(call, Mapping):
                continue
            arguments = _call_arguments(call)
            declared = arguments.get("operation")
            if not isinstance(declared, str) or not declared.strip():
                declared = call.get("operation")
            candidate_name = declared if isinstance(declared, str) else _tool_name(call)
            operation_name = _canonical_operation_name(candidate_name)
            if operation_name in _CANONICAL_OPERATIONS:
                admitted_calls.append(call)

    if not admitted_calls:
        return (), _unavailable_without_events(
            trial_id=trial_id,
            source_digest=source_digest,
        )

    call_ids: list[str] = []
    for call in admitted_calls:
        raw_id = call.get("tool_call_id")
        if not isinstance(raw_id, str) or not raw_id.strip() or "\n" in raw_id:
            return (), _empty_links(
                (),
                status="missing_operation_identity",
                trial_id=trial_id,
                source_digest=source_digest,
            )
        call_ids.append(raw_id.strip())

    if len(call_ids) != len(set(call_ids)):
        return (), _empty_links(
            (),
            status="missing_operation_identity",
            trial_id=trial_id,
            source_digest=source_digest,
        )

    facts = extract_context_operation_facts_from_atif(
        payload,
        trial_id=trial_id,
        source_ref=source_ref,
        source_digest=source_digest,
    )
    if not facts:
        return (), _unavailable_without_events(
            trial_id=trial_id,
            source_digest=source_digest,
        )

    features = extract_memory_continuity_features(facts)
    return facts, features[0]


__all__ = [
    "MemoryContinuityFeatures",
    "MemoryContinuityStatus",
    "extract_context_operation_facts_from_atif",
    "extract_memory_continuity_features",
    "extract_memory_continuity_features_from_atif",
]
