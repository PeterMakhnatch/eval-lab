"""Live Supervisor sources for launchd services, Modal apps and local lab processes.

Three :class:`~evallab.production.live_supervisor.Source` implementations, all read-only
toward their subjects:

* :class:`LaunchdSource` (``services``): the ``com.petermakhnatch.evallab.*`` launch agents.
  Reports a service that keeps reporting the same non-ok status, one that went silent, and
  Python errors in its stderr. Always ``owner=lab, killable=False``: a service is reported,
  never killed by the supervisor.
* :class:`ModalSource` (``modal``): ``modal app list`` plus a bounded ``modal app logs`` tail
  for apps that have running tasks. Production is never killable; only allow-listed lab
  prefixes are.
* :class:`ProcessSource` (``processes``): local ``docker compose`` / ``harbor run`` trials that
  break the ``EVALLAB_DISABLE_LOCAL_DOCKER=1`` contract and lab controllers that have run for
  more than six hours. Never touches ``omp`` processes (the fleet source owns those).

Every external probe (``launchctl``, ``modal``, ``ps``, directories, the clock) is injected, so
the sources are deterministic under test. Thresholds are fixed a priori in the constants below
and are not tuned on any label set.
"""

from __future__ import annotations

import json
import plistlib
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.production.live_supervisor import Event, Observation, Subject, TriggerHit

#: Runs ``argv`` with a timeout in seconds and returns its stdout text.
Runner = Callable[[Sequence[str], float], str]

DEFAULT_LAUNCH_AGENTS_DIR = Path("~/Library/LaunchAgents")
DEFAULT_STATE_DIR = Path("~/.local/state/evallab-prod-live")
LABEL_PREFIX = "com.petermakhnatch.evallab."
SERVICE_GLOB = f"{LABEL_PREFIX}*.plist"

#: Status files written by watchers, relative to the state dir, keyed by label suffix. They are
#: parsed as one JSON document (or JSON lines) and extend the stdout/stderr status history.
KNOWN_STATUS_FILES: Mapping[str, tuple[str, ...]] = {
    "run-atlas-watch": ("run-atlas/logs/run-atlas-watch.out",),
    "docent-watch": ("trace-tools/docent-watch-last.json",),
    "trace-store-sync": ("trace-store/watch-state.json",),
    "trace-store-docent-sync": ("trace-store/watch-state.json",),
}

# ---- launchd thresholds. Origin: Run Health latency conventions (docs/live-supervisor.md): a
# watcher that repeats one failure for six cycles has lost half an hour at a 5 min interval, and
# silence for three missed intervals is the same 3x ratio the core uses for kill (SILENCE_KILL_RATIO).
REPEAT_CYCLES = 6
SILENCE_INTERVALS = 3.0
STATUS_EVENTS = 12
STATUS_TAIL_LINES = 60
ERR_TAIL_LINES = 40
ERR_EVENTS = 3
EVENT_CHARS = 300
LOG_TAIL_BYTES = 256 * 1024
#: Status words that mean "this cycle did not do its job"; everything else is ok.
BAD_STATUSES = frozenset({"blocked", "error", "errored", "failed", "failure", "stalled", "skipped", "aborted"})
_TEXT_BAD = re.compile(r"^(Skipped|Failed|Error|ERROR|Blocked|Aborted)\b")
_STDERR_ERROR = re.compile(r"Traceback \(most recent call last\)|\b\w*Error\b")
_LINE_TS = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)?)\s+(.*)$")

# ---- Modal thresholds. Origin: the Run Health wall-time gate; no lab Modal job is meant to hold
# a sandbox for more than two hours (ts14 / sb1 / fl-verify rollouts finish in minutes).
MODAL_MAX_AGE_SECONDS = 2 * 3600
MODAL_PRODUCTION_APP = "evallab-cloud-production"
#: Description prefixes of apps the lab itself launches (and may therefore stop).
MODAL_LAB_PREFIXES = (
    "ts14-", "sb1-", "fl-verify-", "mimo-", "har", "evallab-", "eval-rollout", "envcheck-",
    "cache-", "mtime-", "2026-", "qa-sft-", "kev-rh", "__harbor__",
)
MODAL_LIST_TIMEOUT = 90.0
MODAL_LOGS_TIMEOUT = 8.0
MODAL_MAX_LOG_SCANS = 5
MODAL_LOG_TAIL_LINES = 200
MODAL_LOG_SIGNATURES: Mapping[str, re.Pattern[str]] = {
    "ImageBuildError": re.compile(r"ImageBuildError"),
    "served_route_mismatch": re.compile(r"served_route_mismatch"),
    "sandbox_gone": re.compile(r"Sandbox .* not found|already shut down"),
}

