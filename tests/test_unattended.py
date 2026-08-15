from __future__ import annotations

import plistlib
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

from evallab.automation import HeadlessDoctor, NightlyCycle, ScheduleInstaller
from evallab.digest import DigestRenderer, DigestTrial, commit_digest
from evallab.paths import DERIVED_ROOT_ENV
from evallab.queue import DirectoryQueue, Executor
from evallab.schemas import (
    AutoRunRule,
    ExperimentSpec,
    HeadlessDoctorChecks,
    HeadlessDoctorReport,
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
    healthy = (
        checks.docker_reachable and checks.postgres_reachable and checks.disk_headroom
    ) and (checks.keychain_readable or checks.codex_auth_present)
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
    ).run(report_date=report_date)

    assert result.dispatched == 1
    assert len(ingested) == 1
    assert committed == [result.digest_path]
    content = result.digest_path.read_text()
    assert "nightly-oracle-control" in content
    assert "local-controls" in content
    assert "Quarantined: no" in content


def test_locked_keychain_quarantines_nightly_with_zero_dispatch(tmp_path: Path) -> None:
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
            hypothesis="locked credentials quarantine all dispatch",
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

    assert result.dispatched == 0
    assert calls == []
    content = result.digest_path.read_text()
    assert "Quarantined: yes" in content
    assert "keychain_readable" in content
    assert "Zero dispatch enforced: yes" in content
    assert approved.exists()


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


def test_doctor_with_no_credentials_quarantines_with_specific_reason(tmp_path: Path) -> None:
    from evallab.automation import blocking_health_failures

    report = HeadlessDoctor(
        tmp_path,
        home=tmp_path / "empty-home",
        executor=RuntimeChecks(),  # type: ignore[arg-type]
        keychain_probe=lambda: False,
        postgres_probe=lambda: True,
        disk_probe=lambda: True,
    ).run()

    assert report.healthy is False
    assert blocking_health_failures(report) == ["no_credentials"]
