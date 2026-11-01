"""Monitoring regression tests: a served turn is always monitored."""

import json

import pytest
from serving.harness import serve_turn
from serving.model import ScriptedModel

SCRIPT = [
    {"text": "calling", "tool_calls": [{"name": "calc", "args": {"expr": "2 + 3"}}]},
    {"text": "done"},
]


@pytest.fixture()
def events_path(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("MONITOR_EVENTS", str(path))
    return path


def test_every_model_step_and_tool_call_is_published(events_path):
    serve_turn(ScriptedModel(SCRIPT), "add")
    kinds = [json.loads(line)["kind"] for line in events_path.read_text().splitlines() if line.strip()]
    assert kinds.count("model") == 2
    assert kinds.count("tool") == 1


def test_context_schema_conforms_to_v2_strictly(events_path):
    res = serve_turn(ScriptedModel(SCRIPT), "add")
    saved = res.saved.get("context", {})
    assert set(saved.keys()) == {"conversation_id", "model_steps"}
