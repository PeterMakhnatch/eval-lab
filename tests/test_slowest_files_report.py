"""Contract tests for the per-shard slowest-test-files JUnit report."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/report_slowest_files.py"


def _case(classname: str, seconds: float, name: str = "test_case") -> str:
    return f'<testcase classname="{classname}" name="{name}" time="{seconds}" />'


def _run(tmp_path: Path, testcases: str, *extra: str) -> list[str]:
    report = tmp_path / "full-suite.xml"
    report.write_text(
        f"<testsuites><testsuite>{testcases}</testsuite></testsuites>", encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *extra, str(report)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.splitlines()


def test_files_are_ordered_slowest_first_with_path_tie_break(tmp_path: Path) -> None:
    lines = _run(
        tmp_path,
        _case("tests.test_b", 1.0) + _case("tests.test_a", 1.0) + _case("tests.test_slow", 9.5),
    )

    assert lines == [
        "SLOWEST seconds=9.50 tests=1 file=tests/test_slow.py",
        "SLOWEST seconds=1.00 tests=1 file=tests/test_a.py",
        "SLOWEST seconds=1.00 tests=1 file=tests/test_b.py",
        "SLOWEST total seconds=11.50 files=3",
    ]


def test_classes_and_module_level_tests_sum_into_one_file(tmp_path: Path) -> None:
    lines = _run(
        tmp_path,
        _case("tests.test_x", 1.25)
        + _case("tests.test_x.TestY", 2.0)
        + _case("tests.test_x.TestY.TestInner", 0.75)
        + _case("tests.test_z", 3.5),
    )

    assert lines == [
        "SLOWEST seconds=4.00 tests=3 file=tests/test_x.py",
        "SLOWEST seconds=3.50 tests=1 file=tests/test_z.py",
        "SLOWEST total seconds=7.50 files=2",
    ]


def test_top_cut_keeps_the_slowest_files_but_total_covers_all(tmp_path: Path) -> None:
    cases = "".join(_case(f"tests.test_f{n}", float(n)) for n in range(1, 26))

    default = _run(tmp_path, cases)
    assert len(default) == 21
    assert default[0] == "SLOWEST seconds=25.00 tests=1 file=tests/test_f25.py"
    assert default[19] == "SLOWEST seconds=6.00 tests=1 file=tests/test_f6.py"
    assert default[20] == "SLOWEST total seconds=325.00 files=25"

    assert _run(tmp_path, cases, "--top", "2") == [
        "SLOWEST seconds=25.00 tests=1 file=tests/test_f25.py",
        "SLOWEST seconds=24.00 tests=1 file=tests/test_f24.py",
        "SLOWEST total seconds=325.00 files=25",
    ]


def test_empty_report_prints_only_a_zero_total(tmp_path: Path) -> None:
    assert _run(tmp_path, "") == ["SLOWEST total seconds=0.00 files=0"]
