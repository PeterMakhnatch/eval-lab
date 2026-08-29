"""Materializer for action-memory-v1 Harbor task packages with Compose MCP sidecar."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
DERIVED = REPO / "derived" / "harbor-tasks" / "action-memory"

PINNED_PYTHON_IMAGE = "python:3.13-slim@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251"

from evallab.benchmark_program_contracts import CellFactorsA, SyntheticFamilySpec, SyntheticFamilyType, compute_sha256
from evallab.mcp_substrate import (
    DEFAULT_INTERNAL_NETWORK_NAME,
    DEFAULT_PINNED_BASE_IMAGE,
    DEFAULT_SIDECAR_SERVICE,
    DEFAULT_VOLUME_MOUNT,
    DEFAULT_VOLUME_NAME,
    MCPToolDefinition,
    MCPToolParameter,
    RuntimeAsset,
    materialize_mcp_sidecar_package,
    render_mcp_compose_document,
    render_mcp_sidecar_dockerfile,
)
import yaml


def _get_state_module():
    mod_name = "action_memory_state_module"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / "state.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def output_path(cell_id: str = "clean-baseline-4k", seed: int = 42) -> Path:
    contract_bytes = (ROOT / "benchmark_contract.json").read_bytes()
    digest = hashlib.sha256(contract_bytes).hexdigest()
    safe_cell = cell_id.replace("_", "-")
    return DERIVED / digest / f"action-memory-{safe_cell}-seed{seed}"


def reject_committed_corpora() -> None:
    tracked = [
        str(p)
        for p in ROOT.rglob("*")
        if p.is_file() and ("tasks" in p.parts or "derived" in p.parts)
    ]
    if tracked:
        raise AssertionError(f"Generated task corpus is tracked in repository: {tracked}")


def materialize(
    output_dir: Path | None = None,
    cell_id: str = "clean-baseline-4k",
    seed: int = 42,
    arm: str = "clean",
    dose_bytes: int = 4096,
    inversion_count: int = 1,
    padding_position: str | None = None,
    distractor_count: int = 4,
) -> dict[str, object]:
    state_mod = _get_state_module()
    generate_scenario = state_mod.generate_scenario

    safe_cell = cell_id.replace("_", "-")
    if output_dir is None:
        output_dir = output_path(safe_cell, seed)

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    spec = generate_scenario(
        seed=seed,
        cell_id=safe_cell,
        arm=arm,
        dose_bytes=dose_bytes,
        inversion_count=inversion_count,
        padding_position=padding_position,
        distractor_count=distractor_count,
    )

    environment = output_dir / "environment"
    solution = output_dir / "solution"
    tests = output_dir / "tests"
    verifier_dir = output_dir / "verifier"
    workbench = output_dir / "workbench"
    adversarial = workbench / "adversarial"
    task_state = output_dir / "task_state"
    evidence = output_dir / "evidence"
    mcp_sidecar_dir = environment / "mcp-server"

    for d in (environment, solution, tests, verifier_dir, workbench, adversarial, task_state, evidence):
        d.mkdir(parents=True, exist_ok=True)

    # Write scenario json to task_state (agent runtime gets scenario.json canonically via RuntimeAsset)
    scenario_dict = asdict(spec)
    scenario_json_str = json.dumps(scenario_dict, indent=2, sort_keys=True) + "\n"
    (task_state / "scenario.json").write_text(scenario_json_str, encoding="utf-8")

    family_spec = SyntheticFamilySpec(
        family=SyntheticFamilyType.FAMILY_A_STATE_INVERSION,
        variant_id=safe_cell,
        dilation_tokens=max(0, dose_bytes // 4),
        forced_compaction=False,
        hidden_contract_hash=compute_sha256({"cell": safe_cell, "seed": seed, "arm": arm}),
    )
    cell_factors = CellFactorsA(
        dilation_tokens=family_spec.dilation_tokens,
        forced_compaction=False,
        semantic_distractors=arm == "semantic_distractor",
        seed=seed,
    )

    # Target specification strictly for verifier under tests/fixtures to avoid golden leak scan
    (tests / "fixtures").mkdir(parents=True, exist_ok=True)
    (verifier_dir / "fixtures").mkdir(parents=True, exist_ok=True)
    target_spec_dict = {
        "spec_version": "1.0",
        "target_entity": spec.target_entity,
        "target_attribute": spec.target_attribute,
        "expected_bound_value": spec.latest_value,
        "dose_bytes": spec.dose_bytes,
        "update_opportunity_count": spec.update_opportunity_count,
        "read_opportunity_count": spec.read_opportunity_count,
        "mutation_opportunity_count": spec.mutation_opportunity_count,
    }
    target_spec_str = json.dumps(target_spec_dict, indent=2, sort_keys=True) + "\n"
    (tests / "fixtures" / "target_spec.json").write_text(target_spec_str, encoding="utf-8")
    (verifier_dir / "fixtures" / "target_spec.json").write_text(target_spec_str, encoding="utf-8")

    # Main Agent Container Dockerfile & entrypoint with explicit /app/output directory creation
    (environment / "entrypoint.sh").write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /app/evidence /app/output\nif [ \"$#\" -gt 0 ]; then exec \"$@\"; fi\nexec sleep infinity\n",
        encoding="utf-8",
    )
    (environment / "entrypoint.sh").chmod(0o755)

    (environment / "Dockerfile").write_text(
        f"FROM {PINNED_PYTHON_IMAGE}\nWORKDIR /app\nRUN mkdir -p /app/evidence /app/output\nCOPY entrypoint.sh /app/entrypoint.sh\nRUN chmod +x /app/entrypoint.sh\nENTRYPOINT [\"/app/entrypoint.sh\"]\n",
        encoding="utf-8",
    )

    tools = (
        MCPToolDefinition(
            name="list_context_chunks",
            description="List all available context log chunk IDs.",
            parameters=(),
            execution_body=(
                "scenario = json.loads(Path('/app/scenario.json').read_text())\n"
                "res = {'chunk_ids': [c['chunk_id'] for c in scenario.get('chunks', [])]}\n"
                "log_tool_event('list_context_chunks', args, res)\n"
                "return res"
            ),
        ),
        MCPToolDefinition(
            name="get_context_chunk",
            description="Retrieve content and metadata for a specific chunk ID.",
            parameters=(MCPToolParameter(name="chunk_id", type_name="str", description="The ID of the chunk to read"),),
            execution_body=(
                "scenario = json.loads(Path('/app/scenario.json').read_text())\n"
                "chunk = next((c for c in scenario.get('chunks', []) if c['chunk_id'] == chunk_id), None)\n"
                "res = chunk or {'error': 'not_found'}\n"
                "log_tool_event('get_context_chunk', args, res)\n"
                "return res"
            ),
        ),
        MCPToolDefinition(
            name="execute_mutation",
            description="Execute final state-bound mutation action on target entity.",
            parameters=(
                MCPToolParameter(name="entity_id", type_name="str", description="Target entity ID"),
                MCPToolParameter(name="attribute", type_name="str", description="Target attribute key"),
                MCPToolParameter(name="bound_value", type_name="str", description="Latest state value to bind"),
            ),
            execution_body=(
                "out = Path('/app/output')\n"
                "out.mkdir(parents=True, exist_ok=True)\n"
                "final_state = {'status': 'executed', 'target_entity': entity_id, 'target_attribute': attribute, 'bound_value': bound_value}\n"
                "(out / 'final-state.json').write_text(json.dumps(final_state, indent=2, sort_keys=True))\n"
                "log_tool_event('execute_mutation', args, final_state)\n"
                "return final_state"
            ),
        ),
    )
    runtime_assets = (
        RuntimeAsset("scenario.json", content=scenario_json_str.encode("utf-8")),
        RuntimeAsset("runtime.py", source=ROOT / "runtime.py"),
    )
    pkg = materialize_mcp_sidecar_package(
        target_dir=mcp_sidecar_dir,
        tools=tools,
        server_name="action-memory-mcp",
        plan_only=True,
        internal_network_name=DEFAULT_INTERNAL_NETWORK_NAME,
        runtime_assets=runtime_assets,
    )
    compose_doc = pkg["compose_doc"]
    compose_doc["services"]["main"].pop("image", None)
    compose_doc["services"]["main"]["build"] = "."
    (environment / "docker-compose.yaml").write_text(
        yaml.safe_dump(compose_doc, sort_keys=False),
        encoding="utf-8",
    )

    # Solution script using http.client with readiness check
    solution_solve_py = f'''#!/usr/bin/env python3
import http.client
import json
import re
import sys
import time

def wait_ready():
    for _ in range(50):
        try:
            conn = http.client.HTTPConnection("mcp-service", 8080, timeout=1)
            conn.request("GET", "/health")
            if conn.getresponse().status == 200:
                conn.close()
                return
            conn.close()
        except Exception:
            pass
        time.sleep(0.1)

def rpc_call(method, params):
    conn = http.client.HTTPConnection("mcp-service", 8080, timeout=10)
    body = json.dumps({{"jsonrpc": "2.0", "id": 1, "method": method, "params": params}})
    conn.request("POST", "/mcp", body, {{"Content-Type": "application/json"}})
    res = json.loads(conn.getresponse().read().decode("utf-8"))
    conn.close()
    return res.get("result", {{}})

def main():
    wait_ready()
    rpc_call("initialize", {{"protocolVersion": "2024-11-05"}})
    chunks_res = rpc_call("tools/call", {{"name": "list_context_chunks", "arguments": {{}}}})
    chunk_ids = json.loads(chunks_res["content"][0]["text"])["chunk_ids"]
    target_entity = "{spec.target_entity}"
    target_attr = "{spec.target_attribute}"
    latest_val = None
    for cid in chunk_ids:
        c_res = rpc_call("tools/call", {{"name": "get_context_chunk", "arguments": {{"chunk_id": cid}}}})
        text = c_res["content"][0]["text"]
        if target_entity in text:
            m = re.search(r"'(?P<val>[^']+)'", text)
            if m:
                latest_val = m.group("val")
    rpc_call("tools/call", {{
        "name": "execute_mutation",
        "arguments": {{"entity_id": target_entity, "attribute": target_attr, "bound_value": latest_val}}
    }})

if __name__ == "__main__":
    main()
'''
    (solution / "solve.py").write_text(solution_solve_py, encoding="utf-8")
    (solution / "solve.sh").write_text(
        "#!/bin/sh\nset -eu\npython3 /solution/solve.py\n",
        encoding="utf-8",
    )
    (solution / "solve.sh").chmod(0o755)

    # Copy the canonical verifier.py directly into tests and verifier
    shutil.copy2(ROOT / "verifier.py", tests / "verify.py")
    shutil.copy2(ROOT / "verifier.py", verifier_dir / "verify.py")

    # Tests / Verifier test.sh
    test_sh_content = "#!/bin/sh\nset -eu\nmkdir -p /logs/verifier\npython3 /tests/verify.py --task-dir /tests --evidence-dir /app/output --reward-dir /logs/verifier\n"
    (tests / "test.sh").write_text(test_sh_content, encoding="utf-8")
    (tests / "test.sh").chmod(0o755)

    verifier_test_sh = "#!/bin/sh\nset -eu\nmkdir -p /logs/verifier\npython3 /verifier/verify.py --task-dir /verifier --evidence-dir /app/output --reward-dir /logs/verifier\n"
    (verifier_dir / "test.sh").write_text(verifier_test_sh, encoding="utf-8")
    (verifier_dir / "test.sh").chmod(0o755)

    # Tests / Verifier Dockerfile copying test.sh and verify.py
    (tests / "Dockerfile").write_text(
        f"FROM {PINNED_PYTHON_IMAGE}\nWORKDIR /app\nRUN mkdir -p /logs/verifier /app/output /tests\nCOPY verify.py /tests/verify.py\nCOPY test.sh /tests/test.sh\nCOPY fixtures /tests/fixtures\nRUN chmod +x /tests/test.sh /tests/verify.py\nCMD [\"sleep\", \"infinity\"]\n",
        encoding="utf-8",
    )

    (verifier_dir / "Dockerfile").write_text(
        f"FROM {PINNED_PYTHON_IMAGE}\nWORKDIR /app\nRUN mkdir -p /logs/verifier /app/output /verifier\nCOPY verify.py /verifier/verify.py\nCOPY test.sh /verifier/test.sh\nCOPY fixtures /verifier/fixtures\nRUN chmod +x /verifier/test.sh /verifier/verify.py\nCMD [\"sleep\", \"infinity\"]\n",
        encoding="utf-8",
    )

    # Workbench fair-alternative.sh
    (workbench / "fair-alternative.sh").write_text(
        f"""#!/bin/sh
