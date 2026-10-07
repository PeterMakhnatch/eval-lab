"""Run every scripted control against fresh worlds, without models or services."""
from __future__ import annotations

import argparse
import json
import platform
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

from agentenv_bench import __version__
from agentenv_bench.generate import SELECTION_RULE, generate_tasks, utc_now, write_json
from agentenv_bench.s3k_1591.controls import CONTROLS, VERSION_PROBE_MAX, VERSION_PROBE_MIN
from agentenv_bench.s3k_1591.grader import grade, verdicts
from agentenv_bench.world import (
    Call,
    direct_call,
    fetch_world,
    materialize,
    sha256_file,
    world_receipt,
)


def _database_hashes(work_dir: Path) -> dict:
    return {path.parent.name: sha256_file(path) for path in sorted((work_dir / "system").glob("*/state.db"))}


def _versions() -> dict:
    import sqlite3
    package = Path(__file__).resolve().parent
    return {
        "python": platform.python_version(), "python_executable": sys.executable,
        "sqlite": sqlite3.sqlite_version, "agentenv_bench": __version__,
        "transport": "direct unchanged upstream Python functions", "third_party_dependencies": [],
        "bench_source_sha256": {str(path.relative_to(package)): sha256_file(path)
                                for path in sorted(package.rglob("*.py"))},
    }


def _trace_call(underlying: Call, calls: list[dict]) -> Call:
    def call(system: str, tool: str, args: dict):
        entry = {"system": system, "tool": tool, "args": args, "started_at_utc": utc_now()}
        calls.append(entry)
        try:
            result = underlying(system, tool, args)
            entry["result"] = result
            return result
        except Exception as error:
            entry["exception"] = {"type": type(error).__name__, "message": str(error)}
            raise
        finally:
            entry["completed_at_utc"] = utc_now()

    return call


