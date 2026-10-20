"""Task-level holdout, source integrity and answer-label confidentiality."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from evallab.registry import task_directory_digest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "tb4_audit_views", ROOT / "research/answer-keys/tb4-audit-v1/views.py"
)
assert _spec is not None and _spec.loader is not None
views = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(views)

SECRET = "SEALED_ANSWER_LABEL_NOT_AN_AUDITOR_INPUT"


def _task(root: Path) -> tuple[dict, Path]:
    upstream = root / "upstream"
    package = upstream / "square"
    for directory in (
        package,
        package / "tests",
        package / "solution",
        package / "environment",
        package / "cheat",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (package / "instruction.md").write_text("Implement square(x) for integer inputs.\n")
    (package / "task.toml").write_text('[task]\nname = "terminal-bench/square"\n')
    (package / "solution/solve.py").write_text("def square(x):\n    return x * x\n")
    (package / "tests/test_square.py").write_text("assert square(-3) == 9\n")
    (package / "environment/Dockerfile").write_text("FROM python:3.12-slim\n")
    (package / "README.md").write_text(SECRET + "\n")
    (package / "cheat/review.txt").write_text(SECRET + "\n")
    row = {
        "split": "test",
        "scope": "task",
        "subject": {
            "kind": "task",
            "task_id": "terminal-bench/square",
            "task_digest": task_directory_digest(package),
        },
        "package": {"name": "square", "pin": "a" * 40},
        "title": SECRET,
        "port_tests": [{"nodeid": SECRET}],
        "expected_verdict": "fail",
        "arbitrary_future_label_field": {"label": SECRET},
    }
    return row, upstream


def test_holdout_is_order_independent_and_disjoint_at_task_level() -> None:
    cohort = [f"task-{index:03}" for index in range(66)]
    assignments = views.task_splits(cohort)
    assert assignments == views.task_splits(reversed(cohort))
    dev = {task for task, split in assignments.items() if split == "dev"}
    test = {task for task, split in assignments.items() if split == "test"}
    assert dev.isdisjoint(test)
    assert dev | test == set(cohort)
    assert (len(dev), len(test)) == (25, 41)
    with pytest.raises(ValueError, match="unique"):
        views.task_splits(cohort + [cohort[0]])


def test_auditor_sees_original_task_surfaces_but_no_answer_labels(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    row, upstream = _task(root)
    outside = root / "scorer-items.jsonl"
    outside.write_text(SECRET)
    taxonomy = {
        "version": 1,
        "label_rules": ["Unsupported class evidence remains suspected."],
        "decision_order": [{"q": "Is a required behavior not checked?", "class": "weak_check"}],
        "task_side": [
            {
                "id": "TS01",
                "key": "weak_check",
                "definition": "A requirement is checked too weakly.",
                "examples": [{"task": "square", "label": SECRET}],
                "pm_count": SECRET,
            }
        ],
        "run_side": [],
        "private_findings": [SECRET],
    }
    destination = root / "actor"
    views.materialize_agent_view(
        [row, row | {"title": SECRET + "_SECOND"}],
        destination,
        split="test",
        task_root=upstream,
        taxonomy=taxonomy,
    )
    tools = views.agent_tools(destination)
    assert "return x * x" in tools.run("read", {"path": "tasks/square/solution/solve.py"})
    assert "A requirement is checked too weakly" in tools.run("read", {"path": "taxonomy.json"})
    assert tools.run("grep", {"path": ".", "pattern": SECRET}).startswith("[no matches")
    inputs = [json.loads(line) for line in (destination / "inputs.jsonl").read_text().splitlines()]
    assert [item["subject"]["task_id"] for item in inputs] == ["terminal-bench/square"]
    for path in (
        str(outside),
        "../scorer-items.jsonl",
        "items.jsonl",
        "bundle.json",
        "tasks/square/README.md",
        "tasks/square/cheat/review.txt",
    ):
        with pytest.raises(ValueError):
            tools.run("read", {"path": path})
    for tool in ("bash", "python", "write", "http"):
        with pytest.raises(ValueError):
            tools.run(tool, {})
    with pytest.raises(FileExistsError):
        views.materialize_agent_view([row], destination, split="test", task_root=upstream)


def test_changed_package_cannot_be_bound_to_an_old_answer_key(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    row, upstream = _task(root)
    (upstream / "square/instruction.md").write_text("Implement cube(x), not square(x).\n")
    destination = root / "actor"
    with pytest.raises(ValueError, match="package digest"):
        views.materialize_agent_view([row], destination, split="test", task_root=upstream)
    assert not (destination / "bundle.json").exists()


def test_symlinked_source_and_changed_exports_cannot_cross_the_read_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    row, upstream = _task(root)
    private = root / "private-label.json"
    private.write_text(SECRET)
    link = upstream / "square/tests/private-label.json"
    link.symlink_to(private)
    with pytest.raises(ValueError, match="symlink"):
        views.materialize_agent_view([row], root / "rejected", split="test", task_root=upstream)
    link.unlink()
    destination = root / "actor"
    views.materialize_agent_view([row], destination, split="test", task_root=upstream)
    changed = destination / "tasks/square/instruction.md"
    changed.chmod(0o600)
    changed.write_text("Mutated after the frozen export.\n")
    with pytest.raises(ValueError, match="changed after export"):
        views.agent_tools(destination)
