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
from evallab.dispatch_guards import SelfhostedProbeOutcome
from evallab.modal_billing import BillingRow
from evallab.modal_ops import MODAL_APP_NAME
from evallab.queue import DirectoryQueue, Executor, PolicyGate, load_events
from evallab.registry import compute_task_digests
from evallab.schemas import (
    AutoRunRule,
    ExperimentSpec,
    ReferenceDeviation,
    StandingApprovalsPolicy,
)
from evallab.task_qualification import estimate_cost_usd

TASK_ID = "format-code-task-001198"
TASK_REF = "test-task/test-task-001198"
CAMPAIGN_ID = "test-campaign"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolate_linear_escalations(monkeypatch):
    monkeypatch.setattr(cap, "LIN_RUNNER", lambda argv: None)


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
        credential_probe=kwargs.pop(
            "credential_probe", lambda: frozenset({"claude_oauth", "codex_auth"})
        ),
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
    agent: str = "codex",
    model: str | None = "test-model",
    environment: str = "docker",
    sampling: CampaignSampling | None = None,
    attempts_per_task: int = 1,
    write: bool = True,
    adaptive_sampling: cap.CampaignAdaptiveSampling | None = None,
    tasks: list[CampaignTaskAllowance] | None = None,
    concurrency: int = 1,
) -> ExperimentCampaign:
    campaign = ExperimentCampaign(
        campaign_id=campaign_id,
        budget_usd=budget_usd,
        tasks=tasks or [CampaignTaskAllowance(task_id=task_id, package_digest=package_digest)],
        adaptive_sampling=adaptive_sampling,
        attempts_per_task=attempts_per_task,
        concurrency=concurrency,
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
        agent=agent,
        model=model,
        environment=environment,
        sampling=CampaignSampling() if sampling is None else sampling,
        queue_cwd=str(root),
        linear_card=card,
        submitted_by="test",
        created_at=NOW,
    )
    if write:
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
    agent: str = "codex",
    model: str | None = "test-model",
    environment: str = "docker",
    est_cost_usd: float = 1.0,
    campaign_id: str | None = CAMPAIGN_ID,
    max_requests: int | None = 10,
    attempts: int = 1,
    cost_limit_usd: float = 1.0,
    campaign_task_attempt: int | None = None,
) -> ExperimentSpec:
    return ExperimentSpec(
        name=name,
        hypothesis="campaign approval admits matching specs",
        purpose="practice",
        task=TASK_REF,
        task_path=TASK_REF,
        agent=agent,
        model=model,
        environment=environment,
        egress_lock=egress_lock,
        reference_profile="xiaomi-mimo-rl",
        deviations=[] if deviations is None else deviations,
        submitted_by="test",
        est_cost_usd=est_cost_usd,
        task_id=task_id,
        task_package_digest=package_digest,
        campaign_id=campaign_id,
        campaign_task_attempt=campaign_task_attempt,
        max_requests=max_requests,
        max_input_tokens=1000,
        max_output_tokens=1000,
        max_total_tokens=2000,
        cost_limit_usd=cost_limit_usd,
        attempts=attempts,
    )


def write_job(
    root: Path,
    spec_name: str,
    *,
    reward: float | None = None,
    exception: str | None = None,
    cost: float | None = None,
    finished: bool = True,
    job_id: str = "job-1",
    trial_id: str = "trial-0",
    started_at: str | None = None,
    finished_at: str | None = None,
    model: str | None = None,
    usage: dict | None = None,
) -> Path:
    """A completed Harbor job, unless ``finished`` is false.

    ``job_id`` and ``trial_id`` must be unique across jobs the meter may see
    together: native identity dedupe is ``(job.result.id, trial.result.id)``.
    """
    job_dir = root / "runs" / spec_name
    trial_dir = job_dir / trial_id
    trial_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "result.json").write_text(
        json.dumps(
            {
                "n_total_trials": 1,
                "stats": {},
                "finished_at": "2026-10-06T00:00:00Z" if finished else None,
                "id": job_id,
            }
        ),
        encoding="utf-8",
    )
    if model is not None:
        (job_dir / "config.json").write_text(
            json.dumps({"agent": {"model_name": model}}),
            encoding="utf-8",
        )
    trial_result: dict = {
        "id": trial_id,
        "task_name": TASK_REF,
        "trial_name": trial_id,
    }
    if started_at is not None:
        trial_result["started_at"] = started_at
    if finished_at is not None:
        trial_result["finished_at"] = finished_at
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
    if usage is not None:
        (trial_dir / "daytona-usage.json").write_text(json.dumps(usage), encoding="utf-8")
    return job_dir


def waiting_code(service: Executor, spec_id: str) -> str:
    path = service.queue.locate(spec_id, ("waiting",))
    assert path.parent.name == "waiting"
    reasons = list(service.queue.reasons_dir.glob(f"{spec_id}-*.json"))
    assert reasons, f"no reason recorded for {spec_id}"
    return json.loads(reasons[-1].read_text(encoding="utf-8"))["code"]


METER_CREDENTIALS = frozenset({"claude_oauth", "codex_auth", "mimo_selfhosted_api_environment"})
PROBE_UPSTREAM = "http://127.0.0.1:9"


def idle_daytona_snapshot() -> dict:
    return {
        "limits": {"memory_gib": 200.0},
        "safety_fraction": 0.8,
        "used": {"memory_gib": 0.0},
        "pending": {"memory_gib": 0.0},
        "per_sandbox_limits": {"memory_gib": 8.0},
    }


def stub_meter_sources(monkeypatch: pytest.MonkeyPatch, rows, sibling_roots=()):
    """Stub the only external meter seams: Modal billing and sibling checkouts."""
    calls: list[dict] = []

    def fetch(**kwargs):
        calls.append(kwargs)
        return list(rows)

    monkeypatch.setattr("evallab.modal_billing.fetch_modal_billing_report", fetch)
    monkeypatch.setattr(
        "evallab.spend_day.sibling_worktree_roots",
        lambda _root: list(sibling_roots),
    )
    return calls


def app_bill(
    cost: float,
    *,
    object_id: str = "gpu-1",
    description: str = MODAL_APP_NAME,
    resource: str = "gpu",
) -> BillingRow:
    return BillingRow(
        object_id=object_id,
        description=description,
        environment="main",
        interval_start=datetime(2026, 10, 6, tzinfo=UTC),
        resource=resource,
        cost_usd=cost,
    )


def daytona_requested(*, cpu: float = 1, memory_gib: float = 1, disk_gib: float = 1) -> dict:
    return {
        "admission": {"requested": {"cpu": cpu, "memory_gib": memory_gib, "disk_gib": disk_gib}}
    }


