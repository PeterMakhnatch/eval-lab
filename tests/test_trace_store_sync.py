from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from evallab.trace_store.core import TracePayload
from evallab.trace_store.laminar import trace_spans
from evallab.trace_store.state import SyncState, content_digest

sync_module = importlib.import_module("evallab.trace_store.sync")


def payload(content: str = "hello") -> TracePayload:
    return TracePayload(
        "own-1", "mimo-defect-map-v1",
        {"run_id": "own-1", "source": "mimo-defect-map-v1", "benchmark": "terminal-bench", "model": "model", "task_id": "terminal-bench/task", "outcome": "unknown", "split": "DEV"},
        [{"step_index": 0, "role": "assistant", "content": content, "reasoning": "r" * 20000,
          "tool_calls": [{"id": "call1", "function": "bash", "arguments": {"command": "echo hello"}}], "tool_call_id": None},
         {"step_index": 1, "role": "tool", "content": "o" * 40000, "reasoning": None,
          "tool_calls": [], "tool_call_id": "call1"}],
    )


def test_incremental_receipts_skip_unchanged_but_replace_changed(tmp_path: Path) -> None:
    state = SyncState(tmp_path / "state.jsonl")
    old = payload()
    digest = content_digest(old)
    assert not state.unchanged(old.trace_id, digest)
    state.record(old.trace_id, digest, {"remote_id": "remote", "payload_bytes": 12}, old.dataset_id)
    reopened = SyncState(tmp_path / "state.jsonl")
    assert reopened.unchanged(old.trace_id, digest)
    assert not reopened.unchanged(old.trace_id, content_digest(payload("changed")))


def test_projection_preserves_reasoning_calls_and_observations_without_opt_in() -> None:
    spans = trace_spans(payload())
    llm = next(s for s in spans if s["attributes"]["lmnr.span.type"] == "LLM")
    tool = next(s for s in spans if s["attributes"]["lmnr.span.type"] == "TOOL")
    assert "r" * 20000 in llm["attributes"]["lmnr.span.output"]
    assert '"id": "call1"' in llm["attributes"]["lmnr.span.output"]
    assert tool["attributes"]["lmnr.span.output"] == "o" * 40000
    root = spans[-1]
    metadata = {k.rsplit("metadata.", 1)[1]: v for k, v in root["attributes"].items() if "metadata." in k}
    assert metadata == payload().metadata
    assert all("evallab.signals" not in key for s in spans for key in s["attributes"])
    assert tool["parent_id"] == llm["span_id"]
    assert {s["trace_id"] for s in spans} == {root["trace_id"]}


def _setup(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    database = tmp_path / "db.sqlite"
    sqlite3.connect(database).close()
    ref = SimpleNamespace(trace_id="own-1", dataset_id="mimo-defect-map-v1")
    monkeypatch.setattr(sync_module, "select_traces", lambda *a, **kw: [ref])
    monkeypatch.setattr(sync_module, "load_trace", lambda *a, **kw: payload())
    return database


def test_sync_reopens_receipts_without_second_upload(monkeypatch, tmp_path: Path) -> None:
    database = _setup(monkeypatch, tmp_path)
    class Uploader:
        uploads = 0
        def upload(self, trace):
            self.uploads += 1
            return {"remote_id": "remote", "payload_bytes": 9}
    target = Uploader()
    options = {"repo_root": tmp_path, "db_path": database, "root": tmp_path / "state", "uploader": target}
    first = sync_module.sync("laminar", **options)
    second = sync_module.sync("laminar", **options)
    assert (first["synced"], first["payload_bytes"], second["unchanged"], target.uploads) == (1, 9, 1, 1)


def test_quota_stops_without_receipt_and_is_resumable(monkeypatch, tmp_path: Path) -> None:
    database = _setup(monkeypatch, tmp_path)
    class Uploader:
        def upload(self, trace):
            raise HTTPError("https://example.invalid", 429, "quota", {}, None)
    options = {"repo_root": tmp_path, "db_path": database, "root": tmp_path / "state", "uploader": Uploader()}
    report = sync_module.sync("docent", **options)
    assert report["synced"] == 0
    assert "429" in report["stopped"]
    assert not SyncState(tmp_path / "state/docent-state.jsonl").receipts


def test_partial_last_receipt_is_preserved_then_recovered(tmp_path: Path) -> None:
    state = SyncState(tmp_path / "state.jsonl")
    old = payload()
    digest = content_digest(old)
    state.record(old.trace_id, digest, {"remote_id": "remote", "payload_bytes": 12}, old.dataset_id)
    with state.path.open("ab") as stream:
        stream.write(b'{"trace_id":')
    recovered = SyncState(state.path)
    assert recovered.unchanged(old.trace_id, digest)
    assert state.path.with_name("state.jsonl.incomplete").read_bytes() == b'{"trace_id":'
    recovered.record("new", "digest", {"remote_id": "new-remote"}, "source")
    assert set(SyncState(state.path).receipts) == {"own-1", "new"}


def test_byte_budget_survives_reopening_and_rejects_before_send(monkeypatch, tmp_path: Path) -> None:
    state_module = importlib.import_module("evallab.trace_store.state")
    monkeypatch.setattr(state_module, "now", lambda: "2026-10-27T00:00:00+00:00")
    monkeypatch.setattr(state_module, "MONTHLY_PAYLOAD_BYTES", 10)
    state_module.byte_reserver(tmp_path, "laminar")(8)
    reopened = state_module.byte_reserver(tmp_path, "docent")
    with pytest.raises(ValueError, match="monthly"):
        reopened(3)
    reopened(2)
