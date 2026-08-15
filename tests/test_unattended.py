from __future__ import annotations

import plistlib
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import evallab.digest as digest_module
from evallab.automation import GuardedTick, HeadlessDoctor, NightlyCycle, ScheduleInstaller
from evallab.digest import DigestRenderer, DigestTrial, commit_digest
from evallab.paths import DERIVED_ROOT_ENV
from evallab.queue import DirectoryQueue, Executor, load_events
from evallab.researchers import (
    CallLedger,
    EvidenceBundle,
    ResearcherDeferred,
    ResearcherLoop,
    TrialEvidence,
    append_fleet_section,
)
from evallab.schemas import (
    AutoRunRule,
    ExperimentSpec,
    HeadlessDoctorChecks,
    HeadlessDoctorReport,
    QueueEvent,
    StandingApprovalsPolicy,
)


def policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
    )


class RuntimeChecks:
    def __init__(self, docker_ok: bool = True) -> None:
        self.docker_ok = docker_ok

    def local_runtime_checks(self):
        return [
            ("harbor", True, "must-not-escape"),
            ("docker", True, "must-not-escape"),
            ("uv", True, "must-not-escape"),
            ("docker-daemon", self.docker_ok, "must-not-escape"),
        ]


class StaticDoctor:
    def __init__(self, report: HeadlessDoctorReport) -> None:
        self.report = report

    def run(self) -> HeadlessDoctorReport:
        return self.report


def health_report(
    *, keychain_readable: bool = True, codex_auth_present: bool = True
) -> HeadlessDoctorReport:
    checks = HeadlessDoctorChecks(
        keychain_readable=keychain_readable,
        codex_auth_present=codex_auth_present,
        docker_reachable=True,
        postgres_reachable=True,
        disk_headroom=True,
    )
    healthy = checks.docker_reachable and checks.postgres_reachable and checks.disk_headroom
    return HeadlessDoctorReport(
        checked_at=datetime.now(UTC),
        healthy=healthy,
        checks=checks,
    )


def test_headless_doctor_emits_boolean_contract_without_secret_values(tmp_path: Path) -> None:
    home = tmp_path / "home"
    auth = home / ".codex/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("never-read-secret")

    report = HeadlessDoctor(
        tmp_path,
        home=home,
        executor=RuntimeChecks(),  # type: ignore[arg-type]
        keychain_probe=lambda: True,
        postgres_probe=lambda: True,
        disk_probe=lambda: True,
    ).run()
    rendered = report.model_dump_json()

    assert report.healthy is True
    assert report.checks.model_dump() == {
        "keychain_readable": True,
        "codex_auth_present": True,
        "docker_reachable": True,
        "postgres_reachable": True,
        "disk_headroom": True,
    }
    assert "never-read-secret" not in rendered
    assert "must-not-escape" not in rendered


def test_schedule_install_writes_and_loads_two_launchagents(tmp_path: Path) -> None:
    calls: list[tuple[list[str], bool]] = []
    installer = ScheduleInstaller(
        tmp_path,
        home=tmp_path,
        uid=501,
        launchctl=lambda command, check: calls.append((command, check)) or 0,
    )

    paths = installer.install()

    assert {path.name for path in paths} == {
        f"{ScheduleInstaller.TICK_LABEL}.plist",
        f"{ScheduleInstaller.NIGHTLY_LABEL}.plist",
    }
    assert [call[0][1] for call in calls] == ["bootout", "bootstrap", "bootout", "bootstrap"]
    assert [call[1] for call in calls] == [False, True, False, True]
    tick = plistlib.loads(
        (installer.launch_agents_dir / f"{ScheduleInstaller.TICK_LABEL}.plist").read_bytes()
    )
    nightly = plistlib.loads(
        (installer.launch_agents_dir / f"{ScheduleInstaller.NIGHTLY_LABEL}.plist").read_bytes()
    )
    assert tick["StartInterval"] == 1800
    assert nightly["StartCalendarInterval"] == {"Hour": 2, "Minute": 30}
    assert tick["ProgramArguments"][:2] == ["/bin/zsh", "-lc"]
    assert tick["ProgramArguments"][2].endswith("uv run evallab tick")
    assert nightly["ProgramArguments"][2].endswith("uv run evallab nightly")
    assert tick["Label"] == "com.petermakhnatch.evallab.tick"
    assert nightly["Label"] == "com.petermakhnatch.evallab.nightly"
    assert tick["StandardOutPath"].endswith("Library/Logs/evallab/tick.log")
    assert tick["EnvironmentVariables"]["PATH"].startswith(str(tmp_path / ".local/bin"))
    assert tick["EnvironmentVariables"][DERIVED_ROOT_ENV] == str(
        tmp_path / "derived/parquet"
    )
    assert nightly["EnvironmentVariables"] == tick["EnvironmentVariables"]


