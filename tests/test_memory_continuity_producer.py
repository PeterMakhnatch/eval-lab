from __future__ import annotations

import hashlib
import json

from evallab.interpretation.feature_registry import TRAJECTORY_FEATURE_REGISTRY
from evallab.interpretation.producers.memory_continuity import (
    MemoryContinuityFeatures,
    extract_context_operation_facts_from_atif,
    extract_memory_continuity_features,
    extract_memory_continuity_features_from_atif,
)
from evallab.semantic_facts import (
    ContextOperationFact,
    ContextOperationPayloadV1,
    context_operation_content_digest,
)

SOURCE_DIGEST = "sha256:" + "a" * 64


def _payload(summary: str, **overrides: object) -> ContextOperationPayloadV1:
    values: dict[str, object] = {
        "summary": summary,
        "forgotten_message_indices": [],
        "compression_metadata": {},
    }
    values.update(overrides)
    return ContextOperationPayloadV1.model_validate(values)


PAYLOAD_A = _payload("The user selected the red key.")
PAYLOAD_B = _payload("The user selected the blue key.")
DIGEST_A = context_operation_content_digest(PAYLOAD_A)
DIGEST_B = context_operation_content_digest(PAYLOAD_B)


def _fact(
    operation_id: str,
    operation: str,
    step_index: int | None,
    *,
    content_digest: str | None = None,
    context_position_tokens: int | None = None,
    trial_id: str = "trial-memory",
    **extra: object,
) -> ContextOperationFact:
    values: dict[str, object] = {
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
    values.update(extra)
    return ContextOperationFact.model_validate(values)


def _atif_digest() -> str:
    return "sha256:" + "d" * 64


def _atif_payload(steps: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": "session-generic",
        "agent": {"name": "opencode", "version": "test"},
        "steps": steps,
    }


def test_links_explicit_write_read_use_across_a_session_boundary() -> None:
    facts = [
        _fact("use-a", "memory_use", 5, content_digest=DIGEST_A),
        _fact("write-b", "memory_write", 2, content_digest=DIGEST_B),
        _fact("read-a", "memory_read", 4, content_digest=DIGEST_A, context_position_tokens=8000),
        _fact("boundary", "session_boundary", 3),
        _fact("read-b", "memory_read", 6, content_digest=DIGEST_B, context_position_tokens=9000),
        _fact("write-a", "memory_write", 1, content_digest=DIGEST_A),
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
    assert features.source_digest == SOURCE_DIGEST
    assert features.fact_set_digest is not None

    (reordered,) = extract_memory_continuity_features(list(reversed(facts)))
    assert reordered == features


def test_zero_boundary_opportunity_is_null_not_zero() -> None:
    facts = [
        _fact("write", "memory_write", 1, content_digest=DIGEST_A),
        _fact("read", "memory_read", 2, content_digest=DIGEST_A),
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
        _fact("write", "memory_write", 1, content_digest=DIGEST_A),
        _fact("boundary", "compaction", 2),
        _fact("read", "memory_read", 3, content_digest=DIGEST_A),
    ]

    (features,) = extract_memory_continuity_features(facts)

    assert features.write_read_link_count == 1
    assert features.write_read_use_link_count == 0
    assert features.boundary_carryover_opportunity_count == 1
    assert features.boundary_carryover_success_count == 0
    assert features.boundary_carryover_rate == 0.0


def test_missing_order_or_content_refuses_link_inference() -> None:
    (missing_order,) = extract_memory_continuity_features(
        [_fact("write", "memory_write", None, content_digest=DIGEST_A)]
    )
    assert missing_order.memory_continuity_status == "missing_step_order"
    assert missing_order.write_read_link_count is None
    assert missing_order.boundary_carryover_rate is None

    (missing_identity,) = extract_memory_continuity_features([_fact("read", "memory_read", 1)])
    assert missing_identity.memory_continuity_status == "missing_content_identity"
    assert missing_identity.write_read_link_count is None


def test_duplicate_step_order_refuses_same_step_precedence() -> None:
    (ambiguous_order,) = extract_memory_continuity_features(
        [
            _fact("write", "memory_write", 4, content_digest=DIGEST_A),
            _fact("read", "memory_read", 4, content_digest=DIGEST_A),
        ]
    )

    assert ambiguous_order.memory_continuity_status == "missing_step_order"
    assert ambiguous_order.memory_write_count == 1
    assert ambiguous_order.memory_read_count == 1
    assert ambiguous_order.write_read_link_count is None
    assert ambiguous_order.write_read_use_link_count is None
    assert ambiguous_order.boundary_carryover_rate is None


def test_duplicate_operation_id_refuses_as_missing_operation_identity() -> None:
    (dup_id,) = extract_memory_continuity_features(
        [
            _fact("call-1", "memory_write", 1, content_digest=DIGEST_A),
            _fact("call-1", "memory_read", 2, content_digest=DIGEST_A),
        ]
    )

    assert dup_id.memory_continuity_status == "missing_operation_identity"
    assert dup_id.write_read_link_count is None
    assert dup_id.write_read_use_link_count is None


def test_multiple_trials_produce_sorted_independent_rows() -> None:
    facts = [
        _fact("read-z", "memory_read", 2, content_digest=DIGEST_A, trial_id="z"),
        _fact("write-z", "memory_write", 1, content_digest=DIGEST_A, trial_id="z"),
        _fact("read-a", "memory_read", 1, content_digest=DIGEST_B, trial_id="a"),
    ]

    rows = extract_memory_continuity_features(facts)

    assert [row.trial_id for row in rows] == ["a", "z"]
    assert rows[0].write_read_link_count == 0
    assert rows[1].write_read_link_count == 1


def test_every_memory_continuity_output_is_governed() -> None:
    output_fields = set(MemoryContinuityFeatures.__dataclass_fields__) - {
        "source_digest",
        "fact_set_digest",
        "trial_id",
    }
    registered = set(TRAJECTORY_FEATURE_REGISTRY.by_family("memory-continuity-v1"))
    assert registered == output_fields


def test_latest_preceding_write_wins_for_same_content() -> None:
    facts = [
        _fact("write-old", "memory_write", 1, content_digest=DIGEST_A),
        _fact("write-new", "memory_write", 3, content_digest=DIGEST_A),
        _fact("read", "memory_read", 4, content_digest=DIGEST_A),
        _fact("use", "memory_use", 5, content_digest=DIGEST_A),
    ]

    (features,) = extract_memory_continuity_features(facts)

    assert features.write_read_link_count == 1
    assert features.write_read_use_link_count == 1
    assert features.mean_write_to_read_latency_steps == 1.0


def test_a2_adversarial_one_read_two_uses_refused_as_unavailable() -> None:
    """A single read cannot be reused across multiple uses; many-to-one is unavailable."""
    facts = [
        _fact("w1", "memory_write", 1, content_digest=DIGEST_A),
        _fact("r1", "memory_read", 2, content_digest=DIGEST_A),
        _fact("u1", "memory_use", 3, content_digest=DIGEST_A),
        _fact("u2", "memory_use", 4, content_digest=DIGEST_A),
    ]

    (features,) = extract_memory_continuity_features(facts)

    assert features.memory_continuity_status == "unavailable"
    assert features.memory_write_count == 1
    assert features.memory_read_count == 1
    assert features.memory_use_count == 2
    assert features.write_read_link_count is None
    assert features.write_read_use_link_count is None
    assert features.mean_read_to_use_latency_steps is None


def test_a2_adversarial_multiple_simultaneously_eligible_reads_refused_as_unavailable() -> None:
    """Multiple unused preceding reads for a single use creates ambiguity and is unavailable."""
    facts = [
        _fact("w1", "memory_write", 1, content_digest=DIGEST_A),
        _fact("r1", "memory_read", 2, content_digest=DIGEST_A),
        _fact("r2", "memory_read", 3, content_digest=DIGEST_A),
        _fact("u1", "memory_use", 4, content_digest=DIGEST_A),
    ]

    (features,) = extract_memory_continuity_features(facts)

    assert features.memory_continuity_status == "unavailable"
    assert features.memory_write_count == 1
    assert features.memory_read_count == 2
    assert features.memory_use_count == 1
    assert features.write_read_link_count is None
    assert features.write_read_use_link_count is None
    assert features.mean_read_to_use_latency_steps is None


def test_a2_adversarial_unmatched_digest_use_refused_as_unavailable() -> None:
    """write(A), read(A), use(B) with unmatched digest makes link metrics unavailable."""
    facts = [
        _fact("w1", "memory_write", 1, content_digest=DIGEST_A),
        _fact("r1", "memory_read", 2, content_digest=DIGEST_A),
        _fact("u1", "memory_use", 3, content_digest=DIGEST_B),
    ]

    (features,) = extract_memory_continuity_features(facts)

    assert features.memory_continuity_status == "unavailable"
    assert features.memory_write_count == 1
    assert features.memory_read_count == 1
    assert features.memory_use_count == 1
    assert features.write_read_link_count is None
    assert features.write_read_use_link_count is None
    assert features.mean_write_to_read_latency_steps is None
    assert features.mean_read_to_use_latency_steps is None


def test_a2_unambiguous_alternating_one_to_one_reads_and_uses_accepted() -> None:
    """Alternating pattern where each use has exactly one eligible unused read links cleanly."""
    facts = [
        _fact("w1", "memory_write", 1, content_digest=DIGEST_A),
        _fact("r1", "memory_read", 2, content_digest=DIGEST_A),
        _fact("u1", "memory_use", 3, content_digest=DIGEST_A),
        _fact("r2", "memory_read", 4, content_digest=DIGEST_A),
        _fact("u2", "memory_use", 5, content_digest=DIGEST_A),
    ]

    (features,) = extract_memory_continuity_features(facts)

    assert features.memory_continuity_status == "observed"
    assert features.memory_write_count == 1
    assert features.memory_read_count == 2
    assert features.memory_use_count == 2
    assert features.write_read_link_count == 2
    assert features.write_read_use_link_count == 2
    assert features.mean_read_to_use_latency_steps == 1.0


def test_a5_adversarial_tied_invalid_facts_digest_is_representation_order_invariant() -> None:
    """Same multiset of invalid facts produces identical fact_set_digest under reversal."""
    fact1 = _fact("dup-id", "memory_write", 1, content_digest=DIGEST_A)
    fact2 = _fact("dup-id", "memory_read", 1, content_digest=DIGEST_B)

    facts = [fact1, fact2]
    reversed_facts = [fact2, fact1]

    (res1,) = extract_memory_continuity_features(facts)
    (res2,) = extract_memory_continuity_features(reversed_facts)

    assert res1.memory_continuity_status == "missing_operation_identity"
    assert res2.memory_continuity_status == "missing_operation_identity"
    assert res1.fact_set_digest is not None
    assert res2.fact_set_digest is not None
    assert res1.fact_set_digest == res2.fact_set_digest


def test_a5_adversarial_changed_emitted_fact_field_changes_fact_set_digest() -> None:
    """Otherwise identical emitted facts with changed prompt_tokens have distinct fact_set_digests."""
    fact_10 = _fact("w1", "memory_write", 1, content_digest=DIGEST_A, prompt_tokens=10)
    fact_20 = _fact("w1", "memory_write", 1, content_digest=DIGEST_A, prompt_tokens=20)

    (res_10,) = extract_memory_continuity_features([fact_10])
    (res_20,) = extract_memory_continuity_features([fact_20])

    assert res_10.fact_set_digest is not None
    assert res_20.fact_set_digest is not None
    assert res_10.fact_set_digest != res_20.fact_set_digest


def test_atif_v17_maps_canonical_operations_with_shared_canonical_digest_accepted() -> None:
    """Canonical declared digest matching domain-separated payload digest is accepted."""
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "write",
                "metrics": {"prompt_tokens": 100},
                "is_copied_context": True,
                "tool_calls": [
                    {
                        "tool_call_id": "w1",
                        "function_name": "memory_mcp_memory_write",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    },
                    {
                        "tool_call_id": "ignore-bash",
                        "function_name": "run_bash",
                        "arguments": {"cmd": "echo"},
                    },
                    {
                        "tool_call_id": "ignore-locomo-like",
                        "function_name": "remember",
                        "arguments": {"content": "alpha"},
                    },
                ],
            },
            {
                "step_id": 2,
                "source": "agent",
                "message": "boundary",
                "tool_calls": [
                    {
                        "tool_call_id": "b1",
                        "function_name": "session_boundary",
                        "arguments": {},
                    }
                ],
            },
            {
                "step_id": 3,
                "source": "agent",
                "message": "read",
                "metrics": {"prompt_tokens": 250},
                "tool_calls": [
                    {
                        "tool_call_id": "r1",
                        "function_name": "memory_read",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    }
                ],
            },
            {
                "step_id": 4,
                "source": "agent",
                "message": "use",
                "tool_calls": [
                    {
                        "tool_call_id": "u1",
                        "function_name": "memory_use",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    }
                ],
            },
        ]
    )

    facts, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-atif",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )

    assert [fact.operation for fact in facts] == [
        "memory_write",
        "session_boundary",
        "memory_read",
        "memory_use",
    ]
    assert all(fact.session_id == "session-generic" for fact in facts)
    assert facts[2].context_position_tokens == 250
    assert features.memory_write_count == 1
    assert features.memory_read_count == 1
    assert features.memory_use_count == 1
    assert features.write_read_link_count == 1
    assert features.write_read_use_link_count == 1
    assert features.context_boundary_count == 1
    assert features.boundary_carryover_success_count == 1
    assert features.memory_continuity_status == "observed"
    assert features.source_digest == _atif_digest()
    assert features.fact_set_digest is not None


