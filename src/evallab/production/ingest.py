"""Admit public DEV corpus records without inventing executable work.

Every native DEV record gets an immutable ``queue.corpus_admitted`` event,
including TB4 task records with no finding. Only an existing digest-bound actor
trajectory becomes ``misalignment.replay_task``. The source indices do not
supply owned candidates or same-submission differential replay inputs, so their
package/pair metadata alone never creates a Task Quality or Seeker job.

Preflight calls the existing candidate runner in read-only ``plan=True`` mode
with an empty artifact mapping. This is a lifecycle/asset check, not a candidate,
Docker probe, executed control, model generation, or proof of measurability.
Static admission therefore remains ``unknown`` until real execution. Raw index
rows (including evaluator labels) are hashed for custody but never exported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evallab.production.corpus_queue import CorpusItem


def _path(value: Any, root: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _package_locators(item: CorpusItem, root: Path) -> list[Path]:
    metadata = item.metadata
    materialization = metadata.get("materialization") or {}
    package = metadata.get("package") or {}
    candidates: list[Path] = []
    for raw in (
        materialization.get("resolved_locator"),
        materialization.get("locator"),
        metadata.get("task_package"),
        package.get("path"),
    ):
        path = _path(raw, root)
        if path is not None and path not in candidates:
            candidates.append(path)
    package_root = _path(package.get("root_hint"), root)
    if package_root is not None and isinstance(package.get("name"), str):
        name = Path(package["name"])
        if not name.is_absolute() and len(name.parts) == 1 and name.name not in (".", ".."):
            candidates.append(package_root / name)
    return candidates




