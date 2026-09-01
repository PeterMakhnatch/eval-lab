"""Content-addressed durable bundles for raw Harbor evidence."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, BinaryIO


@dataclass(frozen=True)
class EvidenceArchive:
    """Authenticated content identity; mutable store paths are deliberately absent."""

    record_id: str
    kind: str
    content_digest: str
    archive_digest: str
    uri: str
    file_count: int
    uncompressed_bytes: int
    record_digest: str


@dataclass(frozen=True)
class EvidenceLocator:
    """Store coordinate plus independent producer anchors used for every reopen."""

    store_root: Path
    kind: str
    record_id: str
    expected_record_digest: str
    expected_content_digest: str

    def __post_init__(self) -> None:
        supplied_store_root = Path(self.store_root)
        if not supplied_store_root.is_absolute():
            raise ValueError("evidence store root must be absolute")
        store_root = Path(os.path.abspath(os.fspath(supplied_store_root)))
        if not self.kind or self.kind in {".", ".."} or "/" in self.kind or "\x00" in self.kind:
            raise ValueError("invalid record kind")
        if (
            not self.record_id
            or self.record_id in {".", ".."}
            or "/" in self.record_id
            or "\x00" in self.record_id
        ):
            raise ValueError("invalid record id")
        for label, digest in (
            ("expected record digest", self.expected_record_digest),
            ("expected content digest", self.expected_content_digest),
        ):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError(f"invalid {label}")
        object.__setattr__(self, "store_root", store_root)


def evidence_locator(store_root: Path, archive: EvidenceArchive) -> EvidenceLocator:
    """Build the only supported downstream locator from authenticated identity."""

    return EvidenceLocator(
        store_root=store_root,
        kind=archive.kind,
        record_id=archive.record_id,
        expected_record_digest=archive.record_digest,
        expected_content_digest=archive.content_digest,
    )


def _inventory(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError(f"evidence tree contains unsupported symlink: {path}")
        if path.is_file():
            files.append(path)
    return files


def _content_digest(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def evidence_tree_digest(source: Path) -> str:
    """Return the canonical content digest used by evidence archives."""
    source = source.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"evidence source directory does not exist: {source}")
    return _content_digest(source, _inventory(source))


def _component(value: str, *, label: str = "path component") -> str:
    if not value or value in {".", ".."} or "/" in value or "\x00" in value:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _absolute(path: Path) -> Path:
    """Canonicalize trusted ancestors without accepting a symlink as the root."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    if os.path.lexists(absolute) and absolute.is_symlink():
        raise ValueError(f"CAS root cannot be a symlink: {absolute}")
    cursor = absolute
    suffix: list[str] = []
    while not cursor.exists():
        if os.path.lexists(cursor):
            raise ValueError(f"CAS root contains a broken symlink: {cursor}")
        if cursor == cursor.parent:
            break
        suffix.append(cursor.name)
        cursor = cursor.parent
    resolved = cursor.resolve(strict=True)
    return resolved.joinpath(*reversed(suffix))


@contextmanager
def _open_directory_chain(path: Path, *, create: bool) -> Iterator[int]:
    """Open a directory without following any component symlink."""
    absolute = _absolute(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(absolute.anchor or "/", flags)
    try:
        for raw_part in absolute.parts[1:]:
            part = _component(raw_part)
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise ValueError(
                    f"CAS path contains a symlink or non-directory component: {absolute}"
                ) from exc
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_nested_directory(
    root_descriptor: int,
    components: tuple[str, ...],
    *,
    create: bool,
) -> Iterator[int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.dup(root_descriptor)
    try:
        for raw_part in components:
            part = _component(raw_part)
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise ValueError(
                    f"CAS path contains a symlink or non-directory component: {part}"
                ) from exc
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _open_regular(
    directory_descriptor: int,
    name: str,
    *,
    missing_ok: bool = False,
) -> int | None:
    name = _component(name, label="file name")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_descriptor,
        )
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except OSError as exc:
        raise ValueError(f"CAS node is unsafe: {name}") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ValueError(f"CAS node is not a regular file: {name}")
    return descriptor


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _hash_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _existing_regular(directory_descriptor: int, name: str) -> bool:
    try:
        metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"CAS destination is unsafe: {name}")
    return True


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short CAS write")
        view = view[written:]


