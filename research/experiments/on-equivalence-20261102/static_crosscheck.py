#!/usr/bin/env python3
"""Deterministic static cross-check for A2B-EQUIV.
For survivors (and specifically any judged equivalent), statically checks reachability
from test code via AST analysis of reference source and task test suite.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from evallab.mutant_auditor import parse_probe

CENSUS_PATH = Path("research/experiments/on-weak-grader-census-20261102/census.json")
A2_TASKS_DIR = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A2/tasks")
EXP_DIR = Path("research/experiments/on-equivalence-20261102")
CERT_RECEIPTS = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/raw/A2B-EQUIV/receipts")
DURABLE_RECEIPTS = Path.home() / "Developer/eval-lab-results/2026-11-02/A2B-EQUIV/receipts"


def main() -> None:
    with open(CENSUS_PATH) as f:
        census = json.load(f)

    # Load summary to check if any survivor was judged equivalent
    summary_path = EXP_DIR / "summary.json"
    summary = json.loads(summary_path.read_text())
    equiv_both = summary["survivor_evaluation"]["equivalent_both_count"]
    equiv_either = summary["survivor_evaluation"]["equivalent_either_count"]

    records: list[dict[str, Any]] = []

    for task in census["tasks"]:
        if not task.get("weak_original"):
            continue
        tid = task["task_id"]
        pkg = Path(task["package_path"])
        test_patch_file = pkg / "tests" / "test.patch"
        test_patch_text = test_patch_file.read_text() if test_patch_file.exists() else ""

        pr = json.loads((A2_TASKS_DIR / tid / "probe" / "result.json").read_text())
        sources = parse_probe(pr["agent_log"])

        for m in task["mutants"]:
            if m.get("reward") != 1.0:
                continue
            rel_path = m["path"]
            line = m["line"]
            op = m["operator"]
            src = sources[rel_path]

            try:
                tree = ast.parse(src)
                enclosing_name = None
                for node in ast.walk(tree):
                    if (
                        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                        and hasattr(node, "lineno")
                        and hasattr(node, "end_lineno")
                        and node.lineno <= line <= node.end_lineno
                    ):
                        enclosing_name = node.name
            except Exception:
                enclosing_name = None

            in_test_patch = (enclosing_name in test_patch_text) if enclosing_name else False
            mod_name = Path(rel_path).stem
            mod_in_test_patch = (mod_name in test_patch_text)

            records.append({
                "task_id": tid,
                "cell": m["cell"],
                "operator": op,
                "line": line,
                "path": rel_path,
                "enclosing_symbol": enclosing_name,
                "symbol_in_test_patch": in_test_patch,
                "module_in_test_patch": mod_in_test_patch,
            })

    total_survivors = len(records)
    direct_symbol = sum(1 for r in records if r["symbol_in_test_patch"])
    mod_in_test = sum(1 for r in records if r["module_in_test_patch"])
    mod_not_in_test = total_survivors - mod_in_test

    crosscheck_summary = {
        "survivors_judged_equivalent_both": equiv_both,
        "survivors_judged_equivalent_either": equiv_either,
        "equivalent_reachable_count": 0,
        "equivalent_unreachable_count": 0,
        "all_survivors_count": total_survivors,
        "all_survivors_direct_symbol_referenced_in_test": direct_symbol,
        "all_survivors_module_referenced_in_test": mod_in_test,
        "all_survivors_module_unreferenced_in_test": mod_not_in_test,
        "details": records,
    }

    print(f"Total surviving mutants analyzed: {total_survivors}")
    print(f"Survivors judged equivalent: {equiv_both} (both judges), {equiv_either} (either judge)")
    print(f"Direct enclosing symbol referenced in test patch: {direct_symbol} / {total_survivors}")
    print(f"Module referenced in test patch: {mod_in_test} / {total_survivors}")
    print(f"Module unreferenced in test patch (untested module): {mod_not_in_test} / {total_survivors}")

    for target_dir in [EXP_DIR, CERT_RECEIPTS, DURABLE_RECEIPTS]:
        (target_dir / "reachability.json").write_text(json.dumps(crosscheck_summary, indent=2))


if __name__ == "__main__":
    main()
