from __future__ import annotations

import hashlib
import json
import os
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
from evallab.evidence.facts import (
    AnalyzerCallResult,
    CanonicalPublicationBinding,
    run_trial_analysis,
)
from evallab.evidence_store import EvidenceLocator, materialize_evidence
from evallab.execution_contracts import RunRequest
from evallab.queue import ExecutionFailure, load_events, select_terminal_job_locator
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
from evallab.trial_admissibility import TrialAdmissibilityError

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
    for request in requests:
        assert not (repo / "runs" / request.name).exists()
        locator = select_terminal_job_locator(
            repo / "queue/events.jsonl",
            expected_event="dispatch_completed",
            job_name=request.name,
            spec_id=request.provenance.spec_id,
        )
        with materialize_evidence(locator) as materialized_job_dir:
            job = load_job(materialized_job_dir)
            trial = job.trials[0]
            binding = CanonicalPublicationBinding.create(
                repo_root=repo,
                job_name=request.name,
                spec_id=request.provenance.spec_id,
                locator=locator,
            )
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
                canonical_binding=binding,
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

    # TAMPER: Corrupt published durable final-state.json while keeping result/control valid
    published_trial = (
        repo / f"research/evidence/runs/{requests[0].name}/uppercase-fixture__{requests[0].agent}"
    )
    assert published_trial.is_dir()
    final_state_file = published_trial / "final-state.json"
    os.chmod(published_trial, 0o700)
    os.chmod(final_state_file, 0o600)
    final_state_file.write_text(
        json.dumps(
            {
                "initial_digest": "initial",
                "final_digest": "tampered-final-digest",
                "step_count": 0,
                "mutations": [],
                "invariants_passed": True,
            }
        ),
        encoding="utf-8",
    )

    from evallab.registry import TaskControlEvidenceError
    from evallab.trial_admissibility import TrialAdmissibilityError

    with pytest.raises(
        TaskControlEvidenceError,
        match="control evidence lacks strict trial admissibility authority",
    ) as exc_info:
        promote_task(
            task,
            repo,
            task_id=staged.task_id,
            task_family=staged.task_family,
            state="registered",
            actor="admission-reviewer",
        )
    assert exc_info.value.__cause__ is not None
    assert isinstance(exc_info.value.__cause__, TrialAdmissibilityError)
    assert "source-digest-drift" in str(exc_info.value.__cause__)


def test_canonical_publication_binding_refuses_same_named_unrelated_copy(
    tmp_path: Path,
) -> None:
    """B3 adversary: same-named byte-identical copy outside research/evidence/runs is rejected."""
    repo, task, certification_path, staged, manifest = _prepare_campaign(tmp_path)
    requests: list[RunRequest] = []
    executor = _executor(
        repo,
        lambda request: requests.append(request) or _write_runner_job(request, task, staged),
    )
    completed = _orchestrator(repo, manifest, executor).run()
    assert completed.state == "completed"

    event = [
        e
        for e in load_events(repo / "queue/events.jsonl")
        if e.job_name == requests[0].name and e.event == "dispatch_completed"
    ][0]
    locator = EvidenceLocator(
        store_root=Path(event.cas_store_root),
        kind=event.cas_record_kind,
        record_id=event.cas_record_id,
        expected_record_digest=event.cas_record_digest,
        expected_content_digest=event.cas_content_digest,
    )

    unrelated_root = tmp_path / "unrelated-authority"
    unrelated_root.mkdir(parents=True)
    shutil.copytree(
        repo / f"research/evidence/runs/{requests[0].name}", unrelated_root / requests[0].name
    )

    binding = CanonicalPublicationBinding(
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
        locator=locator,
        publication_root=unrelated_root,
    )

    calls = []
    prompt = repo / "prompt.txt"
    prompt.write_text("{source_trial_path}\n{rubric}\n{output_schema}")
    rubric = repo / "rubric.json"
    rubric.write_text("{}")

    with materialize_evidence(locator) as mat:
        job = load_job(mat)
        with pytest.raises(TrialAdmissibilityError, match="invalid-canonical-publication-root"):
            run_trial_analysis(
                job,
                job.trials[0],
                analyzer=lambda _p, _s: (
                    calls.append("called") or AnalyzerCallResult(raw_output="{}")
                ),
                repo_root=repo,
                destination_root=repo / "research/evidence/analysis",
                prompt_path=prompt,
                rubric_path=rubric,
                agent="test-analyzer",
                agent_version="1.0.0",
                model="test-model",
                canonical_binding=binding,
            )
    assert calls == []


