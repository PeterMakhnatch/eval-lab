"""Actor projection contracts using only inert, temporary SQLite fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from evallab.production import actor_projection
from evallab.production.actor_projection import project_actor_database
from evallab.production.cloud_trace_db import CloudTraceDatabase

_TABLE_KEYS = {
    "corpus_tasks": "task_id",
    "task_issues": "issue_id",
    "agent_traces": "trace_id",
    "trace_steps": "trace_id, step_index",
}
_TASKS = {
    "fixture-alpha": (" DEV \n", "allowedalpha"),
    "fixture-beta": ("dev", "allowedbeta"),
    "fixture-unrequested": ("dev", "unrequestedpayload"),
    "fixture-held": ("held", "heldpayload"),
    "fixture-test": ("test", "testpayload"),
}
_IDENTITY_FIELDS = (
    "task_id", "dataset_id", "split", "original_grader_digest", "repaired_grader_digest",
    "item_id", "item_type",
)


def _seed_task(database: CloudTraceDatabase, task_id: str, split: str, marker: str) -> None:
    database.upsert_task({
        "task_id": task_id, "dataset_id": "synthetic-fixture", "split": split,
        "instruction_text": f"{marker}: describe the colors in an inert fixture.",
        "original_grader_digest": "sha256:" + "1" * 64,
        "repaired_grader_digest": "sha256:" + "2" * 64,
        "cas_uri": "cas://sha256/" + "3" * 64,
        "metadata_json": {"item_id": task_id + "-item", "item_type": "fixture", "marker": marker},
    })
    database.upsert_task_issue({
        "issue_id": task_id + "-issue", "task_id": task_id,
        "defect_class": "synthetic-label", "direction": "unfair_fail", "status": "provisional",
        "witness_digest": "sha256:" + "4" * 64, "original_reward": 0.25,
        "repaired_reward": 0.75, "citation_file": "synthetic/colors.txt", "citation_line": 7,
        "reproducer_script": f"{marker}: inert source text, not an executable reproducer.",
        "cas_uri": "cas://sha256/" + "5" * 64,
    })
    database.upsert_trace({
        "trace_id": task_id + "-trace", "run_id": task_id + "-run", "task_id": task_id,
        "model_id": "synthetic-agent", "outcome": marker, "reward": 0.25,
        "is_exploit": False, "is_task_defect_fail": True,
        "raw_cas_uri": "cas://sha256/" + "6" * 64,
        "verified_receipt_json": {"status": "provisional", "note": "source label only"},
    }, steps=[
        {
            "step_index": index, "role": "tool" if index == 9 else "assistant",
            "tool_name": "synthetic-reader", "command_or_action": f"{marker}: describe blue",
            "content_text": f"{marker}: inert body at index {index}.",
            "observation_preview": f"{marker}: preview {index}",
            "observation_cas_uri": "cas://sha256/" + "7" * 64,
            "flags_json": ["source-only", {"index": index}],
        }
        for index in (9, 0, 3)
    ])


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "source.sqlite"
    with CloudTraceDatabase(path) as database:
        for task_id, (split, marker) in _TASKS.items():
            _seed_task(database, task_id, split, marker)
    return path


def _rows(path: Path, table: str, task_ids: tuple[str, ...]) -> list[tuple[Any, ...]]:
    placeholders = ", ".join("?" for _ in task_ids)
    predicate = (
        f"trace_id IN (SELECT trace_id FROM agent_traces WHERE task_id IN ({placeholders}))"
        if table == "trace_steps" else f"task_id IN ({placeholders})"
    )
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            f"SELECT * FROM {table} WHERE {predicate} ORDER BY {_TABLE_KEYS[table]}", task_ids,
        ).fetchall()


def _identities(path: Path, task_ids: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute(
            "SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest, "
            "json_extract(metadata_json, '$.item_id'), json_type(metadata_json, '$.item_id') "
            f"FROM corpus_tasks WHERE task_id IN ({', '.join('?' for _ in task_ids)})",
            task_ids,
        )
        return {row[0]: dict(zip(_IDENTITY_FIELDS, row, strict=True)) for row in rows}


def _assert_no_output(destination: Path) -> None:
    assert not destination.exists()
    assert not list(destination.parent.glob(f".{destination.name}.projection-*"))


def _guard_source_bodies(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    connect = sqlite3.connect
    body_reads: list[tuple[str, str]] = []
    body_columns = {
        "instruction_text", "reproducer_script", "command_or_action", "content_text",
        "observation_preview", "verified_receipt_json", "flags_json",
    }

    def authorize(action: int, table: str, column: str, database: str, trigger: str) -> int:
        if action == sqlite3.SQLITE_READ and database == "source" and column in body_columns:
            body_reads.append((table, column))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def guarded_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = connect(*args, **kwargs)
        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(actor_projection.sqlite3, "connect", guarded_connect)
    return body_reads


def test_projection_preserves_all_columns_large_steps_json_and_source_labels(
    source: Path, tmp_path: Path,
) -> None:
    destination = tmp_path / "actor.sqlite"
    first_body = "allowedalpha complete observation café 确定\n" * 40_000
    second_body = "allowedalpha another unabridged step Ω\n" * 35_000
    metadata = ' { "item_type" : "fixture", "item_id" : "fixture-alpha-item", "z" : 1 } \n'
    receipt_json = ' { "note" : "source label only", "status" : "provisional" } \n'
    flags_json = ' [ "opaque source flag", { "z" : 2, "a" : 1 } ] \n'
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute(
            "UPDATE corpus_tasks SET metadata_json = ? WHERE task_id = ?",
            (metadata, "fixture-alpha"),
        )
        connection.execute(
            "UPDATE agent_traces SET verified_receipt_json = ? WHERE task_id = ?",
            (receipt_json, "fixture-alpha"),
        )
        connection.executemany(
            "UPDATE trace_steps SET content_text = ?, flags_json = ? "
            "WHERE trace_id = ? AND step_index = ?",
            [(body, flags_json, "fixture-alpha-trace", index)
             for body, index in ((first_body, 3), (second_body, 9))],
        )
    admitted = ("fixture-alpha", "fixture-beta")
    expected_rows = {table: _rows(source, table, admitted) for table in _TABLE_KEYS}
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    expected_identities = _identities(source, admitted)

    receipt = project_actor_database(
        source, destination, task_ids=iter(("fixture-beta", "fixture-alpha", "fixture-beta")),
        expected_identities=expected_identities,
    )

    assert receipt == {
        "schema": "evallab.actor_projection/v1",
        "admitted_task_ids": list(admitted),
        "table_counts": {"corpus_tasks": 2, "task_issues": 2, "agent_traces": 2, "trace_steps": 6},
        "database_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "database_bytes": destination.stat().st_size,
        "admitted_identities_sha256": hashlib.sha256(json.dumps(
            expected_identities, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest(),
    }
    assert destination.stat().st_mode & 0o777 == 0o600
    for table in _TABLE_KEYS:
        assert _rows(destination, table, admitted) == expected_rows[table]
    with closing(sqlite3.connect(destination)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT step_index, content_text FROM trace_steps WHERE trace_id = ? ORDER BY step_index",
            ("fixture-alpha-trace",),
        ).fetchall()[1:] == [(3, first_body), (9, second_body)]
        assert connection.execute(
            "SELECT split, metadata_json FROM corpus_tasks WHERE task_id = ?", ("fixture-alpha",),
        ).fetchone() == (" DEV \n", metadata)
        assert connection.execute(
            "SELECT verified_receipt_json FROM agent_traces WHERE trace_id = ?",
            ("fixture-alpha-trace",),
        ).fetchone() == (receipt_json,)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    assert not list(tmp_path.glob(".actor.sqlite.projection-*"))


def test_projection_excludes_unrequested_core_fts_and_deleted_pages(source: Path, tmp_path: Path) -> None:
    destination = tmp_path / "actor.sqlite"
    deleted_marker = "deletedpagepayload"
    raw_marker = "unrelatedrawtablepayload"
    with CloudTraceDatabase(source) as database:
        _seed_task(database, "fixture-deleted", "held", deleted_marker)
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("PRAGMA secure_delete=OFF")
        connection.execute(
            "UPDATE trace_steps SET content_text = ? WHERE trace_id = ?",
            ((deleted_marker + " ") * 4_096, "fixture-deleted-trace"),
        )
        connection.execute("DELETE FROM trace_steps WHERE trace_id = ?", ("fixture-deleted-trace",))
        connection.execute("DELETE FROM agent_traces WHERE task_id = ?", ("fixture-deleted",))
        connection.execute("DELETE FROM task_issues WHERE task_id = ?", ("fixture-deleted",))
        connection.execute("DELETE FROM corpus_tasks WHERE task_id = ?", ("fixture-deleted",))
        connection.execute("CREATE TABLE unrelated_raw (body TEXT NOT NULL)")
        connection.execute("INSERT INTO unrelated_raw VALUES (?)", (raw_marker,))
    before = source.read_bytes()
    assert deleted_marker.encode() in before  # Deleted source pages really retain inert fixture bytes.

    project_actor_database(source, destination, task_ids=["fixture-alpha"])

    with closing(sqlite3.connect(destination)) as connection:
        assert connection.execute("SELECT task_id FROM corpus_tasks").fetchall() == [("fixture-alpha",)]
        assert connection.execute("SELECT task_id FROM task_issues").fetchall() == [("fixture-alpha",)]
        assert connection.execute("SELECT task_id FROM agent_traces").fetchall() == [("fixture-alpha",)]
        assert connection.execute("SELECT DISTINCT trace_id FROM trace_steps").fetchall() == [
            ("fixture-alpha-trace",),
        ]
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE name = 'unrelated_raw'",
        ).fetchall() == []
        for table, count in (("corpus_tasks", 1), ("task_issues", 1), ("agent_traces", 1), ("trace_steps", 3)):
            index = table + "_fts"
            assert connection.execute(f"SELECT COUNT(*) FROM {index}").fetchone() == (count,)
            assert connection.execute(
                f"SELECT COUNT(*) FROM {index} WHERE {index} MATCH ?", ("allowedalpha",),
            ).fetchone() == (count,)
            for marker in ("heldpayload", "testpayload", "unrequestedpayload", deleted_marker, raw_marker):
                assert connection.execute(
                    f"SELECT rowid FROM {index} WHERE {index} MATCH ?", (marker,),
                ).fetchall() == []
    output = destination.read_bytes()
    for marker in ("heldpayload", "testpayload", "unrequestedpayload", deleted_marker, raw_marker):
        assert marker.encode() not in output
    assert source.read_bytes() == before


@pytest.mark.parametrize("requested", ["fixture-held", "fixture-test", "fixture-missing", "Fixture-alpha", " fixture-alpha "])
def test_missing_and_non_dev_tasks_are_refused_before_content_copy(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requested: str,
) -> None:
    destination = tmp_path / "actor.sqlite"
    before = source.read_bytes()
    body_reads = _guard_source_bodies(monkeypatch)

    with pytest.raises(ValueError, match="does not exist|not DEV"):
        project_actor_database(source, destination, task_ids=["fixture-alpha", requested])

    assert body_reads == []
    _assert_no_output(destination)
    assert source.read_bytes() == before


@pytest.mark.parametrize("task_ids", [[], (), None, "fixture-alpha", [""], ["\x00"], [7]])
def test_invalid_or_empty_admission_has_no_fallback(source: Path, tmp_path: Path, task_ids: Any) -> None:
    destination = tmp_path / "actor.sqlite"
    with pytest.raises((TypeError, ValueError)):
        project_actor_database(source, destination, task_ids=task_ids)
    _assert_no_output(destination)


def test_missing_admission_argument_has_no_fallback(source: Path, tmp_path: Path) -> None:
    destination = tmp_path / "actor.sqlite"
    with pytest.raises(TypeError, match="task_ids"):
        project_actor_database(source, destination)
    _assert_no_output(destination)


def test_literal_task_id_is_bound_without_normalization(source: Path, tmp_path: Path) -> None:
    destination = tmp_path / "actor.sqlite"
    task_id = "fixture's-exact-id"
    with CloudTraceDatabase(source) as database:
        _seed_task(database, task_id, "dev", "literalpayload")
    receipt = project_actor_database(source, destination, task_ids=[task_id])
    assert receipt["admitted_task_ids"] == [task_id]
    assert _rows(destination, "corpus_tasks", (task_id,)) == _rows(source, "corpus_tasks", (task_id,))


@pytest.mark.parametrize("alias", ["existing-file", "source-itself", "source-hardlink"])
def test_existing_destination_and_source_aliases_are_not_modified(
    source: Path, tmp_path: Path, alias: str,
) -> None:
    destination = tmp_path / "actor.sqlite"
    if alias == "source-itself":
        destination = source
    elif alias == "source-hardlink":
        os.link(source, destination)
    else:
        destination.write_bytes(b"unrelated caller-owned output")
    source_before, destination_before = source.read_bytes(), destination.read_bytes()

    with pytest.raises(FileExistsError):
        project_actor_database(source, destination, task_ids=["fixture-alpha"])

    assert source.read_bytes() == source_before
    assert destination.read_bytes() == destination_before
    assert not list(tmp_path.glob(f".{destination.name}.projection-*"))


@pytest.mark.parametrize("kind", ["source", "destination", "dangling-destination"])
def test_source_and_destination_symlinks_are_refused(source: Path, tmp_path: Path, kind: str) -> None:
    destination = tmp_path / "actor.sqlite"
    input_path = source
    if kind == "source":
        input_path = tmp_path / "source-link.sqlite"
        input_path.symlink_to(source)
    else:
        destination.symlink_to(source if kind == "destination" else tmp_path / "missing.sqlite")
    before = source.read_bytes()

    with pytest.raises(ValueError, match="symlink"):
        project_actor_database(input_path, destination, task_ids=["fixture-alpha"])

    assert source.read_bytes() == before
    if kind == "source":
        _assert_no_output(destination)
        assert input_path.is_symlink()
    else:
        assert destination.is_symlink()


@pytest.mark.parametrize("step_count", [0, 4])
def test_inconsistent_admitted_step_count_has_no_output(source: Path, tmp_path: Path, step_count: int) -> None:
    destination = tmp_path / "actor.sqlite"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute(
            "UPDATE agent_traces SET step_count = ? WHERE task_id = ?", (step_count, "fixture-alpha"),
        )
    before = source.read_bytes()
    with pytest.raises(ValueError, match="step_count"):
        project_actor_database(source, destination, task_ids=["fixture-alpha"])
    _assert_no_output(destination)
    assert source.read_bytes() == before


def test_schema_drift_is_refused_instead_of_discarding_columns(source: Path, tmp_path: Path) -> None:
    destination = tmp_path / "actor.sqlite"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("ALTER TABLE corpus_tasks ADD COLUMN source_extension TEXT")
    with pytest.raises(ValueError, match="source columns differ"):
        project_actor_database(source, destination, task_ids=["fixture-alpha"])
    _assert_no_output(destination)


def test_failed_copy_rolls_back_and_leaves_no_published_output(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "actor.sqlite"
    before = source.read_bytes()
    connect = sqlite3.connect
    attempted: list[str] = []

    def authorize(action: int, table: str, column: str, database: str, trigger: str) -> int:
        if action == sqlite3.SQLITE_INSERT and database == "main" and table in _TABLE_KEYS:
            attempted.append(table)
            if table == "trace_steps":
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def failing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = connect(*args, **kwargs)
        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(actor_projection.sqlite3, "connect", failing_connect)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        project_actor_database(source, destination, task_ids=["fixture-alpha"])
    assert attempted == list(_TABLE_KEYS)
    _assert_no_output(destination)
    assert source.read_bytes() == before


def test_source_attachment_is_read_only(source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "actor.sqlite"
    before = source.read_bytes()
    copy_rows = actor_projection._copy_rows

    def check_read_only(connection: sqlite3.Connection, columns: dict[str, tuple[str, ...]]) -> None:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE source.corpus_tasks SET split = split WHERE task_id = 'fixture-alpha'")
        copy_rows(connection, columns)

    monkeypatch.setattr(actor_projection, "_copy_rows", check_read_only)
    project_actor_database(source, destination, task_ids=["fixture-alpha"])
    assert source.read_bytes() == before


def test_copy_uses_one_source_snapshot_during_concurrent_writes(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "actor.sqlite"
    admitted = ("fixture-alpha",)
    before = {table: _rows(source, table, admitted) for table in _TABLE_KEYS}
    expected = _identities(source, admitted)
    copy_rows = actor_projection._copy_rows
    with closing(sqlite3.connect(source)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)

        def concurrent_copy(connection: sqlite3.Connection, columns: dict[str, tuple[str, ...]]) -> None:
            assert not destination.exists()
            with writer:
                writer.execute(
                    "UPDATE corpus_tasks SET split = 'test', instruction_text = 'new snapshot' "
                    "WHERE task_id = 'fixture-alpha'",
                )
                writer.execute("DELETE FROM trace_steps WHERE trace_id = 'fixture-alpha-trace' AND step_index = 9")
                writer.execute("UPDATE agent_traces SET step_count = 2 WHERE task_id = 'fixture-alpha'")
            copy_rows(connection, columns)

        monkeypatch.setattr(actor_projection, "_copy_rows", concurrent_copy)
        project_actor_database(source, destination, task_ids=admitted, expected_identities=expected)
        assert writer.execute(
            "SELECT split FROM corpus_tasks WHERE task_id = 'fixture-alpha'",
        ).fetchone() == ("test",)
    for table in _TABLE_KEYS:
        assert _rows(destination, table, admitted) == before[table]


@pytest.mark.parametrize("field", ["dataset_id", "split", "original_grader_digest", "repaired_grader_digest", "item_id", "item_type"])
def test_changed_preflight_identity_is_refused_before_content_copy(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    destination = tmp_path / "actor.sqlite"
    expected = _identities(source, ("fixture-alpha",))
    with closing(sqlite3.connect(source)) as connection, connection:
        if field == "item_type":
            connection.execute(
                "UPDATE corpus_tasks SET metadata_json = json_set(metadata_json, '$.item_id', 1)"
                " WHERE task_id = ?", ("fixture-alpha",),
            )
        elif field == "item_id":
            connection.execute(
                "UPDATE corpus_tasks SET metadata_json = json_set(metadata_json, '$.item_id', ?)"
                " WHERE task_id = ?", ("changed-fixture", "fixture-alpha"),
            )
        else:
            connection.execute(
                f"UPDATE corpus_tasks SET {field} = ? WHERE task_id = ?",
                ("dev" if field == "split" else "changed-fixture", "fixture-alpha"),
            )
    body_reads = _guard_source_bodies(monkeypatch)

    with pytest.raises(ValueError, match="source identity changed"):
        project_actor_database(source, destination, task_ids=["fixture-alpha"], expected_identities=expected)

    assert body_reads == []
    _assert_no_output(destination)


@pytest.mark.parametrize("change", ["missing-task", "extra-task", "missing-field", "extra-field", "wrong-task-id"])
def test_expected_identity_requires_exact_coverage_and_all_fields(
    source: Path, tmp_path: Path, change: str,
) -> None:
    destination = tmp_path / "actor.sqlite"
    expected = _identities(source, ("fixture-alpha",))
    if change == "missing-task":
        expected.clear()
    elif change == "extra-task":
        expected["fixture-beta"] = _identities(source, ("fixture-beta",))["fixture-beta"]
    elif change == "missing-field":
        del expected["fixture-alpha"]["item_id"]
    elif change == "extra-field":
        expected["fixture-alpha"]["unexpected"] = "not an identity field"
    else:
        expected["fixture-alpha"]["task_id"] = "fixture-beta"
    with pytest.raises(ValueError, match="expected_identities"):
        project_actor_database(source, destination, task_ids=["fixture-alpha"], expected_identities=expected)
    _assert_no_output(destination)


def test_atomic_publication_does_not_clobber_a_racing_output(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "actor.sqlite"
    link: Callable[..., None] = os.link
    other_bytes = b"another writer owns this output"

    def racing_link(staged: Path, target: Path, **kwargs: Any) -> None:
        assert target == destination
        assert not target.exists()
        with closing(sqlite3.connect(staged)) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("SELECT COUNT(*) FROM trace_steps").fetchone() == (3,)
        with target.open("xb") as stream:
            stream.write(other_bytes)
        link(staged, target, **kwargs)

    monkeypatch.setattr(actor_projection.os, "link", racing_link)
    with pytest.raises(FileExistsError):
        project_actor_database(source, destination, task_ids=["fixture-alpha"])
    assert destination.read_bytes() == other_bytes
    assert not list(tmp_path.glob(".actor.sqlite.projection-*"))
