"""Monitoring regression tests (POLICY.md): a served turn is always monitored."""

import json

import pytest
from serving.context import RuntimeMonitorContext
from serving.harness import default_context, serve_turn
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


def test_saved_context_records_the_monitor(events_path):
    saved = serve_turn(ScriptedModel(SCRIPT), "add").saved
    assert "monitor_ctx" in saved["context"]
    assert saved["context"]["monitor_ctx"]["required"] is True


def test_every_model_step_and_tool_call_is_published(events_path):
    serve_turn(ScriptedModel(SCRIPT), "add")
    kinds = [json.loads(line)["kind"] for line in events_path.read_text().splitlines()]
    assert kinds.count("model") == 2
    assert kinds.count("tool") == 1


def test_default_context_is_monitored():
    assert isinstance(default_context("conv-9"), RuntimeMonitorContext)