def test_canonical_publication_binding_refuses_symlinked_source(
    tmp_path: Path,
) -> None:
    """B3 adversary: symlinked canonical source directory is refused with zero model calls."""
    repo, task, certification_path, staged, manifest = _prepare_campaign(tmp_path)
    requests: list[RunRequest] = []
    executor = _executor(
        repo,
        lambda request: requests.append(request) or _write_runner_job(request, task, staged),
    )
    completed = _orchestrator(repo, manifest, executor).run()
    assert completed.state == "completed"

    event = [
        e
        for e in load_events(repo / "queue/events.jsonl")
        if e.job_name == requests[0].name and e.event == "dispatch_completed"
    ][0]
    locator = EvidenceLocator(
        store_root=Path(event.cas_store_root),
        kind=event.cas_record_kind,
        record_id=event.cas_record_id,
        expected_record_digest=event.cas_record_digest,
        expected_content_digest=event.cas_content_digest,
    )

    # Symlink canonical job dir
    real_dir = repo / f"research/evidence/runs/{requests[0].name}"
    hidden_dir = repo / f"research/evidence/runs/.hidden_{requests[0].name}"
    os.chmod(repo / "research/evidence/runs", 0o700)
    os.chmod(real_dir, 0o700)
    real_dir.rename(hidden_dir)
    real_dir.symlink_to(hidden_dir)

    binding = CanonicalPublicationBinding(
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
        locator=locator,
        publication_root=repo / "research/evidence/runs",
    )

    calls = []
    prompt = repo / "prompt.txt"
    prompt.write_text("{source_trial_path}\n{rubric}\n{output_schema}")
    rubric = repo / "rubric.json"
    rubric.write_text("{}")

    with materialize_evidence(locator) as mat:
        job = load_job(mat)
        with pytest.raises(TrialAdmissibilityError, match="symlinked-canonical-source"):
            run_trial_analysis(
                job,
                job.trials[0],
                analyzer=lambda _p, _s: (
                    calls.append("called") or AnalyzerCallResult(raw_output="{}")
                ),
                repo_root=repo,
                destination_root=repo / "research/evidence/analysis",
                prompt_path=prompt,
                rubric_path=rubric,
                agent="test-analyzer",
                agent_version="1.0.0",
                model="test-model",
                canonical_binding=binding,
            )
    assert calls == []


def test_canonical_publication_binding_refuses_in_repo_unrelated_root(
    tmp_path: Path,
) -> None:
    """B3a adversary: arbitrary in-repository publication root is rejected with zero calls."""
    repo, task, certification_path, staged, manifest = _prepare_campaign(tmp_path)
    requests: list[RunRequest] = []
    executor = _executor(
        repo,
        lambda request: requests.append(request) or _write_runner_job(request, task, staged),
    )
    completed = _orchestrator(repo, manifest, executor).run()
    assert completed.state == "completed"

    locator = select_terminal_job_locator(
        repo / "queue/events.jsonl",
        expected_event="dispatch_completed",
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
    )

    unrelated_in_repo = repo / "unrelated-authority"
    unrelated_in_repo.mkdir(parents=True)
    shutil.copytree(
        repo / f"research/evidence/runs/{requests[0].name}", unrelated_in_repo / requests[0].name
    )

    binding = CanonicalPublicationBinding(
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
        locator=locator,
        publication_root=unrelated_in_repo,
    )

    calls = []
    prompt = repo / "prompt.txt"
    prompt.write_text("{source_trial_path}\n{rubric}\n{output_schema}")
    rubric = repo / "rubric.json"
    rubric.write_text("{}")

    with materialize_evidence(locator) as mat:
        job = load_job(mat)
        with pytest.raises(TrialAdmissibilityError, match="invalid-canonical-publication-root"):
            run_trial_analysis(
                job,
                job.trials[0],
                analyzer=lambda _p, _s: (
                    calls.append("called") or AnalyzerCallResult(raw_output="{}")
                ),
                repo_root=repo,
                destination_root=repo / "research/evidence/analysis",
                prompt_path=prompt,
                rubric_path=rubric,
                agent="test-analyzer",
                agent_version="1.0.0",
                model="test-model",
                canonical_binding=binding,
            )
    assert calls == []


