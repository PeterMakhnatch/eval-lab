"""Harbor job plugin observing protected files with ``inotifywait -m``.

Opt-in via ``EVALLAB_FILE_ACCESS=1`` (sanitized through the Harbor
environment by the execution layer). Every trial gets at least one
``coverage`` record in ``agent/file-access.jsonl``, even with capture
disabled: ``disabled`` without the opt-in, ``unavailable`` when setup fails.
No automatic installs, privilege changes, agent-command wrapping, or campaign
deployment. Missing ``inotifywait``/``python3``, platform, permission, or
setup failures stay visible as ``unavailable``/``partial`` -- never clean.

Lifecycle per agent window (multi-step trials repeat it per agent phase):

1. ``AGENT_START``: probe the sandbox (``probe``), hash the protected roots
   (``snapshot``) into ``evaluator/file-access/`` on the evaluator host --
   never into agent-mounted directories -- then start the sandbox observer
   and await ``Watches established.`` before the agent runs.
2. While the agent runs, a cancellable poll task downloads newly sealed
   sandbox chunks (bounded new bytes each, never whole-file re-downloads)
   and appends access records to the host log, so alerts precede agent end.
3. ``AGENT_END``: cancel and await the poll task, stop the observer, run a
   final drain (unseen chunks in order, torn tail counted not fabricated),
   take the after snapshot, diff before/after hashes into ``file_change``
   records, append everything, and remove the sandbox scratch directory.
4. ``END``/``CANCEL`` (plus job end): backstops that close a still-open
   window, clean up, and consolidate any step-relocated fragments back into
   the root per-trial log, so no sandbox observer or host task is orphaned
   on failure or cancellation and no window is lost to log relocation.

The plugin needs the actual ``Trial`` (hook events carry no environment), so
it wraps the existing ``job._trial_queue._setup_hooks(trial)`` seam -- the
same pattern as the watch-hooks log subscription -- and uses only the public
``trial.add_hook`` afterwards. Control PIDs live only in memory for ``kill``
and are never written to any record.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab.file_access import (
    CATEGORIES,
    CATEGORY_GIT_OBJECTS,
    CATEGORY_GIT_REFS,
    CATEGORY_GRADER,
    DEFAULT_TESTS_DIR,
    EVALUATOR_SNAPSHOT_DIR,
    EXEC_TIMEOUT_SEC,
    FILE_ACCESS_ENV,
    FILE_ACCESS_LOG,
    FILE_ACCESS_PATHS_ENV,
    MAX_DOWNLOAD_BYTES,
    MAX_SAVED_EVENTS,
    POLL_SECONDS,
    READY_POLL_SEC,
    READY_TIMEOUT_SEC,
    SNAPSHOT_TIMEOUT_SEC,
    STDERR_TAIL_CHARS,
    STOP_TIMEOUT_SEC,
    access_record,
    classify_path,
    coverage_record,
    diff_entries,
    file_change_record,
    parse_frames,
    parse_paths_env,
    parse_stream_chunk,
    scan_loss_tokens,
)

HELPER_FILENAME = "file_access_capture.py"
CHUNKS_DIRNAME = "chunks"
CHUNK_NAME_RE = re.compile(r"^c\d{6}\.bin$")
STATUS_FILENAME = "capture-status.json"
READY_FILENAME = "READY"
START_LOG_FILENAME = "start.log"

EXEC_SMALL_TIMEOUT = EXEC_TIMEOUT_SEC
EXEC_SNAPSHOT_TIMEOUT = SNAPSHOT_TIMEOUT_SEC


def _trial_key(event: Any) -> str:
    return str(event.trial_id)


def _trial_dir(event: Any) -> Path:
    return Path(event.config.trials_dir) / event.trial_name


def _sanitize(name: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in name)[:64]


@dataclass
class _Window:
    window_id: int
    trial_name: str
    trial_dir: Path
    scratch: str
    out_dir: str
    control_pid: int | None = None
    baseline_path: Path | None = None
    baseline_entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    baseline_complete: bool = False
    baseline_ref: str = ""
    #: Pre-agent git ancestry from the sandbox probe, persisted verbatim into
    #: the evaluator-owned baseline file (never refreshed after agent start).
    git_history: list[dict[str, Any]] = field(default_factory=list)
    targets: list[tuple[str, str]] = field(default_factory=list)
    watched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    loss_mode: str = "explicit"
    open: bool = True
    env: Any = None
    poll_task: Any = None
    seen_chunks: set[str] = field(default_factory=set)
    carry: bytes = b""
    saved_events: int = 0
    invalid_frames: int = 0
    bound_noted: bool = False
    loss_overflow: bool = False
    loss_ignored: bool = False
    loss_unmount: bool = False
    stream_truncated: bool = False
    poll_notes: list[str] = field(default_factory=list)


class FileAccessPlugin:
    """Harbor ``JobPlugin``: inotifywait observation plus hash baselines."""

    def __init__(self, poll_seconds: float | str = POLL_SECONDS, **_kwargs: Any) -> None:
        self.enabled = os.environ.get(FILE_ACCESS_ENV) == "1"
        raw_paths = os.environ.get(FILE_ACCESS_PATHS_ENV)
        try:
            self.extra_paths = parse_paths_env(raw_paths)
            self.paths_error: str | None = None
        except ValueError as exc:
            self.extra_paths = {}
            self.paths_error = str(exc)
        try:
            self.poll_seconds = float(poll_seconds)
        except (TypeError, ValueError):
            self.poll_seconds = POLL_SECONDS
        if self.poll_seconds <= 0:
            self.poll_seconds = POLL_SECONDS
        self._windows: dict[str, _Window] = {}
        self._counters: dict[str, int] = {}
        self._seen: dict[str, tuple[str, Path]] = {}
        self._consolidated: set[str] = set()
        self._lock = asyncio.Lock()

    # -- Harbor plugin surface -------------------------------------------

    async def on_job_start(self, job: Any) -> None:
        try:
            queue = getattr(job, "_trial_queue", None)
            setup = getattr(queue, "_setup_hooks", None)
            if queue is None or not callable(setup):
                raise AttributeError("trial queue seam is unavailable")
            plugin = self

            def setup_with_file_access(trial: Any) -> None:
                setup(trial)
                with contextlib.suppress(Exception):
                    plugin._bind_trial(trial)

            queue._setup_hooks = setup_with_file_access
        except Exception:
            # Seam missing (older Harbor or a bare fake): fall back to
            # job-level hooks that record honestly without sandbox access.
            with contextlib.suppress(Exception):
                job.on_agent_started(self._on_agent_started_no_trial)
                job.on_agent_ended(self._on_agent_ended_backstop)
                job.on_verification_started(self._on_verification_backstop)
                job.on_trial_ended(self._on_trial_backstop)
                job.on_trial_cancelled(self._on_trial_backstop)

    async def on_job_end(self, _job_result: Any) -> None:
        async with self._lock:
            pending = [key for key, window in self._windows.items() if window.open]
            seen = list(self._seen.items())
        for key in pending:
            with contextlib.suppress(Exception):
                await self._close_window(key, reason="job ended with window open")
        for key, (trial_name, trial_dir) in seen:
            with contextlib.suppress(Exception):
                await self._consolidate_trial(key, trial_name, trial_dir)

    #: Trial lifecycle hook names (``TrialEvent.value``) this plugin needs,
    #: mapped to handler attributes. Matched against the trial's own hook
    #: registry so no Harbor import is required at module scope or at bind
    #: time; the default unit environment omits optional Harbor.
    _WANTED_HOOKS = {
        "start": "_on_trial_started",
        "agent-start": "_on_agent_started",
        "agent-end": "_on_agent_ended",
        "verification-start": "_on_verification_backstop",
        "end": "_on_trial_backstop",
        "cancel": "_on_trial_backstop",
    }

    def _bind_trial(self, trial: Any) -> None:
        """Attach per-trial hooks via the public ``trial.add_hook``."""
        registry = getattr(trial, "_hooks", None)
        if not isinstance(registry, dict):
            raise AttributeError("trial exposes no hook registry")
        bound = 0
        for key in list(registry):
            name = getattr(key, "value", key)
            handler = self._WANTED_HOOKS.get(name) if isinstance(name, str) else None
            if handler is None:
                continue
            trial.add_hook(key, getattr(self, handler))
            bound += 1
        if not bound:
            raise AttributeError("trial hook registry holds no lifecycle hooks")
        self._remember_trial(trial)

    def _bound_trial(self, event: Any) -> Any | None:
        # The bound Trial object is stashed per trial name at bind time; hook
        # events themselves carry no environment.
        return getattr(self, "_trials", {}).get(event.trial_name)

    # -- Hook handlers ----------------------------------------------------
    async def _on_trial_started(self, event: Any) -> None:
        key = _trial_key(event)
        directory = _trial_dir(event)
        self._seen[key] = (event.trial_name, directory)
        with contextlib.suppress(OSError):
            self._append(directory, [coverage_record(
                window_id=0,
                state="starting" if self.enabled else "disabled",
                reason="trial created; agent observation has not started",
            )])


    async def _on_agent_started(self, event: Any) -> None:
        # Observation never fails a trial; failures surface as records.
        with contextlib.suppress(Exception):
            await self._open_window(event)

    async def _on_agent_ended(self, event: Any) -> None:
        with contextlib.suppress(Exception):
            await self._close_window(_trial_key(event), reason="agent ended")

    async def _on_agent_started_no_trial(self, event: Any) -> None:
        with contextlib.suppress(Exception):
            self._append(
                _trial_dir(event),
                [
                    coverage_record(
                        window_id=1,
                        state="unavailable",
                        reason="trial queue seam unavailable: no sandbox access",
                    )
                ],
            )

    async def _on_agent_ended_backstop(self, _event: Any) -> None:
        return None

    async def _on_verification_backstop(self, event: Any) -> None:
        with contextlib.suppress(Exception):
            await self._close_window(_trial_key(event), reason="verification started")

    async def _on_trial_backstop(self, event: Any) -> None:
        key = _trial_key(event)
        with contextlib.suppress(Exception):
            await self._close_window(key, reason="trial ended")
        with contextlib.suppress(Exception):
            await self._consolidate_trial(key, event.trial_name, _trial_dir(event))

    # -- Window open ------------------------------------------------------

    async def _open_window(self, event: Any) -> None:
        trial_dir = _trial_dir(event)
        key = _trial_key(event)
        async with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1
            window_id = self._counters[key]
            self._seen[key] = (event.trial_name, trial_dir)
        if not self.enabled:
            self._append(
                trial_dir,
                [
                    coverage_record(
                        window_id=window_id,
                        state="disabled",
                        reason=f"capture opt-in {FILE_ACCESS_ENV}=1 is not set",
                    )
                ],
            )
            return
        if self.paths_error:
            self._append(trial_dir, [coverage_record(
                window_id=window_id,
                state="unavailable",
                reason=f"{FILE_ACCESS_PATHS_ENV} invalid: {self.paths_error}",
            )])
            return
        trial = self._bound_trial(event)
        if trial is None:
            self._append(
                trial_dir,
                [
                    coverage_record(
                        window_id=window_id,
                        state="unavailable",
                        reason="trial object is not bound to this trial name",
                    )
                ],
            )
            return
        env = trial.agent_environment
        # Per-window scratch: a later window must never re-ingest an earlier
        # window's sealed chunks as its own observations.
        scratch = f"/tmp/evallab-file-access-{_sanitize(event.trial_name)}-w{window_id}"
        window = _Window(
            window_id=window_id,
            trial_name=event.trial_name,
            trial_dir=trial_dir,
            scratch=scratch,
            out_dir=f"{scratch}/out",
            env=env,
        )
        async with self._lock:
            self._windows[key] = window
        try:
            await self._start_capture(env, window)
        except Exception as exc:  # noqa: BLE001 -- visible, not clean
            with contextlib.suppress(Exception):
                await self._cleanup(env, window)
            self._windows.pop(key, None)
            self._append(
                trial_dir,
                [
                    coverage_record(
                        window_id=window_id,
                        state="unavailable",
                        reason=f"{type(exc).__name__}: {exc}",
                        watched_paths=window.watched,
                        missing_paths=window.missing,
                    )
                ],
            )
            return
        async with self._lock:
            self._windows[key] = window
        self._append(
            trial_dir,
            [
                coverage_record(
                    window_id=window_id,
                    state="active",
                    reason=self._start_reason(window),
                    watched_paths=window.watched,
                    missing_paths=window.missing,
                )
            ],
        )
        with contextlib.suppress(Exception):
            window.poll_task = asyncio.get_running_loop().create_task(
                self._poll_window(key, window)
            )

    def _start_reason(self, window: _Window) -> str:
        parts = [f"observing {len(window.watched)} protected path(s)"]
        if window.loss_mode != "explicit":
            parts.append("loss events unverified on this inotifywait version")
        if self.paths_error:
            parts.append(f"{FILE_ACCESS_PATHS_ENV} invalid: {self.paths_error}")
        return "; ".join(parts)

    async def _start_capture(self, env: Any, window: _Window) -> None:
        helper_remote = f"{window.scratch}/{HELPER_FILENAME}"
        await self._upload_helper(env, helper_remote)
        probe = await self._probe(env, helper_remote)
        raw_history = probe.get("git_history")
        if isinstance(raw_history, list):
            window.git_history = [
                dict(item) for item in raw_history if isinstance(item, dict)
            ]
        else:
            window.git_history = []
        self._select_targets(probe, window)
        if not window.watched:
            raise RuntimeError(
                "no protected targets present "
                f"(missing: {', '.join(window.missing) or 'none listed'})"
            )
        await self._take_snapshot(env, helper_remote, window, label="baseline")
        await self._launch_observer(env, helper_remote, window)
        await self._await_ready(env, window)

    # -- Sandbox primitives -------------------------------------------------

    def _helper_source(self) -> Path:
        return Path(__file__).with_name(HELPER_FILENAME)

    async def _exec(self, env: Any, command: str, *, timeout: int) -> Any:
        return await env.exec(command, timeout_sec=timeout)

    async def _upload_helper(self, env: Any, remote: str) -> None:
        source = self._helper_source()
        if not source.is_file():
            raise RuntimeError(f"sandbox helper is missing from the install: {source}")
        await env.upload_file(source_path=str(source), target_path=remote)

    async def _probe(self, env: Any, helper: str) -> dict[str, Any]:
        command = f"python3 {shlex.quote(helper)} probe"
        for extra in self._iter_extra_paths():
            command += f" --candidate {shlex.quote(extra)}"
        result = await self._exec(env, command, timeout=EXEC_SMALL_TIMEOUT)
        if result.return_code != 0:
            tail = (result.stderr or result.stdout or "")[-STDERR_TAIL_CHARS:].strip()
            raise RuntimeError(f"sandbox probe failed: {tail or 'no output'}")
        try:
            probe = json.loads(result.stdout or "")
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(f"sandbox probe returned invalid JSON: {exc}") from exc
        if not isinstance(probe, dict) or probe.get("schema") != "evallab.file_access_probe/v1":
            raise RuntimeError("sandbox probe returned an unexpected document")
        if probe.get("inotifywait") is None:
            raise RuntimeError("inotifywait is not installed in the sandbox")
        return probe

    def _iter_extra_paths(self) -> list[str]:
        ordered: list[str] = []
        for category in CATEGORIES:
            ordered.extend(self.extra_paths.get(category, []))
        return ordered

    def _select_targets(self, probe: dict[str, Any], window: _Window) -> None:
        watched: list[str] = []
        missing: list[str] = []
        targets: list[tuple[str, str]] = []

        def _take(path: str, category: str, present: bool) -> None:
            if present:
                watched.append(path)
                targets.append((path, category))
            else:
                missing.append(path)

        repos = list(probe.get("repos") or [])
        mimo = probe.get("mimo_git_hidden")
        if isinstance(mimo, dict) and mimo.get("git_dir"):
            repos.append(mimo)
        for repo in repos:
            git_dir = repo.get("git_dir")
            if not git_dir:
                continue
            _take(f"{git_dir}/objects", CATEGORY_GIT_OBJECTS, repo.get("objects") == "present")
            _take(f"{git_dir}/refs", CATEGORY_GIT_REFS, repo.get("refs") == "present")
        tests = probe.get("tests") or {}
        _take(DEFAULT_TESTS_DIR, CATEGORY_GRADER, tests.get("status") == "present")
        by_path = {str(item.get("path")): item for item in probe.get("extras") or []}
        for category in CATEGORIES:
            for extra in self.extra_paths.get(category, []):
                info = by_path.get(extra, {})
                _take(extra, category, bool(info.get("exists")))
        # Deterministic order: category order, then path.
        order = {category: index for index, category in enumerate(CATEGORIES)}
        targets.sort(key=lambda item: (order[item[1]], item[0]))
        window.targets = targets
        window.watched = sorted(set(watched))
        window.missing = sorted(set(missing) - set(watched))

    async def _take_snapshot(self, env: Any, helper: str, window: _Window, *, label: str) -> dict[str, Any]:
        # Git storage is watched but presence-only; only protected grader
        # content participates in the evaluator-owned content baseline.
        shallow = sorted(
            root for root, category in window.targets if category != CATEGORY_GRADER
        )
        full = sorted(
            root for root, category in window.targets if category == CATEGORY_GRADER
        )
        command = f"python3 {shlex.quote(helper)} snapshot"
        for root in full:
            command += f" --root {shlex.quote(root + ':' + self._category(root, window))}"
        for root in shallow:
            command += f" --shallow-root {shlex.quote(root + ':' + self._category(root, window))}"
        result = await self._exec(env, command, timeout=EXEC_SNAPSHOT_TIMEOUT)
        if result.return_code != 0:
            tail = (result.stderr or result.stdout or "")[-STDERR_TAIL_CHARS:].strip()
            raise RuntimeError(f"sandbox {label} snapshot failed: {tail or 'no output'}")
        try:
            manifest = json.loads(result.stdout or "")
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(f"sandbox {label} snapshot returned invalid JSON: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("schema") != "evallab.file_access_snapshot/v1":
            raise RuntimeError(f"sandbox {label} snapshot returned an unexpected document")
        if label == "baseline":
            snapshot_dir = window.trial_dir / EVALUATOR_SNAPSHOT_DIR
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(snapshot_dir.parent, 0o700)
                os.chmod(snapshot_dir, 0o700)
            window.baseline_path = snapshot_dir / f"baseline-w{window.window_id}.json"
            manifest["git_history"] = self._baseline_history(window)
            window.baseline_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            window.baseline_ref = (EVALUATOR_SNAPSHOT_DIR / window.baseline_path.name).as_posix()
            window.baseline_entries = {
                str(item.get("path")): item for item in manifest.get("entries") or []
            }
            window.baseline_complete = not manifest.get("truncated", False)
            if not window.baseline_complete:
                self._note(window, "baseline snapshot truncated; file changes unknown")
            discovery_path = snapshot_dir / f"discovery-w{window.window_id}.json"
            discovery_path.write_text(
                json.dumps(
                    {
                        "watched": window.watched,
                        "missing": window.missing,
                        "targets": [
                            {"path": root, "category": category}
                            for root, category in window.targets
                        ],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        return manifest

    @staticmethod
    def _baseline_history(window: _Window) -> list[dict[str, Any]]:
        """Ancestry for the baseline file: initial host state is never overwritten."""
        if window.baseline_path is not None:
            try:
                if window.baseline_path.is_file():
                    prior = json.loads(window.baseline_path.read_text(encoding="utf-8"))
                    existing = prior.get("git_history") if isinstance(prior, dict) else None
                    if isinstance(existing, list) and existing:
                        return existing
            except (OSError, ValueError):
                pass
        return [dict(item) for item in window.git_history]

    def _category(self, root: str, window: _Window) -> str:
        for target_root, category in window.targets:
            if target_root == root:
                return category
        return CATEGORY_GRADER

    async def _launch_observer(self, env: Any, helper: str, window: _Window) -> None:
        command = f"python3 {shlex.quote(helper)} capture --out {shlex.quote(window.out_dir)}"
        for watch in window.watched:
            command += f" --watch {shlex.quote(watch)}"
        launch = (
            f"mkdir -p {shlex.quote(window.scratch)} || exit 1; "
            f"setsid nohup {command} >{shlex.quote(window.scratch + '/' + START_LOG_FILENAME)} "
            "2>&1 < /dev/null & echo $!"
        )
        result = await self._exec(env, launch, timeout=EXEC_SMALL_TIMEOUT)
        if result.return_code != 0:
            tail = (result.stderr or result.stdout or "")[-STDERR_TAIL_CHARS:].strip()
            raise RuntimeError(f"observer launch failed: {tail or 'no output'}")
        try:
            window.control_pid = int((result.stdout or "").strip().split()[-1])
        except (ValueError, IndexError) as exc:
            raise RuntimeError(f"observer launch returned no control PID: {(result.stdout or '').strip()!r}") from exc
        if window.control_pid <= 0:
            raise RuntimeError("observer launch returned an invalid control PID")

    async def _await_ready(self, env: Any, window: _Window) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_SEC
        ready = f"{window.out_dir}/{READY_FILENAME}"
        while time.monotonic() < deadline:
            result = await self._exec(env, f"test -f {shlex.quote(ready)}", timeout=EXEC_SMALL_TIMEOUT)
            if result.return_code == 0:
                return
            alive = await self._exec(
                env, f"kill -0 {int(window.control_pid or 0)}", timeout=EXEC_SMALL_TIMEOUT
            )
            if alive.return_code != 0:
                log = await self._exec(
                    env,
                    f"cat {shlex.quote(window.scratch + '/' + START_LOG_FILENAME)} 2>/dev/null",
                    timeout=EXEC_SMALL_TIMEOUT,
                )
                tail = (log.stdout or "")[-STDERR_TAIL_CHARS:].strip()
                raise RuntimeError(f"observer died before ready: {tail or 'no output'}")
            await asyncio.sleep(READY_POLL_SEC)
        raise RuntimeError(f"observer was not ready within {READY_TIMEOUT_SEC:.0f}s")

    # -- Window close -------------------------------------------------------

    async def _close_window(self, key: str, *, reason: str) -> None:
        async with self._lock:
            window = self._windows.pop(key, None)
        if window is None or not window.open:
            return
        window.open = False
        cleanup = asyncio.create_task(self._finish_window(window, reason=reason))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # Preserve caller cancellation, but do not leave its observer running.
            await cleanup
            raise

    async def _finish_window(self, window: _Window, *, reason: str) -> None:
        await self._cancel_poll(window)
        records: list[dict[str, Any]] = []
        state = "stopped"
        details = [reason]
        env = window.env
        status: dict[str, Any] = {}
        try:
            await self._stop_observer(env, window, details)
            await self._final_drain(env, window, details)
            status = await self._fetch_status(env, window, details)
            after_entries, after_ok = await self._after_entries(env, window, details)
            if after_ok and window.baseline_complete:
                records.extend(self._change_records(window, after_entries))
            else:
                # No after image: diffing against nothing would frame every
                # baseline file as deleted, so report the gap instead.
                details.append("after snapshot unavailable; file changes unknown")
                self._note(window, "incomplete before/after snapshot; file changes unknown")
            state = self._terminal_state(window, status, records, details)
        except Exception as exc:  # noqa: BLE001 -- failures report missing/partial
            state = "partial" if records or window.saved_events else "unavailable"
            details.append(f"{type(exc).__name__}: {exc}")
        finally:
            with contextlib.suppress(Exception):
                await self._cleanup(env, window)
        records.append(
            coverage_record(
                window_id=window.window_id,
                state=state,
                reason="; ".join(details),
                watched_paths=window.watched,
                missing_paths=window.missing,
            )
        )
        self._append(window.trial_dir, records)

    def _terminal_state(
        self,
        window: _Window,
        status: dict[str, Any],
        records: list[dict[str, Any]],
        details: list[str],
    ) -> str:
        """Honest terminal state: any observed or possible loss degrades."""
        if window.invalid_frames:
            details.append(f"{window.invalid_frames} invalid frame(s) rejected")
        if window.stream_truncated:
            details.append("trailing stream truncated; rejected, not fabricated")
        if window.loss_overflow or status.get("overflow_seen"):
            details.append("queue overflow observed; some events were lost")
        if window.loss_ignored:
            details.append("a watch was removed during capture (ignored)")
        if window.loss_unmount:
            details.append("a watched filesystem was unmounted during capture (unmount)")
        if window.poll_notes:
            details.extend(window.poll_notes)
        details.append(
            f"{window.saved_events} access event(s) recorded from "
            f"{len(window.seen_chunks)} chunk(s)"
        )
        degraded = bool(
            window.invalid_frames
            or window.stream_truncated
            or window.loss_overflow
            or window.loss_ignored
            or window.loss_unmount
            or status.get("overflow_seen")
            or status.get("truncated")
            or window.poll_notes
            or window.loss_mode != "explicit"
            or bool(status.get("watch_errors"))
            or status.get("state") != "complete"
        )
        if status.get("state") == "error" and not records and not window.saved_events:
            return "unavailable"
        if degraded or status.get("state") == "error":
            return "partial"
        return "stopped"

    async def _stop_observer(self, env: Any, window: _Window, details: list[str]) -> None:
        if window.control_pid is None:
            details.append("no control PID recorded")
            return
        pid = int(window.control_pid)
        await self._exec(
            env,
            f"kill -TERM -- -{pid} 2>/dev/null || kill -TERM {pid} 2>/dev/null",
            timeout=EXEC_SMALL_TIMEOUT,
        )
        deadline = time.monotonic() + STOP_TIMEOUT_SEC
        while time.monotonic() < deadline:
            alive = await self._exec(
                env,
                f"kill -0 {pid} 2>/dev/null && ! grep -q ') Z ' /proc/{pid}/stat",
                timeout=EXEC_SMALL_TIMEOUT,
            )
            if alive.return_code != 0:
                window.control_pid = None
                return
            await asyncio.sleep(READY_POLL_SEC)
        await self._exec(env, f"kill -KILL -- -{pid} 2>/dev/null || kill -KILL {pid}", timeout=EXEC_SMALL_TIMEOUT)
        window.control_pid = None
        details.append("observer ignored SIGTERM and was killed")

    async def _download_text(self, env: Any, window: _Window, remote: str, *, what: str) -> bytes:
        size_result = await self._exec(
            env,
            f"stat -c %s {shlex.quote(remote)} 2>/dev/null || wc -c < {shlex.quote(remote)}",
            timeout=EXEC_SMALL_TIMEOUT,
        )
        try:
            size = int((size_result.stdout or "").strip().split()[-1])
        except (ValueError, IndexError) as exc:
            raise RuntimeError(f"{what} is not downloadable: unknown size") from exc
        if size > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"{what} is {size} bytes, over the {MAX_DOWNLOAD_BYTES} bound")
        with tempfile.TemporaryDirectory(prefix="evallab-file-access-") as temporary:
            local = Path(temporary) / "chunk.bin"
            await env.download_file(source_path=remote, target_path=local)
            return local.read_bytes()

    def _note(self, window: _Window, text: str) -> None:
        if len(window.poll_notes) < 8 and text not in window.poll_notes:
            window.poll_notes.append(text)

    async def _poll_window(self, key: str, window: _Window) -> None:
        """Publish sealed chunks to the host log while the agent runs."""
        try:
            while window.open:
                await asyncio.sleep(self.poll_seconds)
                if not window.open:
                    break
                try:
                    await self._poll_pass(window)
                except Exception as exc:
                    self._note(window, f"live publication failed: {type(exc).__name__}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- last-resort guard
            self._note(window, f"poll task failed: {type(exc).__name__} ({key})")

    async def _cancel_poll(self, window: _Window) -> None:
        task = window.poll_task
        window.poll_task = None
        if task is None:
            return
        with contextlib.suppress(Exception):
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _list_chunks(self, env: Any, window: _Window) -> list[str]:
        remote = f"{window.out_dir}/{CHUNKS_DIRNAME}"
        result = await self._exec(
            env, f"ls -1 {shlex.quote(remote)} 2>/dev/null", timeout=EXEC_SMALL_TIMEOUT
        )
        if result.return_code != 0:
            probe = await self._exec(
                env, f"test -d {shlex.quote(remote)}", timeout=EXEC_SMALL_TIMEOUT
            )
            if probe.return_code != 0:
                return []
            raise RuntimeError("chunk listing failed while the chunks directory exists")
        names = [line.strip() for line in (result.stdout or "").splitlines()]
        return sorted({name for name in names if CHUNK_NAME_RE.match(name)})

    def _ingest(self, window: _Window, data: bytes) -> list[dict[str, Any]]:
        """Fold downloaded chunk bytes into access records (carry-aware)."""
        loss = scan_loss_tokens(window.carry + data)
        frames, invalid, carry = parse_stream_chunk(window.carry, data)
        window.carry = carry
        window.invalid_frames += invalid
        window.loss_overflow = window.loss_overflow or loss["overflow"]
        window.loss_ignored = window.loss_ignored or loss["ignored"]
        window.loss_unmount = window.loss_unmount or loss["unmount"]
        return self._access_records(window, frames)

    def _access_records(
        self, window: _Window, frames: list[tuple[list[str], bool, str]]
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for events, is_directory, raw_path in frames:
            if window.saved_events >= MAX_SAVED_EVENTS:
                if not window.bound_noted:
                    window.bound_noted = True
                    self._note(
                        window,
                        f"event record bound {MAX_SAVED_EVENTS} hit; "
                        "further events counted, not stored",
                    )
                continue
            classified = classify_path(raw_path, window.targets)
            if classified is None:
                window.invalid_frames += 1
                continue
            category, protected_root = classified
            records.append(
                access_record(
                    window_id=window.window_id,
                    path=raw_path,
                    events=[name for name in events if str(name).upper() in {"OPEN", "ACCESS"}]
                    or list(events),
                    is_directory=is_directory,
                    category=category,
                    protected_root=protected_root,
                )
            )
            window.saved_events += 1
        return records

    async def _poll_pass(self, window: _Window) -> None:
        """Download unseen sealed chunks once; append their access records."""
        if window.env is None:
            return
        try:
            names = await self._list_chunks(window.env, window)
        except Exception as exc:  # noqa: BLE001 -- transient; retried next pass
            self._note(window, f"chunk listing missed: {exc}")
            return
        for name in names:
            if name in window.seen_chunks:
                continue
            if name != f"c{len(window.seen_chunks):06d}.bin":
                self._note(window, f"chunk sequence gap before {name}")
                break
            try:
                data = await self._download_text(
                    window.env,
                    window,
                    f"{window.out_dir}/{CHUNKS_DIRNAME}/{name}",
                    what=f"chunk {name}",
                )
            except Exception as exc:  # noqa: BLE001 -- retried next pass
                self._note(window, f"chunk {name} missed: {exc}")
                # Preserve byte order across retries: skipping a missing middle
                # fragment could fabricate a different, valid-looking pathname.
                break
            window.seen_chunks.add(name)
            records = self._ingest(window, data)
            if records:
                self._append(window.trial_dir, records)

    async def _fetch_status(
        self, env: Any, window: _Window, details: list[str]
    ) -> dict[str, Any]:
        try:
            raw = await self._download_text(
                env, window, f"{window.out_dir}/{STATUS_FILENAME}", what="capture status"
            )
        except Exception as exc:  # noqa: BLE001
            details.append(f"capture status missing: {exc}")
            return {}
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(parsed, dict):
                if parsed.get("loss_events") == "fallback-unverified":
                    window.loss_mode = "fallback-unverified"
                for watch_error in parsed.get("watch_errors") or []:
                    details.append(f"watch: {watch_error}")
                if parsed.get("truncated"):
                    window.stream_truncated = True
                    details.append("sandbox stream hit its byte bound; watching stopped early")
                if parsed.get("stdbuf") == "unavailable":
                    details.append("stdbuf unavailable: live chunks may lag buffer fills")
                tail = str(parsed.get("stderr_tail") or "")[-STDERR_TAIL_CHARS:].strip()
                if tail:
                    details.append(f"observer stderr: {tail}")
                return parsed
        details.append("capture status is not a JSON document")
        return {}

    async def _final_drain(self, env: Any, window: _Window, details: list[str]) -> None:
        """Download unseen chunks in order after the observer stops."""
        try:
            names = await self._list_chunks(env, window)
        except Exception as exc:  # noqa: BLE001
            details.append(f"final chunk listing failed: {exc}")
            names = []
        records: list[dict[str, Any]] = []
        for name in names:
            if name in window.seen_chunks:
                continue
            try:
                data = await self._download_text(
                    env, window, f"{window.out_dir}/{CHUNKS_DIRNAME}/{name}", what=f"chunk {name}"
                )
            except Exception as exc:  # noqa: BLE001
                details.append(f"final chunk {name} missed: {exc}")
                continue
            window.seen_chunks.add(name)
            records.extend(self._ingest(window, data))
        if window.carry:
            frames, invalid, truncated = parse_frames(window.carry)
            window.carry = b""
            window.invalid_frames += invalid
            if truncated:
                window.stream_truncated = True
            records.extend(self._access_records(window, frames))
        if records:
            self._append(window.trial_dir, records)

    async def _after_entries(
        self, env: Any, window: _Window, details: list[str]
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        helper = f"{window.scratch}/{HELPER_FILENAME}"
        try:
            manifest = await self._take_snapshot(env, helper, window, label="after")
        except Exception as exc:  # noqa: BLE001
            details.append(f"after snapshot failed: {exc}")
            return {}, False
        after_path = window.trial_dir / EVALUATOR_SNAPSHOT_DIR / f"after-w{window.window_id}.json"
        after_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        if manifest.get("truncated"):
            details.append("after snapshot hit the entry bound")
            self._note(window, "after snapshot truncated; file changes unknown")
            return {}, False
        return {str(item.get("path")): item for item in manifest.get("entries") or []}, True

    def _change_records(
        self, window: _Window, after_entries: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for body in diff_entries(window.baseline_entries, after_entries):
            if body.get("category") != CATEGORY_GRADER:
                continue
            records.append(
                file_change_record(
                    window_id=window.window_id,
                    path=str(body["path"]),
                    category=str(body.get("category", CATEGORY_GRADER)),
                    change=str(body["change"]),
                    before_sha256=body.get("before_sha256"),
                    after_sha256=body.get("after_sha256"),
                    baseline_ref=window.baseline_ref,
                )
            )
        return records

    async def _cleanup(self, env: Any, window: _Window) -> None:
        if window.control_pid is not None:
            await self._stop_observer(env, window, [])
        with contextlib.suppress(Exception):
            await self._exec(
                env, f"rm -rf {shlex.quote(window.scratch)}", timeout=EXEC_SMALL_TIMEOUT
            )

    # -- Log sink and relocation consolidation ----------------------------------

    def _append(self, trial_dir: Path, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        path = trial_dir / FILE_ACCESS_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")

    async def _consolidate_trial(self, key: str, trial_name: str, trial_dir: Path) -> None:
        """Merge step-relocated fragments back into the root per-trial log.

        Multi-step Harbor moves the trial-root ``agent/`` contents (including
        this log) into ``steps/{name}/agent/`` after each step, so without
        consolidation the root log would retain only the last window. Fragments
        merge in ``(window_id, observed_at)`` order with byte-identical lines
        deduplicated (resume steps copy rather than move); malformed lines are
        counted and skipped, never interpolated. A no-fragment trial is a
        no-op with no extra record.
        """
        _ = trial_name
        if key in self._consolidated:
            return
        self._consolidated.add(key)
        steps_dir = trial_dir / "steps"
        fragments: list[Path] = []
        if steps_dir.is_dir():
            with contextlib.suppress(OSError):
                fragments = sorted(steps_dir.glob("*/agent/file-access.jsonl"))
        if not fragments:
            return
        merged: list[tuple[tuple[int, int, str], str]] = []
        seen_lines: set[str] = set()
        duplicates = 0
        malformed = 0
        sources = [trial_dir / FILE_ACCESS_LOG, *fragments]
        for source in sources:
            try:
                text = source.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                if line in seen_lines:
                    duplicates += 1
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if not isinstance(record, dict):
                    malformed += 1
                    continue
                window_id = record.get("window_id")
                observed = record.get("observed_at")
                if isinstance(window_id, int):
                    order = (0, window_id, observed if isinstance(observed, str) else "")
                else:
                    order = (1, 0, str(window_id))
                merged.append((order, line))
                seen_lines.add(line)
        merged.sort(key=lambda item: item[0])
        root = trial_dir / FILE_ACCESS_LOG
        root.parent.mkdir(parents=True, exist_ok=True)
        root.write_text("".join(line + "\n" for _, line in merged), encoding="utf-8")
        top_window = self._counters.get(key, 1)
        self._append(
            trial_dir,
            [
                coverage_record(
                    window_id=top_window,
                    state="stopped",
                    reason=(
                        f"consolidated {len(fragments)} relocated step fragment(s): "
                        f"{duplicates} duplicate line(s) dropped, "
                        f"{malformed} malformed line(s) skipped"
                    ),
                )
            ],
        )

    # -- Trial binding stash --------------------------------------------------

    @property
    def _trials(self) -> dict[str, Any]:
        stash = self.__dict__.setdefault("_trial_stash", {})
        return stash

    def _remember_trial(self, trial: Any) -> None:
        name = getattr(getattr(trial, "config", None), "trial_name", None)
        if name:
            self._trials[str(name)] = trial
