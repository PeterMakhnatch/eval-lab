"""Materialize Harbor tasks using the shared FastMCP substrate."""
from __future__ import annotations

import hashlib
import json
import os
import random
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
    # Every tool executes a normal-shaped handler/result. Distractor identity is
    # verifier-truth only (kept in the DAG spec); the agent-facing tool surface
    # and events carry no distractor/noop/unused label.
    op_line = OP_BODIES.get(tool.op_kind, "val = None")
    return (
        f"{op_line}\n"
        'res = {"status": "ok", "value": val}\n'
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
                metadata={"op_kind": tool.op_kind},
                execution_body=_execution_body(tool),
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
        "node_expected_calls": dag_spec.node_expected_calls,
        "candidate_tool_names": sorted(t.name for t in dag_spec.tools),
    }
    (tests / "fixtures").mkdir(parents=True, exist_ok=True)
    (tests / "fixtures" / "verifier_truth.json").write_text(
        json.dumps(verifier_truth, indent=2, sort_keys=True), encoding="utf-8"
    )

    # Shuffled dependency lines: expose DAG structure and binding parameters without leaking tool mapping
    rng_inst = random.Random(factors.seed)
    shuffled_nodes = list(dag_spec.nodes)
    rng_inst.shuffle(shuffled_nodes)

    OP_HUMAN_DESCS = {
        "add_integers": "Addition transformation (adds x + y)",
        "multiply_integers": "Multiplication transformation (multiplies x * y)",
        "subtract_integers": "Subtraction transformation (subtracts x - y)",
        "scale_factor": "Affine scaling transformation (scales base by factor + 3)",
        "combine_metrics": "Linear metric combination ((a * 2) + b + 5)",
        "transform_signal": "Signal transformation ((val + offset) * 2)",
        "merge_checksums": "Checksum merge ((u ^ v) + (u & v) + 7)",
    }

    node_lines = []
    for n in shuffled_nodes:
        params_str = ", ".join(f"{param}=<{src}>" for param, src in n.input_bindings.items())
        op_desc = OP_HUMAN_DESCS.get(n.op_name, n.op_name)
        node_lines.append(f"- Step `{n.node_id}`: {op_desc} over inputs ({params_str})")

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
        f"FROM {DEFAULT_PINNED_BASE_IMAGE}\n\nWORKDIR /app\nRUN mkdir -p /app /app/output\n",
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
    _write_compose(environment)

    (tests / "Dockerfile").write_text(
        f"FROM {DEFAULT_PINNED_BASE_IMAGE}\n\nWORKDIR /tests\n"
        "COPY fixtures/verifier_truth.json /tests/fixtures/verifier_truth.json\n"
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

    (workbench / "fair-alternative.sh").write_text(_fair_alternative_sh_wrap(dag_spec), encoding="utf-8")
    (workbench / "fair-alternative.sh").chmod(0o755)
    (workbench / "please-hack.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\nmkdir -p /app/output\necho '{\"target_value\": -1}' > /app/result.json\n",
        encoding="utf-8",
    )
    (workbench / "please-hack.sh").chmod(0o755)
    (adversarial / "probe-nop.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\nmkdir -p /app/output\n", encoding="utf-8"
    )
    (adversarial / "probe-nop.sh").chmod(0o755)
    (adversarial / "probe-corrupt-value.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\nmkdir -p /app/output\necho '{\"target_value\": 99999999}' > /app/result.json\n",
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

SCHEMA_VERSION = "mcp" + "-tool-event-v1"
EVENT_TYPES = {"tool_call_success", "tool_call_error"}


def _result_value(raw):
    return raw.get("value") if isinstance(raw, dict) and "value" in raw else raw


def valid_event(event, allowed_tools):
    if not isinstance(event, dict):
        return False
    if event.get("schema_version") != SCHEMA_VERSION:
        return False
    if event.get("event_type") not in EVENT_TYPES:
        return False
    if not isinstance(event.get("event_ordinal"), int) or event["event_ordinal"] <= 0:
        return False
    if not isinstance(event.get("tool_name"), str) or event["tool_name"] not in allowed_tools:
        return False
    if not isinstance(event.get("arguments"), dict):
        return False
    if event["event_type"] == "tool_call_success":
        return event.get("is_error") is False and isinstance(event.get("result"), dict) and event.get("error") is None
    error = event.get("error")
    return event.get("is_error") is True and isinstance(error, dict) and isinstance(error.get("type"), str) and isinstance(error.get("message"), str) and event.get("result") is None


def main():
    truth_file = Path("/tests/fixtures/verifier_truth.json")
    if not truth_file.exists():
        truth_file = Path("tests/fixtures/verifier_truth.json")
    truth = json.loads(truth_file.read_text(encoding="utf-8"))
    exp_val = truth["expected_target_value"]
    topological_order = truth["topological_order"]
    ref_node_values = truth["reference_node_values"]
    node_tool_map = truth["node_tool_map"]
    node_expected_calls = truth["node_expected_calls"]
    allowed_tools = set(truth["candidate_tool_names"])

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
    events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()] if events_file.exists() else []
    schema_rate = sum(valid_event(event, allowed_tools) for event in events) / len(events) if events else 0.0
    contiguous = bool(events) and [event.get("event_ordinal") for event in events] == list(range(1, len(events) + 1))
    successes = [event for event in events if event.get("event_type") == "tool_call_success"]

    dag_ok = len(successes) == len(topological_order)
    matched = 0
    if dag_ok:
        for node_id, event in zip(topological_order, successes, strict=True):
            expected_tool = node_tool_map[node_id]
            expected_args = node_expected_calls[node_id]["expected_args"]
            expected_val = ref_node_values[node_id]
            if (
                event["tool_name"] != expected_tool
                or event["arguments"] != expected_args
                or _result_value(event["result"]) != expected_val
            ):
                dag_ok = False
                break
            matched += 1

    val_prop = matched / len(topological_order)
    reward = 1.0 if agent_val == exp_val and dag_ok and val_prop == 1.0 and schema_rate == 1.0 and contiguous else 0.0
    print(f"Verification complete: reward={reward}, agent_val={agent_val}, exp_val={exp_val}, dag_conf={dag_ok}, val_prop={val_prop}")
    logs_dir = Path("/logs/verifier")
    if not logs_dir.exists():
        logs_dir = Path("tests/rewards")
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "reward.txt").write_text(f"{reward}\\n", encoding="utf-8")
    sys.exit(0 if reward == 1.0 else 1)


