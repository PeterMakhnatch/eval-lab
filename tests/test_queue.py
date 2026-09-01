import json
import multiprocessing
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest

import evallab.queue as queue_module
from evallab import eventlog
from evallab.credentials import CLAUDE_OAUTH, CODEX_AUTH
from evallab.evidence_store import archive_evidence, evidence_locator
from evallab.queue import (
    DirectoryQueue,
    DispatchCapacity,
    Executor,
    PaidRunAuthorization,
    PolicyGate,
    load_events,
)
from evallab.runner import (
    ExecutionFailure,
    RunRequest,
    SettledRun,
    TransientHarnessFailure,
    TrialTimeoutFailure,
)
from evallab.schemas import (
    AutoRunRule,
    ExperimentSpec,
    QueueEvent,
    StandingApprovalsPolicy,
    canonical_grid_point_id,
)


def policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[
            AutoRunRule(name="local-controls", agents=["oracle", "nop"]),
            AutoRunRule(
                name="canary",
                tasks=["canary/*"],
                agents=["codex", "claude-code"],
                max_attempts=3,
            ),
        ],
        escalate_to_human=["anything_exceeding_ceilings"],
    )


def spec(
    name: str,
    *,
    agent: str = "oracle",
    task: str = "library/tasks/event-summary",
    model: str | None = None,
    est_cost_usd: float = 0,
    policy_rule: str | None = None,
    attempts: int = 1,
    concurrency: int = 1,
) -> ExperimentSpec:
    return ExperimentSpec(
        name=name,
        hypothesis="exercise the queue state machine",
        purpose="practice",
        task=task,
        task_path="library/tasks/event-summary" if task.startswith("canary/") else None,
        agent=agent,
        model=model,
        submitted_by="test-agent",
        est_cost_usd=est_cost_usd,
        policy_rule=policy_rule,
        attempts=attempts,
        concurrency=concurrency,
    )


def submit_authorized(service: Executor, item: ExperimentSpec) -> Path:
    """Submit paid work and record the human authorisation it now requires.

    Billable agents never reach `approved/` from a standing rule; every test
    that needs one dispatched has to go through the same operator step as
    `uv run evallab approve <spec-id> --actor <you>`.
    """
    waiting, _ = service.submit(item)
    return service.queue.approve(str(service.queue.load(waiting).spec_id), actor="peter")


def identified(item: ExperimentSpec) -> tuple[ExperimentSpec, PaidRunAuthorization]:
    """A queued spec plus the recorded authorisation that covers exactly it."""
    moment = datetime(2026, 8, 16, tzinfo=UTC)
    spec_id = f"01TEST{item.name.upper().replace('-', '')[:20]}"
    queued = item.model_copy(update={"spec_id": spec_id, "submitted_at": moment})
    return queued, PaidRunAuthorization(
        spec_id=str(queued.spec_id), actor="peter", authorized_at=moment
    )


def settled(job_dir: Path) -> SettledRun:
    """Return a real CAS-only runner result for queue boundary tests."""

    job_dir.mkdir(parents=True, exist_ok=True)
    store_root = job_dir.parent / ".queue-test-cas"
    archive = archive_evidence(
        job_dir,
        store_root,
        record_id=f"test-{job_dir.name}",
        kind="job",
    )
    shutil.rmtree(job_dir)
    return SettledRun(
        cas_locator=evidence_locator(store_root, archive),
        cas_record=archive,
    )


def executor(
    root: Path,
    *,
    runner=None,
    ingester=None,
    spent: float = 0,
    credentials: frozenset[str] | None = None,
    sleeper=lambda _seconds: None,
    max_transient_retries: int = 2,
    parallel: int = 1,
    capacity: DispatchCapacity | None = None,
) -> Executor:
    return Executor(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=policy(),
        runner=runner or (lambda request: settled(request.jobs_dir / request.name)),
        ingester=ingester or (lambda path: None),
        spent_today=lambda: spent,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: (
            credentials if credentials is not None else frozenset({"claude_oauth", "codex_auth"})
        ),
        sleeper=sleeper,
        max_transient_retries=max_transient_retries,
        parallel=parallel,
        capacity=capacity,
    )


def _event(index: int) -> QueueEvent:
    return QueueEvent(
        event_id=f"event-{index}",
        spec_id=f"spec-{index}",
        occurred_at=datetime(2026, 8, 14, tzinfo=UTC),
        event="test_event",
        actor="test",
    )


def _append_events_process(root: str, start: int, count: int) -> None:
    queue = DirectoryQueue(Path(root), events_max_bytes=500, event_backups=20)
    for index in range(start, start + count):
        queue.append_event(_event(index))


def test_event_log_rotation_is_bounded_and_reads_oldest_first(tmp_path: Path) -> None:
    queue = DirectoryQueue(tmp_path / "queue", events_max_bytes=320, event_backups=2)

    for index in range(5):
        queue.append_event(_event(index))

    assert [path.name for path in sorted(queue.root.glob("events.jsonl*"))] == [
        "events.jsonl",
        "events.jsonl.1",
        "events.jsonl.2",
    ]
    retained = load_events(queue.events_path)
    assert [event.event_id for event in retained] == [f"event-{index}" for index in range(5)]


def test_event_log_concurrent_writes_remain_valid_json(tmp_path: Path) -> None:
    queue = DirectoryQueue(tmp_path / "queue", events_max_bytes=1_000_000)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(queue.append_event, [_event(index) for index in range(100)]))

    events = load_events(queue.events_path)
    assert len(events) == 100
    assert {event.event_id for event in events} == {f"event-{index}" for index in range(100)}


def test_event_rotation_is_consistent_across_writer_processes_and_reader(
    tmp_path: Path,
) -> None:
    root = tmp_path / "queue"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_append_events_process, args=(str(root), start, 10))
        for start in (0, 10)
    ]
    for process in processes:
        process.start()
    while any(process.is_alive() for process in processes):
        # Every snapshot must either parse fully or wait on the process lock;
        # a reader must never observe half a rotation.
        load_events(root / "events.jsonl")
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0

    events = load_events(root / "events.jsonl")
    assert len(events) == 20
    assert {event.event_id for event in events} == {f"event-{index}" for index in range(20)}


def test_event_reader_checks_for_first_append_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    first = _event(1)

    @contextmanager
    def first_writer_finishes_before_reader(_path: Path, *, exclusive: bool):
        assert exclusive is False
        queue.events_path.write_text(first.model_dump_json() + "\n")
        yield

    monkeypatch.setattr(eventlog, "event_log_lock", first_writer_finishes_before_reader)

    lines = eventlog.read_event_log_lines(queue.events_path)

    assert [(line_number, line) for _, line_number, line in lines] == [(1, first.model_dump_json())]


