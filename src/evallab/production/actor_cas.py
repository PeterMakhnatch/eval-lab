"""Stage only explicitly approved, direct CAS references from an actor projection.

The caller owns task admission and operator approval. This module neither grants
export rights nor follows references inside JSON, bundles, or object contents.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import sqlite3
import stat
import sys
from collections.abc import Collection, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

_SCHEMA = "evallab.actor_cas_stage/v1"
_URI = re.compile(r"cas://sha256/([0-9a-f]{64})\Z")
_COPY_BUFFER_BYTES = 1024 * 1024
_DIRECT_COLUMNS = (
    ("corpus_tasks", "cas_uri"),
    ("task_issues", "cas_uri"),
    ("agent_traces", "raw_cas_uri"),
    ("trace_steps", "observation_cas_uri"),
)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _path(value: Path) -> Path:
    path = Path(value)
    if ".." in path.parts or path.anchor == "//":
        raise ValueError("path aliases are not allowed")
    return path.absolute()


@contextmanager
def _directory(
    path: Path, *, forbidden: Collection[tuple[int, int]] = (),
) -> Iterator[int]:
    """Open every ancestor without following symlinks, retaining the final fd."""
    forbidden_identities = frozenset(forbidden)
    descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            status = os.fstat(descriptor)
            if (status.st_dev, status.st_ino) in forbidden_identities:
                raise ValueError("destination aliases or overlaps the source CAS directory")
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _source_identities(source_path: Path) -> Iterator[frozenset[tuple[int, int]]]:
    """Resolve the source root and its hash directory for physical-alias refusal."""
    with _directory(source_path) as source:
        status = os.fstat(source)
        identities = {(status.st_dev, status.st_ino)}
        try:
            hashes = os.open("sha256", _DIRECTORY_FLAGS, dir_fd=source)
        except FileNotFoundError:
            pass
        else:
            try:
                hash_status = os.fstat(hashes)
                identities.add((hash_status.st_dev, hash_status.st_ino))
            finally:
                os.close(hashes)
        yield frozenset(identities)


def preflight_destination(source_cas_directory: Path, destination_directory: Path) -> None:
    """Refuse a destination that aliases or contains the source CAS tree.

    Callers that create staging files before staging CAS objects call this first,
    so nothing is ever written under the source tree. Lexical containment is
    checked before any directory is opened; the inode check also rejects an alias
    that reaches the source root or its ``sha256/`` directory by another path.
    """
    source_path, destination_path = _path(source_cas_directory), _path(destination_directory)
    if (
        destination_path.is_relative_to(source_path)
        or source_path.is_relative_to(destination_path)
    ):
        raise ValueError("destination aliases or overlaps the source CAS directory")
    with (
        _source_identities(source_path) as forbidden,
        _directory(destination_path.parent, forbidden=forbidden),
    ):
        return


def read_owned_file(path: Path) -> bytes:
    """Read a file through descriptor-relative opens, refusing every alias.

    Each component is opened with ``O_NOFOLLOW`` beneath the previous directory
    descriptor, so a swapped ancestor cannot redirect the read. The bytes returned
    are checked against the same descriptor's identity and size afterwards.
    """
    target = _path(path)
    with _directory(target.parent) as parent:
        descriptor = os.open(target.name, _READ_FLAGS, dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            _regular(before)
            with open(descriptor, "rb", closefd=False) as stream:
                payload = stream.read()
            after = os.fstat(descriptor)
            if _identity(after) != _identity(before):
                raise ValueError("file changed while being read")
            if len(payload) != before.st_size:
                raise ValueError("file size does not match the bytes read")
            return payload
        finally:
            os.close(descriptor)


def _remove_tree_at(directory: int) -> None:
    """Delete a directory's contents through its descriptor, never by path."""
    for entry in os.listdir(directory):
        try:
            child = os.open(entry, _DIRECTORY_FLAGS, dir_fd=directory)
        except OSError:
            os.unlink(entry, dir_fd=directory)
        else:
            try:
                _remove_tree_at(child)
            finally:
                os.close(child)
            os.rmdir(entry, dir_fd=directory)


def directory_status(path: Path) -> os.stat_result:
    """Return a directory's status from an ``O_NOFOLLOW`` descriptor."""
    target = _path(path)
    with _directory(target.parent) as parent:
        descriptor = os.open(target.name, _DIRECTORY_FLAGS, dir_fd=parent)
        try:
            return os.fstat(descriptor)
        finally:
            os.close(descriptor)


