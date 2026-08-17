from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from evallab import credentials as credentials_module
from evallab import database
from evallab.atif import IngestProjectionResult
from evallab.digest import DigestRenderer, commit_digest
from evallab.lessons import generate_lessons_file
from evallab.parquet_compaction import compact
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
from evallab.status_generator import update_status_file

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
StatusUpdater = Callable[[date], Path]
Compactor = Callable[[date], object]
LessonsGenerator = Callable[[date], Path]

OnFailPolicy = Literal["abort", "continue"]
StepStatus = Literal["ran", "skipped", "failed"]


@dataclass(frozen=True)
class StepOutcome:
    name: str
    status: StepStatus
    duration_s: float
    reason: str | None = None
    error: str | None = None


@dataclass
class NightlyContext:
    target_date: date
    doctor: HeadlessDoctor
    executor: Executor
    renderer: DigestRenderer
    committer: DigestCommitter
    canary_enqueuer: CanaryEnqueuer | None = None
    researcher_pass: ResearcherPass | None = None
    digest_enricher: DigestEnricher | None = None
    completed_job_ingester: CompletedJobIngester | None = None
    database_backup: DatabaseBackup | None = None
    analysis_stager: Callable[[], object] | None = None
    status_updater: StatusUpdater | None = None
    compactor: Callable[[date], object] | None = None
    lessons_generator: Callable[[date], Path] | None = None

    report: HeadlessDoctorReport | None = None
    quarantined: bool = False
    aborted: bool = False
    enqueued: int = 0
    dispatched: int = 0
    researcher_invocations: int = 0
    backup_path: Path | None = None
    status_path: Path | None = None
    digest_path: Path | None = None
    committed: bool = False
    lessons_path: Path | None = None
    compaction_result: Any = None
    step_outcomes: list[StepOutcome] = field(default_factory=list)


StepCallable = Callable[[NightlyContext], Any]


@dataclass(frozen=True)
class NightlyStep:
    name: str
    fn: StepCallable
    timeout: float = 60.0
    on_fail: OnFailPolicy = "abort"
    description: str = ""
    idempotent: bool = True


def _step_doctor(context: NightlyContext) -> None:
    report = context.doctor.run()
    context.report = report
    if not report.healthy:
        context.quarantined = True
        context.aborted = True
        record_quarantine(
            context.executor.queue,
            event="nightly_quarantined",
            report=report,
            actor="nightly",
        )


def _step_catalog_ingest(context: NightlyContext) -> None:
    if context.completed_job_ingester is None:
        return
    try:
        ingest_result = context.completed_job_ingester()
    except Exception as exc:
        context.quarantined = True
        context.aborted = True
        context.executor.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=f"system-{new_ulid()}",
                occurred_at=date_time_now(),
                event="nightly_quarantined",
                actor="nightly",
                reason_code=f"catalog_ingest_failed:{type(exc).__name__}",
                report_date=context.target_date.isoformat(),
            )
        )
        raise
    else:
        record_projection_failures(
            context.executor.queue,
            ingest_result,
            actor="nightly",
            spec_id=f"system-{new_ulid()}",
        )


def _step_analysis_staging(context: NightlyContext) -> None:
    if context.analysis_stager is None:
        return
    try:
        stage_result = context.analysis_stager()
    except Exception as exc:
        context.executor.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=f"system-{new_ulid()}",
                occurred_at=date_time_now(),
                event="analysis_stage_failed",
                actor="nightly",
                reason_code=f"analysis_stage_failed:{type(exc).__name__}",
                report_date=context.target_date.isoformat(),
            )
        )
        raise
    else:
        issue_reason = _analysis_stage_issue_reason(stage_result)
        if issue_reason is not None:
            context.executor.queue.append_event(
                QueueEvent(
                    event_id=new_ulid(),
                    spec_id=f"system-{new_ulid()}",
                    occurred_at=date_time_now(),
                    event="analysis_stage_reported_issues",
                    actor="nightly",
                    reason_code=issue_reason,
                    report_date=context.target_date.isoformat(),
                )
            )


def _step_parquet_compaction(context: NightlyContext) -> None:
    if context.compactor is None:
        return
    try:
        compaction_result = context.compactor(context.target_date)
        context.compaction_result = compaction_result
    except Exception as exc:
        context.executor.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=f"system-{new_ulid()}",
                occurred_at=date_time_now(),
                event="parquet_compaction_failed",
                actor="nightly",
                reason_code=f"parquet_compaction_failed:{type(exc).__name__}",
                report_date=context.target_date.isoformat(),
            )
        )
        raise


