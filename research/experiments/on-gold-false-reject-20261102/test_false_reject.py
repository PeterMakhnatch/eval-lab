"""Unit and contract tests for A4C false-reject remedies."""

from __future__ import annotations

import json

import pytest
from run_experiments import (
    GATES_DIR,
    compute_r1_filters,
    run_full_evaluation,
)


@pytest.fixture(scope="module")
def full_results():
    return run_full_evaluation()


def test_r1_task_002391_exact_dropped_tests():
    with open(GATES_DIR / "gold-v1.1-certified.json", encoding="utf-8") as f:
        gold_tasks = json.load(f)["tasks"]
    with open(GATES_DIR / "gold-universe.json", encoding="utf-8") as f:
        gu_tasks = {t["task_id"]: t for t in json.load(f)["tasks"]}

    r1_data = compute_r1_filters(gold_tasks, gu_tasks)
    t2391 = r1_data["format-code-task-002391"]

    assert set(t2391["ref_only_symbols"]) == {"alias_of", "merge_aliases"}
    assert t2391["test_functions_total"] == 11
    assert t2391["test_functions_dropped"] == 4
    assert t2391["test_functions_kept"] == 7

    expected_dropped = {
        "test_alias_of_true_when_ids_or_aliases_intersect",
        "test_alias_of_false_when_disjoint",
        "test_merge_aliases_combines_sets_and_excludes_self_id",
        "test_merge_aliases_returns_new_instance_preserving_metadata",
    }
    assert set(t2391["dropped_names"]) == expected_dropped


def test_all_tasks_maintain_minimum_acceptance(full_results):
    for tid, counts in full_results["task_function_counts"].items():
        assert counts["after_r1"] >= 3, f"Task {tid} dropped below 3 kept functions: {counts['after_r1']}"


def test_retained_kills_invariants(full_results):
    arms = full_results["arms"]
    # All 12 unseen EVAL mutant kills retained in all arms
    for arm_id in ["S0", "S1", "S2", "S3"]:
        assert arms[arm_id]["retained_eval_kills"]["k"] == 12

    # Weak grader demonstration kills: at most 1 lost (>= 7/8)
    for arm_id in ["S1", "S2", "S3"]:
        assert arms[arm_id]["retained_weak_demonstrations"]["k"] >= 7


def test_clear_overstrict_rejections_eliminated(full_results):
    # All 3 clear over-strict rejections on 002391 must be eliminated in S1, S2, S3
    arms = full_results["arms"]
    assert arms["S0"]["clear_overstrict_rejections"] == 3
    assert arms["S1"]["clear_overstrict_rejections"] == 0
    assert arms["S2"]["clear_overstrict_rejections"] == 0
    assert arms["S3"]["clear_overstrict_rejections"] == 0


def test_decision_rule_worth_adopting(full_results):
    arms = full_results["arms"]
    assert arms["S1"]["worth_adopting"] is True
    assert arms["S2"]["worth_adopting"] is True
    assert arms["S3"]["worth_adopting"] is True
