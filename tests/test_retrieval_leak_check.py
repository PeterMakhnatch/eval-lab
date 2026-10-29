"""The rendered-prompt leak check: what it catches, what it deliberately does not gate."""

from __future__ import annotations

from typing import Any

import pytest

from evallab.production.cloud_trace_db import (
    RetrievedExample,
    RetrievedStep,
    format_retrieval_prompt_block,
)
from evallab.production.retrieval_leak_check import rendered_leaks
from evallab.production.seeker_blind_examples import blind_example

DATASET = "reward-hack-corpus"


def labelled(**overrides: Any) -> RetrievedExample:
    fields: dict[str, Any] = {
        "task_id": f"{DATASET}/task-a@v1",
        "dataset_id": DATASET,
        "issue_id": f"{DATASET}/issue-1",
        "trace_id": f"{DATASET}/trace-1",
        "defect_class": "wrong_pass",
        "direction": "wrong_pass",
        "status": "confirmed",
        "differential": "original reward 1 -> repaired reward 0",
        "instruction": "make the checks pass",
        "trust_tier": "tier2_consensus_labeled",
        "model_id": "model-x",
        "outcome": "wrong_pass",
        "steps": (
            RetrievedStep(3, "assistant", "bash", "pytest -q", None, flagged=True),
            RetrievedStep(4, "tool", "bash", None, f"{DATASET} collected 3 items"),
        ),
        "provenance": {
            "dataset_id": DATASET, "split": "dev", "label_categories": ["category-marker"],
            "receipt_status": "receipt-marker", "run_id": f"{DATASET}@sha/run",
            "trace_id": f"{DATASET}/trace-1", "task_id": f"{DATASET}/task-a@v1",
            "flagged_step_indices": [3], "passage_window": True, "step_indices": [3, 4],
        },
    }
    fields.update(overrides)
    return RetrievedExample(**fields)


def test_the_shared_block_passes_its_profile_and_names_what_it_still_shows() -> None:
    example = labelled()
    assert rendered_leaks([example], example.prompt_text, "shared") == []
    # The blind profile is stricter by design: the shared block keeps identifiers,
    # the passage marker and the differential for QA workers.
    blind = rendered_leaks([example], example.prompt_text, "blind")
    assert "token FLAGGED" in blind and "token reward" in blind
    assert f"dataset name {DATASET}" in blind and f"original identifier {DATASET}/task-a@v1" in blind
    assert "field flagged_step_indices" in blind


def test_the_blind_adapter_output_passes_the_blind_profile() -> None:
    example = labelled()
    blinded = blind_example(example)
    assert rendered_leaks([example], blinded.prompt_text, "blind") == []
    assert rendered_leaks([example], blinded.prompt_text, "shared") == []
    assert "pytest -q" in blinded.prompt_text and "collected 3 items" in blinded.prompt_text


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ('- provenance: {"dataset_id":"x"}', "field dataset_id"),
        ('- provenance: {"split":"dev"}', "field split"),
        ('- provenance: {"label_categories":["a"]}', "field label_categories"),
        ('- provenance: {"receipt_status":"x"}', "field receipt_status"),
        ("Retrieved from the dev split of the Cloud Trace DB.", "token dev split"),
        (f"- source: dataset {DATASET}; issue i", f"dataset name {DATASET}"),
        ("- mechanism: category-marker", "label category category-marker"),
        ("- verify: receipt receipt-marker", "receipt status receipt-marker"),
    ],
)
def test_each_forbidden_channel_is_caught_in_the_shared_profile(text: str, leak: str) -> None:
    example = labelled()
    assert leak in rendered_leaks([example], f"### 1. x\n{text}", "shared")


def test_values_match_whole_words_not_substrings() -> None:
    example = labelled(provenance={"receipt_status": "verified"}, dataset_id="data")
    assert rendered_leaks([example], "trust: tier1_container_verified; metadata", "shared") == []
    assert "receipt status verified" in rendered_leaks([example], "- verify: receipt verified", "shared")