def _step_postgres_backup(context: NightlyContext) -> None:
    if context.database_backup is None:
        return
    try:
        context.backup_path = context.database_backup(context.target_date)
    except Exception as exc:
        context.quarantined = True
        context.aborted = True
        context.executor.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=f"system-{new_ulid()}",
                occurred_at=date_time_now(),
                event="postgres_backup_failed",
                actor="nightly",
                reason_code=f"pg_dump_failed:{type(exc).__name__}",
                report_date=context.target_date.isoformat(),
            )
        )
        raise
    else:
        context.executor.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=f"system-{new_ulid()}",
                occurred_at=date_time_now(),
                event="postgres_backup_completed",
                actor="nightly",
                reason_code="nightly_pg_dump",
                report_date=context.target_date.isoformat(),
            )
        )


def _step_canary_enqueue(context: NightlyContext) -> None:
    if context.canary_enqueuer is None:
        return
    try:
        context.enqueued = context.canary_enqueuer(context.target_date)
    except (OSError, RuntimeError, ValueError) as exc:
        context.quarantined = True
        context.aborted = True
        context.executor.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=f"system-{new_ulid()}",
                occurred_at=date_time_now(),
                event="nightly_quarantined",
                actor="nightly",
                reason_code=f"canary_enqueue_failed:{type(exc).__name__}",
                report_date=context.target_date.isoformat(),
            )
        )
        raise


def _step_dispatch(context: NightlyContext) -> None:
    context.dispatched = context.executor.tick()


def _step_researcher_pass(context: NightlyContext) -> None:
    if context.researcher_pass is None:
        return
    if context.executor.queue.stop_path.exists():
        record_researcher_deferral(
            context.executor.queue,
            report_date=context.target_date,
            actor="nightly",
            reason="stop_file_present",
        )
        return
    if context.executor.last_tick_reason is not None:
        record_researcher_deferral(
            context.executor.queue,
            report_date=context.target_date,
            actor="nightly",
            reason=context.executor.last_tick_reason,
        )
        return
    if context.report is not None and not context.report.checks.codex_auth_present:
        record_researcher_deferral(
            context.executor.queue,
            report_date=context.target_date,
            actor="nightly",
            reason="missing_credential:codex",
        )
        return
    try:
        context.researcher_invocations = context.researcher_pass(context.target_date)
    except (OSError, RuntimeError, ValueError) as exc:
        context.executor.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=f"system-{new_ulid()}",
                occurred_at=date_time_now(),
                event="researcher_pass_failed",
                actor="nightly",
                reason_code=f"researcher_failed:{type(exc).__name__}",
                report_date=context.target_date.isoformat(),
            )
        )
        raise


def _step_lessons(context: NightlyContext) -> None:
    if context.lessons_generator is None:
        return
    try:
        context.lessons_path = context.lessons_generator(context.target_date)
    except Exception as exc:
        context.executor.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=f"system-{new_ulid()}",
                occurred_at=date_time_now(),
                event="lessons_generation_failed",
                actor="nightly",
                reason_code=f"lessons_generation_failed:{type(exc).__name__}",
                report_date=context.target_date.isoformat(),
            )
        )
        raise


def _step_digest(context: NightlyContext) -> None:
    digest_path = context.renderer.write(
        report_date=context.target_date,
        health_report=context.report,
        dispatched=context.dispatched,
    )
    if context.digest_enricher is not None:
        try:
            context.digest_enricher(digest_path, context.target_date)
        except (OSError, RuntimeError, ValueError) as exc:
            context.quarantined = True
            context.executor.queue.append_event(
                QueueEvent(
                    event_id=new_ulid(),
                    spec_id=f"system-{new_ulid()}",
                    occurred_at=date_time_now(),
                    event="digest_enrichment_failed",
                    actor="nightly",
                    reason_code=f"fleet_digest_failed:{type(exc).__name__}",
                    report_date=context.target_date.isoformat(),
                )
            )
            digest_path = context.renderer.write(
                report_date=context.target_date,
                health_report=context.report,
                dispatched=context.dispatched,
            )
    context.digest_path = digest_path
    context.committed = context.committer(digest_path)


def _step_status_update(context: NightlyContext) -> None:
    if context.status_updater is None:
        return
    try:
        context.status_path = context.status_updater(context.target_date)
    except Exception as exc:
        context.executor.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=f"system-{new_ulid()}",
                occurred_at=date_time_now(),
                event="status_generation_failed",
                actor="nightly",
                reason_code=f"status_generation_failed:{type(exc).__name__}",
                report_date=context.target_date.isoformat(),
            )
        )
        raise


