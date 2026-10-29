"""Live Supervisor LS-5: route ``flag_cheating`` decisions to the Trace Watcher lane.

:class:`CheatRouter` is the ``flag_cheating`` callable ``(observation, decision) -> bool``. It
writes one label-free markdown evidence card per flagged run under ``<root>/cheat-cards/`` and
announces new cards to the Trace Watcher pane (``w1C:pK``) with at most one
``queue-append.jsonl`` line per UTC day. A run is carded once (dedupe per run id); later
cards of a day that already has its line are appended to that day's index file, which the
line names, so the lane hears about the first flag of a day at once and finds the rest there.

The card never carries a dataset or benchmark name, reward, outcome, split or label: it is
built from the subject, the decision, the fired alarms (trace-harness ``cheat`` alarms with
detector, version, reader and score; live-watch alerts) and the cited step text, and every
free-text field is screened for ``key: value`` label fields and the run id's dataset prefix.
Observation metrics are never copied (Harbor metrics name the benchmark source).

:func:`subscribe_alarms` tails the trace-harness and Run Health findings JSONL files with a
byte-offset cursor per file and returns the new ``alarm`` records.

:func:`latency_study` is the LS-5 detection-latency proxy over the replay manifest (no model
calls): for each cheat positive with an onset step and each detector that alarmed on it with
cited steps, ``min(cited step) - onset``; ``python -m evallab.production.live_supervisor_cheat
latency`` writes ``replay/cheat-latency-v1.json``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.production.live_supervisor import (
    DEFAULT_ROOT,
    DETECTOR,
    Decision,
    Observation,
    Subject,
)
from evallab.production.run_atlas import STATE_ROOT, append_jsonl

TRACE_WATCHER_PANE = "w1C:pK"
CARDS_DIR = "cheat-cards"
DAYS_DIR = "days"
ROUTER_STATE = "router-state.json"
DEFAULT_QUEUE = Path(
    "~/Developer/research-context/bridge/2026-10-27-run-atlas/queue-append.jsonl"
)
TRACE_FINDINGS = STATE_ROOT / "trace-harness" / "findings-v1.jsonl"
REPLAY_DIR = DEFAULT_ROOT / "replay"
DEFAULT_MANIFEST = REPLAY_DIR / "manifest-v1.jsonl"
DEFAULT_LATENCY = REPLAY_DIR / "cheat-latency-v1.json"
LATENCY_SCHEMA = "live-supervisor/cheat-latency/v1"

CARD_MAX_TOKENS = 1500
#: The repository's approximate-token convention (``trace_harness.CHARS_PER_TOKEN``).
CHARS_PER_TOKEN = 4
EXCERPT_CHARS = 600
REASON_CHARS = 300
SUMMARY_CHARS = 200
MAX_CITED = 8
#: Bytes hashed at the head of a tailed file to notice a rewrite that regrew past the cursor.
HEAD_BYTES = 4096
WITHHELD = "[withheld]"
#: ``key: value`` / ``key=value`` fields that would carry a label into the card.
_LABEL_FIELD = re.compile(
    r"\b(reward|rewards|outcome|label|labels|dataset|dataset_id|benchmark|split|expected_verdict"
    r"|verdict_enacted|passed|resolved|score_label)\b\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE,
)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


# --------------------------------------------------------------------------- alarm records


def _jsonl_records(path: Path) -> Iterable[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


def _is_alarm(record: Mapping[str, Any]) -> bool:
    return (record.get("verdict") == "alarm" and record.get("detector") != DETECTOR
            and isinstance(record.get("run_id"), str))


def default_findings_paths(state_root: Path = STATE_ROOT) -> list[Path]:
    """The trace-harness findings plus the newest Run Health findings file."""
    health = sorted((state_root / "run-health").glob("findings-run-health-*.jsonl"),
                    key=lambda p: int(m.group(1)) if (m := re.search(r"-(\d+)\.jsonl$", p.name)) else -1)
    return [state_root / "trace-harness" / "findings-v1.jsonl", *health[-1:]]


def _head_digest(handle: Any, length: int) -> str:
    handle.seek(0)
    return hashlib.sha256(handle.read(min(length, HEAD_BYTES))).hexdigest()


def subscribe_alarms(findings_paths: Sequence[Path], *, since_offsets_state: Path) -> list[dict[str, Any]]:
    """New ``alarm`` findings appended since the last call, in file order.

    The cursor per file is a byte offset in ``since_offsets_state`` with the file's inode and a
    digest of its head. The file is re-read from the start when it was replaced (other inode),
    truncated (shorter than the cursor) or rewritten (head digest changed). A trailing line
    without its newline is left for the next call. Unparseable lines and the live supervisor's
    own records are skipped.
    """
    state: dict[str, Any] = {}
    if since_offsets_state.is_file():
        try:
            loaded = json.loads(since_offsets_state.read_text(encoding="utf-8"))
            state = dict(loaded.get("files", {})) if isinstance(loaded, dict) else {}
        except ValueError:
            state = {}
    alarms: list[dict[str, Any]] = []
    for path in findings_paths:
        key = str(path)
        try:
            handle = path.open("rb")
        except OSError:
            continue
        with handle:
            stat = os.fstat(handle.fileno())
            cursor = state.get(key) or {}
            offset = int(cursor.get("offset", 0))
            if (cursor.get("inode") != stat.st_ino or stat.st_size < offset
                    or (offset and cursor.get("head") != _head_digest(handle, offset))):
                offset = 0
            handle.seek(offset)
            chunk = handle.read(stat.st_size - offset)
            complete = chunk[: chunk.rfind(b"\n") + 1]
            for line in complete.splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and _is_alarm(record):
                    alarms.append(record)
            offset += len(complete)
            state[key] = {"offset": offset, "inode": stat.st_ino,
                          "head": _head_digest(handle, offset) if offset else None}
    since_offsets_state.parent.mkdir(parents=True, exist_ok=True)
    tmp = since_offsets_state.with_suffix(".tmp")
    tmp.write_text(json.dumps({"files": state}, sort_keys=True), encoding="utf-8")
    tmp.replace(since_offsets_state)
    return alarms


# --------------------------------------------------------------------------- card


@dataclass(frozen=True)
class Alarm:
    """One fired cheat/integrity alarm as the card shows it."""

    detector: str
    version: str
    reader: str
    score: float | None
    steps: tuple[int, ...]
    summary: str


def _run_aliases(subject: Subject) -> set[str]:
    names = {subject.finding_run_id, subject.id}
    if subject.run_id:
        names.add(subject.run_id)
    names.add(subject.id.rsplit("/", 1)[-1])
    return names


def _steps(values: Any) -> tuple[int, ...]:
    if not isinstance(values, list | tuple):
        return ()
    return tuple(v for v in values if isinstance(v, int) and not isinstance(v, bool))


def gather_alarms(observation: Observation, records: Iterable[Mapping[str, Any]]) -> list[Alarm]:
    """Cheat alarms for the subject: stored findings first, then live alarms they do not cover.

    ``records`` are ``trace-findings/v1`` lines; only ``alarm`` verdicts of kind ``cheat`` for
    this subject count. ``metrics['trace_alarms']`` (Harbor source) adds detectors absent from
    ``records``; ``metrics['alerts']`` adds every open live-watch alert.
    """
    aliases = _run_aliases(observation.subject)
    alarms: list[Alarm] = []
    for record in records:
        if _is_alarm(record) and record.get("kind") == "cheat" and record["run_id"] in aliases:
            score = record.get("score")
            alarms.append(Alarm(str(record.get("detector") or "?"), str(record.get("detector_version") or "?"),
                                str(record.get("reader_model") or "deterministic"),
                                float(score) if isinstance(score, int | float) else None,
                                _steps(record.get("evidence_steps")), str(record.get("summary") or "")))
    known = {alarm.detector for alarm in alarms}
    metrics = observation.metrics
    for live in metrics.get("trace_alarms") or ():
        if isinstance(live, Mapping) and str(live.get("detector") or "?") not in known:
            score = live.get("score")
            alarms.append(Alarm(str(live.get("detector") or "?"), "?", "?",
                                float(score) if isinstance(score, int | float) else None,
                                _steps(live.get("evidence_steps")), str(live.get("summary") or "")))
    for alert in metrics.get("alerts") or ():
        if not isinstance(alert, Mapping):
            continue
        ref = alert.get("step_ref")
        tail = ref.rsplit("#", 1)[-1] if isinstance(ref, str) else ""
        alarms.append(Alarm(f"live_watch.{alert.get('rule') or '?'}", "live_watch", "deterministic", 1.0,
                            (int(tail),) if tail.isdigit() else (), str(alert.get("detail") or "")))
    return alarms


def _screener(subject: Subject, forbidden: Iterable[str]) -> Callable[[str], str]:
    """Whitespace-collapsed text with label fields and forbidden names withheld."""
    terms = {t.casefold() for t in forbidden if t}
    # A stored run id is ``<dataset>/<...>/<id>``: its prefixes (and their leading words) name
    # the dataset and benchmark.
    for part in subject.finding_run_id.split("/")[:-1]:
        terms.update(t.casefold() for t in (part, part.split("-")[0]) if len(t) >= 4)
    pattern = re.compile("|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)),
                         re.IGNORECASE) if terms else None

    def screen(text: str) -> str:
        text = _LABEL_FIELD.sub(WITHHELD, " ".join(str(text).split()))
        return pattern.sub(WITHHELD, text) if pattern else text

    return screen


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def render_card(observation: Observation, decision: Decision, alarms: Sequence[Alarm], *, open_more: Sequence[str],
                forbidden: Iterable[str] = (), max_tokens: int = CARD_MAX_TOKENS) -> str:
    """The markdown evidence card: at most ``max_tokens`` (approximate) and label-free.

    Excerpts shrink (600, 300, 120, 0 chars) until the card fits; only then is it cut.
    """
    subject = observation.subject
    screen = _screener(subject, forbidden)
    reader = decision.reader_model or "rules"
    confidence = "" if decision.confidence is None else f" confidence={decision.confidence:.2f}"
    head = [
        f"# Live Supervisor cheat card: {subject.kind} {screen(subject.id)}",
        f"- observed: {observation.observed_at.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"
        f" | owner={subject.owner} killable={str(subject.killable).lower()}"
        f" | actor_inputs_available={str(observation.actor_inputs_available).lower()}",
        f"- decision: {decision.action} kind={decision.kind} reader={reader}{confidence}",
        f"- reason: {_clip(screen(decision.reason), REASON_CHARS)}",
        "",
        "## Alarms",
    ]
    for alarm in alarms:
        score = "-" if alarm.score is None else f"{alarm.score:g}"
        steps = ",".join(map(str, alarm.steps)) or "-"
        summary = _clip(screen(alarm.summary), SUMMARY_CHARS)
        head.append(f"- {alarm.detector} | version {alarm.version} | reader {alarm.reader} | score {score}"
                    f" | steps {steps}" + (f" | {summary}" if summary else ""))
    if not alarms:
        head.append("- none recorded beyond the decision itself")
    cited = list(dict.fromkeys([*decision.evidence_steps, *(s for a in alarms for s in a.steps)]))[:MAX_CITED]
    events = {event.index: event for event in observation.events}
    tail = ["", "## Open more", *(f"- {line}" for line in open_more),
            "", "Leads, not verdicts: confirm in the trace before acting."]
    text = ""
    for limit in (EXCERPT_CHARS, 300, 120, 0):
        body = ["", "## Cited steps"]
        for step in sorted(cited):
            event = events.get(step)
            if event is None:
                body.append(f"- step {step}: (outside the live window; open the run)")
            elif limit:
                body.append(f"- step {step} [{event.source}]: {_clip(screen(event.text), limit)}")
            else:
                body.append(f"- step {step} [{event.source}]")
        if not cited:
            body.append("- none cited")
        text = "\n".join([*head, *body, *tail]) + "\n"
        if len(text) <= max_tokens * CHARS_PER_TOKEN:
            return text
    return text[: max_tokens * CHARS_PER_TOKEN - 2] + "…\n"


def safe_name(run_id: str) -> str:
    """A file name for ``run_id``: readable tail plus a short digest (unique, no separators)."""
    tail = _UNSAFE.sub("_", run_id.rsplit("/", 1)[-1]).strip("._")[:80] or "run"
    return f"{tail}-{hashlib.sha256(run_id.encode()).hexdigest()[:10]}"


# --------------------------------------------------------------------------- router


class CheatRouter:
    """``flag_cheating`` callable: card the run and tell the Trace Watcher (``w1C:pK``).

    ``root`` holds ``cheat-cards/`` (cards, ``days/<date>.md`` indexes, router state).
    ``queue_path`` is the bridge ``queue-append.jsonl``. ``findings_paths`` are read for the
    run's stored alarms (detector, version, reader). ``stored`` says whether a run id is a
    stored trace (default: it has records in ``findings_paths``, so ``evallab trace card`` can
    show it). ``trial_roots`` locate a live trial's directory as ``<root>/<subject.id>``.
    ``forbidden`` names (datasets, benchmarks) are withheld from card text.
    """

    def __init__(self, root: Path = DEFAULT_ROOT, *, queue_path: Path = DEFAULT_QUEUE,
                 findings_paths: Sequence[Path] = (TRACE_FINDINGS,), trial_roots: Sequence[Path] = (),
                 stored: Callable[[str], bool] | None = None, forbidden: Iterable[str] = (),
                 max_tokens: int = CARD_MAX_TOKENS) -> None:
        self.cards_dir = root.expanduser() / CARDS_DIR
        self.state_path = self.cards_dir / ROUTER_STATE
        self.queue_path = queue_path.expanduser()
        self.findings_paths = [p.expanduser() for p in findings_paths]
        self.trial_roots = [p.expanduser() for p in trial_roots]
        self._stored = stored
        self.forbidden = tuple(forbidden)
        self.max_tokens = max_tokens

    # state ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self.state_path.is_file():
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {"routed": dict(loaded.get("routed", {})), "days": dict(loaded.get("days", {}))}
        return {"routed": {}, "days": {}}

    def _save(self, state: Mapping[str, Any]) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    # card -------------------------------------------------------------------

    def _open_more(self, observation: Observation, decision: Decision, alarms: Sequence[Alarm],
                   records: Sequence[Mapping[str, Any]]) -> list[str]:
        subject = observation.subject
        run_id = subject.finding_run_id
        steps = list(decision.evidence_steps) or [s for a in alarms for s in a.steps]
        around = f" --around {min(steps)} --radius 3" if steps else ""
        stored = (self._stored(run_id) if self._stored is not None
                  else not run_id.startswith("live:") and any(r.get("run_id") == run_id for r in records))
        if stored:
            return [f"`evallab trace card {run_id}`", f"`evallab qa-data window {run_id}{around}`"]
        trial = next((root / subject.id for root in self.trial_roots if (root / subject.id).is_dir()), None)
        where = f"trial dir: `{trial}`" if trial else f"trial dir: not found under the trial roots ({subject.id})"
        return [where, f"`evallab qa-data window {run_id}{around}` once the run is imported"]

    def card(self, observation: Observation, decision: Decision) -> str:
        records = [r for path in self.findings_paths for r in _jsonl_records(path)]
        alarms = gather_alarms(observation, records)
        return render_card(observation, decision, alarms,
                           open_more=self._open_more(observation, decision, alarms, records),
                           forbidden=self.forbidden, max_tokens=self.max_tokens)

    # routing ----------------------------------------------------------------

    def __call__(self, observation: Observation, decision: Decision) -> bool:
        return bool(self.route([(observation, decision)]))

    def route(self, flagged: Iterable[tuple[Observation, Decision]]) -> list[Path]:
        """Card every new flagged run; returns the new card paths.

        Non-``flag_cheating`` decisions and runs already carded are skipped. New cards are
        grouped by the UTC day they were observed: a day without a queue line gets one line
        naming its new cards and the day index; a day that has one gets its cards appended
        to the index only.
        """
        self.cards_dir.mkdir(parents=True, exist_ok=True)
        with (self.cards_dir / "router.lock").open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                return self._route_locked(flagged)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _route_locked(self, flagged: Iterable[tuple[Observation, Decision]]) -> list[Path]:
        state = self._load()
        new: dict[str, list[tuple[str, Path]]] = {}
        for observation, decision in flagged:
            run_id = observation.subject.finding_run_id
            if decision.action != "flag_cheating" or run_id in state["routed"]:
                continue
            path = self.cards_dir / f"{safe_name(run_id)}.md"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(self.card(observation, decision), encoding="utf-8")
            tmp.replace(path)
            day = observation.observed_at.astimezone(UTC).strftime("%Y-%m-%d")
            state["routed"][run_id] = {"card": str(path), "day": day}
            new.setdefault(day, []).append((run_id, path))
        lines = []
        for day, cards in sorted(new.items()):
            index = self.cards_dir / DAYS_DIR / f"{day}.md"
            index.parent.mkdir(parents=True, exist_ok=True)
            fresh = not index.exists()
            with index.open("a", encoding="utf-8") as handle:
                if fresh:
                    handle.write(f"# Live Supervisor cheat cards {day} (UTC)\n\n")
                handle.writelines(f"- `{path}`\n" for _, path in cards)
            if day not in state["days"]:
                queue_id = f"LS-CHEAT-{day}-1"
                listed = " ".join(str(path) for _, path in cards)
                text = (f"Live Supervisor flagged {len(cards)} live runs; cards: {listed}"
                        f" (later cards today are listed in {index})")
                lines.append({"pane": TRACE_WATCHER_PANE, "id": queue_id, "text": text})
                state["days"][day] = {"queue_id": queue_id, "index": str(index)}
        if lines:
            append_jsonl(self.queue_path, lines)
        self._save(state)
        return [path for cards in new.values() for _, path in cards]


# --------------------------------------------------------------------------- latency study


def _quantile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank quantile (``ceil(q*n)``-th smallest)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _summary(deltas: Sequence[float]) -> dict[str, Any]:
    return {"n": len(deltas), "p50": _quantile(deltas, 0.5), "p90": _quantile(deltas, 0.9),
            "min": min(deltas, default=None), "max": max(deltas, default=None),
            "at_or_before_onset": sum(d <= 0 for d in deltas)}


def store_timestamps(database: Path) -> Callable[[str], dict[int, float]]:
    """Per-step epoch timestamps of a stored run (``native_node_residual.timestamp``), read-only."""

    def read(run_id: str) -> dict[int, float]:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT step_index, json_extract(flags_json, '$.native_node_residual.timestamp') "
                "FROM trace_steps WHERE trace_id = ?", (run_id,),
            ).fetchall()
        return {int(step): float(at) for step, at in rows if isinstance(at, int | float)}

    return read


def latency_study(manifest: Iterable[Mapping[str, Any]], findings: Iterable[Mapping[str, Any]], *,
                  timestamps: Callable[[str], Mapping[int, float]] | None = None) -> dict[str, Any]:
    """Steps from labelled onset to each detector's earliest cited alarm step (no model calls).

    Positives are ``flag_cheating`` manifest rows; those with a step onset are measured.
    ``findings`` are ``trace-findings/v1`` records (latest per run/detector/version/reader
    wins). Per detector: ``scored`` positives, ``alarms``, ``timed`` = alarms citing steps,
    ``coverage`` = timed / positives with onset, and step deltas (``min cited - onset``;
    negative = the cited evidence precedes the onset). With ``timestamps`` the same deltas
    are given in seconds for runs whose onset and cited step both carry a timestamp. ``any``
    is the earliest cited step over every detector per run. ``negatives_scored`` and
    ``negative_alarms`` count the same detector on the ``continue`` rows of the measured sets
    (how noisy an early alarm is).
    """
    positives = [row for row in manifest if row.get("label_action") == "flag_cheating"]
    onsets: dict[str, int] = {}
    without: dict[str, int] = {}
    for row in positives:
        run_id = (row.get("locator") or {}).get("trace_id")
        if row.get("onset_kind") == "step" and isinstance(row.get("onset"), int) and isinstance(run_id, str):
            onsets[run_id] = row["onset"]
        else:
            without[str(row.get("set"))] = without.get(str(row.get("set")), 0) + 1
    measured = {str(row.get("set")) for row in positives if (row.get("locator") or {}).get("trace_id") in onsets}
    negatives = {run_id for row in manifest
                 if row.get("label_action") == "continue" and str(row.get("set")) in measured
                 and isinstance(run_id := (row.get("locator") or {}).get("trace_id"), str)}
    latest: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for record in findings:
        run_id = record.get("run_id")
        if ((run_id in onsets or run_id in negatives) and record.get("kind") == "cheat"
                and record.get("detector") != DETECTOR):
            latest[(run_id, record.get("detector"), record.get("detector_version"),
                    record.get("reader_model"))] = record
    clocks: dict[str, Mapping[int, float]] = {}

    def seconds(run_id: str, step: int) -> float | None:
        if timestamps is None:
            return None
        if run_id not in clocks:
            clocks[run_id] = timestamps(run_id)
        clock = clocks[run_id]
        start, hit = clock.get(onsets[run_id]), clock.get(step)
        return None if start is None or hit is None else round(hit - start, 3)

    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    earliest: dict[str, int] = {}
    for (run_id, detector, version, reader), record in latest.items():
        key = (str(detector), str(version), str(reader or "deterministic"))
        group = groups.setdefault(key, {"scored": 0, "alarms": 0, "steps": [], "seconds": [],
                                        "negatives_scored": 0, "negative_alarms": 0})
        alarm = record.get("verdict") == "alarm"
        if run_id in negatives:
            group["negatives_scored"] += 1
            group["negative_alarms"] += alarm
            continue
        group["scored"] += 1
        if not alarm:
            continue
        group["alarms"] += 1
        cited = _steps(record.get("evidence_steps"))
        if not cited:
            continue
        first = min(cited)
        group["steps"].append(first - onsets[run_id])
        elapsed = seconds(run_id, first)
        if elapsed is not None:
            group["seconds"].append(elapsed)
        earliest[run_id] = min(first, earliest.get(run_id, first))
    total = len(onsets)
    detectors = []
    for (detector, version, reader), group in sorted(groups.items()):
        detectors.append({
            "detector": detector, "detector_version": version, "reader_model": reader,
            "scored": group["scored"], "alarms": group["alarms"], "timed": len(group["steps"]),
            "negatives_scored": group["negatives_scored"], "negative_alarms": group["negative_alarms"],
            "coverage": round(len(group["steps"]) / total, 4) if total else None,
            "steps": _summary(group["steps"]), "seconds": _summary(group["seconds"]),
        })
    any_steps = [first - onsets[run_id] for run_id, first in earliest.items()]
    any_seconds = [s for run_id, first in earliest.items() if (s := seconds(run_id, first)) is not None]
    return {
        "schema": LATENCY_SCHEMA,
        "unit": "store step_index (every role); seconds from native step timestamps where present",
        "proxy": "min(cited alarm step) - onset step; negative = evidence cited before the onset",
        "quantiles": "nearest rank",
        "positives": len(positives), "with_onset": total, "without_onset_by_set": without,
        "detectors": detectors,
        "any": {"timed": len(any_steps), "coverage": round(len(any_steps) / total, 4) if total else None,
                "steps": _summary(any_steps), "seconds": _summary(any_seconds)},
    }


def _latency_command(args: argparse.Namespace) -> int:
    manifest = list(_jsonl_records(args.manifest))
    findings = [r for path in args.findings for r in _jsonl_records(path)]
    report = latency_study(manifest, findings, timestamps=store_timestamps(args.database))
    report["inputs"] = {
        "manifest": str(args.manifest),
        "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "findings": [str(p) for p in args.findings],
    }
    report["produced_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(args.out), "with_onset": report["with_onset"], "any": report["any"]}))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evallab.production.live_supervisor_cheat")
    actions = parser.add_subparsers(dest="command", required=True)
    latency = actions.add_parser("latency", help="Detection latency proxy over the replay cheat positives")
    latency.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST.expanduser())
    latency.add_argument("--findings", type=Path, nargs="+", default=[TRACE_FINDINGS])
    latency.add_argument("--database", type=Path, default=STATE_ROOT / "cloud-trace-db.sqlite")
    latency.add_argument("--out", type=Path, default=DEFAULT_LATENCY.expanduser())
    latency.set_defaults(func=_latency_command)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
