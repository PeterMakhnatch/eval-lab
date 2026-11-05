"""Live mode: a thin scheduler that feeds finished DEV runs to the line, increment by increment.

``evallab line watch CONFIG --state-dir DIR`` polls explicit DEV sources. Each cycle it lists the
runs that have been finished and unchanged for the settling time (``watch_sources``), writes a
small increment manifest (plus that increment's own labels and AutoReview readings, which the
line digests as inputs) and calls the line's own ``run_line`` on it. The line's intake, DEV and
label rules and eight checks do all the gating; a refusal or a failed check stops the watcher.
Nothing is copied or frozen: the line names each run by its real path and records its digest.

The state is the committed increments, nothing else. An increment folder exists for the line
only once ``commit.json`` does, and that file is written last, atomically, after the child batch
passed its eight checks and its review/audit runs are in the owned Judge Station queue.
The cumulative view is built on demand from the committed children (``watch_rollup``). One
process owns a state directory; a crash is repaired by restarting: an increment without
``commit.json`` is moved to ``abandoned/`` and its runs are planned again, and the queue write
is idempotent.
"""

from __future__ import annotations

import json
import math
import os
import signal
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import (
    digest,
    file_keys,
    read_bytes,
    read_json,
    read_rows,
    write_json,
    write_rows,
)
from evallab.line.watch_queue import publish
from evallab.line.watch_rollup import (
    batch_name,
    child_dir,
    child_path,
    verify,
)
from evallab.line.watch_sources import (
    Admitter,
    Candidate,
    discover,
    freeze_cache,
    freeze_labels,
    increment_rows,
)
from evallab.line.watch_state import (
    COMMIT_SCHEMA,
    State,
    WatchError,
    WatchRefused,
    config_identity,
    frozen_task_status,
    fsync_directory,
    init_state,
    now_iso,
    owner_alive,
    owner_lock,
    parse_config,
    parse_time,
)

STATUS_SCHEMA = "evallab.line_watch_status/v1"
WATCHDOG_SCHEMA = "evallab.line_watch_watchdog/v1"
#: Phases in which the watcher is expected to be making progress.
BUSY_PHASES = frozenset({"recovering", "discovering", "processing", "publishing", "committing"})


# ---------------------------------------------------------------- lag


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))]