def test_a_clipped_namespaced_identifier_is_an_identifier_but_a_bare_dataset_name_is_not() -> None:
    example = labelled()
    clipped = f'- provenance: {{"run_id":"{DATASET}@bd8fb473568b8ef1d12f…'
    assert rendered_leaks([example], clipped, "shared") == []
    assert f"dataset name {DATASET}" in rendered_leaks([example], f"- source: from {DATASET} data", "shared")
    assert f"dataset name {DATASET}" in rendered_leaks([example], clipped, "blind")


def test_a_receipt_status_shown_as_the_issue_status_or_outcome_is_not_the_receipt_channel() -> None:
    example = labelled(provenance={"receipt_status": "confirmed"}, status="confirmed")
    assert rendered_leaks([example], "### 1. t (wrong_pass, confirmed)", "shared") == []
    separate = labelled(provenance={"receipt_status": "confirmed"}, status="other", outcome="other")
    assert "receipt status confirmed" in rendered_leaks([separate], "- verify: receipt confirmed", "shared")


@pytest.mark.parametrize(
    "variant", ("terminal-wrench-v2/x.py", "Terminal-Wrench", "my-terminal-wrench", "TERMINAL-WRENCH"),
)
def test_a_dataset_name_is_found_in_case_and_hyphen_variants(variant: str) -> None:
    example = labelled(dataset_id="terminal-wrench", provenance={})
    assert "dataset name terminal-wrench" in rendered_leaks([example], f"- citation: {variant}", "shared")
    assert rendered_leaks([example], "- citation: terminal-wrenchers", "shared") == []


def test_a_short_identifier_does_not_blank_a_dataset_name_or_match_inside_words() -> None:
    example = labelled(task_id="corpus", dataset_id="reward-hack-corpus", provenance={}, issue_id="7", trace_id=None)
    assert "dataset name reward-hack-corpus" in rendered_leaks([example], "- source: from reward-hack-corpus data", "shared")
    blind = rendered_leaks([example], "- step 17 of abc", "blind")
    assert not [leak for leak in blind if leak.startswith("original identifier")]


def test_the_blocks_own_header_words_are_not_an_examples_receipt_status() -> None:
    example = labelled(provenance={"receipt_status": "verified"}, status="other", outcome="other")
    assert rendered_leaks([example], format_retrieval_prompt_block([example]), "shared") == []


def test_a_bracket_in_a_role_or_tool_name_cannot_hide_text_in_the_step_body() -> None:
    example = labelled(steps=(RetrievedStep(4, "tool", f"bash] {DATASET} [x", "ls", None),), provenance={})
    text = "\n".join(example.prompt_lines(1))
    assert f"dataset name {DATASET}" in rendered_leaks([example], text, "shared")


def test_a_label_equal_to_the_defect_class_or_direction_is_rendered_on_purpose() -> None:
    example = labelled(provenance={"label_categories": ["wrong_pass", "category-marker"]})
    assert rendered_leaks([example], "### 1. t - wrong_pass (wrong_pass, confirmed)", "shared") == []
    assert "label category category-marker" in rendered_leaks([example], "- mechanism: category-marker", "shared")


def test_step_bodies_are_evidence_and_only_reported_on_request() -> None:
    example = labelled(steps=(RetrievedStep(4, "tool", "bash", None, f"{DATASET} category-marker"),))
    text = "\n".join(example.prompt_lines(1))
    assert rendered_leaks([example], text, "shared") == []
    reported = rendered_leaks([example], text, "shared", include_content=True)
    assert f"dataset name {DATASET}" in reported and "label category category-marker" in reported


def test_an_example_without_values_to_hide_is_not_matched_on_nothing() -> None:
    example = labelled(dataset_id="", provenance={})
    assert rendered_leaks([example], format_retrieval_prompt_block([example]), "shared") == []


def test_unknown_profile_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown profile"):
        rendered_leaks([labelled()], "text", "strict")  # type: ignore[arg-type]