def test_canonical_publication_binding_refuses_nested_file_symlink(
    tmp_path: Path,
) -> None:
    """B3b adversary: nested result.json symlinked to byte-identical file is rejected before model call with zero calls."""
    repo, task, certification_path, staged, manifest = _prepare_campaign(tmp_path)
    requests: list[RunRequest] = []
    executor = _executor(
        repo,
        lambda request: requests.append(request) or _write_runner_job(request, task, staged),
    )
    completed = _orchestrator(repo, manifest, executor).run()
    assert completed.state == "completed"

    locator = select_terminal_job_locator(
        repo / "queue/events.jsonl",
        expected_event="dispatch_completed",
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
    )

    job_dir = repo / f"research/evidence/runs/{requests[0].name}"
    canonical_trial_dir = next(p for p in job_dir.iterdir() if p.is_dir())

    result_file = canonical_trial_dir / "result.json"
    backup_file = canonical_trial_dir / ".backup_result.json"
    os.chmod(canonical_trial_dir, 0o700)
    os.chmod(result_file, 0o600)
    result_file.rename(backup_file)
    result_file.symlink_to(backup_file)

    binding = CanonicalPublicationBinding.create(
        repo_root=repo,
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
        locator=locator,
    )

    calls = []
    prompt = repo / "prompt.txt"
    prompt.write_text("{source_trial_path}\n{rubric}\n{output_schema}")
    rubric = repo / "rubric.json"
    rubric.write_text("{}")

    with materialize_evidence(locator) as mat:
        job = load_job(mat)
        with pytest.raises(TrialAdmissibilityError, match="symlinked-canonical-source"):
            run_trial_analysis(
                job,
                job.trials[0],
                analyzer=lambda _p, _s: (
                    calls.append("called") or AnalyzerCallResult(raw_output="{}")
                ),
                repo_root=repo,
                destination_root=repo / "research/evidence/analysis",
                prompt_path=prompt,
                rubric_path=rubric,
                agent="test-analyzer",
                agent_version="1.0.0",
                model="test-model",
                canonical_binding=binding,
            )
    assert calls == []


