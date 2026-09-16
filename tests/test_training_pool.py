from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from evallab.registry import compute_task_digests, harbor_task_digest
from evallab.schemas import TaskRegistryRecord
from evallab.training_pool import TaskSelection, TrainingPoolError, export_training_pool, main


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _register(root: Path, task_id: str, **overrides: Any) -> dict[str, Any]:
    """Hermetic package with real digest-bound oracle/NOP evidence, no execution."""
    task = root / "library/tasks" / task_id
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "task.toml").write_text('version = "1.0.0"\n')
    (task / "instruction.md").write_text(f"Return the fixture identifier {task_id}.")
    (task / "environment/Dockerfile").write_text("FROM scratch\n")
    (task / "tests/test.sh").write_text("exit 0\n")
    digests = compute_task_digests(task).model_dump(mode="json")
    harbor_digest = harbor_task_digest(task)
    observed_at = "2026-08-15T12:00:00+00:00"
    evidence: dict[str, Any] = {}
    for agent, reward in (("oracle", 1.0), ("nop", 0.0)):
        job_name = f"{task_id}-{agent}"
        trial_name = f"{task_id}__{agent}"
        trial = root / "research/evidence/runs" / job_name / trial_name
        trial.mkdir(parents=True)
        result = {
            "task_name": task_id,
            "trial_name": trial_name,
            "task_id": {"path": str(task)},
            "config": {"task": {"path": str(task)}, "agent": {"name": agent}},
            "agent_info": {"name": agent, "version": "1.0.0"},
            "verifier_result": {"rewards": {"reward": reward}},
            "finished_at": observed_at,
        }
        lock = {
            "schema_version": 2,
            "task": {
                "name": task_id,
                "version": "1.0.0",
                "type": "local",
                "digest": harbor_digest,
                "path": str(task),
            },
            "agent": {"name": agent},
        }
        (trial / "result.json").write_text(json.dumps(result))
        (trial / "lock.json").write_text(json.dumps(lock))
        evidence[agent] = {
            "job_name": job_name,
            "trial_name": trial_name,
            "reward": reward,
            "evidence_path": (trial / "result.json").relative_to(root).as_posix(),
            "evidence_digest": _digest(trial / "result.json"),
            "lock_digest": _digest(trial / "lock.json"),
            "observed_at": observed_at,
            "task_id": task_id,
            "task_version": "1.0.0",
            "task_digests": digests,
            "harbor_task_digest": harbor_digest,
        }
    record = {
        "task_id": task_id,
        "task_family": task_id,
        "version": "1.0.0",
        "task_path": task.relative_to(root).as_posix(),
        "digests": digests,
        "source_uri": f"local/{task_id}",
        "source_ref": "fixture-v1",
        "license": "LicenseRef-Fixture",
        "provenance_zone": "02-local-evidence",
        "is_synthetic": False,
        "control_evidence": evidence,
        "state": "registered",
        "allowed_uses": ["training", "measurement"],
        "approved_by": "fixture-author",
        "approved_at": observed_at,
        **overrides,
    }
    # Check fixtures using the real registry contract, not model_construct.
    record = TaskRegistryRecord.model_validate(record).model_dump(mode="json")
    _save(root, record)
    return record


def _save(root: Path, record: dict[str, Any]) -> None:
    registry = root / "library/registry"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f"{record['task_id']}.json").write_text(json.dumps(record))


