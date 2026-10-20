"""Task-level holdout, source integrity and answer-label confidentiality."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
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


KEY = ROOT / "research/answer-keys/tb4-audit-v1"


def _build_module():
    sys.path.insert(0, str(KEY))
    try:
        spec = importlib.util.spec_from_file_location("tb4_audit_build", KEY / "build.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(KEY))
    return module


build = _build_module()
SOURCE = json.loads((KEY / "source.json").read_text(encoding="utf-8"))
REGISTRY = json.loads((KEY / "witnesses.json").read_text(encoding="utf-8"))


def _confirmed_registry(root: Path) -> dict:
    """One registered witness, copied to a scratch root and marked as confirmed."""
    witness = copy.deepcopy(REGISTRY["witnesses"][0])
    shutil.copytree(KEY / witness["solution_dir"], root / witness["solution_dir"])
    witness |= {
        "upstream_replay": {"reward": 1.0, "cost_usd": 0.02},
        "port_replay": {"reward": 0.0},
        "confirmed": True,
    }
    return {"schema": REGISTRY["schema"], "witnesses": [witness]}


def test_registered_witnesses_are_dev_only_and_digest_bound() -> None:
    assert REGISTRY["schema"] == "tb4.audit.witnesses/v1"
    assert len(REGISTRY["witnesses"]) == 10
    for witness in REGISTRY["witnesses"]:
        assert witness["split"] == "dev"
        assert (KEY / witness["solution_dir"] / "solve.sh").is_file()
    build.build_outputs(SOURCE)


def test_confirmed_witness_is_attached_to_its_item(tmp_path: Path) -> None:
    registry = _confirmed_registry(tmp_path)
    witness = registry["witnesses"][0]
    outputs = build.build_outputs(SOURCE, registry, witness_root=tmp_path)
    items = {row["item_id"]: row for row in map(json.loads, outputs["items.jsonl"].splitlines())}
    item = items[witness["finding_id"]]
    assert item["label_strength"] == "confirmed"
    assert item["reproducer"] == {
        "kind": "command",
        "path": f"research/answer-keys/tb4-audit-v1/{witness['solution_dir']}/solve.sh",
        "expected_reward": 1.0,
    }
    assert item["witness"]["witness_id"] == witness["witness_id"]
    summary = json.loads(outputs["summary.json"])
    assert summary["witness_replay"]["confirmed"] == 1
    assert summary["native_reproductions_performed"] == 1
    assert summary["paid_cost_usd"] == 0.02


def test_witness_on_a_test_split_task_is_rejected(tmp_path: Path) -> None:
    registry = _confirmed_registry(tmp_path)
    tasks = [json.loads(line) for line in (KEY / "tasks.jsonl").read_text().splitlines()]
    held_out = next(row["task"] for row in tasks if row["split"] == "test")
    finding = next(row for row in SOURCE["findings"] if row["task"] == held_out)
    registry["witnesses"][0] |= {"task": held_out, "finding_id": finding["finding_id"]}
    with pytest.raises(ValueError, match="dev-split"):
        build.build_outputs(SOURCE, registry, witness_root=tmp_path)
    registry["witnesses"][0]["finding_id"] = "pm-tb4:unknown:0000000000"
    with pytest.raises(ValueError, match="unknown finding"):
        build.build_outputs(SOURCE, registry, witness_root=tmp_path)


def test_tampered_witness_files_are_rejected(tmp_path: Path) -> None:
    registry = _confirmed_registry(tmp_path)
    solve = tmp_path / registry["witnesses"][0]["solution_dir"] / "solve.sh"
    solve.write_text(solve.read_text() + "echo tampered\n")
    with pytest.raises(ValueError, match="sha256"):
        build.build_outputs(SOURCE, registry, witness_root=tmp_path)


@pytest.mark.parametrize(("upstream", "port"), [(0.0, 0.0), (1.0, 1.0), (None, 0.0), (1.0, None)])
def test_unconfirmed_rewards_cannot_be_registered_as_confirmed(
    tmp_path: Path, upstream: float | None, port: float | None
) -> None:
    registry = _confirmed_registry(tmp_path)
    registry["witnesses"][0] |= {
        "upstream_replay": {"reward": upstream},
        "port_replay": {"reward": port},
    }
    with pytest.raises(ValueError, match="upstream reward 1.0"):
        build.build_outputs(SOURCE, registry, witness_root=tmp_path)


def test_witness_files_never_enter_the_auditor_view(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    row, upstream = _task(root)
    package = upstream / "square"
    (package / "tests/witnesses.json").write_text(SECRET + "\n")
    (package / "solution/witnesses").mkdir()
    (package / "solution/witnesses/solve.sh").write_text(SECRET + "\n")
    row["subject"]["task_digest"] = task_directory_digest(package)
    destination = root / "actor"
    manifest = views.materialize_agent_view([row], destination, split="test", task_root=upstream)
    assert not any("witnesses" in name for name in manifest["files"])
    tools = views.agent_tools(destination)
    assert tools.run("grep", {"path": ".", "pattern": SECRET}).startswith("[no matches")
    for path in ("tasks/square/tests/witnesses.json", "tasks/square/solution/witnesses/solve.sh"):
        with pytest.raises(ValueError):
            tools.run("read", {"path": path})

    # A view whose manifest was edited to list a witness file is refused outright.
    forged = root / "forged"
    views.materialize_agent_view([row], forged, split="test", task_root=upstream)
    (forged / "witnesses.json").write_text(SECRET)
    bundle = forged / "bundle.json"
    manifest = json.loads(bundle.read_text())
    manifest["files"]["witnesses.json"] = views.hashlib.sha256(SECRET.encode()).hexdigest()
    bundle.chmod(0o600)
    bundle.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="scorer-only"):
        views.agent_tools(forged)
