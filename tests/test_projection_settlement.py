from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pyarrow as pa
import pytest
from pydantic import ValidationError

from evallab import database
from evallab.evidence import atif, parquet_io
from evallab.evidence.parquet_io import write_table_atomic
from evallab.evidence_store import EvidenceLocator, archive_evidence, evidence_locator
from evallab.interpretation import trajectory_runtime
from evallab.storage import settlement as settlement_module
from evallab.storage.attach import _attach_z3
from evallab.storage.reconciliation import reconcile_projection_inventory
from evallab.storage.settlement import (
    ProjectionContract,
    ProjectionTableSettlement,
    SettlementError,
    SettlementSource,
    _event_row,
    _table_row,
    _validate_ledger_replay,
    active_settlement_manifests,
    create_settlement_manifest,
    load_settlement_manifest,
    load_settlement_manifests,
    table_contract,
    transition_settlement,
    verify_projected_table,
    write_settlement_manifest,
)

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_DIGEST_C = "sha256:" + "c" * 64
_SCHEMA = pa.schema(
    [
        pa.field("label", pa.string(), nullable=False),
        pa.field("count", pa.int64(), nullable=False),
    ]
)


def _source() -> SettlementSource:
    return SettlementSource(
        source_id="job-1",
        source_kind="job",
        authority_status="verified",
        cas_store_root="/tmp/cas",
        cas_record_kind="job",
        cas_record_id="job-1",
        cas_record_digest=_DIGEST_C,
        cas_uri="cas://sha256/" + "a" * 64,
        cas_content_digest=_DIGEST_A,
        cas_archive_digest=_DIGEST_B,
        source_manifest_digest=_DIGEST_C,
    )


def _contract(*, table_name: str = "behavior_labels", required: bool = True) -> ProjectionContract:
    table = table_contract(
        table_name=table_name,
        partition_identity="job_id=job-1",
        required=required,
        schema=_SCHEMA,
        relative_path=f"job_id=job-1/{table_name}.parquet",
    )
    return ProjectionContract(
        producer_name="tests.projection",
        producer_version="1",
        producer_code_digest=_DIGEST_B,
        tables=(table,),
    )


def _table_state(
    contract: ProjectionContract,
    state: str,
    *,
    source_digest: str | None = None,
    file_digest: str | None = None,
    row_count: int | None = None,
    failure_reason: str | None = None,
) -> ProjectionTableSettlement:
    return ProjectionTableSettlement(
        **contract.tables[0].model_dump(mode="python"),
        state=state,
        source_digest=source_digest,
        file_digest=file_digest,
        row_count=row_count,
        failure_reason=failure_reason,
    )


def _projecting_manifest(source: SettlementSource, contract: ProjectionContract):
    manifest = create_settlement_manifest(source, contract)
    manifest = transition_settlement(manifest, "source_validated")
    manifest = transition_settlement(manifest, "cas_committed")
    manifest = transition_settlement(manifest, "cataloged")
    return transition_settlement(
        manifest,
        "projecting",
        tables=(
            _table_state(
                contract,
                "projecting",
                source_digest=source.cas_content_digest,
            ),
        ),
    )


def _ready_manifest(
    root: Path,
    source: SettlementSource,
    contract: ProjectionContract,
    *,
    rows: list[dict] | None = None,
):
    rows = rows or [{"label": "ready", "count": 1}]
    manifest = _projecting_manifest(source, contract)
    path = root / contract.tables[0].relative_path
    write_table_atomic(path, rows, _SCHEMA)
    settled = verify_projected_table(
        root,
        contract.tables[0],
        source_digest=source.cas_content_digest or "",
        expected_row_count=len(rows),
    )
    manifest = transition_settlement(manifest, "ready", tables=(settled,))
    write_settlement_manifest(root, manifest)
    return manifest, path