def _atomic_write(
    directory_descriptor: int,
    name: str,
    *,
    content: bytes | None = None,
    source: IO[bytes] | None = None,
) -> None:
    """Publish one regular file with an exclusive temp and atomic rename."""
    name = _component(name, label="file name")
    if (content is None) == (source is None):
        raise ValueError("exactly one CAS write source is required")
    _existing_regular(directory_descriptor, name)
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory_descriptor,
    )
    try:
        if content is not None:
            _write_all(descriptor, content)
        else:
            assert source is not None
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                _write_all(descriptor, chunk)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        with suppress(OSError):
            os.unlink(temporary, dir_fd=directory_descriptor)
        raise
    else:
        os.close(descriptor)
    try:
        _existing_regular(directory_descriptor, name)
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary, dir_fd=directory_descriptor)
        raise


def _deterministic_archive(root: Path, files: list[Path]) -> Path:
    raw_descriptor, raw_name = tempfile.mkstemp(suffix=".tar")
    os.close(raw_descriptor)
    compressed_descriptor, compressed_name = tempfile.mkstemp(suffix=".tar.gz")
    try:
        with tarfile.open(raw_name, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in files:
                relative = path.relative_to(root).as_posix()
                info = archive.gettarinfo(str(path), arcname=relative)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                info.mode = 0o600
                with path.open("rb") as source:
                    archive.addfile(info, source)
        with open(raw_name, "rb") as source, os.fdopen(compressed_descriptor, "wb") as target:
            with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as compressed:
                shutil.copyfileobj(source, compressed)
            target.flush()
            os.fsync(target.fileno())
        compressed_descriptor = -1
        return Path(compressed_name)
    except BaseException:
        if compressed_descriptor >= 0:
            os.close(compressed_descriptor)
        Path(compressed_name).unlink(missing_ok=True)
        raise
    finally:
        Path(raw_name).unlink(missing_ok=True)


def _validate_uri(uri: str) -> str:
    prefix = "cas://sha256/"
    if not uri.startswith(prefix):
        raise ValueError(f"unsupported evidence URI: {uri}")
    digest = uri.removeprefix(prefix)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("invalid content-addressed evidence URI")
    return digest


@contextmanager
def _open_archive_file(store_root: Path, uri: str) -> Iterator[BinaryIO]:
    digest = _validate_uri(uri)
    try:
        with (
            _open_directory_chain(store_root, create=False) as root_descriptor,
            _open_nested_directory(
                root_descriptor,
                ("blobs", "sha256", digest[:2]),
                create=False,
            ) as blob_directory,
        ):
            descriptor = _open_regular(blob_directory, f"{digest}.tar.gz")
            assert descriptor is not None
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                yield source
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"evidence blob is missing: {uri}") from exc


