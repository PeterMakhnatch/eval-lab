"""Publish audit routing tags and export one dossier per audited package.

Tags are metadata-only variants. A ``keep`` verdict is a routing label, not
admission, and ``unknown`` stays ``verdict:unknown`` rather than becoming keep.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evallab.dataset_audit_contracts import AuditRecord
from evallab.dataset_audit_plugins import task_page_name
from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.task_dossier import dossier_for_audit
from evallab.task_health_tags import KNOWN_TAGS, SCHEMA, TRANSFORM, metadata_bytes
from evallab.task_variants import (
    TaskVariantParentSource,
    VariantError,
    VariantInvalid,
    VariantRecord,
    derive_task,
    load_records,
    materialize,
    verify,
)

MANIFEST_NAME = "task-health.json"
DOSSIER_INDEX_NAME = "dossiers.json"
_VERDICTS = ("keep", "fix", "discard", "unknown")


def _resolved_package(record: AuditRecord, repo_root: Path) -> Path | None:
    path = record.task.path
    if path is None:
        return None
    return path if path.is_absolute() else repo_root / path


def _projection_tags(record: AuditRecord) -> list[str]:
    """Routing tags only; a stored verdict:keep cannot override unknown."""
    tags = [f"verdict:{record.verdict}"]
    for tag in record.tags:
        if tag in KNOWN_TAGS and not tag.startswith("verdict:") and tag not in tags:
            tags.append(tag)
    return tags


def _description(record: AuditRecord) -> str | None:
    if record.verdict == "keep":
        return None
    reason = (record.verdict_reason or "").strip()
    if not reason:
        reason = f"audit verdict {record.verdict}; not a keep and not certification"
    return " ".join(reason.split())


def _parent_source(record: AuditRecord, parent: Path) -> dict[str, Any] | str:
    raw = dict(record.task.parent_source)
    if raw:
        if raw.get("kind") == "local":
            raw["path"] = str(parent.resolve())
        try:
            TaskVariantParentSource.model_validate(raw)
        except ValueError as exc:
            return f"parent source is not a recognized binding: {exc}"
        return raw
    return {"kind": "local", "path": str(parent)}


def _bind_parent(record: AuditRecord, repo_root: Path) -> tuple[Path, dict[str, Any]] | str:
    parent = _resolved_package(record, repo_root)
    if parent is None or not parent.is_dir():
        return "package path is absent; no metadata variant was derived"
    if record.task.package_digest is None or record.task.harbor_digest is None:
        return "package identity is unpinned; tags were not published"
    try:
        observed_package = task_directory_digest(parent)
        observed_harbor = harbor_task_digest(parent)
    except (OSError, ValueError) as exc:
        return f"package digest could not be read: {exc}"
    if record.task.package_digest != observed_package:
        return "package digest disagrees with the task bytes; tags were not published"
    if record.task.harbor_digest != observed_harbor:
        return "harbor digest disagrees with the task bytes; tags were not published"
    source = _parent_source(record, parent)
    if isinstance(source, str):
        return source
    return parent, source


def _manifest_row(record: AuditRecord, variant: VariantRecord, tags: list[str]) -> dict[str, Any]:
    return {
        "task_id": record.task.task_id,
        "dataset_id": record.task.dataset_id,
        "parent_digest": variant.parent.digest,
        "parent_harbor_digest": variant.parent.harbor_digest,
        "variant_digest": variant.variant_digest,
        "variant_harbor_digest": variant.variant_harbor_digest,
        "tags": tags,
        "verdict": record.verdict,
        "certification": False,
    }


def _publish_one(
    record: AuditRecord,
    *,
    repo_root: Path,
    output_dir: Path,
    records_dir: Path,
    variants_root: Path,
    by_parent: dict[tuple[str, str], list[VariantRecord]],
) -> dict[str, Any] | str:
    bound = _bind_parent(record, repo_root)
    if isinstance(bound, str):
        return bound
    parent, parent_source = bound
    tags = _projection_tags(record)
    description = _description(record)
    try:
        original = (parent / "task.toml").read_bytes()
        updated = metadata_bytes(original, tags, description=description)
        task_name = tomllib.loads(original.decode("utf-8")).get("task", {}).get("name", parent.name)
    except (OSError, ValueError, UnicodeError) as exc:
        return f"task metadata could not be projected: {exc}"
    parent_digest = task_directory_digest(parent)
    matches = [
        existing
        for existing in by_parent.get((str(task_name), parent_digest), [])
        if len(existing.files) == 1
        and existing.files[0].path == "task.toml"
        and existing.files[0].content == updated.decode("utf-8")
    ]
    try:
        if matches:
            variant = matches[0]
            materialize(
                variant,
                parent,
                repo_root=output_dir,
                records_dir=records_dir,
                variants_root=variants_root,
            )
            failures = verify(
                variant,
                repo_root=output_dir,
                records_dir=records_dir,
                variants_root=variants_root,
                parent_dir=parent,
            )
            if failures:
                raise VariantInvalid(f"existing audit tag variant is invalid: {failures}")
        elif updated == original:
            # The source already carries this projection. Bind it directly
            # instead of inventing a variant or editing the package.
            variant_digest = parent_digest
            variant_harbor = harbor_task_digest(parent)
            return {
                "task_id": record.task.task_id,
                "dataset_id": record.task.dataset_id,
                "parent_digest": parent_digest,
                "parent_harbor_digest": variant_harbor,
                "variant_digest": variant_digest,
                "variant_harbor_digest": variant_harbor,
                "tags": tags,
                "verdict": record.verdict,
                "certification": False,
                "reused": True,
            }
        else:
            variant = derive_task(
                parent,
                changes={"task.toml": updated},
                transform=TRANSFORM,
                rationale=(
                    "Harbor metadata projection of an audit routing verdict; "
                    "not admission, certification, or a new grade"
                ),
                created_by="har195-dataset-audit",
                inputs={
                    "assessment": {
                        "task_id": record.task.task_id,
                        "dataset_id": record.task.dataset_id,
                        "verdict": record.verdict,
                        "verdict_reason": record.verdict_reason,
                        "tags": tags,
                        "certification": False,
                    }
                },
                parent_source=parent_source,
                repo_root=output_dir,
                records_dir=records_dir,
                variants_root=variants_root,
            )
    except (OSError, VariantError, ValueError) as exc:
        return f"metadata variant was not published: {exc}"
    return _manifest_row(record, variant, tags)


def publish_audit_tags(
    records: Sequence[AuditRecord],
    *,
    repo_root: Path,
    output_dir: Path,
    variants_root: Path | None = None,
) -> Path:
    """Write a ``task-health-tags@1`` manifest and return its path.

    Source packages and jobs are not modified. Records whose package binding
    is missing or contradictory stay in ``unbound`` and are never tagged keep.
    """
    if not records:
        raise ValueError("audit tag publication needs at least one record")
    root = Path(repo_root).resolve()
    destination = Path(output_dir)
    if not destination.is_absolute():
        destination = root / destination
    destination = destination.resolve()
    store = Path(variants_root) if variants_root is not None else destination / "variants"
    if not store.is_absolute():
        store = root / store
    records_dir = Path("records")
    existing = load_records(destination, records_dir=records_dir)
    by_parent: dict[tuple[str, str], list[VariantRecord]] = {}
    for variant in existing:
        if variant.transform == TRANSFORM:
            by_parent.setdefault((variant.task_name, variant.parent.digest), []).append(variant)
    tasks: list[dict[str, Any]] = []
    unbound: list[dict[str, str]] = []
    for record in records:
        if record.verdict not in _VERDICTS:
            raise ValueError(f"unsupported audit verdict {record.verdict!r}")
        published = _publish_one(
            record,
            repo_root=root,
            output_dir=destination,
            records_dir=records_dir,
            variants_root=store.resolve(),
            by_parent=by_parent,
        )
        if isinstance(published, str):
            unbound.append({
                "task_id": record.task.task_id,
                "dataset_id": record.task.dataset_id,
                "verdict": record.verdict,
                "reason": published,
            })
        else:
            tasks.append(published)
    if not tasks:
        reasons = "; ".join(item["reason"] for item in unbound)
        raise ValueError(f"no audit tag variant could be bound: {reasons}")
    coverage = {verdict: 0 for verdict in _VERDICTS}
    for row in tasks:
        coverage[str(row["verdict"])] += 1
    coverage["unbound"] = len(unbound)
    manifest = {
        "schema": SCHEMA,
        "transform": TRANSFORM,
        "certification": False,
        "coverage": coverage,
        "tasks": tasks,
        "unbound": unbound,
    }
    path = destination / MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _facet_key(record: AuditRecord) -> str:
    from evallab.dataset_audit_plugins import normalize_task_id

    return normalize_task_id(record.task.task_id)


def _dossier_name(record: AuditRecord, used: set[str]) -> str:
    task = record.task
    identity = (
        task.dataset_id, task.source_uri, task.revision, task.task_id,
        task.package_digest, task.harbor_digest,
    )
    digest = hashlib.sha256(
        json.dumps(identity, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()[:16]
    name = f"{task_page_name(task.task_id)}-{digest}.json"
    if name in used:
        raise ValueError(f"duplicate audit dossier identity: {task.task_id}")
    used.add(name)
    return name


def _trial_facet(
    record: AuditRecord, trial_facets: Mapping[str, dict[str, Any]] | None
) -> dict[str, Any] | None:
    if trial_facets is None:
        return None
    return trial_facets.get(_facet_key(record))


def export_audit_dossiers(
    records: Sequence[AuditRecord],
    *,
    repo_root: Path,
    output_dir: Path,
    roots: Sequence[Path] | None = None,
    derived_root: Path | None = None,
    reader_store: Path | None = None,
    trial_facets: Mapping[str, dict[str, Any]] | None = None,
) -> Path:
    """Write one HAR-186 dossier per package and return the index path.

    ``trial_facets`` is a ``task_trials_for_tasks`` result keyed by canonical
    task id. When omitted, that census runs once. A missing key means history
    was not collected, not that the task has no runs. Versions do not share a
    filename.
    """
    from evallab.task_dossier_trials import task_trials_for_tasks

    destination = Path(output_dir)
    if not destination.is_absolute():
        destination = Path(repo_root) / destination
    folder = destination / "dossiers"
    folder.mkdir(parents=True, exist_ok=True)
    facets = trial_facets
    if facets is None and records:
        tasks = []
        seen: set[str] = set()
        for record in records:
            if record.task.task_id in seen:
                continue
            seen.add(record.task.task_id)
            tasks.append(record.task)
        facets = task_trials_for_tasks(
            tasks,
            repo_root=Path(repo_root),
            roots=roots,
            derived_root=derived_root,
            reader_store=reader_store,
        )
    used: set[str] = set()
    entries: list[dict[str, Any]] = []
    for record in records:
        name = _dossier_name(record, used)
        payload = dossier_for_audit(record, _trial_facet(record, facets))
        target = folder / name
        target.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        entries.append({
            "task_id": record.task.task_id,
            "dataset_id": record.task.dataset_id,
            "package_digest": record.task.package_digest,
            "harbor_digest": record.task.harbor_digest,
            "verdict": record.verdict,
            "path": str(target),
            "history": "collected" if _trial_facet(record, facets) is not None else "not_collected",
        })
    index = destination / DOSSIER_INDEX_NAME
    index.write_text(
        json.dumps(
            {"schema": "evallab.audit_dossiers/v1", "dossiers": entries},
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    return index
