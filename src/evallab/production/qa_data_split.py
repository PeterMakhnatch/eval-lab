"""``qa-data split``: a label-free DEV / TEST / UNKNOWN lookup for a task or a (key, item) pair.

The answer is one word. TEST means TEST under any assignment key, or sealed by item id, task name,
task digest or text seal (namespace and version aliases included); UNKNOWN means the ledger does
not say DEV, and never authorizes access; DEV still needs the store's admission before any run
content is read. Only the safe ledger columns (key, item, task, unit, split) are read; the
registry parses label-bearing files internally, so isolation here is by output, not by bytes.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evallab.production.corpus_queue import DATASETS, CorpusRegistry
from evallab.production.qa_data import QaDataError

LEDGER = Path("research/answer-keys/splits/assignments.csv")
SAFE_LEDGER_COLUMNS = ("key_id", "item_id", "task", "unit", "split")  # never expected_verdict / exposures


def _normalized(value: str) -> str:
    return value.strip().casefold()


def split(root: Path, mode: str, ids: Sequence[str]) -> dict[str, Any]:
    """``{"split": "DEV" | "TEST" | "UNKNOWN"}`` for ``task TASK_ID`` or ``item KEY_ID ITEM_ID``."""
    if (mode, len(ids)) not in {("task", 1), ("item", 2)}:
        raise QaDataError("usage: split task TASK_ID | split item KEY_ID ITEM_ID")
    ledger = root / LEDGER
    try:
        registry = CorpusRegistry(root)
    except (OSError, ValueError, KeyError):
        return {"split": "UNKNOWN"}
    if not registry.has_assignment_authority() or not ledger.is_file():
        return {"split": "UNKNOWN"}
    with ledger.open(newline="", encoding="utf-8") as stream:
        rows = [{name: row[name] for name in SAFE_LEDGER_COLUMNS} for row in csv.DictReader(stream)]
    seals = registry.sealed_identifiers()
    digests: list[str] = []
    if mode == "task":
        item_id: str | None = None
        tasks = [ids[0]]
        keys = set(DATASETS) | {row["key_id"] for row in rows}
        assignments = [registry.assigned_split(key, task_id=ids[0]) for key in keys]
    else:
        key_id, item_id = ids
        tasks = [
            row["task"] for row in rows
            if row["task"] and _normalized(row["key_id"]) == _normalized(key_id)
            and _normalized(row["item_id"]) == _normalized(item_id)
        ]
        try:  # inventory-only keys (MiMo) resolve through the registry, identity fields only
            identity = registry.get(key_id, item_id)
        except KeyError:
            pass
        else:
            if identity.task_id:
                tasks.append(identity.task_id)
            if identity.task_digest:
                digests.append(identity.task_digest)
        assignments = [registry.assigned_split(key_id, item_id=item_id)]
        assignments += [registry.assigned_split(key_id, item_id=item_id, task_id=task) for task in tasks]
    if "test" in assignments or registry.assigned_test_anywhere(item_id=item_id):
        return {"split": "TEST"}
    if registry.sealed_identity(item_id=item_id) is not None:
        return {"split": "TEST"}
    for task in tasks:
        if (
            registry.assigned_test_anywhere(task_id=task, item_id=item_id)
            or registry.sealed_identity(task_id=task) is not None
            or _normalized(task) in seals or registry.task_name(task) in seals
        ):
            return {"split": "TEST"}
    if any(registry.sealed_identity(task_digest=digest) is not None for digest in digests):
        return {"split": "TEST"}
    return {"split": "DEV" if "dev" in assignments else "UNKNOWN"}
