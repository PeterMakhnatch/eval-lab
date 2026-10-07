"""Dataset-agnostic audit projection: identity, refresh, and SQL proof."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.dataset_audit_contracts import (
    AuditDataset,
    AuditObservation,
    AuditRecord,
    AuditTask,
)
from evallab.storage.paths import task_audit_path
from evallab.storage.task_audit import (
    AUDIT_SCHEMA,
    read_audit_records,
    read_audit_table,
    write_audit,
)
from evallab.storage.trials import connect_trials

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
HARBOR_A = "sha256:" + "c" * 64
HARBOR_B = "sha256:" + "d" * 64


def _task(
    task_id: str = "harbor/hello-world",
    *,
    dataset: str = "hello",
    package: str | None = DIGEST_A,
    harbor: str | None = HARBOR_A,
) -> AuditTask:
    return AuditTask(
        dataset_id=dataset,
        task_id=task_id,
        task_name="harbor/hello-world",
        package_digest=package,
        harbor_digest=harbor,
        aliases=("harbor/hello-world",),
        source_uri="file://hello",
        revision="1.0",
    )


def _dataset(*tasks: AuditTask, dataset: str = "hello") -> AuditDataset:
    return AuditDataset(
        dataset_id=dataset,
        source_uri="file://hello",
        revision="1.0",
        tasks=tasks,
    )


def _record(task: AuditTask, *, verdict: str = "keep") -> AuditRecord:
    return AuditRecord(
        task=task,
        verdict=verdict,  # type: ignore[arg-type]
        verdict_reason="stored verdict",
        stages={
            "leak": AuditObservation(stage="leak", status="recorded", facts={"found": False}),
            "static": AuditObservation(stage="static", status="recorded", facts={"rules": 3}),
        },
    )


def test_roundtrip_preserves_complete_record(tmp_path: Path) -> None:
    path = tmp_path / "audit.parquet"
    task, other = _task(), _task("other/task", package=DIGEST_B, harbor=HARBOR_B)
    write_audit([_record(task), _record(other)], path, dataset=_dataset(task, other))

    assert read_audit_records(path) == [_record(task), _record(other)]


def test_refresh_replaces_same_identity_and_keeps_versions(tmp_path: Path) -> None:
    path = tmp_path / "audit.parquet"
    old, new = _task(), _task(package=DIGEST_B, harbor=HARBOR_B)
    write_audit([_record(old)], path, dataset=_dataset(old))
    # Same dataset snapshot, refreshed package version: both rows persist.
    write_audit([_record(new, verdict="fix")], path, dataset=_dataset(new))

    table = read_audit_table(path)
    assert table.num_rows == 2
    assert sorted(table.column("verdict").to_pylist()) == ["fix", "keep"]

    # Refreshing the original identity replaces only that row.
    write_audit([_record(old, verdict="discard")], path, dataset=_dataset(old))
    table = read_audit_table(path)
    assert table.num_rows == 2
    rows = {row["package_digest"]: row["verdict"] for row in table.to_pylist()}
    assert rows == {DIGEST_A: "discard", DIGEST_B: "fix"}


def test_stage_facts_are_columns_not_rows(tmp_path: Path) -> None:
    path = tmp_path / "audit.parquet"
    task = _task()
    write_audit([_record(task)], path, dataset=_dataset(task))

    table = read_audit_table(path)
    assert table.num_rows == 1
    row = table.to_pylist()[0]
    assert row["leak_status"] == "recorded"
    assert row["static_status"] == "recorded"
    assert row["oracle_status"] is None
    assert table.schema.equals(AUDIT_SCHEMA, check_metadata=False)


def test_missing_file_gives_typed_empty_relation(tmp_path: Path) -> None:
    path = tmp_path / "absent" / "audit.parquet"
    assert read_audit_records(path) == []
    assert read_audit_table(path).schema.equals(AUDIT_SCHEMA, check_metadata=False)
    assert not (tmp_path / "absent").exists()


def test_corrupt_file_is_a_visible_error(tmp_path: Path) -> None:
    path = tmp_path / "audit.parquet"
    path.write_bytes(b"not parquet")
    with pytest.raises(ValueError, match="invalid audit projection"):
        read_audit_records(path)


def test_record_outside_dataset_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "audit.parquet"
    task = _task()
    with pytest.raises(ValueError, match="not the selected dataset package"):
        write_audit([_record(_task(dataset="other"))], path, dataset=_dataset(task))


def test_audit_queryable_without_any_native_trial(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    task = _task()
    write_audit([_record(task)], derived / "audit.parquet", dataset=_dataset(task))

    con, info = connect_trials(repo_root=tmp_path, roots=[], derived_root=derived)
    try:
        assert info["n_rows"] == 0
        assert info["audit_rows"] == 1
        assert info["audit_path"] == str(task_audit_path(tmp_path, derived_root=derived))
        cursor = con.execute("SELECT task_id, verdict, leak_status FROM audit")
        assert cursor.fetchall() == [("harbor/hello-world", "keep", "recorded")]
    finally:
        con.close()


def test_empty_audit_relation_without_trials(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    con, info = connect_trials(repo_root=tmp_path, roots=[], derived_root=derived)
    try:
        assert con.execute("SELECT COUNT(*) FROM audit").fetchall() == [(0,)]
        assert info["audit_rows"] == 0
        assert not derived.exists()
    finally:
        con.close()


def test_corrupt_audit_is_visible_from_trials(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    (derived / "audit.parquet").write_bytes(b"not parquet")
    with pytest.raises(ValueError, match="invalid audit projection"):
        connect_trials(repo_root=tmp_path, roots=[], derived_root=derived)
