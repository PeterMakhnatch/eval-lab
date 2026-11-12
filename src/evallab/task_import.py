"""Restartable, failure-isolated import of Harbor task packages."""

from __future__ import annotations

import hashlib
import os
import shutil
import tomllib
from pathlib import Path

_IGNORED_NAMES = {".DS_Store", ".git", "__pycache__", ".pytest_cache"}








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
    if destination.is_dir():
        return
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.mkdir(parents=True)
    try:
        for source_file in _package_files(source):
            relative = source_file.relative_to(source)
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            # Snapshots must own their bytes: a hardlink would let later source
            # edits mutate the retained snapshot through the shared inode and
            # silently break the frozen digest pinned by prepared specs.
            # copy2 keeps content independent while preserving executable modes.
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


def import_task_package(source: Path, destination_root: Path) -> tuple[Path, str]:
    """Copy exactly one task package into *destination_root*.

    Returns the ``(destination, source_digest)`` pair using the existing
    content-addressed layout (``<task-name>-<digest[0:12]>``). The snapshot
    owns its bytes, so later source edits cannot mutate it. A destination
    that already holds identical bytes is reused; drifted bytes refuse with
    ``FileExistsError`` instead of overwriting.
    """
    root = Path(source).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"task source directory does not exist: {source}")
    if not (root / "task.toml").is_file():
        raise ValueError(f"task source has no task.toml: {root}")
    digest = package_digest(root)
    name = _task_name(root)
    base = Path(destination_root).resolve()
    base.mkdir(parents=True, exist_ok=True)
    destination = base / f"{name}-{digest[7:19]}"
    if destination.is_symlink():
        raise ValueError(f"import destination is a symlink, refusing overwrite: {destination}")
    _reclaim_stale_staging(destination)
    if destination.is_dir():
        if package_digest(destination) != digest:
            raise FileExistsError(
                f"existing snapshot {destination} no longer matches source digest "
                f"{digest}; remove the drifted directory or investigate before re-preparing"
            )
        return destination, digest
    if destination.exists():
        raise ValueError(f"import destination exists and is not a directory: {destination}")
    _copy_package(root, destination)
    if package_digest(destination) != digest:
        raise RuntimeError(f"snapshot verification failed for {destination}")
    return destination, digest


def discover_task_packages(source_root: Path) -> list[Path]:
    root = source_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"task source directory does not exist: {root}")
    return sorted({path.parent for path in root.rglob("task.toml")}, key=str)




