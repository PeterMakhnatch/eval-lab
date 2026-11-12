"""Quarantined Laminar copies are refused or skipped, and counted, on every Laminar path.

All seams are injected (openers, transports, uploaders, ledgers): no network, no host state.
"""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from evallab.laminar import (
    SIGNAL_OPT_IN_FILTER,
    SIGNALS,
    apply_signals,
    compare_signals,
    laminar_trace_uuid,
)
from evallab.production import trace_harness_own as own
from evallab.production import trace_harness_tools as tools
from evallab.production.trace_harness import HarnessOptions, HarnessRun
from evallab.readers import laminar_signals
from evallab.trace_store import laminar_quarantine as quarantine
from evallab.trace_store.core import trace_uuid
from evallab.trace_store.laminar_quarantine import QuarantinedTrace

#: A real quarantined id, used as-is (a Laminar trace id) on every path.
QUARANTINED = min(quarantine.QUARANTINED_REMOTE_IDS)
OK_ID = str(uuid.UUID(int=0x1234))


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


class Hosted:
    """Read-only SQL seam for the retained Signal quarantine guard."""

    def __init__(self, *, tagged: list[dict[str, Any]]) -> None:
        self.tagged = tagged
        self.requests: list[str] = []

    def __call__(self, request: Any, timeout: int) -> io.BytesIO:
        self.requests.append(request.full_url)
        if request.full_url.endswith("/v1/sql/query"):
            return io.BytesIO(json.dumps({"data": self.tagged}).encode())
        raise AssertionError("Quarantined copies must prevent Signal writes")


def test_every_signal_definition_requires_the_opt_in_tag() -> None:
    # No trace-id predicate exists in a Signal filter, so the tag is the only exclusion.
    assert all(SIGNAL_OPT_IN_FILTER in definition["filters"] for definition in SIGNALS)


def test_apply_signals_refuses_when_a_quarantined_copy_is_selectable() -> None:
    hosted = Hosted(tagged=[{"trace_id": QUARANTINED}])
    with pytest.raises(QuarantinedTrace):
        apply_signals(api_key="k", opener=hosted)
    assert [url for url in hosted.requests if not url.endswith("/v1/sql/query")] == []


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
