from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evallab.interpretation.producers.memgym import (
    extract_context_operation_facts_from_memgym,
    extract_memgym_outcome,
)
from evallab.interpretation.producers.memory_continuity import (
    extract_memory_continuity_features,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "memgym"


def _read_fixture_bytes(filename: str) -> bytes:
    path = FIXTURES_DIR / filename
    return path.read_bytes()


def test_memgym_released_fixture_provenance_and_digests() -> None:
    attribution_path = FIXTURES_DIR / "ATTRIBUTION.json"
    assert attribution_path.exists(), "ATTRIBUTION.json must exist"

    attribution = json.loads(attribution_path.read_text(encoding="utf-8"))
    assert attribution["upstream_commit"] == "50b404e6ae4e1fcd453d3e07963eb3e6312cbded"
    assert attribution["upstream_tree"] == "68c081f0271cfd7951e490afd59457b029ba0535"
    assert attribution["upstream_license"] == "Apache-2.0"

    for filename, meta in attribution["files"].items():
        file_path = FIXTURES_DIR / filename
        assert file_path.exists(), f"Fixture file {filename} must exist"
        data = file_path.read_bytes()
        digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
        assert digest == meta["sha256"], f"Digest mismatch for {filename}"
        assert len(data) == meta["bytes"], f"Byte size mismatch for {filename}"


def test_memgym_extract_context_operation_facts_from_released_fixture() -> None:
    training_path = FIXTURES_DIR / "0_training.json"
    expected_digest = "sha256:85c55f353ec12712d0d208c401fa6dbedfdbabe4314d6f879656d4f49629680f"

    facts = extract_context_operation_facts_from_memgym(
        training_path,
        source_ref="tests/fixtures/memgym/0_training.json",
        expected_source_digest=expected_digest,
    )

    assert len(facts) == 7
    expected_trial_id = (
        "memgym:trial:cf864f352b14a9c33c44ff1c3a12dab0774695d946ebda5709f7e41c323eb08e"
    )
    assert all(fact.trial_id == expected_trial_id for fact in facts)

    # Step index must follow msg_index total ordering (interleaved user/agent turns),
    # NOT step which restarts per side [0, 1, 2, 3, 4, 0, 1]
    step_indices = [fact.step_index for fact in facts]
    assert step_indices == [1, 2, 4, 5, 6, 8, 10]

    session_ids = [fact.session_id for fact in facts]
    assert session_ids == ["user", "agent", "agent", "user", "agent", "agent", "agent"]

    # All operations in this released fixture are session boundaries
    assert all(fact.operation == "session_boundary" for fact in facts)
    assert all(fact.content_digest is None for fact in facts)
    assert all(fact.context_position_tokens is None for fact in facts)
    assert all(fact.configured_size is None for fact in facts)
    assert all(fact.realized_size is None for fact in facts)
    assert all(fact.source_digest == expected_digest for fact in facts)

    # Verify operation_id domain-separated composite structure
    assert all(fact.operation_id.startswith("memgym:op:") for fact in facts)

    # Verify token counts for the first step (msg_index=1, user turn)
    user_fact = facts[0]
    assert user_fact.step_index == 1
    assert user_fact.session_id == "user"
    assert user_fact.before_token_count == 9
    assert user_fact.after_token_count == 9
    assert user_fact.prompt_tokens == 0  # Preserved exact 0!

    # Verify token counts for the second step (msg_index=2, agent turn)
    agent_fact = facts[1]
    assert agent_fact.step_index == 2
    assert agent_fact.session_id == "agent"
    assert agent_fact.before_token_count == 150
    assert agent_fact.after_token_count == 150
    assert agent_fact.prompt_tokens == 0  # Preserved exact 0!


def test_memgym_extract_outcome_from_released_fixture() -> None:
    training_path = FIXTURES_DIR / "0_training.json"
    result_path = FIXTURES_DIR / "result.json"
    expected_result_digest = (
        "sha256:c74cd64ec2fdff2cfb107ddfc14f9b4b135f83038b971a893c1e47d20ac1d4c5"
    )

    outcome = extract_memgym_outcome(
        training_path,
        result_path,
        result_source_ref="tests/fixtures/memgym/result.json",
        expected_source_digest=expected_result_digest,
    )

    assert outcome.domain == "retail"
    assert outcome.task_id == "0"
    assert (
        outcome.trial_id
        == "memgym:trial:cf864f352b14a9c33c44ff1c3a12dab0774695d946ebda5709f7e41c323eb08e"
    )
    assert outcome.episode_reward == 0.0
    assert outcome.episode_outcome == "unresolved"
    assert outcome.result_reward == 0.0
    assert outcome.result_success is False
    assert outcome.evaluation_status == "unavailable"  # Verifier fields are all null in fixture
    assert outcome.provenance_source == "tests/fixtures/memgym/result.json"
    assert outcome.source_digest == expected_result_digest


def test_memgym_extract_outcome_without_result_binds_to_training() -> None:
    training_path = FIXTURES_DIR / "0_training.json"
    expected_training_digest = (
        "sha256:85c55f353ec12712d0d208c401fa6dbedfdbabe4314d6f879656d4f49629680f"
    )

    outcome = extract_memgym_outcome(
        training_path,
        result_bytes=None,
        training_source_ref="tests/fixtures/memgym/0_training.json",
    )

    assert outcome.domain == "retail"
    assert outcome.task_id == "0"
    assert (
        outcome.trial_id
        == "memgym:trial:cf864f352b14a9c33c44ff1c3a12dab0774695d946ebda5709f7e41c323eb08e"
    )
    assert outcome.episode_reward == 0.0
    assert outcome.episode_outcome == "unresolved"
    assert outcome.result_reward is None
    assert outcome.result_success is None
    assert outcome.evaluation_status == "unavailable"
    assert outcome.provenance_source == "tests/fixtures/memgym/0_training.json"
    assert outcome.source_digest == expected_training_digest


def test_memgym_facts_through_memory_continuity_producer() -> None:
    training_path = FIXTURES_DIR / "0_training.json"
    facts = extract_context_operation_facts_from_memgym(
        training_path,
        source_ref="tests/fixtures/memgym/0_training.json",
    )

    (features,) = extract_memory_continuity_features(facts)

    assert (
        features.trial_id
        == "memgym:trial:cf864f352b14a9c33c44ff1c3a12dab0774695d946ebda5709f7e41c323eb08e"
    )
    assert features.memory_continuity_status == "observed"
    assert features.context_boundary_count == 7
    assert features.memory_write_count == 0
    assert features.memory_read_count == 0
    assert features.memory_use_count == 0
    assert features.write_read_link_count == 0
    assert features.write_read_use_link_count == 0
    assert features.mean_write_to_read_latency_steps is None
    assert features.mean_read_to_use_latency_steps is None
    assert features.boundary_carryover_opportunity_count == 0
    assert features.boundary_carryover_success_count == 0
    assert features.boundary_carryover_rate is None
    assert features.fact_set_digest is not None


def test_memgym_representation_order_invariance() -> None:
    training_data = json.loads(_read_fixture_bytes("0_training.json"))
    raw_forward = json.dumps(training_data).encode("utf-8")

    # Reverse the steps in the source payload
    reversed_payload = dict(training_data)
    reversed_payload["steps"] = list(reversed(training_data["steps"]))
    raw_reversed = json.dumps(reversed_payload).encode("utf-8")

    facts_forward = extract_context_operation_facts_from_memgym(
        raw_forward,
        source_ref="test_invariance.json",
    )

    facts_reversed = extract_context_operation_facts_from_memgym(
        raw_reversed,
        source_ref="test_invariance.json",
    )

    assert len(facts_forward) == len(facts_reversed)
    # Check that each fact's operation_id and step_index match exactly
    for f1, f2 in zip(facts_forward, facts_reversed, strict=True):
        assert f1.step_index == f2.step_index
        assert f1.operation_id == f2.operation_id
        assert f1.session_id == f2.session_id
        assert f1.trial_id == f2.trial_id


# ==============================================================================
# M1 Adversaries: Native Identity Types and Collision-Free Structured Composites
# ==============================================================================


def test_memgym_m1_native_identity_types_and_delimiter_collision() -> None:
    # 1. Integer 0 vs string "0" produce distinct trial IDs
    bytes_int_0 = json.dumps(
        {"domain": "retail", "task_id": 0, "steps": [{"side": "agent", "msg_index": 1}]}
    ).encode("utf-8")
    bytes_str_0 = json.dumps(
        {"domain": "retail", "task_id": "0", "steps": [{"side": "agent", "msg_index": 1}]}
    ).encode("utf-8")

    facts_int_0 = extract_context_operation_facts_from_memgym(bytes_int_0)
    facts_str_0 = extract_context_operation_facts_from_memgym(bytes_str_0)

    assert facts_int_0[0].trial_id != facts_str_0[0].trial_id
    assert facts_int_0[0].operation_id != facts_str_0[0].operation_id

    outcome_int = extract_memgym_outcome(bytes_int_0)
    outcome_str = extract_memgym_outcome(bytes_str_0)
    assert outcome_int.task_id == 0
    assert outcome_str.task_id == "0"
    assert outcome_int.trial_id != outcome_str.trial_id
    assert (
        outcome_int.trial_id
        == "memgym:trial:ba67a2163d238887110aa88c62408e34becc723a901b1c450ebecaa1d404582d"
    )
    assert (
        outcome_str.trial_id
        == "memgym:trial:cf864f352b14a9c33c44ff1c3a12dab0774695d946ebda5709f7e41c323eb08e"
    )
    # 2. Delimiter collision: (domain='a:b', task_id='c') vs (domain='a', task_id='b:c')
    bytes_ab_c = json.dumps(
        {"domain": "a:b", "task_id": "c", "steps": [{"side": "agent", "msg_index": 1}]}
    ).encode("utf-8")
    bytes_a_bc = json.dumps(
        {"domain": "a", "task_id": "b:c", "steps": [{"side": "agent", "msg_index": 1}]}
    ).encode("utf-8")

    facts_ab_c = extract_context_operation_facts_from_memgym(bytes_ab_c)
    facts_a_bc = extract_context_operation_facts_from_memgym(bytes_a_bc)

    assert facts_ab_c[0].trial_id != facts_a_bc[0].trial_id
    assert facts_ab_c[0].operation_id != facts_a_bc[0].operation_id

    # 3. Training integer 0 vs result string "0" must refuse conflicting identity
    with pytest.raises(ValueError, match="Task identity mismatch"):
        extract_memgym_outcome(bytes_int_0, bytes_str_0)

    # 4. Invalid task_id types fail closed
    bytes_bool_task = json.dumps({"domain": "retail", "task_id": True, "steps": []}).encode("utf-8")
    with pytest.raises(
        ValueError, match="task_id must be a non-empty string or non-negative integer"
    ):
        extract_context_operation_facts_from_memgym(bytes_bool_task)

    bytes_float_task = json.dumps({"domain": "retail", "task_id": 1.5, "steps": []}).encode("utf-8")
    with pytest.raises(
        ValueError, match="task_id must be a non-empty string or non-negative integer"
    ):
        extract_context_operation_facts_from_memgym(bytes_float_task)


# ==============================================================================
# M2 Adversary: Remove Caller Identity Substitution
# ==============================================================================


def test_memgym_m2_remove_caller_identity_substitution() -> None:
    training_path = FIXTURES_DIR / "0_training.json"

    # Caller cannot pass arbitrary trial_id override
    with pytest.raises(
        ValueError,
        match="Derived trial_id .* does not match expected_trial_id 'path:list-position:7'",
    ):
        extract_context_operation_facts_from_memgym(
            training_path,
            expected_trial_id="path:list-position:7",
        )

    with pytest.raises(
        ValueError,
        match="Derived trial_id .* does not match expected_trial_id 'path:list-position:7'",
    ):
        extract_memgym_outcome(
            training_path,
            expected_trial_id="path:list-position:7",
        )


# ==============================================================================
# M3 Adversaries: Exact Side / Session Admission
# ==============================================================================


@pytest.mark.parametrize(
    "invalid_side",
    [
        " agent ",
        "agent\n",
        "Agent",
        "AGENT",
        " user ",
        "User",
        "USER",
        "",
        123,
        None,
        True,
    ],
)
def test_memgym_m3_exact_side_refuses_whitespace_and_casing(invalid_side: object) -> None:
    data = {
        "domain": "retail",
        "task_id": "0",
        "steps": [{"side": invalid_side, "msg_index": 1}],
    }
    raw = json.dumps(data).encode("utf-8")
    with pytest.raises(ValueError, match=r"steps\[0\]\.side must be an exact string member of"):
        extract_context_operation_facts_from_memgym(raw)


# ==============================================================================
# M4 Adversaries: Direct Prompt Token Semantics
# ==============================================================================


def test_memgym_m4_prompt_tokens_exact_semantics() -> None:
    def _make_step(prompt_tokens: object, *, present: bool = True) -> bytes:
        step: dict[str, object] = {"side": "agent", "msg_index": 1}
        if present:
            step["memory"] = {"summarizer_prompt_tokens": prompt_tokens}
        else:
            step["memory"] = {}
        return json.dumps({"domain": "retail", "task_id": "0", "steps": [step]}).encode("utf-8")

    # 1. Exact 0 -> preserved as 0
    facts_0 = extract_context_operation_facts_from_memgym(_make_step(0))
    assert facts_0[0].prompt_tokens == 0

    # 2. Positive int -> preserved as int
    facts_7 = extract_context_operation_facts_from_memgym(_make_step(7))
    assert facts_7[0].prompt_tokens == 7

    # 3. Explicit null / None -> None (unavailable)
    facts_none = extract_context_operation_facts_from_memgym(_make_step(None))
    assert facts_none[0].prompt_tokens is None

    # 4. Missing key -> None (unavailable)
    facts_missing = extract_context_operation_facts_from_memgym(_make_step(None, present=False))
    assert facts_missing[0].prompt_tokens is None

    # 5. Bool True -> fail closed (ValueError)
    with pytest.raises(ValueError, match="summarizer_prompt_tokens must be a non-negative integer"):
        extract_context_operation_facts_from_memgym(_make_step(True))

    # 6. String "7" -> fail closed (ValueError)
    with pytest.raises(ValueError, match="summarizer_prompt_tokens must be a non-negative integer"):
        extract_context_operation_facts_from_memgym(_make_step("7"))

    # 7. Float 7.5 -> fail closed (ValueError)
    with pytest.raises(ValueError, match="summarizer_prompt_tokens must be a non-negative integer"):
        extract_context_operation_facts_from_memgym(_make_step(7.5))

    # 8. Negative integer -1 -> fail closed (ValueError)
    with pytest.raises(ValueError, match="summarizer_prompt_tokens must be a non-negative integer"):
        extract_context_operation_facts_from_memgym(_make_step(-1))


# ==============================================================================
# M5 Adversaries: Exact-Byte Source Authority and Digest Validation
# ==============================================================================


def test_memgym_m5_exact_byte_source_authority_and_digest_validation() -> None:
    # 1. Whitespace-different JSON bytes with equal values have distinct source digests
    b1 = b'{"domain":"retail","task_id":"0","steps":[{"msg_index":1,"side":"agent"}]}'
    b2 = b'{\n  "domain": "retail",\n  "task_id": "0",\n  "steps": [\n    {\n      "msg_index": 1,\n      "side": "agent"\n    }\n  ]\n}'

    facts1 = extract_context_operation_facts_from_memgym(b1)
    facts2 = extract_context_operation_facts_from_memgym(b2)

    assert facts1[0].source_digest != facts2[0].source_digest
    assert facts1[0].source_digest == f"sha256:{hashlib.sha256(b1).hexdigest()}"
    assert facts2[0].source_digest == f"sha256:{hashlib.sha256(b2).hexdigest()}"

    # Fact contents and identities are otherwise identical
    assert facts1[0].trial_id == facts2[0].trial_id
    assert facts1[0].operation_id == facts2[0].operation_id
    assert facts1[0].step_index == facts2[0].step_index

    # 2. Expected source digest mismatch raises ValueError
    with pytest.raises(
        ValueError, match="Expected source digest .* does not match computed byte digest"
    ):
        extract_context_operation_facts_from_memgym(
            b1, expected_source_digest="sha256:000000000000"
        )

    # 3. Parsed mapping dict input is refused to enforce exact-byte provenance
    with pytest.raises(TypeError, match="training_bytes must be bytes, str, or Path"):
        extract_context_operation_facts_from_memgym(
            {"domain": "retail"}  # type: ignore[arg-type]
        )


# ==============================================================================
# Fail-Closed and Scope Hold Tests
# ==============================================================================


def test_memgym_fail_closed_duplicate_msg_index() -> None:
    training_data = json.loads(_read_fixture_bytes("0_training.json"))
    bad_data = dict(training_data)
    bad_steps = [dict(s) for s in training_data["steps"]]
    bad_steps[1]["msg_index"] = bad_steps[0]["msg_index"]  # create collision
    bad_data["steps"] = bad_steps

    raw = json.dumps(bad_data).encode("utf-8")
    with pytest.raises(ValueError, match="Duplicate msg_index"):
        extract_context_operation_facts_from_memgym(raw)


def test_memgym_fail_closed_bool_vs_int_in_msg_index() -> None:
    training_data = json.loads(_read_fixture_bytes("0_training.json"))
    bad_data = dict(training_data)
    bad_steps = [dict(s) for s in training_data["steps"]]
    bad_steps[0]["msg_index"] = True  # bool instead of int!
    bad_data["steps"] = bad_steps

    raw = json.dumps(bad_data).encode("utf-8")
    with pytest.raises(ValueError, match="msg_index must be a non-negative integer"):
        extract_context_operation_facts_from_memgym(raw)


def test_memgym_compaction_lacks_ordered_indices_refuses_positive_payload() -> None:
    """Synthetic unit test: compaction event without ordered indices must NOT synthesize payload."""
    data = {
        "domain": "retail",
        "task_id": "0",
        "steps": [
            {
                "step": 0,
                "turn_idx": 1,
                "msg_index": 2,
                "side": "agent",
                "memory": {
                    "was_compacted": True,
                    "new_compaction": True,
                    "original_tokens": 500,
                    "filtered_tokens": 200,
                    "summary": "Compacted summary text",
                    "forgotten": 3,
                },
            }
        ],
    }
    raw = json.dumps(data).encode("utf-8")
    facts = extract_context_operation_facts_from_memgym(raw, source_ref="compaction_test.json")
    assert len(facts) == 1
    assert facts[0].operation == "compaction"
    assert facts[0].content_digest is None  # Payload digest is refused / typed unavailable!
    assert facts[0].before_token_count == 500
    assert facts[0].after_token_count == 200


# ==============================================================================
# S1 Adversaries: Exact Outcome String or Null (No str Coercion)
# ==============================================================================


def test_memgym_s1_episode_outcome_exact_string_or_null() -> None:
    def _make_training_with_outcome(outcome: object, *, present: bool = True) -> bytes:
        payload: dict[str, object] = {"domain": "retail", "task_id": "0", "steps": []}
        if present:
            payload["episode_outcome"] = outcome
        return json.dumps(payload).encode("utf-8")

    # 1. Exact valid string -> preserved byte-decoded value exactly
    res_str = extract_memgym_outcome(_make_training_with_outcome("unresolved"))
    assert res_str.episode_outcome == "unresolved"

    res_custom = extract_memgym_outcome(_make_training_with_outcome("success_with_drift"))
    assert res_custom.episode_outcome == "success_with_drift"

    # 2. Explicit null / None -> None (unavailable)
    res_none = extract_memgym_outcome(_make_training_with_outcome(None))
    assert res_none.episode_outcome is None

    # 3. Missing key -> None (unavailable)
    res_missing = extract_memgym_outcome(_make_training_with_outcome(None, present=False))
    assert res_missing.episode_outcome is None

    # 4. Bool -> fail closed (ValueError)
    with pytest.raises(ValueError, match="episode_outcome must be a string or null"):
        extract_memgym_outcome(_make_training_with_outcome(True))

    with pytest.raises(ValueError, match="episode_outcome must be a string or null"):
        extract_memgym_outcome(_make_training_with_outcome(False))

    # 5. Integer -> fail closed (ValueError)
    with pytest.raises(ValueError, match="episode_outcome must be a string or null"):
        extract_memgym_outcome(_make_training_with_outcome(7))

    # 6. Float -> fail closed (ValueError)
    with pytest.raises(ValueError, match="episode_outcome must be a string or null"):
        extract_memgym_outcome(_make_training_with_outcome(0.5))

    # 7. List -> fail closed (ValueError)
    with pytest.raises(ValueError, match="episode_outcome must be a string or null"):
        extract_memgym_outcome(_make_training_with_outcome(["unresolved"]))

    # 8. Dict -> fail closed (ValueError), never Python repr string
    with pytest.raises(ValueError, match="episode_outcome must be a string or null"):
        extract_memgym_outcome(_make_training_with_outcome({"state": "unresolved"}))


# ==============================================================================
# S2 Adversaries: Exact Compaction Bool (Refuse Malformed Values Before Mapping)
# ==============================================================================


def test_memgym_s2_compaction_exact_bool_and_malformed_refusal() -> None:
    def _make_step_compaction(new_compaction: object, *, present: bool = True) -> bytes:
        step: dict[str, object] = {"side": "agent", "msg_index": 1}
        if present:
            step["memory"] = {"new_compaction": new_compaction}
        else:
            step["memory"] = {}
        return json.dumps({"domain": "retail", "task_id": "0", "steps": [step]}).encode("utf-8")

    # 1. True -> maps to compaction with content_digest=None
    facts_true = extract_context_operation_facts_from_memgym(_make_step_compaction(True))
    assert len(facts_true) == 1
    assert facts_true[0].operation == "compaction"
    assert facts_true[0].content_digest is None

    # 2. False -> maps to session_boundary
    facts_false = extract_context_operation_facts_from_memgym(_make_step_compaction(False))
    assert len(facts_false) == 1
    assert facts_false[0].operation == "session_boundary"

    # 3. Explicit null / None -> maps to session_boundary
    facts_none = extract_context_operation_facts_from_memgym(_make_step_compaction(None))
    assert len(facts_none) == 1
    assert facts_none[0].operation == "session_boundary"

    # 4. Missing key -> maps to session_boundary
    facts_missing = extract_context_operation_facts_from_memgym(
        _make_step_compaction(None, present=False)
    )
    assert len(facts_missing) == 1
    assert facts_missing[0].operation == "session_boundary"

    # 5. Malformed integers (1 or 0) -> fail closed (ValueError), never positive or negative boundary
    with pytest.raises(
        ValueError, match=r"steps\[0\]\.memory\.new_compaction must be a boolean or null"
    ):
        extract_context_operation_facts_from_memgym(_make_step_compaction(1))

    with pytest.raises(
        ValueError, match=r"steps\[0\]\.memory\.new_compaction must be a boolean or null"
    ):
        extract_context_operation_facts_from_memgym(_make_step_compaction(0))

    # 6. Malformed strings ("true" or "false") -> fail closed (ValueError)
    with pytest.raises(
        ValueError, match=r"steps\[0\]\.memory\.new_compaction must be a boolean or null"
    ):
        extract_context_operation_facts_from_memgym(_make_step_compaction("true"))

    with pytest.raises(
        ValueError, match=r"steps\[0\]\.memory\.new_compaction must be a boolean or null"
    ):
        extract_context_operation_facts_from_memgym(_make_step_compaction("false"))

    # 7. Malformed float -> fail closed (ValueError)
    with pytest.raises(
        ValueError, match=r"steps\[0\]\.memory\.new_compaction must be a boolean or null"
    ):
        extract_context_operation_facts_from_memgym(_make_step_compaction(1.0))

    # 8. Malformed list / dict -> fail closed (ValueError)
    with pytest.raises(
        ValueError, match=r"steps\[0\]\.memory\.new_compaction must be a boolean or null"
    ):
        extract_context_operation_facts_from_memgym(_make_step_compaction([True]))

    with pytest.raises(
        ValueError, match=r"steps\[0\]\.memory\.new_compaction must be a boolean or null"
    ):
        extract_context_operation_facts_from_memgym(_make_step_compaction({"compacted": True}))

    # 9. Malformed was_compacted -> fail closed (ValueError)
    step_bad_was_compacted = {
        "domain": "retail",
        "task_id": "0",
        "steps": [{"side": "agent", "msg_index": 1, "memory": {"was_compacted": 1}}],
    }
    with pytest.raises(
        ValueError, match=r"steps\[0\]\.memory\.was_compacted must be a boolean or null"
    ):
        extract_context_operation_facts_from_memgym(
            json.dumps(step_bad_was_compacted).encode("utf-8")
        )
