from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab import database
from evallab import profiles as profiles_module
from evallab.queue import (
    DirectoryQueue,
    Executor,
    new_ulid,
)
from evallab.runner import database_url_from_environment, subscription_environment
from evallab.schemas import (
    BrokenLaunchAgent,
    HeadlessDoctorChecks,
    HeadlessDoctorReport,
    LaunchAgentAudit,
    LaunchAgentPathKind,
    QueueEvent,
)

MIN_FREE_DISK_BYTES = 5 * 1024**3
MIN_FREE_DISK_FRACTION = 0.05

BooleanProbe = Callable[[], bool]
STEP_KILL_GRACE_SECONDS = 5.0
_PROCESS_POLL_SECONDS = 0.05


class StepTimeout(BaseException):
    """A step outlived its declared timeout; its new child processes are already gone.

    A ``BaseException`` (like ``KeyboardInterrupt``) so the ``except Exception``
    bookkeeping inside step bodies cannot swallow or reclassify the expiry.
    """

    def __init__(self, seconds: float, terminated: tuple[int, ...]) -> None:
        super().__init__(
            f"exceeded declared timeout of {seconds:g}s; "
            f"terminated {len(terminated)} child process(es)"
        )
        self.seconds = seconds
        self.terminated = terminated


def _process_table() -> dict[int, tuple[int, str]]:
    """Map live pid -> (ppid, state) with one ``ps`` call; zombies count as gone."""
    process = subprocess.Popen(
        ["ps", "-A", "-o", "pid=,ppid=,stat="],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=subscription_environment(),
    )
    output, _ = process.communicate(timeout=10)
    table: dict[int, tuple[int, str]] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            pid = int(fields[0])
            if pid != process.pid and not fields[2].startswith("Z"):
                table[pid] = (int(fields[1]), fields[2])
    return table


