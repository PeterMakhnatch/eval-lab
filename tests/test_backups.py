from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from evallab import backups
from evallab.backups import backup_freshness, check_postgres_restore, create_postgres_backup


def _generation(root: Path, day: str, payload: bytes, *, created_at: str) -> Path:
    generation = root / "backups/postgres" / f"evallab-{day}"
    generation.mkdir(parents=True)
    dump = generation / "database.dump"
    dump.write_bytes(payload)
    (generation / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": created_at,
                "dump": "database.dump",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    )
    return dump


def test_backup_freshness_reports_newest_verified_dump_age(tmp_path: Path) -> None:
    _generation(tmp_path, "2026-09-19", b"older", created_at="2026-09-19T06:32:00+00:00")
    newest = _generation(tmp_path, "2026-09-20", b"newest", created_at="2026-09-20T09:38:00+00:00")
    corrupt = _generation(
        tmp_path, "2026-09-21", b"corrupt", created_at="2026-09-21T06:30:00+00:00"
    )
    corrupt.write_bytes(b"truncated")  # Checksum no longer matches its manifest.

    stale = backup_freshness(tmp_path, now=datetime(2026, 10, 27, 9, 38, tzinfo=UTC))
    fresh = backup_freshness(tmp_path, now=datetime(2026, 9, 21, 9, 38, tzinfo=UTC))

    assert stale == {
        "status": "stale",
        "newest_verified": str(newest),
        "created_at": "2026-09-20T09:38:00+00:00",
        "age_hours": 888.0,
        "max_age_hours": 36.0,
        "unverified_newer": [str(corrupt)],
    }
    assert fresh["status"] == "fresh"
    assert fresh["age_hours"] == 24.0


def test_backup_freshness_without_verified_dump_is_missing(tmp_path: Path) -> None:
    broken = _generation(tmp_path, "2026-09-20", b"dump", created_at="2026-09-20T09:38:00+00:00")
    (broken.parent / "manifest.json").write_text("{not json")

    fact = backup_freshness(tmp_path, now=datetime(2026, 10, 27, tzinfo=UTC))

    assert fact["status"] == "missing"
    assert fact["newest_verified"] is None
    assert fact["unverified_newer"] == [str(broken)]
    assert (
        backup_freshness(tmp_path / "absent", now=datetime(2026, 10, 27, tzinfo=UTC))["status"]
        == "missing"
    )


