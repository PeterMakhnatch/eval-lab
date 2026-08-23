"""Restartable, failure-isolated import of Harbor task packages."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

IMPORTER_VERSION = "task-import/v1"
_IGNORED_NAMES = {".DS_Store", ".git", "__pycache__", ".pytest_cache"}


@dataclass(frozen=True)
class ImportItem:
    source: Path
    source_digest: str
    destination: Path | None
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class ImportReport:
    discovered: int
    imported: int
    skipped: int
    failed: int
    items: tuple[ImportItem, ...]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _package_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in _IGNORED_NAMES for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"task package contains unsupported symlink: {relative}")
        if path.is_file():
            files.append(path)
    return files


def package_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _package_files(root):
        relative = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _task_name(root: Path) -> str:
    task_file = root / "task.toml"
    if not task_file.is_file():
        raise ValueError("task.toml is missing")
    payload = tomllib.loads(task_file.read_text())
    task = payload.get("task")
    name = task.get("name") if isinstance(task, dict) else None
    raw = str(name or root.name).strip().lower()
    normalized = "-".join("".join(char if char.isalnum() else " " for char in raw).split())
    if not normalized:
        raise ValueError("task name is empty")
    return normalized


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS imports (
            source_path TEXT NOT NULL,
            source_digest TEXT NOT NULL,
            importer_version TEXT NOT NULL,
            destination_path TEXT,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source_path, source_digest, importer_version)
        )
        """
    )
    return connection


def _reclaim_stale_staging(destination: Path) -> None:
    prefix = f".{destination.name}.tmp-"
    with os.scandir(destination.parent) as entries:
        for entry in entries:
            if not entry.name.startswith(prefix):
                continue
            staging = Path(entry.path)
            try:
                if entry.is_symlink():
                    staging.unlink()
                elif entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(staging)
            except FileNotFoundError:
                continue


def _copy_package(source: Path, destination: Path) -> None:
    _reclaim_stale_staging(destination)
    if destination.is_dir():
        return
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.mkdir(parents=True)
    try:
        for source_file in _package_files(source):
            relative = source_file.relative_to(source)
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source_file, target)
            except OSError:
                shutil.copy2(source_file, target)
        try:
            temporary.replace(destination)
        except FileExistsError:
            if not destination.is_dir():
                raise
            shutil.rmtree(temporary)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def discover_task_packages(source_root: Path) -> list[Path]:
    root = source_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"task source directory does not exist: {root}")
    return sorted({path.parent for path in root.rglob("task.toml")}, key=str)


def import_task_batch(
    source_root: Path,
    destination_root: Path,
    ledger_path: Path,
    *,
    limit: int | None = None,
) -> ImportReport:
    """Import a local corpus; reruns resume by source digest and item status."""
    packages = discover_task_packages(source_root)
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        packages = packages[:limit]
    destination_root = destination_root.resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    connection = _connect(ledger_path.resolve())
    items: list[ImportItem] = []
    imported = skipped = failed = 0
    try:
        for source in packages:
            digest = f"sha256:{'0' * 64}"
            try:
                digest = package_digest(source)
                name = _task_name(source)
                destination = destination_root / f"{name}-{digest[7:19]}"
                source_key = str(source.resolve())
                existing = connection.execute(
                    """
                    SELECT status, destination_path FROM imports
                    WHERE source_path = ? AND source_digest = ? AND importer_version = ?
                    """,
                    (source_key, digest, IMPORTER_VERSION),
                ).fetchone()
                if (
                    existing is not None
                    and existing[0] == "imported"
                    and existing[1]
                    and Path(existing[1]).is_dir()
                ):
                    skipped += 1
                    items.append(ImportItem(source, digest, destination, "skipped"))
                    continue
                now = _utc_now()
                connection.execute(
                    """
                    INSERT INTO imports (
                        source_path, source_digest, importer_version,
                        destination_path, status, attempts, reason,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'importing', 1, NULL, ?, ?)
                    ON CONFLICT(source_path, source_digest, importer_version)
                    DO UPDATE SET
                        destination_path = excluded.destination_path,
                        status = 'importing',
                        attempts = imports.attempts + 1,
                        reason = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (source_key, digest, IMPORTER_VERSION, str(destination), now, now),
                )
                connection.commit()
                _copy_package(source, destination)
                connection.execute(
                    """
                    UPDATE imports SET status = 'imported', updated_at = ?
                    WHERE source_path = ? AND source_digest = ? AND importer_version = ?
                    """,
                    (_utc_now(), source_key, digest, IMPORTER_VERSION),
                )
                connection.commit()
                imported += 1
                items.append(ImportItem(source, digest, destination, "imported"))
            except Exception as exc:
                failed += 1
                reason = f"{type(exc).__name__}: {str(exc)[:500]}"
                source_key = str(source.resolve())
                now = _utc_now()
                connection.execute(
                    """
                    INSERT INTO imports (
                        source_path, source_digest, importer_version,
                        destination_path, status, attempts, reason,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, NULL, 'failed', 1, ?, ?, ?)
                    ON CONFLICT(source_path, source_digest, importer_version)
                    DO UPDATE SET status = 'failed', attempts = imports.attempts + 1,
                                  reason = excluded.reason, updated_at = excluded.updated_at
                    """,
                    (source_key, digest, IMPORTER_VERSION, reason, now, now),
                )
                connection.commit()
                items.append(ImportItem(source, digest, None, "failed", reason))
    finally:
        connection.close()
    return ImportReport(
        discovered=len(packages), imported=imported, skipped=skipped,
        failed=failed, items=tuple(items),
    )


def ledger_rows(ledger_path: Path) -> list[dict[str, object]]:
    connection = _connect(ledger_path.resolve())
    try:
        columns = [item[1] for item in connection.execute("PRAGMA table_info(imports)")]
        return [
            dict(zip(columns, row, strict=True))
            for row in connection.execute(
                "SELECT * FROM imports ORDER BY source_path, source_digest"
            ).fetchall()
        ]
    finally:
        connection.close()