def test_atif_does_not_invent_facts_from_copied_context_or_empty_tools() -> None:
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "hello",
                "is_copied_context": True,
                "tool_calls": [],
            }
        ]
    )
    facts, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-empty",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert facts == ()
    assert features.memory_continuity_status == "unavailable"
    assert features.source_digest == _atif_digest()
    assert features.fact_set_digest is None
    assert features.memory_write_count == 0
    assert features.memory_read_count == 0
    assert features.memory_use_count == 0
    assert features.write_read_link_count is None
    assert features.boundary_carryover_opportunity_count == 0
    assert features.boundary_carryover_rate is None


def test_atif_does_not_infer_write_read_use_from_chat_turns() -> None:
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "user",
                "message": "Remember that the cafe is on Main Street.",
            },
            {
                "step_id": 2,
                "source": "agent",
                "message": "Where is the cafe?",
            },
            {
                "step_id": 3,
                "source": "agent",
                "message": "The cafe is on Main Street.",
                "metrics": {"prompt_tokens": 400, "completion_tokens": 12},
            },
        ]
    )
    facts, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-chat",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert facts == ()
    assert features.memory_continuity_status == "unavailable"
    assert features.source_digest == _atif_digest()
    assert features.fact_set_digest is None
    assert features.memory_write_count == 0
    assert features.memory_read_count == 0
    assert features.memory_use_count == 0
    assert features.context_boundary_count == 0
    assert features.write_read_link_count is None
    assert features.write_read_use_link_count is None
    assert features.boundary_carryover_opportunity_count == 0
    assert features.boundary_carryover_success_count == 0
    assert features.boundary_carryover_rate is None
    assert features.memory_read_context_position_coverage is None