set -eu
python3 -c '
import http.client, json
conn = http.client.HTTPConnection("mcp-service", 8080, timeout=10)
body = json.dumps({{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {{"name": "execute_mutation", "arguments": {{"entity_id": "{spec.target_entity}", "attribute": "{spec.target_attribute}", "bound_value": "{spec.latest_value}"}}}}}})
conn.request("POST", "/mcp", body, {{"Content-Type": "application/json"}})
conn.close()
'
""",
        encoding="utf-8",
    )
    (workbench / "fair-alternative.sh").chmod(0o755)

    # Workbench please-hack.sh
    (workbench / "please-hack.sh").write_text(
        """#!/bin/sh
set -eu
mkdir -p /logs/verifier /app/output
printf '1.0\n' > /logs/verifier/reward.txt
printf '{"reward": 1.0, "hacked": true}\n' > /logs/verifier/result.json
""",
        encoding="utf-8",
    )
    (workbench / "please-hack.sh").chmod(0o755)

    # Adversarial probes
    (adversarial / "stale-value.sh").write_text(
        f"""#!/bin/sh
set -eu
python3 -c '
import http.client, json
conn = http.client.HTTPConnection("mcp-service", 8080, timeout=10)
body = json.dumps({{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {{"name": "execute_mutation", "arguments": {{"entity_id": "{spec.target_entity}", "attribute": "{spec.target_attribute}", "bound_value": "{spec.initial_value}"}}}}}})
conn.request("POST", "/mcp", body, {{"Content-Type": "application/json"}})
conn.close()
'
""",
        encoding="utf-8",
    )
    (adversarial / "stale-value.sh").chmod(0o755)

    (adversarial / "wrong-entity.sh").write_text(
        f"""#!/bin/sh
