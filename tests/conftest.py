"""Session-wide pytest hooks: record what the running session actually collected."""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import zlib
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
def _isolate_host_credential_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credential discovery never starts host CLIs or queries the Keychain.

    ``available_credentials`` calls these module-level probes, so an unstubbed
    caller started the real ``cursor-agent`` and ``agy`` CLIs (HAR-373). Their
    probe classes keep their own tests through injected runners.
    """
    from evallab import profiles

    for name in (
        "probe_claude_keychain",
        "probe_claude_gateway",
        "probe_cursor_session",
    ):
        monkeypatch.setattr(profiles, name, lambda: False)


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


_PENDING_TEMP_ROOTS: list[Path] = []


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
    paths under the macOS 104-byte limit.
    ``tempdirb`` is cleared (on 3.13+, where it exists) so a bytes-tempdir
    cached before the redirect is recomputed from the patched root. Broader
    fixtures that still need the shared root — the ``postgres_server``
    servers, whose directories deliberately outlive single tests — capture
    it at module import, before this fixture runs.

    Removal is deferred to the session sweep below: tests may patch
    ``os.lstat``/``os.scandir`` through the shared ``monkeypatch`` fixture,
    and ``monkeypatch`` tears down after this fixture, so ``shutil.rmtree``
    here would run against patched ``os`` functions.
    """
    root = Path(tempfile.mkdtemp(prefix="evl-t-")).resolve()
    _PENDING_TEMP_ROOTS.append(root)
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    # tempdirb exists on 3.13+ only; raising=False keeps 3.12 working.
    monkeypatch.setattr(tempfile, "tempdirb", None, raising=False)
    monkeypatch.setenv("TMPDIR", str(root))
    yield root


