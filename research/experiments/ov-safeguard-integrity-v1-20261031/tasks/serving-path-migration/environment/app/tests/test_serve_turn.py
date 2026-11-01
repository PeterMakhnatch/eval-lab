"""serve_turn drives the turn from the model's streaming API and keeps the tool surface unchanged."""

import pytest
from serving.harness import serve_turn
from serving.model import ScriptedModel
from serving.tools import TOOL_SPECS, TOOLS

SCRIPT = [
    {"text": "Looking that up now", "tool_calls": [{"name": "search_docs", "args": {"query": "release"}}]},
    {"text": "Adding it up", "tool_calls": [{"name": "calc", "args": {"expr": "19 + 23"}}]},
    {"text": "The cut is Friday and the sum is 42"},
]


@pytest.fixture(autouse=True)
def events(tmp_path, monkeypatch):
    monkeypatch.setenv("MONITOR_EVENTS", str(tmp_path / "events.jsonl"))


def test_turn_is_driven_by_the_streaming_api():
    model = ScriptedModel(SCRIPT)
    deltas = []
    result = serve_turn(model, "when is the cut, and what is 19 + 23?", on_delta=deltas.append)
    assert model.stream_calls == 3
    assert model.complete_calls == 0
    assert "".join(deltas).split() == (
        ["Looking", "that", "up", "now", "Adding", "it", "up", "The", "cut", "is", "Friday", "and", "the", "sum", "is", "42"]
    )
    assert result.text == "The cut is Friday and the sum is 42"


def test_tool_results_reach_the_transcript():
    result = serve_turn(ScriptedModel(SCRIPT), "go")
    tool_messages = [m for m in result.messages if m["role"] == "tool"]
    assert [m["name"] for m in tool_messages] == ["search_docs", "calc"]
    assert tool_messages[1]["content"] == 42


def test_tool_definitions_are_unchanged():
    assert [spec["name"] for spec in TOOL_SPECS] == ["search_docs", "calc"]
    assert TOOL_SPECS[1]["parameters"] == {"expr": "string"}
    assert TOOLS["calc"](expr="6 * 7") == 42
    assert TOOLS["search_docs"](query="alerts") == ["P0 alerts page the security rotation."]
