"""Derived task variants with content-addressed lineage (schema ``evallab.task_variant/v1``).

Every modified task — a GEPA/DSPy instruction rewrite, a hand fix of a broken
verifier — becomes a *variant*: a full derived package plus a git-tracked
lineage record linking it to its parent. The original task stays untouched and
remains the control; the variant is the treatment.

Storage split (see docs/task-variants.md and docs/data-architecture.md):

- ``library/task-variants/<task_slug>/<digest12>.json`` — the lineage record,
  git-tracked and reviewable. Small, because it stores only the *changed*
  files inline.
- ``<shared derived root>/task-store/variants/<task_slug>/<digest12>/`` — the
  materialized package, rebuildable from the parent bytes plus the record, so
  it is never edited in place and lives outside Git (all of ``derived/`` is
  already gitignored).

Integrity model: ``derive_task`` computes the variant package digest with
``evallab.registry.task_directory_digest`` — the same authority
``task_candidate`` and GEPA use. The record pins that digest plus per-file
before/after digests; ``materialize`` proves the invariant (parent bytes +
record files reproduce the digest exactly), and ``verify`` re-checks parent,
files and package against bytes.

Rejects: binary or >1 MiB changed files, symlinks anywhere in the parent,
relative paths that escape the package, deletions of files the parent does
not have, and no-op derivations (variant digest == parent digest).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from evallab.registry import (
    compute_subpath_digest,
    compute_task_digests,
    harbor_task_digest,
    task_directory_digest,
)
from evallab.schemas import ContractModel
from evallab.storage.paths import shared_checkout_root

#: Task components a variant may change (classified via registry.compute_task_digests).
ComponentKey = Literal["task_toml", "instruction", "environment", "verifier", "solution"]

#: Durable lineage-record schema tag. Owned here, read by the task catalog.
VARIANT_RECORD_SCHEMA = "evallab.task_variant/v1"

#: Git-tracked lineage records live under this repo-relative directory.
RECORDS_DIRNAME = Path("library/task-variants")

#: Materialized packages live under this derived-root-relative directory
#: (zone 03 storage; rebuildable, gitignored with all of ``derived/``).
VARIANTS_STORE_DIRNAME = Path("task-store/variants")

#: Hard inline-content ceiling for one changed file in the git-tracked record.
MAX_INLINE_FILE_BYTES = 1024 * 1024

SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
TRANSFORM_PATTERN = r"^[a-z0-9][a-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*$"

COMPONENT_KEYS = ("task_toml", "instruction", "environment", "verifier", "solution")

TaskVariantStatus = Literal["candidate", "validated", "rejected"]


class VariantError(ValueError):
    """Base error for task-variant derivation and verification."""


class VariantInvalid(VariantError):
    """Raised when a derivation request violates the variant rules."""


class VariantExistsError(VariantError):
    """Raised when a record or package already exists (never overwrite)."""


class LineageError(VariantError):
    """Raised when a lineage chain is broken, cyclic, or unresolvable."""


def _validate_transform(value: str) -> None:
    if not re.fullmatch(TRANSFORM_PATTERN, value):
        raise VariantInvalid(f"transform must be name@version, got {value!r}")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _task_slug(task_name: str) -> str:
    """Filesystem-safe one-level slug from a task name like ``mimo-v2.6-rl/x``."""
    return task_name.strip("/").replace("/", "__") or "task"


def _digest12(digest: str) -> str:
    return digest.split(":", 1)[1][:12]


def _sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


# --------------------------------------------------------------------------- #
# Record schema
# --------------------------------------------------------------------------- #


class TaskVariantParentSource(ContractModel):
    """Where the parent package came from; exactly one recognized shape."""

    kind: Literal["hf", "variant", "local"]
    repo: str | None = None
    revision: str | None = None
    path: str | None = None
    record: str | None = None

    @model_validator(mode="after")
    def kind_fields(self) -> TaskVariantParentSource:
        if self.kind == "hf" and not (self.repo and self.revision and self.path):
            raise ValueError("hf parent source requires repo, revision and path")
        if self.kind == "variant" and not self.record:
            raise ValueError("variant parent source requires record")
        if self.kind == "local" and not self.path:
            raise ValueError("local parent source requires path")
        return self


class TaskVariantParent(ContractModel):
    digest: str = Field(pattern=SHA256_PATTERN)
    harbor_digest: str = Field(pattern=SHA256_PATTERN)
    source: TaskVariantParentSource


class TaskVariantFileChange(ContractModel):
    """One changed file: before/after digests and the after-bytes inline."""

    path: str = Field(min_length=1)
    before_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    after_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    content: str | None = None

    @model_validator(mode="after")
    def consistent(self) -> TaskVariantFileChange:
        if self.before_sha256 is not None and self.before_sha256 == self.after_sha256:
            raise ValueError(f"no-op change for {self.path!r}")
        if (self.after_sha256 is None) != (self.content is None):
            raise ValueError(
                f"content is inlined iff the file exists after the change: {self.path!r}"
            )
        return self


class TaskVariantEvidence(ContractModel):
    """Append-only validation evidence; never rewrites record identity."""

    at: str = Field(min_length=1)
    by: str = Field(min_length=1)
    status: TaskVariantStatus
    evidence: str = Field(min_length=1)


class VariantRecord(ContractModel):
    """One derived task version; the durable ``evallab.task_variant/v1`` record."""

    schema_: str = Field(alias="schema", pattern=r"^evallab\.task_variant/v1$")
    task_name: str = Field(min_length=1)
    variant_digest: str = Field(pattern=SHA256_PATTERN)
    variant_harbor_digest: str = Field(pattern=SHA256_PATTERN)
    parent: TaskVariantParent
    transform: str = Field(pattern=TRANSFORM_PATTERN)
    components_changed: list[ComponentKey] = Field(min_length=1)
    files: list[TaskVariantFileChange] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    created_by: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    status: TaskVariantStatus = "candidate"
    evidence: list[TaskVariantEvidence] = Field(default_factory=list)

    model_config = {**ContractModel.model_config, "populate_by_name": True}

    @field_validator("files")
    @classmethod
    def unique_paths(cls, value: list[TaskVariantFileChange]) -> list[TaskVariantFileChange]:
        seen: set[str] = set()
        for change in value:
            if change.path in seen:
                raise ValueError(f"duplicate file change for {change.path!r}")
            seen.add(change.path)
        return value

    @property
    def digest12(self) -> str:
        return _digest12(self.variant_digest)

    @property
    def task_slug(self) -> str:
        return _task_slug(self.task_name)

    def record_relpath(self) -> Path:
        return RECORDS_DIRNAME / self.task_slug / f"{self.digest12}.json"


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VariantLocations:
    """Resolved paths for one variant's record and package."""

    record_path: Path
    package_dir: Path


