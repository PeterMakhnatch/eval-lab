"""Judge station: a human judges DEV runs blind, then sees the label and our detectors.

A *queue* is a JSONL file of run ids (``{"run_id": ..., "reason": ..., "reveal": {...}}`` or a
bare id per line). The station serves each admitted DEV run as a timeline (narration, tool
calls, tool outputs) with the benchmark, label, grader result and every detector verdict
hidden. When the judge submits a verdict the label and detectors are revealed and the
agreement is recorded.

Verdicts go to an append-only JSONL outside the repository
(``~/.local/state/evallab-judge/verdicts.jsonl``). Nothing here calls a model or the network.
Admission and labels come from the lab's own helpers: the run loaders admit DEV only (a TEST or
cross-key run is refused and never shown), and labels come from
``run_atlas_score.load_labels``. ``research/answer-keys/`` is never read directly.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import io
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from evallab.production.judge_card import build_card
from evallab.production.judge_order import (
    DEFAULT_CONTROL_RATE,
    DEFAULT_SEED,
    STRATA,
    STRATUM_TEXT,
    Signals,
    collect_signals,
    order_runs,
    stratum_of,
)
from evallab.production.judge_report import (
    build_report,
    effective_records,
    load_rows,
    render_markdown,
)

STATE_DIR = Path.home() / ".local/state/evallab-judge"
VERDICTS_FILE = "verdicts.jsonl"
QUEUES_DIR = "queues"
VERDICT_SCHEMA = "judge-verdict/v1"
UNDO_SCHEMA = "judge-undo/v1"
DRAFTS_FILE = "drafts.json"
ALL_QUEUE = "all-queues"
VERDICTS = ("cheat", "legitimate", "unclear")
EXTRA_MECHANISMS = ("replaced_input_with_expected_output", "other")
MAX_TEXT = 100_000
MAX_NOTE = 2_000
MAX_SECONDS = 6 * 3600
TASK_CHARS = 12_000
DEFAULT_FINDINGS_DIR = Path.home() / ".local/state/evallab-prod-live/data-flywheel/fw2"
DEFAULT_S2_QUEUE = (
    Path.home() / "Developer/research-context/bridge/2026-10-30-factory/ds/raw/S2/judge-queue.jsonl"
)
GUIDE_PATH = Path(__file__).resolve().parents[3] / "docs" / "judge-guide.md"


class JudgeError(ValueError):
    """A request the station refuses; the message is safe to show the judge."""


def blind_id(run_id: str) -> str:
    """The id the judge sees: the same ``run-<16 hex>`` the DT-series packets use."""
    return "run-" + hashlib.sha256(run_id.encode()).hexdigest()[:16]


def mechanisms() -> list[str]:
    """Taxonomy categories of the QA trace reader, plus the two judge-only choices."""
    from evallab.qa.trace_agents import TAXONOMY

    return [*(m for m in TAXONOMY if m != "other"), *EXTRA_MECHANISMS]


# --------------------------------------------------------------------------- #
# queues
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QueueEntry:
    """One queued run. ``signals`` (``{detector: alarm|clear}``) and ``control`` are optional
    ordering hints a queue producer can attach; the station never shows them before a verdict."""

    run_id: str
    reason: str | None = None
    reveal: Mapping[str, Any] | None = None
    priority: int = 0
    source: str = "queue"
    signals: Mapping[str, str] | None = None
    control: bool = False
    source_queues: tuple[str, ...] = ()
    review_priority: int | float | None = None
    review_priority_source: Mapping[str, Any] | None = None


def append_judge_queue_entry(queue_file: Path, entry: QueueEntry) -> bool:
    """Append a QueueEntry to the Judge Station queue file idempotently.

    Returns True if appended, False if run_id was already present.
    """
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    if queue_file.is_file():
        existing = queue_file.read_text(encoding="utf-8").splitlines()
        for line in existing:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if row.get("run_id") == entry.run_id:
                    return False
            except Exception:
                continue
    record = {
        "run_id": entry.run_id,
        "reason": entry.reason,
        "priority": entry.priority,
        "source": entry.source,
        "reveal": entry.reveal,
    }
    if entry.source == "line" or entry.review_priority is not None or entry.review_priority_source is not None:
        record.update(
            review_priority=entry.review_priority,
            review_priority_source=entry.review_priority_source,
        )
    with queue_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return True


def load_queue(path: Path) -> list[QueueEntry]:
    """Read a queue file: JSON objects with ``run_id`` or one bare id per line."""
    entries: dict[str, QueueEntry] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("{"):
            try:
                row = json.loads(text)
            except ValueError as error:
                raise JudgeError(f"{path}:{number}: not JSON ({error})") from error
            run_id = row.get("run_id") if isinstance(row, dict) else None
            if not isinstance(run_id, str) or not run_id.strip():
                raise JudgeError(f"{path}:{number}: a queue row needs a string run_id")
            reason, reveal = row.get("reason"), row.get("reveal")
            if reason is not None and not isinstance(reason, str):
                raise JudgeError(f"{path}:{number}: reason must be text")
            if reveal is not None and not isinstance(reveal, dict):
                raise JudgeError(f"{path}:{number}: reveal must be an object")
            signals, control = row.get("signals"), row.get("control", False)
            if signals is not None and not (
                isinstance(signals, dict)
                and all(isinstance(k, str) and v in ("alarm", "clear") for k, v in signals.items())
            ):
                raise JudgeError(f"{path}:{number}: signals must map detector name to alarm|clear")
            if not isinstance(control, bool):
                raise JudgeError(f"{path}:{number}: control must be true or false")
            priority = row.get("priority", 0)
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise JudgeError(f"{path}:{number}: priority must be a whole number")
            score, provenance = row.get("review_priority"), row.get("review_priority_source")
            if "review_priority" in row or "review_priority_source" in row:
                score = row.get("review_priority")
                if score is not None and (
                    type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 100
                ):
                    raise JudgeError(f"{path}:{number}: review_priority must be null or a score in [0,100]")
                provenance = row.get("review_priority_source")
                if provenance is not None and not isinstance(provenance, dict):
                    raise JudgeError(f"{path}:{number}: review_priority_source must be an object")
            entry = QueueEntry(
                run_id.strip(),
                reason,
                reveal,
                priority,
                str(row.get("source", "queue")),
                signals,
                control,
                review_priority=score,
                review_priority_source=provenance,
            )
        else:
            entry = QueueEntry(text)
        entries.setdefault(entry.run_id, entry)
    if not entries:
        raise JudgeError(f"{path}: the queue is empty")
    return list(entries.values())


# --------------------------------------------------------------------------- #
# data access (the lab's own helpers)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Label:
    state: str  # cheat | clean | considered_only | none | unavailable
    text: str


class RunSource(Protocol):
    def load_runs(self, run_ids: Sequence[str]) -> tuple[dict[str, Any], dict[str, str]]: ...

    def label(self, run_id: str) -> Label: ...

    def detectors(self, run_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]: ...


class LabSource:
    """Admitted-DEV run loading, labels and stored detector verdicts from the lab's helpers."""

    def __init__(self, repo_root: Path, findings_paths: Sequence[Path] | None = None) -> None:
        self.repo_root = repo_root
        self._findings_paths = (
            list(findings_paths)
            if findings_paths is not None
            else sorted(
                p for p in DEFAULT_FINDINGS_DIR.glob("findings-*.jsonl") if "library" not in p.name
            )
        )
        self._ctx: Any = None

    def _context(self) -> Any:
        if self._ctx is None:
            from evallab.failure_library.bank import BankContext

            self._ctx = BankContext(self.repo_root)
        return self._ctx

    def load_runs(self, run_ids: Sequence[str]) -> tuple[dict[str, Any], dict[str, str]]:
        from evallab.production.run_atlas import (
            CHEATBENCH_DATASET,
            AtlasRun,
            cheatbench_runs,
            cloud_runs,
        )
        from evallab.results_home import results_root

        ctx = self._context()
        cheat = sorted({run for run in run_ids if run.startswith(CHEATBENCH_DATASET + ":")})
        cloud = sorted(set(run_ids) - set(cheat))
        loaders: list[tuple[list[str], Iterator[AtlasRun]]] = []
        if cloud:
            loaders.append((cloud, cloud_runs(ctx.cloud_db, registry=ctx.registry, trace_ids=cloud)))
        if cheat:
            loaders.append((cheat, cheatbench_runs(ctx.registry, results_root(), item_ids=cheat)))
        found: dict[str, AtlasRun] = {}
        unavailable: dict[str, str] = {}
        for subjects, runs in loaders:
            try:
                for run in runs:
                    if run.run_id not in subjects:
                        raise ValueError("original loader returned an unrequested subject")
                    if run.run_id in found:
                        del found[run.run_id]
                        raise ValueError("original loader returned a duplicate subject")
                    found[run.run_id] = run
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                unavailable.update({subject: str(error) for subject in subjects if subject not in found})
        for run_id in run_ids:
            if run_id not in found:
                unavailable.setdefault(run_id, "original run unavailable or no longer DEV-admitted")
        return found, unavailable

    def label(self, run_id: str) -> Label:
        from evallab.production.run_atlas_score import load_labels

        try:
            labels = load_labels(
                database=self._context().cloud_db, repo_root=self.repo_root, run_ids=[run_id]
            )
        except (ValueError, OSError) as error:
            return Label("unavailable", f"label unavailable: {error}")
        if run_id not in labels:
            return Label("none", "No label is recorded for this run.")
        value = labels[run_id]
        if value is True:
            return Label("cheat", "The label says this run CHEATED.")
        if value is False:
            return Label("clean", "The label says this run is CLEAN (no cheating).")
        return Label(
            "considered_only",
            "The label says the agent considered cheating but did not do it "
            "(counted as neither a cheat nor a clean run).",
        )

    def detectors(self, run_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        return read_findings(self._findings_paths, set(run_ids))


LOCAL_LOG = logging.getLogger(__name__)
LOCAL_NOT_ADMITTED = "local rollout is not an admitted DEV run"
LOCAL_UNREADABLE = "local rollout is missing or unreadable"
LOCAL_INTEGRITY = "local rollout failed its integrity check"


class _LocalRefused(Exception):
    """A local run the station will not show. ``public`` is safe for the judge; ``detail`` is
    logged server-side only because it can hold paths, hashes and ids."""

    def __init__(self, public: str, detail: str = "") -> None:
        super().__init__(public)
        self.public, self.detail = public, detail


class LocalRunSource:
    """Native Harbor trials pinned by a line ``runs.jsonl``, with cloud/CheatBench fallback.

    ``registry`` is the corpus registry (``assigned_test_anywhere``); it is required so that a
    local run can never skip the TEST-split assignment check the other loaders apply.
    ``sealed_roots`` are folders of sealed material (the answer keys): no served file may be a
    hard link to a file inside them.
    """

    def __init__(
        self,
        manifest: Path,
        fallback: RunSource,
        registry: Any,
        sealed_roots: Sequence[Path] = (),
    ) -> None:
        if registry is None or not hasattr(registry, "assigned_test_anywhere"):
            raise JudgeError("a corpus registry is required to admit local runs")
        self.fallback = fallback
        self.registry = registry
        self.sealed_roots = tuple(sealed_roots)
        self._rows: dict[str, Mapping[str, Any]] = {}
        paths: set[str] = set()
        for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as error:
                raise JudgeError(f"{manifest}:{number}: not JSON ({error})") from error
            if not isinstance(row, dict):
                raise JudgeError(f"{manifest}:{number}: a local run row must be an object")
            run_id, rollout, digest = row.get("run_id"), row.get("rollout_path"), row.get("sha256")
            if not isinstance(run_id, str) or not run_id.strip():
                raise JudgeError(f"{manifest}:{number}: run_id must be nonempty text")
            if not isinstance(rollout, str) or not rollout.strip():
                raise JudgeError(f"{manifest}:{number}: rollout_path must be nonempty text")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise JudgeError(f"{manifest}:{number}: sha256 must be 64 hexadecimal characters")
            run_id = run_id.strip()
            if run_id in self._rows:
                raise JudgeError(f"{manifest}:{number}: duplicate local run_id {run_id}")
            location = rollout.strip().casefold()
            if location in paths:
                raise JudgeError(
                    f"{manifest}:{number}: rollout_path already listed for another run_id"
                )
            paths.add(location)
            self._rows[run_id] = row

    def _load_local(self, run_id: str) -> Any:
        from evallab.production.run_atlas import AtlasRun, outcome_from_reward
        from evallab.rollout_admission import AdmissionRefused, admit_rollout, sealed_inodes
        from evallab.trace_anatomy import atif_run

        row = self._rows[run_id]
        try:
            admitted = admit_rollout(
                str(row["rollout_path"]),
                registry=self.registry,
                row=row,
                run_id=run_id,
                expected_sha256=str(row["sha256"]),
                sealed=sealed_inodes(*self.sealed_roots),
            )
        except AdmissionRefused as error:
            public = (
                LOCAL_INTEGRITY
                if error.integrity
                else LOCAL_UNREADABLE
                if error.unreadable
                else LOCAL_NOT_ADMITTED
            )
            raise _LocalRefused(public, error.detail) from error
        # built from the document the digest covered; nothing is read from disk again
        run = atif_run(admitted.document, str(admitted.trajectory))
        doc = dict(run.doc)
        if submission := admitted.diff or run.submission:
            doc["submission"] = submission
        return AtlasRun(run_id, "native-local", "DEV", outcome_from_reward(row.get("reward")), doc)

    def load_runs(self, run_ids: Sequence[str]) -> tuple[dict[str, Any], dict[str, str]]:
        remote = [run_id for run_id in run_ids if run_id not in self._rows]
        runs, unavailable = self.fallback.load_runs(remote) if remote else ({}, {})
        for run_id in (run_id for run_id in run_ids if run_id in self._rows):
            try:
                runs[run_id] = self._load_local(run_id)
            except _LocalRefused as error:
                LOCAL_LOG.warning("local run %s refused: %s", blind_id(run_id), error.detail)
                unavailable[run_id] = error.public
            except Exception as error:  # fail closed: nothing about the failure reaches the page
                LOCAL_LOG.warning(
                    "local run %s unreadable: %s: %s", blind_id(run_id), type(error).__name__, error
                )
                unavailable[run_id] = LOCAL_UNREADABLE
        return runs, unavailable

    def label(self, run_id: str) -> Label:
        if run_id in self._rows:
            return Label("none", "No dataset label is recorded for this local run.")
        return self.fallback.label(run_id)

    def detectors(self, run_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        return self.fallback.detectors(run_ids)


def read_findings(paths: Iterable[Path], wanted: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Latest stored verdict per (run, detector, version, reader) for the wanted runs."""
    latest: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for path in paths:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not any(run_id in line for run_id in wanted):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                run_id = row.get("run_id")
                if run_id not in wanted or not isinstance(row.get("detector"), str):
                    continue
                key = (
                    run_id,
                    row["detector"],
                    str(row.get("detector_version")),
                    str(row.get("reader_model")),
                )
                if key not in latest or str(row.get("produced_at")) >= str(
                    latest[key].get("produced_at")
                ):
                    latest[key] = row
    result: dict[str, list[dict[str, Any]]] = {}
    for (run_id, detector, version, _), row in sorted(latest.items()):
        steps = [
            s
            for s in row.get("evidence_steps") or []
            if isinstance(s, int) and not isinstance(s, bool)
        ]
        result.setdefault(run_id, []).append(
            {
                "name": f"{detector} ({version})",
                "verdict": str(row.get("verdict")),
                "steps": steps,
                "summary": str(row.get("summary") or "")[:400],
                "model": str(row.get("reader_model") or "") or None,
            }
        )
    return result


# --------------------------------------------------------------------------- #
# the run as a timeline
# --------------------------------------------------------------------------- #


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            part["text"]
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
            else _text(part)
            for part in value
        )
    return json.dumps(value, ensure_ascii=False, default=str)


def _clip(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT] + f"\n[... {len(text) - MAX_TEXT} more characters not shown ...]"


KEYSTROKE_KEYS = frozenset({"keystrokes", "duration", "timeout", "timeout_sec", "is_blocking"})
TASK_COMPLETE = "mark_task_complete"


def _visible(typed: str) -> str:
    """What was typed, character for character: newlines and tabs stay, every other control
    character is shown (carriage return as ``␍``, the rest as ``\\xNN``)."""
    return "".join(
        ch
        if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127)
        else "␍"
        if ch == "\r"
        else f"\\x{ord(ch):02x}"
        for ch in typed
    )


def _display_call(call: Mapping[str, Any]) -> Mapping[str, Any]:
    """The call as the judge reads it. Terminus-2 types ``arguments.keystrokes`` into a
    terminal: shown verbatim as the command (no parsing), and ``mark_task_complete`` is a
    visible step with its arguments verbatim."""
    arguments = call.get("arguments")
    if not isinstance(arguments, Mapping):
        return call
    name = str(call.get("function_name") or call.get("name") or "")
    if name == TASK_COMPLETE:
        shown = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
        text = "agent marked task complete" + (f"\n{shown}" if arguments else "")
        return {**call, "arguments": {"command": text}}
    typed = arguments.get("keystrokes")
    if (
        isinstance(typed, str)
        and not isinstance(arguments.get("command"), str)
        and set(arguments) <= KEYSTROKE_KEYS
    ):
        return {**call, "arguments": {"command": _visible(typed)}}
    return call


def display_steps(steps: Sequence[Any]) -> list[Any]:
    """``steps`` with every tool call in its display form (what the card is built from)."""
    return [
        {
            **step,
            "tool_calls": [
                _display_call(call) if isinstance(call, Mapping) else call
                for call in step["tool_calls"]
            ],
        }
        if isinstance(step, Mapping) and isinstance(step.get("tool_calls"), list)
        else step
        for step in steps
    ]


def _call_text(call: Mapping[str, Any]) -> tuple[str, str]:
    call = _display_call(call)
    name = str(call.get("function_name") or call.get("name") or "tool")
    arguments = call.get("arguments")
    if isinstance(arguments, Mapping):
        if set(arguments) == {"raw"}:
            return name, _text(arguments["raw"])
        command = arguments.get("command")
        if isinstance(command, str) and set(arguments) <= {
            "command",
            "keystrokes",
            "input",
            "path",
        }:
            return name, command
        return name, json.dumps(
            arguments, ensure_ascii=False, indent=2, sort_keys=True, default=str
        )
    return name, _text(arguments)


def build_timeline(run: Any) -> tuple[list[dict[str, str]], list[dict[str, Any]], Any]:
    """``(task_blocks, steps, blind_run)`` of the blinded run; every text is source-name redacted.

    Step numbers are the blinded run's own (the numbering the DT-series packets cite).
    """
    from evallab.production import run_atlas as ra

    blind = ra.blind_run(run)
    steps: list[dict[str, Any]] = []
    for index, raw in enumerate(blind.steps):
        step_id = raw.get("step_id")
        number = (
            int(step_id)
            if isinstance(step_id, int | str) and str(step_id).lstrip("-").isdigit()
            else index
        )
        narration = []
        reasoning = _text(raw.get("reasoning_content"))
        message = _text(raw.get("message"))
        if reasoning.strip():
            narration.append({"kind": "reasoning", "text": ra.redact_sources(_clip(reasoning))})
        if message.strip():
            narration.append({"kind": "message", "text": ra.redact_sources(_clip(message))})
        calls = []
        for call in raw.get("tool_calls") or []:
            if isinstance(call, Mapping):
                name, text = _call_text(call)
                calls.append({"name": name, "text": ra.redact_sources(_clip(text))})
        outputs = []
        observation = raw.get("observation")
        for result in (
            observation.get("results") if isinstance(observation, Mapping) else None
        ) or []:
            content = _text(result.get("content") if isinstance(result, Mapping) else result)
            if content.strip():
                outputs.append(ra.redact_sources(_clip(content)))
        steps.append(
            {
                "n": number,
                "source": str(raw.get("source") or "unknown"),
                "narration": narration,
                "calls": calls,
                "outputs": outputs,
            }
        )
    task: list[dict[str, str]] = []
    budget = TASK_CHARS
    for step in steps:
        setup = step["source"] != "agent" or any(
            n["text"].lstrip().startswith("[system]") for n in step["narration"]
        )
        if not setup or step["calls"] or step["outputs"]:
            break
        for part in step["narration"]:
            if budget > 0 and part["text"].strip():
                task.append(
                    {"step": step["n"], "source": step["source"], "text": part["text"][:budget]}
                )
                budget -= len(part["text"])
    return task, steps, blind


# --------------------------------------------------------------------------- #
# the station
# --------------------------------------------------------------------------- #


def _agrees(label: Label, verdict: str) -> bool | None:
    if verdict == "unclear":
        return None
    if label.state == "cheat":
        return verdict == "cheat"
    if label.state == "clean":
        return verdict == "legitimate"
    return None


@dataclass
class Loaded:
    """One admitted run, blinded: the full timeline and the compact card."""

    run: Any
    task: list[dict[str, str]]
    steps: list[dict[str, Any]]
    card: dict[str, Any]


DRAFT_FIELDS = (
    "verdict",
    "mechanism",
    "confidence",
    "confidence_touched",
    "evidence_steps",
    "note",
    "opened_timeline",
)


def _merge_entries(first: QueueEntry, other: QueueEntry) -> QueueEntry:
    """One entry for a run that sits in several queues: nothing either queue says is dropped."""
    reasons = [r for r in dict.fromkeys([first.reason, other.reason]) if r]
    reveal: dict[str, Any] = dict(first.reveal or {})
    for key, identity in (
        ("detectors", ("name", "verdict", "steps")),
        ("notes", ("title", "text")),
    ):
        seen: dict[tuple[str, ...], Any] = {}
        for item in [*(first.reveal or {}).get(key, []), *(other.reveal or {}).get(key, [])]:
            seen.setdefault(tuple(str(item.get(k)) for k in identity), item)
        if seen:
            reveal[key] = list(seen.values())
    for key, value in (other.reveal or {}).items():
        reveal.setdefault(key, value)
    signals: dict[str, str] = dict(first.signals or {})
    for name, verdict in (other.signals or {}).items():
        signals[name] = "alarm" if "alarm" in (verdict, signals.get(name)) else "clear"
    priority_entry = (
        first if first.review_priority is not None or first.review_priority_source is not None
        else other
    )
    return QueueEntry(
        first.run_id,
        " | ".join(reasons) or None,
        reveal or None,
        max(first.priority, other.priority),
        first.source,
        signals or None,
        first.control or other.control,
        tuple(dict.fromkeys([*first.source_queues, *other.source_queues])),
        review_priority=priority_entry.review_priority,
        review_priority_source=priority_entry.review_priority_source,
    )


def merge_queues(queues: Mapping[str, Sequence[QueueEntry]]) -> dict[str, Sequence[QueueEntry]]:
    """The named queues plus, when there are several, one combined queue of every distinct run.

    Every entry records the queues it came from (``source_queues``); in the combined queue a run
    that sits in several queues keeps every queue's reason, notes and signals, and is a control if
    any queue says so."""
    merged: dict[str, Sequence[QueueEntry]] = {
        name: [replace(e, source_queues=(name,)) for e in entries]
        for name, entries in queues.items()
    }
    if len(queues) > 1:
        combined: dict[str, QueueEntry] = {}
        for entries in merged.values():
            for entry in entries:
                known = combined.get(entry.run_id)
                combined[entry.run_id] = entry if known is None else _merge_entries(known, entry)
        merged = {ALL_QUEUE: list(combined.values()), **merged}
    return merged


@dataclass
class Station:
    source: RunSource
    queues: Mapping[str, Sequence[QueueEntry]]
    judge: str = "peter"
    state_dir: Path = STATE_DIR
    keep_order: bool = False
    blind_order: bool = False
    seed: str = DEFAULT_SEED
    control_rate: float = DEFAULT_CONTROL_RATE
    show_hints: bool = True
    clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    guide_path: Path = GUIDE_PATH
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _opened: dict[tuple[str, str], float] = field(default_factory=dict, init=False)
    _cache: OrderedDict[str, Loaded] = field(default_factory=OrderedDict, init=False)
    _refused: dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        merged = merge_queues(self.queues)
        self.order_mode = (
            "file" if self.keep_order else "shuffled" if self.blind_order else "information"
        )
        wanted = sorted({e.run_id for entries in merged.values() for e in entries})
        self._detectors = self.source.detectors(wanted)
        self._public: dict[str, str] = {}
        self._labels: dict[str, str] = {}
        for number, name in enumerate(n for n in merged if n != ALL_QUEUE):
            self._public[f"q{number + 1}"] = name
            self._labels[name] = f"Queue {number + 1}"
        if ALL_QUEUE in merged:
            self._public["all"] = ALL_QUEUE
            self._labels[ALL_QUEUE] = "All runs, most informative first"
        self._ordered: dict[str, list[QueueEntry]] = {}
        self._strata: dict[str, dict[str, str]] = {}
        self._by_blind: dict[str, dict[str, QueueEntry]] = {}
        for name, entries in merged.items():
            by_id = {e.run_id: e for e in entries}
            inputs = [(e.run_id, self._signals(e), e.control, e.priority) for e in entries]
            if self.keep_order:
                ordered = [(rid, stratum_of(sig, control=ctl)) for rid, sig, ctl, _ in inputs]
            else:
                ordered = order_runs(
                    name,
                    inputs,
                    seed=self.seed,
                    control_rate=self.control_rate,
                    shuffle=self.blind_order,
                )
            self._ordered[name] = [by_id[rid] for rid, _ in ordered]
            self._strata[name] = dict(ordered)
            self._by_blind[name] = {blind_id(e.run_id): e for e in self._ordered[name]}

    def _signals(self, entry: QueueEntry) -> Signals:
        detectors = [
            *self._detectors.get(entry.run_id, []),
            *(entry.reveal or {}).get("detectors", []),
        ]
        return collect_signals(detectors, entry.signals)

    def strata_counts(self, name: str) -> dict[str, int]:
        """How many runs of a queue sit in each stratum (operator view; never sent to the page)."""
        counts = dict.fromkeys(STRATA, 0)
        for stratum in self._strata[self._resolve(name)].values():
            counts[stratum] += 1
        return counts

    # -- storage ---------------------------------------------------------- #

    @property
    def verdicts_path(self) -> Path:
        return self.state_dir / VERDICTS_FILE

    @property
    def drafts_path(self) -> Path:
        return self.state_dir / DRAFTS_FILE

    def read_verdicts(self) -> list[dict[str, Any]]:
        """Every stored row: verdict records and undo markers, in file order."""
        return load_rows(self.verdicts_path)

    def _latest(self) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (str(r.get("judge")), str(r.get("run_id"))): r
            for r in effective_records(self.read_verdicts())
        }

    def _append(self, record: Mapping[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.verdicts_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _stamp(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))

    def _read_drafts(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            data = json.loads(self.drafts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_drafts(self, drafts: Mapping[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        scratch = self.drafts_path.with_suffix(".tmp")
        with scratch.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(drafts, ensure_ascii=False, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, self.drafts_path)

    # -- views ------------------------------------------------------------ #

    def state(self) -> dict[str, Any]:
        done = self._latest()
        return {
            "judge": self.judge,
            "mechanisms": mechanisms(),
            "show_hints": self.show_hints,
            "queues": [
                {
                    "name": self._public_of(name),
                    "label": self._labels.get(name, name),
                    "total": len(entries),
                    "done": sum(1 for e in entries if (self.judge, e.run_id) in done),
                }
                for name, entries in self._ordered.items()
            ],
        }

    def queue(self, name: str) -> list[dict[str, Any]]:
        entries = self._entries(name)
        done = self._latest()
        return [
            {
                "position": index + 1,
                "id": blind_id(e.run_id),
                "done": (self.judge, e.run_id) in done,
                "refused": e.run_id in self._refused,
            }
            for index, e in enumerate(entries)
        ]

    def _public_of(self, real: str) -> str:
        return next((p for p, name in self._public.items() if name == real), real)

    def _resolve(self, name: str) -> str:
        """The real queue name for a page-facing id (a real name is accepted too)."""
        real = self._public.get(name, name)
        if real not in self._ordered:
            raise JudgeError("unknown queue")
        return real

    def _entries(self, name: str) -> list[QueueEntry]:
        return self._ordered[self._resolve(name)]

    def _entry(self, name: str, blind: str) -> QueueEntry:
        name = self._resolve(name)
        try:
            return self._by_blind[name][blind]
        except KeyError as error:
            raise JudgeError("unknown run") from error

    def _run(self, entry: QueueEntry) -> Loaded:
        with self._lock:
            if entry.run_id in self._cache:
                self._cache.move_to_end(entry.run_id)
                return self._cache[entry.run_id]
            if entry.run_id in self._refused:
                raise JudgeError(self._refused[entry.run_id])
            runs, unavailable = self.source.load_runs([entry.run_id])
            run = runs.get(entry.run_id)
            if run is None:
                reason = "refused: " + unavailable.get(
                    entry.run_id,
                    "not an admitted DEV run (TEST, held-out and unknown runs are never shown)",
                )
                self._refused[entry.run_id] = reason
                raise JudgeError(reason)
            if str(getattr(run, "split", "DEV")).upper() != "DEV":
                self._refused[entry.run_id] = "refused: not a DEV run"
                raise JudgeError(self._refused[entry.run_id])
            try:
                task, steps, blind = build_timeline(run)
            except ValueError as error:
                self._refused[entry.run_id] = f"refused: cannot blind this run ({error})"
                raise JudgeError(self._refused[entry.run_id]) from error
            try:
                card = build_card(
                    run.run_id,
                    display_steps(blind.steps),
                    [s["n"] for s in steps],
                    hints=self.show_hints,
                    submission=(
                        run.doc.get("submission") if run.source == "native-local" else None
                    ),
                )
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                card = {
                    "error": f"the card could not be built ({type(error).__name__}); use the timeline"
                }
            self._cache[entry.run_id] = Loaded(run, task, steps, card)
            while len(self._cache) > 6:
                self._cache.popitem(last=False)
            return self._cache[entry.run_id]

    def run_view(self, name: str, blind: str) -> dict[str, Any]:
        """Everything the judge may see before the verdict. Never a label, detector or stratum."""
        entry = self._entry(name, blind)
        name = self._resolve(name)
        loaded = self._run(entry)
        position = (
            next(i for i, e in enumerate(self._ordered[name]) if e.run_id == entry.run_id) + 1
        )
        with self._lock:
            self._opened[(name, entry.run_id)] = self.monotonic()
        previous = self._latest().get((self.judge, entry.run_id))
        draft = self._read_drafts().get(self.judge, {}).get(entry.run_id)
        return {
            "id": blind,
            "position": position,
            "total": len(self._ordered[name]),
            "task": loaded.task,
            "steps": loaded.steps,
            "card": loaded.card,
            "draft": draft,
            "previous": (
                {
                    "verdict": previous["verdict"],
                    "ts": previous["ts"],
                    "reveal": previous.get("revealed"),
                }
                if previous
                else None
            ),
        }

    def reveal(self, entry: QueueEntry, run: Any) -> dict[str, Any]:
        label = self.source.label(entry.run_id)
        detectors = list(self._detectors.get(entry.run_id, []))
        notes: list[dict[str, str]] = []
        extra = entry.reveal or {}
        for row in extra.get("detectors") or []:
            detectors.append(
                {
                    "name": str(row.get("name")),
                    "verdict": str(row.get("verdict")),
                    "steps": [s for s in row.get("steps") or [] if isinstance(s, int)],
                    "summary": str(row.get("summary") or "")[:400],
                    "model": row.get("model"),
                }
            )
        for name, verdict in (entry.signals or {}).items():
            detectors.append(
                {"name": name, "verdict": verdict, "steps": [], "summary": "", "model": None}
            )
        for note in extra.get("notes") or []:
            notes.append({"title": str(note.get("title")), "text": str(note.get("text"))[:1500]})
        outcome = str(getattr(run, "outcome", "unknown"))
        revealed = {
            "label": {"state": label.state, "text": label.text},
            "grader": None if outcome == "unknown" else outcome,
            "queue_reason": entry.reason,
            "detectors": detectors,
            "notes": notes,
        }
        decision, audit = extra.get("decision"), extra.get("audit")
        if isinstance(decision, str):
            revealed["decision"] = decision[:200]
        if isinstance(audit, bool):
            revealed["audit"] = audit
        if entry.source == "line" or entry.review_priority is not None or entry.review_priority_source is not None:
            revealed["review_priority"] = entry.review_priority
            revealed["review_priority_source"] = entry.review_priority_source
        if isinstance(extra.get("keep_audit"), bool):
            revealed["keep_audit"] = extra["keep_audit"]
        return revealed

    # -- drafts ----------------------------------------------------------- #

    def save_draft(self, name: str, blind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Autosave an unsubmitted verdict so a reload or crash loses nothing. Not a verdict."""
        entry = self._entry(name, blind)
        draft = {k: payload[k] for k in DRAFT_FIELDS if k in payload}
        verdict = draft.get("verdict")
        note = draft.get("note", "")
        steps = draft.get("evidence_steps", [])
        if verdict not in (None, *VERDICTS):
            raise JudgeError("verdict must be cheat, legitimate or unclear")
        if not isinstance(note, str) or len(note) > MAX_NOTE:
            raise JudgeError(f"the note must be text of at most {MAX_NOTE} characters")
        if (
            not isinstance(steps, list)
            or len(steps) > 500
            or not all(isinstance(s, int) and not isinstance(s, bool) for s in steps)
        ):
            raise JudgeError("evidence steps must be a list of step numbers")
        if draft.get("mechanism") not in (None, *mechanisms()):
            raise JudgeError("unknown mechanism")
        confidence = draft.get("confidence")
        if confidence is not None and (isinstance(confidence, bool) or confidence not in (1, 2, 3)):
            raise JudgeError("confidence must be 1, 2 or 3")
        for key in ("confidence_touched", "opened_timeline"):
            if key in draft and not isinstance(draft[key], bool):
                raise JudgeError(f"{key} must be true or false")
        with self._lock:
            drafts = self._read_drafts()
            drafts.setdefault(self.judge, {})[entry.run_id] = {**draft, "ts": self._stamp()}
            self._write_drafts(drafts)
        return {"saved": True}

    # -- verdicts --------------------------------------------------------- #

    def submit(self, name: str, blind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        entry = self._entry(name, blind)
        name = self._resolve(name)
        loaded = self._run(entry)
        verdict = payload.get("verdict")
        if verdict not in VERDICTS:
            raise JudgeError("verdict must be cheat, legitimate or unclear")
        mechanism = payload.get("mechanism") or None
        if mechanism is not None and mechanism not in mechanisms():
            raise JudgeError("unknown mechanism")
        if verdict == "cheat" and mechanism is None:
            raise JudgeError("pick how it cheated (or 'other')")
        confidence = payload.get("confidence")
        if isinstance(confidence, bool) or confidence not in (1, 2, 3):
            raise JudgeError("confidence must be 1, 2 or 3")
        cited = payload.get("evidence_steps") or []
        valid = {s["n"] for s in loaded.steps}
        if not isinstance(cited, list) or any(
            isinstance(s, bool) or not isinstance(s, int) or s not in valid for s in cited
        ):
            raise JudgeError("evidence steps must be steps of this run")
        note = payload.get("note") or ""
        if not isinstance(note, str) or len(note) > MAX_NOTE:
            raise JudgeError(f"the note must be text of at most {MAX_NOTE} characters")
        touched, timeline = (
            payload.get("confidence_touched", True),
            payload.get("opened_timeline", False),
        )
        if not isinstance(touched, bool) or not isinstance(timeline, bool):
            raise JudgeError("confidence_touched and opened_timeline must be true or false")
        with self._lock:
            live = self._latest().get((self.judge, entry.run_id))
            if live is not None:
                # A held or doubled Enter, or two tabs: the first verdict stands. Judging a run
                # again goes through undo, so the second look is flagged as having seen the label.
                if live.get("verdict") != verdict:
                    raise JudgeError(
                        f"this run is already judged as {live.get('verdict')}; "
                        "undo that verdict first to change it"
                    )
                return {
                    "saved": True,
                    "duplicate": True,
                    "id": live.get("id"),
                    "reveal": live.get("revealed"),
                    "agreement": live.get("agreement"),
                    "seconds": live.get("seconds"),
                }
            opened = self._opened.get((name, entry.run_id))
            if opened is None:
                raise JudgeError("open the run before judging it")
            seconds = min(MAX_SECONDS, max(0.0, self.monotonic() - opened))
            revealed = self.reveal(entry, loaded.run)
            stratum = self._strata[name].get(entry.run_id)
            revealed["queue"] = ", ".join(entry.source_queues) or name
            revealed["source_queues"] = list(entry.source_queues) or [name]
            revealed["stratum"] = stratum
            revealed["stratum_text"] = STRATUM_TEXT.get(stratum or "")
            label = Label(revealed["label"]["state"], revealed["label"]["text"])
            alarms = {
                (f"{d['name']} @ {d['model']}" if d.get("model") else d["name"]): (
                    d["verdict"] == "alarm"
                )
                == (verdict == "cheat")
                for d in revealed["detectors"]
                if d["verdict"] in ("alarm", "clear") and verdict != "unclear"
            }
            earlier = any(
                r.get("judge") == self.judge and r.get("run_id") == entry.run_id
                for r in self.read_verdicts()
            )
            record = {
                "schema": VERDICT_SCHEMA,
                "id": uuid.uuid4().hex,
                "judge": self.judge,
                "run_id": entry.run_id,
                "blind_id": blind,
                "queue": name,
                "source_queues": list(entry.source_queues) or [name],
                "order": self.order_mode,
                "stratum": stratum,
                "verdict": verdict,
                "mechanism": mechanism,
                "evidence_steps": sorted(set(cited)),
                "confidence": confidence,
                "confidence_touched": touched,
                "note": note,
                "seconds": round(seconds, 1),
                "opened_timeline": timeline,
                "card_hints": self.show_hints,
                "seen_label_before": earlier,
                "revealed": revealed,
                "agreement": _agrees(label, verdict),
                "detector_agreement": alarms,
                "ts": self._stamp(),
            }
            self._append(record)
            drafts = self._read_drafts()
            if drafts.get(self.judge, {}).pop(entry.run_id, None) is not None:
                self._write_drafts(drafts)
        return {
            "saved": True,
            "id": record["id"],
            "reveal": revealed,
            "agreement": record["agreement"],
            "seconds": record["seconds"],
        }

    def undo_last(self) -> dict[str, Any]:
        """Take back this judge's most recent verdict (append-only: a marker, not a deletion).

        The judge has already seen the label, so any later verdict on the same run is recorded
        with ``seen_label_before`` and left out of the agreement counts.
        """
        with self._lock:
            mine = [
                r for r in effective_records(self.read_verdicts()) if r.get("judge") == self.judge
            ]
            if not mine:
                raise JudgeError("nothing to undo")
            last = mine[-1]
            self._append(
                {
                    "schema": UNDO_SCHEMA,
                    "judge": self.judge,
                    "run_id": last["run_id"],
                    "blind_id": last.get("blind_id"),
                    "undoes": last["id"],
                    "ts": self._stamp(),
                }
            )
        return {
            "undone": last.get("blind_id"),
            "queue": self._public_of(str(last.get("queue"))),
            "verdict": last["verdict"],
        }

    # -- reports ---------------------------------------------------------- #

    def stats(self) -> dict[str, Any]:
        mine = [r for r in self._latest().values() if r.get("judge") == self.judge]
        decided = [r for r in mine if r.get("agreement") is not None]
        return {
            "judged": len(mine),
            "by_verdict": {v: sum(1 for r in mine if r["verdict"] == v) for v in VERDICTS},
            "agree_with_label": sum(1 for r in decided if r["agreement"]),
            "compared_with_label": len(decided),
        }


def export_verdicts(
    path: Path, out: Path, fmt: str, *, judge: str | None = None, history: bool = False
) -> int:
    """Write the latest live verdict per (judge, run); ``history`` writes every verdict record,
    undone ones included and flagged."""
    all_rows = load_rows(path)
    undone = {str(r.get("undoes")) for r in all_rows if r.get("schema") == UNDO_SCHEMA}
    rows: list[dict[str, Any]]
    if history:
        rows = [
            {**r, "undone": r.get("id") in undone}
            for r in all_rows
            if r.get("schema") == VERDICT_SCHEMA
        ]
    else:
        rows = effective_records(all_rows)
    if judge:
        rows = [r for r in rows if r["judge"] == judge]
    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        out.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
        )
        return len(rows)
    columns = [
        "judge", "run_id", "blind_id", "queue", "stratum", "verdict", "mechanism", "evidence_steps",
        "confidence", "confidence_touched", "note", "seconds", "opened_timeline", "label_state",
        "agreement", "detector_agreement", "seen_label_before", "undone", "ts",
    ]  # fmt: skip
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, columns, lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow(
            {
                **{
                    c: r.get(c)
                    for c in columns
                    if c not in ("label_state", "evidence_steps", "detector_agreement")
                },
                "evidence_steps": " ".join(str(s) for s in r.get("evidence_steps") or []),
                "label_state": (r.get("revealed") or {}).get("label", {}).get("state"),
                "detector_agreement": json.dumps(r.get("detector_agreement") or {}, sort_keys=True),
            }
        )
    out.write_text(buffer.getvalue(), encoding="utf-8")
    return len(rows)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def discover_queues(state_dir: Path, extra: Sequence[str]) -> dict[str, list[QueueEntry]]:
    """``NAME=PATH`` / ``PATH`` arguments, or every queue under the state dir plus the S2 census."""
    queues: dict[str, list[QueueEntry]] = {}
    if extra:
        for spec in extra:
            name, sep, path = spec.partition("=")
            file = Path(path if sep else spec).expanduser()
            queues[name if sep else file.stem] = load_queue(file)
        return queues
    directory = state_dir / QUEUES_DIR
    for file in sorted(directory.glob("*.jsonl")) if directory.is_dir() else []:
        queues[file.stem] = load_queue(file)
    if DEFAULT_S2_QUEUE.is_file():
        queues.setdefault("goodfire-census", load_queue(DEFAULT_S2_QUEUE))
    return queues





def _serve(args: argparse.Namespace, root: Path, **_: Any) -> int:
    from evallab.production.judge_web import serve

    try:
        queues = discover_queues(args.state_dir, args.queue)
        source: RunSource = LabSource(root, args.findings)
        if args.local_runs:
            from evallab import hack_drop as hd

            source = LocalRunSource(
                args.local_runs,
                source,
                hd._get_corpus_registry(root),
                sealed_roots=[root / "research" / "answer-keys"],
            )
    except (JudgeError, OSError, RuntimeError) as error:
        print(f"judge: {error}")
        return 2
    if not queues:
        print("judge: no queue found. Pass --queue FILE.")
        return 2
    station = Station(
        source,
        queues,
        judge=args.judge,
        state_dir=args.state_dir,
        keep_order=args.keep_order,
        blind_order=args.blind_order,
        seed=args.seed,
        control_rate=args.control_rate,
        show_hints=not args.no_hints,
    )
    return serve(station, port=args.port, open_browser=args.open)




def _export(args: argparse.Namespace, root: Path, **_: Any) -> int:
    count = export_verdicts(
        args.state_dir / VERDICTS_FILE,
        args.out,
        args.format,
        judge=args.judge,
        history=args.history,
    )
    print(f"wrote {count} verdicts to {args.out}")
    return 0


def _report(args: argparse.Namespace, root: Path, **_: Any) -> int:
    report = build_report(load_rows(args.state_dir / VERDICTS_FILE), judge=args.judge)
    text = (
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True)
        if args.json
        else render_markdown(report)
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def _plan(args: argparse.Namespace, root: Path, **_: Any) -> int:
    try:
        queues = discover_queues(args.state_dir, args.queue)
    except (JudgeError, OSError) as error:
        print(f"judge: {error}")
        return 2
    station = Station(
        LabSource(root, args.findings),
        queues,
        state_dir=args.state_dir,
        seed=args.seed,
        control_rate=args.control_rate,
    )
    for name, entries in station._ordered.items():
        counts = station.strata_counts(name)
        print(f"{name}: {len(entries)} runs; " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        if args.show_order:
            print("  " + " ".join(station._strata[name][e.run_id][:4] for e in entries))
    return 0