def test_healthy_nightly_dispatches_control_and_renders_catalog_job(tmp_path: Path) -> None:
    ingested: list[Path] = []

    def runner(request):
        job = request.jobs_dir / request.name
        job.mkdir(parents=True)
        return job

    queue = DirectoryQueue(tmp_path / "queue")
    service = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=policy(),
        runner=runner,
        ingester=ingested.append,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
    )
    service.submit(
        ExperimentSpec(
            name="nightly-oracle-control",
            hypothesis="control remains healthy",
            task="library/tasks/event-summary",
            agent="oracle",
            submitted_by="scheduler-test",
        )
    )
    report_date = date(2026, 8, 13)

    def load_trials(day: date) -> list[DigestTrial]:
        if day != report_date or not ingested:
            return []
        return [
            DigestTrial(
                job_name="nightly-oracle-control",
                task_name="local/event-summary",
                agent_name="oracle",
                model_name=None,
                reward=1.0,
                exception_type=None,
                cost_usd=0,
                finished_at="2026-08-13T02:31:00Z",
            )
        ]

    committed: list[Path] = []
    backups: list[date] = []
    backup_path = tmp_path / "backups/postgres/test.dump"
    result = NightlyCycle(
        doctor=StaticDoctor(health_report()),  # type: ignore[arg-type]
        executor=service,
        renderer=DigestRenderer(
            repo_root=tmp_path,
            queue=queue,
            policy=policy(),
            trial_loader=load_trials,
        ),
        committer=lambda path: committed.append(path) or True,
        database_backup=lambda day: backups.append(day) or backup_path,
    ).run(report_date=report_date)

    assert result.dispatched == 1
    assert len(ingested) == 1
    assert committed == [result.digest_path]
    assert backups == [report_date]
    assert result.backup_path == backup_path
    assert any(
        event.event == "postgres_backup_completed"
        and event.reason_code == "nightly_pg_dump"
        for event in load_events(queue.events_path)
    )
    content = result.digest_path.read_text()
    assert "nightly-oracle-control" in content
    assert "local-controls" in content
    assert "Quarantined: no" in content


def test_nightly_researcher_defers_while_running_job_is_unresolved(
    tmp_path: Path,
) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    researcher_calls: list[date] = []
    service = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=policy(),
        runner=lambda request: (_ for _ in ()).throw(
            AssertionError(f"new work dispatched: {request.name}")
        ),
        ingester=lambda _path: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
    )
    approved, _ = service.submit(
        ExperimentSpec(
            name="partial-before-nightly",
            hypothesis="detached Harbor work blocks the researcher",
            task="library/tasks/event-summary",
            agent="oracle",
            submitted_by="scheduler-test",
        )
    )
    queued = queue.load(approved)
    queue.transition(
        approved,
        "running",
        actor="executor",
        event="dispatch_started",
    )
    job_dir = tmp_path / queued.jobs_dir / queued.name
    job_dir.mkdir(parents=True)
    (job_dir / "result.json").write_text(
        '{"n_total_trials": 1, "stats": {}, "finished_at": null}\n'
    )
    report_date = date(2026, 8, 15)

    result = NightlyCycle(
        doctor=StaticDoctor(health_report()),  # type: ignore[arg-type]
        executor=service,
        renderer=DigestRenderer(
            repo_root=tmp_path,
            queue=queue,
            policy=policy(),
            trial_loader=lambda _day: [],
        ),
        committer=lambda _path: False,
        researcher_pass=lambda day: researcher_calls.append(day) or 1,
    ).run(report_date=report_date)

    assert result.dispatched == 0
    assert result.researcher_invocations == 0
    assert researcher_calls == []
    deferrals = [
        event
        for event in load_events(queue.events_path)
        if event.event == "researcher_pass_deferred"
    ]
    assert [(event.reason_code, event.report_date) for event in deferrals] == [
        ("running_specs_unresolved", report_date.isoformat())
    ]


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (OSError("disk full"), "pg_dump_failed:OSError"),
        (
            subprocess.TimeoutExpired(["pg_dump"], 600),
            "pg_dump_failed:TimeoutExpired",
        ),
    ],
)
def test_nightly_backup_failure_quarantines_before_dispatch(
    tmp_path: Path,
    failure: Exception,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "evallab.automation.date_time_now",
        lambda: datetime(2026, 8, 15, 5, 0, 1, tzinfo=UTC),
    )
    queue = DirectoryQueue(tmp_path / "queue")
    calls = []
    service = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=policy(),
        runner=lambda request: calls.append(request),
        ingester=lambda _path: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
    )
    approved, _ = service.submit(
        ExperimentSpec(
            name="backup-gated-control",
            hypothesis="a failed backup prevents nightly dispatch",
            task="library/tasks/event-summary",
            agent="oracle",
            submitted_by="scheduler-test",
        )
    )

    result = NightlyCycle(
        doctor=StaticDoctor(health_report()),  # type: ignore[arg-type]
        executor=service,
        renderer=DigestRenderer(
            repo_root=tmp_path,
            queue=queue,
            policy=policy(),
            trial_loader=lambda _day: [],
        ),
        committer=lambda _path: False,
        database_backup=lambda _day: (_ for _ in ()).throw(failure),
    ).run(report_date=date(2026, 8, 14))

    assert result.quarantined is True
    assert result.dispatched == 0
    assert result.backup_path is None
    assert approved.exists()
    assert calls == []
    assert any(
        event.event == "postgres_backup_failed"
        and event.reason_code == reason
        for event in load_events(queue.events_path)
    )
    content = result.digest_path.read_text()
    assert "Quarantined: yes" in content
    assert f"Failed readiness checks: {reason}" in content
    assert "Zero dispatch enforced: yes" in content