def test_canonical_publication_binding_refuses_byte_identical_replacement_during_analysis(
    tmp_path: Path,
) -> None:
    """B3c adversary: whole-directory byte-identical replacement during analyzer raises typed TrialAdmissibilityError with zero authority."""
    from evallab.trial_admissibility import canonical_trial_admissibility_path

    repo, task, certification_path, staged, manifest = _prepare_campaign(tmp_path)
    requests: list[RunRequest] = []
    executor = _executor(
        repo,
        lambda request: requests.append(request) or _write_runner_job(request, task, staged),
    )
    completed = _orchestrator(repo, manifest, executor).run()
    assert completed.state == "completed"

    locator = select_terminal_job_locator(
        repo / "queue/events.jsonl",
        expected_event="dispatch_completed",
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
    )

    canonical_job_dir = repo / f"research/evidence/runs/{requests[0].name}"
    canonical_trial_dir = next(p for p in canonical_job_dir.iterdir() if p.is_dir())

    binding = CanonicalPublicationBinding.create(
        repo_root=repo,
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
        locator=locator,
    )

    valid_analysis_output = {
        "validity": "valid_agent_attempt",
        "primary_category": "unknown",
        "summary": "Byte replacement test.",
        "evidence": [{"path": "result.json", "supports": "Reward observed."}],
        "proposed_discriminator": "Check reward.",
        "confidence": "high",
    }

    def replacing_analyzer(p: str, s: dict) -> AnalyzerCallResult:
        # Inode-tamper adversary: Replace whole canonical trial directory with newly copied byte-identical tree
        temp_copy = tmp_path / "temp_trial_copy"
        shutil.copytree(canonical_trial_dir, temp_copy)
        os.chmod(canonical_job_dir, 0o700)
        for r, _d, fs in os.walk(canonical_trial_dir):
            os.chmod(r, 0o700)
            for f in fs:
                os.chmod(os.path.join(r, f), 0o600)
        shutil.rmtree(canonical_trial_dir)
        shutil.copytree(temp_copy, canonical_trial_dir)
        for r, _d, fs in os.walk(temp_copy):
            os.chmod(r, 0o700)
            for f in fs:
                os.chmod(os.path.join(r, f), 0o600)
        shutil.rmtree(temp_copy)
        return AnalyzerCallResult(raw_output=json.dumps(valid_analysis_output))

    prompt = repo / "prompt.txt"
    prompt.write_text("{source_trial_path}\n{rubric}\n{output_schema}")
    rubric = repo / "rubric.json"
    rubric.write_text("{}")

    with materialize_evidence(locator) as mat:
        job = load_job(mat)
        with pytest.raises(TrialAdmissibilityError, match="canonical-source-identity-drift"):
            run_trial_analysis(
                job,
                job.trials[0],
                analyzer=replacing_analyzer,
                repo_root=repo,
                destination_root=repo / "research/evidence/analysis",
                prompt_path=prompt,
                rubric_path=rubric,
                agent="test-analyzer",
                agent_version="1.0.0",
                model="test-model",
                canonical_binding=binding,
            )
        # Authority must NOT have been minted!
        assert not canonical_trial_admissibility_path(repo, job.trials[0].id).exists()


