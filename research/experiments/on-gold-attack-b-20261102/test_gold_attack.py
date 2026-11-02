"""Tests for A5B Gold Attack experiment."""

from __future__ import annotations

from pathlib import Path

from analyze import analyze
from run_attack import load_cohort, visible_material


def test_cohort_loading_and_ordering():
    """Verify cohort loads expected 8 certified tasks in correct order."""
    cohort, excluded = load_cohort(max_cohort=12)
    assert len(cohort) == 8
    assert all(t["functions_kept"] >= 11 for t in cohort)

    # Check descending order by functions_kept
    functions_kept = [t["functions_kept"] for t in cohort]
    assert functions_kept == sorted(functions_kept, reverse=True)

    # First task should have 12 functions kept
    assert cohort[0]["functions_kept"] == 12

    # Check all packages exist
    for t in cohort:
        assert Path(t["orig_package"]).is_dir(), f"Missing orig package: {t['orig_package']}"
        assert Path(t["gold_package"]).is_dir(), f"Missing gold package: {t['gold_package']}"


def test_visible_material_extraction(tmp_path):
    """Verify visible material extraction from task package structure."""
    pkg = tmp_path / "task_pkg"
    pkg.mkdir()
    (pkg / "instruction.md").write_text("Solve this problem", encoding="utf-8")
    tests_dir = pkg / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_main.py").write_text("assert False", encoding="utf-8")

    instruction, tests, _ = visible_material(pkg)
    assert instruction == "Solve this problem"
    assert "test_main.py" in tests
    assert "assert False" in tests


def test_statistical_analysis():
    """Verify statistical analysis over synthetic receipts."""
    receipts = [
        {
            "task_id": "format-code-task-000001",
            "attestation": {"attested": True},
            "taxonomy_class": "grader_tamperable",
            "mechanism": "tamper with conftest",
            "L0": {"reward": 1.0, "status": "completed"},
            "Gold": {"reward": 0.0, "status": "completed"},
        },
        {
            "task_id": "format-code-task-000002",
            "attestation": {"attested": True},
            "taxonomy_class": "weak_check",
            "mechanism": "pass with exit 0",
            "L0": {"reward": 1.0, "status": "completed"},
            "Gold": {"reward": 0.0, "status": "completed"},
        },
        {
            "task_id": "format-code-task-000003",
            "attestation": {"attested": True},
            "taxonomy_class": "exploit_grader_gap",
            "mechanism": "gap exploit",
            "L0": {"reward": 0.0, "status": "completed"},
            "Gold": {"reward": 0.0, "status": "completed"},
        },
    ]

    res = analyze(receipts)
    assert res["n_paired"] == 3
    assert res["k_l0"] == 2
    assert res["k_gold"] == 0
    assert res["paired"]["b"] == 2
    assert res["paired"]["c"] == 0
    assert len(res["gold_bypasses"]) == 0