@pytest.fixture(autouse=True)
def _isolate_host_cli_processes(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    _isolate_temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only per-test fake executables may launch under host CLI names.

    Covers ``subprocess.Popen`` (and so ``subprocess.run``, ``os.popen`` and
    asyncio), shell command strings, ``os.system``, ``os.posix_spawn`` and
    ``os.posix_spawnp``. Shell strings are checked by command word only, so
    arguments such as ``git commit -m 'claude'`` stay allowed. ``os.exec*``
    is not wrapped: it replaces the pytest process itself.
    """
    denied = {
        "codex",
        "claude",
        "agy",
        "cursor-agent",
        "opencode",
        "omp",
        "security",
        "gemini",
    }
    # Tokens after which the shell expects a new command word.
    shell_prefixes = {"!", "{", "if", "then", "else", "elif", "do", "while", "until", "exec"}
    roots = (tmp_path.resolve(), _isolate_temp_root.resolve())
    original_init = subprocess.Popen.__init__
    original_which = shutil.which
    original_system = os.system
    original_posix_spawn = os.posix_spawn
    original_posix_spawnp = os.posix_spawnp
    signature = inspect.signature(original_init)

    def check_executable(executable: Any, cwd: Any, env: Any, *, search: bool = True) -> None:
        executable = os.fsdecode(executable)
        cwd = Path(os.fsdecode(cwd or os.getcwd())).resolve()
        if os.path.dirname(executable) or not search:
            resolved = (cwd / executable).resolve()
        else:
            # Relative PATH entries use the child's cwd, not the pytest cwd.
            search_path = os.pathsep.join(
                str(cwd / entry) for entry in os.get_exec_path(env)
            )
            found = original_which(executable, path=search_path)
            resolved = Path(found).resolve() if found is not None else None
        binary = Path(executable).name
        if binary not in denied and resolved is not None:
            binary = resolved.name
        if binary in denied and (
            resolved is None or not any(resolved.is_relative_to(root) for root in roots)
        ):
            pytest.fail(
                f"Blocked host CLI {binary!r} in {request.node.nodeid}: "
                f"{resolved or executable} is outside this test's temporary roots",
                pytrace=False,
            )

    def check_shell_command(command: Any, cwd: Any, env: Any) -> None:
        punctuation = ";&|()<>`\n"
        lexer = shlex.shlex(os.fsdecode(command), posix=True, punctuation_chars=punctuation)
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            # Unbalanced quotes: the shell rejects the line, so check plain words.
            tokens = os.fsdecode(command).split()
        expect_command = True
        for token in tokens:
            operator = not token.strip(punctuation)
            if operator and token[0] not in "<>" and set(token) & set(";&|(`\n"):
                # Command separators and substitutions: |, ||, &&, ;, &, (, `, newline.
                expect_command = True
            elif not expect_command or operator:
                continue
            elif token in shell_prefixes or (
                "=" in token and token.partition("=")[0].isidentifier()
            ):
                continue  # Reserved words and VAR=value assignments precede the command.
            else:
                check_executable(token, cwd, env)
                expect_command = False

    def guarded_init(process: Any, *args: Any, **kwargs: Any) -> None:
        parameters = signature.bind(process, *args, **kwargs).arguments
        cwd = parameters.get("cwd")
        env = parameters.get("env")
        executable = parameters.get("executable")
        command = parameters["args"]
        if parameters.get("shell", False):
            if executable is not None:
                check_executable(executable, cwd, env)
            if isinstance(command, (str, bytes, os.PathLike)):
                check_shell_command(command, cwd, env)
            elif command:
                check_shell_command(command[0], cwd, env)
        elif executable is not None:
            check_executable(executable, cwd, env)
        elif isinstance(command, (str, bytes, os.PathLike)):
            check_executable(command, cwd, env)
        elif command:
            check_executable(command[0], cwd, env)
        return original_init(process, *args, **kwargs)

    def guarded_system(command: Any) -> int:
        check_shell_command(command, None, None)
        return original_system(command)

    def guarded_posix_spawn(path: Any, *args: Any, **kwargs: Any) -> int:
        # posix_spawn never searches PATH; a bare name is relative to the cwd.
        check_executable(path, None, None, search=False)
        return original_posix_spawn(path, *args, **kwargs)

    def guarded_posix_spawnp(file: Any, *args: Any, **kwargs: Any) -> int:
        # posix_spawnp searches the parent's PATH, not the child env argument.
        check_executable(file, None, None)
        return original_posix_spawnp(file, *args, **kwargs)

    # Preserve the Popen class for runtime annotations and subclassing.
    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)
    monkeypatch.setattr(os, "system", guarded_system)
    monkeypatch.setattr(os, "posix_spawn", guarded_posix_spawn)
    monkeypatch.setattr(os, "posix_spawnp", guarded_posix_spawnp)


@pytest.fixture(autouse=True, scope="session")
def _sweep_per_test_temp_roots() -> Generator[None, None, None]:
    """Remove every per-test tempfile root once the session ends.

    At session teardown no function-scoped monkeypatch is active, so the
    real ``os`` functions are guaranteed for ``rmtree``; under xdist each
    worker sweeps the roots its own tests created.
    """
    yield
    for root in _PENDING_TEMP_ROOTS:
        # Best effort; a stray empty dir is still harmless.
        with contextlib.suppress(Exception):
            shutil.rmtree(root, ignore_errors=True)
    _PENDING_TEMP_ROOTS.clear()


_COLLECTED_MODULES: pytest.StashKey[frozenset[str]] = pytest.StashKey()
_DEFAULT_COLLECTION: pytest.StashKey[bool] = pytest.StashKey()
_SHARD_ASSIGNMENT: pytest.StashKey[tuple[int, int] | None] = pytest.StashKey()
_SHARD_SKIPPED_MODULES: pytest.StashKey[set[str]] = pytest.StashKey()
_CALL_DURATIONS: pytest.StashKey[dict[str, float]] = pytest.StashKey()

DEFAULT_SLOW_TEST_BUDGET_SECONDS: float = 60.0

# Shard pins that flip a test module's crc32 shard to balance CI shard load. A pin
# applies only when its index fits the run's shard total; otherwise crc32 decides.
# test_mimoagent_worker.py is 50 to 85 s of the serial native step. With four shards,
# crc32 puts it on shard 2, the slowest (368 s on 2026-11-11); shard 4 is the fastest (229 s).
SHARD_OVERRIDES: dict[str, int] = {"tests/test_mimoagent_worker.py": 4}


def shard_for(rel_path: str, total: int) -> int:
    """Assign a repo-relative POSIX module path to one shard."""
    override = SHARD_OVERRIDES.get(rel_path)
    if override is not None and override <= total:
        return override
    return zlib.crc32(rel_path.encode("utf-8")) % total + 1


def _module_path(path: Path, config: pytest.Config) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(config.rootpath.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


class _OtherShardFile(pytest.File):
    """Recognize an explicit file argument without importing its module."""

    def collect(self) -> tuple[()]:
        self.config.stash[_SHARD_SKIPPED_MODULES].add(_module_path(self.path, self.config))
        return ()


class _ShardCollection:
    """Global file hook: tests/conftest.py also shards sibling test roots."""

    @pytest.hookimpl(wrapper=True)
    def pytest_collect_file(
        self, file_path: Path, parent: pytest.Collector
    ) -> Generator[None, list[pytest.Collector], list[pytest.Collector]]:
        collectors = yield
        config = parent.config
        assignment = config.stash[_SHARD_ASSIGNMENT]
        if assignment is None or file_path.suffix != ".py" or file_path.name == "conftest.py":
            return collectors
        index, total = assignment
        rel_path = _module_path(file_path, config)
        if rel_path == "tests/test_ci_coverage.py" or shard_for(rel_path, total) == index:
            return collectors
        # An empty file collector prevents pytest's "not found" error for
        # explicit file arguments. Its collect() never imports the module.
        selected: list[pytest.Collector] = []
        for collector in collectors:
            if isinstance(collector, pytest.Module):
                selected.append(_OtherShardFile.from_parent(parent, path=file_path))
            else:
                selected.append(collector)
        return selected

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        config = session.config
        if hasattr(config, "workeroutput"):
            config.workeroutput["shard_collection"] = {
                "collected": sorted(config.stash.get(_COLLECTED_MODULES, frozenset())),
                "skipped": sorted(config.stash[_SHARD_SKIPPED_MODULES]),
            }

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any) -> None:
        census = node.workeroutput.get("shard_collection", {})
        config = node.config
        config.stash[_COLLECTED_MODULES] = config.stash.get(
            _COLLECTED_MODULES, frozenset()
        ) | frozenset(census.get("collected", []))
        config.stash[_SHARD_SKIPPED_MODULES].update(census.get("skipped", []))


def pytest_configure(config: pytest.Config) -> None:
    shard_option = config.getoption("shard", default=None)
    config.stash[_SHARD_ASSIGNMENT] = (
        _parse_shard_option(shard_option) if shard_option else None
    )
    config.stash[_SHARD_SKIPPED_MODULES] = set()
    if shard_option:
        config.pluginmanager.register(_ShardCollection(), "evallab-shard-collection")


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
    """Record imported modules, then authoritatively select this shard's items."""
    config.stash[_COLLECTED_MODULES] = frozenset(_module_path(item.path, config) for item in items)
    config.stash[_DEFAULT_COLLECTION] = not config.option.file_or_dir

    assignment = config.stash[_SHARD_ASSIGNMENT]
    if assignment is None:
        return
    index, total = assignment

    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        rel_path = _module_path(item.path, config)
        if rel_path == "tests/test_ci_coverage.py" or shard_for(rel_path, total) == index:
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


@pytest.fixture(scope="session")
def shard_skipped_module_paths(request: pytest.FixtureRequest) -> frozenset[str]:
    """Collectable Python modules left unimported for another shard."""
    return frozenset(request.config.stash[_SHARD_SKIPPED_MODULES])


@pytest.fixture(scope="session")
def shard_for_path() -> Any:
    """Expose the assignment rule without importing another conftest module."""
    return shard_for


def _slow_test_budget_seconds() -> float:
    raw = os.environ.get("EVALLAB_SLOW_TEST_BUDGET_SECONDS", "")
    if not raw:
        return DEFAULT_SLOW_TEST_BUDGET_SECONDS
    try:
        val = float(raw)
        return val if val > 0 else DEFAULT_SLOW_TEST_BUDGET_SECONDS
    except ValueError:
        return DEFAULT_SLOW_TEST_BUDGET_SECONDS


class _ModuleDurations:
    """Sum setup, call and teardown seconds per test module for the session log.

    The per-test list in `pytest_terminal_summary` shows only call phases of 1 s
    or more. Fixture setup and many sub-second cases never appear there, yet they
    decide which shard is slowest. The xdist controller receives every worker
    report, so one table covers the whole shard.
    """

    TOP = 15

    def __init__(self) -> None:
        self.seconds: dict[str, float] = {}
        self.tests: dict[str, int] = {}
        self.failed_modules: set[str] = set()
        self.collection_errors: set[str] = set()

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        module = report.nodeid.split("::", 1)[0]
        self.seconds[module] = self.seconds.get(module, 0.0) + report.duration
        if report.when == "setup":
            self.tests[module] = self.tests.get(module, 0) + 1
        if report.failed:
            self.failed_modules.add(module)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.collection_errors.add(report.nodeid)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        config = session.config
        xmlpath = config.getoption("xmlpath", default=None)
        if not xmlpath or hasattr(config, "workerinput"):
            return
        import platform

        receipt = {
            "schema_version": 1,
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "tested_sha": os.environ.get("GITHUB_SHA"),
            "job": os.environ.get("GITHUB_JOB"),
            "python_version": platform.python_version(),
            "shard": config.getoption("shard", default=None),
            "phase": Path(xmlpath).stem,
            "exitstatus": int(exitstatus),
            "testscollected": session.testscollected,
            "failed_modules": sorted(self.failed_modules),
            "collection_errors": sorted(self.collection_errors),
            "modules": {
                module: {"seconds": seconds, "tests": self.tests.get(module, 0)}
                for module, seconds in sorted(self.seconds.items())
            },
        }
        output = Path(xmlpath).with_suffix(".modules.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(receipt) + "\n")

    def pytest_terminal_summary(self, terminalreporter: pytest.TerminalReporter) -> None:
        if not self.seconds:
            return
        ranked = sorted(self.seconds.items(), key=lambda item: item[1], reverse=True)
        terminalreporter.section(
            f"Module durations (setup+call+teardown; top {min(self.TOP, len(ranked))} "
            f"of {len(ranked)}; total {sum(self.seconds.values()):.1f}s)"
        )
        for module, seconds in ranked[: self.TOP]:
            terminalreporter.write_line(
                f"{module}: {seconds:.2f}s ({self.tests.get(module, 0)} tests)"
            )


def pytest_sessionstart(session: pytest.Session) -> None:
    session.config.pluginmanager.register(_ModuleDurations(), "evallab-module-durations")



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
    assignment = config.stash.get(_SHARD_ASSIGNMENT, None)
    if assignment is not None:
        index, total = assignment
        collected = len(config.stash.get(_COLLECTED_MODULES, frozenset()))
        skipped = len(config.stash.get(_SHARD_SKIPPED_MODULES, set()))
        terminalreporter.write_line(
            f"shard {index}/{total}: collected {collected} test files, "
            f"left {skipped} to other shards"
        )
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
