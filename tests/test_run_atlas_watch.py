"""Deterministic watcher checks: native SQLite columns, no live model or state."""

from __future__ import annotations

import argparse
import json
import plistlib
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.production import run_atlas
from evallab.production import run_atlas_watch as watch

# Column names and constraints match the read-only production .schema.
SCHEMA = """
CREATE TABLE corpus_tasks (
    task_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, split TEXT NOT NULL,
    instruction_text TEXT NOT NULL, original_grader_digest TEXT,
    repaired_grader_digest TEXT, cas_uri TEXT, metadata_json TEXT NOT NULL
);
CREATE TABLE agent_traces (
    trace_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES corpus_tasks(task_id), model_id TEXT NOT NULL,
    outcome TEXT NOT NULL, reward DOUBLE PRECISION, is_exploit INTEGER NOT NULL,
    is_task_defect_fail INTEGER NOT NULL, step_count INTEGER NOT NULL CHECK(step_count >= 0),
    raw_cas_uri TEXT, verified_receipt_json TEXT,
    trust_tier TEXT NOT NULL DEFAULT 'tier3_provisional',
    flagged_passage_steps TEXT NOT NULL DEFAULT '[]'
){without_rowid};
CREATE TABLE trace_steps (
    trace_id TEXT NOT NULL REFERENCES agent_traces(trace_id) ON DELETE CASCADE,
    step_index INTEGER NOT NULL CHECK(step_index >= 0), role TEXT NOT NULL,
    tool_name TEXT, command_or_action TEXT, content_text TEXT,
    observation_preview TEXT, observation_cas_uri TEXT, flags_json TEXT NOT NULL,
    PRIMARY KEY(trace_id, step_index)
);
"""


def _database(path: Path, *, without_rowid: bool = False) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA.format(without_rowid=" WITHOUT ROWID" if without_rowid else ""))
    return path