def discard_owned_directory(directory: Path, expected: os.stat_result) -> bool:
    """Remove a tree only while the name still resolves to the recorded directory.

    The entry is opened with ``O_NOFOLLOW`` and compared by inode before anything
    is deleted, contents are removed through that descriptor, and the name is
    unlinked only if it still resolves to the same directory. A competitor's entry,
    including a symlink to the same inode, is left untouched.
    """
    target = _path(directory)
    with _directory(target.parent) as parent:
        try:
            descriptor = os.open(target.name, _DIRECTORY_FLAGS, dir_fd=parent)
        except (FileNotFoundError, OSError):
            return False
        try:
            status = os.fstat(descriptor)
            if (status.st_dev, status.st_ino) != (expected.st_dev, expected.st_ino):
                return False
            _remove_tree_at(descriptor)
        finally:
            os.close(descriptor)
        try:
            current = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return True
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            return False
        os.rmdir(target.name, dir_fd=parent)
        return True


def _regular(status: os.stat_result) -> None:
    if not stat.S_ISREG(status.st_mode):
        raise ValueError("CAS inputs must be regular files, not symlinks or special files")
    if status.st_nlink != 1:
        raise ValueError("hard-linked file aliases are not allowed")


def _identity(status: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns,
    )


def _digest(uri: Any) -> str:
    match = _URI.fullmatch(uri) if isinstance(uri, str) else None
    if match is None:
        raise ValueError("expected CAS URI cas://sha256/<64 lowercase hexadecimal characters>")
    return match[1]


