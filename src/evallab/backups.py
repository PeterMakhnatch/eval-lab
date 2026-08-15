from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import BinaryIO, TextIO

from evallab.paths import shared_checkout_root
from evallab.runner import subscription_environment

POSTGRES_BACKUP_TIMEOUT_SECONDS = 600
BackupRunner = Callable[[list[str], BinaryIO], subprocess.CompletedProcess[bytes]]


def _private_descriptor(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def _open_private_binary(path: Path) -> BinaryIO:
    return os.fdopen(_private_descriptor(path), "wb")


def _open_private_text(path: Path) -> TextIO:
    return os.fdopen(_private_descriptor(path), "w")


def _run_backup(command: list[str], output: BinaryIO) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.PIPE,
        timeout=POSTGRES_BACKUP_TIMEOUT_SECONDS,
        env=subscription_environment(),
    )


def create_postgres_backup(
    repo_root: Path,
    report_date: date,
    *,
    runner: BackupRunner = _run_backup,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Path:
    """Create an atomic custom-format dump using PostgreSQL inside Compose."""
    shared_root = shared_checkout_root(repo_root)
    compose_path = shared_root / "compose.yaml"
    if not compose_path.is_file():
        raise FileNotFoundError(f"Compose definition not found: {compose_path}")

    backup_dir = shared_root / "backups/postgres"
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"evallab-{report_date.isoformat()}.dump"
    manifest_path = destination.with_suffix(".dump.json")
    token = secrets.token_hex(8)
    temporary = backup_dir / f".{destination.name}.{token}.tmp"
    manifest_temporary = backup_dir / f".{manifest_path.name}.{token}.tmp"
    command = [
        "docker",
        "compose",
        "-f",
        str(compose_path),
        "exec",
        "-T",
        "postgres",
        "sh",
        "-c",
        'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom',
    ]

    try:
        with _open_private_binary(temporary) as output:
            completed = runner(command, output)
            output.flush()
            os.fsync(output.fileno())
        if completed.returncode != 0:
            detail = completed.stderr.decode(errors="replace").strip()[:500]
            raise RuntimeError(
                f"pg_dump exited {completed.returncode}" + (f": {detail}" if detail else "")
            )
        size = temporary.stat().st_size
        if size == 0:
            raise RuntimeError("pg_dump produced an empty backup")
        digest_builder = hashlib.sha256()
        with temporary.open("rb") as dump:
            while chunk := dump.read(1024 * 1024):
                digest_builder.update(chunk)
        manifest = {
            "schema_version": 1,
            "created_at": now().astimezone(UTC).isoformat(),
            "report_date": report_date.isoformat(),
            "dump": destination.name,
            "format": "postgres-custom",
            "size_bytes": size,
            "sha256": digest_builder.hexdigest(),
        }
        with _open_private_text(manifest_temporary) as manifest_output:
            manifest_output.write(json.dumps(manifest, indent=2) + "\n")
            manifest_output.flush()
            os.fsync(manifest_output.fileno())
        temporary.replace(destination)
        manifest_temporary.replace(manifest_path)
    finally:
        temporary.unlink(missing_ok=True)
        manifest_temporary.unlink(missing_ok=True)
    return destination
