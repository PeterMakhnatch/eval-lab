#!/usr/bin/env python3
"""Print per-module coverage rows for the pytest files devloop selects."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.devloop import affected_test_modules, plan_devloop

FULL_SUITE_SENTINEL = "__all__"
COVERAGE_PREFIX = ("uv", "run", "--no-sync", "--with", "coverage")
PYTEST_FLAGS = ("-n0", "-q", "-p", "no:cacheprovider", "-o", "addopts=")
SOURCE_ROOT = "src/evallab"

Runner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


class CensusError(RuntimeError):
    """A census cannot be produced from selection, pytest, or report state."""


@dataclass(frozen=True)
class ModuleCoverage:
    """One module's coverage row and the tests devloop selected for it."""

    module: str
    tests: tuple[str, ...]
    statements: int
    missed: int
    percent: float
    missing_lines: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "statements": self.statements,
            "missed": self.missed,
            "percent": round(self.percent, 1),
            "missing_lines": list(self.missing_lines),
            "tests": list(self.tests),
        }


def _subprocess_runner(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    """Run one coverage command in the repository root."""
    return subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)


def normalize_module(value: str, root: Path) -> str:
    """Map a requested module path to its repository-relative POSIX path."""
    root = root.resolve()
    candidate = Path(value)
    absolute = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        relative = absolute.relative_to(root).as_posix()
    except ValueError as exc:
        raise CensusError(f"{value}: outside the repository root {root}") from exc
    if not absolute.is_file():
        raise CensusError(f"{value}: no such source file under {root}")
    return relative


def select_tests(module: str, root: Path) -> list[str]:
    """Return the pytest files that attribute coverage to one source module.

    The devloop plan marks a test that imports the module itself as a direct
    consumer and a test that reaches it through another module as ``via``.
    Coverage attribution uses the direct consumers; the full affected set only
    runs when no test imports the module directly.
    """
    plan = plan_devloop([module], root)
    selected = [
        test
        for test in plan.modules
        if any(
            " via " not in reason and reason.endswith(f"({module})")
            for reason in plan.reasons.get(test, ())
        )
    ]
    return selected or affected_test_modules([module], root)


def format_ranges(lines: Sequence[int]) -> str:
    """Compress sorted line numbers into a compact ``start-end`` list."""
    if not lines:
        return "-"
    ranges: list[str] = []
    start = previous = lines[0]
    for line in lines[1:]:
        if line == previous + 1:
            previous = line
            continue
        ranges.append(f"{start}-{previous}" if start != previous else str(start))
        start = previous = line
    ranges.append(f"{start}-{previous}" if start != previous else str(start))
    return ",".join(ranges)


def _module_coverage(
    report: object, module: str, tests: tuple[str, ...]
) -> ModuleCoverage:
    """Read one module's summary and missing lines from a coverage JSON report."""
    files = report.get("files") if isinstance(report, dict) else None
    entry = files.get(module) if isinstance(files, dict) else None
    if not isinstance(entry, dict):
        raise CensusError(f"{module}: no coverage entry in the report")
    summary = entry.get("summary")
    missing = entry.get("missing_lines")
    if not isinstance(summary, dict):
        raise CensusError(f"{module}: coverage report has no summary")
    if not isinstance(missing, list):
        raise CensusError(f"{module}: coverage report has no missing-line list")
    try:
        statements = int(summary["num_statements"])
        percent = float(summary["percent_covered"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CensusError(f"{module}: malformed coverage summary: {exc}") from exc
    return ModuleCoverage(
        module=module,
        tests=tests,
        statements=statements,
        missed=len(missing),
        percent=percent,
        missing_lines=tuple(sorted(int(line) for line in missing)),
    )


def _failure_tail(completed: subprocess.CompletedProcess[str]) -> str:
    lines = (completed.stderr or completed.stdout or "").strip().splitlines()
    return lines[-1] if lines else "no output"


def run_census(
    modules: Sequence[str],
    root: Path,
    *,
    runner: Runner | None = None,
    select: Callable[[str, Path], list[str]] | None = None,
) -> list[ModuleCoverage]:
    """Select tests per module, run coverage once, and read one row per module."""
    root = root.resolve()
    run = runner or _subprocess_runner
    choose = select or select_tests
    selected: list[tuple[str, tuple[str, ...]]] = []
    for value in modules:
        module = normalize_module(value, root)
        tests = tuple(choose(module, root))
        if FULL_SUITE_SENTINEL in tests:
            raise CensusError(
                f"{module}: devloop requires the full suite for this path; "
                "pass a source module"
            )
        if not tests:
            raise CensusError(
                f"{module}: no tests selected by devloop; "
                "add an importing test or pass another module"
            )
        selected.append((module, tests))

    # One coverage run serves every requested module.
    tests = sorted({test for _, chosen in selected for test in chosen})
    with tempfile.TemporaryDirectory(prefix="coverage-census-") as temporary:
        data_file = Path(temporary) / ".coverage"
        json_file = Path(temporary) / "coverage.json"
        run_command = [
            *COVERAGE_PREFIX,
            "coverage",
            "run",
            f"--data-file={data_file}",
            f"--source={SOURCE_ROOT}",
            "-m",
            "pytest",
            *PYTEST_FLAGS,
            *tests,
        ]
        completed = run(run_command, root)
        if completed.returncode != 0:
            detail = _failure_tail(completed)
            raise CensusError(
                f"pytest failed under coverage (exit {completed.returncode}): {detail}"
            )
        json_command = [
            *COVERAGE_PREFIX,
            "coverage",
            "json",
            f"--data-file={data_file}",
            f"--include={','.join(module for module, _ in selected)}",
            "-o",
            str(json_file),
        ]
        completed = run(json_command, root)
        if completed.returncode != 0:
            detail = _failure_tail(completed)
            raise CensusError(
                f"coverage json failed (exit {completed.returncode}): {detail}"
            )
        report = json.loads(json_file.read_text(encoding="utf-8"))

    return [_module_coverage(report, module, chosen) for module, chosen in selected]


def format_row(row: ModuleCoverage) -> str:
    """Render one census row for plain-text output."""
    return (
        f"{row.module}  stmts={row.statements} missed={row.missed} "
        f"percent={round(row.percent, 1)} tests={len(row.tests)} "
        f"missing={format_ranges(row.missing_lines)}"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner | None = None,
    select: Callable[[str, Path], list[str]] | None = None,
) -> int:
    """Parse arguments, print the census, and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "modules",
        nargs="+",
        metavar="MODULE",
        help="source module path, such as src/evallab/automation.py",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    parser.add_argument(
        "--min-percent",
        type=float,
        default=None,
        metavar="N",
        help="exit 1 when any module is below N percent covered",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root (default: current directory)",
    )
    args = parser.parse_args(argv)
    try:
        rows = run_census(args.modules, args.root, runner=runner, select=select)
    except CensusError as exc:
        print(f"coverage-census: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([row.as_dict() for row in rows], indent=2))
    else:
        for row in rows:
            print(format_row(row))
            for test in row.tests:
                print(f"  {test}")

    if args.min_percent is not None:
        below = [row for row in rows if row.percent < args.min_percent]
        if below:
            names = ", ".join(row.module for row in below)
            print(
                f"coverage-census: {names} below {args.min_percent} percent",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