def test_production_terminal_locator_selector_adversaries(
    tmp_path: Path,
) -> None:
    """B4 adversaries: wrong event type, wrong job name, wrong kind, missing/duplicate events fail closed."""
    from datetime import UTC, datetime

    from evallab.queue import QueueEvent, new_ulid

    events_file = tmp_path / "events.jsonl"

    base_event = QueueEvent(
        event_id=new_ulid(),
        spec_id="spec-123",
        occurred_at=datetime.now(UTC),
        event="dispatch_completed",
        actor="executor",
        job_name="job-123",
        cas_store_root=str(tmp_path / "cas"),
        cas_record_kind="job",
        cas_record_id="rec-123",
        cas_record_digest="sha256:" + "a" * 64,
        cas_content_digest="sha256:" + "b" * 64,
    )

    def write_events(evs: list[QueueEvent]) -> None:
        lines = [ev.model_dump_json() for ev in evs]
        events_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 1. Missing events file
    with pytest.raises(ExecutionFailure) as exc_info:
        select_terminal_job_locator(
            tmp_path / "nonexistent.jsonl",
            expected_event="dispatch_completed",
            job_name="job-123",
            spec_id="spec-123",
        )
    assert exc_info.value.reason_code == "terminal_event_missing"

    # 2. Wrong event type (dispatch_started or refusal when expecting dispatch_completed)
    ev_wrong_type = base_event.model_copy(update={"event": "dispatch_started"})
    write_events([ev_wrong_type])
    with pytest.raises(ExecutionFailure) as exc_info:
        select_terminal_job_locator(
            events_file,
            expected_event="dispatch_completed",
            job_name="job-123",
            spec_id="spec-123",
        )
    assert exc_info.value.reason_code == "terminal_event_missing"

    # 3. Wrong job name
    ev_wrong_job = base_event.model_copy(update={"job_name": "other-job"})
    write_events([ev_wrong_job])
    with pytest.raises(ExecutionFailure) as exc_info:
        select_terminal_job_locator(
            events_file,
            expected_event="dispatch_completed",
            job_name="job-123",
            spec_id="spec-123",
        )
    assert exc_info.value.reason_code == "terminal_event_missing"

    # 4. Wrong kind (e.g. analysis instead of job)
    ev_wrong_kind = base_event.model_copy(update={"cas_record_kind": "analysis"})
    write_events([ev_wrong_kind])
    with pytest.raises(ExecutionFailure) as exc_info:
        select_terminal_job_locator(
            events_file,
            expected_event="dispatch_completed",
            job_name="job-123",
            spec_id="spec-123",
        )
    assert exc_info.value.reason_code == "terminal_event_missing"

    # 5. Duplicate exact match -> fails closed as ambiguous
    ev_dup = base_event.model_copy(update={"event_id": new_ulid()})
    write_events([base_event, ev_dup])
    with pytest.raises(ExecutionFailure) as exc_info:
        select_terminal_job_locator(
            events_file,
            expected_event="dispatch_completed",
            job_name="job-123",
            spec_id="spec-123",
        )
    assert exc_info.value.reason_code == "terminal_event_ambiguous"

    # 6. Missing attempt number when expected_attempt is specified -> fails closed
    ev_no_attempt = base_event.model_copy(update={"attempt_number": None})
    write_events([ev_no_attempt])
    with pytest.raises(ExecutionFailure) as exc_info:
        select_terminal_job_locator(
            events_file,
            expected_event="dispatch_completed",
            job_name="job-123",
            spec_id="spec-123",
            expected_attempt=1,
        )
    assert exc_info.value.reason_code == "terminal_event_missing"

    # 7. Wrong attempt number -> fails closed
    ev_attempt_2 = base_event.model_copy(update={"attempt_number": 2})
    write_events([ev_attempt_2])
    with pytest.raises(ExecutionFailure) as exc_info:
        select_terminal_job_locator(
            events_file,
            expected_event="dispatch_completed",
            job_name="job-123",
            spec_id="spec-123",
            expected_attempt=1,
        )
    assert exc_info.value.reason_code == "terminal_event_missing"

    # 8. Clean single match with matching expected_attempt -> returns locator
    ev_attempt_1 = base_event.model_copy(update={"attempt_number": 1})
    write_events([ev_attempt_1])
    loc = select_terminal_job_locator(
        events_file,
        expected_event="dispatch_completed",
        job_name="job-123",
        spec_id="spec-123",
        expected_attempt=1,
    )
    assert loc.kind == "job"
    assert loc.record_id == "rec-123"
    assert loc.expected_content_digest == "sha256:" + "b" * 64


def test_canonical_publication_binding_refuses_ancestor_symlink(
    tmp_path: Path,
) -> None:
    """B3a adversary: ancestor symlink (e.g. repo/research -> external) is rejected with zero model calls."""
    repo, task, certification_path, staged, manifest = _prepare_campaign(tmp_path)
    requests: list[RunRequest] = []
    executor = _executor(
        repo,
        lambda request: requests.append(request) or _write_runner_job(request, task, staged),
    )
    completed = _orchestrator(repo, manifest, executor).run()
    assert completed.state == "completed"

    locator = select_terminal_job_locator(
        repo / "queue/events.jsonl",
        expected_event="dispatch_completed",
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
        expected_attempt=requests[0].provenance.campaign_attempt_index
        if hasattr(requests[0].provenance, "campaign_attempt_index")
        else None,
    )

    # Symlink repo/research to external directory
    external_research = tmp_path / "external-research"
    shutil.copytree(repo / "research", external_research)
    for r, _d, fs in os.walk(repo / "research"):
        os.chmod(r, 0o700)
        for f in fs:
            os.chmod(os.path.join(r, f), 0o600)
    shutil.rmtree(repo / "research")
    (repo / "research").symlink_to(external_research)

    binding = CanonicalPublicationBinding.create(
        repo_root=repo,
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
        locator=locator,
    )

    calls = []
    prompt = repo / "prompt.txt"
    prompt.write_text("{source_trial_path}\n{rubric}\n{output_schema}")
    rubric = repo / "rubric.json"
    rubric.write_text("{}")

    with materialize_evidence(locator) as mat:
        job = load_job(mat)
        with pytest.raises(TrialAdmissibilityError, match="symlinked-canonical-source"):
            run_trial_analysis(
                job,
                job.trials[0],
                analyzer=lambda _p, _s: (
                    calls.append("called") or AnalyzerCallResult(raw_output="{}")
                ),
                repo_root=repo,
                destination_root=repo / "research/evidence/analysis",
                prompt_path=prompt,
                rubric_path=rubric,
                agent="test-analyzer",
                agent_version="1.0.0",
                model="test-model",
                canonical_binding=binding,
            )
    assert calls == []


