"""Small built-in registry for dataset-specific audit evidence conventions."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from evallab.dataset_audit_contracts import (
    AuditDataset,
    AuditObservation,
    AuditRecord,
    AuditStage,
    AuditTask,
)


class DatasetAuditPlugin(Protocol):
    def recognizes_dataset(self, selector: str) -> bool: ...

    def recognizes_task(self, task_id: str) -> bool: ...

    def normalize_task_id(self, task_id: str) -> str: ...

    def task_aliases(self, task_id: str) -> list[str]: ...

    def task_page_name(self, task_id: str) -> str: ...

    def task_evidence(
        self, task_id: str, *, repo_root: Path, static_audit: Path | None = None
    ) -> dict[str, Any]: ...

    def read_stored_audit(
        self,
        repo_root: Path,
        *,
        stages: Sequence[AuditStage],
        static_audit: Path | None = None,
    ) -> tuple[AuditDataset, list[AuditRecord]]: ...

    def static_observation(self, task: AuditTask) -> AuditObservation: ...

    def finalize_record(self, record: AuditRecord) -> AuditRecord: ...


def _plugins() -> tuple[DatasetAuditPlugin, ...]:
    from evallab import audit_mimo

    return (audit_mimo,)


def dataset_plugin(selector: str) -> DatasetAuditPlugin | None:
    return next((plugin for plugin in _plugins() if plugin.recognizes_dataset(selector)), None)


def _validate_task_id(value: str) -> str:
    if (
        not value
        or value != value.strip()
        or not value.isprintable()
        or any(char in value for char in "\\*?[]")
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise ValueError(f"invalid task id {value!r}")
    return value


def normalize_task_id(value: str) -> str:
    _validate_task_id(value)
    for plugin in _plugins():
        if plugin.recognizes_task(value):
            return _validate_task_id(plugin.normalize_task_id(value))
    return value


def task_aliases(task_id: str) -> list[str]:
    canonical = normalize_task_id(task_id)
    for plugin in _plugins():
        if plugin.recognizes_task(canonical):
            return plugin.task_aliases(canonical)
    return [canonical]


def task_page_name(task_id: str) -> str:
    canonical = normalize_task_id(task_id)
    for plugin in _plugins():
        if plugin.recognizes_task(canonical):
            return plugin.task_page_name(canonical)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", canonical)[:72].strip(".-") or "task"
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:12]
    return f"task-{slug}-{digest}"


def task_evidence(
    task_id: str, *, repo_root: Path, static_audit: Path | None = None
) -> dict[str, Any]:
    canonical = normalize_task_id(task_id)
    for plugin in _plugins():
        if plugin.recognizes_task(canonical):
            return plugin.task_evidence(canonical, repo_root=repo_root, static_audit=static_audit)
    return {
        "health": None,
        "verdict": None,
        "tags": None,
        "leak": None,
        "repair": None,
        "static_flags": None,
        "failing_tests": None,
        "exploit_probes": None,
        "sources": {},
        "errors": [],
    }
