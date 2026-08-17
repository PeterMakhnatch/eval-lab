"""Cover for the subscription quota shown and enforced at authorisation time.

`src/evallab/quota.py` measures what remains on the subscription; PR #65 made a
paid run require a named human authorisation. These tests cover the link: the
human authorising a paid run is told the allowance state, and the *provider's
own* statement that the allowance is gone refuses dispatch.

Two traps from `docs/quota-accounting.md` are the point of this file:

1. `headroom.availability` must be read before any percentage. An unavailable
   reading carries `None` in every numeric field, and `None` read as "plenty
   left" is the original defect in a new unit.
2. A window count from `since()` is a lower bound, so nothing here counts
   trials.

Read `docs/operations.md`, "What the quota gate does and does not decide".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evallab import queue as queue_module
from evallab.queue import (
    DirectoryQueue,
    Executor,
    PolicyGate,
    lab_threshold_reached,
    provider_reported_exhaustion,
    quota_window_expired,
    render_headroom_notice,
)
from evallab.quota import Headroom
from evallab.schemas import AutoRunRule, ExperimentSpec, StandingApprovalsPolicy

OBSERVED_AT = datetime(2026, 8, 16, 14, 0, 31, tzinfo=UTC)
RESETS_AT = datetime(2026, 8, 20, 18, 32, 49, tzinfo=UTC)

#: The live reading on the day this gate was written, staleness included.
LIVE_STALENESS_SECONDS = 5 * 3600 + 18 * 60


def policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
        escalate_to_human=["any_billable_agent"],
    )


def headroom(
    *,
    used_percent: float | None = 92.0,
    availability: str = "observed",
    rate_limit_reached_type: str | None = None,
    staleness_seconds: float | None = 60.0,
    resets_at: datetime | None = RESETS_AT,
    hard_stop: bool | None = True,
    reason: str | None = None,
    remaining_percent: float | None = None,
) -> Headroom:
    """A reading shaped exactly like the one `quota.py` builds from a rollout."""
    if availability != "observed":
        return Headroom(
            availability="unavailable",
            reason=reason or "no paid trial recorded a provider quota snapshot",
            used_percent=used_percent if reason == "poisoned" else None,
            remaining_percent=remaining_percent,
        )
    return Headroom(
        availability="observed",
        used_percent=used_percent,
        remaining_percent=(
            remaining_percent
            if remaining_percent is not None
            else (None if used_percent is None else max(0.0, 100.0 - used_percent))
        ),
        window_minutes=10080,
        observed_at=OBSERVED_AT,
        resets_at=resets_at,
        staleness_seconds=staleness_seconds,
        plan_type="prolite",
        limit_id="codex",
        has_credits=False,
        credits_unlimited=False,
        credits_balance="0",
        rate_limit_reached_type=rate_limit_reached_type,
        source="runs/job/trial/agent/sessions/2026/08/16/rollout-x.jsonl",
        hard_stop=hard_stop,
    )


def spec(
    name: str = "quota-gate-probe",
    *,
    agent: str = "codex",
    est_cost_usd: float = 1.0,
    **overrides: object,
) -> ExperimentSpec:
    return ExperimentSpec(
        name=name,
        hypothesis="exercise the quota gate",
        task="canary/event-summary",
        task_path="library/tasks/event-summary",
        agent=agent,
        submitted_by="test-agent",
        est_cost_usd=est_cost_usd,
        **overrides,  # type: ignore[arg-type]
    )


def executor(root: Path, requests: list, reading: Headroom) -> Executor:
    def runner(request):
        destination = request.jobs_dir / request.name
        destination.mkdir(parents=True, exist_ok=True)
        requests.append(request)
        return destination

    return Executor(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=policy(),
        runner=runner,
        ingester=lambda path: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: frozenset({"claude_oauth", "codex_auth"}),
        headroom=lambda: reading,
        sleeper=lambda _seconds: None,
    )


def authorized_dispatch(root: Path, reading: Headroom, **spec_overrides: object):
    """Submit one billable spec, authorise it, and tick. Returns the executor."""
    requests: list = []
    service = executor(root, requests, reading)
    path, _ = service.submit(spec(**spec_overrides))  # type: ignore[arg-type]
    spec_id = str(service.queue.load(path).spec_id)
    service.queue.approve(spec_id, actor="peter")
    service.tick()
    return service, spec_id, requests


def reasons_for(queue: DirectoryQueue, spec_id: str) -> list[dict]:
    return [
        json.loads(path.read_text())
        for path in sorted(queue.reasons_dir.glob(f"{spec_id}-*.json"))
    ]


# --- trap 1: an unavailable reading is never headroom -----------------------


def test_an_unavailable_reading_never_renders_a_percentage() -> None:
    """The numeric fields are `None`; nothing may print them as a figure.

    Deliberately poisoned: `availability` says unavailable while a stray
    `remaining_percent` of 99 sits in the object. Reading the number instead of
    the availability is exactly the defect being closed.
    """
    poisoned = Headroom(
        availability="unavailable",
        reason="no paid trial recorded a provider quota snapshot",
        used_percent=1.0,
        remaining_percent=99.0,
    )
    notice = render_headroom_notice(poisoned)
    assert "UNKNOWN" in notice
    assert "99" not in notice
    assert "remaining_percent" not in notice
    assert "not 'plenty left'" in notice


def test_an_unavailable_reading_is_not_evidence_of_exhaustion_either() -> None:
    """It refuses nothing — and it reassures nobody. Both halves matter."""
    unknown = headroom(availability="unavailable")
    assert provider_reported_exhaustion(unknown) is None
    assert lab_threshold_reached(unknown) is None
    assert quota_window_expired(unknown) is False


def test_an_unavailable_reading_does_not_silently_permit(tmp_path: Path) -> None:
    """A permit against an unmeasurable allowance must announce itself.

    Refusing outright would be a bootstrap deadlock: the reading exists only
    because a paid trial wrote it, so a checkout with no paid history could
    never run its first paid trial. The control is that the permit is loud and
    reaches a named human, not that it is silent.
    """
    service, spec_id, requests = authorized_dispatch(
        tmp_path, headroom(availability="unavailable")
    )
    assert len(requests) == 1
    decision = service.gate.decide(
        spec(submitted_at=datetime(2026, 8, 16, tzinfo=UTC), spec_id=spec_id),
        spent_today_usd=0,
        authorization=service.queue.authorizations()[spec_id],
    )
    assert decision.admitted
    assert "UNKNOWN" in decision.message
    assert "not 'plenty left'" in decision.message


def test_a_failing_quota_reader_is_unavailable_not_permissive() -> None:
    """An exception while scanning must not become an implied clean bill."""

    def broken() -> Headroom:
        raise OSError("runs/ is not readable")

    gate = PolicyGate(policy(), headroom=broken)
    reading = gate.headroom()
    assert reading.availability == "unavailable"
    assert "OSError" in str(reading.reason)
    assert reading.remaining_percent is None
    assert "UNKNOWN" in render_headroom_notice(reading)


def test_a_gate_with_no_reader_says_so_rather_than_reporting_zero_use() -> None:
    gate = PolicyGate(policy())
    reading = gate.headroom()
    assert reading.availability == "unavailable"
    assert reading.used_percent is None
    assert "never looked up" in str(reading.reason)


# --- the provider's own statement of exhaustion -----------------------------


def test_provider_reported_exhaustion_refuses_a_billable_dispatch(tmp_path: Path) -> None:
    service, spec_id, requests = authorized_dispatch(tmp_path, headroom(used_percent=100.0))
    assert requests == []
    assert reasons_for(service.queue, spec_id)[-1]["code"] == "subscription_quota_exhausted"


def test_rate_limit_reached_type_refuses_even_below_one_hundred_percent(
    tmp_path: Path,
) -> None:
    """The provider can report a limit reached without the counter saying 100."""
    reading = headroom(used_percent=87.0, rate_limit_reached_type="primary")
    service, spec_id, requests = authorized_dispatch(tmp_path, reading)
    assert requests == []
    reason = reasons_for(service.queue, spec_id)[-1]
    assert reason["code"] == "subscription_quota_exhausted"
    assert "rate_limit_reached_type" in reason["message"]
    assert "not a threshold this lab invented" in reason["message"]


def test_the_exhaustion_refusal_shows_the_allowance_it_refused_on(tmp_path: Path) -> None:
    service, spec_id, _ = authorized_dispatch(tmp_path, headroom(used_percent=100.0))
    message = reasons_for(service.queue, spec_id)[-1]["message"]
    assert "used_percent         100.0" in message
    assert "remaining_percent    0.0" in message
    assert RESETS_AT.isoformat() in message
    assert "hard_stop            True" in message
    assert "staleness" in message


# --- no invented threshold --------------------------------------------------


def test_no_threshold_is_invented_below_provider_exhaustion(tmp_path: Path) -> None:
    """92% is where the account actually sits. It must still dispatch.

    Whether to stop short of the provider's own limit is a spend decision, and
    this module does not make it.
    """
    assert queue_module.REFUSE_BILLABLE_AT_USED_PERCENT is None
    for used in (80.0, 92.0, 99.0):
        reading = headroom(used_percent=used)
        assert provider_reported_exhaustion(reading) is None
        assert lab_threshold_reached(reading) is None
    _, _, requests = authorized_dispatch(tmp_path, headroom(used_percent=92.0))
    assert len(requests) == 1


def test_a_configured_threshold_refuses_under_its_own_reason_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting the one constant is the whole mechanism, and it stays distinct.

    A lab policy must never be recorded in the reasons log as the provider's
    statement, so it gets its own code.
    """
    monkeypatch.setattr(queue_module, "REFUSE_BILLABLE_AT_USED_PERCENT", 90.0)
    service, spec_id, requests = authorized_dispatch(tmp_path, headroom(used_percent=92.0))
    assert requests == []
    reason = reasons_for(service.queue, spec_id)[-1]
    assert reason["code"] == "subscription_quota_ceiling"
    assert "REFUSE_BILLABLE_AT_USED_PERCENT" in reason["message"]


