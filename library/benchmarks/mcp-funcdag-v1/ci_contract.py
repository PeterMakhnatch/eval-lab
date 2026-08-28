#!/usr/bin/env python3
"""CI contract: deterministic regeneration, oracle/NOP/mutant verification, corpus guard."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from contract import CAMPAIGN_0_CELLS
from dag_generator import generate_dag_spec
from materializer import materialize_task
from runtime import MCPRuntime
from templates import get_mutants, run_nop_solve, run_oracle_solve
from verifier import verify_execution


def snapshot_dir(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.endswith(".pyc") and "__pycache__" not in p.parts
    }


def main():
    # 1. Corpus Guard: Reject any committed generated task tree
    tracked = subprocess.check_output(
        ["git", "ls-files", "library/benchmarks/mcp-funcdag-v1/derived", "derived/harbor-tasks/mcp-funcdag"],
        text=True,
    ).splitlines()
    if tracked:
        raise AssertionError(f"Generated task corpus is tracked in Git: {tracked}")

    # 2. Test Materialization & Deterministic Regeneration
    baseline_cell = CAMPAIGN_0_CELLS[0]
    task_dir_1 = materialize_task(baseline_cell)
    snap_1 = snapshot_dir(task_dir_1)

    task_dir_2 = materialize_task(baseline_cell)
    snap_2 = snapshot_dir(task_dir_2)

    if snap_1 != snap_2:
        diff_keys = set(snap_1.keys()) ^ set(snap_2.keys())
        diff_vals = {k for k in snap_1 if snap_1[k] != snap_2.get(k)}
        raise AssertionError(f"Regeneration is not deterministic. Diffs: {diff_keys | diff_vals}")

    # 3. Oracle, NOP, and Mutant Control Tests
    spec_data = json.loads((task_dir_1 / "environment" / "runtime_tools.json").read_text())
    truth_path = task_dir_1 / "tests" / "verifier_truth.json"
    evidence_dir = task_dir_1 / "evidence"
    workspace_dir = task_dir_1 / "agent_workspace"

    # A. Test NOP control (must score 0.0)
    materialize_task(baseline_cell)
    runtime = MCPRuntime(spec_data, evidence_dir)
    run_nop_solve(runtime, spec_data, workspace_dir)
    nop_res = verify_execution(task_dir_1, truth_path, evidence_dir, workspace_dir)
    if nop_res["reward"] != 0.0:
        raise AssertionError(f"NOP control received reward {nop_res['reward']}, expected 0.0")

    # B. Test Oracle solver (must score 1.0)
    materialize_task(baseline_cell)
    runtime = MCPRuntime(spec_data, evidence_dir)
    run_oracle_solve(runtime, spec_data, workspace_dir)
    oracle_res = verify_execution(task_dir_1, truth_path, evidence_dir, workspace_dir)
    if oracle_res["reward"] != 1.0:
        raise AssertionError(f"Oracle solver received reward {oracle_res['reward']}, expected 1.0: {oracle_res}")
    if oracle_res["schema_conformance_rate"] != 1.0:
        raise AssertionError(f"Oracle schema conformance is {oracle_res['schema_conformance_rate']}, expected 1.0")
    if not oracle_res["dag_conformance"]:
        raise AssertionError("Oracle DAG conformance failed")
    if oracle_res["value_propagation_accuracy"] != 1.0:
        raise AssertionError(f"Oracle value propagation accuracy is {oracle_res['value_propagation_accuracy']}, expected 1.0")

    # C. Test Mutants (wrong-order, wrong-value, distractor-trace must all score 0.0)
    for mutant_name, mutant_fn in get_mutants().items():
        materialize_task(baseline_cell)
        runtime = MCPRuntime(spec_data, evidence_dir)
        mutant_fn(runtime, spec_data, workspace_dir)
        mutant_res = verify_execution(task_dir_1, truth_path, evidence_dir, workspace_dir)
        if mutant_res["reward"] != 0.0:
            raise AssertionError(f"Mutant '{mutant_name}' received reward {mutant_res['reward']}, expected 0.0: {mutant_res}")

    print("mcp-funcdag-v1 CI contract passed successfully:")
    print("  - Corpus guard passed (zero tracked generated assets)")
    print("  - Deterministic regeneration byte-equal")
    print("  - NOP control reward: 0.0")
    print("  - Oracle solver reward: 1.0 (100% schema conformance & value propagation)")
    print("  - 3 Mutants (wrong-order, wrong-value, distractor-trace) reward: 0.0")


if __name__ == "__main__":
    main()
