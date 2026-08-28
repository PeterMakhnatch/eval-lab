#!/usr/bin/env python3
"""CLI helper to run calibration controls across Campaign 0 cells."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = ROOT / "library" / "benchmarks" / "mcp-funcdag-v1"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, BENCH_ROOT / f"{filename}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {name} from {filename}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


contract_mod = _load_module("mcp_funcdag_contract", "contract")
materializer_mod = _load_module("mcp_funcdag_materializer", "materializer")
runtime_mod = _load_module("mcp_funcdag_runtime", "runtime")
templates_mod = _load_module("mcp_funcdag_templates", "templates")
verifier_mod = _load_module("mcp_funcdag_verifier", "verifier")


def main():
    cells = contract_mod.CAMPAIGN_0_CELLS
    print(f"Running MCP FuncDAG Campaign 0 calibration across {len(cells)} cells...")
    results = []

    for cell in cells:
        name = f"{cell['name']}_s{cell['seed']}"
        task_dir = materializer_mod.materialize_task(cell)
        spec_data = json.loads((task_dir / "environment" / "runtime_tools.json").read_text())
        truth_path = task_dir / "tests" / "verifier_truth.json"
        evidence_dir = task_dir / "evidence"
        workspace_dir = task_dir / "agent_workspace"

        # Oracle run
        runtime = runtime_mod.MCPRuntime(spec_data, evidence_dir)
        templates_mod.run_oracle_solve(runtime, spec_data, workspace_dir)
        oracle_res = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, workspace_dir)

        # NOP run
        materializer_mod.materialize_task(cell)
        runtime = runtime_mod.MCPRuntime(spec_data, evidence_dir)
        templates_mod.run_nop_solve(runtime, spec_data, workspace_dir)
        nop_res = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, workspace_dir)

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