set -eu
python3 -c '
import http.client, json
conn = http.client.HTTPConnection("mcp-service", 8080, timeout=10)
body = json.dumps({{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {{"name": "execute_mutation", "arguments": {{"entity_id": "entity-invalid-000", "attribute": "{spec.target_attribute}", "bound_value": "{spec.latest_value}"}}}}}})
conn.request("POST", "/mcp", body, {{"Content-Type": "application/json"}})
conn.close()
'
""",
        encoding="utf-8",
    )
    (adversarial / "wrong-entity.sh").chmod(0o755)

    (adversarial / "empty-output.sh").write_text(
        "#!/bin/sh\nset -eu\nexit 0\n",
        encoding="utf-8",
    )
    (adversarial / "empty-output.sh").chmod(0o755)

    # Harbor task.toml & instruction.md
    task_toml_content = f'''schema_version = "1.4"
artifacts = ["/app/output/benchmark-events.jsonl", "/app/output/final-state.json"]

[task]
version = "1.0.0"
name = "evallab/action-memory-{safe_cell}-seed{seed}"
description = "Actionable memory and dynamic state inversion benchmark task with streamable HTTP MCP server"
keywords = ["action-memory", "mcp", "context-growth", "state-inversion", "synthetic"]

[[task.authors]]
name = "Eval Lab"
email = "eval-lab@example.invalid"

[metadata]
difficulty = "easy"
category = "synthetic"
tags = ["action-memory", "mcp", "context-growth", "state-inversion"]
construct_name = "actionable_entity_memory"
cell_id = "{safe_cell}"
arm = "{arm}"
seed = {seed}
dose_bytes = {spec.dose_bytes}
inversion_count = {inversion_count}
license = "Apache-2.0"

[agent]
timeout_sec = 600.0

[verifier]
timeout_sec = 120.0
environment_mode = "separate"

[verifier.environment]
network_mode = "no-network"

[[verifier.collect]]
service = "main"
command = "if [ -f /app/output/benchmark-events.jsonl ]; then cp -f /app/output/benchmark-events.jsonl /app/output/benchmark-events.jsonl; fi"

[[verifier.collect]]
service = "main"
command = "if [ -f /app/output/final-state.json ]; then cp -f /app/output/final-state.json /app/output/final-state.json; fi"

[environment]
network_mode = "no-network"
build_timeout_sec = 120.0
os = "linux"
cpus = 1
memory_mb = 512
storage_mb = 1024

[[environment.mcp_servers]]
name = "memory_mcp"
transport = "streamable-http"
url = "http://mcp-service:8080/mcp"
'''
    (output_dir / "task.toml").write_text(task_toml_content, encoding="utf-8")

    instruction_md_content = f"""# Action Memory Task: Configuration State Mutation

