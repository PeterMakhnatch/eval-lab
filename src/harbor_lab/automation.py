from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from harbor_lab import database
from harbor_lab.digest import DigestRenderer, commit_digest
from harbor_lab.queue import DirectoryQueue, Executor, new_ulid
from harbor_lab.runner import database_url_from_environment
from harbor_lab.schemas import (
    HeadlessDoctorChecks,
    HeadlessDoctorReport,
    QueueEvent,
)

MIN_FREE_DISK_BYTES = 5 * 1024**3
MIN_FREE_DISK_FRACTION = 0.05
KEYCHAIN_SERVICE = "harbor-practice-claude-oauth"

BooleanProbe = Callable[[], bool]
LaunchctlRunner = Callable[[list[str], bool], int]
DigestCommitter = Callable[[Path], bool]
CanaryEnqueuer = Callable[[date], int]


def _quiet_command_succeeds(command: list[str]) -> bool:
    """Run a health probe without retaining or emitting either output stream."""
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


class HeadlessDoctor:
    """Return boolean-only readiness without reading credential values into Python."""

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
        checks = HeadlessDoctorChecks(
            keychain_readable=self._keychain_probe(),
            codex_auth_present=(self.home / ".codex/auth.json").is_file(),
            docker_reachable=self._docker_probe(),
            postgres_reachable=self._postgres_probe(),
            disk_headroom=self._disk_probe(),
        )
        return HeadlessDoctorReport(
            checked_at=datetime.now(UTC),
            healthy=all(checks.model_dump().values()),
            checks=checks,
        )

    def _probe_keychain(self) -> bool:
        service = os.environ.get("HARBOR_CLAUDE_KEYCHAIN_SERVICE", KEYCHAIN_SERVICE)
        account = os.environ.get("HARBOR_CLAUDE_KEYCHAIN_ACCOUNT", os.environ.get("USER", ""))
        if not account:
            return False
        return _quiet_command_succeeds(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            ]
        )

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


def record_quarantine(
    queue: DirectoryQueue,
    *,
    event: str,
    report: HeadlessDoctorReport,
    actor: str,
) -> None:
    failed = failed_health_checks(report)
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


class GuardedTick:
    def __init__(self, *, doctor: HeadlessDoctor, executor: Executor) -> None:
        self.doctor = doctor
        self.executor = executor

    def run(self) -> GuardedTickResult:
        report = self.doctor.run()
        if not report.healthy:
            record_quarantine(
                self.executor.queue,
                event="tick_quarantined",
                report=report,
                actor="scheduled-tick",
            )
            return GuardedTickResult(report=report, dispatched=0)
        return GuardedTickResult(report=report, dispatched=self.executor.tick())


@dataclass(frozen=True)
class NightlyResult:
    report: HeadlessDoctorReport
    enqueued: int
    dispatched: int
    digest_path: Path
    committed: bool


class NightlyCycle:
    def __init__(
        self,
        *,
        doctor: HeadlessDoctor,
        executor: Executor,
        renderer: DigestRenderer,
        committer: DigestCommitter = commit_digest,
        canary_enqueuer: CanaryEnqueuer | None = None,
    ) -> None:
        self.doctor = doctor
        self.executor = executor
        self.renderer = renderer
        self.committer = committer
        self.canary_enqueuer = canary_enqueuer

    def run(self, *, report_date: date | None = None) -> NightlyResult:
        target_date = report_date or date.today()
        report = self.doctor.run()
        enqueued = 0
        dispatched = 0
        if report.healthy:
            try:
                if self.canary_enqueuer is not None:
                    enqueued = self.canary_enqueuer(target_date)
            except (OSError, RuntimeError, ValueError) as exc:
                self.executor.queue.append_event(
                    QueueEvent(
                        event_id=new_ulid(),
                        spec_id=f"system-{new_ulid()}",
                        occurred_at=date_time_now(),
                        event="nightly_quarantined",
                        actor="nightly",
                        reason_code=f"canary_enqueue_failed:{type(exc).__name__}",
                        report_date=target_date.isoformat(),
                    )
                )
            else:
                dispatched = self.executor.tick()
        else:
            record_quarantine(
                self.executor.queue,
                event="nightly_quarantined",
                report=report,
                actor="nightly",
            )
        digest_path = self.renderer.write(
            report_date=target_date,
            health_report=report,
            dispatched=dispatched,
        )
        return NightlyResult(
            report=report,
            enqueued=enqueued,
            dispatched=dispatched,
            digest_path=digest_path,
            committed=self.committer(digest_path),
        )


def _launchctl(command: list[str], check: bool) -> int:
    completed = subprocess.run(command, check=False)
    if check and completed.returncode != 0:
        raise RuntimeError(f"launchctl exited {completed.returncode}")
    return completed.returncode


class ScheduleInstaller:
    TICK_LABEL = "com.petermakhnatch.harbor-lab.tick"
    NIGHTLY_LABEL = "com.petermakhnatch.harbor-lab.nightly"

    def __init__(
        self,
        repo_root: Path,
        *,
        home: Path | None = None,
        uid: int | None = None,
        launchctl: LaunchctlRunner = _launchctl,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.home = (home or Path.home()).resolve()
        self.uid = os.getuid() if uid is None else uid
        self._launchctl = launchctl

    @property
    def launch_agents_dir(self) -> Path:
        return self.home / "Library/LaunchAgents"

    def definitions(self) -> dict[str, dict[str, Any]]:
        logs = self.home / "Library/Logs/harbor-lab"
        environment = {
            "PATH": ":".join(
                [
                    str(self.home / ".local/bin"),
                    "/opt/homebrew/bin",
                    "/usr/local/bin",
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                ]
            )
        }
        return {
            self.TICK_LABEL: {
                "Label": self.TICK_LABEL,
                "ProgramArguments": [
                    "/bin/zsh",
                    "-lc",
                    self._shell_command("tick"),
                ],
                "StartInterval": 30 * 60,
                "RunAtLoad": True,
                "ProcessType": "Background",
                "EnvironmentVariables": environment,
                "StandardOutPath": str(logs / "tick.log"),
                "StandardErrorPath": str(logs / "tick.error.log"),
            },
            self.NIGHTLY_LABEL: {
                "Label": self.NIGHTLY_LABEL,
                "ProgramArguments": [
                    "/bin/zsh",
                    "-lc",
                    self._shell_command("nightly"),
                ],
                "StartCalendarInterval": {"Hour": 2, "Minute": 30},
                "ProcessType": "Background",
                "EnvironmentVariables": environment,
                "StandardOutPath": str(logs / "nightly.log"),
                "StandardErrorPath": str(logs / "nightly.error.log"),
            },
        }

    def install(self) -> list[Path]:
        self.launch_agents_dir.mkdir(parents=True, exist_ok=True)
        (self.home / "Library/Logs/harbor-lab").mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        domain = f"gui/{self.uid}"
        for label, definition in self.definitions().items():
            path = self.launch_agents_dir / f"{label}.plist"
            temporary = path.with_name(f".{path.name}.{new_ulid()}.tmp")
            temporary.write_bytes(plistlib.dumps(definition, fmt=plistlib.FMT_XML, sort_keys=False))
            temporary.replace(path)
            self._launchctl(["launchctl", "bootout", f"{domain}/{label}"], False)
            self._launchctl(["launchctl", "bootstrap", domain, str(path)], True)
            paths.append(path)
        return paths

    def _shell_command(self, command: str) -> str:
        return f"cd {shlex.quote(str(self.repo_root))} && uv run harbor-lab {command}"