def hour_sandbox_usd() -> float:
    cost = estimate_cost_usd(
        backend="daytona",
        sandbox_seconds=3600,
        cpus=1,
        memory_mb=1024,
        storage_mb=1024,
    )
    assert cost is not None and cost > 0
    return cost


def guarded_executor(
    root: Path,
    launched: list,
    probes: list,
    teardowns: list,
    observes: list,
) -> Executor:
    def runner(request):
        launched.append(request.name)
        destination = Path(request.jobs_dir) / request.name
        destination.mkdir(parents=True, exist_ok=True)
        return destination

    def probe(endpoint, model, key, timeout):
        probes.append(endpoint)
        return SelfhostedProbeOutcome(ok=True, status=200, cold=False, detail="ready")

    return make_executor(
        root,
        runner=runner,
        credential_probe=lambda: METER_CREDENTIALS,
        sleeper=lambda _seconds: None,
        selfhosted_probe_fn=probe,
        selfhosted_warmup_seconds=0.05,
        daytona_observe_fn=lambda: observes.append(True) or idle_daytona_snapshot(),
        modal_teardown=lambda *args: teardowns.append(args) or None,
    )


def finish(service: Executor, path: Path, **kwargs):
    spec = service.queue.load(path)
    service.queue.transition(path, "done", actor="executor", event="dispatch_completed")
    write_job(service.repo_root, spec.name, **kwargs)
    return spec


def assert_spend_conserved(root: Path, campaign_id: str):
    breakdown = cap.campaign_spend_breakdown(root, campaign_id)
    spend = cap.campaign_spend_usd(root, campaign_id)
    assert set(breakdown) == {"modal_gpu", "daytona", "model_api", "unmeasured_reserved"}
    assert sum(breakdown.values()) == pytest.approx(spend[2])
    assert spend[0] + spend[1] == pytest.approx(spend[2])
    return spend, breakdown


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


MIMO_MODEL = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
MIMO_SAMPLING = CampaignSampling(temperature=1.0, top_p=0.95, top_k=20)


@pytest.mark.parametrize("size", ["n50", "n18"])
def test_har188_campaigns_declare_only_native_harness_adapters(size: str) -> None:
    from evallab.mimoagent_worker import WRAPPER_ADDITIONS

    root = Path(__file__).resolve().parents[1]
    path = root / f"research/experiments/har188-breadth/har188-breadth-{size}.json"
    campaign = ExperimentCampaign.model_validate_json(path.read_text())
    assert campaign.agent == "mimoagent"
    assert campaign.attempts_per_task == 2
    if size == "n18":
        assert len(campaign.tasks) == 18
        assert len(campaign.tasks) * campaign.attempts_per_task == 36
    assert campaign.sampling == MIMO_SAMPLING
    assert len(campaign.allowed_deviations) == 1
    deviation = campaign.allowed_deviations[0]
    assert deviation.field == "harness.additions"
    assert deviation.value == WRAPPER_ADDITIONS


def test_campaign_mimoagent_sampling_pins_what_agent_sends(tmp_path: Path) -> None:
    """The mimoagent route sends 1.0, not the model-only proxy config (0.6)."""
    assert cap.intended_sampling("mimoagent", MIMO_MODEL) == MIMO_SAMPLING
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    make_campaign(
        tmp_path,
        digest,
        agent="mimoagent",
        model=MIMO_MODEL,
        environment="daytona",
        sampling=MIMO_SAMPLING,
    )

    path, decision = service.submit(
        make_spec(
            "campaign-mimo-a",
            digest,
            agent="mimoagent",
            model=MIMO_MODEL,
            environment="daytona",
        )
    )
    assert decision.admitted
    assert decision.policy_rule == f"campaign:{CAMPAIGN_ID}"
    assert path.parent.name == "approved"


def test_campaign_mimoagent_wrong_sampling_fails_validate(tmp_path: Path) -> None:
    """A campaign pinning 0.6 for agent mimoagent contradicts what it sends."""
    digest = seed_task(tmp_path)
    good = make_campaign(
        tmp_path,
        digest,
        agent="mimoagent",
        model=MIMO_MODEL,
        environment="daytona",
        sampling=MIMO_SAMPLING,
    )
    bad = good.model_copy(
        update={
            "sampling": CampaignSampling(temperature=0.6, top_p=0.95, top_k=20),
        }
    )
    errors = cap.validate_campaign_content(tmp_path, bad)
    assert any("sampling" in error for error in errors)


def test_campaign_terminus_spec_refused_on_mimoagent_campaign(tmp_path: Path) -> None:
    """Same model, different agent: the setup (and sampling) does not match."""
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    make_campaign(
        tmp_path,
        digest,
        agent="mimoagent",
        model=MIMO_MODEL,
        environment="daytona",
        sampling=MIMO_SAMPLING,
    )

    path, decision = service.submit(
        make_spec(
            "campaign-terminus-a",
            digest,
            agent="terminus-2",
            model=MIMO_MODEL,
            environment="daytona",
        )
    )
    assert not decision.admitted
    assert decision.reason_code == "campaign_setup_mismatch"
    assert "terminus-2" in decision.message
    assert "mimoagent" in decision.message
    assert path.parent.name == "waiting"


def test_campaign_cost_estimate_optional_and_pinned(tmp_path: Path) -> None:
    """The HAR-188 cost envelope is optional, digest-pinned approvable content."""
    digest = seed_task(tmp_path)
    plain = make_campaign(tmp_path, digest)
    assert plain.cost_estimate is None
    estimate = cap.CampaignCostEstimate(
        expected_usd=8.71,
        worst_case_usd=60.0,
        formula="E(n)=T*(2.90+0.23148*n)",
    )
    pinned = plain.model_copy(update={"cost_estimate": estimate})
    assert cap.campaign_content_digest(pinned) != cap.campaign_content_digest(plain)
    reloaded = cap.ExperimentCampaign.model_validate_json(pinned.model_dump_json())
    assert reloaded.cost_estimate == estimate


def test_wave_cost_estimate_waves_and_fence() -> None:
    """The HAR-188 wave model: 19-wide clamp, cold start, ceiling worst-case."""
    assert cap.DAYTONA_WAVE_CONCURRENCY == 19
    full = cap.wave_cost_estimate(100)
    assert full.expected_usd == 13.76
    assert full.worst_case_usd == 60.0
    small = cap.wave_cost_estimate(36)
    assert small.expected_usd == 4.92
    assert small.worst_case_usd == 21.6
    assert cap.wave_cost_estimate(38).expected_usd > 5.0
    assert cap.wave_cost_estimate(40).expected_usd > 5.0
    assert cap.fenced_spend_bound(30.0) == 41.4
    assert cap.fenced_spend_bound(5.0) == 16.4


