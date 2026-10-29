"""Live Supervisor windows: the observation behind each live decision, for LS-3 adjudication.

Design: ``docs/live-supervisor.md`` (Records). ``decisions-v1.jsonl`` keeps the decision only;
the adjudicator (:mod:`live_supervisor_adjudicate`) also needs the window the reader saw.
:class:`WindowLog` appends one ``windows-v1.jsonl`` row per recorded decision, in exactly the
shape :func:`live_supervisor_adjudicate.window_record` writes, keyed by
:func:`live_supervisor_adjudicate.row_key` of the decision record (``run_id@observed_at``), so a
decision and its window join by construction.

A persisted window is bounded and blinded. It carries the last :data:`~live_supervisor_triage.WINDOW_EVENTS`
events, each at most :data:`~live_supervisor_triage.EVENT_CHARS` characters: what the reader prompt
shows. At most :data:`~live_supervisor_triage.MAX_HITS` trigger hits, scalar metrics only, and
every string passes ``run_atlas.redact_sources``, so no benchmark or dataset name is stored. Metric
keys that could carry a label, split or reward are dropped. A whole row is capped at
:data:`MAX_WINDOW_BYTES` by dropping its oldest events. The only identifier a window holds is its
join key; the adjudicator never shows it.

Events of an OMP lane are kept only when the fleet source read that lane as an authorized lane
(owner ``fleet``, ``authorized`` and ``session_read`` true); a subject owned by Peter never has
events. This repeats the fleet source's own rule, so a source that leaks cannot persist a transcript.

:func:`export_rows` turns decisions plus windows into the adjudicator's ``--rows`` input, one
file per arm: ``model`` (decisions a model reader made) or ``rules`` (the rule-table proposal).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from evallab.production import live_supervisor as ls
from evallab.production.live_supervisor_adjudicate import (
    HIDDEN_KEYS,
    METRIC_CHARS,
    row_key,
    window_record,
)
from evallab.production.live_supervisor_triage import (
    DETAIL_CHARS,
    EVENT_CHARS,
    FORBIDDEN_METRIC_KEY,
    MAX_HITS,
    WINDOW_EVENTS,
)
from evallab.production.run_atlas import bound, redact_sources

WINDOWS_FILE = "windows-v1.jsonl"
#: A row above this is trimmed from its oldest event; typical rows are a few KB (see the doc's size note).
MAX_WINDOW_BYTES = 64_000
MAX_METRICS = 40
ARMS = ("model", "rules")
_TAIL_BLOCK = 65_536


# --------------------------------------------------------------------------- building a window


def _events_allowed(observation: ls.Observation) -> bool:
    """Whether this subject's event text may be persisted (fail closed)."""
    subject, metrics = observation.subject, observation.metrics
    if subject.owner == "peter":
        return False
    if subject.kind in ls.NUDGEABLE_KINDS:
        return subject.owner == "fleet" and metrics.get("authorized") is True and metrics.get("session_read") is True
    return True


def _clean(text: str, limit: int) -> str:
    return bound(redact_sources(text), limit)


def _metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for name in sorted(metrics, key=str):
        value = metrics[name]
        if str(name).lower() in HIDDEN_KEYS or FORBIDDEN_METRIC_KEY.search(str(name)):
            continue
        if isinstance(value, str):
            value = _clean(value, METRIC_CHARS)
        elif not (value is None or isinstance(value, int | float | bool)):
            continue
        kept[redact_sources(str(name))] = value
        if len(kept) >= MAX_METRICS:
            break
    return kept


def _hits(hits: Sequence[ls.TriggerHit]) -> list[ls.TriggerHit]:
    return [replace(h, detail=_clean(h.detail, DETAIL_CHARS)) for h in hits[:MAX_HITS]]


def build_window(key: str, observation: ls.Observation, hits: Sequence[ls.TriggerHit]) -> dict[str, Any]:
    """The bounded, blinded ``--windows`` row for one observation."""
    events = observation.events[-WINDOW_EVENTS:] if _events_allowed(observation) else ()
    events = tuple(replace(e, source=_clean(e.source, EVENT_CHARS), text=_clean(e.text, EVENT_CHARS)) for e in events)
    observed = replace(observation, events=events, metrics=_metrics(observation.metrics))
    shown = _hits(hits)
    record = window_record(key, observed, shown)
    while events and len(json.dumps(record, sort_keys=True).encode()) > MAX_WINDOW_BYTES:
        events = events[1:]
        record = window_record(key, replace(observed, events=events), shown)
    return record