def archive_evidence(
    source: Path,
    store_root: Path,
    *,
    record_id: str,
    kind: str = "job",
) -> EvidenceArchive:
    source = source.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"evidence source directory does not exist: {source}")
    store_root = _absolute(store_root)
    record_id = _component(record_id, label="record id")
    kind = _component(kind, label="record kind")
    files = _inventory(source)
    content_digest = _content_digest(source, files)
    digest_hex = content_digest[7:]
    blob_relative = Path("blobs/sha256") / digest_hex[:2] / f"{digest_hex}.tar.gz"
    blob = store_root / blob_relative

    with _open_directory_chain(store_root, create=True) as root_descriptor:
        with _open_nested_directory(
            root_descriptor,
            ("blobs", "sha256", digest_hex[:2]),
            create=True,
        ) as blob_directory:
            if not _existing_regular(blob_directory, blob.name):
                generated = _deterministic_archive(source, files)
                try:
                    with generated.open("rb") as generated_source:
                        _atomic_write(blob_directory, blob.name, source=generated_source)
                finally:
                    generated.unlink(missing_ok=True)
            blob_descriptor = _open_regular(blob_directory, blob.name)
            assert blob_descriptor is not None
            try:
                archive_digest = f"sha256:{_hash_descriptor(blob_descriptor)}"
            finally:
                os.close(blob_descriptor)

        manifest = {
            "schema_version": 2,
            "record_id": record_id,
            "kind": kind,
            "content_digest": content_digest,
            "archive_digest": archive_digest,
            "uri": f"cas://sha256/{digest_hex}",
            "file_count": len(files),
            "uncompressed_bytes": sum(path.stat().st_size for path in files),
            "archived_at": datetime.now(UTC).isoformat(),
        }
        record_name = f"{record_id}.json"
        with _open_nested_directory(
            root_descriptor,
            ("records", kind),
            create=True,
        ) as record_directory:
            record_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
            _atomic_write(
                record_directory,
                record_name,
                content=record_bytes,
            )

    return EvidenceArchive(
        record_id=record_id,
        kind=kind,
        content_digest=content_digest,
        archive_digest=archive_digest,
        uri=manifest["uri"],
        file_count=manifest["file_count"],
        uncompressed_bytes=manifest["uncompressed_bytes"],
        record_digest=f"sha256:{hashlib.sha256(record_bytes).hexdigest()}",
    )


