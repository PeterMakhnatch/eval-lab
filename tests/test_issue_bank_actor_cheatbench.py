"""Synthetic regression tests for the CheatBench actor adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evallab.failure_library.bank.actors import ActorUnavailable
from evallab.failure_library.bank.actors.cheatbench import to_atif

RUN = SimpleNamespace(run_id="run-synthetic-1")


def test_step_events_keep_native_ids_origins_and_exact_feedback() -> None:
    raw = {
        "events": [
            {
                "step": 3,
                "src": "agent",
                "msg": "checking",
                "tools": [{"fn": "bash", "cmd": "ls"}],
                "obs": "a\n  b  ",
            },
            {"step": 7, "src": "agent", "msg": "done"},
        ]
    }
    projection = to_atif(raw, RUN)
    assert projection.scope == "source_native_run"
    assert projection.doc["session_id"] == "run-synthetic-1"
    assert [s["step_id"] for s in projection.doc["steps"]] == [3, 7]
    assert projection.origins == {3: 3, 7: 7}
    first = projection.doc["steps"][0]
    assert first["observation"] == {"results": [{"content": "a\n  b  "}]}
    assert first["tool_calls"][0]["function_name"] == "bash"


def test_duplicate_step_ids_fall_back_to_bound_ordinals() -> None:
    raw = {
        "events": [
            {"step": 1, "src": "user", "msg": "task"},
            {"step": 1, "src": "agent", "msg": "answer"},
        ]
    }
    projection = to_atif(raw, RUN)
    assert [s["step_id"] for s in projection.doc["steps"]] == [0, 1]
    assert projection.origins == {0: "events[0]", 1: "events[1]"}
    assert projection.scope == "single_response"


def test_messages_bind_tool_feedback_to_preceding_agent_step() -> None:
    raw = {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "thinking out loud"},
            {"role": "tool", "content": "exit 0\nfull output"},
            {"role": "assistant", "content": "final"},
        ]
    }
    projection = to_atif(raw, RUN)
    sources = [s["source"] for s in projection.doc["steps"]]
    assert sources == ["system", "user", "agent", "agent"]
    assert projection.doc["steps"][2]["observation"] == {
        "results": [{"content": "exit 0\nfull output"}]
    }
    assert projection.origins[2] == "messages[2]"
    assert projection.limitations == ("messages form carries no structured tool calls",)


@pytest.mark.parametrize(
    "raw",
    [
        {"messages": [{"role": "trace", "content": "rendered"}]},
        {"messages": [{"role": "assistant", "content": "<function=bash>x</function>"}]},
        {"messages": [{"role": "assistant", "content": "[action] {}"}]},
        {"messages": [{"role": "tool", "content": "orphan feedback"}]},
        {"events": [{"step": 1, "src": "judge", "msg": "x"}]},
        {"events": [{"step": 1, "src": "agent", "msg": "x"}, "not-a-step"]},
        {"messages": [{"role": "assistant", "content": ""}]},
        {},
    ],
)
def test_unsupported_or_ambiguous_shapes_are_refused(raw: dict) -> None:
    with pytest.raises(ActorUnavailable):
        to_atif(raw, RUN)


def test_non_mapping_raw_is_refused() -> None:
    with pytest.raises(ActorUnavailable):
        to_atif(["not", "a", "mapping"], RUN)
