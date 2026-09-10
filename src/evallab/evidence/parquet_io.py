"""Atomic Parquet writes shared by deterministic evidence projections."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import uuid
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

# Staging roots are hidden dot-directories so partition discovery (which only
# classifies ``job_id=``/``compact``/two-level layouts) never sees them.
STAGING_PREFIX = ".staging-"

@cache
def _empty_parquet_bytes(schema: pa.Schema) -> bytes:
    """Serialize each schema's required empty table once per process."""
    sink = pa.BufferOutputStream()
    pq.write_table(
        pa.Table.from_pylist([], schema=schema),
        sink,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
    )
    return sink.getvalue().to_pybytes()


@cache
def empty_table_sha256(schema: pa.Schema) -> str:
    """Digest of the canonical empty table for *schema*.

    A pruned empty table keeps the digest a materialized zero-row file would
    have had, so projection digests stay comparable across the cutover.
    """
    return hashlib.sha256(_empty_parquet_bytes(schema)).hexdigest()


def write_table_atomic(
    path: Path,
    rows: Sequence[dict[str, Any]],
    schema: pa.Schema,
    *,
    keep_empty: bool = True,
) -> bool:
    """Write a complete Parquet table, preserving the existing atomic cutover.

    With ``keep_empty=False`` an empty table writes no file and removes any
    stale one, so re-projection converges instead of leaving a former
    non-empty table behind. Returns whether a file exists at *path* after the
    call.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows and not keep_empty:
        path.unlink(missing_ok=True)
        return False
    temporary = _unique_temporary(path)
    if rows:
        pq.write_table(
            pa.Table.from_pylist(rows, schema=schema),
            temporary,
            compression="zstd",
            use_dictionary=False,
            write_statistics=True,
        )
    else:
        temporary.write_bytes(_empty_parquet_bytes(schema))
    temporary.replace(path)
    return True


def _unique_temporary(path: Path) -> Path:
    """Return a process-unique temporary sibling for an atomic replace.

    The previous fixed ``.parquet.tmp`` name meant two processes projecting
    the same partition concurrently wrote through one file: interleaved bytes
    and lost updates. Unique names make the final ``replace`` a choice between
    two complete files instead of one torn file.
    """
    return path.with_name(f"{path.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")


def new_staging_root(output_root: Path) -> Path:
    """Create an inert staging root under *output_root* and return it.

    The name embeds pid plus randomness, so concurrent writers never share
    one. Staging roots are inert by construction: no discovery glob or SQL
    pattern in ``storage/paths.py`` matches ``.staging-*/`` contents.
    """
    tag = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    staging_root = output_root.resolve() / f"{STAGING_PREFIX}{tag}"
    staging_root.mkdir(parents=True, exist_ok=True)
    return staging_root


def publish_staged_job(derived_root: Path, staging_root: Path, job_id: str) -> Path:
    """Atomically publish one staged ``job_id=`` directory into the live tree.

    The staged job is complete before this call; each step below is a single
    atomic rename, so concurrent readers observe either the previous complete
    version or the new complete version, never a half-written partition. The
    displaced previous version (if any) stays inside the staging root, which
    the caller removes when done.
    """
    live = derived_root.resolve() / f"job_id={job_id}"
    staged = staging_root.resolve() / f"job_id={job_id}"
    previous = staging_root.resolve() / "prev-job"
    if live.exists() or live.is_symlink():
        os.replace(live, previous)
    os.replace(staged, live)
    return live


def purge_inert_staging(output_root: Path, *, older_than_hours: float = 24.0) -> tuple[Path, ...]:
    """Remove crash-leftover ``.staging-*`` roots older than the cutoff.

    Only dot-prefixed staging roots match, so live ``job_id=``/``compact``
    data can never be selected. Staging roots younger than the cutoff are
    left alone: they may belong to a live concurrent writer. Never called
    automatically; invoke explicitly (e.g. from nightly hygiene).
    """
    cutoff = time.time() - older_than_hours * 3600.0
    removed: list[Path] = []
    for candidate in sorted(output_root.resolve().glob(f"{STAGING_PREFIX}*")):
        if not candidate.is_dir() or candidate.stat().st_mtime >= cutoff:
            continue
        shutil.rmtree(candidate, ignore_errors=True)
        removed.append(candidate)
    return tuple(removed)
