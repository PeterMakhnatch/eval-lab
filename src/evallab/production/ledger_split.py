"""Ledger-side split lookup for a Verifier: DEV / TEST / UNKNOWN for a task or a (key, item) pair.

Run it locally, not through ``qa-data`` (which answers only for admitted DEV ids and refuses every
other id alike)::

    uv run python -m evallab.production.ledger_split task TASK_ID
    uv run python -m evallab.production.ledger_split item KEY_ID ITEM_ID

The answer is one word. TEST means TEST under any assignment key, or sealed by item id, task name,
task digest or text seal (namespace and version aliases included); UNKNOWN means the ledger does
not say DEV, and never authorizes access; DEV still needs the store's admission before any run
content is read. Only the safe ledger columns (key, item, task, unit, split) are read; the
registry parses label-bearing files internally, so isolation here is by output, not by bytes.
"""

from __future__ import annotations

import csv
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evallab.production.corpus_queue import DATASETS, CorpusRegistry

LEDGER = Path("research/answer-keys/splits/assignments.csv")
SAFE_LEDGER_COLUMNS = ("key_id", "item_id", "task", "unit", "split")  # never expected_verdict / exposures


def _normalized(value: str) -> str:
    return value.strip().casefold()


CHECKOUT = Path(__file__).resolve().parents[3]  # the ledger of the checkout this code runs from
USAGE = "usage: ledger_split task TASK_ID | ledger_split item KEY_ID ITEM_ID"


def lookup(registry: CorpusRegistry, root: Path, mode: str, ids: Sequence[str]) -> tuple[str, list[str]]:
    """The ledger's word and the task ids the lookup resolved, for ``task TASK_ID`` or ``item KEY_ID ITEM_ID``."""
    if (mode, len(ids)) not in {("task", 1), ("item", 2)}:
        raise ValueError(USAGE)
    ledger = root / LEDGER
    if not registry.has_assignment_authority() or not ledger.is_file():
        return "UNKNOWN", []
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
        return "TEST", tasks
    if registry.sealed_identity(item_id=item_id) is not None:
        return "TEST", tasks
    for task in tasks:
        if (
            registry.assigned_test_anywhere(task_id=task, item_id=item_id)
            or registry.sealed_identity(task_id=task) is not None
            or _normalized(task) in seals or registry.task_name(task) in seals
        ):
            return "TEST", tasks
    if any(registry.sealed_identity(task_digest=digest) is not None for digest in digests):
        return "TEST", tasks
    return ("DEV" if "dev" in assignments else "UNKNOWN"), tasks


def split(root: Path, mode: str, ids: Sequence[str]) -> dict[str, Any]:
    """``{"split": "DEV" | "TEST" | "UNKNOWN"}`` from the ledger alone: no store, no admission."""
    try:
        registry = CorpusRegistry(root)
    except (OSError, ValueError, KeyError):
        return {"split": "UNKNOWN"}
    return {"split": lookup(registry, root, mode, ids)[0]}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        answer = split(CHECKOUT, arguments[0] if arguments else "", arguments[1:])
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