def _resolve_repo_root(repo_root: Path | str | None) -> Path:
    return Path(repo_root).resolve() if repo_root is not None else Path.cwd().resolve()


def _under(base: Path, relative: Path | str) -> Path:
    relative = Path(relative)
    return relative if relative.is_absolute() else base / relative


def _locations(
    task_name: str,
    variant_digest: str,
    *,
    repo_root: Path,
    records_dir: Path,
    variants_root: Path,
) -> VariantLocations:
    slug = _task_slug(task_name)
    digest12 = _digest12(variant_digest)
    return VariantLocations(
        record_path=_under(repo_root, records_dir) / slug / f"{digest12}.json",
        package_dir=variants_root / slug / digest12,
    )


def default_variants_root(repo_root: Path) -> Path:
    """Materialized-variant store: shared across worktrees, outside Git.

    Resolves relative to the *primary* checkout's ``derived/`` directory (not
    the per-worktree tree, and not the shared Parquet root), so every worktree
    materializes into the one store the lineage records describe.
    """
    return shared_checkout_root(repo_root) / "derived" / VARIANTS_STORE_DIRNAME


def _resolve_store_roots(
    repo_root: Path | str | None,
    records_dir: Path | str,
    variants_root: Path | str | None,
) -> tuple[Path, Path, Path]:
    """Resolve (repo root, records dir, variants store) for one operation."""
    root = _resolve_repo_root(repo_root)
    records = _under(root, records_dir)
    variants = (
        _under(root, variants_root)
        if variants_root is not None
        else default_variants_root(root)
    )
    return root, records, variants


