"""Manifest-gated acceptance tests for the DuckDB Z3 attach surface."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from evallab.evidence.parquet_io import write_table_atomic
from evallab.storage.attach import TABLES, _attach_z3
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

_SCHEMA = pa.schema([pa.field("identity", pa.string(), nullable=False)])
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_DIGEST_C = "sha256:" + "c" * 64


def _source() -> SettlementSource:
    return SettlementSource(
        source_id="job-1",
        source_kind="job",
        authority_status="verified",
        cas_uri="cas://sha256/" + "a" * 64,
        cas_content_digest=_DIGEST_A,
        cas_archive_digest=_DIGEST_B,
        source_manifest_digest=_DIGEST_C,
        record_path="/tmp/records/job/job-1.json",
    )


def _write_ready_tables(root: Path, count: int) -> None:
    source = _source()
    contracts = tuple(
        table_contract(
            table_name=table_name,
            partition_identity="job_id=job-1",
            required=True,
            schema=_SCHEMA,
            relative_path=f"job_id=job-1/{table_name}.parquet",
        )
        for table_name in TABLES[:count]
    )
    contract = ProjectionContract(
        producer_name="tests.attach",
        producer_version="1",
        producer_code_digest=_DIGEST_B,
        tables=contracts,
    )
    manifest = create_settlement_manifest(source, contract)
    manifest = transition_settlement(manifest, "source_validated")
    manifest = transition_settlement(manifest, "cas_committed")
    manifest = transition_settlement(manifest, "cataloged")
    manifest = transition_settlement(
        manifest,
        "projecting",
        tables=tuple(
            ProjectionTableSettlement(
                **table.model_dump(mode="python"),
                state="projecting",
                source_digest=source.cas_content_digest,
            )
            for table in contracts
        ),
    )
    settled = []
    for table in contracts:
        path = root / table.relative_path
        write_table_atomic(path, [{"identity": table.table_name}], _SCHEMA)
        settled.append(
            verify_projected_table(
                root,
                table,
                source_digest=source.cas_content_digest or "",
                expected_row_count=1,
            )
        )
    manifest = transition_settlement(manifest, "ready", tables=tuple(settled))
    write_settlement_manifest(root, manifest)


def test_all_thirty_nine_manifested_tables_make_z3_ready(tmp_path: Path) -> None:
    root = tmp_path / "derived" / "parquet"
    _write_ready_tables(root, len(TABLES))
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE SCHEMA z3")
    try:
        status = _attach_z3(connection, root)
        assert status.state == "ready"
        assert status.attached is True
        assert sum(table.state == "ready" for table in status.tables) == len(TABLES)
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (1,)
    finally:
        connection.close()


def test_twenty_seven_of_thirty_nine_is_partial_with_twelve_reasons(
    tmp_path: Path,
) -> None:
    root = tmp_path / "derived" / "parquet"
    _write_ready_tables(root, 27)
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE SCHEMA z3")
    try:
        status = _attach_z3(connection, root)
        blocked = [table for table in status.tables if table.state != "ready"]
        assert status.state == "partial"
        assert status.attached is False
        assert len(blocked) == 12
        assert all(table.state == "missing" for table in blocked)
        assert all(table.reason == "no active settlement contract" for table in blocked)
        with pytest.raises(duckdb.CatalogException):
            connection.execute(f"SELECT * FROM {blocked[0].table_name}")
    finally:
        connection.close()


def test_absent_root_is_unavailable_with_no_fake_relations(tmp_path: Path) -> None:
    root = tmp_path / "derived" / "parquet"
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE SCHEMA z3")
    try:
        status = _attach_z3(connection, root)
        assert status.state == "unavailable"
        assert status.attached is False
        assert len(status.tables) == len(TABLES)
        assert all(table.state == "missing" for table in status.tables)
        with pytest.raises(duckdb.CatalogException):
            connection.execute("SELECT * FROM jobs")
    finally:
        connection.close()
