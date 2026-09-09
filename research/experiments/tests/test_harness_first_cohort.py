from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.registry import TaskRegistry

REPO_ROOT = Path(__file__).resolve().parents[3]
COHORT = REPO_ROOT / "research/experiments/harness-first/cohort.json"


def _compile():
    import importlib.util

    path = REPO_ROOT / "research/experiments/harness-first/compile.py"
    spec = importlib.util.spec_from_file_location("harness_first_compile", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module

compile_mod = _compile()


def test_compiler_repo_root_is_the_eval_lab_checkout() -> None:
    assert (compile_mod.REPO_ROOT / "library/registry").is_dir()
    assert compile_mod.REPO_ROOT == REPO_ROOT


def test_frozen_cohort_is_the_registered_measurement_set() -> None:
    cohort = json.loads(COHORT.read_text())
    registry = TaskRegistry.from_repo(REPO_ROOT)
    selected = [member["task_id"] for member in cohort["members"]]
    assert selected == ["event-summary", "travel-lisbon-002", "syn-funcdag-easy"]
    assert cohort["selected_count"] <= cohort["max_tasks"] == 4
    assert cohort["canary_task_id"] == "event-summary"
    registered = {record.task_id for record in registry.list_records(state="registered")}
    assert set(selected) == registered
    for member in cohort["members"]:
        record = registry.get(member["task_id"])
        assert record is not None
        assert record.state == "registered"
        assert "measurement" in record.allowed_uses
        assert member["heldout"] is False
        assert member["digests"] == record.digests.model_dump()
        assert member["task_path"] == record.task_path
        oracle = member["control_evidence"]["oracle"]
        nop = member["control_evidence"]["nop"]
        assert oracle["reward"] == 1.0
        assert nop["reward"] == 0.0
        assert (REPO_ROOT / oracle["evidence_path"]).is_file()
        assert (REPO_ROOT / nop["evidence_path"]).is_file()


def test_compile_keeps_task_identity_fixed_across_arms() -> None:
    cohort = compile_mod.load_cohort()
    pair = compile_mod.compile_pair(
        cohort,
        "event-summary",
        baseline_agent="mini-swe-agent",
        candidate_agent="authors-rlm-agent",
        model="root-checkpoint-required",
    )
    compile_mod.resolve_compiled(list(pair.values()), REPO_ROOT)
    left = pair["mini-swe"].model_dump(exclude={"name", "agent", "grid_point"})
    right = pair["authors-rlm"].model_dump(exclude={"name", "agent", "grid_point"})
    assert left == right
    assert pair["mini-swe"].agent == "mini-swe-agent"
    assert pair["authors-rlm"].agent == "authors-rlm-agent"
    assert pair["mini-swe"].model == pair["authors-rlm"].model == "root-checkpoint-required"
    assert pair["mini-swe"].attempts == 1
    assert pair["mini-swe"].task == "registered/event-summary"


def test_matrix_compile_does_not_include_canary_or_unregistered_tasks() -> None:
    cohort = compile_mod.load_cohort()
    pairs = compile_mod.compile_stage(
        cohort,
        "matrix",
        baseline_agent="mini-swe-agent",
        candidate_agent="authors-rlm-agent",
        model="root-checkpoint-required",
    )
    assert set(pairs) == {"travel-lisbon-002", "syn-funcdag-easy"}
    specs = [spec for arms in pairs.values() for spec in arms.values()]
    compile_mod.resolve_compiled(specs, REPO_ROOT)
    assert all(spec.task.startswith("registered/") for spec in specs)
    assert "dspy-rlm" not in json.dumps([spec.model_dump() for spec in specs])


def test_comparison_requires_both_arm_job_paths() -> None:
    with pytest.raises(ValueError, match="both arms"):
        compile_mod.build_comparison_spec(
            comparison_id="harness-first-canary",
            baseline_jobs=[],
            candidate_jobs=["runs/example"],
        )
    spec = compile_mod.build_comparison_spec(
        comparison_id="harness-first-canary",
        baseline_jobs=["runs/mini-swe-event-summary"],
        candidate_jobs=["runs/rlm-event-summary"],
    )
    assert spec.declared_variable == "agent_name"
    assert spec.mode == "exploratory"
    assert spec.pairing_key == "task_digest"
    assert spec.pass_k == [1]


def test_missing_execution_parameters_are_rejected() -> None:
    cohort = compile_mod.load_cohort()
    member = compile_mod.member_for(cohort, "event-summary")
    with pytest.raises(ValueError, match="required"):
        compile_mod.compile_spec(
            member, arm_id="mini-swe", agent=" ", model="root-checkpoint-required"
        )
