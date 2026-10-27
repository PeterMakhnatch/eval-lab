from __future__ import annotations

import io
import json
from typing import Any

import pytest

from evallab.trace_store.core import TracePayload
from evallab.trace_store.laminar import PROJECT_ID, LaminarTarget


def trace() -> TracePayload:
    return TracePayload("own", "mimo-defect-map-v1", {
        "model": "m", "run_id": "own", "source": "mimo-defect-map-v1",
        "benchmark": "terminal-bench", "task_id": "terminal-bench/task",
        "outcome": "unknown", "split": "DEV",
    }, [
        {"step_index": 0, "role": "assistant", "content": "hello", "reasoning": "reasoning",
         "tool_calls": [], "tool_call_id": None},
    ])


class Recorder:
    def __init__(self, *, disabled: bool = True, project: str = PROJECT_ID, reply: bytes = b""):
        self.disabled, self.project, self.reply = disabled, project, reply
        self.posts: list[bytes] = []

    def __call__(self, request: Any, timeout: int):
        if request.get_method() == "GET":
            return io.BytesIO(json.dumps({"signals": [{"name": "preset", "projectId": self.project, "disabled": self.disabled}]}).encode())
        self.posts.append(request.data)
        return io.BytesIO(self.reply)


@pytest.mark.parametrize("disabled,project", [(False, PROJECT_ID), (True, "other")])
def test_upload_refuses_live_signals_or_wrong_project(disabled: bool, project: str) -> None:
    recorder = Recorder(disabled=disabled, project=project)
    with pytest.raises(ValueError):
        LaminarTarget(api_key="test", opener=recorder).upload(trace())
    assert not recorder.posts


def test_wire_byte_budget_is_checked_before_any_post() -> None:
    recorder = Recorder()
    target = LaminarTarget(api_key="test", opener=recorder)
    target.max_bytes = 1
    with pytest.raises(ValueError, match="allowance"):
        target.upload(trace())
    assert not recorder.posts
    assert target.sent_bytes == 0


def test_partial_otlp_rejection_cannot_be_marked_successful() -> None:
    recorder = Recorder(reply=b'{"partialSuccess":{"rejectedSpans":"1"}}')
    target = LaminarTarget(api_key="test", opener=recorder)
    with pytest.raises(ValueError, match="partially rejected"):
        target.upload(trace())
    assert target.sent_bytes == sum(map(len, recorder.posts)) > 0


def test_each_wire_attempt_reserves_exact_encoded_bytes() -> None:
    recorder = Recorder()
    target = LaminarTarget(api_key="test", opener=recorder)
    reservations: list[int] = []
    target.reserve_bytes = reservations.append
    result = target.upload(trace())
    assert reservations == list(map(len, recorder.posts))
    assert result["payload_bytes"] == sum(reservations) == target.sent_bytes