# ---- Process thresholds. Origin: AGENTS.md contract (EVALLAB_DISABLE_LOCAL_DOCKER=1; no local
# Docker) and the six-hour overnight-controller bound used by the lane handoffs.
PROCESS_MAX_ETIME_SECONDS = 6 * 3600
PS_ARGV = ("ps", "-axo", "pid,ppid,etime,command")
PS_TIMEOUT = 20.0
LAB_PATH_MARKER = "eval-lab"
LOCAL_DOCKER_DETAIL = "local docker trial: contract sets EVALLAB_DISABLE_LOCAL_DOCKER=1"
_DOCKER_COMPOSE = re.compile(r"(?:^|/)docker\s+compose\b")
_HARBOR_RUN = re.compile(r"(?:^|[\s/])harbor\s+run\b")
_ENV_FLAG = re.compile(r"(?:--env|-e)[ =]([^\s]+)", re.IGNORECASE)
#: Harbor backends that run off this Mac; an ``--env`` naming one of these is never local Docker.
REMOTE_ENVS = ("modal", "daytona", "e2b", "runloop", "gke")
_CONTROLLER = re.compile(r"(?:^|[\s/])controller\.py\b|(?:^|[\s/])evallab\s+[a-z]")
#: Launchd-managed servers that legitimately run for days.
_LONG_LIVED_SERVICE = re.compile(r"Application Support/evallab/|evallab[ -](?:results-viewer|dashboard)")
_PS_LINE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*\S)\s*$")
_ETIME = re.compile(r"^(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)$")


# --------------------------------------------------------------------------- default runners


def run_command(argv: Sequence[str], timeout: float) -> str:
    """Run ``argv`` and return stdout; a timeout or non-zero exit raises."""
    done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"{argv[0]} exited {done.returncode}: {done.stderr.strip()[:200]}")
    return done.stdout


def run_command_bounded(argv: Sequence[str], timeout: float) -> str:
    """Run ``argv`` and return whatever stdout arrived; for commands that stream until stopped."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout
        return out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
    return done.stdout


# --------------------------------------------------------------------------- shared helpers


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _clip(text: str, limit: int = EVENT_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None


def _tail_text(path: Path, max_bytes: int) -> str:
    """The last ``max_bytes`` of a text file; a cut first line is dropped. Missing -> ''."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    if size > max_bytes:
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return text


def _tail_lines(path: Path, count: int, max_bytes: int = LOG_TAIL_BYTES) -> list[str]:
    lines = [line for line in _tail_text(path, max_bytes).splitlines() if line.strip()]
    return lines[-count:]


# --------------------------------------------------------------------------- launchd


@dataclass(frozen=True)
class StatusLine:
    """One cycle's report: ``signature`` is None when the cycle was ok."""

    at: datetime | None
    text: str
    signature: str | None
    detail: str = ""