def _summary(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def lag_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Measured delays in seconds, from the times recorded in the commit records.

    ``discovery_to_commit`` starts when the watcher first saw a run, not when the run finished:
    a backlog found at start-up counts from that scan. ``completion_to_commit`` is reported only
    for runs whose own recorded finish time carries a timezone; file mtimes are never used.
    """
    series: dict[str, list[float]] = {"first_seen_at": [], "ready_at": [], "finished_at": []}
    processing: list[float] = []
    for record in records:
        times = record.get("times") if isinstance(record, dict) else None
        runs = record.get("runs") if isinstance(record, dict) else None
        if not isinstance(times, dict) or not isinstance(runs, dict):
            continue  # a damaged record is the live check's to name, not this report's to crash on
        committed = parse_time(times.get("committed_at"))
        planned = parse_time(times.get("planned_at"))
        if committed is None:
            continue
        if planned is not None:
            processing.append(committed - planned)
        for run in runs.values():
            if not isinstance(run, dict):
                continue
            for key, values in series.items():
                moment = parse_time(run.get(key))
                if moment is not None:
                    values.append(committed - moment)
    return {
        "discovery_to_commit_seconds": _summary(series["first_seen_at"]),
        "ready_to_commit_seconds": _summary(series["ready_at"]),
        "completion_to_commit_seconds": _summary(series["finished_at"]),
        "increment_processing_seconds": _summary(processing),
    }


# ---------------------------------------------------------------- validation before any write


def preflight(config: dict[str, Any], repo_root: Path) -> Admitter:
    """Read-only checks of everything the configuration names; returns the admission gate.

    Task status, labels and the AutoReview cache are read through the same loaders the line
    uses, so a non-DEV or malformed one is refused here, before the state directory exists.
    A cached reading must name an allowed model and be attested: the line's own spend-and-models
    check would fail the increment that used it, so the watcher refuses it up front.
    """
    line = config["line"]
    if line["task_status"]:
        core._task_status_dev(line["task_status"])
        hd.load_certified_tasks(line["task_status"])
    for source in config["sources"]:
        if source["labels"]:
            hd.load_labels(
                source["labels"],
                repo_root=repo_root,
                attempted_cheat=line["labels_policy"]["attempted_cheat"],
                attempted_ids=set(),
            )
    if line["autoreview"]["mode"] == "cached":
        readings, _ = core._cache({"autoreview": line["autoreview"]})
        allowed = set(config["line"]["models"]["allowed"])
        for run_id, reading in sorted(readings.items()):
            model = reading.details.get("model")
            if model and (model not in allowed or reading.details.get("attested") is not True):
                raise WatchRefused(
                    f"cached AutoReview reading for {run_id!r} names model {model!r} "
                    f"({'attested' if reading.details.get('attested') is True else 'unattested'}): "
                    "readings must be attested and from models.allowed"
                )
    verdicts = Path(config["judge"]["verdicts"])
    if os.path.lexists(verdicts):  # the Station's own reader, so a bad file is refused up front
        core._station_verdicts(read_bytes(verdicts), set())
    if config["judge"]["write"]:
        core._refuse_queue({"judge": {"queue": config["judge"]["queue"]}}, [])
    return Admitter(repo_root)


def failed_checks(result: dict[str, Any]) -> str:
    return "; ".join(
        f"{check['name']}: {check['details']}"
        for check in result["checks"]
        if check["status"] != "PASS"
    )


def _open_or_invalid(state: State) -> str | None:
    try:
        folders = state.open_folders()
    except OSError:
        return "invalid"
    return folders[0].name if len(folders) == 1 else ("invalid" if folders else None)


def record_failure(
    state: State, error: BaseException, fields: dict[str, Any] | None = None
) -> None:
    """Write ``errors.jsonl`` and a ``failed`` ``status.json``; never raises for a damaged state."""
    moment = now_iso(time.time())
    last_error = {"class": type(error).__name__, "message": str(error)[:500]}
    state.root.mkdir(parents=True, exist_ok=True)
    with state.errors_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": moment, **last_error}, sort_keys=True) + "\n")
    write_json(
        state.status_path,
        {
            "schema": STATUS_SCHEMA,
            "pid": os.getpid(),
            "phase": "failed",
            "last_activity_at": moment,
            "last_error": last_error,
            **(fields or {}),
        },
    )


# ---------------------------------------------------------------- watcher


class Watcher:
    def __init__(
        self,
        state: State,
        repo_root: Path,
        admitter: Admitter,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state = state
        self.repo_root = repo_root
        self.admitter = admitter
        self.clock = clock
        self.config = state.config()
        self.phase = "starting"
        self.cycle_no = 0
        self.started_at = clock()
        self.last_activity = self.started_at
        self.last_cycle_at: float | None = None
        self.last_error: dict[str, str] | None = None
        self.seen: dict[str, dict[str, float | str]] = {}  # settling timers, in memory only
        self.visible: dict[str, Any] = {}
        self.records: list[dict[str, Any]] = []
        self.committed_ids: set[str] = set()
        self._reload()

    # -- bookkeeping

    def _reload(self) -> None:
        self.records = self.state.committed()
        self.committed_ids = {
            row["run_id"]
            for record in self.records
            for row in read_rows(child_dir(self.state, record) / "runs.jsonl")
        }

    def _touch(self, phase: str) -> None:
        self.phase = phase
        self.last_activity = self.clock()
        self.write_status()

    def status_fields(self) -> dict[str, Any]:
        return {
            "schema": STATUS_SCHEMA,
            "watch_id": self.config["watch_id"],
            "pid": os.getpid(),
            "phase": self.phase,
            "cycle": self.cycle_no,
            "started_at": now_iso(self.started_at),
            "last_cycle_at": None if self.last_cycle_at is None else now_iso(self.last_cycle_at),
            "last_activity_at": now_iso(self.last_activity),
            "last_error": self.last_error,
            "increments": len(self.records),
            "runs_committed": len(self.committed_ids),
            "open_increment": _open_or_invalid(self.state),
            "lag": lag_summary(self.records),
            **self.visible,
        }

    def write_status(self) -> None:
        write_json(self.state.status_path, self.status_fields())

    def fail(self, error: BaseException) -> None:
        self.last_error = {"class": type(error).__name__, "message": str(error)[:500]}
        self.phase = "failed"
        self.last_activity = self.clock()
        try:
            fields = self.status_fields()
        except Exception:  # noqa: BLE001 - a damaged record must not hide the failure itself
            fields = {}
        record_failure(self.state, error, fields)

    def _guard(self) -> None:
        """Before anything is planned: the configuration and frozen task status are as created."""
        stored = read_json(self.state.watch_json)
        if stored["config_sha256"] != config_identity(stored["config"]) or stored[
            "config_sha256"
        ] != config_identity(self.config):
            raise WatchError("watch.json was edited after the state directory was created")
        if self.config["line"]["task_status"]:
            frozen = frozen_task_status(self.state)
            if frozen is None or digest(frozen) != stored["frozen_task_status_sha256"]:
                raise WatchError(
                    "the frozen task status changed after the state directory was created"
                )

    def _require_verified(self, result: dict[str, Any], where: str) -> None:
        if not result["passed"]:
            raise WatchError(f"live check failed {where}: {failed_checks(result)}")

    # -- recovery

    def recover(self) -> dict[str, Any]:
        """Repair after a crash, then refuse to go on unless the committed state verifies.

        An increment without ``commit.json`` is moved aside (its runs are planned again), and
        the live checks run on the newest child and the globals.
        """
        self._touch("recovering")
        self._guard()
        moved = self.state.sweep_open()
        self._require_verified(verify(self.state, self.repo_root, deep=True), "at start-up")
        return {"abandoned": moved}

    # -- one cycle

    def cycle(self) -> dict[str, Any]:
        self.cycle_no += 1
        self._guard()
        committed: list[int] = []
        tuning = self.config["watch"]
        self._touch("discovering")
        found = discover(self.config["sources"], self.admitter, self.committed_ids)
        now = self.clock()
        keys = {c.key for c in found.fresh}
        self.seen = {k: v for k, v in self.seen.items() if k in keys}
        for candidate in found.fresh:
            entry = self.seen.setdefault(
                candidate.key,
                {"signature": candidate.signature, "first_seen": now, "stable_since": now},
            )
            if entry["signature"] != candidate.signature:
                entry.update(signature=candidate.signature, stable_since=now)
        settle = tuning["settle_seconds"]
        ready = sorted(
            (c for c in found.fresh if now - float(self.seen[c.key]["stable_since"]) >= settle),
            key=lambda c: (float(self.seen[c.key]["stable_since"]), c.corpus, c.run_id),
        )
        done: set[str] = set()
        self._show(found, ready, done, settle)
        size = tuning["max_runs_per_increment"]
        for start in range(0, len(ready), size):
            chunk = ready[start : start + size]
            committed.append(self._run_increment(chunk))
            done.update(c.run_id for c in chunk)
            self._show(found, ready, done, settle)
        self.seen = {k: v for k, v in self.seen.items() if k.split("|", 1)[1] not in done}
        self.last_cycle_at = self.clock()
        self._touch("idle")
        return {
            "cycle": self.cycle_no,
            "committed": committed,
            "ready": len(ready),
            "held": len(found.held),
        }

    def _show(self, found: Any, ready: list[Candidate], done: set[str], settle: float) -> None:
        """What is waiting right now: runs committed in this cycle are no longer waiting."""
        now = self.clock()
        waiting = [c for c in ready if c.run_id not in done]
        reasons = Counter(item["reason"] for item in found.held)
        self.visible = {
            "visible_runs": found.visible,
            "ready_runs": len(waiting),
            "settling_runs": len(found.fresh) - len(ready),
            "held": {"counts": dict(sorted(reasons.items())), "examples": found.held[:10]},
            "oldest_ready_age_seconds": (
                round(
                    max(now - float(self.seen[c.key]["stable_since"]) - settle for c in waiting), 3
                )
                if waiting
                else None
            ),
        }

    # -- one increment

    def _write_inputs(self, chunk: list[Candidate], n: int) -> Path:
        """The increment manifest, its labels and its AutoReview readings; returns the spec path."""
        inc = self.state.inc(n)
        inputs = inc / "inputs"
        line = self.config["line"]
        autoreview = dict(line["autoreview"])
        sources = {s["corpus"]: s for s in self.config["sources"]}
        declared = [Path(s["labels"]) for s in sources.values() if s["labels"]]
        if autoreview["mode"] == "cached":
            declared += core._cache({"autoreview": autoreview})[1]
        if self.admitter.shares_a_file(file_keys(declared), chunk):
            raise WatchRefused("a label or AutoReview cache file is also a file of a run")
        child_sources = []
        for corpus, rows in sorted(increment_rows(chunk).items()):
            write_rows(inputs / corpus / "manifest.jsonl", rows)
            labels = None
            if sources[corpus]["labels"]:
                labels = inputs / corpus / "labels.jsonl"
                write_rows(
                    labels,
                    freeze_labels(
                        sources[corpus],
                        [r["id"] for r in rows],
                        self.repo_root,
                        line["labels_policy"]["attempted_cheat"],
                    ),
                )
            child_sources.append(
                {
                    "corpus": corpus,
                    "rollouts": str(inputs / corpus / "manifest.jsonl"),
                    "labels": None if labels is None else str(labels),
                }
            )
        if autoreview["mode"] == "cached":
            cache = inputs / "autoreview-cache.jsonl"
            write_rows(cache, freeze_cache(autoreview["cache"], [c.run_id for c in chunk]))
            autoreview["cache"] = str(cache)
        frozen = frozen_task_status(self.state)
        spec = inc / "spec.json"
        write_json(
            spec,
            {
                "schema": "evallab.line_spec/v1",
                "batch_id": batch_name(self.config, n),
                "split": "dev",
                "sources": child_sources,
                "task_status": str(frozen) if frozen else None,
                **({"task_action": line["task_action"]} if "task_action" in line else {}),
                **(
                    {"goodfire_review_floor": line["goodfire_review_floor"]}
                    if "goodfire_review_floor" in line
                    else {}
                ),
                "task_policy": line["task_policy"],
                "autoreview": autoreview,
                "models": line["models"],
                "audit": line["audit"],
                "judge": {
                    "queue": self.config["judge"]["queue"],
                    "write": False,  # the watcher publishes after the checks, under its own lock
                    "verdicts": self.config["judge"]["verdicts"],
                },
                "labels_policy": line["labels_policy"],
                "out_root": str(inc / "batch"),
            },
        )
        return spec

    def _run_increment(self, chunk: list[Candidate]) -> int:
        self._guard()
        state = self.state
        n = len(self.records) + 1
        planned = self.clock()
        self._touch("processing")
        spec = self._write_inputs(chunk, n)
        batch = core.run_line(spec, self.repo_root)
        result = read_json(batch / "check.json")
        if result.get("passed") is not True:
            failed = [c["name"] for c in result.get("checks", []) if c["status"] != "PASS"]
            raise WatchError(f"increment {n} failed the line checks: {failed}")
        self._touch("publishing")
        queue_rows = read_rows(batch / "judge-queue.jsonl")
        added = (
            publish(Path(self.config["judge"]["queue"]), queue_rows)
            if self.config["judge"]["write"]
            else 0
        )
        self._touch("committing")
        record = {
            "schema": COMMIT_SCHEMA,
            "n": n,
            "batch_id": batch_name(self.config, n),
            "batch": child_path(self.config, n),
            "decisions_sha256": digest(batch / "decisions.jsonl"),
            "batch_sha256": digest(batch),
            "spec_sha256": digest(spec),
            "config_sha256": config_identity(self.config),
            "task_status_sha256": read_json(state.watch_json)["frozen_task_status_sha256"],
            "runs": {
                c.run_id: {
                    "corpus": c.corpus,
                    "first_seen_at": now_iso(float(self.seen[c.key]["first_seen"])),
                    "ready_at": now_iso(
                        float(self.seen[c.key]["stable_since"])
                        + self.config["watch"]["settle_seconds"]
                    ),
                    "finished_at": c.finished_at,
                }
                for c in sorted(chunk, key=lambda c: c.run_id)
            },
            "queue_added": added,
            "times": {"planned_at": now_iso(planned), "committed_at": now_iso(self.clock())},
        }
        write_json(state.inc(n) / "commit.json", record)  # atomic: the increment now exists
        fsync_directory(state.inc(n))
        self._reload()
        self._require_verified(
            verify(state, self.repo_root, deep=False), f"after committing increment {n}"
        )
        return n


# ---------------------------------------------------------------- entry points


def run_watch(
    config_path: Path,
    state_dir: Path,
    repo_root: Path,
    *,
    once: bool = False,
    max_cycles: int | None = None,
    stop: threading.Event | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Own the state directory and process finished runs; returns a summary (``error`` on failure).

    Nothing is written until the configuration and everything it names have been validated.
    """
    state = State(state_dir)
    with core.declared_scope(repo_root):  # every declared input is read the way the line reads it
        return _watch(state, config_path, repo_root, once, max_cycles, stop, clock, sleep)


def _watch(
    state: State,
    config_path: Path,
    repo_root: Path,
    once: bool,
    max_cycles: int | None,
    stop: threading.Event | None,
    clock: Callable[[], float],
    sleep: Callable[[float], None] | None,
) -> dict[str, Any]:
    config = parse_config(config_path, state.root)
    admitter = preflight(config, repo_root)
    rev, _ = core._code(repo_root)
    summary: dict[str, Any] = {"state_dir": str(state.root), "cycles": 0, "committed": []}
    with owner_lock(state):
        init_state(state, config, code_rev=rev, moment=clock())
        stop = stop or threading.Event()
        pause = sleep or stop.wait
        watcher: Watcher | None = None
        try:
            watcher = Watcher(state, repo_root, admitter, clock=clock)
            summary["recovered"] = watcher.recover()
            while not stop.is_set():
                outcome = watcher.cycle()
                summary["cycles"] += 1
                summary["committed"] += outcome["committed"]
                if once or (max_cycles is not None and summary["cycles"] >= max_cycles):
                    break
                if not outcome["committed"]:
                    pause(watcher.config["watch"]["poll_seconds"])
        except Exception as error:  # noqa: BLE001 - service boundary: record the failure, never hide it
            if watcher is not None:
                watcher.fail(error)
            else:
                record_failure(state, error)
            summary["error"] = f"{type(error).__name__}: {error}"
        else:
            watcher.phase = "stopped"
            watcher.last_activity = clock()
            watcher.write_status()
    return summary


def install_stop_signals(stop: threading.Event) -> None:
    """Finish the current increment, then stop, on SIGINT/SIGTERM."""
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())


