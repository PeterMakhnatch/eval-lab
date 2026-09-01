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


def _read_fixture_json(filename: str) -> dict[str, object]:
    path = FIXTURES_DIR / filename
    return json.loads(path.read_text(encoding="utf-8"))


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
    training_data = _read_fixture_json("0_training.json")
    facts = extract_context_operation_facts_from_memgym(
        training_data,
        source_ref="tests/fixtures/memgym/0_training.json",
        source_digest="sha256:85c55f353ec12712d0d208c401fa6dbedfdbabe4314d6f879656d4f49629680f",
    )

    assert len(facts) == 7
    assert all(fact.trial_id == "memgym:retail:0" for fact in facts)

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

    # Verify token counts for the first step (msg_index=1, user turn)
    user_fact = facts[0]
    assert user_fact.step_index == 1
    assert user_fact.session_id == "user"
    assert user_fact.before_token_count == 9
    assert user_fact.after_token_count == 9
    assert user_fact.prompt_tokens is None

    # Verify token counts for the second step (msg_index=2, agent turn)
    agent_fact = facts[1]
    assert agent_fact.step_index == 2
    assert agent_fact.session_id == "agent"
    assert agent_fact.before_token_count == 150
    assert agent_fact.after_token_count == 150
    assert agent_fact.prompt_tokens is None


def test_memgym_extract_outcome_from_released_fixture() -> None:
    training_data = _read_fixture_json("0_training.json")
    result_data = _read_fixture_json("result.json")

    outcome = extract_memgym_outcome(
        training_data,
        result_data,
        source_ref="tests/fixtures/memgym/result.json",
        source_digest="sha256:c74cd64ec2fdff2cfb107ddfc14f9b4b135f83038b971a893c1e47d20ac1d4c5",
    )

    assert outcome.domain == "retail"
    assert outcome.task_id == "0"
    assert outcome.trial_id == "memgym:retail:0"
    assert outcome.episode_reward == 0.0
    assert outcome.episode_outcome == "unresolved"
    assert outcome.result_reward == 0.0
    assert outcome.result_success is False
    assert outcome.evaluation_status == "unavailable"  # Verifier fields are all null in fixture


def test_memgym_facts_through_memory_continuity_producer() -> None:
    training_data = _read_fixture_json("0_training.json")
    facts = extract_context_operation_facts_from_memgym(
        training_data,
        source_ref="tests/fixtures/memgym/0_training.json",
    )

    (features,) = extract_memory_continuity_features(facts)

    assert features.trial_id == "memgym:retail:0"
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
    training_data = _read_fixture_json("0_training.json")
    fixed_digest = "sha256:85c55f353ec12712d0d208c401fa6dbedfdbabe4314d6f879656d4f49629680f"
    facts_forward = extract_context_operation_facts_from_memgym(
        training_data,
        source_ref="tests/fixtures/memgym/0_training.json",
        source_digest=fixed_digest,
    )

    # Reverse the steps in the source payload
    reversed_payload = dict(training_data)
    reversed_payload["steps"] = list(reversed(training_data["steps"]))

    facts_reversed = extract_context_operation_facts_from_memgym(
        reversed_payload,
        source_ref="tests/fixtures/memgym/0_training.json",
        source_digest=fixed_digest,
    )

    # Must produce the exact same facts in the exact same sorted total order
    assert len(facts_forward) == len(facts_reversed)
    for f1, f2 in zip(facts_forward, facts_reversed, strict=True):
        assert f1.model_dump() == f2.model_dump()


def test_memgym_fail_closed_duplicate_msg_index() -> None:
    training_data = _read_fixture_json("0_training.json")
    bad_data = dict(training_data)
    bad_steps = [dict(s) for s in training_data["steps"]]
    bad_steps[1]["msg_index"] = bad_steps[0]["msg_index"]  # create collision
    bad_data["steps"] = bad_steps

    with pytest.raises(ValueError, match="Duplicate msg_index"):
        extract_context_operation_facts_from_memgym(bad_data, source_ref="bad.json")


def test_memgym_fail_closed_bool_vs_int_in_msg_index() -> None:
    training_data = _read_fixture_json("0_training.json")
    bad_data = dict(training_data)
    bad_steps = [dict(s) for s in training_data["steps"]]
    bad_steps[0]["msg_index"] = True  # bool instead of int!
    bad_data["steps"] = bad_steps

    with pytest.raises(ValueError, match="msg_index must be a non-negative integer"):
        extract_context_operation_facts_from_memgym(bad_data, source_ref="bad.json")


def test_memgym_fail_closed_task_id_mismatch_in_outcome() -> None:
    training_data = _read_fixture_json("0_training.json")
    result_data = _read_fixture_json("result.json")

    bad_result = dict(result_data)
    bad_result["task_id"] = "999"  # mismatch

    with pytest.raises(ValueError, match="Task identity mismatch"):
        extract_memgym_outcome(training_data, bad_result)


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
    facts = extract_context_operation_facts_from_memgym(data, source_ref="compaction_test.json")
    assert len(facts) == 1
    assert facts[0].operation == "compaction"
    assert facts[0].content_digest is None  # Payload digest is refused / typed unavailable!
    assert facts[0].before_token_count == 500
    assert facts[0].after_token_count == 200
