from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

from evallab import database
from evallab.automation import NightlyCycle
from evallab.cli import _doctor
from evallab.digest import DigestRenderer
from evallab.evidence import atif as atif_module
from evallab.evidence import facts as facts_module
from evallab.evidence.atif import (
    JOB_PROJECTION_FILE,
    PROJECTED_TABLES,
    IngestProjectionResult,
    ProjectionFailure,
    ProjectionInvariant,
    check_projection_invariant,
    ingest_and_project,
)
from evallab.evidence_store import archive_evidence, evidence_locator
from evallab.queue import DirectoryQueue, Executor, load_events
from evallab.runner import SettledRun
from evallab.schemas import (
    AutoRunRule,
    ExperimentSpec,
    HeadlessDoctorChecks,
    HeadlessDoctorReport,
    StandingApprovalsPolicy,
)


def _policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
    )


def _result(*failures: ProjectionFailure) -> IngestProjectionResult:
    return IngestProjectionResult(cataloged_jobs=1, tables=(), failures=failures)


def _failure(job_id: str = "00000000-0000-0000-0000-000000000001") -> ProjectionFailure:
    return ProjectionFailure(
        job_id=job_id,
        job_name="oracle-control",
        error_type="PermissionError",
        message="PermissionError: derived directory is unavailable",
    )


def test_daily_cost_query_uses_explicit_utc_policy_day(monkeypatch) -> None:
    observed: list[tuple[str, tuple[object, ...] | None]] = []

    class Result:
        def fetchone(self):
            return (3.5,)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, parameters=None):
            observed.append((query, parameters))
            return Result()

    monkeypatch.setattr(
        database.psycopg,
        "connect",
        lambda *_args, **_kwargs: Connection(),
    )

    assert (
        database.daily_cost_usd(
            "postgresql://catalog",
            date(2026, 8, 15),
        )
        == 3.5
    )
    query, parameters = observed[0]
    assert "AT TIME ZONE 'UTC'" in query
    assert "current_setting" not in query
    assert parameters == (date(2026, 8, 15),)


def test_catalog_finishes_before_projection_failure_is_returned(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []
    job = SimpleNamespace(id=_failure().job_id, name="oracle-control", trials=())
    cas_job = SimpleNamespace(id=job.id, name="cas-authoritative", trials=())
    cataloged: list[SimpleNamespace] = []
    source_dir = tmp_path / "raw-job"
    source_dir.mkdir()
    (source_dir / "result.json").write_text("{}\n")
    archive = archive_evidence(
        source_dir,
        tmp_path / "store",
        kind="job",
        record_id=job.id,
    )

    monkeypatch.setattr(database, "initialize", lambda url: calls.append("initialize"))

    def ingest_catalog_job(_url, jobs, *, root):
        calls.append("base-catalog")
        cataloged.extend(jobs)
        return len(jobs)

    monkeypatch.setattr(database, "ingest", ingest_catalog_job)
    monkeypatch.setattr(
        facts_module,
        "ingest_catalog",
        lambda url, jobs, root, derived_root: calls.append("fact-catalog"),
    )

    def fail_projection(jobs, output_root):
        calls.append("parquet")
        raise PermissionError("derived directory is unavailable")

    monkeypatch.setattr(facts_module, "rebuild_from_raw", fail_projection)
    monkeypatch.setattr(atif_module, "load_job", lambda _path: cas_job)

    result = ingest_and_project(
        "postgresql://test",
        [job],  # type: ignore[list-item]
        root=tmp_path,
        output_root=tmp_path / "derived/parquet",
        source_locators={job.id: evidence_locator(tmp_path / "store", archive)},
        settlement_recorder=lambda *_args: None,
    )

    assert calls == ["initialize", "base-catalog", "fact-catalog", "parquet"]
    assert result.cataloged_jobs == 1
    assert [(table.table, table.rows) for table in result.tables] == [("jobs", 1)]
    assert cataloged == [cas_job]
    assert result.failures[0].reason_code == (
        "projection_failed:00000000-0000-0000-0000-000000000001:PermissionError"
    )


def test_queue_projection_failure_is_not_execution_failure(tmp_path: Path) -> None:
    failure = _failure()

    def runner(request):
        destination = request.jobs_dir / request.name
        destination.mkdir(parents=True)
        store_root = tmp_path / "queue-cas"
        archive = archive_evidence(
            destination,
            store_root,
            kind="job",
            record_id=f"test-{request.name}",
        )
        shutil.rmtree(destination)
        return SettledRun(
            cas_locator=evidence_locator(store_root, archive),
            cas_record=archive,
        )

    queue = DirectoryQueue(tmp_path / "queue")
    executor = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=_policy(),
        runner=runner,
        ingester=lambda path: _result(failure),
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: frozenset(),
    )
    approved, _ = executor.submit(
        ExperimentSpec(
            name="oracle-control",
            hypothesis="projection failures are harness events",
            purpose="practice",
            task="library/tasks/event-summary",
            agent="oracle",
            submitted_by="test",
        )
    )
    spec_id = str(queue.load(approved).spec_id)

    assert executor.tick() == 1
    assert queue.locate(spec_id, ("done",)).parent.name == "done"
    events = load_events(queue.events_path)
    assert [event.event for event in events][-2:] == [
        "projection_failed",
        "dispatch_completed",
    ]
    projection_event = events[-2]
    assert projection_event.reason_code == failure.reason_code
    assert all(event.reason_code != "execution_failed" for event in events)


