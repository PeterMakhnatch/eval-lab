"""Storage writing, manifest generation, and CAS archiving for Inspect AI source evidence."""

from __future__ import annotations

import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from evallab.evidence.parquet_io import write_table_atomic
from evallab.evidence_store import archive_evidence
from evallab.results import sha256_file
from evallab.schemas import ContractModel

if TYPE_CHECKING:
    from evallab.inspect_adapter import InspectIngestResult, InspectProjection


class InspectSourceManifestV1(ContractModel):
    """Manifest linking an ingested Inspect AI evaluation log to its CAS archive and projected source tables."""

    schema_version: Literal["inspect-source-manifest/v1"] = "inspect-source-manifest/v1"
    evidence_only: bool = True
    projector_identity: str = "evallab.inspect_adapter"
    projector_version: str = "1.0.0"
    job_id: str
    source_revision_id: str
    identity_source: str
    eval_id: str | None = None
    run_id: str | None = None
    source_revision: str | None = None
    source_digest: str
    source_file: str
    source_bytes_size: int = Field(ge=0)
    raw_cas_uri: str
    rebuild_digest: str
    inspect_log_version: int | None = None
    status: str
    task_name: str | None = None
    model_name: str | None = None
    sample_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    score_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
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
    raw_cas_uri: str,
    table_paths: dict[str, Path] | None = None,
) -> InspectSourceManifestV1:
    """Create a validated source manifest for one Inspect AI source projection."""
    table_row_counts = {
        "inspect_runs": 1,
        "inspect_attempts": len(projection.attempts),
        "inspect_scores": len(projection.scores),
        "inspect_events": len(projection.events),
        "inspect_attachments": len(projection.attachments),
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
        schema_version="inspect-source-manifest/v1",
        evidence_only=True,
        projector_identity="evallab.inspect_adapter",
        projector_version="1.0.0",
        job_id=projection.run.job_id,
        source_revision_id=projection.run.source_revision_id,
        identity_source=projection.run.identity_source,
        eval_id=projection.run.eval_id,
        run_id=projection.run.run_id,
        source_revision=projection.run.source_revision,
        source_digest=projection.run.source_digest,
        source_file=source_file,
        source_bytes_size=source_bytes_size,
        raw_cas_uri=raw_cas_uri,
        rebuild_digest=projection.rebuild_digest,
        inspect_log_version=projection.run.inspect_log_version,
        status=projection.run.status,
        task_name=projection.run.task_name,
        model_name=projection.run.model_name,
        sample_count=projection.run.sample_count,
        attempt_count=len(projection.attempts),
        score_count=len(projection.scores),
        event_count=len(projection.events),
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
    raw_cas_uri: str | None = None,
) -> dict[str, Path]:
    """Write Inspect-native source tables into a discoverable job and revision partitioned Parquet root."""
    paths, _ = write_inspect_projection_with_manifest(
        projection,
        output_root,
        write_manifest=write_manifest,
        source_file=source_file,
        source_bytes_size=source_bytes_size,
        raw_cas_uri=raw_cas_uri,
    )
    return paths


def write_inspect_projection_with_manifest(
    projection: InspectProjection,
    output_root: Path,
    *,
    write_manifest: bool = True,
    source_file: str | None = None,
    source_bytes_size: int | None = None,
    raw_cas_uri: str | None = None,
) -> tuple[dict[str, Path], InspectSourceManifestV1 | None]:
    """Write Inspect-native source tables and return table paths plus the exact persisted manifest."""
    from evallab.inspect_adapter import INSPECT_SCHEMAS

    root = (
        output_root.resolve()
        / f"job_id={projection.run.job_id}"
        / f"revision_id={projection.run.source_revision_id}"
    )
    root.mkdir(parents=True, exist_ok=True)

    table_rows: dict[str, list[dict[str, Any]]] = {
        "inspect_runs": [projection.run.model_dump(mode="json")],
        "inspect_attempts": [row.model_dump(mode="json") for row in projection.attempts],
        "inspect_scores": [row.model_dump(mode="json") for row in projection.scores],
        "inspect_events": [row.model_dump(mode="json") for row in projection.events],
        "inspect_attachments": [row.model_dump(mode="json") for row in projection.attachments],
    }

    paths: dict[str, Path] = {}
    for name, rows in table_rows.items():
        schema = INSPECT_SCHEMAS[name]
        path = root / f"{name}.parquet"
        write_table_atomic(path, rows, schema)
        paths[name] = path

    manifest: InspectSourceManifestV1 | None = None
    if write_manifest:
        if not raw_cas_uri:
            raise ValueError(
                "write_inspect_projection with write_manifest requires a valid raw_cas_uri"
            )
        src_name = source_file or projection.run.source_path
        src_size = source_bytes_size if source_bytes_size is not None else 0
        manifest = create_inspect_source_manifest(
            projection,
            source_file=src_name,
            source_bytes_size=src_size,
            raw_cas_uri=raw_cas_uri,
            table_paths=paths,
        )
        manifest_path = root / "source-manifest.json"
        manifest_path.write_text(
            manifest.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )

    return paths, manifest


def ingest_inspect_eval_log(
    path: Path,
    *,
    output_root: Path,
    store_root: Path,
) -> InspectIngestResult:
    """Read official .eval log, archive to CAS, normalize, and project Inspect source tables."""
    from evallab.inspect_adapter import (
        InspectIngestResult,
        load_inspect_eval_log,
        project_inspect_eval_log,
    )

    path = path.resolve()
    if path.suffix != ".eval":
        raise ValueError(
            f"Production Inspect ingest accepts official .eval files only (got {path.name}); "
            "use load_inspect_eval_fixture_json/project_inspect_eval_log for test fixtures"
        )

    source_bytes = path.read_bytes()
    payload = load_inspect_eval_log(path)
    projection = project_inspect_eval_log(
        payload,
        source_path=path.name,
        source_bytes=source_bytes,
        validator="inspect_ai.log.read_eval_log",
    )

    # Archive raw source bytes only to CAS (mandatory)
    store_root = store_root.resolve()
    with tempfile.TemporaryDirectory(prefix="evallab-inspect-") as temporary:
        staging = Path(temporary)
        shutil.copy2(path, staging / path.name)
        archive = archive_evidence(
            staging,
            store_root,
            record_id=projection.run.source_revision_id,
            kind="inspect_eval_log",
        )
        cas_uri = archive.uri

    # Final manifest beside Parquet with real CAS URI and populated table_digests
    table_paths, manifest = write_inspect_projection_with_manifest(
        projection,
        output_root,
        write_manifest=True,
        source_file=path.name,
        source_bytes_size=len(source_bytes),
        raw_cas_uri=cas_uri,
    )

    return InspectIngestResult(
        projection=projection,
        table_paths=table_paths,
        raw_cas_uri=cas_uri,
        source_manifest=manifest,
    )
