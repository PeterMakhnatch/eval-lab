"""Read-only reconciliation across CAS, settlement, catalog, and Parquet state."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from evallab.results import sha256_file
from evallab.storage.settlement import (
    ProjectionSettlementManifest,
    SettlementSource,
    active_settlement_manifests,
    load_settlement_manifests,
)

ReconciliationState = Literal[
    "matched",
    "missing_source",
    "missing_projection",
    "extra_projection",
    "stale_producer",
    "digest_mismatch",
    "unverifiable",
]


@dataclass(frozen=True)
class ReconciliationEntry:
    source_kind: str | None
    source_id: str | None
    table_name: str | None
    partition_identity: str | None
    state: ReconciliationState
    reason: str
    source_record_path: Path | None = None
    settlement_id: str | None = None
    projection_path: Path | None = None

    @property
    def sort_key(self) -> tuple[str, ...]:
        return (
            self.source_kind or "",
            self.source_id or "",
            self.table_name or "",
            self.partition_identity or "",
            self.state,
            str(self.projection_path or ""),
            self.reason,
        )


@dataclass(frozen=True)
class ReconciliationInventory:
    entries: tuple[ReconciliationEntry, ...]

    @property
    def counts(self) -> dict[str, int]:
        counts = Counter(entry.state for entry in self.entries)
        return {state: counts[state] for state in sorted(counts)}

    @property
    def clean(self) -> bool:
        return all(entry.state == "matched" for entry in self.entries)


def read_postgres_settlement_rows(database_url: str) -> tuple[dict[str, Any], ...]:
    """Read the settlement registry without changing catalog state."""
    import psycopg

    query = """
        SELECT settlement_id, source_kind, source_id, source_manifest_digest,
               producer_code_digest, state
        FROM projection_settlements
        ORDER BY source_kind, source_id, rebuild_sequence, settlement_id
    """
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(query)
        columns = tuple(description.name for description in cursor.description or ())
        return tuple(dict(zip(columns, row, strict=True)) for row in cursor.fetchall())


def _cas_sources(
    store_root: Path,
    expected_source_digests: Mapping[tuple[str, str], str],
) -> tuple[dict[tuple[str, str], SettlementSource], list[ReconciliationEntry]]:
    sources: dict[tuple[str, str], SettlementSource] = {}
    errors: list[ReconciliationEntry] = []
    records_root = store_root.resolve() / "records"
    for kind in ("interpretation", "job"):
        directory = records_root / kind
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            key = (kind, path.stem)
            expected_record_digest = expected_source_digests.get(key)
            if expected_record_digest is None:
                errors.append(
                    ReconciliationEntry(
                        source_kind=kind,
                        source_id=path.stem,
                        table_name=None,
                        partition_identity=None,
                        state="unverifiable",
                        reason="independent CAS record digest is unavailable",
                        source_record_path=path,
                    )
                )
                continue
            try:
                source = SettlementSource.from_cas_record(
                    path,
                    expected_record_digest=expected_record_digest,
                )
            except Exception as exc:
                errors.append(
                    ReconciliationEntry(
                        source_kind=kind,
                        source_id=path.stem,
                        table_name=None,
                        partition_identity=None,
                        state="unverifiable",
                        reason=f"invalid CAS record: {type(exc).__name__}: {exc}",
                        source_record_path=path,
                    )
                )
                continue
            key = (source.source_kind, source.source_id)
            if key in sources:
                errors.append(
                    ReconciliationEntry(
                        source_kind=kind,
                        source_id=source.source_id,
                        table_name=None,
                        partition_identity=None,
                        state="unverifiable",
                        reason="duplicate CAS source identity",
                        source_record_path=path,
                    )
                )
                continue
            sources[key] = source
    return sources, errors


def _manifest_paths(root: Path, manifests: Iterable[ProjectionSettlementManifest]) -> set[Path]:
    return {
        (root.resolve() / table.relative_path).resolve()
        for manifest in manifests
        for table in manifest.tables
        if table.state == "ready"
    }


def _physical_parquet(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    settlement_root = (root / "_settlement").resolve()
    return tuple(
        path.resolve()
        for path in sorted(root.rglob("*.parquet"), key=lambda item: item.as_posix())
        if settlement_root not in path.resolve().parents
    )


def _sidecar_entries(
    roots: Sequence[Path],
    sources: Mapping[tuple[str, str], SettlementSource],
) -> list[ReconciliationEntry]:
    entries: list[ReconciliationEntry] = []
    for root in sorted({path.resolve() for path in roots}, key=str):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("acceptance_decision.json"), key=str):
            try:
                payload = json.loads(path.read_text())
                decision_id = payload["decision_id"]
                if not isinstance(decision_id, str) or not decision_id:
                    raise ValueError("decision_id must be a non-empty string")
            except Exception as exc:
                entries.append(
                    ReconciliationEntry(
                        source_kind="interpretation",
                        source_id=None,
                        table_name="acceptance_decisions",
                        partition_identity=None,
                        state="unverifiable",
                        reason=f"invalid interpretation sidecar: {type(exc).__name__}: {exc}",
                        projection_path=path,
                    )
                )
                continue
            state: ReconciliationState = (
                "matched" if ("interpretation", decision_id) in sources else "missing_source"
            )
            entries.append(
                ReconciliationEntry(
                    source_kind="interpretation",
                    source_id=decision_id,
                    table_name="acceptance_decisions",
                    partition_identity=None,
                    state=state,
                    reason=(
                        "sidecar has a verified interpretation CAS record"
                        if state == "matched"
                        else "sidecar has no verified interpretation CAS record"
                    ),
                    projection_path=path,
                )
            )
    return entries


def reconcile_projection_inventory(
    *,
    store_root: Path,
    derived_root: Path,
    postgres_rows: Iterable[Mapping[str, Any]] | None = None,
    database_url: str | None = None,
    interpretation_sidecar_roots: Sequence[Path] = (),
    expected_producer_digests: Mapping[str, str] | None = None,
    expected_source_digests: Mapping[tuple[str, str], str] | None = None,
) -> ReconciliationInventory:
    """Return a deterministic inventory; never adopt, delete, or rewrite state."""
    if postgres_rows is not None and database_url is not None:
        raise ValueError("provide postgres_rows or database_url, not both")
    if database_url is not None:
        postgres_rows = read_postgres_settlement_rows(database_url)
    catalog_rows = tuple(postgres_rows) if postgres_rows is not None else None
    trusted_source_digests = dict(expected_source_digests or {})
    if catalog_rows is not None:
        for row in catalog_rows:
            source_kind = row.get("source_kind")
            source_id = row.get("source_id")
            record_digest = row.get("source_manifest_digest")
            if all(
                isinstance(value, str) and value
                for value in (source_kind, source_id, record_digest)
            ):
                trusted_source_digests[(source_kind, source_id)] = record_digest

    derived_root = derived_root.resolve()
    sources, entries = _cas_sources(store_root, trusted_source_digests)
    manifest_inventory = load_settlement_manifests(derived_root)
    manifests = () if manifest_inventory.errors else active_settlement_manifests(manifest_inventory)
    for error in manifest_inventory.errors:
        entries.append(
            ReconciliationEntry(
                source_kind=None,
                source_id=None,
                table_name=None,
                partition_identity=None,
                state="unverifiable",
                reason=f"invalid settlement manifest: {error.reason}",
                projection_path=Path(error.path),
            )
        )

    manifest_sources = {
        (manifest.source.source_kind, manifest.source.source_id) for manifest in manifests
    }
    for key, source in sources.items():
        if key not in manifest_sources:
            entries.append(
                ReconciliationEntry(
                    source_kind=key[0],
                    source_id=key[1],
                    table_name=None,
                    partition_identity=None,
                    state="missing_projection",
                    reason="verified CAS source has no active settlement",
                    source_record_path=Path(source.source_record_path or ""),
                )
            )

    catalog = (
        {str(row["settlement_id"]): row for row in catalog_rows}
        if catalog_rows is not None
        else None
    )
    manifests_by_id = {manifest.settlement_id: manifest for manifest in manifests}
    expected_producer_digests = dict(expected_producer_digests or {})

    for manifest in manifests:
        source_key = (manifest.source.source_kind, manifest.source.source_id)
        verified_source = sources.get(source_key)
        if verified_source is None:
            entries.append(
                ReconciliationEntry(
                    source_kind=source_key[0],
                    source_id=source_key[1],
                    table_name=None,
                    partition_identity=None,
                    state="missing_source",
                    reason="active settlement has no verified CAS source record",
                    settlement_id=manifest.settlement_id,
                )
            )
        elif verified_source.source_manifest_digest != manifest.source.source_manifest_digest:
            entries.append(
                ReconciliationEntry(
                    source_kind=source_key[0],
                    source_id=source_key[1],
                    table_name=None,
                    partition_identity=None,
                    state="digest_mismatch",
                    reason="settlement source record digest differs from verified CAS record",
                    source_record_path=Path(verified_source.source_record_path or ""),
                    settlement_id=manifest.settlement_id,
                )
            )

        expected_producer = expected_producer_digests.get(manifest.contract.producer_name)
        if (
            expected_producer is not None
            and expected_producer != manifest.contract.producer_code_digest
        ):
            entries.append(
                ReconciliationEntry(
                    source_kind=source_key[0],
                    source_id=source_key[1],
                    table_name=None,
                    partition_identity=None,
                    state="stale_producer",
                    reason=(
                        f"expected={expected_producer} "
                        f"actual={manifest.contract.producer_code_digest}"
                    ),
                    settlement_id=manifest.settlement_id,
                )
            )

        if catalog is not None:
            row = catalog.get(manifest.settlement_id)
            if row is None:
                entries.append(
                    ReconciliationEntry(
                        source_kind=source_key[0],
                        source_id=source_key[1],
                        table_name=None,
                        partition_identity=None,
                        state="unverifiable",
                        reason="active settlement is absent from PostgreSQL registry",
                        settlement_id=manifest.settlement_id,
                    )
                )
            else:
                catalog_digest = row.get("source_manifest_digest")
                if catalog_digest != manifest.source.source_manifest_digest:
                    entries.append(
                        ReconciliationEntry(
                            source_kind=source_key[0],
                            source_id=source_key[1],
                            table_name=None,
                            partition_identity=None,
                            state="digest_mismatch",
                            reason="PostgreSQL source record digest differs from manifest",
                            settlement_id=manifest.settlement_id,
                        )
                    )
                if row.get("producer_code_digest") != manifest.contract.producer_code_digest:
                    entries.append(
                        ReconciliationEntry(
                            source_kind=source_key[0],
                            source_id=source_key[1],
                            table_name=None,
                            partition_identity=None,
                            state="stale_producer",
                            reason="PostgreSQL producer digest differs from manifest",
                            settlement_id=manifest.settlement_id,
                        )
                    )

        contract_by_key = {contract.key: contract for contract in manifest.contract.tables}
        for table in manifest.tables:
            contract = contract_by_key[table.key]
            path = (derived_root / table.relative_path).resolve()
            if table.state == "not_applicable":
                entries.append(
                    ReconciliationEntry(
                        source_kind=source_key[0],
                        source_id=source_key[1],
                        table_name=table.table_name,
                        partition_identity=table.partition_identity,
                        state="matched",
                        reason="table is explicitly not applicable",
                        settlement_id=manifest.settlement_id,
                    )
                )
                continue
            if table.state != "ready" or manifest.state != "ready":
                entries.append(
                    ReconciliationEntry(
                        source_kind=source_key[0],
                        source_id=source_key[1],
                        table_name=table.table_name,
                        partition_identity=table.partition_identity,
                        state="missing_projection",
                        reason=f"settlement={manifest.state} table={table.state}",
                        settlement_id=manifest.settlement_id,
                        projection_path=path,
                    )
                )
                continue
            if not path.is_file():
                state: ReconciliationState = "missing_projection"
                reason = "manifest-bound Parquet file is missing"
            else:
                digest = f"sha256:{sha256_file(path)}"
                if digest != table.file_digest:
                    state = "digest_mismatch"
                    reason = f"expected={table.file_digest} actual={digest}"
                elif contract.schema_digest != table.schema_digest:
                    state = "digest_mismatch"
                    reason = "table schema digest differs from contract"
                else:
                    state = "matched"
                    reason = "CAS, settlement, and Parquet digests agree"
            entries.append(
                ReconciliationEntry(
                    source_kind=source_key[0],
                    source_id=source_key[1],
                    table_name=table.table_name,
                    partition_identity=table.partition_identity,
                    state=state,
                    reason=reason,
                    settlement_id=manifest.settlement_id,
                    projection_path=path,
                )
            )

    if catalog is not None:
        for settlement_id, row in sorted(catalog.items()):
            if settlement_id not in manifests_by_id:
                entries.append(
                    ReconciliationEntry(
                        source_kind=str(row.get("source_kind") or "") or None,
                        source_id=str(row.get("source_id") or "") or None,
                        table_name=None,
                        partition_identity=None,
                        state="missing_projection",
                        reason="PostgreSQL settlement has no active manifest",
                        settlement_id=settlement_id,
                    )
                )

    referenced = _manifest_paths(derived_root, manifests)
    for path in _physical_parquet(derived_root):
        if path not in referenced:
            entries.append(
                ReconciliationEntry(
                    source_kind=None,
                    source_id=None,
                    table_name=path.stem,
                    partition_identity=path.parent.relative_to(derived_root).as_posix(),
                    state="extra_projection",
                    reason="physical Parquet is not referenced by an active settlement",
                    projection_path=path,
                )
            )

    entries.extend(_sidecar_entries(interpretation_sidecar_roots, sources))
    return ReconciliationInventory(entries=tuple(sorted(entries, key=lambda entry: entry.sort_key)))
