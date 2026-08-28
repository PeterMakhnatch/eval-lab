"""Materializer for complete Harbor 0.21.0 workbench v2 compliant tasks."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from contract import CAMPAIGN_0_CELLS, CellFactors, make_benchmark_contract
from dag_generator import generate_dag_spec

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
DERIVED_HARBOR = REPO / "derived" / "harbor-tasks" / "mcp-funcdag"

PYTHON_BASE_IMAGE = "python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217"

# Darwin/macOS Docker cannot enforce no-network mode; Linux CI does
DEFAULT_NETWORK_MODE = "no-network" if sys.platform.startswith("linux") else "public"


def compute_source_digest(spec_dict: dict[str, Any]) -> str:
    canonical = json.dumps(spec_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def materialize_task(
    cell: dict[str, Any],
    output_root: Path | None = None,
    network_mode: str | None = None,
) -> Path:
    net_mode = network_mode or DEFAULT_NETWORK_MODE
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
    full_task_name = f"evallab/{task_id}"
    contract = make_benchmark_contract(factors, dag_spec, task_id)

    src_digest = compute_source_digest(asdict(contract))[:16]
    
    if output_root is None:
        target_dir = DERIVED_HARBOR / src_digest / task_id
    else:
        target_dir = output_root / src_digest / task_id

    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    environment = target_dir / "environment"
    solution = target_dir / "solution"
    tests = target_dir / "tests"
    workbench = target_dir / "workbench"
    adversarial = workbench / "adversarial"
    agent_workspace = target_dir / "agent_workspace"
    evidence_dir = target_dir / "evidence"

    environment.mkdir(parents=True)
    solution.mkdir(parents=True)
    tests.mkdir(parents=True)
    workbench.mkdir(parents=True)
    adversarial.mkdir(parents=True)
    agent_workspace.mkdir(parents=True)
    evidence_dir.mkdir(parents=True)

    # 1. Agent Visible Schema & Runtime Spec
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
    
    agent_graph_nodes = [
        {
            "node_id": n.node_id,
            "tool_name": n.tool_name,
            "input_bindings": n.input_bindings,
        }
        for n in dag_spec.nodes
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

    (environment / "runtime_tools.json").write_text(
        json.dumps(runtime_tools_spec, indent=2, sort_keys=True), encoding="utf-8"
    )

    # 2. Verifier-only Truth (Strictly in tests/ and isolated from agent workspace)
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

    # 3. Instruction with complete agent-visible graph topology
    inputs_desc = ", ".join(f"`{k}={v}`" for k, v in dag_spec.initial_inputs.items())
    nodes_desc_lines = []
    for n in agent_graph_nodes:
        bindings_str = ", ".join(f"{param}={src}" for param, src in n["input_bindings"].items())
        nodes_desc_lines.append(f"- Node `{n['node_id']}`: uses tool `{n['tool_name']}` with inputs ({bindings_str})")
    nodes_desc = "\n".join(nodes_desc_lines)

    instruction_content = f"""# MCP Tool Composition: Function DAG Evaluation

You are tasked with evaluating the target output value for a composite function DAG.

## Initial Inputs:
{inputs_desc}

## Dependency Graph Nodes:
{nodes_desc}

## Target Objective:
Evaluate the root target node `{dag_spec.target_node_id}` by discovering and calling the appropriate streamable-HTTP MCP tools in prerequisite topological order, correctly propagating intermediate values between dependent tools.

## MCP Tools Endpoint:
The local streamable-HTTP MCP tool server is available at: `http://mcp-server:8000/mcp` (transport: `streamable-http`).

## Deliverable:
Save the final calculated integer result to `/app/output/result.json` in format:
```json
{{
  "target_value": <INTEGER_VALUE>
}}
```
"""
    (target_dir / "instruction.md").write_text(instruction_content, encoding="utf-8")

    # 4. Environment Dockerfile and docker-compose.yaml with task-local named volume
    agent_dockerfile = f"""FROM {PYTHON_BASE_IMAGE}

WORKDIR /app
RUN mkdir -p /app/output /app/evidence
"""
    (environment / "Dockerfile").write_text(agent_dockerfile, encoding="utf-8")

    # Dedicated MCP Server Dockerfile under environment/mcp-server/
    mcp_server_dir = environment / "mcp-server"
    mcp_server_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "runtime.py", mcp_server_dir / "runtime.py")
    shutil.copy2(ROOT / "dag_generator.py", mcp_server_dir / "dag_generator.py")
    shutil.copy2(environment / "runtime_tools.json", mcp_server_dir / "runtime_tools.json")

    mcp_dockerfile = f"""FROM {PYTHON_BASE_IMAGE}