def test_digest_enrichment_failure_rerenders_a_quarantined_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "evallab.automation.date_time_now",
        lambda: datetime(2026, 8, 15, 5, 0, 1, tzinfo=UTC),
    )
    queue = DirectoryQueue(tmp_path / "queue")
    service = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=policy(),
        runner=lambda request: request.jobs_dir / request.name,
        ingester=lambda _path: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
    )

    def partial_enrichment(path: Path, _day: date) -> None:
        path.write_text(path.read_text() + "PARTIAL ENRICHMENT\n")
        raise RuntimeError("fleet unavailable")

    result = NightlyCycle(
        doctor=StaticDoctor(health_report()),  # type: ignore[arg-type]
        executor=service,
        renderer=DigestRenderer(
            repo_root=tmp_path,
            queue=queue,
            policy=policy(),
            trial_loader=lambda _day: [],
        ),
        committer=lambda _path: True,
        digest_enricher=partial_enrichment,
    ).run(report_date=date(2026, 8, 14))

    assert result.quarantined is True
    assert result.committed is True
    content = result.digest_path.read_text()
    assert "Quarantined: yes" in content
    assert "Failed readiness checks: fleet_digest_failed:RuntimeError" in content
    assert "digest_enrichment_failed" in content
    assert "PARTIAL ENRICHMENT" not in content


def test_fleet_uses_semantic_report_date_across_local_midnight(tmp_path: Path) -> None:
    report_date = date(2026, 8, 14)
    digest_path = tmp_path / "digests/2026-08-14.md"
    digest_path.parent.mkdir(parents=True)
    digest_path.write_text("# Digest\n")
    queue = DirectoryQueue(tmp_path / "queue")
    queue.append_event(
        QueueEvent(
            event_id="01M00000000000000000000000",
            spec_id="system-01M00000000000000000000000",
            occurred_at=datetime(2026, 8, 15, 5, 0, 1, tzinfo=UTC),
            event="researcher_pass_deferred",
            actor="nightly",
            reason_code="missing_credential:codex",
            report_date=report_date.isoformat(),
        )
    )

    append_fleet_section(
        digest_path,
        report_date=report_date,
        repo_root=tmp_path,
        policy=policy(),
        ledger=CallLedger(tmp_path / "queue/researchers/calls.jsonl"),
        catalog_spend=lambda _day: 0,
    )

    content = digest_path.read_text()
    assert "Deferrals: 1" in content
    assert "researcher_pass_deferred: missing_credential:codex" in content


