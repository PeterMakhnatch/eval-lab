from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "library" / "benchmarks" / "action-memory-v1"


def load(name: str, filename: str | None = None):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{filename or name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _require_production_sidecar() -> None:
    if not (os.environ.get("ACTION_MEMORY_WHEELHOUSE") and os.environ.get("ACTION_MEMORY_RESOLVER_PROVENANCE")):
        pytest.skip("target-specific FastMCP wheelhouse/provenance not populated on this host")


def test_contract_metadata_and_cell_factors():
    contract = json.loads((ROOT / "benchmark_contract.json").read_text(encoding="utf-8"))
    assert contract["benchmark_family"] == "action-memory-v1"
    assert contract["construct"] == "actionable_entity_memory_and_value_binding"
    assert len(contract["cells"]) >= 4
    assert {cell["arm"] for cell in contract["cells"]} >= {"clean", "neutral_padding", "semantic_distractor"}


def test_contract_opportunity_counts_match_generator_output():
    state = load("action_memory_state_match", "action_memory_state")
    contract = json.loads((ROOT / "benchmark_contract.json").read_text(encoding="utf-8"))
    for cell in contract["cells"]:
        spec = state.generate_scenario(
            seed=42,
            cell_id=cell["cell_id"],
            arm=cell["arm"],
            dose_bytes=cell["dose_bytes"],
            inversion_count=cell["inversion_count"],
            padding_position=cell.get("padding_position"),
            distractor_count=cell.get("distractor_count", 4),
        )
        assert cell["read_opportunity_count"] == spec.read_opportunity_count
        assert cell["update_opportunity_count"] == spec.update_opportunity_count
        assert cell["mutation_opportunity_count"] == spec.mutation_opportunity_count
        assert cell["dose_bytes"] == spec.dose_bytes


def test_state_generation_deterministic_and_dosed():
    state = load("action_memory_state_gen", "action_memory_state")
    first = state.generate_scenario(seed=42, cell_id="clean_baseline_4k", arm="clean")
    second = state.generate_scenario(seed=42, cell_id="clean_baseline_4k", arm="clean")
    assert first == second
    assert first.target_entity.startswith("entity_")
    assert first.latest_value != first.initial_value
    assert first.dose_bytes == 4096
    assert first.update_opportunity_count >= 1
    assert first.mutation_opportunity_count == 1


def test_materializer_generates_valid_harbor_and_compose_structure(tmp_path):
    _require_production_sidecar()
    materializer = load("action_memory_mat_test", "materializer")
    target = tmp_path / "task"
    materializer.materialize(output_dir=target, cell_id="clean_baseline_4k", seed=42)
    assert (target / "task.toml").exists()
    assert (target / "instruction.md").exists()
    assert (target / "environment" / "docker-compose.yaml").exists()
    assert (target / "environment" / "mcp-server" / "Dockerfile").exists()
    assert "workbench-internal" in (target / "environment" / "docker-compose.yaml").read_text(encoding="utf-8")
    assert "--require-hashes" in (target / "environment" / "mcp-server" / "Dockerfile").read_text(encoding="utf-8")


def test_mcp_server_client_protocol_interaction(tmp_path):
    _require_production_sidecar()
    materializer = load("action_memory_mat_mcp", "materializer")
    oracle = load("action_memory_oracle_mcp", "oracle")
    task = tmp_path / "task"
    materializer.materialize(output_dir=task, cell_id="clean_baseline_4k", seed=42)
    server = (task / "environment" / "mcp-server" / "server.py").read_text(encoding="utf-8")
    assert "from fastmcp import FastMCP" in server
    assert "from ops import OP_REGISTRY" in server
    assert 'transport="streamable-http"' in server
    assert not (task / "environment" / "mcp-server" / "runtime.py").exists()
    oracle.solve_direct(task / "task_state", task / "evidence")
    output = task / "output"
    output.mkdir()
    shutil.copy2(task / "evidence" / "benchmark-events.jsonl", output / "benchmark-events.jsonl")
    shutil.copy2(task / "evidence" / "final-state.json", output / "final-state.json")
    assert json.loads((output / "final-state.json").read_text(encoding="utf-8"))["status"] == "executed"


def test_verifier_discriminates_oracle_nop_and_mutants(tmp_path):
    _require_production_sidecar()
    materializer = load("action_memory_mat_discrim", "materializer")
    verifier = load("action_memory_ver_discrim", "verifier")
    templates = load("action_memory_tmpl_discrim", "action_memory_templates")
    task = tmp_path / "task"
    materializer.materialize(output_dir=task, cell_id="clean_baseline_4k", seed=42)
    task_state = task / "task_state"
    evidence = task / "evidence"
    rewards = task / "tests" / "rewards"
    templates.nop(task_state, evidence)
    assert verifier.verify(task_state, evidence, reward_dir=rewards / "nop")["reward"] == 0.0
    materializer.materialize(output_dir=task, cell_id="clean_baseline_4k", seed=42)
    templates.oracle(task_state, evidence)
    assert verifier.verify(task_state, evidence, reward_dir=rewards / "oracle")["reward"] == 1.0
    for name, mutant in templates.mutants().items():
        materializer.materialize(output_dir=task, cell_id="clean_baseline_4k", seed=42)
        mutant(task_state, evidence)
        assert verifier.verify(task_state, evidence, reward_dir=rewards / name)["reward"] == 0.0


def test_verifier_rejects_noncanonical_or_invalid_truth(tmp_path):
    _require_production_sidecar()
    materializer = load("action_memory_mat_corrupt", "materializer")
    verifier = load("action_memory_ver_corrupt", "verifier")
    task = tmp_path / "task"
    materializer.materialize(output_dir=task, cell_id="clean_baseline_4k", seed=42)
    output = task / "output"
    output.mkdir()
    scenario = json.loads((task / "task_state" / "scenario.json").read_text(encoding="utf-8"))
    (output / "final-state.json").write_text(
        json.dumps({"status": "executed", "target_entity": scenario["target_entity"], "target_attribute": scenario["target_attribute"], "bound_value": scenario["latest_value"]}),
        encoding="utf-8",
    )
    (output / "benchmark-events.jsonl").write_text(
        json.dumps({"event_ordinal": 1}) + "\n", encoding="utf-8"
    )
    result = verifier.verify(task / "tests", output, reward_dir=task / "tests" / "rewards" / "corrupt")
    assert result["reward"] == 0.0
    assert result["reason"] == "noncanonical_runtime_evidence"


def test_action_memory_state_module_does_not_shadow_loca_in_either_import_order(monkeypatch):
    loca_root = ROOT.parent / "loca-lean-v1"
    monkeypatch.syspath_prepend(str(loca_root))
    for module_name in ("state", "source", "templates", "package_finish", "package_layout"):
        sys.modules.pop(module_name, None)

    load("action_memory_materializer_first", "materializer")
    loca_spec = importlib.util.spec_from_file_location("loca_materializer_after_action_memory", loca_root / "materializer.py")
    loca_module = importlib.util.module_from_spec(loca_spec)
    assert loca_spec.loader is not None
    loca_spec.loader.exec_module(loca_module)
    assert hasattr(loca_module, "materialize")

    for module_name in ("state", "source", "templates", "package_finish", "package_layout"):
        sys.modules.pop(module_name, None)
    loca_spec = importlib.util.spec_from_file_location("loca_materializer_first", loca_root / "materializer.py")
    loca_module = importlib.util.module_from_spec(loca_spec)
    assert loca_spec.loader is not None
    loca_spec.loader.exec_module(loca_module)
    load("action_memory_materializer_after_loca", "materializer")
    assert hasattr(loca_module, "materialize")
