"""Synthetic regression tests for the Terminal Wrench actor projection boundary."""

from __future__ import annotations

import json

import pytest

from evallab.failure_library.bank.actors import ActorUnavailable
from evallab.failure_library.bank.actors.wrench import to_atif
from evallab.failure_library.records import RunRecord, TaskRef
from evallab.failure_library.runs.common import outcome

REF = "file:///synthetic/wrench/trajectory.json#field=steps"


def _run(collection: str = "terminal-wrench") -> RunRecord:
    return RunRecord(
        run_id="wrench-synthetic-1",
        collection=collection,
        task=TaskRef(
            source_id="terminal-wrench", task_id="synthetic-task", upstream_repo="synthetic"
        ),
        outcome=outcome(1.0),
        transcript_ref=REF,
    )


STEPS = [
    {"step_id": 1, "source": "user", "message": "Synthetic task prompt."},
    {
        "step_id": 4,
        "source": "agent",
        "message": "Inspect, then write.",
        "reasoning": "plan A",
        "tool_calls": [
            {
                "tool_call_id": "call_a",
                "function_name": "bash_command",
                "arguments": {"keystrokes": "ls\n"},
            },
            {
                "tool_call_id": "call_b",
                "function_name": "bash_command",
                "arguments": {"keystrokes": "pwd\n"},
            },
        ],
        "observation": {
            "results": [
                {"source_call_id": "call_a", "content": "Terminal output:\na.txt\n"},
                {"source_call_id": "call_b", "content": "/app café ✓"},
            ]
        },
        "reward": 1.0,
        "label": "rewarded_serious_exploit",
    },
    {"step_id": 9, "source": "agent", "message": "Done.", "judge": {"verdict": "hack"}},
]


def test_projects_actor_steps_with_native_ids_calls_and_feedback():
    projection = to_atif(STEPS, _run())

    assert projection.scope == "source_native_run"
    assert projection.doc["schema_version"] == "ATIF-v1.6"
    assert projection.doc["session_id"] == "wrench-synthetic-1"
    steps = projection.doc["steps"]
    assert [step["step_id"] for step in steps] == [4, 9]
    assert projection.origins == {4: 4, 9: 9}
    assert steps[0]["message"] == "Inspect, then write."
    assert steps[0]["reasoning"] == "plan A"
    assert steps[0]["tool_calls"][1] == {
        "tool_call_id": "call_b",
        "function_name": "bash_command",
        "arguments": {"keystrokes": "pwd\n"},
    }
    assert steps[0]["observation"]["results"] == [
        {"source_call_id": "call_a", "content": "Terminal output:\na.txt\n"},
        {"source_call_id": "call_b", "content": "/app café ✓"},
    ]


def test_excludes_verifier_metadata_and_user_prompts():
    trajectory = {"schema_version": "ATIF-v1.6", "reward": 1.0, "label": "x", "steps": STEPS}
    projection = to_atif(trajectory, _run())
    serialized = json.dumps(projection.doc)

    assert "rewarded_serious_exploit" not in serialized
    assert "hack" not in serialized
    assert "Synthetic task prompt." not in serialized
    assert "reward" not in serialized
    assert all(step["source"] == "agent" for step in projection.doc["steps"])


def test_falls_back_to_ordinals_bound_to_native_indexes_when_ids_are_unusable():
    steps = [
        {"step_id": "a", "source": "agent", "message": "first"},
        {"source": "user", "message": "prompt"},
        {"step_id": 3, "source": "agent", "message": "second"},
    ]
    projection = to_atif(steps, _run())

    assert [step["step_id"] for step in projection.doc["steps"]] == [1, 2]
    assert projection.origins == {1: 0, 2: 2}


def test_refuses_log_only_rows_without_agent_steps():
    logs = [{"step_id": 1, "source": "system", "message": "boot"}]
    with pytest.raises(ActorUnavailable, match="no agent steps"):
        to_atif(logs, _run())


def test_refuses_agent_steps_without_actor_content():
    empty = [{"step_id": 1, "source": "agent"}]
    with pytest.raises(ActorUnavailable, match="carry no"):
        to_atif(empty, _run())


def test_refuses_unattributed_steps_and_other_collections():
    with pytest.raises(ActorUnavailable, match="source attribution"):
        to_atif([{"step_id": 1, "message": "no source"}], _run())
    with pytest.raises(ActorUnavailable, match="not a terminal-wrench run"):
        to_atif(STEPS, _run(collection="metr-malt"))


def test_refuses_calls_without_identifiers():
    bad = [
        {
            "step_id": 1,
            "source": "agent",
            "tool_calls": [{"function_name": "bash_command", "arguments": {}}],
        }
    ]
    with pytest.raises(ActorUnavailable, match="lacks a tool_call_id"):
        to_atif(bad, _run())
