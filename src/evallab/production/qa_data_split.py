"""``qa-data split``: confirms DEV for an admitted id, and refuses every other id the same way.

Missing, TEST, held-out, sealed and cross-key-locked ids all get the refusal ``task`` gives, so this
call is not a ledger-membership oracle. A Verifier who needs the ledger's own TEST/UNKNOWN word runs
``python -m evallab.production.ledger_split`` locally (see ``docs/task-quality.md``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from evallab.production import ledger_split
from evallab.production.cloud_trace_export import CHEATBENCH_DATASET, cheatbench_item_admitted
from evallab.production.qa_data import UNAVAILABLE, QaContext, QaDataError, match_admitted_task


def _item_admitted(ctx: QaContext, key_id: str, item_id: str, tasks: Sequence[str]) -> bool:
    registry = ctx.retriever.registry
    if key_id.strip().casefold() == CHEATBENCH_DATASET.casefold():
        try:
            return cheatbench_item_admitted(registry, registry.get(key_id, item_id))
        except KeyError:
            return False
    try:  # the registry's own identity for the pair is authoritative; a looser ledger alias never overrides it
        registered = registry.get(key_id, item_id).task_id
    except KeyError:
        registered = None
    admitted = ctx.admitted_task_ids()

    def admitted_exactly(task: str) -> bool:
        try:
            return match_admitted_task(admitted, task) is not None
        except QaDataError:  # several admitted tasks share the bare name: not one identity
            return False

    wanted = [registered] if registered else list(tasks)
    return bool(wanted) and all(admitted_exactly(task) for task in wanted)


def split(ctx: QaContext, mode: str, ids: Sequence[str]) -> dict[str, Any]:
    """``{"split": "DEV"}`` when the task, or the (key, item) pair, is an admitted DEV identity."""
    if (mode, len(ids)) not in {("task", 1), ("item", 2)}:
        raise QaDataError("usage: split task TASK_ID | split item KEY_ID ITEM_ID")
    if mode == "task":
        ctx.require_task(ids[0])  # admission is the authority; it raises the uniform refusal
        return {"split": "DEV"}
    try:
        word, tasks = ledger_split.lookup(ctx.retriever.registry, ctx.repo_root, mode, ids)
    except (OSError, ValueError, KeyError):
        raise QaDataError(UNAVAILABLE) from None
    if word != "DEV" or not _item_admitted(ctx, ids[0], ids[1], tasks):
        raise QaDataError(UNAVAILABLE)
    return {"split": "DEV"}
