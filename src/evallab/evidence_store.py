"""Content-addressed durable bundles for raw Harbor evidence."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class EvidenceArchive:
    record_id: str
    kind: str
    content_digest: str
    archive_digest: str
    uri: str
    blob_path: Path
    manifest_path: Path
    file_count: int
    uncompressed_bytes: int


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


def _deterministic_archive(root: Path, files: list[Path], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".tar", delete=False) as raw:
        raw_path = Path(raw.name)
    try:
        with tarfile.open(raw_path, mode="w", format=tarfile.PAX_FORMAT) as archive:
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
        temporary = output.with_suffix(".tar.gz.tmp")
        with raw_path.open("rb") as source, temporary.open("wb") as target:
            with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as compressed:
                shutil.copyfileobj(source, compressed)
            target.flush()
            os.fsync(target.fileno())
        temporary.chmod(0o600)
        try:
            temporary.replace(output)
        except FileExistsError:
            temporary.unlink()
    finally:
        raw_path.unlink(missing_ok=True)


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
    store_root = store_root.resolve()
    store_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    store_root.chmod(0o700)
    files = _inventory(source)
    content_digest = _content_digest(source, files)
    digest_hex = content_digest[7:]
    blob = store_root / "blobs/sha256" / digest_hex[:2] / f"{digest_hex}.tar.gz"
    if not blob.is_file():
        _deterministic_archive(source, files, blob)
    archive_digest = f"sha256:{hashlib.sha256(blob.read_bytes()).hexdigest()}"
    manifest = {
        "schema_version": 1,
        "record_id": record_id,
        "kind": kind,
        "content_digest": content_digest,
        "archive_digest": archive_digest,
        "uri": f"cas://sha256/{digest_hex}",
        "blob_path": str(blob.relative_to(store_root)),
        "source_path": str(source),
        "file_count": len(files),
        "uncompressed_bytes": sum(path.stat().st_size for path in files),
        "archived_at": datetime.now(UTC).isoformat(),
    }
    record_path = store_root / "records" / kind / f"{record_id}.json"
    record_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = record_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(record_path)
    return EvidenceArchive(
        record_id=record_id,
        kind=kind,
        content_digest=content_digest,
        archive_digest=archive_digest,
        uri=manifest["uri"],
        blob_path=blob,
        manifest_path=record_path,
        file_count=manifest["file_count"],
        uncompressed_bytes=manifest["uncompressed_bytes"],
    )


def load_archive(store_root: Path, uri: str) -> Path:
    prefix = "cas://sha256/"
    if not uri.startswith(prefix):
        raise ValueError(f"unsupported evidence URI: {uri}")
    digest = uri.removeprefix(prefix)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("invalid content-addressed evidence URI")
    path = store_root.resolve() / "blobs/sha256" / digest[:2] / f"{digest}.tar.gz"
    if not path.is_file():
        raise FileNotFoundError(f"evidence blob is missing: {uri}")
    return path


def restore_evidence(store_root: Path, uri: str, destination: Path) -> Path:
    blob = load_archive(store_root, uri)
    expected_digest = f"sha256:{uri.removeprefix('cas://sha256/')}"
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(blob, mode="r:gz") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination)
            except ValueError as exc:
                raise ValueError(
                    f"evidence archive path escapes destination: {member.name}"
                ) from exc
            if not member.isfile():
                raise ValueError(f"evidence archive contains non-file member: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read evidence member: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    restored_files = _inventory(destination)
    actual_digest = _content_digest(destination, restored_files)
    if actual_digest != expected_digest:
        raise ValueError(
            f"restored evidence digest mismatch: expected {expected_digest}, got {actual_digest}"
        )
    return destination


def store_blob(store_root: Path, content: bytes | str) -> str:
    """Store raw payload bytes/text in content-addressed storage and return cas://sha256/<hash>."""
    raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
    digest_hex = hashlib.sha256(raw_bytes).hexdigest()
    uri = f"cas://sha256/{digest_hex}"
    blob = store_root.resolve() / "blobs/sha256" / digest_hex[:2] / f"{digest_hex}.bin"
    if not blob.is_file():
        blob.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = blob.with_suffix(".bin.tmp")
        temporary.write_bytes(raw_bytes)
        temporary.chmod(0o600)
        try:
            temporary.replace(blob)
        except FileExistsError:
            temporary.unlink(missing_ok=True)
    return uri


def load_blob(store_root: Path, uri: str) -> bytes:
    """Load raw payload bytes from content-addressed storage."""
    prefix = "cas://sha256/"
    if not uri.startswith(prefix):
        raise ValueError(f"unsupported evidence URI: {uri}")
    digest = uri.removeprefix(prefix)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("invalid content-addressed evidence URI")
    path = store_root.resolve() / "blobs/sha256" / digest[:2] / f"{digest}.bin"
    if not path.is_file():
        if (store_root.resolve() / "blobs/sha256" / digest[:2] / f"{digest}.tar.gz").is_file():
            raise ValueError(f"URI {uri} points to a directory archive, use load_archive instead")
        raise FileNotFoundError(f"evidence blob is missing: {uri}")
    content = path.read_bytes()
    actual_digest = hashlib.sha256(content).hexdigest()
    if actual_digest != digest:
        raise ValueError(f"evidence blob digest mismatch: expected {digest}, got {actual_digest}")
    return content