WORKDIR /app
COPY runtime.py /app/runtime.py
COPY dag_generator.py /app/dag_generator.py
COPY runtime_tools.json /app/runtime_tools.json
RUN mkdir -p /app/evidence
ENTRYPOINT ["python3", "/app/runtime.py", "--spec", "/app/runtime_tools.json", "--evidence-dir", "/app/evidence", "--port", "8000", "--host", "0.0.0.0"]
"""
    (mcp_server_dir / "Dockerfile").write_text(mcp_dockerfile, encoding="utf-8")

    # docker-compose.yaml with task-local named volume shared between main and sidecar
    compose_yaml = """services:
  main:
    build: .
    volumes:
      - mcp_evidence:/app/evidence:ro
  mcp-server:
    build: ./mcp-server
    volumes:
      - mcp_evidence:/app/evidence:rw

volumes:
  mcp_evidence:
"""
    (environment / "docker-compose.yaml").write_text(compose_yaml, encoding="utf-8")

    # 5. Tests Dockerfile, verifier_eval.py, and test.sh
    tests_dockerfile = f"""FROM {PYTHON_BASE_IMAGE}

WORKDIR /tests
COPY verifier_truth.json /tests/verifier_truth.json
COPY verifier_eval.py /tests/verifier_eval.py
COPY test.sh /tests/test.sh
RUN chmod +x /tests/test.sh
CMD ["sleep", "infinity"]
"""
    (tests / "Dockerfile").write_text(tests_dockerfile, encoding="utf-8")

    verifier_eval_content = """import json
import os
import sys
from pathlib import Path

def main():
    truth_file = Path("/tests/verifier_truth.json")
    if not truth_file.exists():
        truth_file = Path("tests/verifier_truth.json")
    
    truth = json.loads(truth_file.read_text(encoding="utf-8"))
    exp_val = truth["expected_target_value"]
    topological_order = truth["topological_order"]
    ref_node_values = truth["reference_node_values"]
    node_tool_map = truth["node_tool_map"]
    required_tools = [node_tool_map[nid] for nid in topological_order]

    # Result resolution from /app/output/result.json or collected fallback
    res_file = Path("/app/output/result.json")
    if not res_file.exists():
        res_file = Path("/app/result.json")
        if not res_file.exists():
            res_file = Path("agent_workspace/result.json")
            if not res_file.exists():
                res_file = Path("result.json")

    agent_val = None
    if res_file.exists():
        try:
            data = json.loads(res_file.read_text(encoding="utf-8"))
            agent_val = data.get("target_value")
        except Exception:
            pass

    # Events resolution from collected /app/output/benchmark-events.jsonl or /app/evidence
    events_file = Path("/app/output/benchmark-events.jsonl")
    if not events_file.exists():
        events_file = Path("/app/evidence/benchmark-events.jsonl")
        if not events_file.exists():
            events_file = Path("evidence/benchmark-events.jsonl")

    events = []
    if events_file.exists():
        for line in events_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass

    successful_calls = [e for e in events if e.get("event_type") == "tool_call_success"]
    executed_tools = [e["tool_name"] for e in successful_calls]

    # Check topological DAG ordering
    tool_idx = 0
    dag_conformance = True
    for req_tool in required_tools:
        try:
            pos = executed_tools.index(req_tool, tool_idx)
            tool_idx = pos + 1
        except ValueError:
            dag_conformance = False
            break

    # Check intermediate values strictly one-to-one without duplicate consumption
    consumed_indices = set()
    valid_intermediates = 0
    for node_id in topological_order:
        tname = node_tool_map[node_id]
        eval_v = ref_node_values[node_id]
        matched = False
        for idx, c in enumerate(successful_calls):
            if idx not in consumed_indices and c["tool_name"] == tname and c.get("result") == eval_v:
                consumed_indices.add(idx)
                matched = True
                break
        if matched:
            valid_intermediates += 1

    val_prop_acc = (valid_intermediates / len(topological_order)) if topological_order else 0.0

    # Verification requires matching target value, DAG conformance, and full value propagation
    reward = 0.0
    if agent_val == exp_val and dag_conformance and val_prop_acc == 1.0:
        reward = 1.0

    print(f"Verification complete: reward={reward}, agent_val={agent_val}, exp_val={exp_val}, dag_conf={dag_conformance}, val_prop={val_prop_acc}")
    
    logs_dir = Path("/logs/verifier")
    if not logs_dir.exists():
        logs_dir = Path("tests/rewards")
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "reward.txt").write_text(f"{reward}\\n", encoding="utf-8")

    if reward == 1.0:
        sys.exit(0)
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
"""
    (tests / "verifier_eval.py").write_text(verifier_eval_content, encoding="utf-8")

    test_sh_content = """#!/bin/bash