def test_atif_a1_missing_declared_digest_is_typed_missing_content_identity() -> None:
    """Missing declared content_digest is refusal, never synthesized from content."""
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "write",
                "tool_calls": [
                    {
                        "tool_call_id": "w1",
                        "function_name": "memory_write",
                        "arguments": {"payload": PAYLOAD_A.model_dump(mode="json")},
                    }
                ],
            },
            {
                "step_id": 2,
                "source": "agent",
                "message": "read",
                "tool_calls": [
                    {
                        "tool_call_id": "r1",
                        "function_name": "memory_read",
                        "arguments": {"payload": PAYLOAD_A.model_dump(mode="json")},
                    }
                ],
            },
        ]
    )
    facts, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-no-declared-digest",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert len(facts) == 2
    assert facts[0].content_digest is None
    assert facts[1].content_digest is None
    assert features.memory_continuity_status == "missing_content_identity"
    assert features.write_read_link_count is None


def test_atif_a1_adversarial_digest_only_rejected() -> None:
    """Declared digest without verifiable payload cannot prove equality and is rejected."""
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "write digest-only",
                "tool_calls": [
                    {
                        "tool_call_id": "w1",
                        "function_name": "memory_write",
                        "arguments": {"content_digest": DIGEST_A},
                    }
                ],
            },
            {
                "step_id": 2,
                "source": "agent",
                "message": "read digest-only",
                "tool_calls": [
                    {
                        "tool_call_id": "r1",
                        "function_name": "memory_read",
                        "arguments": {"content_digest": DIGEST_A},
                    }
                ],
            },
        ]
    )
    facts, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-digest-only",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert len(facts) == 2
    assert facts[0].content_digest is None
    assert facts[1].content_digest is None
    assert features.memory_continuity_status == "missing_content_identity"
    assert features.write_read_link_count is None


