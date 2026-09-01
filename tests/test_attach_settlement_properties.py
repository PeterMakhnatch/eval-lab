"""Property checks for strict manifest-gated Z3 readiness."""

from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from z3_settlement_helpers import admit_z3_tree

import evallab.storage.attach as attach_module
from evallab.storage.attach import TABLES, attach
from evallab.storage.settlement import (
    ProjectionContract,
    ProjectionTableSettlement,
    SettlementSource,
    SettlementState,
    create_settlement_manifest,
    table_contract,
    transition_settlement,
    write_settlement_manifest,
)

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_DIGEST_C = "sha256:" + "c" * 64
_SCHEMA = pa.schema([pa.field("identity", pa.string(), nullable=False)])


def _source(source_id: str) -> SettlementSource:
    return SettlementSource(
        source_id=source_id,
        source_kind="job",
        authority_status="verified",
        cas_store_root="/tmp/cas",
        cas_record_kind="job",
        cas_record_id=source_id,
        cas_record_digest=_DIGEST_C,
        cas_uri="cas://sha256/" + "a" * 64,
        cas_content_digest=_DIGEST_A,
        cas_archive_digest=_DIGEST_B,
        source_manifest_digest=_DIGEST_C,
    )


def _partial_manifest(root: Path, table_count: int) -> None:
    source = _source("fixture")
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
    result = attach(repo_root=tmp_path, explicit_derived=root)
    status = next(zone for zone in result.zones if zone.name == "z3")
    try:
        assert status.state == "partial"
        assert status.attached is False
        assert sum(table.state == "not_applicable" for table in status.tables) == table_count
        assert sum(table.state == "missing" for table in status.tables) == len(TABLES) - table_count
    finally:
        result.connection.close()


def test_missing_and_empty_derived_roots_are_explicitly_unavailable(tmp_path: Path) -> None:
    for root in (tmp_path / "missing", tmp_path / "empty"):
        if root.name == "empty":
            root.mkdir()
        result = attach(repo_root=tmp_path, explicit_derived=root)
        status = next(zone for zone in result.zones if zone.name == "z3")
        try:
            assert status.state == "unavailable"
            assert status.attached is False
            assert len(status.tables) == len(TABLES)
            assert {table.state for table in status.tables} == {"missing"}
        finally:
            result.connection.close()


def _write_single_trial_table(root: Path, *, trial_id: str, reward: int) -> Path:
    path = root / "job_id=fixture" / "trial_id=fixture" / "trial_facts.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([{"trial_id": trial_id, "reward": reward}]),
        path,
    )
    return path


def _replace_single_trial_table(path: Path, *, trial_id: str, reward: int) -> None:
    replacement = path.with_suffix(".replacement.parquet")
    pq.write_table(
        pa.Table.from_pylist([{"trial_id": trial_id, "reward": reward}]),
        replacement,
    )
    os.replace(replacement, path)


def _write_non_ready_manifest(root: Path, target: SettlementState) -> None:
    source = _source(f"fixture-{target}")
    table = table_contract(
        table_name="trial_facts",
        partition_identity="fixture",
        required=True,
        schema=_SCHEMA,
        relative_path="fixture/trial_facts.parquet",
    )
    contract = ProjectionContract(
        producer_name="tests.attach.non-ready",
        producer_version="1",
        producer_code_digest=_DIGEST_B,
        tables=(table,),
    )
    manifest = create_settlement_manifest(source, contract)
    if target == "quarantined":
        quarantined = ProjectionTableSettlement(
            **table.model_dump(mode="python"),
            state="quarantined",
            failure_reason="source authority rejected",
        )
        manifest = transition_settlement(
            manifest,
            "quarantined",
            tables=(quarantined,),
            reason_code="invalid_cas_authority",
        )
        write_settlement_manifest(root, manifest)
        return

    if target == "discovered":
        write_settlement_manifest(root, manifest)
        return
    for state in ("source_validated", "cas_committed", "cataloged"):
        manifest = transition_settlement(manifest, state)
        if state == target:
            write_settlement_manifest(root, manifest)
            return

    projecting = ProjectionTableSettlement(
        **table.model_dump(mode="python"),
        state="projecting",
        source_digest=source.cas_content_digest,
    )
    manifest = transition_settlement(
        manifest,
        "projecting",
        tables=(projecting,),
    )
    if target == "projecting":
        write_settlement_manifest(root, manifest)
        return

    failed = ProjectionTableSettlement(
        **table.model_dump(mode="python"),
        state="failed",
        source_digest=source.cas_content_digest,
        failure_reason="projection write failed",
    )
    manifest = transition_settlement(
        manifest,
        "projection_failed",
        tables=(failed,),
        reason_code="projection_io_error",
    )
    write_settlement_manifest(root, manifest)


