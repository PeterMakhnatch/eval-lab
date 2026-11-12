"""Host-wide FIFO slot lock for shared local Docker (``evallab docker-lock``).

Several agent tabs on one Mac share the local Docker daemon. This module
provides an advisory, host-wide FIFO queue: cooperating callers enqueue and
only the first ``slots`` entries run at once, in strict queue order. State
lives in ``$EVALLAB_DOCKER_LOCK_DIR`` or ``~/.cache/evallab/docker-lock/`` so
every process, tab, and worktree on the machine contends on the same files.

Queue mutations are serialised with ``fcntl.flock`` on a mutex file and the
queue file itself is replaced atomically. Entries whose pid is dead or whose
heartbeat is older than the TTL are pruned, so a crashed holder cannot pin a
slot forever. See ``docs/execution-tiers.md``.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DEFAULT_NAME = "docker"
DEFAULT_TTL_SECONDS = 120.0
DEFAULT_POLL_SECONDS = 0.5
DEFAULT_HEARTBEAT_SECONDS = 15.0
#: Host-wide concurrency default. The host has 16 cores and 64 GiB; the
#: Docker VM (about 8 GiB) is the binding limit, and measured trial
#: containers use a few hundred MiB each, so 4 concurrent holders fit.
#: Lower it with $EVALLAB_DOCKER_LOCK_SLOTS if containers start OOMing.
DEFAULT_SLOTS = 4
ENV_DIR = "EVALLAB_DOCKER_LOCK_DIR"
ENV_SLOTS = "EVALLAB_DOCKER_LOCK_SLOTS"

ClockFn = Callable[[], float]
SleepFn = Callable[[float], None]
PidAliveFn = Callable[[int], bool]


class DockerLockTimeout(TimeoutError):
    """Raised when a slot is not granted within the requested timeout."""


def default_state_dir() -> Path:
    """Return the lock state dir: env override, else the per-user cache."""
    override = os.environ.get(ENV_DIR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "evallab" / "docker-lock"


def default_slots() -> int:
    """Return the default slot count: ``$EVALLAB_DOCKER_LOCK_SLOTS`` or 4."""
    raw = os.environ.get(ENV_SLOTS)
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            return DEFAULT_SLOTS
        if parsed >= 1:
            return parsed
    return DEFAULT_SLOTS


def _validate_name(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"invalid lock name: {name!r}")
    return name


def queue_path(name: str = DEFAULT_NAME, state_dir: Path | None = None) -> Path:
    base = state_dir if state_dir is not None else default_state_dir()
    return base / f"{_validate_name(name)}.queue.json"


def mutex_path(name: str = DEFAULT_NAME, state_dir: Path | None = None) -> Path:
    base = state_dir if state_dir is not None else default_state_dir()
    return base / f"{_validate_name(name)}.mutex"


def pid_alive(pid: int) -> bool:
    """Return True when ``pid`` names a live process on this host."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _is_stale(
    entry: dict[str, Any],
    *,
    now: float,
    ttl: float,
    is_alive: PidAliveFn,
    hostname: str,
) -> bool:
    heartbeat = entry.get("heartbeat_at")
    if not isinstance(heartbeat, (int, float)) or now - float(heartbeat) > ttl:
        return True
    if entry.get("host") == hostname:
        pid = entry.get("pid")
        if not isinstance(pid, int) or not is_alive(pid):
            return True
    return False


