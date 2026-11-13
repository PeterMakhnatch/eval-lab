"""MiMo task identity and page-name plugin helpers."""

from __future__ import annotations

import re

DATASET_ID = "mimo-v2.6-rl"
_TASK_ID = re.compile(r"format-code-task-[0-9]{6}\Z")
_TASK_PREFIXES = (f"{DATASET_ID}/", f"{DATASET_ID}__")


def recognizes_task(task_id: str) -> bool:
    """Recognize the leaf or either exact native MiMo namespace spelling."""
    for prefix in _TASK_PREFIXES:
        if task_id.startswith(prefix):
            task_id = task_id.removeprefix(prefix)
            break
    return _TASK_ID.fullmatch(task_id) is not None


def normalize_task_id(task_id: str) -> str:
    for prefix in _TASK_PREFIXES:
        if task_id.startswith(prefix):
            task_id = task_id.removeprefix(prefix)
            break
    if not _TASK_ID.fullmatch(task_id):
        raise ValueError(f"invalid MiMo task id {task_id!r}")
    return task_id


def task_page_name(task_id: str) -> str:
    return "task-" + normalize_task_id(task_id).removeprefix("format-code-task-")


__all__ = ["recognizes_task", "normalize_task_id", "task_page_name"]
