from __future__ import annotations

import os
from pathlib import Path


def fsync_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def durable_mkdir(directory: Path) -> None:
    created: list[Path] = []
    probe = directory
    while not probe.exists():
        created.append(probe)
        probe = probe.parent
    directory.mkdir(parents=True, exist_ok=True)
    for path in reversed(created):
        fsync_directory(path.parent)


def durable_replace(source: Path, destination: Path) -> None:
    with source.open("rb") as handle:
        os.fsync(handle.fileno())
    source.replace(destination)
    fsync_directory(destination.parent)


