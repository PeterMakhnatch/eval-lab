"""Behavioral contracts for required experiment purpose."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from evallab.queue import DirectoryQueue, PolicyGate
from evallab.schemas import (
    EXPERIMENT_PURPOSES,
    AutoRunRule,
    ExperimentSpec,
    StandingApprovalsPolicy,
)


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "purpose-probe",
        "hypothesis": "a spec records why it exists",
        "purpose": "baseline",
        "task": "library/tasks/event-summary",
        "agent": "oracle",
        "submitted_by": "test",
    }
    payload.update(overrides)
    return payload


def _policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
    )


@pytest.mark.parametrize("purpose", EXPERIMENT_PURPOSES)
def test_every_experiment_purpose_parses(purpose: str) -> None:
    assert ExperimentSpec.model_validate(_payload(purpose=purpose)).purpose == purpose


def test_missing_experiment_purpose_does_not_parse() -> None:
    payload = _payload()
    del payload["purpose"]

    with pytest.raises(ValidationError, match="purpose"):
        ExperimentSpec.model_validate(payload)


def test_unknown_experiment_purpose_does_not_parse() -> None:
    with pytest.raises(ValidationError, match="purpose"):
        ExperimentSpec.model_validate(_payload(purpose="exploration"))


def test_queue_round_trip_preserves_parsed_purpose(tmp_path) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    path, decision = queue.submit(
        ExperimentSpec.model_validate(_payload(purpose="drift", policy_rule="local-controls")),
        gate=PolicyGate(_policy()),
        spent_today_usd=0.0,
    )

    assert decision.admitted is True
    assert queue.load(path).purpose == "drift"
