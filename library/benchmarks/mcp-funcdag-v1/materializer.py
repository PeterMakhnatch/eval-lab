"""Materialize Harbor tasks using the shared FastMCP substrate."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from evallab.mcp_substrate import (
    DEFAULT_INTERNAL_NETWORK_NAME,
    DEFAULT_MCP_PORT,
    DEFAULT_PINNED_BASE_IMAGE,
    DEFAULT_SIDECAR_SERVICE,
    DEFAULT_VOLUME_MOUNT,
    DEFAULT_VOLUME_NAME,
    MCPToolDefinition,
    MCPToolParameter,
    ResolverProvenance,
    WheelhouseTarget,
    materialize_mcp_sidecar_package,
    render_mcp_compose_document,
    validate_mcp_compose_document,
)

from contract import CellFactors, make_benchmark_contract
from dag_generator import DAGSpec, ToolSpec, generate_dag_spec

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
DERIVED_HARBOR = REPO / "derived" / "harbor-tasks" / "mcp-funcdag"
DEFAULT_WHEELHOUSE = Path(os.environ.get("FASTMCP_WHEELHOUSE", "/tmp/fastmcp3_wheelhouse"))

OP_BODIES: dict[str, str] = {
    "add_integers": "val = int(x) + int(y)",
    "multiply_integers": "val = int(x) * int(y)",
    "subtract_integers": "val = int(x) - int(y)",
    "scale_factor": "val = int(base) * int(factor) + 3",
    "combine_metrics": "val = (int(a) * 2) + int(b) + 5",
    "transform_signal": "val = (int(val) + int(offset)) * 2",
    "merge_checksums": "val = (int(u) ^ int(v)) + (int(u) & int(v)) + 7",
}


def compute_source_digest(spec_dict: dict[str, Any]) -> str:
    canonical = json.dumps(spec_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _execution_body(tool: ToolSpec) -> str:
    if tool.is_distractor:
        return ""
    op_line = OP_BODIES.get(tool.op_kind, "val = None")
    return (
        f"{op_line}\n"
        'res = {"status": "ok", "value": val}\n'
        f'log_tool_event("{tool.name}", args, res, is_distractor=False)\n'
        "return res"
    )


def tools_to_mcp_definitions(spec: DAGSpec) -> list[MCPToolDefinition]:
    defs: list[MCPToolDefinition] = []
    for tool in spec.tools:
        params = tuple(
            MCPToolParameter(
                name=p.name,
                type_name="int" if p.type_name == "integer" else p.type_name,
                description=p.description,
                required=p.required,
            )
            for p in tool.parameters
        )
        defs.append(
            MCPToolDefinition(
                name=tool.name,
                description=tool.description,
                parameters=params,
                output_type=tool.output_type,
                is_distractor=tool.is_distractor,
                metadata={"op_kind": tool.op_kind},
                execution_body=_execution_body(tool) or None,
            )
        )
    return defs


def _write_compose(environment: Path, sidecar_rel: str = "./mcp-server") -> dict[str, Any]:
    compose = render_mcp_compose_document(
        sidecar_service=DEFAULT_SIDECAR_SERVICE,
        volume_name=DEFAULT_VOLUME_NAME,
        volume_mount=DEFAULT_VOLUME_MOUNT,
        sidecar_build_context=sidecar_rel,
        network_name=DEFAULT_INTERNAL_NETWORK_NAME,
    )
    compose["services"]["main"] = {
        "build": ".",
        "networks": [DEFAULT_INTERNAL_NETWORK_NAME],
        "volumes": [f"{DEFAULT_VOLUME_NAME}:{DEFAULT_VOLUME_MOUNT}:ro"],
    }
    valid, errors = validate_mcp_compose_document(compose)
    if not valid:
        raise RuntimeError(f"generated compose failed substrate validation: {errors}")
    (environment / "docker-compose.yaml").write_text(
        yaml.safe_dump(compose, sort_keys=False), encoding="utf-8"
    )
    return compose



def _write_workbench_build_proof(environment: Path, sidecar_dir: Path) -> None:
    """Emit the workbench v2 offline_build_proof covering sidecar wheels."""
    reqs = sidecar_dir / "requirements.txt"
    lock_digest = "sha256:" + hashlib.sha256(reqs.read_bytes()).hexdigest()
    pinned = []
    wheelhouse = sidecar_dir / "wheelhouse"
    for wheel in sorted(wheelhouse.glob("*.whl")):
        stem = wheel.name[:-4]
        parts = stem.split("-")
        name = parts[0].replace("_", "-")
        version = parts[1] if len(parts) > 1 else "0"
        pinned.append(
            {
                "name": name,
                "version": version,
                "wheel": wheel.name,
            }
        )
    proof = {
        "schema_version": "1.0",
        "kind": "offline_build_proof",
        "ecosystem": "python",
        "lockfile": "mcp-server/requirements.txt",
        "lockfile_digest": lock_digest,
        "pinned_dependencies": pinned,
        "reviewed_by": "eval-lab-mcp-funcdag",
    }
    (environment / "offline-build-proof.json").write_text(
        json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def materialize_task(
    cell: dict[str, Any],
    output_root: Path | None = None,
    network_mode: str | None = None,
    wheelhouse: Path | None = None,
) -> Path:
    net_mode = network_mode or (
        "no-network" if sys.platform.startswith("linux") else "public"
    )
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
    cell_name = cell.get(
        "name", f"cell_d{factors.depth}_w{factors.width}_dist{factors.distractor_count}"
    )
    task_id = f"mcp-funcdag-{cell_name}-seed{factors.seed}"
    full_task_name = f"evallab/{task_id}"
    contract = make_benchmark_contract(factors, dag_spec, task_id)
    src_digest = compute_source_digest(asdict(contract))[:16]
    target_dir = (output_root or DERIVED_HARBOR) / src_digest / task_id
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    environment = target_dir / "environment"
    solution = target_dir / "solution"
    tests = target_dir / "tests"
    workbench = target_dir / "workbench"
    adversarial = workbench / "adversarial"
    for path in (environment, solution, tests, workbench, adversarial):
        path.mkdir(parents=True, exist_ok=True)

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
    (environment / "runtime_tools.json").write_text(
        json.dumps(runtime_tools_spec, indent=2, sort_keys=True), encoding="utf-8"
    )

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

    node_lines = []
    for n in dag_spec.nodes:
        bindings = ", ".join(f"{param}={src}" for param, src in n.input_bindings.items())
        node_lines.append(
            f"- Node `{n.node_id}`: uses tool `{n.tool_name}` with inputs ({bindings})"
        )
    inputs_desc = ", ".join(f"`{k}={v}`" for k, v in dag_spec.initial_inputs.items())
    (target_dir / "instruction.md").write_text(
        f"""# MCP Tool Composition: Function DAG Evaluation

