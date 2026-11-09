"""Test hygiene checks for bare-module imports, shard overrides, and slow-test budgets."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import tomllib
import zlib
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
SHARD_OVERRIDES = _conftest.SHARD_OVERRIDES
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


def test_shard_overrides_balance_heavy_modules() -> None:
    """Pins follow scripts/ci/propose_shard_pins.py over CI run 41761936272 (HAR-308).

    Every pin must flip its module's crc32 default; a pin equal to the hash
    shard is a no-op. The compaction pin was dropped because it put 109 s on
    the heavier shard; crc32 alone now places that module on shard 1.
    """
    assert SHARD_OVERRIDES == {"tests/test_trajectory_recipes.py": 1}

    for path, shard in SHARD_OVERRIDES.items():
        hash_shard = (zlib.crc32(path.encode("utf-8")) % 2) + 1
        assert shard != hash_shard, f"{path} pin to shard {shard} equals its crc32 default"

    assert (zlib.crc32(b"tests/test_compaction_properties.py") % 2) + 1 == 1


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