def test_atif_a1_adversarial_content_fallback_refused() -> None:
    """Valid typed payload provided under arguments['content'] instead of payload must refuse."""
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "write under content",
                "tool_calls": [
                    {
                        "tool_call_id": "w1",
                        "function_name": "memory_write",
                        "arguments": {
                            "content": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    }
                ],
            }
        ]
    )
    facts, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-content-fallback",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert len(facts) == 1
    assert facts[0].content_digest is None
    assert features.memory_continuity_status == "missing_content_identity"
    assert features.write_read_link_count is None


def test_atif_a1_adversarial_flattened_top_level_fields_refused() -> None:
    """Flattened top-level payload fields without explicit payload key must refuse."""
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "write flattened",
                "tool_calls": [
                    {
                        "tool_call_id": "w1",
                        "function_name": "memory_write",
                        "arguments": {
                            "summary": "The user selected the red key.",
                            "forgotten_message_indices": [],
                            "compression_metadata": {},
                            "content_digest": DIGEST_A,
                        },
                    }
                ],
            }
        ]
    )
    facts, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-flattened",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert len(facts) == 1
    assert facts[0].content_digest is None
    assert features.memory_continuity_status == "missing_content_identity"
    assert features.write_read_link_count is None


def test_atif_a1_adversarial_raw_undomained_sha_rejected() -> None:
    """Raw undomained SHA256 digest is rejected against domain-separated canonical digest."""
    raw_json_bytes = json.dumps(
        PAYLOAD_A.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    raw_undomained_sha = f"sha256:{hashlib.sha256(raw_json_bytes).hexdigest()}"

    assert raw_undomained_sha != DIGEST_A

    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "write with raw undomained sha",
                "tool_calls": [
                    {
                        "tool_call_id": "w1",
                        "function_name": "memory_write",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": raw_undomained_sha,
                        },
                    }
                ],
            }
        ]
    )
    facts, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-raw-sha",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert len(facts) == 1
    assert facts[0].content_digest is None
    assert features.memory_continuity_status == "missing_content_identity"
    assert features.write_read_link_count is None