def test_researcher_budget_uses_utc_day_at_local_evening_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_date = date(2026, 8, 14)
    bundle = EvidenceBundle(
        report_date=report_date,
        period_date=date(2026, 8, 13),
        generated_at=datetime(2026, 8, 15, 0, 30, tzinfo=UTC),
        trials=[
            TrialEvidence(
                job_name="boundary-job",
                task_name="boundary-task",
                agent_name="codex",
                reward=0,
                finished_at="2026-08-15T00:20:00Z",
                evidence_paths=["runs/boundary-job/result.json"],
            )
        ],
        allowed_evidence_paths=["runs/boundary-job/result.json"],
    )
    loop = ResearcherLoop(
        repo_root=tmp_path,
        invoker=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("invoker should not be reached")
        ),  # type: ignore[arg-type]
        policy=policy(),
        evidence_loader=lambda _day, _path: bundle,
        catalog_spend=lambda _day: 0,
        clock=lambda: datetime(2026, 8, 15, 0, 30, tzinfo=UTC),
    )
    observed_days: list[date] = []

    def capture_budget_day(**kwargs):
        observed_days.append(kwargs["day"])
        raise ResearcherDeferred("boundary-observed")

    monkeypatch.setattr(loop, "_invoke_validated", capture_budget_day)

    result = loop.run(report_date=report_date)

    assert observed_days == [date(2026, 8, 15)]
    assert result.deferred_reason == "boundary-observed"


def test_guarded_tick_records_dispatch_idle_and_stop_deferrals(tmp_path: Path) -> None:
    queue = DirectoryQueue(tmp_path / "queue")

    def runner(request):
        job = request.jobs_dir / request.name
        job.mkdir(parents=True)
        return job

    service = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=policy(),
        runner=runner,
        ingester=lambda _path: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
    )
    tick = GuardedTick(
        doctor=StaticDoctor(health_report()),  # type: ignore[arg-type]
        executor=service,
    )

    assert tick.run().dispatched == 0
    service.queue.stop()
    assert tick.run().dispatched == 0
    service.queue.resume()
    service.submit(
        ExperimentSpec(
            name="tick-outcome-control",
            hypothesis="record a dispatched cycle",
            task="library/tasks/event-summary",
            agent="oracle",
            submitted_by="scheduler-test",
        )
    )
    assert tick.run().dispatched == 1

    outcomes = [
        (event.event, event.reason_code)
        for event in load_events(queue.events_path)
        if event.actor == "scheduled-tick"
    ]
    assert outcomes == [
        ("tick_deferred", "no_approved_specs"),
        ("tick_deferred", "stop_file_present"),
        ("tick_dispatched", "dispatched:1"),
    ]


def test_locked_keychain_still_dispatches_credentialless_nightly_control(
    tmp_path: Path,
) -> None:
    calls = []
    queue = DirectoryQueue(tmp_path / "queue")
    service = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=policy(),
        runner=lambda request: calls.append(request),
        ingester=lambda path: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
    )
    approved, _ = service.submit(
        ExperimentSpec(
            name="must-not-dispatch",
            hypothesis="locked model credentials do not block a free control",
            task="library/tasks/event-summary",
            agent="oracle",
            submitted_by="scheduler-test",
        )
    )
    result = NightlyCycle(
        doctor=StaticDoctor(
            health_report(keychain_readable=False, codex_auth_present=False)
        ),  # type: ignore[arg-type]
        executor=service,
        renderer=DigestRenderer(
            repo_root=tmp_path,
            queue=queue,
            policy=policy(),
            trial_loader=lambda day: [],
        ),
        committer=lambda path: True,
    ).run(report_date=date.today())

    assert result.dispatched == 1
    assert len(calls) == 1
    content = result.digest_path.read_text()
    assert "Quarantined: no" in content
    assert not approved.exists()


