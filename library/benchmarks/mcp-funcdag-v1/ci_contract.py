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
    repo_root = HERE.parents[2]

    # 1. Reject tracked generated corpus
    res = subprocess.run(["git", "ls-files"], cwd=repo_root, capture_output=True, text=True, check=True)
    tracked_files = res.stdout.splitlines()
    forbidden = [
        f for f in tracked_files
        if f.startswith("derived/harbor-tasks/mcp-funcdag/")
        or f.startswith("library/benchmarks/mcp-funcdag-v1/derived/")
    ]
    if forbidden:
        print(f"ERROR: Found tracked generated benchmark assets: {forbidden}", file=sys.stderr)
        sys.exit(1)

    # 2. Deterministic Regeneration Check across Campaign 0
    tmp_out_1 = repo_root / "derived" / "tmp_ci_regen_1"
    tmp_out_2 = repo_root / "derived" / "tmp_ci_regen_2"

    baseline_cell = CAMPAIGN_0_CELLS[0]
    task_dir_1 = materialize_task(baseline_cell, output_root=tmp_out_1)
    task_dir_2 = materialize_task(baseline_cell, output_root=tmp_out_2)

    snap1 = snapshot_dir(task_dir_1)
    snap2 = snapshot_dir(task_dir_2)

    if snap1 != snap2:
        print("ERROR: Regeneration mismatch between runs:", file=sys.stderr)
        for k in set(snap1.keys()) | set(snap2.keys()):
            if snap1.get(k) != snap2.get(k):
                print(f"  {k}: {snap1.get(k)} vs {snap2.get(k)}", file=sys.stderr)
        sys.exit(1)

    # 3. Control evaluation on in-memory / local artifacts
    evidence_dir = task_dir_1 / "evidence"
    workspace_dir = task_dir_1 / "agent_workspace"
    truth_path = task_dir_1 / "tests" / "fixtures" / "verifier_truth.json"
    spec_data = json.loads((task_dir_1 / "environment" / "runtime_tools.json").read_text(encoding="utf-8"))

    # NOP Control
    materialize_task(baseline_cell, output_root=tmp_out_1)
    runtime = MCPRuntime(spec_data, evidence_dir)
    run_nop_solve(runtime, spec_data, workspace_dir)
    nop_res = verify_execution(task_dir_1, truth_path, evidence_dir, workspace_dir)
    if nop_res["reward"] != 0.0:
        print(f"ERROR: NOP control scored nonzero: {nop_res}", file=sys.stderr)
        sys.exit(1)

    # Oracle Control
    materialize_task(baseline_cell, output_root=tmp_out_1)
    runtime = MCPRuntime(spec_data, evidence_dir)
    run_oracle_solve(runtime, spec_data, workspace_dir)
    oracle_res = verify_execution(task_dir_1, truth_path, evidence_dir, workspace_dir)
    if oracle_res["reward"] != 1.0:
        print(f"ERROR: Oracle control did not score 1.0: {oracle_res}", file=sys.stderr)
        sys.exit(1)

    # Mutant Controls (all 4 must score 0.0)
    mutants = get_mutants()
    for mutant_name, mutant_fn in mutants.items():
        materialize_task(baseline_cell, output_root=tmp_out_1)
        runtime = MCPRuntime(spec_data, evidence_dir)
        mutant_fn(runtime, spec_data, workspace_dir)
        mutant_res = verify_execution(task_dir_1, truth_path, evidence_dir, workspace_dir)
        if mutant_res["reward"] != 0.0:
            print(f"ERROR: Mutant '{mutant_name}' scored nonzero reward: {mutant_res}", file=sys.stderr)
            sys.exit(1)

    print("mcp-funcdag-v1 CI contract passed successfully:")
    print("  - Corpus guard passed (zero tracked generated assets)")
    print("  - Deterministic regeneration byte-equal")
    print(f"  - NOP control reward: {nop_res['reward']}")
    print(f"  - Oracle solver reward: {oracle_res['reward']} (100% schema conformance & value propagation)")
    print(f"  - {len(mutants)} Mutants ({', '.join(mutants.keys())}) reward: 0.0")


if __name__ == "__main__":
    main()
