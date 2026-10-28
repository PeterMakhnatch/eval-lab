from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

from evallab.backups import backup_freshness, create_postgres_backup


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
