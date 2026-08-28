from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1] / "library" / "benchmarks" / "action-memory-v1"
sys.path.insert(0, str(ROOT))


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


def test_state_generation_deterministic_and_dosed():
    state_mod = load("state_test", "state")
    spec1 = state_mod.generate_scenario(seed=42, cell_id="clean_baseline_4k", arm="clean")
    spec2 = state_mod.generate_scenario(seed=42, cell_id="clean_baseline_4k", arm="clean")
    assert spec1 == spec2
    assert spec1.target_entity.startswith("entity_")
    assert spec1.latest_value != spec1.initial_value
    assert spec1.dose_bytes > 0


def test_materializer_generates_valid_harbor_structure(tmp_path):
    mat_mod = load("mat_test", "materializer")
    target = tmp_path / "action_mem_task"
    manifest = mat_mod.materialize(output_dir=target, cell_id="clean_baseline_4k", seed=42)
    assert (target / "task.toml").exists()
    assert (target / "instruction.md").exists()
    assert (target / "environment" / "Dockerfile").exists()
    assert (target / "verifier" / "verify.py").exists()
    assert (target / "solution" / "solve.sh").exists()
    assert (target / "workbench" / "adversarial" / "stale-value.sh").exists()


def test_verifier_discriminates_oracle_nop_and_mutants(tmp_path):
    mat_mod = load("mat_discrim", "materializer")
    ver_mod = load("ver_discrim", "verifier")
    tmpl_mod = load("tmpl_discrim", "templates")

    target = tmp_path / "task_for_discrim"
    mat_mod.materialize(output_dir=target, cell_id="clean_baseline_4k", seed=42)
    task_dir = target / "task_state"
    evidence_dir = target / "evidence"
    rewards = target / "tests" / "rewards"

    # NOP should score 0.0
    tmpl_mod.nop(task_dir, evidence_dir)
    res_nop = ver_mod.verify(task_dir, evidence_dir, reward_dir=rewards / "nop")
    assert res_nop["reward"] == 0.0

    # Oracle should score 1.0
    mat_mod.materialize(output_dir=target, cell_id="clean_baseline_4k", seed=42)
    tmpl_mod.oracle(task_dir, evidence_dir)
    res_oracle = ver_mod.verify(task_dir, evidence_dir, reward_dir=rewards / "oracle")
    assert res_oracle["reward"] == 1.0

    # Mutants should score 0.0
    for name, mutant in tmpl_mod.mutants().items():
        mat_mod.materialize(output_dir=target, cell_id="clean_baseline_4k", seed=42)
        mutant(task_dir, evidence_dir)
        res_mutant = ver_mod.verify(task_dir, evidence_dir, reward_dir=rewards / name)
        assert res_mutant["reward"] == 0.0, f"Mutant {name} must yield reward 0.0"
