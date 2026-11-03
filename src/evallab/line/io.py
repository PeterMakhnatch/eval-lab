"""Small strict, deterministic file helpers for assembly-line artifacts."""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import stat
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from evallab.hack_drop import strict_json_loads

MAX_READ_BYTES = 512 * 1024 * 1024
"""The largest file the line reads whole. A bigger one is refused with a plain error."""

LABEL_KEYS = frozenset(
    {
        "label",
        "labels",
        "is_cheat",
        "is_exploit",
        "ground_truth",
        "expected_verdict",
        "attempted_cheat",
        "attempted_but_failed",
        "considered_only",
        "hack",
        "is_hack",
        "cheat",
        "cheated",
        "behavior",
        "verdict_enacted",
    }
)
"""Evaluator/label fields: they must never appear in anything the line reads or writes."""


class ReadLog:
    """What :func:`read_bytes` returned, by file identity.

    Two names for one file (a hard link, a case variant) share one entry, so a file that changed
    between two reads is seen however each read spelled it.
    """

    def __init__(self) -> None:
        self.hashes: dict[tuple[int, int], set[str]] = {}
        self.names: dict[tuple[int, int], set[str]] = {}

    def unstable(self) -> list[str]:
        return sorted(
            ", ".join(sorted(self.names[key]))
            for key, hashes in self.hashes.items()
            if len(hashes) > 1
        )


_READS: contextvars.ContextVar[ReadLog | None] = contextvars.ContextVar("line_reads", default=None)
_SEALED: contextvars.ContextVar[frozenset[tuple[int, int]] | None] = contextvars.ContextVar(
    "line_sealed", default=None
)


@contextlib.contextmanager
def record_reads() -> Iterator[ReadLog]:
    """Record the SHA-256 of every byte string :func:`read_bytes` returns, by file identity.

    A file that yields two different hashes inside one scope changed while it was being used.
    """
    log = ReadLog()
    token = _READS.set(log)
    try:
        yield log
    finally:
        _READS.reset(token)


@contextlib.contextmanager
def sealed_scope(repo_root: Path) -> Iterator[None]:
    """Refuse, inside this scope, any read of a file that is another name for sealed material.

    ``(device, inode)`` of every file under ``research/answer-keys`` is taken once; a declared
    input that shares one (a hard link under a DEV-looking name) is refused by
    :func:`read_bytes`. A nested scope reuses the outer one.
    """
    if _SEALED.get() is not None:
        yield
        return
    from evallab.rollout_admission import sealed_inodes

    token = _SEALED.set(sealed_inodes(repo_root / "research" / "answer-keys"))
    try:
        yield
    finally:
        _SEALED.reset(token)


def sealed_set() -> frozenset[tuple[int, int]]:
    """The sealed inodes of the enclosing :func:`sealed_scope` (empty outside one)."""
    return _SEALED.get() or frozenset()


def read_bytes(path: Path, *, single_link: bool = False) -> bytes:
    """Read one regular file through a single open descriptor, refusing a final symlink.

    The type, link count, identity and size are taken from the open descriptor (not from an
    earlier path check), so a path swapped after validation cannot change what is hashed or
    parsed. ``single_link`` also refuses a file with other hard links (the line never writes
    any). A file that is another name for sealed material, or larger than
    :data:`MAX_READ_BYTES`, is refused.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"not a regular file: {path}")
        if single_link and info.st_nlink > 1:
            raise ValueError(f"file has other hard links: {path}")
        if (info.st_dev, info.st_ino) in sealed_set():
            raise ValueError(f"file is a hard link to sealed material: {path}")
        if info.st_size > MAX_READ_BYTES:
            raise ValueError(f"file is larger than {MAX_READ_BYTES} bytes: {path}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_READ_BYTES + 1)
        if len(data) > MAX_READ_BYTES:
            raise ValueError(f"file is larger than {MAX_READ_BYTES} bytes: {path}")
    finally:
        os.close(fd)
    log = _READS.get()
    if log is not None:
        key = (info.st_dev, info.st_ino)
        log.hashes.setdefault(key, set()).add(hashlib.sha256(data).hexdigest())
        log.names.setdefault(key, set()).add(str(path))
    return data


def parse_json(data: bytes, label: object) -> dict[str, Any]:
    value = strict_json_loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {label}")
    return value


def parse_rows(data: bytes, label: object) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if line.strip():
            row = strict_json_loads(line)
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


def strict_digest(path: Path) -> str:
    """Hash a file, or a rollout tree, refusing links and special files anywhere in it.

    A symlink (to a file or a directory) or a FIFO/socket/device inside a tree is refused rather
    than followed, and every file is read through :func:`read_bytes`. Hard links are allowed:
    native Harbor trials legitimately share files. A directory hashes exactly as
    :func:`digest` does, so existing recorded hashes stay valid.
    """
    top = os.lstat(path).st_mode
    if stat.S_ISLNK(top):
        raise ValueError(f"refusing a symlink: {path}")
    if stat.S_ISREG(top):
        return hashlib.sha256(read_bytes(path)).hexdigest()
    if not stat.S_ISDIR(top):
        raise ValueError(f"refusing a special file: {path}")
    files: list[Path] = []
    for directory, directories, names in os.walk(path, followlinks=False):
        for name in [*directories, *names]:
            child = Path(directory) / name
            mode = os.lstat(child).st_mode
            if stat.S_ISLNK(mode):
                raise ValueError(f"refusing a symlink inside a rollout tree: {child}")
            if stat.S_ISREG(mode):
                files.append(child)
            elif not stat.S_ISDIR(mode):
                raise ValueError(f"refusing a special file inside a rollout tree: {child}")
    return canonical_hash(
        [
            (p.relative_to(path).as_posix(), hashlib.sha256(read_bytes(p)).hexdigest())
            for p in sorted(files)
        ]
    )


def file_keys(paths: Iterable[Path]) -> set[tuple[Any, ...]]:
    """Identities of files for set comparison: (device, inode) and the casefolded real path.

    A resolved path string alone can be fooled by hard links and by case variants on a
    case-insensitive volume; either identity matching is enough to call two names one file.
    """
    keys: set[tuple[Any, ...]] = set()
    for path in paths:
        keys.add(("path", str(path.resolve()).casefold()))
        with contextlib.suppress(OSError):
            info = path.stat()
            keys.add(("id", info.st_dev, info.st_ino))
    return keys