def test_atif_a1_adversarial_bool_vs_int_exact_type_mismatch_rejected() -> None:
    """Bool in forgotten_message_indices or exact-type mismatch fails payload validation and is rejected."""
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "write with bool in forgotten indices",
                "tool_calls": [
                    {
                        "tool_call_id": "w1",
                        "function_name": "memory_write",
                        "arguments": {
                            "payload": {
                                "summary": "The user selected the red key.",
                                "forgotten_message_indices": [True],
                                "compression_metadata": {},
                            },
                            "content_digest": DIGEST_A,
                        },
                    }
                ],
            }
        ]
    )
    facts, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-type-mismatch",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert len(facts) == 1
    assert facts[0].content_digest is None
    assert features.memory_continuity_status == "missing_content_identity"
    assert features.write_read_link_count is None


def test_atif_a1_mismatched_declared_digest_and_payload_is_refused() -> None:
    """Declared digest that does not match declared payload content is refused."""
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "write",
                "tool_calls": [
                    {
                        "tool_call_id": "w1",
                        "function_name": "memory_write",
                        "arguments": {
                            "payload": PAYLOAD_B.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    }
                ],
            }
        ]
    )
    facts, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-mismatch",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert len(facts) == 1
    assert facts[0].content_digest is None
    assert features.memory_continuity_status == "missing_content_identity"
    assert features.write_read_link_count is None


