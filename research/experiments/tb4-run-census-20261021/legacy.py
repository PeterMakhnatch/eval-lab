"""Read-only adapter over the frozen RC-1 implementation (research/experiments/tb4-run-census-20261020).

Only the two DEV tasks of the original eight (distributed-dedup, vf2-speedup-networkx) are routed
here. The six historical TEST tasks stay refused until ``validate_legacy8`` has verified the parent's
authorization and consumed its once-only lock (``allow_historical_test``). The historical modules,
labels and results are NOT modified. RC-1 per-run labels contain TEST rows too, so
``legacy_dev_labels`` drops every non-DEV line BEFORE it is parsed.
"""
from __future__ import annotations

import hashlib
import importlib
import sys
from pathlib import Path
from typing import Any

from census_core import LEGACY, LEGACY_DEV_TASKS, ROOT
from evidence import Trial

LEGACY_DIR = ROOT / "research/experiments/tb4-run-census-20261020"
_ALLOWED: set[str] = set(LEGACY_DEV_TASKS)


def allow_historical_test(tasks: set[str]) -> None:
    """Called ONLY by validate_legacy8 after authorization + lock; never by the DEV census."""
    _ALLOWED.update(tasks)


def _load(name: str):
    if str(LEGACY_DIR) not in sys.path:
        sys.path.append(str(LEGACY_DIR))
    return importlib.import_module(name)


def _guard(task: str) -> None:
    if task not in _ALLOWED:
        raise SystemExit(f"legacy adapter refuses {task!r}")


def dedup_fail(trial: Trial):
    from census_core import NEITHER, TASK_DEFECT, UNMEASURED
    from fail_rules import Decision, mid

    fc = _load("fail_census")
    ev = fc.Evidence.read(trial.dir)
    d = fc.classify("distributed-dedup", ev)
    if d.label == "task_defect":
        slug = "scalability_floor" if d.mechanism == fc.FLOOR else "hidden_memory_shuffle_budget"
        return Decision(TASK_DEFECT, [mid("distributed-dedup", slug)], d.evidence, d.confidence,
                        {"tags": d.tags, **d.facts})
    if d.label == "unmeasured":
        return Decision(UNMEASURED, [], d.evidence, "low")
    return Decision(NEITHER, [], d.evidence, d.confidence)


def fail_classify(task: str, trial: Trial) -> dict[str, Any]:
    """RC-1 deterministic fail rule (historical tasks only; used by the gated replay)."""
    _guard(task)
    fc = _load("fail_census")
    d = fc.classify(task, fc.Evidence.read(trial.dir))
    return {"label": d.label, "mechanism": d.mechanism, "evidence": d.evidence}


def questions(task: str) -> dict[str, str]:
    _guard(task)
    return dict(_load("pass_census").QUESTIONS[task])


def sources_and_commands(trial: Trial, task: str):
    """Final exported source files + recorded tool commands, exactly as RC-1 read them."""
    _guard(task)
    pc = _load("pass_census")
    return pc.source_files(trial.dir, task), pc.tool_commands(trial.dir)


def metrics(trial: Trial, task: str) -> dict[str, Any]:
    _guard(task)
    pc = _load("pass_census")
    return pc.biped_metrics(trial.dir) if task == "biped-contact-dynamics" else {}


def direct_findings(task: str, sources, commands, metrics_: dict[str, Any] | None = None):
    _guard(task)
    return _load("pass_census").direct_findings(task, sources, commands, metrics_ or {})


def needs_reading(task: str, sources, commands) -> bool:
    _guard(task)
    return bool(_load("pass_census").needs_reading(task, sources, commands))


def excerpt(task: str, sources, commands, metrics_: dict[str, Any] | None = None):
    _guard(task)
    return _load("pass_census").excerpt(task, sources, commands, metrics_ or {})


def static_adjudications(task: str, sources) -> dict[str, dict[str, str]]:
    """Source-hash-pinned negative proofs of RC-1 (vf2 only): mechanism_id -> evidence."""
    _guard(task)
    pc = _load("pass_census")
    hashes = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sources}
    out: dict[str, dict[str, str]] = {}
    for digest in hashes.values():
        for key, ev in pc.STATIC_PROOFS.get(digest, {}).items():
            out[key] = {"answer": "no", "evidence": ev}
    if task == "vf2-speedup-networkx":
        present = set(hashes.values())
        for required, ev in pc.CONSTRUCTION_NEGATIVE_PROOFS:
            if required <= present:
                out["vf2_untimed"] = {"answer": "no", "evidence": ev}
    return out


def legacy_labels(tasks: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """RC-1 per-run labels keyed by Hub trial_id. Defaults to the DEV tasks only; other tasks are
    returned only after ``allow_historical_test`` (validate_legacy8)."""
    want = set(tasks) if tasks is not None else set(LEGACY_DEV_TASKS)
    for t in want:
        _guard(t)
    rows: list[dict[str, Any]] = []
    for name in ("labels-pass.jsonl", "labels-fail.jsonl"):
        path = LEGACY / name
        if path.exists():
            rows.extend(_filter(path, want))
    return {r["trial_id"]: r for r in rows}


def _filter(path: Path, tasks: set[str]) -> list[dict[str, Any]]:
    import json

    needles = tuple(f'"task":"{t}"' for t in tasks)
    out = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if any(n in line for n in needles):
                row = json.loads(line)
                if row["task"] in tasks:
                    out.append(row)
    return out


def legacy_dev_labels() -> dict[str, dict[str, Any]]:
    return legacy_labels(set(LEGACY_DEV_TASKS))

