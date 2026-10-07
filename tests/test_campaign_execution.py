"""Campaign dispatch optimization never replaces admission or native evidence."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab import campaign_approval as cap
from evallab.campaign_execution import (
    CampaignExecutionPolicy,
    CampaignSetupQualification,
    docker_available_resources,
    docker_task_resources,
    qualification_evidence_digest,
    qualification_matches_request,
    validate_qualification,
)
from evallab.dispatch_guards import SelfhostedProbeOutcome
from evallab.execution_contracts import (
    LOCKED_DOCKER_ENVIRONMENT_IMPORT_PATH,
    MIMO_SELFHOSTED_MODEL_SELECTOR,
    resolve_harbor_agent,
)
from evallab.queue import DirectoryQueue, Executor, load_events
from evallab.registry import compute_task_digests
from evallab.schemas import AutoRunRule, ExperimentSpec, StandingApprovalsPolicy
from evallab.setup_fingerprint import lock_setup_fingerprint

NOW = datetime(2026, 10, 7, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolate_escalations(monkeypatch):
    monkeypatch.setattr(cap, "LIN_RUNNER", lambda *args, **kwargs: None)


def seed(root: Path):
    task = root / "task"
    task.mkdir()
    (task / "task.toml").write_text(
        '[task]\nname = "fixture"\n[environment]\ncpus = 2\nmemory_mb = 8192\n'
    )
    (task / "instruction.md").write_text("Offline protocol fixture, not a scientific task.\n")
    serving = root / "tools/modal-mimo-serve/serve.py"
    serving.parent.mkdir(parents=True)
    serving.write_text('CONTEXT_LENGTH = 262144\nTOOL_CALL_PARSER = "qwen3_coder"\n')
    native = root / "tools/mimoagent-harbor/swe.yaml"
    native.parent.mkdir(parents=True)
    native.write_text("agent:\n  step_limit: 500\n")
    digest = compute_task_digests(task).package
    spec = ExperimentSpec(
        name="qualified-first",
        hypothesis="scoped execution",
        purpose="practice",
        task="task",
        task_path="task",
        task_id="fixture-task",
        task_package_digest=digest,
        agent="mimoagent",
        model=MIMO_SELFHOSTED_MODEL_SELECTOR,
        environment="docker",
        egress_lock=True,
        submitted_by="test",
        campaign_id="execution-fixture",
        reference_profile="fixture",
        est_cost_usd=1,
        max_requests=100,
        max_input_tokens=10000,
        max_output_tokens=10000,
        max_total_tokens=20000,
        cost_limit_usd=1,
    )
    return spec, digest


def native_qualification(root: Path, spec: ExperimentSpec) -> CampaignSetupQualification:
    request = Executor.prepare_request(spec, repo_root=root)
    trial = root / "runs/qualification/trial"
    trial.mkdir(parents=True)
    agent = {
        "name": resolve_harbor_agent(request.agent, request.model),
        "model_name": request.model,
        "env": {"EVALLAB_SETUP_FINGERPRINT": lock_setup_fingerprint(request, repo_root=root)},
    }
    environment = {
        "import_path": LOCKED_DOCKER_ENVIRONMENT_IMPORT_PATH,
        "kwargs": {"egress_lock": True},
    }
    config = {"agent": agent, "environment": environment}
    for name, data in {
        "config.json": config,
        "lock.json": {"schema_version": 2, "install_only": False, **config},
        "result.json": {
            "finished_at": NOW.isoformat(),
            "config": config,
            "exception_info": None,
            "verifier_result": {"rewards": {"reward": 0.0}},
        },
        "egress-lock.json": {"requested": True, "applied": True, "error": None},
    }.items():
        (trial / name).write_text(json.dumps(data))
    return CampaignSetupQualification(
        trial_dir=str(trial), evidence_digest=qualification_evidence_digest(trial)
    )


def campaign(root, spec, digest, qualification, *, limit=2, model_host="modal", estimate=None):
    record = cap.ExperimentCampaign(
        campaign_id=spec.campaign_id,
        budget_usd=10,
        cost_estimate=estimate,
        tasks=[cap.CampaignTaskAllowance(task_id=spec.task_id, package_digest=digest)],
        reference_profile=spec.reference_profile,
        require_egress_lock=True,
        agent=spec.agent,
        model=spec.model,
        environment=spec.environment,
        sampling=cap.intended_sampling(spec.agent, spec.model),
        queue_cwd=str(root),
        linear_card="HAR-192",
        submitted_by="test",
        created_at=NOW,
        execution=CampaignExecutionPolicy(
            max_concurrent_trials=limit, model_host=model_host, qualification=qualification
        ),
    )
    cap.write_campaign(root, record)
    cap.approve_campaign(root, record.campaign_id, actor="test", approved_at=NOW)
    return record


def executor(root, monkeypatch, runner, *, capacity=(46.0, 190 * 1024), **kwargs):
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", "http://127.0.0.1:12345")
    monkeypatch.setenv("MIMO_SELFHOSTED_API_KEY", "fixture-native-key")
    return Executor(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=StandingApprovalsPolicy(
            daily_cost_ceiling_usd=100,
            per_job_cost_ceiling_usd=10,
            quiet_failure_rule=3,
            auto_run=[AutoRunRule(name="controls", agents=["nop", "oracle"])],
            escalate_to_human=[],
        ),
        runner=runner,
        ingester=lambda _: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: frozenset({"mimo_selfhosted_api_environment"}),
        docker_observe_fn=lambda: capacity,
        watch_enabled=False,
        selfhosted_probe_fn=lambda *args: SelfhostedProbeOutcome(
            ok=True, status=200, cold=False, detail="fixture"
        ),
        **kwargs,
    )


def test_qualified_campaign_dispatches_in_parallel_and_leaves_overflow_approved(
    tmp_path, monkeypatch
):
    spec, digest = seed(tmp_path)
    proof = native_qualification(tmp_path, spec)
    campaign(tmp_path, spec, digest, proof)
    barrier = threading.Barrier(2)
    ran = []

    def run(request):
        barrier.wait(timeout=5)
        ran.append(request.name)
        return request.jobs_dir / request.name

    service = executor(tmp_path, monkeypatch, run)
    ids = []
    for index in range(3):
        path, decision = service.submit(spec.model_copy(update={"name": f"qualified-{index}"}))
        assert decision.admitted
        ids.append(service.queue.load(path).spec_id)
    assert service.tick() == 2
    assert set(ran) == {"qualified-0", "qualified-1"}
    assert service.queue.locate(ids[2]).parent.name == "approved"
    events = load_events(service.queue.events_path)
    assert any(event.event == "smoke_gate_campaign_qualified" for event in events)
    assert not service.queue.stop_path.exists()


@pytest.mark.parametrize("mutation", ["context", "parser", "source", "backend", "budget"])
def test_setup_drift_cannot_inherit_qualification(tmp_path, mutation):
    spec, _ = seed(tmp_path)
    proof = native_qualification(tmp_path, spec)
    if mutation == "context":
        path = tmp_path / "tools/modal-mimo-serve/serve.py"
        path.write_text(path.read_text().replace("262144", "65536"))
    elif mutation == "parser":
        path = tmp_path / "tools/modal-mimo-serve/serve.py"
        path.write_text(path.read_text().replace("qwen3_coder", "mimo"))
    elif mutation == "source":
        path = tmp_path / "tools/modal-mimo-serve/serve.py"
        path.write_text(path.read_text() + "MAX_RUNNING_REQUESTS = 1\n")
    elif mutation == "backend":
        spec = spec.model_copy(update={"environment": "daytona"})
    else:
        spec = spec.model_copy(update={"max_requests": 101})
    assert not qualification_matches_request(
        proof, Executor.prepare_request(spec, repo_root=tmp_path), repo_root=tmp_path
    )


@pytest.mark.parametrize(
    "change", ["ungraded", "infra", "unfinished", "unlocked", "different-config"]
)
def test_changed_or_invalid_native_evidence_cannot_qualify(tmp_path, change):
    spec, _ = seed(tmp_path)
    proof = native_qualification(tmp_path, spec)
    trial = Path(proof.trial_dir)
    result = json.loads((trial / "result.json").read_text())
    if change == "ungraded":
        result["verifier_result"] = {"rewards": {"reward": None}}
    elif change == "infra":
        result["exception_info"] = {"exception_type": "RuntimeError"}
    elif change == "unfinished":
        result["finished_at"] = None
    elif change == "unlocked":
        (trial / "egress-lock.json").write_text('{"applied": false}')
    else:
        result["config"]["agent"]["model_name"] = "another-model"
    (trial / "result.json").write_text(json.dumps(result))
    with pytest.raises(ValueError):
        validate_qualification(proof, repo_root=tmp_path)
    updated = proof.model_copy(update={"evidence_digest": qualification_evidence_digest(trial)})
    with pytest.raises(ValueError):
        validate_qualification(updated, repo_root=tmp_path)


def test_policy_change_invalidates_approval_and_absence_preserves_historical_digest(tmp_path):
    spec, digest = seed(tmp_path)
    record = campaign(tmp_path, spec, digest, None)
    original = record.model_copy(update={"execution": None})
    old_payload = original.model_dump(mode="json", exclude_none=True)
    old_payload.pop("execution", None)
    assert cap.campaign_content_digest(original) == cap.campaign_content_digest(old_payload)
    changed = record.model_copy(
        update={"execution": record.execution.model_copy(update={"max_concurrent_trials": 3})}
    )
    (cap.campaign_dir(tmp_path, record.campaign_id) / cap.CAMPAIGN_FILENAME).write_text(
        changed.model_dump_json()
    )
    with pytest.raises(cap.CampaignApprovalError):
        cap.load_approved_campaign(tmp_path, record.campaign_id)


def test_docker_capacity_respects_declared_memory_and_does_not_resize(tmp_path, monkeypatch):
    spec, digest = seed(tmp_path)
    proof = native_qualification(tmp_path, spec)
    campaign(tmp_path, spec, digest, proof, limit=20)
    ran = []
    service = executor(
        tmp_path,
        monkeypatch,
        lambda request: ran.append(request.name) or request.jobs_dir / request.name,
        capacity=(40.0, 16 * 1024),
    )
    for index in range(3):
        assert service.submit(spec.model_copy(update={"name": f"memory-{index}"}))[1].admitted
    assert service.tick() == 2
    assert len(service.queue.list_specs("approved")) == 1
    assert docker_task_resources(tmp_path / "task") == (2.0, 8192)


def test_unbounded_existing_container_refuses_host_capacity(monkeypatch):
    def command(argv, **kwargs):
        from types import SimpleNamespace

        if argv[1] == "info":
            payload = {"OSType": "linux", "NCPU": 48, "MemTotal": 192 * 1024**3}
        elif argv[1] == "ps":
            return SimpleNamespace(stdout="some-container\n")
        else:
            payload = [{"HostConfig": {"NanoCpus": 0, "Memory": 0}}]
        return SimpleNamespace(stdout=json.dumps(payload))

    monkeypatch.setattr("evallab.campaign_execution.subprocess.run", command)
    with pytest.raises(ValueError):
        docker_available_resources()


def test_infra_replacement_is_admitted_before_the_model_can_be_stopped(tmp_path, monkeypatch):
    from evallab import modal_ops

    spec, digest = seed(tmp_path)
    campaign(tmp_path, spec, digest, native_qualification(tmp_path, spec), limit=1)
    stopped = []
    monkeypatch.setattr(modal_ops, "daytona_sandbox_counts", lambda: None)

    def run(request):
        job = request.jobs_dir / request.name
        trial = job / "trial"
        trial.mkdir(parents=True)
        (job / "result.json").write_text(
            json.dumps(
                {
                    "n_total_trials": 1,
                    "stats": {},
                    "finished_at": NOW.isoformat(),
                    "id": "fixture-job",
                }
            )
        )
        (trial / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "fixture-task",
                    "trial_name": "trial",
                    "exception_info": {"exception_type": "DaytonaNotFoundError"},
                }
            )
        )
        return job

    def no_provider_stop(argv):
        stopped.append(argv)
        raise RuntimeError("provider operations are not allowed in this test")

    service = executor(
        tmp_path,
        monkeypatch,
        run,
        modal_teardown=lambda queue, root, candidates: modal_ops.stop_selfhosted_app_if_drained(
            queue,
            root,
            candidates,
            runner=no_provider_stop,
        ),
    )
    path, decision = service.submit(spec)
    assert decision.admitted
    original_id = service.queue.load(path).spec_id
    assert service.tick() == 1
    replacements = [item for _, item in service.queue.list_specs("approved")]
    assert [item.campaign_replaces for item in replacements] == [original_id]
    assert stopped == []


def test_teardown_error_does_not_repeat_a_completed_trial(tmp_path, monkeypatch):
    spec, digest = seed(tmp_path)
    campaign(tmp_path, spec, digest, native_qualification(tmp_path, spec), limit=1)
    calls = []

    def failed_teardown(*args):
        raise RuntimeError("provider unavailable")

    service = executor(
        tmp_path,
        monkeypatch,
        lambda request: calls.append(request.name) or request.jobs_dir / request.name,
        modal_teardown=failed_teardown,
    )
    path, decision = service.submit(spec)
    assert decision.admitted
    spec_id = service.queue.load(path).spec_id
    assert service.tick() == 1
    assert service.queue.locate(spec_id).parent.name == "done"
    assert service.tick() == 0
    assert calls == [spec.name]


def test_mixed_modal_and_runpod_window_still_stops_the_modal_app(tmp_path, monkeypatch):
    spec, digest = seed(tmp_path)
    modal = spec.model_copy(update={"name": "modal-one", "campaign_id": "modal-policy"})
    runpod = spec.model_copy(update={"name": "runpod-one", "campaign_id": "runpod-policy"})
    campaign(tmp_path, modal, digest, None)
    campaign(tmp_path, runpod, digest, None, model_host="runpod")
    calls = []
    service = executor(
        tmp_path,
        monkeypatch,
        lambda request: request.jobs_dir / request.name,
        modal_teardown=lambda queue, root, candidates: calls.append(
            sorted(item.name for item in candidates)
        ),
    )
    service._maybe_stop_selfhosted_app([runpod])
    assert calls == []
    service._maybe_stop_selfhosted_app([modal, runpod])
    assert calls == [["modal-one", "runpod-one"]]


def test_runpod_policy_cannot_borrow_unrecorded_host_qualification(tmp_path):
    spec, _ = seed(tmp_path)
    proof = native_qualification(tmp_path, spec)
    CampaignExecutionPolicy(max_concurrent_trials=2, model_host="modal", qualification=proof)
    with pytest.raises(ValueError, match="Modal model host"):
        CampaignExecutionPolicy(max_concurrent_trials=2, model_host="runpod", qualification=proof)


def test_locked_docker_control_reserves_the_approved_vm_envelope(tmp_path):
    spec, digest = seed(tmp_path)
    estimate = cap.CampaignCostEstimate(expected_usd=1, worst_case_usd=2, formula="fixture VM")
    record = campaign(tmp_path, spec, digest, None, estimate=estimate)
    control = {"agent": "nop", "environment": "docker", "egress_lock": True}
    assert cap._reservation_usd(control, record) == pytest.approx(1)
    assert cap._reservation_usd(control, record.model_copy(update={"execution": None})) == 0


def settled_meter_fixture(root, spec):
    """Native-shaped retained evidence; no provider or trial is executed."""
    state = root / "queue/done"
    state.mkdir(parents=True, exist_ok=True)
    (state / f"{spec.name}.json").write_text(spec.model_dump_json())
    job = root / "runs" / spec.name
    trial = job / "trial"
    trial.mkdir(parents=True)
    config = {"agent": {"model_name": spec.model}}
    (job / "experiment-spec.json").write_text(spec.model_dump_json())
    (job / "config.json").write_text(json.dumps(config))
    (job / "result.json").write_text(
        json.dumps(
            {
                "id": f"job-{spec.name}",
                "n_total_trials": 1,
                "stats": {},
                "finished_at": "2026-10-07T01:00:00Z",
            }
        )
    )
    (trial / "config.json").write_text(json.dumps(config))
    (trial / "result.json").write_text(
        json.dumps(
            {
                "id": f"trial-{spec.name}",
                "task_name": "fixture-task",
                "trial_name": "trial",
                "started_at": "2026-10-07T00:00:00Z",
                "finished_at": "2026-10-07T01:00:00Z",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        )
    )


def test_new_provider_costs_stay_reserved_and_cannot_take_modal_dollars(tmp_path, monkeypatch):
    from evallab.modal_billing import BillingRow
    from evallab.modal_ops import MODAL_APP_NAME

    spec, digest = seed(tmp_path)
    estimate = cap.CampaignCostEstimate(
        expected_usd=0.5, worst_case_usd=1, formula="fixture all-in"
    )
    modal = spec.model_copy(
        update={"name": "modal-one", "campaign_id": "modal-policy", "est_cost_usd": 0}
    )
    runpod = spec.model_copy(
        update={"name": "runpod-one", "campaign_id": "runpod-policy", "est_cost_usd": 0}
    )
    campaign(tmp_path, modal, digest, None, estimate=estimate)
    campaign(tmp_path, runpod, digest, None, model_host="runpod", estimate=estimate)
    for item in (modal, runpod):
        settled_meter_fixture(tmp_path, item)
    fetches = []

    def bill(**kwargs):
        fetches.append(kwargs)
        return [
            BillingRow(
                object_id="modal-app",
                description=MODAL_APP_NAME,
                environment="main",
                interval_start=NOW,
                resource="gpu",
                cost_usd=0.2,
            )
        ]

    monkeypatch.setattr("evallab.modal_billing.fetch_modal_billing_report", bill)
    monkeypatch.setattr("evallab.spend_day.sibling_worktree_roots", lambda root: [])
    runpod_cost = cap.campaign_spend_breakdown(tmp_path, "runpod-policy")
    assert fetches == []
    assert runpod_cost["modal_gpu"] == 0
    assert runpod_cost["unmeasured_reserved"] == pytest.approx(0.5)
    modal_cost = cap.campaign_spend_breakdown(tmp_path, "modal-policy")
    # The unrelated Runpod wall interval must not halve the actual Modal pool.
    assert modal_cost["modal_gpu"] == pytest.approx(0.2)
    # GPU billing alone cannot silently settle the shared VM rent at zero.
    assert modal_cost["unmeasured_reserved"] == pytest.approx(0.3)
    assert cap.campaign_spend_usd(tmp_path, "modal-policy")[2] == pytest.approx(0.5)
