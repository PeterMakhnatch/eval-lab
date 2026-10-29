"""Synthetic regression tests for the MALT actor projection boundary."""

from __future__ import annotations

import json

import pytest

from evallab.failure_library.bank.actors import ActorUnavailable
from evallab.failure_library.bank.actors.malt import to_atif
from evallab.failure_library.runs import cached

REF = "file:///synthetic/rh-bench.parquet#row=0"
RESPONSE = [
    {"role": "user", "content": "Synthetic task prompt."},
    {"role": "assistant", "content": "First line.\nSecond line: café ✓", "reasoning": "plan A"},
    {"role": "tool", "content": "synthetic observation"},
    {"role": "assistant", "content": [{"type": "text", "text": "Final answer."}]},
]


def _run(response_field: str, value: object, label: str = "hacking"):
    row = {
        "source_dataset": "metr_malt",
        "source_id": "syn-1",
        "label": label,
        "response_model": "synthetic-model",
        "response_hacking": None,
        "response_clean": None,
        response_field: value,
    }
    return cached.redistributed_malt(row, REF), value


def test_projects_only_assistant_text_exactly_as_single_response():
    run, raw = _run("response_hacking", json.dumps(RESPONSE))
    projection = to_atif(raw, run)

    assert projection.scope == "single_response"
    assert projection.doc["schema_version"] == "ATIF-v1.6"
    assert projection.doc["session_id"] == run.run_id
    steps = projection.doc["steps"]
    assert [step["message"] for step in steps] == [
        "First line.\nSecond line: café ✓",
        "Final answer.",
    ]
    assert steps[0]["reasoning_content"] == "plan A"
    assert all(step["source"] == "agent" for step in steps)
    assert "synthetic observation" not in json.dumps(projection.doc)
    assert "Synthetic task prompt." not in json.dumps(projection.doc)


def test_origins_bind_steps_to_native_message_indexes():
    run, raw = _run("response_clean", json.dumps(RESPONSE))
    projection = to_atif(raw, run)

    assert projection.origins == {1: 1, 2: 3}


def test_native_integer_step_ids_are_preserved_when_unique():
    response = [
        {"role": "assistant", "step_id": 7, "content": "alpha"},
        {"role": "user", "step_id": 8, "content": "ignored"},
        {"role": "assistant", "step_id": 9, "content": "beta"},
    ]
    run, raw = _run("response_hacking", json.dumps(response))
    projection = to_atif(raw, run)

    assert [step["step_id"] for step in projection.doc["steps"]] == [7, 9]
    assert projection.origins == {7: 7, 9: 9}


def test_native_evaluator_labels_are_not_projected():
    run, raw = _run(
        "response_hacking",
        json.dumps([{"role": "assistant", "content": "plain reply"}]),
        label="hacking",
    )
    projection = to_atif(raw, run)
    serialized = json.dumps(projection.doc)

    assert "hacking" not in serialized
    assert "reward_hacked" not in serialized
    assert "clean" not in serialized


def test_limitations_state_no_inferred_tool_history():
    run, raw = _run("response_hacking", json.dumps(RESPONSE))
    projection = to_atif(raw, run)

    assert any("single captured response" in note for note in projection.limitations)
    assert any("none are inferred" in note for note in projection.limitations)
    assert "tool_calls" not in json.dumps(projection.doc)


def test_plain_unattributed_text_is_refused():
    run, raw = _run("response_hacking", "The model wrote this without a role.")
    with pytest.raises(ActorUnavailable):
        to_atif(raw, run)


def test_sequence_without_assistant_message_is_refused():
    response = [{"role": "user", "content": "only the prompt"}]
    run, raw = _run("response_clean", json.dumps(response))
    with pytest.raises(ActorUnavailable):
        to_atif(raw, run)


def test_unattributed_message_inside_sequence_is_refused():
    response = [{"role": "assistant", "content": "ok"}, {"content": "no role"}]
    run, raw = _run("response_hacking", json.dumps(response))
    with pytest.raises(ActorUnavailable):
        to_atif(raw, run)


def test_non_malt_run_is_refused():
    run, raw = _run("response_hacking", json.dumps(RESPONSE))
    other = run.model_copy(update={"collection": "cheatbench"})
    with pytest.raises(ActorUnavailable):
        to_atif(raw, other)
