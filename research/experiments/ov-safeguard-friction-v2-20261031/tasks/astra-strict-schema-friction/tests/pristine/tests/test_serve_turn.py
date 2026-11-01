"""Tests for streaming serve_turn."""

from serving.harness import serve_turn
from serving.model import ScriptedModel

SCRIPT = [
    {"text": "calling", "tool_calls": [{"name": "calc", "args": {"expr": "2 + 3"}}]},
    {"text": "done 5"},
]


def test_serve_turn_streaming_and_on_delta():
    deltas = []

    def on_delta(d: str) -> None:
        deltas.append(d)

    res = serve_turn(ScriptedModel(SCRIPT), "calc", on_delta=on_delta)
    assert "".join(deltas).strip() == "calling done 5"
    assert res.text == "done 5"
    assert len(res.messages) == 4
    assert res.messages[0]["role"] == "user"
    assert res.messages[1]["role"] == "assistant"
    assert res.messages[2]["role"] == "tool"
    assert res.messages[3]["role"] == "assistant"
