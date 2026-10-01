"""HAR-126: the 2026-10-01 $35 daily-ceiling override.

The standing $20 ceiling holds every day except 2026-10-01, when a dated
override (Peter's ~03:55Z approval, ruled by Research-Harbor at 08:42Z)
raises it to $35 until 2026-10-02T00:00Z.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from evallab.execution_contracts import PaidRunAuthorization
from evallab.queue import PolicyGate
from evallab.schemas import (
    AutoRunRule,
    ExperimentSpec,
    StandingApprovalsPolicy,
    effective_daily_cost_ceiling,
)

OCT1_NOON = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
EXPIRY = datetime(2026, 10, 2, 0, 0, 0, tzinfo=UTC)


def _policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=100.0,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="controls", agents=["oracle"])],
        escalate_to_human=[],
        daily_cost_ceiling_overrides=[
            {
                "utc_date": "2026-10-01",
                "ceiling_usd": 35,
                "expires_at": "2026-10-02T00:00:00Z",
                "approved_by": "Research-Harbor HAR-126 ruling 2026-10-01T08:42Z",
                "approval_quote": (
                    'Peter 2026-10-01 ~03:55Z "i approve like a $30 budget '
                    'for runs total", from 04:00Z, plus ~$5 approved earlier on HAR-116'
                ),
                "card": "HAR-126",
            }
        ],
    )


def _spec(est: float, at: datetime) -> tuple[ExperimentSpec, PaidRunAuthorization]:
    spec_id = "01TESTSPEC0000000000000000"
    spec = ExperimentSpec(
        spec_id=spec_id,
        name="test-spec",
        hypothesis="test",
        purpose="practice",
        task="library/tasks/event-summary",
        agent="codex",
        submitted_by="peter",
        submitted_at=at,
        est_cost_usd=est,
    )
    return spec, PaidRunAuthorization(spec_id=spec_id, actor="peter", authorized_at=at)


def test_resolver_applies_override_only_on_its_utc_day() -> None:
    policy = _policy()
    assert effective_daily_cost_ceiling(policy, OCT1_NOON) == 35
    assert effective_daily_cost_ceiling(policy, EXPIRY) == 20
    assert effective_daily_cost_ceiling(policy, datetime(2026, 9, 30, 12, tzinfo=UTC)) == 20
    assert effective_daily_cost_ceiling(policy, datetime(2026, 10, 3, 12, tzinfo=UTC)) == 20


def test_dispatch_gate_admits_within_override_ceiling() -> None:
    """$28 of daily spend exceeds $20 but not the $35 override: admitted."""
    gate = PolicyGate(_policy())
    spec, auth = _spec(10.0, OCT1_NOON)
    decision = gate.decide(spec, spent_today_usd=18.0, authorization=auth, now=OCT1_NOON)
    assert decision.admitted


def test_dispatch_gate_restores_standing_ceiling_at_expiry() -> None:
    gate = PolicyGate(_policy())
    spec, auth = _spec(10.0, EXPIRY)
    decision = gate.decide(spec, spent_today_usd=18.0, authorization=auth, now=EXPIRY)
    assert not decision.admitted
    assert decision.reason_code == "daily_cost_ceiling"


def test_duplicate_override_dates_refused() -> None:
    raw = _policy().model_dump(mode="json")
    raw["daily_cost_ceiling_overrides"] = raw["daily_cost_ceiling_overrides"] * 2
    with pytest.raises(ValidationError):
        StandingApprovalsPolicy.model_validate(raw)


def test_override_expiring_past_its_utc_day_refused() -> None:
    with pytest.raises(ValidationError):
        StandingApprovalsPolicy(
            daily_cost_ceiling_usd=20,
            per_job_cost_ceiling_usd=100.0,
            quiet_failure_rule=3,
            auto_run=[AutoRunRule(name="controls", agents=["oracle"])],
            daily_cost_ceiling_overrides=[
                {
                    "utc_date": "2026-10-01",
                    "ceiling_usd": 35,
                    "expires_at": "2026-10-03T00:00:00Z",
                    "approved_by": "x",
                    "approval_quote": "y",
                    "card": "HAR-126",
                }
            ],
        )


def test_override_with_non_positive_ceiling_refused() -> None:
    with pytest.raises(ValidationError):
        StandingApprovalsPolicy(
            daily_cost_ceiling_usd=20,
            per_job_cost_ceiling_usd=100.0,
            quiet_failure_rule=3,
            auto_run=[AutoRunRule(name="controls", agents=["oracle"])],
            daily_cost_ceiling_overrides=[
                {
                    "utc_date": "2026-10-01",
                    "ceiling_usd": 0,
                    "expires_at": "2026-10-02T00:00:00Z",
                    "approved_by": "x",
                    "approval_quote": "y",
                    "card": "HAR-126",
                }
            ],
        )


def test_empty_overrides_stay_out_of_the_canonical_dump() -> None:
    dumped = StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=100.0,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="controls", agents=["oracle"])],
    ).model_dump(mode="json")
    assert "daily_cost_ceiling_overrides" not in dumped