# --- staleness: visible, warning, never a refusal ---------------------------


def test_a_stale_reading_is_shown_with_its_age_and_still_dispatches(
    tmp_path: Path,
) -> None:
    """The decided policy: stale warns, stale never refuses.

    Refusing on age would deadlock — only a paid trial can produce a fresher
    reading — so the operator gets the age instead of a rule.
    """
    reading = headroom(used_percent=92.0, staleness_seconds=LIVE_STALENESS_SECONDS)
    service, spec_id, requests = authorized_dispatch(tmp_path, reading)
    assert len(requests) == 1
    decision = service.gate.decide(
        spec(submitted_at=datetime(2026, 8, 16, tzinfo=UTC), spec_id=spec_id),
        spent_today_usd=0,
        authorization=service.queue.authorizations()[spec_id],
    )
    assert decision.admitted
    assert "staleness            5h18m old" in decision.message
    assert "a stale reading warns; it never refuses" in decision.message


def test_an_expired_window_cannot_refuse_and_says_why() -> None:
    """A final trial that recorded 100% must not lock the lab out forever.

    `resets_at` is the provider's own statement of when the window rolls over.
    Past it, the recorded percentage describes a window that no longer exists.
    """
    reading = headroom(
        used_percent=100.0,
        resets_at=OBSERVED_AT + timedelta(hours=1),
        staleness_seconds=7200,
    )
    assert quota_window_expired(reading) is True
    assert provider_reported_exhaustion(reading) is None
    notice = render_headroom_notice(reading)
    assert "has already passed" in notice
    assert "cannot reassure you either" in notice


