from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_campaigns import _executor, _orchestrator
from test_task_workbench import _bundle, _copy_candidate, _inspect

import evallab.queue as queue_module
from evallab.benchmark_program_contracts import (
    CampaignCalibrationLedger,
    SyntheticFamilyType,
)
from evallab.campaigns import (
    CampaignDefinition,
    CampaignDefinitionAttempt,
    CampaignLimits,
    ControlBootstrapRuntimeIdentity,
    TrialLimits,
    build_campaign_manifest,
)
from evallab.evidence.facts import AnalyzerCallResult, run_trial_analysis
from evallab.evidence_store import EvidenceLocator, materialize_evidence
from evallab.execution_contracts import RunRequest
from evallab.queue import ExecutionFailure, load_events
from evallab.registry import (
    TaskCertificationError,
    TaskRegistry,
    harbor_task_digest,
    promote_task,
    task_runtime_identity,
)
from evallab.results import load_job
from evallab.schemas import (
    NETWORK_ESCAPE_CLASSES,
    ExperimentMatrix,
    ExperimentSpec,
    NetworkEscapeProbeResultV1,
    NetworkIsolationDispatchIdentityV1,
    NetworkIsolationEvidenceV1,
    NetworkIsolationProbeIdentityV1,
    NetworkIsolationRuntimeIdentityV1,
    NetworkPolicyEvidenceV1,
    TaskRegistryRecord,
    build_network_isolation_evidence,
)
from evallab.task_workbench import check_candidate, write_packet

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
MATRIX_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
LEDGER_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
FAMILY = SyntheticFamilyType.FAMILY_A_STATE_INVERSION


def _causal_isolation_evidence(adapter: str) -> NetworkIsolationEvidenceV1:
    policy = NetworkPolicyEvidenceV1(mode="no-network")
    digest = "sha256:" + "a" * 64
    return build_network_isolation_evidence(
        requested_agent_policy=policy,
        effective_agent_policy=policy,
        requested_verifier_policy=policy,
        effective_verifier_policy=policy,
        requested_verifier_phase_policy=policy,
        effective_verifier_phase_policy=policy,
        runtime_identity=NetworkIsolationRuntimeIdentityV1(
            platform_system="Linux",
            platform_release="test",
            platform_machine="arm64",
            container_runtime="docker",
            container_runtime_version="29.4.1",
            container_image_digest=digest,
            adapter=adapter,
            adapter_version="1",
            adapter_digest="sha256:" + "b" * 64,
        ),
        probe_identity=NetworkIsolationProbeIdentityV1(
            implementation="test-probe",
            implementation_version="1",
            implementation_digest="sha256:" + "c" * 64,
            config_digest="sha256:" + "d" * 64,
        ),
        probe_results=tuple(
            NetworkEscapeProbeResultV1(
                escape_class=escape_class,
                target=f"http://target.invalid/{escape_class}",
                outcome="blocked",
                detail="blocked",
            )
            for escape_class in NETWORK_ESCAPE_CLASSES
        ),
        observed_at=NOW,
        valid_until=NOW + timedelta(days=7),
        evaluated_at=NOW,
    )


def _stage_task(tmp_path: Path) -> tuple[Path, Path, Path, TaskRegistryRecord]:
    repo, task = _copy_candidate(tmp_path)
    policy = repo / "policy/standing-approvals.yaml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text((ROOT / "policy/standing-approvals.yaml").read_text())
    inspection = _inspect(repo, task)
    report = check_candidate(
        inspection,
        _bundle(inspection, repo=repo, task=task),
        repo_root=repo,
    )
    _, certification_path = write_packet(repo_root=repo, report=report)
    staged = promote_task(
        task,
        repo,
        task_id="uppercase-fixture",
        task_family=FAMILY.value,
        state="registered",
        actor="admission-reviewer",
        approved_at=NOW,
        certification_path=certification_path,
        stage_controls=True,
    )
    return repo, task, certification_path, staged