def _read_queue_file(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        return []
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _write_queue_file(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def _locked_queue(
    name: str, state_dir: Path | None
) -> Iterator[tuple[Path, list[dict[str, Any]]]]:
    """Hold the mutex flock and yield ``(queue_path, entries)``.

    The caller mutates the list in place; on clean exit the queue is written
    back atomically. On exception nothing is written.
    """
    base = state_dir if state_dir is not None else default_state_dir()
    base.mkdir(parents=True, exist_ok=True)
    qpath = queue_path(name, base)
    mpath = mutex_path(name, base)
    with mpath.open("a+") as mutex_file:
        fcntl.flock(mutex_file.fileno(), fcntl.LOCK_EX)
        try:
            entries = _read_queue_file(qpath)
            yield qpath, entries
            _write_queue_file(qpath, entries)
        finally:
            fcntl.flock(mutex_file.fileno(), fcntl.LOCK_UN)


class DockerLock:
    """One FIFO queue entry: enqueue, wait for a slot, heartbeat, release."""

    def __init__(
        self,
        label: str,
        *,
        slots: int | None = None,
        name: str = DEFAULT_NAME,
        state_dir: Path | None = None,
        ttl: float = DEFAULT_TTL_SECONDS,
        poll_interval: float = DEFAULT_POLL_SECONDS,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_SECONDS,
        clock: ClockFn = time.time,
        sleep: SleepFn = time.sleep,
        is_alive: PidAliveFn = pid_alive,
        hostname: str | None = None,
        pid: int | None = None,
        cwd: str | None = None,
    ) -> None:
        resolved_slots = default_slots() if slots is None else slots
        if resolved_slots < 1:
            raise ValueError("slots must be >= 1")
        self.label = label
        self.slots = resolved_slots
        self.name = _validate_name(name)
        self.state_dir = state_dir
        self.ttl = ttl
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self._clock = clock
        self._sleep = sleep
        self._is_alive = is_alive
        self.hostname = hostname if hostname is not None else socket.gethostname()
        self.pid = pid if pid is not None else os.getpid()
        self.cwd = cwd if cwd is not None else str(Path.cwd())
        self.ticket = uuid.uuid4().hex
        self._enqueued = False
        self._acquired = False
        self._released = False
        self._stop_heartbeat = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    # -- queue primitives -------------------------------------------------

    def _new_entry(self, now: float) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "pid": self.pid,
            "host": self.hostname,
            "label": self.label,
            "cwd": self.cwd,
            "enqueued_at": now,
            "started_at": None,
            "heartbeat_at": now,
        }

    def enqueue(self) -> None:
        """Append this lock's entry to the queue (idempotent)."""
        if self._enqueued:
            return
        now = self._clock()
        with _locked_queue(self.name, self.state_dir) as (_, entries):
            if not any(entry.get("ticket") == self.ticket for entry in entries):
                entries.append(self._new_entry(now))
        self._enqueued = True
        self._released = False

    def _prune_except_self(
        self, entries: list[dict[str, Any]], now: float
    ) -> None:
        kept = [
            entry
            for entry in entries
            if entry.get("ticket") == self.ticket
            or not _is_stale(
                entry, now=now, ttl=self.ttl, is_alive=self._is_alive, hostname=self.hostname
            )
        ]
        entries[:] = kept

    def try_acquire_step(self) -> tuple[bool, int, int, list[dict[str, Any]]]:
        """One acquire check: prune stale, grant if within ``slots``.

        Returns ``(acquired, position, total, holders)`` where ``position``
        is this entry's 1-based queue index and ``holders`` are the entries
        with a slot (``started_at`` set), after pruning.
        """
        if not self._enqueued:
            self.enqueue()
        now = self._clock()
        with _locked_queue(self.name, self.state_dir) as (_, entries):
            self._prune_except_self(entries, now)
            index = next(
                (i for i, entry in enumerate(entries) if entry.get("ticket") == self.ticket),
                None,
            )
            if index is None:
                # Our entry was pruned by a peer (e.g. long pause); rejoin
                # at the back rather than spinning on a missing ticket.
                entries.append(self._new_entry(now))
                index = len(entries) - 1
            entry = entries[index]
            if index < self.slots:
                if entry.get("started_at") is None:
                    entry["started_at"] = now
                entry["heartbeat_at"] = now
                self._acquired = True
                holders = [e for e in entries if e.get("started_at") is not None]
                return True, index + 1, len(entries), holders
            holders = [e for e in entries if e.get("started_at") is not None]
            return False, index + 1, len(entries), holders

    # -- heartbeat ----------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        while not self._stop_heartbeat.wait(self.heartbeat_interval):
            now = self._clock()
            try:
                with _locked_queue(self.name, self.state_dir) as (_, entries):
                    self._prune_except_self(entries, now)
                    for entry in entries:
                        if entry.get("ticket") == self.ticket:
                            entry["heartbeat_at"] = now
                            break
                    else:
                        self._stop_heartbeat.set()
                        return
            except OSError:
                continue

    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        if self.heartbeat_interval <= 0:
            return
        self._stop_heartbeat.clear()
        thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"docker-lock-heartbeat-{self.ticket[:8]}",
            daemon=True,
        )
        self._heartbeat_thread = thread
        thread.start()

    def _stop_heartbeat_thread(self) -> None:
        self._stop_heartbeat.set()
        thread = self._heartbeat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._heartbeat_thread = None

    # -- acquire / release --------------------------------------------------

    def acquire(
        self,
        timeout: float | None = None,
        *,
        on_wait: Callable[[int, int, list[dict[str, Any]]], None] | None = None,
    ) -> None:
        """Enqueue and block (polling) until a slot is granted.

        Raises :class:`DockerLockTimeout` after ``timeout`` seconds of
        waiting; the waiter entry is removed before raising.
        """
        if self._acquired:
            return
        self.enqueue()
        self._start_heartbeat()
        started = self._clock()
        while True:
            acquired, position, total, holders = self.try_acquire_step()
            if acquired:
                return
            if on_wait is not None:
                on_wait(position, total, holders)
            if timeout is not None and self._clock() - started >= timeout:
                self.release()
                raise DockerLockTimeout(
                    f"timed out after {timeout:g}s waiting for a Docker slot "
                    f"(label {self.label!r})"
                )
            self._sleep(self.poll_interval)

    def release(self) -> None:
        """Remove this lock's entry. Idempotent; safe to call twice."""
        if self._released:
            return
        self._released = True
        self._acquired = False
        self._stop_heartbeat_thread()
        if not self._enqueued:
            return
        self._enqueued = False
        with (
            contextlib.suppress(OSError),
            _locked_queue(self.name, self.state_dir) as (_, entries),
        ):
            entries[:] = [
                entry for entry in entries if entry.get("ticket") != self.ticket
            ]

    def __enter__(self) -> DockerLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


