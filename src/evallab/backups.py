from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from evallab.runner import subscription_environment
from evallab.storage.paths import shared_checkout_root

POSTGRES_BACKUP_TIMEOUT_SECONDS = 600
# One nightly cadence plus slack, matching the daily report's 36-hour window.
BACKUP_MAX_AGE_HOURS = 36.0
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


def _created_at(manifest_path: Path, dump_path: Path) -> datetime:
    try:
        created = datetime.fromisoformat(json.loads(manifest_path.read_text())["created_at"])
    except (OSError, KeyError, TypeError, ValueError):
        return datetime.fromtimestamp(dump_path.stat().st_mtime, UTC)
    return created if created.tzinfo else created.replace(tzinfo=UTC)


def backup_freshness(repo_root: Path, *, now: datetime) -> dict[str, Any]:
    """Read-only fact: the newest dump whose manifest checksum verifies, and its age.

    Newer generations that fail verification are listed, never counted as fresh.
    """
    backup_dir = shared_checkout_root(repo_root) / "backups/postgres"
    candidates: list[tuple[str, Path, Path]] = []
    if backup_dir.is_dir():
        for entry in backup_dir.iterdir():
            if not entry.name.startswith("evallab-"):
                continue
            if entry.is_dir():
                candidates.append((entry.name, entry / "database.dump", entry / "manifest.json"))
            elif entry.suffix == ".dump":
                candidates.append((entry.stem, entry, entry.with_suffix(".dump.json")))
    unverified: list[str] = []
    # Names embed the ISO report date, so reverse name order is newest first.
    for _, dump_path, manifest_path in sorted(candidates, reverse=True):
        if not _verify_backup(dump_path, manifest_path):
            unverified.append(str(dump_path))
            continue
        created = _created_at(manifest_path, dump_path)
        age_hours = (now - created).total_seconds() / 3600
        return {
            "status": "fresh" if age_hours <= BACKUP_MAX_AGE_HOURS else "stale",
            "newest_verified": str(dump_path),
            "created_at": created.astimezone(UTC).isoformat(),
            "age_hours": round(age_hours, 1),
            "max_age_hours": BACKUP_MAX_AGE_HOURS,
            "unverified_newer": unverified,
        }
    return {
        "status": "missing",
        "newest_verified": None,
        "created_at": None,
        "age_hours": None,
        "max_age_hours": BACKUP_MAX_AGE_HOURS,
        "unverified_newer": unverified,
    }


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
                    f"pg_dump exited {completed.returncode}" + (f": {detail}" if detail else "")
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


MAIN_TABLES = (
    "experiments",
    "jobs",
    "trials",
    "rewards",
    "artifacts",
    "run_files",
    "trajectory_documents",
    "deterministic_trial_facts",
)


def check_postgres_restore(
    repo_root: Path,
    dump_path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, Any]:
    """Restore only into a new random database; compare counts; always drop it.

    Live queries are read-only. Concurrent catalog writes can cause a count
    mismatch; this check never changes the live catalog to make counts agree.
    """
    started = time.monotonic()
    manifest = (
        dump_path.with_suffix(".dump.json")
        if dump_path.suffix == ".dump" and dump_path.name != "database.dump"
        else dump_path.parent / "manifest.json"
    )
    if not _verify_backup(dump_path, manifest):
        raise ValueError(f"Backup integrity verification failed: {dump_path}")
    compose = shared_checkout_root(repo_root) / "compose.yaml"
    database = f"evallab_restore_{secrets.token_hex(12)}"
    prefix = ["docker", "compose", "-f", str(compose), "exec", "-T", "postgres", "sh", "-c"]

    def execute(script: str, *args: str, source: BinaryIO | None = None) -> bytes:
        result = runner(
            [*prefix, script, "restore-check", *args],
            stdin=source if source is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=POSTGRES_BACKUP_TIMEOUT_SECONDS,
            env=subscription_environment(),
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Restore check command exited {result.returncode}: "
                f"{result.stderr.decode(errors='replace').strip()[:500]}"
            )
        return result.stdout

    count_sql = (
        "SELECT json_build_object("
        + ", ".join(f"'{table}', (SELECT count(*) FROM public.{table})" for table in MAIN_TABLES)
        + ");"
    )
    psql = 'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER"'
    live = json.loads(
        execute(
            psql + ' -d "$POSTGRES_DB" -c "$1"',
            "BEGIN READ ONLY; " + count_sql + " COMMIT;",
        )
    )
    execute('exec createdb -U "$POSTGRES_USER" --template=template0 "$1"', database)
    try:
        with dump_path.open("rb") as source:
            execute(
                'exec pg_restore -U "$POSTGRES_USER" --exit-on-error '
                '--no-owner --no-privileges -d "$1"',
                database,
                source=source,
            )
        restored = json.loads(execute(psql + ' -d "$1" -c "$2"', database, count_sql))
        if live != restored:
            raise RuntimeError(f"Restore row counts differ: live={live}, restored={restored}")
    finally:
        execute('exec dropdb -U "$POSTGRES_USER" "$1"', database)
    return {
        "path": str(dump_path),
        "size_bytes": dump_path.stat().st_size,
        "sha256": _sha256(dump_path),
        "live": live,
        "restored": restored,
        "database": database,
        "dropped": True,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local PostgreSQL backup and isolated restore check"
    )
    parser.add_argument("action", choices=("status", "create", "restore"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--dump", type=Path)
    args = parser.parse_args()
    if args.action == "restore":
        if args.dump is None:
            parser.error("restore requires --dump")
        result = check_postgres_restore(args.root, args.dump)
    else:
        if args.action == "create":
            create_postgres_backup(args.root, datetime.now(UTC).date())
        result = backup_freshness(args.root, now=datetime.now(UTC))
        result["health"] = "PASS" if result["status"] == "fresh" else "FAIL"
        result["message"] = (
            f"Verified backup {result['status']}; age {result['age_hours']} h; "
            f"limit {result['max_age_hours']} h"
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return int(result.get("health") == "FAIL")


if __name__ == "__main__":
    raise SystemExit(main())