def selfhosted_campaign(root: Path, digest: str, **kwargs) -> ExperimentCampaign:
    kwargs.setdefault("agent", "mimoagent")
    kwargs.setdefault("model", MIMO_MODEL)
    kwargs.setdefault("environment", "daytona")
    kwargs.setdefault("sampling", MIMO_SAMPLING)
    return make_campaign(root, digest, **kwargs)


def selfhosted_spec(name: str, digest: str, **kwargs) -> ExperimentSpec:
    kwargs.setdefault("agent", "mimoagent")
    kwargs.setdefault("model", MIMO_MODEL)
    kwargs.setdefault("environment", "daytona")
    kwargs.setdefault("est_cost_usd", 0.0)
    return make_spec(name, digest, **kwargs)


def silence_meter(monkeypatch: pytest.MonkeyPatch, rows=(), sibling_roots=()):
    monkeypatch.setattr(cap, "LIN_RUNNER", lambda argv, check: None)
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", PROBE_UPSTREAM)
    monkeypatch.setenv("MIMO_SELFHOSTED_API_KEY", "test-key")
    return stub_meter_sources(monkeypatch, rows, sibling_roots)


def test_default_attempts_pin_keeps_digest_attempts_two_changes_it(tmp_path: Path) -> None:
    """Omitting attempts_per_task matches the parsed default of 1."""
    digest = seed_task(tmp_path)
    campaign = make_campaign(tmp_path, digest)
    assert campaign.attempts_per_task == 1
    raw = campaign.model_dump(mode="json", exclude_none=True)
    raw.pop("attempts_per_task")
    parsed = ExperimentCampaign.model_validate(raw)
    assert parsed.attempts_per_task == 1
    assert cap.campaign_content_digest(raw) == cap.campaign_content_digest(campaign)
    assert cap.campaign_content_digest(parsed) == cap.campaign_content_digest(raw)
    assert cap.campaign_content_digest(
        campaign.model_copy(update={"attempts_per_task": 2})
    ) != cap.campaign_content_digest(campaign)


def test_selfhosted_zero_estimate_reserves_wave_average(tmp_path: Path) -> None:
    """Est-0 selfhosted reserves the one-trial wave average, not $0 or $1."""
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    selfhosted_campaign(tmp_path, digest, budget_usd=10)
    per = cap.wave_cost_estimate(1).expected_usd
    assert per == 1.24

    zero_path, zero = service.submit(selfhosted_spec("reserve-zero", digest))
    assert zero.admitted
    assert cap.campaign_spend_usd(tmp_path, CAMPAIGN_ID)[0] == pytest.approx(per)

    declared_path, declared = service.submit(
        selfhosted_spec("reserve-declared", digest, est_cost_usd=3)
    )
    assert declared.admitted
    assert cap.campaign_spend_usd(tmp_path, CAMPAIGN_ID)[0] == pytest.approx(per + 3)

    doubled_path, doubled = service.submit(selfhosted_spec("reserve-attempts", digest, attempts=2))
    assert doubled.admitted
    assert cap.campaign_spend_usd(tmp_path, CAMPAIGN_ID)[0] == pytest.approx(per + 3 + 2 * per)
    assert all(path.parent.name == "approved" for path in (zero_path, declared_path, doubled_path))


def test_zero_estimate_daytona_reserves_same_wave_average(tmp_path: Path) -> None:
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    make_campaign(tmp_path, digest, environment="daytona", budget_usd=10)
    path, decision = service.submit(
        make_spec("daytona-zero-est", digest, environment="daytona", est_cost_usd=0)
    )
    assert decision.admitted
    assert path.parent.name == "approved"
    reserved, settled, total = cap.campaign_spend_usd(tmp_path, CAMPAIGN_ID)
    assert reserved == pytest.approx(cap.wave_cost_estimate(1).expected_usd)
    assert reserved == pytest.approx(1.24)
    assert settled == 0
    assert total == pytest.approx(reserved)


def test_budget_bound_sums_positive_per_trial_gaps(tmp_path: Path) -> None:
    """One over-reserved trial must not erase another trial's ceiling gap."""
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    selfhosted_campaign(tmp_path, digest, budget_usd=10)
    per = cap.wave_cost_estimate(1).expected_usd
    _, low = service.submit(selfhosted_spec("gap-low", digest, cost_limit_usd=0.6))
    _, high = service.submit(selfhosted_spec("gap-high", digest, cost_limit_usd=2.0))
    assert low.admitted and high.admitted
    bound = cap.campaign_budget_bound(tmp_path, CAMPAIGN_ID)
    high_gap = max(0.0, 2.0 - per)
    erased = max(0.0, (0.6 + 2.0) - 2 * per)
    assert bound["in_flight_trials"] == 2
    assert bound["in_flight_worst_case_usd"] == pytest.approx(2.6)
    assert bound["overrun_bound_usd"] == pytest.approx(high_gap)
    assert bound["overrun_bound_usd"] > erased
    assert bound["realized_spend_bound_usd"] == pytest.approx(10 + high_gap)


def test_missing_selfhosted_evidence_counts_wave_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    silence_meter(monkeypatch, [app_bill(9.0)])
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    selfhosted_campaign(tmp_path, digest, environment="docker", budget_usd=10)
    path, decision = service.submit(
        selfhosted_spec("missing-evidence", digest, environment="docker")
    )
    assert decision.admitted
    finish(
        service,
        path,
        reward=1.0,
        job_id="job-no-window",
        trial_id="trial-no-window",
        model=MIMO_MODEL,
    )
    per = cap.wave_cost_estimate(1).expected_usd
    spend, breakdown = assert_spend_conserved(tmp_path, CAMPAIGN_ID)
    assert breakdown["modal_gpu"] == 0
    assert breakdown["unmeasured_reserved"] == pytest.approx(per)
    assert spend[1] == pytest.approx(per)
    assert spend[1] == pytest.approx(1.24)