def check_state(state_dir: Path, repo_root: Path, *, deep: bool = True) -> dict[str, Any]:
    """``evallab line check STATE_DIR``: the line's own check on every child, then the globals.

    Read-only: nothing is written into the state directory.
    """
    return verify(State(state_dir), repo_root, deep=deep)


def watchdog(
    state_dir: Path, *, max_age: float | None = None, now: float | None = None
) -> dict[str, Any]:
    """Read-only liveness verdict in the live supervisor's vocabulary: ok / stalled / failed.

    Healthy idle polling is ``ok``. While an increment runs, progress means a new status phase
    or a new file in the open increment's folder (receipts appear stage by stage); a heartbeat
    alone never counts, so a wedged ``run_line`` cannot look alive. Damaged state files make
    the verdict ``failed``; the verdict is always written to ``watchdog.json``.
    """
    state = State(state_dir)
    moment = time.time() if now is None else now
    stamp = now_iso(moment)
    result: dict[str, Any] = {"schema": WATCHDOG_SCHEMA, "checked_at": stamp, "at": stamp}

    def finish(status: str, reason: str, **extra: Any) -> dict[str, Any]:
        value = {**result, "status": status, "reason": reason, **extra}
        if state.root.is_dir():
            write_json(state.watchdog_path, value)
        return value

    if not state.watch_json.is_file():
        return finish("failed", "no live state here")
    try:
        status = read_json(state.status_path)
        limit = (
            max_age if max_age is not None else state.config()["watch"]["watchdog_max_age_seconds"]
        )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int | float)
            or not 0 < limit < math.inf
        ):
            result["max_age_seconds"] = None
            return finish("failed", f"watchdog_max_age_seconds is not a positive number: {limit!r}")
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return finish("failed", "status file missing or unreadable")
    result["max_age_seconds"] = limit
    try:
        if status.get("phase") == "failed":
            return finish("failed", f"watcher failed: {status.get('last_error')}")
        activity = parse_time(status.get("last_activity_at")) or 0.0
        open_folders = state.open_folders()
        if len(open_folders) > 1:
            return finish(
                "failed", f"more than one open increment: {[p.name for p in open_folders]}"
            )
        for folder in open_folders:
            for path in (folder, *folder.rglob("*")):
                activity = max(activity, path.stat().st_mtime)
        age = moment - activity
        extra = {
            "phase": status.get("phase"),
            "age_seconds": round(age, 1),
            "increments": status.get("increments"),
            "open_increment": open_folders[0].name if open_folders else None,
        }
        alive = owner_alive(state)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RecursionError,
        WatchError,
    ) as error:
        return finish("failed", f"damaged live state: {type(error).__name__}: {error}")
    if not alive:
        return finish("stalled", "no process owns this state directory", **extra)
    if status.get("phase") == "stopped":
        return finish("stalled", "watcher stopped", **extra)
    if age > limit:
        return finish("stalled", "no progress within the allowed age", **extra)
    return finish(
        "ok", "making progress" if status.get("phase") in BUSY_PHASES else "idle", **extra
    )