def _write_matrix_catalog(repo: Path, staged: TaskRegistryRecord) -> None:
    matrix = ExperimentMatrix.model_validate(
        {
            "schema_version": 2,
            "matrix_id": MATRIX_ID,
            "name": "control-bootstrap",
            "hypothesis": "Live-bound controls establish promotion evidence",
            "benchmark_family": FAMILY.value,
            "task_id": staged.task_id,
            "task": staged.task_path,
            "task_package_digest": staged.digests.package,
            "verifier_digest": staged.digests.verifier,
            "environment": "docker",
            "jobs_dir": "runs",
            "concurrency": 1,
            "timeout_seconds": 60,
            "runs": [
                {"name": "bootstrap-oracle", "agent": "oracle"},
                {"name": "bootstrap-nop", "agent": "nop"},
            ],
        }
    )
    experiments = repo / "research/experiments"
    experiments.mkdir(parents=True, exist_ok=True)
    matrix_path = experiments / "control-bootstrap.json"
    matrix_path.write_text(matrix.model_dump_json(indent=2) + "\n")
    matrix_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                matrix.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    (experiments / "matrix-registry.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "matrices": [
                    {
                        "matrix_id": MATRIX_ID,
                        "path": "research/experiments/control-bootstrap.json",
                        "matrix_digest": matrix_digest,
                    }
                ],
            }
        )
    )


def _campaign_definition(
    staged: TaskRegistryRecord,
    *,
    include_runtime: bool = True,
) -> CampaignDefinition:
    attempts: list[CampaignDefinitionAttempt] = []
    limits = TrialLimits(
        max_requests=0,
        max_cost_usd=0,
        max_input_tokens=0,
        max_output_tokens=0,
        max_total_tokens=0,
        max_wall_clock_seconds=60,
    )
    for index, agent in enumerate(("oracle", "nop"), start=1):
        evidence = _causal_isolation_evidence(agent)
        runtime = ControlBootstrapRuntimeIdentity(
            adapter=agent,
            network_isolation_evidence=evidence,
            network_isolation_evidence_digest=evidence.evidence_digest,
            network_isolation_status=evidence.status,
            network_isolation_reason=evidence.reason,
            analysis_eligibility=evidence.analysis_eligibility,
            evaluated_at=NOW,
        )
        raw: dict[str, Any] = {
            "cell_id": f"control-{agent}",
            "task_id": staged.task_id,
            "attempt": index,
            "spec": ExperimentSpec(
                name=f"definition-{agent}",
                hypothesis="Control bootstrap must be live-bound",
                purpose="baseline",
                task=f"registered/{staged.task_id}",
                task_family=FAMILY.value,
                agent=agent,
                jobs_dir="runs",
                concurrency=1,
                timeout_seconds=60,
                submitted_by="test",
            ).model_dump(mode="json"),
            "limits": limits.model_dump(mode="json"),
        }
        if include_runtime:
            raw["control_runtime_identity"] = runtime.model_dump(mode="json")
        attempts.append(CampaignDefinitionAttempt.model_validate(raw))
    return CampaignDefinition(
        ledger=CampaignCalibrationLedger(
            ledger_id=LEDGER_ID,
            matrix_ref=MATRIX_ID,
            family=FAMILY,
            status="pending",
        ),
        submitted_by="test",
        limits=CampaignLimits(
            max_requests=0,
            max_cost_usd=0,
            max_input_tokens=0,
            max_output_tokens=0,
            max_total_tokens=0,
            max_wall_clock_seconds=120,
            max_concurrency=1,
            max_consecutive_transient_failures=1,
        ),
        attempts=tuple(attempts),
    )