You are tasked with evaluating the target output value for a composite function DAG.

## Initial Inputs:
{inputs_desc}

## Dependency Graph Nodes:
{chr(10).join(node_lines)}

## Target Objective:
Evaluate the root target node `{dag_spec.target_node_id}` by discovering and calling the appropriate streamable-HTTP MCP tools in prerequisite topological order, correctly propagating intermediate values between dependent tools.

## MCP Tools Endpoint:
The local FastMCP streamable-HTTP server is available at: `http://{DEFAULT_SIDECAR_SERVICE}:{DEFAULT_MCP_PORT}/mcp`.

## Deliverable:
Save the final calculated integer result to `/app/result.json` in format:
```json
{{
  "target_value": <INTEGER_VALUE>
}}
```
""",
        encoding="utf-8",
    )

    (environment / "Dockerfile").write_text(
        f"FROM {DEFAULT_PINNED_BASE_IMAGE}\n\nWORKDIR /app\nRUN mkdir -p /app/output\n",
        encoding="utf-8",
    )

    sidecar_dir = environment / "mcp-server"
    wheel_src = wheelhouse or DEFAULT_WHEELHOUSE
    provenance_path = wheel_src / "resolver-provenance.json"
    plan_only = not (wheel_src.is_dir() and provenance_path.is_file())
    resolver_provenance = (
        ResolverProvenance.from_json(json.loads(provenance_path.read_text(encoding="utf-8")))
        if not plan_only
        else None
    )
    materialize_mcp_sidecar_package(
        target_dir=sidecar_dir,
        tools=tools_to_mcp_definitions(dag_spec),
        server_name="mcp-funcdag-sidecar",
        port=DEFAULT_MCP_PORT,
        wheelhouse_source=None if plan_only else wheel_src,
        target=None if resolver_provenance is None else resolver_provenance.target,
        resolver_provenance=resolver_provenance,
        plan_only=plan_only,
        internal_network_name=DEFAULT_INTERNAL_NETWORK_NAME,
    )
    if plan_only:
        (sidecar_dir / "Dockerfile").write_text(
            f"FROM {DEFAULT_PINNED_BASE_IMAGE}\nWORKDIR /app\n"
            "COPY server.py /app/server.py\n"
            "RUN mkdir -p /app/output\n"
            'CMD ["python", "/app/server.py"]\n',
            encoding="utf-8",
        )
    if not plan_only:
        (environment / "Dockerfile").write_text(
            f"FROM {DEFAULT_PINNED_BASE_IMAGE}\n\nWORKDIR /app\n"
            "COPY mcp-server/requirements.txt /requirements.txt\n"
            "COPY mcp-server/wheelhouse /wheelhouse\n"
            "RUN python -m pip install --no-cache-dir --no-index --find-links=/wheelhouse --require-hashes -r /requirements.txt\n"
            "RUN mkdir -p /app/output\n",
            encoding="utf-8",
        )
        _write_workbench_build_proof(environment, sidecar_dir)
    _write_compose(environment)

    (tests / "Dockerfile").write_text(
        f"FROM {DEFAULT_PINNED_BASE_IMAGE}\n\nWORKDIR /tests\n"
        "COPY verifier_truth.json /tests/verifier_truth.json\n"
        "COPY verifier_eval.py /tests/verifier_eval.py\n"
        "COPY test.sh /tests/test.sh\n"
        "RUN chmod +x /tests/test.sh\n"
        'CMD ["sleep", "infinity"]\n',
        encoding="utf-8",
    )
    (tests / "verifier_eval.py").write_text(_VERIFIER_EVAL, encoding="utf-8")
    (tests / "test.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\npython3 /tests/verifier_eval.py\n",
        encoding="utf-8",
    )
    (tests / "test.sh").chmod(0o755)

    (target_dir / "task.toml").write_text(
        f"""schema_version = "1.4"