def run(task_dir: Path, out_dir: Path, cache_dir: Path) -> int:
    started = utc_now()
    if out_dir.exists():
        raise FileExistsError(f"refusing to overwrite an existing run: {out_dir}")
    out_dir.mkdir(parents=True)
    root = fetch_world(task_dir, cache_dir)
    tasks = generate_tasks(task_dir, root, out_dir / "generated")
    inputs = world_receipt(task_dir, root)
    versions = _versions()
    controls = []
    mismatches = []
    findings = []
    for task in tasks:
        for name, control in CONTROLS.items():
            directory = out_dir / "controls" / task["task_id"] / name
            work_dir = materialize(root, directory)
            before = _database_hashes(work_dir)
            control_started = utc_now()
            underlying = direct_call(work_dir)
            calls = []

            call = _trace_call(underlying, calls)

            error = None
            try:
                report = control.run(call, task["params"])
            except Exception as exception:
                report = ""
                error = {"type": type(exception).__name__, "message": str(exception),
                         "traceback": traceback.format_exc()}
            rows = grade(work_dir, report, task["params"])
            outcomes = verdicts(rows)
            # State controls are compared on bench rules. The explicit report-only
            # mutant necessarily passes those rules and is compared on the proxy.
            observed = outcomes["pass_with_report"] if control.report_only else outcomes["pass_rules"]
            matched = observed == (control.expected == "pass")
            if name == "oracle" and not outcomes["pass_with_report"]:
                matched = False
            if error is not None:
                matched = False  # Crashed mutants are not valid negative controls.
            if not matched:
                mismatches.append({"task_id": task["task_id"], "control": name, "expected": control.expected,
                                   "observed": "pass" if observed else "fail", "error": error})
            if control.expected == "fail" and outcomes["upstream_verdict"] == "PASS":
                kind = "report_only_proxy_required" if control.report_only else "upstream_rule_blind_spot"
                findings.append({"task_id": task["task_id"], "control": name, "finding": kind,
                                 "upstream_verdict": "PASS", "bench_verdict": outcomes["bench_verdict"],
                                 "pass_with_report": outcomes["pass_with_report"], "mistake": control.mistake})
            result = {
                "schema": "agentenv_bench.control_receipt/v1", "task_id": task["task_id"], "control": name,
                "expected": control.expected, "mistake": control.mistake, "report_only": control.report_only,
                "comparison": "pass_with_report" if control.report_only else "bench_verdict",
                "expected_matched": matched, "started_at_utc": control_started, "completed_at_utc": utc_now(),
                "params": task["params"], "rows": rows, **outcomes, "final_report": report,
                "tool_calls": calls, "error": error, "pre_db_sha256": before,
                "post_db_sha256": _database_hashes(work_dir), "work_dir": str(work_dir.resolve()),
                "inputs": inputs, "tool_versions": versions,
                "row_version_discovery": {"method": "nonmutating submit_merit_batch dry_run validation",
                                          "minimum": VERSION_PROBE_MIN, "maximum": VERSION_PROBE_MAX},
            }
            (directory / "final_report.txt").write_text(report + "\n")
            write_json(directory / "receipt.json", result)
            controls.append(result)
    generation = json.loads((out_dir / "generated/generation.json").read_text())
    receipt = {
        "schema": "agentenv_bench.direct_receipt/v1", "started_at_utc": started, "completed_at_utc": utc_now(),
        "safety": {"model_calls": 0, "paid_api_calls": 0, "cloud_sandboxes": 0, "gpus": 0,
                   "downloads": "pinned public Hugging Face files only"},
        "scope": "Task solvability and grader validity controls; no model-capability claim; Harbor is unchanged",
        "inputs": inputs, "tool_versions": versions, "task_count": len(tasks),
        "positive_variant_count": len(tasks) - 1, "negative_variant_count": 0,
        "selection_rule": SELECTION_RULE, "negative_variants_skipped_reason": generation["negative_variants_skipped_reason"],
        "row_version_discovery": {
            "minimum": VERSION_PROBE_MIN, "maximum": VERSION_PROBE_MAX,
            "finding": "Public BlueSky read tools omit row_version; version is discovered with nonmutating dry_run probes.",
            "evidence": "search_merit_cases SELECT omits row_version; get_approval_history case projection omits it. "
                        "An accepted submit_merit_batch(dry_run=True) returns case_results[].new_row_version=current+1.",
            "retry": "on ROW_VERSION_CONFLICT re-read facts, re-probe, retry real submission once with same stable key",
        },
        "grading": {
            "upstream_rules": "Three check_code functions executed verbatim from hash-pinned verifier_meta.json",
            "variants": "Parametrized port of upstream checks preserving original target events when present",
            "bench_supplement": "Non-BlueSky DB-byte isolation, labeled supplemental_not_upstream_world_isolation",
            "report": "Deterministic lexical proxy only, NOT upstream LLM grading",
        },
        "findings": findings, "controls": controls, "mismatches": mismatches, "expected_controls_match": not mismatches,
    }
    write_json(out_dir / "receipt.json", receipt)
    headers = ("TASK", "CONTROL", "UPSTREAM", "RULES", "BENCH", "REPORT", "WITH_REPORT", "EXPECTED", "MATCH")
    table_rows = []
    for result in controls:
        task_rules = [row for row in result["rows"] if row["source"] != "report_proxy"
                      and not row["criterion"].startswith("supplemental_")]
        table_rows.append((
            result["task_id"], result["control"], result["upstream_verdict"].replace("NOT_APPLICABLE", "--"),
            f"{sum(row['result'] for row in task_rules)}/{len(task_rules)}", result["bench_verdict"],
            "PASS" if result["report_proxy_pass"] else "FAIL", "PASS" if result["pass_with_report"] else "FAIL",
            result["expected"].upper(), "YES" if result["expected_matched"] else "NO",
        ))
    widths = [max(len(str(row[column])) for row in [headers, *table_rows]) for column in range(len(headers))]
    table = "\n".join("  ".join(str(value).ljust(widths[index]) for index, value in enumerate(row)).rstrip()
                      for row in [headers, *table_rows])
    print(table)
    print(f"\nTasks: 1 upstream + {len(tasks) - 1} positive data variants; 0 negative variants.")
    print("Negative variants skipped: no operationally returned ineligible cases in the pristine DB.")
    print("RULES = three task rules; BENCH = task rules + non-upstream DB-isolation supplement.")
    print("REPORT = deterministic proxy, NOT upstream LLM grading; vp_claim is compared only on WITH_REPORT.")
    for finding in findings:
        print(f"FINDING: {finding['task_id']}/{finding['control']}: upstream rules PASS despite expected FAIL; "
              f"bench={finding['bench_verdict']}, with_report={'PASS' if finding['pass_with_report'] else 'FAIL'}.")
    print(f"Row-version discovery: no read tool exposes it; nonmutating dry_run probes {VERSION_PROBE_MIN}..{VERSION_PROBE_MAX}.")
    print(f"Receipt: {(out_dir / 'receipt.json').resolve()}")
    print(f"Expected verdict mismatches: {len(mismatches)}")
    (out_dir / "summary.txt").write_text(table + "\n")
    return 1 if mismatches else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("runs/agentenv-bench") / ("direct-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")))
    parser.add_argument("--cache-dir", type=Path, default=Path("runs/agentenv-bench/cache/s3k_1591"))
    args = parser.parse_args()
    raise SystemExit(run(args.task_dir, args.out, args.cache_dir))


if __name__ == "__main__":
    main()