def test_digest_uses_queue_when_catalog_is_unavailable(tmp_path: Path) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    service = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=policy(),
        runner=lambda request: request.jobs_dir / request.name,
        ingester=lambda path: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
    )
    waiting, _ = service.submit(
        ExperimentSpec(
            name="waiting-proposal",
            hypothesis="wait for human review",
            task="unregistered/task",
            agent="other",
            submitted_by="agent",
        )
    )
    renderer = DigestRenderer(
        repo_root=tmp_path,
        queue=queue,
        policy=policy(),
        trial_loader=lambda day: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    content = renderer.write(report_date=date(2026, 8, 13)).read_text()

    assert waiting.parent.name == "waiting"
    assert "Catalog readable: no" in content
    assert "waiting-proposal" in content


def test_digest_separates_transient_provider_capacity_from_other_failures(
    tmp_path: Path,
) -> None:
    report_date = date(2026, 8, 14)
    trials = [
        DigestTrial(
            job_name="provider-capacity",
            task_name="canary/example",
            agent_name="codex",
            model_name="configured",
            reward=None,
            exception_type="transient_harness",
            cost_usd=0,
            finished_at="2026-08-13T12:00:00Z",
        ),
        DigestTrial(
            job_name="broken-container",
            task_name="canary/example",
            agent_name="codex",
            model_name="configured",
            reward=None,
            exception_type="EnvironmentError",
            cost_usd=0,
            finished_at="2026-08-13T12:01:00Z",
        ),
    ]
    renderer = DigestRenderer(
        repo_root=tmp_path,
        queue=DirectoryQueue(tmp_path / "queue"),
        policy=policy(),
        trial_loader=lambda day: trials if day == date(2026, 8, 13) else [],
        drift_loader=lambda _day: [],
    )

    text = renderer.write(report_date=report_date).read_text()

    assert "harness_failure=1" in text
    assert "transient_harness=1" in text


def test_commit_digest_commits_only_the_digest(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    digest = tmp_path / "digests/2026-08-13.md"
    digest.parent.mkdir()
    digest.write_text("daily\n")
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("do not commit\n")

    assert commit_digest(digest) is True

    tracked = subprocess.run(
        ["git", "show", "--pretty=", "--name-only", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert tracked == ["digests/2026-08-13.md"]
    assert unrelated.exists()


def test_commit_digest_bounds_every_noninteractive_git_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = tmp_path / "digests/2026-08-14.md"
    digest.parent.mkdir()
    digest.write_text("daily\n")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs):
        calls.append((command, kwargs))
        if "diff" in command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        if "commit" in command:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(digest_module.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="bounded digest Git command failed"):
        commit_digest(digest)

    assert len(calls) == 3
    assert all(
        kwargs["timeout"] == digest_module.SUPPORT_COMMAND_TIMEOUT_SECONDS
        for _, kwargs in calls
    )
    assert all(kwargs["stdin"] is subprocess.DEVNULL for _, kwargs in calls)
    assert all(kwargs["capture_output"] is True for _, kwargs in calls)
    assert all(kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0" for _, kwargs in calls)


def test_doctor_codex_only_night_is_healthy(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/auth.json").write_text("{}")

    report = HeadlessDoctor(
        tmp_path,
        home=home,
        executor=RuntimeChecks(),  # type: ignore[arg-type]
        keychain_probe=lambda: False,
        postgres_probe=lambda: True,
        disk_probe=lambda: True,
    ).run()

    assert report.healthy is True
    assert report.checks.keychain_readable is False


def test_doctor_with_no_credentials_keeps_controls_runnable(tmp_path: Path) -> None:
    from evallab.automation import blocking_health_failures

    report = HeadlessDoctor(
        tmp_path,
        home=tmp_path / "empty-home",
        executor=RuntimeChecks(),  # type: ignore[arg-type]
        keychain_probe=lambda: False,
        postgres_probe=lambda: True,
        disk_probe=lambda: True,
    ).run()

    assert report.healthy is True
    assert blocking_health_failures(report) == []


def test_guarded_tick_with_no_credentials_dispatches_only_controls(tmp_path: Path) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    requests = []
    tick_policy = StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[
            AutoRunRule(name="local-controls", agents=["oracle", "nop"]),
            AutoRunRule(name="model-controls", agents=["codex"]),
        ],
    )
    service = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=tick_policy,
        runner=lambda request: requests.append(request)
        or (request.jobs_dir / request.name),
        ingester=lambda _path: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: frozenset(),
    )
    service.submit(
        ExperimentSpec(
            name="credentialless-control",
            hypothesis="controls remain runnable without model credentials",
            task="library/tasks/event-summary",
            agent="oracle",
            submitted_by="scheduler-test",
        )
    )
    service.submit(
        ExperimentSpec(
            name="credentialless-codex",
            hypothesis="model work waits for its own credential",
            task="library/tasks/event-summary",
                agent="codex",
                model="openai/example",
                est_cost_usd=1,
            submitted_by="scheduler-test",
        )
    )
    doctor = StaticDoctor(
        health_report(keychain_readable=False, codex_auth_present=False)
    )

    result = GuardedTick(doctor=doctor, executor=service).run()  # type: ignore[arg-type]

    assert result.dispatched == 1
    assert [request.name for request in requests] == ["credentialless-control"]
    assert [spec.name for _, spec in queue.list_specs("approved")] == [
        "credentialless-codex"
    ]
    terminal = [
        event
        for event in load_events(queue.events_path)
        if event.actor == "scheduled-tick"
    ]
    assert terminal[-1].event == "tick_dispatched"
