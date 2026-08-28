from __future__ import annotations

import http.client
import importlib.util
import json
import socketserver
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).parents[1] / "library" / "benchmarks" / "mcp-funcdag-v1"
sys.path.insert(0, str(ROOT))


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_dag_generator_determinism():
    dag_gen = load_module("dag_generator")
    spec1 = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2)
    spec2 = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2)

    assert spec1.target_node_id == spec2.target_node_id
    assert spec1.expected_target_value == spec2.expected_target_value
    assert spec1.topological_order == spec2.topological_order
    assert len(spec1.tools) == len(spec2.tools)
    assert len(spec1.nodes) == 5


def test_benchmark_contract_and_campaign_cells():
    contract_mod = load_module("contract")
    dag_gen = load_module("dag_generator")
    factors = contract_mod.CellFactors(depth=3, width=2, distractor_count=2)
    spec = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2)
    contract = contract_mod.make_benchmark_contract(factors, spec, "test-task-1")

    assert contract.family == "mcp-funcdag-v1"
    assert contract.opportunity_counts["required_node_count"] == 5
    assert contract.opportunity_counts["distractor_count"] == 2
    assert len(contract_mod.CAMPAIGN_0_CELLS) == 30


def test_streamable_mcp_runtime_and_events(tmp_path):
    dag_gen = load_module("dag_generator")
    runtime_mod = load_module("runtime")
    spec = dag_gen.generate_dag_spec(seed=42, depth=2, width=2, distractor_count=1)

    spec_dict = {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": [
                    {"name": p.name, "type_name": p.type_name, "description": p.description, "required": p.required}
                    for p in t.parameters
                ],
                "output_type": t.output_type,
                "is_distractor": t.is_distractor,
                "op_kind": t.op_kind,
            }
            for t in spec.tools
        ],
        "nodes": [
            {"node_id": n.node_id, "tool_name": n.tool_name, "op_name": n.op_name, "input_bindings": n.input_bindings}
            for n in spec.nodes
        ],
        "initial_inputs": spec.initial_inputs,
        "target_node_id": spec.target_node_id,
        "topological_order": spec.topological_order,
    }

    runtime = runtime_mod.MCPRuntime(spec_dict, tmp_path)
    tools = runtime.list_tools()
    assert len(tools) == len(spec.tools)

    target_node = spec.nodes[0]
    args = {k: spec.initial_inputs[src] for k, src in target_node.input_bindings.items()}
    out = runtime.call_tool(target_node.tool_name, args)
    assert "result" in out
    assert out["result"]["value"] == spec.reference_node_values[target_node.node_id]

    out_dup = runtime.call_tool(target_node.tool_name, args)
    assert "result" in out_dup
    assert runtime.redundant_calls == 1

    different_args = {k: (v + 1 if isinstance(v, int) else v) for k, v in args.items()}
    runtime.call_tool(target_node.tool_name, different_args)
    assert runtime.redundant_calls == 1

    bad_out = runtime.call_tool(target_node.tool_name, {"x": "not_an_int"})
    assert "error" in bad_out

    unk_out = runtime.call_tool("non_existent_tool", {})
    assert "error" in unk_out

    events_file = tmp_path / "benchmark-events.jsonl"
    assert events_file.exists()
    lines = [json.loads(line) for line in events_file.read_text().splitlines() if line.strip()]
    assert len(lines) == 5
    assert lines[0]["event_type"] == "tool_call_success"
    assert lines[1]["is_redundant"] is True
    assert lines[2]["is_redundant"] is False
    assert lines[3]["schema_conforming"] is False
    assert lines[4]["event_type"] == "tool_call_rejected"


