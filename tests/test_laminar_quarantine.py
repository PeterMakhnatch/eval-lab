"""Quarantined Laminar copies are refused or skipped, and counted, on every Laminar path.

All seams are injected (openers, transports, uploaders, ledgers): no network, no host state.
"""

from __future__ import annotations

import importlib
import io
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from evallab import laminar
from evallab.laminar import (
    SIGNAL_OPT_IN_FILTER,
    SIGNAL_OPT_IN_TAG,
    SIGNALS,
    LaminarExporter,
    apply_signals,
    compare_signals,
    laminar_trace_uuid,
)
from evallab.production import trace_harness_own as own
from evallab.production import trace_harness_tools as tools
from evallab.production.trace_harness import HarnessOptions, HarnessRun
from evallab.readers import laminar_import, laminar_signals
from evallab.trace_store import laminar_quarantine as quarantine
from evallab.trace_store.core import GOODFIRE_DATASET, TracePayload, trace_uuid
from evallab.trace_store.fetch import fetch
from evallab.trace_store.laminar import PROJECT_ID, LaminarTarget
from evallab.trace_store.laminar_quarantine import QuarantinedTrace

sync_module = importlib.import_module("evallab.trace_store.sync")

#: A real quarantined id, used as-is (a Laminar trace id) on every path.
QUARANTINED = min(quarantine.QUARANTINED_REMOTE_IDS)
OK_ID = str(uuid.UUID(int=0x1234))
DATASET = "mimo-defect-map-v1"


