"""One approval per experiment campaign instead of one per run ID (HAR-175).

Behaviour tests through the real queue/tick path with stubs: admission
without per-ID approval, one reason per mismatch, one infra replacement (not
two), and budget exhaustion that stops launches while leaving running trials
alone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab import campaign_approval as cap
from evallab.campaign_approval import (
    CampaignCeilingFloor,
    CampaignSampling,
    CampaignTaskAllowance,
    ExperimentCampaign,
)
from evallab.queue import DirectoryQueue, Executor, PolicyGate, load_events
from evallab.registry import compute_task_digests
from evallab.schemas import (
    AutoRunRule,
    ExperimentSpec,
    ReferenceDeviation,
    StandingApprovalsPolicy,
)

TASK_ID = "format-code-task-001198"
TASK_REF = "test-task/test-task-001198"
CAMPAIGN_ID = "test-campaign"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
        escalate_to_human=["anything_exceeding_ceilings"],
    )


def seed_task(root: Path) -> str:
    """A minimal on-disk task package; returns its real package digest."""
    task_dir = root / TASK_REF
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text('[task]\nname = "test-task-001198"\n', encoding="utf-8")
    (task_dir / "instruction.md").write_text("Do the thing.\n", encoding="utf-8")
    return compute_task_digests(task_dir).package


def make_executor(root: Path, **kwargs) -> Executor:
    return Executor(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=policy(),
        runner=kwargs.pop("runner", lambda request: request.jobs_dir / request.name),
        ingester=kwargs.pop("ingester", lambda path: None),
        spent_today=kwargs.pop("spent_today", lambda: 0),
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: frozenset({"claude_oauth", "codex_auth"}),
        smoke_gate_enabled=False,
        **kwargs,
    )


def make_campaign(
    root: Path,
    package_digest: str,
    *,
    campaign_id: str = CAMPAIGN_ID,
    budget_usd: float = 10.0,
    task_id: str = TASK_ID,
    card: str = "HAR-175",
) -> ExperimentCampaign:
    campaign = ExperimentCampaign(
        campaign_id=campaign_id,
        budget_usd=budget_usd,
        tasks=[CampaignTaskAllowance(task_id=task_id, package_digest=package_digest)],
        reference_profile="xiaomi-mimo-rl",
        allowed_deviations=[],
        require_egress_lock=True,
        ceiling_floor=CampaignCeilingFloor(
            max_requests=5,
            max_input_tokens=500,
            max_output_tokens=500,
            max_total_tokens=1000,
            cost_limit_usd=0.5,
        ),
        agent="codex",
        model="test-model",
        environment="docker",
        sampling=CampaignSampling(),
        queue_cwd=str(root),
        linear_card=card,
        submitted_by="test",
        created_at=NOW,
    )
    cap.write_campaign(root, campaign)
    cap.approve_campaign(root, campaign_id, actor="research-harbor", approved_at=NOW)
    return campaign


def make_spec(
    name: str,
    package_digest: str,
    *,
    task_id: str = TASK_ID,
    egress_lock: bool | None = True,
    deviations: list[ReferenceDeviation] | None = None,
    model: str | None = "test-model",
    est_cost_usd: float = 1.0,
    campaign_id: str | None = CAMPAIGN_ID,
    max_requests: int | None = 10,
) -> ExperimentSpec:
    return ExperimentSpec(
        name=name,
        hypothesis="campaign approval admits matching specs",
        purpose="practice",
        task=TASK_REF,
        task_path=TASK_REF,
        agent="codex",
        model=model,
        environment="docker",
        egress_lock=egress_lock,
        reference_profile="xiaomi-mimo-rl",
        deviations=[] if deviations is None else deviations,
        submitted_by="test",
        est_cost_usd=est_cost_usd,
        task_id=task_id,
        task_package_digest=package_digest,
        campaign_id=campaign_id,
        max_requests=max_requests,
        max_input_tokens=1000,
        max_output_tokens=1000,
        max_total_tokens=2000,
        cost_limit_usd=1.0,
    )


def write_job(
    root: Path,
    spec_name: str,
    *,
    reward: float | None = None,
    exception: str | None = None,
    cost: float | None = None,
    finished: bool = True,
) -> Path:
    job_dir = root / "runs" / spec_name
    trial_dir = job_dir / "trial-0"
    trial_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "result.json").write_text(
        json.dumps(
            {
                "n_total_trials": 1,
                "stats": {},
                "finished_at": "2026-10-06T00:00:00Z" if finished else None,
                "id": "job-1",
            }
        ),
        encoding="utf-8",
    )
    trial_result: dict = {
        "id": "trial-0",
        "task_name": TASK_REF,
        "trial_name": "trial-0",
    }
    if reward is not None:
        trial_result["verifier_result"] = {"rewards": {"reward": reward}}
    if exception is not None:
        trial_result["exception_info"] = {
            "exception_type": exception,
            "exception_message": exception,
        }
    if cost is not None:
        trial_result["agent_result"] = {"cost_usd": cost}
    (trial_dir / "result.json").write_text(json.dumps(trial_result), encoding="utf-8")
    return job_dir


def waiting_code(service: Executor, spec_id: str) -> str:
    path = service.queue.locate(spec_id, ("waiting",))
    assert path.parent.name == "waiting"
    reasons = list(service.queue.reasons_dir.glob(f"{spec_id}-*.json"))
    assert reasons, f"no reason recorded for {spec_id}"
    return json.loads(reasons[-1].read_text(encoding="utf-8"))["code"]


def test_campaign_matching_spec_runs_without_per_id_approval(tmp_path: Path) -> None:
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    make_campaign(tmp_path, digest)

    path, decision = service.submit(make_spec("campaign-match-a", digest))
    assert decision.admitted
    assert decision.policy_rule == f"campaign:{CAMPAIGN_ID}"
    assert path.parent.name == "approved"
    spec_id = str(service.queue.load(path).spec_id)

    assert service.tick() == 1
    done = service.queue.locate(spec_id, ("done",))
    assert done.parent.name == "done"
    assert not [
        event for event in load_events(service.queue.events_path) if event.event == "human_approved"
    ]


def test_campaign_refuses_different_temperature(tmp_path: Path) -> None:
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    make_campaign(tmp_path, digest)

    spec = make_spec(
        "campaign-hot-a",
        digest,
        deviations=[ReferenceDeviation(field="sampling.temperature", value=0.2, reason="hot take")],
    )
    path, decision = service.submit(spec)
    assert not decision.admitted
    assert decision.reason_code == "campaign_setup_mismatch"
    assert "sampling.temperature" in decision.message
    assert path.parent.name == "waiting"
    assert waiting_code(service, str(service.queue.load(path).spec_id)) == (
        "campaign_setup_mismatch"
    )
    assert cap.read_escalations(tmp_path, CAMPAIGN_ID) == []


def test_campaign_refuses_missing_lock(tmp_path: Path) -> None:
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    make_campaign(tmp_path, digest)

    path, decision = service.submit(make_spec("campaign-open-a", digest, egress_lock=False))
    assert not decision.admitted
    assert decision.reason_code == "campaign_lock_missing"
    assert path.parent.name == "waiting"
    assert cap.read_escalations(tmp_path, CAMPAIGN_ID) == []


def test_campaign_refuses_off_list_task_and_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cap, "LIN_RUNNER", lambda argv, check: None)
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    make_campaign(tmp_path, digest)

    off_list, off_decision = service.submit(
        make_spec("campaign-offlist-a", digest, task_id="format-code-task-999999")
    )
    assert not off_decision.admitted
    assert off_decision.reason_code == "campaign_task_not_allowed"
    assert off_list.parent.name == "waiting"

    wrong_digest, digest_decision = service.submit(
        make_spec("campaign-wrongdigest-a", "sha256:" + "b" * 64)
    )
    assert not digest_decision.admitted
    assert digest_decision.reason_code == "campaign_task_digest_mismatch"
    assert wrong_digest.parent.name == "waiting"

    # Rule breaches page Research-Harbor; routine mismatches stay silent.
    escalations = cap.read_escalations(tmp_path, CAMPAIGN_ID)
    assert {item.reason_code for item in escalations} == {
        "campaign_task_not_allowed",
        "campaign_task_digest_mismatch",
    }


def test_campaign_refuses_ceiling_below_floor(tmp_path: Path) -> None:
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    make_campaign(tmp_path, digest)

    path, decision = service.submit(make_spec("campaign-lowceil-a", digest, max_requests=2))
    assert not decision.admitted
    assert decision.reason_code == "campaign_ceiling_below_floor"
    assert path.parent.name == "waiting"


def test_campaign_infra_failure_triggers_one_replacement_not_second(
    tmp_path: Path,
) -> None:
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    make_campaign(tmp_path, digest)

    path, decision = service.submit(make_spec("campaign-infra-a", digest))
    assert decision.admitted
    original_id = str(service.queue.load(path).spec_id)
    assert service.tick() == 1
    write_job(tmp_path, "campaign-infra-a", exception="DaytonaNotFoundError")

    assert service.tick() == 0
    replacements = [
        item
        for _, item in service.queue.list_specs("approved")
        if item.campaign_replaces == original_id
    ]
    assert len(replacements) == 1
    replacement = replacements[0]
    assert replacement.campaign_id == CAMPAIGN_ID
    assert replacement.name == "campaign-infra-a-r1"
    assert not [
        event
        for event in load_events(service.queue.events_path)
        if event.event == "human_approved" and event.spec_id == str(replacement.spec_id)
    ]

    # The replacement runs, then fails infra itself: no second replacement.
    assert service.tick(spec_ids=[str(replacement.spec_id)]) == 1
    write_job(
        tmp_path,
        str(replacement.name),
        exception="ServiceUnavailableError",
    )
    assert service.tick() == 0
    done_specs = [item for _, item in service.queue.list_specs("done")]
    assert len([item for item in done_specs if item.campaign_replaces == original_id]) == 1
    for state in ("approved", "waiting", "pending", "running", "done", "failed"):
        for _, item in service.queue.list_specs(state):  # type: ignore[arg-type]
            assert item.campaign_replaces != str(replacement.spec_id)


def test_campaign_budget_exhaustion_stops_launches_leaves_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list = []
    monkeypatch.setattr(cap, "LIN_RUNNER", lambda argv, check: posted.append(argv))
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    make_campaign(tmp_path, digest, budget_usd=1.0)

    running_path, admitted = service.submit(make_spec("campaign-funded-a", digest))
    assert admitted.admitted
    queued = service.queue.load(running_path)
    service.queue.transition(running_path, "running", actor="executor", event="dispatch_started")
    write_job(tmp_path, queued.name, finished=False)

    refused_path, refused = service.submit(make_spec("campaign-funded-b", digest))
    assert not refused.admitted
    assert refused.reason_code == "campaign_budget_exhausted"
    assert refused_path.parent.name == "waiting"
    assert service.queue.stop_path.is_file()

    escalations = cap.read_escalations(tmp_path, CAMPAIGN_ID)
    assert len(escalations) == 1
    assert escalations[0].reason_code == "campaign_budget_exhausted"
    assert escalations[0].card == "HAR-175"
    assert escalations[0].lin_posted is True
    assert posted[0][:3] == ["lin", "comment", "HAR-175"]
    assert "campaign_budget_exhausted" in posted[0][3]
    assert len(posted) == 1

    assert service.tick() == 0
    assert service.queue.locate(str(queued.spec_id), ("running",)).is_file()
    assert service.queue.locate(
        str(service.queue.load(refused_path).spec_id), ("waiting",)
    ).is_file()


def test_campaign_dispatch_refuses_settled_overrun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cap, "LIN_RUNNER", lambda argv, check: None)
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    make_campaign(tmp_path, digest, budget_usd=2.0)

    overrun_path, _ = service.submit(make_spec("campaign-overrun-a", digest))
    other_path, _ = service.submit(make_spec("campaign-overrun-b", digest))
    overrun = service.queue.load(overrun_path)
    other_id = str(service.queue.load(other_path).spec_id)
    service.queue.transition(overrun_path, "done", actor="executor", event="dispatch_completed")
    write_job(tmp_path, overrun.name, reward=1.0, cost=1.5)

    assert service.tick() == 0
    assert service.queue.locate(other_id, ("waiting",)).is_file()
    assert waiting_code(service, other_id) == "campaign_budget_exhausted"
    assert service.queue.stop_path.is_file()
    assert service.queue.locate(str(overrun.spec_id), ("done",)).is_file()
    assert [
        item
        for item in cap.read_escalations(tmp_path, CAMPAIGN_ID)
        if item.reason_code == "campaign_budget_exhausted"
    ]


def test_campaign_escalation_deduplicates_per_spec_and_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list = []
    monkeypatch.setattr(cap, "LIN_RUNNER", lambda argv, check: posted.append(argv))
    digest = seed_task(tmp_path)
    campaign = make_campaign(tmp_path, digest)

    first = cap.escalate(tmp_path, campaign, "campaign_budget_exhausted", "broke", spec_id="spec-1")
    second = cap.escalate(
        tmp_path, campaign, "campaign_budget_exhausted", "broke", spec_id="spec-1"
    )
    assert first == second
    assert len(cap.read_escalations(tmp_path, CAMPAIGN_ID)) == 1
    assert len(posted) == 1
    assert posted[0][:3] == ["lin", "comment", "HAR-175"]


def test_campaign_approve_once_and_immutable(tmp_path: Path) -> None:
    digest = seed_task(tmp_path)
    make_campaign(tmp_path, digest)
    with pytest.raises(cap.CampaignApprovalError):
        cap.approve_campaign(tmp_path, CAMPAIGN_ID, actor="research-harbor")

    frozen = tmp_path / "runs" / "campaigns" / CAMPAIGN_ID / "campaign.json"
    raw = json.loads(frozen.read_text(encoding="utf-8"))
    raw["budget_usd"] = 999.0
    frozen.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cap.CampaignApprovalError) as excinfo:
        cap.load_approved_campaign(tmp_path, CAMPAIGN_ID)
    assert excinfo.value.reason_code == "campaign_content_changed"

    gate = PolicyGate(policy(), repo_root=tmp_path)
    refusal = cap.campaign_admission_refusal(gate, make_spec("campaign-tampered-a", digest))
    assert refusal is not None
    assert refusal.reason_code == "campaign_content_changed"


def test_per_id_path_unchanged_without_campaign(tmp_path: Path) -> None:
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    make_campaign(tmp_path, digest)

    path, decision = service.submit(make_spec("campaign-noclaim-a", digest, campaign_id=None))
    assert not decision.admitted
    assert decision.reason_code == "paid_run_unauthorized"
    assert path.parent.name == "waiting"
