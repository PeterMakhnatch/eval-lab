"""Adversarial cover for the paid-execution authorization gate.

Every test here fails if a future edit lets a billable agent reach Harbor
without a human authorisation recorded in the append-only queue event log.
Read `docs/operations.md`, "Paid execution requires a recorded authorisation".
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from evallab.automation import NightlyCycle
from evallab.canary import CanaryEnqueuer, task_directory_digest
from evallab.digest import DigestRenderer
from evallab.queue import (
    DirectoryQueue,
    Executor,
    PolicyGate,
    new_ulid,
    standing_rule_admits,
)
from evallab.runner import TransientHarnessFailure
from evallab.schemas import (
    AutoRunRule,
    CanaryMember,
    CanarySuite,
    ExperimentSpec,
    HeadlessDoctorChecks,
    HeadlessDoctorReport,
    QueueEvent,
    StandingApprovalsPolicy,
)

ROOT = Path(__file__).resolve().parents[1]


def permissive_policy() -> StandingApprovalsPolicy:
    """The policy exactly as it was when nine paid Codex sessions ran per night.

    These tests deliberately hand the gate the *loosest* standing policy the
    repository has ever carried. If admission still depends on the policy file,
    they dispatch; the point is that they must not.
    """
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
            AutoRunRule(
                name="researcher-followups",
                tasks=["registered/*"],
                agents=["codex", "claude-code"],
                max_attempts=5,
            ),
        ],
        escalate_to_human=["anything_exceeding_ceilings"],
    )


def spec(
    name: str,
    *,
    agent: str = "codex",
    task: str = "canary/event-summary",
    est_cost_usd: float = 1.0,
    **overrides: object,
) -> ExperimentSpec:
    return ExperimentSpec(
        name=name,
        hypothesis="exercise the paid-authorization gate",
        purpose="practice",
        task=task,
        task_path="library/tasks/event-summary" if task.startswith("canary/") else None,
        agent=agent,
        submitted_by="test-agent",
        est_cost_usd=est_cost_usd,
        **overrides,  # type: ignore[arg-type]
    )


def executor(root: Path, requests: list) -> Executor:
    def runner(request):
        requests.append(request)
        destination = request.jobs_dir / request.name
        destination.mkdir(parents=True, exist_ok=True)
        return destination

    return Executor(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=permissive_policy(),
        runner=runner,
        ingester=lambda path: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: frozenset({"claude_oauth", "codex_auth"}),
        sleeper=lambda _seconds: None,
    )


def reasons_for(queue: DirectoryQueue, spec_id: str) -> list[dict]:
    return [
        json.loads(path.read_text())
        for path in sorted(queue.reasons_dir.glob(f"{spec_id}-*.json"))
    ]


# --- 1. a billable spec with no authorisation is refused -------------------


def test_billable_spec_without_authorization_is_parked_not_approved(tmp_path: Path) -> None:
    service = executor(tmp_path, [])

    path, decision = service.submit(spec("unauthorized-codex"))

    assert not decision.admitted
    assert decision.reason_code == "paid_run_unauthorized"
    assert path.parent.name == "waiting"


def test_refusal_names_the_agent_and_the_exact_next_command(tmp_path: Path) -> None:
    service = executor(tmp_path, [])

    path, decision = service.submit(spec("legible-refusal"))
    spec_id = str(service.queue.load(path).spec_id)

    assert "codex" in decision.message
    assert f"uv run evallab approve {spec_id} --actor" in decision.message
    assert f"uv run evallab reject {spec_id} --actor" in decision.message
    assert "oracle" in decision.message and "nop" in decision.message
    # The same guidance has to survive into queue/reasons/, because that is
    # where an operator looks the morning after an unattended refusal.
    assert any(
        "uv run evallab approve" in reason["message"]
        for reason in reasons_for(service.queue, spec_id)
    )


def test_unauthorized_billable_spec_never_reaches_harbor(tmp_path: Path) -> None:
    requests: list = []
    service = executor(tmp_path, requests)
    service.submit(spec("no-dispatch-codex"))

    assert service.tick() == 0
    assert requests == []


# --- 2. the same spec with authorisation is admitted -----------------------


def test_recorded_authorization_admits_and_dispatches_the_same_spec(tmp_path: Path) -> None:
    requests: list = []
    service = executor(tmp_path, requests)
    path, refusal = service.submit(spec("authorized-codex"))
    spec_id = str(service.queue.load(path).spec_id)
    assert not refusal.admitted

    service.queue.approve(spec_id, actor="peter")
    dispatched = service.tick()

    assert dispatched == 1
    assert [request.agent for request in requests] == ["codex"]
    assert requests[0].provenance.policy_rule == "human-approval"


def test_authorization_does_not_lift_the_per_job_cost_ceiling(tmp_path: Path) -> None:
    requests: list = []
    service = executor(tmp_path, requests)
    path, _ = service.submit(spec("expensive-codex", est_cost_usd=9.0))
    spec_id = str(service.queue.load(path).spec_id)

    service.queue.approve(spec_id, actor="peter")

    assert service.tick() == 0
    assert requests == []
    assert reasons_for(service.queue, spec_id)[-1]["code"] == "per_job_cost_ceiling"


def test_rejecting_an_authorized_spec_withdraws_the_authorization(tmp_path: Path) -> None:
    service = executor(tmp_path, [])
    path, _ = service.submit(spec("withdrawn-codex"))
    spec_id = str(service.queue.load(path).spec_id)
    service.queue.approve(spec_id, actor="peter")
    service.queue.reject(spec_id, actor="peter", message="changed my mind")

    assert service.queue.authorizations() == {}


# --- 3. free controls are unaffected ---------------------------------------


@pytest.mark.parametrize("agent", ["oracle", "nop"])
def test_free_controls_still_run_unattended(tmp_path: Path, agent: str) -> None:
    requests: list = []
    service = executor(tmp_path, requests)

    path, decision = service.submit(
        spec(f"free-{agent}", agent=agent, task="library/tasks/event-summary", est_cost_usd=0)
    )

    assert decision.admitted
    assert decision.policy_rule == "local-controls"
    assert path.parent.name == "approved"
    assert service.tick() == 1
    assert [request.agent for request in requests] == [agent]
    assert service.queue.authorizations() == {}


# --- 4. the nightly cycle cannot bypass the check --------------------------


class StaticDoctor:
    def run(self) -> HeadlessDoctorReport:
        return HeadlessDoctorReport(
            checked_at=datetime.now(UTC),
            healthy=True,
            checks=HeadlessDoctorChecks(
                keychain_readable=True,
                codex_auth_present=True,
                docker_reachable=True,
                postgres_reachable=True,
                disk_headroom=True,
            ),
        )


def canary_suite(root: Path) -> CanarySuite:
    members = []
    for index in range(3):
        task = root / f"library/tasks/canary-{index}"
        task.mkdir(parents=True)
        (task / "task.toml").write_text(f'name = "test/canary-{index}"\n')
        members.append(
            CanaryMember(
                name=f"fixture-{index}",
                task_path=f"library/tasks/canary-{index}",
                task_version="1.0.0",
                task_digest=task_directory_digest(task),
                source_ref=f"test/canary-{index}@1",
                est_cost_usd=1,
            )
        )
    return CanarySuite(agents=["codex"], members=members)


def test_nightly_cycle_enqueues_paid_canaries_but_dispatches_none(tmp_path: Path) -> None:
    """The exact defect: nine paid Codex sessions a night, unattended."""
    requests: list = []
    service = executor(tmp_path, requests)
    enqueuer = CanaryEnqueuer(
        repo_root=tmp_path, executor=service, suite=canary_suite(tmp_path)
    )
    cycle = NightlyCycle(
        doctor=StaticDoctor(),  # type: ignore[arg-type]
        executor=service,
        renderer=DigestRenderer(
            repo_root=tmp_path,
            queue=service.queue,
            policy=permissive_policy(),
            trial_loader=lambda day: [],
            drift_loader=lambda day: [],
        ),
        committer=lambda path: True,
        canary_enqueuer=enqueuer.enqueue,
    )

    first = cycle.run(report_date=date(2026, 8, 14))
    second = cycle.run(report_date=date(2026, 8, 15))

    assert first.enqueued == second.enqueued == 3
    assert first.dispatched == second.dispatched == 0
    assert requests == []
    assert list(service.queue.state_dir("approved").glob("*.json")) == []
    waiting = service.queue.list_specs("waiting")
    assert len(waiting) == 6
    assert all(
        reasons_for(service.queue, str(item.spec_id))[-1]["code"] == "paid_run_unauthorized"
        for _path, item in waiting
    )


# --- 5. forgery and replay --------------------------------------------------


def test_a_spec_file_cannot_authorize_itself(tmp_path: Path) -> None:
    """`policy_rule` lives in a file the automation writes; it is not a record.

    This reproduces the pre-fix trust path: the executor used to read
    `spec.policy_rule == "human-approval"` straight off the queued artifact.
    """
    requests: list = []
    service = executor(tmp_path, requests)
    forged = spec("self-approved-codex").model_copy(
        update={
            "spec_id": "01SELFAPPROVED0000000000AA",
            "policy_rule": "human-approval",
            "submitted_at": datetime.now(UTC),
        }
    )
    destination = service.queue.state_dir("approved") / "codex-01SELFAPPROVED0000000000AA.json"
    destination.write_text(forged.model_dump_json(indent=2, exclude_none=True))

    assert service.tick() == 0
    assert requests == []
    assert [item.spec_id for _path, item in service.queue.list_specs("waiting")] == [
        "01SELFAPPROVED0000000000AA"
    ]


def test_an_authorization_cannot_be_replayed_by_a_later_spec_reusing_its_id(
    tmp_path: Path,
) -> None:
    """A spec id is a name, not a bearer token: reuse does not inherit consent."""
    requests: list = []
    service = executor(tmp_path, requests)
    reused_id = "01REPLAYEDSPECID000000000B"
    first, _ = service.submit(spec("replay-first", spec_id=reused_id))
    service.queue.approve(reused_id, actor="peter")
    assert service.tick() == 1
    for state in ("done", "failed", "running"):
        for path in service.queue.state_dir(state).glob("*.json"):
            path.unlink()
    requests.clear()

    replayed = spec("replay-second", spec_id=reused_id).model_copy(
        update={"submitted_at": datetime.now(UTC) + timedelta(seconds=1)}
    )
    (service.queue.state_dir("approved") / first.name).write_text(
        replayed.model_dump_json(indent=2, exclude_none=True)
    )

    assert service.tick() == 0
    assert requests == []
    assert (
        reasons_for(service.queue, reused_id)[-1]["code"] == "paid_run_authorization_stale"
    )


def test_an_authorization_for_one_spec_does_not_cover_another(tmp_path: Path) -> None:
    service = executor(tmp_path, [])
    authorized, _ = service.submit(spec("covered-codex"))
    other, _ = service.submit(spec("uncovered-codex"))
    authorized_id = str(service.queue.load(authorized).spec_id)
    other_spec = service.queue.load(other)
    service.queue.approve(authorized_id, actor="peter")

    decision = service.gate.decide(
        other_spec,
        spent_today_usd=0,
        authorization=service.queue.authorizations()[authorized_id],
    )

    assert not decision.admitted
    assert decision.reason_code == "paid_run_authorization_mismatch"


# --- 6. structural invariants a future edit must trip over ------------------


def test_no_standing_rule_can_admit_a_billable_agent() -> None:
    """`auto_run` is unreachable for paid work, whatever the policy file says."""
    paid_rule = AutoRunRule(
        name="canary", tasks=["canary/*"], agents=["codex", "claude-code"], max_attempts=3
    )

    assert not standing_rule_admits(paid_rule, spec("would-be-canary", agent="codex"))
    assert not standing_rule_admits(paid_rule, spec("would-be-claude", agent="claude-code"))
    assert standing_rule_admits(
        AutoRunRule(name="local-controls", agents=["oracle", "nop"]),
        spec("control", agent="oracle", task="library/tasks/event-summary", est_cost_usd=0),
    )


def test_committed_policy_lists_no_billable_agent_under_auto_run() -> None:
    raw = yaml.safe_load((ROOT / "policy/standing-approvals.yaml").read_text())
    policy = StandingApprovalsPolicy.model_validate(raw)

    listed = {agent for rule in policy.auto_run for agent in rule.agents}

    assert listed <= {"oracle", "nop"}, (
        f"{sorted(listed - {'oracle', 'nop'})} would claim standing approval to spend"
    )


def test_the_gate_refuses_a_billable_spec_that_carries_no_submission_time() -> None:
    gate = PolicyGate(permissive_policy())

    decision = gate.decide(
        spec("undated-codex").model_copy(update={"spec_id": "01UNDATED0000000000000000"}),
        spent_today_usd=0,
    )

    assert not decision.admitted
    assert decision.reason_code == "paid_run_unauthorized"


# --- 7. fail closed when authorisation cannot be determined -----------------


def test_tick_dispatches_nothing_when_the_authorization_ledger_is_unreadable(
    tmp_path: Path,
) -> None:
    requests: list = []
    service = executor(tmp_path, requests)
    path, _ = service.submit(spec("ledger-corrupt-codex"))
    spec_id = str(service.queue.load(path).spec_id)
    service.queue.approve(spec_id, actor="peter")
    with service.queue.events_path.open("a") as handle:
        handle.write('{"event": "not-a-valid-queue-event"}\n')

    assert service.tick() == 0
    assert requests == []
    assert service.last_tick_reason == "authorization_ledger_unreadable"


def test_free_controls_are_also_held_when_the_ledger_is_unreadable(tmp_path: Path) -> None:
    """Fail closed means closed: an unreadable ledger stops the whole tick."""
    requests: list = []
    service = executor(tmp_path, requests)
    service.submit(
        spec("free-during-corruption", agent="oracle", task="library/tasks/event-summary")
    )
    with service.queue.events_path.open("a") as handle:
        handle.write("{ not json\n")

    assert service.tick() == 0
    assert requests == []


# --- 8. the retry path is covered too --------------------------------------


def test_transient_retry_stops_once_the_authorization_is_withdrawn(tmp_path: Path) -> None:
    attempts: list = []

    def runner(request):
        attempts.append(request)
        if len(attempts) == 1:
            service.queue.append_event(
                QueueEvent(
                    event_id=new_ulid(),
                    spec_id=str(request.provenance.spec_id),
                    occurred_at=datetime.now(UTC),
                    event="human_rejected",
                    actor="peter",
                    reason_code="human_rejected",
                )
            )
        raise TransientHarnessFailure("provider_capacity", "capacity")

    service = Executor(
        repo_root=tmp_path,
        queue=DirectoryQueue(tmp_path / "queue"),
        policy=permissive_policy(),
        runner=runner,
        ingester=lambda path: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: frozenset({"codex_auth"}),
        sleeper=lambda _seconds: None,
    )
    path, _ = service.submit(spec("retry-withdrawn"))
    service.queue.approve(str(service.queue.load(path).spec_id), actor="peter")

    service.tick()

    assert len(attempts) == 1
