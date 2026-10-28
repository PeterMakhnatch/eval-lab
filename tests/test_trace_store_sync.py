from __future__ import annotations

import importlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from evallab.trace_store.core import TracePayload, trace_uuid
from evallab.trace_store.laminar import trace_spans
from evallab.trace_store.secrets import SecretScanRejected
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
    assert first["cost_usd"] is None and first["model_cost_usd"] == 0
    assert "unknown" in first["storage_tariff"]
    ledger = tmp_path / ".local/state/evallab-failure-library/spend.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert all(row["cost_usd"] is None and row["model_cost_usd"] == 0 for row in rows)


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
    reopened = state_module.byte_reserver(tmp_path, "laminar")
    with pytest.raises(ValueError, match="monthly"):
        reopened(3)
    reopened(2)
    # Another provider has independent headroom, without clearing either ledger.
    state_module.byte_reserver(tmp_path, "docent")(10)
    with pytest.raises(ValueError, match="docent.*monthly"):
        state_module.byte_reserver(tmp_path, "docent")(1)
    with pytest.raises(ValueError, match="laminar.*monthly"):
        state_module.byte_reserver(tmp_path, "laminar")(1)


def test_byte_budget_keeps_old_month_receipts_without_charging_them(
    monkeypatch, tmp_path: Path,
) -> None:
    state_module = importlib.import_module("evallab.trace_store.state")
    monkeypatch.setattr(state_module, "MONTHLY_PAYLOAD_BYTES", 10)
    monkeypatch.setattr(state_module, "now", lambda: "2026-09-30T23:59:59+00:00")
    state_module.byte_reserver(tmp_path, "laminar")(10)
    monkeypatch.setattr(state_module, "now", lambda: "2026-10-01T00:00:00+00:00")
    state_module.byte_reserver(tmp_path, "laminar")(10)
    assert len((tmp_path / "upload-bytes.jsonl").read_text().splitlines()) == 2
    with pytest.raises(ValueError, match="monthly"):
        state_module.byte_reserver(tmp_path, "laminar")(1)


@pytest.mark.parametrize("batched", [False, True])
def test_encoded_secret_skips_only_its_trace_then_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, batched: bool,
) -> None:
    database = _setup(monkeypatch, tmp_path)
    bad = payload()
    good = replace(bad, trace_id="own-2", metadata={**bad.metadata, "run_id": "own-2"})
    traces = {trace.trace_id: trace for trace in (bad, good)}
    refs = [SimpleNamespace(trace_id=trace.trace_id, dataset_id=trace.dataset_id)
            for trace in traces.values()]
    monkeypatch.setattr(sync_module, "select_traces", lambda *args, **kwargs: refs)
    monkeypatch.setattr(sync_module, "load_trace", lambda con, ref, **kwargs: traces[ref.trace_id])
    delivered: list[str] = []

    def upload_batch(batch):
        if any(trace.trace_id == bad.trace_id for trace in batch):
            raise SecretScanRejected(bad.trace_id)
        delivered.extend(trace.trace_id for trace in batch)
        return [{"remote_id": trace.trace_id, "payload_bytes": 7} for trace in batch]

    uploader = (SimpleNamespace(upload_batch=upload_batch) if batched
                else SimpleNamespace(upload=lambda trace: upload_batch([trace])[0]))
    report = sync_module.sync(
        "docent" if batched else "laminar", repo_root=tmp_path,
        db_path=database, root=tmp_path / "state", uploader=uploader,
    )
    assert report["stopped"] is None
    assert report["synced"] == 1
    assert delivered == [good.trace_id]
    assert report["rejected"] == [{
        "trace_id": bad.trace_id, "dataset": bad.dataset_id,
        "kind": "secret_scan", "stage": "encoded",
    }]
    state = SyncState(tmp_path / "state" / ("docent-state.jsonl" if batched else "laminar-state.jsonl"))
    assert set(state.receipts) == {good.trace_id}


def test_secret_quarantine_tombstones_preserve_history_with_opaque_ids(tmp_path: Path) -> None:
    path = tmp_path / "state.jsonl"
    state = SyncState(path)
    bad = payload()
    good = replace(payload(), trace_id="other-trace")
    for trace in (bad, good):
        state.record(
            trace.trace_id, content_digest(trace), {"remote_id": trace_uuid(trace.trace_id)},
            trace.dataset_id,
        )
    original = path.read_bytes()
    state.quarantine_secret(bad.trace_id)
    assert path.read_bytes().startswith(original)
    tombstone = json.loads(path.read_text().splitlines()[-1])
    assert tombstone["remote_id"] == trace_uuid(bad.trace_id)
    assert tombstone["pattern_ids"] == ["secret-pattern"]
    assert "trace_id" not in tombstone and "dataset" not in tombstone
    assert bad.trace_id not in json.dumps(tombstone)
    reopened = SyncState(path)
    assert not reopened.unchanged(bad.trace_id, content_digest(bad))
    assert set(reopened.receipts) == {good.trace_id}


@pytest.mark.parametrize("target", ["laminar", "docent"])
def test_canonical_secret_gate_rechecks_unchanged_receipts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str,
) -> None:
    database = _setup(monkeypatch, tmp_path)
    key = "sk-" + "proj-" + "A7b" * 16
    trace = replace(payload(), messages=[{
        "role": "tool", "content": json.dumps({"stdout": "heading\n" + key}),
    }])
    monkeypatch.setattr(sync_module, "load_trace", lambda *a, **kw: trace)
    root = tmp_path / "state"
    state = SyncState(root / f"{target}-state.jsonl")
    state.record(
        trace.trace_id, content_digest(trace), {"remote_id": trace_uuid(trace.trace_id)},
        trace.dataset_id,
    )
    class Uploader:
        def upload(self, trace):
            raise AssertionError("An unchanged receipt must not bypass a current secret gate")
    report = sync_module.sync(
        target, repo_root=tmp_path, db_path=database, root=root, uploader=Uploader(),
    )
    assert report["synced"] == report["unchanged"] == 0
    assert report["rejected"] == [{
        "trace_id": trace.trace_id, "dataset": trace.dataset_id, "kind": "secret_scan",
    }]