def test_evidence_locator_handoff_binds_exact_record_and_content_bytes(tmp_path: Path) -> None:
    source_dir = tmp_path / "job-source"
    source_dir.mkdir()
    (source_dir / "result.json").write_text('{"ok": true}\n')
    store_root = tmp_path / "store"
    archive = archive_evidence(
        source_dir,
        store_root,
        kind="job",
        record_id="job-1",
    )
    locator = evidence_locator(store_root, archive)
    source = SettlementSource.from_cas_locator(locator)

    assert source.evidence_locator == locator
    assert source.source_manifest_digest == archive.record_digest
    assert source.cas_content_digest == archive.content_digest
    assert source.cas_archive_digest == archive.archive_digest

    with pytest.raises(SettlementError, match="invalid_cas_record"):
        SettlementSource.from_cas_locator(
            EvidenceLocator(
                store_root=store_root,
                kind="job",
                record_id="job-1",
                expected_record_digest=_DIGEST_A,
                expected_content_digest=archive.content_digest,
            )
        )
    with pytest.raises(SettlementError, match="invalid_cas_record"):
        SettlementSource.from_cas_locator(
            EvidenceLocator(
                store_root=store_root,
                kind="job",
                record_id="job-1",
                expected_record_digest=archive.record_digest,
                expected_content_digest=_DIGEST_A,
            )
        )


def test_missing_cas_authority_is_quarantined_before_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    job = SimpleNamespace(id="job-1", name="job", trials=())
    monkeypatch.setattr(database, "initialize", lambda _url: calls.append("initialize"))
    monkeypatch.setattr(
        database,
        "ingest",
        lambda *_args, **_kwargs: pytest.fail("unverified source reached catalog"),
    )
    monkeypatch.setattr(
        atif,
        "project_jobs",
        lambda *_args, **_kwargs: pytest.fail("unverified source reached projection"),
    )

    result = atif.ingest_and_project(
        "postgresql://unused",
        [job],  # type: ignore[list-item]
        root=tmp_path,
        output_root=tmp_path / "derived" / "parquet",
        settlement_recorder=lambda *_args: None,
    )

    assert calls == ["initialize"]
    assert result.cataloged_jobs == 0
    assert result.failures[0].error_type == "MissingCASAuthority"
    assert result.settlements[0].state == "quarantined"


def test_projection_contract_matches_export_order_for_multiple_trials() -> None:
    job = SimpleNamespace(
        id="job-1",
        trials=(SimpleNamespace(id="trial-b"), SimpleNamespace(id="trial-a")),
    )
    contract = atif._job_projection_contract(job)
    assert [table.table_name for table in contract.tables[:9]] == [
        "jobs",
        "trajectories",
        "steps",
        "tool_calls",
        "observations",
        "trajectories",
        "steps",
        "tool_calls",
        "observations",
    ]
    assert [table.partition_identity for table in contract.tables[1:5]] == [
        "job_id=job-1/trial_id=trial-a"
    ] * 4


def test_ready_manifest_rejects_required_failure_and_contract_tamper() -> None:
    source = _source()
    contract = _contract()
    manifest = _projecting_manifest(source, contract)
    failed = _table_state(contract, "failed", failure_reason="write failed")
    with pytest.raises(ValidationError, match="requires every required table"):
        transition_settlement(manifest, "ready", tables=(failed,))

    tampered = ProjectionTableSettlement(
        **{
            **contract.tables[0].model_dump(mode="python"),
            "relative_path": "job_id=job-1/other.parquet",
        },
        state="ready",
        source_digest=source.cas_content_digest,
        file_digest=_DIGEST_C,
        row_count=1,
    )
    with pytest.raises(ValidationError, match="differs from its projection contract"):
        transition_settlement(manifest, "ready", tables=(tampered,))