@pytest.fixture
def quarantine_ids(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Add synthetic Laminar ids to the list, so derived identifiers can be exercised."""
    with monkeypatch.context() as patch:

        def add(*remote_ids: str) -> None:
            patch.setattr(
                quarantine, "QUARANTINED_REMOTE_IDS", quarantine.QUARANTINED_REMOTE_IDS | set(remote_ids)
            )
            quarantine.is_quarantined.cache_clear()

        yield add
    quarantine.is_quarantined.cache_clear()


def payload(trace_id: str, dataset: str = DATASET) -> TracePayload:
    return TracePayload(trace_id, dataset, {
        "model": "m", "run_id": trace_id, "source": dataset,
        "benchmark": "terminal-bench", "task_id": "terminal-bench/task",
        "outcome": "unknown", "split": "DEV",
    }, [
        {"step_index": 0, "role": "assistant", "content": "hello", "reasoning": "r",
         "tool_calls": [], "tool_call_id": None},
    ])


def test_list_is_the_108_unique_owner_action_ids() -> None:
    ids = quarantine.QUARANTINED_REMOTE_IDS
    assert len(ids) == 108
    assert all(str(uuid.UUID(item)) == item and uuid.UUID(item).version == 5 for item in ids)


def test_predicate_matches_every_identifier_form(quarantine_ids: Callable[..., None]) -> None:
    assert quarantine.is_quarantined(QUARANTINED)
    assert quarantine.is_quarantined(QUARANTINED.upper())
    assert quarantine.is_quarantined(QUARANTINED.replace("-", ""))
    assert not quarantine.is_quarantined(OK_ID)
    assert not quarantine.is_quarantined("own-ok")
    quarantine_ids(trace_uuid("own-q"), laminar_trace_uuid("trial-q"))
    assert quarantine.is_quarantined("own-q")  # cloud-DB trace id -> trace_store uuid
    assert quarantine.is_quarantined(trace_uuid("own-q"))
    assert quarantine.is_quarantined("trial-q")  # Harbor trial name -> exporter uuid
    assert not quarantine.is_quarantined("own-ok")
    with pytest.raises(QuarantinedTrace):
        quarantine.reject_quarantined("own-q")


# --- LaminarTarget.upload / check_signals ---------------------------------------------------


class Hosted:
    """Laminar HTTP seam: signals, the read-only SQL guard, and OTLP posts."""

    def __init__(self, *, opt_in: bool = False, tagged: list[dict[str, Any]] | None = None) -> None:
        self.opt_in = opt_in
        self.tagged = tagged or []
        self.requests: list[str] = []
        self.queries: list[dict[str, Any]] = []
        self.posts: list[bytes] = []

    def __call__(self, request: Any, timeout: int) -> io.BytesIO:
        self.requests.append(request.full_url)
        if request.full_url.endswith("/v1/sql/query"):
            self.queries.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"data": self.tagged}).encode())
        if request.get_method() == "GET":
            signal = {
                "name": "preset", "projectId": PROJECT_ID, "disabled": not self.opt_in,
                "filters": [SIGNAL_OPT_IN_FILTER] if self.opt_in else [],
            }
            return io.BytesIO(json.dumps({"signals": [signal]}).encode())
        self.posts.append(request.data)
        return io.BytesIO(b"")


def test_upload_refuses_a_quarantined_trace_before_any_request() -> None:
    hosted = Hosted()
    with pytest.raises(QuarantinedTrace):
        LaminarTarget(api_key="k", opener=hosted).upload(payload(QUARANTINED))
    assert hosted.requests == []


def test_upload_still_admits_a_non_quarantined_trace() -> None:
    hosted = Hosted()
    result = LaminarTarget(api_key="k", opener=hosted).upload(payload("own-ok"))
    assert hosted.posts and result["remote_id"] == trace_uuid("own-ok")


def test_signals_opt_in_verifies_no_quarantined_copy_carries_the_tag() -> None:
    clean = Hosted(opt_in=True)
    LaminarTarget(api_key="k", opener=clean, signals_opt_in=True).upload(payload("gf-1", GOODFIRE_DATASET))
    assert clean.queries[0]["parameters"] == {
        "ids": sorted(quarantine.QUARANTINED_REMOTE_IDS), "tag": SIGNAL_OPT_IN_TAG,
    }
    assert clean.posts
    tagged = Hosted(opt_in=True, tagged=[{"trace_id": QUARANTINED}])
    with pytest.raises(QuarantinedTrace):
        LaminarTarget(api_key="k", opener=tagged, signals_opt_in=True).upload(
            payload("gf-1", GOODFIRE_DATASET)
        )
    assert tagged.posts == []


def test_every_signal_definition_requires_the_opt_in_tag() -> None:
    # No trace-id predicate exists in a Signal filter, so the tag is the only exclusion.
    assert all(SIGNAL_OPT_IN_FILTER in definition["filters"] for definition in SIGNALS)


def test_apply_signals_refuses_when_a_quarantined_copy_is_selectable() -> None:
    hosted = Hosted(tagged=[{"trace_id": QUARANTINED}])
    with pytest.raises(QuarantinedTrace):
        apply_signals(api_key="k", opener=hosted)
    assert [url for url in hosted.requests if not url.endswith("/v1/sql/query")] == []


# --- sync / watch ---------------------------------------------------------------------------


@pytest.mark.parametrize(("target", "uploaded", "skipped"), [
    ("laminar", ["own-ok"], 1),
    ("docent", [QUARANTINED, "own-ok"], 0),
])
def test_sync_skips_quarantined_traces_for_laminar_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str, uploaded: list[str], skipped: int,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    database = tmp_path / "db.sqlite"
    sqlite3.connect(database).close()
    refs = [SimpleNamespace(trace_id=item, dataset_id=DATASET) for item in (QUARANTINED, "own-ok")]
    monkeypatch.setattr(sync_module, "select_traces", lambda *a, **kw: refs)
    monkeypatch.setattr(sync_module, "load_trace", lambda con, ref, **kw: payload(ref.trace_id))

    class Uploader:
        def __init__(self) -> None:
            self.uploads: list[str] = []

        def upload(self, trace: TracePayload) -> dict[str, Any]:
            self.uploads.append(trace.trace_id)
            return {"remote_id": "remote", "payload_bytes": 9}

    uploader = Uploader()
    report = sync_module.sync(
        target, repo_root=tmp_path, db_path=database, root=tmp_path / "state", uploader=uploader,
    )
    assert uploader.uploads == uploaded
    assert (report["quarantined"], report["selected"]) == (skipped, 2 - skipped)


def test_watch_summary_carries_the_quarantined_count() -> None:
    from evallab.trace_store.watch import _summary

    assert _summary({"quarantined": 3})["quarantined"] == 3


# --- fetch ----------------------------------------------------------------------------------


class SqlOpener:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.requests = 0

    def __call__(self, request: Any, *, timeout: int) -> io.BytesIO:
        self.requests += 1
        return io.BytesIO(json.dumps({"data": self.rows}).encode())


def test_fetch_run_id_of_a_quarantined_trace_makes_no_hosted_call() -> None:
    opener = SqlOpener([{"trace_id": trace_uuid("own-ok")}])
    result = fetch(
        "laminar", run_id=QUARANTINED, opener=opener, api_key="k", spend_recorder=lambda *a, **k: None,
    )
    assert (result["rows"], result["skipped_quarantined"], opener.requests) == ([], 1, 0)


def test_fetch_query_skips_and_counts_quarantined_rows() -> None:
    rows = [
        {"trace_id": QUARANTINED, "name": "a"}, {"trace_id": OK_ID, "name": "b"},
        {"id": QUARANTINED}, {"count": 3},
    ]
    opener = SqlOpener(rows)
    result = fetch(
        "laminar", query="SELECT 1", opener=opener, api_key="k", spend_recorder=lambda *a, **k: None,
    )
    assert result["rows"] == [{"trace_id": OK_ID, "name": "b"}, {"count": 3}]
    assert result["skipped_quarantined"] == 2


def test_fetch_run_id_of_an_admitted_trace_still_queries() -> None:
    opener = SqlOpener([{"trace_id": trace_uuid("own-ok")}])
    result = fetch(
        "laminar", run_id="own-ok", opener=opener, api_key="k", spend_recorder=lambda *a, **k: None,
    )
    assert (len(result["rows"]), result["skipped_quarantined"], opener.requests) == (1, 0, 1)


# --- live exporter and importer -------------------------------------------------------------


def write_trial(runs: Path, name: str) -> Path:
    trial = runs / "job" / name
    (trial / "agent").mkdir(parents=True)
    steps = [
        {"step_id": "1", "timestamp": "2026-10-05T10:00:00+00:00", "source": "user", "message": "Fix"},
        {"step_id": "2", "timestamp": "2026-10-05T10:00:01+00:00", "source": "agent", "message": "ls",
         "tool_calls": [{"tool_call_id": "c", "function_name": "bash_command", "arguments": {"keystrokes": "ls"}}],
         "observation": {"results": [{"source_call_id": "c", "content": "a.py"}]}},
    ]
    (trial / "agent" / "trajectory.json").write_text(json.dumps({"steps": steps}))
    (trial / "result.json").write_text(json.dumps({"finished_at": "2026-10-05T10:01:00+00:00"}))
    return trial


class Transport:
    def __init__(self) -> None:
        self.trace_ids: set[str] = set()

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        self.trace_ids |= {
            span.trace_id.hex()
            for resource in request.resource_spans for scope in resource.scope_spans for span in scope.spans
        }
        return 200


def test_exporter_skips_and_counts_quarantined_spans_but_exports_the_rest(
    tmp_path: Path, quarantine_ids: Callable[..., None],
) -> None:
    quarantine_ids(laminar_trace_uuid("trial-q"))
    transport = Transport()
    exporter = LaminarExporter(
        api_key="k", out_dir=tmp_path / "out", endpoint="http://x", transport=transport, secrets=[],
    )
    for name in ("trial-q", "trial-ok"):
        exporter.sync_trial(tmp_path, write_trial(tmp_path / name, name), finished=True)
    outcome = exporter.flush()
    assert transport.trace_ids == {laminar.trial_trace_id("trial-ok")}
    assert outcome["spans"] > 0 and outcome["quarantined"] > 0


def test_import_trial_posts_nothing_for_a_quarantined_trial(
    tmp_path: Path, quarantine_ids: Callable[..., None],
) -> None:
    quarantine_ids(laminar_trace_uuid("trial-q"))
    transport = Transport()
    outcome = laminar_import.import_trial(
        write_trial(tmp_path / "runs", "trial-q"), api_key="k", endpoint="http://x",
        state_dir=tmp_path / "state", transport=transport,
    )
    assert (outcome["spans"], transport.trace_ids) == (0, set())
    assert outcome["quarantined"] > 0


class Signals:
    """Read-only Laminar SQL seam: records the trace ids each query asks about."""

    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    def __call__(self, request: Any, timeout: float) -> io.BytesIO:
        if request.get_method() == "GET":
            reply: dict[str, Any] = {
                "projectId": "p",
                "signals": [{"id": "s1", "name": "copied_upstream_fix"}],
            }
        else:
            self.asked.append(json.loads(request.data)["parameters"]["ids"])
            reply = {"data": []}
        return io.BytesIO(json.dumps(reply).encode())


def test_signal_reader_collect_skips_quarantined_trials(
    tmp_path: Path, quarantine_ids: Callable[..., None],
) -> None:
    quarantine_ids(laminar_trace_uuid("trial-q"))
    hosted, skipped = Signals(), []
    verdicts = laminar_signals.collect(
        [write_trial(tmp_path / "a", "trial-q"), write_trial(tmp_path / "b", "trial-ok")],
        api_key="k", opener=hosted, skipped=skipped,
    )
    assert [verdict["trial"] for verdict in verdicts] == ["trial-ok"]
    assert skipped == [laminar_trace_uuid("trial-q")]
    assert hosted.asked and all(ids == [laminar_trace_uuid("trial-ok")] for ids in hosted.asked)


def test_signal_comparison_skips_quarantined_trials(
    tmp_path: Path, quarantine_ids: Callable[..., None],
) -> None:
    quarantine_ids(laminar_trace_uuid("task-q__aaa"))
    runs = tmp_path / "runs"
    write_trial(runs, "task-q__aaa")
    write_trial(runs, "task-ok__bbb")
    skipped: list[str] = []
    rows = compare_signals(
        runs_dirs=[runs], out_dir=tmp_path / "out", api_key="k", opener=Signals(), skipped=skipped,
    )
    assert [row["trial"] for row in rows] == ["task-ok__bbb"] and skipped == ["task-q__aaa"]


def test_import_signal_state_never_asks_about_quarantined_traces() -> None:
    hosted, skipped = Signals(), []
    state = laminar_import.read_signal_state(
        [QUARANTINED, OK_ID], {"copied_upstream_fix": "s1"}, api_key="k", opener=hosted, skipped=skipped,
    )
    assert state == {} and skipped == [QUARANTINED] and hosted.asked == [[OK_ID], [OK_ID]]


def test_awaiting_only_quarantined_traces_returns_without_a_request_or_wait() -> None:
    hosted, skipped = Signals(), []
    state = laminar_import.await_signal_runs(
        [QUARANTINED], {"copied_upstream_fix": "s1"}, api_key="k", opener=hosted,
        interval_s=0, sleep=lambda _: pytest.fail("must not wait"), skipped=skipped,
    )
    assert state == {} and skipped == [QUARANTINED] and hosted.asked == []


# --- harness adapters over stored ledgers ----------------------------------------------------


def ledger_row(run_id: str, signal: str) -> dict[str, Any]:
    return {
        "schema": "evallab.trace_tool_result/v1", "run_id": run_id, "source": "t", "tool": "laminar",
        "detector": signal, "detector_version": tools.LAMINAR_VERSION, "flag": False,
        "value": {"observations": [{"flag": False}], "conflicting": False},
        "evidence_steps": [], "scan": "scan_id=s1", "at": "2026-10-27T10:00:00+00:00",
    }


def harness_run(run_id: str) -> HarnessRun:
    steps = ({"step_id": 0, "source": "agent", "message": "x"},)
    return HarnessRun(run_id, "test", "DEV", "unknown", steps, True)


@pytest.mark.parametrize(("cls", "signal"), [
    (tools.LaminarDetector, "stuck_loop"),
    (own.LaminarCopyDetector, "copied_upstream_fix"),
])
def test_harness_adapters_skip_and_count_quarantined_runs(
    tmp_path: Path, quarantine_ids: Callable[..., None], cls: Any, signal: str,
) -> None:
    quarantine_ids(laminar_trace_uuid("run-q"))
    path = tmp_path / "ledger.jsonl"
    path.write_text("".join(json.dumps(ledger_row(name, signal)) + "\n" for name in ("run-q", "run-ok")))
    detector = cls(HarnessOptions(database=tmp_path / "missing.sqlite", repo_root=tmp_path), results=path)
    found = [record["run_id"] for record in detector.detect([harness_run("run-q"), harness_run("run-ok")])]
    assert found == ["run-ok"] and detector.quarantined_skipped == 1
