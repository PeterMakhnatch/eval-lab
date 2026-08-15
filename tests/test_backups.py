from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

from evallab.backups import create_postgres_backup


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
    assert stat.S_IMODE(result.with_suffix(".dump.json").stat().st_mode) == 0o600
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
    manifest = json.loads(result.with_suffix(".dump.json").read_text())
    assert manifest == {
        "schema_version": 1,
        "created_at": "2026-08-15T02:30:00+00:00",
        "report_date": "2026-08-14",
        "dump": "evallab-2026-08-14.dump",
        "format": "postgres-custom",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    assert list(result.parent.glob("*.tmp")) == []


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

    assert [path.name for path in (tmp_path / "backups/postgres").iterdir()] == [
        ".backup.lock"
    ]


def test_concurrent_backups_leave_matching_dump_and_manifest(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("name: eval-lab\n")
    payloads = iter((b"first complete dump", b"second complete dump"))

    def backup(_index: int) -> Path:
        payload = next(payloads)

        def runner(command, output):
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
    manifest = json.loads(paths[0].with_suffix(".dump.json").read_text())
    assert final_payload in {b"first complete dump", b"second complete dump"}
    assert manifest["size_bytes"] == len(final_payload)
    assert manifest["sha256"] == hashlib.sha256(final_payload).hexdigest()
