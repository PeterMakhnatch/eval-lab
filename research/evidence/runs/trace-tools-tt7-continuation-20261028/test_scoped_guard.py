"""Offline quota-boundary controls; no host credentials, network or model calls."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
NOW_MS = 2_000_000
READ = ["omp", "usage", "--provider", "anthropic", "--json", "--redact"]
INVALIDATE = ["omp", "usage", "invalidate", "--provider", "anthropic"]


def load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    common = load(HERE / "common.py", "tt7_scoped_common_test")
    monkeypatch.setitem(sys.modules, "common", common)
    module = load(HERE / "run_omp.py", "tt7_scoped_runner_test")
    module.PRIVATE = tmp_path
    module.time = SimpleNamespace(time=lambda: NOW_MS / 1000)
    module.os = SimpleNamespace(environ={"EXAMPLE_API_KEY": "synthetic", "KEEP": "visible"})
    return module


def report(age_ms: int = 0, *, weekly: bool = True) -> dict[str, Any]:
    limits = [{"id": "anthropic:5h", "amount": {"usedFraction": 0.2}, "scope": {}}]
    if weekly:
        limits.append({"id": "anthropic:7d", "amount": {"usedFraction": 0.6}, "scope": {}})
    return {"reports": [{"provider": "anthropic", "fetchedAt": NOW_MS - age_ms, "limits": limits}]}


def transport(runner: Any, replies: list[dict[str, Any]]) -> list[list[str]]:
    calls = []
    queue = iter(replies)

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert "EXAMPLE_API_KEY" not in kwargs["env"]
        assert kwargs["env"]["KEEP"] == "visible"
        if argv[:3] == ["omp", "usage", "invalidate"]:
            assert argv == INVALIDATE  # Never unscoped or a positional reset target.
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert argv == READ
        return subprocess.CompletedProcess(argv, 0, json.dumps(next(queue)), "")

    runner.subprocess = SimpleNamespace(run=run)
    return calls


def test_stale_cache_is_invalidated_only_for_requested_provider(runner: Any) -> None:
    calls = transport(runner, [report(120_001), report()])
    snapshot = runner.refresh_usage("anthropic")
    assert calls == [READ, INVALIDATE, READ]
    assert snapshot["fetched_at_ms"] == NOW_MS
    assert snapshot["maximum_fraction"] == 0.6
    assert {row["id"] for row in snapshot["limits"]} == {"anthropic:5h", "anthropic:7d"}


def test_fast_successful_cli_does_not_make_stale_telemetry_fresh(runner: Any) -> None:
    calls = transport(runner, [report(120_001), report(120_001)])
    with pytest.raises(runner.RouteError, match="older than 120 seconds"):
        runner.refresh_usage("anthropic")
    assert calls == [READ, INVALIDATE, READ]
    assert not (runner.PRIVATE / "usage.jsonl").exists()


def test_fresh_but_missing_weekly_window_fails_without_forced_invalidation(runner: Any) -> None:
    calls = transport(runner, [report(weekly=False)])
    with pytest.raises(runner.RouteError, match="both five-hour and weekly"):
        runner.refresh_usage("anthropic")
    assert calls == [READ]
