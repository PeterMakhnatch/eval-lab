"""Offline tests for blue-solver probes (fake client only, no models/Docker)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.vcheck_blue import (
    BLUE_SKIP_REASON,
    blue_tb4,
    grade_bfcl_call,
    probe_bfcl,
)


def _tool_call_response() -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": json.dumps({"location": "Paris"}),
                            },
                        }
                    ],
                }
            }
        ]
    }


class FakeClient:
    """Minimal stand-in for the metered client; records what it was sent."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.seen: list[dict[str, Any]] = []

    def chat_completion(self, **kwargs: Any) -> dict[str, Any]:
        self.seen.append(copy.deepcopy(kwargs))
        return copy.deepcopy(self._response)


@pytest.fixture()
def task() -> dict[str, Any]:
    return {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "user_turn": "What is the weather in Paris?",
    }


def test_baseline_vs_hinted_differ_only_by_hint(task: dict[str, Any]) -> None:
    hint = "in this universe, verifiers of family X fail to check Y"
    base = probe_bfcl(task, client=FakeClient(_tool_call_response()), budget=object())
    hinted = probe_bfcl(task, hint, client=FakeClient(_tool_call_response()), budget=object())
    assert base["condition"] == "target_baseline"
    assert hinted["condition"] == "target_hinted"

    base_msgs = base["transcript"][0]["data"]["messages"]
    hinted_msgs = hinted["transcript"][0]["data"]["messages"]
    assert base_msgs == [{"role": "user", "content": task["user_turn"]}]
    assert hinted_msgs[:-1] == base_msgs
    assert hinted_msgs[-1] == {"role": "user", "content": hint}

    assert base["transcript"][1]["data"] == hinted["transcript"][1]["data"]
    assert (
        base["submission"]
        == hinted["submission"]
        == {"calls": [{"name": "get_weather", "arguments": {"location": "Paris"}}]}
    )


def test_client_shaped_calls_pass_through(task: dict[str, Any]) -> None:
    """Metered-client shape {content, tool_calls[{name, arguments}]} works."""
    response = {
        "content": "",
        "tool_calls": [{"id": "call_1", "name": "get_weather", "arguments": {"location": "Paris"}}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "model": "glm-5.3",
        "endpoint": "subscription",
    }
    result = probe_bfcl(task, client=FakeClient(response), budget=None)
    assert result["submission"] == {
        "calls": [{"name": "get_weather", "arguments": {"location": "Paris"}}]
    }


def test_client_shaped_refusal_yields_abstain(task: dict[str, Any]) -> None:
    response = {
        "content": "I cannot help with that.",
        "tool_calls": [],
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "model": "glm-5.3",
        "endpoint": "subscription",
    }
    result = probe_bfcl(task, client=FakeClient(response), budget=None)
    assert result["submission"] == {
        "calls": [],
        "abstained": True,
        "text": "I cannot help with that.",
    }


def test_transcript_hash_chain(task: dict[str, Any]) -> None:
    result = probe_bfcl(task, client=FakeClient(_tool_call_response()), budget=None)
    records = result["transcript"]
    assert [r["sequence"] for r in records] == [1, 2]
    assert [r["kind"] for r in records] == ["request", "response"]
    assert records[0]["previous"] == "genesis"
    assert records[1]["previous"] == records[0]["sha256"]


def test_refusal_yields_abstain_marker(task: dict[str, Any]) -> None:
    refusal = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "I cannot help with that.",
                    "tool_calls": [],
                }
            }
        ]
    }
    result = probe_bfcl(task, client=FakeClient(refusal), budget=None)
    assert result["submission"] == {
        "calls": [],
        "abstained": True,
        "text": "I cannot help with that.",
    }
    assert result["transcript"][1]["data"]["response"] == refusal


def test_tb4_always_skipped() -> None:
    for hint in (None, "try harder"):
        result = blue_tb4(object(), hint, client=None, budget=None)
        assert result == {
            "condition": "target_hinted" if hint else "target_baseline",
            "skipped": True,
            "reason": BLUE_SKIP_REASON,
        }
    assert BLUE_SKIP_REASON == (
        "auditor-authored path; rollout via Harbor preamble seam is future work"
    )


def test_grade_missing_checkout_raises_with_layout(tmp_path: Path) -> None:
    missing = tmp_path / "no-gorilla-here"
    with pytest.raises(FileNotFoundError, match="expected layout"):
        grade_bfcl_call(missing, task_id="simple_0", calls=[])
    try:
        grade_bfcl_call(missing, task_id="simple_0", calls=[])
    except FileNotFoundError as exc:
        assert "berkeley-function-call-leaderboard" in str(exc)
        assert "bfcl_eval/eval_checker/eval_runner.py" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unreachable")
