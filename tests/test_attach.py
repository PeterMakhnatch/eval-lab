"""Tests for E04 unified attach surface.

CI has no derived/, runs/, or PostgreSQL — tests must pass without them.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from z3_settlement_helpers import admit_z3_tree

from evallab.cli import _redact_database_dsn, run_cli
from evallab.runner import database_url_from_environment
from evallab.storage.attach import (
    SEMANTIC_COMPARISON_COLUMNS,
    TABLES,
    build_sql_preamble,
)
from evallab.storage.attach import (
    attach as public_attach,
)


def _catalog_reachable(dsn: str | None = None) -> bool:
    if os.environ.get("EVALLAB_RUN_POSTGRES_TESTS") != "1":
        return False
    try:
        import psycopg

        url = database_url_from_environment(dsn)
        with psycopg.connect(url, connect_timeout=1) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


_DSN_FOR_TEST = database_url_from_environment()


def _write_parquet_tree(root: Path, table: str, rows: list[dict]) -> None:
    job_dir = root / "job_id=testjob" / "trial_id=testtrial"
    job_dir.mkdir(parents=True, exist_ok=True)
    table_path = job_dir / f"{table}.parquet"
    tbl = pa.Table.from_pylist(rows) if rows else pa.table({"id": pa.array([], type=pa.string())})
    pq.write_table(tbl, table_path)
    admit_z3_tree(root)


def attach(*, repo_root: Path, explicit_derived: Path | None = None):
    if explicit_derived is not None and any(explicit_derived.rglob("*.parquet")):
        admit_z3_tree(explicit_derived)
    return public_attach(repo_root=repo_root, explicit_derived=explicit_derived)


def test_z3_views_return_written_rows(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(
        derived,
        "trial_facts",
        [{"trial_id": "t1", "reward": 1.0}, {"trial_id": "t2", "reward": 0.0}],
    )
    _write_parquet_tree(
        derived,
        "jobs",
        [{"job_id": "testjob", "status": "done"}],
    )
    _write_parquet_tree(
        derived,
        "behavior_labels",
        [
            {
                "label_id": "label-1",
                "trial_id": "t1",
                "target_type": "trajectory",
                "model_name": "judge-model",
            }
        ],
    )
    _write_parquet_tree(
        derived,
        "behavior_episodes",
        [
            {
                "episode_id": "episode-1",
                "trial_id": "t1",
                "document_id": "document-1",
                "label": "tool_error",
            }
        ],
    )
    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        rows = result.connection.execute("SELECT COUNT(*) FROM trial_facts").fetchone()
        assert rows is not None
        assert rows[0] == 2
        rows_z3 = result.connection.execute("SELECT COUNT(*) FROM z3.trial_facts").fetchone()
        assert rows_z3 is not None
        assert rows_z3[0] == 2
        job_rows = result.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()
        assert job_rows is not None
        assert job_rows[0] == 1
        job_rows_z3 = result.connection.execute("SELECT COUNT(*) FROM z3.jobs").fetchone()
        assert job_rows_z3 is not None
        assert job_rows_z3[0] == 1
        label_rows = result.connection.execute(
            "SELECT target_type, model_name FROM behavior_labels"
        ).fetchall()
        assert label_rows == [("trajectory", "judge-model")]
        episode_rows = result.connection.execute(
            "SELECT episode_id, document_id, label FROM z3.behavior_episodes"
        ).fetchall()
        assert episode_rows == [("episode-1", "document-1", "tool_error")]
    finally:
        result.connection.close()


def test_z3_jobs_view_reads_job_level_parquet(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    job_dir = derived / "job_id=abc123"
    job_dir.mkdir(parents=True)
    rows = [
        {"job_id": "abc123", "status": "done", "n_trials": 2},
        {"job_id": "abc123", "status": "done", "n_trials": 3},
    ]
    pq.write_table(pa.Table.from_pylist(rows), job_dir / "jobs.parquet")
    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        jobs_count = result.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()
        assert jobs_count is not None
        assert jobs_count[0] == 2
        z3_jobs_count = result.connection.execute("SELECT COUNT(*) FROM z3.jobs").fetchone()
        assert z3_jobs_count is not None
        assert z3_jobs_count[0] == 2
        job_ids = result.connection.execute(
            "SELECT DISTINCT job_id FROM jobs ORDER BY job_id"
        ).fetchall()
        assert job_ids == [("abc123",)]
    finally:
        result.connection.close()


def test_z3_jobs_view_prefers_job_level_over_legacy_trial_nested(tmp_path: Path) -> None:
    """When both a job-level jobs.parquet and a legacy trial-nested jobs.parquet
    exist for the same job, the job-level file must win — unioning both would
    double-count job rows since jobs.parquet is keyed by job_id alone.
    """
    derived = tmp_path / "derived"
    job_dir = derived / "job_id=abc123"
    job_dir.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist([{"job_id": "abc123", "status": "done"}]),
        job_dir / "jobs.parquet",
    )
    legacy_dir = job_dir / "trial_id=legacytrial"
    legacy_dir.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist([{"job_id": "abc123", "status": "done"}]),
        legacy_dir / "jobs.parquet",
    )
    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        jobs_count = result.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()
        assert jobs_count is not None
        assert jobs_count[0] == 1
        z3_jobs_count = result.connection.execute("SELECT COUNT(*) FROM z3.jobs").fetchone()
        assert z3_jobs_count is not None
        assert z3_jobs_count[0] == 1
    finally:
        result.connection.close()


@pytest.mark.parametrize("layout", ("hot", "cold-day"))
def test_semantic_vs_mechanical_view_joins_trial_tool_identity(
    tmp_path: Path,
    layout: str,
) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()

    def _write_layout_tree(table: str, rows: list[dict]) -> None:
        if layout == "hot":
            _write_parquet_tree(derived, table, rows)
            return
        path = derived / "compact" / "dt=2026-08-25" / f"{table}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows), path)

    _write_layout_tree(
        "agent_actions",
        [
            {
                "job_id": "job-1",
                "trial_id": "trial-1",
                "document_id": "document-1",
                "step_id": "step-1",
                "tool_call_id": "call-1",
                "action_id": "action-1",
                "function_name": "bash",
                "outcome": "error",
                "exit_code": 1,
                "arguments_sha256": "sha256:" + "a" * 64,
            }
        ],
    )
    _write_layout_tree(
        "semantic_action_facts",
        [
            {
                "job_id": "job-1",
                "trial_id": "trial-1",
                "document_id": "document-1",
                "action_id": "action-1",
                "tool_call_id": "call-1",
                "task_id": "task-1",
                "binding_digest": "sha256:" + "b" * 64,
                "profile_id": "posix-generic",
                "profile_version": "1.0.0",
                "profile_digest": "sha256:" + "c" * 64,
                "role": "search",
                "outcome": "expected_negative",
                "reason_code": "pattern_not_found",
                "detail_digest": "sha256:" + "d" * 64,
                "detail_size": 16,
                "observation_correlation": "matched_call_id",
                "correlation_reason": None,
                "intervention_provenance": "autonomous",
                "intervention_sha256": None,
                "intervention_length": None,
                "intervention_reason": None,
            }
        ],
    )
    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        row = result.connection.execute(
            "SELECT mechanical_outcome, semantic_outcome, semantic_role, profile_id, "
            "reason_code, detail_digest, detail_size "
            "FROM v_semantic_vs_mechanical"
        ).fetchone()
        assert row == (
            "error",
            "expected_negative",
            "search",
            "posix-generic",
            "pattern_not_found",
            "sha256:" + "d" * 64,
            16,
        )
        columns = tuple(
            item[0]
            for item in result.connection.execute("DESCRIBE v_semantic_vs_mechanical").fetchall()
        )
        assert columns == SEMANTIC_COMPARISON_COLUMNS
        assert "mechanical.*" not in result.sql_preamble
        assert "semantic.outcome_detail" not in result.sql_preamble
        assert "semantic.reason_code" in result.sql_preamble
        assert "mechanical.document_id = semantic.document_id" in result.sql_preamble
        assert (
            result.connection.execute("SELECT * FROM v_semantic_vs_mechanical").fetchall()
            == result.connection.execute("SELECT * FROM z3.v_semantic_vs_mechanical").fetchall()
        )
    finally:
        result.connection.close()


def test_semantic_comparison_uses_document_identity(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(
        derived,
        "agent_actions",
        [
            {
                "job_id": "job-1",
                "trial_id": "trial-1",
                "document_id": "document-a",
                "tool_call_id": "same-call",
                "action_id": "action-a",
                "function_name": "bash",
                "outcome": "success",
                "exit_code": 0,
                "arguments_sha256": "sha256:" + "a" * 64,
            },
            {
                "job_id": "job-1",
                "trial_id": "trial-1",
                "document_id": "document-b",
                "tool_call_id": "same-call",
                "action_id": "action-b",
                "function_name": "bash",
                "outcome": "error",
                "exit_code": 1,
                "arguments_sha256": "sha256:" + "b" * 64,
            },
        ],
    )
    _write_parquet_tree(
        derived,
        "semantic_action_facts",
        [
            {
                "job_id": "job-1",
                "trial_id": "trial-1",
                "document_id": "document-a",
                "action_id": "action-a",
                "tool_call_id": "same-call",
                "task_id": "task-1",
                "binding_digest": "sha256:" + "c" * 64,
                "profile_id": "profile",
                "profile_version": "1",
                "profile_digest": "sha256:" + "d" * 64,
                "role": "search",
                "outcome": "expected_positive",
                "reason_code": None,
                "detail_digest": None,
                "detail_size": None,
                "observation_correlation": "matched_call_id",
                "correlation_reason": None,
                "intervention_provenance": "autonomous",
                "intervention_sha256": None,
                "intervention_length": None,
                "intervention_reason": None,
            }
        ],
    )
    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        rows = result.connection.execute(
            "SELECT document_id, semantic_outcome "
            "FROM v_semantic_vs_mechanical ORDER BY document_id"
        ).fetchall()
        assert rows == [
            ("document-a", "expected_positive"),
            ("document-b", None),
        ]
    finally:
        result.connection.close()


def test_semantic_comparison_matches_null_tool_ids_by_action_identity(
    tmp_path: Path,
) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(
        derived,
        "agent_actions",
        [
            {
                "job_id": "job-1",
                "trial_id": "trial-1",
                "document_id": "document-1",
                "tool_call_id": None,
                "action_id": "action-1",
                "function_name": "bash",
                "outcome": "success",
                "exit_code": 0,
                "arguments_sha256": "sha256:" + "a" * 64,
            }
        ],
    )
    _write_parquet_tree(
        derived,
        "semantic_action_facts",
        [
            {
                "job_id": "job-1",
                "trial_id": "trial-1",
                "document_id": "document-1",
                "action_id": "action-1",
                "tool_call_id": None,
                "task_id": "task-1",
                "binding_digest": "sha256:" + "b" * 64,
                "profile_id": "profile",
                "profile_version": "1",
                "profile_digest": "sha256:" + "c" * 64,
                "role": "search",
                "outcome": "unknown_semantics",
                "reason_code": "missing_call_id",
                "detail_digest": None,
                "detail_size": None,
                "observation_correlation": "unavailable",
                "correlation_reason": "nullable tool identity",
                "intervention_provenance": "autonomous",
                "intervention_sha256": None,
                "intervention_length": None,
                "intervention_reason": None,
            }
        ],
    )
    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        row = result.connection.execute(
            "SELECT tool_call_id, semantic_outcome, reason_code FROM v_semantic_vs_mechanical"
        ).fetchone()
        assert row == (None, "unknown_semantics", "missing_call_id")
    finally:
        result.connection.close()


def test_standalone_parquet_is_attached_once_and_preamble_has_one_glob(
    tmp_path: Path,
) -> None:
    derived = tmp_path / "derived"
    labels_dir = derived / "behavior_labels"
    labels_dir.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist([{"label_id": "label-1", "trial_id": "t1"}]),
        labels_dir / "behavior_labels.parquet",
    )

    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        assert result.connection.execute(
            "SELECT count(*), count(DISTINCT label_id) FROM behavior_labels"
        ).fetchone() == (1, 1)
        assert result.connection.execute(
            "SELECT count(*), count(DISTINCT label_id) FROM z3.behavior_labels"
        ).fetchone() == (1, 1)
        exact_path = str(labels_dir / "behavior_labels.parquet")
        assert result.sql_preamble.count(exact_path) == 2
        assert "behavior_labels/*.parquet" not in result.sql_preamble
    finally:
        result.connection.close()


def test_z2_unavailable_reports_reason_not_empty_view(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    result = attach(repo_root=Path.cwd())
    try:
        z2 = next(z for z in result.zones if z.name == "z2")
        assert z2.attached is False
        assert z2.reason is not None
    finally:
        result.connection.close()


def test_z4_front_matter_populates_from_docs(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "test.md").write_text(
        "---\nstatus: living\naudience:\n  - builder\n---\n\n# Title\nbody",
        encoding="utf-8",
    )
    result = attach(repo_root=tmp_path)
    try:
        rows = result.connection.execute(
            "SELECT status, audience FROM z4.front_matter WHERE path = 'docs/test.md'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "living"
        rows_unqualified = result.connection.execute(
            "SELECT status, audience FROM front_matter WHERE path = 'docs/test.md'"
        ).fetchall()
        assert len(rows_unqualified) == 1
        assert rows_unqualified[0][0] == "living"
    finally:
        result.connection.close()


def test_sql_preamble_is_byte_identical(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    r1 = attach(repo_root=tmp_path, explicit_derived=derived)
    r2 = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        assert r1.sql_preamble == r2.sql_preamble
    finally:
        r1.connection.close()
        r2.connection.close()


def test_cross_zone_join_when_postgres_unavailable_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    result = attach(repo_root=Path.cwd())
    try:
        z2 = next(z for z in result.zones if z.name == "z2")
        assert z2.attached is False
    finally:
        result.connection.close()


def test_cli_zones_reports_z3_with_row_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: E501
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(derived, "trial_facts", [{"trial_id": "t1"}, {"trial_id": "t2"}])
    _write_parquet_tree(derived, "jobs", [{"job_id": "j1"}])
    for t in [
        "reward_facts",
        "artifact_facts",
        "trajectories",
        "steps",
        "tool_calls",
        "tool_usage",
        "observations",
        "state_changes",
    ]:  # noqa: E501
        _write_parquet_tree(derived, t, [{"trial_id": "t1"}])
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    code = run_cli(["db", "attach", "--derived-root", "derived", "--zones"], workspace=tmp_path)
    out, _ = capsys.readouterr()
    assert code == 0
    assert "z3: ready" in out
    assert f"10 ready, {len(TABLES) - 10} not applicable, 0 blocked" in out


def test_cli_zones_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: E501
    # every zone unavailable -> non-zero
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    code = run_cli(["db", "attach", "--derived-root", "derived", "--zones"], workspace=tmp_path)
    assert code == 1
    # partial (z3 present) -> zero
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(derived, "trial_facts", [{"trial_id": "t1"}])
    code = run_cli(["db", "attach", "--derived-root", "derived", "--zones"], workspace=tmp_path)
    assert code == 0


def test_cli_print_sql_byte_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: E501
    derived = tmp_path / "derived"
    derived.mkdir()
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    code1 = run_cli(
        ["db", "attach", "--derived-root", "derived", "--print-sql"], workspace=tmp_path
    )  # noqa: E501
    out1, _ = capsys.readouterr()
    code2 = run_cli(
        ["db", "attach", "--derived-root", "derived", "--print-sql"], workspace=tmp_path
    )  # noqa: E501
    out2, _ = capsys.readouterr()
    assert code1 == 0 and code2 == 0
    assert out1 == out2


def test_sql_preamble_escapes_quote_bearing_dsn_and_parquet_paths(tmp_path: Path) -> None:
    derived = tmp_path / "derived'root"
    dsn = "postgresql://evallab:p'ass@invalid.example/evallab"
    derived.mkdir()
    _write_parquet_tree(derived, "trial_facts", [{"trial_id": "t1"}])

    preamble = build_sql_preamble(dsn, derived, tmp_path)

    escaped_dsn = dsn.replace("'", "''")
    admitted_path = derived / "job_id=testjob" / "trial_id=testtrial" / "trial_facts.parquet"
    escaped_path = str(admitted_path).replace("'", "''")
    assert f"'{escaped_dsn}' AS z2" in preamble
    assert f"'{escaped_path}'" in preamble
    assert "p'ass" not in preamble


def test_cli_print_sql_redacts_password_and_attach_repr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    secret = "not-for-output"
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql://evallab:{secret}@invalid.example/evallab",
    )

    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        assert secret not in repr(result)
    finally:
        result.connection.close()

    code = run_cli(
        ["db", "attach", "--derived-root", "derived", "--print-sql"],
        workspace=tmp_path,
    )
    out, _ = capsys.readouterr()
    assert code == 0
    assert secret not in out
    assert "REDACTED" in out
    assert "NON-EXECUTABLE" in out


@pytest.mark.parametrize(
    "dsn, secret",
    [
        (
            r"host=invalid.example user=evallab password='pa\'ss word' dbname=evallab",
            r"pa\'ss word",
        ),
        (
            r"host=invalid.example user=evallab sslpassword=pa\ ss dbname=evallab",
            r"pa\ ss",
        ),
    ],
)
def test_database_dsn_redaction_consumes_backslash_escaped_keyword_values(
    dsn: str, secret: str
) -> None:
    redacted, had_credentials = _redact_database_dsn(dsn)

    assert had_credentials is True
    assert secret not in redacted
    assert "invalid.example" in redacted
    assert "dbname=evallab" in redacted
    assert "<REDACTED>" in redacted


def test_cli_query_returns_fixture_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: E501
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(derived, "trial_facts", [{"trial_id": "t1", "reward": 1.0}])
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    code = run_cli(
        [
            "db",
            "attach",
            "--derived-root",
            "derived",
            "--query",
            "select count(*) from trial_facts",
        ],
        workspace=tmp_path,
    )  # noqa: E501
    out, _ = capsys.readouterr()
    assert code == 0
    assert out.strip() == "(1,)"


def test_duckdb_z2_catalog_prefix_resolution_standalone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a clean DuckDB session with attached database z2, z2.<table> resolves unambiguously."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    db_path = tmp_path / "z2_catalog.duckdb"
    with duckdb.connect(str(db_path)) as z2_con:
        z2_con.execute("CREATE TABLE verdicts (id TEXT, status TEXT)")
        z2_con.execute(
            "INSERT INTO verdicts VALUES ('testjob', 'accepted'), ('otherjob', 'rejected')"
        )
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(derived, "trial_facts", [{"trial_id": "t1", "job_id": "1"}])

    # Attach with z2 as an attached catalog (simulating what postgres_scanner does)
    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        result.connection.execute(f"ATTACH '{db_path}' AS z2")
        # Qualified query must resolve unambiguously
        rows = result.connection.execute("SELECT count(*) FROM z2.verdicts").fetchall()
        assert rows == [(2,)]
        # Cross-zone join spanning z2 catalog and z3 parquet view
        join_rows = result.connection.execute(
            "SELECT v.status, COUNT(*) FROM z2.verdicts v "
            "JOIN z3.trial_facts t ON v.id::text = t.job_id::text GROUP BY v.status"
        ).fetchall()
        assert join_rows == [("accepted", 1)]
    finally:
        result.connection.close()


@pytest.mark.skipif(
    not _catalog_reachable(),
    reason=f"requires live PostgreSQL {_DSN_FOR_TEST}",
)
def test_cli_query_with_real_postgres(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(derived, "trial_facts", [{"trial_id": "t1", "job_id": "j1"}])
    code = run_cli(
        [
            "db",
            "attach",
            "--derived-root",
            "derived",
            "--query",
            "SELECT COUNT(*) FROM z2.verdicts",
        ],
        workspace=tmp_path,
    )  # noqa: E501
    out, _ = capsys.readouterr()
    assert code == 0
    assert out.strip().startswith("(")


@pytest.mark.skipif(
    not _catalog_reachable(),
    reason=f"requires live PostgreSQL {_DSN_FOR_TEST}",
)
def test_cross_zone_join_with_real_postgres(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(derived, "trial_facts", [{"trial_id": "t1", "job_id": "j1"}])
    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        # Both qualified z3.trial_facts and unqualified trial_facts join with z2.jobs
        rows1 = result.connection.execute(
            "SELECT j.job_name, COUNT(*) FROM z2.jobs j "
            "JOIN z3.trial_facts t ON j.id::text = t.job_id::text GROUP BY j.job_name"
        ).fetchall()
        assert isinstance(rows1, list)
        rows2 = result.connection.execute(
            "SELECT j.job_name, COUNT(*) FROM z2.jobs j "
            "JOIN trial_facts t ON j.id::text = t.job_id::text GROUP BY j.job_name"
        ).fetchall()
        assert isinstance(rows2, list)
    finally:
        result.connection.close()


def test_semantic_tables_hot_cold_standalone_discovery(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()

    # 1. Standalone layout: capability_opportunities
    standalone_dir = derived / "capability_opportunities"
    standalone_dir.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "source_ref": "ref1",
                    "source_digest": "sha256:" + "a" * 64,
                    "provenance_kind": "mechanical",
                    "opportunity_id": "opp-standalone-1",
                    "trial_id": "t1",
                    "benchmark": "b1",
                    "construct": "c1",
                    "start_step": 0,
                    "end_step": 1,
                    "eligible": True,
                    "required_evidence": ["e1"],
                    "missing_evidence": [],
                }
            ]
        ),
        standalone_dir / "data.parquet",
    )

    # 2. Hot layout: process_step_facts
    _write_parquet_tree(
        derived,
        "process_step_facts",
        [
            {
                "source_ref": "ref2",
                "source_digest": "sha256:" + "b" * 64,
                "provenance_kind": "mechanical",
                "trial_id": "t1",
                "source_trajectory_id": "traj-1",
                "source_step_id": "step-1",
                "label": "correct",
                "original_label": None,
                "propagated_from_step": None,
                "first_error": None,
            }
        ],
    )

    # 3. Cold layout: constraint_facts
    cold_dir = derived / "compact" / "constraint_facts" / "dt=2026-08-25"
    cold_dir.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "source_ref": "ref3",
                    "source_digest": "sha256:" + "c" * 64,
                    "provenance_kind": "benchmark_verifier",
                    "trial_id": "t1",
                    "plan_id": "p1",
                    "action_id": None,
                    "constraint_id": "c1",
                    "constraint_scope": "local",
                    "required": True,
                    "verdict": "satisfied",
                    "verifier_evidence": "verified",
                }
            ]
        ),
        cold_dir / "part0.parquet",
    )

    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        # Standalone
        res_opp = result.connection.execute(
            "SELECT opportunity_id, benchmark, construct FROM capability_opportunities"
        ).fetchall()
        assert res_opp == [("opp-standalone-1", "b1", "c1")]
        res_opp_z3 = result.connection.execute(
            "SELECT opportunity_id FROM z3.capability_opportunities"
        ).fetchall()
        assert res_opp_z3 == [("opp-standalone-1",)]

        # Hot
        res_step = result.connection.execute(
            "SELECT source_step_id, label FROM process_step_facts"
        ).fetchall()
        assert res_step == [("step-1", "correct")]
        res_step_z3 = result.connection.execute(
            "SELECT source_step_id FROM z3.process_step_facts"
        ).fetchall()
        assert res_step_z3 == [("step-1",)]

        # Cold
        res_const = result.connection.execute(
            "SELECT constraint_id, verdict FROM constraint_facts"
        ).fetchall()
        assert res_const == [("c1", "satisfied")]
        res_const_z3 = result.connection.execute(
            "SELECT constraint_id FROM z3.constraint_facts"
        ).fetchall()
        assert res_const_z3 == [("c1",)]
    finally:
        result.connection.close()


def test_semantic_tables_multi_layout_union(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()

    # Write context_operation_facts across hot, cold, and standalone
    # Hot
    _write_parquet_tree(
        derived,
        "context_operation_facts",
        [
            {
                "source_ref": "ref-hot",
                "source_digest": "sha256:" + "1" * 64,
                "provenance_kind": "mechanical",
                "trial_id": "t1",
                "operation_id": "op-hot",
                "operation": "compaction",
                "configured_size": 100,
                "realized_size": 80,
                "prompt_tokens": 50,
                "before_token_count": 200,
                "after_token_count": 120,
                "content_digest": None,
            }
        ],
    )
    # Cold
    cold_dir = derived / "compact" / "context_operation_facts" / "dt=2026-08-20"
    cold_dir.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "source_ref": "ref-cold",
                    "source_digest": "sha256:" + "2" * 64,
                    "provenance_kind": "mechanical",
                    "trial_id": "t2",
                    "operation_id": "op-cold",
                    "operation": "clear",
                    "configured_size": None,
                    "realized_size": None,
                    "prompt_tokens": None,
                    "before_token_count": None,
                    "after_token_count": None,
                    "content_digest": None,
                }
            ]
        ),
        cold_dir / "part0.parquet",
    )
    # Standalone
    standalone_dir = derived / "context_operation_facts"
    standalone_dir.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "source_ref": "ref-standalone",
                    "source_digest": "sha256:" + "3" * 64,
                    "provenance_kind": "mechanical",
                    "trial_id": "t3",
                    "operation_id": "op-standalone",
                    "operation": "evict",
                    "configured_size": None,
                    "realized_size": None,
                    "prompt_tokens": None,
                    "before_token_count": None,
                    "after_token_count": None,
                    "content_digest": None,
                }
            ]
        ),
        standalone_dir / "batch1.parquet",
    )

    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        rows = result.connection.execute(
            "SELECT operation_id, trial_id, operation FROM context_operation_facts ORDER BY trial_id"
        ).fetchall()
        assert rows == [
            ("op-hot", "t1", "compaction"),
            ("op-cold", "t2", "clear"),
            ("op-standalone", "t3", "evict"),
        ]
        assert result.connection.execute(
            "SELECT COUNT(*) FROM z3.context_operation_facts"
        ).fetchone() == (3,)
    finally:
        result.connection.close()


def test_honest_missing_semantic_tables_and_empty_views(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    # Write only one semantic table
    _write_parquet_tree(
        derived,
        "evidence_coverage",
        [
            {
                "source_ref": "ref-cov",
                "source_digest": "sha256:" + "d" * 64,
                "provenance_kind": "derived",
                "trial_id": "t1",
                "benchmark": "bench1",
                "construct": "planning",
                "exposed": True,
                "eligible": True,
                "required_evidence": ["p1"],
                "observed_evidence": ["p1"],
                "missing_evidence": [],
                "analysis_ready": True,
            }
        ],
    )

    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        z3_status = next(z for z in result.zones if z.name == "z3")
        assert z3_status.attached is True
        assert f"1 ready, {len(TABLES) - 1} not applicable, 0 blocked" in z3_status.detail
        readiness = {table.table_name: table.state for table in z3_status.tables}
        assert readiness["evidence_coverage"] == "ready"
        assert readiness["capability_opportunities"] == "not_applicable"
        assert readiness["paired_condition_facts"] == "not_applicable"
        assert readiness["session_dependency_facts"] == "not_applicable"

        # Missing semantic table returns 0 rows (honest empty view)
        missing_count = result.connection.execute(
            "SELECT COUNT(*) FROM capability_opportunities"
        ).fetchone()
        assert missing_count == (0,)

        missing_count_z3 = result.connection.execute(
            "SELECT COUNT(*) FROM z3.session_dependency_facts"
        ).fetchone()
        assert missing_count_z3 == (0,)
        assert result.connection.execute(
            "SELECT COUNT(*) FROM v_semantic_vs_mechanical"
        ).fetchone() == (0,)
        comparison_columns = tuple(
            item[0]
            for item in result.connection.execute("DESCRIBE v_semantic_vs_mechanical").fetchall()
        )
        assert comparison_columns == SEMANTIC_COMPARISON_COLUMNS

        # Present semantic table returns its data
        present_rows = result.connection.execute(
            "SELECT benchmark, construct, analysis_ready FROM evidence_coverage"
        ).fetchall()
        assert present_rows == [("bench1", "planning", True)]
    finally:
        result.connection.close()


def test_all_eight_semantic_tables_registered_in_preamble(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    admit_z3_tree(derived)
    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        expected_tables = [
            "capability_opportunities",
            "process_step_facts",
            "retrieval_facts",
            "constraint_facts",
            "context_operation_facts",
            "paired_condition_facts",
            "session_dependency_facts",
            "evidence_coverage",
        ]
        for tbl in expected_tables:
            assert f'CREATE OR REPLACE VIEW "{tbl}" AS' in result.sql_preamble
            assert f'CREATE OR REPLACE VIEW z3."{tbl}" AS' in result.sql_preamble
    finally:
        result.connection.close()


def test_existing_attach_layouts_return_exact_z3_rows(tmp_path: Path) -> None:
    derived = tmp_path / "derived"

    def write(relative: str, trial_id: str) -> None:
        path = derived / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.Table.from_pylist(
                [{"job_id": f"job-{trial_id}", "trial_id": trial_id, "reward": 1.0}]
            ),
            path,
        )

    write("job_id=hot/trial_id=hot/trial_facts.parquet", "hot")
    write("job_id=job-level/trial_facts.parquet", "job-level")
    write("compact/trial_facts/dt=2026-08-24/part0.parquet", "cold-table")
    write("compact/dt=2026-08-25/trial_facts.parquet", "cold-day")
    write("trial_facts/batch.parquet", "directory")
    write("trial_facts.parquet", "root")

    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        expected = [
            ("cold-day",),
            ("cold-table",),
            ("directory",),
            ("hot",),
            ("root",),
        ]
        assert (
            result.connection.execute(
                "SELECT trial_id FROM trial_facts ORDER BY trial_id"
            ).fetchall()
            == expected
        )
        assert (
            result.connection.execute(
                "SELECT trial_id FROM z3.trial_facts ORDER BY trial_id"
            ).fetchall()
            == expected
        )
        for relative_path in (
            "job_id=hot/trial_id=hot/trial_facts.parquet",
            "compact/trial_facts/dt=2026-08-24/part0.parquet",
            "compact/dt=2026-08-25/trial_facts.parquet",
            "trial_facts/batch.parquet",
            "trial_facts.parquet",
        ):
            assert relative_path in result.sql_preamble
        assert "job_id=job-level/trial_facts.parquet" not in result.sql_preamble
    finally:
        result.connection.close()


def test_cold_day_overlap_prefers_hot_primary_key(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    hot = derived / "job_id=job-1" / "trial_id=trial-1" / "trial_facts.parquet"
    cold = derived / "compact" / "dt=2026-08-25" / "trial_facts.parquet"
    hot.parent.mkdir(parents=True)
    cold.parent.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist([{"job_id": "job-1", "trial_id": "trial-1", "reward": 1.0}]),
        hot,
    )
    pq.write_table(
        pa.Table.from_pylist([{"job_id": "job-1", "trial_id": "trial-1", "reward": 0.0}]),
        cold,
    )

    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        assert result.connection.execute(
            "SELECT job_id, trial_id, reward FROM z3.trial_facts"
        ).fetchall() == [("job-1", "trial-1", 1.0)]
    finally:
        result.connection.close()


def test_every_z3_sql_view_remains_registered(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    admit_z3_tree(derived)
    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        for table in TABLES:
            assert result.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
            assert result.connection.execute(f"SELECT COUNT(*) FROM z3.{table}").fetchone() == (0,)
            assert f'CREATE OR REPLACE VIEW "{table}" AS' in result.sql_preamble
            assert f'CREATE OR REPLACE VIEW z3."{table}" AS' in result.sql_preamble
    finally:
        result.connection.close()
