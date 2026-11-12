"""Task dossiers keep absent evidence unknown and remain read-only."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.task_dossier import task_dossier


def _write_projection(records, path: Path, *, dataset) -> None:
    from evallab.storage.task_audit import AUDIT_SCHEMA, SCHEMA, _record_columns

    descriptor = dataset.model_dump(mode="json", exclude={"tasks"})
    rows = [
        {
            **_record_columns(record),
            "dataset_source_uri": dataset.source_uri,
            "dataset_revision": dataset.revision,
            "dataset_json": json.dumps(descriptor),
        }
        for record in records
    ]
    schema = AUDIT_SCHEMA.with_metadata({
        b"evallab.audit.schema": SCHEMA.encode(),
        b"evallab.audit.datasets": json.dumps([descriptor]).encode(),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


@pytest.mark.parametrize("task_id", ["format-code-task-999999", "never-recorded-task"])
def test_missing_task_json_keeps_sources_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_id: str,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    derived = tmp_path / "derived"
    readers = tmp_path / "readers"
    monkeypatch.setenv("EVALLAB_RESULTS_HOME", str(tmp_path / "results"))
    monkeypatch.setenv("EVALLAB_READERS_STORE", str(readers))
    monkeypatch.setenv("EVALLAB_RUNS_ROOT", str(runs))
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))

    dossier = task_dossier(
        task_id, repo_root=tmp_path, roots=[runs], derived_root=derived,
        reader_store=readers, static_audit=tmp_path / "absent-static.csv",
    )
    assert dossier["trials"] == []
    for field in (
        "health", "verdict", "tags", "leak", "repair", "static_flags",
        "failing_tests", "exploit_probes",
    ):
        assert dossier[field] is None, field
    assert dossier["coverage"]["read_only"] is True
    assert not derived.exists()
    assert not readers.exists()
    assert list(runs.iterdir()) == []


@pytest.mark.parametrize("selector", ["../outside", "task/*", "task\nsecond-command"])
def test_dossier_rejects_paths_and_patterns(tmp_path: Path, selector: str) -> None:
    with pytest.raises(ValueError):
        task_dossier(selector, repo_root=tmp_path, roots=[], static_audit=None)




def test_ambiguous_audit_versions_select_no_verdict(tmp_path: Path) -> None:
    from evallab.dataset_audit_contracts import AuditDataset, AuditRecord, AuditTask

    def _task(package: str) -> AuditTask:
        return AuditTask(
            dataset_id="hello", task_id="harbor/hello-world",
            task_name="harbor/hello-world", package_digest=package,
            harbor_digest=None, aliases=("harbor/hello-world",),
            source_uri="file://hello", revision="1.0",
        )

    audit = tmp_path / "audit.parquet"
    old, new = _task("sha256:" + "a" * 64), _task("sha256:" + "b" * 64)
    _write_projection(
        [AuditRecord(task=old, verdict="keep"), AuditRecord(task=new, verdict="fix")],
        audit, dataset=AuditDataset(
            dataset_id="hello", source_uri="file://hello", revision="1.0", tasks=(old,)),
    )
    dossier = task_dossier(
        "harbor/hello-world", repo_root=tmp_path, roots=[],
        derived_root=tmp_path / "derived", reader_store=tmp_path / "readers",
        static_audit=None, audit_path=audit,
    )
    assert dossier["audit"]["status"] == "ambiguous"
    assert [row["verdict"] for row in dossier["audit"]["records"]] == ["keep", "fix"]
    assert dossier["verdict"] is None  # no audited package was guessed
    json.dumps(dossier)


@pytest.mark.parametrize(
    ("audit_digest", "trial_digest"),
    [
        ({"harbor_digest": "sha256:" + "a" * 64}, {"source_package_digest": "sha256:" + "a" * 64}),
        ({"package_digest": "sha256:" + "a" * 64}, {"task_digest": "sha256:" + "a" * 64}),
    ],
)
def test_dossier_never_compares_different_digest_algorithms(audit_digest, trial_digest):
    from evallab.dataset_audit_contracts import AuditRecord, AuditTask
    from evallab.task_dossier import dossier_for_audit

    task = AuditTask(
        dataset_id="example", task_id="example/task", task_name="example/task",
        source_uri="test://example/task", **audit_digest,
    )
    dossier = dossier_for_audit(
        AuditRecord(task=task), {"trials": [trial_digest], "coverage": {}},
    )
    assert dossier["trials"][0]["audited_package_relation"] == "unknown"


def test_dossier_reads_default_audit_projection_without_writes(tmp_path: Path):
    from evallab.dataset_audit_contracts import AuditDataset, AuditRecord, AuditTask

    derived = tmp_path / "derived"
    task = AuditTask(
        dataset_id="example", task_id="example/task", task_name="example/task",
        source_uri="test://example/task", package_digest="sha256:" + "a" * 64,
    )
    _write_projection(
        [AuditRecord(task=task, verdict="fix",
                     facets={"verdict": {"verdict": "fix", "tag": "verdict:fix"}})],
        derived / "audit.parquet",
        dataset=AuditDataset(dataset_id="example", source_uri="test://example", tasks=(task,)),
    )
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    dossier = task_dossier(
        task.task_id, repo_root=tmp_path, roots=[], derived_root=derived,
        reader_store=tmp_path / "readers", static_audit=None,
    )
    assert dossier["audit"]["status"] == "recorded"
    assert dossier["verdict"]["verdict"] == "fix"
    assert {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
