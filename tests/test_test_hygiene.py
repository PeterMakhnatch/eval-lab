"""Unit tests for conftest test hygiene: shard overrides and slow-test budget reporting."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import zlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Load the local tests/conftest.py directly via importlib to avoid polluting
# sys.path or creating a top-level 'tests' package collision with research/*/tests.
_CONFTEST_PATH = Path(__file__).resolve().parent / "conftest.py"
_spec = importlib.util.spec_from_file_location("local_tests_conftest", _CONFTEST_PATH)
assert _spec is not None and _spec.loader is not None
_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_conftest)

_CALL_DURATIONS = _conftest._CALL_DURATIONS
SHARD_OVERRIDES = _conftest.SHARD_OVERRIDES
_slow_test_budget_seconds = _conftest._slow_test_budget_seconds
pytest_runtest_makereport = _conftest.pytest_runtest_makereport
pytest_terminal_summary = _conftest.pytest_terminal_summary


class FakeConfig:
    """Minimal pytest Config stand-in with a Stash."""

    def __init__(self) -> None:
        self.stash = pytest.Stash()


class FakeItem:
    """Minimal pytest Item stand-in carrying config."""

    def __init__(self, config: FakeConfig) -> None:
        self.config = config


class FakeReport:
    """Minimal pytest TestReport stand-in with nodeid, duration, and when."""

    def __init__(self, nodeid: str, duration: float, when: str = "call") -> None:
        self.nodeid = nodeid
        self.duration = duration
        self.when = when


class FakeTerminalReporter:
    """Minimal TerminalReporter capturing sections and lines."""

    def __init__(self) -> None:
        self.sections: list[str] = []
        self.lines: list[str] = []
        self.stats: dict[str, list[Any]] = {}

    def section(self, title: str, **kwargs: Any) -> None:
        self.sections.append(title)

    def write_line(self, line: str) -> None:
        self.lines.append(line)


def test_shard_overrides_balance_heavy_modules() -> None:
    """Pins must place heavy modules on opposite shards, overriding hash defaults."""
    assert "tests/test_compaction_properties.py" in SHARD_OVERRIDES
    assert "tests/test_trajectory_recipes.py" in SHARD_OVERRIDES

    compaction_shard = SHARD_OVERRIDES["tests/test_compaction_properties.py"]
    trajectory_shard = SHARD_OVERRIDES["tests/test_trajectory_recipes.py"]

    # Compaction and trajectory recipes must be on opposite shards
    assert compaction_shard != trajectory_shard
    assert {compaction_shard, trajectory_shard} == {1, 2}

    # Compaction was on shard 1 by hash; pin must flip it to shard 2
    compaction_hash_shard = (zlib.crc32(b"tests/test_compaction_properties.py") % 2) + 1
    assert compaction_hash_shard == 1
    assert compaction_shard == 2

    # Trajectory recipes was on shard 2 by hash; pin must flip it to shard 1
    trajectory_hash_shard = (zlib.crc32(b"tests/test_trajectory_recipes.py") % 2) + 1
    assert trajectory_hash_shard == 2
    assert trajectory_shard == 1


def test_slow_test_budget_makereport_records_call_duration() -> None:
    """makereport hook must record call-phase durations into config stash."""
    config = FakeConfig()
    item = FakeItem(config)

    # Call phase should be recorded
    gen = pytest_runtest_makereport(item, None)  # type: ignore[arg-type]
    next(gen)
    report_call = FakeReport("tests/test_foo.py::test_slow", 72.5, when="call")
    try:
        gen.send(report_call)  # type: ignore[arg-type]
    except StopIteration as stop:
        returned_report = stop.value

    assert returned_report is report_call
    durations = config.stash.get(_CALL_DURATIONS, {})
    assert durations.get("tests/test_foo.py::test_slow") == 72.5

    # Setup phase should not overwrite call duration
    gen_setup = pytest_runtest_makereport(item, None)  # type: ignore[arg-type]
    next(gen_setup)
    report_setup = FakeReport("tests/test_foo.py::test_slow", 0.1, when="setup")
    with contextlib.suppress(StopIteration):
        gen_setup.send(report_setup)  # type: ignore[arg-type]
    assert durations.get("tests/test_foo.py::test_slow") == 72.5


def test_slow_test_budget_terminal_summary_reports_over_budget() -> None:
    """Calls exceeding 60s budget must be reported with their duration."""
    config = FakeConfig()
    config.stash[_CALL_DURATIONS] = {
        "tests/test_compaction.py::test_heavy": 75.34,
        "tests/test_fast.py::test_quick": 1.20,
    }

    tr = FakeTerminalReporter()
    with patch.dict(os.environ, {}, clear=True):
        pytest_terminal_summary(tr, pytest.ExitCode.OK, config)  # type: ignore[arg-type]

    assert tr.sections == ["Slow tests (budget 60s)"]
    assert len(tr.lines) == 1
    assert tr.lines[0] == "tests/test_compaction.py::test_heavy: 75.34s"


def test_slow_test_budget_terminal_summary_omitted_when_all_fast() -> None:
    """When no calls exceed the budget, the slow-tests section is omitted."""
    config = FakeConfig()
    config.stash[_CALL_DURATIONS] = {
        "tests/test_a.py::test_1": 5.0,
        "tests/test_b.py::test_2": 59.9,
    }

    tr = FakeTerminalReporter()
    with patch.dict(os.environ, {}, clear=True):
        pytest_terminal_summary(tr, pytest.ExitCode.OK, config)  # type: ignore[arg-type]

    assert tr.sections == []
    assert tr.lines == []


def test_slow_test_budget_configurable_via_env() -> None:
    """EVALLAB_SLOW_TEST_BUDGET_SECONDS must override threshold and section header."""
    with patch.dict(os.environ, {"EVALLAB_SLOW_TEST_BUDGET_SECONDS": "10"}):
        assert _slow_test_budget_seconds() == 10.0

        config = FakeConfig()
        config.stash[_CALL_DURATIONS] = {
            "tests/test_mid.py::test_medium": 15.2,
            "tests/test_fast.py::test_quick": 2.0,
        }

        tr = FakeTerminalReporter()
        pytest_terminal_summary(tr, pytest.ExitCode.OK, config)  # type: ignore[arg-type]

        assert tr.sections == ["Slow tests (budget 10s)"]
        assert tr.lines == ["tests/test_mid.py::test_medium: 15.20s"]


def test_slow_test_budget_reads_xdist_stats() -> None:
    """Under xdist, reports collected in terminalreporter.stats must be processed."""
    config = FakeConfig()  # empty stash
    tr = FakeTerminalReporter()
    tr.stats = {
        "passed": [
            FakeReport("tests/test_worker.py::test_heavy", 88.0, when="call"),
            FakeReport("tests/test_worker.py::test_light", 0.5, when="call"),
        ]
    }

    with patch.dict(os.environ, {}, clear=True):
        pytest_terminal_summary(tr, pytest.ExitCode.OK, config)  # type: ignore[arg-type]

    assert tr.sections == ["Slow tests (budget 60s)"]
    assert tr.lines == ["tests/test_worker.py::test_heavy: 88.00s"]
