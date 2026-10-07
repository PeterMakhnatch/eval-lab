from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab import campaign_approval as cap
from evallab import cli
from evallab.campaigns import CampaignAttemptStatus, CampaignStatus
from evallab.schemas import ExperimentSpec, PolicyDecision

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
BREAKDOWN = {
    "modal_gpu": 0.4,
    "daytona": 0.2,
    "model_api": 0.15,
    "unmeasured_reserved": 0.25,
}
BUDGET_BOUND = {
    "in_flight_trials": 2,
    "in_flight_reserved_usd": 0.25,
    "in_flight_worst_case_usd": 1.2,
    "overrun_bound_usd": 0.95,
    "realized_spend_bound_usd": 5.95,
}


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def campaign_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    campaign = cap.ExperimentCampaign(
        campaign_id="cli-spend-campaign",
        budget_usd=5.0,
        attempts_per_task=2,
        cost_estimate=cap.CampaignCostEstimate(
            expected_usd=0.58, worst_case_usd=1.2, formula="injected wave estimate"
        ),
        tasks=[cap.CampaignTaskAllowance(task_id="test-task", package_digest="sha256:" + "a" * 64)],
        reference_profile="test-profile",
        agent="oracle",
        environment="docker",
        queue_cwd=str(tmp_path),
        linear_card="HAR-189",
        submitted_by="test",
        created_at=NOW,
    )
    path = tmp_path / "staged.json"
    path.write_text(campaign.model_dump_json(), encoding="utf-8")
    calls: list[tuple[str, cap.ExperimentCampaign | None]] = []

    def meter_call(
        name: str,
        root: Path,
        campaign_id: str,
        campaign: cap.ExperimentCampaign | None,
    ) -> None:
        assert root == tmp_path
        assert campaign_id == "cli-spend-campaign"
        calls.append((name, campaign))

    def spend(root: Path, campaign_id: str, *, campaign=None):
        meter_call("spend", root, campaign_id, campaign)
        return 0.25, 0.75, 1.0

    def breakdown(root: Path, campaign_id: str, *, campaign=None):
        meter_call("breakdown", root, campaign_id, campaign)
        return dict(BREAKDOWN)

    def draft_meter(root: Path, campaign_id: str, *, campaign):
        meter_call("spend", root, campaign_id, campaign)
        meter_call("breakdown", root, campaign_id, campaign)
        return 0.25, 0.75, dict(BREAKDOWN)

    def bound(root: Path, campaign_id: str, *, campaign=None):
        meter_call("bound", root, campaign_id, campaign)
        return dict(BUDGET_BOUND)

    def forbidden(*args, **kwargs):
        pytest.fail("read-only campaign command invoked a write or orchestrator")

    monkeypatch.setattr(cli, "load_local_env", lambda _path: None)
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    monkeypatch.setattr(cli, "_campaign_orchestrator", forbidden)
    monkeypatch.setattr(cap, "campaign_spend_usd", spend)
    monkeypatch.setattr(cap, "campaign_spend_breakdown", breakdown)
    monkeypatch.setattr(cap, "_campaign_meter", draft_meter)
    monkeypatch.setattr(cap, "campaign_budget_bound", bound)
    monkeypatch.setattr(cap, "validate_campaign_content", lambda _root, _draft: [])
    monkeypatch.setattr(cap, "read_approvals", lambda _root, _campaign_id: [])
    monkeypatch.setattr(cap, "write_campaign", forbidden)
    monkeypatch.setattr(cap, "approve_campaign", forbidden)
    monkeypatch.setattr(cap, "LIN_RUNNER", forbidden)
    return SimpleNamespace(root=tmp_path, campaign=campaign, path=path, calls=calls)


def _freeze(data: SimpleNamespace) -> Path:
    path = data.root / cap.CAMPAIGN_STATE_ROOT / data.campaign.campaign_id / cap.CAMPAIGN_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text(data.campaign.model_dump_json(), encoding="utf-8")
    return path