def _write_runner_job(
    request: RunRequest,
    task: Path,
    staged: TaskRegistryRecord,
) -> Path:
    job_dir = request.jobs_dir / request.name
    suffix = "1" if request.agent == "oracle" else "2"
    trial_id = f"00000000-0000-0000-0000-00000000001{suffix}"
    job_id = f"00000000-0000-0000-0000-00000000002{suffix}"
    trial_dir = job_dir / f"{staged.task_id}__{request.agent}"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "verifier").mkdir()
    finished_at = NOW.isoformat()
    reward = 1.0 if request.agent == "oracle" else 0.0
    result = {
        "id": trial_id,
        "trial_name": trial_dir.name,
        "task_name": staged.task_id,
        "task_id": {"path": str(task)},
        "config": {"task": {"path": str(task)}},
        "agent_info": {"name": request.agent},
        "verifier_result": {"rewards": {"reward": reward}},
        "started_at": finished_at,
        "finished_at": finished_at,
    }
    lock = {
        "task": {
            "name": staged.task_id,
            "version": staged.version,
            "type": "local",
            "digest": harbor_task_digest(task),
        },
        "agent": {"name": request.agent},
    }
    (trial_dir / "result.json").write_text(json.dumps(result))
    (trial_dir / "lock.json").write_text(json.dumps(lock))
    (trial_dir / "config.json").write_text("{}")
    (trial_dir / "benchmark_contract.json").write_text(
        json.dumps(
            {
                "family": staged.task_family,
                "task_id": staged.task_id,
                "task_name": staged.task_id,
                "cell_factors": {"control": request.agent},
            }
        )
    )
    (trial_dir / "benchmark-events.jsonl").write_text(
        json.dumps(
            {
                "event_index": 1,
                "timestamp": finished_at,
                "event_type": "control_complete",
            }
        )
        + "\n"
    )
    (trial_dir / "final-state.json").write_text(
        json.dumps(
            {
                "initial_digest": "initial",
                "final_digest": "final",
                "step_count": 0,
                "mutations": [],
                "invariants_passed": True,
            }
        )
    )
    (trial_dir / "agent/trajectory.json").write_text(
        json.dumps({"schema_version": "1.0.0", "session_id": trial_id, "steps": []})
    )
    (trial_dir / "verifier/result.json").write_text(json.dumps({"rewards": {"reward": reward}}))
    (trial_dir / "verifier/reward.txt").write_text(f"{reward}\n")
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "result.json").write_text(
        json.dumps(
            {
                "id": job_id,
                "n_total_trials": 1,
                "stats": {},
                "finished_at": finished_at,
            }
        )
    )
    (job_dir / "config.json").write_text("{}")
    (job_dir / "lock.json").write_text("{}")
    (job_dir / "lab-metadata.json").write_text(
        json.dumps({"experiment": request.provenance.model_dump(mode="json")})
    )
    return job_dir


def _analyze_dispatched_jobs(repo: Path, requests: list[RunRequest]) -> None:
    prompt = repo / "analysis-prompt.txt"
    prompt.write_text("{source_trial_path}\n{rubric}\n{output_schema}")
    rubric = repo / "analysis-rubric.json"
    rubric.write_text("{}")
    output = {
        "validity": "valid_agent_attempt",
        "primary_category": "unknown",
        "summary": "Control completed with exact verifier reward.",
        "evidence": [{"path": "result.json", "supports": "Observed control reward."}],
        "proposed_discriminator": "Repeat the exact control.",
        "confidence": "high",
    }
    queue_events = load_events(repo / "queue/events.jsonl")
    for request in requests:
        assert not (repo / "runs" / request.name).exists()
        matching_events = [
            e
            for e in queue_events
            if e.job_name == request.name and e.event == "dispatch_completed"
        ]
        assert matching_events, f"No dispatch_completed event found for {request.name}"
        event = matching_events[-1]
        assert event.cas_store_root is not None
        assert event.cas_record_kind is not None
        assert event.cas_record_id is not None
        assert event.cas_record_digest is not None
        assert event.cas_content_digest is not None
        locator = EvidenceLocator(
            store_root=Path(event.cas_store_root),
            kind=event.cas_record_kind,
            record_id=event.cas_record_id,
            expected_record_digest=event.cas_record_digest,
            expected_content_digest=event.cas_content_digest,
        )
        # Authenticate and materialize from exact terminal event locator with live lifetime
        with materialize_evidence(locator) as materialized_job_dir:
            job = load_job(materialized_job_dir)
            trial = job.trials[0]
            canonical_trial_path = repo / f"research/evidence/runs/{request.name}/{trial.name}"
            sidecar_path, sidecar = run_trial_analysis(
                job,
                trial,
                analyzer=lambda _prompt, _schema: AnalyzerCallResult(raw_output=json.dumps(output)),
                repo_root=repo,
                destination_root=repo / "research/evidence/analysis",
                prompt_path=prompt,
                rubric_path=rubric,
                agent="test-analyzer",
                agent_version="1.0.0",
                model="test-model",
                canonical_trial_path=canonical_trial_path,
            )
            assert sidecar.analysis_id
            assert sidecar_path.is_file()