artifacts = [
  "/app/result.json",
  "/app/output/benchmark-events.jsonl",
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

[verifier.environment]
network_mode = "no-network"

[environment]
network_mode = "{net_mode}"
build_timeout_sec = 300.0
cpus = 1
memory_mb = 512
storage_mb = 2048

[[environment.mcp_servers]]
name = "{DEFAULT_SIDECAR_SERVICE}"
transport = "streamable-http"
url = "http://{DEFAULT_SIDECAR_SERVICE}:{DEFAULT_MCP_PORT}/mcp"
""",
        encoding="utf-8",
    )

    (solution / "solve.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\npython3 /solution/solve.py\n",
        encoding="utf-8",
    )
    (solution / "solve.sh").chmod(0o755)
    (solution / "solve.py").write_text(_oracle_solve_py(dag_spec), encoding="utf-8")
    (solution / "solve.py").chmod(0o755)

    (workbench / "fair-alternative.sh").write_text(_oracle_solve_sh_wrap(dag_spec), encoding="utf-8")
    (workbench / "fair-alternative.sh").chmod(0o755)
    (workbench / "please-hack.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\nmkdir -p /app/output\necho '{\"target_value\": -1}' > /app/output/result.json\n",
        encoding="utf-8",
    )
    (workbench / "please-hack.sh").chmod(0o755)
    (adversarial / "probe-nop.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\nmkdir -p /app/output\n", encoding="utf-8"
    )
    (adversarial / "probe-nop.sh").chmod(0o755)
    (adversarial / "probe-corrupt-value.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\nmkdir -p /app/output\necho '{\"target_value\": 99999999}' > /app/output/result.json\n",
        encoding="utf-8",
    )
    (adversarial / "probe-corrupt-value.sh").chmod(0o755)
    first_distractor = next((t.name for t in dag_spec.tools if t.is_distractor), dag_spec.tools[0].name)
    (adversarial / "probe-distractor-only.sh").write_text(
        _distractor_probe(first_distractor), encoding="utf-8"
    )
    (adversarial / "probe-distractor-only.sh").chmod(0o755)

    (target_dir / "benchmark_contract.json").write_text(contract.to_json(), encoding="utf-8")
    return target_dir


_VERIFIER_EVAL = r'''import json
import sys
from pathlib import Path


def _result_value(raw):
    if isinstance(raw, dict):
        return raw.get("value", raw)
    return raw


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

    res_file = Path("/app/result.json")
    if not res_file.exists():
        res_file = Path("/app/output/result.json")
    agent_val = None
    if res_file.exists():
        try:
            agent_val = json.loads(res_file.read_text(encoding="utf-8")).get("target_value")
        except Exception:
            agent_val = None

    events_file = Path("/app/output/benchmark-events.jsonl")
    events = []
    if events_file.exists():
        for line in events_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))

    successful_calls = [e for e in events if e.get("event_type") == "tool_call_success"]
    executed_tools = [e["tool_name"] for e in successful_calls]
    tool_idx = 0
    dag_conformance = True
    for req_tool in required_tools:
        try:
            pos = executed_tools.index(req_tool, tool_idx)
            tool_idx = pos + 1
        except ValueError:
            dag_conformance = False
            break

    consumed = set()
    valid = 0
    for node_id in topological_order:
        tname = node_tool_map[node_id]
        expected = ref_node_values[node_id]
        matched = False
        for idx, call in enumerate(successful_calls):
            if idx in consumed:
                continue
            if call.get("tool_name") == tname and _result_value(call.get("result")) == expected:
                consumed.add(idx)
                matched = True
                break
        if matched:
            valid += 1
    val_prop = (valid / len(topological_order)) if topological_order else 0.0
    reward = 1.0 if agent_val == exp_val and dag_conformance and val_prop == 1.0 else 0.0
    print(
        f"Verification complete: reward={reward}, agent_val={agent_val}, "
        f"exp_val={exp_val}, dag_conf={dag_conformance}, val_prop={val_prop}"
    )
    logs_dir = Path("/logs/verifier")
    if not logs_dir.exists():
        logs_dir = Path("tests/rewards")
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "reward.txt").write_text(f"{reward}\n", encoding="utf-8")
    sys.exit(0 if reward == 1.0 else 1)


if __name__ == "__main__":
    main()
'''


def _oracle_solve_py(dag_spec: DAGSpec) -> str:
    return f'''#!/usr/bin/env python3
import asyncio
import json
from pathlib import Path

from fastmcp import Client

initial_inputs = {json.dumps(dag_spec.initial_inputs)}
nodes = {json.dumps([{"node_id": n.node_id, "tool_name": n.tool_name, "bindings": n.input_bindings} for n in dag_spec.nodes])}
topological_order = {json.dumps(dag_spec.topological_order)}
target_node_id = {json.dumps(dag_spec.target_node_id)}
PORT = {DEFAULT_MCP_PORT}
MCP_HOST = "mcp-service"
MCP_ENDPOINT = "http" + "://" + MCP_HOST + ":" + str(PORT) + "/mcp"


async def solve() -> None:
    node_map = {{n["node_id"]: n for n in nodes}}
    node_values = dict(initial_inputs)
    async with Client(MCP_ENDPOINT) as client:
        for nid in topological_order:
            node = node_map[nid]
            tool_args = {{param: node_values[src] for param, src in node["bindings"].items()}}
            call_result = await client.call_tool(node["tool_name"], tool_args)
            value = call_result.data
            if not isinstance(value, dict) or "value" not in value:
                raise RuntimeError(f"Tool {{node['tool_name']}} returned no integer value: {{value!r}}")
            node_values[nid] = value["value"]

    target_val = node_values[target_node_id]
    result_path = Path("/app/result.json")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({{"target_value": target_val}}, indent=2) + "\\n", encoding="utf-8"
    )
    print(f"Oracle solved with target_value: {{target_val}}")


asyncio.run(solve())
'''


def _oracle_solve_sh_wrap(dag_spec: DAGSpec) -> str:
    py = _oracle_solve_py(dag_spec)
    return "#!/bin/bash\nset -euo pipefail\npython3 - <<'PYEOF'\n" + py.split("#!/usr/bin/env python3\n", 1)[-1] + "\nPYEOF\n"


def _distractor_probe(tool_name: str) -> str:
    return f"""#!/bin/bash
set -euo pipefail
python3 - <<'PYEOF'
import json, http.client
from pathlib import Path
payload = json.dumps({{
    "jsonrpc": "2.0", "id": "probe3", "method": "tools/call",
    "params": {{"name": "{tool_name}", "arguments": {{"input_payload": "noise", "flag": True}}}},
}})
for host in ("mcp-service", "mcp-server", "127.0.0.1"):
    try:
        conn = http.client.HTTPConnection(host, {DEFAULT_MCP_PORT}, timeout=2)
        conn.request("POST", "/mcp", body=payload, headers={{"Content-Type": "application/json"}})
        conn.getresponse().read()
        conn.close()
        break
    except Exception:
        pass
out = Path("/app/output/result.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({{"target_value": 0}}) + "\\n")
PYEOF
"""
