"""Runner-neutral write/read/use and context-boundary feature producer."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from evallab.semantic_facts import ContextOperationFact

_MEMORY_OPERATIONS = frozenset({"memory_write", "memory_read", "memory_use"})
_BOUNDARY_OPERATIONS = frozenset({"compaction", "clear", "evict", "session_boundary"})


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


__all__ = ["MemoryContinuityFeatures", "extract_memory_continuity_features"]
