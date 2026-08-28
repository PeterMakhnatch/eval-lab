from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

BENCH_ROOT = Path(__file__).parents[1] / "library" / "benchmarks" / "mcp-funcdag-v1"


def _load_module(name: str):
    module_name = f"mcp_funcdag_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    orig_path = list(sys.path)
    sys.path.insert(0, str(BENCH_ROOT))
    try:
        spec = importlib.util.spec_from_file_location(module_name, BENCH_ROOT / f"{name}.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path[:] = orig_path
        for generic_name in ("contract", "dag_generator", "materializer", "runtime", "templates", "verifier"):
            mod = sys.modules.get(generic_name)
            if mod is not None and getattr(mod, "__file__", "").startswith(str(BENCH_ROOT)):
                del sys.modules[generic_name]


def test_dag_generator_determinism():
    dag_gen = _load_module("dag_generator")
    spec1 = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2)
    spec2 = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2)
    assert spec1.target_node_id == spec2.target_node_id
    assert spec1.expected_target_value == spec2.expected_target_value
    assert spec1.topological_order == spec2.topological_order
    assert len(spec1.nodes) == 5


def test_benchmark_contract_and_campaign_cells():
    contract_mod = _load_module("contract")
    dag_gen = _load_module("dag_generator")
    factors = contract_mod.CellFactors(depth=3, width=2, distractor_count=2)
    spec = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2)
    contract = contract_mod.make_benchmark_contract(factors, spec, "test-task-1")
    assert contract.family == "mcp-funcdag-v1"
    assert contract.opportunity_counts["required_node_count"] == 5
    assert len(contract_mod.CAMPAIGN_0_CELLS) == 30
    assert "saturation_state" not in contract.to_dict()


def test_streamable_mcp_runtime_and_events(tmp_path):
    dag_gen = _load_module("dag_generator")
    runtime_mod = _load_module("runtime")
    spec = dag_gen.generate_dag_spec(seed=42, depth=2, width=2, distractor_count=1)
    spec_dict = {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": [
                    {
                        "name": p.name,
                        "type_name": p.type_name,
                        "description": p.description,
                        "required": p.required,
                    }
                    for p in t.parameters
                ],
                "output_type": t.output_type,
                "is_distractor": t.is_distractor,
                "op_kind": t.op_kind,
            }
            for t in spec.tools
        ],
        "nodes": [
            {
                "node_id": n.node_id,
                "tool_name": n.tool_name,
                "op_name": n.op_name,
                "input_bindings": n.input_bindings,
            }
            for n in spec.nodes
        ],
        "initial_inputs": spec.initial_inputs,
        "target_node_id": spec.target_node_id,
        "topological_order": spec.topological_order,
    }
    runtime = runtime_mod.MCPRuntime(spec_dict, tmp_path)
    target_node = spec.nodes[0]
    args = {k: spec.initial_inputs[src] for k, src in target_node.input_bindings.items()}
    out = runtime.call_tool(target_node.tool_name, args)
    assert out["result"]["value"] == spec.reference_node_values[target_node.node_id]
    runtime.call_tool(target_node.tool_name, args)
    assert runtime.redundant_calls == 1
    different_args = {k: (v + 1 if isinstance(v, int) else v) for k, v in args.items()}
    runtime.call_tool(target_node.tool_name, different_args)
    assert runtime.redundant_calls == 1


def test_materializer_uses_fastmcp_substrate(tmp_path):
    contract_mod = _load_module("contract")
    materializer_mod = _load_module("materializer")
    cell = contract_mod.CAMPAIGN_0_CELLS[0]
    task_dir = materializer_mod.materialize_task(cell, output_root=tmp_path)
    sidecar = task_dir / "environment" / "mcp-server"
    server_py = (sidecar / "server.py").read_text(encoding="utf-8")
    assert "from fastmcp import FastMCP" in server_py
    assert 'transport="http"' in server_py
    assert not (sidecar / "runtime.py").exists()
    compose = (task_dir / "environment" / "docker-compose.yaml").read_text(encoding="utf-8")
    assert "mcp-service:" in compose
    assert "workbench-internal:" in compose
    assert "internal: true" in compose
    assert "evidence-volume:" in compose
    task_toml = (task_dir / "task.toml").read_text(encoding="utf-8")
    assert 'transport = "streamable-http"' in task_toml
    assert "http://mcp-service:8080/mcp" in task_toml
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    assert "Dependency Graph Nodes:" in instruction
    assert "uses tool" in instruction


def test_materializer_oracle_nop_mutants_and_answer_only(tmp_path):
    contract_mod = _load_module("contract")
    materializer_mod = _load_module("materializer")
    runtime_mod = _load_module("runtime")
    templates_mod = _load_module("templates")
    verifier_mod = _load_module("verifier")
    cell = contract_mod.CAMPAIGN_0_CELLS[0]
    task_dir = materializer_mod.materialize_task(cell, output_root=tmp_path)
    spec_data = json.loads((task_dir / "environment" / "runtime_tools.json").read_text())
    truth_path = task_dir / "tests" / "verifier_truth.json"
    evidence_dir = tmp_path / "evidence-oracle"
    workspace_dir = tmp_path / "workspace-oracle"
    runtime = runtime_mod.MCPRuntime(spec_data, evidence_dir)
    templates_mod.run_oracle_solve(runtime, spec_data, workspace_dir)
    res_oracle = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, workspace_dir)
    assert res_oracle["reward"] == 1.0
    assert res_oracle["dag_conformance"] is True
    assert res_oracle["value_propagation_accuracy"] == 1.0

    evidence_nop = tmp_path / "evidence-nop"
    workspace_nop = tmp_path / "workspace-nop"
    runtime = runtime_mod.MCPRuntime(spec_data, evidence_nop)
    templates_mod.run_nop_solve(runtime, spec_data, workspace_nop)
    assert verifier_mod.verify_execution(task_dir, truth_path, evidence_nop, workspace_nop)["reward"] == 0.0

    evidence_ao = tmp_path / "evidence-ao"
    workspace_ao = tmp_path / "workspace-ao"
    workspace_ao.mkdir()
    truth = json.loads(truth_path.read_text())
    (workspace_ao / "result.json").write_text(json.dumps({"target_value": truth["expected_target_value"]}))
    runtime = runtime_mod.MCPRuntime(spec_data, evidence_ao)
    assert verifier_mod.verify_execution(task_dir, truth_path, evidence_ao, workspace_ao)["reward"] == 0.0

    for mname, mfn in templates_mod.get_mutants().items():
        evidence_m = tmp_path / f"evidence-{mname}"
        workspace_m = tmp_path / f"workspace-{mname}"
        runtime = runtime_mod.MCPRuntime(spec_data, evidence_m)
        mfn(runtime, spec_data, workspace_m)
        res = verifier_mod.verify_execution(task_dir, truth_path, evidence_m, workspace_m)
        assert res["reward"] == 0.0, f"Mutant {mname} did not score 0.0"


def test_coexistence_with_loca_lean():
    loca_test_path = Path(__file__).parents[1] / "tests" / "test_loca_lean.py"
    spec = importlib.util.spec_from_file_location("test_loca_lean_isolated", loca_test_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    loca_source = mod.load("loca_source_coexist", "source")
    assert loca_source is not None
