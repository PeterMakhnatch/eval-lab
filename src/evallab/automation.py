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

from evallab import credentials as credentials_module
from evallab import database
from evallab.atif import IngestProjectionResult
from evallab.digest import DigestRenderer, commit_digest
from evallab.paths import DERIVED_ROOT_ENV, derived_root_from_environment
from evallab.queue import (
    DirectoryQueue,
    Executor,
    new_ulid,
    record_projection_failures,
)
from evallab.runner import database_url_from_environment, subscription_environment
from evallab.schemas import (
    HeadlessDoctorChecks,
    HeadlessDoctorReport,
    QueueEvent,
)

MIN_FREE_DISK_BYTES = 5 * 1024**3
MIN_FREE_DISK_FRACTION = 0.05
KEYCHAIN_SERVICE = credentials_module.KEYCHAIN_SERVICE

BooleanProbe = Callable[[], bool]
LaunchctlRunner = Callable[[list[str], bool], int]
DigestCommitter = Callable[[Path], bool]
CanaryEnqueuer = Callable[[date], int]
ResearcherPass = Callable[[date], int]
DigestEnricher = Callable[[Path, date], None]
CompletedJobIngester = Callable[[], IngestProjectionResult]
DatabaseBackup = Callable[[date], Path]


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
            env=subscription_environment(),
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
        infrastructure_ok = (
            checks.docker_reachable and checks.postgres_reachable and checks.disk_headroom
        )
        credentials_ok = checks.keychain_readable or checks.codex_auth_present
        return HeadlessDoctorReport(
            checked_at=datetime.now(UTC),
            healthy=infrastructure_ok and credentials_ok,
            checks=checks,
        )

    def _probe_keychain(self) -> bool:
        return credentials_module.probe_claude_keychain()

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
    if not (checks.keychain_readable or checks.codex_auth_present):
        blocking.append("no_credentials")
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


