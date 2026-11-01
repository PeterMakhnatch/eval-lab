"""``nonpytest-hardening@1``: runner-family detection, summary parsing and the derived grader text."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.nonpytest_hardening import (
    ENV_SCRUB,
    EXPECTED_REL,
    build_test_sh,
    parse_runner_summary,
    runner_family,
)

PARENT = """#!/bin/bash
M=/var/lib/mimo
CWD=/testbed
V=/logs/verifier
mkdir -p "$V"
cd "$CWD" || exit 1
if ! (
  true
); then echo "could not reset the test files (testbed problem, not scored)" >&2; exit 1; fi
timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1
RC=$?
if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi
echo "test command exited $RC"
"""


def test_runner_family_detects_each_runner() -> None:
    assert runner_family("npx vitest run") == "vitest"
    assert runner_family("npx mocha -r ts-node/register 'tests/**/*-test.js'") == "mocha"
    assert runner_family("python -m unittest discover -s tests") == "unittest"
    assert runner_family("python -m pytest -q") == "pytest"
    assert runner_family("bash /testbed/mimo_test_command.sh") is None


def test_summary_parser_counts_the_runners_own_line() -> None:
    # vitest prints "Test Files  1 passed" before "Tests  12 passed": the last match is the real count.
    assert parse_runner_summary("vitest", " Test Files  1 passed (1)\n      Tests  12 passed (12)") == {
        "passed": 12,
        "failed": 0,
        "skipped": 0,
    }
    assert parse_runner_summary("vitest", "Tests  2 failed | 10 passed (12)") == {
        "passed": 10,
        "failed": 2,
        "skipped": 0,
    }
    assert parse_runner_summary("mocha", "  12 passing (34ms)\n  1 pending") == {
        "passed": 12,
        "failed": 0,
        "skipped": 1,
    }
    assert parse_runner_summary("unittest", "Ran 12 tests in 0.345s\n\nOK") == {
        "passed": 12,
        "failed": 0,
        "skipped": 0,
    }
    assert parse_runner_summary("unittest", "Ran 3 tests in 0.1s\n\nFAILED (failures=1)") == {
        "passed": 2,
        "failed": 1,
        "skipped": 0,
    }
    assert parse_runner_summary("pytest", "12 passed, 2 skipped in 1.2s") == {
        "passed": 12,
        "failed": 0,
        "skipped": 2,
    }
    assert parse_runner_summary("vitest", "no summary at all") is None


def test_build_test_sh_adds_reset_scrub_and_gate() -> None:
    text = build_test_sh(PARENT, family="vitest")
    assert "nonpytest-hardening@1 R1" in text
    assert "package.json|*/package.json" in text
    assert "vitest.config.*" in text and ".mocharc.*" in text and ".npmrc" in text
    assert "node_modules" in text  # dependency directories are never purged
    assert "unset " + ENV_SCRUB in text
    assert "GATE=GATE_NO" in text and "GATE=$(" in text
    assert '[ "$GATE" = GATE_OK ] || RC=1' in text
    assert "expected_runner_report.json" in text
    # the parent's own reset block and reward line survive exactly once
    assert text.count("could not reset the test files") == 1
    assert text.count('if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"') == 1


def test_build_test_sh_refuses_an_already_hardened_parent() -> None:
    twice = build_test_sh(PARENT, family="mocha")
    from evallab.task_variants import VariantInvalid

    with pytest.raises(VariantInvalid):
        build_test_sh(twice, family="mocha")


def test_build_changes_requires_a_calibrated_count(tmp_path: Path) -> None:
    from evallab.nonpytest_hardening import build_changes
    from evallab.task_variants import VariantInvalid

    pkg = tmp_path / "pkg"
    (pkg / "tests").mkdir(parents=True)
    (pkg / "tests" / "test.sh").write_text(PARENT)
    (pkg / "tests" / "test_command.sh").write_text("npx vitest run\n")

    with pytest.raises(VariantInvalid):
        build_changes(pkg, expected_passed=None)

    changes, inputs = build_changes(pkg, expected_passed=12, expected_skipped=1)
    assert set(changes) == {"tests/test.sh", EXPECTED_REL}
    assert b'"passed": 12' in changes[EXPECTED_REL]
    assert b'"skipped": 1' in changes[EXPECTED_REL]
    assert inputs["runner_family"] == "vitest"
    assert inputs["layers"] == ["R1", "R2"]
    assert inputs["degraded_count_gate"] is False

    degraded, degraded_inputs = build_changes(pkg, expected_passed=None, allow_degraded=True)
    assert b'"passed": 1' in degraded[EXPECTED_REL]
    assert degraded_inputs["degraded_count_gate"] is True