def test_postgres_backup_is_atomic_and_has_integrity_manifest(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("name: eval-lab\n")
    payload = b"PGDMP deterministic test backup"
    commands: list[list[str]] = []

    def runner(command, output):
        commands.append(command)
        output.write(payload)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    result = create_postgres_backup(
        tmp_path,
        date(2026, 8, 14),
        runner=runner,
        now=lambda: datetime(2026, 8, 15, 2, 30, tzinfo=UTC),
    )

    assert result.read_bytes() == payload
    assert stat.S_IMODE(result.stat().st_mode) == 0o600
    manifest_path = result.parent / "manifest.json"
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert commands == [
        [
            "docker",
            "compose",
            "-f",
            str(tmp_path / "compose.yaml"),
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom',
        ]
    ]
    manifest = json.loads(manifest_path.read_text())
    assert manifest == {
        "schema_version": 1,
        "created_at": "2026-08-15T02:30:00+00:00",
        "report_date": "2026-08-14",
        "dump": "database.dump",
        "format": "postgres-custom",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    assert list(result.parent.parent.glob("*.tmp")) == []


def test_postgres_backup_failure_leaves_no_partial_artifacts(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("name: eval-lab\n")

    def runner(command, output):
        output.write(b"partial")
        return subprocess.CompletedProcess(command, 1, b"", b"database unavailable")

    try:
        create_postgres_backup(tmp_path, date(2026, 8, 14), runner=runner)
    except RuntimeError as exc:
        assert "pg_dump exited 1" in str(exc)
    else:
        raise AssertionError("failed pg_dump was accepted")

    assert sorted(path.name for path in (tmp_path / "backups/postgres").iterdir()) == [
        ".backup.lock"
    ]


def test_concurrent_backups_leave_matching_dump_and_manifest(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("name: eval-lab\n")
    payloads = iter((b"first complete dump",))

    def backup(_index: int) -> Path:
        def runner(command, output):
            payload = next(payloads)
            output.write(payload)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        return create_postgres_backup(
            tmp_path,
            date(2026, 8, 14),
            runner=runner,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(backup, range(2)))

    assert paths[0] == paths[1]
    final_payload = paths[0].read_bytes()
    manifest = json.loads((paths[0].parent / "manifest.json").read_text())
    assert final_payload == b"first complete dump"
    assert manifest["size_bytes"] == len(final_payload)
    assert manifest["sha256"] == hashlib.sha256(final_payload).hexdigest()


def test_publication_failure_leaves_no_visible_or_partial_generation(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("name: eval-lab\n")

    def runner(command, output):
        output.write(b"complete but unpublished")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def fail_publish(_source: Path, _destination: Path) -> None:
        raise OSError("simulated rename failure")

    try:
        create_postgres_backup(
            tmp_path,
            date(2026, 8, 14),
            runner=runner,
            publisher=fail_publish,
        )
    except OSError as exc:
        assert "simulated rename failure" in str(exc)
    else:
        raise AssertionError("failed publication was accepted")

    backup_dir = tmp_path / "backups/postgres"
    assert sorted(path.name for path in backup_dir.iterdir()) == [".backup.lock"]


@pytest.mark.parametrize("age,expected", [(None, 1), (36, 0), (36.01, 1)])
def test_backup_health_fails_for_missing_or_expired_dump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    age: float | None,
    expected: int,
) -> None:
    from evallab.nightly_refresh import refresh_exit_code

    moment = datetime(2026, 11, 10, 12, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(backups, "datetime", Clock)
    if age is not None:
        _generation(
            tmp_path,
            "2026-11-10",
            b"backup",
            created_at=(moment - timedelta(hours=age)).isoformat(),
        )
    monkeypatch.setattr(sys, "argv", ["backups", "status", "--root", str(tmp_path)])
    assert backups.main() == expected
    assert json.loads(capsys.readouterr().out)["health"] == ("FAIL" if expected else "PASS")
    fact = backup_freshness(tmp_path, now=moment)
    assert refresh_exit_code({"status": "noop", "backup": {**fact, "attempt": None}}) == expected


@pytest.mark.parametrize("failure", [None, "mismatch", "restore"])
def test_restore_compares_main_tables_and_drops_only_its_database(
    tmp_path: Path,
    failure: str | None,
) -> None:
    dump = _generation(tmp_path, "2026-11-10", b"PGDMP", created_at="2026-11-10T00:00:00Z")
    counts = {table: index + 1 for index, table in enumerate(backups.MAIN_TABLES)}
    databases: dict[str, dict[str, int]] = {"evallab": counts.copy()}
    targets: list[str] = []

    def runner(command, **kwargs):
        script = command[9]
        args = command[11:]
        output = b""
        code = 0
        if "createdb" in script:
            target = args[0]
            assert target.startswith("evallab_restore_")
            assert target not in databases
            databases[target] = {}
            targets.append(target)
        elif "pg_restore" in script:
            assert kwargs["stdin"].read() == b"PGDMP"
            target = args[0]
            assert target == targets[0] and target != "evallab"
            databases[target] = counts.copy()
            if failure == "mismatch":
                databases[target]["trials"] -= 1
            code = int(failure == "restore")
        elif "dropdb" in script:
            assert args == [targets[0]]
            del databases[args[0]]
        else:
            if '"$POSTGRES_DB"' in script:
                assert args[0].startswith("BEGIN READ ONLY;")
                target = "evallab"
            else:
                target = args[0]
            output = json.dumps(databases[target]).encode()
        return subprocess.CompletedProcess(
            command, code, output, b"restore failed" if code else b""
        )

    if failure:
        with pytest.raises(RuntimeError):
            check_postgres_restore(tmp_path, dump, runner=runner)
    else:
        receipt = check_postgres_restore(tmp_path, dump, runner=runner)
        assert receipt["live"] == receipt["restored"] == counts
        assert receipt["dropped"] is True
        assert receipt["sha256"] == hashlib.sha256(b"PGDMP").hexdigest()
    assert databases == {"evallab": counts}


def test_restore_rejects_corrupt_dump_before_accessing_database(tmp_path: Path) -> None:
    dump = _generation(tmp_path, "2026-11-10", b"PGDMP", created_at="2026-11-10T00:00:00Z")
    dump.write_bytes(b"truncated")

    def runner(*args, **kwargs):
        pytest.fail("Corrupt backup reached the database")

    with pytest.raises(ValueError):
        check_postgres_restore(tmp_path, dump, runner=runner)


def test_sqlite_backup_includes_wal_and_restores_all_tables(tmp_path: Path) -> None:
    source = tmp_path / "journal.sqlite"
    with closing(sqlite3.connect(source)) as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, body TEXT)")
        live.execute("INSERT INTO events VALUES (1, 'committed in WAL')")
        live.execute("PRAGMA user_version=7")
        live.commit()
        wal = source.with_name(source.name + "-wal")
        before = (source.read_bytes(), wal.read_bytes())
        receipt = backups.create_sqlite_backup(tmp_path, source)
        assert (source.read_bytes(), wal.read_bytes()) == before
    assert receipt["live_snapshot"] == receipt["restored"] == {"events": 1}
    assert receipt["user_version"] == 7
    assert receipt["integrity"] == "ok"
    dump = Path(receipt["path"])
    with closing(sqlite3.connect(dump.as_uri() + "?mode=ro", uri=True)) as restored:
        assert restored.execute("SELECT body FROM events").fetchone() == ("committed in WAL",)
    assert hashlib.sha256(dump.read_bytes()).hexdigest() == receipt["sha256"]
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600
    assert json.loads((dump.parent / "manifest.json").read_text()) == receipt


def test_sqlite_backup_failure_does_not_publish_or_change_source(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "journal.sqlite"
    with closing(sqlite3.connect(source)) as live:
        live.execute("CREATE TABLE events (id INTEGER)")
        live.execute("INSERT INTO events VALUES (1)")
        live.commit()
    before = source.read_bytes()
    real_counts = backups._sqlite_counts
    calls = 0

    def mismatched_counts(connection):
        nonlocal calls
        calls += 1
        return real_counts(connection) if calls == 1 else {"events": 0}

    monkeypatch.setattr(backups, "_sqlite_counts", mismatched_counts)
    with pytest.raises(ValueError, match="counts differ"):
        backups.create_sqlite_backup(tmp_path, source)
    assert source.read_bytes() == before
    assert list((tmp_path / "backups/sqlite").glob("*/manifest.json")) == []
    assert list((tmp_path / "backups/sqlite").glob("*.tmp")) == []


def test_sqlite_missing_source_never_creates_a_database(tmp_path: Path) -> None:
    source = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        backups.create_sqlite_backup(tmp_path, source)
    assert not source.exists()
