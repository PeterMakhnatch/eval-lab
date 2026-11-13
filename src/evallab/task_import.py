"""Harbor task package loading and source identity."""

from __future__ import annotations

import hashlib
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


def discover_task_packages(source_root: Path) -> list[Path]:
    root = source_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"task source directory does not exist: {root}")
    return sorted({path.parent for path in root.rglob("task.toml")}, key=str)