def test_canonical_publication_binding_refuses_job_uuid_mismatch(
    tmp_path: Path,
) -> None:
    """B3b adversary: canonical durable job result.json UUID mismatch mints zero authority."""
    from evallab.trial_admissibility import canonical_trial_admissibility_path

    repo, task, certification_path, staged, manifest = _prepare_campaign(tmp_path)
    requests: list[RunRequest] = []
    executor = _executor(
        repo,
        lambda request: requests.append(request) or _write_runner_job(request, task, staged),
    )
    completed = _orchestrator(repo, manifest, executor).run()
    assert completed.state == "completed"

    locator = select_terminal_job_locator(
        repo / "queue/events.jsonl",
        expected_event="dispatch_completed",
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
    )

    # Corrupt UUID in canonical durable job-level result.json
    job_result_file = repo / f"research/evidence/runs/{requests[0].name}/result.json"
    os.chmod(job_result_file.parent, 0o700)
    os.chmod(job_result_file, 0o600)
    job_res = json.loads(job_result_file.read_text())
    job_res["id"] = "00000000-0000-0000-0000-000000000099"
    job_result_file.write_text(json.dumps(job_res, indent=2))

    binding = CanonicalPublicationBinding.create(
        repo_root=repo,
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
        locator=locator,
    )

    valid_analysis_output = {
        "validity": "valid_agent_attempt",
        "primary_category": "unknown",
        "summary": "UUID mismatch test.",
        "evidence": [{"path": "result.json", "supports": "Reward observed."}],
        "proposed_discriminator": "Check reward.",
        "confidence": "high",
    }

    prompt = repo / "prompt.txt"
    prompt.write_text("{source_trial_path}\n{rubric}\n{output_schema}")
    rubric = repo / "rubric.json"
    rubric.write_text("{}")

    with materialize_evidence(locator) as mat:
        job = load_job(mat)
        with pytest.raises(TrialAdmissibilityError, match="job-identity-mismatch"):
            run_trial_analysis(
                job,
                job.trials[0],
                analyzer=lambda _p, _s: AnalyzerCallResult(
                    raw_output=json.dumps(valid_analysis_output)
                ),
                repo_root=repo,
                destination_root=repo / "research/evidence/analysis",
                prompt_path=prompt,
                rubric_path=rubric,
                agent="test-analyzer",
                agent_version="1.0.0",
                model="test-model",
                canonical_binding=binding,
            )
        assert not canonical_trial_admissibility_path(repo, job.trials[0].id).exists()


