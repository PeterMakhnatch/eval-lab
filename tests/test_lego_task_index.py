import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.lego_task_index import (
    LegoIndexError,
    build_index_rows,
    load_registry_record,
    main,
    write_index,
)


@pytest.fixture
def registry(tmp_path):
    registry_dir = tmp_path / "library/registry"
    tasks_root = tmp_path / "library/tasks"
    registry_dir.mkdir(parents=True)

    def register(task_id="example", **overrides):
        # Exercise the nested paths used by the real travel-lisbon registry entry.
        relative = f"experimental/example-v1/{task_id}"
        task = tasks_root / relative
        task.mkdir(parents=True, exist_ok=True)
        (task / "instruction.md").write_text("Fixture task instruction\n")
        record = {
            "task_id": task_id,
            "task_path": f"library/tasks/{relative}",
            "state": "registered",
            "approved_by": "fixture reviewer",
            "allowed_uses": ["measurement", "training"],
            "digests": {"verifier": "sha256:" + "a" * 64},
            "control_evidence": {
                "oracle": {"reward": 1.0, "harbor_task_digest": "sha256:" + "b" * 64},
                "nop": {"reward": 0.0, "harbor_task_digest": "sha256:" + "b" * 64},
            },
        }
        record.update(overrides)
        (registry_dir / f"{task_id}.json").write_text(json.dumps(record))
        return task.resolve()

    return registry_dir, tasks_root, register


def test_lego_columns_provenance_and_dev_filename(registry, tmp_path, capsys):
    registry_dir, tasks_root, register = registry
    task = register()
    assert main([
        "--registry", str(registry_dir), "--tasks", str(tasks_root),
        "--assign", "example=dev", "--output", str(tmp_path / "index.parquet"),
    ]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["row_counts"] == {"train": 0, "dev": 1, "final": 0}
    assert Path(receipt["paths"]["dev"]).name == "index_val.parquet"
    table = pq.read_table(receipt["paths"]["dev"])
    assert table.to_pylist() == [{
        "prompt": [{"role": "user", "content": str(task)}],
        "reward_model": {"style": "rule", "ground_truth": None},
        "extra_info": {
            "harbor_task_path": str(task), "instance_id": "example", "data_source": "harbor",
        },
        "lab_task_id": "example",
        "verifier_sha256": "sha256:" + "a" * 64,
        "harbor_task_sha256": "sha256:" + "b" * 64,
        "runtime_qualified": True,
        "admission_source": "explicit-cli-argument",
        "split": "dev",
        "registry_record_sha256": "sha256:" + hashlib.sha256(
            (registry_dir / "example.json").read_bytes()
        ).hexdigest(),
    }]
    assert table.schema.field("prompt").type == pa.list_(pa.struct([
        ("role", pa.string()), ("content", pa.string()),
    ]))
    assert table.schema.field("reward_model").type == pa.struct([
        ("style", pa.string()), ("ground_truth", pa.null()),
    ])
    assert pq.read_table(receipt["paths"]["train"]).to_pylist() == []


def test_unregistered_directory_is_not_admission(registry):
    registry_dir, tasks_root, register = registry
    register()
    (registry_dir / "example.json").unlink()
    with pytest.raises(LegoIndexError, match="registry_record_missing"):
        load_registry_record(registry_dir, "example")
    with pytest.raises(LegoIndexError, match="registry_record_missing"):
        build_index_rows(
            registry_dir=registry_dir, tasks_root=tasks_root,
            assignments=[("example", "dev")], admitted=set(),
        )


@pytest.mark.parametrize(("overrides", "admitted", "reason"), [
    ({}, set(), "training_admission_missing"),
    ({"allowed_uses": ["measurement"]}, {"example"}, "training_use_not_allowed"),
    ({"state": "retired"}, {"example"}, "task_not_registered"),
    ({"approved_by": None}, {"example"}, "task_not_registered"),
])
def test_training_requires_registration_and_explicit_admission(
    registry, overrides, admitted, reason,
):
    registry_dir, tasks_root, register = registry
    register(**overrides)
    with pytest.raises(LegoIndexError, match=reason):
        build_index_rows(
            registry_dir=registry_dir, tasks_root=tasks_root,
            assignments=[("example", "train")], admitted=admitted,
        )


@pytest.mark.parametrize(("evidence", "reason"), [
    (None, "runtime_evidence_missing"),
    ({"oracle": {"reward": 1}}, "runtime_evidence_missing"),
    ({"oracle": {"reward": 1}, "nop": {}}, "runtime_evidence_missing"),
    ({"oracle": {"reward": 0}, "nop": {"reward": 0}}, "runtime_evidence_invalid"),
    ({"oracle": {"reward": 1}, "nop": {"reward": 1}}, "runtime_evidence_invalid"),
    ({"oracle": {"reward": True}, "nop": {"reward": False}}, "runtime_evidence_invalid"),
])
def test_controls_must_record_both_exact_numeric_rewards(registry, evidence, reason):
    registry_dir, tasks_root, register = registry
    register(control_evidence=evidence)
    with pytest.raises(LegoIndexError, match=reason):
        build_index_rows(
            registry_dir=registry_dir, tasks_root=tasks_root,
            assignments=[("example", "dev")], admitted=set(),
        )


def test_final_is_separate_and_cannot_be_assigned_to_training(registry, tmp_path):
    registry_dir, tasks_root, register = registry
    for name in ("train-task", "dev-task", "final-task"):
        register(name)
    rows = build_index_rows(
        registry_dir=registry_dir, tasks_root=tasks_root,
        assignments=[("train-task", "train"), ("dev-task", "dev"), ("final-task", "final")],
        admitted={"train-task"},
    )
    receipt = write_index(rows, tmp_path / "index")
    for split, task_id, suffix in (
        ("train", "train-task", "train"), ("dev", "dev-task", "val"),
        ("final", "final-task", "final"),
    ):
        assert receipt["paths"][split] == str(tmp_path / f"index_{suffix}.parquet")
        exported = pq.read_table(receipt["paths"][split]).to_pylist()
        assert [row["lab_task_id"] for row in exported] == [task_id]
        assert [row["split"] for row in exported] == [split]
    with pytest.raises(LegoIndexError, match="split_conflict"):
        build_index_rows(
            registry_dir=registry_dir, tasks_root=tasks_root,
            assignments=[("final-task", "final"), ("final-task", "train")],
            admitted={"final-task"},
        )


def test_invalid_split_is_not_silently_dropped(registry, tmp_path):
    registry_dir, tasks_root, register = registry
    register()
    with pytest.raises(LegoIndexError, match="invalid_split"):
        build_index_rows(
            registry_dir=registry_dir, tasks_root=tasks_root,
            assignments=[("example", "val")], admitted=set(),
        )
    with pytest.raises(LegoIndexError, match="invalid_split"):
        write_index([{"lab_task_id": "example", "split": "val"}], tmp_path / "index")
    assert not (tmp_path / "index_train.parquet").exists()
