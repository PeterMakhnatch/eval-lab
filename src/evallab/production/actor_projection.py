"""Open cloud SQLite sources without creating sidecar files."""

from __future__ import annotations

import os
from pathlib import Path


def source_read_uri(source: Path) -> str:
    """Return a read URI that cannot create sidecar files beside the source.

    SQLite writes ``-shm``/``-wal`` bookkeeping for any read of a WAL-format
    database, which would mutate the source directory. A closed, checkpointed WAL
    database can be read as an immutable image with no side effects; a WAL database
    with a live journal cannot, so that case is refused rather than silently read
    without its committed tail.
    """
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        header = os.read(descriptor, 100)
    finally:
        os.close(descriptor)
    if len(header) < 20:
        raise ValueError("source database is too small to be a SQLite database")
    if header[18:20] == b"\x01\x01":
        return source.as_uri() + "?mode=ro"
    for suffix in ("-wal", "-shm"):
        if (source.parent / (source.name + suffix)).exists():
            raise ValueError(
                "source database has a live WAL; close and checkpoint it before projection "
                "so no sidecar files are written beside the source",
            )
    return source.as_uri() + "?mode=ro&immutable=1"