def _insert_trace(database: Path, trace_id: str, *, task: str = "dev", split: str = "DEV") -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO corpus_tasks "
            "(task_id, dataset_id, split, instruction_text, metadata_json) VALUES (?, ?, ?, ?, ?)",
            (task, "fixture-local", split, "Implement a counter.", json.dumps({"item_id": task})),
        )
        connection.execute(
            "INSERT INTO agent_traces "
            "(trace_id, run_id, task_id, model_id, outcome, reward, is_exploit, "
            "is_task_defect_fail, step_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trace_id, "campaign", task, "fixture/model", "pass", 1.0, 0, 0, 1),
        )
        connection.execute(
            "INSERT INTO trace_steps (trace_id, step_index, role, content_text, flags_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (trace_id, 7, "assistant", "Implemented the counter and checked its output.", "{}"),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    watch.add_watch_parser(parser.add_subparsers(dest="action", required=True))
    return parser


def _unexpected(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("unexpected external process or gateway")


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_atlas, "ATLAS_ROOT", tmp_path / "atlas-state")
    monkeypatch.setattr(run_atlas, "GatewayTransport", _unexpected)
    monkeypatch.setattr(watch.subprocess, "run", _unexpected)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    database = _database(tmp_path / "traces.sqlite")
    args = _parser().parse_args([
        "watch", "--database", str(database), "--facets", str(tmp_path / "facets.jsonl"),
        "--calls", str(tmp_path / "calls.jsonl"), "--route", "openai-codex/fixture", "--once",
    ])
    state = SimpleNamespace(args=args, root=tmp_path, calls=[], closed=0, fail=False)

    class FakeTransport:
        def complete(
            self, model: str, messages: Any, *, max_tokens: int,
        ) -> run_atlas.Completion:
            state.calls.append((model, messages, max_tokens))
            if state.fail:
                raise run_atlas.TransportError("intentional failure")
            form = {
                "goal": "Implement a counter", "approach": "Edited the implementation and checked output",
                "tests": "untouched", "grader_or_harness_touched": False,
                "answer_material_accessed": False, "success_claim_without_evidence": False,
                "eval_awareness": "none", "shortcut": "none", "suspicion": 0,
                "evidence_steps": [0],
            }
            return run_atlas.Completion(json.dumps(form), model, 11, 7, 0.0)

        def close(self) -> None:
            state.closed += 1

    monkeypatch.setattr(run_atlas, "GatewayTransport", FakeTransport)
    monkeypatch.setattr(watch, "routes_with_headroom", lambda routes: (list(routes), [], "fixture"))
    monkeypatch.setattr(watch, "CorpusRegistry", lambda root: SimpleNamespace(repo_root=root))

    def admitted(connection: sqlite3.Connection, registry: Any) -> frozenset[str]:
        assert registry.repo_root == tmp_path
        return frozenset(
            row["task_id"] for row in connection.execute("SELECT task_id FROM corpus_tasks")
            if row["task_id"] != "sealed"
        )

    monkeypatch.setattr(run_atlas, "admitted_task_ids", admitted)
    return state


def _cycle(rig: SimpleNamespace) -> dict[str, Any]:
    with watch.watch_lock(run_atlas.ATLAS_ROOT):
        return watch.watch_cycle(rig.args, rig.root)


def test_registration_is_inert_and_options_are_complete() -> None:
    args = _parser().parse_args([
        "watch", "--route", "openai-codex/a", "--route", "anthropic/b", "--dry-run",
    ])
    assert args.func is watch._watch_command
    assert args.routes == ["openai-codex/a", "anthropic/b"]
    assert args.interval == 300
    assert args.max_per_cycle == 25
    assert args.max_failures == 3
    assert args.per_route == 1
    assert args.max_tokens_per_day is None
    assert not run_atlas.ATLAS_ROOT.exists()


def test_new_committed_dev_trace_is_faceted_on_next_cycle(rig: SimpleNamespace) -> None:
    assert _cycle(rig)["pending"] == 0
    _insert_trace(rig.args.database, "new-run")
    result = _cycle(rig)
    assert result["written"] == 1
    assert result["tokens_today"] == 18
    assert run_atlas.load_facets(rig.args.facets)["new-run"]["evidence_steps"] == [7]
    assert len(rig.calls) == 1
    assert rig.closed == 1
    assert _cycle(rig)["pending"] == 0
    assert len(rig.calls) == 1


def test_existing_facets_test_and_cross_key_sealed_tasks_are_never_read(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_trace(rig.args.database, "existing")
    _insert_trace(rig.args.database, "test-trace", task="test", split="TEST")
    _insert_trace(rig.args.database, "sealed-trace", task="sealed", split="DEV")
    _insert_trace(rig.args.database, "allowed")
    run_atlas.append_jsonl(rig.args.facets, [{"run_id": "existing"}])
    seen = []
    original = run_atlas.cloud_doc

    def cloud_doc(connection: sqlite3.Connection, trace_id: str) -> dict[str, Any]:
        seen.append(trace_id)
        assert trace_id == "allowed"
        return original(connection, trace_id)

    monkeypatch.setattr(run_atlas, "cloud_doc", cloud_doc)
    result = _cycle(rig)
    assert result["pending"] == result["written"] == 1
    assert seen == ["allowed"]
    assert set(run_atlas.load_facets(rig.args.facets)) == {"existing", "allowed"}


def test_max_per_cycle_prefers_newest_not_lexical_trace_id(rig: SimpleNamespace) -> None:
    _insert_trace(rig.args.database, "z-older")
    _insert_trace(rig.args.database, "a-newer")
    rig.args.max_per_cycle = 1
    result = _cycle(rig)
    assert result["pending"] == 2
    assert result["selected"] == result["written"] == 1
    assert set(run_atlas.load_facets(rig.args.facets)) == {"a-newer"}
    assert _cycle(rig)["written"] == 1


def test_without_rowid_falls_back_to_stable_trace_order(
    rig: SimpleNamespace, tmp_path: Path,
) -> None:
    rig.args.database = _database(tmp_path / "without-rowid.sqlite", without_rowid=True)
    _insert_trace(rig.args.database, "z-first")
    _insert_trace(rig.args.database, "a-second")
    pending = watch.pending_trace_ids(
        rig.args.database, registry=watch.CorpusRegistry(rig.root), facets=rig.args.facets,
        failures={}, max_failures=3,
    )
    assert pending == ["a-second", "z-first"]


def test_repeated_failures_stop_at_persisted_threshold(rig: SimpleNamespace) -> None:
    _insert_trace(rig.args.database, "bad-run")
    rig.fail = True
    for _ in range(rig.args.max_failures):
        assert _cycle(rig)["failed"] == 1
    assert _cycle(rig)["pending"] == 0
    assert len(rig.calls) == 3
    path = run_atlas.ATLAS_ROOT / "watch-state.json"
    assert watch.load_failures(path) == {"bad-run": 3}
    assert json.loads(path.read_text())["version"] == 1
    assert {file.name for file in path.parent.iterdir()} == {"watch-state.json", "watch.lock"}


def test_a_success_removes_old_failure_state(rig: SimpleNamespace) -> None:
    _insert_trace(rig.args.database, "retried")
    watch.save_failures(run_atlas.ATLAS_ROOT / "watch-state.json", {"retried": 2})
    assert _cycle(rig)["written"] == 1
    assert watch.load_failures(run_atlas.ATLAS_ROOT / "watch-state.json") == {}


def test_lock_prevents_second_instance_and_is_released(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str],
) -> None:
    _insert_trace(rig.args.database, "run")
    with watch.watch_lock(run_atlas.ATLAS_ROOT):
        assert rig.args.func(rig.args, rig.root) == 1
    assert "already running" in capsys.readouterr().err
    assert rig.calls == []
    assert rig.args.func(rig.args, rig.root) == 0
    assert len(rig.calls) == 1


def test_dry_run_is_read_only_and_exits_without_once(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str],
) -> None:
    _insert_trace(rig.args.database, "pending")
    rig.args.dry_run = True
    rig.args.once = False
    assert rig.args.func(rig.args, rig.root) == 0
    assert json.loads(capsys.readouterr().out)["pending"] == 1
    assert rig.calls == []
    assert not run_atlas.ATLAS_ROOT.exists()
    assert not rig.args.facets.exists()
    assert not rig.args.calls.exists()


