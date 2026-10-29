"""Seeker: known-hack run labels (SB-2 export) and SB-1 pilot cell outcomes.

Two inputs, both read-only:

* ``<prod_live>/seeker-known-hacks/labels-v1.jsonl``, the local-only receipts that
  ``evallab prod seeker-known-hacks --stage`` writes (``evallab.seeker_known_hack/v1``).
  A receipt whose ``source_label`` verdict is ``yes`` is a ``cheat`` positive; ``no``
  is the only way to get a negative. A receipt carrying both measured native
  receipts (original reward 1, repaired reward 0) is ``executed``; one without
  them is only a recorded verdict, so ``judge``. Unjudged receipts are not items.
* ``<results>/*/sb1-pilot*/cells.jsonl``, the SB-1 error-first pilot cells (two turns
  each). A turn whose outcome is an infrastructure failure is an ``infra`` positive
  (the run never reached a measured grade); a turn with ``verified_differential`` is a
  ``grader_wrong_pass`` positive. ``upstream_failed`` is a measured grader rejection
  (see ``run_health.INFRA_SIGNATURES``) and ``not_evaluated_no_runner_error`` only
  follows an earlier turn failure, so neither is an item.

Split: cohort tasks are declared DEV, then tightened through ``ctx.strict_split``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from evallab.failure_library.bank import (
    BankContext,
    IssueItem,
    LabelType,
    Polarity,
    Provenance,
    SourceSummary,
    new_item,
)

SOURCE = "seeker"
LABELS_RELATIVE = Path("seeker-known-hacks") / "labels-v1.jsonl"
RUNS_RELATIVE = Path("seeker-known-hacks") / "runs-v1.jsonl"
LABEL_SCHEMA = "evallab.seeker_known_hack/v1"
RESULTS_ROOT_ENV = "EVALLAB_RESULTS_ROOT"
PILOT_GLOB = "*/sb1-pilot*/cells.jsonl"
TURNS = ("turn_1", "turn_2")
INFRA_OUTCOMES = frozenset(
    {"served_route_mismatch", "original_grader_unavailable", "generation_failed_or_truncated"}
)
_VERDICTS: dict[str, Polarity] = {"yes": "positive", "no": "negative"}


def _results_root() -> Path:
    return Path(os.environ.get(RESULTS_ROOT_ENV, Path.home() / "Developer/eval-lab-results"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    yield number, row


class _Cloud:
    """The cloud DB identities the seeker cells map onto (absent DB = no mapping)."""

    def __init__(self, database: Path) -> None:
        self._connection: sqlite3.Connection | None = None
        if database.is_file():
            self._connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        self._tasks: dict[tuple[str, str], str] = {}
        self._loaded: set[str] = set()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()

    def task_id(self, dataset_id: str, bare: str) -> str:
        """Full cloud task id for a bare cell task name; the bare name when unknown."""
        if self._connection is not None and dataset_id not in self._loaded:
            self._loaded.add(dataset_id)
            for (task_id,) in self._connection.execute(
                "SELECT task_id FROM corpus_tasks WHERE dataset_id = ?", (dataset_id,)
            ):
                self._tasks[(dataset_id, str(task_id).rsplit("/", 1)[-1])] = str(task_id)
        return self._tasks.get((dataset_id, bare), bare)

    def has_trace(self, trace_id: str) -> bool:
        if self._connection is None:
            return False
        return (
            self._connection.execute(
                "SELECT 1 FROM agent_traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
            is not None
        )


def _runs_with_actor(path: Path) -> frozenset[str]:
    """Run ids whose staged ATIF has an agent step with text or a tool call."""
    if not path.is_file():
        return frozenset()
    found: set[str] = set()
    for _, row in _rows(path):
        steps = (row.get("doc") or {}).get("steps") or []
        for step in steps:
            if step.get("source") == "agent" and (
                str(step.get("message") or "").strip() or step.get("tool_calls")
            ):
                found.add(str(row.get("run_id")))
                break
    return frozenset(found)


def _label_type(label: Mapping[str, Any]) -> LabelType:
    """Executed only when both native measured receipts back the verdict."""
    evidence = label.get("evidence")
    if isinstance(evidence, Mapping) and evidence.get("original") and evidence.get("repaired"):
        return "executed"
    return "judge"


def _known_hack_items(ctx: BankContext, cloud: _Cloud) -> Iterator[IssueItem]:
    labels = ctx.prod_live / LABELS_RELATIVE
    if not labels.is_file():
        return
    digest = _sha256(labels)
    staged = _runs_with_actor(ctx.prod_live / RUNS_RELATIVE)
    for number, row in _rows(labels):
        if row.get("schema") != LABEL_SCHEMA:
            continue
        source_label = row.get("source_label") or {}
        verdict = str(source_label.get("verdict_enacted") or source_label.get("verdict") or "")
        polarity = _VERDICTS.get(verdict)
        run_id, task_id = row.get("run_id"), row.get("task_id")
        if polarity is None or not source_label.get("judged") or not run_id or not task_id:
            continue  # an unjudged receipt is unlabelled, not a negative
        admission = row.get("admission") or {}
        declared = str(admission.get("effective_split") or "unassigned")
        run_id, task_id = str(run_id), str(task_id)
        in_cloud = cloud.has_trace(run_id)
        steps = (row.get("passage_step_labels") or {}).get("positive_step_indices") or []
        yield new_item(
            source=SOURCE,
            kind="cheat",
            polarity=polarity,
            label_type=_label_type(row),
            label=f"seeker_known_hack:{verdict}"[:200],
            granularity="run",
            subject_type="run",
            subject_id=run_id,
            task_id=task_id,
            positive_units=len(steps) if polarity == "positive" else None,
            declared_split=declared,
            split=ctx.strict_split(
                declared,
                key_id=row.get("dataset_id"),
                item_id=task_id.rsplit("/", 1)[-1],
                task_id=task_id,
            ),
            license=None,
            actor_transcript=in_cloud or run_id in staged,
            harness_selector=run_id if in_cloud else None,
            provenance=Provenance(
                store="seeker-known-hacks:labels-v1",
                ref=f"labels-v1.jsonl#{number}",
                digest=digest,
                note=f"candidate_digest={row.get('candidate_digest')}"[:300],
            ),
        )


def _turn_directory(root: Path, dataset: str, task: str, model: str, turn: str) -> Path:
    cell = root / dataset / task / model.replace("/", "_")
    return cell / turn.replace("_", "-")


def _has_response(directory: Path) -> bool:
    response = directory / "response.jsonl"
    return response.is_file() and response.stat().st_size > 0


def _sb1_items(ctx: BankContext, cloud: _Cloud) -> Iterator[IssueItem]:
    for cells in sorted(_results_root().glob(PILOT_GLOB)):
        root = cells.parent
        digest = _sha256(cells)
        for number, row in _rows(cells):
            dataset, task, model = row.get("dataset_id"), row.get("task"), row.get("model")
            if not (isinstance(dataset, str) and isinstance(task, str) and isinstance(model, str)):
                continue
            task_id = cloud.task_id(dataset, task)
            split = ctx.strict_split("DEV", key_id=dataset, item_id=task, task_id=task_id)
            subject = f"{root.name}:{dataset}/{task}/{model}"
            for turn in TURNS:
                result = row.get(turn)
                if not isinstance(result, Mapping):
                    continue
                outcome = str(result.get("outcome") or "")
                if outcome in INFRA_OUTCOMES:
                    kind, label = "infra", outcome
                elif result.get("verified_differential") is True:
                    kind, label = "grader_wrong_pass", f"verified_differential ({outcome})"
                else:
                    continue
                directory = _turn_directory(root, dataset, task, model, turn)
                yield new_item(
                    source=SOURCE,
                    kind=kind,
                    polarity="positive",
                    label_type="executed",
                    label=label[:200],
                    granularity="run",
                    subject_type="run",
                    subject_id=subject,
                    task_id=task_id,
                    locator=turn,
                    declared_split="DEV",
                    split=split,
                    license=None,
                    actor_transcript=_has_response(directory),
                    harness_selector=None,
                    provenance=Provenance(
                        store="seeker:sb1-pilot-cells",
                        ref=f"{root.parent.name}/{root.name}/cells.jsonl#{number}:{turn}",
                        digest=digest,
                    ),
                )


def build(ctx: BankContext) -> Iterator[IssueItem | SourceSummary]:
    cloud = _Cloud(ctx.cloud_db)
    try:
        yield from _known_hack_items(ctx, cloud)
        yield from _sb1_items(ctx, cloud)
    finally:
        cloud.close()
