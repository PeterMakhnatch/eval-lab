"""Small strict, deterministic file helpers for assembly-line artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any


def read_bytes(path: Path, *, single_link: bool = False) -> bytes:
    """Read one regular file through a single open descriptor, refusing a final symlink.

    The type and link count are taken from the open descriptor (not from an earlier path
    check), so a path swapped after validation cannot change what is hashed or parsed.
    ``single_link`` also refuses a file with other hard links (the line never writes any).
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"not a regular file: {path}")
        if single_link and info.st_nlink > 1:
            raise ValueError(f"file has other hard links: {path}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(fd)


def parse_json(data: bytes, label: object) -> dict[str, Any]:
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {label}")
    return value


def parse_rows(data: bytes, label: object) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"expected an object: {label}:{number}")
            rows.append(row)
    return rows


def read_json(path: Path) -> dict[str, Any]:
    return parse_json(read_bytes(path), path)


def read_rows(path: Path) -> list[dict[str, Any]]:
    return parse_rows(read_bytes(path), path)


def write_json(path: Path, value: Any) -> None:
    """Write atomically: a crash leaves the previous file or none, never a torn one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    scratch = path.with_name(f".{path.name}.tmp")
    scratch.write_text(text, encoding="utf-8")
    os.replace(scratch, path)


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def digest(path: Path) -> str:
    """Hash bytes, or a directory's sorted relative names and file hashes.

    This follows links, as it always has: it is what producers record and what other callers
    (Judge Station) hash. ``line check`` verifies recorded hashes through ``read_bytes`` instead.
    """
    if path.is_file():
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    if path.is_dir():
        return canonical_hash(
            [
                (p.relative_to(path).as_posix(), digest(p))
                for p in sorted(path.rglob("*"))
                if p.is_file()
            ]
        )
    raise FileNotFoundError(path)