def test_canonical_publication_binding_refuses_missing_canonical_provenance(
    tmp_path: Path,
) -> None:
    """B3 adversary: missing canonical job provenance fails closed with zero calls/authority."""
    repo, task, certification_path, staged, manifest = _prepare_campaign(tmp_path)
    requests: list[RunRequest] = []
    executor = _executor(
        repo,
        lambda request: requests.append(request) or _write_runner_job(request, task, staged),
    )
    completed = _orchestrator(repo, manifest, executor).run()
    assert completed.state == "completed"

    locator = select_terminal_job_locator(
        repo / "queue/events.jsonl",
        expected_event="dispatch_completed",
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
    )

    # Remove provenance from canonical durable job-level lab-metadata.json and result.json
    job_dir = repo / f"research/evidence/runs/{requests[0].name}"
    os.chmod(job_dir, 0o700)
    lab_meta = job_dir / "lab-metadata.json"
    if lab_meta.exists():
        os.chmod(lab_meta, 0o600)
        lab_meta.unlink()
    job_result_file = job_dir / "result.json"
    os.chmod(job_result_file, 0o600)
    job_res = json.loads(job_result_file.read_text())
    job_res.pop("metadata", None)
    job_res.pop("experiment", None)
    job_result_file.write_text(json.dumps(job_res, indent=2))

    binding = CanonicalPublicationBinding.create(
        repo_root=repo,
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
        locator=locator,
    )

    calls = []
    prompt = repo / "prompt.txt"
    prompt.write_text("{source_trial_path}\n{rubric}\n{output_schema}")
    rubric = repo / "rubric.json"
    rubric.write_text("{}")

    with materialize_evidence(locator) as mat:
        job = load_job(mat)
        with pytest.raises(TrialAdmissibilityError, match="provenance-spec-id-mismatch"):
            run_trial_analysis(
                job,
                job.trials[0],
                analyzer=lambda _p, _s: (
                    calls.append("called") or AnalyzerCallResult(raw_output="{}")
                ),
                repo_root=repo,
                destination_root=repo / "research/evidence/analysis",
                prompt_path=prompt,
                rubric_path=rubric,
                agent="test-analyzer",
                agent_version="1.0.0",
                model="test-model",
                canonical_binding=binding,
            )
    assert calls == []


def test_canonical_publication_binding_refuses_post_call_content_drift(
    tmp_path: Path,
) -> None:
    """B3c adversary: post-call canonical content drift raises typed TrialAdmissibilityError with zero authority."""
    from evallab.trial_admissibility import canonical_trial_admissibility_path

    repo, task, certification_path, staged, manifest = _prepare_campaign(tmp_path)
    requests: list[RunRequest] = []
    executor = _executor(
        repo,
        lambda request: requests.append(request) or _write_runner_job(request, task, staged),
    )
    completed = _orchestrator(repo, manifest, executor).run()
    assert completed.state == "completed"

    locator = select_terminal_job_locator(
        repo / "queue/events.jsonl",
        expected_event="dispatch_completed",
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
    )

    canonical_job_dir = repo / f"research/evidence/runs/{requests[0].name}"
    canonical_trial_dir = next(p for p in canonical_job_dir.iterdir() if p.is_dir())

    binding = CanonicalPublicationBinding.create(
        repo_root=repo,
        job_name=requests[0].name,
        spec_id=requests[0].provenance.spec_id,
        locator=locator,
    )

    valid_analysis_output = {
        "validity": "valid_agent_attempt",
        "primary_category": "unknown",
        "summary": "Content drift test.",
        "evidence": [{"path": "result.json", "supports": "Reward observed."}],
        "proposed_discriminator": "Check reward.",
        "confidence": "high",
    }

    def drifting_analyzer(p: str, s: dict) -> AnalyzerCallResult:
        # Mutate canonical trial result.json content during analyzer
        os.chmod(canonical_trial_dir / "result.json", 0o600)
        (canonical_trial_dir / "result.json").write_text("TAMPERED_POST_CALL\n")
        return AnalyzerCallResult(raw_output=json.dumps(valid_analysis_output))

    prompt = repo / "prompt.txt"
    prompt.write_text("{source_trial_path}\n{rubric}\n{output_schema}")
    rubric = repo / "rubric.json"
    rubric.write_text("{}")

    with materialize_evidence(locator) as mat:
        job = load_job(mat)
        with pytest.raises(TrialAdmissibilityError, match="canonical-source-content-mismatch"):
            run_trial_analysis(
                job,
                job.trials[0],
                analyzer=drifting_analyzer,
                repo_root=repo,
                destination_root=repo / "research/evidence/analysis",
                prompt_path=prompt,
                rubric_path=rubric,
                agent="test-analyzer",
                agent_version="1.0.0",
                model="test-model",
                canonical_binding=binding,
            )
        assert not canonical_trial_admissibility_path(repo, job.trials[0].id).exists()
