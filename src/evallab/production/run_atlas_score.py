"""DEV-only label joins for retained scoring consumers (no model calls)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.run_atlas import (
    CHEATBENCH_DATASET,
    GOODFIRE_DATASET,
    admitted_task_ids,
    connect_readonly,
)

_STRATA = {
    ("yes", "yes"): "enacted_yes",
    ("no", "no"): "clean_no",
    ("yes", "no"): "considered_only",
}
_IDENTITY_SQL = (
    "SELECT a.trace_id, c.task_id, c.dataset_id, c.split, c.metadata_json"
    " FROM agent_traces a JOIN corpus_tasks c USING(task_id)"
)


@dataclass(frozen=True)
class _Label:
    run_id: str
    source: str
    stratum: str
    positive: bool | None


def _check(
    registry: CorpusRegistry, checked: set[str], run_id: str, *,
    task_id: str | None = None, item_id: str | None = None, split: str = "dev",
) -> None:
    if split.casefold() != "dev" or registry.assigned_test_anywhere(
        item_id=run_id, task_id=task_id,
    ) or (item_id is not None and registry.assigned_test_anywhere(item_id=item_id)):
        raise ValueError(f"refusing TEST/non-DEV run: {run_id}")
    checked.add(run_id)


def _read_labels(
    connection: sqlite3.Connection, registry: CorpusRegistry, *,
    required: set[str], wanted: set[str] | None = None,
) -> tuple[dict[str, _Label], set[str]]:
    """Check identity-only rows before reading any scored receipt's labels."""
    checked: set[str] = set()
    for run_id in sorted(required):
        _check(registry, checked, run_id)
    admitted = admitted_task_ids(connection, registry)
    rows = {
        row["trace_id"]: row for row in connection.execute(
            _IDENTITY_SQL + " WHERE c.dataset_id = ? AND lower(c.split) = 'dev'",
            (GOODFIRE_DATASET,),
        ) if wanted is None or row["trace_id"] in wanted
    }
    extra = sorted(required - rows.keys())
    for start in range(0, len(extra), 500):
        batch = extra[start:start + 500]
        rows.update({row["trace_id"]: row for row in connection.execute(
            _IDENTITY_SQL + f" WHERE a.trace_id IN ({','.join('?' * len(batch))})", batch,
        )})
    labels: dict[str, _Label] = {}
    for run_id, row in sorted(rows.items()):
        metadata = json.loads(row["metadata_json"])
        item_id = metadata.get("item_id")
        if item_id is not None and not isinstance(item_id, str):
            raise ValueError(f"invalid item identity for {run_id}")
        if row["task_id"] not in admitted and run_id not in required:
            continue
        _check(registry, checked, run_id, task_id=row["task_id"], item_id=item_id,
               split=row["split"])
        if row["task_id"] not in admitted:
            raise ValueError(f"run is not DEV-admitted: {run_id}")
        if row["dataset_id"] != GOODFIRE_DATASET:
            continue
        receipt_row = connection.execute(
            "SELECT verified_receipt_json FROM agent_traces WHERE trace_id = ?", (run_id,),
        ).fetchone()
        receipt = json.loads(receipt_row[0] or "{}")
        effective_split = (receipt.get("admission") or {}).get("effective_split")
        if effective_split is not None and str(effective_split).casefold() != "dev":
            if run_id in required:
                raise ValueError(f"refusing TEST/non-DEV receipt: {run_id}")
            continue
        source_label = receipt.get("source_label") or {}
        if source_label.get("judged") is not True:
            continue
        stratum = _STRATA.get((source_label.get("verdict"), source_label.get("verdict_enacted")))
        if stratum is None:
            continue
        labels[run_id] = _Label(
            run_id, run_id.split("/")[1], stratum,
            None if stratum == "considered_only" else stratum == "enacted_yes",
        )
    for item in registry.items(dataset_id=CHEATBENCH_DATASET):
        if wanted is not None and item.item_id not in wanted:
            continue
        if item.split != "dev" and item.item_id not in required:
            continue
        _check(registry, checked, item.item_id, task_id=item.task_id, split=item.split)
        if str(item.metadata.get("split", "")).casefold() != "dev":
            raise ValueError(f"refusing non-DEV CheatBench item: {item.item_id}")
        positive = item.metadata.get("expected_verdict") == "fail"
        labels[item.item_id] = _Label(
            item.item_id, CHEATBENCH_DATASET, "positive" if positive else "negative", positive,
        )
    return labels, checked


def load_labels(
    *, database: Path, repo_root: Path, run_ids: Iterable[str] | None = None,
) -> dict[str, bool | None]:
    """Join DEV labels for scoring consumers; unknown/unjudged ids are omitted.

    A requested TEST, cross-key TEST, or non-admitted run raises rather than
    silently disappearing. ``None`` denotes Goodfire's considered-only stratum,
    not a negative. No trajectories or model inputs are read.
    """
    wanted = None if run_ids is None else set(run_ids)
    with closing(connect_readonly(database)) as connection:
        labels, _ = _read_labels(
            connection, CorpusRegistry(repo_root), required=wanted or set(), wanted=wanted,
        )
    return {run_id: label.positive for run_id, label in labels.items()}