def test_cli_exports_reader_compatible_rows_and_bound_unknowns(tmp_path: Path) -> None:
    record = _register(tmp_path, "train-task")
    _register(tmp_path, "validation-task", allowed_uses=["measurement"])
    output = tmp_path / "pool"
    assert (
        main(
            [
                "--repo-root",
                str(tmp_path),
                "--output",
                str(output),
                "--train",
                "train-task=openhands_sdk",
                "--validation",
                "validation-task",
            ]
        )
        == 0
    )
    row = pq.read_table(output / "train.parquet").to_pylist()[0]
    # Match the upstream reader's path fallback, without importing a trainer.
    extra_info = row["extra_info"]
    task_path = (
        extra_info.get("harbor_task_path")
        or extra_info.get("task_path")
        or row["prompt"][0]["content"]
    )
    assert (
        Path(task_path) / "instruction.md"
    ).read_text() == "Return the fixture identifier train-task."
    assert row["prompt"] == [{"role": "user", "content": str(tmp_path / record["task_path"])}]
    assert row["reward_model"] == {"style": "rule", "ground_truth": None}
    assert extra_info["agent_harness"] == "openhands_sdk"
    assert extra_info["instance_id"] == "train-task"
    assert extra_info["data_source"] == "harbor"
    manifest = json.loads((output / "manifest.json").read_text())
    snapshot = manifest["tasks"][0]["registry_record"]
    assert snapshot == record
    assert snapshot["contamination"] is None
    assert snapshot["certification"]["state"] == "legacy_missing"
    for name, metadata in manifest["files"].items():
        assert metadata["digest"] == _digest(output / name)
    assert (
        pq.read_table(output / "validation.parquet").to_pylist()[0]["extra_info"]["instance_id"]
        == "validation-task"
    )
    # The current upstream resolver treats a present null as an invalid explicit
    # override, even during fixed-harness validation.
    validation_row = pq.read_table(output / "validation.parquet").to_pylist()[0]
    assert "agent_harness" not in validation_row["extra_info"]


@pytest.mark.parametrize(
    ("overrides", "split"),
    [
        ({"state": "candidate"}, "train"),
        ({"state": "retired"}, "train"),
        ({"allowed_uses": ["measurement"]}, "train"),
        ({"allowed_uses": ["training"]}, "validation"),
        ({"allowed_uses": ["training", "measurement", "heldout"]}, "train"),
        ({"allowed_uses": ["training", "measurement", "heldout"]}, "validation"),
        ({"license": None}, "train"),
        ({"license": "   "}, "validation"),
    ],
)
def test_ineligible_selection_rejects_whole_bundle(
    tmp_path: Path,
    overrides: dict[str, Any],
    split: str,
) -> None:
    _register(tmp_path, "good-task")
    _register(tmp_path, "bad-task", **overrides)
    output = tmp_path / "never-created" / "pool"
    train = [TaskSelection("good-task")]
    validation: list[TaskSelection] = []
    (train if split == "train" else validation).append(TaskSelection("bad-task"))
    with pytest.raises(TrainingPoolError):
        export_training_pool(tmp_path, output, train=train, validation=validation)
    assert not output.parent.exists()


@pytest.mark.parametrize(
    "damage", ["task", "missing-task", "component", "evidence", "lock", "certificate"]
)
def test_damaged_sources_never_publish(tmp_path: Path, damage: str) -> None:
    record = _register(tmp_path, "source-task")
    task = tmp_path / record["task_path"]
    evidence = tmp_path / record["control_evidence"]["oracle"]["evidence_path"]
    if damage == "task":
        (task / "instruction.md").write_text("drifted")
    elif damage == "missing-task":
        shutil.rmtree(task)
    elif damage == "component":
        shutil.rmtree(task / "environment")
    elif damage == "evidence":
        evidence.unlink()
    elif damage == "lock":
        evidence.with_name("lock.json").write_text("{}")
    else:
        record["certification"] = {"state": "bound"}
        _save(tmp_path, record)
    output = tmp_path / "pool"
    with pytest.raises(TrainingPoolError):
        export_training_pool(tmp_path, output, train=[TaskSelection("source-task")])
    assert not output.exists()


@pytest.mark.parametrize("escape", ["task", "nested", "evidence", "registry"])
def test_source_symlink_escape_is_rejected(tmp_path: Path, escape: str) -> None:
    root = tmp_path / "repo"
    record = _register(root, "source-task")
    outside = tmp_path / "outside"
    outside.mkdir()
    task = root / record["task_path"]
    if escape == "task":
        shutil.move(str(task), outside / "task")
        task.symlink_to(outside / "task", target_is_directory=True)
    elif escape == "nested":
        (outside / "instruction.md").write_bytes((task / "instruction.md").read_bytes())
        (task / "instruction.md").unlink()
        (task / "instruction.md").symlink_to(outside / "instruction.md")
    elif escape == "evidence":
        evidence = root / record["control_evidence"]["oracle"]["evidence_path"]
        (outside / "result.json").write_bytes(evidence.read_bytes())
        evidence.unlink()
        evidence.symlink_to(outside / "result.json")
    else:
        registry = root / "library/registry"
        shutil.move(str(registry), outside / "registry")
        registry.symlink_to(outside / "registry", target_is_directory=True)
    with pytest.raises(TrainingPoolError):
        export_training_pool(root, root / "pool", train=[TaskSelection("source-task")])
    assert not (root / "pool").exists()


