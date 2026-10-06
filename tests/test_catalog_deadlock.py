from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg.errors import DeadlockDetected

from evallab import database


class Transaction:
    def __init__(self, catalog: Catalog, *, fail_at: str | None) -> None:
        self.catalog = catalog
        self.fail_at = fail_at
        self.staged: list[str] = []
        self.closed = False

    def __enter__(self) -> Transaction:
        return self

    def write(self, job_id: str) -> None:
        self.staged.append(job_id)
        if self.fail_at == "write" and len(self.staged) == 2:
            raise self.catalog.error

    def __exit__(self, exc_type: Any, *_args: Any) -> None:
        try:
            if exc_type is None:
                if self.fail_at == "commit":
                    raise self.catalog.error
                self.catalog.committed.extend(self.staged)
        finally:
            self.staged.clear()
            self.closed = True


class Catalog:
    def __init__(self, *, fail_at: str, persistent: bool = False) -> None:
        self.fail_at = fail_at
        self.persistent = persistent
        self.error: Exception = DeadlockDetected("transaction lost the deadlock race")
        self.committed: list[str] = []
        self.connections: list[Transaction] = []
        self.backoffs: list[float] = []

    def connect(self, _url: str) -> Transaction:
        assert all(connection.closed for connection in self.connections)
        assert len(self.connections) < database.CATALOG_DEADLOCK_MAX_ATTEMPTS
        should_fail = self.persistent or not self.connections
        connection = Transaction(self, fail_at=self.fail_at if should_fail else None)
        self.connections.append(connection)
        return connection

    def backoff(self, delay: float) -> None:
        assert self.connections[-1].closed
        assert not self.connections[-1].staged
        self.backoffs.append(delay)


def install_catalog(monkeypatch: pytest.MonkeyPatch, catalog: Catalog) -> None:
    monkeypatch.setattr(database.psycopg, "connect", catalog.connect)
    monkeypatch.setattr(database.time, "sleep", catalog.backoff)

    def write_job(connection: Transaction, job: SimpleNamespace, *, root: Path) -> None:
        connection.write(job.id)

    monkeypatch.setattr(database, "ingest_job", write_job)


def jobs():
    return (SimpleNamespace(id=job_id) for job_id in ("job-a", "job-b", "job-c"))


@pytest.mark.parametrize("fail_at", ["write", "commit"])
def test_one_shot_batch_replays_after_deadlock_without_partial_or_duplicate_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: str
) -> None:
    catalog = Catalog(fail_at=fail_at)
    install_catalog(monkeypatch, catalog)

    count = database.ingest("postgresql://isolated-test", jobs(), root=tmp_path)

    assert count == 3
    assert catalog.committed == ["job-a", "job-b", "job-c"]
    assert len(catalog.connections) == 2
    assert all(connection.closed for connection in catalog.connections)
    assert len(catalog.backoffs) == 1


def test_persistent_deadlock_preserves_last_error_and_commits_no_partial_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = Catalog(fail_at="write", persistent=True)
    install_catalog(monkeypatch, catalog)

    with pytest.raises(DeadlockDetected) as error:
        database.ingest("postgresql://isolated-test", jobs(), root=tmp_path)

    assert error.value is catalog.error
    assert catalog.committed == []
    assert len(catalog.connections) == database.CATALOG_DEADLOCK_MAX_ATTEMPTS
    assert len(catalog.backoffs) == len(catalog.connections) - 1
    assert all(connection.closed for connection in catalog.connections)


def test_permanent_error_is_not_retried_and_leaves_no_partial_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = Catalog(fail_at="write")
    catalog.error = ValueError("job association is invalid")
    install_catalog(monkeypatch, catalog)

    with pytest.raises(ValueError) as error:
        database.ingest("postgresql://isolated-test", jobs(), root=tmp_path)

    assert error.value is catalog.error
    assert catalog.committed == []
    assert len(catalog.connections) == 1
    assert catalog.connections[0].closed
    assert catalog.backoffs == []