@contextmanager
def docker_slot(
    label: str = "docker",
    *,
    slots: int | None = None,
    name: str = DEFAULT_NAME,
    state_dir: Path | None = None,
    timeout: float | None = None,
    ttl: float = DEFAULT_TTL_SECONDS,
    poll_interval: float = DEFAULT_POLL_SECONDS,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_SECONDS,
    clock: ClockFn = time.time,
    sleep: SleepFn = time.sleep,
    is_alive: PidAliveFn = pid_alive,
    hostname: str | None = None,
) -> Iterator[DockerLock]:
    """Context manager holding one FIFO Docker slot.

    Example: ``with docker_slot(label="my-job"): ...``
    """
    lock = DockerLock(
        label,
        slots=slots,
        name=name,
        state_dir=state_dir,
        ttl=ttl,
        poll_interval=poll_interval,
        heartbeat_interval=heartbeat_interval,
        clock=clock,
        sleep=sleep,
        is_alive=is_alive,
        hostname=hostname,
    )
    lock.acquire(timeout=timeout)
    try:
        yield lock
    finally:
        lock.release()


def run_with_lock(
    label: str,
    command: Sequence[str],
    *,
    slots: int | None = None,
    name: str = DEFAULT_NAME,
    state_dir: Path | None = None,
    timeout: float | None = None,
    poll_interval: float = DEFAULT_POLL_SECONDS,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_SECONDS,
    ttl: float = DEFAULT_TTL_SECONDS,
    clock: ClockFn = time.time,
    sleep: SleepFn = time.sleep,
    is_alive: PidAliveFn = pid_alive,
    hostname: str | None = None,
) -> int:
    """Acquire a slot, run ``command`` as a child, release, return its code.

    Returns 124 on wait timeout. Signals received by this process are
    forwarded to the child; the slot is always released.
    """
    if not command:
        print("error: docker-lock run: no command given", file=sys.stderr)
        return 2
    resolved_slots = slots if slots is not None else default_slots()
    lock = DockerLock(
        label,
        slots=resolved_slots,
        name=name,
        state_dir=state_dir,
        ttl=ttl,
        poll_interval=poll_interval,
        heartbeat_interval=heartbeat_interval,
        clock=clock,
        sleep=sleep,
        is_alive=is_alive,
        hostname=hostname,
    )
    last_wait: tuple[int, int, str] | None = None

    def _on_wait(position: int, total: int, holders: list[dict[str, Any]]) -> None:
        nonlocal last_wait
        holder_desc = (
            f"{holders[0].get('label')} (pid {holders[0].get('pid')})"
            if holders
            else "none"
        )
        key = (position, total, holder_desc)
        if key != last_wait:
            last_wait = key
            print(
                f"docker-lock: waiting for slot ({label}): position {position} of "
                f"{total}; holder: {holder_desc}",
                file=sys.stderr,
            )

    try:
        lock.acquire(timeout=timeout, on_wait=_on_wait)
    except DockerLockTimeout:
        print(
            f"docker-lock: timed out after {timeout:g}s waiting for a slot "
            f"(label {label!r}); command not run",
            file=sys.stderr,
        )
        return 124
    print(
        f"docker-lock: acquired slot ({label}, pid {os.getpid()}); "
        f"running: {' '.join(command)}",
        file=sys.stderr,
    )
    child: subprocess.Popen[bytes] | None = None
    old_handlers: dict[int, Any] = {}

    def _forward(signum: int, _frame: Any) -> None:
        if child is not None and child.poll() is None:
            with contextlib.suppress(OSError):
                child.send_signal(signum)

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError, OSError):
                old_handlers[sig] = signal.signal(sig, _forward)
        child = subprocess.Popen(list(command))
        returncode = child.wait()
    finally:
        for sig, handler in old_handlers.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)
        lock.release()
    if returncode < 0:
        return 128 + abs(returncode)
    return returncode


