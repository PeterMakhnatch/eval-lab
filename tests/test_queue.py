from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from evallab.credentials import CLAUDE_OAUTH, CODEX_AUTH
from evallab.queue import DirectoryQueue, Executor, PolicyGate, load_events
from evallab.runner import RunRequest, TransientHarnessFailure, TrialTimeoutFailure
from evallab.schemas import AutoRunRule, ExperimentSpec, StandingApprovalsPolicy


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
) -> ExperimentSpec:
    return ExperimentSpec(
        name=name,
        hypothesis="exercise the queue state machine",
        task=task,
        task_path="library/tasks/event-summary" if task.startswith("canary/") else None,
        agent=agent,
        model=model,
        submitted_by="test-agent",
        est_cost_usd=est_cost_usd,
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
) -> Executor:
    return Executor(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=policy(),
        runner=runner or (lambda request: request.jobs_dir / request.name),
        ingester=ingester or (lambda path: None),
        spent_today=lambda: spent,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: credentials
        if credentials is not None
        else frozenset({"claude_oauth", "codex_auth"}),
        sleeper=sleeper,
        max_transient_retries=max_transient_retries,
    )


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

    path, decision = service.submit(
        spec("manual-model-run", agent="other-agent", model="provider/model", est_cost_usd=1)
    )

    assert path.parent.name == "waiting"
    assert decision.reason_code == "out_of_policy"
    reason_files = list((tmp_path / "queue/reasons").glob("*.json"))
    assert len(reason_files) == 1
    assert '"code": "out_of_policy"' in reason_files[0].read_text()


def test_spec_past_ceiling_is_refused_with_reason_file(tmp_path: Path) -> None:
    service = executor(tmp_path, spent=19.5)

    path, decision = service.submit(
        spec(
            "canary-over-daily-ceiling",
            task="canary/event-summary",
            agent="codex",
            model="openai/example",
            est_cost_usd=1,
        )
    )

    assert path.parent.name == "waiting"
    assert decision.reason_code == "daily_cost_ceiling"
    reason = next((tmp_path / "queue/reasons").glob("*.json"))
    assert "daily_cost_ceiling" in reason.read_text()


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

    def run(request):
        requests.append(request)
        destination = request.jobs_dir / request.name
        destination.mkdir(parents=True)
        return destination

    service = executor(tmp_path, runner=run, ingester=ingested.append)
    approved, _ = service.submit(spec("completed-oracle-control"))
    queued = service.queue.load(approved)

    assert service.tick() == 1
    assert len(requests) == 1
    assert ingested == [tmp_path / "runs/completed-oracle-control"]
    assert service.queue.locate(str(queued.spec_id), ("done",)).parent.name == "done"
    events = [event.event for event in load_events(service.queue.events_path)]
    assert events == [
        "submitted",
        "policy_admitted",
        "dispatch_started",
        "dispatch_completed",
    ]


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
    billable = spec(
        "canary-after-failures",
        task="canary/event-summary",
        agent="codex",
        model="openai/example",
        est_cost_usd=1,
    )

    decision = gate.decide(
        billable,
        spent_today_usd=0,
        consecutive_harness_failures=3,
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
    _, decision = service.submit(spec("codex-blocked", agent="codex", task="canary/event-summary"))
    assert decision.admitted
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
    decisions = [service.submit(item)[1] for item in submissions]

    assert all(decision.admitted for decision in decisions)
    assert service.tick() == 3

    expected_dispatched = {
        item.name for item in submissions if item.agent != missing_agent
    }
    assert {request.name for request in requests} == expected_dispatched
    approved = [item for _, item in service.queue.list_specs("approved")]
    assert [(item.name, item.agent) for item in approved] == [
        (
            "credential-scope-codex"
            if missing_agent == "codex"
            else "credential-scope-claude",
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
    service.submit(spec("codex-default-model", agent="codex", task="canary/event-summary"))
    service.submit(
        spec("codex-pinned-model", agent="codex", task="canary/event-summary", model="pinned-x")
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

    def run(request: RunRequest) -> Path:
        calls.append(request.name)
        destination = request.jobs_dir / request.name
        destination.mkdir(parents=True)
        (destination / "attempt.txt").write_text(str(len(calls)))
        if len(calls) < 3:
            raise TransientHarnessFailure("transient_harness:provider_http_429")
        return destination

    service = executor(tmp_path, runner=run, sleeper=sleeps.append)
    service.submit(spec("provider-recovers"))

    assert service.tick() == 1
    assert calls == ["provider-recovers"] * 3
    assert sleeps == [5.0, 10.0]
    assert (
        tmp_path
        / "runs/.transient-attempts/provider-recovers/attempt-1/attempt.txt"
    ).read_text() == "1"
    assert (
        tmp_path
        / "runs/.transient-attempts/provider-recovers/attempt-2/attempt.txt"
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
    submitted = spec(
        "budgeted-provider-retry",
        task="canary/event-summary",
        agent="codex",
        est_cost_usd=2,
    )
    service.submit(submitted)

    assert service.tick() == 1
    assert calls == 1
    refused = [
        event
        for event in load_events(service.queue.events_path)
        if event.event == "dispatch_retry_refused"
    ]
    assert [event.reason_code for event in refused] == [
        "transient_retry:daily_cost_ceiling"
    ]