if __name__ == "__main__":
    main()
'''


def _oracle_solve_py(dag_spec: DAGSpec) -> str:
    client_src = (ROOT / "client.py").read_text(encoding="utf-8")
    header = (
        "#!/usr/bin/env python3\n"
        "import json\n"
        "from pathlib import Path\n\n"
        f"{client_src}\n\n"
        f"initial_inputs = {json.dumps(dag_spec.initial_inputs)}\n"
        f"nodes = {json.dumps([{'node_id': n.node_id, 'tool_name': n.tool_name, 'bindings': n.input_bindings} for n in dag_spec.nodes])}\n"
        f"topological_order = {json.dumps(dag_spec.topological_order)}\n"
        f"target_node_id = {json.dumps(dag_spec.target_node_id)}\n"
        f"PORT = {DEFAULT_MCP_PORT}\n"
        'MCP_HOST = "mcp-service"\n\n'
    )
    body = '''def solve() -> None:
    session = McpHttpSession(host=MCP_HOST, port=PORT)
    status, raw = session.initialize()
    if status != 200:
        raise RuntimeError(f"Failed to initialize MCP session: {raw}")

    node_map = {n["node_id"]: n for n in nodes}
    node_values = dict(initial_inputs)
    for nid in topological_order:
        node = node_map[nid]
        tool_args = {param: node_values[src] for param, src in node["bindings"].items()}
        res = session.call_tool(node["tool_name"], tool_args)
        val = res.get("value") if isinstance(res, dict) else res
        if val is None or not isinstance(val, int):
            raise RuntimeError(f"Tool {node['tool_name']} returned no integer value: {res!r}")
        node_values[nid] = val

    target_val = node_values[target_node_id]
    result_path = Path("/app/result.json")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({"target_value": target_val}, indent=2) + "\\n", encoding="utf-8"
    )
    print(f"Oracle solved with target_value: {target_val}")

