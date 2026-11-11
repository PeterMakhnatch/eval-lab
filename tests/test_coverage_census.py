"""Contract tests for the per-module coverage census script."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from evallab.devloop import DevloopPlan

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/coverage_census.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("coverage_census", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


census = _load_script()


def _entry(statements: int, missing: list[int]) -> dict[str, object]:
    covered = statements - len(missing)
    return {
        "summary": {
            "num_statements": statements,
            "missing_lines": len(missing),
            "percent_covered": 100.0 * covered / statements,
        },
        "missing_lines": missing,
    }


def _report(entries: dict[str, dict[str, object]]) -> dict[str, object]:
    return {"files": entries}


def _selection(mapping: dict[str, list[str]]) -> Callable[[str, Path], list[str]]:
    def select(module: str, root: Path) -> list[str]:
        return mapping[module]

    return select


def _fake_runner(
    payload: dict[str, object] | None,
    calls: list[list[str]],
    *,
    pytest_rc: int = 0,
) -> Callable[[list[str], Path], subprocess.CompletedProcess[str]]:
    """Serve the coverage JSON step from a fixture; never run real coverage."""

    def run(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "json" in command:
            assert payload is not None
            target = Path(command[command.index("-o") + 1])
            target.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if pytest_rc:
            return subprocess.CompletedProcess(
                command, pytest_rc, stdout="", stderr="1 failed, 2 passed\n"
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    return run


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for name in ("alpha", "beta"):
        source = tmp_path / "src/evallab" / f"{name}.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("VALUE = 1\n", encoding="utf-8")
    return tmp_path


def test_json_reports_statements_missing_lines_and_selected_tests(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    module = "src/evallab/alpha.py"
    runner = _fake_runner(_report({module: _entry(964, [12, 13, 14, 30])}), calls)

    code = census.main(
        [module, "--json", "--root", str(root)],
        runner=runner,
        select=_selection({module: ["tests/test_alpha.py"]}),
    )

    assert code == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [
        {
            "module": module,
            "statements": 964,
            "missed": 4,
            "percent": 99.6,
            "missing_lines": [12, 13, 14, 30],
            "tests": ["tests/test_alpha.py"],
        }
    ]


def test_one_coverage_run_serves_every_module(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    alpha = "src/evallab/alpha.py"
    beta = "src/evallab/beta.py"
    runner = _fake_runner(
        _report({alpha: _entry(10, []), beta: _entry(4, [3])}), calls
    )

    code = census.main(
        [alpha, beta, "--root", str(root)],
        runner=runner,
        select=_selection(
            {
                alpha: ["tests/test_alpha.py"],
                beta: ["tests/test_beta.py"],
            }
        ),
    )

    assert code == 0
    assert len(calls) == 2
    pytest_run, report_run = calls
    assert pytest_run[:7] == [*census.COVERAGE_PREFIX, "coverage", "run"]
    assert "--source=src/evallab" in pytest_run
    for flag in ("-n0", "-q", "-p", "no:cacheprovider", "-o", "addopts="):
        assert flag in pytest_run
    assert "tests/test_alpha.py" in pytest_run
    assert "tests/test_beta.py" in pytest_run
    assert pytest_run[-2:] == ["tests/test_alpha.py", "tests/test_beta.py"]
    assert "coverage" in report_run and "json" in report_run
    assert f"--include={alpha},{beta}" in report_run
    assert report_run[report_run.index("-o") + 1].endswith("coverage.json")
    assert "percent=100.0" in capsys.readouterr().out


def test_selector_keeps_only_direct_importers(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = "src/evallab/alpha.py"
    plan = DevloopPlan(
        changed=[module],
        modules=["tests/test_direct.py", "tests/test_via.py"],
        command="",
        reasons={
            "tests/test_direct.py": [f"Imports evallab.alpha ({module})"],
            "tests/test_via.py": ["Imports evallab.alpha via evallab.cli"],
        },
        lane="focused",
    )
    monkeypatch.setattr(census, "plan_devloop", lambda changed, root: plan)
    monkeypatch.setattr(
        census,
        "affected_test_modules",
        lambda changed, root: pytest.fail("direct selection must not fall back"),
    )

    assert census.select_tests(module, root) == ["tests/test_direct.py"]


def test_selector_falls_back_to_affected_modules(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = "src/evallab/alpha.py"
    plan = DevloopPlan(
        changed=[module],
        modules=["tests/test_via.py"],
        command="",
        reasons={"tests/test_via.py": ["Imports evallab.alpha via evallab.cli"]},
        lane="focused",
    )
    monkeypatch.setattr(census, "plan_devloop", lambda changed, root: plan)
    monkeypatch.setattr(
        census,
        "affected_test_modules",
        lambda changed, root: ["tests/test_via.py", "tests/test_other.py"],
    )

    assert census.select_tests(module, root) == [
        "tests/test_via.py",
        "tests/test_other.py",
    ]


def test_module_without_selected_tests_exits_two(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    module = "src/evallab/alpha.py"

    code = census.main(
        [module, "--root", str(root)],
        runner=_fake_runner(_report({}), calls),
        select=_selection({module: []}),
    )

    assert code == 2
    assert calls == []
    error = capsys.readouterr().err
    assert "no tests selected by devloop" in error
    assert module in error


def test_full_suite_lane_is_rejected(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = "src/evallab/alpha.py"

    code = census.main(
        [module, "--root", str(root)],
        runner=_fake_runner(_report({}), []),
        select=_selection({module: ["__all__"]}),
    )

    assert code == 2
    assert "requires the full suite" in capsys.readouterr().err


def test_pytest_failure_reports_exit_code_and_tail(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = "src/evallab/alpha.py"

    code = census.main(
        [module, "--root", str(root)],
        runner=_fake_runner(None, [], pytest_rc=3),
        select=_selection({module: ["tests/test_alpha.py"]}),
    )

    assert code == 2
    error = capsys.readouterr().err
    assert "pytest failed under coverage (exit 3)" in error
    assert "1 failed, 2 passed" in error


def test_missing_report_entry_is_a_clear_error(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = "src/evallab/alpha.py"

    code = census.main(
        [module, "--root", str(root)],
        runner=_fake_runner(_report({"src/evallab/other.py": _entry(2, [])}), []),
        select=_selection({module: ["tests/test_alpha.py"]}),
    )

    assert code == 2
    assert "no coverage entry in the report" in capsys.readouterr().err


def test_min_percent_below_threshold_exits_one(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = "src/evallab/alpha.py"

    code = census.main(
        [module, "--min-percent", "80", "--root", str(root)],
        runner=_fake_runner(_report({module: _entry(10, [1, 2, 3, 4, 5])}), []),
        select=_selection({module: ["tests/test_alpha.py"]}),
    )

    assert code == 1
    assert module in capsys.readouterr().err


def test_min_percent_at_threshold_passes(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = "src/evallab/alpha.py"

    code = census.main(
        [module, "--min-percent", "50", "--root", str(root)],
        runner=_fake_runner(_report({module: _entry(10, [1, 2, 3, 4, 5])}), []),
        select=_selection({module: ["tests/test_alpha.py"]}),
    )

    assert code == 0
    assert "percent=50.0" in capsys.readouterr().out


def test_arguments_require_a_module_and_numeric_threshold(
    root: Path,
) -> None:
    with pytest.raises(SystemExit) as missing:
        census.main(["--root", str(root)])
    assert missing.value.code == 2

    with pytest.raises(SystemExit) as bad_threshold:
        census.main(["src/evallab/alpha.py", "--min-percent", "high"])
    assert bad_threshold.value.code == 2


def test_module_outside_root_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")

    code = census.main([str(outside), "--root", str(tmp_path)])

    assert code == 2
    assert "outside the repository root" in capsys.readouterr().err


def test_missing_source_file_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = census.main(["src/evallab/absent.py", "--root", str(tmp_path)])

    assert code == 2
    assert "no such source file" in capsys.readouterr().err


def test_plain_output_prints_missing_ranges_and_tests(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = "src/evallab/alpha.py"

    code = census.main(
        [module, "--root", str(root)],
        runner=_fake_runner(_report({module: _entry(20, [3, 4, 5, 9])}), []),
        select=_selection({module: ["tests/test_alpha.py"]}),
    )

    assert code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"{module}  stmts=20 missed=4 percent=80.0 tests=1 missing=3-5,9"
    assert lines[1] == "  tests/test_alpha.py"


def test_script_entrypoint_prints_help() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "--min-percent" in completed.stdout