def test_missing_billing_keeps_known_daytona_and_reserves_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(**_kwargs):
        raise RuntimeError("billing lag")

    monkeypatch.setattr(cap, "LIN_RUNNER", lambda argv, check: None)
    monkeypatch.setattr("evallab.modal_billing.fetch_modal_billing_report", fail)
    monkeypatch.setattr("evallab.spend_day.sibling_worktree_roots", lambda _root: [])
    sandbox = hour_sandbox_usd()
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    selfhosted_campaign(tmp_path, digest, budget_usd=10)
    path, decision = service.submit(selfhosted_spec("missing-bill", digest))
    assert decision.admitted
    finish(
        service,
        path,
        reward=1.0,
        job_id="job-missing-bill",
        trial_id="trial-missing-bill",
        started_at="2026-10-06T00:00:00+00:00",
        finished_at="2026-10-06T01:00:00+00:00",
        model=MIMO_MODEL,
        usage=daytona_requested(),
    )
    per = cap.wave_cost_estimate(1).expected_usd
    spend, breakdown = assert_spend_conserved(tmp_path, CAMPAIGN_ID)
    assert breakdown["modal_gpu"] == 0
    assert breakdown["daytona"] == pytest.approx(sandbox)
    assert breakdown["unmeasured_reserved"] == pytest.approx(max(0.0, per - sandbox))
    assert spend[1] == pytest.approx(sandbox + max(0.0, per - sandbox))


def test_partial_modal_day_keeps_known_gpu_and_reserves_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A midnight-spanning trial keeps the billed day and reserves the gap."""
    known = 0.40
    silence_meter(
        monkeypatch,
        [
            app_bill(known, object_id="serving-day"),
            BillingRow(
                object_id="other-app",
                description="not-the-serving-app",
                environment="main",
                interval_start=datetime(2026, 10, 7, tzinfo=UTC),
                resource="gpu",
                cost_usd=9.0,
            ),
        ],
    )
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    selfhosted_campaign(tmp_path, digest, environment="docker", budget_usd=10)
    path, decision = service.submit(selfhosted_spec("span-midnight", digest, environment="docker"))
    assert decision.admitted
    finish(
        service,
        path,
        reward=1.0,
        job_id="job-span",
        trial_id="trial-span",
        started_at="2026-10-06T22:00:00+00:00",
        finished_at="2026-10-07T02:00:00+00:00",
        model=MIMO_MODEL,
    )
    per = cap.wave_cost_estimate(1).expected_usd
    spend, breakdown = assert_spend_conserved(tmp_path, CAMPAIGN_ID)
    assert breakdown["modal_gpu"] == pytest.approx(known)
    assert breakdown["unmeasured_reserved"] == pytest.approx(max(0.0, per - known))
    assert spend[1] == pytest.approx(known + max(0.0, per - known))


def test_modal_pool_splits_by_overlap_including_outside_trials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sibling = tmp_path / "sibling"
    calls = silence_meter(
        monkeypatch,
        [
            app_bill(12.0, object_id="app-day"),
            app_bill(12.0, object_id="app-day"),
            app_bill(100.0, object_id="ignored", description="some-other-app"),
        ],
        sibling_roots=[sibling],
    )
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    selfhosted_campaign(
        tmp_path, digest, campaign_id="pool-main", environment="docker", budget_usd=20
    )
    selfhosted_campaign(
        tmp_path, digest, campaign_id="pool-other", environment="docker", budget_usd=10
    )
    long_path, long_decision = service.submit(
        selfhosted_spec("pool-long", digest, campaign_id="pool-main", environment="docker")
    )
    short_path, short_decision = service.submit(
        selfhosted_spec("pool-short", digest, campaign_id="pool-main", environment="docker")
    )
    other_path, other_decision = service.submit(
        selfhosted_spec("pool-other-trial", digest, campaign_id="pool-other", environment="docker")
    )
    assert long_decision.admitted and short_decision.admitted and other_decision.admitted
    finish(
        service,
        long_path,
        reward=1.0,
        job_id="job-long",
        trial_id="trial-long",
        started_at="2026-10-06T00:00:00+00:00",
        finished_at="2026-10-06T02:00:00+00:00",
        model=MIMO_MODEL,
    )
    finish(
        service,
        short_path,
        reward=1.0,
        job_id="job-short",
        trial_id="trial-short",
        started_at="2026-10-06T00:00:00+00:00",
        finished_at="2026-10-06T01:00:00+00:00",
        model=MIMO_MODEL,
    )
    finish(
        service,
        other_path,
        reward=1.0,
        job_id="job-other",
        trial_id="trial-other",
        started_at="2026-10-06T01:00:00+00:00",
        finished_at="2026-10-06T02:00:00+00:00",
        model=MIMO_MODEL,
    )
    write_job(
        sibling,
        "outsider",
        reward=1.0,
        job_id="job-outsider",
        trial_id="trial-outsider",
        started_at="2026-10-06T00:00:00+00:00",
        finished_at="2026-10-06T03:00:00+00:00",
        model=MIMO_MODEL,
    )
    _main_spend, main_parts = assert_spend_conserved(tmp_path, "pool-main")
    _other_spend, other_parts = assert_spend_conserved(tmp_path, "pool-other")
    # Weights: 2h + 1h in main, 1h cross-campaign, 3h noncampaign sibling. Pool $12.
    assert other_parts["modal_gpu"] == pytest.approx(12 * 1 / 7)
    assert main_parts["modal_gpu"] == pytest.approx(12 * 3 / 7)
    assert main_parts["modal_gpu"] == pytest.approx(3 * other_parts["modal_gpu"])
    assert main_parts["modal_gpu"] + other_parts["modal_gpu"] == pytest.approx(12 * 4 / 7)
    assert calls and calls[0]["resolution"] == "d"


def test_partial_daytona_keeps_known_spend_unknown_api_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    silence_meter(monkeypatch)
    sandbox = hour_sandbox_usd()
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    make_campaign(tmp_path, digest, environment="daytona", budget_usd=10)
    path, decision = service.submit(
        make_spec("partial-api", digest, environment="daytona", est_cost_usd=2.5)
    )
    assert decision.admitted
    finish(
        service,
        path,
        job_id="job-partial-api",
        trial_id="trial-partial-api",
        started_at="2026-10-06T00:00:00+00:00",
        finished_at="2026-10-06T01:00:00+00:00",
        usage=daytona_requested(),
    )
    spend, breakdown = assert_spend_conserved(tmp_path, CAMPAIGN_ID)
    assert breakdown["model_api"] == 0
    assert breakdown["daytona"] == pytest.approx(sandbox)
    assert breakdown["unmeasured_reserved"] == pytest.approx(max(0.0, 2.5 - sandbox))
    assert breakdown["unmeasured_reserved"] > 0
    assert spend[1] == pytest.approx(2.5)


def test_nonselfhosted_missing_api_cost_falls_back_to_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    silence_meter(monkeypatch)
    digest = seed_task(tmp_path)
    service = make_executor(tmp_path)
    make_campaign(tmp_path, digest, budget_usd=10)
    path, decision = service.submit(make_spec("api-missing", digest, est_cost_usd=1.75))
    assert decision.admitted
    finish(
        service,
        path,
        job_id="job-api-missing",
        trial_id="trial-api-missing",
    )
    spend, breakdown = assert_spend_conserved(tmp_path, CAMPAIGN_ID)
    assert breakdown["model_api"] == 0
    assert breakdown["daytona"] == 0
    assert breakdown["modal_gpu"] == 0
    assert breakdown["unmeasured_reserved"] == pytest.approx(1.75)
    assert spend[1] == pytest.approx(1.75)


def test_settled_gpu_daytona_fences_next_launch_leaves_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settled GPU+Daytona that reaches the budget fences the next launch.

    Tick does not launch, and the trial already running stays running.
    """
    sandbox = hour_sandbox_usd()
    budget = 4.0
    gpu_bill = budget - sandbox / 2
    silence_meter(monkeypatch, [app_bill(gpu_bill)])
    launched: list = []
    probes: list = []
    teardowns: list = []
    observes: list = []
    digest = seed_task(tmp_path)
    service = guarded_executor(tmp_path, launched, probes, teardowns, observes)
    selfhosted_campaign(tmp_path, digest, budget_usd=budget)
    per = cap.wave_cost_estimate(1).expected_usd
    settled_path, settled_decision = service.submit(selfhosted_spec("fence-settled", digest))
    running_path, running_decision = service.submit(selfhosted_spec("fence-running", digest))
    assert settled_decision.admitted and running_decision.admitted
    assert cap.campaign_spend_usd(tmp_path, CAMPAIGN_ID)[0] == pytest.approx(2 * per)
    assert 2 * per + per <= budget

    settled = finish(
        service,
        settled_path,
        reward=1.0,
        job_id="job-fence-settled",
        trial_id="trial-fence-settled",
        started_at="2026-10-06T00:00:00+00:00",
        finished_at="2026-10-06T01:00:00+00:00",
        model=MIMO_MODEL,
        usage=daytona_requested(),
    )
    running = service.queue.load(running_path)
    service.queue.transition(running_path, "running", actor="executor", event="dispatch_started")
    write_job(
        service.repo_root,
        running.name,
        finished=False,
        job_id="job-fence-running",
        trial_id="trial-fence-running",
    )
    _spend, breakdown = assert_spend_conserved(tmp_path, CAMPAIGN_ID)
    assert breakdown["modal_gpu"] == pytest.approx(gpu_bill)
    assert breakdown["daytona"] == pytest.approx(sandbox)
    assert breakdown["modal_gpu"] < budget
    assert breakdown["modal_gpu"] + breakdown["daytona"] >= budget

    refused_path, refused = service.submit(selfhosted_spec("fence-next", digest))
    assert not refused.admitted
    assert refused.reason_code == "campaign_budget_exhausted"
    assert refused_path.parent.name == "waiting"
    assert service.queue.stop_path.is_file()
    refused_id = str(service.queue.load(refused_path).spec_id)
    assert waiting_code(service, refused_id) == "campaign_budget_exhausted"
    assert service.tick() == 0
    assert launched == []
    assert probes == []
    assert service.queue.locate(str(running.spec_id), ("running",)).is_file()
    assert service.queue.locate(refused_id, ("waiting",)).is_file()
    assert service.queue.locate(str(settled.spec_id), ("done",)).is_file()


