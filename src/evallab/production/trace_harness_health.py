"""Trace-harness adapters for the Run Health detectors (deterministic, no model, $0).

``run_health.detect`` returns four ``trace-findings/v1`` records per run (``loop``,
``runaway``, ``latency``, ``infra``). The harness wants one detector per record kind,
so :class:`HealthLoopDetector` and its three siblings are four views of one shared,
per-run computation: the first detector asked about a run computes its features and
all four records, the other three read that result.

A :class:`~evallab.production.trace_harness.HarnessRun` has the steps but not the run
facts Run Health uses (step timestamps and token counts live in the cloud store's
``flags_json``, the run status in ``agent_traces``). Runs the cloud DB admits are
therefore passed through :func:`evallab.production.run_health.store_runs` once per
batch, the same loader behind ``evallab run-health detect --db``, so their findings
are identical to that command's. Local Harbor trial loaders retain the same
allowlisted health facts via ``trial_metadata``. Sources with only trajectories
(CheatBench, HackBench, most staged sets) have no run-level facts: their latency
uses only timestamps in the trajectory and abstains below the native minimum;
their wall time, exceptions and stop reason remain unknown.

Labels, splits and source names are never read.
"""

from __future__ import annotations

import time
import weakref
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from evallab.production import run_health
from evallab.production.run_atlas import CHEATBENCH_DATASET
from evallab.production.trace_harness import HarnessOptions, HarnessRun, finding

HEALTH_VERSION = run_health.DETECTOR_VERSION
#: Ids per ``store_runs`` call; well under SQLite's bound-variable limit.
STORE_CHUNK = 10_000


def _health_run(run: HarnessRun) -> run_health.HealthRun:
    """Retained native health facts, or steps-only when the source has no facts."""
    return run_health.HealthRun(
        run_id=run.run_id, source=run.source, steps=run.steps,
        meta={**run.meta, "actor_inputs_available": run.actor_inputs_available},
    )


def _store_runs(
    runs: Sequence[HarnessRun], database: Path, repo_root: Path,
) -> Iterator[run_health.HealthRun]:
    """Run Health's store loader for the runs the cloud DB admits (others are skipped)."""
    from evallab.production.corpus_queue import CorpusRegistry

    ids = [run.run_id for run in runs if not run.run_id.startswith(CHEATBENCH_DATASET + ":")]
    if not ids:
        return
    registry = CorpusRegistry(repo_root)
    for start in range(0, len(ids), STORE_CHUNK):
        yield from run_health.store_runs(
            database, registry=registry, trace_ids=ids[start:start + STORE_CHUNK],
        )


class HealthComputation:
    """Run Health features and records, computed once per run and shared by the four kinds.

    Results are held only while the ``HarnessRun`` object they were computed for is
    alive (the harness hands every detector the same objects), so a different run
    reusing an id can never read a stale result.
    """

    def __init__(self, database: Path, repo_root: Path) -> None:
        self.database = database
        self.repo_root = repo_root
        self._results: dict[str, tuple[weakref.ref[HarnessRun], dict[str, dict[str, Any]]]] = {}

    def records(self, runs: Sequence[HarnessRun]) -> dict[str, dict[str, dict[str, Any]]]:
        """``run_id -> kind -> trace-findings/v1 record`` for every run in ``runs``."""
        pending = [run for run in runs if not self._cached(run)]
        if pending:
            self._compute(pending)
        return {run.run_id: self._results[run.run_id][1] for run in runs}

    def _cached(self, run: HarnessRun) -> bool:
        entry = self._results.get(run.run_id)
        return entry is not None and entry[0]() is run

    def _compute(self, pending: Sequence[HarnessRun]) -> None:
        by_id = {run.run_id: run for run in pending}
        loaded: set[str] = set()
        if self.database.is_file():
            for health_run in _store_runs(pending, self.database, self.repo_root):
                if health_run.run_id in by_id:
                    self._store(by_id[health_run.run_id], health_run)
                    loaded.add(health_run.run_id)
        for run in pending:
            if run.run_id not in loaded:
                self._store(run, _health_run(run))

    def _store(self, run: HarnessRun, health_run: run_health.HealthRun) -> None:
        started = time.perf_counter()
        records = run_health.detect(health_run)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        by_kind = {
            record["kind"]: finding(
                run.run_id, record["detector"], detector_version=record["detector_version"],
                reader_model=None, verdict=record["verdict"], kind=record["kind"],
                score=record["score"], evidence_steps=record["evidence_steps"],
                summary=record["summary"], latency_ms=elapsed_ms,
            )
            for record in records
        }
        key = run.run_id
        self._results[key] = (
            weakref.ref(run, lambda ref, key=key: self._drop(key, ref)), by_kind,
        )

    def _drop(self, key: str, ref: weakref.ref[HarnessRun]) -> None:
        entry = self._results.get(key)
        if entry is not None and entry[0] is ref:
            del self._results[key]


_SHARED: dict[tuple[Path, Path], HealthComputation] = {}


def shared_computation(options: HarnessOptions | None) -> HealthComputation:
    """One computation per (database, repo root), so the four detectors share work."""
    options = options or HarnessOptions()
    key = (options.database.resolve(), options.repo_root.resolve())
    if key not in _SHARED:
        _SHARED[key] = HealthComputation(options.database, options.repo_root)
    return _SHARED[key]


class _HealthDetector:
    """One run-health kind as a harness detector."""

    kind: str
    name: str
    version = HEALTH_VERSION
    reader_model = None

    def __init__(self, options: HarnessOptions | None = None) -> None:
        self.options = options
        self.computation = shared_computation(options)

    def detect(self, runs: Sequence[HarnessRun]) -> Iterable[dict[str, Any]]:
        results: Mapping[str, Mapping[str, dict[str, Any]]] = self.computation.records(runs)
        for run in runs:
            yield results[run.run_id][self.kind]


class HealthLoopDetector(_HealthDetector):
    """The agent repeats itself without progress."""

    kind = "loop"
    name = "run_health.loop"


class HealthRunawayDetector(_HealthDetector):
    """The run is far too long, or ended on a budget or timeout."""

    kind = "runaway"
    name = "run_health.runaway"


class HealthLatencyDetector(_HealthDetector):
    """Steps are slow or the run stalls between steps; abstains without clocks."""

    kind = "latency"
    name = "run_health.latency"


class HealthInfraDetector(_HealthDetector):
    """The run ended on, or was corrupted by, infrastructure."""

    kind = "infra"
    name = "run_health.infra"
