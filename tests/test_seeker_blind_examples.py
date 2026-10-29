"""Blind seeker retrieval: the attacked task, labels, split and source never reach the prompt."""

from __future__ import annotations

from typing import Any

import pytest

from evallab.production.cloud_trace_db import (
    RetrievedExample,
    RetrievedStep,
    format_retrieval_prompt_block,
)
from evallab.production.retrieval_leak_check import rendered_leaks
from evallab.production.seeker_blind_examples import (
    BLIND_ALLOWED_PROVENANCE,
    BLIND_WITHHELD_FIELDS,
    blind_example,
    blind_examples,
    opaque_id,
    render_blind,
)

TARGET = "ns/target-task@v2"
TARGET_NAME = "target-task"


def example(task_id: str = "ns/other-task@v2", **overrides: Any) -> RetrievedExample:
    fields: dict[str, Any] = {
        "task_id": task_id,
        "dataset_id": "example-benchmark-v1",
        "instruction": "solve the target by printing the expected table",
        "differential": "original 1.0 -> repaired 0.0",
        "reproducer": "cat > /app/solve.sh <<'EOF'",
        "defect_class": "wrong_pass",
        "direction": "wrong_pass",
        "status": "confirmed",
        "issue_id": "issue-1",
        "trace_id": "trace-1",
        "provenance": {
            "dataset_id": "example-benchmark-v1",
            "split": "dev",
            "label_categories": ["reward_hack"],
            "receipt_status": "confirmed",
            "trust_tier": "tier1_container_verified",
            "trace_ids": ["trace-1"],
            "step_indices": [3],
        },
    }
    fields.update(overrides)
    return RetrievedExample(**fields)


class FakeRetriever:
    def __init__(self, found: list[RetrievedExample]) -> None:
        self.found = found
        self.calls: list[dict[str, Any]] = []

    def retrieve_similar_examples(self, query: str, *args: Any, **kwargs: Any) -> list[RetrievedExample]:
        self.calls.append({"query": query, "args": args, **kwargs})
        return list(self.found)


def test_blank_target_refuses_before_the_store_is_touched() -> None:
    retriever = FakeRetriever([example()])
    for blank in ("", "   ", None, 7):
        with pytest.raises(ValueError, match="for_task_id"):
            blind_examples(retriever, blank, "weak_check")  # type: ignore[arg-type]
    assert retriever.calls == []


def test_blank_extra_exclusion_refuses() -> None:
    retriever = FakeRetriever([example()])
    with pytest.raises(ValueError, match="exclude_task_ids"):
        blind_examples(retriever, TARGET, "weak_check", exclude_task_ids=("ok", " "))
    assert retriever.calls == []


def test_attacked_task_is_always_excluded() -> None:
    retriever = FakeRetriever([example()])
    blind_examples(retriever, TARGET, "weak_check")
    assert retriever.calls[0]["exclude_task_ids"] == (TARGET,)


def test_extra_exclusions_are_merged_and_deduplicated() -> None:
    retriever = FakeRetriever([example()])
    blind_examples(retriever, TARGET, "weak_check", exclude_task_ids=("a", TARGET, "a"))
    assert retriever.calls[0]["exclude_task_ids"] == (TARGET, "a")


def test_atlas_arguments_pass_through() -> None:
    retriever = FakeRetriever([example()])
    facets = {"outcome": "pass", "grader_or_harness_touched": True}
    blind_examples(retriever, TARGET, "weak_check", facets=facets, cluster_id="c1", like_run_id="run-1")
    call = retriever.calls[0]
    assert call["facets"] == facets and call["cluster_id"] == "c1" and call["like_run_id"] == "run-1"


@pytest.mark.parametrize("task_id", [TARGET, TARGET_NAME, "other/ns/target-task", "target-task@v9"])
def test_a_same_task_example_is_dropped_even_if_returned(task_id: str) -> None:
    retriever = FakeRetriever([example(task_id=task_id), example(task_id="ns/different@v2")])
    found = blind_examples(retriever, TARGET, "weak_check")
    assert [item.task_id for item in found] == [opaque_id("example", "ns/different@v2")]


