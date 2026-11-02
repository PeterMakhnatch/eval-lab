#!/usr/bin/env python3
"""Fail when a JUnit report contains a skipped test with a Harbor-named reason."""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def check(report: Path, *, report_only: bool = False) -> int:
    """Print skip-reason counts and optionally reject Harbor-named skips."""
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

    ordered_reasons = (
        sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
        if report_only
        else sorted(reasons.items())
    )
    for reason, count in ordered_reasons:
        print(f"SKIP reason count={count}: {reason}")

    for test_id, reason in harbor_skips:
        print(f"Harbor-named skip: {test_id}: {reason}")
    return 0 if report_only else int(bool(harbor_skips))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="rank reasons by count and exit successfully even for Harbor-named skips",
    )
    parser.add_argument("report", type=Path, help="pytest JUnit XML report")
    args = parser.parse_args()
    return check(args.report, report_only=args.report_only)


if __name__ == "__main__":
    raise SystemExit(main())