@pytest.mark.parametrize("command", ["validate", "status"])
@pytest.mark.parametrize("frozen", [False, True])
def test_approval_campaign_cli_spend_snapshot_json_and_text(
    campaign_cli: SimpleNamespace,
    command: str,
    frozen: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = campaign_cli
    if frozen:
        _freeze(data)
    before = _files(data.root)
    argv = ["campaign", command, data.path.name]
    assert cli.run_cli([*argv, "--json"], workspace=data.root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["campaign_id"] == data.campaign.campaign_id
    assert payload["budget_usd"] == 5.0
    assert payload["reserved_usd"] == 0.25
    assert payload["settled_usd"] == 0.75
    assert payload["committed_usd"] == 1.0
    assert payload["spend_breakdown"] == BREAKDOWN
    assert sum(payload["spend_breakdown"].values()) == pytest.approx(payload["committed_usd"])
    assert payload["budget_bound"] == BUDGET_BOUND
    assert payload["approval"] == {"state": "UNAPPROVED"}
    assert payload["cost_estimate"] == data.campaign.cost_estimate.model_dump(mode="json")
    assert payload["tasks"] == 1
    assert payload["attempts_per_task"] == 2
    if command == "validate":
        assert payload["errors"] == []
        assert payload["specs"] == []

    assert cli.run_cli(argv, workspace=data.root) == 0
    text = capsys.readouterr().out
    assert "spend: committed $1.000000, reserved $0.250000, settled $0.750000" in text
    for source, amount in payload["spend_breakdown"].items():
        assert f"  {source}: ${amount:.6f}" in text
    assert "approval: UNAPPROVED" in text
    assert "budget_bound: 2 in-flight x $0.475000" in text
    assert "= $0.950000 overrun above budget" in text
    assert "in-flight reserved $0.250000, worst-case $1.200000" in text
    assert "realized <= $5.950000" in text
    assert "19 in-flight" not in text
    expected_campaign = None if frozen else data.campaign
    assert data.calls == [
        (name, expected_campaign) for _ in range(2) for name in ("spend", "breakdown", "bound")
    ]
    assert _files(data.root) == before


def test_approval_campaign_validate_and_status_share_snapshot(
    campaign_cli: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    data = campaign_cli
    assert cli.run_cli(["campaign", "validate", data.path.name, "--json"], workspace=data.root) == 0
    validated = json.loads(capsys.readouterr().out)
    assert cli.run_cli(["campaign", "status", data.path.name, "--json"], workspace=data.root) == 0
    status = json.loads(capsys.readouterr().out)
    assert validated.pop("errors") == []
    assert validated.pop("specs") == []
    assert validated == status
    assert cli.run_cli(["campaign", "validate", data.path.name], workspace=data.root) == 0
    validated_text = capsys.readouterr().out
    assert cli.run_cli(["campaign", "status", data.path.name], workspace=data.root) == 0
    assert capsys.readouterr().out == validated_text


@pytest.mark.parametrize("command", ["validate", "status"])
@pytest.mark.parametrize("digest_match", [False, True])
def test_approval_campaign_cli_preserves_approval_digest_state(
    campaign_cli: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    digest_match: bool,
) -> None:
    data = campaign_cli
    _freeze(data)
    approval = cap.CampaignApprovalRecord(
        campaign_id=data.campaign.campaign_id,
        content_digest=(
            cap.campaign_content_digest(data.campaign) if digest_match else "sha256:" + "b" * 64
        ),
        actor="research-harbor",
        approved_at=NOW,
    )
    monkeypatch.setattr(cap, "read_approvals", lambda _root, _campaign_id: [approval])
    argv = ["campaign", command, data.path.name]
    assert cli.run_cli([*argv, "--json"], workspace=data.root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["approval"] == {
        "state": "APPROVED",
        "actor": approval.actor,
        "approved_at": approval.approved_at.isoformat(),
        "content_digest_match": digest_match,
    }
    assert cli.run_cli(argv, workspace=data.root) == 0
    text = capsys.readouterr().out
    assert "approval: APPROVED by research-harbor" in text
    assert ("digest match" if digest_match else "DIGEST CHANGED AFTER APPROVAL") in text
    assert all(campaign is None for _name, campaign in data.calls)


@pytest.mark.parametrize("count,reserved", [(0, 0.0), (2, 2.0)])
def test_approval_campaign_bound_handles_no_flights_and_overreservation(
    campaign_cli: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    count: int,
    reserved: float,
) -> None:
    data = campaign_cli
    no_estimate = data.campaign.model_copy(update={"cost_estimate": None})
    data.path.write_text(no_estimate.model_dump_json(), encoding="utf-8")
    bound = {
        "in_flight_trials": count,
        "in_flight_reserved_usd": reserved,
        "in_flight_worst_case_usd": count * 0.6,
        "overrun_bound_usd": 0.0,
        "realized_spend_bound_usd": 5.0,
    }
    monkeypatch.setattr(cap, "campaign_budget_bound", lambda *args, **kwargs: dict(bound))
    argv = ["campaign", "status", data.path.name]
    assert cli.run_cli([*argv, "--json"], workspace=data.root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cost_estimate"] is None
    assert payload["budget_bound"] == bound
    assert cli.run_cli(argv, workspace=data.root) == 0
    text = capsys.readouterr().out
    assert "expected: unstated (no cost_estimate pinned)" in text
    assert f"budget_bound: {count} in-flight x $0.000000" in text
    assert "= $0.000000 overrun above budget" in text
    assert "realized <= $5.000000" in text
    assert "$-" not in text


def test_campaign_validate_preserves_spec_preflight_with_committed_spend(
    campaign_cli: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = campaign_cli
    spec = ExperimentSpec(
        name="cli-preflight",
        hypothesis="preflight checks campaign admission",
        purpose="practice",
        task="test-task",
        agent="oracle",
        environment="docker",
        submitted_by="test",
        campaign_id=data.campaign.campaign_id,
    )
    spec_path = data.root / "spec.json"
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    admission_calls: list[float] = []

    def admission(candidate, draft, *, repo_root, committed_usd):
        assert candidate == spec
        assert draft == data.campaign
        assert repo_root == data.root
        admission_calls.append(committed_usd)
        return PolicyDecision(
            admitted=False,
            reason_code="campaign_budget_exhausted",
            message="injected campaign refusal",
        )

    monkeypatch.setattr(cap, "check_campaign_admission", admission)
    argv = ["campaign", "validate", data.path.name, "--spec", spec_path.name]
    assert cli.run_cli([*argv, "--json"], workspace=data.root) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["specs"] == [
        {
            "spec": "spec.json",
            "admitted": False,
            "reason_code": "campaign_budget_exhausted",
            "message": "injected campaign refusal",
        }
    ]
    assert payload["spend_breakdown"] == BREAKDOWN
    assert cli.run_cli(argv, workspace=data.root) == 1
    text = capsys.readouterr().out
    assert "spec.json: refused (campaign_budget_exhausted)" in text
    assert "injected campaign refusal" in text
    assert admission_calls == [1.0, 1.0]


def _orchestrator_status(campaign_id: str) -> CampaignStatus:
    return CampaignStatus(
        campaign_id=campaign_id,
        benchmark="test-benchmark",
        manifest_digest="sha256:" + "c" * 64,
        state="circuit-open",
        attempts=(
            CampaignAttemptStatus(
                attempt_id="attempt-1",
                spec_id="spec-1",
                job_name="job-1",
                cell_id="cell-1",
                task_id="task-1",
                attempt=1,
                queue_state="waiting",
                spec_digest="sha256:" + "d" * 64,
                completed=False,
                approval_command="evallab approve spec-1 --actor test",
            ),
        ),
        completed_attempts=0,
        total_attempts=1,
        cost_usd=0.75,
        input_tokens=12,
        output_tokens=34,
        wall_clock_seconds=5.0,
        circuit_reason="injected circuit reason",
        block_reason="injected block reason",
    )


@pytest.mark.parametrize("frozen", [False, True])
def test_orchestrator_status_preserves_output_and_only_adds_frozen_approval_spend(
    campaign_cli: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    frozen: bool,
) -> None:
    data = campaign_cli
    if frozen:
        _freeze(data)
    manifest_path = data.root / "manifest.json"
    manifest_path.write_text(json.dumps({"schema_version": "campaign-manifest/v3"}))
    status = _orchestrator_status(data.campaign.campaign_id)
    monkeypatch.setattr(
        cli, "_campaign_orchestrator", lambda _args, _root: SimpleNamespace(status=lambda: status)
    )
    before = _files(data.root)
    argv = ["campaign", "status", manifest_path.name]
    assert cli.run_cli([*argv, "--json"], workspace=data.root) == 0
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    expected = status.model_dump(mode="json")
    if frozen:
        expected.update(
            reserved_usd=0.25,
            settled_usd=0.75,
            committed_usd=1.0,
            spend_breakdown=BREAKDOWN,
            budget_bound=BUDGET_BOUND,
        )
    assert payload == expected
    assert cli.run_cli(argv, workspace=data.root) == 0
    text = capsys.readouterr().out
    assert f"campaign: {status.campaign_id}" in text
    assert "benchmark: test-benchmark" in text
    assert f"manifest_digest: {status.manifest_digest}" in text
    assert "state: circuit-open" in text
    assert "attempts: 0/1 completed" in text
    assert "usage: $0.750000, 12 input tokens, 34 output tokens, 5.000s" in text
    assert "circuit: injected circuit reason" in text
    assert "blocked: injected block reason" in text
    assert "- attempt-1 cell-1/task-1#1: waiting" in text
    assert "approve: evallab approve spec-1 --actor test" in text
    if frozen:
        assert "spend_breakdown:" in text
        assert "modal_gpu: $0.400000" in text
        assert "budget_bound: 2 in-flight x $0.475000" in text
        assert data.calls == [
            (name, None) for _ in range(2) for name in ("spend", "breakdown", "bound")
        ]
    else:
        assert "spend_breakdown:" not in text
        assert "budget_bound:" not in text
        assert data.calls == []
    assert _files(data.root) == before


def test_approval_campaign_status_rejects_invalid_campaign_without_meter_reads(
    campaign_cli: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    data = campaign_cli
    data.path.write_text(json.dumps({"schema_version": "experiment-campaign/v1"}))
    before = _files(data.root)
    assert cli.run_cli(["campaign", "status", data.path.name], workspace=data.root) == 2
    assert "error:" in capsys.readouterr().err
    assert data.calls == []
    assert _files(data.root) == before