def test_two_agents_submit_concurrently_without_interference(tmp_path: Path) -> None:
    service = executor(tmp_path)
    submissions = [spec("agent-one-control"), spec("agent-two-control", agent="nop")]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(service.submit, submissions))

    paths = [path for path, decision in results if decision.admitted]
    assert len(paths) == 2
    assert len({path.name for path in paths}) == 2
    assert all(path.parent.name == "approved" for path in paths)
    events = load_events(tmp_path / "queue/events.jsonl")
    assert len(events) == 4
    assert {event.spec_id for event in events} == {
        service.queue.load(path).spec_id for path in paths
    }


def test_out_of_policy_spec_waits_with_a_reason(tmp_path: Path) -> None:
    service = executor(tmp_path)

    path, decision = service.submit(spec("unmatched-control", policy_rule="no-such-standing-rule"))

    assert path.parent.name == "waiting"
    assert decision.reason_code == "out_of_policy"
    reason_files = list((tmp_path / "queue/reasons").glob("*.json"))
    assert len(reason_files) == 1
    assert '"code": "out_of_policy"' in reason_files[0].read_text()


def test_spec_past_ceiling_is_refused_with_reason_file(tmp_path: Path) -> None:
    service = executor(tmp_path)
    submit_authorized(
        service,
        spec(
            "canary-over-per-job-ceiling",
            task="canary/event-summary",
            agent="codex",
            model="openai/example",
            est_cost_usd=9,
        ),
    )

    assert service.tick() == 0
    reason = sorted((tmp_path / "queue/reasons").glob("*.json"))[-1]
    assert '"code": "per_job_cost_ceiling"' in reason.read_text()


def test_stop_file_halts_dispatch(tmp_path: Path) -> None:
    requests = []
    service = executor(tmp_path, runner=lambda request: requests.append(request))
    path, _ = service.submit(spec("stopped-oracle-control"))
    service.queue.stop()

    assert service.tick() == 0
    assert requests == []
    assert path.exists()
    assert service.queue.stop_path.is_file()


def test_tick_uses_stub_runner_ingests_and_records_every_transition(tmp_path: Path) -> None:
    requests = []
    ingested = []

    def run(request: RunRequest) -> SettledRun:
        requests.append(request)
        destination = request.jobs_dir / request.name
        destination.mkdir(parents=True)
        return settled(destination)

    service = executor(tmp_path, runner=run, ingester=ingested.append)
    approved, _ = service.submit(spec("completed-oracle-control"))
    queued = service.queue.load(approved)

    assert service.tick() == 1
    assert len(requests) == 1
    assert len(ingested) == 1
    assert ingested[0].record_id == "test-completed-oracle-control"
    assert ingested[0].expected_record_digest.startswith("sha256:")
    assert not (requests[0].jobs_dir / requests[0].name).exists()
    completed_event = load_events(service.queue.events_path)[-1]
    assert completed_event.cas_store_root == str(ingested[0].store_root)
    assert completed_event.cas_record_kind == ingested[0].kind
    assert completed_event.cas_record_id == ingested[0].record_id
    assert completed_event.cas_record_digest == ingested[0].expected_record_digest
    assert completed_event.cas_content_digest == ingested[0].expected_content_digest
    assert service.queue.locate(str(queued.spec_id), ("done",)).parent.name == "done"
    events = [event.event for event in load_events(service.queue.events_path)]
    assert events == [
        "submitted",
        "policy_admitted",
        "dispatch_started",
        "dispatch_completed",
    ]


def test_doctor_docker_daemon_probe_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts: list[float] = []

    def timeout(_command, **kwargs):
        observed_timeouts.append(kwargs["timeout"])
        raise queue_module.subprocess.TimeoutExpired(_command, kwargs["timeout"])

    monkeypatch.setattr(queue_module, "tool_version", lambda _command: "present")
    monkeypatch.setattr(queue_module.shutil, "which", lambda _command: "/bin/docker")
    monkeypatch.setattr(queue_module.subprocess, "run", timeout)

    checks = executor(tmp_path).local_runtime_checks()

    assert checks[-1] == ("docker-daemon", False, "unavailable: TimeoutExpired")
    assert observed_timeouts == [queue_module.SUPPORT_COMMAND_TIMEOUT_SECONDS]


def test_human_approval_does_not_override_hard_cost_ceiling(tmp_path: Path) -> None:
    service = executor(tmp_path, spent=19.5)
    waiting, _ = service.submit(
        spec(
            "human-approved-over-ceiling",
            task="unregistered/task",
            agent="codex",
            model="openai/example",
            est_cost_usd=1,
        )
    )
    queued = service.queue.load(waiting)
    service.queue.approve(str(queued.spec_id), actor="peter")

    assert service.tick() == 0
    final = service.queue.locate(str(queued.spec_id), ("waiting",))
    assert final.parent.name == "waiting"
    assert any(
        "daily_cost_ceiling" in path.read_text()
        for path in service.queue.reasons_dir.glob("*.json")
    )


def test_quiet_failure_rule_only_quarantines_billable_specs(tmp_path: Path) -> None:
    gate = PolicyGate(policy())
    billable, authorization = identified(
        spec(
            "canary-after-failures",
            task="canary/event-summary",
            agent="codex",
            model="openai/example",
            est_cost_usd=1,
        )
    )

    decision = gate.decide(
        billable,
        spent_today_usd=0,
        consecutive_harness_failures=3,
        authorization=authorization,
    )
    control = gate.decide(
        spec("control-after-failures"),
        spent_today_usd=0,
        consecutive_harness_failures=3,
    )

    assert decision.reason_code == "quiet_failure_rule"
    assert control.admitted is True


def test_direct_execution_cannot_bypass_policy_for_billable_agent(tmp_path: Path) -> None:
    service = executor(tmp_path)
    request = RunRequest(
        task=tmp_path / "library/tasks/event-summary",
        agent="codex",
        model="openai/example",
        name="bypass-attempt",
        jobs_dir=tmp_path / "runs",
        allow_billable=True,
    )

    try:
        service.execute_direct(request)
    except ValueError as exc:
        assert "standing-policy queue" in str(exc)
        assert "--allow-billable" in str(exc)
        assert "does not bypass" in str(exc)
    else:
        raise AssertionError("billable direct execution unexpectedly bypassed policy")


