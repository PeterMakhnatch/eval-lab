"""Immutable Git-selected source authority for historical regeneration."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import selectors
import subprocess
import tempfile
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any, BinaryIO, Literal, cast

from pydantic import Field, model_validator

from evallab.schemas import ContractModel

HISTORICAL_SOURCE_SNAPSHOT_DOMAIN = b"evallab.historical-source-snapshot.v1\x00"
HISTORICAL_CONTRACT_FILENAME = "historical-contract.json"
_DOCUMENT_SUFFIXES = frozenset({"lock.json", "verifier/result.json"})
_REGULAR_MODES = frozenset({"100644", "100755"})
_BLOB_READ_CHUNK_SIZE = 64 * 1024
_MAX_BATCH_HEADER_BYTES = 1024
_MAX_BATCH_STDERR_BYTES = 64 * 1024
_CAT_FILE_TERMINATE_GRACE_SECONDS = 1.0
_CAT_FILE_IO_TIMEOUT_SECONDS = 120.0


class HistoricalRegenerationError(RuntimeError):
    """Base failure for strict historical regeneration."""


class HistoricalSnapshotInvalid(HistoricalRegenerationError):
    """Raised when selected Git tree metadata is unsafe or unsupported."""


class HistoricalSnapshotUnavailable(HistoricalRegenerationError):
    """Raised when an exact selected Git object cannot be authenticated."""


class HistoricalGitBlobV1(ContractModel):
    """One identity-bearing selected regular Git blob."""

    path: str = Field(min_length=1)
    mode: Literal["100644", "100755"]
    git_oid: str = Field(pattern=r"^[0-9a-f]+$")
    sha256_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def identity_is_safe(self) -> HistoricalGitBlobV1:
        _validate_repo_relative_path(self.path)
        return self


class HistoricalSourceSnapshotV1(ContractModel):
    """Canonical selected-blob source snapshot independent of commit identity."""

    schema_version: Literal["historical-source-snapshot/v1"]
    authority: Literal["git-selected-blobs"]
    git_object_format: Literal["sha1", "sha256"]
    runs_root: str = Field(min_length=1)
    blobs: tuple[HistoricalGitBlobV1, ...]
    snapshot_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def snapshot_identity_matches(self) -> HistoricalSourceSnapshotV1:
        _validate_repo_relative_path(self.runs_root)
        paths = tuple(blob.path for blob in self.blobs)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("historical snapshot blob paths must be unique and sorted")
        expected_oid_length = 40 if self.git_object_format == "sha1" else 64
        if any(len(blob.git_oid) != expected_oid_length for blob in self.blobs):
            raise ValueError("historical snapshot Git OID length does not match object format")
        prefix = f"{self.runs_root}/"
        if any(not blob.path.startswith(prefix) for blob in self.blobs):
            raise ValueError("historical snapshot blob escapes runs root")
        body = self.model_dump(mode="json", exclude={"snapshot_digest"})
        if self.snapshot_digest != _domain_json_digest(
            HISTORICAL_SOURCE_SNAPSHOT_DOMAIN,
            body,
        ):
            raise ValueError("historical source snapshot digest mismatch")
        return self


@dataclass(frozen=True)
class HistoricalSourceCapture:
    """Operational retrieval result plus only planner-required source bytes."""

    snapshot: HistoricalSourceSnapshotV1
    resolved_commit: str
    document_bytes: dict[str, bytes]


@dataclass(frozen=True)
class _GitTreeEntry:
    path: str
    mode: str
    object_type: str
    git_oid: str
    size_bytes: int


@dataclass(frozen=True)
class _AuthenticatedGitBlob:
    git_oid: str
    sha256_digest: str
    size_bytes: int
    retained_content: bytes | None


@dataclass
class _CatFileDeadlineReader:
    stream: IO[bytes]
    deadline: float
    selector: selectors.BaseSelector = field(init=False)
    pending: bytearray = field(default_factory=bytearray, init=False)

    def __post_init__(self) -> None:
        os_fd = self.stream.fileno()
        self.selector = selectors.DefaultSelector()
        try:
            os.set_blocking(os_fd, False)
            self.selector.register(os_fd, selectors.EVENT_READ)
        except (OSError, ValueError) as exc:
            self.selector.close()
            raise HistoricalSnapshotUnavailable(
                "Git cat-file stdout cannot be monitored nonblocking"
            ) from exc

    def read_line(self, limit: int) -> bytes:
        while True:
            line_end = self.pending.find(b"\n")
            if line_end >= 0:
                if line_end > limit:
                    raise HistoricalSnapshotUnavailable("oversized Git cat-file batch header")
                line = bytes(self.pending[:line_end])
                del self.pending[: line_end + 1]
                return line
            if len(self.pending) > limit:
                raise HistoricalSnapshotUnavailable("oversized Git cat-file batch header")
            chunk = self._read_ready(limit + 1 - len(self.pending), "batch header")
            if not chunk:
                raise HistoricalSnapshotUnavailable("truncated Git cat-file batch header")
            self.pending.extend(chunk)

    def read_payload(self, maximum: int) -> bytes:
        if maximum <= 0:
            raise HistoricalSnapshotInvalid("Git payload read bound must be positive")
        if self.pending:
            size = min(maximum, len(self.pending))
            value = bytes(self.pending[:size])
            del self.pending[:size]
            return value
        return self._read_ready(maximum, "blob payload")

    def read_one(self, phase: str) -> bytes:
        if self.pending:
            value = bytes(self.pending[:1])
            del self.pending[:1]
            return value
        return self._read_ready(1, phase)

    def expect_eof(self) -> None:
        if self.pending:
            raise HistoricalSnapshotUnavailable("unexpected trailing Git cat-file output")
        if self._read_ready(1, "final EOF"):
            raise HistoricalSnapshotUnavailable("unexpected trailing Git cat-file output")

    def remaining_seconds(self, phase: str) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise HistoricalSnapshotUnavailable(f"Git cat-file timed out waiting for {phase}")
        return remaining

    def close(self) -> None:
        self.selector.close()

    def _read_ready(self, maximum: int, phase: str) -> bytes:

        while True:
            ready = self.selector.select(self.remaining_seconds(phase))
            if not ready:
                raise HistoricalSnapshotUnavailable(f"Git cat-file timed out waiting for {phase}")
            try:
                return os.read(self.stream.fileno(), maximum)
            except BlockingIOError:
                continue


def _open_cat_file_deadline_reader(
    stream: IO[bytes],
    deadline: float,
) -> _CatFileDeadlineReader:
    return _CatFileDeadlineReader(stream=stream, deadline=deadline)


def normalize_runs_root(runs_root: Path) -> str:
    """Return one safe repository-relative POSIX runs root."""
    if runs_root.is_absolute():
        raise HistoricalSnapshotInvalid("runs root must be repository-relative")
    raw = runs_root.as_posix()
    return _validate_repo_relative_path(raw)


def resolve_git_repository(repo_root: Path) -> Path:
    """Resolve a worktree path to its explicit Git top-level."""
    requested = repo_root.resolve()
    result = _run_git(requested, ["rev-parse", "--show-toplevel"])
    try:
        top_level = Path(result.stdout.decode("utf-8", errors="strict").strip()).resolve()
    except (UnicodeDecodeError, ValueError) as exc:
        raise HistoricalSnapshotInvalid("Git top-level is not valid UTF-8") from exc
    if not top_level.is_dir():
        raise HistoricalSnapshotInvalid(f"Git top-level is not a directory: {top_level}")
    return top_level


def capture_historical_source_snapshot(
    *,
    repo_root: Path,
    runs_root: Path,
    source_revision: str,
) -> HistoricalSourceCapture:
    """Select and authenticate every promoted-trial blob from one resolved commit."""
    repository = resolve_git_repository(repo_root)
    normalized_runs_root = normalize_runs_root(runs_root)
    resolved_commit = _resolve_commit(repository, source_revision)
    object_format = _object_format(repository)
    entries = _enumerate_tree(repository, resolved_commit, normalized_runs_root)
    selected = _select_promoted_trial_entries(entries, normalized_runs_root)
    selected_trial_roots = tuple(
        entry.path.removeprefix(f"{normalized_runs_root}/").removesuffix("/artifacts/manifest.json")
        for entry in selected
        if entry.path.endswith("/artifacts/manifest.json")
    )
    semantic_paths = frozenset(
        entry.path
        for entry in selected
        if any(
            entry.path == f"{normalized_runs_root}/{trial_root}/{document_suffix}"
            for trial_root in selected_trial_roots
            for document_suffix in _DOCUMENT_SUFFIXES
        )
    )
    entries_by_oid: dict[str, list[_GitTreeEntry]] = {}
    expected_sizes: dict[str, int] = {}
    for entry in selected:
        previous_size = expected_sizes.setdefault(entry.git_oid, entry.size_bytes)
        if previous_size != entry.size_bytes:
            raise HistoricalSnapshotInvalid(
                f"one Git OID has conflicting selected sizes: {entry.git_oid}"
            )
        entries_by_oid.setdefault(entry.git_oid, []).append(entry)

    blobs: list[HistoricalGitBlobV1] = []
    documents: dict[str, bytes] = {}

    def record_blob(authenticated: _AuthenticatedGitBlob) -> None:
        for entry in entries_by_oid[authenticated.git_oid]:
            blobs.append(
                HistoricalGitBlobV1(
                    path=entry.path,
                    mode=cast(Literal["100644", "100755"], entry.mode),
                    git_oid=entry.git_oid,
                    sha256_digest=authenticated.sha256_digest,
                    size_bytes=authenticated.size_bytes,
                )
            )
            if entry.path in semantic_paths:
                if authenticated.retained_content is None:
                    raise HistoricalSnapshotUnavailable(
                        f"semantic Git blob was not retained: {entry.git_oid}"
                    )
                documents[entry.path] = authenticated.retained_content

    _stream_blob_batch(
        repository,
        expected_sizes,
        retain_oids=frozenset(entry.git_oid for entry in selected if entry.path in semantic_paths),
        on_blob=record_blob,
    )
    blobs.sort(key=lambda blob: blob.path)

    body: dict[str, Any] = {
        "schema_version": "historical-source-snapshot/v1",
        "authority": "git-selected-blobs",
        "git_object_format": object_format,
        "runs_root": normalized_runs_root,
        "blobs": [blob.model_dump(mode="json") for blob in blobs],
    }
    snapshot = HistoricalSourceSnapshotV1.model_validate(
        {
            **body,
            "snapshot_digest": _domain_json_digest(
                HISTORICAL_SOURCE_SNAPSHOT_DOMAIN,
                body,
            ),
        }
    )
    return HistoricalSourceCapture(
        snapshot=snapshot,
        resolved_commit=resolved_commit,
        document_bytes=documents,
    )


def reopen_historical_source_snapshot(
    *,
    repo_root: Path,
    snapshot: HistoricalSourceSnapshotV1,
) -> None:
    """Reopen every manifest-listed blob by OID and authenticate the snapshot."""
    repository = resolve_git_repository(repo_root)
    if _object_format(repository) != snapshot.git_object_format:
        raise HistoricalSnapshotUnavailable("repository object format does not match snapshot")
    expected: dict[str, tuple[int, str]] = {}
    for blob in snapshot.blobs:
        identity = (blob.size_bytes, blob.sha256_digest)
        previous = expected.setdefault(blob.git_oid, identity)
        if previous != identity:
            raise HistoricalSnapshotInvalid(
                f"one snapshot Git OID has conflicting identities: {blob.git_oid}"
            )

    def verify_blob(authenticated: _AuthenticatedGitBlob) -> None:
        expected_size, expected_digest = expected[authenticated.git_oid]
        if (
            authenticated.size_bytes != expected_size
            or authenticated.sha256_digest != expected_digest
            or authenticated.retained_content is not None
        ):
            raise HistoricalSnapshotUnavailable(
                f"selected Git blob bytes do not match snapshot: {authenticated.git_oid}"
            )

    _stream_blob_batch(
        repository,
        {git_oid: identity[0] for git_oid, identity in expected.items()},
        retain_oids=frozenset(),
        on_blob=verify_blob,
    )
    body = snapshot.model_dump(mode="json", exclude={"snapshot_digest"})
    if _domain_json_digest(HISTORICAL_SOURCE_SNAPSHOT_DOMAIN, body) != snapshot.snapshot_digest:
        raise HistoricalSnapshotInvalid("reopened historical snapshot digest mismatch")


def _resolve_commit(repository: Path, revision: str) -> str:
    if not revision or "\x00" in revision:
        raise HistoricalSnapshotInvalid("source revision must be a non-empty Git revision")
    result = _run_git(repository, ["rev-parse", "--verify", f"{revision}^{{commit}}"])
    try:
        commit = result.stdout.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise HistoricalSnapshotInvalid("resolved commit ID is not ASCII") from exc
    expected_length = 40 if _object_format(repository) == "sha1" else 64
    if len(commit) != expected_length or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise HistoricalSnapshotInvalid("resolved commit ID is malformed")
    return commit


def _object_format(repository: Path) -> Literal["sha1", "sha256"]:
    result = _run_git(repository, ["rev-parse", "--show-object-format"])
    try:
        value = result.stdout.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise HistoricalSnapshotInvalid("Git object format is not ASCII") from exc
    if value not in {"sha1", "sha256"}:
        raise HistoricalSnapshotInvalid(f"unsupported Git object format: {value}")
    return value


def _enumerate_tree(
    repository: Path,
    resolved_commit: str,
    runs_root: str,
) -> tuple[_GitTreeEntry, ...]:
    result = _run_git(
        repository,
        ["ls-tree", "-rz", "-l", "--full-tree", resolved_commit, "--", runs_root],
    )
    entries: list[_GitTreeEntry] = []
    seen: set[str] = set()
    for record in result.stdout.split(b"\x00"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise HistoricalSnapshotInvalid("malformed NUL-delimited Git tree entry")
        try:
            path = raw_path.decode("utf-8", errors="strict")
            fields = header.decode("ascii", errors="strict").split()
        except UnicodeDecodeError as exc:
            raise HistoricalSnapshotInvalid("Git tree contains a non-UTF-8 selected path") from exc
        if len(fields) != 4:
            raise HistoricalSnapshotInvalid("malformed Git tree metadata")
        mode, object_type, git_oid, raw_size = fields
        _validate_repo_relative_path(path)
        if path in seen:
            raise HistoricalSnapshotInvalid(f"duplicate Git tree path: {path}")
        seen.add(path)
        try:
            size = int(raw_size)
        except ValueError:
            size = -1
        entries.append(
            _GitTreeEntry(
                path=path,
                mode=mode,
                object_type=object_type,
                git_oid=git_oid,
                size_bytes=size,
            )
        )
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _select_promoted_trial_entries(
    entries: tuple[_GitTreeEntry, ...],
    runs_root: str,
) -> tuple[_GitTreeEntry, ...]:
    prefix = f"{runs_root}/"
    markers: list[str] = []
    for entry in entries:
        if not entry.path.startswith(prefix):
            raise HistoricalSnapshotInvalid(f"Git tree entry escaped runs root: {entry.path}")
        relative = entry.path.removeprefix(prefix)
        parts = PurePosixPath(relative).parts
        if len(parts) >= 3 and parts[-2:] == ("artifacts", "manifest.json"):
            markers.append(relative)
    trial_roots = [PurePosixPath(marker).parent.parent.as_posix() for marker in markers]
    if len(trial_roots) != len(set(trial_roots)):
        raise HistoricalSnapshotInvalid("duplicate promoted trial root")
    for index, root in enumerate(trial_roots):
        root_prefix = f"{root}/"
        for other in trial_roots[index + 1 :]:
            if other.startswith(root_prefix) or root.startswith(f"{other}/"):
                raise HistoricalSnapshotInvalid("overlapping promoted trial roots")

    selected: list[_GitTreeEntry] = []
    for entry in entries:
        relative = entry.path.removeprefix(prefix)
        owner = next(
            (root for root in trial_roots if relative.startswith(f"{root}/")),
            None,
        )
        if owner is None:
            continue
        generated = f"{owner}/artifacts/{HISTORICAL_CONTRACT_FILENAME}"
        if relative == generated:
            continue
        if entry.mode not in _REGULAR_MODES or entry.object_type != "blob" or entry.size_bytes < 0:
            raise HistoricalSnapshotInvalid(
                f"selected historical source is not a supported regular blob: {entry.path}"
            )
        selected.append(entry)
    return tuple(selected)


def _stream_blob_batch(
    repository: Path,
    expected_sizes: Mapping[str, int],
    *,
    retain_oids: Collection[str],
    on_blob: Callable[[_AuthenticatedGitBlob], None],
) -> None:
    """Authenticate selected blobs with fixed-size reads and bounded retention."""
    ordered_oids = tuple(sorted(expected_sizes))
    if not ordered_oids:
        return
    unknown_retained = set(retain_oids).difference(expected_sizes)
    if unknown_retained:
        raise HistoricalSnapshotInvalid(
            f"retained Git OIDs are not selected: {sorted(unknown_retained)}"
        )

    process: subprocess.Popen[bytes] | None = None
    reader: _CatFileDeadlineReader | None = None
    with tempfile.TemporaryFile() as stderr:
        try:
            process = _start_cat_file_batch(repository, stderr)
            if process.stdin is None or process.stdout is None:
                raise HistoricalSnapshotUnavailable("Git cat-file batch pipes are unavailable")
            reader = _open_cat_file_deadline_reader(
                process.stdout,
                time.monotonic() + _CAT_FILE_IO_TIMEOUT_SECONDS,
            )
            for expected_oid in ordered_oids:
                process.stdin.write(f"{expected_oid}\n".encode("ascii"))
                process.stdin.flush()
                header = reader.read_line(_MAX_BATCH_HEADER_BYTES)
                try:
                    fields = header.decode("ascii", errors="strict").split()
                except UnicodeDecodeError as exc:
                    raise HistoricalSnapshotUnavailable(
                        "non-ASCII Git cat-file batch header"
                    ) from exc
                if len(fields) != 3 or fields[1] != "blob" or fields[0] != expected_oid:
                    raise HistoricalSnapshotUnavailable(
                        f"Git object unavailable or wrong type/OID: {expected_oid}"
                    )
                try:
                    reported_size = int(fields[2])
                except ValueError as exc:
                    raise HistoricalSnapshotUnavailable("invalid Git blob size") from exc
                if reported_size != expected_sizes[expected_oid]:
                    raise HistoricalSnapshotUnavailable(
                        f"Git blob size differs from selected tree metadata: {expected_oid}"
                    )

                digest = hashlib.sha256()
                retained = bytearray() if expected_oid in retain_oids else None
                remaining = reported_size
                while remaining:
                    chunk = reader.read_payload(min(_BLOB_READ_CHUNK_SIZE, remaining))
                    if not chunk:
                        raise HistoricalSnapshotUnavailable(
                            f"truncated Git blob payload: {expected_oid}"
                        )
                    if len(chunk) > remaining:
                        raise HistoricalSnapshotUnavailable(
                            f"oversized Git blob payload read: {expected_oid}"
                        )
                    digest.update(chunk)
                    if retained is not None:
                        retained.extend(chunk)
                    remaining -= len(chunk)
                if reader.read_one("record delimiter") != b"\n":
                    raise HistoricalSnapshotUnavailable(
                        f"Git blob payload lacks delimiter: {expected_oid}"
                    )
                on_blob(
                    _AuthenticatedGitBlob(
                        git_oid=expected_oid,
                        sha256_digest=f"sha256:{digest.hexdigest()}",
                        size_bytes=reported_size,
                        retained_content=None if retained is None else bytes(retained),
                    )
                )

            process.stdin.close()
            reader.expect_eof()
            try:
                returncode = process.wait(timeout=reader.remaining_seconds("process exit"))
            except subprocess.TimeoutExpired as exc:
                raise HistoricalSnapshotUnavailable(
                    "Git cat-file timed out waiting for process exit"
                ) from exc
            stderr_bytes = _read_bounded_stderr(stderr)
            if returncode != 0 or stderr_bytes:
                detail = stderr_bytes.decode("utf-8", errors="replace").strip()
                raise HistoricalSnapshotUnavailable(
                    f"Git cat-file batch failed: {detail or f'exit {returncode}'}"
                )
        except (OSError, BrokenPipeError) as exc:
            raise HistoricalSnapshotUnavailable("Git cat-file batch unavailable") from exc
        finally:
            if reader is not None:
                reader.close()
            if process is not None:
                _terminate_cat_file_batch(process)


def _start_cat_file_batch(
    repository: Path,
    stderr: BinaryIO,
) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(
            ["git", "-C", str(repository), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            bufsize=0,
        )
    except OSError as exc:
        raise HistoricalSnapshotUnavailable("Git cat-file batch is unavailable") from exc


def _read_bounded_stderr(stderr: BinaryIO) -> bytes:
    stderr.seek(0)
    value = stderr.read(_MAX_BATCH_STDERR_BYTES + 1)
    if len(value) > _MAX_BATCH_STDERR_BYTES:
        raise HistoricalSnapshotUnavailable("Git cat-file stderr exceeded safety bound")
    return value


def _terminate_cat_file_batch(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout):
        if stream is not None and not stream.closed:
            with contextlib.suppress(OSError):
                stream.close()
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=_CAT_FILE_TERMINATE_GRACE_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=_CAT_FILE_TERMINATE_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _validate_repo_relative_path(raw: str) -> str:
    if not raw or raw in {".", ".."} or "\x00" in raw or "\\" in raw:
        raise HistoricalSnapshotInvalid(f"unsafe repository-relative path: {raw!r}")
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise HistoricalSnapshotInvalid(f"unsafe repository-relative path: {raw!r}")
    normalized = candidate.as_posix()
    if normalized != raw:
        raise HistoricalSnapshotInvalid(f"non-canonical repository-relative path: {raw!r}")
    return normalized


def _run_git(
    repository: Path,
    arguments: list[str],
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HistoricalSnapshotUnavailable(
            f"Git command unavailable: {' '.join(arguments)}"
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise HistoricalSnapshotUnavailable(
            f"Git command failed ({' '.join(arguments)}): {stderr or 'unknown error'}"
        )
    return result


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _domain_json_digest(domain: bytes, value: Any) -> str:
    return _sha256_digest(domain + _canonical_json_bytes(value))


def _sha256_digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"
