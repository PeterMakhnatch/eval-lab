"""Read-only and WAL source URI contracts using inert temporary SQLite fixtures."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from evallab.production.actor_projection import source_read_uri


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "source.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE items (id TEXT PRIMARY KEY, text TEXT)")
        connection.execute("INSERT INTO items (id, text) VALUES ('alpha', 'initial')")
    return path


def test_source_read_uri_is_read_only(source: Path) -> None:
    before = source.read_bytes()
    with (
        closing(sqlite3.connect(source_read_uri(source), uri=True)) as reader,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        reader.execute("UPDATE items SET text = 'mutated' WHERE id = 'alpha'")
    assert source.read_bytes() == before


def test_live_wal_source_is_refused_before_writing_sidecars(source: Path) -> None:
    with closing(sqlite3.connect(source)) as writer, writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("UPDATE items SET text = 'live wal text'")
        assert (source.parent / (source.name + "-wal")).exists()
        sidecars = sorted(p.name for p in source.parent.glob(source.name + "-*"))

        with pytest.raises(ValueError, match="live WAL"):
            source_read_uri(source)

        assert sorted(p.name for p in source.parent.glob(source.name + "-*")) == sidecars


def test_closed_wal_source_is_read_without_creating_sidecars(source: Path) -> None:
    with closing(sqlite3.connect(source)) as writer, writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("UPDATE items SET text = 'closed wal text'")
    assert not (source.parent / (source.name + "-wal")).exists()
    before = source.read_bytes()
    before_files = sorted(p.name for p in source.parent.glob(source.name + "*"))

    with closing(sqlite3.connect(source_read_uri(source), uri=True)) as reader:
        assert reader.execute("SELECT id, text FROM items").fetchall() == [
            ("alpha", "closed wal text"),
        ]

    assert sorted(p.name for p in source.parent.glob(source.name + "*")) == before_files
    assert source.read_bytes() == before
