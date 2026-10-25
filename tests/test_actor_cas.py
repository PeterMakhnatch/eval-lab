"""Inert synthetic CAS staging fixtures; never execute stored bodies or use cloud I/O."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from evallab.production import actor_cas
from evallab.production.actor_cas import (
    preflight_destination,
    publish_staged_directory,
    stage_projection_cas,
)
from evallab.production.cloud_trace_db import CloudTraceDatabase


@pytest.fixture
def root(tmp_path: Path) -> Path:
    # pytest's own trusted temporary root may use /var or /tmp aliases on macOS.
    return tmp_path.resolve()


def _object(source: Path, body: bytes) -> str:
    digest = hashlib.sha256(body).hexdigest()
    directory = source / "sha256"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / digest).write_bytes(body)
    return f"cas://sha256/{digest}"


def _object_path(source: Path, uri: str) -> Path:
    return source / "sha256" / uri.rsplit("/", 1)[1]


def _database(
    root: Path,
    *,
    task_uri: str | None = None,
    issue_uri: str | None = None,
    trace_uri: str | None = None,
    observation_uri: str | None = None,
    metadata_uri: str | None = None,
) -> Path:
    path = root / "projection.sqlite"
    with CloudTraceDatabase(path, sqlite_fts=False) as database:
        database.upsert_task({
            "task_id": "synthetic-task", "dataset_id": "synthetic-fixture", "split": "dev",
            "instruction_text": "Inert CAS staging fixture.", "cas_uri": task_uri,
            "metadata_json": {"unfollowed_uri": metadata_uri},
        })
        database.upsert_task_issue({
            "issue_id": "synthetic-issue", "task_id": "synthetic-task",
            "defect_class": "synthetic", "direction": "wrong_pass", "status": "provisional",
            "cas_uri": issue_uri,
        })
        database.upsert_trace({
            "trace_id": "synthetic-trace", "run_id": "synthetic-run",
            "task_id": "synthetic-task", "model_id": "synthetic-agent",
            "outcome": "synthetic", "raw_cas_uri": trace_uri,
            "verified_receipt_json": {"unfollowed_uri": metadata_uri},
        }, steps=[{
            "step_index": 0, "role": "tool", "content_text": "Inert observation.",
            "observation_cas_uri": observation_uri,
            "flags_json": {"unfollowed_uri": metadata_uri},
        }])
    return path


def _assert_unpublished(root: Path, destination: Path) -> None:
    assert not destination.exists()
    assert not destination.is_symlink()
    assert not list(root.glob(".actor-cas-*"))


def test_stages_only_direct_references_with_exact_bytes_and_deduplication(root: Path) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    unreferenced = _object(source, b"unreferenced synthetic bytes")
    task_body = b"synthetic binary package\x00\xff\n"
    issue_body = "synthetic issue café\n".encode()
    trace_body = json.dumps({"nested_cas_uri": unreferenced}).encode()
    observation_body = b"synthetic observation\r\n\x00"
    task_uri = _object(source, task_body)
    issue_uri = _object(source, issue_body)
    trace_uri = _object(source, trace_body)
    observation_uri = _object(source, observation_body)
    database = _database(
        root, task_uri=task_uri, issue_uri=issue_uri, trace_uri=trace_uri,
        observation_uri=observation_uri, metadata_uri=unreferenced,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO trace_steps (trace_id, step_index, role, observation_cas_uri, flags_json) "
            "VALUES (?, ?, ?, ?, ?)",
            ("synthetic-trace", 1, "tool", trace_uri, "{}"),
        )
    expected = {
        task_uri: task_body, issue_uri: issue_body,
        trace_uri: trace_body, observation_uri: observation_body,
    }
    database_before = database.read_bytes()
    source_before = {path.name: path.read_bytes() for path in (source / "sha256").iterdir()}
    # Even an extra approval must not expand the direct-reference set.
    approved = (uri for uri in [*expected, task_uri, unreferenced])
    manifest = stage_projection_cas(
        database, source, destination, approved_uris=approved,
        max_bytes=sum(map(len, expected.values())),
    )
    assert manifest == {
        "schema": "evallab.actor_cas_stage/v1", "object_count": 4,
        "total_bytes": sum(map(len, expected.values())),
        "objects": [
            {"uri": uri, "sha256": uri.rsplit("/", 1)[1], "bytes": len(expected[uri]),
             "relative_path": "sha256/" + uri.rsplit("/", 1)[1]}
            for uri in sorted(expected)
        ],
    }
    assert {path.name for path in (destination / "sha256").iterdir()} == {
        uri.rsplit("/", 1)[1] for uri in expected
    }
    for uri, body in expected.items():
        copied = _object_path(destination, uri)
        assert copied.read_bytes() == body
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600
        assert copied.stat().st_ino != _object_path(source, uri).stat().st_ino
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "sha256").stat().st_mode) == 0o700
    assert not _object_path(destination, unreferenced).exists()
    assert database.read_bytes() == database_before
    assert {path.name: path.read_bytes() for path in (source / "sha256").iterdir()} == source_before
    assert not list(root.glob(".actor-cas-*"))


@pytest.mark.parametrize("column", ["task_uri", "issue_uri", "trace_uri", "observation_uri"])
def test_requires_explicit_approval_for_every_reference(root: Path, column: str) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    uri = _object(source, b"explicit approval required")
    database = _database(root, **{column: uri})
    with pytest.raises(ValueError, match="explicit operator approval"):
        stage_projection_cas(database, source, destination, approved_uris=[], max_bytes=1000)
    _assert_unpublished(root, destination)


def test_does_not_require_approval_for_metadata_or_nested_blob_references(root: Path) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    missing_uri = "cas://sha256/" + "a" * 64
    body = json.dumps({"bundle": {"cas_uri": missing_uri}}).encode()
    uri = _object(source, body)
    database = _database(root, task_uri=uri, metadata_uri=missing_uri)
    manifest = stage_projection_cas(
        database, source, destination, approved_uris=[uri], max_bytes=len(body),
    )
    assert manifest["object_count"] == 1
    assert _object_path(destination, uri).read_bytes() == body
    assert not _object_path(destination, missing_uri).exists()


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_rejects_missing_or_corrupt_objects(root: Path, failure: str) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    uri = _object(source, b"original synthetic bytes")
    database = _database(root, task_uri=uri)
    if failure == "missing":
        _object_path(source, uri).unlink()
        error: type[Exception] = FileNotFoundError
    else:
        _object_path(source, uri).write_bytes(b"replaced synthetic data!")
        error = ValueError
    with pytest.raises(error):
        stage_projection_cas(database, source, destination, approved_uris=[uri], max_bytes=1000)
    _assert_unpublished(root, destination)


@pytest.mark.parametrize("cap", [-1, True, False, 1.5, "10", None])
def test_rejects_invalid_max_bytes(root: Path, cap: Any) -> None:
    destination = root / "staged-cas"
    with pytest.raises(ValueError, match="max_bytes"):
        stage_projection_cas(
            root / "absent.sqlite", root / "absent-cas", destination,
            approved_uris=[], max_bytes=cap,
        )
    _assert_unpublished(root, destination)


def test_byte_cap_accepts_exact_total_and_rejects_one_byte_short(root: Path) -> None:
    source = root / "source-cas"
    first = _object(source, b"first object")
    second = _object(source, b"second object")
    empty = _object(source, b"")
    database = _database(root, task_uri=first, issue_uri=second, trace_uri=first, observation_uri=empty)
    total = len(b"first object") + len(b"second object")
    rejected = root / "too-small"
    with pytest.raises(ValueError, match="max_bytes"):
        stage_projection_cas(
            database, source, rejected, approved_uris=[first, second, empty], max_bytes=total - 1,
        )
    _assert_unpublished(root, rejected)
    manifest = stage_projection_cas(
        database, source, root / "exact", approved_uris=[first, second, empty], max_bytes=total,
    )
    assert manifest["total_bytes"] == total
    assert manifest["object_count"] == 3
    assert _object_path(root / "exact", empty).read_bytes() == b""


def test_streams_objects_larger_than_copy_buffer(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    body = bytes(range(256)) * 5
    uri = _object(source, body)
    database = _database(root, task_uri=uri)
    original_read = os.read
    requested_sizes: list[int] = []

    def bounded_read(descriptor: int, size: int) -> bytes:
        requested_sizes.append(size)
        assert 0 < size <= 17
        return original_read(descriptor, size)

    monkeypatch.setattr(actor_cas, "_COPY_BUFFER_BYTES", 17)
    monkeypatch.setattr(actor_cas.os, "read", bounded_read)
    manifest = stage_projection_cas(
        database, source, destination, approved_uris=[uri], max_bytes=len(body),
    )
    assert len(requested_sizes) > 2
    assert manifest["total_bytes"] == len(body)
    assert _object_path(destination, uri).read_bytes() == body


def test_empty_projection_allows_zero_cap(root: Path) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    source.mkdir()
    database = _database(root, metadata_uri="cas://sha256/" + "a" * 64)
    manifest = stage_projection_cas(
        database, source, destination, approved_uris=[], max_bytes=0,
    )
    assert manifest == {
        "schema": "evallab.actor_cas_stage/v1", "object_count": 0,
        "total_bytes": 0, "objects": [],
    }
    assert list((destination / "sha256").iterdir()) == []


@pytest.mark.parametrize("uri", [
    "", "cas://sha256/" + "A" * 64, "cas://sha256/" + "a" * 63,
    "cas://sha256/" + "a" * 64 + "/", "cas://sha256/" + "a" * 64 + "\n",
    "cas://sha256/../outside", "file:///synthetic", b"cas://sha256/" + b"a" * 64,
])
def test_rejects_invalid_reference_uris(root: Path, uri: Any) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    source.mkdir()
    database = _database(root)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE corpus_tasks SET cas_uri = ?", (uri,))
    with pytest.raises(ValueError, match="CAS URI"):
        stage_projection_cas(database, source, destination, approved_uris=[], max_bytes=100)
    _assert_unpublished(root, destination)


@pytest.mark.parametrize("approvals", [["cas://sha256/" + "A" * 64], [None], "cas://sha256/" + "a" * 64])
def test_rejects_invalid_approved_uris(root: Path, approvals: Any) -> None:
    destination = root / "staged-cas"
    with pytest.raises(ValueError, match="CAS URI|approved_uris"):
        stage_projection_cas(
            root / "absent.sqlite", root / "absent-cas", destination,
            approved_uris=approvals, max_bytes=0,
        )
    _assert_unpublished(root, destination)


@pytest.mark.parametrize("location", [
    "source-root", "source-parent", "hash-directory", "object", "database",
    "database-parent", "destination-parent", "destination-root",
])
def test_rejects_symlinked_paths(root: Path, location: str) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    uri = _object(source, b"symlink fixture")
    database = _database(root, task_uri=uri)
    alias = root / "alias"
    if location == "source-root":
        alias.symlink_to(source, target_is_directory=True)
        source = alias
    elif location == "source-parent":
        alias.symlink_to(root, target_is_directory=True)
        source = alias / source.name
    elif location == "hash-directory":
        (source / "sha256").rename(alias)
        (source / "sha256").symlink_to(alias, target_is_directory=True)
    elif location == "object":
        _object_path(source, uri).rename(alias)
        _object_path(source, uri).symlink_to(alias)
    elif location == "database":
        alias.symlink_to(database)
        database = alias
    elif location == "database-parent":
        alias.symlink_to(root, target_is_directory=True)
        database = alias / database.name
    elif location == "destination-parent":
        alias.symlink_to(root, target_is_directory=True)
        destination = alias / destination.name
    else:
        destination.symlink_to(root / "absent-target", target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        stage_projection_cas(database, source, destination, approved_uris=[uri], max_bytes=100)
    assert not list(root.glob(".actor-cas-*"))
    if location == "destination-root":
        assert destination.is_symlink()
        assert not (root / "absent-target").exists()
    else:
        assert not (root / "staged-cas").exists()


@pytest.mark.parametrize("location", ["source", "database", "destination"])
def test_rejects_parent_traversal_aliases(root: Path, location: str) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    uri = _object(source, b"path alias fixture")
    database = _database(root, task_uri=uri)
    if location == "source":
        source = root / "unused" / ".." / source.name
    elif location == "database":
        database = root / "unused" / ".." / database.name
    else:
        destination = root / "unused" / ".." / destination.name
    with pytest.raises(ValueError, match="aliases"):
        stage_projection_cas(database, source, destination, approved_uris=[uri], max_bytes=100)
    _assert_unpublished(root, root / "staged-cas")


def test_rejects_source_destination_overlap(root: Path) -> None:
    source = root / "source-cas"
    uri = _object(source, b"separate physical destination")
    database = _database(root, task_uri=uri)
    with pytest.raises(ValueError, match="overlaps"):
        stage_projection_cas(
            database, source, source / "nested-output", approved_uris=[uri], max_bytes=100,
        )
    assert not (source / "nested-output").exists()
    assert not list(source.glob(".actor-cas-*"))


def test_rejects_hardlinked_source_objects(root: Path) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    uri = _object(source, b"hardlink alias fixture")
    database = _database(root, task_uri=uri)
    os.link(_object_path(source, uri), root / "other-name")
    with pytest.raises(ValueError, match="aliases"):
        stage_projection_cas(database, source, destination, approved_uris=[uri], max_bytes=100)
    _assert_unpublished(root, destination)
    assert (root / "other-name").read_bytes() == b"hardlink alias fixture"


@pytest.mark.parametrize("existing", ["empty-directory", "nonempty-directory", "file"])
def test_refuses_existing_destination_without_overwriting(root: Path, existing: str) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    uri = _object(source, b"new bytes")
    database = _database(root, task_uri=uri)
    if existing == "file":
        destination.write_bytes(b"keep existing file")
    else:
        destination.mkdir()
        if existing == "nonempty-directory":
            (destination / "keep").write_bytes(b"keep existing bytes")
    before = destination.stat()
    with pytest.raises(FileExistsError):
        stage_projection_cas(database, source, destination, approved_uris=[uri], max_bytes=100)
    assert destination.stat() == before
    if existing == "file":
        assert destination.read_bytes() == b"keep existing file"
    elif existing == "nonempty-directory":
        assert (destination / "keep").read_bytes() == b"keep existing bytes"
    else:
        assert list(destination.iterdir()) == []
    assert not list(root.glob(".actor-cas-*"))


def test_no_partial_output_after_late_integrity_failure(root: Path) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    uris = sorted([_object(source, b"first synthetic body"), _object(source, b"second synthetic body")])
    database = _database(root, task_uri=uris[0], issue_uri=uris[1])
    later = _object_path(source, uris[1])
    corrupt = b"!" * later.stat().st_size
    later.write_bytes(corrupt)
    with pytest.raises(ValueError, match="integrity mismatch"):
        stage_projection_cas(database, source, destination, approved_uris=uris, max_bytes=100)
    _assert_unpublished(root, destination)
    assert later.read_bytes() == corrupt
    assert _object_path(source, uris[0]).exists()


def test_rejects_source_growth_during_transfer(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    body = b"inert growing object" * 8
    uri = _object(source, body)
    database = _database(root, task_uri=uri)
    original_read = os.read
    changed = False

    def grow_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = original_read(descriptor, size)
        if not changed:
            changed = True
            with _object_path(source, uri).open("ab") as stream:
                stream.write(b"!")
        return chunk

    monkeypatch.setattr(actor_cas, "_COPY_BUFFER_BYTES", 17)
    monkeypatch.setattr(actor_cas.os, "read", grow_after_first_read)
    with pytest.raises(ValueError, match="exceeds declared size or max_bytes"):
        stage_projection_cas(
            database, source, destination, approved_uris=[uri], max_bytes=len(body),
        )
    assert changed
    _assert_unpublished(root, destination)


def test_publication_race_never_clobbers_competing_empty_directory(
    root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    uri = _object(source, b"publication race fixture")
    database = _database(root, task_uri=uri)
    original_publish = actor_cas._publish_no_replace
    competing_inode: list[int] = []

    def publish_after_competitor(parent: int, staging_name: str, destination_name: str) -> None:
        destination.mkdir()
        competing_inode.append(destination.stat().st_ino)
        original_publish(parent, staging_name, destination_name)

    monkeypatch.setattr(actor_cas, "_publish_no_replace", publish_after_competitor)
    with pytest.raises(FileExistsError):
        stage_projection_cas(database, source, destination, approved_uris=[uri], max_bytes=100)
    assert destination.stat().st_ino == competing_inode[0]
    assert list(destination.iterdir()) == []
    assert not list(root.glob(".actor-cas-*"))


def test_parent_rename_cannot_publish_an_unrelated_tree(
    root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, parent = root / "source-cas", root / "output-parent"
    parent.mkdir()
    moved_parent = root / "moved-output-parent"
    body = b"approved inert object"
    uri = _object(source, body)
    database = _database(root, task_uri=uri)
    original_copy = actor_cas._copy_object
    unrelated: list[Path] = []

    def copy_then_move_parent(*args: Any, **kwargs: Any) -> int:
        copied = original_copy(*args, **kwargs)
        staging_name = next(parent.glob(".actor-cas-*")).name
        parent.rename(moved_parent)
        parent.mkdir()
        replacement = parent / staging_name
        replacement.mkdir()
        (replacement / "unrelated").write_bytes(b"retain unrelated bytes")
        unrelated.append(replacement)
        return copied

    monkeypatch.setattr(actor_cas, "_copy_object", copy_then_move_parent)
    receipt = stage_projection_cas(
        database, source, parent / "cas", approved_uris=[uri], max_bytes=len(body),
    )
    assert receipt["total_bytes"] == len(body)
    assert _object_path(moved_parent / "cas", uri).read_bytes() == body
    assert not (parent / "cas").exists()
    assert (unrelated[0] / "unrelated").read_bytes() == b"retain unrelated bytes"
    assert not list(moved_parent.glob(".actor-cas-*"))


def test_reads_checkpointed_projection_without_mutation_or_sidecars(root: Path) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    uri = _object(source, b"readonly database fixture")
    database = _database(root, task_uri=uri)
    database.chmod(0o400)
    before = database.stat()
    before_digest = hashlib.sha256(database.read_bytes()).hexdigest()
    before_files = set(root.glob("projection.sqlite*"))

    stage_projection_cas(database, source, destination, approved_uris=[uri], max_bytes=100)

    assert set(root.glob("projection.sqlite*")) == before_files
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_digest
    assert database.stat().st_mtime_ns == before.st_mtime_ns
    assert database.stat().st_size == before.st_size
    assert stat.S_IMODE(database.stat().st_mode) == 0o400


def test_refuses_wal_mode_projection_that_cannot_stand_alone(root: Path) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    uri = _object(source, b"inert WAL projection fixture")
    database = _database(root, task_uri=uri)
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)

    with pytest.raises(ValueError, match="rollback journal"):
        stage_projection_cas(database, source, destination, approved_uris=[uri], max_bytes=100)
    _assert_unpublished(root, destination)


def test_refuses_uncheckpointed_projection_instead_of_ignoring_live_wal(root: Path) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    uri = _object(source, b"inert live WAL fixture")
    database = _database(root, task_uri=uri)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE corpus_tasks SET instruction_text = 'inert live update'")
        connection.commit()
        assert (root / "projection.sqlite-wal").exists()
        with pytest.raises(ValueError, match="closed and checkpointed"):
            stage_projection_cas(
                database, source, destination, approved_uris=[uri], max_bytes=100,
            )
        _assert_unpublished(root, destination)


def test_discard_refuses_alias_entries_and_removes_only_owned_trees(root: Path) -> None:
    owned, foreign = root / "owned", root / "foreign"
    for directory in (owned, foreign):
        directory.mkdir()
        (directory / "payload").write_bytes(b"inert bytes")
    owned_status = owned.stat()
    alias = root / "owned-alias"
    alias.symlink_to(owned)

    assert actor_cas.discard_owned_directory(alias, owned_status) is False
    assert alias.is_symlink()
    assert (owned / "payload").exists()

    foreign_status = foreign.stat()
    substitute = root / "renamed-owned"
    owned.rename(substitute)
    foreign_empty = root / "owned"
    foreign_empty.mkdir()
    assert actor_cas.discard_owned_directory(foreign_empty, owned_status) is False
    assert foreign_empty.exists()
    assert actor_cas.discard_owned_directory(substitute, owned_status) is True
    assert not substitute.exists()
    assert actor_cas.discard_owned_directory(foreign, foreign_status) is True
    assert not foreign.exists()


def test_read_owned_file_refuses_symlinked_ancestry(root: Path) -> None:
    real = root / "real"
    real.mkdir()
    (real / "approvals.json").write_text("[]", encoding="utf-8")
    linked_parent = root / "linked"
    linked_parent.symlink_to(real)

    assert actor_cas.read_owned_file(real / "approvals.json") == b"[]"
    with pytest.raises(OSError):
        actor_cas.read_owned_file(linked_parent / "approvals.json")

    fifo = root / "approvals.fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular files"):
        actor_cas.read_owned_file(fifo)


def test_staging_renamed_midflight_never_leaves_a_partial_destination(
    root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    body = b"partial publication fixture"
    uri = _object(source, body)
    database = _database(root, task_uri=uri)
    original_copy = actor_cas._copy_object

    def copy_then_publish_partial(*args: Any, **kwargs: Any) -> int:
        copied = original_copy(*args, **kwargs)
        staging = next(root.glob(".actor-cas-*"))
        staging.rename(destination)
        return copied

    monkeypatch.setattr(actor_cas, "_copy_object", copy_then_publish_partial)
    with pytest.raises(OSError):
        stage_projection_cas(
            database, source, destination, approved_uris=[uri], max_bytes=len(body),
        )
    _assert_unpublished(root, destination)


def test_publication_refuses_a_staging_tree_that_was_swapped(root: Path) -> None:
    staging, foreign, destination = root / "private-staging", root / "foreign", root / "published"
    for directory in (staging, foreign):
        directory.mkdir(mode=0o700)
    (staging / "staged").write_bytes(b"validated bytes")
    (foreign / "unrelated").write_bytes(b"unrelated bytes")
    expected = actor_cas.directory_status(staging)
    staging.rename(root / "moved-staging")
    foreign.rename(staging)

    with pytest.raises(ValueError, match="no longer the one this run created"):
        publish_staged_directory(staging, destination, expected=expected)
    assert not destination.exists()
    assert (staging / "unrelated").read_bytes() == b"unrelated bytes"


def test_publication_of_a_substituted_tree_is_reported_not_accepted(
    root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging, substitute, destination = (
        root / "private-staging", root / "substituted", root / "published",
    )
    for directory in (staging, substitute):
        directory.mkdir(mode=0o700)
    (staging / "staged").write_bytes(b"validated bytes")
    (substitute / "unrelated").write_bytes(b"unrelated bytes")

    original_publish = actor_cas._publish_no_replace

    def publish_substitute(parent: int, staging_name: str, destination_name: str) -> None:
        original_publish(parent, substitute.name, destination_name)

    monkeypatch.setattr(actor_cas, "_publish_no_replace", publish_substitute)
    with pytest.raises(actor_cas.PublicationUncertainError, match="not the staged tree"):
        publish_staged_directory(staging, destination)

    assert (destination / "unrelated").read_bytes() == b"unrelated bytes"
    assert (staging / "staged").read_bytes() == b"validated bytes"


def test_preflight_refuses_destinations_inside_or_around_the_source(root: Path) -> None:
    source = root / "source-cas"
    (source / "sha256").mkdir(parents=True)
    (source / "sha256" / "inert").write_bytes(b"retain source bytes")
    before = sorted(path.relative_to(root) for path in root.rglob("*"))

    with pytest.raises(ValueError, match="overlaps the source CAS"):
        preflight_destination(source, source / "sha256" / "bundle")
    with pytest.raises(ValueError, match="overlaps the source CAS"):
        preflight_destination(source, root)
    preflight_destination(source, root / "bundle")
    assert sorted(path.relative_to(root) for path in root.rglob("*")) == before


def test_publish_staged_directory_moves_complete_sibling_tree(root: Path) -> None:
    staging, destination = root / "private-staging", root / "published"
    staging.mkdir(mode=0o700)
    (staging / "inert-data").write_bytes(b"complete staged bundle")
    publish_staged_directory(staging, destination)
    assert not staging.exists()
    assert (destination / "inert-data").read_bytes() == b"complete staged bundle"


@pytest.mark.parametrize("invalid", ["different-parent", "same-path", "symlink-staging", "symlink-parent"])
def test_publish_staged_directory_rejects_aliases(root: Path, invalid: str) -> None:
    staging, destination = root / "private-staging", root / "published"
    staging.mkdir(mode=0o700)
    (staging / "inert-data").write_bytes(b"retain staged bytes")
    if invalid == "different-parent":
        other = root / "other"
        other.mkdir()
        destination = other / destination.name
    elif invalid == "same-path":
        destination = staging
    elif invalid == "symlink-staging":
        alias = root / "alias"
        alias.symlink_to(staging, target_is_directory=True)
        staging = alias
    else:
        alias = root / "alias"
        alias.symlink_to(root, target_is_directory=True)
        staging, destination = alias / staging.name, alias / destination.name
    with pytest.raises((ValueError, OSError)):
        publish_staged_directory(staging, destination)
    assert (root / "private-staging" / "inert-data").read_bytes() == b"retain staged bytes"
    assert not (root / "published").exists()


def test_unsupported_atomic_publication_fails_closed(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, destination = root / "source-cas", root / "staged-cas"
    uri = _object(source, b"unsupported platform fixture")
    database = _database(root, task_uri=uri)
    monkeypatch.setattr(actor_cas.sys, "platform", "unsupported-fixture-platform")
    with pytest.raises(RuntimeError, match="unsupported"):
        stage_projection_cas(database, source, destination, approved_uris=[uri], max_bytes=100)
    _assert_unpublished(root, destination)
