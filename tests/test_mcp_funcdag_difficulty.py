"""Focused behavioral tests for mcp-funcdag-v1 certified difficulty axis and verifier diagnostics."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BENCH_ROOT = Path(__file__).parents[1] / "library" / "benchmarks" / "mcp-funcdag-v1"


def _load_module(name: str):
    module_name = f"mcp_funcdag_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    orig_path = list(sys.path)
    sys.path.insert(0, str(BENCH_ROOT))
    try:
        spec = importlib.util.spec_from_file_location(module_name, BENCH_ROOT / f"{name}.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path[:] = orig_path
        for generic_name in ("contract", "dag_generator", "materializer", "runtime", "templates", "verifier"):
            mod = sys.modules.get(generic_name)
            if mod is not None and getattr(mod, "__file__", "").startswith(str(BENCH_ROOT)):
                del sys.modules[generic_name]


def test_difficulty_axis_structure_and_determinism():
    """Each difficulty level generates distinct structural extras deterministically."""
    dag_gen = _load_module("dag_generator")

    # Level 0: none (baseline)
    spec_none = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2, difficulty="none")
    assert len(spec_none.alias_tools) == 0
    assert len(spec_none.near_collision_tools) == 0
    assert len(spec_none.dead_end_tools) == 0
    assert len(spec_none.dead_end_nodes) == 0
    assert len(spec_none.nodes) >= 5

    # Level 1: aliases
    spec_alias = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2, difficulty="aliases")
    assert len(spec_alias.alias_tools) > 0
    assert len(spec_alias.near_collision_tools) == 0
    assert len(spec_alias.dead_end_tools) == 0
    # Aliases must be marked as distractors
    for at in spec_alias.alias_tools:
        assert at.is_distractor is True
        assert at.op_kind == "distractor"

    # Level 2: near_collision (cumulative: includes aliases + near-collisions)
    spec_nc = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2, difficulty="near_collision")
    assert len(spec_nc.alias_tools) > 0
    assert len(spec_nc.near_collision_tools) > 0
    assert len(spec_nc.dead_end_tools) == 0
    for nct in spec_nc.near_collision_tools:
        assert nct.is_distractor is True
        assert nct.op_kind == "distractor"

    # Level 3: dead_end (cumulative: includes aliases + near-collisions + dead-end branches)
    spec_de = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2, difficulty="dead_end")
    assert len(spec_de.alias_tools) > 0
    assert len(spec_de.near_collision_tools) > 0
    assert len(spec_de.dead_end_tools) == 2
    assert len(spec_de.dead_end_nodes) == 4

    # Determinism: same seed and difficulty produces identical outputs
    spec_de_repeat = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2, difficulty="dead_end")
    assert [t.name for t in spec_de.tools] == [t.name for t in spec_de_repeat.tools]
    assert [n.node_id for n in spec_de.nodes] == [n.node_id for n in spec_de_repeat.nodes]
    assert [dn.node_id for dn in spec_de.dead_end_nodes] == [dn.node_id for dn in spec_de_repeat.dead_end_nodes]
    assert spec_de.expected_target_value == spec_de_repeat.expected_target_value


def test_difficulty_preserves_unique_correct_dag():
    """The required DAG, topological order, and reference values are invariant to difficulty."""
    dag_gen = _load_module("dag_generator")

    spec_none = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2, difficulty="none")
    for diff in ("aliases", "near_collision", "dead_end"):
        spec_diff = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2, difficulty=diff)
        # The true required DAG must be identical across difficulty levels for the same seed
        assert spec_diff.target_node_id == spec_none.target_node_id
        assert spec_diff.expected_target_value == spec_none.expected_target_value
        assert spec_diff.topological_order == spec_none.topological_order
        assert [n.node_id for n in spec_diff.nodes] == [n.node_id for n in spec_none.nodes]
        assert spec_diff.reference_node_values == spec_none.reference_node_values
        assert spec_diff.node_expected_calls == spec_none.node_expected_calls

        # Dead-end nodes are excluded from the required sequence
        for dn in spec_diff.dead_end_nodes:
            assert dn.node_id not in spec_diff.topological_order
            assert dn.node_id not in spec_diff.node_expected_calls


def test_difficulty_factors_and_source_digest_binding(tmp_path):
    """Difficulty factors and source digest are bound to the task contract and metadata."""
    contract_mod = _load_module("contract")
    materializer_mod = _load_module("materializer")

    # Materialize two tasks varying ONLY difficulty
    cell_none = {"name": "test_diff_none", "depth": 3, "width": 2, "distractor_count": 2, "name_similarity": "low", "schema_token_volume": "concise", "schema_drift": False, "difficulty": "none", "seed": 42}
    cell_de = {"name": "test_diff_de", "depth": 3, "width": 2, "distractor_count": 2, "name_similarity": "low", "schema_token_volume": "concise", "schema_drift": False, "difficulty": "dead_end", "seed": 42}

    dir_none = materializer_mod.materialize_task(cell_none, output_root=tmp_path / "out_none")
    dir_de = materializer_mod.materialize_task(cell_de, output_root=tmp_path / "out_de")

    # Contracts must bind the declared difficulty factor
    contract_none = json.loads((dir_none / "benchmark_contract.json").read_text(encoding="utf-8"))
    contract_de = json.loads((dir_de / "benchmark_contract.json").read_text(encoding="utf-8"))
    assert contract_none["cell_factors"]["difficulty"] == "none"
    assert contract_de["cell_factors"]["difficulty"] == "dead_end"

    # Source digests must be distinct because difficulty factors differ
    digest_none = (dir_none / "source_digest.txt").read_text(encoding="utf-8").strip()
    digest_de = (dir_de / "source_digest.txt").read_text(encoding="utf-8").strip()
    assert digest_none != digest_de
    assert len(digest_none) == 16
    assert len(digest_de) == 16

    # Task.toml metadata must bind difficulty_level and source_digest
    task_toml_none = (dir_none / "task.toml").read_text(encoding="utf-8")
    task_toml_de = (dir_de / "task.toml").read_text(encoding="utf-8")
    assert 'difficulty_level = "none"' in task_toml_none
    assert f'source_digest = "{digest_none}"' in task_toml_none
    assert 'difficulty_level = "dead_end"' in task_toml_de
    assert f'source_digest = "{digest_de}"' in task_toml_de

    # Difficulty ladder is exported
    assert len(contract_mod.DIFFICULTY_CELLS) == 12  # 4 levels x 3 seeds


def test_oracle_solves_all_difficulty_levels(tmp_path):
    """Oracle solver achieves reward 1.0 across every structural difficulty level."""
    materializer_mod = _load_module("materializer")
    runtime_mod = _load_module("runtime")
    templates_mod = _load_module("templates")
    verifier_mod = _load_module("verifier")

    for diff in ("none", "aliases", "near_collision", "dead_end"):
        cell = {
            "name": f"oracle_{diff}",
            "depth": 3,
            "width": 2,
            "distractor_count": 2,
            "name_similarity": "low",
            "schema_token_volume": "concise",
            "schema_drift": False,
            "difficulty": diff,
            "seed": 42,
        }
        task_dir = materializer_mod.materialize_task(cell, output_root=tmp_path / f"task_{diff}")
        spec_data = json.loads((task_dir / "environment" / "runtime_tools.json").read_text())
        truth_path = task_dir / "tests" / "fixtures" / "verifier_truth.json"
        evidence_dir = tmp_path / f"evidence_{diff}"
        workspace_dir = tmp_path / f"workspace_{diff}"

        runtime = runtime_mod.MCPRuntime(spec_data, evidence_dir)
        templates_mod.run_oracle_solve(runtime, spec_data, workspace_dir)

        res = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, workspace_dir)
        assert res["reward"] == 1.0, f"Oracle failed for difficulty={diff}: {res}"
        assert res["artifact_format_ok"] is True
        assert res["artifact_format_error"] is None
        assert res["value_match"] is True
        assert res["dag_structure_ok"] is True
        assert res["dag_conformance"] is True
        assert res["value_propagation_accuracy"] == 1.0


def test_negative_control_format_rejection(tmp_path):
    """Verifier rejects malformed result.json with artifact_format_ok=False and distinct error tags."""
    materializer_mod = _load_module("materializer")
    verifier_mod = _load_module("verifier")

    cell = {
        "name": "format_controls",
        "depth": 3,
        "width": 2,
        "distractor_count": 2,
        "name_similarity": "low",
        "schema_token_volume": "concise",
        "schema_drift": False,
        "difficulty": "dead_end",
        "seed": 42,
    }
    task_dir = materializer_mod.materialize_task(cell, output_root=tmp_path / "task_fmt")
    truth_path = task_dir / "tests" / "fixtures" / "verifier_truth.json"
    evidence_dir = tmp_path / "evidence_fmt"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # 1. Missing result.json
    ws_missing = tmp_path / "ws_missing"
    ws_missing.mkdir(parents=True, exist_ok=True)
    res_missing = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, ws_missing)
    assert res_missing["reward"] == 0.0
    assert res_missing["artifact_format_ok"] is False
    assert res_missing["artifact_format_error"] == "missing_result_file"

    # 2. Invalid JSON syntax
    ws_bad_json = tmp_path / "ws_bad_json"
    ws_bad_json.mkdir(parents=True, exist_ok=True)
    (ws_bad_json / "result.json").write_text("{broken json", encoding="utf-8")
    res_bad_json = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, ws_bad_json)
    assert res_bad_json["reward"] == 0.0
    assert res_bad_json["artifact_format_ok"] is False
    assert "invalid_json" in res_bad_json["artifact_format_error"]

    # 3. Not a JSON object (e.g. JSON array)
    ws_array = tmp_path / "ws_array"
    ws_array.mkdir(parents=True, exist_ok=True)
    (ws_array / "result.json").write_text("[1, 2, 3]", encoding="utf-8")
    res_array = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, ws_array)
    assert res_array["reward"] == 0.0
    assert res_array["artifact_format_ok"] is False
    assert res_array["artifact_format_error"] == "not_a_json_object"

    # 4. Missing target_value key
    ws_no_key = tmp_path / "ws_no_key"
    ws_no_key.mkdir(parents=True, exist_ok=True)
    (ws_no_key / "result.json").write_text('{"other_key": 42}', encoding="utf-8")
    res_no_key = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, ws_no_key)
    assert res_no_key["reward"] == 0.0
    assert res_no_key["artifact_format_ok"] is False
    assert res_no_key["artifact_format_error"] == "missing_target_value_key"

    # 5. Non-integer target_value (string, boolean, float)
    for bad_val in ('"42"', 'true', '42.5'):
        ws_bad_type = tmp_path / f"ws_bad_type_{bad_val.strip('\"')}"
        ws_bad_type.mkdir(parents=True, exist_ok=True)
        (ws_bad_type / "result.json").write_text(f'{{"target_value": {bad_val}}}', encoding="utf-8")
        res_bad_type = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, ws_bad_type)
        assert res_bad_type["reward"] == 0.0
        assert res_bad_type["artifact_format_ok"] is False
        assert res_bad_type["artifact_format_error"] == "non_integer_target_value"


def test_negative_control_format_vs_value_rejection(tmp_path):
    """Verifier distinguishes valid artifact format with wrong value from format failure."""
    materializer_mod = _load_module("materializer")
    verifier_mod = _load_module("verifier")

    cell = {
        "name": "value_control",
        "depth": 3,
        "width": 2,
        "distractor_count": 2,
        "name_similarity": "low",
        "schema_token_volume": "concise",
        "schema_drift": False,
        "difficulty": "aliases",
        "seed": 42,
    }
    task_dir = materializer_mod.materialize_task(cell, output_root=tmp_path / "task_val")
    truth_path = task_dir / "tests" / "fixtures" / "verifier_truth.json"
    evidence_dir = tmp_path / "evidence_val"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # Valid JSON format with wrong integer value: format_ok is True, but value_match is False
    ws_wrong_val = tmp_path / "ws_wrong_val"
    ws_wrong_val.mkdir(parents=True, exist_ok=True)
    (ws_wrong_val / "result.json").write_text('{"target_value": -999999}', encoding="utf-8")

    res = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, ws_wrong_val)
    assert res["reward"] == 0.0
    assert res["artifact_format_ok"] is True
    assert res["artifact_format_error"] is None
    assert res["value_match"] is False
    assert res["agent_target_value"] == -999999


def test_negative_control_dead_end_and_mutants_rejected(tmp_path):
    """Calling dead-end branches or executing mutants fails verification at max difficulty."""
    materializer_mod = _load_module("materializer")
    runtime_mod = _load_module("runtime")
    templates_mod = _load_module("templates")
    verifier_mod = _load_module("verifier")

    cell = {
        "name": "mutants_max_diff",
        "depth": 3,
        "width": 2,
        "distractor_count": 2,
        "name_similarity": "low",
        "schema_token_volume": "concise",
        "schema_drift": False,
        "difficulty": "dead_end",
        "seed": 42,
    }
    task_dir = materializer_mod.materialize_task(cell, output_root=tmp_path / "task_mut")
    spec_data = json.loads((task_dir / "environment" / "runtime_tools.json").read_text())
    truth_path = task_dir / "tests" / "fixtures" / "verifier_truth.json"

    # All standard mutants must score 0.0 at difficulty=dead_end
    for mutant_name, mutant_fn in templates_mod.get_mutants().items():
        ev_dir = tmp_path / f"ev_{mutant_name}"
        ws_dir = tmp_path / f"ws_{mutant_name}"
        runtime = runtime_mod.MCPRuntime(spec_data, ev_dir)
        mutant_fn(runtime, spec_data, ws_dir)
        res = verifier_mod.verify_execution(task_dir, truth_path, ev_dir, ws_dir)
        assert res["reward"] == 0.0, f"Mutant {mutant_name} scored non-zero: {res}"
        assert res["dag_structure_ok"] is False

    # Calling dead-end branch tool before oracle sequence must fail due to extra call
    ev_dead = tmp_path / "ev_dead_call"
    ws_dead = tmp_path / "ws_dead_call"
    runtime = runtime_mod.MCPRuntime(spec_data, ev_dead)
    dead_tool = next(t for t in spec_data["tools"] if "dead_end" in t["name"])
    runtime.call_tool(dead_tool["name"], {"a": 2, "b": 2})
    templates_mod.run_oracle_solve(runtime, spec_data, ws_dead)
    res_dead = verifier_mod.verify_execution(task_dir, truth_path, ev_dead, ws_dead)
    assert res_dead["reward"] == 0.0
    assert res_dead["dag_structure_ok"] is False
    assert res_dead["dag_conformance"] is False
