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
    corpus = payload.get("corpus")
    if not isinstance(corpus, dict):
        raise ValueError(
            f"budgets file {path} declares no 'corpus' block. Budgets are only "
            "meaningful against the corpus they were measured on; declare "
            "roots/jobs/result_json for the pinned corpus."
        )
    for key in ("roots", "jobs", "result_json"):
        if key not in corpus:
            raise ValueError(f"budgets corpus block missing {key!r}: {path}")
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


def assert_corpus_shape(report: dict, budgets: dict) -> list[str]:
    """Fail when a report was measured against a corpus the budgets do not describe.

    A perf number only means something relative to the corpus it was measured
    on. If the profiled corpus changes, every budget silently changes meaning
    and the run is not a usable re-baseline sample either. Say both.
    """
    expected = budgets["corpus"]
    actual_roots = list(report.get("corpus_roots", []))
    expected_roots = list(expected["roots"])
    mismatches: list[str] = []
    if actual_roots != expected_roots:
        mismatches.append(f"corpus_roots {actual_roots} != declared {expected_roots}")
    for key, field in (("jobs", "corpus_jobs"), ("result_json", "corpus_result_json")):
        actual = report.get(field)
        if actual != expected[key]:
            mismatches.append(f"{field} {actual} != declared {expected[key]}")
    if not mismatches:
        return []
    return [
        "profiled corpus does not match the shape the budgets were measured on: "
        + "; ".join(mismatches)
        + ". The six budgets describe the pinned corpus only, so this report "
        "neither passes nor fails them meaningfully, and it MUST NOT be cited "
        "as a re-baseline sample. Either profile the pinned default (drop "
        "--corpus), or, if the pin changed deliberately, re-baseline and update "
        "the 'corpus' block in budgets.json in the same commit."
    ]


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
    corpus_problems = assert_corpus_shape(report, budgets)
    if corpus_problems:
        print("perf corpus mismatch:")
        for item in corpus_problems:
            print(f"  {item}")
        return 1
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