def _json_signature(row: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    status = row.get("status")
    if isinstance(status, str) and status.lower() in BAD_STATUSES:
        reason = row.get("reason")
        parts.append(f"status={status.lower()}" + (f" reason={reason}" if reason else ""))
    if row.get("ok") is False:
        parts.append("ok=false")
    errors = row.get("errors")
    if errors:
        parts.append("errors=" + (",".join(sorted(map(str, errors))) if isinstance(errors, Mapping) else "yes"))
    if row.get("budget_exhausted") and not row.get("written"):
        parts.append("budget_exhausted written=0")
    targets = row.get("targets")
    if isinstance(targets, Mapping):
        for name, target in sorted(targets.items(), key=lambda item: str(item[0])):
            if not isinstance(target, Mapping):
                continue
            target_status = target.get("status")
            if isinstance(target_status, str) and target_status.lower() in BAD_STATUSES:
                parts.append(f"{name}:{target_status.lower()}")
            if target.get("error_type"):
                parts.append(f"{name}:{target['error_type']}")
            if target.get("paused"):
                parts.append(f"{name}:paused" + (f" reason={target['reason']}" if target.get("reason") else ""))
    return "; ".join(parts) or None


def parse_status_line(line: str) -> StatusLine:
    """Parse one JSON-or-text cycle line. Non-status text is an ok cycle."""
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            row = json.loads(stripped)
        except ValueError:
            row = None
        if isinstance(row, dict):
            return StatusLine(_parse_time(row.get("at")), _clip(stripped), _json_signature(row))
    match = _LINE_TS.match(stripped)
    at, body = (_parse_time(match.group(1)), match.group(2)) if match else (None, stripped)
    bad = _TEXT_BAD.match(body)
    return StatusLine(at, _clip(stripped), bad.group(1).title() if bad else None, _clip(body, 160) if bad else "")


def _status_entries(path: Path, count: int) -> list[StatusLine]:
    """Cycle reports from a log tail (one per line) or one whole JSON status document."""
    text = _tail_text(path, LOG_TAIL_BYTES)
    if not text.strip():
        return []
    if text.lstrip().startswith("{"):
        try:
            whole = json.loads(text)
        except ValueError:
            whole = None
        if isinstance(whole, dict):
            return [StatusLine(_parse_time(whole.get("at")), _clip(text), _json_signature(whole))]
    return [parse_status_line(line) for line in text.splitlines() if line.strip()][-count:]


def parse_launchctl_list(output: str) -> dict[str, tuple[int | None, int | None]]:
    """``launchctl list`` rows (``pid<TAB>status<TAB>label``) -> {label: (pid, last_exit)}."""
    loaded: dict[str, tuple[int | None, int | None]] = {}
    for row in output.splitlines():
        parts = row.split("\t")
        if len(parts) != 3 or parts[2].strip() == "Label":
            continue
        pid, status, label = parts
        loaded[label.strip()] = (int(pid) if pid.strip().isdigit() else None,
                                 int(status) if status.strip().lstrip("-").isdigit() else None)
    return loaded


class LaunchdSource:
    """The evallab launch agents. Observation events are the last status lines (stdout, or stderr
    when stdout is empty), then up to three stderr error lines. All decisions about a service go to
    research: the subject is never killable."""

    name = "services"

    def __init__(self, *, launch_agents_dir: Path = DEFAULT_LAUNCH_AGENTS_DIR,
                 state_dir: Path = DEFAULT_STATE_DIR, runner: Runner = run_command,
                 launchctl_timeout: float = 15.0) -> None:
        self.launch_agents_dir = Path(launch_agents_dir).expanduser()
        self.state_dir = Path(state_dir).expanduser()
        self.runner = runner
        self.launchctl_timeout = launchctl_timeout

    def _loaded(self) -> dict[str, tuple[int | None, int | None]] | None:
        try:
            return parse_launchctl_list(self.runner(("launchctl", "list"), self.launchctl_timeout))
        except Exception:  # noqa: BLE001 - an unreadable launchctl leaves loaded state unknown
            return None

    def observe(self, now: datetime) -> Iterable[Observation]:
        loaded = self._loaded()
        for plist_path in sorted(self.launch_agents_dir.glob(SERVICE_GLOB)):
            if ".bak" in plist_path.name:
                continue
            try:
                plist = plistlib.loads(plist_path.read_bytes())
            except (OSError, plistlib.InvalidFileException, ValueError):
                continue
            observation = self._observe_service(plist, plist_path, loaded, now)
            if observation is not None:
                yield observation

    def _observe_service(self, plist: Mapping[str, Any], plist_path: Path,
                         loaded: Mapping[str, tuple[int | None, int | None]] | None,
                         now: datetime) -> Observation | None:
        label = plist.get("Label")
        if not isinstance(label, str) or not label.startswith(LABEL_PREFIX):
            return None
        suffix = label[len(LABEL_PREFIX):]
        interval = plist.get("StartInterval")
        interval = int(interval) if isinstance(interval, int) and interval > 0 else None
        out_path = Path(plist["StandardOutPath"]).expanduser() if plist.get("StandardOutPath") else None
        err_path = Path(plist["StandardErrorPath"]).expanduser() if plist.get("StandardErrorPath") else None

        history: list[StatusLine] = []
        source_path: Path | None = None
        for candidate in (out_path, err_path):
            if candidate is not None:
                history = _status_entries(candidate, STATUS_TAIL_LINES)
                if history:
                    source_path = candidate
                    break
        known = [self.state_dir / rel for rel in KNOWN_STATUS_FILES.get(suffix, ())]
        for path in known:
            if path == source_path:
                continue
            latest = (_status_entries(path, 1) or [None])[-1]
            if latest is None:
                continue
            last_at = history[-1].at if history else None
            if not history or (latest.at is not None and (last_at is None or latest.at > last_at)):
                history.append(latest)

        run, run_signature = _trailing_run(history)
        window = history[-STATUS_EVENTS:]
        events = [Event(index, line.at, "status", line.text) for index, line in enumerate(window)]
        run_start = len(window) - min(run, len(window))
        run_steps = tuple(range(run_start, len(window)))[-5:] if run else ()

        err_lines = _recent_error_lines(err_path, now, interval)
        for line in err_lines[-ERR_EVENTS:]:
            events.append(Event(len(events), None, "stderr", _clip(line)))
        err_steps = tuple(range(len(window), len(events)))

        written = [m for m in (_mtime(p) for p in (out_path, err_path, *known) if p is not None) if m]
        last_written = max(written) if written else None
        pid, last_exit = (loaded or {}).get(label, (None, None))
        is_loaded = None if loaded is None else label in loaded
        metrics = {
            "label": label, "plist": plist_path.name, "interval_seconds": interval,
            "calendar_scheduled": bool(plist.get("StartCalendarInterval")),
            "keep_alive": bool(plist.get("KeepAlive")), "loaded": is_loaded, "pid": pid,
            "last_exit_status": last_exit, "status_cycles": len(history),
            "status_run": run, "status_signature": run_signature,
            "status_detail": next((h.detail for h in reversed(history) if h.signature), ""),
            "status_source": source_path.name if source_path else None,
            "status_run_steps": list(run_steps),
            "last_written_at": last_written.isoformat() if last_written else None,
            "silent_seconds": (now - last_written).total_seconds() if last_written else None,
            "stderr_error_lines": len(err_lines), "stderr_steps": list(err_steps),
        }
        return Observation(Subject("launchd_service", label, "lab", False, label=suffix), now,
                           tuple(events), metrics, actor_inputs_available=True,
                           terminal=is_loaded is False)

    def triggers(self, observation: Observation) -> list[TriggerHit]:
        m = observation.metrics
        if m.get("loaded") is False:
            return []
        hits: list[TriggerHit] = []
        run = int(m.get("status_run") or 0)
        if run >= REPEAT_CYCLES:
            hits.append(TriggerHit(
                "repetition", "infra", run / REPEAT_CYCLES,
                f"{run} consecutive cycles report {m.get('status_detail') or m['status_signature']}",
                tuple(m.get("status_run_steps") or ())))
        interval, silent = m.get("interval_seconds"), m.get("silent_seconds")
        if interval and silent is not None and silent >= SILENCE_INTERVALS * interval:
            hits.append(TriggerHit(
                "silence", "latency", silent / (SILENCE_INTERVALS * interval),
                f"no log or status write for {int(silent)}s (interval {interval}s"
                + (f", job running pid {m['pid']})" if m.get("pid") else ")")))
        if m.get("stderr_error_lines"):
            hits.append(TriggerHit(
                "error_signature", "infra", 1.0,
                f"{m['stderr_error_lines']} Traceback/Error line(s) in recent stderr",
                tuple(m.get("stderr_steps") or ())))
        return hits


def _trailing_run(history: Sequence[StatusLine]) -> tuple[int, str | None]:
    """Length of the trailing run of cycles sharing one non-ok signature, and that signature."""
    if not history or history[-1].signature is None:
        return 0, None
    signature = history[-1].signature
    run = 0
    for line in reversed(history):
        if line.signature != signature:
            break
        run += 1
    return run, signature


def _recent_error_lines(err_path: Path | None, now: datetime, interval: int | None) -> list[str]:
    """Traceback/Error lines in the tail of a stderr log that was written recently enough to still
    describe the last cycle (twice the interval, or an hour for services without one)."""
    if err_path is None:
        return []
    written = _mtime(err_path)
    window = 2 * interval if interval else 3600
    if written is None or (now - written).total_seconds() > window:
        return []
    return [line for line in _tail_lines(err_path, ERR_TAIL_LINES) if _STDERR_ERROR.search(line)]


# --------------------------------------------------------------------------- modal


def modal_owner(description: str) -> str:
    """production for the production app, lab for allow-listed prefixes, else other."""
    if description == MODAL_PRODUCTION_APP:
        return "production"
    return "lab" if description.startswith(MODAL_LAB_PREFIXES) else "other"


def _task_count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def log_signatures(text: str) -> dict[str, str]:
    """{signature name: first matching line} over the tail of a ``modal app logs`` output."""
    found: dict[str, str] = {}
    for line in text.splitlines()[-MODAL_LOG_TAIL_LINES:]:
        for name, pattern in MODAL_LOG_SIGNATURES.items():
            if name not in found and pattern.search(line):
                found[name] = _clip(line)
    return found


class ModalSource:
    """Modal apps from ``modal app list --json``. Apps with no running task are observed (ownership
    and counts) but never trigger. ``production`` apps are deployed services, so wall time does not
    apply to them, and they are never killable."""

    name = "modal"

    def __init__(self, *, runner: Runner = run_command, logs_runner: Runner | None = run_command_bounded,
                 list_timeout: float = MODAL_LIST_TIMEOUT, logs_timeout: float = MODAL_LOGS_TIMEOUT,
                 max_log_scans: int = MODAL_MAX_LOG_SCANS) -> None:
        self.runner = runner
        self.logs_runner = logs_runner
        self.list_timeout = list_timeout
        self.logs_timeout = logs_timeout
        self.max_log_scans = max_log_scans

    def observe(self, now: datetime) -> Iterable[Observation]:
        apps = json.loads(self.runner(("keys", "run", "--", "modal", "app", "list", "--json"),
                                      self.list_timeout))
        if not isinstance(apps, list):
            raise TypeError("modal app list --json did not return a list")
        rows = [a for a in apps if isinstance(a, dict) and a.get("app_id")]
        running = sum(1 for a in rows if _task_count(a.get("tasks")) > 0)
        deployed_idle = sum(1 for a in rows if _task_count(a.get("tasks")) == 0 and a.get("state") == "deployed")
        scans = 0
        for app in rows:
            description = str(app.get("description") or "")
            owner = modal_owner(description)
            tasks = _task_count(app.get("tasks"))
            created = _parse_time(app.get("created_at"))
            stopped = app.get("stopped_at")
            events: list[Event] = []
            signatures: dict[str, str] = {}
            logs_error = None
            if tasks > 0 and self.logs_runner is not None and scans < self.max_log_scans:
                scans += 1
                try:
                    signatures = log_signatures(self.logs_runner(
                        ("keys", "run", "--", "modal", "app", "logs", str(app["app_id"])), self.logs_timeout))
                except Exception as exc:  # noqa: BLE001 - a log fetch failure must not hide the app
                    logs_error = f"{type(exc).__name__}: {exc}"[:200]
                events = [Event(i, None, "modal-logs", f"{name}: {line}")
                          for i, (name, line) in enumerate(signatures.items())]
            metrics = {
                "description": description, "state": app.get("state"), "tasks": tasks,
                "created_at": created.isoformat() if created else None,
                "age_seconds": (now - created).total_seconds() if created else None,
                "log_signatures": sorted(signatures), "log_steps": list(range(len(events))),
                "logs_error": logs_error, "apps_total": len(rows), "apps_running": running,
                "deployed_idle_apps": deployed_idle,
            }
            yield Observation(
                Subject("modal_app", str(app["app_id"]), owner, owner == "lab", label=description),
                now, tuple(events), metrics, actor_inputs_available=False,
                terminal=bool(stopped) or app.get("state") == "stopped")

    def triggers(self, observation: Observation) -> list[TriggerHit]:
        m = observation.metrics
        if int(m.get("tasks") or 0) <= 0:
            return []
        hits: list[TriggerHit] = []
        age = m.get("age_seconds")
        if age is not None and age >= MODAL_MAX_AGE_SECONDS and observation.subject.owner != "production":
            hits.append(TriggerHit("wall_time", "runaway", age / MODAL_MAX_AGE_SECONDS,
                                   f"{m['tasks']} task(s) running, app is {age / 3600:.1f}h old"))
        if m.get("log_signatures"):
            hits.append(TriggerHit("error_signature", "infra", 1.0,
                                   "modal logs show " + ", ".join(m["log_signatures"]),
                                   tuple(m.get("log_steps") or ())))
        return hits


# --------------------------------------------------------------------------- processes


def parse_etime(value: str) -> int | None:
    """``ps`` elapsed time ``[[dd-]hh:]mm:ss`` -> seconds."""
    match = _ETIME.match(value)
    if not match:
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    etime_seconds: int
    command: str


def parse_ps(output: str) -> list[Proc]:
    procs: list[Proc] = []
    for row in output.splitlines():
        match = _PS_LINE.match(row)
        if not match:
            continue
        etime = parse_etime(match.group(3))
        if etime is not None:
            procs.append(Proc(int(match.group(1)), int(match.group(2)), etime, match.group(4)))
    return procs


def _is_omp(command: str) -> bool:
    first = command.split(None, 2)[:2]
    return any(Path(token).name == "omp" for token in first) or "__omp_" in command


def _is_local_docker(command: str) -> bool:
    """Only explicit evidence counts: ``docker compose``, or ``harbor run`` whose ``--env``/``-e``
    names a Docker environment. A harbor run with no env flag (or whose backend lives in a
    ``--config``) stays unflagged: argv alone is not proof of a local Docker backend."""
    if LAB_PATH_MARKER not in command:
        return False
    if _DOCKER_COMPOSE.search(command):
        return True
    if not _HARBOR_RUN.search(command):
        return False
    envs = [value.lower() for value in _ENV_FLAG.findall(command)]
    if any(remote in value for value in envs for remote in REMOTE_ENVS):
        return False
    return any("docker" in value for value in envs)


def _is_controller(command: str) -> bool:
    return bool(_CONTROLLER.search(command)) and not _LONG_LIVED_SERVICE.search(command)


class ProcessSource:
    """Local lab processes from ``ps``. Wrapper chains (``uv run`` -> python, harbor -> docker
    compose) are one subject: only the outermost matching process is observed.

    Report-only by design, not by omission: a PID plus argv is not a stable stop identity (PIDs
    are reused, and the matching wrapper usually exits before the children it spawned), so a
    decision here is ``alert_research``; Harbor and Modal subjects carry their own registered stop
    paths."""

    name = "processes"

    def __init__(self, *, runner: Runner = run_command, ps_timeout: float = PS_TIMEOUT) -> None:
        self.runner = runner
        self.ps_timeout = ps_timeout

    def observe(self, now: datetime) -> Iterable[Observation]:
        candidates: dict[int, tuple[Proc, bool, bool]] = {}
        for proc in parse_ps(self.runner(PS_ARGV, self.ps_timeout)):
            if _is_omp(proc.command):
                continue
            docker, controller = _is_local_docker(proc.command), _is_controller(proc.command)
            if docker or controller:
                candidates[proc.pid] = (proc, docker, controller)
        for pid, (proc, docker, controller) in sorted(candidates.items()):
            parent = candidates.get(proc.ppid)
            if parent is not None and ((docker and parent[1]) or (controller and not docker and parent[2])):
                continue
            metrics = {"pid": pid, "ppid": proc.ppid, "etime_seconds": proc.etime_seconds,
                       "local_docker": docker, "controller": controller}
            yield Observation(
                Subject("local_process", str(pid), "lab", False, label=_clip(proc.command, 120)),
                now, (Event(0, None, "ps", _clip(proc.command)),), metrics,
                actor_inputs_available=False)

    def triggers(self, observation: Observation) -> list[TriggerHit]:
        m = observation.metrics
        hits: list[TriggerHit] = []
        if m.get("local_docker"):
            hits.append(TriggerHit("error_signature", "other", 1.0, LOCAL_DOCKER_DETAIL, (0,)))
        etime = int(m.get("etime_seconds") or 0)
        if m.get("controller") and etime >= PROCESS_MAX_ETIME_SECONDS:
            hits.append(TriggerHit("wall_time", "runaway", etime / PROCESS_MAX_ETIME_SECONDS,
                                   f"lab controller running {etime / 3600:.1f}h", (0,)))
        return hits
