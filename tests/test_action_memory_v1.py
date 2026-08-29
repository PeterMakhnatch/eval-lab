from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from evallab.mcp_substrate import (
    DEFAULT_TARGET_PLATFORM_TAG,
    DEFAULT_TARGET_PYTHON_TAG,
    SubstrateError,
    WheelhouseTarget,
    record_prepackaging_provenance,
)

ROOT = Path(__file__).parents[1] / "library" / "benchmarks" / "action-memory-v1"


def load(name: str, filename: str | None = None):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{filename or name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_contract_metadata_and_cell_factors():
    contract_file = ROOT / "benchmark_contract.json"
    assert contract_file.exists()
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    assert contract["benchmark_family"] == "action-memory-v1"
    assert contract["construct"] == "actionable_entity_memory_and_value_binding"
    assert len(contract["cells"]) >= 4
    cell_arms = {c["arm"] for c in contract["cells"]}
    assert "clean" in cell_arms
    assert "neutral_padding" in cell_arms
    assert "semantic_distractor" in cell_arms


def test_contract_opportunity_counts_match_generator_output():
    state_mod = load("action_memory_state_match", "state")
    contract_file = ROOT / "benchmark_contract.json"
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    for cell in contract["cells"]:
        spec = state_mod.generate_scenario(
            seed=42,
            cell_id=cell["cell_id"],
            arm=cell["arm"],
            dose_bytes=cell["dose_bytes"],
            inversion_count=cell["inversion_count"],
            padding_position=cell.get("padding_position"),
            distractor_count=cell.get("distractor_count", 4),
        )
        assert cell["read_opportunity_count"] == spec.read_opportunity_count, (
            f"Cell {cell['cell_id']} read_opportunity_count mismatch: "
            f"declared {cell['read_opportunity_count']} != generated {spec.read_opportunity_count}"
        )
        assert cell["update_opportunity_count"] == spec.update_opportunity_count
        assert cell["mutation_opportunity_count"] == spec.mutation_opportunity_count
        assert cell["dose_bytes"] == spec.dose_bytes


def test_state_generation_deterministic_and_dosed():
    state_mod = load("action_memory_state_gen", "state")
    spec1 = state_mod.generate_scenario(seed=42, cell_id="clean_baseline_4k", arm="clean")
    spec2 = state_mod.generate_scenario(seed=42, cell_id="clean_baseline_4k", arm="clean")
    assert spec1 == spec2
    assert spec1.target_entity.startswith("entity_")
    assert spec1.latest_value != spec1.initial_value
    assert spec1.dose_bytes == 4096
    assert spec1.update_opportunity_count >= 1
    assert spec1.mutation_opportunity_count == 1


def test_materializer_plan_only_emits_clean_specification_without_compose_or_dockerfile(tmp_path: Path):
    mat_mod = load("action_memory_mat_test", "materializer")
    target = tmp_path / "action_mem_task_plan"
    mat_mod.materialize(output_dir=target, cell_id="clean_baseline_4k", seed=42, plan_only=True)
    assert not (target / "task.toml").exists()
    assert (target / "instruction.md").exists()
    assert not (target / "environment" / "docker-compose.yaml").exists()
    assert not (target / "environment" / "Dockerfile").exists()
    assert (target / "environment" / "mcp-server" / "server.py").exists()
    assert (target / "environment" / "mcp-server" / "scenario.json").exists()
    assert (target / "environment" / "mcp-server" / "ops.py").exists()
    assert (target / "environment" / "mcp-server" / "offline-build-proof.json").exists()
    assert not (target / "environment" / "mcp-server" / "Dockerfile").exists()
    assert not (target / "environment" / "mcp-server" / "wheelhouse").exists()


def test_materializer_production_generates_valid_harbor_and_compose_structure(tmp_path: Path):
    mat_mod = load("action_memory_mat_prod", "materializer")
    target = tmp_path / "action_mem_task_prod_pkg"

    # The production trust root requires the exact reviewed 66-wheel wheelhouse;
    # a synthetic single-wheel house is rejected (TOFU refusal). Use the real
    # trusted wheelhouse when available.
    real_wh = Path("/tmp/fastmcp3_wheelhouse")
    if not real_wh.is_dir():
        pytest.skip("FastMCP 3.4.7 trusted wheelhouse not populated on this host")

    wheel_target = WheelhouseTarget(DEFAULT_TARGET_PYTHON_TAG, DEFAULT_TARGET_PLATFORM_TAG)
    provenance = record_prepackaging_provenance(real_wh, wheel_target)

    mat_mod.materialize(
        output_dir=target,
        cell_id="clean_baseline_4k",
        seed=42,
        wheelhouse_source=real_wh,
        resolver_provenance=provenance,
        plan_only=False,
    )
    assert (target / "task.toml").exists()
    assert (target / "instruction.md").exists()
    assert (target / "environment" / "docker-compose.yaml").exists()
    assert (target / "environment" / "mcp-server" / "server.py").exists()
    assert (target / "environment" / "mcp-server" / "scenario.json").exists()
    assert (target / "environment" / "mcp-server" / "ops.py").exists()
    assert (target / "environment" / "mcp-server" / "Dockerfile").exists()
    assert (target / "environment" / "mcp-server" / "wheelhouse").exists()

    compose = (target / "environment" / "docker-compose.yaml").read_text(encoding="utf-8")
    assert "workbench-internal" in compose
    assert "mcp-service:" in compose
    assert "internal: true" in compose
    assert (target / "verifier" / "verify.py").exists()
    assert (target / "tests" / "verify.py").exists()
    assert (target / "solution" / "solve.sh").exists()
    assert (target / "workbench" / "fair-alternative.sh").exists()
    assert (target / "workbench" / "please-hack.sh").exists()
    assert (target / "workbench" / "adversarial" / "stale-value.sh").exists()
    assert (target / "workbench" / "adversarial" / "wrong-entity.sh").exists()
    assert (target / "workbench" / "adversarial" / "empty-output.sh").exists()


def test_materializer_production_requires_wheelhouse_and_provenance(tmp_path: Path):
    mat_mod = load("action_memory_mat_prod_test", "materializer")
    target = tmp_path / "action_mem_task_prod"
    with pytest.raises(SubstrateError, match="wheelhouse_source is mandatory"):
        mat_mod.materialize(output_dir=target, cell_id="clean_baseline_4k", seed=42, plan_only=False)
    assert not target.exists()


def test_mcp_server_client_protocol_interaction(tmp_path: Path):
    mat_mod = load("action_memory_mat_mcp", "materializer")
    runtime_mod = load("action_memory_runtime_mcp", "runtime")
    oracle_mod = load("action_memory_oracle_mcp", "oracle")

    task_dir = tmp_path / "mcp_task"
    mat_mod.materialize(output_dir=task_dir, cell_id="clean_baseline_4k", seed=42, plan_only=True)

    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server_thread = threading.Thread(
        target=runtime_mod.start_server,
        args=(task_dir / "task_state", task_dir / "evidence", port),
        daemon=True,
    )
    server_thread.start()
    time.sleep(0.2)

    # Exercise standard client session: initialize, list_tools, call_tools
    client = runtime_mod.MCPClient(f"http://127.0.0.1:{port}/mcp")
    assert client.wait_until_ready(timeout_sec=5.0)
    init_res = client.initialize()
    assert init_res["serverInfo"]["name"] == "action-memory-mcp"

    tools = client.list_tools()
    tool_names = {t["name"] for t in tools}
    assert tool_names == {"list_context_chunks", "get_context_chunk", "execute_mutation"}

    # Run oracle solver over the live MCP protocol
    oracle_mod.solve_via_mcp(mcp_url=f"http://127.0.0.1:{port}/mcp")

    # Verify produced evidence in evidence/ and simulate collect transfer to output/
    evidence_dir = task_dir / "evidence"
    output_dir = task_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    assert (evidence_dir / "benchmark-events.jsonl").exists()
    assert (evidence_dir / "final-state.json").exists()

    shutil.copy2(evidence_dir / "benchmark-events.jsonl", output_dir / "benchmark-events.jsonl")
    shutil.copy2(evidence_dir / "final-state.json", output_dir / "final-state.json")

    final_state = json.loads((output_dir / "final-state.json").read_text(encoding="utf-8"))
    assert final_state["status"] == "executed"
    assert final_state["bound_value"] != ""

    # Verify verifier passes with reward 1.0 on copied live MCP evidence
    ver_mod = load("action_memory_ver_mcp", "verifier")
    reward_dir = task_dir / "rewards"
    ver_res = ver_mod.verify(task_dir / "task_state", evidence_dir, reward_dir)
    assert ver_res["reward"] == 1.0, f"Live MCP trial verifier failed: {ver_res}"