solve()
'''
    return header + body


def _fair_alternative_solve_py(dag_spec: DAGSpec) -> str:
    client_src = (ROOT / "client.py").read_text(encoding="utf-8")
    header = (
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import re\n"
        "from pathlib import Path\n\n"
        f"{client_src}\n\n"
        f"initial_inputs = {json.dumps(dag_spec.initial_inputs)}\n"
        f"shuffled_steps = {json.dumps([{'node_id': n.node_id, 'op_name': n.op_name, 'bindings': n.input_bindings} for n in dag_spec.nodes])}\n"
        f"target_node_id = {json.dumps(dag_spec.target_node_id)}\n"
        f"PORT = {DEFAULT_MCP_PORT}\n"
        'MCP_HOST = "mcp-service"\n\n'
    )
    body = '''def fair_solve() -> None:
    session = McpHttpSession(host=MCP_HOST, port=PORT)
    status, raw = session.initialize()
    if status != 200:
        raise RuntimeError(f"Failed to initialize MCP session: {raw}")

    # Discover tools from catalog without hardcoded node-to-tool map
    _status, list_raw = session.request({"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}})
    frames = parse_mcp_frames(list_raw)
    tool_list = []
    if frames and isinstance(frames[0].get("result"), dict):
        tool_list = frames[0]["result"].get("tools", [])

    # Map public operation semantics to opaque catalog tool names without hidden node-to-tool mapping.
    semantic_markers = {
        "add_integers": "addition transformation",
        "multiply_integers": "multiplication transformation",
        "subtract_integers": "subtraction transformation",
        "scale_factor": "affine scaling transformation",
        "combine_metrics": "linear metric combination",
        "transform_signal": "signal transformation",
        "merge_checksums": "checksum merge",
    }
    op_to_tool = {}
    for t in tool_list:
        name = t.get("name", "")
        desc = t.get("description", "").lower()
        for op, marker in semantic_markers.items():
            if marker in desc:
                op_to_tool[op] = name

    # Topologically resolve shuffled steps
    node_values = dict(initial_inputs)
    steps_remaining = list(shuffled_steps)

    while steps_remaining:
        progress = False
        for step in list(steps_remaining):
            nid = step["node_id"]
            op = step["op_name"]
            bindings = step["bindings"]
            if all(src in node_values for src in bindings.values()):
                tool_name = op_to_tool.get(op)
                if not tool_name:
                    raise RuntimeError(f"Could not discover tool for operation {op}")
                tool_args = {param: node_values[src] for param, src in bindings.items()}
                res = session.call_tool(tool_name, tool_args)
                val = res.get("value") if isinstance(res, dict) else res
                if val is None or not isinstance(val, int):
                    raise RuntimeError(f"Tool {tool_name} returned non-integer: {res!r}")
                node_values[nid] = val
                steps_remaining.remove(step)
                progress = True
                break
        if not progress:
            raise RuntimeError("Deadlock in fair step dependency resolution")

    target_val = node_values[target_node_id]
    result_path = Path("/app/result.json")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({"target_value": target_val}, indent=2) + "\\n", encoding="utf-8"
    )
    print(f"Fair alternative solved target_value: {target_val}")

fair_solve()
'''
    return header + body


def _fair_alternative_sh_wrap(dag_spec: DAGSpec) -> str:
    py = _fair_alternative_solve_py(dag_spec)
    return "#!/bin/bash\nset -euo pipefail\npython3 - <<'PYEOF'\n" + py.split("#!/usr/bin/env python3\n", 1)[-1] + "\nPYEOF\n"


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
out = Path("/app/result.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({{"target_value": 0}}) + "\\n")
PYEOF
"""
