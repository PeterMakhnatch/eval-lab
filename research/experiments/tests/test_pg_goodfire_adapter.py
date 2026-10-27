"""Inert native-node fixtures for the passive Goodfire adapter; no corpus or trace execution."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evallab.edit_signals import step_bash_commands
from evallab.tripwires import scan_trace

REPO = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "pg_goodfire_adapter",
    REPO / "research/experiments/pg-self1-goodfire-20261026/adapter.py",
)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)
adapt_trace = adapter.adapt_trace
TraceAvailabilityError = adapter.TraceAvailabilityError


@pytest.fixture(autouse=True)
def _passive_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("synthetic adapter tests must not open a DB or execute content")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)


def _call(call_id: str, name: str = "bash", arguments: Any = None) -> dict:
    return {
        "id": call_id, "name": name,
        "arguments": {"cmd": "printf fixture-only"} if arguments is None else arguments,
    }


def _node(
    role: str,
    parent: int | None,
    content: str | None = None,
    *,
    sampled: bool = False,
    reasoning: str | None = None,
    calls: list[dict] | None = None,
    reply: str | None = None,
) -> dict:
    return {
        "role": role, "parent": parent, "content": content, "sampled": sampled,
        "reasoning": reasoning, "calls": calls or [], "reply": reply,
    }


def _rows(nodes: list[dict]) -> list[dict]:
    children = [0] * len(nodes)
    for node in nodes:
        if node["parent"] is not None:
            children[node["parent"]] += 1
    path: set[int] = set()
    index: int | None = len(nodes) - 1
    while index is not None:
        path.add(index)
        index = nodes[index]["parent"]
    return [
        {
            "step_index": index, "role": node["role"], "content_text": node["content"],
            "flags_json": json.dumps({
                "schema": "lb1.goodfire.native_node_step/1",
                "native_node_index": index,
                "native_parent": node["parent"], "native_parent_key_present": True,
                "on_final_parent_path": index in path, "native_child_count": children[index],
                "sampled": node["sampled"], "source_role": node["role"],
                "has_reasoning_content": bool(node["reasoning"]),
                "reasoning_content": node["reasoning"], "reasoning_content_key_present": True,
                "content_encoding": "null_or_absent" if node["content"] is None else "string",
                "content_key_present": True, "tool_calls": node["calls"],
                "tool_call_id": node["reply"],
                "native_node_residual": {"publisher_only": "not actor input"},
                "source_judge_flags": [], "source_passages": [],
            }),
        }
        for index, node in enumerate(nodes)
    ]


def _paired_nodes() -> list[dict]:
    original_calls = [
        _call("a", arguments='{"cmd":"printf fixture-only","timeout":3}'),
        _call("b", "python", {"cmd": "value = 'fixture-only'"}),
    ]
    carrier_calls = [
        _call("a", arguments='{ "timeout": 3.0, "cmd": "printf fixture-only" }'),
        _call("b", "python", '{"cmd": "value = \'fixture-only\'"}'),
    ]
    return [
        _node("system", None, "SYSTEM_CONTEXT_SENTINEL"),
        _node("user", 0, "USER_CONTEXT_SENTINEL"),
        _node("assistant", 1, "ORIGINAL_CONTENT_SENTINEL", sampled=True,
              reasoning="ORIGINAL_REASONING_SENTINEL", calls=original_calls),
        _node("assistant", 1, "ORIGINAL_REASONING_SENTINEL\n\nORIGINAL_CONTENT_SENTINEL",
              calls=carrier_calls),
        _node("tool", 3, "BASH_OBSERVATION_SENTINEL", reply="a"),
        _node("tool", 4, "PYTHON_OBSERVATION_SENTINEL", reply="b"),
        _node("user", 5, "FOLLOWUP_CONTEXT_SENTINEL"),
        _node("assistant", 6, "FINAL_CONTENT_SENTINEL", sampled=True),
    ]


def _set_flags(rows: list[dict], index: int, **changes: Any) -> None:
    flags = json.loads(rows[index]["flags_json"])
    flags.update(changes)
    rows[index]["flags_json"] = json.dumps(flags)


def _chain_with_calls(nodes: list[dict]) -> list[dict]:
    return _rows([
        _node("system", None, "Synthetic system context"),
        _node("user", 0, "Synthetic user context"),
        *nodes,
    ])


def test_original_reasoning_calls_observations_and_nonagent_context_are_retained() -> None:
    nodes = _paired_nodes()
    rows = _rows(nodes)
    untouched = copy.deepcopy(rows)
    agents, steps, metadata = adapt_trace(rows)
    assert rows == untouched
    assert [step["step_id"] for step in agents] == [2, 7]
    assert agents[0]["message"] == "ORIGINAL_CONTENT_SENTINEL"
    assert agents[0]["reasoning_content"] == "ORIGINAL_REASONING_SENTINEL"
    assert [call["tool_call_id"] for call in agents[0]["tool_calls"]] == ["a", "b"]
    assert [call["arguments"] for call in agents[0]["tool_calls"]] == [
        {"command": "printf fixture-only", "timeout": 3},
        {"code": "value = 'fixture-only'"},
    ]
    assert [call["extra"]["native_arguments"] for call in agents[0]["tool_calls"]] == [
        call["arguments"] for call in nodes[2]["calls"]
    ]
    assert step_bash_commands(agents[0]) == ["printf fixture-only"]
    assert agents[0]["observation"]["results"] == [
        {"source_call_id": "a", "content": "BASH_OBSERVATION_SENTINEL",
         "extra": {"native_step_id": 4}},
        {"source_call_id": "b", "content": "PYTHON_OBSERVATION_SENTINEL",
         "extra": {"native_step_id": 5}},
    ]
    assert [step["step_id"] for step in steps] == [0, 1, 2, 4, 5, 6, 7]
    assert [step["source"] for step in steps] == [
        "system", "user", "agent", "tool", "tool", "user", "agent",
    ]
    assert [steps[index]["message"] for index in (0, 1, 5)] == [
        "SYSTEM_CONTEXT_SENTINEL", "USER_CONTEXT_SENTINEL", "FOLLOWUP_CONTEXT_SENTINEL",
    ]
    assert steps[3]["extra"] == {
        "parent_step_id": 2, "source_step_id": 2,
        "content_key_present": True, "reasoning_content_key_present": True,
    }
    assert steps[4]["extra"]["parent_step_id"] == 4
    assert all(step in steps for step in agents)
    assert {key: metadata[key] for key in (
        "native_nodes", "normalized_nodes", "sampled_agent_nodes", "carrier_aliases",
        "tool_observations", "argument_projections", "unanswered_calls",
    )} == {
        "native_nodes": 8, "normalized_nodes": 7, "sampled_agent_nodes": 2,
        "carrier_aliases": 1, "tool_observations": 2, "argument_projections": 2,
        "unanswered_calls": 0,
    }
    canonical = json.dumps(steps, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert metadata["normalized_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert "SENTINEL" not in json.dumps(metadata)


@pytest.mark.parametrize("raw", [
    '{"cmd":"printf fixture-only", "timeout":3}',
    '{"timeout":3e0,"cmd":"printf fixture-only"}',
    '{"timeout": 3, "cmd":"printf fixture-\\u006fnly"}',
    {"timeout": 3, "cmd": "printf fixture-only"},
])
def test_carrier_semantic_serialization_equivalence_never_replaces_or_duplicates_actions(
    raw: Any,
) -> None:
    baseline = adapt_trace(_rows(_paired_nodes()))
    nodes = _paired_nodes()
    nodes[3]["calls"][0]["arguments"] = raw
    agents, steps, metadata = adapt_trace(_rows(nodes))
    assert agents == baseline[0]
    assert steps == baseline[1]
    assert metadata["normalized_sha256"] == baseline[2]["normalized_sha256"]
    assert metadata["source_sha256"] != baseline[2]["source_sha256"]


@pytest.mark.parametrize("field", [
    "system", "user", "followup", "content", "reasoning", "arguments", "observation", "call_id",
])
def test_relevant_actor_changes_change_both_source_and_normalized_hashes(field: str) -> None:
    baseline = adapt_trace(_rows(_paired_nodes()))[2]
    nodes = _paired_nodes()
    if field in ("system", "user", "followup", "observation"):
        index = {"system": 0, "user": 1, "followup": 6, "observation": 4}[field]
        nodes[index]["content"] += " changed"
    elif field == "content":
        nodes[2]["content"] += " changed"
        nodes[3]["content"] += " changed"
    elif field == "reasoning":
        nodes[2]["reasoning"] += " changed"
        nodes[3]["content"] = nodes[2]["reasoning"] + "\n" + nodes[2]["content"]
    elif field == "arguments":
        for index in (2, 3):
            nodes[index]["calls"][0]["arguments"] = {"cmd": "printf changed-fixture", "timeout": 3}
    else:
        for index in (2, 3):
            nodes[index]["calls"][0]["id"] = "changed-a"
        nodes[4]["reply"] = "changed-a"
    metadata = adapt_trace(_rows(nodes))[2]
    assert metadata["source_sha256"] != baseline["source_sha256"]
    assert metadata["normalized_sha256"] != baseline["normalized_sha256"]


def test_original_raw_argument_spelling_is_preserved_separately_from_parsed_arguments() -> None:
    baseline = adapt_trace(_rows(_paired_nodes()))
    nodes = _paired_nodes()
    raw = '{ "timeout":3, "cmd": "printf fixture-only" }'
    nodes[2]["calls"][0]["arguments"] = raw
    agents, _steps, metadata = adapt_trace(_rows(nodes))
    assert agents[0]["tool_calls"][0]["arguments"] == baseline[0][0]["tool_calls"][0]["arguments"]
    assert agents[0]["tool_calls"][0]["extra"]["native_arguments"] == raw
    assert metadata["source_sha256"] != baseline[2]["source_sha256"]
    assert metadata["normalized_sha256"] != baseline[2]["normalized_sha256"]


def test_publisher_annotations_cannot_change_hashes_scanner_input_or_findings() -> None:
    rows = _rows(_paired_nodes())
    baseline = adapt_trace(rows)
    for index, row in enumerate(rows):
        row["label_tuple"] = {"verdict": "PUBLISHER_ONLY_SENTINEL"}
        _set_flags(
            rows, index,
            source_judge_flags=[{"quote": "PUBLISHER_ONLY_SENTINEL", "verdict": "yes"}],
            source_passages=[{"text": "PUBLISHER_ONLY_SENTINEL", "step": index}],
            labels={"reasoning_content": "PUBLISHER_ONLY_SENTINEL"},
            native_node_residual={"message": {"content": "PUBLISHER_ONLY_SENTINEL"}},
        )
    relabeled = adapt_trace(rows)
    assert relabeled == baseline
    assert "PUBLISHER_ONLY_SENTINEL" not in json.dumps(relabeled)
    assert scan_trace(relabeled[0], steps=relabeled[1], trial_dir=None) == scan_trace(
        baseline[0], steps=baseline[1], trial_dir=None,
    )


def test_actual_scanner_accepts_full_steps_and_missing_grader_context_stays_unavailable() -> None:
    agents, steps, _metadata = adapt_trace(_rows(_paired_nodes()))
    report = scan_trace(agents, steps=steps, trial_dir=None)
    assert report["structural_context"] == {"provided": False, "keys": [], "dropped_records": 0}
    grader_checks = [record for record in report["structural_checks"] if record["requires"] == ["grader"]]
    assert grader_checks
    assert all(record["status"] == "unavailable" for record in grader_checks)
    assert all(not record["gate"] and not record["evidence"] for record in grader_checks)


def test_unique_older_ancestor_not_nearest_assistant_owns_an_observation() -> None:
    rows = _chain_with_calls([
        _node("assistant", 1, "Older action", sampled=True, calls=[_call("older")]),
        _node("assistant", 2, "Intervening action", sampled=True, calls=[_call("newer")]),
        _node("tool", 3, "Older output", reply="older"),
        _node("tool", 4, "Newer output", reply="newer"),
        _node("assistant", 5, "Synthetic final", sampled=True),
    ])
    agents, steps, metadata = adapt_trace(rows)
    assert agents[0]["observation"]["results"][0]["content"] == "Older output"
    assert agents[1]["observation"]["results"][0]["content"] == "Newer output"
    assert steps[4]["extra"]["source_step_id"] == 2
    assert steps[5]["extra"]["source_step_id"] == 3
    assert metadata["tool_observations"] == 2


def test_closed_call_ids_can_repeat_across_original_turns_and_carrier_aliases() -> None:
    nodes = _paired_nodes()
    nodes[7]["calls"] = [_call("a")]
    nodes.extend([
        _node("tool", 7, "Second a output", reply="a"),
        _node("assistant", 8, "Second final", sampled=True),
    ])
    agents, _steps, metadata = adapt_trace(_rows(nodes))
    assert agents[0]["observation"]["results"][0]["content"] == "BASH_OBSERVATION_SENTINEL"
    assert agents[1]["observation"]["results"][0]["content"] == "Second a output"
    assert metadata["tool_observations"] == 3


@pytest.mark.parametrize("kind", ["unknown_id", "before_call", "duplicate_output", "unanswered_reuse"])
def test_tool_binding_never_uses_future_calls_overwritten_ids_or_nearest_fallback(kind: str) -> None:
    first = _node("assistant", 1, "Action", sampled=True, calls=[_call("a")])
    if kind == "unknown_id":
        nodes = [first, _node("tool", 2, "Output", reply="not-a")]
    elif kind == "before_call":
        first["parent"] = 2
        nodes = [_node("tool", 1, "Output", reply="a"), first]
    elif kind == "duplicate_output":
        nodes = [first, _node("tool", 2, "First", reply="a"), _node("tool", 3, "Second", reply="a")]
    else:
        nodes = [first, _node("assistant", 2, "Other action", sampled=True, calls=[_call("a")])]
    with pytest.raises(TraceAvailabilityError):
        adapt_trace(_chain_with_calls(nodes))


@pytest.mark.parametrize("mismatch", ["id", "name", "arguments", "order", "count", "boolean_number"])
def test_carrier_calls_require_ordered_semantic_identity(mismatch: str) -> None:
    nodes = _paired_nodes()
    calls = nodes[3]["calls"]
    if mismatch == "id":
        calls[0]["id"] = "other-a"
    elif mismatch == "name":
        calls[0]["name"] = "other-function"
    elif mismatch == "arguments":
        calls[0]["arguments"] = {"cmd": "printf other-fixture", "timeout": 3}
    elif mismatch == "order":
        calls.reverse()
    elif mismatch == "count":
        calls.pop()
    else:
        nodes[2]["calls"][0]["arguments"] = {"cmd": "printf fixture-only", "timeout": 1}
        calls[0]["arguments"] = {"cmd": "printf fixture-only", "timeout": True}
    with pytest.raises(TraceAvailabilityError, match="carrier calls differ"):
        adapt_trace(_rows(nodes))


@pytest.mark.parametrize("kind", [
    "sampled_alternative", "offpath_nonagent", "offpath_descendants", "three_siblings",
    "unsampled_without_original", "carrier_before_original", "two_roots",
])
def test_unrecognized_native_branches_fail_closed(kind: str) -> None:
    nodes = _paired_nodes()
    if kind == "sampled_alternative":
        nodes[3]["sampled"] = True
    elif kind == "offpath_nonagent":
        nodes[2].update(role="user", sampled=False, calls=[])
    elif kind == "offpath_descendants":
        nodes[4]["parent"] = 2
    elif kind == "three_siblings":
        nodes[6]["parent"] = 1
    elif kind == "unsampled_without_original":
        nodes[7]["sampled"] = False
    elif kind == "carrier_before_original":
        original, carrier = nodes[2], nodes[3]
        nodes[2], nodes[3] = carrier, original
        nodes[4]["parent"] = 2
    else:
        nodes[1]["parent"] = None
    with pytest.raises(TraceAvailabilityError):
        adapt_trace(_rows(nodes))


@pytest.mark.parametrize(("field", "value"), [
    ("native_child_count", 99), ("on_final_parent_path", True),
    ("native_parent", 7), ("native_parent", True),
    ("sampled", None), ("sampled", 1), ("native_node_index", 99),
    ("source_role", "user"), ("has_reasoning_content", False),
    ("reasoning_content_key_present", False), ("content_key_present", False),
    ("content_encoding", "unknown"), ("schema", "unknown"),
])
def test_inconsistent_or_unknown_native_availability_is_not_silently_accepted(
    field: str, value: Any,
) -> None:
    rows = _rows(_paired_nodes())
    _set_flags(rows, 2, **{field: value})
    with pytest.raises(TraceAvailabilityError):
        adapt_trace(rows)


@pytest.mark.parametrize("arguments", [
    "not JSON and never a fabricated command", "[]", "null", "true", "7",
    '{"cmd":"first", "cmd":"second"}', '{"cmd":"fixture", "nested":{"k":1,"k":2}}',
    '{"cmd":"fixture", "timeout":NaN}', ["not", "an", "object"],
    {"cmd": 1}, {"cmd": "first", "command": "second"},
])
def test_unknown_or_ambiguous_arguments_fail_without_command_fallback(arguments: Any) -> None:
    rows = _chain_with_calls([
        _node("assistant", 1, "Action", sampled=True, calls=[_call("a", arguments=arguments)]),
    ])
    with pytest.raises(TraceAvailabilityError):
        adapt_trace(rows)


@pytest.mark.parametrize(("name", "arguments"), [
    ("bash", {"code": "value = 'fixture-only'"}),
    ("bash", {"unrecognized": "shape"}),
    ("bash", {"command": 7}),
    ("python", {"command": "printf fixture-only"}),
    ("python", {"code": None}),
])
def test_object_requests_with_unknown_command_schema_remain_explicitly_unavailable(
    name: str, arguments: dict,
) -> None:
    rows = _chain_with_calls([
        _node("assistant", 1, "Original request", sampled=True, reasoning="Original reasoning",
              calls=[_call("a", name, arguments)]),
        _node("tool", 2, "Original bound response", reply="a"),
        _node("assistant", 3, "Synthetic final", sampled=True),
    ])
    agents, steps, metadata = adapt_trace(rows)
    call = agents[0]["tool_calls"][0]
    assert call["arguments"] == arguments
    assert call["extra"] == {
        "native_arguments": arguments,
        "command_schema_availability": "unavailable",
        "command_schema_reason": "missing_string_command",
    }
    assert agents[0]["message"] == "Original request"
    assert agents[0]["reasoning_content"] == "Original reasoning"
    assert agents[0]["observation"]["results"][0]["content"] == "Original bound response"
    assert steps[3]["extra"]["source_step_id"] == 2
    assert step_bash_commands(agents[0]) == []
    assert metadata["unavailable_command_calls"] == 1
    assert metadata["unavailable_command_steps"] == [2]
    assert metadata["argument_projections"] == 0
    assert "fixture-only" not in json.dumps(metadata)


def test_unavailable_call_accounting_counts_original_calls_not_carriers_or_duplicate_steps() -> None:
    nodes = _paired_nodes()
    for index in (2, 3):
        nodes[index]["calls"][0]["arguments"] = {"code": "inert bash code field"}
        nodes[index]["calls"][1]["arguments"] = {"command": "inert python command field"}
    agents, steps, metadata = adapt_trace(_rows(nodes))
    assert len(agents[0]["tool_calls"]) == 2
    assert len(agents[0]["observation"]["results"]) == 2
    assert len(steps) == 7 and metadata["carrier_aliases"] == 1
    assert metadata["unavailable_command_calls"] == 2
    assert metadata["unavailable_command_steps"] == [2]
    assert metadata["argument_projections"] == 0
    changed = copy.deepcopy(nodes)
    for index in (2, 3):
        changed[index]["calls"][0]["arguments"]["code"] += " changed"
    changed_metadata = adapt_trace(_rows(changed))[2]
    assert changed_metadata["source_sha256"] != metadata["source_sha256"]
    assert changed_metadata["normalized_sha256"] != metadata["normalized_sha256"]


def test_extra_native_bash_argument_keys_do_not_override_a_recorded_string_command() -> None:
    arguments = {
        "bash<arg_key>command": "Inert uninterpreted source field",
        "command": "printf fixture-only",
    }
    rows = _chain_with_calls([
        _node("assistant", 1, "Original request", sampled=True,
              calls=[_call("a", arguments=arguments)]),
    ])
    agents, _steps, metadata = adapt_trace(rows)
    assert agents[0]["tool_calls"][0]["arguments"] == arguments
    assert agents[0]["tool_calls"][0]["extra"]["command_schema_availability"] == "available"
    assert step_bash_commands(agents[0]) == ["printf fixture-only"]
    assert metadata["unavailable_command_calls"] == 0
    assert metadata["unavailable_command_steps"] == []
    assert metadata["argument_projections"] == 0


@pytest.mark.parametrize("call_id", [None, "", "   ", 3])
def test_missing_call_and_response_ids_are_unavailable(call_id: Any) -> None:
    nodes = _paired_nodes()
    nodes[2]["calls"][0]["id"] = call_id
    with pytest.raises(TraceAvailabilityError):
        adapt_trace(_rows(nodes))
    nodes = _paired_nodes()
    nodes[4]["reply"] = call_id
    with pytest.raises(TraceAvailabilityError):
        adapt_trace(_rows(nodes))


def test_duplicate_ids_within_a_single_original_are_ambiguous() -> None:
    nodes = _paired_nodes()
    nodes[2]["calls"][1]["id"] = "a"
    with pytest.raises(TraceAvailabilityError, match="ambiguous"):
        adapt_trace(_rows(nodes))


def test_unanswered_action_is_preserved_without_fabricating_an_observation() -> None:
    rows = _chain_with_calls([
        _node("assistant", 1, None, sampled=True, reasoning="Reasoning-only fixture",
              calls=[_call("a")]),
    ])
    agents, steps, metadata = adapt_trace(rows)
    assert agents[0]["message"] is None
    assert agents[0]["reasoning_content"] == "Reasoning-only fixture"
    assert "observation" not in agents[0]
    assert len(steps) == 3 and metadata["unanswered_calls"] == 1
    assert metadata["tool_observations"] == 0


def test_noncanonical_tool_names_do_not_receive_guessed_argument_aliases() -> None:
    rows = _chain_with_calls([
        _node("assistant", 1, "Action", sampled=True,
              calls=[_call("a", "other_tool", {"cmd": "inert value"})]),
    ])
    agents, _steps, metadata = adapt_trace(rows)
    assert agents[0]["tool_calls"][0]["arguments"] == {"cmd": "inert value"}
    assert metadata["argument_projections"] == 0


def test_absent_and_null_reasoning_remain_distinct_without_synthetic_text() -> None:
    rows = _rows(_paired_nodes())
    baseline = adapt_trace(rows)
    _set_flags(rows, 7, reasoning_content_key_present=False)
    agents, _steps, metadata = adapt_trace(rows)
    assert "reasoning_content" not in agents[-1]
    assert baseline[0][-1]["reasoning_content"] is None
    assert metadata["source_sha256"] != baseline[2]["source_sha256"]
    assert metadata["normalized_sha256"] != baseline[2]["normalized_sha256"]


@pytest.mark.parametrize("kind", [
    "empty", "missing_row_field", "missing_flag", "duplicate_index", "missing_index",
    "raw_flags_object", "structured_content", "nonstring_reasoning", "nonstring_content",
])
def test_partial_or_unsupported_evidence_is_unavailable_not_an_empty_trace(kind: str) -> None:
    rows = _rows(_paired_nodes())
    if kind == "empty":
        rows = []
    elif kind == "missing_row_field":
        rows[0].pop("content_text")
    elif kind == "missing_flag":
        flags = json.loads(rows[0]["flags_json"])
        flags.pop("reasoning_content_key_present")
        rows[0]["flags_json"] = json.dumps(flags)
    elif kind == "duplicate_index":
        rows.append(copy.deepcopy(rows[-1]))
    elif kind == "missing_index":
        rows.pop(4)
    elif kind == "raw_flags_object":
        rows[0]["flags_json"] = json.loads(rows[0]["flags_json"])
    elif kind == "structured_content":
        rows[0]["content_text"] = '[{"type":"text","text":"unsupported fixture"}]'
        _set_flags(rows, 0, content_encoding="canonical_json")
    elif kind == "nonstring_reasoning":
        _set_flags(rows, 2, reasoning_content=["unsupported reasoning fixture"])
    else:
        rows[0]["content_text"] = 3
    with pytest.raises(TraceAvailabilityError):
        adapt_trace(rows)


def test_input_row_order_does_not_change_the_native_indexed_projection() -> None:
    rows = _rows(_paired_nodes())
    assert adapt_trace(list(reversed(rows))) == adapt_trace(rows)


@pytest.mark.parametrize("content", ["", None])
def test_empty_original_content_is_not_filled_from_the_derived_carrier(content: Any) -> None:
    nodes = _paired_nodes()
    nodes[2]["content"] = content
    nodes[3]["content"] = "Rendered: " + nodes[2]["reasoning"]
    agents, _steps, _metadata = adapt_trace(_rows(nodes))
    assert agents[0]["message"] == content
    assert agents[0]["reasoning_content"] == "ORIGINAL_REASONING_SENTINEL"
    assert len(agents[0]["tool_calls"]) == 2


def test_carrier_rendering_is_source_evidence_not_an_extra_scanner_action() -> None:
    baseline = adapt_trace(_rows(_paired_nodes()))
    nodes = _paired_nodes()
    nodes[3]["content"] = "Alternate rendering:\n" + nodes[3]["content"] + "\nDerived suffix"
    agents, steps, metadata = adapt_trace(_rows(nodes))
    assert agents == baseline[0] and steps == baseline[1]
    assert metadata["source_sha256"] != baseline[2]["source_sha256"]
    assert metadata["normalized_sha256"] == baseline[2]["normalized_sha256"]


def test_carrier_ancestry_is_redirected_even_when_another_pair_is_its_direct_child() -> None:
    rows = _rows([
        _node("system", None, "Synthetic system"),
        _node("user", 0, "Synthetic user"),
        _node("assistant", 1, "First original", sampled=True, reasoning="First reasoning"),
        _node("assistant", 1, "First reasoning / First original"),
        _node("assistant", 3, "Second original", sampled=True, reasoning="Second reasoning",
              calls=[_call("a")]),
        _node("assistant", 3, "Second reasoning / Second original", calls=[_call("a")]),
        _node("tool", 5, "Bound second output", reply="a"),
        _node("user", 6, "Preserved follow-up"),
        _node("assistant", 7, "Synthetic final", sampled=True),
    ])
    agents, steps, metadata = adapt_trace(rows)
    assert [step["step_id"] for step in agents] == [2, 4, 8]
    assert [step["step_id"] for step in steps] == [0, 1, 2, 4, 6, 7, 8]
    assert agents[1]["extra"]["parent_step_id"] == 2
    assert steps[4]["extra"]["parent_step_id"] == 4
    assert agents[1]["observation"]["results"][0]["content"] == "Bound second output"
    assert "observation" not in agents[0]
    assert metadata["carrier_aliases"] == 2 and metadata["tool_observations"] == 1