set -euo pipefail
python3 /tests/verifier_eval.py
"""
    (tests / "test.sh").write_text(test_sh_content, encoding="utf-8")
    (tests / "test.sh").chmod(0o755)

    # 6. task.toml (Harbor 0.21.0 & Task Workbench v2 schema compliant with /app/output collect hooks)
    task_toml_content = f"""schema_version = "1.4"
artifacts = [
  "/app/output/result.json",
  "/app/output/benchmark-events.jsonl",
  "/app/output/final-state.json",
]

[task]
name = "{full_task_name}"
version = "1.0.0"
description = "MCP function DAG composition benchmark cell {cell_name}"
keywords = ["mcp", "tool-composition", "function-dag", "calibration"]
authors = [
  {{ name = "Eval Lab Synthetic Authors", email = "synthetic@eval-lab.local" }}
]

[metadata]
difficulty = "medium"
category = "tool-use"
tags = ["mcp", "dag", "calibration"]

[agent]
timeout_sec = 180.0

[verifier]
timeout_sec = 60.0
environment_mode = "separate"

[[verifier.collect]]
service = "main"
command = "if [ -f /app/output/result.json ]; then cp /app/output/result.json /app/output/result.json; fi"

[[verifier.collect]]
service = "main"
command = "if [ -f /app/evidence/benchmark-events.jsonl ]; then cp /app/evidence/benchmark-events.jsonl /app/output/benchmark-events.jsonl; fi"

[[verifier.collect]]
service = "main"
command = "if [ -f /app/evidence/final-state.json ]; then cp /app/evidence/final-state.json /app/output/final-state.json; fi"

[verifier.environment]
network_mode = "no-network"

[environment]
network_mode = "{net_mode}"
build_timeout_sec = 120.0
cpus = 1
memory_mb = 512
storage_mb = 1024

[[environment.mcp_servers]]
name = "funcdag-server"
transport = "streamable-http"
url = "http://mcp-server:8000/mcp"
"""
    (target_dir / "task.toml").write_text(task_toml_content, encoding="utf-8")

    # 7. Solution solver: solve.sh and solve.py
    solve_sh_content = """#!/bin/bash
set -euo pipefail
if [ -f /solution/solve.py ]; then
    python3 /solution/solve.py
else
    python3 /app/solve.py
fi
"""
    (solution / "solve.sh").write_text(solve_sh_content, encoding="utf-8")
    (solution / "solve.sh").chmod(0o755)

    solve_py_content = f"""#!/usr/bin/env python3
import json
import http.client
import time
from pathlib import Path

# Solve DAG topologically using HTTP JSON-RPC MCP calls
initial_inputs = {json.dumps(dag_spec.initial_inputs)}
nodes = {json.dumps([{"node_id": n.node_id, "tool_name": n.tool_name, "bindings": n.input_bindings} for n in dag_spec.nodes])}
topological_order = {json.dumps(dag_spec.topological_order)}
target_node_id = "{dag_spec.target_node_id}"

# Wait for MCP server to accept requests
for _ in range(30):
    try:
        conn = http.client.HTTPConnection("mcp-server", 8000, timeout=2)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        if resp.status == 200:
            conn.close()
            break
        conn.close()
    except Exception:
        time.sleep(0.5)

def call_tool(tool_name, args):
    payload = json.dumps({{
        "jsonrpc": "2.0",
        "id": "oracle_call",
        "method": "tools/call",
        "params": {{
            "name": tool_name,
            "arguments": args
        }}
    }})
    conn = http.client.HTTPConnection("mcp-server", 8000, timeout=10)
    conn.request("POST", "/mcp", body=payload, headers={{"Content-Type": "application/json"}})
    res = conn.getresponse()
    body = res.read().decode("utf-8")
    data = json.loads(body)
    conn.close()
    if "error" in data:
        raise RuntimeError(f"Tool call failed: {{data['error']}}")
    return data["result"]["value"]

node_map = {{n["node_id"]: n for n in nodes}}
node_values = dict(initial_inputs)

for nid in topological_order:
    node = node_map[nid]
    tool_args = {{param: node_values[src] for param, src in node["bindings"].items()}}
    val = call_tool(node["tool_name"], tool_args)
    node_values[nid] = val

target_val = node_values[target_node_id]

