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
    SubstrateError,
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
    wheelhouse_source: Path | None = None,
    resolver_provenance: Any | None = None,
    plan_only: bool = True,
) -> dict[str, object]:
    state_mod = _get_state_module()
    generate_scenario = state_mod.generate_scenario

    safe_cell = cell_id.replace("_", "-")
    if output_dir is None:
        output_dir = output_path(safe_cell, seed)


    if not plan_only and (wheelhouse_source is None or resolver_provenance is None):
        raise SubstrateError("wheelhouse_source is mandatory for production sidecar materialization; pass plan_only=True to emit plan without container build artifacts")

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

    if not plan_only:
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
            metadata={"op_kind": "list_context_chunks"},
        ),
        MCPToolDefinition(
            name="get_context_chunk",
            description="Retrieve content and metadata for a specific chunk ID.",
            parameters=(MCPToolParameter(name="chunk_id", type_name="str", description="The ID of the chunk to read"),),
            metadata={"op_kind": "get_context_chunk"},
        ),
        MCPToolDefinition(
            name="execute_mutation",
            description="Execute final state-bound mutation action on target entity.",
            parameters=(
                MCPToolParameter(name="entity_id", type_name="str", description="Target entity ID"),
                MCPToolParameter(name="attribute", type_name="str", description="Target attribute key"),
                MCPToolParameter(name="bound_value", type_name="str", description="Latest state value to bind"),
            ),
            metadata={"op_kind": "execute_mutation"},
        ),
    )

    runtime_assets = (
        RuntimeAsset("ops.py", source=ROOT / "ops.py"),
        RuntimeAsset("scenario.json", content=scenario_json_str.encode("utf-8")),
    )

    pkg = materialize_mcp_sidecar_package(
        target_dir=mcp_sidecar_dir,
        tools=tools,
        server_name="action-memory-mcp",
        op_registry_module="ops",
        wheelhouse_source=wheelhouse_source,
        resolver_provenance=resolver_provenance,
        plan_only=plan_only,
        internal_network_name=DEFAULT_INTERNAL_NETWORK_NAME,
        runtime_assets=runtime_assets,
    )

    if not plan_only:
        assert pkg["compose_doc"] is not None
        compose_doc = pkg["compose_doc"]
        compose_doc["services"]["main"].pop("image", None)
        compose_doc["services"]["main"]["build"] = "."
        (environment / "docker-compose.yaml").write_text(
            yaml.safe_dump(compose_doc, sort_keys=False),
            encoding="utf-8",
        )

        solution_solve_py = """#!/usr/bin/env python3
from client import McpHttpSession
import json
import re

def main():
    session = McpHttpSession()
    status, raw = session.initialize()
    if status != 200:
        raise RuntimeError(raw)
    listed = session.call_tool("list_context_chunks", {})
    if not isinstance(listed, dict) or "chunk_ids" not in listed:
        raise RuntimeError(f"list_context_chunks returned {listed!r}")
    target_entity = None
    latest_value = None
    b = chr(92) + "b"
    for chunk_id in listed["chunk_ids"]:
        chunk = session.call_tool("get_context_chunk", {"chunk_id": chunk_id})
        text = json.dumps(chunk, sort_keys=True) if isinstance(chunk, dict) else str(chunk)
        if target_entity is None:
            match = re.search(r"for\\s+(entity_\\d+)", text)
            if match:
                target_entity = match.group(1)
        if target_entity and re.search(b + re.escape(target_entity) + b, text):
            match_value = re.search(r"'(?P<val>[^']+)'", text)
            if match_value:
                latest_value = match_value.group("val")
    if not target_entity or not latest_value:
        raise RuntimeError("could not resolve latest bound value from MCP chunks")
    res = session.call_tool("execute_mutation", {"entity_id": target_entity, "attribute": "routing_key", "bound_value": latest_value})
    if isinstance(res, dict) and res.get("status") != "executed":
        raise RuntimeError(f"execute_mutation failed: {res!r}")

if __name__ == "__main__":
    main()
"""
        shutil.copy2(ROOT / "client.py", solution / "client.py")
        (solution / "solve.py").write_text(solution_solve_py, encoding="utf-8")
        (solution / "solve.sh").write_text(
            "#!/bin/sh\nset -eu\npython3 /solution/solve.py\n",
            encoding="utf-8",
        )
        (solution / "solve.sh").chmod(0o755)

        shutil.copy2(ROOT / "verifier.py", tests / "verify.py")
        shutil.copy2(ROOT / "verifier.py", verifier_dir / "verify.py")

        test_sh_content = "#!/bin/sh\nset -eu\nmkdir -p /logs/verifier\npython3 /tests/verify.py --task-dir /tests --evidence-dir /app/output --reward-dir /logs/verifier\n"
        (tests / "Dockerfile").write_text(f"FROM {PINNED_PYTHON_IMAGE}\nWORKDIR /tests\nCOPY . /tests\n", encoding="utf-8")
        (tests / "test.sh").write_text(test_sh_content, encoding="utf-8")
        (tests / "test.sh").chmod(0o755)

        verifier_test_sh = "#!/bin/sh\nset -eu\nmkdir -p /logs/verifier\npython3 /verifier/verify.py --task-dir /verifier --evidence-dir /app/output --reward-dir /logs/verifier\n"
        (verifier_dir / "test.sh").write_text(verifier_test_sh, encoding="utf-8")
        (verifier_dir / "test.sh").chmod(0o755)

        # Fair alternative is self-contained: only the instruction-visible streamable MCP contract is used.
        client_source = (ROOT / "client.py").read_text(encoding="utf-8")
        fair_python = """
import json
import re

client = McpHttpSession()
status, response = client.initialize()
if status != 200:
    raise RuntimeError(response)
catalog = client.call_tool("list_context_chunks", {})
entity = None
value = None
b = chr(92) + "b"
for chunk_id in catalog.get("chunk_ids", []):
    visible = client.call_tool("get_context_chunk", {"chunk_id": chunk_id})
    text = json.dumps(visible, sort_keys=True) if isinstance(visible, dict) else str(visible)
    found = re.search(r"for\\s+(entity_\\d+)", text)
    if entity is None and found:
        entity = found.group(1)
    if entity and re.search(b + re.escape(entity) + b, text):
        candidates = re.findall(r"'([^']+)'", text)
        if candidates:
            value = candidates[-1]
if not entity or not value:
    raise RuntimeError("unable to derive public MCP mutation")
res = client.call_tool("execute_mutation", {"entity_id": entity, "attribute": "routing_key", "bound_value": value})
if isinstance(res, dict) and res.get("status") != "executed":
    raise RuntimeError(f"execute_mutation failed: {res!r}")
"""
        fair_shell = "#!/bin/sh\nset -eu\npython3 - <<'PY'\n" + client_source + fair_python + "PY\n"
        (workbench / "fair-alternative.sh").write_text(fair_shell, encoding="utf-8")
        (workbench / "fair-alternative.sh").chmod(0o755)

        (workbench / "please-hack.sh").write_text(
            "#!/bin/sh\nmkdir -p /app/output\necho '{\"status\":\"hacked\"}' > /app/output/final-state.json\n",
            encoding="utf-8",
        )
        (workbench / "please-hack.sh").chmod(0o755)

        (adversarial / "stale-value.sh").write_text(
            f"#!/bin/sh\nset -eu\npython3 -c 'from templates import stale_value_mutant; stale_value_mutant(Path(\"/app\"), Path(\"/app/output\"))'\n",
            encoding="utf-8",
        )
        (adversarial / "stale-value.sh").chmod(0o755)

        (adversarial / "wrong-entity.sh").write_text(
            f"#!/bin/sh\nset -eu\npython3 -c 'from templates import wrong_entity_mutant; wrong_entity_mutant(Path(\"/app\"), Path(\"/app/output\"))'\n",
            encoding="utf-8",
        )
        (adversarial / "wrong-entity.sh").chmod(0o755)

        (adversarial / "empty-output.sh").write_text(
            "#!/bin/sh\nmkdir -p /app/output\n",
            encoding="utf-8",
        )
        (adversarial / "empty-output.sh").chmod(0o755)

        task_toml_content = f"""schema_version = "1.0"

artifacts = ["/app/output/benchmark-events.jsonl", "/app/output/final-state.json"]

[task]
name = "action-memory/{safe_cell}"
version = "1.0"
authors = [{{ name = "Eval Lab" }}]
description = "Two-session entity state update and memory retention task."
keywords = ["action-memory", "mcp-sidecar", "state-inversion"]

[metadata]
category = "synthetic"
difficulty = "easy"
tags = ["action-memory", "mcp-sidecar", "state-inversion"]

[agent]
timeout_sec = 600.0

[environment]
network_mode = "no-network"
build_timeout_sec = 120.0
cpus = 1
memory_mb = 2048
storage_mb = 1024

[[environment.mcp_servers]]
name = "action-memory-mcp"
transport = "streamable-http"
url = "http://mcp-service:8080/mcp"

[verifier]
timeout_sec = 60.0
environment_mode = "separate"
network_mode = "no-network"
"""
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
    parser = argparse.ArgumentParser(description="Action Memory benchmark materializer")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--cell-id", type=str, default="clean-baseline-4k")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--wheelhouse-source", type=Path, default=None)
    parser.add_argument("--provenance-file", type=Path, default=None)
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--plan-only", action="store_true", help="Generate task plan specification without container build artifacts")
    mode_group.add_argument("--production", action="store_true", help="Generate complete production package requiring wheelhouse")
    args = parser.parse_args()

    provenance = None
    if args.production:
        if args.wheelhouse_source is None:
            parser.error("--production requires --wheelhouse-source")
        if args.provenance_file is None or not args.provenance_file.is_file():
            parser.error("--production requires valid --provenance-file")
        from evallab.mcp_substrate import ResolverProvenance
        provenance = ResolverProvenance.from_json(json.loads(args.provenance_file.read_text(encoding="utf-8")))

    res = materialize(
        output_dir=args.output_dir,
        cell_id=args.cell_id,
        seed=args.seed,
        wheelhouse_source=args.wheelhouse_source,
        resolver_provenance=provenance,
        plan_only=args.plan_only,
    )
    print(json.dumps(res, indent=2))