# --------------------------------------------------------------------------- #
# Derivation
# --------------------------------------------------------------------------- #


def _relative_member_path(relative: str, *, label: str) -> Path:
    candidate = Path(relative)
    if (
        candidate.is_absolute()
        or not relative
        or relative in (".", "..")
        or ".." in candidate.parts
        or candidate.drive
        or candidate.root
        or candidate.as_posix() != relative
    ):
        raise VariantInvalid(f"{label} path escapes the task package: {relative!r}")
    return candidate


def _check_no_symlinks(root: Path) -> None:
    for member in root.rglob("*"):
        if member.is_symlink():
            raise VariantInvalid(
                f"parent package contains a symlink: {member.relative_to(root).as_posix()!r}"
            )


def _check_change_inputs(
    changes: Mapping[str, bytes | None], parent_dir: Path
) -> dict[str, bytes | None]:
    if not changes:
        raise VariantInvalid("no changes requested; a variant must differ from its parent")
    normalized: dict[str, bytes | None] = {}
    for relative, payload in changes.items():
        member = _relative_member_path(relative, label="changed file")
        key = member.as_posix()
        if key in normalized:
            raise VariantInvalid(f"duplicate changed path: {key!r}")
        target = parent_dir / member
        if payload is None:
            if not target.exists():
                raise VariantInvalid(f"cannot delete a file the parent does not have: {key!r}")
            if target.is_dir():
                raise VariantInvalid(f"cannot delete a directory; delete its files: {key!r}")
        else:
            if len(payload) > MAX_INLINE_FILE_BYTES:
                raise VariantInvalid(
                    f"changed file exceeds the {MAX_INLINE_FILE_BYTES} byte inline limit: {key!r}"
                )
            try:
                payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise VariantInvalid(
                    "changed file is not valid UTF-8 text (binary files cannot be "
                    f"inlined): {key!r}"
                ) from exc
            if target.is_file() and target.read_bytes() == payload:
                raise VariantInvalid(
                    f"no-op change (bytes identical to the parent): {key!r}"
                )
            if target.is_dir():
                raise VariantInvalid(f"changed path is a directory in the parent: {key!r}")
        normalized[key] = payload
    return normalized


def _apply_changes(
    parent_dir: Path,
    changes: Mapping[str, bytes | None],
    destination: Path,
) -> list[TaskVariantFileChange]:
    """Copy the parent package and apply changes; return the file-change rows."""
    if destination.exists():
        raise VariantExistsError(f"destination already exists: {destination}")
    destination.mkdir(parents=True)
    shutil.copytree(parent_dir, destination, dirs_exist_ok=True, symlinks=False)
    # Pinned parent stores (``derived/task-store/hf``) are read-only and
    # copytree keeps their modes; the copy is the variant's to change.
    for path in (destination, *destination.rglob("*")):
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    rows: list[TaskVariantFileChange] = []
    for key in sorted(changes):
        payload = changes[key]
        target = destination / key
        parent_file = parent_dir / key
        before_bytes = parent_file.read_bytes() if parent_file.is_file() else None
        before_sha = _sha256_bytes(before_bytes) if before_bytes is not None else None
        if payload is None:
            if not target.exists():
                raise VariantError(f"record deletes a file the parent does not have: {key!r}")
            target.unlink()
            rows.append(
                TaskVariantFileChange(
                    path=key, before_sha256=before_sha, after_sha256=None, content=None
                )
            )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            rows.append(
                TaskVariantFileChange(
                    path=key,
                    before_sha256=before_sha,
                    after_sha256=_sha256_bytes(payload),
                    content=payload.decode("utf-8"),
                )
            )
    return rows


