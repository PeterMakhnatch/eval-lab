"""Metadata-only Hugging Face history feasibility probes, not fix evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "fixmine-hf/v1"


def _json_value(value: Any) -> Any:
    """Preserve public API metadata without depending on Hub implementation types."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if hasattr(value, "__dict__"):
        return {
            key: _json_value(item) for key, item in vars(value).items() if not key.startswith("_")
        }
    raise TypeError(f"unsupported Hub metadata type: {type(value).__name__}")


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _has_field(value: Any, name: str) -> bool:
    return name in value if isinstance(value, Mapping) else hasattr(value, name)


def _encoded(value: Any) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def _persist(root: Path, directory: Path, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Publish content-addressed bytes atomically without replacing existing evidence."""
    data = _encoded(payload)
    digest = hashlib.sha256(data).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{kind}-{digest}.json"
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".metadata-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.close()
            temporary.chmod(0o444)
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise ValueError(f"immutable metadata digest conflict at {path}") from None
                path.chmod(0o444)
        finally:
            temporary.unlink(missing_ok=True)
    return {"path": path.relative_to(root).as_posix(), "sha256": digest, "bytes": len(data)}


def _error(stage: str, exc: Exception) -> dict[str, Any]:
    """Do not persist exception text, which can contain credentials or request IDs."""
    name = type(exc).__name__
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if "gated" in name.lower():
        reason = "gated_dataset"
    elif status == 401:
        reason = "authentication_required"
    elif status == 403:
        reason = "gated_or_forbidden"
    elif status == 404:
        reason = "not_found_or_private"
    elif isinstance(exc, ImportError):
        reason = "huggingface_hub_unavailable"
    else:
        reason = "metadata_request_failed"
    return {"stage": stage, "reason": reason, "exception_type": name, "http_status": status}


def probe_hf_history(dataset_id: str, state_root: Path, *, api: Any = None) -> dict[str, Any]:
    """Persist a pinned, metadata-only dataset history and its feasibility report.

    ``state_root`` is the Failure Library state root, not the ``hf`` directory.
    Only commit, filename and optional LFS metadata are requested; dataset blobs
    and task rows are never downloaded. Inject ``api`` to avoid external state.
    Raw metadata lives under ``raw/hf/<dataset-slug>/<pinned-revision>/``;
    feasibility reports live separately under ``fixmine/hf/<dataset-slug>/``.
    """
    root = Path(state_root).expanduser()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "--", dataset_id).strip(".-") or "dataset"
    identity = hashlib.sha256(dataset_id.encode("utf-8")).hexdigest()[:12]
    directory = root / "fixmine" / "hf" / f"{slug}-{identity}"
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repo_id": dataset_id,
        "repo_type": "dataset",
        "status": "unavailable",
        "revision": None,
        "sha": None,
        "commit_count": 0,
        "revisions": [],
        "file_count": None,
        "parquet_file_count": None,
        "lfs_file_count": None,
        "has_parquet": None,
        "has_lfs": None,
        "gated": None,
        "task_level_diff_feasible": False,
        "metadata": {},
        "errors": [],
        "limitations": [
            "Commit titles and messages are untrusted metadata, not proof of a task fix.",
            "No dataset contents were downloaded or compared; no task-level fixes are established.",
        ],
    }

    def finish() -> dict[str, Any]:
        artifact = _persist(root, directory, "report", report)
        return {**report, "report_ref": artifact["path"], "report_sha256": artifact["sha256"]}

    if api is None:
        try:
            from huggingface_hub import HfApi

            api = HfApi()
        except ImportError as exc:
            report["errors"].append(_error("client", exc))
            return finish()

    try:
        commits = list(api.list_repo_commits(dataset_id, repo_type="dataset"))
        raw_commits = [_json_value(commit) for commit in commits]
    except Exception as exc:
        report["errors"].append(_error("commits", exc))
        return finish()

    # The Hub returns newest-first history. Keep its observed tip as the pin,
    # while sorting persisted metadata independently of transport ordering.
    revisions = [str(_field(commit, "id", _field(commit, "commit_id", ""))) for commit in commits]
    revision = revisions[0] if revisions and revisions[0] else None
    report.update(
        status="available",
        revision=revision,
        sha=revision,
        commit_count=len(commits),
        revisions=sorted({item for item in revisions if item}),
    )
    if revision is None:
        report["limitations"].append(
            "No commit revision was returned; a file inventory cannot be pinned."
        )
        return finish()
    pin = {"repo_id": dataset_id, "repo_type": "dataset", "revision": revision, "sha": revision}
    raw_directory = root / "raw" / "hf" / directory.name / revision
    report["metadata"]["commits"] = _persist(
        root,
        raw_directory,
        "commits",
        {
            **pin,
            "requested_revision": None,
            "commits": sorted(raw_commits, key=_encoded),
        },
    )

    inventory: list[str] | None = None
    list_files = getattr(api, "list_repo_files", None)
    if callable(list_files):
        try:
            inventory = sorted(set(list_files(dataset_id, repo_type="dataset", revision=revision)))
            report["metadata"]["files"] = _persist(
                root, raw_directory, "files", {**pin, "files": inventory}
            )
        except Exception as exc:
            report["errors"].append(_error("files", exc))
    else:
        report["limitations"].append(
            "The metadata client does not provide a pinned filename listing."
        )

    siblings: list[Any] | None = None
    dataset_info = getattr(api, "dataset_info", None)
    if callable(dataset_info):
        try:
            info = dataset_info(dataset_id, revision=revision, files_metadata=True)
            resolved = _field(info, "sha", revision)
            report["metadata"]["dataset"] = _persist(
                root,
                raw_directory,
                "dataset",
                {**pin, "resolved_sha": resolved, "dataset": _json_value(info)},
            )
            if resolved != revision:
                report["errors"].append(
                    {"stage": "dataset", "reason": "resolved_revision_mismatch", "sha": resolved}
                )
            else:
                report["gated"] = _json_value(_field(info, "gated"))
                raw_siblings = _field(info, "siblings")
                if raw_siblings is not None:
                    siblings = list(raw_siblings)
        except Exception as exc:
            report["errors"].append(_error("dataset", exc))

    if inventory is None and siblings is not None:
        names = [_field(sibling, "rfilename") for sibling in siblings]
        if all(isinstance(name, str) for name in names):
            inventory = sorted(set(names))
    if inventory is not None:
        parquet_files = [name for name in inventory if name.lower().endswith(".parquet")]
        report.update(
            file_count=len(inventory),
            parquet_file_count=len(parquet_files),
            has_parquet=bool(parquet_files),
        )
        report["limitations"].append(
            "The file inventory covers only the pinned revision, not every historical revision."
        )
    else:
        report["limitations"].append(
            "Pinned filenames are unavailable; data formats remain unknown."
        )

    if siblings is not None and all(_has_field(sibling, "lfs") for sibling in siblings):
        lfs_count = sum(_field(sibling, "lfs") is not None for sibling in siblings)
        report.update(lfs_file_count=lfs_count, has_lfs=bool(lfs_count))
    else:
        report["limitations"].append(
            "LFS metadata is unavailable; filename extensions alone do not prove LFS storage."
        )
    if report["has_parquet"]:
        report["limitations"].append(
            "Parquet changes require decoding snapshots and aligning stable task/row IDs across "
            "revisions; commit metadata cannot expose row-level diffs."
        )
    if report["has_lfs"]:
        report["limitations"].append(
            "LFS object metadata identifies data blobs, not changed tasks; row-level comparison "
            "requires separately fetching the pinned snapshots."
        )
    if not report["has_parquet"] and not report["has_lfs"]:
        report["limitations"].append(
            "Task-level feasibility still requires inspecting content and task identifiers; "
            "a text filename or commit message alone is insufficient."
        )
    return finish()
