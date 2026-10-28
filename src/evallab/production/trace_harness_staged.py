"""Staged cross-test sets as trace-harness runs and labels (local-only, eval-only).

The Library lane staged permissive cross-test sets under
``~/.local/state/evallab-cross-test/<owner>__<name>/<rev>/`` and registered
each with a read-only adapter (``research/evidence/runs/corpus-expansion-pq*``).
This module reuses those adapters: it never copies, caches or writes a record,
and it reads nothing but the adapter's ``specs()`` output. Per spec only
``_steps`` reaches a detector (as :class:`HarnessRun`); the label and the native
id are read here for the scorer and are never placed on the run.

Qualifying sets are the registry entries whose ``labels.false_alarm_basis``
gives a run-level false-alarm rate (independent positives AND real negatives)
at the run level. None of them has per-step gold (the registry marks command
level localization not scorable), so ``positive_steps`` is empty: these sets
score run-level recall and false-alarm rate, not localization.

HackBench (CC BY-NC, owned by ``trace_harness_corpora``), the two Task-QA sets
and the sets without run-level real negatives are not here; see
:data:`EXCLUDED_SETS`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import statistics
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from evallab.production.trace_harness import HarnessRun, actor_inputs_available

SPLIT = "CROSS-TEST"
OUTCOME = "unknown"
STATE_ROOT = Path.home() / ".local/state/evallab-cross-test"
REPO_ROOT = Path(__file__).resolve().parents[3]
_RUNS = "research/evidence/runs"


@dataclass(frozen=True)
class StagedSet:
    """One qualifying staged set, as registered (counts are the published census)."""

    name: str
    bundle: str  # directory under STATE_ROOT: "<owner>__<name>/<rev12>"
    adapter: str  # repo-relative adapter module; only ``specs()`` is used
    licence: str
    rows: int  # rows the adapter yields (primary cohort, including considered-only)
    positives: int
    negatives: int
    negative_basis: str  # "established" or "mixed" (some negatives are only unflagged)
    established_negatives: int

    @property
    def considered_only(self) -> int:
        return self.rows - self.positives - self.negatives

    @property
    def source(self) -> str:
        return f"staged:{self.name}"


STAGED_SETS: dict[str, StagedSet] = {
    entry.name: entry
    for entry in (
        StagedSet(
            "kernelbench-hard-runs",
            "infatoshi__kernelbench-hard-runs/a17d332b967d",
            f"{_RUNS}/corpus-expansion-pq1-20261027/corpora/infatoshi__kernelbench-hard-runs/tools/adapter.py",
            "cc-by-4.0", 30, 11, 19, "established", 19,
        ),
        StagedSet(
            "reflex-sentinel",
            "matu79go__reflex-sentinel-agent-trajectories/9ca0d7ae9893",
            f"{_RUNS}/corpus-expansion-pq1-20261027/corpora/matu79go__reflex-sentinel-agent-trajectories"
            "/tools/adapter.py",
            "cc-by-4.0", 250, 45, 205, "mixed", 50,
        ),
        StagedSet(
            "cuaverifierbench",
            "microsoft__cuaverifierbench/c19eb323cd80",
            f"{_RUNS}/corpus-expansion-pq1-20261027/corpora/microsoft__cuaverifierbench/tools/adapter.py",
            "mit", 106, 77, 29, "established", 29,
        ),
        StagedSet(
            "agent-cheat-index",
            "b0yko__cheat-index/f1eae4893c8b",
            f"{_RUNS}/corpus-expansion-pq2-20261028/corpora/b0yko__cheat-index/tools/adapter.py",
            "apache-2.0", 1148, 62, 1086, "mixed", 270,
        ),
        StagedSet(
            "evilgenie-escalation",
            "wiserhumanexperimental__evilgenie-escalation/1bb777b666b0",
            f"{_RUNS}/corpus-expansion-pq2-20261028/corpora/wiserhumanexperimental__evilgenie-escalation"
            "/tools/adapter.py",
            "mit", 2295, 124, 1990, "mixed", 177,
        ),
        StagedSet(
            "sdf-shortcut-taking",
            "noahrossi__sdf-shortcut-taking/a50109798793",
            f"{_RUNS}/corpus-expansion-pq2-20261028/corpora/noahrossi__sdf-shortcut-taking/tools/adapter.py",
            "cc-by-4.0", 1219, 359, 636, "mixed", 331,
        ),
        StagedSet(
            "researcharena-benchmark-gaming",
            "aisa-group__researcharena-trajectories/8cd737c58bda",
            f"{_RUNS}/corpus-expansion-pq2-20261028/corpora/aisa-group__researcharena-trajectories"
            "/tools/adapter.py",
            "apache-2.0", 41, 21, 20, "established", 20,
        ),
        StagedSet(
            "beam-tripwire",
            "dhairyapandya__beam-tripwire-episodes/e575c837ab56",
            f"{_RUNS}/corpus-expansion-pq2-20261028/corpora/dhairyapandya__beam-tripwire-episodes/tools/adapter.py",
            "apache-2.0", 616, 171, 442, "mixed", 175,
        ),
    )
}

#: Registry sets that are NOT staged runs here, and why.
EXCLUDED_SETS: dict[str, str] = {
    "hackbench": "owned by trace_harness_corpora; CC BY-NC, all-HACK release with no negatives",
    "sleight-bench": "segment-level construction labels (publisher attack vs benign segment), not run-level",
    "assistant-axis": "turn-level observations; the 635 unflagged episodes are weak negatives (FAR upper bound only)",
    "mistrace-reward-hacking": "negatives are only unflagged runs (false-alarm upper bound, not a rate)",
    "rhb-delivery-queue": "negatives are environment-state non-hacks (false-alarm upper bound, not a rate)",
    "apollo-insider-trading": "completion-level rows, not run trajectories; false-alarm rate not supported",
    "hack-ignition": "completion-level rows, not run trajectories; false-alarm rate not supported",
    "paichecker": "Task QA set (task-defect labels), not a trace-detector corpus",
    "automationbench-verified": "Task QA set (positive-only task defects), not a trace-detector corpus",
}


def _hash16(native: str) -> str:
    return hashlib.sha256(native.encode()).hexdigest()[:16]


def _state(spec: Mapping[str, Any], *path: str) -> bool | None:
    """True for ``positive``, False for ``negative``; anything else is considered-only."""
    value: Any = spec
    for key in path:
        value = value[key]
    return {"positive": True, "negative": False}.get(value)


def _flag(spec: Mapping[str, Any]) -> bool | None:
    value = spec["positive"]
    return value if isinstance(value, bool) else None


# set -> (native id of a spec, label of a spec)
_ACCESSORS: dict[str, tuple[Callable[[Mapping[str, Any]], str], Callable[[Mapping[str, Any]], bool | None]]] = {
    "kernelbench-hard-runs": (lambda s: s["trace_key"], _flag),
    "reflex-sentinel": (lambda s: s["trace_key"], _flag),
    "cuaverifierbench": (lambda s: s["trace_key"], _flag),
    "agent-cheat-index": (
        lambda s: _hash16(s["identity"]["run_id"]),
        lambda s: _state(s, "gold", "label_state"),
    ),
    "evilgenie-escalation": (
        lambda s: _hash16(s["identity"]["run_key"]),
        lambda s: _state(s, "labels", "state"),
    ),
    "sdf-shortcut-taking": (
        lambda s: _hash16(s["identity"]["run_id"]),
        lambda s: _state(s, "labels", "state"),
    ),
    "researcharena-benchmark-gaming": (lambda s: s["trace_key"], lambda s: _state(s, "labels", "state")),
    "beam-tripwire": (lambda s: s["identity"]["trace_key"][:16], lambda s: _state(s, "labels", "state")),
}

_ADAPTERS: dict[tuple[str, Path], ModuleType] = {}


def staged_set(name: str) -> StagedSet:
    try:
        return STAGED_SETS[name]
    except KeyError:
        reason = EXCLUDED_SETS.get(name)
        hint = f" (excluded: {reason})" if reason else ""
        raise ValueError(f"unknown staged set {name!r}{hint}; qualifying sets: {sorted(STAGED_SETS)}") from None


def _adapter(entry: StagedSet, repo_root: Path) -> ModuleType:
    path = (repo_root / entry.adapter).resolve()
    key = (entry.name, path)
    if key not in _ADAPTERS:
        if not path.is_file():
            raise FileNotFoundError(f"adapter for {entry.name} not found at {path}")
        module_name = f"evallab_staged_adapter_{entry.name.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load adapter {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        _ADAPTERS[key] = module
    return _ADAPTERS[key]


def _specs(entry: StagedSet, root: Path | None, repo_root: Path) -> Iterator[Mapping[str, Any]]:
    bundle = root if root is not None else STATE_ROOT / entry.bundle
    if not bundle.is_dir():
        raise FileNotFoundError(f"staged bundle for {entry.name} is not on this machine: {bundle}")
    yield from _adapter(entry, repo_root).specs(bundle)


def _rows(
    name: str, root: Path | None, repo_root: Path | None
) -> Iterator[tuple[str, Mapping[str, Any], bool | None]]:
    """(row id, spec, label) per adapter row; refuses a repeated row id."""
    entry = staged_set(name)
    native, label = _ACCESSORS[name]
    seen: set[str] = set()
    for spec in _specs(entry, root, repo_root or REPO_ROOT):
        row_id = native(spec)
        if row_id in seen:
            raise ValueError(f"{name}: duplicate row id {row_id}")
        seen.add(row_id)
        yield f"staged:{name}:{row_id}", spec, label(spec)


def staged_runs(
    set_name: str, limit: int | None = None, *, root: Path | None = None, repo_root: Path | None = None
) -> Iterator[HarnessRun]:
    """Runs of one staged set, in adapter order; ``steps`` keep the adapter's original step ids."""
    entry = staged_set(set_name)
    for count, (run_id, spec, _label) in enumerate(_rows(set_name, root, repo_root)):
        if limit is not None and count >= limit:
            return
        steps = tuple(spec["_steps"])
        yield HarnessRun(run_id, entry.source, SPLIT, OUTCOME, steps, actor_inputs_available(steps))


