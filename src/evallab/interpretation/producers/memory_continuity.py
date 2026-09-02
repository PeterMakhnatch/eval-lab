"""Runner-neutral write/read/use and context-boundary feature producer."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from evallab.semantic_facts import ContextOperationFact

_MEMORY_OPERATIONS = frozenset({"memory_write", "memory_read", "memory_use"})
_BOUNDARY_OPERATIONS = frozenset({"compaction", "clear", "evict", "session_boundary"})
_CANONICAL_OPERATIONS = _MEMORY_OPERATIONS | _BOUNDARY_OPERATIONS
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOOL_NAME_KEYS = ("function_name", "tool_name", "name", "tool", "method")
_TOOL_PREFIXES = ("memory_mcp_", "mcp_", "functions.")


@dataclass(frozen=True)
class MemoryContinuityFeatures:
    """Per-trial facts over explicit memory identities and boundary events."""

    trial_id: str
    source_digest: str
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
    memory_continuity_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _source_digest(facts: Sequence[ContextOperationFact]) -> str:
    payload = [
        fact.model_dump(mode="json", exclude_none=False)
        for fact in sorted(facts, key=lambda fact: fact.operation_id)
    ]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _mean(values: Sequence[int]) -> float | None:
    return sum(values) / len(values) if values else None


def _empty_links(
    facts: Sequence[ContextOperationFact],
    *,
    status: str,
) -> MemoryContinuityFeatures:
    reads = [fact for fact in facts if fact.operation == "memory_read"]
    positions = [
        fact.context_position_tokens for fact in reads if fact.context_position_tokens is not None
    ]
    return MemoryContinuityFeatures(
        trial_id=facts[0].trial_id,
        source_digest=_source_digest(facts),
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


def _extract_trial(facts: Sequence[ContextOperationFact]) -> MemoryContinuityFeatures:
    if any(fact.step_index is None for fact in facts):
        return _empty_links(facts, status="missing_step_order")
    if any(fact.operation in _MEMORY_OPERATIONS and fact.content_digest is None for fact in facts):
        return _empty_links(facts, status="missing_content_identity")

    ordered = sorted(facts, key=lambda fact: (fact.step_index, fact.operation_id))
    writes = [fact for fact in ordered if fact.operation == "memory_write"]
    reads = [fact for fact in ordered if fact.operation == "memory_read"]
    uses = [fact for fact in ordered if fact.operation == "memory_use"]
    boundaries = [fact for fact in ordered if fact.operation in _BOUNDARY_OPERATIONS]

    writes_by_digest: dict[str, list[ContextOperationFact]] = defaultdict(list)
    for fact in writes:
        assert fact.content_digest is not None
        writes_by_digest[fact.content_digest].append(fact)

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
        write = max(candidates, key=lambda fact: (fact.step_index, fact.operation_id))
        linked_reads.append((write, read))
        linked_reads_by_digest[read.content_digest].append(read)

    linked_uses: list[tuple[ContextOperationFact, ContextOperationFact]] = []
    for use in uses:
        assert use.content_digest is not None and use.step_index is not None
        candidates = [
            read
            for read in linked_reads_by_digest[use.content_digest]
            if read.step_index is not None and read.step_index < use.step_index
        ]
        if not candidates:
            continue
        read = max(candidates, key=lambda fact: (fact.step_index, fact.operation_id))
        linked_uses.append((read, use))

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
            if any(
                use.content_digest == content_digest
                and use.step_index is not None
                and read.step_index is not None
                and use.step_index > read.step_index
                for read in post_boundary_reads
                for use in uses
            ):
                carryover_successes += 1

    positions = [
        read.context_position_tokens for read in reads if read.context_position_tokens is not None
    ]
    return MemoryContinuityFeatures(
        trial_id=facts[0].trial_id,
        source_digest=_source_digest(facts),
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


def _canonical_operation_name(name: str) -> str:
    normalized = name.strip().lower()
    for prefix in _TOOL_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
    if "." in normalized:
        normalized = normalized.rsplit(".", 1)[-1]
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    return normalized


def _tool_name(call: Mapping[str, Any]) -> str:
    for key in _TOOL_NAME_KEYS:
        value = call.get(key)
        if isinstance(value, str) and value.strip():
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


def _content_digest(value: Any) -> str | None:
    if isinstance(value, str) and _SHA256.fullmatch(value):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        encoded = value.encode()
    else:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


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


def _observation_content_by_call(step: Mapping[str, Any]) -> dict[str, Any]:
    observation = _mapping(step.get("observation"))
    contents: dict[str, Any] = {}
    for result in observation.get("results") or ():
        if not isinstance(result, Mapping):
            continue
        call_id = result.get("source_call_id")
        if isinstance(call_id, str) and call_id and "content" in result:
            contents[call_id] = result.get("content")
    return contents


def extract_context_operation_facts_from_atif(
    payload: Mapping[str, Any],
    *,
    trial_id: str,
    source_ref: str,
    source_digest: str,
) -> tuple[ContextOperationFact, ...]:
    """Map explicit ATIF tool calls onto ContextOperationFact rows.

    Only canonical operation names are admitted: memory_write, memory_read,
    memory_use, compaction, clear, evict, and session_boundary. MCP/OpenCode
    prefixes already used at the ATIF boundary are stripped. Unrecognized
    function names, copied-context flags, and token drops are not inferred
    as memory or boundary operations.
    """
    session_id = payload.get("session_id")
    session_value = session_id if isinstance(session_id, str) and session_id else None
    facts: list[ContextOperationFact] = []
    for step in payload.get("steps") or ():
        if not isinstance(step, Mapping):
            continue
        step_index = _optional_int(step.get("step_id"))
        metrics = _mapping(step.get("metrics"))
        prompt_tokens = _optional_int(metrics.get("prompt_tokens"))
        observation_content = _observation_content_by_call(step)
        for call in step.get("tool_calls") or ():
            if not isinstance(call, Mapping):
                continue
            arguments = _call_arguments(call)
            declared = arguments.get("operation")
            if not isinstance(declared, str) or not declared.strip():
                declared = call.get("operation")
            operation_name = _canonical_operation_name(
                declared if isinstance(declared, str) else _tool_name(call)
            )
            if operation_name not in _CANONICAL_OPERATIONS:
                continue
            call_id = call.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                continue
            content_digest = _content_digest(arguments.get("content_digest"))
            if content_digest is None:
                content_digest = _content_digest(arguments.get("content"))
            if content_digest is None:
                content_digest = _content_digest(observation_content.get(call_id))
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
) -> tuple[tuple[ContextOperationFact, ...], MemoryContinuityFeatures | None]:
    """Return ATIF-derived context facts and the continuity row, if any facts exist."""
    facts = extract_context_operation_facts_from_atif(
        payload,
        trial_id=trial_id,
        source_ref=source_ref,
        source_digest=source_digest,
    )
    if not facts:
        return facts, None
    features = extract_memory_continuity_features(facts)
    return facts, features[0]


__all__ = [
    "MemoryContinuityFeatures",
    "extract_context_operation_facts_from_atif",
    "extract_memory_continuity_features",
    "extract_memory_continuity_features_from_atif",
]
