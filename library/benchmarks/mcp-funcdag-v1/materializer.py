"""Materializer for complete Harbor tasks with streamable-HTTP MCP sidecars."""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from contract import CAMPAIGN_0_CELLS, CellFactors, make_benchmark_contract
from dag_generator import generate_dag_spec

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
DERIVED_HARBOR = REPO / "derived" / "harbor-tasks" / "mcp-funcdag"


def compute_source_digest(spec_dict: dict[str, Any]) -> str:
    canonical = json.dumps(spec_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def materialize_task(
    cell: dict[str, Any],
    output_root: Path | None = None,
) -> Path:
    factors = CellFactors(
        depth=cell["depth"],
        width=cell["width"],
        distractor_count=cell["distractor_count"],
        name_similarity=cell["name_similarity"],
        schema_token_volume=cell["schema_token_volume"],
        schema_drift=cell["schema_drift"],
        seed=cell["seed"],
    )
    dag_spec = generate_dag_spec(
        seed=factors.seed,
        depth=factors.depth,
        width=factors.width,
        distractor_count=factors.distractor_count,
        name_similarity=factors.name_similarity,
        schema_token_volume=factors.schema_token_volume,
        schema_drift=factors.schema_drift,
    )

    cell_name = cell.get("name", f"cell_d{factors.depth}_w{factors.width}_dist{factors.distractor_count}")
    task_id = f"mcp-funcdag-{cell_name}-seed{factors.seed}"
    contract = make_benchmark_contract(factors, dag_spec, task_id)

    # Compute source digest from contract & spec
    src_digest = compute_source_digest(asdict(contract))[:16]
    
    if output_root is None:
        target_dir = DERIVED_HARBOR / src_digest / task_id
    else:
        target_dir = output_root / src_digest / task_id

    # Create clean directory tree
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    environment = target_dir / "environment"
    solution = target_dir / "solution"
    tests = target_dir / "tests"
    agent_workspace = target_dir / "agent_workspace"
    evidence_dir = target_dir / "evidence"

    environment.mkdir(parents=True)
    solution.mkdir(parents=True)
    tests.mkdir(parents=True)
    agent_workspace.mkdir(parents=True)
    evidence_dir.mkdir(parents=True)

    # 1. Agent Visible Schema & Initial Inputs
    # Note: Agent schemas contain NO DAG solution or intermediate node values!
    agent_tools = [
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
        for t in dag_spec.tools
    ]
    
    runtime_tools_spec = {
        "tools": agent_tools,
        "nodes": [
            {
                "node_id": n.node_id,
                "tool_name": n.tool_name,
                "op_name": n.op_name,
                "input_bindings": n.input_bindings,
            }
            for n in dag_spec.nodes
        ],
        "initial_inputs": dag_spec.initial_inputs,
        "target_node_id": dag_spec.target_node_id,
        "topological_order": dag_spec.topological_order,
    }

    # Write runtime tool specification for the MCP sidecar
    (environment / "runtime_tools.json").write_text(
        json.dumps(runtime_tools_spec, indent=2, sort_keys=True), encoding="utf-8"
    )

    # 2. Verifier-only Truth (Placed strictly in tests/ and isolated from agent workspace)
    verifier_truth = {
        "target_node_id": dag_spec.target_node_id,
        "expected_target_value": dag_spec.expected_target_value,
        "topological_order": dag_spec.topological_order,
        "reference_node_values": dag_spec.reference_node_values,
        "initial_inputs": dag_spec.initial_inputs,
        "node_tool_map": {n.node_id: n.tool_name for n in dag_spec.nodes},
    }
    (tests / "verifier_truth.json").write_text(
        json.dumps(verifier_truth, indent=2, sort_keys=True), encoding="utf-8"
    )

    # 3. Instruction
    inputs_desc = ", ".join(f"`{k}={v}`" for k, v in dag_spec.initial_inputs.items())
    instruction_content = f"""# Tool Composition Task: Dependency Graph Evaluation

You are tasked with computing the target value for the composite function DAG.

## Initial Inputs:
{inputs_desc}

## Target Objective:
Evaluate the root target node `{dag_spec.target_node_id}` by discovering and executing the appropriate streamable-HTTP MCP tools in prerequisite topological order, correctly propagating intermediate values between dependent tools.

## MCP Tools Endpoint:
The local streamable-HTTP MCP tool server is mounted at: `http://mcp-server:8000/mcp` (or via standard MCP protocol).

## Deliverable:
Save the final calculated integer result to `/workspace/result.json` in format:
```json
{{
  "target_value": <INTEGER_VALUE>
}}
```
"""
    (target_dir / "instruction.md").write_text(instruction_content, encoding="utf-8")

    # 4. Copy shared modules to environment and tests
    shutil.copy2(ROOT / "runtime.py", environment / "runtime.py")
    shutil.copy2(ROOT / "dag_generator.py", environment / "dag_generator.py")
    shutil.copy2(ROOT / "verifier.py", tests / "verifier.py")

    # 5. Environment Dockerfile & Compose
    dockerfile_content = """FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY . /app
ENTRYPOINT ["python3", "/app/runtime.py", "--spec", "/app/runtime_tools.json", "--evidence-dir", "/app/evidence", "--port", "8000", "--host", "0.0.0.0"]
"""
    (environment / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")

    # Compose YAML for Harbor multi-container topology (main + mcp-server)
    compose_yaml_content = """version: '3.8'
services:
  main:
    image: python:3.12-slim
    command: sleep infinity
    volumes:
      - ./agent_workspace:/workspace
      - ./evidence:/app/evidence
    depends_on:
      - mcp-server
  mcp-server:
    build: ./environment
    ports:
      - "8000:8000"
    volumes:
      - ./evidence:/app/evidence
"""
    (target_dir / "docker-compose.yaml").write_text(compose_yaml_content, encoding="utf-8")

    # 6. Task.toml
    task_toml_content = f"""[task]
id = "{task_id}"
version = "1.0.0"
title = "MCP Function DAG Composition - {cell_name}"
description = "Deterministic streamable-HTTP MCP tool selection and DAG composition benchmark."
construct = "tool-composition"

[environment]
image = "python:3.12-slim"
mcp_servers = [
  {{ name = "funcdag_tools", transport = "streamable-http", url = "http://mcp-server:8000/mcp" }}
]

[verifier]
timeout_sec = 60
"""
    (target_dir / "task.toml").write_text(task_toml_content, encoding="utf-8")

    # 7. Solution & Tests Scripts
    solve_py_content = f"""import json
import urllib.request
from pathlib import Path

# Clean oracle solver
spec = json.loads(Path("/app/runtime_tools.json").read_text()) if Path("/app/runtime_tools.json").exists() else json.loads(Path("environment/runtime_tools.json").read_text())
# Execute topological order over local MCP or direct runtime
from runtime import MCPRuntime
runtime = MCPRuntime(spec, Path("evidence"))
nodes = {{n["node_id"]: n for n in spec["nodes"]}}
node_values = dict(spec["initial_inputs"])

for nid in spec["topological_order"]:
    n = nodes[nid]
    args = {{k: node_values[src] for k, src in n["input_bindings"].items()}}
    out = runtime.call_tool(n["tool_name"], args)
    node_values[nid] = out["result"]["value"]

target_val = node_values[spec["target_node_id"]]
Path("agent_workspace/result.json").write_text(json.dumps({{"target_value": target_val}}, indent=2))
print(f"Oracle solved with target_value: {{target_val}}")
"""
    (solution / "solve.py").write_text(solve_py_content, encoding="utf-8")

    test_py_content = """import json
import sys
from pathlib import Path
from verifier import verify_execution

task_dir = Path(".").resolve()
truth_path = task_dir / "tests" / "verifier_truth.json"
evidence_dir = task_dir / "evidence"
workspace_dir = task_dir / "agent_workspace"

res = verify_execution(task_dir, truth_path, evidence_dir, workspace_dir)
print("Verification result:", json.dumps(res, indent=2))

reward = res.get("reward", 0.0)
reward_dir = task_dir / "tests" / "rewards"
reward_dir.mkdir(parents=True, exist_ok=True)
(reward_dir / "reward.txt").write_text(str(reward) + "\\n")

if reward == 1.0:
    sys.exit(0)
else:
    sys.exit(1)
"""
    (tests / "test.py").write_text(test_py_content, encoding="utf-8")

    # Write contract to evidence and task root
    (evidence_dir / "benchmark_contract.json").write_text(contract.to_json(), encoding="utf-8")
    (target_dir / "benchmark_contract.json").write_text(contract.to_json(), encoding="utf-8")

    return target_dir
