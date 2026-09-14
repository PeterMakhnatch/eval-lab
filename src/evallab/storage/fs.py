"""Filesystem durability and crash-safety primitives."""

from __future__ import annotations

import os
from pathlib import Path


def fsync_directory(directory: Path) -> None:
    """Fsync a directory so dirents created inside it survive a host crash."""
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def durable_mkdir(directory: Path) -> None:
    """Create ``directory``, fsyncing the dirent of every level this adds.

    ``mkdir`` leaves the new directory entries in their parents' dirty cache.
    A crash can therefore lose a whole subtree — including invocation journals
    that prove a possibly-paid call already happened.
    """
    created: list[Path] = []
    probe = directory
    while not probe.exists():
        created.append(probe)
        probe = probe.parent
    directory.mkdir(parents=True, exist_ok=True)
    for path in reversed(created):
        fsync_directory(path.parent)


def durable_replace(source: Path, destination: Path) -> None:
    """Fsync file bytes before atomically publishing the stable sidecar path."""
    with source.open("rb") as handle:
        os.fsync(handle.fileno())
    source.replace(destination)
    fsync_directory(destination.parent)


_fsync_directory = fsync_directory
_durable_mkdir = durable_mkdir
_durable_replace = durable_replace
