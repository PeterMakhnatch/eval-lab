"""Unit tests for the cleanroom Synthetic Tool + Working Memory Environment Generator."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from evallab.synthetic_tool_memory import (
    HARBOR_CANARY_GUID,
    DifficultyAnchorResult,
    HintRegretResult,
    SyntheticToolMemorySpec,
    compute_canonical_digest,
    compute_difficulty_score,
    compute_hint_regret,
    create_tool_memory_env,
    export_to_harbor_task,
    generate_tool_memory_env_code,
    generate_tool_memory_env_spec,
    harbor_task_identity,
    validate_ast_safety,
    validate_ast_syntax,
    validate_environment_lifecycle,
    validate_multi_seed_determinism,
    validate_synthetic_tool_memory_environment,
)

# --- Contract & Utility Tests ------------------------------------------------


def test_harbor_task_identity_normalization() -> None:
    """Test normalization and validation of Harbor task names."""
    assert harbor_task_identity("syn-toolmem-easy") == "evallab/syn-toolmem-easy"
    assert harbor_task_identity("custom-org/syn-toolmem-01") == "custom-org/syn-toolmem-01"

    with pytest.raises(ValueError, match="non-empty string"):
        harbor_task_identity("")

    with pytest.raises(ValueError, match="whitespace"):
        harbor_task_identity("syn toolmem")

    with pytest.raises(ValueError, match="exactly one"):
        harbor_task_identity("org/sub/task")


def test_canonical_digest_deterministic() -> None:
    """Test that canonical digest is deterministic regardless of dict key ordering."""
    d1 = {"b": 2, "a": 1, "c": [1, 2, 3]}
    d2 = {"a": 1, "c": [1, 2, 3], "b": 2}
    assert compute_canonical_digest(d1) == compute_canonical_digest(d2)
    assert compute_canonical_digest(d1).startswith("sha256:")
    assert len(compute_canonical_digest(d1)) == 71


# --- Scenario Generator Tests ------------------------------------------------


@pytest.mark.parametrize(
    "scenario_type",
    [
        "entity_reconciliation",
        "ledger_transaction",
        "cache_invalidation",
        "generic_pipeline",
    ],
)
def test_generate_tool_memory_env_spec_scenarios(scenario_type: str) -> None:
    """Verify spec generator generates valid specifications for all supported scenarios."""
    spec = generate_tool_memory_env_spec(
        scenario_type=scenario_type,
        depth=3,
        branching_factor=4,
        memory_slots_count=4,
        distractor_tools_count=2,
        distractor_slots_count=2,
        seed=101,
        difficulty="medium",
    )

    assert isinstance(spec, SyntheticToolMemorySpec)
    assert spec.scenario_type == scenario_type
    assert spec.seed == 101
    assert len(spec.tools) >= 3
    assert len(spec.memory_slots) >= 2
    assert len(spec.oracle_actions) >= 2
    assert spec.spec_id.startswith("sha256:")
    assert spec.distractor_tools_count == 2
    assert spec.distractor_slots_count == 2

    # Check distractor flag assignments
    distractor_tools = [t for t in spec.tools if t.is_distractor]
    assert len(distractor_tools) == 2


def test_generator_respects_depth_and_parameters() -> None:
    """Verify generator scales max_steps, tools, and slots with input parameters."""
    spec_easy = generate_tool_memory_env_spec(
        depth=2,
        distractor_tools_count=0,
        distractor_slots_count=0,
        difficulty="easy",
    )
    spec_hard = generate_tool_memory_env_spec(
        depth=6,
        distractor_tools_count=3,
        distractor_slots_count=3,
        difficulty="hard",
    )

    assert spec_hard.distractor_tools_count > spec_easy.distractor_tools_count
    assert spec_hard.distractor_slots_count > spec_easy.distractor_slots_count
    assert len(spec_hard.tools) > len(spec_easy.tools)


# --- AST Validation Tests ---------------------------------------------------


def test_validate_ast_syntax_valid() -> None:
    """Test AST syntax validator on freshly generated environment code."""
    spec = generate_tool_memory_env_spec(seed=42)
    code = generate_tool_memory_env_code(spec)

    result = validate_ast_syntax(code)
    assert result.passed is True
    assert result.stage == "ast_syntax"
    assert len(result.errors) == 0
    assert "reset" in result.metadata["methods_found"]
    assert "step" in result.metadata["methods_found"]


def test_validate_ast_syntax_invalid_code() -> None:
    """Test AST syntax validator with malformed Python code."""
    bad_code = "class SyntheticToolMemoryEnv:\n    def broken_syntax( self:"
    result = validate_ast_syntax(bad_code)
    assert result.passed is False
    assert any("Syntax error" in err for err in result.errors)


def test_validate_ast_syntax_missing_methods() -> None:
    """Test AST syntax validator when required methods are absent."""
    incomplete_code = "class SyntheticToolMemoryEnv:\n    def __init__(self):\n        pass\n"
    result = validate_ast_syntax(incomplete_code)
    assert result.passed is False
    assert any("Missing required methods" in err for err in result.errors)


def test_validate_ast_safety_clean() -> None:
    """Test that generated environment code passes safety validation."""
    spec = generate_tool_memory_env_spec(seed=42)
    code = generate_tool_memory_env_code(spec)

    result = validate_ast_safety(code)
    assert result.passed is True
    assert len(result.errors) == 0


@pytest.mark.parametrize(
    "forbidden_snippet,expected_err",
    [
        ("import os", "Forbidden import: 'os'"),
        ("import subprocess", "Forbidden import: 'subprocess'"),
        ("from sys import exit", "Forbidden from-import: 'sys'"),
        ("import socket", "Forbidden import: 'socket'"),
        ("eval('2+2')", "Forbidden builtin call: 'eval'"),
        ("exec('print(1)')", "Forbidden builtin call: 'exec'"),
        ("__import__('os')", "Forbidden builtin call: '__import__'"),
    ],
)
def test_validate_ast_safety_rejects_dangerous_constructs(
    forbidden_snippet: str, expected_err: str
) -> None:
    """Test that AST safety validator catches forbidden imports and dangerous builtins."""
    dangerous_code = f"""