def test_daily_budget_prevents_entire_cycle_without_starting_gateway(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_trace(rig.args.database, "pending")
    run_atlas.append_jsonl(rig.args.calls, [{
        "at": datetime.now(UTC).isoformat(), "input_tokens": 60, "output_tokens": 40,
    }])
    rig.args.max_tokens_per_day = 100
    monkeypatch.setattr(run_atlas, "GatewayTransport", _unexpected)
    result = _cycle(rig)
    assert result["budget_exhausted"]
    assert result["selected"] == result["written"] == 0
    assert not rig.args.facets.exists()


def test_daily_tokens_are_summed_by_utc_date(tmp_path: Path) -> None:
    calls = tmp_path / "receipts.jsonl"
    now = datetime(2026, 10, 28, 1, tzinfo=UTC)
    run_atlas.append_jsonl(calls, [
        {"at": "2026-10-27T23:30:00-04:00", "input_tokens": 7, "output_tokens": 3},
        {"at": "2026-10-27T23:30:00+00:00", "input_tokens": 900, "output_tokens": 100},
        {"purpose": "run", "run_id": "x", "status": "ok"},
        {"at": now.isoformat(), "status": "error"},
    ])
    assert watch.tokens_used_today(calls, now=now) == 10
    assert watch.tokens_used_today(calls, now=now + timedelta(days=1)) == 0


def test_requests_that_do_not_fit_budget_are_not_trace_failures(rig: SimpleNamespace) -> None:
    _insert_trace(rig.args.database, "oversized")
    rig.args.max_tokens_per_day = 1
    result = _cycle(rig)
    assert result["budget_exhausted"]
    assert result["budget_skipped"] == 1
    assert result["failed"] == result["written"] == 0
    assert rig.calls == []
    assert watch.load_failures(run_atlas.ATLAS_ROOT / "watch-state.json") == {}


def test_budget_reserves_concurrent_calls_before_their_receipts() -> None:
    entered = threading.Event()
    release = threading.Event()

    class Transport:
        def complete(self, model: str, messages: Any, *, max_tokens: int) -> run_atlas.Completion:
            entered.set()
            assert release.wait(timeout=5)
            return run_atlas.Completion("{}", model, 3, 2, 0.0)

    budget = watch._BudgetTransport(Transport(), used=0, maximum=2000)
    errors = []

    def first_call() -> None:
        try:
            budget.complete("openai-codex/fixture", [], max_tokens=500)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=first_call)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(watch.DailyTokenBudgetExceeded):
            budget.complete("openai-codex/fixture", [], max_tokens=500)
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    assert budget.used == 5
    assert budget.reserved == 0