@pytest.mark.parametrize("field", BLIND_WITHHELD_FIELDS)
def test_answer_and_prompt_fields_are_withheld(field: str) -> None:
    blinded = blind_example(example())
    assert getattr(blinded, field) is None


def test_only_allowlisted_provenance_survives_including_a_key_nobody_thought_of() -> None:
    labelled = example(provenance={
        **example().provenance, "run_id": "example-benchmark-v1@sha/run", "flagged_step_indices": [3],
        "passage_window": True, "model_id": "model-x", "something_stored_next_year": "example-benchmark-v1/zzz",
    })
    blinded = blind_example(labelled)
    assert set(blinded.provenance) == {"step_indices", "model_id"}
    assert set(blinded.provenance) <= BLIND_ALLOWED_PROVENANCE
    assert rendered_leaks([labelled], blinded.prompt_text, "blind") == []


def test_source_attributes_are_cleared_not_only_provenance() -> None:
    blinded = blind_example(example())
    assert blinded.dataset_id == ""
    assert blinded.split == ""
    assert blinded.trust_tier is None
    assert "example-benchmark-v1" not in blinded.prompt_text


def _leaky() -> RetrievedExample:
    return example(
        task_id="example-benchmark-v1/reward-task@v2", trace_id="example-benchmark-v1/run/abc",
        citation="tests/example-benchmark-v1/check.py:3",
        issue={"reproducer_script": "cat /logs/verifier/reward.txt # reward", "task_id": "example-benchmark-v1/reward-task@v2"},
        trace_steps=[
            {"step_index": 3, "role": "assistant", "command_or_action": "ls", "flagged": True,
             "trace_id": "example-benchmark-v1/run/abc", "observation_cas_uri": "cas://sha256/ab"},
        ],
        steps=(
            RetrievedStep(step_index=3, role="assistant", tool_name="bash", command_or_action="ls", observation=None, flagged=True),
        ),
        provenance={
            **example().provenance, "receipt_status": "receipt-marker", "run_id": "example-benchmark-v1@sha/run",
            "flagged_step_indices": [3], "passage_window": True,
        },
    )


def test_rendered_prompt_passes_the_blind_leak_check_where_the_raw_block_fails_it() -> None:
    raw = _leaky()
    assert rendered_leaks([raw], raw.prompt_text, "blind")  # the unblinded block is exactly what the check must catch
    (blinded,) = blind_examples(FakeRetriever([raw]), TARGET, "weak_check")
    assert rendered_leaks([raw], blinded.prompt_text, "blind") == []
    assert "ls" in blinded.prompt_text  # the evidence a seeker may use is still there


def test_the_compact_card_of_a_blinded_example_leaks_neither_reproducer_nor_citation() -> None:
    raw = _leaky()
    card = format_retrieval_prompt_block([blind_example(raw)], format_mode="compact_cards")
    assert rendered_leaks([raw], card, "blind") == []
    assert "reward.txt" not in card and "check.py" not in card and "[None]" not in card


def test_the_structured_blinded_example_keeps_no_issue_citation_or_step_identity() -> None:
    blinded = blind_example(_leaky())
    assert blinded.issue is None and blinded.citation is None
    assert blinded.trace_steps == [{"step_index": 3, "role": "assistant", "command_or_action": "ls", "flagged": False}]


def test_render_blind_blinds_examples_it_is_handed() -> None:
    text = render_blind([example()])
    assert "example-benchmark-v1" not in text
    assert "expected table" not in text


def test_blinding_is_idempotent_and_leaves_the_input_untouched() -> None:
    original = example()
    once = blind_example(original)
    twice = blind_example(once)
    assert once == twice
    assert original.instruction is not None and original.provenance["split"] == "dev"
