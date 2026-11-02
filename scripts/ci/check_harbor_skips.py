#!/usr/bin/env python3
"""Fail when a JUnit report contains a skipped test with a Harbor-named reason."""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def check(report: Path) -> int:
    """Print skip-reason counts and reject skips whose messages name Harbor."""
    root = ET.parse(report).getroot()
    reasons: Counter[str] = Counter()
    harbor_skips: list[tuple[str, str]] = []

    for testcase in root.iter("testcase"):
        for skipped in testcase.findall("skipped"):
            reason = " ".join(skipped.get("message", "").split()) or "<no message>"
            reasons[reason] += 1
            if "harbor" in reason.lower():
                classname = testcase.get("classname", "")
                name = testcase.get("name", "<unnamed test>")
                test_id = f"{classname}.{name}" if classname else name
                harbor_skips.append((test_id, reason))

    for reason, count in sorted(reasons.items()):
        print(f"SKIP reason count={count}: {reason}")

    for test_id, reason in harbor_skips:
        print(f"Harbor-named skip: {test_id}: {reason}")
    return int(bool(harbor_skips))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="pytest JUnit XML report")
    args = parser.parse_args()
    return check(args.report)


if __name__ == "__main__":
    raise SystemExit(main())