@pytest.mark.parametrize("identity", ["task_id", "task_family", "source_uri", "package"])
def test_cross_split_identity_leakage_fails(tmp_path: Path, identity: str) -> None:
    first = _register(tmp_path, "first-task")
    second = _register(tmp_path, "second-task")
    validation_id = "second-task"
    if identity == "task_id":
        validation_id = "first-task"
    elif identity == "package":
        # Rebuild the second task/evidence as an independently registered alias
        # with byte-identical package content, rather than forging its digest.
        task = tmp_path / second["task_path"]
        (task / "instruction.md").write_bytes(
            (tmp_path / first["task_path"] / "instruction.md").read_bytes()
        )
        second["digests"] = compute_task_digests(task).model_dump(mode="json")
        for ref in second["control_evidence"].values():
            ref["task_digests"] = second["digests"]
            ref["harbor_task_digest"] = harbor_task_digest(task)
            lock_path = (tmp_path / ref["evidence_path"]).with_name("lock.json")
            lock = json.loads(lock_path.read_text())
            lock["task"]["digest"] = ref["harbor_task_digest"]
            lock_path.write_text(json.dumps(lock))
            ref["lock_digest"] = _digest(lock_path)
        _save(tmp_path, second)
    else:
        second[identity] = first[identity]
        _save(tmp_path, second)
    with pytest.raises(TrainingPoolError, match="cross-split leakage"):
        export_training_pool(
            tmp_path,
            tmp_path / "pool",
            train=[TaskSelection("first-task")],
            validation=[TaskSelection(validation_id)],
        )
    assert not (tmp_path / "pool").exists()


def test_repeat_is_order_invariant_and_nonidentical_output_is_preserved(tmp_path: Path) -> None:
    _register(tmp_path, "first-task")
    _register(tmp_path, "second-task")
    selections = [TaskSelection("second-task"), TaskSelection("first-task")]
    output = tmp_path / "pool"
    export_training_pool(tmp_path, output, train=selections)
    original = {path.name: path.read_bytes() for path in output.iterdir()}
    assert set(original) == {"train.parquet", "validation.parquet", "manifest.json"}
    export_training_pool(tmp_path, output, train=list(reversed(selections)))
    assert {path.name: path.read_bytes() for path in output.iterdir()} == original
    with pytest.raises(TrainingPoolError, match="not an identical bundle"):
        export_training_pool(tmp_path, output, train=selections[:1])
    assert {path.name: path.read_bytes() for path in output.iterdir()} == original
    assert pq.read_table(output / "validation.parquet").num_rows == 0
    for row in pq.read_table(output / "train.parquet").to_pylist():
        assert "agent_harness" not in row["extra_info"]


def test_mixed_optional_harnesses_cannot_publish_null_overrides(tmp_path: Path) -> None:
    _register(tmp_path, "first-task")
    _register(tmp_path, "second-task")
    with pytest.raises(TrainingPoolError, match="all specify a harness"):
        export_training_pool(
            tmp_path,
            tmp_path / "pool",
            train=[TaskSelection("first-task", "opencode"), TaskSelection("second-task")],
        )
    assert not (tmp_path / "pool").exists()


def test_invalid_selections_cannot_silently_drop_rows(tmp_path: Path) -> None:
    _register(tmp_path, "valid-task")
    for train in ([], [TaskSelection("missing-task")], [TaskSelection("valid-task")] * 2):
        with pytest.raises(TrainingPoolError):
            export_training_pool(tmp_path, tmp_path / "pool", train=train)
    with pytest.raises(TrainingPoolError, match="val_harness"):
        export_training_pool(
            tmp_path,
            tmp_path / "pool",
            train=[],
            validation=[TaskSelection("valid-task", "opencode")],
        )
    assert not (tmp_path / "pool").exists()


def test_output_cannot_mutate_verified_source(tmp_path: Path) -> None:
    record = _register(tmp_path, "source-task")
    task = tmp_path / record["task_path"]
    with pytest.raises(TrainingPoolError, match="source trees"):
        export_training_pool(tmp_path, task / "pool", train=[TaskSelection("source-task")])
    assert compute_task_digests(task).model_dump(mode="json") == record["digests"]