# Standard model output destination
out_dir = Path("/app/output")
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "result.json").write_text(json.dumps({{"target_value": target_val}}, indent=2) + "\\n", encoding="utf-8")
print(f"Oracle solved with target_value: {{target_val}}")
"""
    (solution / "solve.py").write_text(solve_py_content, encoding="utf-8")
    (solution / "solve.py").chmod(0o755)

    # 8. Workbench Controls: fair-alternative.sh, please-hack.sh, and 3 adversarial probes
    fair_alt_content = f"""#!/bin/bash
set -euo pipefail

python3 - <<'PYEOF'
import json
import http.client
from pathlib import Path

init_inputs = {json.dumps(dag_spec.initial_inputs)}
nodes_spec = {json.dumps([{"node_id": n.node_id, "tool_name": n.tool_name, "bindings": n.input_bindings} for n in dag_spec.nodes])}
topo_order = {json.dumps(dag_spec.topological_order)}
target_id = "{dag_spec.target_node_id}"

def execute_remote_tool(name, tool_args):
    payload = json.dumps({{
        "jsonrpc": "2.0",
        "id": "fair_call",
        "method": "tools/call",
        "params": {{"name": name, "arguments": tool_args}}
    }})
    conn = http.client.HTTPConnection("mcp-server", 8000, timeout=10)
    conn.request("POST", "/mcp", body=payload, headers={{"Content-Type": "application/json"}})
    res = conn.getresponse()
    body = res.read().decode("utf-8")
    data = json.loads(body)
    conn.close()
    return data["result"]["value"]

vals = dict(init_inputs)
node_lut = {{n["node_id"]: n for n in nodes_spec}}
for nid in topo_order:
    n = node_lut[nid]
    args = {{k: vals[src] for k, src in n["bindings"].items()}}
    vals[nid] = execute_remote_tool(n["tool_name"], args)

final_val = vals[target_id]
out_p = Path("/app/output/result.json")
out_p.parent.mkdir(parents=True, exist_ok=True)
out_p.write_text(json.dumps({{"target_value": final_val}}, indent=2) + "\\n", encoding="utf-8")
print(f"Fair alternative completed: target_value={{final_val}}")
PYEOF
"""
    (workbench / "fair-alternative.sh").write_text(fair_alt_content, encoding="utf-8")
    (workbench / "fair-alternative.sh").chmod(0o755)

    please_hack_content = """#!/bin/bash
set -euo pipefail
mkdir -p /app/output
echo '{"target_value": -1}' > /app/output/result.json
"""
    (workbench / "please-hack.sh").write_text(please_hack_content, encoding="utf-8")
    (workbench / "please-hack.sh").chmod(0o755)

    first_distractor = next((t.name for t in dag_spec.tools if t.is_distractor), "dag_tool_add_integers")
    
    probe1_content = """#!/bin/bash
set -euo pipefail
mkdir -p /app/output
"""
    (adversarial / "probe-nop.sh").write_text(probe1_content, encoding="utf-8")
    (adversarial / "probe-nop.sh").chmod(0o755)

    probe2_content = """#!/bin/bash
set -euo pipefail
mkdir -p /app/output
echo '{"target_value": 99999999}' > /app/output/result.json
"""
    (adversarial / "probe-corrupt-value.sh").write_text(probe2_content, encoding="utf-8")
    (adversarial / "probe-corrupt-value.sh").chmod(0o755)

    probe3_content = f"""#!/bin/bash
set -euo pipefail
python3 - <<'PYEOF'
import json, http.client
from pathlib import Path

payload = json.dumps({{
    "jsonrpc": "2.0",
    "id": "probe3",
    "method": "tools/call",
    "params": {{"name": "{first_distractor}", "arguments": {{"input_payload": "noise", "flag": True}}}}
}})
try:
    conn = http.client.HTTPConnection("mcp-server", 8000, timeout=2)
    conn.request("POST", "/mcp", body=payload, headers={{"Content-Type": "application/json"}})
    conn.getresponse()
    conn.close()
except Exception:
    pass

out_p = Path("/app/output/result.json")
out_p.parent.mkdir(parents=True, exist_ok=True)
out_p.write_text(json.dumps({{"target_value": 0}}) + "\\n")
PYEOF
"""
    (adversarial / "probe-distractor-only.sh").write_text(probe3_content, encoding="utf-8")
    (adversarial / "probe-distractor-only.sh").chmod(0o755)

    # Write contract to evidence and task root
    (evidence_dir / "benchmark_contract.json").write_text(contract.to_json(), encoding="utf-8")
    (target_dir / "benchmark_contract.json").write_text(contract.to_json(), encoding="utf-8")

    return target_dir