# --- the deliberate override ------------------------------------------------


def test_the_override_is_recorded_on_the_event_not_asserted_by_the_spec(
    tmp_path: Path,
) -> None:
    """#65's property extended: the file the automation writes proves nothing."""
    service = executor(tmp_path, [], headroom(used_percent=100.0))
    path, _ = service.submit(spec())
    spec_id = str(service.queue.load(path).spec_id)

    assert service.queue.authorizations() == {}
    service.queue.approve(spec_id, actor="peter", quota_override=True)
    granted = service.queue.authorizations()[spec_id]
    assert granted.quota_override is True

    forged = json.loads(service.queue.locate(spec_id).read_text())
    forged["quota_override"] = True
    service.queue.locate(spec_id).write_text(json.dumps(forged))
    with pytest.raises(ValueError):
        service.queue.load(service.queue.locate(spec_id))


def test_a_plain_authorization_carries_no_override(tmp_path: Path) -> None:
    service = executor(tmp_path, [], headroom())
    path, _ = service.submit(spec())
    spec_id = str(service.queue.load(path).spec_id)
    service.queue.approve(spec_id, actor="peter")
    assert service.queue.authorizations()[spec_id].quota_override is False


def test_the_override_dispatches_an_exhausted_run_and_records_that_it_did(
    tmp_path: Path,
) -> None:
    requests: list = []
    service = executor(tmp_path, requests, headroom(used_percent=100.0))
    path, _ = service.submit(spec())
    spec_id = str(service.queue.load(path).spec_id)
    service.queue.approve(spec_id, actor="peter", quota_override=True)
    assert service.tick() == 1
    assert len(requests) == 1
    decision = service.gate.decide(
        spec(submitted_at=datetime(2026, 8, 16, tzinfo=UTC), spec_id=spec_id),
        spent_today_usd=0,
        authorization=service.queue.authorizations()[spec_id],
    )
    assert decision.admitted
    assert "DESPITE" in decision.message


