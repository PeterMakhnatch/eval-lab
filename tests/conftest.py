"""Session-wide pytest hooks: record what the running session actually collected."""

from __future__ import annotations

import os
import zlib
from collections.abc import Generator
from pathlib import Path

import pytest

_COLLECTED_MODULES: pytest.StashKey[frozenset[str]] = pytest.StashKey()
_DEFAULT_COLLECTION: pytest.StashKey[bool] = pytest.StashKey()
_SHARD_ASSIGNMENT: pytest.StashKey[tuple[int, int] | None] = pytest.StashKey()
_CALL_DURATIONS: pytest.StashKey[dict[str, float]] = pytest.StashKey()

DEFAULT_SLOW_TEST_BUDGET_SECONDS: float = 60.0

# Shard pins for heavy test modules to balance CI execution times across shards.
# Measured CI durations (as of 2026-09-06):
#   - tests/test_compaction_properties.py: ~75s
#   - tests/test_trajectory_recipes.py: ~40s
# Rule: Update these pins whenever a module's CI runtime changes by >30s.
SHARD_OVERRIDES: dict[str, int] = {
    "tests/test_compaction_properties.py": 2,
    "tests/test_trajectory_recipes.py": 1,
}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        action="store",
        default=None,
        metavar="INDEX/TOTAL",
        help="Run tests in shard INDEX/TOTAL (e.g. 1/2, 2/2).",
    )


def _parse_shard_option(value: str) -> tuple[int, int]:
    index_text, sep, total_text = value.partition("/")
    if not sep or not index_text.isdigit() or not total_text.isdigit():
        raise pytest.UsageError(f"--shard expects INDEX/TOTAL such as 1/2, got {value!r}")
    index, total = int(index_text), int(total_text)
    if not 1 <= index <= total:
        raise pytest.UsageError(f"--shard {value!r}: INDEX must satisfy 1 <= INDEX <= TOTAL")
    return index, total


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Record collected module paths and whether this was a bare (testpaths) invocation.

    Under pytest-xdist every worker collects the full set before receiving its
    share, so the record is complete in workers as well as in a serial run.
    """
    root = config.rootpath.resolve()
    collected: set[str] = set()
    for item in items:
        path = Path(item.path).resolve()
        try:
            collected.add(path.relative_to(root).as_posix())
        except ValueError:
            collected.add(path.as_posix())
    config.stash[_COLLECTED_MODULES] = frozenset(collected)
    config.stash[_DEFAULT_COLLECTION] = not config.option.file_or_dir

    shard_option: str | None = config.getoption("shard", default=None)
    if not shard_option:
        config.stash[_SHARD_ASSIGNMENT] = None
        return

    index, total = _parse_shard_option(shard_option)
    config.stash[_SHARD_ASSIGNMENT] = (index, total)

    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        path = Path(item.path).resolve()
        try:
            rel_path = path.relative_to(root).as_posix()
        except ValueError:
            rel_path = path.as_posix()

        if rel_path == "tests/test_ci_coverage.py":
            kept.append(item)
        else:
            override = SHARD_OVERRIDES.get(rel_path)
            if override is not None and override <= total:
                item_shard = override
            else:
                item_shard = (zlib.crc32(rel_path.encode("utf-8")) % total) + 1
            if item_shard == index:
                kept.append(item)
            else:
                deselected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = kept


@pytest.fixture(scope="session")
def collected_module_paths(request: pytest.FixtureRequest) -> frozenset[str]:
    """Repo-relative paths of every module the running session collected."""
    return request.config.stash[_COLLECTED_MODULES]


@pytest.fixture(scope="session")
def default_collection_session(request: pytest.FixtureRequest) -> bool:
    """True when no positional paths were given, i.e. `testpaths` drove collection."""
    return request.config.stash[_DEFAULT_COLLECTION]


@pytest.fixture(scope="session")
def shard_assignment(request: pytest.FixtureRequest) -> tuple[int, int] | None:
    """The (index, total) shard assignment tuple if --shard was given, otherwise None."""
    return request.config.stash.get(_SHARD_ASSIGNMENT, None)


def _slow_test_budget_seconds() -> float:
    raw = os.environ.get("EVALLAB_SLOW_TEST_BUDGET_SECONDS", "")
    if not raw:
        return DEFAULT_SLOW_TEST_BUDGET_SECONDS
    try:
        val = float(raw)
        return val if val > 0 else DEFAULT_SLOW_TEST_BUDGET_SECONDS
    except ValueError:
        return DEFAULT_SLOW_TEST_BUDGET_SECONDS


@pytest.hookimpl(trylast=True, wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Record per-test call durations to track against the session slow-test budget."""
    report: pytest.TestReport = yield
    if report.when == "call":
        item.config.stash.setdefault(_CALL_DURATIONS, {})[report.nodeid] = report.duration
    return report


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: pytest.ExitCode,
    config: pytest.Config,
) -> None:
    """Report tests that exceeded the session slow-test budget."""
    budget = _slow_test_budget_seconds()
    durations = dict(config.stash.get(_CALL_DURATIONS, {}))
    if hasattr(terminalreporter, "stats"):
        for rep_list in terminalreporter.stats.values():
            for rep in rep_list:
                if getattr(rep, "when", None) == "call" and hasattr(rep, "duration"):
                    durations.setdefault(rep.nodeid, rep.duration)

    slow = [(nodeid, dur) for nodeid, dur in durations.items() if dur > budget]
    if not slow:
        return

    slow.sort(key=lambda item: item[1], reverse=True)
    budget_label = f"{int(budget)}s" if budget.is_integer() else f"{budget:g}s"
    terminalreporter.section(f"Slow tests (budget {budget_label})")
    for nodeid, dur in slow:
        terminalreporter.write_line(f"{nodeid}: {dur:.2f}s")