class SyntheticToolMemoryEnv:
    def __init__(self):
        {forbidden_snippet}
    def reset(self): pass
    def step(self, action): pass
    def get_tools(self): pass
    def is_goal_reached(self): pass
    def get_oracle_trajectory(self): pass
"""
    result = validate_ast_safety(dangerous_code)
    assert result.passed is False
    assert any(expected_err in err for err in result.errors)


# --- Isolated Lifecycle Execution Validation Tests ---------------------------


def test_environment_lifecycle_validation_success() -> None:
    """Verify that a valid generated environment passes all lifecycle checks."""
    for scenario in ["entity_reconciliation", "ledger_transaction", "cache_invalidation"]:
        spec = generate_tool_memory_env_spec(scenario_type=scenario, seed=77)
        code = generate_tool_memory_env_code(spec)

        result = validate_environment_lifecycle(code, spec)
        assert result.passed is True, f"Lifecycle failed for {scenario}: {result.errors}"
        assert len(result.errors) == 0


def test_create_tool_memory_env_runtime() -> None:
    """Verify dynamic instantiation and interaction with the Gym MDP environment."""
    spec = generate_tool_memory_env_spec(scenario_type="entity_reconciliation", seed=42)
    env = create_tool_memory_env(spec)

    # 1. Reset
    obs, info = env.reset(seed=42)
    assert "observation" in obs
    assert "working_memory" in obs
    assert info["goal_reached"] is False

    # 2. Get tools and oracle
    tools = env.get_tools()
    assert len(tools) == len(spec.tools)
    oracle = env.get_oracle_trajectory()
    assert len(oracle) == len(spec.oracle_actions)

    # 3. Step through oracle
    for act in oracle:
        obs, reward, term, trunc, sinfo = env.step(act)

    assert term is True
    assert reward == 1.0
    assert env.is_goal_reached() is True
    assert "Goal successfully achieved" in obs["observation"]

    # 4. Render
    rendered = env.render()
    assert "Goal Met: True" in rendered


def test_environment_noop_does_not_succeed() -> None:
    """Verify that stepping no-ops never triggers spurious rewards."""
    spec = generate_tool_memory_env_spec(scenario_type="ledger_transaction", seed=42)
    env = create_tool_memory_env(spec)
    obs, info = env.reset()

    total_reward = 0.0
    for _ in range(spec.max_steps + 5):
        obs, reward, term, trunc, _ = env.step("noop")
        total_reward += reward
        if term or trunc:
            break

    assert total_reward == 0.0
    assert env.is_goal_reached() is False


def test_environment_malformed_actions_handled_safely() -> None:
    """Verify environment handles malformed, string, JSON, and dict actions gracefully."""
    spec = generate_tool_memory_env_spec(scenario_type="cache_invalidation", seed=42)
    env = create_tool_memory_env(spec)
    env.reset()

    # Empty string
    obs, r, _, _, info = env.step("")
    assert r == 0.0
    assert "empty string action" in info.get("error", "")

    # Invalid JSON string
    obs, r, _, _, info = env.step("{invalid_json:}")
    assert r == 0.0
    assert "invalid JSON" in info.get("error", "")

    # Unknown tool
    obs, r, _, _, info = env.step({"tool": "unknown_tool_xyz"})
    assert r == 0.0
    assert info.get("error") == "unknown_tool"

    # Missing required argument
    obs, r, _, _, info = env.step({"tool": "drain_traffic", "arguments": {}})
    assert r == 0.0
    assert "missing_parameter" in info.get("error", "")


# --- Multi-Seed Determinism Tests -------------------------------------------


def test_validate_multi_seed_determinism() -> None:
    """Verify determinism validator detects consistent trajectories across seeds."""
    spec = generate_tool_memory_env_spec(scenario_type="entity_reconciliation", seed=42)
    result = validate_multi_seed_determinism(spec, seeds=(1, 42, 999))
    assert result.passed is True
    assert len(result.errors) == 0


def test_full_pipeline_validation() -> None:
    """Verify complete 4-stage validation pipeline on generated environment."""
    spec = generate_tool_memory_env_spec(scenario_type="ledger_transaction", seed=123)
    results = validate_synthetic_tool_memory_environment(spec)

    assert results["ast_syntax"].passed is True
    assert results["ast_safety"].passed is True
    assert results["lifecycle_execution"].passed is True
    assert results["multi_seed_determinism"].passed is True
    assert results["full_pipeline"].passed is True


# --- SPADE Hint-Based Regret Tests ------------------------------------------


def test_compute_hint_regret_at_capability_boundary() -> None:
    """Test hint regret calculation when task is at the capability boundary."""
    # Hinted scores high (0.9), unhinted struggles (0.2) -> Regret = 0.7 >= 0.15
    hinted = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0]  # 0.9
    unhinted = [0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0]  # 0.2

    res = compute_hint_regret(hinted, unhinted, spec_id="test-spec-01", threshold=0.15)
    assert isinstance(res, HintRegretResult)
    assert res.hinted_mean_reward == 0.9
    assert res.unhinted_mean_reward == 0.2
    assert res.hint_regret == 0.7
    assert res.at_capability_boundary is True
    assert res.difficulty_band == "hard"


def test_compute_hint_regret_easy_and_frontier() -> None:
    """Test hint regret bands for easy (unhinted high) and frontier (both low) tasks."""
    # Easy task: unhinted succeeds high
    easy_res = compute_hint_regret([1.0, 1.0], [0.95, 0.90], threshold=0.15)
    assert easy_res.difficulty_band == "easy"
    assert easy_res.at_capability_boundary is False

    # Frontier task: both struggle
    frontier_res = compute_hint_regret([0.1, 0.2], [0.0, 0.0], threshold=0.15)
    assert frontier_res.difficulty_band == "frontier"


def test_compute_hint_regret_empty_raises() -> None:
    """Test that empty input lists raise ValueError."""
    with pytest.raises(ValueError, match="requires non-empty"):
        compute_hint_regret([], [1.0])


# --- Difficulty Anchor Scoring Tests ----------------------------------------


def test_compute_difficulty_score() -> None:
    """Test multidimensional difficulty anchor computation and scaling."""
    spec = generate_tool_memory_env_spec(
        depth=5,
        branching_factor=4,
        distractor_tools_count=2,
        distractor_slots_count=2,
        seed=42,
    )
    diff = compute_difficulty_score(spec)

    assert isinstance(diff, DifficultyAnchorResult)
    assert 1.0 <= diff.depth_score <= 10.0
    assert 1.0 <= diff.branching_score <= 10.0
    assert 1.0 <= diff.memory_load_score <= 10.0
    assert 1.0 <= diff.distractor_pressure_score <= 10.0
    assert 0.0 <= diff.aggregate_difficulty_index <= 1.0
    assert diff.difficulty_category in {"easy", "medium", "hard", "frontier"}
    assert diff.min_steps == len(spec.oracle_actions)


# --- Harbor Task Export Tests ------------------------------------------------


def test_export_to_harbor_task(tmp_path: Path) -> None:
    """Verify that export_to_harbor_task generates a valid, self-contained Harbor task package."""
    spec = generate_tool_memory_env_spec(
        scenario_type="entity_reconciliation",
        seed=42,
        task_name="syn-toolmem-reconcile-01",
    )
    task_dir = tmp_path / "harbor_task_output"

    out_dir = export_to_harbor_task(spec, task_dir)
    assert out_dir.exists()

    # 1. task.toml check
    task_toml = out_dir / "task.toml"
    assert task_toml.exists()
    toml_text = task_toml.read_text(encoding="utf-8")
    assert HARBOR_CANARY_GUID in toml_text
    assert "evallab/syn-toolmem-reconcile-01" in toml_text
    assert 'schema_version = "1.4"' in toml_text

    # 2. instructions check
    assert (out_dir / "instruction.md").exists()
    assert (out_dir / "instruction.txt").exists()
    instr_text = (out_dir / "instruction.md").read_text(encoding="utf-8")
    assert "fetch_customer_record" in instr_text
    assert "Target Goal State" in instr_text

    # 3. environment/
    env_dir = out_dir / "environment"
    assert (env_dir / "Dockerfile").exists()
    assert (env_dir / "env.py").exists()
    assert (env_dir / "task_spec.json").exists()

    # 4. verifier/ & tests/
    assert (out_dir / "verifier" / "verify.py").exists()
    assert (out_dir / "tests" / "test_env.py").exists()
    test_sh = out_dir / "test.sh"
    assert test_sh.exists()
    assert os.access(test_sh, os.X_OK)

    # 5. solution/ & oracle/
    assert (out_dir / "oracle" / "solve.py").exists()
    assert (out_dir / "solution" / "solve.py").exists()
    solve_sh = out_dir / "solve.sh"
    assert solve_sh.exists()
    assert os.access(solve_sh, os.X_OK)

    # 6. workbench/
    probe_sh = out_dir / "workbench" / "test_baseline.sh"
    assert probe_sh.exists()
    assert os.access(probe_sh, os.X_OK)


def test_lifecycle_validation_detects_failing_oracle() -> None:
    """Verify that lifecycle validator flags when oracle fails to satisfy target state."""
    spec = generate_tool_memory_env_spec(scenario_type="entity_reconciliation", seed=42)
    # Intentionally corrupt target state so oracle cannot satisfy it
    corrupted_dict = spec.model_dump(mode="json")
    corrupted_dict["target_state"]["reconciled_balance"] = 99999999
    corrupted_dict["spec_id"] = compute_canonical_digest(corrupted_dict)
    corrupted_spec = SyntheticToolMemorySpec.model_validate(corrupted_dict)

    result = validate_environment_lifecycle(generate_tool_memory_env_code(corrupted_spec), corrupted_spec)
    assert result.passed is False
    assert any("Oracle execution failed to achieve goal" in err for err in result.errors)


def test_lifecycle_validation_rejects_invalid_env_type() -> None:
    """Verify that lifecycle validator rejects invalid non-environment objects."""
    spec = generate_tool_memory_env_spec(seed=42)
    result = validate_environment_lifecycle(12345, spec)
    assert result.passed is False
    assert any("Invalid environment object" in err for err in result.errors)


def test_multi_seed_determinism_catches_non_determinism() -> None:
    """Verify determinism validator detects random non-deterministic transitions."""
    spec = generate_tool_memory_env_spec(seed=42)
    # Create non-deterministic code
    broken_code = """
