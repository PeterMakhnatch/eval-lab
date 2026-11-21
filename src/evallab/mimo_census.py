from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_JUNIT_RE = re.compile(
    r"rc=(?P<rc>-?\d+)\s+cases=(?P<cases>\d+)\s+bad=(?P<bad>\d+)\s+"
    r"(?:skipped=\d+\s+)?named=(?P<named>\d+)\s+missing=\[(?P<missing>[^\]]*)\]"
)


def parse_junit_grade(text: str) -> dict[str, Any] | None:
    match = _JUNIT_RE.search(text.strip())
    if match is None:
        return None
    missing = [item.strip() for item in match.group("missing").split(",") if item.strip()]
    return {
        "rc": int(match.group("rc")),
        "cases": int(match.group("cases")),
        "bad": int(match.group("bad")),
        "named": int(match.group("named")),
        "missing": missing,
    }


def trial_grade_logs(trial_dir: Path) -> list[dict[str, Any]]:
    grades: list[dict[str, Any]] = []
    for text in trial_output_logs(trial_dir, "junit-grade.log"):
        grade = parse_junit_grade(text)
        if grade is not None:
            grades.append(grade)
    return grades


_PYTEST_RESULT_LINE_RE = re.compile(r"(?m)^(?:FAILED|PASSED)\s+\S+")
_PYTEST_ERROR_LINE_RE = re.compile(r"(?m)^ERROR\s+\S*::\S+")
_PYTEST_SUMMARY_RE = re.compile(r"(?m)^.*\b\d+\s+(?:failed|passed|error)\b.*in\s+[\d.]+s.*$")
_PYTEST_COUNT_RE = re.compile(r"(\d+)\s+(failed|passed|error)")
_PYTEST_COLLECTED_NONE_RE = re.compile(r"collected 0 items?")

_RUNNER_LINE_RE = re.compile(r"(?m)^runner=(?P<runner>[A-Za-z-]+)\s+rc=(?P<rc>-?\d+)")


_GO_CASE_RE = re.compile(r"(?m)^--- (?:FAIL|PASS)|^=== RUN\s+\S|panic:")
_GO_PACKAGE_RE = re.compile(r"(?m)^(ok|FAIL)\s+\S+")
_GO_NOCASE_RE = re.compile(r"no test files|no tests to run|\[build failed\]")
_JEST_TESTS_RE = re.compile(r"Tests:\s+([^\n]+)")
_JEST_NO_TESTS_RE = re.compile(r"No tests found")
_VITEST_SUMMARY_RE = re.compile(r"(?:Test Files|Tests)\s+([^\n]+)")
_MOCHA_COUNT_RE = re.compile(r"(\d+)\s+(?:passing|failing|pending)")
_TAP_OK_RE = re.compile(r"(?m)^(ok|not ok)\b")
_TAP_PLAN_RE = re.compile(r"(?m)^1\.\.(\d+)")
_UNITTEST_RAN_RE = re.compile(r"Ran (\d+) tests?")
_PHPUNIT_OK_RE = re.compile(r"(?m)^OK\s*\(|FAILURES!|ERRORS!")
_PHPUNIT_TESTS_RE = re.compile(r"Tests:\s*(\d+)")
_PHPUNIT_NO_TESTS_RE = re.compile(r"No tests executed")
_RSPEC_SUMMARY_RE = re.compile(r"(\d+) examples?, (\d+) failures?")
_CARGO_RESULT_RE = re.compile(r"test result:\s*\S+\.\s*(\d+) passed(?:;\s*(\d+) failed)?")
_KARMA_EXECUTED_RE = re.compile(r"Executed\s+([1-9]\d*)\s+of")
_JASMINE_SUMMARY_RE = re.compile(r"(\d+) specs?, (\d+) failures?")
_INT_RE = re.compile(r"\d+")