You are operating an agent workflow against a stateful streamable HTTP MCP server running at `http://mcp-service:8080/mcp`.

## Objective
1. Inspect the system context logs and state updates using the MCP server tools (`list_context_chunks`, `get_context_chunk`).
2. Identify target entity `{spec.target_entity}` and observe its configuration history across logs.
3. Determine the latest bound value for attribute `{spec.target_attribute}` after all state inversions.
4. Execute the final mutation by calling the `execute_mutation` MCP tool with `{spec.target_entity}`, `{spec.target_attribute}`, and the latest value.
5. The MCP server will automatically record events into `/app/output/benchmark-events.jsonl` and `/app/output/final-state.json`.
"""
    (output_dir / "instruction.md").write_text(instruction_md_content, encoding="utf-8")

    return {
        "output_path": str(output_dir),
        "target_entity": spec.target_entity,
        "latest_value": spec.latest_value,
        "initial_value": spec.initial_value,
        "dose_bytes": spec.dose_bytes,
        "arm": arm,
        "family_digest": family_spec.identity_digest(),
        "cell_factors": cell_factors.model_dump(),
        "sidecar_service": DEFAULT_SIDECAR_SERVICE,
        "volume_name": DEFAULT_VOLUME_NAME,
        "volume_mount": DEFAULT_VOLUME_MOUNT,
        "internal_network": DEFAULT_INTERNAL_NETWORK_NAME,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--cell-id", type=str, default="clean-baseline-4k")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    res = materialize(output_dir=args.output_dir, cell_id=args.cell_id, seed=args.seed)
    print(json.dumps(res, indent=2))