def test_atif_a2_partial_missing_tool_call_id_is_typed_missing_operation_identity() -> None:
    """Admitted tool call missing tool_call_id emits no positive facts and produces missing_operation_identity."""
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "write without ID",
                "tool_calls": [
                    {
                        "function_name": "memory_write",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    }
                ],
            },
            {
                "step_id": 2,
                "source": "agent",
                "message": "read with ID",
                "tool_calls": [
                    {
                        "tool_call_id": "r1",
                        "function_name": "memory_read",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    }
                ],
            },
        ]
    )
    facts, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-missing-id",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert facts == ()
    assert features.memory_continuity_status == "missing_operation_identity"
    assert features.write_read_link_count is None
    assert features.write_read_use_link_count is None


def test_atif_a2_duplicate_tool_call_id_is_typed_missing_operation_identity() -> None:
    """Duplicate tool_call_id across admitted operations emits no positive facts and produces missing_operation_identity."""
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "write",
                "tool_calls": [
                    {
                        "tool_call_id": "dup-id",
                        "function_name": "memory_write",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    }
                ],
            },
            {
                "step_id": 2,
                "source": "agent",
                "message": "read with duplicate id",
                "tool_calls": [
                    {
                        "tool_call_id": "dup-id",
                        "function_name": "memory_read",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    }
                ],
            },
        ]
    )
    facts, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-duplicate-id",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert facts == ()
    assert features.memory_continuity_status == "missing_operation_identity"
    assert features.write_read_link_count is None


def test_atif_a3_same_step_ambiguity_is_typed_missing_step_order() -> None:
    """Multiple admitted tool calls in the same ATIF step share step_id and produce missing_step_order."""
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "write and read in same step",
                "tool_calls": [
                    {
                        "tool_call_id": "w1",
                        "function_name": "memory_write",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    },
                    {
                        "tool_call_id": "r1",
                        "function_name": "memory_read",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    },
                ],
            }
        ]
    )
    facts, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-same-step",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert len(facts) == 2
    assert facts[0].step_index == 1
    assert facts[1].step_index == 1
    assert features.memory_continuity_status == "missing_step_order"
    assert features.memory_write_count == 1
    assert features.memory_read_count == 1
    assert features.write_read_link_count is None


