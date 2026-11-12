"""Version-preserving dataset audits in the existing derived Parquet lake."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.dataset_audit_contracts import ALL_STAGES, AuditDataset, AuditRecord

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


