"""Pytest plugin: run the SQLite-fixture tests of the Run Atlas store against a real PostgreSQL.

Every ``CloudTraceDatabase("<tmp>/x.sqlite", ...)`` is redirected to a schema of the throwaway
cluster derived from the path, so reopening the same path (read-only or not) sees the same store.
``sqlite_fts`` is ignored. Tests that poke SQLite internals fail here by construction; the README
classifies them.

    PS5_PG_SOCKET=/private/tmp/ps5d/sock PYTHONPATH=<this directory> \
        pytest -p pg_atlas_plugin tests/test_run_atlas_store.py ...

Environment: PS5_PG_SOCKET (socket directory), PS5_PG_PORT (default 55432); the database ``atlas`` must exist.
"""

from __future__ import annotations

import hashlib
import os
from urllib.parse import quote

import psycopg
from psycopg import sql

from evallab.production import cloud_trace_db

_ORIGINAL = cloud_trace_db.CloudTraceDatabase.__init__
_SOCKET = os.environ["PS5_PG_SOCKET"]
_PORT = os.environ.get("PS5_PG_PORT", "55432")
_BASE = f"postgresql://postgres@/atlas?host={quote(_SOCKET, safe='')}&port={_PORT}"
_INITIALIZED: set[str] = set()


def _redirected(self, database=None, *, sqlite_fts=True, read_only=False):  # noqa: ANN001
    target = None if database is None else str(database)
    if target is None or target.startswith(("postgresql://", "postgres://")):
        return _ORIGINAL(self, database, sqlite_fts=sqlite_fts, read_only=read_only)
    path = target.removeprefix("sqlite:///")
    schema = "t_" + hashlib.sha1(path.encode()).hexdigest()[:24]
    if schema not in _INITIALIZED:
        with psycopg.connect(_BASE, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        _INITIALIZED.add(schema)
    url = _BASE + "&options=" + quote(f"-csearch_path={schema}", safe="")
    return _ORIGINAL(self, url, sqlite_fts=sqlite_fts, read_only=read_only)


cloud_trace_db.CloudTraceDatabase.__init__ = _redirected