# --------------------------------------------------------------------------- appending


def _repair_tail(handle: Any) -> None:
    """Cut a torn last line (a crash mid-write) so the next row starts on its own line."""
    size = handle.seek(0, os.SEEK_END)
    if size == 0:
        return
    handle.seek(size - 1)
    if handle.read(1) == b"\n":
        return
    end = size
    while end > 0:
        start = max(0, end - _TAIL_BLOCK)
        handle.seek(start)
        newline = handle.read(end - start).rfind(b"\n")
        if newline >= 0:
            handle.truncate(start + newline + 1)
            return
        end = start
    handle.truncate(0)


class WindowLog:
    """Append-only ``windows-v1.jsonl`` under ``root``; the caller holds ``supervisor.lock``."""

    def __init__(self, root: Path) -> None:
        self.path = root / WINDOWS_FILE
        self._observed_at = ""
        self._keys: set[str] = set()

    def write(self, record: Mapping[str, Any], observation: ls.Observation,
              hits: Sequence[ls.TriggerHit]) -> None:
        """Append the window of one recorded decision; raises (and writes nothing) on failure."""
        key = row_key(record)
        if self._observed_at != record.get("observed_at"):
            self._observed_at, self._keys = str(record.get("observed_at")), set()
        if key in self._keys:
            raise ValueError(f"window key {key!r} already written in this cycle")
        line = (json.dumps(build_window(key, observation, hits), sort_keys=True) + "\n").encode()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab+", buffering=0) as handle:
            _repair_tail(handle)
            handle.seek(0, os.SEEK_END)
            view = memoryview(line)
            while view:
                view = view[handle.write(view):]
        self._keys.add(key)


# --------------------------------------------------------------------------- adjudicator rows


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    text = path.expanduser().read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def arm_row(record: Mapping[str, Any], arm: str) -> dict[str, Any] | None:
    """The adjudicator row for one decision record in ``arm``, or None when the record has no such arm.

    ``rules`` re-decides with the rule table's proposal; ``model`` is the recorded decision and
    exists only when a model reader made it (``reader_model``), not when a reader failure or an
    exhausted budget fell back to ``alert_research``.
    """
    if arm == "rules":
        proposal = record.get("proposal")
        if proposal not in ls.ACTIONS:
            return None
        return {**record, "arm": arm, "action": proposal, "recorded_action": record.get("action")}
    if arm == "model":
        return {**record, "arm": arm} if record.get("reader_model") else None
    raise ValueError(f"arm {arm!r} not in {ARMS}")


def join_rows(decisions: Iterable[Mapping[str, Any]], windows: Iterable[Mapping[str, Any]],
              arm: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """``(rows, counts)``: decisions of ``arm`` that have a window, in file order."""
    have = {str(w["key"]) for w in windows}
    rows: list[dict[str, Any]] = []
    counts = {"decisions": 0, "no_arm": 0, "no_window": 0, "rows": 0}
    for record in decisions:
        counts["decisions"] += 1
        row = arm_row(record, arm)
        if row is None:
            counts["no_arm"] += 1
        elif row_key(record) not in have:
            counts["no_window"] += 1
        else:
            rows.append(row)
    counts["rows"] = len(rows)
    return rows, counts


def export_rows(root: Path, arm: str, out: Path) -> dict[str, Any]:
    """Write ``out`` for ``live_supervisor_adjudicate --rows`` (windows: ``<root>/windows-v1.jsonl``)."""
    root = root.expanduser()
    decisions_path, windows_path = root / ls.DECISIONS_FILE, root / WINDOWS_FILE
    if not decisions_path.is_file() or not windows_path.is_file():
        missing = [p.name for p in (decisions_path, windows_path) if not p.is_file()]
        raise FileNotFoundError(f"{', '.join(missing)} not found under {root}")
    rows, counts = join_rows(read_jsonl(decisions_path), read_jsonl(windows_path), arm)
    out = out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    return {"arm": arm, "out": str(out), "windows": str(windows_path), **counts}
