from __future__ import annotations

from evallab.interpretation.feature_registry import TRAJECTORY_FEATURE_REGISTRY
from evallab.interpretation.producers.memory_continuity import (
    MemoryContinuityFeatures,
    extract_memory_continuity_features,
)
from evallab.semantic_facts import ContextOperationFact

SOURCE_DIGEST = "sha256:" + "a" * 64
MEMORY_A = "sha256:" + "b" * 64
MEMORY_B = "sha256:" + "c" * 64


def _fact(
    operation_id: str,
    operation: str,
    step_index: int | None,
    *,
    content_digest: str | None = None,
    context_position_tokens: int | None = None,
    trial_id: str = "trial-memory",
) -> ContextOperationFact:
    return ContextOperationFact.model_validate(
        {
            "source_ref": f"trajectory.json#{operation_id}",
            "source_digest": SOURCE_DIGEST,
            "provenance_kind": "mechanical",
            "trial_id": trial_id,
            "operation_id": operation_id,
            "operation": operation,
            "step_index": step_index,
            "content_digest": content_digest,
            "context_position_tokens": context_position_tokens,
        }
    )


def test_links_explicit_write_read_use_across_a_session_boundary() -> None:
    facts = [
        _fact("use-a", "memory_use", 5, content_digest=MEMORY_A),
        _fact("write-b", "memory_write", 2, content_digest=MEMORY_B),
        _fact("read-a", "memory_read", 4, content_digest=MEMORY_A, context_position_tokens=8000),
        _fact("boundary", "session_boundary", 3),
        _fact("read-b", "memory_read", 6, content_digest=MEMORY_B, context_position_tokens=9000),
        _fact("write-a", "memory_write", 1, content_digest=MEMORY_A),
    ]

    (features,) = extract_memory_continuity_features(facts)

    assert features.memory_write_count == 2
    assert features.memory_read_count == 2
    assert features.memory_use_count == 1
    assert features.write_read_link_count == 2
    assert features.write_read_use_link_count == 1
    assert features.mean_write_to_read_latency_steps == 3.5
    assert features.mean_read_to_use_latency_steps == 1.0
    assert features.context_boundary_count == 1
    assert features.boundary_carryover_opportunity_count == 2
    assert features.boundary_carryover_success_count == 1
    assert features.boundary_carryover_rate == 0.5
    assert features.memory_read_context_position_observation_count == 2
    assert features.memory_read_context_position_coverage == 1.0
    assert features.mean_memory_read_context_position_tokens == 8500.0
    assert features.max_memory_read_context_position_tokens == 9000
    assert features.memory_continuity_status == "observed"

    (reordered,) = extract_memory_continuity_features(list(reversed(facts)))
    assert reordered == features


def test_zero_boundary_opportunity_is_null_not_zero() -> None:
    facts = [
        _fact("write", "memory_write", 1, content_digest=MEMORY_A),
        _fact("read", "memory_read", 2, content_digest=MEMORY_A),
    ]

    (features,) = extract_memory_continuity_features(facts)

    assert features.write_read_link_count == 1
    assert features.context_boundary_count == 0
    assert features.boundary_carryover_opportunity_count == 0
    assert features.boundary_carryover_success_count == 0
    assert features.boundary_carryover_rate is None
    assert features.memory_read_context_position_observation_count == 0
    assert features.memory_read_context_position_coverage == 0.0


def test_read_does_not_imply_use() -> None:
    facts = [
        _fact("write", "memory_write", 1, content_digest=MEMORY_A),
        _fact("boundary", "compaction", 2),
        _fact("read", "memory_read", 3, content_digest=MEMORY_A),
    ]

    (features,) = extract_memory_continuity_features(facts)

    assert features.write_read_link_count == 1
    assert features.write_read_use_link_count == 0
    assert features.boundary_carryover_opportunity_count == 1
    assert features.boundary_carryover_success_count == 0
    assert features.boundary_carryover_rate == 0.0


def test_missing_order_or_content_refuses_link_inference() -> None:
    (missing_order,) = extract_memory_continuity_features(
        [_fact("write", "memory_write", None, content_digest=MEMORY_A)]
    )
    assert missing_order.memory_continuity_status == "missing_step_order"
    assert missing_order.write_read_link_count is None
    assert missing_order.boundary_carryover_rate is None

    (missing_identity,) = extract_memory_continuity_features([_fact("read", "memory_read", 1)])
    assert missing_identity.memory_continuity_status == "missing_content_identity"
    assert missing_identity.write_read_link_count is None


def test_multiple_trials_produce_sorted_independent_rows() -> None:
    facts = [
        _fact("read-z", "memory_read", 2, content_digest=MEMORY_A, trial_id="z"),
        _fact("write-z", "memory_write", 1, content_digest=MEMORY_A, trial_id="z"),
        _fact("read-a", "memory_read", 1, content_digest=MEMORY_B, trial_id="a"),
    ]

    rows = extract_memory_continuity_features(facts)

    assert [row.trial_id for row in rows] == ["a", "z"]
    assert rows[0].write_read_link_count == 0
    assert rows[1].write_read_link_count == 1


def test_every_memory_continuity_output_is_governed() -> None:
    output_fields = set(MemoryContinuityFeatures.__dataclass_fields__) - {
        "source_digest",
        "trial_id",
    }
    registered = set(TRAJECTORY_FEATURE_REGISTRY.by_family("memory-continuity-v1"))
    assert registered == output_fields
