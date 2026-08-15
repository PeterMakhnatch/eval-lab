from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import BinaryIO, TextIO

from evallab.paths import shared_checkout_root
from evallab.runner import subscription_environment

POSTGRES_BACKUP_TIMEOUT_SECONDS = 600
BackupRunner = Callable[[list[str], BinaryIO], subprocess.CompletedProcess[bytes]]
BackupPublisher = Callable[[Path, Path], None]
_BACKUP_THREAD_LOCK = threading.Lock()


def _private_descriptor(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def _open_private_binary(path: Path) -> BinaryIO:
    return os.fdopen(_private_descriptor(path), "wb")


def _open_private_text(path: Path) -> TextIO:
    return os.fdopen(_private_descriptor(path), "w")


@contextmanager
def _backup_lock(backup_dir: Path) -> Iterator[None]:
    lock_path = backup_dir / ".backup.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with _BACKUP_THREAD_LOCK, os.fdopen(descriptor, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


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


def _sha256(path: Path) -> str:
    digest_builder = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest_builder.update(chunk)
    return digest_builder.hexdigest()


def _verify_backup(dump_path: Path, manifest_path: Path) -> bool:
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        dump_path.is_file()
        and isinstance(manifest, dict)
        and manifest.get("dump") == dump_path.name
        and manifest.get("size_bytes") == dump_path.stat().st_size
        and manifest.get("sha256") == _sha256(dump_path)
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_postgres_backup(
    repo_root: Path,
    report_date: date,
    *,
    runner: BackupRunner = _run_backup,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    publisher: BackupPublisher = lambda source, destination: source.replace(destination),
) -> Path:
    """Publish one immutable dump/manifest generation with one atomic rename."""
    shared_root = shared_checkout_root(repo_root)
    compose_path = shared_root / "compose.yaml"
    if not compose_path.is_file():
        raise FileNotFoundError(f"Compose definition not found: {compose_path}")

    backup_dir = shared_root / "backups/postgres"
    backup_dir.mkdir(parents=True, exist_ok=True)
    generation = backup_dir / f"evallab-{report_date.isoformat()}"
    destination = generation / "database.dump"
    manifest_path = generation / "manifest.json"
    legacy_destination = backup_dir / f"evallab-{report_date.isoformat()}.dump"
    legacy_manifest = legacy_destination.with_suffix(".dump.json")
    token = secrets.token_hex(8)
    temporary_generation = backup_dir / f".{generation.name}.{token}.tmp"
    temporary = temporary_generation / destination.name
    manifest_temporary = temporary_generation / manifest_path.name
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

    with _backup_lock(backup_dir):
        if _verify_backup(destination, manifest_path):
            return destination
        if generation.exists():
            raise RuntimeError(f"incomplete backup generation exists: {generation}")
        if _verify_backup(legacy_destination, legacy_manifest):
            return legacy_destination
        if legacy_destination.exists() or legacy_manifest.exists():
            raise RuntimeError("incomplete legacy backup pair exists")
        temporary_generation.mkdir(mode=0o700)
        try:
            with _open_private_binary(temporary) as output:
                completed = runner(command, output)
                output.flush()
                os.fsync(output.fileno())
            if completed.returncode != 0:
                detail = completed.stderr.decode(errors="replace").strip()[:500]
                raise RuntimeError(
                    f"pg_dump exited {completed.returncode}"
                    + (f": {detail}" if detail else "")
                )
            size = temporary.stat().st_size
            if size == 0:
                raise RuntimeError("pg_dump produced an empty backup")
            manifest = {
                "schema_version": 1,
                "created_at": now().astimezone(UTC).isoformat(),
                "report_date": report_date.isoformat(),
                "dump": temporary.name,
                "format": "postgres-custom",
                "size_bytes": size,
                "sha256": _sha256(temporary),
            }
            with _open_private_text(manifest_temporary) as manifest_output:
                manifest_output.write(json.dumps(manifest, indent=2) + "\n")
                manifest_output.flush()
                os.fsync(manifest_output.fileno())
            _fsync_directory(temporary_generation)
            publisher(temporary_generation, generation)
            _fsync_directory(backup_dir)
        finally:
            if temporary_generation.exists():
                shutil.rmtree(temporary_generation)
    return destination
