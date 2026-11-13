"""Small built-in registry for dataset-specific audit evidence conventions."""

from __future__ import annotations

import hashlib
import re
from typing import Protocol


class DatasetAuditPlugin(Protocol):
    def recognizes_task(self, task_id: str) -> bool: ...

    def normalize_task_id(self, task_id: str) -> str: ...

    def task_page_name(self, task_id: str) -> str: ...


def _plugins() -> tuple[DatasetAuditPlugin, ...]:
    from evallab import audit_mimo

    return (audit_mimo,)


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


def task_page_name(task_id: str) -> str:
    canonical = normalize_task_id(task_id)
    for plugin in _plugins():
        if plugin.recognizes_task(canonical):
            return plugin.task_page_name(canonical)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", canonical)[:72].strip(".-") or "task"
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:12]
    return f"task-{slug}-{digest}"