def record_researcher_deferral(
    queue: DirectoryQueue,
    *,
    report_date: date,
    actor: str,
    reason: str,
) -> None:
    queue.append_event(
        QueueEvent(
            event_id=new_ulid(),
            spec_id=f"system-{new_ulid()}",
            occurred_at=date_time_now(),
            event="researcher_pass_deferred",
            actor=actor,
            reason_code=reason,
            report_date=report_date.isoformat(),
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
        dispatched = self.executor.tick()
        if dispatched:
            event = "tick_dispatched"
            reason = f"dispatched:{dispatched}"
        elif self.executor.queue.stop_path.exists():
            event = "tick_deferred"
            reason = "stop_file_present"
        elif self.executor.queue.list_specs("approved"):
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
        return GuardedTickResult(report=report, dispatched=dispatched)


@dataclass(frozen=True)
class NightlyResult:
    report: HeadlessDoctorReport
    quarantined: bool
    enqueued: int
    dispatched: int
    digest_path: Path
    committed: bool
    researcher_invocations: int = 0
    backup_path: Path | None = None


class NightlyCycle:
    def __init__(
        self,
        *,
        doctor: HeadlessDoctor,
        executor: Executor,
        renderer: DigestRenderer,
        committer: DigestCommitter = commit_digest,
        canary_enqueuer: CanaryEnqueuer | None = None,
        researcher_pass: ResearcherPass | None = None,
        digest_enricher: DigestEnricher | None = None,
        completed_job_ingester: CompletedJobIngester | None = None,
        database_backup: DatabaseBackup | None = None,
    ) -> None:
        self.doctor = doctor
        self.executor = executor
        self.renderer = renderer
        self.committer = committer
        self.canary_enqueuer = canary_enqueuer
        self.researcher_pass = researcher_pass
        self.digest_enricher = digest_enricher
        self.completed_job_ingester = completed_job_ingester
        self.database_backup = database_backup

    def run(self, *, report_date: date | None = None) -> NightlyResult:
        target_date = report_date or date.today()
        report = self.doctor.run()
        quarantined = not report.healthy
        enqueued = 0
        dispatched = 0
        researcher_invocations = 0
        backup_path: Path | None = None
        if report.healthy and self.completed_job_ingester is not None:
            try:
                ingest_result = self.completed_job_ingester()
            except Exception as exc:
                quarantined = True
                self.executor.queue.append_event(
                    QueueEvent(
                        event_id=new_ulid(),
                        spec_id=f"system-{new_ulid()}",
                        occurred_at=date_time_now(),
                        event="nightly_quarantined",
                        actor="nightly",
                        reason_code=f"catalog_ingest_failed:{type(exc).__name__}",
                        report_date=target_date.isoformat(),
                    )
                )
            else:
                record_projection_failures(
                    self.executor.queue,
                    ingest_result,
                    actor="nightly",
                    spec_id=f"system-{new_ulid()}",
                )
        if report.healthy and not quarantined and self.database_backup is not None:
            try:
                backup_path = self.database_backup(target_date)
            except Exception as exc:
                quarantined = True
                self.executor.queue.append_event(
                    QueueEvent(
                        event_id=new_ulid(),
                        spec_id=f"system-{new_ulid()}",
                        occurred_at=date_time_now(),
                        event="postgres_backup_failed",
                        actor="nightly",
                        reason_code=f"pg_dump_failed:{type(exc).__name__}",
                        report_date=target_date.isoformat(),
                    )
                )
            else:
                self.executor.queue.append_event(
                    QueueEvent(
                        event_id=new_ulid(),
                        spec_id=f"system-{new_ulid()}",
                        occurred_at=date_time_now(),
                        event="postgres_backup_completed",
                        actor="nightly",
                        reason_code="nightly_pg_dump",
                        report_date=target_date.isoformat(),
                    )
                )
        if report.healthy and not quarantined:
            try:
                if self.canary_enqueuer is not None:
                    enqueued = self.canary_enqueuer(target_date)
            except (OSError, RuntimeError, ValueError) as exc:
                quarantined = True
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
                if self.researcher_pass is not None:
                    if self.executor.queue.stop_path.exists():
                        record_researcher_deferral(
                            self.executor.queue,
                            report_date=target_date,
                            actor="nightly",
                            reason="stop_file_present",
                        )
                    elif not report.checks.codex_auth_present:
                        record_researcher_deferral(
                            self.executor.queue,
                            report_date=target_date,
                            actor="nightly",
                            reason="missing_credential:codex",
                        )
                    else:
                        try:
                            researcher_invocations = self.researcher_pass(target_date)
                        except (OSError, RuntimeError, ValueError) as exc:
                            self.executor.queue.append_event(
                                QueueEvent(
                                    event_id=new_ulid(),
                                    spec_id=f"system-{new_ulid()}",
                                    occurred_at=date_time_now(),
                                    event="researcher_pass_failed",
                                    actor="nightly",
                                    reason_code=f"researcher_failed:{type(exc).__name__}",
                                    report_date=target_date.isoformat(),
                                )
                            )
        elif not report.healthy:
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
        if self.digest_enricher is not None:
            try:
                self.digest_enricher(digest_path, target_date)
            except (OSError, RuntimeError, ValueError) as exc:
                quarantined = True
                self.executor.queue.append_event(
                    QueueEvent(
                        event_id=new_ulid(),
                        spec_id=f"system-{new_ulid()}",
                        occurred_at=date_time_now(),
                        event="digest_enrichment_failed",
                        actor="nightly",
                        reason_code=f"fleet_digest_failed:{type(exc).__name__}",
                        report_date=target_date.isoformat(),
                    )
                )
        return NightlyResult(
            report=report,
            quarantined=quarantined,
            enqueued=enqueued,
            dispatched=dispatched,
            digest_path=digest_path,
            committed=self.committer(digest_path),
            researcher_invocations=researcher_invocations,
            backup_path=backup_path,
        )


def _launchctl(command: list[str], check: bool) -> int:
    completed = subprocess.run(
        command,
        check=False,
        env=subscription_environment(),
    )
    if check and completed.returncode != 0:
        raise RuntimeError(f"launchctl exited {completed.returncode}")
    return completed.returncode


class ScheduleInstaller:
    TICK_LABEL = "com.petermakhnatch.evallab.tick"
    NIGHTLY_LABEL = "com.petermakhnatch.evallab.nightly"

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
        logs = self.home / "Library/Logs/evallab"
        environment = {
            DERIVED_ROOT_ENV: str(derived_root_from_environment(self.repo_root)),
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
        (self.home / "Library/Logs/evallab").mkdir(parents=True, exist_ok=True)
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
        return f"cd {shlex.quote(str(self.repo_root))} && uv run evallab {command}"