def test_the_override_lifts_nothing_except_the_quota_refusal(tmp_path: Path) -> None:
    """It is not a master key: every other #65 and ceiling refusal still fires."""
    service = executor(tmp_path, [], headroom(used_percent=100.0))
    submitted = spec(spec_id="01JQUOTAGATE0000000000000A", est_cost_usd=99.0)
    submitted = submitted.model_copy(update={"submitted_at": datetime(2026, 8, 16, tzinfo=UTC)})
    override = queue_module.PaidRunAuthorization(
        spec_id="01JQUOTAGATE0000000000000A",
        actor="peter",
        authorized_at=datetime(2026, 8, 16, 1, tzinfo=UTC),
        quota_override=True,
    )
    assert (
        service.gate.decide(submitted, spent_today_usd=0, authorization=override).reason_code
        == "per_job_cost_ceiling"
    )
    assert (
        service.gate.decide(submitted, spent_today_usd=0, authorization=None).reason_code
        == "paid_run_unauthorized"
    )
    stale = queue_module.PaidRunAuthorization(
        spec_id="01JQUOTAGATE0000000000000A",
        actor="peter",
        authorized_at=datetime(2026, 8, 15, tzinfo=UTC),
        quota_override=True,
    )
    assert (
        service.gate.decide(submitted, spent_today_usd=0, authorization=stale).reason_code
        == "paid_run_authorization_stale"
    )


# --- free controls are untouched -------------------------------------------


@pytest.mark.parametrize("agent", ["oracle", "nop"])
def test_free_controls_dispatch_unattended_against_an_exhausted_quota(
    tmp_path: Path, agent: str
) -> None:
    """`oracle` and `nop` spend no allowance, so no quota state may hold them."""
    requests: list = []
    service = executor(tmp_path, requests, headroom(used_percent=100.0))
    path, decision = service.submit(spec(agent=agent))
    assert path.parent.name == "approved"
    assert decision.admitted
    assert service.tick() == 1
    assert len(requests) == 1


@pytest.mark.parametrize("agent", ["oracle", "nop"])
def test_a_free_control_admission_is_not_annotated_with_quota_text(
    tmp_path: Path, agent: str
) -> None:
    service = executor(tmp_path, [], headroom(used_percent=100.0))
    _, decision = service.submit(spec(agent=agent))
    assert "subscription quota" not in decision.message


# --- what submit tells the operator -----------------------------------------


def test_the_unauthorized_refusal_carries_the_quota_state(tmp_path: Path) -> None:
    """The moment an operator is asked to authorise is the moment to tell them."""
    service = executor(tmp_path, [], headroom(used_percent=92.0, staleness_seconds=1800))
    path, decision = service.submit(spec())
    assert path.parent.name == "waiting"
    assert decision.reason_code == "paid_run_unauthorized"
    for expected in (
        "used_percent         92.0",
        "remaining_percent    8.0",
        RESETS_AT.isoformat(),
        "hard_stop            True",
        "staleness            30m00s old",
        "scope: account, NOT the lab",
    ):
        assert expected in decision.message
    spec_id = str(service.queue.load(path).spec_id)
    assert "used_percent" in reasons_for(service.queue, spec_id)[-1]["message"]


def test_hard_stop_is_explained_as_a_lockout_not_a_charge(tmp_path: Path) -> None:
    service = executor(tmp_path, [], headroom(used_percent=92.0))
    _, decision = service.submit(spec())
    assert "does not incur an extra charge" in decision.message
