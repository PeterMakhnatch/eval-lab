#!/usr/bin/env python3
"""Print the slowest test files of a pytest JUnit report with summed durations."""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_TOP = 20


def test_file(classname: str) -> str:
    """Return the test module path a JUnit ``classname`` belongs to.

    ``tests.test_x`` and ``tests.test_x.TestY`` (or nested classes) both map to
    ``tests/test_x.py``; the module is the first ``test_*`` segment.
    """
    parts = classname.split(".")
    for index, part in enumerate(parts):
        if part.startswith("test_"):
            return "/".join(parts[: index + 1]) + ".py"
    # No test_* module segment: drop trailing class-like (capitalised) segments.
    while len(parts) > 1 and parts[-1][:1].isupper():
        parts.pop()
    return "/".join(parts) + ".py" if classname else "<no classname>"


def aggregate(report: Path) -> dict[str, tuple[float, int]]:
    """Sum testcase seconds and count testcases per test file."""
    files: dict[str, tuple[float, int]] = {}
    for testcase in ET.parse(report).getroot().iter("testcase"):
        path = test_file(testcase.get("classname", ""))
        seconds, tests = files.get(path, (0.0, 0))
        files[path] = (seconds + float(testcase.get("time") or 0.0), tests + 1)
    return files


def report_slowest(report: Path, *, top: int = DEFAULT_TOP) -> None:
    files = aggregate(report)
    ranked = sorted(files.items(), key=lambda item: (-item[1][0], item[0]))
    for path, (seconds, tests) in ranked[:top]:
        print(f"SLOWEST seconds={seconds:.2f} tests={tests} file={path}")
    total = sum(seconds for seconds, _ in files.values())
    print(f"SLOWEST total seconds={total:.2f} files={len(files)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help="number of files to print")
    parser.add_argument("report", type=Path, help="pytest JUnit XML report")
    args = parser.parse_args()
    report_slowest(args.report, top=args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
