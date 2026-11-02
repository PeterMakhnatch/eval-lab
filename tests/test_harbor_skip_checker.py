"""Contract tests for the native Harbor JUnit skip checker."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/check_harbor_skips.py"


def _run_checker(tmp_path: Path, testcases: str) -> subprocess.CompletedProcess[str]:
    report = tmp_path / "harbor-native.xml"
    report.write_text(f'<testsuite>{testcases}</testsuite>', encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(report)],
        text=True,
        capture_output=True,
        check=False,
    )


def test_harbor_named_skip_fails_and_names_the_test(tmp_path: Path) -> None:
    result = _run_checker(
        tmp_path,
        '<testcase classname="tests.test_native" name="test_contract">'
        '<skipped message="HARBOR source checkout is unavailable" />'
        "</testcase>",
    )

    assert result.returncode != 0
    assert "tests.test_native.test_contract" in result.stdout
    assert "SKIP reason count=1: HARBOR source checkout is unavailable" in result.stdout


def test_non_harbor_skips_pass_and_print_distinct_reason_counts(tmp_path: Path) -> None:
    result = _run_checker(
        tmp_path,
        '<testcase classname="tests.test_export" name="test_scout">'
        '<skipped message="inspect_scout is not installed" />'
        "</testcase>"
        '<testcase classname="tests.test_export" name="test_other_scout">'
        '<skipped message="inspect_scout is not installed" />'
        "</testcase>"
        '<testcase classname="tests.test_worker" name="test_xiaomi">'
        '<skipped message="Xiaomi interpreter is not installed" />'
        "</testcase>",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "SKIP reason count=1: Xiaomi interpreter is not installed",
        "SKIP reason count=2: inspect_scout is not installed",
    ]
