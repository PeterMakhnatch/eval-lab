"""Manifest-bound projection settlement and PostgreSQL readiness registry.

Raw evidence remains authoritative in CAS.  This module records the independent
catalog/projection lifecycle and publishes only verified Parquet files through a
manifest that DuckDB can admit without inferring readiness from the filesystem.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.types.json import Jsonb
from pydantic import Field, model_validator

from evallab.evidence_store import EvidenceLocator, materialize_evidence, reopen_evidence_archive
from evallab.interpretation.trajectory_judgment import canonical_json_digest
from evallab.results import sha256_file
from evallab.schemas import ContractModel

Digest = str
SettlementState = Literal[
    "discovered",
    "source_validated",
    "cas_committed",
    "cataloged",
    "projecting",
    "ready",
    "projection_failed",
    "quarantined",
]
ProjectionState = Literal[
    "missing",
    "projecting",
    "ready",
    "not_applicable",
    "failed",
    "stale",
    "quarantined",
]
AuthorityStatus = Literal["verified", "unverified"]
ReconciliationClass = Literal[
    "matched",
    "missing_source",
    "missing_projection",
    "extra_projection",
    "stale_producer",
    "digest_mismatch",
    "unverifiable",
]

SETTLEMENT_SCHEMA_VERSION = "projection-settlement/v1"
PROJECTION_SCHEMA_VERSION = "evallab-parquet/v1"
SETTLEMENT_DIRECTORY = "_settlement"
MANIFEST_DIRECTORY = "manifests"
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@=-]*$")


class SettlementError(RuntimeError):
    """A typed fail-closed settlement refusal."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code


class SettlementSource(ContractModel):
    """Exact independently anchored CAS authority bound to one source identity."""

    source_id: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    authority_status: AuthorityStatus
    cas_store_root: str | None = None
    cas_record_kind: str | None = None
    cas_record_id: str | None = None
    cas_record_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    cas_uri: str | None = Field(default=None, pattern=r"^cas://sha256/[0-9a-f]{64}$")
    cas_content_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    cas_archive_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    source_manifest_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_identity: dict[str, Any] | None = None
    compatibility_result: str | None = None
    authority_error: str | None = None

    @model_validator(mode="after")
    def authority_is_complete_or_absent(self) -> SettlementSource:
        authority = (
            self.cas_store_root,
            self.cas_record_kind,
            self.cas_record_id,
            self.cas_record_digest,
            self.cas_uri,
            self.cas_content_digest,
            self.cas_archive_digest,
            self.source_manifest_digest,
        )
        if self.authority_status == "verified":
            if any(value is None for value in authority):
                raise ValueError("verified source requires complete CAS authority")
            if self.authority_error is not None:
                raise ValueError("verified source cannot carry authority_error")
            locator = self.evidence_locator
            if locator.kind != self.source_kind or locator.record_id != self.source_id:
                raise ValueError("settlement source identity differs from CAS locator")
            if self.source_manifest_digest != locator.expected_record_digest:
                raise ValueError("source manifest digest differs from CAS record digest")
            if self.cas_uri != (
                f"cas://sha256/{locator.expected_content_digest.removeprefix('sha256:')}"
            ):
                raise ValueError("CAS URI differs from locator content identity")
        else:
            if any(value is not None for value in authority):
                raise ValueError("unverified source cannot carry partial CAS authority")
            if not self.authority_error:
                raise ValueError("unverified source requires authority_error")
        return self

    @property
    def evidence_locator(self) -> EvidenceLocator:
        if self.authority_status != "verified":
            raise SettlementError("unverified_cas_authority", self.source_id)
        return EvidenceLocator(
            store_root=Path(str(self.cas_store_root)),
            kind=str(self.cas_record_kind),
            record_id=str(self.cas_record_id),
            expected_record_digest=str(self.cas_record_digest),
            expected_content_digest=str(self.cas_content_digest),
        )

    @classmethod
    def from_cas_locator(cls, locator: EvidenceLocator) -> SettlementSource:
        """Authenticate and materialize one independently anchored CAS record."""

        try:
            archive, _record_bytes = reopen_evidence_archive(
                locator.store_root,
                kind=locator.kind,
                record_id=locator.record_id,
                expected_record_digest=locator.expected_record_digest,
                expected_content_digest=locator.expected_content_digest,
            )
            with materialize_evidence(locator) as restored:
                if not restored.is_dir():
                    raise ValueError("materialized CAS evidence is not a directory")
        except Exception as exc:
            raise SettlementError(
                "invalid_cas_record",
                f"{type(exc).__name__}: {exc}",
            ) from exc
        return cls(
            source_id=locator.record_id,
            source_kind=locator.kind,
            authority_status="verified",
            cas_store_root=str(locator.store_root),
            cas_record_kind=locator.kind,
            cas_record_id=locator.record_id,
            cas_record_digest=locator.expected_record_digest,
            cas_uri=archive.uri,
            cas_content_digest=archive.content_digest,
            cas_archive_digest=archive.archive_digest,
            source_manifest_digest=locator.expected_record_digest,
        )

    @classmethod
    def quarantined(cls, source_id: str, source_kind: str, reason: str) -> SettlementSource:
        return cls(
            source_id=source_id,
            source_kind=source_kind,
            authority_status="unverified",
            authority_error=reason,
        )

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def catalog_identity(self) -> tuple[Any, ...]:
        return (
            self.source_id,
            self.source_kind,
            self.cas_store_root,
            self.cas_record_kind,
            self.cas_record_id,
            self.cas_record_digest,
            self.cas_uri,
            self.cas_content_digest,
            self.cas_archive_digest,
            self.source_manifest_digest,
        )