def _descendants(table: dict[int, tuple[int, str]], root: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, (parent, _) in table.items():
        children.setdefault(parent, []).append(pid)
    found: set[int] = set()
    frontier = [root]
    while frontier:
        for child in children.get(frontier.pop(), ()):
            if child not in found:
                found.add(child)
                frontier.append(child)
    return found


def _signal_processes(pids: set[int], signum: signal.Signals) -> None:
    for pid in pids:
        try:
            # Harbor-style children lead their own session; signal the whole
            # group so grandchildren that were already reparented die too.
            if os.getpgid(pid) == pid:
                os.killpg(pid, signum)
            else:
                os.kill(pid, signum)
        except (ProcessLookupError, PermissionError):
            continue


def _terminate_new_descendants(baseline: set[int], grace_seconds: float) -> tuple[int, ...]:
    """SIGTERM every descendant started since ``baseline``; SIGKILL survivors."""
    targets = child_processes() - baseline
    if not targets:
        return ()
    _signal_processes(targets, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    alive = targets
    while alive and time.monotonic() < deadline:
        time.sleep(_PROCESS_POLL_SECONDS)
        alive = targets & _process_table().keys()
    if alive:
        _signal_processes(alive, signal.SIGKILL)
    return tuple(sorted(targets))


def child_processes() -> set[int]:
    """Current descendants of this process (one ``ps`` call)."""
    return _descendants(_process_table(), os.getpid())


def run_with_timeout(
    fn: Callable[[], Any],
    seconds: float | None,
    *,
    baseline: set[int] | None = None,
    grace_seconds: float = STEP_KILL_GRACE_SECONDS,
) -> Any:
    """Run ``fn`` under a SIGALRM wall-clock bound, or unbounded when ``None``.

    On expiry, descendants not in ``baseline`` (default: those alive when this
    call starts) are terminated before ``StepTimeout`` unwinds ``fn``, so cleanup
    that waits on a child cannot hang and nothing is orphaned. Never retries:
    callers decide what expiry means.
    """
    if seconds is None:
        return fn()
    if seconds <= 0:
        raise ValueError(f"step timeout must be positive, got {seconds}")
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("step timeouts use SIGALRM and must run on the main thread")
    known = child_processes() if baseline is None else baseline

    def expire(_signum: int, _frame: object) -> None:
        raise StepTimeout(seconds, _terminate_new_descendants(known, grace_seconds))

    previous = signal.signal(signal.SIGALRM, expire)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            return fn()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    finally:
        signal.signal(signal.SIGALRM, previous)


LAUNCH_AGENT_LABEL_PREFIX = "com.petermakhnatch.evallab."
_SHELL_PROGRAMS = frozenset({"/bin/zsh", "/bin/bash", "/bin/sh"})
_SHELL_COMMAND_FLAGS = frozenset({"-lc", "-cl", "-c"})


def _shell_command_string(argv: Sequence[str]) -> str | None:
    """The command string a ``<shell> -lc '<command>'`` plist passes to the shell."""
    for index, token in enumerate(argv):
        if index == 0 or token not in _SHELL_COMMAND_FLAGS:
            continue
        return argv[index + 1] if index + 1 < len(argv) else None
    return None


def _shell_command_references(command: str) -> list[tuple[LaunchAgentPathKind, str]]:
    """Working directory and runtime a shell command string names.

    Only the installer's shape is read — an optional leading
    ``cd <dir> &&`` followed by the command word — because any other absolute
    argument is data the agent may create on first run, not a path it needs to
    start.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    references: list[tuple[LaunchAgentPathKind, str]] = []
    start = 0
    if len(tokens) >= 4 and tokens[0] == "cd" and tokens[2] == "&&":
        references.append(("working_directory", tokens[1]))
        start = 3
    if start < len(tokens) and os.path.isabs(tokens[start]):
        references.append(("interpreter", tokens[start]))
    return references


def _launch_agent_references(
    definition: Mapping[str, Any],
) -> list[tuple[LaunchAgentPathKind, str]]:
    """Every absolute path an installed plist must resolve to start."""
    references: list[tuple[LaunchAgentPathKind, str]] = []

    def add(kind: LaunchAgentPathKind, value: object) -> None:
        if isinstance(value, str) and os.path.isabs(value):
            references.append((kind, value))

    add("program", definition.get("Program"))
    add("working_directory", definition.get("WorkingDirectory"))
    arguments = definition.get("ProgramArguments")
    argv = (
        [argument for argument in arguments if isinstance(argument, str)]
        if isinstance(arguments, list)
        else []
    )
    if not argv:
        return references
    add("program", argv[0])
    if argv[0] in _SHELL_PROGRAMS:
        command = _shell_command_string(argv)
        if command is None:
            add("program", argv[1] if len(argv) > 1 else None)
        else:
            references.extend(_shell_command_references(command))
    else:
        # An exec wrapper (`keys run -- <interpreter> ...`) starts the runtime
        # it names; a direct interpreter is already covered as argv[0], and its
        # own script/value arguments may legitimately be created on first run.
        rest = argv[1:]
        if rest[:2] == ["run", "--"] and len(rest) > 2:
            add("interpreter", rest[2])
    return references


def _launch_agent_missing_references(
    definition: Mapping[str, Any],
) -> list[tuple[LaunchAgentPathKind, str]]:
    return [
        (kind, path)
        for kind, path in dict.fromkeys(_launch_agent_references(definition))
        if not Path(path).exists()
    ]


def audit_launch_agents(
    home: Path, *, label_prefix: str = LAUNCH_AGENT_LABEL_PREFIX
) -> LaunchAgentAudit:
    """List installed evallab LaunchAgents whose program or runtime path is gone.

    Read-only: it parses ``<home>/Library/LaunchAgents/*.plist`` and checks the
    paths they name. It never calls launchctl, so a run cannot install, load,
    unload or rewrite a single agent.
    """
    directory = home / "Library/LaunchAgents"
    try:
        candidates = sorted(directory.glob("*.plist"))
    except OSError:
        candidates = []
    checked = 0
    broken: list[BrokenLaunchAgent] = []
    for path in candidates:
        filename_label = path.name[: -len(".plist")]
        if not filename_label.startswith(label_prefix):
            continue
        try:
            raw = plistlib.loads(path.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            raw = None
        definition: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
        label = definition.get("Label")
        if not isinstance(label, str) or not label:
            checked += 1
            broken.append(
                BrokenLaunchAgent(
                    label=filename_label,
                    plist_path=str(path),
                    kind="unreadable",
                    path=str(path),
                )
            )
            continue
        if not label.startswith(label_prefix):
            # The filename claims our prefix; the plist belongs to someone else.
            continue
        checked += 1
        broken.extend(
            BrokenLaunchAgent(
                label=label, plist_path=str(path), kind=kind, path=missing_path
            )
            for kind, missing_path in _launch_agent_missing_references(definition)
        )
    return LaunchAgentAudit(checked=checked, broken=broken)


SYNC_STATUS_ENV = "EVALLAB_SYNC_STATUS"
SYNC_INTERVAL_SECONDS_ENV = "EVALLAB_SYNC_INTERVAL_SECONDS"
DEFAULT_SYNC_INTERVAL_SECONDS = 300
SYNC_STALE_INTERVALS = 3


@dataclass(frozen=True)
class SyncPrimaryStatus:
    """The primary checkout's last sync-timer run, as doctor reports it."""

    ok: bool
    outcome: str
    reason: str
    behind: int
    age_seconds: int | None
    detail: str


def default_sync_status_path(home: Path) -> Path:
    """The status file the sync timer writes under the installer's default state dir."""
    return home / "Library/Application Support/evallab/sync-status.json"


def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours, rest = divmod(seconds, 3600)
        return f"{hours}h{rest // 60}m"
    days, rest = divmod(seconds, 86400)
    return f"{days}d{rest // 3600}h"


def _status_text(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _status_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(value), 0)
        except ValueError:
            return 0
    return 0


def sync_primary_status(
    home: Path,
    *,
    now: datetime | None = None,
    status_path: Path | None = None,
    interval_seconds: int | None = None,
) -> SyncPrimaryStatus:
    """Read the primary-checkout sync timer's status file; never write it.

    ``scripts/sync-primary-checkout.sh`` records why each run synced or skipped
    (dirty tree, off-branch, diverged, fetch failure) plus the ahead/behind
    counts. Nothing else surfaces that file, so a timer that stopped running
    or kept skipping a dirty checkout let the primary drift 848 commits behind
    with only a rate-limited notification to say so.

    The row is advisory: it reports ``ok`` when the timer ran within
    ``SYNC_STALE_INTERVALS`` intervals and had nothing to do, and FAIL when the
    file is missing or unreadable, the last run is older than that, the run
    skipped while HEAD is still behind, or its fetch failed. Doctor's exit
    code does not depend on it.

    ``status_path`` or ``EVALLAB_SYNC_STATUS`` overrides the default location;
    ``interval_seconds`` or ``EVALLAB_SYNC_INTERVAL_SECONDS`` overrides the
    timer interval. Both exist so tests never read the real home or environment.
    """
    path = status_path or (
        Path(os.environ[SYNC_STATUS_ENV]).expanduser()
        if os.environ.get(SYNC_STATUS_ENV)
        else default_sync_status_path(home)
    )
    if interval_seconds is None:
        try:
            interval_seconds = int(os.environ.get(SYNC_INTERVAL_SECONDS_ENV, ""))
        except ValueError:
            interval_seconds = 0
        if interval_seconds <= 0:
            interval_seconds = DEFAULT_SYNC_INTERVAL_SECONDS
    stale_after = interval_seconds * SYNC_STALE_INTERVALS
    moment = now or datetime.now(UTC)

    def unreadable(reason: str, detail: str) -> SyncPrimaryStatus:
        return SyncPrimaryStatus(
            ok=False,
            outcome="unreadable",
            reason=reason,
            behind=0,
            age_seconds=None,
            detail=detail,
        )

    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return SyncPrimaryStatus(
            ok=False,
            outcome="missing",
            reason="missing",
            behind=0,
            age_seconds=None,
            detail=f"missing {path} (the sync timer has not run here)",
        )
    except (OSError, ValueError) as exc:
        return unreadable(type(exc).__name__, f"unreadable {path} ({type(exc).__name__})")
    if not isinstance(raw, dict):
        return unreadable("not-an-object", f"unreadable {path} (not a JSON object)")
    try:
        written = datetime.fromisoformat(str(raw.get("timestamp")))
    except ValueError:
        return unreadable("bad-timestamp", f"unreadable {path} (bad timestamp)")
    if written.tzinfo is None:
        written = written.replace(tzinfo=UTC)
    age_seconds = max(int((moment - written).total_seconds()), 0)
    outcome = _status_text(raw.get("outcome"), "unknown")
    reason = _status_text(raw.get("reason"), "unknown")
    behind = _status_count(raw.get("behind"))

    problems: list[str] = []
    if age_seconds > stale_after:
        problems.append(
            f"stale: last run {_format_age(age_seconds)} ago (limit {stale_after}s)"
        )
    if outcome == "skipped" and behind > 0:
        problems.append(f"skipped while {behind} commits behind")
    if raw.get("fetch_ok") is False:
        problems.append("fetch failed")
    detail = f"{outcome} ({reason}) behind {behind} age {_format_age(age_seconds)}"
    if problems:
        detail = "; ".join([detail, *problems])
    return SyncPrimaryStatus(
        ok=not problems,
        outcome=outcome,
        reason=reason,
        behind=behind,
        age_seconds=age_seconds,
        detail=detail,
    )


# Supported host CLI contracts. Python distributions remain hash-pinned in uv.lock.
CORE_TOOL_PINS = {
    "docker-engine": "29.8.0",
    "docker-compose": "5.5.1",
    "omp": "18.18.8",
    "gh": "2.96.0",
    "git": "2.50.1",
    "uv": "0.9.24",
    "keys-code-sha256": "c92060fa29987daf2490e0b69f86fbfa2bb6a45e304ef423cad2072e55e7ce3c",
}


def installed_job_health(
    home: Path,
    *,
    now: datetime | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[tuple[str, bool, str]]:
    """Observe installed jobs, not repository templates; never change launchd.

    Only useful domain receipts qualify. A recent stdout log is not evidence
    of useful work. An unknown receipt is a failed check, not a fresh service.
    """
    state = home / ".local/state"
    prod = state / "evallab-prod-live"
    outputs = {
        "nightly": state / "evallab-nightly/trials.parquet",
        "prod-factory": prod / "factory/factory-cycles.jsonl",
        "prod-factory-watchdog": prod / "factory/factory-watchdog.json",
        "run-atlas-watch": prod / "run-atlas/facets-v1.jsonl",
        "docent-watch": prod / "trace-tools/results-v1.jsonl",
        "scout-watch": prod / "trace-tools/scout-native-actor-v2/results-v1.jsonl",
        "live-supervisor": prod / "live-supervisor/decisions-v1.jsonl",
        "live-supervisor-alarms": prod / "live-supervisor/alarm-cursor.json",
        "live-supervisor-fleet-report": prod / "live-supervisor/fleet-state.json",
        "sync-primary": default_sync_status_path(home),
    }
    checks = []
    for path in sorted((home / "Library/LaunchAgents").glob("*.plist")):
        if not path.stem.startswith(LAUNCH_AGENT_LABEL_PREFIX):
            continue
        label = path.stem
        try:
            definition = plistlib.loads(path.read_bytes())
            if definition.get("Label") != label:
                raise ValueError("plist label mismatch")
            result = run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            loaded = result.returncode == 0
            match = re.search(r"^\s*last exit code = (-?\d+)\s*$", result.stdout, re.MULTILINE)
            exit_code = int(match[1]) if match else None
            short = label.removeprefix(LAUNCH_AGENT_LABEL_PREFIX)
            output = outputs.get(short)
            if short == "insights":
                candidates = list((state / "evallab-insights/passes").glob("*/out/*.json"))
                output = max(candidates, key=lambda path: path.stat().st_mtime, default=None)
            stamp = output.stat().st_mtime if output is not None and output.is_file() and output.stat().st_size else None
            observed_at = now or datetime.now(UTC)
            age = (observed_at.timestamp() - stamp) / 3600 if stamp is not None else None
            if short == "sync-primary" and sync_primary_status(home, now=observed_at).outcome != "synced":
                age = None
            if short == "prod-factory" and output is not None and output.is_file():
                useful_at: datetime | None = None
                with output.open() as stream:
                    for line in stream:
                        cycle = json.loads(line)
                        if any(cycle.get(key, 0) for key in ("emitted", "consumed_meaningful", "replayed_steps")):
                            stamp = cycle.get("ts")
                            if isinstance(stamp, str):
                                useful_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                age = ((now or datetime.now(UTC)) - useful_at).total_seconds() / 3600 if useful_at is not None else None
            interval = definition.get("StartInterval")
            limit_h = max(900, interval * 3) / 3600 if type(interval) is int else 36.0
            cwd = str(definition.get("WorkingDirectory", ""))
            revision = re.search(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])", cwd)
            pin = revision[0] if revision else "MISSING"
            missing = _launch_agent_missing_references(definition)
            running = bool(re.search(r"^\s*state = running\s*$", result.stdout, re.MULTILINE))
            exit_ok = exit_code == 0 or (exit_code is None and running)
            ok = loaded and exit_ok and age is not None and 0 <= age <= limit_h and not missing
            # Missing pin or useful-output mapping is deliberately visible and fails.
            ok = ok and pin != "MISSING"
            detail = (
                f"pin={pin} loaded={loaded} running={running} last_exit={exit_code} "
                f"useful_age_h={round(age, 2) if age is not None else 'MISSING'} "
                f"limit_h={limit_h} "
                f"output={output or 'MISSING'} broken_paths={len(missing)}"
            )
        except Exception as exc:
            ok, detail = False, f"unavailable: {type(exc).__name__}"
        checks.append((f"launchd:{label}", ok, detail))
    return checks


def _python_runtime_health(root: Path) -> tuple[bool, str]:
    from packaging.specifiers import SpecifierSet

    requirement = tomllib.loads((root / "pyproject.toml").read_text())["project"]["requires-python"]
    version = sys.version.split()[0]
    return version in SpecifierSet(requirement), (
        f"installed={version} pin={requirement} (pyproject.toml); CI=3.12,3.14 exact_patch=MISSING"
    )


def core_runtime_checks(
    root: Path,
    *,
    home: Path | None = None,
    now: datetime | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[tuple[str, bool, str]]:
    """Read-only CORE checks for the existing doctor; no inference or downloads.

    subprocess output is never copied into an error. Credential probes discard
    both streams. Only version/config fields and public GitHub rules are used.
    """
    from evallab.backups import backup_freshness, sqlite_store_paths
    from evallab.failure_library.runs.io import retained_source_health
    from evallab.storage.paths import derived_root_from_environment, shared_checkout_root

    home = home or Path.home()
    job_now = now
    now = now or datetime.now(UTC)
    checks: list[tuple[str, bool, str]] = []

    def probe(name: str, check: Callable[[], tuple[bool, str]]) -> None:
        try:
            ok, detail = check()
        except Exception as exc:
            ok, detail = False, f"unavailable: {type(exc).__name__}"
        checks.append((name, ok, detail))

    def command(argv: list[str], **kwargs: Any) -> str:
        result = run(argv, capture_output=True, text=True, timeout=15, check=False, **kwargs)
        if result.returncode:
            raise RuntimeError("health command failed")
        return result.stdout

    lock = tomllib.loads((root / "uv.lock").read_text())
    locked = {package["name"]: package["version"] for package in lock["package"]}
    for package in ("harbor", "psycopg", "duckdb", "pyarrow", "huggingface-hub",
                    "pydantic", "pyyaml", "tomlkit"):
        def library_version(package: str = package) -> tuple[bool, str]:
            version = importlib.metadata.version(package)
            return version == locked[package], f"installed={version} pin={locked[package]} (uv.lock)"
        probe(package, library_version)
    for tool in ("omp", "gh", "git", "uv"):
        def cli_version(tool: str = tool) -> tuple[bool, str]:
            output = command([tool, "--version"])
            match = re.search(r"\d+\.\d+\.\d+", output)
            version = match[0] if match else "unparsed"
            pin = CORE_TOOL_PINS[tool]
            if tool == "omp":
                compatible = version.rsplit(".", 1)[0] == pin.rsplit(".", 1)[0]
                return compatible, f"installed={version} supported={pin.rsplit('.', 1)[0]}.*"
            return version == pin, f"installed={version} pin={pin}"
        probe(f"{tool}-version", cli_version)
    probe("python", lambda: _python_runtime_health(root))

    def docker_engine() -> tuple[bool, str]:
        version = command(["docker", "version", "--format", "{{.Server.Version}}"]).strip()
        return version == CORE_TOOL_PINS["docker-engine"], (
            f"reachable=True installed={version} pin={CORE_TOOL_PINS['docker-engine']}"
        )
    probe("docker-engine", docker_engine)

    def compose() -> tuple[bool, str]:
        version = command(["docker", "compose", "version", "--short"]).strip().removeprefix("v")
        return version == CORE_TOOL_PINS["docker-compose"], (
            f"reachable=True installed={version} pin={CORE_TOOL_PINS['docker-compose']}"
        )
    probe("docker-compose", compose)

    def isolation() -> tuple[bool, str]:
        from evallab.harbor_common import validate_docker_daemon
        info = json.loads(command(["docker", "info", "--format", "{{json .}}"]))
        validate_docker_daemon(info)
        return True, "Linux daemon preflight passes; per-container nft/mount proof remains mandatory"
    probe("docker-isolation", isolation)

    def postgres() -> tuple[bool, str]:
        url = database_url_from_environment()
        with database.psycopg.connect(
            url, connect_timeout=3, options="-c default_transaction_read_only=on -c statement_timeout=5000",
        ) as connection:
            row = connection.execute("SHOW server_version").fetchone()
            if row is None:
                raise ValueError("PostgreSQL returned no server version")
            version = row[0]
            columns = connection.execute(
                "SELECT table_name,column_name,data_type FROM information_schema.columns "
                "WHERE table_schema='public' ORDER BY table_name,ordinal_position"
            ).fetchall()
        fingerprint = hashlib.sha256(json.dumps(columns).encode()).hexdigest()
        schema = hashlib.sha256((root / "sql/schema.sql").read_bytes()).hexdigest()
        # The catalog has no migration ledger. Never invent an applied revision.
        return False, (
            f"reachable=True installed={version} pin=18.4 db={database.identity(url)} "
            f"schema_revision=MISSING schema_sha256={schema} columns_sha256={fingerprint}"
        )
    probe("postgres-schema", postgres)

    def postgres_backup() -> tuple[bool, str]:
        fact = backup_freshness(root, now=now)
        return fact["status"] == "fresh", (
            f"{fact['status']} age_h={fact['age_hours']} limit_h={fact['max_age_hours']} "
            f"path={fact['newest_verified']}"
        )
    probe("postgres-backup", postgres_backup)

    for path in sqlite_store_paths(root, home):
        def sqlite_store(path: Path = path) -> tuple[bool, str]:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
                connection.execute("PRAGMA query_only=ON")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                schema = connection.execute("PRAGMA schema_version").fetchone()[0]
                connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            with path.open("rb") as header:
                format_version = header.read(20)[18:20].hex()
            compatible = format_version in {"0101", "0202"} and sqlite3.sqlite_version_info >= (3, 49, 1)
            return compatible, (
                f"read_only=True engine={sqlite3.sqlite_version} pin=SQLite>=3.49.1 "
                f"user_version={version} schema_version={schema} format_rw={format_version} path={path}"
            )
        probe(f"sqlite:{path.parent.name}/{path.name}", sqlite_store)
    derived = derived_root_from_environment(root)
    stores = sorted((shared_checkout_root(root) / "derived").glob("*.duckdb"))
    for path in stores:
        def duckdb_store(path: Path = path) -> tuple[bool, str]:
            import duckdb
            with duckdb.connect(str(path), read_only=True) as connection:
                metadata = connection.execute("PRAGMA metadata_info").fetchone()
            with path.open("rb") as stream:
                header = stream.read(20)
            return header[8:12] == b"DUCK", (
                f"read_only=True storage_version={int.from_bytes(header[12:20], 'little')} "
                f"metadata={metadata} path={path}"
            )
        probe(f"duckdb:{path.name}", duckdb_store)
    def duckdb_engine() -> tuple[bool, str]:
        import duckdb
        with duckdb.connect(":memory:", config={
            "autoinstall_known_extensions": False, "autoload_known_extensions": False,
        }) as connection:
            row = connection.execute("SELECT version()").fetchone()
            if row is None:
                raise ValueError("DuckDB returned no engine version")
            version = row[0].removeprefix("v")
        return version == locked["duckdb"], (
            f"reachable=True installed={version} pin={locked['duckdb']} persistent_stores={len(stores)}; "
            "storage/attach.py uses in-memory DuckDB over Parquet; retained_write=False"
        )
    probe("duckdb-engine", duckdb_engine)
    parquet_files = sorted(derived.rglob("*.parquet"))

    def parquet_stores() -> tuple[bool, str]:
        import pyarrow.parquet as pq
        if not parquet_files:
            return False, f"projections=MISSING root={derived}"
        versions: set[str] = set()
        for path in parquet_files:
            metadata = pq.read_metadata(path)
            versions.add(str(metadata.format_version))
        return True, f"read_only=True files={len(parquet_files)} format_versions={','.join(sorted(versions))} root={derived}"
    probe("parquet-stores", parquet_stores)

    def omp_config() -> tuple[bool, str]:
        with tempfile.TemporaryDirectory(prefix="evallab-omp-config-") as directory:
            enabled, disabled = (Path(directory) / name for name in ("enabled.yml", "disabled.yml"))
            enabled.write_text("retry:\n  modelFallback: true\n")
            disabled.write_text("retry:\n  modelFallback: false\n")
            observed = []
            for paths in (str(enabled), os.pathsep.join((str(enabled), str(disabled)))):
                output = command(
                    ["omp", "config", "get", "retry.modelFallback", "--json"],
                    env={**os.environ, "PI_CONFIG_FILES": paths},
                )
                observed.append(json.loads(output).get("value"))
        return observed == [True, False], (
            f"config overlay control={observed[0]} no_fallback={observed[1]} expected=True,False; no model call"
        )
    probe("omp-no-fallback", omp_config)

    def keys_presence() -> tuple[bool, str]:
        executable = shutil.which("keys")
        pin = CORE_TOOL_PINS["keys-code-sha256"]
        if executable is None:
            return False, f"installed=MISSING pin_sha256={pin} HF_TOKEN=not-probed"
        with Path(executable).open("rb") as stream:
            installed = hashlib.file_digest(stream, "sha256").hexdigest()
        version = f"installed_sha256={installed} pin_sha256={pin}"
        if installed != pin:
            return False, f"{version} HF_TOKEN=not-probed(code drift)"
        result = run(
            [executable, "has", "HF_TOKEN"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )
        return result.returncode == 0, version + " interface=keys-has-NAME/v1 HF_TOKEN=" + (
            "present" if result.returncode == 0 else "missing-or-unavailable"
        )
    probe("keys", keys_presence)

    def github() -> tuple[bool, str]:
        result = run(
            ["gh", "auth", "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, check=False,
        )
        if result.returncode:
            return False, "auth=unavailable; branch protection visibility=MISSING"
        rules = json.loads(command([
            "gh", "api", "repos/PeterMakhnatch/eval-lab/rules/branches/main",
        ]))
        required = {
            check["context"]
            for rule in rules if rule.get("type") == "required_status_checks"
            for check in rule.get("parameters", {}).get("required_status_checks", [])
        }
        if not {"quality-required", "typecheck-required"} <= required:
            protection = json.loads(command([
                "gh", "api", "repos/PeterMakhnatch/eval-lab/branches/main/protection/required_status_checks",
            ]))
            required.update(protection.get("contexts", []))
            required.update(check["context"] for check in protection.get("checks", []))
        ok = {"quality-required", "typecheck-required"} <= required
        return ok, (
            f"auth=True branch_rules_visible=True required_checks={','.join(sorted(required)) or 'MISSING'}; "
            "pending/missing exact-head CI still blocks ship"
        )
    probe("github", github)
    probe("goodfire-data", lambda: retained_source_health(
        root, home=home, collections=("goodfire-reward-hack-data",),
    ))
    from evallab.failure_library.runs.io import REPOSITORIES
    probe("hugging-face-data", lambda: retained_source_health(
        root, home=home, collections=tuple(name for name in REPOSITORIES if name != "goodfire-reward-hack-data"),
    ))

    def gold_localization() -> tuple[bool, str]:
        from evallab.gold import load_rows
        rows = [row for row in load_rows(root) if row.source.startswith("goodfire-")]
        positive = [row for row in rows if row.label_state == "executed-positive"]
        missing = sum(
            not row.trajectory_available or not row.trajectory_ref or not row.trajectory_digest or not row.step_locators
            for row in positive
        )
        return bool(rows) and missing == 0, (
            f"loader=evallab.gold.load_rows rows={len(rows)} positive={len(positive)} "
            f"positive_without_action={missing}; unresolved chronology is not execution proof"
        )
    probe("goodfire-localization", gold_localization)
    checks.extend(installed_job_health(home, now=job_now, run=run))
    return checks


class HeadlessDoctor:
    """Return readiness without reading credential values into Python.

    The audit of installed evallab LaunchAgents is read-only: it parses plists
    and checks the paths they name. It never calls launchctl, so it cannot load,
    unload, install or rewrite an agent.
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        home: Path | None = None,
        keychain_probe: BooleanProbe | None = None,
        docker_probe: BooleanProbe | None = None,
        postgres_probe: BooleanProbe | None = None,
        disk_probe: BooleanProbe | None = None,
        executor: Executor | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.home = (home or Path.home()).resolve()
        self.executor = executor or Executor.from_repo(self.repo_root)
        self._keychain_probe = keychain_probe or self._probe_keychain
        self._docker_probe = docker_probe or self._probe_docker
        self._postgres_probe = postgres_probe or self._probe_postgres
        self._disk_probe = disk_probe or self._probe_disk

    def run(self) -> HeadlessDoctorReport:
        launch_agents = audit_launch_agents(self.home)
        checks = HeadlessDoctorChecks(
            keychain_readable=self._keychain_probe(),
            codex_auth_present=(self.home / ".codex/auth.json").is_file(),
            docker_reachable=self._docker_probe(),
            postgres_reachable=self._postgres_probe(),
            disk_headroom=self._disk_probe(),
            launch_agents_intact=launch_agents.intact,
        )
        infrastructure_ok = (
            checks.docker_reachable and checks.postgres_reachable and checks.disk_headroom
        )
        return HeadlessDoctorReport(
            checked_at=datetime.now(UTC),
            # Credential availability is informational and enforced per spec
            # by Executor.tick(). Controls need no credential, so a machine
            # with healthy infrastructure and zero model credentials remains
            # capable of useful, free work. A broken schedule is reported
            # alongside it; `ready` is the operator gate (see the schema).
            healthy=infrastructure_ok,
            checks=checks,
            launch_agents=launch_agents,
        )

    def _probe_keychain(self) -> bool:
        return profiles_module.probe_claude_keychain()

    def _probe_docker(self) -> bool:
        checks = {name: ok for name, ok, _ in self.executor.local_runtime_checks()}
        return checks.get("docker-daemon", False)

    @staticmethod
    def _probe_postgres() -> bool:
        try:
            database.ping(database_url_from_environment())
        except Exception:
            return False
        return True

    def _probe_disk(self) -> bool:
        usage = shutil.disk_usage(self.repo_root)
        required = max(MIN_FREE_DISK_BYTES, int(usage.total * MIN_FREE_DISK_FRACTION))
        return usage.free >= required


def failed_health_checks(report: HeadlessDoctorReport) -> list[str]:
    return [name for name, succeeded in report.checks.model_dump().items() if not succeeded]


def blocking_health_failures(report: HeadlessDoctorReport) -> list[str]:
    """Failures that justify quarantining the cycle.

    Infrastructure failures always block. Credentials block only when *no*
    credential is available; a single missing credential merely defers the
    specs that need it (see Executor.tick).
    """
    checks = report.checks
    blocking = [
        name
        for name in ("docker_reachable", "postgres_reachable", "disk_headroom")
        if not getattr(checks, name)
    ]
    return blocking


def record_quarantine(
    queue: DirectoryQueue,
    *,
    event: str,
    report: HeadlessDoctorReport,
    actor: str,
) -> None:
    failed = blocking_health_failures(report) or failed_health_checks(report)
    queue.append_event(
        QueueEvent(
            event_id=new_ulid(),
            spec_id=f"system-{new_ulid()}",
            occurred_at=date_time_now(),
            event=event,
            actor=actor,
            reason_code="headless_doctor_failed:" + ",".join(failed),
        )
    )


def date_time_now() -> datetime:
    # Kept as one seam so tests can validate event shape without patching datetime.
    return datetime.now(UTC)


@dataclass(frozen=True)
class GuardedTickResult:
    report: HeadlessDoctorReport
    dispatched: int
    enqueued: int = 0
    quarantined: bool = False
    quarantine_reason: str | None = None


class GuardedTick:
    def __init__(
        self,
        *,
        doctor: HeadlessDoctor,
        executor: Executor,
        canary_enqueuer: Callable[[], int] | None = None,
    ) -> None:
        self.doctor = doctor
        self.executor = executor
        self.canary_enqueuer = canary_enqueuer

    def run(self, spec_ids: Sequence[str] | None = None) -> GuardedTickResult:
        report = self.doctor.run()
        if not report.healthy:
            record_quarantine(
                self.executor.queue,
                event="tick_quarantined",
                report=report,
                actor="scheduled-tick",
            )
            return GuardedTickResult(
                report=report,
                dispatched=0,
                enqueued=0,
                quarantined=True,
                quarantine_reason="unhealthy_preflight",
            )
        enqueued = 0
        if self.canary_enqueuer is not None:
            try:
                enqueued = self.canary_enqueuer()
            except (OSError, RuntimeError, ValueError) as exc:
                reason = f"canary_enqueue_failed:{type(exc).__name__}"
                self.executor.queue.append_event(
                    QueueEvent(
                        event_id=new_ulid(),
                        spec_id=f"system-{new_ulid()}",
                        occurred_at=date_time_now(),
                        event="tick_quarantined",
                        actor="scheduled-tick",
                        reason_code=reason,
                    )
                )
                return GuardedTickResult(
                    report=report,
                    dispatched=0,
                    enqueued=0,
                    quarantined=True,
                    quarantine_reason=f"{reason}: {exc}",
                )
        dispatched = self.executor.tick(spec_ids=spec_ids)
        if dispatched:
            event = "tick_dispatched"
            reason = f"dispatched:{dispatched}"
        elif self.executor.last_tick_reason is not None:
            event = "tick_deferred"
            reason = self.executor.last_tick_reason
        elif self.executor.queue.stop_path.exists():
            event = "tick_deferred"
            reason = "stop_file_present"
        else:
            approved = self.executor.queue.list_specs("approved")
            if spec_ids is not None:
                allowed = frozenset(spec_ids)
                approved = [(path, spec) for path, spec in approved if spec.spec_id in allowed]
            if approved:
                event = "tick_deferred"
                reason = "approved_specs_deferred"
            else:
                event = "tick_deferred"
                reason = "no_approved_specs"
        self.executor.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=f"system-{new_ulid()}",
                occurred_at=date_time_now(),
                event=event,
                actor="scheduled-tick",
                reason_code=reason,
            )
        )
        return GuardedTickResult(
            report=report,
            dispatched=dispatched,
            enqueued=enqueued,
            quarantined=False,
            quarantine_reason=None,
        )