def test_materializer_harbor_topology_and_controls(tmp_path):
    contract_mod = load_module("contract")
    materializer_mod = load_module("materializer")
    runtime_mod = load_module("runtime")
    templates_mod = load_module("templates")
    verifier_mod = load_module("verifier")

    cell = contract_mod.CAMPAIGN_0_CELLS[0]
    task_dir = materializer_mod.materialize_task(cell, output_root=tmp_path)
    
    assert (task_dir / "task.toml").exists()
    instruction_text = (task_dir / "instruction.md").read_text()
    assert "Dependency Graph Nodes:" in instruction_text
    assert "uses tool" in instruction_text
    assert (task_dir / "environment" / "Dockerfile").exists()
    assert (task_dir / "environment" / "docker-compose.yaml").exists()
    assert (task_dir / "environment" / "mcp-server" / "Dockerfile").exists()
    assert (task_dir / "solution" / "solve.sh").exists()
    assert (task_dir / "solution" / "solve.py").exists()
    assert (task_dir / "tests" / "Dockerfile").exists()
    assert (task_dir / "tests" / "test.sh").exists()
    assert (task_dir / "tests" / "verifier_truth.json").exists()

    spec_data = json.loads((task_dir / "environment" / "runtime_tools.json").read_text())
    truth_path = task_dir / "tests" / "verifier_truth.json"
    evidence_dir = task_dir / "evidence"
    workspace_dir = task_dir / "agent_workspace"

    # Test Oracle -> 1.0
    runtime = runtime_mod.MCPRuntime(spec_data, evidence_dir)
    templates_mod.run_oracle_solve(runtime, spec_data, workspace_dir)
    res_oracle = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, workspace_dir)
    assert res_oracle["reward"] == 1.0
    assert res_oracle["dag_conformance"] is True
    assert res_oracle["value_propagation_accuracy"] == 1.0

    # Test NOP -> 0.0
    materializer_mod.materialize_task(cell, output_root=tmp_path)
    runtime = runtime_mod.MCPRuntime(spec_data, evidence_dir)
    templates_mod.run_nop_solve(runtime, spec_data, workspace_dir)
    res_nop = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, workspace_dir)
    assert res_nop["reward"] == 0.0

    # Test Answer-only exploit without tool execution -> 0.0
    materializer_mod.materialize_task(cell, output_root=tmp_path)
    runtime = runtime_mod.MCPRuntime(spec_data, evidence_dir)
    truth_data = json.loads(truth_path.read_text())
    (workspace_dir / "result.json").write_text(json.dumps({"target_value": truth_data["expected_target_value"]}))
    res_answer_only = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, workspace_dir)
    assert res_answer_only["reward"] == 0.0

    # Test Mutants -> 0.0
    for mname, mfn in templates_mod.get_mutants().items():
        materializer_mod.materialize_task(cell, output_root=tmp_path)
        runtime = runtime_mod.MCPRuntime(spec_data, evidence_dir)
        mfn(runtime, spec_data, workspace_dir)
        res_mutant = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, workspace_dir)
        assert res_mutant["reward"] == 0.0, f"Mutant {mname} did not score 0.0"


def test_standard_mcp_client_compatibility_and_event_ledger(tmp_path):
    """Standard client compatibility: initialize, tools/list, and tools/call JSON-RPC methods against /mcp endpoint."""
    dag_gen = load_module("dag_generator")
    runtime_mod = load_module("runtime")
    # Baseline cell with 5 DAG nodes (2 unique ops) + 2 distractors -> 7 tools total
    spec = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2)

    spec_dict = {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": [
                    {"name": p.name, "type_name": p.type_name, "description": p.description, "required": p.required}
                    for p in t.parameters
                ],
                "output_type": t.output_type,
                "is_distractor": t.is_distractor,
                "op_kind": t.op_kind,
            }
            for t in spec.tools
        ],
        "nodes": [
            {"node_id": n.node_id, "tool_name": n.tool_name, "op_name": n.op_name, "input_bindings": n.input_bindings}
            for n in spec.nodes
        ],
        "initial_inputs": spec.initial_inputs,
        "target_node_id": spec.target_node_id,
        "topological_order": spec.topological_order,
    }

    evidence_dir = tmp_path / "evidence"
    runtime = runtime_mod.MCPRuntime(spec_dict, evidence_dir)
    handler = runtime_mod.make_mcp_handler(runtime)

    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

        # 1. Standard MCP initialize handshake
        init_payload = json.dumps({
            "jsonrpc": "2.0",
            "id": "init_1",
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}}
        })
        conn.request("POST", "/mcp", body=init_payload, headers={"Content-Type": "application/json"})
        init_res = conn.getresponse()
        assert init_res.status == 200
        init_data = json.loads(init_res.read().decode())
        assert init_data["result"]["protocolVersion"] == "2024-11-05"
        assert "capabilities" in init_data["result"]

        # 2. Standard MCP tools/list
        list_payload = json.dumps({
            "jsonrpc": "2.0",
            "id": "list_1",
            "method": "tools/list",
            "params": {}
        })
        conn.request("POST", "/mcp", body=list_payload, headers={"Content-Type": "application/json"})
        list_res = conn.getresponse()
        assert list_res.status == 200
        list_data = json.loads(list_res.read().decode())
        assert len(list_data["result"]["tools"]) == len(spec.tools) == 7

        # 3. Standard MCP tools/call
        target_node = spec.nodes[0]
        args = {k: spec.initial_inputs[src] for k, src in target_node.input_bindings.items()}
        call_payload = json.dumps({
            "jsonrpc": "2.0",
            "id": "call_1",
            "method": "tools/call",
            "params": {"name": target_node.tool_name, "arguments": args}
        })
        conn.request("POST", "/mcp", body=call_payload, headers={"Content-Type": "application/json"})
        call_res = conn.getresponse()
        assert call_res.status == 200
        call_data = json.loads(call_res.read().decode())
        assert call_data["result"]["value"] == spec.reference_node_values[target_node.node_id]

        # 4. Confirm event ledger captures call
        conn.request("GET", "/events")
        ev_res = conn.getresponse()
        assert ev_res.status == 200
        ev_lines = ev_res.read().decode().splitlines()
        assert len(ev_lines) == 1
        ev = json.loads(ev_lines[0])
        assert ev["event_type"] == "tool_call_success"
        assert ev["tool_name"] == target_node.tool_name
        assert ev["result"] == spec.reference_node_values[target_node.node_id]
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