def _prepare_campaign(
    tmp_path: Path,
) -> tuple[Path, Path, Path, TaskRegistryRecord, Any]:
    repo, task, certification_path, staged = _stage_task(tmp_path)
    _write_matrix_catalog(repo, staged)
    manifest = build_campaign_manifest(
        _campaign_definition(staged),
        repo_root=repo,
    )
    return repo, task, certification_path, staged, manifest


def test_registered_control_bootstrap_uses_live_dispatch_and_preserves_identity(
    tmp_path: Path,
) -> None:
    repo, task, certification_path, staged, manifest = _prepare_campaign(tmp_path)
    staged_identity = task_runtime_identity(staged)
    requests: list[RunRequest] = []
    live_rebinds: list[str] = []

    def live_identity(
        evidence: NetworkIsolationEvidenceV1,
    ) -> NetworkIsolationDispatchIdentityV1:
        assert evidence.runtime_identity is not None
        assert evidence.probe_identity is not None
        live_rebinds.append(evidence.runtime_identity.adapter)
        return NetworkIsolationDispatchIdentityV1(
            runtime_identity=evidence.runtime_identity,
            probe_identity=evidence.probe_identity,
        )

    executor = _executor(
        repo,
        lambda request: requests.append(request) or _write_runner_job(request, task, staged),
        isolation_identity_provider=live_identity,
    )
    completed = _orchestrator(repo, manifest, executor).run()
    assert completed.state == "completed"
    assert completed.completed_attempts == 2
    assert sorted(live_rebinds) == ["nop", "oracle"]
    assert sorted(request.agent for request in requests) == ["nop", "oracle"]
    for request in requests:
        assert request.provenance.task_runtime_identity == staged_identity
        assert request.provenance.analysis_eligibility == "causal-eligible"
    _analyze_dispatched_jobs(repo, requests)

    with pytest.raises(ValueError, match="cannot change approved_by"):
        promote_task(
            task,
            repo,
            task_id=staged.task_id,
            task_family=staged.task_family,
            state="registered",
            actor="different-reviewer",
        )
    with pytest.raises(ValueError, match="cannot change approved_at"):
        promote_task(
            task,
            repo,
            task_id=staged.task_id,
            task_family=staged.task_family,
            state="registered",
            actor="admission-reviewer",
            approved_at=NOW + timedelta(seconds=1),
        )
    copied_packet = repo / "research/registration/candidates/copied-packet"
    shutil.copytree(certification_path.parent, copied_packet)
    with pytest.raises(TaskCertificationError):
        promote_task(
            task,
            repo,
            task_id=staged.task_id,
            task_family=staged.task_family,
            state="registered",
            actor="admission-reviewer",
            certification_path=copied_packet / certification_path.name,
        )

    admitted = promote_task(
        task,
        repo,
        task_id=staged.task_id,
        task_family=staged.task_family,
        state="registered",
        actor="admission-reviewer",
    )
    assert admitted.certification == staged.certification
    assert admitted.approved_by == staged.approved_by
    assert admitted.approved_at == staged.approved_at
    assert task_runtime_identity(admitted) == staged_identity
    assert admitted.control_evidence is not None
    assert TaskRegistry.from_repo(repo).get(staged.task_id) == admitted


def test_control_bootstrap_campaign_rejects_missing_isolation_binding(
    tmp_path: Path,
) -> None:
    _, _, _, staged = _stage_task(tmp_path)
    with pytest.raises(ValueError, match="explicit control-bootstrap runtime"):
        _campaign_definition(staged, include_runtime=False)