def trial_output_logs(trial_dir: Path, name: str) -> list[str]:
    texts: list[str] = []
    for log_path in sorted(trial_dir.rglob(name)):
        try:
            texts.append(log_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


def trial_runner(trial_dir: Path) -> str:
    for log_path in sorted(trial_dir.rglob("runner.txt")):
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r"(?m)^RUNNER=(\S+)", text)
        if match:
            return match.group(1)
    for text in trial_output_logs(trial_dir, "junit-grade.log"):
        match = _RUNNER_LINE_RE.search(text)
        if match:
            return match.group("runner")
    return "pytest"


def _ints_total(text: str) -> int:
    return sum(int(item) for item in _INT_RE.findall(text))


def _runner_output_executed(runner: str, output: str) -> bool:
    if runner == "go-test":
        if _GO_CASE_RE.search(output):
            return True
        return _GO_PACKAGE_RE.search(output) is not None and not _GO_NOCASE_RE.search(output)
    if runner == "jest":
        if _JEST_NO_TESTS_RE.search(output):
            return False
        return any(_ints_total(segment) > 0 for segment in _JEST_TESTS_RE.findall(output))
    if runner == "vitest":
        return any(_ints_total(segment) > 0 for segment in _VITEST_SUMMARY_RE.findall(output))
    if runner == "mocha":
        return sum(int(count) for count in _MOCHA_COUNT_RE.findall(output)) > 0
    if runner in ("tap", "ava", "node-test"):
        if _TAP_OK_RE.search(output):
            return True
        if any(int(count) > 0 for count in _MOCHA_COUNT_RE.findall(output)):
            return True
        return any(int(count) > 0 for count in _TAP_PLAN_RE.findall(output))
    if runner == "unittest":
        return any(int(count) >= 1 for count in _UNITTEST_RAN_RE.findall(output))
    if runner == "phpunit":
        if _PHPUNIT_NO_TESTS_RE.search(output):
            return False
        if _PHPUNIT_OK_RE.search(output):
            return True
        return any(int(count) > 0 for count in _PHPUNIT_TESTS_RE.findall(output))
    if runner == "rspec":
        return any(int(examples) > 0 for examples, _ in _RSPEC_SUMMARY_RE.findall(output))
    if runner == "cargo-test":
        return any(
            int(passed) + int(failed or 0) > 0
            for passed, failed in _CARGO_RESULT_RE.findall(output)
        )
    if runner == "karma":
        return _KARMA_EXECUTED_RE.search(output) is not None or "FAILED" in output
    if runner == "jasmine":
        return any(int(specs) > 0 for specs, _ in _JASMINE_SUMMARY_RE.findall(output))
    if runner == "pytest":
        if _PYTEST_COLLECTED_NONE_RE.search(output):
            return False
        if _PYTEST_RESULT_LINE_RE.search(output) or _PYTEST_ERROR_LINE_RE.search(output):
            return True
        for line in _PYTEST_SUMMARY_RE.findall(output):
            counts = {kind: int(count) for count, kind in _PYTEST_COUNT_RE.findall(line)}
            if counts.get("failed", 0) > 0 or counts.get("passed", 0) > 0:
                return True
        return False
    return False


def _job_payload(job_dir: Path) -> dict[str, Any] | None:
    payload_path = job_dir / "result.json"
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def job_has_trial_errors(job_dir: Path) -> bool:
    payload = _job_payload(job_dir)
    if not isinstance(payload, dict):
        return False
    stats = payload.get("stats")
    if not isinstance(stats, Mapping):
        return False
    if int(stats.get("n_errored_trials", 0) or 0) > 0:
        return True
    evals = stats.get("evals")
    if not isinstance(evals, Mapping):
        return False
    for report in evals.values():
        if not isinstance(report, Mapping):
            continue
        if int(report.get("n_errors", 0) or 0) > 0:
            return True
        exceptions = report.get("exception_stats")
        if isinstance(exceptions, Mapping) and exceptions:
            return True
    return False
