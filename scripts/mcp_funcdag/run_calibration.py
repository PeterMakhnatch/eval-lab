#!/usr/bin/env python3
"""CLI helper to run calibration controls across Campaign 0 cells."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = ROOT / "library" / "benchmarks" / "mcp-funcdag-v1"
sys.path.insert(0, str(BENCH_ROOT))

from contract import CAMPAIGN_0_CELLS
from materializer import materialize_task
from runtime import MCPRuntime
from templates import get_mutants, run_nop_solve, run_oracle_solve
from verifier import verify_execution


def main():
    print(f"Running MCP FuncDAG Campaign 0 calibration across {len(CAMPAIGN_0_CELLS)} cells...")
    results = []

    for cell in CAMPAIGN_0_CELLS:
        name = cell["name"]
        task_dir = materialize_task(cell)
        spec_data = json.loads((task_dir / "environment" / "runtime_tools.json").read_text())
        truth_path = task_dir / "tests" / "verifier_truth.json"
        evidence_dir = task_dir / "evidence"
        workspace_dir = task_dir / "agent_workspace"

        # Oracle run
        runtime = MCPRuntime(spec_data, evidence_dir)
        run_oracle_solve(runtime, spec_data, workspace_dir)
        oracle_res = verify_execution(task_dir, truth_path, evidence_dir, workspace_dir)

        # NOP run
        materialize_task(cell)
        runtime = MCPRuntime(spec_data, evidence_dir)
        run_nop_solve(runtime, spec_data, workspace_dir)
        nop_res = verify_execution(task_dir, truth_path, evidence_dir, workspace_dir)

        cell_summary = {
            "cell": name,
            "oracle_reward": oracle_res["reward"],
            "oracle_schema_conformance": oracle_res["schema_conformance_rate"],
            "oracle_dag_conformance": oracle_res["dag_conformance"],
            "oracle_val_prop": oracle_res["value_propagation_accuracy"],
            "nop_reward": nop_res["reward"],
        }
        results.append(cell_summary)
        print(f"  [{name}] Oracle: {oracle_res['reward']} | NOP: {nop_res['reward']} | DAG conf: {oracle_res['dag_conformance']}")

    print("\nCalibration Summary:")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