def test_control_bootstrap_live_identity_drift_refuses_before_runner(
    tmp_path: Path,
) -> None:
    repo, _, _, _, manifest = _prepare_campaign(tmp_path)
    calls: list[RunRequest] = []

    def drifted_identity(
        evidence: NetworkIsolationEvidenceV1,
    ) -> NetworkIsolationDispatchIdentityV1:
        assert evidence.runtime_identity is not None
        assert evidence.probe_identity is not None
        runtime = evidence.runtime_identity.model_copy(
            update={"container_runtime_version": "drifted-runtime"}
        )
        return NetworkIsolationDispatchIdentityV1(
            runtime_identity=runtime,
            probe_identity=evidence.probe_identity,
        )

    executor = _executor(
        repo,
        lambda request: calls.append(request),
        isolation_identity_provider=drifted_identity,
    )
    status = _orchestrator(repo, manifest, executor).run()
    assert status.completed_attempts == 0
    assert calls == []
    assert any(
        event.reason_code == "campaign_isolation_identity_drift"
        for event in load_events(executor.queue.events_path)
    )


def test_noncampaign_registered_control_cannot_bypass_runtime_binding(
    tmp_path: Path,
) -> None:
    repo, _, _, staged = _stage_task(tmp_path)
    calls: list[RunRequest] = []
    executor = _executor(repo, lambda request: calls.append(request))
    spec = ExperimentSpec(
        name="unbound-control",
        hypothesis="Unbound controls cannot establish causal evidence",
        purpose="baseline",
        task=f"registered/{staged.task_id}",
        agent="oracle",
        submitted_by="test",
    )
    executor.submit(spec)
    assert executor.tick() == 0
    assert calls == []
    assert any(
        event.reason_code == "control_bootstrap_binding_missing"
        for event in load_events(executor.queue.events_path)
    )


def test_direct_execute_spec_cannot_bypass_control_runtime_binding(
    tmp_path: Path,
) -> None:
    repo, _, _, staged = _stage_task(tmp_path)
    runner_calls: list[RunRequest] = []
    identity_calls: list[NetworkIsolationEvidenceV1] = []

    def identity_provider(
        evidence: NetworkIsolationEvidenceV1,
    ) -> NetworkIsolationDispatchIdentityV1:
        identity_calls.append(evidence)
        raise AssertionError("unbound control reached isolation identity provider")

    executor = _executor(
        repo,
        lambda request: runner_calls.append(request),
        isolation_identity_provider=identity_provider,
    )
    spec = ExperimentSpec(
        name="direct-unbound-control",
        hypothesis="Direct execution cannot bypass causal binding",
        purpose="baseline",
        task=f"registered/{staged.task_id}",
        agent="oracle",
        submitted_by="test",
    )

    assert not hasattr(queue_module, "_CAMPAIGN_DISPATCH_VALIDATED")
    with pytest.raises(TypeError, match="_campaign_validation"):
        executor.execute_spec(spec, _campaign_validation=object())  # type: ignore[call-arg]
    assert identity_calls == []
    assert runner_calls == []

    with pytest.raises(ExecutionFailure, match="frozen campaign runtime binding"):
        executor.execute_spec(spec)
    assert identity_calls == []
    assert runner_calls == []


def test_control_bootstrap_tampered_durable_publication_refuses_promotion(
    tmp_path: Path,
) -> None:
    repo, task, certification_path, staged, manifest = _prepare_campaign(tmp_path)
    requests: list[RunRequest] = []

    def live_identity(
        evidence: NetworkIsolationEvidenceV1,
    ) -> NetworkIsolationDispatchIdentityV1:
        return NetworkIsolationDispatchIdentityV1(
            runtime_identity=evidence.runtime_identity,
            probe_identity=evidence.probe_identity,
        )

    executor = _executor(
        repo,
        lambda request: requests.append(request) or _write_runner_job(request, task, staged),
        isolation_identity_provider=live_identity,
    )
    completed = _orchestrator(repo, manifest, executor).run()
    assert completed.state == "completed"
    _analyze_dispatched_jobs(repo, requests)

    # TAMPER: Corrupt published durable result.json
    published_trial = (
        repo / f"research/evidence/runs/{requests[0].name}/uppercase-fixture__{requests[0].agent}"
    )
    assert published_trial.is_dir()
    result_file = published_trial / "result.json"
    result_file.write_text('{"tampered": true}\n', encoding="utf-8")

    from evallab.registry import TaskControlEvidenceError

    with pytest.raises(
        TaskControlEvidenceError,
        match="control evidence",
    ):
        promote_task(
            task,
            repo,
            task_id=staged.task_id,
            task_family=staged.task_family,
            state="registered",
            actor="admission-reviewer",
        )