# ---------------------------------------------------------------------------
# CLI (``evallab docker-lock``)
# ---------------------------------------------------------------------------


def _docker_lock_run_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    del root
    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("error: docker-lock run: LABEL and CMD are required", file=sys.stderr)
        return 2
    return run_with_lock(
        args.label,
        command,
        slots=args.slots,
        name=args.name,
        timeout=args.timeout,
    )


def build_docker_lock_parser(commands: argparse._SubParsersAction) -> None:  # noqa: SLF001
    """Register the ``evallab docker-lock`` subcommand (one self-contained block)."""
    parser = commands.add_parser(
        "docker-lock",
        help="Host-wide FIFO slot lock for shared local Docker ($0, advisory)",
        description=(
            "Host-wide FIFO slot lock: cooperating callers queue so only the "
            "first N run at once, across every tab/worktree on this machine."
        ),
    )
    sub = parser.add_subparsers(dest="docker_lock_command", required=True)

    run = sub.add_parser(
        "run",
        help="Acquire a slot, run CMD, release; exit with the child's code",
    )
    run.add_argument(
        "--slots",
        type=int,
        default=None,
        help=f"Concurrent slots (default: ${ENV_SLOTS} or {DEFAULT_SLOTS})",
    )
    run.add_argument("--name", default=DEFAULT_NAME, help="Lock name (default: docker)")
    run.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Give up waiting after SECONDS (exit 124)",
    )
    run.add_argument("label", help="Queue label")
    run.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command and arguments to run under the slot",
    )
    run.set_defaults(func=_docker_lock_run_command)