def test_missing_credential_defers_spec_without_moving_it(tmp_path: Path) -> None:
    requests = []

    def run(request):
        requests.append(request)
        destination = request.jobs_dir / request.name
        destination.mkdir(parents=True)
        return destination

    service = executor(tmp_path, runner=run, credentials=frozenset())
    approved = submit_authorized(
        service, spec("codex-blocked", agent="codex", task="canary/event-summary")
    )
    assert approved.parent.name == "approved"
    service.submit(spec("oracle-proceeds"))

    dispatched = service.tick()

    # The credential-less control ran; the codex spec neither ran nor moved.
    assert dispatched == 1
    assert [request.name for request in requests] == ["oracle-proceeds"]
    remaining = [item.name for _, item in service.queue.list_specs("approved")]
    assert remaining == ["codex-blocked"]
    events = load_events(tmp_path / "queue/events.jsonl")
    deferrals = [e for e in events if e.event == "dispatch_deferred"]
    assert deferrals and deferrals[-1].reason_code == "missing_credential:codex_auth"


@pytest.mark.parametrize(
    ("credentials", "missing_agent", "missing_reason"),
    [
        (frozenset({CLAUDE_OAUTH}), "codex", f"missing_credential:{CODEX_AUTH}"),
        (frozenset({CODEX_AUTH}), "claude-code", f"missing_credential:{CLAUDE_OAUTH}"),
    ],
)
def test_tick_defers_only_the_agent_with_a_missing_credential(
    tmp_path: Path,
    credentials: frozenset[str],
    missing_agent: str,
    missing_reason: str,
) -> None:
    requests: list[RunRequest] = []

    def run(request: RunRequest) -> Path:
        requests.append(request)
        destination = request.jobs_dir / request.name
        destination.mkdir(parents=True)
        return destination

    service = executor(tmp_path, runner=run, credentials=credentials)
    submissions = [
        spec("credential-scope-oracle", agent="oracle"),
        spec("credential-scope-nop", agent="nop"),
        spec("credential-scope-codex", agent="codex", task="canary/event-summary"),
        spec(
            "credential-scope-claude",
            agent="claude-code",
            task="canary/event-summary",
        ),
    ]
    for item in submissions:
        if item.billable:
            submit_authorized(service, item)
        else:
            assert service.submit(item)[1].admitted

    assert service.tick() == 3

    expected_dispatched = {item.name for item in submissions if item.agent != missing_agent}
    assert {request.name for request in requests} == expected_dispatched
    approved = [item for _, item in service.queue.list_specs("approved")]
    assert [(item.name, item.agent) for item in approved] == [
        (
            "credential-scope-codex" if missing_agent == "codex" else "credential-scope-claude",
            missing_agent,
        )
    ]

    events = load_events(service.queue.events_path)
    deferrals = [event for event in events if event.event == "dispatch_deferred"]
    assert [(event.job_name, event.reason_code) for event in deferrals] == [
        (approved[0].name, missing_reason)
    ]
    assert {
        event.job_name for event in events if event.event == "dispatch_started"
    } == expected_dispatched


def test_spec_without_model_gets_agent_default_and_explicit_model_wins(tmp_path: Path) -> None:
    requests = []

    def run(request):
        requests.append(request)
        destination = request.jobs_dir / request.name
        destination.mkdir(parents=True)
        return destination

    service = executor(tmp_path, runner=run)
    submit_authorized(
        service, spec("codex-default-model", agent="codex", task="canary/event-summary")
    )
    submit_authorized(
        service,
        spec("codex-pinned-model", agent="codex", task="canary/event-summary", model="pinned-x"),
    )
    service.tick()

    by_name = {request.name: request.model for request in requests}
    assert by_name["codex-default-model"] == "gpt-5.6-terra"
    assert by_name["codex-pinned-model"] == "pinned-x"


def test_concurrent_tick_claiming_a_spec_mid_listing_is_tolerated(tmp_path: Path) -> None:
    service = executor(tmp_path)
    service.submit(spec("stays"))
    vanish_path, _ = service.submit(spec("claimed-by-other-tick"))

    queue = service.queue
    original_load = queue.load

    def racing_load(path):
        if path.name == vanish_path.name and path.exists():
            path.unlink()  # the other tick moved it between glob and read
        return original_load(path)

    queue.load = racing_load
    remaining = queue.list_specs("approved")
    assert [item.name for _, item in remaining] == ["stays"]