def test_atomic_parquet_verification_preserves_previous_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "table.parquet"
    write_table_atomic(path, [{"label": "old", "count": 1}], _SCHEMA)
    previous = path.read_bytes()
    monkeypatch.setattr(parquet_io.pq, "read_schema", lambda _path: pa.schema([]))

    with pytest.raises(ValueError, match="schema mismatch"):
        write_table_atomic(path, [{"label": "new", "count": 2}], _SCHEMA)

    assert path.read_bytes() == previous
    assert not tuple(tmp_path.glob(".table.parquet.*.tmp"))


def test_attach_admits_only_verified_manifest_and_rejects_mutation(tmp_path: Path) -> None:
    root = tmp_path / "derived" / "parquet"
    manifest, path = _ready_manifest(root, _source(), _contract())
    assert manifest.state == "ready"

    connection = duckdb.connect(":memory:")
    connection.execute("CREATE SCHEMA z3")
    status = _attach_z3(connection, root)
    assert status.state == "partial"
    assert connection.execute("SELECT label, count FROM behavior_labels").fetchall() == [
        ("ready", 1)
    ]
    connection.close()

    write_table_atomic(path, [{"label": "tampered", "count": 2}], _SCHEMA)
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE SCHEMA z3")
    status = _attach_z3(connection, root)
    assert (
        next(table for table in status.tables if table.table_name == "behavior_labels").state
        == "stale"
    )
    with pytest.raises(duckdb.CatalogException):
        connection.execute("SELECT * FROM behavior_labels")
    connection.close()


def test_attach_typed_empty_requires_explicit_not_applicable(tmp_path: Path) -> None:
    root = tmp_path / "derived" / "parquet"
    source = _source()
    contract = _contract(table_name="behavior_labels", required=False)
    manifest = _projecting_manifest(source, contract)
    manifest = transition_settlement(
        manifest,
        "ready",
        tables=(_table_state(contract, "not_applicable"),),
    )
    write_settlement_manifest(root, manifest)

    connection = duckdb.connect(":memory:")
    connection.execute("CREATE SCHEMA z3")
    status = _attach_z3(connection, root)
    assert (
        next(table for table in status.tables if table.table_name == "behavior_labels").state
        == "not_applicable"
    )
    assert connection.execute("SELECT count(*) FROM behavior_labels").fetchone() == (0,)
    columns = connection.execute("DESCRIBE behavior_labels").fetchall()
    assert [(row[0], row[1]) for row in columns] == [
        ("label", "VARCHAR"),
        ("count", "BIGINT"),
    ]
    connection.close()


def test_unmanifested_parquet_is_unavailable_not_an_empty_success(tmp_path: Path) -> None:
    root = tmp_path / "derived" / "parquet"
    write_table_atomic(
        root / "job_id=job-1/behavior_labels.parquet",
        [{"label": "orphan", "count": 1}],
        _SCHEMA,
    )
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE SCHEMA z3")
    status = _attach_z3(connection, root)
    assert status.state == "unavailable"
    with pytest.raises(duckdb.CatalogException):
        connection.execute("SELECT * FROM behavior_labels")
    assert connection.execute(
        "SELECT state FROM table_readiness WHERE table_name = 'behavior_labels'"
    ).fetchone() == ("missing",)
    connection.close()


def _catalog_snapshot(manifest):
    source = manifest.source
    contract = manifest.contract
    current = (
        manifest.state,
        manifest.manifest_digest,
        *source.catalog_identity(),
        contract.contract_digest,
        contract.producer_code_digest,
        manifest.rebuild_sequence,
        manifest.supersedes_settlement_id,
    )
    events = tuple(_event_row(event) for event in manifest.events)
    tables = tuple(_table_row(table, contract) for table in manifest.tables)
    return current, events, tables


