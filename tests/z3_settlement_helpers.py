from __future__ import annotations

import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.evidence_store import archive_evidence, evidence_locator
from evallab.storage.attach import TABLES
from evallab.storage.settlement import (
    ProjectionContract,
    ProjectionTableSettlement,
    SettlementSource,
    create_settlement_manifest,
    table_contract,
    transition_settlement,
    verify_projected_table,
    write_settlement_manifest,
)

_PRODUCER_DIGEST = "sha256:" + "d" * 64
_EMPTY_SCHEMA = pa.schema([pa.field("_unavailable", pa.string(), nullable=True)])


def admit_z3_tree(root: Path) -> None:
    """Bind current fixture Parquet files to one complete ready settlement."""
    root = root.resolve()
    settlement_root = root / "_settlement"
    if settlement_root.exists():
        shutil.rmtree(settlement_root)
    source_dir = root / "_fixture_source"
    store_root = root / "_fixture_cas"
    for path in (source_dir, store_root):
        if path.exists():
            shutil.rmtree(path)
    source_dir.mkdir(parents=True)
    (source_dir / "authority.txt").write_text("manifest-gated attach fixture\n")
    archive = archive_evidence(
        source_dir,
        store_root,
        kind="job",
        record_id="attach-fixture",
    )
    source = SettlementSource.from_cas_locator(evidence_locator(store_root, archive))
    source_digest = source.cas_content_digest
    assert source_digest is not None

    discovered: list[tuple[Path, str]] = []
    for path in root.rglob("*.parquet"):
        if settlement_root in path.parents:
            continue
        relative = path.relative_to(root)
        table_name = (
            path.stem
            if path.stem in TABLES
            else next(
                (part for part in reversed(relative.parts[:-1]) if part in TABLES),
                None,
            )
        )
        if table_name is not None:
            discovered.append((path, table_name))
    discovered.sort(key=lambda item: item[0].relative_to(root).as_posix())
    discovered = [
        (path, table_name)
        for path, table_name in discovered
        if not (
            table_name != "jobs"
            and any(part.startswith("job_id=") for part in path.relative_to(root).parts)
            and not any(part.startswith("trial_id=") for part in path.relative_to(root).parts)
        )
    ]

    hot_trial_keys: set[tuple[object, object]] = set()
    for path, table_name in discovered:
        relative = path.relative_to(root)
        if (
            table_name == "trial_facts"
            and any(part.startswith("job_id=") for part in relative.parts)
            and any(part.startswith("trial_id=") for part in relative.parts)
        ):
            parquet = pq.ParquetFile(path)
            if {"job_id", "trial_id"} <= set(parquet.schema_arrow.names):
                table = parquet.read(columns=["job_id", "trial_id"])
                hot_trial_keys.update(
                    zip(
                        table["job_id"].to_pylist(),
                        table["trial_id"].to_pylist(),
                        strict=True,
                    )
                )
    if hot_trial_keys:
        filtered: list[tuple[Path, str]] = []
        for path, table_name in discovered:
            relative = path.relative_to(root)
            if table_name == "trial_facts" and not any(
                part.startswith("job_id=") for part in relative.parts
            ):
                parquet = pq.ParquetFile(path)
                if {"job_id", "trial_id"} <= set(parquet.schema_arrow.names):
                    table = parquet.read(columns=["job_id", "trial_id"])
                    keys = set(
                        zip(
                            table["job_id"].to_pylist(),
                            table["trial_id"].to_pylist(),
                            strict=True,
                        )
                    )
                    if keys and keys <= hot_trial_keys:
                        continue
            filtered.append((path, table_name))
        discovered = filtered
    job_level_ids = {
        next(part for part in path.relative_to(root).parts if part.startswith("job_id="))
        for path, table_name in discovered
        if table_name == "jobs"
        and any(part.startswith("job_id=") for part in path.relative_to(root).parts)
        and not any(part.startswith("trial_id=") for part in path.relative_to(root).parts)
    }
    discovered = [
        (path, table_name)
        for path, table_name in discovered
        if not (
            table_name == "jobs"
            and any(part.startswith("trial_id=") for part in path.relative_to(root).parts)
            and any(part in job_level_ids for part in path.relative_to(root).parts)
        )
    ]

    paths_by_table: dict[str, list[Path]] = {}
    for path, table_name in discovered:
        paths_by_table.setdefault(table_name, []).append(path)
    for table_paths in paths_by_table.values():
        if len(table_paths) < 2:
            continue
        unified = pa.unify_schemas([pq.read_schema(path) for path in table_paths])
        for path in table_paths:
            table = pq.ParquetFile(path).read()
            if table.schema != unified:
                pq.write_table(table.cast(unified), path)

    contracts = []
    paths_by_key: dict[tuple[str, str], Path] = {}
    present_tables: set[str] = set()
    for path, table_name in discovered:
        relative_path = path.relative_to(root).as_posix()
        partition_identity = path.parent.relative_to(root).as_posix() or "."
        contract = table_contract(
            table_name=table_name,
            partition_identity=partition_identity,
            required=False,
            schema=pq.read_schema(path),
            relative_path=relative_path,
        )
        contracts.append(contract)
        paths_by_key[contract.key] = path
        present_tables.add(table_name)

    for table_name in TABLES:
        if table_name in present_tables:
            continue
        contracts.append(
            table_contract(
                table_name=table_name,
                partition_identity="not-applicable",
                required=False,
                schema=_EMPTY_SCHEMA,
                relative_path=f"_not_applicable/{table_name}.parquet",
            )
        )

    contract = ProjectionContract(
        producer_name="tests.attach-fixture",
        producer_version="1",
        producer_code_digest=_PRODUCER_DIGEST,
        tables=tuple(contracts),
    )
    manifest = create_settlement_manifest(source, contract)
    for state in ("source_validated", "cas_committed", "cataloged"):
        manifest = transition_settlement(manifest, state)
    projecting = tuple(
        ProjectionTableSettlement(
            **table.model_dump(mode="python"),
            state="projecting",
            source_digest=source_digest,
        )
        for table in contracts
    )
    manifest = transition_settlement(manifest, "projecting", tables=projecting)

    settled = []
    for table in contracts:
        path = paths_by_key.get(table.key)
        if path is None:
            settled.append(
                ProjectionTableSettlement(
                    **table.model_dump(mode="python"),
                    state="not_applicable",
                    failure_reason="fixture table is intentionally absent",
                )
            )
            continue
        settled.append(
            verify_projected_table(
                root,
                table,
                source_digest=source_digest,
                expected_row_count=pq.read_metadata(path).num_rows,
            )
        )
    manifest = transition_settlement(manifest, "ready", tables=tuple(settled))
    write_settlement_manifest(root, manifest)