def _digest_value(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError(f"invalid {label}")
    return value


def _capture_authenticated_evidence(
    store_root: Path,
    *,
    kind: str,
    record_id: str,
    expected_record_digest: str,
    expected_content_digest: str,
    destination: Path,
) -> tuple[EvidenceArchive, bytes, bytes, Path]:
    """Capture and authenticate exact record/archive bytes, then restore those bytes."""

    kind = _component(kind, label="record kind")
    record_id = _component(record_id, label="record id")
    expected_record_digest = _digest_value(
        expected_record_digest,
        label="expected record digest",
    )
    expected_content_digest = _digest_value(
        expected_content_digest,
        label="expected content digest",
    )
    try:
        record_bytes = read_record(store_root, kind=kind, record_id=record_id)
        record = json.loads(record_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("evidence record is unreadable") from exc
    actual_record_digest = f"sha256:{hashlib.sha256(record_bytes).hexdigest()}"
    if actual_record_digest != expected_record_digest:
        raise ValueError("evidence record digest mismatch")
    if not isinstance(record, dict):
        raise ValueError("evidence record must be an object")
    required = {
        "schema_version",
        "record_id",
        "kind",
        "content_digest",
        "archive_digest",
        "uri",
        "file_count",
        "uncompressed_bytes",
        "archived_at",
    }
    if set(record) != required:
        raise ValueError("evidence record schema is not canonical")
    canonical_bytes = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    if record_bytes != canonical_bytes:
        raise ValueError("evidence record bytes are noncanonical")
    if (
        type(record["schema_version"]) is not int
        or record["schema_version"] != 2
        or record["record_id"] != record_id
        or record["kind"] != kind
    ):
        raise ValueError("evidence record identity is invalid")
    content_digest = _digest_value(record["content_digest"], label="content digest")
    if content_digest != expected_content_digest:
        raise ValueError("evidence content digest mismatch")
    archive_digest = _digest_value(record["archive_digest"], label="archive digest")
    uri = record["uri"]
    if not isinstance(uri, str) or _validate_uri(uri) != content_digest.removeprefix("sha256:"):
        raise ValueError("evidence record URI is invalid")
    if (
        isinstance(record["file_count"], bool)
        or not isinstance(record["file_count"], int)
        or isinstance(record["uncompressed_bytes"], bool)
        or not isinstance(record["uncompressed_bytes"], int)
        or record["file_count"] < 0
        or record["uncompressed_bytes"] < 0
    ):
        raise ValueError("evidence record size fields are invalid")
    if not isinstance(record["archived_at"], str):
        raise ValueError("evidence record timestamp is invalid")
    try:
        archived_at = datetime.fromisoformat(record["archived_at"])
    except ValueError as exc:
        raise ValueError("evidence record timestamp is invalid") from exc
    if (
        archived_at.tzinfo is None
        or archived_at.utcoffset() != UTC.utcoffset(archived_at)
        or archived_at.isoformat() != record["archived_at"]
    ):
        raise ValueError("evidence record timestamp is invalid")

    archive_bytes = read_archive(store_root, uri)
    actual_archive_digest = f"sha256:{hashlib.sha256(archive_bytes).hexdigest()}"
    if archive_digest != actual_archive_digest:
        raise ValueError("evidence archive digest mismatch")
    restored = restore_evidence(
        store_root,
        uri,
        destination,
        archive_bytes=archive_bytes,
    )
    restored_files = _inventory(restored)
    restored_digest = _content_digest(restored, restored_files)
    restored_bytes = sum(path.stat().st_size for path in restored_files)
    if (
        restored_digest != content_digest
        or record["file_count"] != len(restored_files)
        or record["uncompressed_bytes"] != restored_bytes
    ):
        raise ValueError("evidence record content mismatch")
    return (
        EvidenceArchive(
            record_id=record_id,
            kind=kind,
            content_digest=content_digest,
            archive_digest=archive_digest,
            uri=uri,
            file_count=record["file_count"],
            uncompressed_bytes=record["uncompressed_bytes"],
            record_digest=actual_record_digest,
        ),
        record_bytes,
        archive_bytes,
        restored,
    )


def reopen_evidence_archive(
    store_root: Path,
    *,
    kind: str,
    record_id: str,
    expected_record_digest: str,
    expected_content_digest: str,
) -> tuple[EvidenceArchive, bytes]:
    """Reopen exact captured bytes against independent record and content anchors."""

    with tempfile.TemporaryDirectory(prefix="evallab-evidence-reopen-") as temporary:
        archive, record_bytes, _archive_bytes, _restored = _capture_authenticated_evidence(
            store_root,
            kind=kind,
            record_id=record_id,
            expected_record_digest=expected_record_digest,
            expected_content_digest=expected_content_digest,
            destination=Path(temporary),
        )
    return archive, record_bytes


def materialize_evidence_at(locator: EvidenceLocator, destination: Path) -> Path:
    """Restore exact authenticated CAS bytes into a caller-owned empty directory."""

    archive, _record_bytes, _archive_bytes, restored = _capture_authenticated_evidence(
        locator.store_root,
        kind=locator.kind,
        record_id=locator.record_id,
        expected_record_digest=locator.expected_record_digest,
        expected_content_digest=locator.expected_content_digest,
        destination=destination,
    )
    if archive.content_digest != locator.expected_content_digest:
        raise ValueError("materialized evidence content identity mismatch")
    return restored


@contextmanager
def materialize_evidence(locator: EvidenceLocator) -> Iterator[Path]:
    """Yield an ephemeral tree restored from the exact authenticated CAS bytes."""
    with tempfile.TemporaryDirectory(prefix="evallab-evidence-materialized-") as temporary:
        yield materialize_evidence_at(locator, Path(temporary))


def load_archive(store_root: Path, uri: str) -> Path:
    """Validate a CAS archive through no-follow descriptors and return its path."""
    digest = _validate_uri(uri)
    with _open_archive_file(store_root, uri):
        pass
    return _absolute(store_root) / "blobs/sha256" / digest[:2] / f"{digest}.tar.gz"


@contextmanager
def open_archive(store_root: Path, uri: str) -> Iterator[BinaryIO]:
    """Open a CAS archive while retaining no-follow directory descriptors."""

    with _open_archive_file(store_root, uri) as source:
        yield source


def read_archive(store_root: Path, uri: str) -> bytes:
    """Read a CAS archive without following store or blob symlinks."""

    with _open_archive_file(store_root, uri) as source:
        return source.read()


def restore_evidence(
    store_root: Path,
    uri: str,
    destination: Path,
    *,
    archive_bytes: bytes | None = None,
) -> Path:
    """Restore trusted archive bytes, or open the current CAS archive when omitted."""

    expected_digest = f"sha256:{_validate_uri(uri)}"
    destination = _absolute(destination)
    blob_context = (
        nullcontext(io.BytesIO(archive_bytes))
        if archive_bytes is not None
        else _open_archive_file(store_root, uri)
    )
    with (
        _open_directory_chain(destination, create=True) as destination_descriptor,
        blob_context as blob,
        tarfile.open(fileobj=blob, mode="r:gz") as archive,
    ):
        for member in archive.getmembers():
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or not member_path.parts:
                raise ValueError(f"evidence archive path escapes destination: {member.name}")
            parts = tuple(_component(part) for part in member_path.parts)
            if not member.isfile():
                raise ValueError(f"evidence archive contains non-file member: {member.name}")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read evidence member: {member.name}")
            with (
                source,
                _open_nested_directory(
                    destination_descriptor,
                    parts[:-1],
                    create=True,
                ) as target_directory,
            ):
                _atomic_write(target_directory, parts[-1], source=source)
    restored_files = _inventory(destination)
    actual_digest = _content_digest(destination, restored_files)
    if actual_digest != expected_digest:
        raise ValueError(
            f"restored evidence digest mismatch: expected {expected_digest}, got {actual_digest}"
        )
    return destination


def store_blob(store_root: Path, content: bytes | str) -> str:
    """Store raw payload bytes/text in content-addressed storage."""
    raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
    digest_hex = hashlib.sha256(raw_bytes).hexdigest()
    uri = f"cas://sha256/{digest_hex}"
    store_root = _absolute(store_root)
    name = f"{digest_hex}.bin"
    with (
        _open_directory_chain(store_root, create=True) as root_descriptor,
        _open_nested_directory(
            root_descriptor,
            ("blobs", "sha256", digest_hex[:2]),
            create=True,
        ) as blob_directory,
    ):
        if not _existing_regular(blob_directory, name):
            _atomic_write(blob_directory, name, content=raw_bytes)
        descriptor = _open_regular(blob_directory, name)
        assert descriptor is not None
        try:
            actual_digest = _hash_descriptor(descriptor)
        finally:
            os.close(descriptor)
        if actual_digest != digest_hex:
            raise ValueError(
                f"evidence blob digest mismatch: expected {digest_hex}, got {actual_digest}"
            )
    return uri


def load_blob(store_root: Path, uri: str) -> bytes:
    """Load raw payload bytes from content-addressed storage."""
    digest = _validate_uri(uri)
    try:
        with (
            _open_directory_chain(store_root, create=False) as root_descriptor,
            _open_nested_directory(
                root_descriptor,
                ("blobs", "sha256", digest[:2]),
                create=False,
            ) as blob_directory,
        ):
            descriptor = _open_regular(
                blob_directory,
                f"{digest}.bin",
                missing_ok=True,
            )
            if descriptor is None:
                archive = _open_regular(
                    blob_directory,
                    f"{digest}.tar.gz",
                    missing_ok=True,
                )
                if archive is not None:
                    os.close(archive)
                    raise ValueError(
                        f"URI {uri} points to a directory archive, use load_archive instead"
                    )
                raise FileNotFoundError(f"evidence blob is missing: {uri}")
            try:
                content = _read_descriptor(descriptor)
            finally:
                os.close(descriptor)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"evidence blob is missing: {uri}") from exc
    actual_digest = hashlib.sha256(content).hexdigest()
    if actual_digest != digest:
        raise ValueError(f"evidence blob digest mismatch: expected {digest}, got {actual_digest}")
    return content


def read_record(store_root: Path, *, kind: str, record_id: str) -> bytes:
    """Read one CAS record without following nested directory or file symlinks."""
    kind = _component(kind, label="record kind")
    record_id = _component(record_id, label="record id")
    try:
        with (
            _open_directory_chain(store_root, create=False) as root_descriptor,
            _open_nested_directory(
                root_descriptor,
                ("records", kind),
                create=False,
            ) as record_directory,
        ):
            descriptor = _open_regular(record_directory, f"{record_id}.json")
            assert descriptor is not None
            try:
                return _read_descriptor(descriptor)
            finally:
                os.close(descriptor)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"evidence record is missing: {kind}/{record_id}") from exc