def _require_checkpointed(parent: int, name: str) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            os.stat(name + suffix, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise ValueError("projected database must be closed and checkpointed, without journals")


def _direct_references(database: Path) -> list[str]:
    with _directory(database.parent) as parent:
        descriptor = os.open(database.name, _READ_FLAGS, dir_fd=parent)
        try:
            original = os.fstat(descriptor)
            _regular(original)
            _require_checkpointed(parent, database.name)
            if os.pread(descriptor, 2, 18) != b"\x01\x01":
                raise ValueError(
                    "projected database must use a rollback journal, not WAL"
                )

            def unchanged() -> bool:
                try:
                    named = os.stat(database.name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return False
                return _identity(os.fstat(descriptor)) == _identity(original) == _identity(named)

            # SQLite reads in place through /dev/fd/N of the checked descriptor:
            # unlike deserializing a copy, there is no 2 GiB allocation ceiling,
            # and immutable=1 takes no locks and creates no sidecars. macOS opens
            # the descriptor itself; Linux SQLite resolves it to the inode's
            # current name. Either way the descriptor and name must keep the
            # checked identity throughout, or the read fails closed.
            references: set[str] = set()
            try:
                connection = sqlite3.connect(
                    f"file:/dev/fd/{descriptor}?mode=ro&immutable=1", uri=True,
                )
                try:
                    connection.execute("PRAGMA query_only=ON")
                    connection.execute("PRAGMA trusted_schema=OFF")
                    for table, column in _DIRECT_COLUMNS:
                        table_type = connection.execute(
                            "SELECT type FROM sqlite_schema WHERE name = ?", (table,),
                        ).fetchone()
                        if table_type != ("table",):
                            raise ValueError(f"projected database requires core table {table}")
                        for (uri,) in connection.execute(
                            f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
                        ):
                            _digest(uri)
                            references.add(uri)
                finally:
                    connection.close()
            except sqlite3.Error as error:
                if not unchanged():
                    raise ValueError("projected database changed while reading") from error
                raise
            if not unchanged():
                raise ValueError("projected database changed while reading")
            _require_checkpointed(parent, database.name)
            return sorted(references)
        finally:
            os.close(descriptor)


def _publish_no_replace(parent: int, staging_name: str, destination_name: str) -> None:
    """Atomically rename a complete directory, refusing even an empty destination."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux":
        rename = getattr(library, "renameat2", None)
        flags = 1  # RENAME_NOREPLACE
    elif sys.platform == "darwin":
        rename = getattr(library, "renameatx_np", None)
        flags = 4  # RENAME_EXCL
    else:
        raise RuntimeError("atomic no-clobber CAS publication is unsupported on this platform")
    if rename is None:
        raise RuntimeError("atomic no-clobber CAS publication is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(staging_name), parent, os.fsencode(destination_name), flags):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination_name)


class PublicationUncertainError(RuntimeError):
    """The rename happened, but the published tree could not be confirmed.

    The destination may exist and be complete. Callers must not retry blindly into
    the same path, and must not treat this as "nothing was published".
    """


def _verify_published(parent: int, destination_name: str, staging: int) -> None:
    """Fail if the renamed entry is not the directory that was validated."""
    published = os.stat(destination_name, dir_fd=parent, follow_symlinks=False)
    staged = os.fstat(staging)
    if (published.st_dev, published.st_ino) != (staged.st_dev, staged.st_ino):
        raise ValueError("published directory is not the staged directory")


def publish_staged_directory(
    staging: Path, destination: Path, *, expected: os.stat_result | None = None,
) -> None:
    """Publish a complete sibling tree atomically, without replacing any entry.

    Both paths must have the same existing, symlink-free parent. The staging
    root must be a real directory. Its contents remain the caller's responsibility.
    The caller must exclusively own the staging tree and keep its ancestry stable.
    When ``expected`` is given, the staging entry must still be that directory
    before anything is renamed, so work written into a swapped tree is never
    published. Unsupported atomic no-clobber platforms fail closed.
    """
    staging_path, destination_path = _path(staging), _path(destination)
    if staging_path.parent != destination_path.parent or staging_path == destination_path:
        raise ValueError("staging and destination must be distinct siblings without aliases")
    with _directory(staging_path.parent) as parent:
        descriptor = os.open(staging_path.name, _DIRECTORY_FLAGS, dir_fd=parent)
        try:
            status = os.fstat(descriptor)
            if expected is not None and (status.st_dev, status.st_ino) != (
                expected.st_dev, expected.st_ino,
            ):
                raise ValueError("staging directory is no longer the one this run created")
            os.fsync(descriptor)
            _publish_no_replace(parent, staging_path.name, destination_path.name)
            try:
                _verify_published(parent, destination_path.name, descriptor)
                os.fsync(parent)
            except OSError as error:
                raise PublicationUncertainError(
                    f"published {destination_path} but could not confirm it: {error}"
                ) from error
            except ValueError as error:
                raise PublicationUncertainError(
                    f"published {destination_path} but it is not the staged tree: {error}"
                ) from error
        finally:
            os.close(descriptor)


def _copy_object(
    source: int,
    destination: int,
    digest: str,
    declared: os.stat_result,
    remaining_bytes: int,
) -> int:
    descriptor = os.open(digest, _READ_FLAGS, dir_fd=source)
    try:
        opened = os.fstat(descriptor)
        _regular(opened)
        if _identity(opened) != _identity(declared):
            raise ValueError(f"CAS object changed after preflight: sha256/{digest}")
        output = os.open(
            digest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=destination,
        )
        with os.fdopen(output, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            actual = 0
            checksum = hashlib.sha256()
            while True:
                # One extra byte detects growth without transferring past either cap.
                size = min(_COPY_BUFFER_BYTES, remaining_bytes - actual + 1,
                           declared.st_size - actual + 1)
                chunk = os.read(descriptor, size)
                if not chunk:
                    break
                actual += len(chunk)
                if actual > remaining_bytes or actual > declared.st_size:
                    raise ValueError(f"CAS object exceeds declared size or max_bytes: sha256/{digest}")
                checksum.update(chunk)
                stream.write(chunk)
            final = os.fstat(descriptor)
            _regular(final)
            if actual != declared.st_size or _identity(final) != _identity(declared):
                raise ValueError(f"CAS object changed during transfer: sha256/{digest}")
            if checksum.hexdigest() != digest:
                raise ValueError(f"CAS integrity mismatch for sha256/{digest}")
            stream.flush()
            os.fsync(stream.fileno())
        return actual
    finally:
        os.close(descriptor)


def _discard_staging(
    parent_directory: Path, staging_name: str, staged_status: os.stat_result,
    destination_name: str,
) -> None:
    """Remove our staging entry, including if it was renamed to the destination.

    Ownership is the recorded inode, so a competitor's entry is never removed.
    A rename into the destination name before publication is ours to undo: it is
    the partially filled tree, never a completed bundle.
    """
    for name in (staging_name, destination_name):
        discard_owned_directory(parent_directory / name, staged_status)


def stage_projection_cas(
    projected_database: Path,
    source_cas_directory: Path,
    destination_directory: Path,
    *,
    approved_uris: Iterable[str],
    max_bytes: int,
) -> dict[str, Any]:
    """Publish a new private CAS directory containing only approved direct objects.

    All input paths and the existing destination parent must be symlink-free;
    parent traversal, hard-linked input files, overlapping source/destination
    paths, and existing destinations are refused. Relative paths are permitted.
    Operator approval must include every non-null direct reference. Unreferenced
    approvals do not cause reads or copies. A nonnegative integer ``max_bytes``
    bounds both the stat-declared total and the bytes actually transferred.
    The projected database must be closed, checkpointed and exclusively owned,
    with stable ancestry during the call. Journal sidecars are refused, and
    SQLite reads it as immutable to avoid creating read-only WAL bookkeeping.

    Validation/integrity failures raise ``ValueError``. Filesystem and SQLite
    failures propagate. Unsupported atomic publication fails closed. No failure
    publishes a partially staged tree, and source objects are never modified.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer, not bool")
    if isinstance(approved_uris, (str, bytes)):
        raise ValueError("approved_uris must be an iterable of complete CAS URIs")
    approved: set[str] = set()
    for uri in approved_uris:
        _digest(uri)
        approved.add(uri)
    database = _path(projected_database)
    source_path = _path(source_cas_directory)
    destination_path = _path(destination_directory)
    if (
        destination_path.is_relative_to(source_path)
        or source_path.is_relative_to(destination_path)
        or database.is_relative_to(destination_path)
    ):
        raise ValueError("destination aliases or overlaps an input path")
    references = _direct_references(database)
    if any(uri not in approved for uri in references):
        raise ValueError("every directly referenced CAS URI requires explicit operator approval")

    with ExitStack() as stack:
        source = stack.enter_context(_directory(source_path))
        forbidden = stack.enter_context(_source_identities(source_path))
        parent = stack.enter_context(_directory(
            destination_path.parent, forbidden=forbidden,
        ))
        try:
            os.stat(destination_path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(f"CAS destination already exists: {destination_path}")
        source_hashes = None
        if references:
            source_hashes = os.open("sha256", _DIRECTORY_FLAGS, dir_fd=source)
            stack.callback(os.close, source_hashes)
        planned: list[tuple[str, str, os.stat_result]] = []
        declared_total = 0
        for uri in references:
            digest = _digest(uri)
            assert source_hashes is not None
            status = os.stat(digest, dir_fd=source_hashes, follow_symlinks=False)
            _regular(status)
            declared_total += status.st_size
            if declared_total > max_bytes:
                raise ValueError("declared CAS object total exceeds max_bytes")
            planned.append((uri, digest, status))

        staging_name = ".actor-cas-" + uuid4().hex
        os.mkdir(staging_name, 0o700, dir_fd=parent)
        staged_status: os.stat_result | None = None
        published = False
        try:
            with ExitStack() as staging_handles:
                staging = os.open(staging_name, _DIRECTORY_FLAGS, dir_fd=parent)
                staging_handles.callback(os.close, staging)
                # Ownership comes from the descriptor that is filled and published,
                # never from a separate path stat a competitor could have swapped.
                staged_status = os.fstat(staging)
                os.fchmod(staging, 0o700)
                os.mkdir("sha256", 0o700, dir_fd=staging)
                hashes = os.open("sha256", _DIRECTORY_FLAGS, dir_fd=staging)
                staging_handles.callback(os.close, hashes)
                os.fchmod(hashes, 0o700)
                total_bytes = 0
                objects: list[dict[str, Any]] = []
                for uri, digest, declared in planned:
                    assert source_hashes is not None
                    actual = _copy_object(
                        source_hashes, hashes, digest, declared, max_bytes - total_bytes,
                    )
                    total_bytes += actual
                    objects.append({
                        "uri": uri, "sha256": digest, "bytes": actual,
                        "relative_path": f"sha256/{digest}",
                    })
                os.fsync(hashes)
                os.fsync(staging)
                _publish_no_replace(parent, staging_name, destination_path.name)
                published = True
                try:
                    _verify_published(parent, destination_path.name, staging)
                    os.fsync(parent)
                except (OSError, ValueError) as error:
                    raise PublicationUncertainError(
                        f"published {destination_path} but could not confirm it: {error}"
                    ) from error
            return {
                "schema": _SCHEMA, "object_count": len(objects),
                "total_bytes": total_bytes, "objects": objects,
            }
        finally:
            if not published and staged_status is not None:
                _discard_staging(
                    destination_path.parent, staging_name, staged_status,
                    destination_path.name,
                )