def test_ledger_replay_is_idempotent_and_regression_or_fork_is_refused() -> None:
    manifest = create_settlement_manifest(_source(), _contract())
    assert _validate_ledger_replay(manifest, None, (), ()) == "insert"
    current, events, tables = _catalog_snapshot(manifest)
    assert _validate_ledger_replay(manifest, current, events, tables) == "replay"

    advanced = transition_settlement(manifest, "source_validated")
    assert _validate_ledger_replay(advanced, current, events, tables) == "advance"
    advanced_current, advanced_events, advanced_tables = _catalog_snapshot(advanced)
    with pytest.raises(SettlementError, match="catalog_history_regression"):
        _validate_ledger_replay(
            manifest,
            advanced_current,
            advanced_events,
            advanced_tables,
        )

    forked = list(advanced_events)
    forked[-1] = (forked[-1][0], _DIGEST_A, *forked[-1][2:])
    with pytest.raises(SettlementError, match="catalog_history_fork"):
        _validate_ledger_replay(advanced, advanced_current, forked, advanced_tables)

    divergent = list(advanced_tables)
    divergent[0] = (*divergent[0][:11], "unexpected", *divergent[0][12:])
    with pytest.raises(SettlementError, match="catalog_table_replay_divergence"):
        _validate_ledger_replay(advanced, advanced_current, advanced_events, divergent)


class _Rows:
    def __init__(self, *, one=None, many=(), rowcount=1):
        self._one = one
        self._many = many
        self.rowcount = rowcount

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class _LedgerConnection:
    def __init__(self, current, events, tables):
        self.current = current
        self.events = events
        self.tables = tables
        self.mutations: list[str] = []
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, _kind, _value, _traceback):
        self.rolled_back = _kind is not None

    def execute(self, query, _parameters):
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT state, manifest_digest"):
            return _Rows(one=self.current)
        if normalized.startswith("SELECT sequence, event_id"):
            return _Rows(many=self.events)
        if normalized.startswith("SELECT table_name, partition_identity"):
            return _Rows(many=self.tables)
        self.mutations.append(normalized)
        return _Rows()


def test_postgres_persistence_replay_is_noop_and_regression_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = create_settlement_manifest(_source(), _contract())
    current, events, tables = _catalog_snapshot(manifest)
    replay = _LedgerConnection(current, events, tables)
    monkeypatch.setattr(
        settlement_module.psycopg,
        "connect",
        lambda _url: replay,
    )
    settlement_module.persist_settlement_manifest("postgresql://unused", manifest)
    assert replay.mutations == []
    assert replay.rolled_back is False

    advanced = transition_settlement(manifest, "source_validated")
    advanced_current, advanced_events, advanced_tables = _catalog_snapshot(advanced)
    regression = _LedgerConnection(
        advanced_current,
        advanced_events,
        advanced_tables,
    )
    monkeypatch.setattr(
        settlement_module.psycopg,
        "connect",
        lambda _url: regression,
    )
    with pytest.raises(SettlementError, match="catalog_history_regression"):
        settlement_module.persist_settlement_manifest(
            "postgresql://unused",
            manifest,
        )
    assert regression.mutations == []
    assert regression.rolled_back is True


def test_reconciliation_reports_matched_and_extra_without_mutation(tmp_path: Path) -> None:
    source_dir = tmp_path / "raw" / "job-1"
    source_dir.mkdir(parents=True)
    (source_dir / "result.json").write_text("{}\n")
    archive = archive_evidence(
        source_dir,
        tmp_path / "store",
        kind="job",
        record_id="job-1",
    )
    locator = evidence_locator(tmp_path / "store", archive)
    source = SettlementSource.from_cas_locator(locator)
    root = tmp_path / "derived" / "parquet"
    _ready_manifest(root, source, _contract())
    orphan = root / "orphan.parquet"
    write_table_atomic(orphan, [{"label": "orphan", "count": 1}], _SCHEMA)
    before = orphan.read_bytes()

    first = reconcile_projection_inventory(
        store_root=tmp_path / "store",
        derived_root=root,
        expected_source_locators={("job", "job-1"): locator},
    )
    second = reconcile_projection_inventory(
        store_root=tmp_path / "store",
        derived_root=root,
        expected_source_locators={("job", "job-1"): locator},
    )

    assert first == second
    assert first.counts["matched"] == 1
    assert first.counts["extra_projection"] == 1
    assert orphan.read_bytes() == before