def test_headroom_skips_provider_at_ninety_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = {"reports": [
        {"provider": "anthropic", "limits": [
            {"window": {"id": "5h"}, "amount": {"usedFraction": 0.9}},
        ]},
        {"provider": "openai-codex", "limits": [
            {"window": {"id": "7d"}, "amount": {"used": 89, "limit": 100}},
        ]},
    ]}
    monkeypatch.setattr(watch.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=json.dumps(snapshot)))
    routes, blocked, mode = watch.routes_with_headroom(["anthropic/a", "openai-codex/b", "anthropic/c"])
    assert routes == ["openai-codex/b"]
    assert blocked == ["anthropic"]
    assert mode == "omp-usage-json"


def test_unavailable_usage_falls_back_to_route_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("omp usage", timeout=30)

    monkeypatch.setattr(watch.subprocess, "run", unavailable)
    assert watch.routes_with_headroom(["xai-oauth/a"]) == (["xai-oauth/a"], [], "route-pool-quota-only")


def test_no_provider_headroom_does_not_start_gateway(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_trace(rig.args.database, "pending")
    monkeypatch.setattr(watch, "routes_with_headroom", lambda _: ([], ["openai-codex"], "fixture"))
    monkeypatch.setattr(run_atlas, "GatewayTransport", _unexpected)
    result = _cycle(rig)
    assert result["headroom_exhausted"]
    assert result["selected"] == 0
    assert not (run_atlas.ATLAS_ROOT / "watch-state.json").exists()


@pytest.mark.parametrize("option,value", [
    ("--per-route", "0"), ("--per-route", "9"), ("--interval", "0"),
    ("--interval", "nan"), ("--max-per-cycle", "0"), ("--max-failures", "0"),
    ("--max-tokens-per-day", "-1"),
])
def test_invalid_limits_fail_before_io(option: str, value: str, tmp_path: Path) -> None:
    args = _parser().parse_args(["watch", option, value, "--dry-run"])
    with pytest.raises(ValueError):
        args.func(args, tmp_path)
    assert not run_atlas.ATLAS_ROOT.exists()


def test_non_subscription_route_is_rejected_before_io(tmp_path: Path) -> None:
    args = _parser().parse_args(["watch", "--route", "openai/paid", "--dry-run"])
    with pytest.raises(ValueError, match="subscription route"):
        args.func(args, tmp_path)
    assert not run_atlas.ATLAS_ROOT.exists()


def test_corrupt_state_is_not_silently_reset(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"version":1,"failures":{"x":-1}}')
    with pytest.raises(ValueError, match="invalid watcher failures"):
        watch.load_failures(path)
    path.write_text('{"version":2,"failures":{}}')
    with pytest.raises(ValueError, match="invalid watcher state"):
        watch.load_failures(path)


def test_launchd_definition_matches_existing_template_convention() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts/ops/launchd/com.petermakhnatch.evallab.run-atlas-watch.plist"
    with path.open("rb") as handle:
        service = plistlib.load(handle)
    assert service["ProgramArguments"][:3] == ["__EVALLAB__", "atlas", "watch"]
    assert service["KeepAlive"] is service["RunAtLoad"] is True
    assert service["ThrottleInterval"] == 30
    assert "--max-tokens-per-day" in service["ProgramArguments"]
    assert "__HOME__/.local/bin" in service["EnvironmentVariables"]["PATH"]
