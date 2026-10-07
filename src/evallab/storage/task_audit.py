"""Version-preserving dataset audits in the existing derived Parquet lake."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.dataset_audit_contracts import ALL_STAGES, AuditDataset, AuditRecord
from evallab.evidence.parquet_io import parquet_root_lock, write_table_atomic

SCHEMA = "evallab.task_audit/v1"
AUDIT_SCHEMA = pa.schema([
    pa.field("dataset_id", pa.string(), nullable=False),
    pa.field("dataset_source_uri", pa.string(), nullable=False),
    pa.field("dataset_revision", pa.string()),
    pa.field("task_id", pa.string(), nullable=False),
    pa.field("task_name", pa.string(), nullable=False),
    pa.field("task_path", pa.string()),
    pa.field("package_digest", pa.string()),
    pa.field("harbor_digest", pa.string()),
    pa.field("source_uri", pa.string(), nullable=False),
    pa.field("revision", pa.string()),
    pa.field("aliases", pa.list_(pa.string()), nullable=False),
    pa.field("verdict", pa.string(), nullable=False),
    pa.field("verdict_reason", pa.string()),
    pa.field("tags", pa.list_(pa.string()), nullable=False),
    *(pa.field(f"{stage}_status", pa.string()) for stage in ALL_STAGES),
    pa.field("record_json", pa.string(), nullable=False),
    pa.field("dataset_json", pa.string(), nullable=False),
])
_IDENTITY_COLUMNS = (
    "dataset_id", "dataset_source_uri", "dataset_revision", "task_id",
    "source_uri", "revision", "package_digest", "harbor_digest",
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _identity(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row[column] for column in _IDENTITY_COLUMNS)


def _record_columns(record: AuditRecord) -> dict[str, Any]:
    task = record.task
    return {
        "dataset_id": task.dataset_id,
        "task_id": task.task_id,
        "task_name": task.task_name,
        "task_path": str(task.path) if task.path is not None else None,
        "package_digest": task.package_digest,
        "harbor_digest": task.harbor_digest,
        "source_uri": task.source_uri,
        "revision": task.revision,
        "aliases": list(task.aliases),
        "verdict": record.verdict,
        "verdict_reason": record.verdict_reason,
        "tags": list(record.tags),
        **{f"{stage}_status": record.stages[stage].status if stage in record.stages else None
           for stage in ALL_STAGES},
        "record_json": _json(record.model_dump(mode="json")),
    }


def read_audit_table(path: Path) -> pa.Table:
    """Read validated records; absence is typed empty, corruption is an error."""
    path = Path(path)
    if not path.exists():
        return pa.Table.from_pylist([], schema=AUDIT_SCHEMA)
    try:
        table = pq.ParquetFile(path).read()
        if not table.schema.equals(AUDIT_SCHEMA, check_metadata=False):
            raise ValueError("unsupported audit column schema")
        metadata = table.schema.metadata or {}
        if metadata.get(b"evallab.audit.schema") != SCHEMA.encode():
            raise ValueError("missing or unsupported audit provenance schema")
        descriptors = json.loads(metadata[b"evallab.audit.datasets"])
        if not isinstance(descriptors, list):
            raise ValueError("dataset provenance must be a list")
        declared = {_json(value) for value in descriptors}
        seen: set[tuple[Any, ...]] = set()
        for row in table.to_pylist():
            record = AuditRecord.model_validate_json(row["record_json"])
            expected = _record_columns(record)
            if any(row[key] != value for key, value in expected.items()):
                raise ValueError("audit typed columns disagree with the complete record")
            descriptor = json.loads(row["dataset_json"])
            dataset = AuditDataset.model_validate({**descriptor, "tasks": [record.task]})
            if dataset.dataset_id != record.task.dataset_id:
                raise ValueError("record and dataset identities disagree")
            if (
                row["dataset_source_uri"] != dataset.source_uri
                or row["dataset_revision"] != dataset.revision
                or _json(descriptor) not in declared
            ):
                raise ValueError("audit row and canonical dataset provenance disagree")
            identity = _identity(row)
            if identity in seen:
                raise ValueError("duplicate audit package identity")
            seen.add(identity)
        return table
    except (OSError, ValueError, KeyError, TypeError, pa.ArrowException) as exc:
        raise ValueError(f"invalid audit projection {path}: {exc}") from exc


def read_audit_records(path: Path) -> list[AuditRecord]:
    """Restore complete frozen contracts without collapsing task versions."""
    return [AuditRecord.model_validate_json(value)
            for value in read_audit_table(path).column("record_json").to_pylist()]


def write_audit(
    records: Sequence[AuditRecord], path: Path, *, dataset: AuditDataset,
) -> Path:
    """Refresh only identical dataset/task/package versions, atomically.

    Other datasets and historical package versions remain in the root table.
    Stage facts stay inside the single complete record, never fan out trials.
    """
    path = Path(path)
    descriptor = dataset.model_dump(mode="json", exclude={"tasks"})
    descriptor_json = _json(descriptor)
    selected = {task.task_id: task for task in dataset.tasks}
    if len(selected) != len(dataset.tasks):
        raise ValueError("an audit dataset must have unique canonical task ids")
    replacements: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        if record.task.dataset_id != dataset.dataset_id or selected.get(record.task.task_id) != record.task:
            raise ValueError(f"audit record is not the selected dataset package: {record.task.task_id}")
        row = {
            **_record_columns(record),
            "dataset_source_uri": dataset.source_uri,
            "dataset_revision": dataset.revision,
            "dataset_json": descriptor_json,
        }
        identity = _identity(row)
        if identity in replacements:
            raise ValueError(f"duplicate input audit package: {record.task.task_id}")
        replacements[identity] = row
    with parquet_root_lock(path.parent):
        rows = {_identity(row): row for row in read_audit_table(path).to_pylist()}
        rows.update(replacements)
        ordered = sorted(rows.values(), key=lambda row: _json(_identity(row)))
        declared = {row["dataset_json"] for row in ordered}
        if replacements:
            declared.add(descriptor_json)
        datasets = sorted(declared)
        metadata = {
            b"evallab.audit.schema": SCHEMA.encode(),
            b"evallab.audit.projector": b"evallab.storage.task_audit.write_audit",
            b"evallab.audit.datasets": _json([json.loads(value) for value in datasets]).encode(),
        }
        write_table_atomic(path, ordered, AUDIT_SCHEMA.with_metadata(metadata))
    return path
