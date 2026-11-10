"""Session-wide pytest hooks: record what the running session actually collected."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zlib
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_claude_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Queue tests receive recorded headroom without probing the host's OMP.

    Patch the queue's imported collaborator only, so tests of the live reader
    still exercise its explicit injected subprocess seam.
    """
    import evallab.queue as queue_module
    from evallab.quota import parse_claude_headroom

    payload = json.loads(
        (Path(__file__).parent / "fixtures/claude-quota/usage-headroom.json").read_text()
    )
    reading = parse_claude_headroom(payload, now=datetime(2026, 10, 13, 4, tzinfo=UTC))
    monkeypatch.setattr(queue_module, "read_claude_headroom", lambda **_kwargs: reading)


@pytest.fixture(autouse=True)
def _isolate_results_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Path, None, None]:
    """Publishing from tests never touches Peter's real results home.

    ``process_job(..., publish=True)`` (the default) resolves the home from
    ``EVALLAB_RESULTS_HOME``; without this fixture a plain call publishes
    into ``~/Developer/eval-lab-results`` and pollutes its INDEX (HAR-117:
    ``unknown-job`` rows with a pytest tmp ``source_path``). Tests that need
    an explicit home keep setting the variable themselves; the last setenv
    wins because this fixture runs first.
    """
    from evallab.results_home import ENV_VAR

    isolated = tmp_path / "results-home"
    monkeypatch.setenv(ENV_VAR, str(isolated))
    yield isolated


@pytest.fixture(autouse=True)
def _isolate_temp_root(monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Every test gets its own tempfile root so nothing lands in the shared
    system temp directory.

    Tests and the children they spawn that call ``tempfile.mkdtemp``,
    ``TemporaryDirectory`` and friends without ``dir=`` used to leave folders
    in the shared system temp directory (HAR-374: 25 from the DeepSeek adapter
    tests alone). Point ``tempfile.tempdir`` at a per-test directory for
    in-process calls and ``TMPDIR`` at the same directory for subprocesses.
    The directory sits directly in the real temp root to keep AF_UNIX socket
    paths under the macOS 104-byte limit, and is removed at teardown.
    ``tempdirb`` is cleared (on 3.13+, where it exists) so a bytes-tempdir
    cached before the redirect is recomputed from the patched root. Broader
    fixtures that still need the shared root — the ``postgres_server``
    servers, whose directories deliberately outlive single tests — capture
    it at module import, before this fixture runs.
    """
    root = Path(tempfile.mkdtemp(prefix="evl-t-")).resolve()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    # tempdirb exists on 3.13+ only; raising=False keeps 3.12 working.
    monkeypatch.setattr(tempfile, "tempdirb", None, raising=False)
    monkeypatch.setenv("TMPDIR", str(root))
    yield root
    shutil.rmtree(root, ignore_errors=True)


_COLLECTED_MODULES: pytest.StashKey[frozenset[str]] = pytest.StashKey()
_DEFAULT_COLLECTION: pytest.StashKey[bool] = pytest.StashKey()
_SHARD_ASSIGNMENT: pytest.StashKey[tuple[int, int] | None] = pytest.StashKey()
_CALL_DURATIONS: pytest.StashKey[dict[str, float]] = pytest.StashKey()

DEFAULT_SLOW_TEST_BUDGET_SECONDS: float = 60.0

# Shard pins that flip a test module's crc32 shard to balance CI shard load.
# Refresh with scripts/ci/propose_shard_pins.py --total 2 over the full-suite.xml
# files of the junit-<python>-<shard> artifacts of one CI run; paste its block.
# Measured in CI run 41761936272 (2026-11-04, seconds averaged over 3.12 and 3.14):
#   - tests/test_trajectory_recipes.py: 0.26 s (hash shard 2); kept, removing it widens the gap.
#   - tests/test_compaction_properties.py: 109.37 s, unpinned (was pinned to shard 2): the
#     pin put it on the heavier shard (harmful); crc32 places it on shard 1.
# Serial seconds shard 1 / shard 2 under these pins: 921.96 / 930.14 averaged.
SHARD_OVERRIDES: dict[str, int] = {
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
    """Print per-test durations (>=1s) and report tests over the slow-test budget."""
    budget = _slow_test_budget_seconds()
    durations = dict(config.stash.get(_CALL_DURATIONS, {}))
    if hasattr(terminalreporter, "stats"):
        for rep_list in terminalreporter.stats.values():
            for rep in rep_list:
                if getattr(rep, "when", None) == "call" and hasattr(rep, "duration"):
                    durations.setdefault(rep.nodeid, rep.duration)

    slow = [(nodeid, dur) for nodeid, dur in durations.items() if dur > budget]

    # HAR-354: Always print every duration >= 1 s (equivalent of --durations with
    # --durations-min) so CI shard logs carry per-test timing evidence; the junit
    # XML is not uploaded.
    slowest = sorted(
        ((nodeid, dur) for nodeid, dur in durations.items() if dur >= 1.0),
        key=lambda item: item[1],
        reverse=True,
    )
    if slowest:
        terminalreporter.section("Slowest test durations")
        for nodeid, dur in slowest:
            terminalreporter.write_line(f"{nodeid}: {dur:.2f}s")

    if not slow:
        return

    slow.sort(key=lambda item: item[1], reverse=True)
    budget_label = f"{int(budget)}s" if budget.is_integer() else f"{budget:g}s"
    terminalreporter.section(f"Slow tests (budget {budget_label})")
    for nodeid, dur in slow:
        terminalreporter.write_line(f"{nodeid}: {dur:.2f}s")