def test_atif_a4_exact_prefix_admission_and_unrelated_namespace_refusal() -> None:
    """Only exact documented prefixes are stripped; unrelated namespaces or extra prefixes are refused."""
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "namespace tests",
                "tool_calls": [
                    {
                        "tool_call_id": "c1",
                        "function_name": "memory_mcp_memory_write",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    },
                    {
                        "tool_call_id": "c2",
                        "function_name": "mcp_memory_write",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    },
                    {
                        "tool_call_id": "c3",
                        "function_name": "functions.memory_write",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    },
                    {
                        "tool_call_id": "c4",
                        "function_name": "unrelated.memory_write",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    },
                    {
                        "tool_call_id": "c5",
                        "function_name": "mcp_functions.memory_write",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    },
                    {
                        "tool_call_id": "c6",
                        "function_name": "MEMORY_WRITE",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    },
                ],
            }
        ]
    )
    facts = extract_context_operation_facts_from_atif(
        payload,
        trial_id="trial-namespace",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    admitted_ids = [f.operation_id for f in facts]
    assert admitted_ids == ["c1", "c2", "c3"]


def test_atif_a4_adversarial_whitespace_in_operation_name_refused() -> None:
    """Whitespace in function name or operation is never normalized and must be refused."""
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "whitespace tests",
                "tool_calls": [
                    {
                        "tool_call_id": "w1",
                        "function_name": " memory_write ",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    },
                    {
                        "tool_call_id": "w2",
                        "function_name": "\tmemory_write",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    },
                    {
                        "tool_call_id": "w3",
                        "function_name": "memory_write\n",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    },
                    {
                        "tool_call_id": "w4",
                        "function_name": " memory_mcp_memory_write",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    },
                    {
                        "tool_call_id": "w5",
                        "function_name": "memory_mcp_memory_write ",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    },
                ],
            }
        ]
    )
    facts = extract_context_operation_facts_from_atif(
        payload,
        trial_id="trial-whitespace",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert facts == ()


def test_atif_a5_source_digest_domain_consistency_and_fact_set_digest() -> None:
    """source_digest is consistently the upstream source digest; fact_set_digest is domain-separated."""
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "write",
                "tool_calls": [
                    {
                        "tool_call_id": "w1",
                        "function_name": "memory_write",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    }
                ],
            }
        ]
    )
    _, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-domain",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert features.source_digest == _atif_digest()
    assert features.fact_set_digest is not None
    assert features.fact_set_digest != _atif_digest()
    assert features.fact_set_digest.startswith("sha256:")


def test_atif_substring_and_log_text_does_not_link_read_to_use() -> None:
    """Logs, bash commands, message text, or substring argument overlap never link read to use."""
    payload = _atif_payload(
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "write",
                "tool_calls": [
                    {
                        "tool_call_id": "w1",
                        "function_name": "memory_write",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    }
                ],
            },
            {
                "step_id": 2,
                "source": "agent",
                "message": "read",
                "tool_calls": [
                    {
                        "tool_call_id": "r1",
                        "function_name": "memory_read",
                        "arguments": {
                            "payload": PAYLOAD_A.model_dump(mode="json"),
                            "content_digest": DIGEST_A,
                        },
                    }
                ],
            },
            {
                "step_id": 3,
                "source": "agent",
                "message": "echo alpha in bash",
                "tool_calls": [
                    {
                        "tool_call_id": "bash1",
                        "function_name": "run_bash",
                        "arguments": {"cmd": "echo alpha"},
                    }
                ],
                "observation": {"results": [{"source_call_id": "bash1", "content": "alpha"}]},
            },
            {
                "step_id": 4,
                "source": "agent",
                "message": "use different memory",
                "tool_calls": [
                    {
                        "tool_call_id": "u1",
                        "function_name": "memory_use",
                        "arguments": {
                            "payload": PAYLOAD_B.model_dump(mode="json"),
                            "content_digest": DIGEST_B,
                        },
                    }
                ],
            },
        ]
    )
    facts, features = extract_memory_continuity_features_from_atif(
        payload,
        trial_id="trial-no-sub-link",
        source_ref="agent/trajectory.json",
        source_digest=_atif_digest(),
    )
    assert [f.operation for f in facts] == ["memory_write", "memory_read", "memory_use"]
    assert features.memory_continuity_status == "unavailable"
    assert features.write_read_link_count is None
    assert features.write_read_use_link_count is None
    assert features.mean_read_to_use_latency_steps is None
