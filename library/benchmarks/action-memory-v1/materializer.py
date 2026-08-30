"""Materializer for action-memory-v1 Harbor task packages with a FastMCP sidecar."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

import yaml

from evallab.benchmark_program_contracts import (
    CellFactorsA,
    SyntheticFamilySpec,
    SyntheticFamilyType,
    compute_sha256,
)
from evallab.mcp_substrate import (
    DEFAULT_INTERNAL_NETWORK_NAME,
    DEFAULT_PINNED_BASE_IMAGE,
    DEFAULT_SIDECAR_SERVICE,
    DEFAULT_VOLUME_MOUNT,
    DEFAULT_VOLUME_NAME,
    MCPToolDefinition,
    MCPToolParameter,
    ResolverProvenance,
    RuntimeAsset,
    SubstrateError,
    materialize_mcp_sidecar_package,
    validate_mcp_compose_document,
)

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from package_finish import write_remaining_package
from package_layout import (
    SIDECAR_DIRNAME,
    instruction_md,
    oracle_solve_py,
    python_mcp_snippet,
    task_toml,
    write_environment_build_proof,
)

REPO = ROOT.parents[2]
DERIVED = REPO / "derived" / "harbor-tasks" / "action-memory"
WHEELHOUSE_ENV = "ACTION_MEMORY_WHEELHOUSE"
RESOLVER_PROVENANCE_ENV = "ACTION_MEMORY_RESOLVER_PROVENANCE"
TOOL_NAMES = ("list_context_chunks", "get_context_chunk", "execute_mutation")


def _get_state_module():
    mod_name = "action_memory_state_module"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / "action_memory_state.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def output_path(cell_id: str = "clean-baseline-4k", seed: int = 42) -> Path:
    contract_digest = hashlib.sha256((ROOT / "benchmark_contract.json").read_bytes()).hexdigest()
    return DERIVED / contract_digest / f"action-memory-{cell_id.replace('_', '-')}-seed{seed}"


def dose_ladder_output_path(cell_id: str, seed: int) -> Path:
    contract_digest = hashlib.sha256((ROOT / "dose_ladder_contract.json").read_bytes()).hexdigest()
    return (
        REPO
        / "derived"
        / "harbor-tasks"
        / "action-memory-dose-ladder"
        / contract_digest
        / f"action-memory-{cell_id}-seed{seed}"
    )


def handle_rep_output_path(cell_id: str, seed: int) -> Path:
    contract_file = ROOT / "handle_representation_contract.json"
    raw_contract = contract_file.read_bytes() if contract_file.exists() else b"{}"
    contract_digest = hashlib.sha256(raw_contract).hexdigest()
    return (
        REPO
        / "derived"
        / "harbor-tasks"
        / "action-memory-handle-representation"
        / contract_digest
        / f"action-memory-{cell_id}-seed{seed}"
    )


def reject_committed_corpora() -> None:
    tracked = [
        str(path)
        for path in ROOT.rglob("*")
        if path.is_file() and ("tasks" in path.parts or "derived" in path.parts)
    ]
    if tracked:
        raise AssertionError(f"Generated task corpus is tracked in repository: {tracked}")


def _wheelhouse_inputs() -> tuple[Path, ResolverProvenance]:
    """Load the required, target-specific offline dependency provenance."""
    wheelhouse_raw = os.environ.get(WHEELHOUSE_ENV, "").strip()
    provenance_raw = os.environ.get(RESOLVER_PROVENANCE_ENV, "").strip()
    if not wheelhouse_raw or not provenance_raw:
        raise ValueError(
            "production FastMCP materialization requires both "
            f"{WHEELHOUSE_ENV} and {RESOLVER_PROVENANCE_ENV}"
        )
    wheelhouse = Path(wheelhouse_raw)
    provenance_path = Path(provenance_raw)
    if not wheelhouse.is_dir():
        raise ValueError(f"target wheelhouse does not exist: {wheelhouse}")
    if not provenance_path.is_file():
        raise ValueError(f"resolver provenance does not exist: {provenance_path}")
    provenance = ResolverProvenance.from_json(
        json.loads(provenance_path.read_text(encoding="utf-8"))
    )
    return wheelhouse, provenance


def action_memory_tools(representation: str = "opaque") -> tuple[MCPToolDefinition, ...]:
    """Return tool definitions tailored to the declared handle representation mode."""
    base_tools = [
        MCPToolDefinition(
            name="list_context_chunks",
            description="List handles for the context records that must be inspected.",
            parameters=(),
            metadata={"op_kind": "list_context_chunks"},
        ),
        MCPToolDefinition(
            name="get_context_chunk",
            description="Read the content for one context handle.",
            parameters=(
                MCPToolParameter(name="chunk_id", type_name="str", description="Context handle"),
            ),
            metadata={"op_kind": "get_context_chunk"},
        ),
    ]

    if representation == "range_batch":
        base_tools.append(
            MCPToolDefinition(
                name="get_context_chunks",
                description="Batch or range retrieval of multiple context records.",
                parameters=(
                    MCPToolParameter(
                        name="chunk_ids",
                        type_name="list",
                        description="List of context handles",
                        required=False,
                    ),
                    MCPToolParameter(
                        name="range",
                        type_name="dict",
                        description="Range descriptor object with start and end indices",
                        required=False,
                    ),
                ),
                metadata={"op_kind": "get_context_chunks"},
            )
        )

    base_tools.append(
        MCPToolDefinition(
            name="execute_mutation",
            description="Execute the state mutation after deriving its arguments from retrieved context.",
            parameters=(
                MCPToolParameter(name="entity_id", type_name="str", description="Entity identifier"),
                MCPToolParameter(name="attribute", type_name="str", description="Attribute identifier"),
                MCPToolParameter(name="bound_value", type_name="str", description="Derived state value"),
            ),
            metadata={"op_kind": "execute_mutation"},
        )
    )
    return tuple(base_tools)


def materialize(
    output_dir: Path | None = None,
    cell_id: str = "clean-baseline-4k",
    seed: int = 42,
    arm: str = "clean",
    dose_bytes: int = 4096,
    inversion_count: int = 1,
    padding_position: str | None = None,
    distractor_count: int = 4,
    spec: object | None = None,
    extra_metadata: dict[str, object] | None = None,
    representation: str | None = None,
) -> dict[str, object]:
    state = _get_state_module()
    safe_cell = cell_id.replace("_", "-")
    extra_metadata = dict(extra_metadata or {})
    is_handle_rep = representation is not None or "handle_axis_version" in extra_metadata
    active_representation = (
        representation
        or str(extra_metadata.get("representation", "opaque") or "opaque")
        if is_handle_rep
        else "opaque"
    )

    if output_dir is None:
        output_dir = output_path(safe_cell, seed)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if spec is None:
        spec = state.generate_scenario(
            seed=seed,
            cell_id=safe_cell,
            arm=arm,
            dose_bytes=dose_bytes,
            inversion_count=inversion_count,
            padding_position=padding_position,
            distractor_count=distractor_count,
        )
    else:
        seed = spec.seed
        arm = spec.arm
        inversion_count = spec.inversion_count
        safe_cell = spec.cell_id.replace("_", "-")

    environment = output_dir / "environment"
    solution = output_dir / "solution"
    tests = output_dir / "tests"
    verifier_dir = output_dir / "verifier"
    workbench = output_dir / "workbench"
    adversarial = workbench / "adversarial"
    task_state = output_dir / "task_state"
    evidence = output_dir / "evidence"
    sidecar_dir = environment / SIDECAR_DIRNAME
    for directory in (
        environment,
        solution,
        tests,
        verifier_dir,
        workbench,
        adversarial,
        task_state,
        evidence,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    scenario_json = json.dumps(asdict(spec), indent=2, sort_keys=True) + "\n"
    (task_state / "scenario.json").write_text(scenario_json, encoding="utf-8")

    # Record handle-representation state when declared as a handle-representation intervention
    if is_handle_rep:
        handle_rep_payload = {
            "representation": active_representation,
            "representation_digest": str(extra_metadata.get("representation_digest", "")),
            "handle_axis_version": str(extra_metadata.get("handle_axis_version", "")),
            "declared_delta": str(extra_metadata.get("declared_delta", "")),
        }
        (task_state / "handle_representation.json").write_text(
            json.dumps(handle_rep_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

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

    target_spec: dict[str, Any] = {
        "spec_version": "1.1",
        "target_entity": spec.target_entity,
        "target_attribute": spec.target_attribute,
        "expected_bound_value": spec.latest_value,
        "required_chunk_ids": [
            chunk.chunk_id if hasattr(chunk, "chunk_id") else chunk["chunk_id"]
            for chunk in spec.chunks
        ],
        "dose_bytes": spec.dose_bytes,
        "update_opportunity_count": spec.update_opportunity_count,
        "read_opportunity_count": spec.read_opportunity_count,
        "mutation_opportunity_count": spec.mutation_opportunity_count,
    }
    if is_handle_rep:
        target_spec["representation"] = active_representation
        if "representation_digest" in extra_metadata:
            target_spec["representation_digest"] = extra_metadata["representation_digest"]
        if "handle_axis_version" in extra_metadata:
            target_spec["handle_axis_version"] = extra_metadata["handle_axis_version"]

    target_spec_json = json.dumps(target_spec, indent=2, sort_keys=True) + "\n"
    (tests / "fixtures").mkdir(parents=True, exist_ok=True)
    (verifier_dir / "fixtures").mkdir(parents=True, exist_ok=True)
    (tests / "fixtures" / "target_spec.json").write_text(target_spec_json, encoding="utf-8")
    (verifier_dir / "fixtures" / "target_spec.json").write_text(target_spec_json, encoding="utf-8")

    (environment / "entrypoint.sh").write_text(
        '#!/bin/sh\nset -eu\nmkdir -p /app/evidence /app/output\nif [ "$#" -gt 0 ]; then exec "$@"; fi\nexec sleep infinity\n',
        encoding="utf-8",
    )
    (environment / "entrypoint.sh").chmod(0o755)
    (environment / "Dockerfile").write_text(
        f"FROM {DEFAULT_PINNED_BASE_IMAGE}\nWORKDIR /app\nRUN mkdir -p /app/evidence /app/output\nCOPY entrypoint.sh /app/entrypoint.sh\nRUN chmod +x /app/entrypoint.sh\nENTRYPOINT [\"/app/entrypoint.sh\"]\n",
        encoding="utf-8",
    )

    tools = action_memory_tools(active_representation if is_handle_rep else "opaque")
    tool_names = tuple(t.name for t in tools)

    wheelhouse, resolver_provenance = _wheelhouse_inputs()
    runtime_assets = [
        RuntimeAsset(destination="ops.py", source=ROOT / "ops.py"),
        RuntimeAsset(destination="scenario.json", source=task_state / "scenario.json"),
    ]
    if is_handle_rep:
        runtime_assets.append(
            RuntimeAsset(
                destination="handle_representation.json",
                source=task_state / "handle_representation.json",
            )
        )

    package = materialize_mcp_sidecar_package(
        target_dir=sidecar_dir,
        tools=tools,
        server_name="action-memory-mcp",
        wheelhouse_source=wheelhouse,
        target=resolver_provenance.target,
        resolver_provenance=resolver_provenance,
        plan_only=False,
        op_registry_module="ops",
        internal_network_name=DEFAULT_INTERNAL_NETWORK_NAME,
        runtime_assets=tuple(runtime_assets),
    )
    write_environment_build_proof(environment, sidecar_dir)

    server = (sidecar_dir / "server.py").read_text(encoding="utf-8")
    if "from fastmcp import FastMCP" not in server:
        raise SubstrateError("sidecar server.py is not a generated FastMCP script")
    if 'transport="streamable-http"' not in server:
        raise SubstrateError("sidecar server.py is not streamable-http")
    if "HTTPServer" in server or "BaseHTTPRequestHandler" in server:
        raise SubstrateError("stdlib HTTP server leaked into sidecar")
    if "from ops import OP_REGISTRY" not in server:
        raise SubstrateError("sidecar server.py does not import the Action Memory op registry")

    compose_document = package["compose_doc"]
    compose_document["services"]["main"].pop("image", None)
    compose_document["services"]["main"]["build"] = "."
    valid, errors = validate_mcp_compose_document(compose_document)
    if not valid:
        raise RuntimeError(f"generated compose failed substrate validation: {errors}")
    (environment / "docker-compose.yaml").write_text(
        yaml.safe_dump(compose_document, sort_keys=False), encoding="utf-8"
    )

    return write_remaining_package(
        output_dir=output_dir,
        environment=environment,
        solution=solution,
        tests=tests,
        verifier_dir=verifier_dir,
        workbench=workbench,
        adversarial=adversarial,
        spec=spec,
        safe_cell=safe_cell,
        seed=seed,
        arm=arm,
        inversion_count=inversion_count,
        extra_metadata=extra_metadata,
        family_spec=family_spec,
        cell_factors=cell_factors,
        wheelhouse_inputs=(wheelhouse, resolver_provenance),
        tool_names=tool_names,
        sidecar_service=DEFAULT_SIDECAR_SERVICE,
        volume_name=DEFAULT_VOLUME_NAME,
        volume_mount=DEFAULT_VOLUME_MOUNT,
        internal_network=DEFAULT_INTERNAL_NETWORK_NAME,
    )


def materialize_dose_ladder_cell(
    seed: int, dose_bytes: int, arm: str, output_dir: Path | None = None
) -> dict[str, object]:
    spec_module = importlib.util.spec_from_file_location("am_dose_ladder_mod", ROOT / "dose_ladder.py")
    ladder = importlib.util.module_from_spec(spec_module)
    assert spec_module.loader is not None
    spec_module.loader.exec_module(ladder)
    spec = ladder.generate_matched_dose_arm(seed=seed, dose_bytes=dose_bytes, arm=arm)
    metadata = {
        "dose_axis_version": ladder.DOSE_AXIS_VERSION,
        "base_task_pair_id": ladder.base_task_pair_id(seed, dose_bytes),
        "declared_delta": ladder.DECLARED_DELTA,
        "padding_position": ladder.DOSE_LADDER_PADDING_POSITION,
        "step_budget": ladder.STEP_BUDGET,
    }
    if output_dir is None:
        output_dir = dose_ladder_output_path(spec.cell_id, seed)
    return materialize(
        output_dir=output_dir,
        cell_id=spec.cell_id,
        seed=seed,
        arm=arm,
        dose_bytes=dose_bytes,
        spec=spec,
        extra_metadata=metadata,
    )


def materialize_handle_representation_cell(
    seed: int,
    dose_bytes: int,
    arm: str,
    representation: str,
    output_dir: Path | None = None,
) -> dict[str, object]:
    """Materialize a matched Harbor task cell under the E0b handle-representation intervention."""
    spec_module = importlib.util.spec_from_file_location(
        "am_handle_rep_mod", ROOT / "handle_representation.py"
    )
    rep_module = importlib.util.module_from_spec(spec_module)
    assert spec_module.loader is not None
    spec_module.loader.exec_module(rep_module)

    norm_rep = rep_module.normalize_representation(representation)
    spec = rep_module.generate_matched_handle_arm(
        seed=seed, dose_bytes=dose_bytes, arm=arm, representation=norm_rep
    )
    metadata = {
        "handle_axis_version": rep_module.HANDLE_AXIS_VERSION,
        "base_task_pair_id": rep_module.base_task_pair_id(seed, dose_bytes, arm),
        "declared_delta": rep_module.DECLARED_DELTA,
        "representation": norm_rep,
        "representation_digest": rep_module.representation_digest(norm_rep),
        "padding_position": rep_module.HANDLE_PADDING_POSITION,
        "step_budget": rep_module.STEP_BUDGET,
    }
    if output_dir is None:
        output_dir = handle_rep_output_path(spec.cell_id, seed)
    return materialize(
        output_dir=output_dir,
        cell_id=spec.cell_id,
        seed=seed,
        arm=arm,
        dose_bytes=dose_bytes,
        spec=spec,
        extra_metadata=metadata,
        representation=norm_rep,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--cell-id", type=str, default="clean-baseline-4k")
    parser.add_argument("--seed", type=int, default=42)
    arguments = parser.parse_args()
    print(
        json.dumps(
            materialize(
                output_dir=arguments.output_dir,
                cell_id=arguments.cell_id,
                seed=arguments.seed,
            ),
            indent=2,
        )
    )
