"""Test hygiene checks for imports, subprocess isolation, and slow-test budgets."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import subprocess
import tomllib
from collections.abc import Sequence
from fnmatch import fnmatch
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
_slow_test_budget_seconds = _conftest._slow_test_budget_seconds
pytest_runtest_makereport = _conftest.pytest_runtest_makereport
pytest_terminal_summary = _conftest.pytest_terminal_summary


def _find_bare_module_collisions(
    root: Path,
    *,
    testpaths: Sequence[str],
    pythonpath: Sequence[str] = (),
    python_files: Sequence[str] = ("test_*.py", "*_test.py"),
) -> dict[str, tuple[Path, ...]]:
    """Find repeated names exposed by non-package pytest import directories.

    Inspect filenames only: importing candidates would reproduce the collision
    before this check can report all affected paths.
    """
    import_dirs = {(root / entry).resolve() for entry in pythonpath}
    excluded = {"__init__.py", "conftest.py"}
    for entry in testpaths:
        path = (root / entry).resolve()
        if path.is_file():
            if path.suffix == ".py":
                import_dirs.add(path.parent)
        else:
            import_dirs.update(
                candidate.parent
                for candidate in path.rglob("*.py")
                if candidate.is_file()
                and candidate.name not in excluded
                and any(fnmatch(candidate.name, pattern) for pattern in python_files)
            )

    modules: dict[str, set[Path]] = {}
    for directory in sorted(import_dirs):
        if (directory / "__init__.py").is_file():
            continue
        for path in directory.glob("*.py"):
            if path.is_file() and path.name not in excluded:
                modules.setdefault(path.stem, set()).add(path.resolve())
    return {
        name: tuple(sorted(paths))
        for name, paths in sorted(modules.items())
        if len(paths) > 1
    }


def _assert_unique_bare_modules(root: Path) -> None:
    """Read the configured import roots even during a focused pytest run."""
    with (root / "pyproject.toml").open("rb") as stream:
        options = tomllib.load(stream)["tool"]["pytest"]["ini_options"]
    collisions = _find_bare_module_collisions(
        root,
        testpaths=options["testpaths"],
        pythonpath=options.get("pythonpath", ()),
        python_files=options.get("python_files", ("test_*.py", "*_test.py")),
    )
    details = "\n".join(
        f"  {name}:\n" + "\n".join(f"    {path}" for path in paths)
        for name, paths in collisions.items()
    )
    assert not collisions, (
        "Duplicate bare Python module names in pytest import directories:\n"
        f"{details}\n"
        "Rename the colliding modules or make their test directories packages "
        "with __init__.py."
    )


def _write_hygiene_tree(
    root: Path,
    files: Sequence[str],
    *,
    testpaths: Sequence[str],
    pythonpath: Sequence[str] = (),
    python_files: Sequence[str] | None = None,
) -> None:
    options = {"testpaths": list(testpaths), "pythonpath": list(pythonpath)}
    if python_files is not None:
        options["python_files"] = list(python_files)
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        + "\n".join(f"{name} = {json.dumps(value)}" for name, value in options.items()),
        encoding="utf-8",
    )
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("raise RuntimeError('candidate modules must not be imported')\n")


def test_configured_pytest_import_dirs_have_unique_bare_modules() -> None:
    _assert_unique_bare_modules(Path(__file__).resolve().parents[1])


def test_bare_module_collisions_report_tests_and_sibling_helpers(tmp_path: Path) -> None:
    files = [
        f"suites/{directory}/{name}"
        for directory in ("first", "second")
        for name in ("score.py", "test_admission.py")
    ]
    _write_hygiene_tree(tmp_path, files, testpaths=["suites"])

    with pytest.raises(AssertionError) as error:
        _assert_unique_bare_modules(tmp_path)

    message = str(error.value)
    for name in files:
        assert str(tmp_path / name) in message


@pytest.mark.parametrize("packaged", [False, True])
def test_bare_module_collisions_exclude_conftest_and_packages(
    tmp_path: Path, packaged: bool
) -> None:
    files = [
        "first/test_first.py",
        "first/conftest.py",
        "second/test_second.py",
        "second/conftest.py",
    ]
    if packaged:
        files.extend(
            [
                "second/__init__.py",
                "second/test_first.py",
                "first/score.py",
                "second/score.py",
            ]
        )
    _write_hygiene_tree(tmp_path, files, testpaths=["first", "second"])

    _assert_unique_bare_modules(tmp_path)


@pytest.mark.parametrize("second_root", ["single-file", "pythonpath"])
def test_bare_module_collisions_include_configured_import_roots(
    tmp_path: Path, second_root: str
) -> None:
    testpaths = ["first"]
    pythonpath = []
    if second_root == "single-file":
        testpaths.append("second/check.py")
    else:
        pythonpath.append("second")
    _write_hygiene_tree(
        tmp_path,
        ["first/test_first.py", "first/score.py", "second/check.py", "second/score.py"],
        testpaths=testpaths,
        pythonpath=pythonpath,
    )

    with pytest.raises(AssertionError) as error:
        _assert_unique_bare_modules(tmp_path)

    message = str(error.value)
    assert f"{tmp_path}/first/score.py" in message
    assert f"{tmp_path}/second/score.py" in message


def test_bare_module_collisions_deduplicate_overlapping_roots(tmp_path: Path) -> None:
    _write_hygiene_tree(
        tmp_path,
        ["suites/nested/test_admission.py", "suites/nested/score.py"],
        testpaths=["suites", "suites/nested", "suites/nested/test_admission.py", "suites"],
        pythonpath=["suites/nested", "suites/nested/."],
    )

    _assert_unique_bare_modules(tmp_path)


@pytest.mark.parametrize("python_files", [None, ["check_*.py"]])
def test_bare_module_collisions_only_use_collectable_test_dirs(
    tmp_path: Path, python_files: list[str] | None
) -> None:
    test_name = "test_active.py" if python_files is None else "check_active.py"
    files = [f"suites/active/{test_name}", "suites/active/score.py", "suites/helper/score.py"]
    if python_files is not None:
        files.append("suites/helper/test_ignored.py")
    _write_hygiene_tree(
        tmp_path,
        files,
        testpaths=["suites"],
        python_files=python_files,
    )

    _assert_unique_bare_modules(tmp_path)


def test_bare_module_collisions_honor_python_filename_patterns(tmp_path: Path) -> None:
    _write_hygiene_tree(
        tmp_path,
        ["first/check_first.py", "first/score.py", "second/check_second.py", "second/score.py"],
        testpaths=["first", "second"],
        python_files=["check_*.py"],
    )

    with pytest.raises(AssertionError, match="score"):
        _assert_unique_bare_modules(tmp_path)


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

    assert tr.sections == ["Slowest test durations", "Slow tests (budget 60s)"]
    assert tr.lines == [
        "tests/test_compaction.py::test_heavy: 75.34s",
        "tests/test_fast.py::test_quick: 1.20s",
        "tests/test_compaction.py::test_heavy: 75.34s",
    ]


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

    assert tr.sections == ["Slowest test durations"]
    assert tr.lines == [
        "tests/test_b.py::test_2: 59.90s",
        "tests/test_a.py::test_1: 5.00s",
    ]


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

        assert tr.sections == ["Slowest test durations", "Slow tests (budget 10s)"]
        assert tr.lines == [
            "tests/test_mid.py::test_medium: 15.20s",
            "tests/test_fast.py::test_quick: 2.00s",
            "tests/test_mid.py::test_medium: 15.20s",
        ]


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

    assert tr.sections == ["Slowest test durations", "Slow tests (budget 60s)"]
    assert tr.lines == [
        "tests/test_worker.py::test_heavy: 88.00s",
        "tests/test_worker.py::test_heavy: 88.00s",
    ]


_MODEL_CLI_BINARIES = (
    "codex",
    "claude",
    "agy",
    "cursor-agent",
    "opencode",
    "omp",
    "security",
    "gemini",
)
_MODEL_CLI_FORMS = (
    "absolute",
    "cwd-relative",
    "path",
    "relative-path",
    "executable",
    "executable-path",
    "symlink",
)


def _write_fake_model_cli(root: Path, binary: str) -> Path:
    executable = root / binary
    executable.write_text(
        "#!/bin/sh\n"
        "printf 'fake-model-cli\\n'\n"
        'printf "launched\\n" > "$1"\n'
    )
    executable.chmod(0o755)
    return executable


def _model_cli_invocation(
    form: str, executable: Path, marker: Path, tmp_path: Path
) -> tuple[list[str], dict[str, Any]]:
    options: dict[str, Any] = {"cwd": tmp_path}
    command = str(executable)
    if form == "cwd-relative":
        command = f"./{executable.name}"
        options["cwd"] = executable.parent
    elif form == "path":
        command = executable.name
        options["env"] = {"PATH": str(executable.parent)}
    elif form == "relative-path":
        command = executable.name
        options["cwd"] = executable.parent
        options["env"] = {"PATH": "."}
    elif form == "executable":
        command = "not-a-model-cli"
        options["executable"] = str(executable)
    elif form == "executable-path":
        command = "not-a-model-cli"
        options["executable"] = executable.name
        options["env"] = {"PATH": str(executable.parent)}
    elif form == "symlink":
        alias = tmp_path / "model-cli-alias"
        alias.symlink_to(executable)
        command = str(alias)
    else:
        assert form == "absolute"
    return [command, str(marker)], options


@pytest.fixture
def outside_model_cli_root(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    _isolate_temp_root: Path,
) -> Path:
    root = tmp_path_factory.mktemp("outside-model-cli").resolve()
    assert not root.is_relative_to(tmp_path.resolve())
    assert not root.is_relative_to(_isolate_temp_root.resolve())
    return root


@pytest.mark.parametrize(
    ("binary", "form"),
    [(binary, "absolute") for binary in _MODEL_CLI_BINARIES]
    + [("codex", form) for form in _MODEL_CLI_FORMS[1:]],
)
def test_model_cli_guard_rejects_outside_executables(
    binary: str,
    form: str,
    tmp_path: Path,
    outside_model_cli_root: Path,
    request: pytest.FixtureRequest,
) -> None:
    executable = _write_fake_model_cli(outside_model_cli_root, binary)
    marker = tmp_path / "must-not-launch"
    args, options = _model_cli_invocation(form, executable, marker, tmp_path)

    with pytest.raises(pytest.fail.Exception) as caught:
        try:
            subprocess.run(
                args, **options, check=True, capture_output=True, text=True, timeout=5
            )
        except Exception:
            pytest.fail("An application Exception handler swallowed the guard violation")

    message = str(caught.value)
    assert binary in message
    assert request.node.nodeid in message
    assert not marker.exists()


@pytest.mark.parametrize(
    ("binary", "root_name"),
    [(binary, "tmp_path") for binary in _MODEL_CLI_BINARIES]
    + [("codex", "_isolate_temp_root")],
)
def test_model_cli_guard_allows_each_binary_in_test_roots(
    binary: str, root_name: str, request: pytest.FixtureRequest
) -> None:
    root: Path = request.getfixturevalue(root_name)
    executable = _write_fake_model_cli(root, binary)
    marker = root / "launched"

    result = subprocess.run(
        [str(executable), str(marker)],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.stdout == "fake-model-cli\n"
    assert marker.read_text() == "launched\n"


@pytest.mark.parametrize("form", _MODEL_CLI_FORMS[1:])
def test_model_cli_guard_allows_invocation_forms_in_test_roots(
    form: str, tmp_path: Path
) -> None:
    executable = _write_fake_model_cli(tmp_path, "codex")
    marker = tmp_path / "launched"
    args, options = _model_cli_invocation(form, executable, marker, tmp_path)

    result = subprocess.run(
        args, **options, check=True, capture_output=True, text=True, timeout=5
    )

    assert result.stdout == "fake-model-cli\n"
    assert marker.read_text() == "launched\n"


def test_model_cli_guard_uses_executable_override_instead_of_argv(
    tmp_path: Path,
) -> None:
    executable = _write_fake_model_cli(tmp_path, "harmless-probe")
    marker = tmp_path / "launched"

    result = subprocess.run(
        ["codex", str(marker)],
        executable=str(executable),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.stdout == "fake-model-cli\n"
    assert marker.read_text() == "launched\n"


def test_model_cli_guard_rejects_asyncio_subprocess_before_launch(
    tmp_path: Path,
    outside_model_cli_root: Path,
    request: pytest.FixtureRequest,
) -> None:
    executable = _write_fake_model_cli(outside_model_cli_root, "codex")
    marker = tmp_path / "must-not-launch"

    async def launch() -> None:
        process = await asyncio.create_subprocess_exec(
            str(executable),
            str(marker),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(process.communicate(), timeout=5)

    with pytest.raises(pytest.fail.Exception) as caught:
        asyncio.run(launch())

    message = str(caught.value)
    assert "codex" in message
    assert request.node.nodeid in message
    assert not marker.exists()


def test_model_cli_guard_resolves_positional_executable_cwd_and_env(
    tmp_path: Path,
    outside_model_cli_root: Path,
    request: pytest.FixtureRequest,
) -> None:
    _write_fake_model_cli(outside_model_cli_root, "codex")
    marker = tmp_path / "must-not-launch"

    with (
        pytest.raises(pytest.fail.Exception) as caught,
        subprocess.Popen(
            ["not-a-model-cli", str(marker)],
            -1,  # bufsize
            "codex",  # executable
            None,  # stdin
            subprocess.PIPE,  # stdout
            subprocess.PIPE,  # stderr
            None,  # preexec_fn
            True,  # close_fds
            False,  # shell
            outside_model_cli_root,  # cwd
            {"PATH": "."},  # env
        ) as process,
    ):
        process.communicate(timeout=5)

    message = str(caught.value)
    assert "codex" in message
    assert request.node.nodeid in message
    assert not marker.exists()


_SHELL_SEARCH_PATH = os.pathsep.join(("/usr/bin", "/bin"))
_BLOCKED_SHELL_COMMANDS = (
    "codex {marker}",
    "{executable} {marker}",
    "true && codex {marker}",
    "false || codex {marker}",
    "true; codex {marker}",
    "echo x | codex {marker}",
    "echo x\ncodex {marker}",
    "MODEL=1 codex {marker}",
    "echo $(codex {marker})",
    "if true; then codex {marker}; fi",
)


def _shell_options(root: Path) -> dict[str, Any]:
    return {"env": {"PATH": os.pathsep.join((str(root), _SHELL_SEARCH_PATH))}}


def _assert_blocked(caught: pytest.ExceptionInfo[Any], request: pytest.FixtureRequest) -> None:
    message = str(caught.value)
    assert message.startswith("Blocked host CLI 'codex'")
    assert request.node.nodeid in message


@pytest.mark.parametrize("as_list", [False, True])
@pytest.mark.parametrize("template", _BLOCKED_SHELL_COMMANDS)
def test_model_cli_guard_rejects_shell_command_words(
    template: str,
    as_list: bool,
    tmp_path: Path,
    outside_model_cli_root: Path,
    request: pytest.FixtureRequest,
) -> None:
    executable = _write_fake_model_cli(outside_model_cli_root, "codex")
    marker = tmp_path / "must-not-launch"
    command = template.format(executable=executable, marker=marker)

    with pytest.raises(pytest.fail.Exception) as caught:
        subprocess.run(
            [command] if as_list else command,
            shell=True,
            **_shell_options(outside_model_cli_root),
            check=True,
            capture_output=True,
            timeout=5,
        )

    _assert_blocked(caught, request)
    assert not marker.exists()


@pytest.mark.parametrize(
    ("command", "stdout"),
    [
        ("/bin/echo codex", "codex\n"),
        ("/bin/echo 'claude; codex'", "claude; codex\n"),
        ("/bin/echo -m claude | /bin/cat", "-m claude\n"),
    ],
)
def test_model_cli_guard_allows_host_cli_names_as_shell_arguments(
    command: str, stdout: str, outside_model_cli_root: Path
) -> None:
    for binary in ("codex", "claude"):
        _write_fake_model_cli(outside_model_cli_root, binary)

    result = subprocess.run(
        command,
        shell=True,
        **_shell_options(outside_model_cli_root),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.stdout == stdout


def test_model_cli_guard_rejects_os_system(
    tmp_path: Path,
    outside_model_cli_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    _write_fake_model_cli(outside_model_cli_root, "codex")
    marker = tmp_path / "must-not-launch"
    monkeypatch.setenv("PATH", os.pathsep.join((str(outside_model_cli_root), _SHELL_SEARCH_PATH)))

    with pytest.raises(pytest.fail.Exception) as caught:
        os.system(f"true && codex {marker}")

    _assert_blocked(caught, request)
    assert not marker.exists()


@pytest.mark.parametrize("search", [False, True])
def test_model_cli_guard_rejects_posix_spawn(
    search: bool,
    tmp_path: Path,
    outside_model_cli_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    executable = _write_fake_model_cli(outside_model_cli_root, "codex")
    marker = tmp_path / "must-not-launch"
    if search:
        monkeypatch.setenv("PATH", str(outside_model_cli_root))
        spawn, file = os.posix_spawnp, "codex"
    else:
        spawn, file = os.posix_spawn, str(executable)

    with pytest.raises(pytest.fail.Exception) as caught:
        spawn(file, [file, str(marker)], dict(os.environ))

    _assert_blocked(caught, request)
    assert not marker.exists()


@pytest.mark.parametrize("launcher", ["popen", "shell", "system", "posix_spawn", "posix_spawnp"])
def test_model_cli_guard_allows_fake_cli_in_tmp_path_through_each_launcher(
    launcher: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _write_fake_model_cli(tmp_path, "codex")
    marker = tmp_path / "launched"
    monkeypatch.setenv("PATH", os.pathsep.join((str(tmp_path), _SHELL_SEARCH_PATH)))

    if launcher == "popen":
        with subprocess.Popen([str(executable), str(marker)]) as process:
            assert process.wait(timeout=5) == 0
    elif launcher == "shell":
        subprocess.run(f"true && codex {marker}", shell=True, check=True, timeout=5)
    elif launcher == "system":
        assert os.system(f"codex {marker} >/dev/null") == 0
    else:
        file = "codex" if launcher == "posix_spawnp" else str(executable)
        spawn = os.posix_spawnp if launcher == "posix_spawnp" else os.posix_spawn
        pid = spawn(file, [file, str(marker)], dict(os.environ))
        assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 0

    assert marker.read_text() == "launched\n"