@pytest.mark.parametrize(
    ("manifest_state", "expected_table_state", "expected_event_reason"),
    [
        ("discovered", "missing", None),
        ("source_validated", "missing", None),
        ("cas_committed", "missing", None),
        ("cataloged", "missing", None),
        ("projecting", "projecting", None),
        ("projection_failed", "failed", "projection_io_error"),
        ("quarantined", "quarantined", "invalid_cas_authority"),
    ],
)
def test_public_attach_types_every_non_ready_manifest_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_state: SettlementState,
    expected_table_state: str,
    expected_event_reason: str | None,
) -> None:
    root = tmp_path / manifest_state
    _write_non_ready_manifest(root, manifest_state)
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")

    result = attach(repo_root=tmp_path, explicit_derived=root)
    status = next(zone for zone in result.zones if zone.name == "z3")
    try:
        table = next(item for item in status.tables if item.table_name == "trial_facts")
        assert status.state == "unavailable"
        assert status.attached is False
        assert table.state == expected_table_state
        assert f"state={manifest_state}" in table.reason
        assert f"table_state={expected_table_state}" in table.reason
        if expected_event_reason is not None:
            assert f"event_reason={expected_event_reason}" in table.reason
            assert "table_reason=" in table.reason
        assert result.connection.execute(
            "SELECT state, reason FROM table_readiness WHERE table_name = 'trial_facts'"
        ).fetchone() == (table.state, table.reason)
    finally:
        result.connection.close()


def test_public_attach_binds_captured_bytes_across_pre_binding_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "derived"
    path = _write_single_trial_table(root, trial_id="trusted", reward=1)
    admit_z3_tree(root)
    original_bind = attach_module._bind_captured_table
    replaced = False

    def replace_before_binding(conn, readiness):
        nonlocal replaced
        if readiness.table_name == "trial_facts":
            _replace_single_trial_table(path, trial_id="forged", reward=999)
            replaced = True
        return original_bind(conn, readiness)

    monkeypatch.setattr(attach_module, "_bind_captured_table", replace_before_binding)
    result = attach_module.attach(repo_root=tmp_path, explicit_derived=root)
    try:
        assert replaced is True
        assert "read_parquet" not in result.sql_preamble
        assert result.connection.execute(
            "SELECT trial_id, reward FROM z3.trial_facts"
        ).fetchall() == [("fixture", 1)]
    finally:
        result.connection.close()


def test_public_attach_first_query_ignores_post_attach_replacement(tmp_path: Path) -> None:
    root = tmp_path / "derived"
    path = _write_single_trial_table(root, trial_id="trusted", reward=1)
    admit_z3_tree(root)
    result = attach(repo_root=tmp_path, explicit_derived=root)
    try:
        _replace_single_trial_table(path, trial_id="forged", reward=999)
        assert result.connection.execute(
            "SELECT trial_id, reward FROM z3.trial_facts"
        ).fetchall() == [("fixture", 1)]
    finally:
        result.connection.close()


def test_public_attach_repeated_queries_ignore_path_replacement(tmp_path: Path) -> None:
    root = tmp_path / "derived"
    path = _write_single_trial_table(root, trial_id="trusted", reward=1)
    admit_z3_tree(root)
    result = attach(repo_root=tmp_path, explicit_derived=root)
    try:
        assert result.connection.execute(
            "SELECT trial_id, reward FROM z3.trial_facts"
        ).fetchall() == [("fixture", 1)]
        _replace_single_trial_table(path, trial_id="forged", reward=999)
        assert result.connection.execute(
            "SELECT trial_id, reward FROM z3.trial_facts"
        ).fetchall() == [("fixture", 1)]
    finally:
        result.connection.close()