def test_settled_gpu_daytona_moves_approved_launch_to_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already approved launch is rechecked on tick and fenced."""
    sandbox = hour_sandbox_usd()
    budget = 4.0
    gpu_bill = budget - sandbox / 2
    silence_meter(monkeypatch, [app_bill(gpu_bill)])
    launched: list = []
    probes: list = []
    teardowns: list = []
    observes: list = []
    digest = seed_task(tmp_path)
    service = guarded_executor(tmp_path, launched, probes, teardowns, observes)
    selfhosted_campaign(tmp_path, digest, budget_usd=budget)
    settled_path, settled_decision = service.submit(selfhosted_spec("recheck-settled", digest))
    launch_path, launch_decision = service.submit(selfhosted_spec("recheck-launch", digest))
    assert settled_decision.admitted and launch_decision.admitted
    assert launch_path.parent.name == "approved"
    finish(
        service,
        settled_path,
        reward=1.0,
        job_id="job-recheck-settled",
        trial_id="trial-recheck-settled",
        started_at="2026-10-06T00:00:00+00:00",
        finished_at="2026-10-06T01:00:00+00:00",
        model=MIMO_MODEL,
        usage=daytona_requested(),
    )
    launch_id = str(service.queue.load(launch_path).spec_id)
    assert service.tick() == 0
    assert launched == []
    assert probes == []
    assert observes
    assert service.queue.stop_path.is_file()
    assert service.queue.locate(launch_id, ("waiting",)).is_file()
    assert waiting_code(service, launch_id) == "campaign_budget_exhausted"


def test_thirty_six_trial_pin_admits_nineteen_wide_under_five(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """18 tasks x attempts_per_task 2 admits a 19-wide wave under $5."""
    monkeypatch.setattr(cap, "LIN_RUNNER", lambda argv, check: None)
    digest = seed_task(tmp_path)
    base = selfhosted_campaign(tmp_path, digest, budget_usd=5.0, write=False)
    tasks = [
        CampaignTaskAllowance(task_id=TASK_ID, package_digest=digest),
        *[
            CampaignTaskAllowance(
                task_id=f"format-code-task-{index:06d}",
                package_digest=digest,
            )
            for index in range(1, 18)
        ],
    ]
    pinned = base.model_copy(update={"attempts_per_task": 2, "tasks": tasks})
    assert len(pinned.tasks) * pinned.attempts_per_task == 36
    assert cap.campaign_content_digest(pinned) != cap.campaign_content_digest(base)
    cap.write_campaign(tmp_path, pinned)
    cap.approve_campaign(tmp_path, CAMPAIGN_ID, actor="research-harbor", approved_at=NOW)
    per = cap.wave_cost_estimate(36).expected_usd / 36
    assert cap.wave_cost_estimate(36).expected_usd == 4.92
    assert per != pytest.approx(cap.wave_cost_estimate(18).expected_usd / 18)
    assert 19 * cap.HAR168_TRIAL_CEILING_USD > 5.0
    assert 19 * per < 5.0

    service = make_executor(tmp_path)
    for index in range(19):
        path, decision = service.submit(
            selfhosted_spec(f"wave-{index:02d}", digest, cost_limit_usd=0.6)
        )
        assert decision.admitted, decision.message
        assert path.parent.name == "approved"

    bound = cap.campaign_budget_bound(tmp_path, CAMPAIGN_ID)
    overrun = 19 * (cap.HAR168_TRIAL_CEILING_USD - per)
    assert bound["in_flight_trials"] == 19
    assert bound["in_flight_reserved_usd"] == pytest.approx(19 * per)
    assert bound["in_flight_worst_case_usd"] == pytest.approx(19 * 0.6)
    assert bound["overrun_bound_usd"] == pytest.approx(overrun)
    assert bound["overrun_bound_usd"] == pytest.approx(19 * (0.6 - 4.92 / 36))
    assert bound["realized_spend_bound_usd"] == pytest.approx(5.0 + overrun)


def seed_confirmed_controls(root: Path, tasks: list[CampaignTaskAllowance]) -> None:
    base = root / "research/experiments/python-task-ledger"
    base.mkdir(parents=True, exist_ok=True)
    (base / "ledger.csv").write_text(
        "task_id,status,run,run_digest\n"
        + "".join(f"{task.task_id},usable,original,{task.package_digest}\n" for task in tasks)
    )
    (base / "oracle_sweep.csv").write_text(
        "task_id,label,run_digest,evidence\n"
        + "".join(
            f"{task.task_id},oracle:pass+nop:fail,{task.package_digest},controls.json\n"
            for task in tasks
        )
    )
    locked = root / "research/experiments/har122-egress-lock/har146-locked-nop.csv"
    locked.parent.mkdir(parents=True, exist_ok=True)
    locked.write_text(
        "task_id,locked_nop,note\n" + "".join(f"{task.task_id},sound,\n" for task in tasks)
    )


def adaptive_fixture(
    root: Path,
    *,
    confidence: float = 0.95,
    max_attempts: int = 2,
    budget: float = 10.0,
    tasks: list[CampaignTaskAllowance] | None = None,
    concurrency: int = 1,
    runner=None,
) -> tuple[Executor, ExperimentCampaign, str]:
    digest = seed_task(root)
    tasks = tasks or [CampaignTaskAllowance(task_id=TASK_ID, package_digest=digest)]
    seed_confirmed_controls(root, tasks)
    campaign = make_campaign(
        root,
        digest,
        tasks=tasks,
        budget_usd=budget,
        attempts_per_task=max_attempts,
        concurrency=concurrency,
        adaptive_sampling=cap.CampaignAdaptiveSampling(target_confidence=confidence, seed=42),
    )
    service = make_executor(
        root, runner=runner or (lambda request: write_job(root, request.name, reward=1.0))
    )
    return service, campaign, digest


def stage_draws(
    service: Executor, digest: str, *, task_id: str = TASK_ID, attempts: int = 2, cost: float = 1.0
) -> list[str]:
    ids = []
    for index in range(1, attempts + 1):
        path, decision = service.submit(
            make_spec(
                f"adaptive-{task_id[-6:]}-a{index}",
                digest,
                task_id=task_id,
                campaign_task_attempt=index,
                est_cost_usd=cost,
            )
        )
        assert not decision.admitted
        assert decision.reason_code in {cap.REASON_SAMPLING_QUEUED, cap.REASON_SAMPLING_PENDING}
        ids.append(str(service.queue.load(path).spec_id))
    return ids


def test_adaptive_nonconfirmed_task_refused(tmp_path: Path) -> None:
    service, _, digest = adaptive_fixture(tmp_path)
    (tmp_path / "research/experiments/python-task-ledger/oracle_sweep.csv").unlink()
    path, decision = service.submit(
        make_spec("adaptive-unconfirmed", digest, campaign_task_attempt=1)
    )
    assert path.parent.name == "waiting"
    assert decision.reason_code == cap.REASON_ORACLE_UNCONFIRMED
    assert "no confirmed" in decision.message
    assert service.tick() == 0


def test_adaptive_attempt_two_skipped_when_band_known(tmp_path: Path) -> None:
    service, campaign, digest = adaptive_fixture(tmp_path, confidence=0.6)
    first, second = stage_draws(service, digest)
    assert service.tick(parallel=4) == 1
    assert service.queue.locate(first, ("done",)).is_file()
    assert cap.adaptive_band([True], 2, 0.6) == ("always", pytest.approx(2 / 3))
    assert service.tick() == 0
    assert service.queue.locate(second, ("rejected",)).is_file()
    event = next(
        event
        for event in load_events(service.queue.events_path)
        if event.event == "sampling_skipped"
    )
    assert event.reason_code == cap.REASON_SAMPLING_CLASSIFIED
    assert campaign.adaptive_sampling is not None


def test_adaptive_attempt_two_launched_when_uncertain(tmp_path: Path) -> None:
    service, _, digest = adaptive_fixture(tmp_path)
    first, second = stage_draws(service, digest)
    assert service.tick(parallel=4) == 1
    assert service.queue.locate(first, ("done",)).is_file()
    assert service.queue.locate(second, ("waiting",)).is_file()
    assert cap.adaptive_band([True], 2) == (None, pytest.approx(2 / 3))
    assert service.tick(parallel=4) == 1
    assert service.queue.locate(second, ("done",)).is_file()
    assert service.tick() == 0


def test_adaptive_mixed_prefix_stops_four_attempt_campaign(tmp_path: Path) -> None:
    def runner(request):
        return write_job(tmp_path, request.name, reward=1.0 if request.name.endswith("a1") else 0.0)

    service, _, digest = adaptive_fixture(tmp_path, max_attempts=4, runner=runner)
    ids = stage_draws(service, digest, attempts=4)
    assert service.tick() == 1
    assert service.tick() == 1
    assert service.tick() == 0
    assert all(service.queue.locate(spec_id, ("rejected",)).is_file() for spec_id in ids[2:])
    assert cap.adaptive_band([True, False], 4) == ("sometimes", 1.0)


def test_adaptive_ordering_deterministic_for_seed(tmp_path: Path) -> None:
    digest = seed_task(tmp_path)
    tasks = [
        CampaignTaskAllowance(task_id=f"format-code-task-{number:06}", package_digest=digest)
        for number in (1, 2, 3)
    ]
    _, campaign, _ = adaptive_fixture(tmp_path, tasks=tasks)
    specs = [
        make_spec(f"priority-{number}", digest, task_id=task.task_id, campaign_task_attempt=1)
        for number, task in enumerate(tasks)
    ]
    order = sorted(specs, key=lambda spec: cap.adaptive_priority(tmp_path, campaign, spec))
    reverse_order = sorted(
        reversed(specs), key=lambda spec: cap.adaptive_priority(tmp_path, campaign, spec)
    )
    assert [spec.task_id for spec in order] == [spec.task_id for spec in reverse_order]
    assert len({cap.adaptive_priority(tmp_path, campaign, spec)[2] for spec in specs}) == 3


def test_adaptive_budget_stop_leaves_priority_task_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cap, "LIN_RUNNER", lambda argv, check: None)
    digest = seed_task(tmp_path)
    cheap, expensive = "format-code-task-000001", "format-code-task-000002"
    tasks = [
        CampaignTaskAllowance(task_id=cheap, package_digest=digest, expected_cost_usd=0.6),
        CampaignTaskAllowance(task_id=expensive, package_digest=digest, expected_cost_usd=0.9),
    ]
    service, _, _ = adaptive_fixture(tmp_path, tasks=tasks, budget=1.2)

    def measured(root, campaign_id, *, exclude_spec_id=None):
        settled = sum(
            float(spec.est_cost_usd)
            for _, spec in service.queue.list_specs("done")
            if spec.spec_id != exclude_spec_id
        )
        reserved = sum(
            float(spec.est_cost_usd)
            for _, spec in service.queue.list_specs("approved")
            if spec.spec_id != exclude_spec_id
        )
        return reserved, settled, reserved + settled

    monkeypatch.setattr(cap, "campaign_spend_usd", measured)
    expensive_ids = stage_draws(service, digest, task_id=expensive, cost=0.9)
    cheap_ids = stage_draws(service, digest, task_id=cheap, cost=0.6)
    assert service.tick(parallel=4) == 1
    assert service.tick(parallel=4) == 1
    assert service.tick(parallel=4) == 0
    assert service.queue.stop_path.is_file()
    assert all(service.queue.locate(spec_id, ("done",)).is_file() for spec_id in cheap_ids)
    assert all(service.queue.locate(spec_id, ("waiting",)).is_file() for spec_id in expensive_ids)
    assert cap.adaptive_band([True, True], 2) == ("always", 1.0)


def test_adaptive_context_exhaustion_is_nonpass_not_replaced(tmp_path: Path) -> None:
    service, campaign, digest = adaptive_fixture(tmp_path)
    first, _ = stage_draws(service, digest)
    path = service.queue.locate(first, ("waiting",))
    spec = service.queue.load(path)
    job = write_job(tmp_path, spec.name)
    result = job / "trial-0/result.json"
    native = json.loads(result.read_text())
    native["agent_result"] = {
        "metadata": {"stop_reason": "context_exhausted", "native_exit_status": "ContextExhausted"}
    }
    result.write_text(json.dumps(native))
    service.queue.transition(path, "done", actor="test", event="dispatch_completed")
    assert cap.adaptive_task_outcomes(tmp_path, campaign, TASK_ID) == [False]
    assert cap.reconcile_campaign_replacements(service) == 0
    assert service.tick() == 1


def test_adaptive_attempt_ceiling_and_duplicate_refused(tmp_path: Path) -> None:
    service, _, digest = adaptive_fixture(tmp_path)
    stage_draws(service, digest)
    for index, name, native_attempts in [(1, "duplicate", 1), (3, "over-cap", 1), (1, "batch", 2)]:
        spec = make_spec(f"adaptive-{name}", digest, campaign_task_attempt=index)
        spec = spec.model_copy(update={"attempts": native_attempts})
        _, decision = service.submit(spec)
        assert decision.reason_code == cap.REASON_SAMPLING_INVALID


def test_adaptive_wave_fills_width_in_priority_order(tmp_path: Path) -> None:
    digest = seed_task(tmp_path)
    tasks = [
        CampaignTaskAllowance(
            task_id=f"task-wave-{i}", package_digest=digest, expected_cost_usd=cost
        )
        for i, cost in enumerate((0.2, 0.4, 0.8))
    ]
    service, _, digest = adaptive_fixture(tmp_path, tasks=tasks, concurrency=2)
    ids = {
        task.task_id: stage_draws(
            service, digest, task_id=task.task_id, cost=task.expected_cost_usd or 0
        )
        for task in reversed(tasks)
    }
    assert service.tick(parallel=4) == 2
    assert {spec.task_id for _, spec in service.queue.list_specs("done")} == {
        tasks[0].task_id,
        tasks[1].task_id,
    }
    assert all(service.queue.locate(ids[task.task_id][1], ("waiting",)).is_file() for task in tasks)
    # Both settled-but-uncertain follow-ups jump ahead of the remaining first draw.
    assert service.tick(parallel=4) == 2
    assert all(
        service.queue.locate(spec_id, ("done",)).is_file()
        for task in tasks[:2]
        for spec_id in ids[task.task_id]
    )
    assert service.queue.locate(ids[tasks[2].task_id][0], ("waiting",)).is_file()


def test_adaptive_budget_stop_has_at_most_one_partial_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cap, "LIN_RUNNER", lambda argv, check: None)
    digest = seed_task(tmp_path)
    tasks = [
        CampaignTaskAllowance(task_id=f"task-budget-{i}", package_digest=digest) for i in range(3)
    ]
    service, campaign, digest = adaptive_fixture(
        tmp_path,
        tasks=tasks,
        concurrency=2,
        budget=1.8,
        runner=lambda request: write_job(tmp_path, request.name, reward=1.0, cost=0.6),
    )
    ids = {
        task.task_id: stage_draws(service, digest, task_id=task.task_id, cost=0.6)
        for task in reversed(tasks)
    }
    ordered = sorted(
        tasks,
        key=lambda task: cap.adaptive_priority(
            tmp_path,
            campaign,
            service.queue.load(service.queue.locate(ids[task.task_id][0], ("waiting",))),
        ),
    )
    assert service.tick(parallel=4) == 2
    assert service.tick(parallel=4) == 1
    assert service.tick(parallel=4) == 0
    assert service.queue.stop_path.is_file()
    assert cap.adaptive_task_outcomes(tmp_path, campaign, ordered[0].task_id) == [True, True]
    assert cap.adaptive_task_outcomes(tmp_path, campaign, ordered[1].task_id) == [True]
    assert cap.adaptive_task_outcomes(tmp_path, campaign, ordered[2].task_id) == []


def test_adaptive_duplicate_does_not_poison_original(tmp_path: Path) -> None:
    service, _, digest = adaptive_fixture(tmp_path)
    original, _ = stage_draws(service, digest)
    _, refusal = service.submit(make_spec("late-duplicate", digest, campaign_task_attempt=1))
    assert refusal.reason_code == cap.REASON_SAMPLING_INVALID
    assert service.tick() == 1
    assert service.queue.locate(original, ("done",)).is_file()


def test_adaptive_tick_filter_does_not_release_other_task(tmp_path: Path) -> None:
    service, _, digest = adaptive_fixture(tmp_path)
    first, second = stage_draws(service, digest)
    assert service.tick(spec_ids=[second]) == 0
    assert service.queue.locate(first, ("waiting",)).is_file()
    assert service.tick(spec_ids=[first]) == 1


@pytest.mark.parametrize("label", ["oracle:none", "oracle:fail-network", "oracle:unknown"])
def test_adaptive_sound_solve_tag_is_not_oracle_confirmation(tmp_path: Path, label: str) -> None:
    service, _, digest = adaptive_fixture(tmp_path)
    sweep = tmp_path / "research/experiments/python-task-ledger/oracle_sweep.csv"
    sweep.write_text(sweep.read_text().replace("oracle:pass+nop:fail", label))
    _, refusal = service.submit(make_spec("not-oracle-confirmed", digest, campaign_task_attempt=1))
    assert refusal.reason_code == cap.REASON_ORACLE_UNCONFIRMED


def test_adaptive_oracle_label_wrong_package_refused(tmp_path: Path) -> None:
    service, _, digest = adaptive_fixture(tmp_path)
    sweep = tmp_path / "research/experiments/python-task-ledger/oracle_sweep.csv"
    sweep.write_text(sweep.read_text().replace(digest, "sha256:" + "a" * 64))
    _, refusal = service.submit(make_spec("wrong-control-package", digest, campaign_task_attempt=1))
    assert refusal.reason_code == cap.REASON_ORACLE_UNCONFIRMED


def test_adaptive_infra_replacement_preserves_scientific_draw(tmp_path: Path) -> None:
    def runner(request):
        return write_job(
            tmp_path,
            request.name,
            exception="DaytonaNotFoundError" if request.name.endswith("-a1") else None,
            reward=None if request.name.endswith("-a1") else 1.0,
        )

    service, campaign, digest = adaptive_fixture(tmp_path, runner=runner)
    first, second = stage_draws(service, digest)
    assert service.tick() == 1
    replacements = [
        spec for _, spec in service.queue.list_specs("waiting") if spec.campaign_replaces == first
    ]
    assert len(replacements) == 1
    assert replacements[0].campaign_task_attempt == 1
    assert cap.adaptive_task_outcomes(tmp_path, campaign, TASK_ID) == []
    assert service.queue.locate(second, ("waiting",)).is_file()
    assert service.tick() == 1
    assert cap.adaptive_task_outcomes(tmp_path, campaign, TASK_ID) == [True]
    assert service.tick() == 1
    assert cap.adaptive_task_outcomes(tmp_path, campaign, TASK_ID) == [True, True]
    assert cap.reconcile_campaign_replacements(service) == 0


def test_adaptive_explicit_control_does_not_need_older_nop_census(tmp_path: Path) -> None:
    service, _, digest = adaptive_fixture(tmp_path)
    (tmp_path / "research/experiments/har122-egress-lock/har146-locked-nop.csv").unlink()
    _, decision = service.submit(
        make_spec("new-confirmed-control", digest, campaign_task_attempt=1)
    )
    assert decision.reason_code == cap.REASON_SAMPLING_QUEUED


def test_adaptive_digestless_sweep_cannot_retarget_current_package(tmp_path: Path) -> None:
    service, _, digest = adaptive_fixture(tmp_path)
    sweep = tmp_path / "research/experiments/python-task-ledger/oracle_sweep.csv"
    sweep.write_text(sweep.read_text().replace(digest, ""))
    _, refusal = service.submit(make_spec("digestless-control", digest, campaign_task_attempt=1))
    assert refusal.reason_code == cap.REASON_ORACLE_UNCONFIRMED


def test_campaign_validate_reports_adaptive_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from argparse import Namespace

    from evallab.cli import _campaign_validate_command, run_cli

    _, campaign, _ = adaptive_fixture(tmp_path, concurrency=3)
    monkeypatch.setattr(cap, "validate_campaign_content", lambda root, draft: [])
    args = Namespace(
        campaign=cap.campaign_dir(tmp_path, campaign.campaign_id) / cap.CAMPAIGN_FILENAME,
        spec=[],
        json=True,
    )
    assert _campaign_validate_command(args, tmp_path) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["attempts_per_task"] == 2
    assert summary["concurrency"] == 3
    assert summary["adaptive_sampling"] == {"target_confidence": 0.95, "seed": 42}
    assert summary["oracle_confirmed_tasks"] == 1
    assert run_cli(["campaign", "status", str(args.campaign), "--json"], workspace=tmp_path) == 0
    summary.pop("errors")
    summary.pop("specs")
    assert summary == json.loads(capsys.readouterr().out)


def test_adaptive_wave_uses_har189_zero_estimate_reservations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    silence_meter(monkeypatch)
    digest = seed_task(tmp_path)
    tasks = [
        CampaignTaskAllowance(task_id=f"task-reservation-{i}", package_digest=digest)
        for i in range(20)
    ]
    seed_confirmed_controls(tmp_path, tasks)
    campaign = make_campaign(
        tmp_path,
        digest,
        tasks=tasks,
        attempts_per_task=2,
        concurrency=19,
        budget_usd=3.0,
        environment="daytona",
        adaptive_sampling=cap.CampaignAdaptiveSampling(),
    )
    service = make_executor(
        tmp_path,
        runner=lambda request: write_job(
            tmp_path, request.name, reward=1.0, job_id=f"job-{request.name}"
        ),
        daytona_observe_fn=lambda: {
            "limits": {"memory_gib": 200.0},
            "safety_fraction": 0.8,
            "used": {"memory_gib": 0.0},
            "pending": {"memory_gib": 0.0},
            "per_sandbox_limits": {"memory_gib": 8.0},
        },
    )
    for task in tasks:
        for index in (1, 2):
            _, decision = service.submit(
                make_spec(
                    f"zero-{task.task_id}-a{index}",
                    digest,
                    task_id=task.task_id,
                    environment="daytona",
                    est_cost_usd=0,
                    campaign_task_attempt=index,
                )
            )
            assert not decision.admitted
            assert decision.reason_code in {cap.REASON_SAMPLING_QUEUED, cap.REASON_SAMPLING_PENDING}
    assert service.tick(parallel=19) == 19
    done = service.queue.list_specs("done")
    assert len(done) == 19
    per_trial = cap._reservation_usd(done[0][1].model_dump(mode="json"), campaign)
    assert per_trial < 0.6
    assert cap.campaign_spend_usd(tmp_path, campaign.campaign_id)[2] == pytest.approx(
        19 * per_trial
    )
    assert service.tick(parallel=19) == 0
    assert service.queue.stop_path.is_file()
    assert (
        sum(bool(cap.adaptive_task_outcomes(tmp_path, campaign, task.task_id)) for task in tasks)
        == 19
    )
