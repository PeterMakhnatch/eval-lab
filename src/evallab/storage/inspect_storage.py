"""Storage writing, manifest generation, and CAS archiving for Inspect AI evaluations."""

from __future__ import annotations

import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from evallab.evidence.atif import PARQUET_SCHEMAS
from evallab.evidence.parquet_io import write_table_atomic
from evallab.evidence_store import archive_evidence
from evallab.results import sha256_file
from evallab.schemas import ContractModel

if TYPE_CHECKING:
    from evallab.inspect_adapter import InspectIngestResult, InspectProjection


class InspectSourceManifestV1(ContractModel):
    """Manifest linking an ingested Inspect AI evaluation log to its CAS archive and projected tables."""

    schema_version: Literal["inspect-source-manifest/v1"] = "inspect-source-manifest/v1"
    job_id: str
    source_digest: str
    source_file: str
    source_bytes_size: int = Field(ge=0)
    rebuild_digest: str
    inspect_log_version: int | None = None
    status: str
    task_name: str | None = None
    model_name: str | None = None
    run_id: str | None = None
    sample_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    score_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    step_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    observation_count: int = Field(ge=0)
    attachment_count: int = Field(default=0, ge=0)
    table_row_counts: dict[str, int]
    table_digests: dict[str, str] = Field(default_factory=dict)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str


def create_inspect_source_manifest(
    projection: InspectProjection,
    *,
    source_file: str,
    source_bytes_size: int,
    table_paths: dict[str, Path] | None = None,
) -> InspectSourceManifestV1:
    """Create a validated source manifest for one Inspect AI projection."""
    table_row_counts = {
        "inspect_runs": 1,
        "inspect_attempts": len(projection.attempts),
        "inspect_scores": len(projection.scores),
        "inspect_events": len(projection.events),
        "inspect_attachments": len(projection.attachments),
        "trajectories": len(projection.trajectories.trajectories),
        "steps": len(projection.trajectories.steps),
        "tool_calls": len(projection.trajectories.tool_calls),
        "observations": len(projection.trajectories.observations),
    }
    table_digests: dict[str, str] = {}
    if table_paths:
        for name, path in sorted(table_paths.items()):
            if path.is_file():
                table_digests[name] = f"sha256:{sha256_file(path)}"

    attachment_manifests = [
        att.model_dump(mode="json") if hasattr(att, "model_dump") else dict(att)
        for att in projection.attachments
    ]

    return InspectSourceManifestV1(
        job_id=projection.run.job_id,
        source_digest=projection.run.source_digest,
        source_file=source_file,
        source_bytes_size=source_bytes_size,
        rebuild_digest=projection.rebuild_digest,
        inspect_log_version=projection.run.inspect_log_version,
        status=projection.run.status,
        task_name=projection.run.task_name,
        model_name=projection.run.model_name,
        run_id=projection.run.run_id,
        sample_count=projection.run.sample_count,
        attempt_count=len(projection.attempts),
        score_count=len(projection.scores),
        event_count=len(projection.events),
        step_count=len(projection.trajectories.steps),
        tool_call_count=len(projection.trajectories.tool_calls),
        observation_count=len(projection.trajectories.observations),
        attachment_count=len(projection.attachments),
        table_row_counts=table_row_counts,
        table_digests=table_digests,
        attachments=attachment_manifests,
        created_at=datetime.now(UTC).isoformat(),
    )


def write_inspect_projection(
    projection: InspectProjection,
    output_root: Path,
    *,
    write_manifest: bool = True,
    source_file: str | None = None,
    source_bytes_size: int | None = None,
) -> dict[str, Path]:
    """Write Inspect-native and canonical facts into one partitioned Parquet root."""
    from evallab.inspect_adapter import INSPECT_SCHEMAS

    root = output_root.resolve() / "source=inspect" / f"job_id={projection.run.job_id}"
    root.mkdir(parents=True, exist_ok=True)

    from dataclasses import asdict

    table_rows: dict[str, list[dict[str, Any]]] = {
        "inspect_runs": [projection.run.model_dump(mode="json")],
        "inspect_attempts": [row.model_dump(mode="json") for row in projection.attempts],
        "inspect_scores": [row.model_dump(mode="json") for row in projection.scores],
        "inspect_events": [row.model_dump(mode="json") for row in projection.events],
        "inspect_attachments": [row.model_dump(mode="json") for row in projection.attachments],
        "trajectories": [asdict(row) for row in projection.trajectories.trajectories],
        "steps": [asdict(row) for row in projection.trajectories.steps],
        "tool_calls": [asdict(row) for row in projection.trajectories.tool_calls],
        "observations": [asdict(row) for row in projection.trajectories.observations],
    }

    paths: dict[str, Path] = {}
    for name, rows in table_rows.items():
        schema = INSPECT_SCHEMAS.get(name) or PARQUET_SCHEMAS[name]
        path = root / f"{name}.parquet"
        write_table_atomic(path, rows, schema)
        paths[name] = path

    if write_manifest:
        src_name = source_file or projection.run.source_path
        src_size = source_bytes_size if source_bytes_size is not None else 0
        manifest = create_inspect_source_manifest(
            projection,
            source_file=src_name,
            source_bytes_size=src_size,
            table_paths=paths,
        )
        manifest_path = root / "source-manifest.json"
        manifest_path.write_text(
            manifest.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )

    return paths


def ingest_inspect_eval_log(
    path: Path,
    *,
    output_root: Path,
    store_root: Path | None = None,
) -> InspectIngestResult:
    """Read, optionally archive, normalize, and project one Inspect evaluation log."""
    from evallab.inspect_adapter import (
        InspectIngestResult,
        load_inspect_eval_log,
        project_inspect_eval_log,
    )

    path = path.resolve()
    source_bytes = path.read_bytes()
    payload = load_inspect_eval_log(path)
    projection = project_inspect_eval_log(
        payload,
        source_path=path.name,
        source_bytes=source_bytes,
    )
    table_paths = write_inspect_projection(
        projection,
        output_root,
        write_manifest=True,
        source_file=path.name,
        source_bytes_size=len(source_bytes),
    )

    cas_uri: str | None = None
    manifest = create_inspect_source_manifest(
        projection,
        source_file=path.name,
        source_bytes_size=len(source_bytes),
        table_paths=table_paths,
    )

    if store_root is not None:
        with tempfile.TemporaryDirectory(prefix="evallab-inspect-") as temporary:
            staging = Path(temporary)
            shutil.copy2(path, staging / path.name)
            (staging / "source-manifest.json").write_text(
                manifest.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            archive = archive_evidence(
                staging,
                store_root,
                record_id=projection.run.job_id,
                kind="inspect_eval_log",
            )
            cas_uri = archive.uri

    return InspectIngestResult(
        projection=projection,
        table_paths=table_paths,
        raw_cas_uri=cas_uri,
        source_manifest=manifest,
    )
