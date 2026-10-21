"""A weak-test receipt must not accept stale, cross-task or unusable evidence."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "har209_receipt",
    REPO / "research/experiments/mimo-weak-tests-line-backfill-20261021/build.py",
)
assert SPEC and SPEC.loader
receipt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receipt)

TASK = "format-code-task-000001"


def evidence(root: Path) -> tuple[Path, dict]:
    directory = root / TASK
    directory.mkdir()
    (directory / "report.json").write_text(json.dumps({
        "task_id": TASK,
        "status": "measured",
        "verdict": "pass",
        "package_digest": "sha256:package",
        "reference": {"sha256": "reference"},
        "mutants": {
            "status": "measured", "verdict": "fail", "tried": 1, "killed": 0,
            "unscored": 0, "survivors": [{"id": "one", "file": "answer.py", "line": 3}],
        },
    }))
    (directory / "grades.log").write_text(
        "== full reward=1.0 rc=0 scored 1.0s\nreference accepted\n"
        "== nop reward=0.0 rc=0 scored 1.0s\nno-op rejected\n"
        "== mutant:one reward=1.0 rc=0 scored 1.0s\nwrong answer accepted\n"
    )
    review = {
        "task_id": TASK,
        "package_digest": "sha256:package",
        "reference_sha256": "reference",
        "report_sha256": receipt.sha256(directory / "report.json"),
        "grades_log_sha256": receipt.sha256(directory / "grades.log"),
        "minimal_patch_sha256": None,
        "dropped_patch_sha256": None,
        "block": None,
        "survivors": [{
            "id": "one", "label": "uncertain", "reason": "The consuming API is unavailable.",
            "evidence_refs": ["instruction.md:3", "mutants/one.patch:5"],
            "grade": {"reward": 1.0, "rc": 0, "status": "scored"},
        }],
    }
    return directory, review


def test_an_unresolved_review_does_not_become_a_positive_or_clean_negative(tmp_path: Path) -> None:
    _, review = evidence(tmp_path)
    observed = receipt.new_scan(tmp_path, TASK)
    assert observed["survivors"][0]["review"] == "unreviewed"
    reviewed = receipt.new_scan(tmp_path, TASK, review)
    assert reviewed["survivors"][0]["review"] == "uncertain"
    assert reviewed["mutant_stage"]["verdict"] == "fail"


def test_a_report_for_another_task_is_not_coverage(tmp_path: Path) -> None:
    directory, _ = evidence(tmp_path)
    path = directory / "report.json"
    report = json.loads(path.read_text())
    report["task_id"] = "format-code-task-000002"
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="belongs to another task"):
        receipt.new_scan(tmp_path, TASK)


@pytest.mark.parametrize("bad_grade", ["reward=1.0 rc=0 scored", "reward=0.0 rc=1 scored", "reward=0.0 rc=0 unscored"])
def test_a_survivor_needs_a_genuine_negative_control(tmp_path: Path, bad_grade: str) -> None:
    directory, _ = evidence(tmp_path)
    path = directory / "grades.log"
    path.write_text(path.read_text().replace("nop reward=0.0 rc=0 scored", f"nop {bad_grade}"))
    with pytest.raises(ValueError, match="usable full/nop controls"):
        receipt.new_scan(tmp_path, TASK)


def test_stale_semantic_review_cannot_follow_a_changed_passing_patch(tmp_path: Path) -> None:
    directory, review = evidence(tmp_path)
    (directory / "minimal.patch").write_text("a different incomplete fix\n")
    with pytest.raises(ValueError, match="stale review minimal_patch_sha256"):
        receipt.new_scan(tmp_path, TASK, review)


def test_a_failed_process_is_not_a_scored_survivor(tmp_path: Path) -> None:
    directory, _ = evidence(tmp_path)
    path = directory / "grades.log"
    path.write_text(path.read_text().replace("mutant:one reward=1.0 rc=0", "mutant:one reward=1.0 rc=1"))
    with pytest.raises(ValueError, match="survivor list disagrees"):
        receipt.new_scan(tmp_path, TASK)


def test_duplicate_retry_grades_are_not_independent_observations(tmp_path: Path) -> None:
    directory, _ = evidence(tmp_path)
    path = directory / "grades.log"
    with path.open("a") as stream:
        stream.write("== mutant:one reward=1.0 rc=0 scored 1.0s\n")
    with pytest.raises(ValueError, match="duplicate grade label"):
        receipt.new_scan(tmp_path, TASK)


def test_a_missing_mutant_grade_cannot_be_hidden_by_the_summary(tmp_path: Path) -> None:
    directory, _ = evidence(tmp_path)
    path = directory / "grades.log"
    path.write_text(path.read_text().split("== mutant:")[0])
    with pytest.raises(ValueError, match="mutant count disagrees"):
        receipt.new_scan(tmp_path, TASK)