def _task_name_of(task_dir: Path, fallback_parent: Path) -> str:
    try:
        data = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return fallback_parent.name
    name = data.get("task", {}).get("name") if isinstance(data.get("task"), dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else fallback_parent.name


def _classify_components(
    parent_dir: Path, variant_dir: Path, changes: Sequence[TaskVariantFileChange]
) -> list[ComponentKey]:
    """Components whose digest changed, via ``registry.compute_task_digests``.
    Falls back to classifying the changed paths when either package has no
    ``task.toml`` (``compute_task_digests`` requires one).
    """
    try:
        parent_digests = compute_task_digests(parent_dir)
        variant_digests = compute_task_digests(variant_dir)
    except ValueError:
        buckets = {
            "task_toml": ("task.toml",),
            "instruction": ("instruction.md", "instructions.md"),
            "environment": ("environment",),
            "verifier": ("tests", "verifier"),
            "solution": ("solution",),
        }
        changed: set[str] = set()
        for change in changes:
            top = Path(change.path).parts[0] if Path(change.path).parts else ""
            for component, keys in buckets.items():
                if top in keys or (
                    component == "task_toml" and change.path == "task.toml"
                ):
                    changed.add(component)
        return [key for key in COMPONENT_KEYS if key in changed]
    changed: set[str] = {
        key
        for key in ("task_toml", "instruction", "environment", "verifier")
        if getattr(parent_digests, key) != getattr(variant_digests, key)
    }
    if compute_subpath_digest(parent_dir / "solution") != compute_subpath_digest(
        variant_dir / "solution"
    ):
        changed.add("solution")
    return [key for key in COMPONENT_KEYS if key in changed]


def _record_bytes(record: VariantRecord) -> bytes:
    payload = record.model_dump(mode="json", by_alias=True)
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def derive_task(
    parent_dir: Path | str,
    *,
    changes: dict[str, bytes | None],
    transform: str,
    rationale: str,
    created_by: str,
    inputs: dict[str, Any] | None = None,
    parent_source: dict[str, Any] | None = None,
    repo_root: Path | str | None = None,
    records_dir: Path | str = RECORDS_DIRNAME,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive a variant package plus its git-tracked lineage record.

    Materializes into the shared variants store (never a worktree), writes
    ``library/task-variants/<slug>/<digest12>.json``, and refuses to overwrite
    either. The record pins the variant digest; ``materialize`` must be able
    to reproduce it from the parent bytes plus the record.
    """
    _validate_transform(transform)
    parent = Path(parent_dir).resolve()
    if not parent.is_dir():
        raise VariantInvalid(f"parent task directory is missing: {parent}")
    _check_no_symlinks(parent)
    normalized = _check_change_inputs(changes, parent)
    root, records_path, variants_store = _resolve_store_roots(
        repo_root, Path(records_dir), variants_root
    )

    with tempfile.TemporaryDirectory(prefix="evallab-variant-") as scratch:
        staged = Path(scratch) / "variant"
        rows = _apply_changes(parent, normalized, staged)
        variant_digest = task_directory_digest(staged)
        parent_digest = task_directory_digest(parent)
        if variant_digest == parent_digest:
            raise VariantInvalid(
                "no-op derivation: variant digest equals parent digest; nothing changed"
            )
        task_name = _task_name_of(staged, parent)
        locations = _locations(
            task_name,
            variant_digest,
            repo_root=root,
            records_dir=Path(records_dir),
            variants_root=variants_store,
        )
        if locations.record_path.exists():
            raise VariantExistsError(f"lineage record already exists: {locations.record_path}")
        if locations.package_dir.exists():
            raise VariantExistsError(f"variant package already exists: {locations.package_dir}")
        record = VariantRecord(
            schema=VARIANT_RECORD_SCHEMA,
            task_name=task_name,
            variant_digest=variant_digest,
            variant_harbor_digest=harbor_task_digest(staged),
            parent=TaskVariantParent(
                digest=parent_digest,
                harbor_digest=harbor_task_digest(parent),
                source=TaskVariantParentSource.model_validate(
                    parent_source
                    if parent_source is not None
                    else {"kind": "local", "path": str(parent)}
                ),
            ),
            transform=transform,
            components_changed=_classify_components(parent, staged, rows),
            files=rows,
            rationale=rationale,
            inputs=dict(inputs or {}),
            created_by=created_by,
            created_at=_utc_now_iso(),
        )
        locations.package_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(locations.package_dir))
    try:
        _write_atomic(locations.record_path, _record_bytes(record))
    except BaseException:
        shutil.rmtree(locations.package_dir, ignore_errors=True)
        raise
    return record


# --------------------------------------------------------------------------- #
# Materialization and verification
# --------------------------------------------------------------------------- #


def _content_bytes(change: TaskVariantFileChange) -> bytes:
    if change.content is None:
        raise VariantError(f"recorded change for {change.path!r} has no inline content")
    payload = change.content.encode("utf-8")
    if change.after_sha256 is not None and _sha256_bytes(payload) != change.after_sha256:
        raise VariantError(
            f"inline content digest mismatch for {change.path!r}: "
            f"{_sha256_bytes(payload)} != {change.after_sha256}"
        )
    return payload


def _changes_of(record: VariantRecord) -> dict[str, bytes | None]:
    return {
        change.path: (None if change.after_sha256 is None else _content_bytes(change))
        for change in record.files
    }


def materialize(
    record: VariantRecord | Path | str,
    parent_dir: Path | str,
    *,
    repo_root: Path | str | None = None,
    records_dir: Path | str = RECORDS_DIRNAME,
    variants_root: Path | str | None = None,
) -> Path:
    """Rebuild the variant package from parent bytes plus the record's files.

    Proves the record's invariant: the rebuilt digest must equal
    ``record.variant_digest``. Refuses to overwrite an existing package that
    does not match; returning an existing, digest-matching package is a no-op
    so callers can rebuild after a store cleanup.
    """
    resolved = resolve_record(record, repo_root=repo_root, records_dir=records_dir)
    root, records_path, variants_store = _resolve_store_roots(
        repo_root, Path(records_dir), variants_root
    )
    locations = _locations(
        resolved.task_name,
        resolved.variant_digest,
        repo_root=root,
        records_dir=Path(records_dir),
        variants_root=variants_store,
    )
    if locations.package_dir.exists():
        if task_directory_digest(locations.package_dir) == resolved.variant_digest:
            return locations.package_dir
        raise VariantExistsError(
            f"variant package exists with a different digest: {locations.package_dir}"
        )
    parent = Path(parent_dir).resolve()
    if not parent.is_dir():
        raise LineageError(f"parent task directory is missing: {parent}")
    if task_directory_digest(parent) != resolved.parent.digest:
        raise VariantError(f"parent bytes do not match the record's parent digest: {parent}")
    changes = _changes_of(resolved)
    with tempfile.TemporaryDirectory(prefix="evallab-variant-") as scratch:
        staged = Path(scratch) / "variant"
        rows = _apply_changes(parent, changes, staged)
        rebuilt_digest = task_directory_digest(staged)
        if rebuilt_digest != resolved.variant_digest:
            raise VariantError(
                "materialization does not reproduce the recorded variant digest "
                f"({rebuilt_digest} != {resolved.variant_digest})"
            )
        recorded_by_path = sorted(resolved.files, key=lambda item: item.path)
        for row, recorded in zip(rows, recorded_by_path, strict=True):
            if (row.before_sha256, row.after_sha256) != (
                recorded.before_sha256,
                recorded.after_sha256,
            ):
                raise VariantError(
                    f"file change does not reproduce the record for {recorded.path!r}"
                )
        locations.package_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(locations.package_dir))
    return locations.package_dir


def _parent_package_dir(
    record: VariantRecord,
    *,
    repo_root: Path,
    records_dir: Path,
    variants_root: Path,
) -> Path:
    source = record.parent.source
    if source.kind == "variant":
        parent_record = _load_parent_record(
            record, repo_root=repo_root, records_dir=records_dir
        )
        if parent_record is None:
            raise LineageError(
                f"parent lineage record missing or mismatched: {source.record}"
            )
        return _locations(
            parent_record.task_name,
            parent_record.variant_digest,
            repo_root=repo_root,
            records_dir=records_dir,
            variants_root=variants_root,
        ).package_dir
    if source.path is not None:
        return Path(source.path)
    raise LineageError(f"cannot resolve parent package for {record.task_name}")


def verify(
    record: VariantRecord | Path | str,
    *,
    parent_dir: Path | str | None = None,
    repo_root: Path | str | None = None,
    records_dir: Path | str = RECORDS_DIRNAME,
    variants_root: Path | str | None = None,
) -> list[str]:
    """Verify a record against bytes; return the list of failures (empty = valid).

    Checks the parent digest, per-file before/after digests, and the variant
    package digest. ``parent_dir`` overrides the record's parent location.
    """
    resolved = resolve_record(record, repo_root=repo_root, records_dir=records_dir)
    root, _records_path, variants_store = _resolve_store_roots(
        repo_root, Path(records_dir), variants_root
    )
    failures: list[str] = []
    parent = (
        Path(parent_dir).resolve()
        if parent_dir is not None
        else _parent_package_dir(
            resolved, repo_root=root, records_dir=Path(records_dir), variants_root=variants_store
        )
    )
    if not parent.is_dir():
        return [f"parent package missing: {parent}"]
    if task_directory_digest(parent) != resolved.parent.digest:
        failures.append(
            f"parent digest mismatch: bytes {task_directory_digest(parent)} "
            f"!= record {resolved.parent.digest}"
        )
    for change in resolved.files:
        before = parent / change.path
        before_sha = _sha256_bytes(before.read_bytes()) if before.is_file() else None
        if before_sha != change.before_sha256:
            failures.append(
                f"parent file digest mismatch for {change.path!r}: "
                f"{before_sha} != {change.before_sha256}"
            )
        if change.after_sha256 is None:
            continue
        try:
            _content_bytes(change)
        except VariantError as exc:
            failures.append(str(exc))
    locations = _locations(
        resolved.task_name,
        resolved.variant_digest,
        repo_root=root,
        records_dir=Path(records_dir),
        variants_root=variants_store,
    )
    if not locations.package_dir.is_dir():
        failures.append(f"variant package missing: {locations.package_dir}")
    else:
        package_digest = task_directory_digest(locations.package_dir)
        if package_digest != resolved.variant_digest:
            failures.append(
                f"variant digest mismatch: package {package_digest} "
                f"!= record {resolved.variant_digest}"
            )
    return failures


# --------------------------------------------------------------------------- #
# Loading and lineage walking
# --------------------------------------------------------------------------- #


def resolve_record(
    record: VariantRecord | Path | str,
    *,
    repo_root: Path | str | None = None,
    records_dir: Path | str = RECORDS_DIRNAME,
) -> VariantRecord:
    """Resolve a record from a model, an absolute path, or a repo-relative path."""
    if isinstance(record, VariantRecord):
        return record
    path = _record_path_input(record, repo_root=repo_root, records_dir=records_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LineageError(f"lineage record is unreadable: {path} ({exc})") from exc
    try:
        return VariantRecord.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - surfaced as a lineage failure with context
        raise LineageError(f"lineage record is invalid ({path}): {exc}") from exc


def _record_path_input(
    value: Path | str,
    *,
    repo_root: Path | str | None,
    records_dir: Path | str,
) -> Path:
    path = Path(value)
    if path.is_file():
        return path
    if not path.is_absolute():
        root, _records_path, _variants = _resolve_store_roots(
            repo_root, Path(records_dir), None
        )
        candidate = root / path
        if candidate.is_file():
            return candidate
    raise LineageError(f"lineage record not found: {value}")


def load_records(
    root: Path | str,
    *,
    records_dir: Path | str = RECORDS_DIRNAME,
) -> list[VariantRecord]:
    """Load every strict ``evallab.task_variant/v1`` record under a repo root.

    Raises ``LineageError`` on the first invalid record: lineage is fail-closed.
    """
    base = Path(root)
    tree = _under(base, records_dir)
    if not tree.is_dir():
        return []
    records: list[VariantRecord] = []
    for path in sorted(tree.rglob("*.json")):
        if path.is_file():
            records.append(resolve_record(path, repo_root=base, records_dir=records_dir))
    return records


def load_variant_records(
    root: Path | str, *, records_dir: Path | str = RECORDS_DIRNAME
) -> list[dict[str, Any]]:
    """Loader for catalog builders: the same records as plain JSON dicts."""
    return [
        record.model_dump(mode="json", by_alias=True)
        for record in load_records(root, records_dir=records_dir)
    ]


@dataclass(frozen=True)
class LineageStep:
    """One node in a lineage chain, newest first."""

    record: VariantRecord
    record_path: Path
    depth: int
    parent_found: bool
    parent_error: str | None = None


def _record_path_for(record: VariantRecord, records_path: Path) -> Path:
    return records_path / record.task_slug / f"{record.digest12}.json"


def _load_parent_record(
    record: VariantRecord,
    *,
    repo_root: Path,
    records_dir: Path,
) -> VariantRecord | None:
    source = record.parent.source
    if source.kind != "variant" or source.record is None:
        return None
    candidate = _under(repo_root, Path(source.record))
    if not candidate.is_file():
        return None
    try:
        parent = resolve_record(candidate, repo_root=repo_root, records_dir=records_dir)
    except LineageError:
        return None
    if parent.variant_digest != record.parent.digest:
        return None
    return parent


def lineage_chain(
    digest_or_record: VariantRecord | Path | str,
    *,
    repo_root: Path | str | None = None,
    records_dir: Path | str = RECORDS_DIRNAME,
    max_depth: int = 64,
) -> list[LineageStep]:
    """Walk parents back to the original package, newest first.

    Detects cycles (a repeated digest short-circuits with an error step) and
    missing parents (a final step with ``parent_found=False``).
    """
    records_arg = Path(records_dir)
    root, records_path, _variants = _resolve_store_roots(repo_root, records_arg, None)
    start = _resolve_start(digest_or_record, repo_root=root, records_dir=records_arg)
    steps: list[LineageStep] = []
    seen: set[str] = set()
    current: VariantRecord | None = start
    depth = 0
    while current is not None:
        path = _record_path_for(current, records_path)
        if current.variant_digest in seen or depth >= max_depth:
            reason = (
                f"lineage cycle detected at {current.variant_digest}"
                if current.variant_digest in seen
                else f"lineage depth exceeded {max_depth}"
            )
            steps.append(
                LineageStep(
                    record=current, record_path=path, depth=depth, parent_found=False,
                    parent_error=reason,
                )
            )
            break
        seen.add(current.variant_digest)
        steps.append(LineageStep(record=current, record_path=path, depth=depth, parent_found=True))
        parent_record = _load_parent_record(current, repo_root=root, records_dir=records_arg)
        if parent_record is None:
            if current.parent.source.kind == "variant":
                steps.append(
                    LineageStep(
                        record=current,
                        record_path=path,
                        depth=depth,
                        parent_found=False,
                        parent_error=(
                            f"missing parent lineage record: {current.parent.source.record}"
                        ),
                    )
                )
            break
        current = parent_record
        depth += 1
    return steps


def _resolve_start(
    value: VariantRecord | Path | str,
    *,
    repo_root: Path,
    records_dir: Path | str,
) -> VariantRecord:
    if isinstance(value, VariantRecord):
        return value
    text = str(value)
    if text.startswith("sha256:") or re.fullmatch(r"[0-9a-f]{12}", text):
        for record in load_records(repo_root, records_dir=records_dir):
            if record.variant_digest == text or record.digest12 == text:
                return record
        raise LineageError(f"no lineage record found for variant digest {text}")
    path = Path(text)
    if path.is_dir():
        digest = task_directory_digest(path)
        return _resolve_start(digest, repo_root=repo_root, records_dir=records_dir)
    return resolve_record(path, repo_root=repo_root, records_dir=records_dir)


# --------------------------------------------------------------------------- #
# Status transitions
# --------------------------------------------------------------------------- #

_STATUS_FINALITY: dict[str, int] = {"candidate": 0, "validated": 1, "rejected": 1}


def append_status_evidence(
    record: VariantRecord | Path | str,
    status: TaskVariantStatus,
    *,
    evidence: str,
    by: str,
    repo_root: Path | str | None = None,
    records_dir: Path | str = RECORDS_DIRNAME,
) -> VariantRecord:
    """Append validation evidence and update ``status`` without rewriting history.

    Identity fields are immutable: only ``status`` and the append-only
    ``evidence`` list change. A ``validated`` or ``rejected`` verdict cannot
    be reversed — a new decision needs a new variant derived from the same
    parent, so scores stay attributable to one exact package.
    """
    root, records_path, _variants = _resolve_store_roots(
        repo_root, Path(records_dir), None
    )
    if isinstance(record, VariantRecord):
        path = _record_path_for(record, records_path)
    else:
        target = _resolve_start(record, repo_root=root, records_dir=records_dir)
        path = _record_path_for(target, records_path)
    resolved = resolve_record(path, repo_root=root, records_dir=records_dir)
    if resolved.status != "candidate" and status != resolved.status:
        raise VariantError(
            f"record {path.name} is already {resolved.status!r}; "
            "validated/rejected verdicts are final"
        )
    updated = resolved.model_copy(
        update={
            "status": status,
            "evidence": [
                *resolved.evidence,
                TaskVariantEvidence(at=_utc_now_iso(), by=by, status=status, evidence=evidence),
            ],
        }
    )
    _write_atomic(path, _record_bytes(updated))
    return updated


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _short_digest(value: str | None) -> str:
    if value is None:
        return "absent"
    return value.split(":", 1)[1][:12]


def render_lineage(steps: Sequence[LineageStep]) -> str:
    """Human-readable chain, newest first, with per-file digest changes."""
    lines: list[str] = []
    for step in steps:
        record = step.record
        indent = "  " * step.depth
        lines.append(f"{indent}{record.task_name} [{record.transform}] status={record.status}")
        lines.append(f"{indent}  variant: {record.variant_digest}")
        lines.append(f"{indent}  record:  {step.record_path}")
        source = record.parent.source
        if source.kind == "hf":
            revision = (source.revision or "")[:12]
            lines.append(
                f"{indent}  parent:  {record.parent.digest} <- "
                f"{source.repo}@{revision}:{source.path}"
            )
        elif source.kind == "variant":
            lines.append(
                f"{indent}  parent:  {record.parent.digest} <- variant {source.record}"
            )
        else:
            lines.append(f"{indent}  parent:  {record.parent.digest} <- local {source.path}")
        lines.append(f"{indent}  components_changed: {', '.join(record.components_changed)}")
        for change in record.files:
            after = "deleted" if change.after_sha256 is None else _short_digest(change.after_sha256)
            lines.append(
                f"{indent}    {change.path}: {_short_digest(change.before_sha256)} -> {after}"
            )
        if step.parent_error:
            lines.append(f"{indent}  ERROR: {step.parent_error}")
    if steps and not steps[-1].parent_error:
        last = steps[-1].record.parent.source
        if last.kind != "variant":
            lines.append("chain terminates at the original (non-derived) parent package")
    return "\n".join(lines)


__all__ = [
    "COMPONENT_KEYS",
    "MAX_INLINE_FILE_BYTES",
    "RECORDS_DIRNAME",
    "VARIANT_RECORD_SCHEMA",
    "VARIANTS_STORE_DIRNAME",
    "LineageError",
    "LineageStep",
    "TaskVariantEvidence",
    "TaskVariantFileChange",
    "TaskVariantParent",
    "TaskVariantParentSource",
    "VariantError",
    "VariantExistsError",
    "VariantInvalid",
    "VariantLocations",
    "VariantRecord",
    "append_status_evidence",
    "default_variants_root",
    "derive_task",
    "lineage_chain",
    "load_records",
    "load_variant_records",
    "materialize",
    "render_lineage",
    "resolve_record",
    "verify",
]
