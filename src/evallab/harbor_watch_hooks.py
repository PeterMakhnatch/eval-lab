from __future__ import annotations

import contextlib
import json
import os
import threading
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

HOOK_JOURNAL = Path("watch") / "hooks.jsonl"
HOOK_SCHEMA = "evallab.watch_hooks/v1"
LOG_HOOKS_ENV = "EVALLAB_WATCH_LOG_HOOKS"
LOG_TAIL_CHARS = 2_000
LOG_FLUSH_SECONDS = 2.0
LOG_STREAMING_MIN_VERSION = (0, 24)


def _harbor_version() -> tuple[int, ...]:
    try:
        raw = metadata.version("harbor")
    except metadata.PackageNotFoundError:
        return ()
    parts: list[int] = []
    for piece in raw.split(".")[:3]:
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def log_streaming_supported(version: tuple[int, ...] | None = None) -> bool:
    return (version if version is not None else _harbor_version()) >= LOG_STREAMING_MIN_VERSION


def _event_record(event: Any) -> dict[str, Any]:
    name = getattr(getattr(event, "event", None), "value", None) or str(event.event)
    record: dict[str, Any] = {
        "schema": HOOK_SCHEMA,
        "kind": "event",
        "event": name,
        "trial": event.trial_name,
        "task": getattr(event, "task_name", None),
        "trial_id": str(getattr(event, "trial_id", "") or ""),
        "at": event.timestamp.isoformat(),
    }
    if name in ("end", "cancel"):
        result = event.result
        info = getattr(result, "exception_info", None)
        record["exception_type"] = getattr(info, "exception_type", None)
        verifier = getattr(result, "verifier_result", None)
        rewards = getattr(verifier, "rewards", None)
        if isinstance(rewards, dict):
            record["rewards"] = dict(rewards)
    return record


class _LogBuffer:

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entries: dict[tuple[str, str], dict[str, Any]] = {}
        self.dirty: set[tuple[str, str]] = set()

    def add(self, trial: str, phase: str, text: str, at: str) -> None:
        with self.lock:
            slot = self.entries.setdefault(
                (trial, phase), {"chunks": 0, "chars": 0, "tail": "", "at": at}
            )
            slot["chunks"] += 1
            slot["chars"] += len(text)
            slot["tail"] = (slot["tail"] + text)[-LOG_TAIL_CHARS:]
            slot["at"] = at
            self.dirty.add((trial, phase))

    def drain(self) -> list[dict[str, Any]]:
        with self.lock:
            out = [
                {
                    "schema": HOOK_SCHEMA,
                    "kind": "log",
                    "trial": trial,
                    "phase": phase,
                    **self.entries[(trial, phase)],
                }
                for trial, phase in sorted(self.dirty)
            ]
            self.dirty.clear()
        return out


class WatchHookPlugin:

    def __init__(self, *, flush_seconds: float = LOG_FLUSH_SECONDS) -> None:
        self._journal: Path | None = None
        self._trial_names: dict[str, str] = {}
        self._logs = _LogBuffer()
        self._write_lock = threading.Lock()
        self._flush_seconds = flush_seconds
        self._stop = threading.Event()
        self._flusher: threading.Thread | None = None
        self.log_streaming = False

    async def on_job_start(self, job: Any) -> None:
        try:
            self._journal = Path(job.job_dir) / HOOK_JOURNAL
            for event_name in (
                "on_trial_started",
                "on_environment_started",
                "on_agent_started",
                "on_agent_ended",
                "on_verification_started",
                "on_trial_ended",
                "on_trial_cancelled",
            ):
                getattr(job, event_name)(self._on_event)
            if os.environ.get(LOG_HOOKS_ENV) == "1" and log_streaming_supported():
                self.log_streaming = self._subscribe_logs(job)
        except Exception:  # noqa: BLE001 -- observation never fails a job
            pass

    async def on_job_end(self, _job_result: Any) -> None:
        self._stop.set()
        flusher, self._flusher = self._flusher, None
        if flusher is not None:
            flusher.join(timeout=5)
        self._flush_logs()

    def _subscribe_logs(self, job: Any) -> bool:
        queue = getattr(job, "_trial_queue", None)
        setup = getattr(queue, "_setup_hooks", None)
        if not callable(setup):
            return False

        def setup_with_logs(trial: Any) -> None:
            setup(trial)
            with contextlib.suppress(Exception):
                trial.add_log_callback(self._on_log)

        setattr(queue, "_setup_hooks", setup_with_logs)  # noqa: B010 -- queue is untyped Any|None
        self._flusher = threading.Thread(
            target=self._flush_loop, name="evallab-watch-hook-logs", daemon=True
        )
        self._flusher.start()
        return True

    async def _on_event(self, event: Any) -> None:
        try:
            record = _event_record(event)
            if record["trial_id"]:
                self._trial_names[record["trial_id"]] = record["trial"]
            self._append([record])
        except Exception:  # noqa: BLE001
            pass

    async def _on_log(self, entry: Any) -> None:
        try:
            trial = self._trial_names.get(str(entry.trial_id))
            if trial is not None:
                self._logs.add(
                    trial, str(entry.phase), str(entry.text), entry.timestamp.isoformat()
                )
        except Exception:  # noqa: BLE001
            pass

    def _flush_loop(self) -> None:
        while not self._stop.wait(self._flush_seconds):
            self._flush_logs()

    def _flush_logs(self) -> None:
        with contextlib.suppress(Exception):
            self._append(self._logs.drain())

    def _append(self, records: list[dict[str, Any]]) -> None:
        if not records or self._journal is None:
            return
        text = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
        with self._write_lock:
            self._journal.parent.mkdir(parents=True, exist_ok=True)
            with self._journal.open("a", encoding="utf-8") as handle:
                handle.write(text)


def read_hook_journal(job_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        lines = (Path(job_dir) / HOOK_JOURNAL).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    trials: dict[str, dict[str, Any]] = {}
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or not isinstance(record.get("trial"), str):
            continue
        state = trials.setdefault(
            record["trial"],
            {
                "events": [],
                "phase": None,
                "terminal": False,
                "exception_type": None,
                "rewards": None,
                "logs": {},
                "last_at": 0.0,
            },
        )
        at = _epoch(record.get("at"))
        state["last_at"] = max(state["last_at"], at)
        if record.get("kind") == "log":
            state["logs"][str(record.get("phase"))] = {
                key: record.get(key) for key in ("chunks", "chars", "tail", "at")
            }
            continue
        event = str(record.get("event"))
        state["events"].append([event, record.get("at")])
        state["phase"] = event
        if event in ("end", "cancel"):
            state["terminal"] = True
            state["exception_type"] = record.get("exception_type")
            state["rewards"] = record.get("rewards")
    return trials


def _epoch(stamp: Any) -> float:
    if not isinstance(stamp, str):
        return 0.0
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()
