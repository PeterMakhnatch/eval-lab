"""Unit and contract tests for R3-REPAIR-VS-GOLD experiment artifacts."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

EXP_DIR = Path(__file__).resolve().parent
REPO = EXP_DIR.parents[2]
sys.path.insert(0, str(REPO / "src"))

CERT_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert")
sys.path.insert(0, str(CERT_DIR))
import stats  # noqa: E402


@pytest.fixture
def summary_data() -> dict:
    results_dir = Path(os.environ.get("R3_RESULTS_DIR", Path.home() / "Developer" / "eval-lab-results" / "2026-11-02" / "R3-REPAIR-VS-GOLD"))
    path = results_dir / "repair_vs_gold.json"
    if not path.is_file():
        path = EXP_DIR / "repair_vs_gold.json"
    if not path.is_file():
        pytest.skip(f"Row-level repair_vs_gold.json kept local; not found at {path}")
    return json.loads(path.read_text())


@pytest.fixture
def paired_data() -> dict:
    path = EXP_DIR / "paired_comparison.json"
    assert path.is_file(), f"Missing {path}"
    return json.loads(path.read_text())


@pytest.fixture
def overlap_data() -> dict:
    path = EXP_DIR / "manifest_overlap.json"
    assert path.is_file(), f"Missing {path}"
    return json.loads(path.read_text())


def test_cohort_and_task_counts(summary_data: dict, paired_data: dict):
    cohort = summary_data["cohort"]
    assert cohort["repaired_30_total"] == 30
    assert cohort["overlap_total"] == 27
    assert cohort["audited_paired_tasks"] == 24
    assert cohort["total_paired_mutants"] == 64

    tasks = summary_data["tasks"]
    assert len(tasks) == 24
    assert len(paired_data["pairs"]) == 24


def test_task_level_paired_mcnemar(summary_data: dict, paired_data: dict):
    task_stats = summary_data["task_level_paired"]
    assert task_stats["n"] == 24
    assert task_stats["orig_weak_count"] == 17
    assert task_stats["rep_weak_count"] == 8
    assert task_stats["both_weak"] == 7
    assert task_stats["neither_weak"] == 6
    assert task_stats["b_repair_fixed"] == 10
    assert task_stats["c_repair_degraded"] == 1
    assert abs(task_stats["mcnemar_p"] - 0.011719) < 1e-5

    # Verify directly using stats.paired on the pair list
    orig_bools = [p["orig_weak"] for p in paired_data["pairs"]]
    rep_bools = [p["rep_weak"] for p in paired_data["pairs"]]
    calc_res = stats.paired(orig_bools, rep_bools)
    assert calc_res["b"] == 10
    assert calc_res["c"] == 1
    assert calc_res["both"] == 7
    assert calc_res["neither"] == 6
    assert abs(calc_res["p"] - task_stats["mcnemar_p"]) < 1e-9


def test_mutant_level_paired_mcnemar(summary_data: dict):
    mut_stats = summary_data["mutant_level_paired"]
    assert mut_stats["n"] == 64
    assert mut_stats["orig_survivors_count"] == 21
    assert mut_stats["rep_survivors_count"] == 8
    assert mut_stats["both_survived"] == 7
    assert mut_stats["neither_survived"] == 42
    assert mut_stats["b_killed_by_repair"] == 14
    assert mut_stats["c_accepted_by_repair_only"] == 1
    assert abs(mut_stats["mcnemar_p"] - 0.000977) < 1e-5


def test_degraded_task(summary_data: dict, paired_data: dict):
    assert summary_data["degraded_tasks_count"] == 1
    degraded = summary_data["degraded_tasks"][0]
    assert degraded["task_id"] == "format-code-task-000322"
    assert degraded["orig_survivors_count"] == 0
    assert degraded["rep_survivors_count"] == 1


def test_gold_overlap_2x2(overlap_data: dict):
    assert overlap_data["cohort_overlap_size"] == 27
    table = overlap_data["contingency_table_2x2"]
    assert table["repair_qual_and_gold"] == 0
    assert table["repair_qual_and_not_gold"] == 27
    assert table["not_repair_qual_and_gold"] == 0
    assert table["not_repair_qual_and_not_gold"] == 0

    reasons = overlap_data["gold_certified_status_reasons"]
    assert reasons.get("ref_failed_no_repair") == 27