def test_filesystem_manifest_refuses_regression_and_fork(tmp_path: Path) -> None:
    root = tmp_path / "derived" / "parquet"
    moment = datetime(2026, 8, 31, tzinfo=UTC)
    initial = create_settlement_manifest(
        _source(),
        _contract(),
        clock=lambda: moment,
    )
    path = write_settlement_manifest(root, initial)
    advanced = transition_settlement(
        initial,
        "source_validated",
        detail={"branch": "accepted"},
        clock=lambda: moment + timedelta(seconds=1),
    )
    write_settlement_manifest(root, advanced)
    accepted_bytes = path.read_bytes()

    with pytest.raises(SettlementError, match="catalog_history_regression"):
        write_settlement_manifest(root, initial)
    assert path.read_bytes() == accepted_bytes

    fork = transition_settlement(
        initial,
        "source_validated",
        detail={"branch": "fork"},
        clock=lambda: moment + timedelta(seconds=2),
    )
    with pytest.raises(SettlementError, match="catalog_history_fork"):
        write_settlement_manifest(root, fork)
    assert load_settlement_manifest(path) == advanced


def test_supersession_inventory_rejects_dangling_cross_source_and_fork(
    tmp_path: Path,
) -> None:
    root = tmp_path / "derived" / "parquet"
    parent = create_settlement_manifest(_source(), _contract())
    write_settlement_manifest(root, parent)
    dangling = create_settlement_manifest(
        _source(),
        _contract(),
        rebuild_sequence=1,
        supersedes_settlement_id=_DIGEST_A,
    )
    write_settlement_manifest(root, dangling)
    inventory = load_settlement_manifests(root)
    assert any("dangling supersession" in error.reason for error in inventory.errors)
    with pytest.raises(SettlementError, match="malformed_settlement_inventory"):
        active_settlement_manifests(inventory)

    other_root = tmp_path / "cross-source"
    write_settlement_manifest(other_root, parent)
    other_source = SettlementSource(
        **{
            **_source().model_dump(mode="python"),
            "source_id": "job-2",
            "cas_record_id": "job-2",
        }
    )
    cross_source = create_settlement_manifest(
        other_source,
        _contract(),
        rebuild_sequence=1,
        supersedes_settlement_id=parent.settlement_id,
    )
    write_settlement_manifest(other_root, cross_source)
    inventory = load_settlement_manifests(other_root)
    assert any("crosses source" in error.reason for error in inventory.errors)

    fork_root = tmp_path / "fork"
    write_settlement_manifest(fork_root, parent)
    for suffix in ("d", "e"):
        successor_source = SettlementSource(
            **{
                **_source().model_dump(mode="python"),
                "cas_record_digest": "sha256:" + suffix * 64,
                "source_manifest_digest": "sha256:" + suffix * 64,
            }
        )
        successor = create_settlement_manifest(
            successor_source,
            _contract(),
            rebuild_sequence=1,
            supersedes_settlement_id=parent.settlement_id,
        )
        write_settlement_manifest(fork_root, successor)
    inventory = load_settlement_manifests(fork_root)
    assert any("fork" in error.reason for error in inventory.errors)


def test_interpretation_defaults_to_canonical_derived_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Path] = {}

    def fake_core(*_args, **kwargs):
        captured["derived_root"] = kwargs["derived_root"]
        return {}, None

    monkeypatch.setattr(trajectory_runtime, "_analyze_trial_core", fake_core)
    trajectory_runtime.analyze_trial(
        "unused",
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        output_dir=tmp_path / "interpretation" / "trial-1",
    )
    assert captured["derived_root"] == tmp_path / "derived" / "parquet"