class ProjectionColumn(ContractModel):
    name: str = Field(min_length=1)
    arrow_type: str = Field(min_length=1)
    nullable: bool


class ProjectionTableContract(ContractModel):
    table_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    partition_identity: str = Field(min_length=1)
    required: bool
    schema_version: str = Field(min_length=1)
    schema_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    columns: tuple[ProjectionColumn, ...]
    relative_path: str = Field(min_length=1)

    @model_validator(mode="after")
    def binding_is_canonical(self) -> ProjectionTableContract:
        if not self.columns:
            raise ValueError("projection table requires a typed schema")
        names = [column.name for column in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("projection schema contains duplicate columns")
        path = Path(self.relative_path)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".parquet":
            raise ValueError("relative_path must be a canonical Parquet path")
        expected = canonical_json_digest(
            {
                "schema_version": self.schema_version,
                "columns": [column.model_dump(mode="json") for column in self.columns],
            }
        )
        if self.schema_digest != expected:
            raise ValueError("schema_digest does not match the declared typed schema")
        return self

    @property
    def key(self) -> tuple[str, str]:
        return self.table_name, self.partition_identity


class ProjectionContract(ContractModel):
    schema_version: Literal["projection-contract/v1"] = "projection-contract/v1"
    producer_name: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    producer_code_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tables: tuple[ProjectionTableContract, ...]

    @model_validator(mode="after")
    def tables_are_exact_and_unique(self) -> ProjectionContract:
        if not self.tables:
            raise ValueError("projection contract requires at least one table")
        keys = [table.key for table in self.tables]
        if len(keys) != len(set(keys)):
            raise ValueError("projection contract contains duplicate table partitions")
        paths = [table.relative_path for table in self.tables]
        if len(paths) != len(set(paths)):
            raise ValueError("projection contract contains duplicate paths")
        return self

    @property
    def contract_digest(self) -> str:
        return canonical_json_digest(self.model_dump(mode="json"))


class ProjectionTableSettlement(ContractModel):
    table_name: str
    partition_identity: str
    required: bool
    schema_version: str
    schema_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    columns: tuple[ProjectionColumn, ...]
    relative_path: str
    state: ProjectionState
    source_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    file_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    row_count: int | None = Field(default=None, ge=0)
    failure_reason: str | None = None

    @model_validator(mode="after")
    def state_fields_are_exact(self) -> ProjectionTableSettlement:
        if self.state == "ready":
            if self.source_digest is None or self.file_digest is None or self.row_count is None:
                raise ValueError("ready table requires source/file digests and row count")
            if self.failure_reason is not None:
                raise ValueError("ready table cannot carry a failure reason")
        elif self.state == "not_applicable":
            if self.required:
                raise ValueError("required table cannot be not_applicable")
            if any(value is not None for value in (self.file_digest, self.row_count)):
                raise ValueError("not_applicable table cannot bind a file")
        elif self.state in {"failed", "stale", "quarantined"} and not self.failure_reason:
            raise ValueError(f"{self.state} table requires a failure reason")
        return self

    @property
    def key(self) -> tuple[str, str]:
        return self.table_name, self.partition_identity


class SettlementEvent(ContractModel):
    event_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sequence: int = Field(ge=0)
    from_state: SettlementState | None
    to_state: SettlementState
    reason_code: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime

    @model_validator(mode="after")
    def id_matches_content(self) -> SettlementEvent:
        body = self.model_dump(mode="json", exclude={"event_id"})
        if self.event_id != canonical_json_digest(body):
            raise ValueError("event_id does not match canonical event content")
        return self


class ProjectionSettlementManifest(ContractModel):
    schema_version: Literal["projection-settlement/v1"] = SETTLEMENT_SCHEMA_VERSION
    settlement_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source: SettlementSource
    contract: ProjectionContract
    rebuild_sequence: int = Field(ge=0)
    supersedes_settlement_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    state: SettlementState
    tables: tuple[ProjectionTableSettlement, ...]
    events: tuple[SettlementEvent, ...]
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def identities_and_history_are_exact(self) -> ProjectionSettlementManifest:
        expected_id = settlement_id_for(
            self.source,
            self.contract,
            rebuild_sequence=self.rebuild_sequence,
            supersedes_settlement_id=self.supersedes_settlement_id,
        )
        if self.settlement_id != expected_id:
            raise ValueError("settlement_id does not match bound authority")
        expected_digest = canonical_json_digest(
            self.model_dump(mode="json", exclude={"manifest_digest"})
        )
        if self.manifest_digest != expected_digest:
            raise ValueError("manifest_digest does not match canonical content")
        contract_keys = [table.key for table in self.contract.tables]
        table_keys = [table.key for table in self.tables]
        if table_keys != contract_keys:
            raise ValueError("settled tables are missing, duplicate, or reordered")
        for contract, table in zip(self.contract.tables, self.tables, strict=True):
            settled_contract = table.model_dump(
                mode="python",
                exclude={
                    "state",
                    "source_digest",
                    "file_digest",
                    "row_count",
                    "failure_reason",
                },
            )
            if settled_contract != contract.model_dump(mode="python"):
                raise ValueError("settled table differs from its projection contract")
            if self.state == "ready":
                if contract.required and table.state != "ready":
                    raise ValueError("ready manifest requires every required table")
                if not contract.required and table.state not in {
                    "ready",
                    "not_applicable",
                }:
                    raise ValueError(
                        "ready manifest optional tables must be ready or not_applicable"
                    )
                if table.state == "ready" and table.source_digest != self.source.cas_content_digest:
                    raise ValueError("ready table source digest differs from CAS source")
        if self.state == "ready" and self.source.authority_status != "verified":
            raise ValueError("ready manifest requires verified source authority")
        if not self.events or [event.sequence for event in self.events] != list(
            range(len(self.events))
        ):
            raise ValueError("settlement events are missing, duplicate, or reordered")
        if self.events[-1].to_state != self.state:
            raise ValueError("manifest state does not match final event")
        for previous, current in zip(self.events, self.events[1:], strict=False):
            if current.from_state != previous.to_state:
                raise ValueError("settlement event chain is discontinuous")
        return self


class ManifestLoadError(ContractModel):
    path: str
    reason: str


class ManifestInventory(ContractModel):
    manifests: tuple[ProjectionSettlementManifest, ...]
    errors: tuple[ManifestLoadError, ...]


_ALLOWED_TRANSITIONS: Mapping[SettlementState, frozenset[SettlementState]] = {
    "discovered": frozenset({"source_validated", "quarantined"}),
    "source_validated": frozenset({"cas_committed", "quarantined"}),
    "cas_committed": frozenset({"cataloged", "quarantined"}),
    "cataloged": frozenset({"projecting", "quarantined"}),
    "projecting": frozenset({"ready", "projection_failed", "quarantined"}),
    "ready": frozenset(),
    "projection_failed": frozenset(),
    "quarantined": frozenset(),
}


def _now() -> datetime:
    return datetime.now(UTC)


def _event(
    *,
    sequence: int,
    from_state: SettlementState | None,
    to_state: SettlementState,
    occurred_at: datetime,
    reason_code: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> SettlementEvent:
    body = {
        "sequence": sequence,
        "from_state": from_state,
        "to_state": to_state,
        "reason_code": reason_code,
        "detail": dict(detail or {}),
        "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
    }
    return SettlementEvent(event_id=canonical_json_digest(body), **body)


def settlement_id_for(
    source: SettlementSource,
    contract: ProjectionContract,
    *,
    rebuild_sequence: int,
    supersedes_settlement_id: str | None,
) -> str:
    return canonical_json_digest(
        {
            "schema_version": SETTLEMENT_SCHEMA_VERSION,
            "source": source.identity_payload(),
            "contract_digest": contract.contract_digest,
            "rebuild_sequence": rebuild_sequence,
            "supersedes_settlement_id": supersedes_settlement_id,
        }
    )


def _table_settlement(
    contract: ProjectionTableContract,
    *,
    state: ProjectionState = "missing",
    source_digest: str | None = None,
    file_digest: str | None = None,
    row_count: int | None = None,
    failure_reason: str | None = None,
) -> ProjectionTableSettlement:
    return ProjectionTableSettlement(
        **contract.model_dump(mode="python"),
        state=state,
        source_digest=source_digest,
        file_digest=file_digest,
        row_count=row_count,
        failure_reason=failure_reason,
    )


def create_settlement_manifest(
    source: SettlementSource,
    contract: ProjectionContract,
    *,
    rebuild_sequence: int = 0,
    supersedes_settlement_id: str | None = None,
    clock: Callable[[], datetime] = _now,
) -> ProjectionSettlementManifest:
    now = clock().astimezone(UTC)
    settlement_id = settlement_id_for(
        source,
        contract,
        rebuild_sequence=rebuild_sequence,
        supersedes_settlement_id=supersedes_settlement_id,
    )
    event = _event(
        sequence=0,
        from_state=None,
        to_state="discovered",
        occurred_at=now,
    )
    body = {
        "schema_version": SETTLEMENT_SCHEMA_VERSION,
        "settlement_id": settlement_id,
        "source": source.model_dump(mode="json"),
        "contract": contract.model_dump(mode="json"),
        "rebuild_sequence": rebuild_sequence,
        "supersedes_settlement_id": supersedes_settlement_id,
        "state": "discovered",
        "tables": [_table_settlement(table).model_dump(mode="json") for table in contract.tables],
        "events": [event.model_dump(mode="json")],
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "updated_at": now.isoformat().replace("+00:00", "Z"),
    }
    return ProjectionSettlementManifest(
        manifest_digest=canonical_json_digest(body),
        **body,
    )


def transition_settlement(
    manifest: ProjectionSettlementManifest,
    to_state: SettlementState,
    *,
    tables: Sequence[ProjectionTableSettlement] | None = None,
    reason_code: str | None = None,
    detail: Mapping[str, Any] | None = None,
    clock: Callable[[], datetime] = _now,
) -> ProjectionSettlementManifest:
    if to_state not in _ALLOWED_TRANSITIONS[manifest.state]:
        raise SettlementError("invalid_settlement_transition", f"{manifest.state} -> {to_state}")
    now = clock().astimezone(UTC)
    next_tables = tuple(tables) if tables is not None else manifest.tables
    event = _event(
        sequence=len(manifest.events),
        from_state=manifest.state,
        to_state=to_state,
        reason_code=reason_code,
        detail=detail,
        occurred_at=now,
    )
    body = manifest.model_dump(mode="json", exclude={"manifest_digest"})
    body.update(
        {
            "state": to_state,
            "tables": [table.model_dump(mode="json") for table in next_tables],
            "events": [
                *(existing.model_dump(mode="json") for existing in manifest.events),
                event.model_dump(mode="json"),
            ],
            "updated_at": now.isoformat().replace("+00:00", "Z"),
        }
    )
    return ProjectionSettlementManifest(
        manifest_digest=canonical_json_digest(body),
        **body,
    )


def columns_for_arrow_schema(schema: pa.Schema) -> tuple[ProjectionColumn, ...]:
    return tuple(
        ProjectionColumn(name=field.name, arrow_type=str(field.type), nullable=field.nullable)
        for field in schema
    )


def schema_digest_for_columns(
    columns: Sequence[ProjectionColumn], *, schema_version: str = PROJECTION_SCHEMA_VERSION
) -> str:
    return canonical_json_digest(
        {
            "schema_version": schema_version,
            "columns": [column.model_dump(mode="json") for column in columns],
        }
    )


def table_contract(
    *,
    table_name: str,
    partition_identity: str,
    required: bool,
    schema: pa.Schema,
    relative_path: str,
    schema_version: str = PROJECTION_SCHEMA_VERSION,
) -> ProjectionTableContract:
    columns = columns_for_arrow_schema(schema)
    return ProjectionTableContract(
        table_name=table_name,
        partition_identity=partition_identity,
        required=required,
        schema_version=schema_version,
        schema_digest=schema_digest_for_columns(columns, schema_version=schema_version),
        columns=columns,
        relative_path=relative_path,
    )


def producer_code_digest(paths: Sequence[Path]) -> str:
    ordered = sorted({path.resolve() for path in paths}, key=str)
    return canonical_json_digest(
        [{"path": path.name, "digest": f"sha256:{sha256_file(path)}"} for path in ordered]
    )


def settlement_manifest_path(root: Path, settlement_id: str) -> Path:
    return (
        root.resolve()
        / SETTLEMENT_DIRECTORY
        / MANIFEST_DIRECTORY
        / f"{settlement_id.removeprefix('sha256:')}.json"
    )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def write_settlement_manifest(
    root: Path,
    manifest: ProjectionSettlementManifest,
) -> Path:
    path = settlement_manifest_path(root, manifest.settlement_id)
    if path.exists():
        current = load_settlement_manifest(path)
        mode = _validate_manifest_file_replay(current, manifest)
        if mode == "replay":
            return path
    content = (
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    ).encode()
    _atomic_write(path, content)
    return path


def load_settlement_manifest(path: Path) -> ProjectionSettlementManifest:
    manifest = ProjectionSettlementManifest.model_validate_json(path.read_text())
    if path.name != f"{manifest.settlement_id.removeprefix('sha256:')}.json":
        raise ValueError("settlement manifest filename does not match settlement_id")
    return manifest


def _supersession_errors(
    manifests: Sequence[ProjectionSettlementManifest],
) -> tuple[ManifestLoadError, ...]:
    by_id = {manifest.settlement_id: manifest for manifest in manifests}
    errors: list[ManifestLoadError] = []
    children: dict[str, list[ProjectionSettlementManifest]] = {}
    roots: dict[tuple[str, str, str], list[ProjectionSettlementManifest]] = {}
    for manifest in manifests:
        group = (
            manifest.source.source_kind,
            manifest.source.source_id,
            manifest.contract.contract_digest,
        )
        parent_id = manifest.supersedes_settlement_id
        if parent_id is None:
            roots.setdefault(group, []).append(manifest)
            if manifest.rebuild_sequence != 0:
                errors.append(
                    ManifestLoadError(
                        path=manifest.settlement_id,
                        reason="rebuild settlement is missing supersedes_settlement_id",
                    )
                )
            continue
        children.setdefault(parent_id, []).append(manifest)
        parent = by_id.get(parent_id)
        if parent is None:
            errors.append(
                ManifestLoadError(
                    path=manifest.settlement_id,
                    reason=f"dangling supersession target: {parent_id}",
                )
            )
            continue
        parent_group = (
            parent.source.source_kind,
            parent.source.source_id,
            parent.contract.contract_digest,
        )
        if group != parent_group:
            errors.append(
                ManifestLoadError(
                    path=manifest.settlement_id,
                    reason="supersession crosses source or projection contract",
                )
            )
        if manifest.rebuild_sequence != parent.rebuild_sequence + 1:
            errors.append(
                ManifestLoadError(
                    path=manifest.settlement_id,
                    reason="supersession rebuild_sequence does not increment by one",
                )
            )
    for parent_id, successors in children.items():
        if len(successors) > 1:
            errors.append(
                ManifestLoadError(
                    path=parent_id,
                    reason="supersession fork has multiple active successors",
                )
            )
    for group, group_roots in roots.items():
        if len(group_roots) > 1:
            errors.append(
                ManifestLoadError(
                    path="/".join(group),
                    reason="source and contract have multiple supersession roots",
                )
            )
    for manifest in manifests:
        seen: set[str] = set()
        current: ProjectionSettlementManifest | None = manifest
        while current is not None and current.supersedes_settlement_id is not None:
            if current.settlement_id in seen:
                errors.append(
                    ManifestLoadError(
                        path=manifest.settlement_id,
                        reason="supersession cycle detected",
                    )
                )
                break
            seen.add(current.settlement_id)
            current = by_id.get(current.supersedes_settlement_id)
    return tuple(errors)


def load_settlement_manifests(root: Path) -> ManifestInventory:
    directory = root.resolve() / SETTLEMENT_DIRECTORY / MANIFEST_DIRECTORY
    manifests: list[ProjectionSettlementManifest] = []
    errors: list[ManifestLoadError] = []
    if not directory.is_dir():
        return ManifestInventory(manifests=(), errors=())
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        try:
            manifests.append(load_settlement_manifest(path))
        except Exception as exc:
            errors.append(ManifestLoadError(path=str(path), reason=f"{type(exc).__name__}: {exc}"))
    errors.extend(_supersession_errors(manifests))
    return ManifestInventory(manifests=tuple(manifests), errors=tuple(errors))


def active_settlement_manifests(
    inventory: ManifestInventory,
) -> tuple[ProjectionSettlementManifest, ...]:
    if inventory.errors:
        raise SettlementError(
            "malformed_settlement_inventory",
            "; ".join(error.reason for error in inventory.errors),
        )
    superseded = {
        manifest.supersedes_settlement_id
        for manifest in inventory.manifests
        if manifest.supersedes_settlement_id is not None
    }
    return tuple(
        sorted(
            (
                manifest
                for manifest in inventory.manifests
                if manifest.settlement_id not in superseded
            ),
            key=lambda manifest: (
                manifest.source.source_kind,
                manifest.source.source_id,
                manifest.rebuild_sequence,
                manifest.settlement_id,
            ),
        )
    )


def begin_or_resume_settlement(
    root: Path,
    source: SettlementSource,
    contract: ProjectionContract,
    *,
    clock: Callable[[], datetime] = _now,
) -> ProjectionSettlementManifest:
    inventory = load_settlement_manifests(root)
    if inventory.errors:
        raise SettlementError(
            "malformed_settlement_manifest",
            "; ".join(f"{error.path}: {error.reason}" for error in inventory.errors),
        )
    candidates = [
        manifest
        for manifest in active_settlement_manifests(inventory)
        if manifest.source.source_id == source.source_id
        and manifest.source.source_kind == source.source_kind
        and manifest.contract.contract_digest == contract.contract_digest
    ]
    if len(candidates) > 1:
        raise SettlementError("ambiguous_active_settlement", source.source_id)
    if not candidates:
        return create_settlement_manifest(source, contract, clock=clock)
    current = candidates[0]
    if current.source != source:
        return create_settlement_manifest(
            source,
            contract,
            rebuild_sequence=current.rebuild_sequence + 1,
            supersedes_settlement_id=current.settlement_id,
            clock=clock,
        )
    if current.state == "quarantined":
        return current
    if current.state == "projection_failed":
        return create_settlement_manifest(
            source,
            contract,
            rebuild_sequence=current.rebuild_sequence + 1,
            supersedes_settlement_id=current.settlement_id,
            clock=clock,
        )
    if current.state == "ready" and verify_ready_manifest(root, current):
        return current
    if current.state == "ready":
        return create_settlement_manifest(
            source,
            contract,
            rebuild_sequence=current.rebuild_sequence + 1,
            supersedes_settlement_id=current.settlement_id,
            clock=clock,
        )
    return current


def _schema_from_columns(columns: Sequence[ProjectionColumn]) -> pa.Schema:
    fields = []
    for column in columns:
        fields.append(pa.field(column.name, _arrow_type(column.arrow_type), column.nullable))
    return pa.schema(fields)


def _arrow_type(value: str) -> pa.DataType:
    direct: dict[str, pa.DataType] = {
        "null": pa.null(),
        "string": pa.string(),
        "large_string": pa.large_string(),
        "bool": pa.bool_(),
        "int8": pa.int8(),
        "int16": pa.int16(),
        "int32": pa.int32(),
        "int64": pa.int64(),
        "uint8": pa.uint8(),
        "uint16": pa.uint16(),
        "uint32": pa.uint32(),
        "uint64": pa.uint64(),
        "float": pa.float32(),
        "double": pa.float64(),
        "binary": pa.binary(),
        "large_binary": pa.large_binary(),
        "date32[day]": pa.date32(),
        "date64[ms]": pa.date64(),
    }
    if value in direct:
        return direct[value]
    if value.startswith(("list<", "large_list<")) and value.endswith(">"):
        inner = value.split(": ", 1)
        if len(inner) == 2:
            item_type = _arrow_type(inner[1][:-1])
            return (
                pa.large_list(item_type) if value.startswith("large_list<") else pa.list_(item_type)
            )
    if value.startswith("timestamp[") and value.endswith("]"):
        parameters = value[10:-1].split(", tz=", 1)
        return pa.timestamp(parameters[0], tz=parameters[1] if len(parameters) == 2 else None)
    if value.startswith("duration[") and value.endswith("]"):
        return pa.duration(value[9:-1])
    for prefix, factory in (("decimal128(", pa.decimal128), ("decimal256(", pa.decimal256)):
        if value.startswith(prefix) and value.endswith(")"):
            precision, scale = (int(part.strip()) for part in value[len(prefix) : -1].split(","))
            return factory(precision, scale)
    raise SettlementError("unsupported_manifest_arrow_type", value)


def verify_projected_table(
    root: Path,
    contract: ProjectionTableContract,
    *,
    source_digest: str,
    expected_file_digest: str | None = None,
    expected_row_count: int | None = None,
) -> ProjectionTableSettlement:
    resolved_root = root.resolve()
    path = (resolved_root / contract.relative_path).resolve(strict=True)
    if not path.is_relative_to(resolved_root):
        raise SettlementError("projection_path_escape", contract.relative_path)
    actual_schema = pq.read_schema(path)
    expected_schema = _schema_from_columns(contract.columns)
    if not actual_schema.equals(expected_schema, check_metadata=False):
        raise SettlementError(
            "projection_schema_mismatch",
            f"{contract.table_name}:{contract.partition_identity}",
        )
    actual_schema_digest = schema_digest_for_columns(
        columns_for_arrow_schema(actual_schema), schema_version=contract.schema_version
    )
    if actual_schema_digest != contract.schema_digest:
        raise SettlementError("projection_schema_digest_mismatch", contract.relative_path)
    metadata = pq.ParquetFile(path).metadata
    actual_rows = metadata.num_rows
    if expected_row_count is not None and actual_rows != expected_row_count:
        raise SettlementError(
            "projection_row_count_mismatch",
            f"expected={expected_row_count} actual={actual_rows} path={contract.relative_path}",
        )
    actual_digest = f"sha256:{sha256_file(path)}"
    if expected_file_digest is not None and actual_digest != expected_file_digest:
        raise SettlementError(
            "projection_file_digest_mismatch",
            f"expected={expected_file_digest} actual={actual_digest}",
        )
    return _table_settlement(
        contract,
        state="ready",
        source_digest=source_digest,
        file_digest=actual_digest,
        row_count=actual_rows,
    )


def verify_ready_manifest(root: Path, manifest: ProjectionSettlementManifest) -> bool:
    if manifest.state != "ready" or manifest.source.cas_content_digest is None:
        return False
    try:
        for contract, table in zip(manifest.contract.tables, manifest.tables, strict=True):
            if table.state == "not_applicable" and not contract.required:
                continue
            if table.state != "ready":
                return False
            verified = verify_projected_table(
                root,
                contract,
                source_digest=manifest.source.cas_content_digest,
                expected_file_digest=table.file_digest,
                expected_row_count=table.row_count,
            )
            if verified.file_digest != table.file_digest:
                return False
            if table.source_digest != manifest.source.cas_content_digest:
                return False
    except (OSError, ValueError, SettlementError):
        return False
    return True


def _execute_checked(connection: Any, query: str, parameters: Sequence[Any]) -> None:
    cursor = connection.execute(query, tuple(parameters))
    rowcount = getattr(cursor, "rowcount", 1)
    if rowcount == 0:
        raise SettlementError("catalog_authority_mismatch", "conflicting settlement row")


def _event_row(event: SettlementEvent) -> tuple[Any, ...]:
    return (
        event.sequence,
        event.event_id,
        event.from_state,
        event.to_state,
        event.reason_code,
        event.detail,
        event.occurred_at,
    )


def _table_row(
    table: ProjectionTableSettlement,
    contract: ProjectionContract,
) -> tuple[Any, ...]:
    return (
        table.table_name,
        table.partition_identity,
        table.required,
        table.state,
        table.schema_version,
        table.schema_digest,
        [column.model_dump(mode="json") for column in table.columns],
        table.relative_path,
        table.source_digest,
        table.file_digest,
        table.row_count,
        table.failure_reason,
        contract.producer_name,
        contract.producer_version,
        contract.producer_code_digest,
    )


def _validate_manifest_file_replay(
    current: ProjectionSettlementManifest,
    incoming: ProjectionSettlementManifest,
) -> Literal["replay", "advance"]:
    source = current.source
    contract = current.contract
    current_row = (
        current.state,
        current.manifest_digest,
        *source.catalog_identity(),
        contract.contract_digest,
        contract.producer_code_digest,
        current.rebuild_sequence,
        current.supersedes_settlement_id,
    )
    mode = _validate_ledger_replay(
        incoming,
        current_row,
        tuple(_event_row(event) for event in current.events),
        tuple(_table_row(table, contract) for table in current.tables),
    )
    if mode == "insert":
        raise SettlementError(
            "filesystem_manifest_authority_missing",
            incoming.settlement_id,
        )
    return mode


_TABLE_TRANSITIONS: Mapping[ProjectionState, frozenset[ProjectionState]] = {
    "missing": frozenset({"missing", "projecting", "quarantined"}),
    "projecting": frozenset(
        {"projecting", "ready", "not_applicable", "failed", "stale", "quarantined"}
    ),
    "ready": frozenset({"ready"}),
    "not_applicable": frozenset({"not_applicable"}),
    "failed": frozenset({"failed"}),
    "stale": frozenset({"stale"}),
    "quarantined": frozenset({"quarantined"}),
}


def _validate_ledger_replay(
    manifest: ProjectionSettlementManifest,
    current_row: tuple[Any, ...] | None,
    persisted_events: Sequence[tuple[Any, ...]],
    persisted_tables: Sequence[tuple[Any, ...]],
) -> Literal["insert", "replay", "advance"]:
    """Validate a locked PostgreSQL snapshot before any mutation."""
    if current_row is None:
        if persisted_events or persisted_tables:
            raise SettlementError(
                "catalog_orphan_rows",
                "events or tables exist without a settlement row",
            )
        return "insert"

    source = manifest.source
    contract = manifest.contract
    expected_authority = (
        *source.catalog_identity(),
        contract.contract_digest,
        contract.producer_code_digest,
        manifest.rebuild_sequence,
        manifest.supersedes_settlement_id,
    )
    if current_row[2:] != expected_authority:
        raise SettlementError(
            "catalog_authority_mismatch",
            "locked settlement authority differs from manifest",
        )

    expected_events = tuple(_event_row(event) for event in manifest.events)
    persisted_event_tuple = tuple(persisted_events)
    if len(persisted_event_tuple) > len(expected_events):
        raise SettlementError("catalog_history_regression", manifest.settlement_id)
    if persisted_event_tuple != expected_events[: len(persisted_event_tuple)]:
        raise SettlementError("catalog_history_fork", manifest.settlement_id)

    expected_tables = {
        row[:2]: row for row in (_table_row(table, contract) for table in manifest.tables)
    }
    current_tables = {row[:2]: row for row in persisted_tables}
    if set(current_tables) != set(expected_tables):
        raise SettlementError(
            "catalog_table_set_divergence",
            manifest.settlement_id,
        )
    contract_indexes = (0, 1, 2, 4, 5, 6, 7, 12, 13, 14)
    for key, current in current_tables.items():
        expected = expected_tables[key]
        if tuple(current[index] for index in contract_indexes) != tuple(
            expected[index] for index in contract_indexes
        ):
            raise SettlementError(
                "catalog_table_contract_divergence",
                f"{key}",
            )

    current_state, current_digest = current_row[:2]
    if len(persisted_event_tuple) == len(expected_events):
        if current_state != manifest.state or current_digest != manifest.manifest_digest:
            raise SettlementError(
                "catalog_replay_digest_mismatch",
                manifest.settlement_id,
            )
        if current_tables != expected_tables:
            raise SettlementError(
                "catalog_table_replay_divergence",
                manifest.settlement_id,
            )
        return "replay"

    if len(expected_events) != len(persisted_event_tuple) + 1:
        raise SettlementError(
            "catalog_transition_skip",
            manifest.settlement_id,
        )
    appended = manifest.events[-1]
    if (
        appended.from_state != current_state
        or appended.to_state != manifest.state
        or manifest.state not in _ALLOWED_TRANSITIONS[current_state]
    ):
        raise SettlementError(
            "catalog_illegal_transition",
            f"{current_state}->{manifest.state}",
        )
    for key, current in current_tables.items():
        expected = expected_tables[key]
        current_table_state = current[3]
        expected_table_state = expected[3]
        if expected_table_state not in _TABLE_TRANSITIONS[current_table_state]:
            raise SettlementError(
                "catalog_illegal_table_transition",
                f"{key}: {current_table_state}->{expected_table_state}",
            )
        if current_table_state == expected_table_state and current != expected:
            raise SettlementError(
                "catalog_table_state_divergence",
                f"{key}",
            )
    return "advance"


def persist_settlement_manifest(
    database_url: str,
    manifest: ProjectionSettlementManifest,
) -> None:
    """Persist exactly one append-only ledger transition or an exact replay."""
    source = manifest.source
    contract = manifest.contract
    with psycopg.connect(database_url) as connection:
        current_row = connection.execute(
            """
            SELECT state, manifest_digest, source_id, source_kind,
                   cas_store_root, cas_record_kind, cas_record_id,
                   cas_record_digest, cas_uri, cas_content_digest,
                   cas_archive_digest, source_manifest_digest, contract_digest,
                   producer_code_digest, rebuild_sequence, supersedes_settlement_id
            FROM projection_settlements
            WHERE settlement_id = %s
            FOR UPDATE
            """,
            (manifest.settlement_id,),
        ).fetchone()
        persisted_events = connection.execute(
            """
            SELECT sequence, event_id, from_state, to_state, reason_code, detail,
                   occurred_at
            FROM projection_settlement_events
            WHERE settlement_id = %s
            ORDER BY sequence
            FOR UPDATE
            """,
            (manifest.settlement_id,),
        ).fetchall()
        persisted_tables = connection.execute(
            """
            SELECT table_name, partition_identity, required, state, schema_version,
                   schema_digest, columns_json, relative_path, source_digest,
                   file_digest, row_count, failure_reason, producer_name,
                   producer_version, producer_code_digest
            FROM projection_table_settlements
            WHERE settlement_id = %s
            ORDER BY table_name, partition_identity
            FOR UPDATE
            """,
            (manifest.settlement_id,),
        ).fetchall()
        mode = _validate_ledger_replay(
            manifest,
            current_row,
            persisted_events,
            persisted_tables,
        )
        if mode == "replay":
            return

        if mode == "insert":
            _execute_checked(
                connection,
                """
                INSERT INTO projection_settlements (
                    settlement_id, source_id, source_kind, state, authority_status,
                    cas_store_root, cas_record_kind, cas_record_id, cas_record_digest,
                    cas_uri, cas_content_digest, cas_archive_digest,
                    source_manifest_digest, runtime_identity, compatibility_result,
                    authority_error, required_tables, optional_tables, producer_name,
                    producer_version, producer_code_digest, contract_digest,
                    rebuild_sequence, supersedes_settlement_id, manifest_digest,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    manifest.settlement_id,
                    source.source_id,
                    source.source_kind,
                    manifest.state,
                    source.authority_status,
                    source.cas_store_root,
                    source.cas_record_kind,
                    source.cas_record_id,
                    source.cas_record_digest,
                    source.cas_uri,
                    source.cas_content_digest,
                    source.cas_archive_digest,
                    source.source_manifest_digest,
                    Jsonb(source.runtime_identity) if source.runtime_identity is not None else None,
                    source.compatibility_result,
                    source.authority_error,
                    Jsonb([table.table_name for table in contract.tables if table.required]),
                    Jsonb([table.table_name for table in contract.tables if not table.required]),
                    contract.producer_name,
                    contract.producer_version,
                    contract.producer_code_digest,
                    contract.contract_digest,
                    manifest.rebuild_sequence,
                    manifest.supersedes_settlement_id,
                    manifest.manifest_digest,
                    manifest.created_at,
                    manifest.updated_at,
                ),
            )
            events_to_insert = manifest.events
        else:
            if current_row is None:
                raise AssertionError("advance settlement requires a locked current row")
            _execute_checked(
                connection,
                """
                UPDATE projection_settlements
                SET state = %s, manifest_digest = %s, updated_at = %s
                WHERE settlement_id = %s AND state = %s AND manifest_digest = %s
                """,
                (
                    manifest.state,
                    manifest.manifest_digest,
                    manifest.updated_at,
                    manifest.settlement_id,
                    current_row[0],
                    current_row[1],
                ),
            )
            events_to_insert = (manifest.events[-1],)

        for event in events_to_insert:
            _execute_checked(
                connection,
                """
                INSERT INTO projection_settlement_events (
                    event_id, settlement_id, sequence, from_state, to_state,
                    reason_code, detail, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event.event_id,
                    manifest.settlement_id,
                    event.sequence,
                    event.from_state,
                    event.to_state,
                    event.reason_code,
                    Jsonb(event.detail),
                    event.occurred_at,
                ),
            )
        for table in manifest.tables:
            if mode == "insert":
                query = """
                    INSERT INTO projection_table_settlements (
                        settlement_id, table_name, partition_identity, required,
                        state, schema_version, schema_digest, columns_json,
                        relative_path, source_digest, file_digest, row_count,
                        failure_reason, producer_name, producer_version,
                        producer_code_digest, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                """
                parameters = (
                    manifest.settlement_id,
                    table.table_name,
                    table.partition_identity,
                    table.required,
                    table.state,
                    table.schema_version,
                    table.schema_digest,
                    Jsonb([column.model_dump(mode="json") for column in table.columns]),
                    table.relative_path,
                    table.source_digest,
                    table.file_digest,
                    table.row_count,
                    table.failure_reason,
                    contract.producer_name,
                    contract.producer_version,
                    contract.producer_code_digest,
                    manifest.updated_at,
                )
            else:
                query = """
                    UPDATE projection_table_settlements
                    SET required = %s, state = %s, schema_version = %s,
                        schema_digest = %s, columns_json = %s, relative_path = %s,
                        source_digest = %s, file_digest = %s, row_count = %s,
                        failure_reason = %s, producer_name = %s,
                        producer_version = %s, producer_code_digest = %s,
                        updated_at = %s
                    WHERE settlement_id = %s AND table_name = %s
                      AND partition_identity = %s
                """
                parameters = (
                    table.required,
                    table.state,
                    table.schema_version,
                    table.schema_digest,
                    Jsonb([column.model_dump(mode="json") for column in table.columns]),
                    table.relative_path,
                    table.source_digest,
                    table.file_digest,
                    table.row_count,
                    table.failure_reason,
                    contract.producer_name,
                    contract.producer_version,
                    contract.producer_code_digest,
                    manifest.updated_at,
                    manifest.settlement_id,
                    table.table_name,
                    table.partition_identity,
                )
            _execute_checked(connection, query, parameters)


def persist_and_write_settlement(
    database_url: str,
    root: Path,
    manifest: ProjectionSettlementManifest,
) -> Path:
    """Commit PostgreSQL authority before publishing its filesystem manifest."""

    persist_settlement_manifest(database_url, manifest)
    return write_settlement_manifest(root, manifest)


def ready_table_settlements(
    root: Path,
) -> tuple[ProjectionSettlementManifest, ...]:
    inventory = load_settlement_manifests(root)
    if inventory.errors:
        raise SettlementError(
            "malformed_settlement_manifest",
            "; ".join(error.reason for error in inventory.errors),
        )
    ready = []
    for manifest in active_settlement_manifests(inventory):
        if manifest.state != "ready":
            continue
        if not verify_ready_manifest(root, manifest):
            raise SettlementError("stale_ready_manifest", manifest.settlement_id)
        ready.append(manifest)
    return tuple(ready)


__all__ = [
    "MANIFEST_DIRECTORY",
    "PROJECTION_SCHEMA_VERSION",
    "SETTLEMENT_DIRECTORY",
    "ManifestInventory",
    "ManifestLoadError",
    "ProjectionColumn",
    "ProjectionContract",
    "ProjectionSettlementManifest",
    "ProjectionState",
    "ProjectionTableContract",
    "ProjectionTableSettlement",
    "SettlementError",
    "SettlementSource",
    "SettlementState",
    "active_settlement_manifests",
    "begin_or_resume_settlement",
    "columns_for_arrow_schema",
    "create_settlement_manifest",
    "load_settlement_manifest",
    "load_settlement_manifests",
    "persist_and_write_settlement",
    "persist_settlement_manifest",
    "producer_code_digest",
    "ready_table_settlements",
    "schema_digest_for_columns",
    "settlement_manifest_path",
    "table_contract",
    "transition_settlement",
    "verify_projected_table",
    "verify_ready_manifest",
    "write_settlement_manifest",
]
