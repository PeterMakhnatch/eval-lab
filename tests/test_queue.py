from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evallab.queue import DirectoryQueue, Executor, PolicyGate, load_events
from evallab.runner import RunRequest
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
