"""HAR-126: the 2026-10-01 $35 daily-ceiling override.

The standing $20 ceiling holds every day except 2026-10-01, when a dated
override (Peter's ~03:55Z approval, ruled by Research-Harbor at 08:42Z)
raises it to $35 until 2026-10-02T00:00Z.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab import cli, modal_billing, spend_day
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


@pytest.fixture
def spend_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Real cap arithmetic over an isolated $29.16 billed-day fixture."""
    path = tmp_path / "policy/standing-approvals.yaml"
    path.parent.mkdir()
    # Also exercise an override that expires during its day, not only at midnight.
    policy = _policy().model_dump(mode="json")
    policy["daily_cost_ceiling_overrides"][0]["expires_at"] = "2026-10-01T18:00:00Z"
    path.write_text(json.dumps(policy))
    row = spend_day.SpendRow(
        source="modal",
        card="HAR-126",
        job="test",
        usd=29.16,
        basis=spend_day.BASIS_BILLED,
        evidence="fixture",
    )
    monkeypatch.setattr(spend_day, "query_modal_rows", lambda *_: (row, "fixture billing"))
    monkeypatch.setattr(spend_day, "query_model_job_rows", lambda *_: ([], [], 0.0))
    monkeypatch.setattr(spend_day, "query_daytona_rows", lambda *_: ([], []))
    monkeypatch.setattr(spend_day, "sibling_worktree_roots", lambda _: [])
    monkeypatch.setattr(modal_billing, "fetch_modal_billing_report", lambda **_: [])
    monkeypatch.setattr(modal_billing, "store_billing_rows", lambda *_, **__: None)
    return tmp_path


@pytest.mark.parametrize(
    "day,cap,over", [("2026-09-30", 20, True), ("2026-10-01", 35, False), ("2026-10-02", 20, True)]
)
def test_spend_day_uses_reported_day_policy_after_override_expiry(
    spend_root: Path, capsys: pytest.CaptureFixture, day: str, cap: int, over: bool
) -> None:
    args = cli.parser().parse_args(["spend", "day", "--date", day, "--json"])
    assert cli._spend_day_command(args, spend_root) == 0
    ledger = json.loads(capsys.readouterr().out)
    assert ledger["cap_usd"] == cap
    assert ledger["total_usd"] == pytest.approx(29.16)
    assert ledger["over_cap"] is over
    assert ledger["headroom_usd"] == pytest.approx(cap - 29.16)
    args.json = False
    assert cli._spend_day_command(args, spend_root) == 0
    output = capsys.readouterr().out
    if cap == 35:
        assert "cap $35.00 (dated override HAR-126; standing $20.00)" in output
        assert "OVER CAP" not in output
    else:
        assert "cap $20.00 (standing policy)" in output
        assert "OVER CAP" in output
        assert "dated override" not in output


@pytest.mark.parametrize(
    "moment,cap,allowed",
    [
        (datetime(2026, 9, 30, 12, tzinfo=UTC), 20, False),
        (OCT1_NOON, 35, True),
        (datetime(2026, 10, 1, 18, tzinfo=UTC), 20, False),
        (datetime(2026, 10, 2, 12, tzinfo=UTC), 20, False),
    ],
)
def test_spend_check_default_cap_respects_launch_day_and_expiry(
    spend_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    moment: datetime,
    cap: int,
    allowed: bool,
) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(cli, "datetime", Clock)
    # A historical --since must not revive yesterday's expired launch allowance.
    args = cli.parser().parse_args(
        [
            "spend",
            "check",
            "--candidate-usd",
            "1",
            "--since",
            "2026-09-29T00:00:00Z",
            "--json",
        ]
    )
    assert cli._spend_check_command(args, spend_root) == (0 if allowed else 3)
    decision = json.loads(capsys.readouterr().out)
    assert decision["cap_usd"] == cap
    assert decision["committed_usd"] == pytest.approx(30.16)
    assert decision["allowed"] is allowed
    args.json = False
    assert cli._spend_check_command(args, spend_root) == (0 if allowed else 3)
    output = capsys.readouterr().out
    if allowed:
        assert "cap: $35.00 (dated override HAR-126; standing $20.00)" in output
    else:
        assert "cap: $20.00 (standing policy)" in output
        assert "dated override" not in output


def test_spend_day_explicit_cap_takes_precedence_over_dated_policy(
    spend_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    args = cli.parser().parse_args(
        [
            "spend",
            "day",
            "--date",
            "2026-10-01",
            "--cap-usd",
            "25",
            "--json",
        ]
    )
    assert cli._spend_day_command(args, spend_root) == 0
    ledger = json.loads(capsys.readouterr().out)
    assert ledger["cap_usd"] == 25
    assert ledger["over_cap"] is True
    assert ledger["cap_description"] is None