def test_transient_provider_failure_retries_with_capped_backoff_and_archives(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    def run(request: RunRequest) -> SettledRun:
        calls.append(request.name)
        destination = request.jobs_dir / request.name
        destination.mkdir(parents=True)
        (destination / "attempt.txt").write_text(str(len(calls)))
        if len(calls) < 3:
            raise TransientHarnessFailure("transient_harness:provider_http_429")
        return settled(destination)

    service = executor(tmp_path, runner=run, sleeper=sleeps.append)
    service.submit(spec("provider-recovers"))

    assert service.tick() == 1
    assert calls == ["provider-recovers"] * 3
    assert sleeps == [5.0, 10.0]
    assert (
        tmp_path / "runs/.transient-attempts/provider-recovers/attempt-1/attempt.txt"
    ).read_text() == "1"
    assert (
        tmp_path / "runs/.transient-attempts/provider-recovers/attempt-2/attempt.txt"
    ).read_text() == "2"
    events = load_events(service.queue.events_path)
    assert [event.reason_code for event in events if event.event == "dispatch_retry_scheduled"] == [
        "transient_harness:provider_http_429",
        "transient_harness:provider_http_429",
    ]
    assert service.queue.list_specs("done")


def test_transient_retry_cap_and_timeout_have_distinct_failure_reasons(
    tmp_path: Path,
) -> None:
    transient_calls = 0

    def transient(_request: RunRequest) -> Path:
        nonlocal transient_calls
        transient_calls += 1
        raise TransientHarnessFailure("transient_harness:provider_http_5xx")

    transient_service = executor(tmp_path / "transient", runner=transient)
    transient_service.submit(spec("provider-stays-down"))
    assert transient_service.tick() == 1
    assert transient_calls == 3
    transient_reason = next(transient_service.queue.reasons_dir.glob("*.json")).read_text()
    assert '"code": "transient_harness:provider_http_5xx"' in transient_reason

    def timeout(_request: RunRequest) -> Path:
        raise TrialTimeoutFailure("bounded by executor")

    timeout_service = executor(tmp_path / "timeout", runner=timeout)
    timeout_service.submit(spec("trial-times-out"))
    assert timeout_service.tick() == 1
    timeout_reason = next(timeout_service.queue.reasons_dir.glob("*.json")).read_text()
    assert '"code": "trial_wall_clock_timeout"' in timeout_reason


def test_billable_transient_retry_reserves_budget_before_another_call(
    tmp_path: Path,
) -> None:
    calls = 0

    def transient(_request: RunRequest) -> Path:
        nonlocal calls
        calls += 1
        raise TransientHarnessFailure("transient_harness:provider_http_429")

    service = executor(tmp_path, runner=transient, spent=17)
    submit_authorized(
        service,
        spec(
            "budgeted-provider-retry",
            task="canary/event-summary",
            agent="codex",
            est_cost_usd=2,
        ),
    )

    assert service.tick() == 1
    assert calls == 1
    refused = [
        event
        for event in load_events(service.queue.events_path)
        if event.event == "dispatch_retry_refused"
    ]
    assert [event.reason_code for event in refused] == ["transient_retry:daily_cost_ceiling"]


def test_failed_attempt_reservations_survive_executor_restart(tmp_path: Path) -> None:
    calls = 0

    def transient(_request: RunRequest) -> Path:
        nonlocal calls
        calls += 1
        raise TransientHarnessFailure("transient_harness:provider_http_503")

    first = executor(tmp_path, runner=transient, spent=15)
    submit_authorized(
        first,
        spec(
            "durable-provider-retry",
            task="canary/event-summary",
            agent="codex",
            est_cost_usd=2,
        ),
    )

    assert first.tick() == 1
    assert calls == 2
    reservations = [
        event
        for event in load_events(first.queue.events_path)
        if event.event == "dispatch_attempt_reserved"
    ]
    assert [(event.attempt_number, event.estimated_cost_usd) for event in reservations] == [
        (1, 2),
        (2, 2),
    ]

    restarted = executor(tmp_path, runner=lambda request: request.jobs_dir, spent=15)
    later = submit_authorized(
        restarted,
        spec(
            "later-billable-spec",
            task="canary/event-summary",
            agent="codex",
            est_cost_usd=2,
        ),
    )
    later_id = str(restarted.queue.load(later).spec_id)

    assert restarted.tick() == 0
    assert restarted.queue.locate(later_id, ("waiting",)).is_file()
    assert any(
        '"code": "daily_cost_ceiling"' in path.read_text()
        for path in restarted.queue.reasons_dir.glob(f"{later_id}-*.json")
    )


def test_running_reconciliation_settles_the_final_attempt_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVALLAB_EVIDENCE_STORE_ROOT", str(tmp_path / "evidence-cas"))
    service = executor(tmp_path, spent=2)
    approved = submit_authorized(
        service,
        spec(
            "reconciled-billable-spec",
            task="canary/event-summary",
            agent="codex",
            est_cost_usd=2,
        ),
    )
    queued = service.queue.load(approved)
    running = service.queue.transition(
        approved,
        "running",
        actor="executor",
        event="dispatch_started",
    )
    service._reserve_attempt(queued, 1)
    job_dir = tmp_path / queued.jobs_dir / queued.name
    job_dir.mkdir(parents=True)
    (job_dir / "result.json").write_text(
        '{"id": "job-reconciled", "n_total_trials": 1, "stats": {}, '
        '"finished_at": "2026-08-15T00:00:00Z"}\n'
    )
    trial_dir = job_dir / "trial-0"
    trial_dir.mkdir()
    (trial_dir / "result.json").write_text(
        '{"task_name": "canary/event-summary", "trial_name": "trial-0"}\n'
    )

    restarted = executor(tmp_path, spent=2)
    restarted.reconcile_running()

    assert not running.exists()
    assert restarted.queue.locate(str(queued.spec_id), ("done",)).is_file()
    assert restarted._reserved_attempt_spend_today() == 0
    assert restarted._effective_spend_today() == 2
    assert "running_reconciled" in [
        event.event for event in load_events(restarted.queue.events_path)
    ]


def test_reconciliation_never_settles_partial_harbor_job(tmp_path: Path) -> None:
    ingested: list[Path] = []
    service = executor(tmp_path, ingester=ingested.append)
    approved, _ = service.submit(spec("partial-running-control"))
    queued = service.queue.load(approved)
    running = service.queue.transition(
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

    service.reconcile_running()

    assert running.is_file()
    assert ingested == []
    assert not service.queue.list_specs("done")


def test_reconciliation_never_settles_completed_header_missing_trial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVALLAB_EVIDENCE_STORE_ROOT", str(tmp_path / "evidence-cas"))
    ingested: list[Path] = []
    service = executor(tmp_path, ingester=ingested.append)
    approved, _ = service.submit(spec("missing-trial-running-control"))
    queued = service.queue.load(approved)
    running = service.queue.transition(
        approved,
        "running",
        actor="executor",
        event="dispatch_started",
    )
    job_dir = tmp_path / queued.jobs_dir / queued.name
    job_dir.mkdir(parents=True)
    (job_dir / "result.json").write_text(
        '{"n_total_trials": 1, "stats": {}, "finished_at": "2026-08-15T00:00:00Z"}\n'
    )

    service.reconcile_running()

    assert not running.exists()
    assert ingested == []
    assert not service.queue.list_specs("done")
    failed = service.queue.list_specs("failed")
    assert len(failed) == 1
    reason = next(service.queue.reasons_dir.glob("*.json")).read_text()
    assert '"code": "running_reconcile_incomplete_evidence"' in reason


def test_unresolved_running_job_blocks_all_new_dispatch(tmp_path: Path) -> None:
    requests: list[RunRequest] = []
    service = executor(tmp_path, runner=lambda request: requests.append(request))
    partial, _ = service.submit(spec("partial-prior-control"))
    queued = service.queue.load(partial)
    service.queue.transition(
        partial,
        "running",
        actor="executor",
        event="dispatch_started",
    )
    job_dir = tmp_path / queued.jobs_dir / queued.name
    job_dir.mkdir(parents=True)
    (job_dir / "result.json").write_text(
        '{"n_total_trials": 1, "stats": {}, "finished_at": null}\n'
    )
    approved, _ = service.submit(spec("must-wait-control", agent="nop"))

    assert service.tick() == 0

    assert requests == []
    assert approved.is_file()
    assert service.last_tick_reason == "running_specs_unresolved"


def test_reconciliation_fails_closed_on_terminal_transient_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVALLAB_EVIDENCE_STORE_ROOT", str(tmp_path / "evidence-cas"))
    service = executor(
        tmp_path,
        ingester=lambda path: (_ for _ in ()).throw(
            AssertionError(f"transient job was ingested: {path}")
        ),
    )
    approved = submit_authorized(
        service,
        spec(
            "interrupted-transient-spec",
            task="canary/event-summary",
            agent="codex",
            est_cost_usd=2,
        ),
    )
    queued = service.queue.load(approved)
    service.queue.transition(
        approved,
        "running",
        actor="executor",
        event="dispatch_started",
    )
    service._reserve_attempt(queued, 1)
    job_dir = tmp_path / queued.jobs_dir / queued.name
    trial_dir = job_dir / "event-summary__trial"
    trial_dir.mkdir(parents=True)
    (job_dir / "result.json").write_text(
        '{"n_total_trials": 1, "stats": {}, "finished_at": "2026-08-15T00:00:00Z"}\n'
    )
    (trial_dir / "result.json").write_text(
        '{"task_name": "event-summary", '
        '"trial_name": "event-summary__trial", '
        '"exception_info": {"exception_type": "ApiOverloadedError", '
        '"exception_message": "API Error: Overloaded"}}\n'
    )

    service.reconcile_running()

    assert service.queue.list_specs("failed")
    assert not service.queue.list_specs("done")
    assert service._reserved_attempt_spend_today() == 2
    # submit_authorized leaves the paid_run_unauthorized refusal behind it;
    # the reconciliation reason is the newest file, and ULIDs sort by time.
    reason = sorted(service.queue.reasons_dir.glob("*.json"))[-1].read_text()
    assert "transient_harness:provider_http_5xx" in reason


def test_reconciliation_fails_closed_if_retry_archive_has_no_canonical_job(
    tmp_path: Path,
) -> None:
    service = executor(tmp_path)
    approved = submit_authorized(
        service,
        spec(
            "archive-only-transient-spec",
            task="canary/event-summary",
            agent="codex",
            est_cost_usd=2,
        ),
    )
    queued = service.queue.load(approved)
    service.queue.transition(
        approved,
        "running",
        actor="executor",
        event="dispatch_started",
    )
    service._reserve_attempt(queued, 1)
    archive = tmp_path / queued.jobs_dir / ".transient-attempts" / queued.name / "attempt-1"
    archive.mkdir(parents=True)

    service.reconcile_running()

    assert service.queue.list_specs("failed")
    # submit_authorized leaves the paid_run_unauthorized refusal behind it;
    # the reconciliation reason is the newest file, and ULIDs sort by time.
    reason = sorted(service.queue.reasons_dir.glob("*.json"))[-1].read_text()
    assert "transient_harness:retry_interrupted" in reason


def test_reservation_policy_day_is_utc_at_local_evening_boundary(
    tmp_path: Path,
) -> None:
    service = executor(tmp_path)
    service.queue.append_event(
        QueueEvent(
            event_id="utc-boundary-reservation",
            spec_id="utc-boundary-spec",
            occurred_at=datetime(2026, 8, 15, 0, 30, tzinfo=UTC),
            event="dispatch_attempt_reserved",
            actor="executor",
            estimated_cost_usd=2,
        )
    )

    assert service._reserved_attempt_spend_today(now=datetime(2026, 8, 15, 1, 0, tzinfo=UTC)) == 2
    assert service._reserved_attempt_spend_today(now=datetime(2026, 8, 14, 23, 59, tzinfo=UTC)) == 0


def test_concurrent_executor_tick_defers_to_single_queue_owner(tmp_path: Path) -> None:
    entered = Event()
    release = Event()
    calls: list[str] = []

    def blocking_run(request: RunRequest) -> Path:
        calls.append(request.name)
        entered.set()
        assert release.wait(timeout=2)
        return request.jobs_dir / request.name

    first = executor(tmp_path, runner=blocking_run)
    second = executor(
        tmp_path,
        runner=lambda request: (_ for _ in ()).throw(
            AssertionError(f"second executor ran {request.name}")
        ),
    )
    first.submit(spec("single-owner-control"))

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(first.tick)
        assert entered.wait(timeout=2)
        assert second.tick() == 0
        assert second.last_tick_reason == "executor_busy"
        release.set()
        assert running.result(timeout=2) == 1

    assert calls == ["single-owner-control"]


def test_global_dispatch_capacity_bounds_internal_trial_slots(tmp_path: Path) -> None:
    calls: list[str] = []
    service = executor(
        tmp_path,
        runner=lambda request: calls.append(request.name) or request.jobs_dir / request.name,
        parallel=4,
        capacity=DispatchCapacity(max_specs_per_tick=4, max_active_trials=3),
    )
    for index in range(4):
        service.submit(
            spec(
                f"capacity-{index}",
                attempts=3,
                concurrency=2,
            )
        )

    assert service.tick() == 1
    assert len(calls) == 1
    assert len(service.queue.list_specs("approved")) == 3


def test_max_specs_per_tick_is_independent_of_worker_parallelism(tmp_path: Path) -> None:
    calls: list[str] = []
    service = executor(
        tmp_path,
        runner=lambda request: calls.append(request.name) or request.jobs_dir / request.name,
        parallel=1,
        capacity=DispatchCapacity(max_specs_per_tick=3),
    )
    for index in range(4):
        service.submit(spec(f"batch-limit-{index}"))

    assert service.tick() == 3
    assert len(calls) == 3
    assert len(service.queue.list_specs("approved")) == 1


def test_trial_capacity_without_batch_limit_is_not_clamped_to_parallel(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    service = executor(
        tmp_path,
        runner=lambda request: calls.append(request.name) or request.jobs_dir / request.name,
        parallel=2,
        capacity=DispatchCapacity(max_active_trials=4),
    )
    for index in range(4):
        service.submit(spec(f"trial-capacity-{index}"))

    assert service.tick() == 4
    assert len(calls) == 4
    assert not service.queue.list_specs("approved")


def test_per_agent_capacity_and_oversized_specs_remain_approved(tmp_path: Path) -> None:
    calls: list[str] = []
    service = executor(
        tmp_path,
        runner=lambda request: calls.append(request.name) or request.jobs_dir / request.name,
        parallel=4,
        capacity=DispatchCapacity(
            max_active_trials=4,
            per_agent_active_trials={"oracle": 2, "nop": 2},
        ),
    )
    service.submit(spec("oracle-one", attempts=2, concurrency=2))
    service.submit(spec("oracle-two", attempts=2, concurrency=2))
    service.submit(spec("nop-one", agent="nop", attempts=2, concurrency=2))

    assert service.tick() == 2
    assert set(calls) == {"oracle-one", "nop-one"}
    assert len(service.queue.list_specs("approved")) == 1

    blocked = executor(
        tmp_path / "blocked",
        parallel=2,
        capacity=DispatchCapacity(max_active_trials=1),
    )
    blocked.submit(spec("too-wide", attempts=2, concurrency=2))
    assert blocked.tick() == 0
    assert blocked.last_tick_reason == "capacity_no_approved_spec_fits"
    assert len(blocked.queue.list_specs("approved")) == 1


def test_atomic_lease_acquisition_and_release(tmp_path: Path) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    s = spec("lease-test-control")
    lease_path = queue.lease_path(s)
    assert lease_path.name == "oracle-01TESTLEASETESTCONTR.lease" or lease_path.suffix == ".lease"
    assert not lease_path.exists()

    # First acquisition succeeds
    acquired = queue.acquire_lease(s)
    assert acquired == lease_path
    assert lease_path.is_file()

    # Second concurrent acquisition fails (returns None)
    second = queue.acquire_lease(s)
    assert second is None

    # Release removes the lease file
    assert queue.release_lease(s) is True
    assert not lease_path.exists()

    # Second release returns False
    assert queue.release_lease(s) is False


def test_lease_heartbeat_updates_mtime(tmp_path: Path) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    s = spec("heartbeat-test-control")
    lease_path = queue.acquire_lease(s)
    assert lease_path is not None

    # Set mtime to past
    past = time.time() - 100.0
    os.utime(lease_path, (past, past))
    assert lease_path.stat().st_mtime < time.time() - 50.0

    # Heartbeat touches mtime
    assert queue.heartbeat_lease(s) is True
    assert lease_path.stat().st_mtime >= time.time() - 5.0
    queue.release_lease(s)


def test_stale_lease_is_reclaimed_on_acquire(tmp_path: Path) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    s = spec("stale-reclaim-control")
    lease_path = queue.acquire_lease(s)
    assert lease_path is not None

    # Age the lease beyond stale threshold
    past = time.time() - 400.0
    os.utime(lease_path, (past, past))
    assert queue.is_lease_stale(lease_path, stale_seconds=300.0) is True

    # New acquire on stale lease reclaims it
    reclaimed = queue.acquire_lease(s, stale_seconds=300.0)
    assert reclaimed == lease_path
    assert queue.is_lease_stale(lease_path, stale_seconds=300.0) is False
    queue.release_lease(s)


def test_cancel_marker_survives_failed_claimant_and_stale_reclaim(tmp_path: Path) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    s = spec("cancel-reclaim-control")
    generation = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    lease_path = queue.acquire_lease(s, lease_generation=generation)
    assert lease_path is not None
    assert queue.request_cancel(s) is True
    marker = queue.cancel_path(s, lease_generation=generation)
    assert marker.is_file()

    # A concurrent/failed claimant on the active lease must not erase the marker.
    assert queue.acquire_lease(s, lease_generation="f" * 32) is None
    assert marker.is_file()

    # Age the lease and let a new generation reclaim it; the old generation's
    # cancel marker must survive the reclaim untouched.
    past = time.time() - 400.0
    os.utime(lease_path, (past, past))
    reclaimed = queue.acquire_lease(s, stale_seconds=300.0, lease_generation="e" * 32)
    assert reclaimed == lease_path
    assert marker.is_file()


def test_lease_generation_readers_fail_closed_on_malformed_records(tmp_path: Path) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    s = spec("malformed-generation-control")
    malformed = ["../../x", "A" * 32, "not-a-generation", ""]
    for value in malformed:
        lease_path = queue.acquire_lease(s, lease_generation="f" * 32)
        assert lease_path is not None
        # Tamper the durable lease record after acquisition.
        lease_path.write_text(
            json.dumps({"lease_generation": value}) + "\n",
            encoding="utf-8",
        )
        # Every reader must fail closed: no Path.with_name crash, no traversal.
        assert queue.lease_generation(s) is None
        assert queue.request_cancel(s) is False
        assert queue.release_lease(s, lease_generation="f" * 32) is False
        # Clean up so the next iteration starts fresh.
        lease_path.unlink(missing_ok=True)


def test_runner_read_generation_fails_closed_on_malformed(tmp_path: Path) -> None:
    from evallab.runner import _read_generation

    malformed = ["../../x", "A" * 32, "not-a-generation", ""]
    for value in malformed:
        lease = tmp_path / f"malformed-{len(value)}.lease"
        lease.write_text(
            json.dumps({"lease_generation": value}) + "\n",
            encoding="utf-8",
        )
        assert _read_generation(lease) is None
        # A valid generation still reads back.
        valid = tmp_path / "valid.lease"
        valid.write_text(
            json.dumps({"lease_generation": "d" * 32}) + "\n",
            encoding="utf-8",
        )
        assert _read_generation(valid) == "d" * 32


def test_runner_wrapper_touches_lease_for_fast_process(tmp_path: Path) -> None:
    from evallab.runner import run_harbor_process

    generation = "c" * 32
    lease_path = tmp_path / "test.lease"
    lease_path.write_text(
        json.dumps({"lease_generation": generation}) + "\n",
        encoding="utf-8",
    )
    past_ns = lease_path.stat().st_mtime_ns - 100_000_000_000
    os.utime(lease_path, ns=(past_ns, past_ns))

    result = run_harbor_process(
        ["python3", "-c", "pass"],
        cwd=tmp_path,
        timeout_seconds=5.0,
        log_path=tmp_path / "process.log",
        lease_path=lease_path,
        lease_generation=generation,
        heartbeat_interval_seconds=30.0,
    )

    assert result.returncode == 0
    assert not result.timed_out
    assert lease_path.stat().st_mtime_ns > past_ns


def test_runner_wrapper_keeps_periodic_lease_heartbeat(tmp_path: Path) -> None:
    from evallab.runner import run_harbor_process

    generation = "d" * 32
    lease_path = tmp_path / "test.lease"
    lease_path.write_text(
        json.dumps({"lease_generation": generation}) + "\n",
        encoding="utf-8",
    )
    past_ns = lease_path.stat().st_mtime_ns - 100_000_000_000
    os.utime(lease_path, ns=(past_ns, past_ns))
    observed_mtimes: list[int] = []
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            observed_mtimes.append(lease_path.stat().st_mtime_ns)
            time.sleep(0.005)

    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()

    try:
        result = run_harbor_process(
            ["python3", "-c", "import time; time.sleep(0.3)"],
            cwd=tmp_path,
            timeout_seconds=5.0,
            log_path=tmp_path / "process.log",
            lease_path=lease_path,
            lease_generation=generation,
            heartbeat_interval_seconds=0.02,
        )
    finally:
        stop.set()
        sampler_thread.join()

    assert result.returncode == 0
    assert not result.timed_out
    assert len(set(observed_mtimes)) >= 2


def test_parallel_dispatch_executes_multiple_specs_concurrently(tmp_path: Path) -> None:
    concurrently_running = [0]
    max_concurrent = [0]
    lock = threading.Lock()

    def parallel_run(request: RunRequest) -> SettledRun:
        with lock:
            concurrently_running[0] += 1
            if concurrently_running[0] > max_concurrent[0]:
                max_concurrent[0] = concurrently_running[0]
        time.sleep(0.1)
        with lock:
            concurrently_running[0] -= 1
        dest = request.jobs_dir / request.name
        dest.mkdir(parents=True, exist_ok=True)
        return settled(dest)

    service = executor(tmp_path, runner=parallel_run)
    service.submit(spec("p-spec-1"))
    service.submit(spec("p-spec-2"))
    service.submit(spec("p-spec-3"))

    dispatched = service.tick(parallel=3)
    assert dispatched == 3
    assert max_concurrent[0] >= 2, f"Expected concurrency >= 2, got {max_concurrent[0]}"
    assert len(service.queue.list_specs("done")) == 3
    assert len(service.queue.list_leases()) == 0


def test_parallel_1_compatibility_matches_single_threaded(tmp_path: Path) -> None:
    order: list[str] = []

    def tracking_run(request: RunRequest) -> SettledRun:
        order.append(request.name)
        dest = request.jobs_dir / request.name
        dest.mkdir(parents=True, exist_ok=True)
        return settled(dest)

    service = executor(tmp_path, runner=tracking_run)
    service.submit(spec("seq-spec-1"))
    service.submit(spec("seq-spec-2"))
    service.submit(spec("seq-spec-3"))

    dispatched = service.tick(parallel=1)
    assert dispatched == 3
    assert order == ["seq-spec-1", "seq-spec-2", "seq-spec-3"]
    assert len(service.queue.list_specs("done")) == 3
    assert len(service.queue.list_leases()) == 0


def test_dispatch_preserves_bound_factor_execution_values(tmp_path: Path) -> None:
    requests: list[RunRequest] = []
    service = executor(
        tmp_path,
        runner=lambda request: requests.append(request) or settled(tmp_path),
    )
    item = spec("bound-factor", concurrency=2).model_copy(
        update={
            "timeout_seconds": 60,
            "grid_id": "grid-1",
            "grid_point": {
                "point_id": canonical_grid_point_id(
                    task_ref="library/tasks/event-summary",
                    agent_key="oracle",
                    preamble=None,
                    k=1,
                    arm_id=None,
                    factor_values={"parallelism": 2, "wall_clock": 60},
                    factor_bindings={
                        "parallelism": "concurrency",
                        "wall_clock": "timeout_seconds",
                    },
                ),
                "task_ref": "library/tasks/event-summary",
                "agent": "oracle",
                "preamble": None,
                "k": 1,
                "factors": {"parallelism": 2, "wall_clock": 60},
                "factor_bindings": {
                    "parallelism": "concurrency",
                    "wall_clock": "timeout_seconds",
                },
                "bindings": {"concurrency": 2, "timeout_seconds": 60},
            },
        }
    )

    service.execute_spec(item)

    assert requests[0].concurrency == 2
    assert requests[0].timeout_seconds == 60
    assert requests[0].provenance is not None
    assert requests[0].provenance.factor_bindings == {
        "parallelism": "concurrency",
        "wall_clock": "timeout_seconds",
    }


def test_dispatch_refuses_unhonored_and_tampered_factor_bindings(tmp_path: Path) -> None:
    service = executor(tmp_path)
    unhonored = spec("unhonored-factor").model_copy(
        update={
            "timeout_seconds": 120,
            "grid_point": {
                "factors": {"wall_clock": 60},
                "factor_bindings": {"wall_clock": "timeout_seconds"},
                "bindings": {"timeout_seconds": 60},
            },
        }
    )
    with pytest.raises(ExecutionFailure, match="requested 60"):
        service.execute_spec(unhonored)

    tampered = spec("tampered-factor").model_copy(
        update={
            "timeout_seconds": 60,
            "grid_point": {
                "factors": {"wall_clock": 60},
                "factor_bindings": {"wall_clock": "concurrency"},
                "bindings": {"timeout_seconds": 60},
            },
        }
    )
    with pytest.raises(ExecutionFailure, match="does not match bound execution"):
        service.execute_spec(tampered)


def test_dispatch_refuses_stale_point_identity_after_consistent_edit(
    tmp_path: Path,
) -> None:
    service = executor(tmp_path)
    stale_point = canonical_grid_point_id(
        task_ref="library/tasks/event-summary",
        agent_key="oracle",
        preamble=None,
        k=1,
        arm_id=None,
        factor_values={"wall_clock": 60},
        factor_bindings={"wall_clock": "timeout_seconds"},
    )
    consistently_edited = spec("stale-point").model_copy(
        update={
            "timeout_seconds": 120,
            "grid_point": {
                "point_id": stale_point,
                "task_ref": "library/tasks/event-summary",
                "agent": "oracle",
                "preamble": None,
                "k": 1,
                "factors": {"wall_clock": 120},
                "factor_bindings": {"wall_clock": "timeout_seconds"},
                "bindings": {"timeout_seconds": 120},
            },
        }
    )
    with pytest.raises(ExecutionFailure, match="stored point_id"):
        service.execute_spec(consistently_edited)


def test_dispatch_refuses_preamble_digest_mismatch(tmp_path: Path) -> None:
    preamble = tmp_path / "instructions.txt"
    preamble.write_text("changed after generation\n")
    item = spec("preamble-drift").model_copy(
        update={
            "extra_instruction_path": "instructions.txt",
            "extra_instruction_sha256": "sha256:" + "0" * 64,
        }
    )

    with pytest.raises(ExecutionFailure, match="no longer matches"):
        executor(tmp_path).execute_spec(item)


def test_control_bootstrap_atomic_publication_and_conflict_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2 adversary: control bootstrap publication is atomic, fail-clean, and refuses destination overwrite."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "queue").mkdir()
    (root / "policy").mkdir()
    (root / "policy/standing-approvals.yaml").write_text("version: 1\n")
    (root / "research/evidence/runs").mkdir(parents=True)

    # 1. Create a valid settled control job
    source_job = tmp_path / "raw-control-job"
    trial_dir = source_job / "trial-1"
    trial_dir.mkdir(parents=True)
    (trial_dir / "result.json").write_text(
        json.dumps({"task_name": "task", "trial_name": "trial-1"}), encoding="utf-8"
    )
    (source_job / "result.json").write_text(
        json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": "2026-08-25T12:00:00Z"}),
        encoding="utf-8",
    )

    store = root / "cas"
    archive = archive_evidence(source_job, store, kind="job", record_id="bootstrap-spec")
    locator = evidence_locator(store, archive)
    settled_run = SettledRun(cas_locator=locator, cas_record=archive)

    service = executor(root)
    control_spec = spec("bootstrap-spec", agent="oracle", task="registered/task-1")

    # First promotion succeeds
    dest = service._promote_control_bootstrap_job(settled_run, control_spec)
    assert dest.is_dir()
    assert (dest / "result.json").is_file()
    # Ensure no staging directory leaked in research/evidence/runs
    assert not list((root / "research/evidence/runs").glob(".staging-*"))

    # Conflict refusal: destination already exists -> must raise control_bootstrap_job_conflict without overwrite
    with pytest.raises(
        ExecutionFailure, match="durable control-bootstrap job destination already exists"
    ):
        service._promote_control_bootstrap_job(settled_run, control_spec)

    # Failure injection: if validation fails, staging directory must be cleaned up and no partial directory left
    fail_spec = spec("fail-bootstrap-spec", agent="oracle", task="registered/task-1")
    monkeypatch.setattr(
        service,
        "_assert_persistent_artifacts_safe",
        lambda _spec, _dir: (_ for _ in ()).throw(ValueError("injected validation failure")),
    )
    with pytest.raises(ValueError, match="injected validation failure"):
        service._promote_control_bootstrap_job(settled_run, fail_spec)
    assert not (root / "research/evidence/runs/fail-bootstrap-spec").exists()
    assert not list((root / "research/evidence/runs").glob(".staging-*"))


def test_control_bootstrap_refuses_broken_in_root_destination_symlink(
    tmp_path: Path,
) -> None:
    """B2a adversary: broken in-root destination symlink is refused as conflict without publishing under alias."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "queue").mkdir()
    (root / "policy").mkdir()
    (root / "policy/standing-approvals.yaml").write_text("version: 1\n")
    runs_dir = root / "research/evidence/runs"
    runs_dir.mkdir(parents=True)

    source_job = tmp_path / "raw-control-job"
    trial_dir = source_job / "trial-1"
    trial_dir.mkdir(parents=True)
    (trial_dir / "result.json").write_text(
        json.dumps({"task_name": "task", "trial_name": "trial-1"}), encoding="utf-8"
    )
    (source_job / "result.json").write_text(
        json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": "2026-08-25T12:00:00Z"}),
        encoding="utf-8",
    )

    store = root / "cas"
    archive = archive_evidence(source_job, store, kind="job", record_id="symlink-dest-spec")
    locator = evidence_locator(store, archive)
    settled_run = SettledRun(cas_locator=locator, cas_record=archive)

    # Create broken symlink at destination
    symlink_path = runs_dir / "symlink-dest-spec"
    target_path = runs_dir / "nonexistent-alias-target"
    symlink_path.symlink_to(target_path)

    service = executor(root)
    control_spec = spec("symlink-dest-spec", agent="oracle", task="registered/task-1")

    with pytest.raises(ExecutionFailure) as exc_info:
        service._promote_control_bootstrap_job(settled_run, control_spec)
    assert exc_info.value.reason_code == "control_bootstrap_job_conflict"

    # Destination alias must NOT have been created!
    assert not target_path.exists()
    assert not list(runs_dir.glob(".staging-*"))


def test_control_bootstrap_refuses_symlinked_durable_root(
    tmp_path: Path,
) -> None:
    """B2a adversary: symlinked durable root is refused fail-closed."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "queue").mkdir()
    (root / "policy").mkdir()
    (root / "policy/standing-approvals.yaml").write_text("version: 1\n")
    (root / "research/evidence").mkdir(parents=True)

    real_runs = tmp_path / "real_runs"
    real_runs.mkdir()
    symlinked_runs = root / "research/evidence/runs"
    symlinked_runs.symlink_to(real_runs)

    source_job = tmp_path / "raw-control-job"
    trial_dir = source_job / "trial-1"
    trial_dir.mkdir(parents=True)
    (trial_dir / "result.json").write_text(
        json.dumps({"task_name": "task", "trial_name": "trial-1"}), encoding="utf-8"
    )
    (source_job / "result.json").write_text(
        json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": "2026-08-25T12:00:00Z"}),
        encoding="utf-8",
    )

    store = root / "cas"
    archive = archive_evidence(source_job, store, kind="job", record_id="symlink-root-spec")
    locator = evidence_locator(store, archive)
    settled_run = SettledRun(cas_locator=locator, cas_record=archive)

    service = executor(root)
    control_spec = spec("symlink-root-spec", agent="oracle", task="registered/task-1")

    with pytest.raises(ExecutionFailure) as exc_info:
        service._promote_control_bootstrap_job(settled_run, control_spec)
    assert exc_info.value.reason_code == "symlink_rejected"


def test_control_bootstrap_refuses_post_validation_staging_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2b adversary: mutating staged bytes after validation fails reauthentication with no publication."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "queue").mkdir()
    (root / "policy").mkdir()
    (root / "policy/standing-approvals.yaml").write_text("version: 1\n")
    runs_dir = root / "research/evidence/runs"
    runs_dir.mkdir(parents=True)

    source_job = tmp_path / "raw-control-job"
    trial_dir = source_job / "trial-1"
    trial_dir.mkdir(parents=True)
    (trial_dir / "result.json").write_text(
        json.dumps({"task_name": "task", "trial_name": "trial-1"}), encoding="utf-8"
    )
    (source_job / "result.json").write_text(
        json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": "2026-08-25T12:00:00Z"}),
        encoding="utf-8",
    )

    store = root / "cas"
    archive = archive_evidence(source_job, store, kind="job", record_id="post-val-spec")
    locator = evidence_locator(store, archive)
    settled_run = SettledRun(cas_locator=locator, cas_record=archive)

    service = executor(root)
    control_spec = spec("post-val-spec", agent="oracle", task="registered/task-1")

    def mutating_validation(_spec, staging_path: Path) -> None:
        # Mutate a file in staging AFTER validation completes
        (staging_path / "result.json").write_text("TAMPERED_AFTER_VALIDATION\n")

    monkeypatch.setattr(service, "_assert_persistent_artifacts_safe", mutating_validation)

    with pytest.raises(ExecutionFailure) as exc_info:
        service._promote_control_bootstrap_job(settled_run, control_spec)
    assert exc_info.value.reason_code == "staged_evidence_tampered"

    assert not (runs_dir / "post-val-spec").exists()
    assert not list(runs_dir.glob(".staging-*"))
