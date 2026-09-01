from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_task_workbench import _bundle, _copy_candidate, _inspect

from evallab.registry import (
    TaskRegistry,
    TaskUsageNotAllowedError,
    harbor_task_digest,
    promote_task,
    task_runtime_identity,
)
from evallab.results import load_job
from evallab.schemas import (
    NETWORK_ESCAPE_CLASSES,
    ExperimentSpec,
    NetworkEscapeProbeResultV1,
    NetworkIsolationProbeIdentityV1,
    NetworkIsolationRuntimeIdentityV1,
    NetworkPolicyEvidenceV1,
    RunProvenance,
    TaskRegistryRecord,
    build_network_isolation_evidence,
)
from evallab.task_workbench import check_candidate, write_packet
from evallab.trial_admissibility import finalize_trial_admissibility


def _causal_isolation_evidence():
    policy = NetworkPolicyEvidenceV1(mode="no-network")
    observed_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
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
            adapter="test-adapter",
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
        observed_at=observed_at,
        valid_until=observed_at + timedelta(days=7),
        evaluated_at=observed_at,
    )


def _write_admissible_control_job(
    repo: Path,
    task: Path,
    record: TaskRegistryRecord,
    *,
    agent: str,
    reward: float,
) -> None:
    job_dir = repo / "research/evidence/runs" / f"bootstrap-{agent}"
    trial_id = f"bootstrap-{record.task_id}-{agent}"
    trial_dir = job_dir / f"{record.task_id}__{agent}"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "verifier").mkdir()
    (trial_dir / "analysis").mkdir()
    finished_at = "2026-08-31T12:00:00Z"
    result = {
        "id": trial_id,
        "trial_name": trial_dir.name,
        "task_name": record.task_id,
        "task_id": {"path": str(task)},
        "config": {"task": {"path": str(task)}},
        "agent_info": {"name": agent},
        "verifier_result": {"rewards": {"reward": reward}},
        "started_at": finished_at,
        "finished_at": finished_at,
    }
    lock = {
        "task": {
            "name": record.task_id,
            "version": record.version,
            "type": "local",
            "digest": harbor_task_digest(task),
        },
        "agent": {"name": agent},
    }
    (trial_dir / "result.json").write_text(json.dumps(result))
    (trial_dir / "lock.json").write_text(json.dumps(lock))
    (trial_dir / "config.json").write_text("{}")
    (trial_dir / "benchmark_contract.json").write_text(
        json.dumps(
            {
                "family": record.task_family,
                "task_id": record.task_id,
                "task_name": record.task_id,
                "cell_factors": {"control": agent},
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
        json.dumps(
            {
                "schema_version": "1.0.0",
                "session_id": trial_id,
                "steps": [],
            }
        )
    )
    (trial_dir / "verifier/result.json").write_text(
        json.dumps({"rewards": {"reward": reward}})
    )
    (trial_dir / "verifier/reward.txt").write_text(f"{reward}\n")
    (trial_dir / "analysis/interpretation.json").write_text(
        json.dumps({"disposition": "complete"})
    )
    evidence = _causal_isolation_evidence()
    provenance = RunProvenance(
        spec_id=f"bootstrap-{agent}",
        task=f"registered/{record.task_id}",
        task_runtime_identity=task_runtime_identity(record),
        network_isolation_evidence=evidence,
        network_isolation_evidence_digest=evidence.evidence_digest,
        network_isolation_status=evidence.status,
        network_isolation_reason=evidence.reason,
        analysis_eligibility=evidence.analysis_eligibility,
    )
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "result.json").write_text(
        json.dumps(
            {
                "id": f"job-{agent}",
                "n_total_trials": 1,
                "stats": {},
                "finished_at": finished_at,
            }
        )
    )
    (job_dir / "config.json").write_text("{}")
    (job_dir / "lock.json").write_text("{}")
    (job_dir / "lab-metadata.json").write_text(
        json.dumps({"experiment": provenance.model_dump(mode="json")})
    )
    job = load_job(job_dir)
    finalized = finalize_trial_admissibility(
        job=job,
        trial=job.trials[0],
        repo_root=repo,
    )
    assert finalized is not None
    assert finalized.causal_eligible


def test_registered_control_bootstrap_reaches_strict_final_admission(
    tmp_path: Path,
) -> None:
    repo, task = _copy_candidate(tmp_path)
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
        task_family="uppercase-fixture",
        state="registered",
        actor="admission-reviewer",
        certification_path=certification_path,
        stage_controls=True,
    )
    staged_identity = task_runtime_identity(staged)
    assert staged.state == "registered"
    assert staged.state_reason == "control_evidence_pending"
    assert staged.allowed_uses == ["canary"]
    assert staged.control_evidence is None

    staged_registry = TaskRegistry.from_repo(repo)
    control_spec = ExperimentSpec(
        name="bootstrap-oracle",
        hypothesis="control bootstrap",
        purpose="baseline",
        task="registered/uppercase-fixture",
        agent="oracle",
        submitted_by="test",
    )
    assert staged_registry.resolve_spec(control_spec, repo) == staged
    measurement_spec = control_spec.model_copy(
        update={"name": "forbidden-measurement", "agent": "codex"}
    )
    with pytest.raises(TaskUsageNotAllowedError, match="pending control evidence"):
        staged_registry.resolve_spec(measurement_spec, repo)

    _write_admissible_control_job(repo, task, staged, agent="oracle", reward=1.0)
    _write_admissible_control_job(repo, task, staged, agent="nop", reward=0.0)

    admitted = promote_task(
        task,
        repo,
        task_id="uppercase-fixture",
        task_family="uppercase-fixture",
        state="registered",
        actor="admission-reviewer",
        certification_path=certification_path,
    )
    assert admitted.state == "registered"
    assert admitted.state_reason is None
    assert admitted.allowed_uses == ["measurement", "training"]
    assert admitted.control_evidence is not None
    assert task_runtime_identity(admitted) == staged_identity
    assert TaskRegistry.from_repo(repo).get("uppercase-fixture") == admitted
