"""Property checks for strict manifest-gated Z3 readiness."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from evallab.storage.attach import TABLES, _attach_z3
from evallab.storage.settlement import (
    ProjectionContract,
    ProjectionTableSettlement,
    SettlementSource,
    create_settlement_manifest,
    table_contract,
    transition_settlement,
    write_settlement_manifest,
)

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_DIGEST_C = "sha256:" + "c" * 64
_SCHEMA = pa.schema([pa.field("identity", pa.string(), nullable=False)])


def _partial_manifest(root: Path, table_count: int) -> None:
    source = SettlementSource(
        source_id="fixture",
        source_kind="job",
        authority_status="verified",
        cas_uri="cas://sha256/" + "a" * 64,
        cas_content_digest=_DIGEST_A,
        cas_archive_digest=_DIGEST_B,
        source_manifest_digest=_DIGEST_C,
        record_path="/tmp/records/job/fixture.json",
    )
    contracts = tuple(
        table_contract(
            table_name=name,
            partition_identity="fixture",
            required=False,
            schema=_SCHEMA,
            relative_path=f"fixture/{name}.parquet",
        )
        for name in TABLES[:table_count]
    )
    contract = ProjectionContract(
        producer_name="tests.attach.property",
        producer_version="1",
        producer_code_digest=_DIGEST_B,
        tables=contracts,
    )
    manifest = create_settlement_manifest(source, contract)
    for state in ("source_validated", "cas_committed", "cataloged"):
        manifest = transition_settlement(manifest, state)
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
    manifest = transition_settlement(
        manifest,
        "ready",
        tables=tuple(
            ProjectionTableSettlement(
                **table.model_dump(mode="python"),
                state="not_applicable",
            )
            for table in contracts
        ),
    )
    write_settlement_manifest(root, manifest)


@given(table_count=st.integers(min_value=1, max_value=len(TABLES) - 1))
@settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_any_incomplete_manifest_coverage_is_never_ready(tmp_path: Path, table_count: int) -> None:
    root = tmp_path / f"case-{table_count}"
    _partial_manifest(root, table_count)
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE SCHEMA z3")
    status = _attach_z3(connection, root)
    try:
        assert status.state == "partial"
        assert status.attached is False
        assert sum(table.state == "not_applicable" for table in status.tables) == table_count
        assert sum(table.state == "missing" for table in status.tables) == len(TABLES) - table_count
    finally:
        connection.close()


def test_missing_and_empty_derived_roots_are_explicitly_unavailable(tmp_path: Path) -> None:
    for root in (tmp_path / "missing", tmp_path / "empty"):
        if root.name == "empty":
            root.mkdir()
        connection = duckdb.connect(":memory:")
        connection.execute("CREATE SCHEMA z3")
        status = _attach_z3(connection, root)
        try:
            assert status.state == "unavailable"
            assert status.attached is False
            assert len(status.tables) == len(TABLES)
            assert {table.state for table in status.tables} == {"missing"}
        finally:
            connection.close()
