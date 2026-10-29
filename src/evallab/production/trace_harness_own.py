"""Trace-harness view of our OWN Harbor runs and the own-run window cascade.

The Trace Tools lane (TT-3) ran its readers on a frozen cohort of 169 of our own
Harbor trials (``~/.local/state/evallab-prod-live/trace-tools/tt3-cohort.jsonl``),
not on the cloud-DB own datasets (``mimo-defect-map-v1``, ``tb4-audit-v1``,
``tb2-to-tb21-v1``): none of the cohort's trial names is a cloud-DB ``trace_id``, so
``dataset:<own dataset>`` selects runs that have no stored own-run result. This module
supplies the missing pieces; the Laminar and Docent (``rh_reading_v1``) detectors in
``trace_harness_tools`` already cover the cohort once its runs can be selected:

``own_runs`` / ``own_dev_runs``
    Corpus loaders (selectors ``own`` and ``own-dev``). Each cohort trial's
    ``agent/trajectory.json`` is read exactly as the TT-3 replay and Docent upload did:
    ATIF continuation chains followed, copied-context steps omitted, steps renumbered
    consecutively from 1 across the chain (single-document trials keep their own step
    ids). Admission is resolved from the canonical split registry, with the strict
    cross-key TEST guard, before any trajectory is opened. A cohort's own DEV tag
    is never authority; unsplit or unresolved rows are omitted.

``LaminarCopyDetector`` (``laminar_copy``)
    The one Laminar Signal the Trace Tools lane published for the own cohort,
    ``copied_upstream_fix`` (35 hits, 0 false alarms in TT-3), from the same stored
    ledger rows as ``laminar``. ``laminar`` is the any-flag union of every flagging
    Signal, including the broad ``Failure Detector`` and ``stuck_loop`` presets, so it
    cannot reproduce that number.

``CascadeOwnDetector`` (``cascade_own``)
    The stored CM-9 ``pf_r`` own-run replay (``cascade-own.jsonl``,
    ``window-cascade/v7`` with the ``submission_tamper`` pass-through) as
    ``trace-findings/v1``, with the verdict rules of ``trace_harness_cascade``: ``alarm``
    iff a card is confirmed or its rule passes through; ``clear`` iff none and no judge
    call was unavailable; otherwise ``abstain``. Cited steps are the alarming cards'
    flagged steps, in the stitched step ids the loader produces.

    The stored rows were judged by two routes and the detector keeps them apart by
    ``reader_model``: the default reader ``kimi-code/k3.1-pro-highspeed`` (the attested
    CM-9 judge route) yields the Kimi-judged rows plus the rows that needed no judge
    (no Stage-1 lead, or a pass-through alarm, or all judge calls unavailable);
    ``cascade_own:xai-oauth/grok-4.3`` yields the 23 rows completed by the Grok
    alternate before its quota stopped (not the production judge). A run with no stored
    row is omitted, never ``clear``. Nothing is replayed and no model is called, so
    ``--allow-model`` has no effect. Per-run tokens were not recorded: reported as 0.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.production.cloud_trace_db import admitted_cloud_task_ids
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.run_health import trial_metadata
from evallab.production.trace_harness import (
    HarnessOptions,
    HarnessRun,
    actor_inputs_available,
    finding,
)
from evallab.production.trace_harness_cascade import (
    JUDGE_MODEL,
    V7_PASS_THROUGH,
    _read_jsonl,
    _StoredCascade,
)
from evallab.production.trace_harness_tools import (
    TRACE_TOOLS,
    LaminarDetector,
    _utc,
    known_steps,
    read_ledger,
    verdict_of,
)

OWN_SOURCE = "own-harbor"
DEV_SPLIT = "DEV"
UNSPLIT = "UNSPLIT"
OUTCOME = "unknown"
COHORT = TRACE_TOOLS / "tt3-cohort.jsonl"
CASCADE_ROWS = TRACE_TOOLS / "cascade-own.jsonl"
DEV_STRATUM = "trace-paired-v1:dev"
GROK_MODEL = "xai-oauth/grok-4.3"
OWN_ROUTES = (JUDGE_MODEL, GROK_MODEL)
ACTOR_FIELDS = ("source", "message", "reasoning_content", "tool_calls", "observation", "timestamp", "metrics")


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OwnTrial:
    run_id: str
    trial_dir: Path
    dev: bool


def cohort_trials(
    path: Path | None = None, *, repo_root: Path = Path("."), registry: CorpusRegistry | None = None,
) -> list[OwnTrial]:
    """The frozen own cohort, in file order; a repeated run id is refused."""
    path = COHORT if path is None else path
    trials: list[OwnTrial] = []
    registry = registry if registry is not None else CorpusRegistry(repo_root)
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            run_id = row["run_id"]
            if run_id in seen:
                raise ValueError(f"{path}:{number}: repeated run {run_id}")
            seen.add(run_id)
            task_name = row.get("task_name")
            if not isinstance(task_name, str) or not task_name:
                continue
            identity = {
                "task_id": task_name, "dataset_id": "trace-paired-v1", "split": "DEV",
                "item_id": f"trace-paired-v1:{run_id}", "item_type": "text",
                "original_grader_digest": None, "repaired_grader_digest": None,
            }
            if task_name not in admitted_cloud_task_ids([identity], registry=registry):
                continue
            trials.append(OwnTrial(run_id, Path(row["trial_dir"]), row.get("cohort") == DEV_STRATUM))
    return trials


def stitched_steps(trial_dir: Path) -> list[dict[str, Any]]:
    """Actor-visible ATIF steps of one trial across its continuation chain.

    Copied-context steps are omitted and ``step_id`` runs 1..N over the kept steps, the
    numbering of the TT-3 cascade replay and Docent upload. Continuations must stay in
    ``agent/``; a loop or an escape raises.
    """
    agent = (trial_dir / "agent").resolve()
    path = agent / "trajectory.json"
    seen: set[Path] = set()
    steps: list[dict[str, Any]] = []
    while True:
        path = path.resolve()
        if path in seen or path.parent != agent:
            raise ValueError(f"invalid ATIF continuation chain in {trial_dir}")
        seen.add(path)
        document = json.loads(path.read_text(encoding="utf-8"))
        for step in document["steps"]:
            if step.get("is_copied_context"):
                continue
            kept = {key: step[key] for key in ACTOR_FIELDS if key in step}
            kept["step_id"] = len(steps) + 1
            if kept.get("source") == "assistant":
                kept["source"] = "agent"
            steps.append(kept)
        reference = document.get("continued_trajectory_ref")
        if not reference:
            return steps
        path = agent / reference


def _runs(
    path: Path | None, limit: int | None, *, dev_only: bool,
    repo_root: Path, registry: CorpusRegistry | None,
) -> Iterator[HarnessRun]:
    count = 0
    for trial in cohort_trials(path, repo_root=repo_root, registry=registry):
        if dev_only and not trial.dev:
            continue
        if limit is not None and count >= limit:
            return
        count += 1
        steps = tuple(stitched_steps(trial.trial_dir))
        yield HarnessRun(
            trial.run_id, OWN_SOURCE, DEV_SPLIT, OUTCOME, steps,
            actor_inputs_available(steps), meta=trial_metadata(trial.trial_dir),
        )


def own_runs(
    limit: int | None = None, *, cohort: Path | None = None, repo_root: Path = Path("."),
    registry: CorpusRegistry | None = None,
) -> Iterator[HarnessRun]:
    """Own-cohort runs admitted DEV by the strict split registry."""
    return _runs(cohort, limit, dev_only=False, repo_root=repo_root, registry=registry)


def own_dev_runs(
    limit: int | None = None, *, cohort: Path | None = None, repo_root: Path = Path("."),
    registry: CorpusRegistry | None = None,
) -> Iterator[HarnessRun]:
    """The trace-paired DEV stratum, additionally checked against cross-key seals."""
    return _runs(cohort, limit, dev_only=True, repo_root=repo_root, registry=registry)


# --------------------------------------------------------------------------- #
# cascade_own
# --------------------------------------------------------------------------- #


class CascadeOwnDetector(_StoredCascade):
    """Stored CM-9 ``pf_r`` own-run cascade rows (``cascade-own.jsonl``)."""

    name = "cascade_own"
    version = "window-cascade/v7-cm9-pf_r-own"
    pass_through = V7_PASS_THROUGH

    def __init__(self, options: HarnessOptions | None = None, *, rows: Path | None = None) -> None:
        super().__init__(options)
        reader = self.options.reader or JUDGE_MODEL
        if reader not in OWN_ROUTES:
            raise ValueError(f"{self.name} reader must be one of {OWN_ROUTES}, got {reader!r}")
        self.reader_model = reader
        self.rows = CASCADE_ROWS if rows is None else rows

    def _judged_here(self, row: dict[str, Any]) -> bool:
        model = row.get("model")
        return model == self.reader_model or (model is None and self.reader_model == JUDGE_MODEL)

    def detect(self, runs: Sequence[HarnessRun]) -> Iterator[dict[str, Any]]:
        if not self.rows.is_file():
            return
        stored = _read_jsonl(self.rows, "run_id")
        for run in runs:
            row = stored.get(run.run_id)
            if row is None or not self._judged_here(row):
                continue
            if row["n_steps"] != len(run.steps):
                raise ValueError(
                    f"{run.run_id}: stored replay saw {row['n_steps']} steps, run has {len(run.steps)}"
                )
            yield self._record(run, row.get("cards") or (), int(row.get("triage_unavailable") or 0), self.rows)


# --------------------------------------------------------------------------- #
# laminar_copy
# --------------------------------------------------------------------------- #


class LaminarCopyDetector(LaminarDetector):
    """The stored ``copied_upstream_fix`` Laminar Signal alone (reader ``zai/glm-5.3-flash``)."""

    name = "laminar_copy"
    SIGNAL = "copied_upstream_fix"

    def detect(self, runs: Sequence[HarnessRun]) -> Iterator[dict[str, Any]]:
        ledger, _ = read_ledger(
            self.results, "laminar",
            lambda row: row.get("detector_version") == self.version and row.get("detector") == self.SIGNAL,
            [run.run_id for run in runs],
        )
        for run in runs:
            row = ledger.get((self.SIGNAL, run.run_id))
            if row is None:
                continue
            hit = row["flag"] is True
            read, written = self._usage(row)
            yield finding(
                run.run_id, self.name, detector_version=self.version, reader_model=self.reader_model,
                verdict=verdict_of(row["flag"]), kind="cheat",
                evidence_steps=known_steps(run, self._steps(self.SIGNAL, row) if hit else (), self.options.database),
                summary=f"signal fired: {self.SIGNAL}" if hit else f"{self.SIGNAL} signal did not fire"
                if row["flag"] is False else f"{self.SIGNAL} signal abstained",
                input_tokens=read, output_tokens=written, produced_at=_utc(str(row["at"])),
            )