DEFAULT_NIGHTLY_STEPS: tuple[NightlyStep, ...] = (
    NightlyStep(
        name="doctor",
        fn=_step_doctor,
        timeout=60.0,
        on_fail="abort",
        description="Run health probes and record quarantine if unhealthy",
        idempotent=True,
    ),
    NightlyStep(
        name="catalog_ingest",
        fn=_step_catalog_ingest,
        timeout=300.0,
        on_fail="abort",
        description="Ingest completed jobs into catalog and parquet projections",
        idempotent=True,
    ),
    NightlyStep(
        name="analysis_staging",
        fn=_step_analysis_staging,
        timeout=60.0,
        on_fail="continue",
        description="Stage analysis requests for completed jobs",
        idempotent=True,
    ),
    NightlyStep(
        name="parquet_compaction",
        fn=_step_parquet_compaction,
        timeout=300.0,
        on_fail="continue",
        description="Compact closed-day granular parquet partitions and prune old files",
        idempotent=True,
    ),
    NightlyStep(
        name="postgres_backup",
        fn=_step_postgres_backup,
        timeout=120.0,
        on_fail="abort",
        description="Create PostgreSQL dump backup before dispatch",
        idempotent=True,
    ),
    NightlyStep(
        name="canary_enqueue",
        fn=_step_canary_enqueue,
        timeout=60.0,
        on_fail="abort",
        description="Enqueue nightly canary evaluation specs",
        idempotent=False,
    ),
    NightlyStep(
        name="dispatch",
        fn=_step_dispatch,
        timeout=600.0,
        on_fail="abort",
        description="Execute approved specs in queue via executor tick",
        idempotent=False,
    ),
    NightlyStep(
        name="researcher_pass",
        fn=_step_researcher_pass,
        timeout=300.0,
        on_fail="continue",
        description="Run unattended researcher autopilot iterations",
        idempotent=False,
    ),
    NightlyStep(
        name="lessons",
        fn=_step_lessons,
        timeout=120.0,
        on_fail="continue",
        description="Materialize statistical lesson aggregation views and research/lessons.md",
        idempotent=True,
    ),
    NightlyStep(
        name="digest",
        fn=_step_digest,
        timeout=60.0,
        on_fail="abort",
        description="Render, enrich, and commit daily markdown digest",
        idempotent=True,
    ),
    NightlyStep(
        name="status_update",
        fn=_step_status_update,
        timeout=30.0,
        on_fail="continue",
        description="Generate and update STATUS.md operator surface",
        idempotent=True,
    ),
)

SURFACE_STEP_NAMES: frozenset[str] = frozenset({"digest", "status_update"})