import random

class SyntheticToolMemoryEnv:
    def __init__(self, config=None): pass
    def reset(self, seed=None):
        return {"observation": "init", "working_memory": {}, "val": random.random()}, {}
    def step(self, action):
        return {"observation": "step", "working_memory": {}, "val": random.random()}, 0.0, False, False, {}
    def get_tools(self): return []
    def get_oracle_trajectory(self): return []
    def is_goal_reached(self): return False
"""
    result_broken = validate_multi_seed_determinism(spec, seeds=(1, 2), code=broken_code)
    assert result_broken.passed is False
    assert any("Non-deterministic" in err for err in result_broken.errors)

    result_valid = validate_multi_seed_determinism(spec, seeds=(1, 2))
    assert result_valid.passed is True

def test_distractor_tool_behavior() -> None:
    """Verify distractor tools execute safely without advancing goal state."""
    spec = generate_tool_memory_env_spec(
        scenario_type="entity_reconciliation",
        distractor_tools_count=1,
        seed=42,
    )
    env = create_tool_memory_env(spec)
    obs, info = env.reset()

    distractor_tools = [t for t in spec.tools if t.is_distractor]
    assert len(distractor_tools) >= 1
    d_tool = distractor_tools[0]

    obs, reward, term, trunc, info = env.step({"tool": d_tool.name, "arguments": {"cust_id": "CUST-1234"}})
    assert reward == 0.0
    assert term is False
    assert env.is_goal_reached() is False
    assert "_last_distractor_hit" in env.working_memory


def test_harbor_task_importable_and_executable(tmp_path: Path) -> None:
    """Test importing and executing env.py directly from the generated Harbor task directory."""
    import importlib.util

    spec = generate_tool_memory_env_spec(scenario_type="cache_invalidation", seed=42)
    task_dir = tmp_path / "cache_task"
    export_to_harbor_task(spec, task_dir)

    env_path = task_dir / "environment" / "env.py"
    assert env_path.exists()

    # Dynamically load module
    module_name = "harbor_exported_env"
    spec_module = importlib.util.spec_from_file_location(module_name, env_path)
    assert spec_module is not None
    assert spec_module.loader is not None
    loaded_mod = importlib.util.module_from_spec(spec_module)
    spec_module.loader.exec_module(loaded_mod)

    env_cls = loaded_mod.SyntheticToolMemoryEnv
    env_inst = env_cls()
    obs, info = env_inst.reset()
    assert obs["status"] == "success"

    oracle = env_inst.get_oracle_trajectory()
    for a in oracle:
        obs, r, term, trunc, sinfo = env_inst.step(a)

    assert term is True
    assert r == 1.0
    assert env_inst.is_goal_reached() is True