def _write_complete_partition(root: Path, job_id: str, trial_id: str) -> None:
    job_root = root / f"job_id={job_id}"
    job_root.mkdir(parents=True)
    (job_root / JOB_PROJECTION_FILE).write_bytes(b"parquet")
    partition = job_root / f"trial_id={trial_id}"
    partition.mkdir(parents=True)
    for filename in PROJECTED_TABLES:
        (partition / filename).write_bytes(b"parquet")


def test_catalog_projection_invariant_requires_partition_or_reasoned_exception(
    tmp_path: Path,
) -> None:
    projected_job = "00000000-0000-0000-0000-000000000001"
    excepted_job = "00000000-0000-0000-0000-000000000003"
    _write_complete_partition(
        tmp_path / "derived",
        projected_job,
        "00000000-0000-0000-0000-000000000002",
    )
    queue = DirectoryQueue(tmp_path / "queue")
    queue.append_event(
        SimpleNamespace(
            model_dump_json=lambda exclude_none=True: (
                '{"reason_code":"projection_failed:' + excepted_job + ':PermissionError"}'
            )
        )
    )
    rows = [
        (projected_job, "projected", "00000000-0000-0000-0000-000000000002"),
        (excepted_job, "excepted", "00000000-0000-0000-0000-000000000004"),
    ]

    invariant = check_projection_invariant(
        "postgresql://test",
        tmp_path / "derived",
        queue.events_path,
        catalog_rows_loader=lambda url: rows,
    )

    assert invariant.ok is True
    assert invariant.projected_job_ids == {projected_job}
    assert invariant.excepted_job_ids == {excepted_job}
    assert invariant.exceptions_by_reason == {"PermissionError": frozenset({excepted_job})}
    expected_detail = "catalog=2 projected=1 exceptions=1 (PermissionError=1) missing=0 extra=0"
    assert invariant.detail == expected_detail
    archived_events = queue.events_path.with_name("events.jsonl.1")
    queue.events_path.replace(archived_events)
    archived = check_projection_invariant(
        "postgresql://test",
        tmp_path / "derived",
        queue.events_path,
        catalog_rows_loader=lambda url: rows,
    )
    assert archived.ok is True
    assert archived.excepted_job_ids == {excepted_job}

    archived_events.unlink()
    broken = check_projection_invariant(
        "postgresql://test",
        tmp_path / "derived",
        queue.events_path,
        catalog_rows_loader=lambda url: rows,
    )
    assert broken.ok is False
    assert broken.missing_job_ids == {excepted_job}


def test_zero_trial_catalog_job_is_projected_by_job_marker(tmp_path: Path) -> None:
    job_id = "00000000-0000-0000-0000-000000000005"
    job_root = tmp_path / "derived" / f"job_id={job_id}"
    job_root.mkdir(parents=True)
    (job_root / JOB_PROJECTION_FILE).write_bytes(b"parquet")

    invariant = check_projection_invariant(
        "postgresql://test",
        tmp_path / "derived",
        tmp_path / "events.jsonl",
        catalog_rows_loader=lambda url: [(job_id, "zero-trial-job", None)],
    )

    assert invariant.ok is True
    assert invariant.projected_job_ids == {job_id}


def test_nightly_uses_completed_job_ingester_and_records_projection_failure(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    queue = DirectoryQueue(tmp_path / "queue")
    executor = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=_policy(),
        ingester=lambda path: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
    )
    checks = HeadlessDoctorChecks(
        keychain_readable=True,
        codex_auth_present=False,
        docker_reachable=True,
        postgres_reachable=True,
        disk_headroom=True,
    )
    report = HeadlessDoctorReport(
        checked_at=datetime.now(UTC),
        healthy=True,
        checks=checks,
    )
    doctor = SimpleNamespace(run=lambda: report)
    result = NightlyCycle(
        doctor=doctor,  # type: ignore[arg-type]
        executor=executor,
        renderer=DigestRenderer(
            repo_root=tmp_path,
            queue=queue,
            policy=_policy(),
            trial_loader=lambda day: [],
        ),
        committer=lambda path: False,
        completed_job_ingester=lambda: calls.append("ingest-and-project") or _result(_failure()),
    ).run(report_date=date(2026, 8, 14))

    assert calls == ["ingest-and-project"]
    assert result.quarantined is False
    event = next(
        event for event in load_events(queue.events_path) if event.event == "projection_failed"
    )
    assert event.actor == "nightly"
    assert event.reason_code == _failure().reason_code


def test_doctor_fails_when_catalog_projection_invariant_is_broken(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from evallab import cli

    task = tmp_path / "library/tasks/event-summary/task.toml"
    task.parent.mkdir(parents=True)
    task.write_text("version = 1\n")
    runtime = SimpleNamespace(
        local_runtime_checks=lambda: [
            ("harbor", True, "ok"),
            ("docker", True, "ok"),
            ("uv", True, "ok"),
            ("docker-daemon", True, "ok"),
        ]
    )
    monkeypatch.setattr(cli.Executor, "from_repo", lambda root: runtime)
    monkeypatch.setattr(cli.database, "ping", lambda url: "ok")
    monkeypatch.setattr(
        cli,
        "check_projection_invariant",
        lambda url, output, events: ProjectionInvariant(
            catalog_job_ids=frozenset({"job"}),
            projected_job_ids=frozenset(),
            excepted_job_ids=frozenset(),
            missing_job_ids=frozenset({"job"}),
            extra_job_ids=frozenset(),
        ),
    )

    assert _doctor(tmp_path) == 1
    assert "FAIL  catalog-parquet catalog=1 projected=0" in capsys.readouterr().out