def staged_labels(
    set_name: str, *, root: Path | None = None, repo_root: Path | None = None
) -> tuple[frozenset[str], dict[str, bool | None], dict[str, frozenset[int]]]:
    """``(run_ids, positive, positive_steps)`` for ``CorpusLabels(name='staged:<set>')``.

    ``run_ids`` is the whole cohort; ``positive`` is None for considered-only rows
    (ambiguous or excluded by the publisher), which are never negatives.
    ``positive_steps`` is empty: no qualifying set has per-step gold, and the
    localization unit is ``"step"`` (original ``step_id``).
    """
    positive = {run_id: label for run_id, _spec, label in _rows(set_name, root, repo_root)}
    return frozenset(positive), positive, {}


def staged_census(
    set_name: str, *, root: Path | None = None, repo_root: Path | None = None
) -> dict[str, Any]:
    """Counts for one set, computed from the adapter: rows, labels, steps median, runs with no actor input."""
    steps: list[int] = []
    no_actor = positives = negatives = 0
    for _run_id, spec, label in _rows(set_name, root, repo_root):
        run_steps = spec["_steps"]
        steps.append(len(run_steps))
        no_actor += not actor_inputs_available(run_steps)
        positives += label is True
        negatives += label is False
    return {
        "set": set_name,
        "rows": len(steps),
        "positives": positives,
        "negatives": negatives,
        "considered_only": len(steps) - positives - negatives,
        "steps_median": statistics.median(steps) if steps else None,
        "no_actor_input": no_actor,
    }