def _analysis_stage_issue_reason(report: object, *, limit: int = 512) -> str | None:
    """Return one bounded summary for non-throwing stage failures."""

    def counts(name: str) -> dict[str, int]:
        value = getattr(report, name, {})
        if not isinstance(value, Mapping):
            return {}
        return {
            str(reason): int(count)
            for reason, count in value.items()
            if isinstance(count, int) and count > 0
        }

    quarantined = counts("quarantined")
    errors = counts("errors")
    quarantined_total = sum(quarantined.values())
    error_total = sum(errors.values())
    if quarantined_total == 0 and error_total == 0:
        return None
    details = [
        *(f"{reason}={count}" for reason, count in sorted(quarantined.items())),
        *(f"error:{reason}={count}" for reason, count in sorted(errors.items())),
    ]
    reason = (
        "analysis_stage_reported_issues:"
        f"quarantined={quarantined_total};errors={error_total};"
        f"reasons={','.join(details)}"
    )
    return reason[:limit]


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
        return HeadlessDoctorReport(
            checked_at=datetime.now(UTC),
            # Credential availability is informational and enforced per spec
            # by Executor.tick(). Controls need no credential, so a machine
            # with healthy infrastructure and zero model credentials remains
            # capable of useful, free work.
            healthy=infrastructure_ok,
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
        elif self.executor.last_tick_reason is not None:
            event = "tick_deferred"
            reason = self.executor.last_tick_reason
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
    status_path: Path | None = None
    lessons_path: Path | None = None
    steps: tuple[StepOutcome, ...] = ()

    @property
    def step_outcomes(self) -> dict[str, StepOutcome]:
        return {outcome.name: outcome for outcome in self.steps}

    def step_by_name(self, name: str) -> StepOutcome | None:
        return self.step_outcomes.get(name)

    def format_step_report(self) -> str:
        lines = ["Nightly Step Report:"]
        for step in self.steps:
            duration_str = f"{step.duration_s:.3f}s"
            if step.status == "ran":
                lines.append(f"  - {step.name}: ran ({duration_str})")
            elif step.status == "skipped":
                reason = f" [{step.reason}]" if step.reason else ""
                lines.append(f"  - {step.name}: skipped{reason} ({duration_str})")
            else:
                err = f" [{step.error}]" if step.error else ""
                lines.append(f"  - {step.name}: failed{err} ({duration_str})")
        return "\n".join(lines)


class NightlyCycle:
    DEFAULT_STEPS: tuple[NightlyStep, ...] = DEFAULT_NIGHTLY_STEPS

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
        analysis_stager: Callable[[], object] | None = None,
        status_updater: StatusUpdater | None = None,
        compactor: Callable[[date], object] | None = None,
        lessons_generator: Callable[[date], Path] | None = None,
        steps: Sequence[NightlyStep] | None = None,
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
        self.analysis_stager = analysis_stager
        self.status_updater = (
            status_updater
            if status_updater is not None
            else (lambda day: update_status_file(self.renderer.repo_root, target_date=day))
        )
        self.compactor = (
            compactor
            if compactor is not None
            else (
                lambda day: compact(
                    derived_root=derived_root_from_environment(self.renderer.repo_root),
                    runs_dir=self.renderer.repo_root / "runs",
                    clock_today=day,
                )
            )
        )
        self.lessons_generator = (
            lessons_generator
            if lessons_generator is not None
            else (lambda day: generate_lessons_file(root=self.renderer.repo_root))
        )
        self.steps = tuple(steps) if steps is not None else self.DEFAULT_STEPS

    @property
    def step_names(self) -> tuple[str, ...]:
        return tuple(step.name for step in self.steps)

    def run(self, *, report_date: date | None = None) -> NightlyResult:
        target_date = report_date or date.today()
        context = NightlyContext(
            target_date=target_date,
            doctor=self.doctor,
            executor=self.executor,
            renderer=self.renderer,
            committer=self.committer,
            canary_enqueuer=self.canary_enqueuer,
            researcher_pass=self.researcher_pass,
            digest_enricher=self.digest_enricher,
            completed_job_ingester=self.completed_job_ingester,
            database_backup=self.database_backup,
            analysis_stager=self.analysis_stager,
            status_updater=self.status_updater,
            compactor=self.compactor,
            lessons_generator=self.lessons_generator,
        )

        for step in self.steps:
            if context.aborted and step.name not in SURFACE_STEP_NAMES:
                reason = (
                    "quarantined_by_prior_step"
                    if context.quarantined
                    else "aborted_by_prior_step"
                )
                context.step_outcomes.append(
                    StepOutcome(
                        name=step.name,
                        status="skipped",
                        duration_s=0.0,
                        reason=reason,
                    )
                )
                continue

            skip_reason: str | None = None
            if step.name == "catalog_ingest" and context.completed_job_ingester is None:
                skip_reason = "no_ingester_configured"
            elif step.name == "analysis_staging" and context.analysis_stager is None:
                skip_reason = "no_stager_configured"
            elif step.name == "parquet_compaction" and context.compactor is None:
                skip_reason = "no_compactor_configured"
            elif step.name == "postgres_backup" and context.database_backup is None:
                skip_reason = "no_backup_configured"
            elif step.name == "canary_enqueue" and context.canary_enqueuer is None:
                skip_reason = "no_canary_enqueuer_configured"
            elif step.name == "researcher_pass" and context.researcher_pass is None:
                skip_reason = "no_researcher_configured"
            elif step.name == "lessons" and context.lessons_generator is None:
                skip_reason = "no_lessons_generator_configured"
            elif step.name == "status_update" and context.status_updater is None:
                skip_reason = "no_status_updater_configured"

            if skip_reason is not None:
                context.step_outcomes.append(
                    StepOutcome(
                        name=step.name,
                        status="skipped",
                        duration_s=0.0,
                        reason=skip_reason,
                    )
                )
                continue

            start_time = time.perf_counter()
            try:
                step.fn(context)
                elapsed = time.perf_counter() - start_time
                context.step_outcomes.append(
                    StepOutcome(
                        name=step.name,
                        status="ran",
                        duration_s=elapsed,
                    )
                )
            except Exception as exc:
                elapsed = time.perf_counter() - start_time
                context.step_outcomes.append(
                    StepOutcome(
                        name=step.name,
                        status="failed",
                        duration_s=elapsed,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                if step.on_fail == "abort":
                    context.quarantined = True
                    context.aborted = True

        digest_path = context.digest_path or self.renderer.write(
            report_date=target_date,
            health_report=context.report or self.doctor.run(),
            dispatched=context.dispatched,
        )

        return NightlyResult(
            report=context.report or self.doctor.run(),
            quarantined=context.quarantined,
            enqueued=context.enqueued,
            dispatched=context.dispatched,
            digest_path=digest_path,
            committed=context.committed,
            researcher_invocations=context.researcher_invocations,
            backup_path=context.backup_path,
            status_path=context.status_path,
            lessons_path=context.lessons_path,
            steps=tuple(context.step_outcomes),
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
