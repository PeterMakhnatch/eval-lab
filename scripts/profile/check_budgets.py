"""Fail if a profile report exceeds committed median budgets by more than X%."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REQUIRED_PATHS = (
    "ingest",
    "projection",
    "facts",
    "digest",
    "queue-tick-100",
    "fleet-status",
)


def load_budgets(path: Path) -> dict:
    payload = json.loads(path.read_text())
    if "tolerance_pct" not in payload or "paths" not in payload:
        raise ValueError(f"invalid budgets file: {path}")
    missing = [name for name in REQUIRED_PATHS if name not in payload["paths"]]
    if missing:
        raise ValueError(f"budgets missing paths: {missing}")
    return payload


def load_report(path: Path) -> dict:
    payload = json.loads(path.read_text())
    names = [item["path"] for item in payload.get("paths", [])]
    missing = [name for name in REQUIRED_PATHS if name not in names]
    if missing:
        raise ValueError(f"report missing paths: {missing}")
    return payload


def evaluate(report: dict, budgets: dict) -> list[str]:
    tolerance = float(budgets["tolerance_pct"])
    by_path = {item["path"]: item for item in report["paths"]}
    failures: list[str] = []
    for name, budget_ms in budgets["paths"].items():
        median = float(by_path[name]["median_ms"])
        ceiling = float(budget_ms) * (1.0 + tolerance / 100.0)
        if median > ceiling:
            failures.append(
                f"{name}: median {median:.3f} ms exceeds budget "
                f"{budget_ms:.3f} ms + {tolerance:g}% "
                f"(ceiling {ceiling:.3f} ms)"
            )
    return failures


def maybe_rebaseline_notice(report: dict, budgets: dict) -> list[str]:
    notices: list[str] = []
    by_path = {item["path"]: item for item in report["paths"]}
    for name, budget_ms in budgets["paths"].items():
        median = float(by_path[name]["median_ms"])
        if median < 0.5 * float(budget_ms):
            notices.append(
                f"{name}: median {median:.3f} ms is under 50% of budget "
                f"{budget_ms:.3f} ms — consider re-baselining"
            )
    return notices


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument(
        "--budgets",
        type=Path,
        default=Path(__file__).resolve().parent / "budgets.json",
    )
    args = parser.parse_args(argv)
    report = load_report(args.report)
    budgets = load_budgets(args.budgets)
    failures = evaluate(report, budgets)
    for notice in maybe_rebaseline_notice(report, budgets):
        print(f"notice: {notice}")
    if failures:
        print("perf budget exceeded:")
        for item in failures:
            print(f"  {item}")
        return 1
    print("perf budgets ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
