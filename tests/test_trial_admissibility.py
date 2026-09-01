from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evallab.evidence.facts import extract_outcome_records, extract_trial_fact
from evallab.interpretation.benchmark_events import (
    BenchmarkContractDriftError,
    load_trial_bundle,
)
from evallab.results import JobRecord, TrialRecord
from evallab.schemas import (
    NETWORK_ESCAPE_CLASSES,
    NetworkEscapeProbeResultV1,
    NetworkIsolationProbeIdentityV1,
    NetworkIsolationRuntimeIdentityV1,
    NetworkPolicyEvidenceV1,
    RunProvenance,
    TaskRuntimeIdentityV1,
    TrialSourceDigestsV1,
    build_network_isolation_evidence,
    build_trial_admissibility,
)
from evallab.trial_admissibility import (
    TrialAdmissibilityError,
    canonical_trial_admissibility_path,
    finalize_trial_admissibility,
    verify_trial_admissibility,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def _isolation_evidence():
    policy = NetworkPolicyEvidenceV1(mode="no-network")
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
            container_image_digest=DIGEST,
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
        observed_at=NOW,
        valid_until=NOW + timedelta(days=7),
        evaluated_at=NOW,
    )


def _records(tmp_path: Path) -> tuple[JobRecord, TrialRecord, RunProvenance]:
    trial_dir = tmp_path / "job-one" / "trial-one"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "verifier").mkdir()
    (trial_dir / "analysis").mkdir()
    (trial_dir / "benchmark_contract.json").write_text(
        json.dumps(
            {
                "family": "action-memory-v1",
                "task_id": "task-one",
                "task_name": "task-one",
                "cell_factors": {"seed": 42},
            }
        ),
        encoding="utf-8",
    )
    (trial_dir / "benchmark-events.jsonl").write_text(
        json.dumps(
            {
                "event_index": 1,
                "timestamp": "2026-08-31T12:00:00Z",
                "event_type": "noop",
            }
        )
        + "\n",
        encoding="utf-8",
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
        ),
        encoding="utf-8",
    )
    (trial_dir / "agent/trajectory.json").write_text(
        json.dumps({"schema_version": "1.0.0", "session_id": "trial-one", "steps": []}),
        encoding="utf-8",
    )
    (trial_dir / "verifier/result.json").write_text(
        json.dumps({"rewards": {"reward": 1.0}}), encoding="utf-8"
    )
    (trial_dir / "verifier/reward.txt").write_text("1\n", encoding="utf-8")
    (trial_dir / "analysis/interpretation.json").write_text(
        json.dumps({"disposition": "complete"}), encoding="utf-8"
    )
    result = {
        "id": "trial-one",
        "trial_name": "trial-one",
        "task_name": "task-one",
        "verifier_result": {"rewards": {"reward": 1.0}},
    }
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    identity = TaskRuntimeIdentityV1(
        task_id="task-one",
        task_version="1.0.0",
        registry_record_digest="sha256:" + "e" * 64,
        certified_runtime_package_digest="sha256:" + "f" * 64,
        registry_admission_state="registered",
    )
    evidence = _isolation_evidence()
    provenance = RunProvenance(
        spec_id="spec-one",
        task="registered/task-one",
        task_runtime_identity=identity,
        network_isolation_evidence=evidence,
        network_isolation_evidence_digest=evidence.evidence_digest,
        network_isolation_status=evidence.status,
        network_isolation_reason=evidence.reason,
        analysis_eligibility=evidence.analysis_eligibility,
    )
    trial = TrialRecord(
        path=trial_dir,
        result=result,
        config={},
        lock={},
        rewards={"reward": 1.0},
        artifacts=(),
    )
    job = JobRecord(
        path=trial_dir.parent,
        result={"id": "job-one"},
        config={},
        lock={},
        metadata={"experiment": provenance.model_dump(mode="json")},
        trials=(trial,),
    )
    return job, trial, provenance


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def test_finalization_atomically_generates_exactly_one_canonical_artifact(
    tmp_path: Path,
) -> None:
    job, trial, _ = _records(tmp_path)
    interpretation = trial.path / "analysis/interpretation.json"
    interpretation_bytes = interpretation.read_bytes()
    interpretation.unlink()
    assert finalize_trial_admissibility(job=job, trial=trial, repo_root=tmp_path) is None
    artifact = canonical_trial_admissibility_path(tmp_path, trial.id)
    assert not artifact.exists()
    interpretation.write_bytes(interpretation_bytes)

    first = finalize_trial_admissibility(job=job, trial=trial, repo_root=tmp_path)
    assert first is not None
    artifact = canonical_trial_admissibility_path(tmp_path, trial.id)
    first_bytes = artifact.read_bytes()
    first_inode = artifact.stat().st_ino
    second = finalize_trial_admissibility(job=job, trial=trial, repo_root=tmp_path)
    assert second is not None

    assert artifact.read_bytes() == first_bytes
    assert artifact.stat().st_ino == first_inode
    assert list(artifact.parent.glob("trial-one.json")) == [artifact]
    assert first.record == second.record
    assert first.record.decision == "admissible"


def test_self_consistent_forged_source_chain_is_rejected_by_every_consumer(
    tmp_path: Path,
) -> None:
    job, trial, provenance = _records(tmp_path)
    finalized = finalize_trial_admissibility(job=job, trial=trial, repo_root=tmp_path)
    assert finalized is not None
    generated = finalized.record
    forged_sources = TrialSourceDigestsV1.model_validate(
        {name: "sha256:" + "9" * 64 for name in TrialSourceDigestsV1.model_fields}
    )
    forged = build_trial_admissibility(
        trial_id=trial.id,
        task_runtime_identity=generated.task_runtime_identity,
        source_digests=forged_sources,
        source_paths=generated.source_paths,
        network_isolation_evidence=generated.network_isolation_evidence,
        evaluated_at=generated.evaluated_at,
    )
    canonical_trial_admissibility_path(tmp_path, trial.id).write_bytes(
        _canonical_bytes(forged.model_dump(mode="json"))
    )

    with pytest.raises(TrialAdmissibilityError, match="source-digest-drift"):
        verify_trial_admissibility(
            trial_dir=trial.path,
            trial_id=trial.id,
            provenance=provenance,
            repo_root=tmp_path,
        )
    with pytest.raises(TrialAdmissibilityError, match="source-digest-drift"):
        extract_trial_fact(job, trial, repo_root=tmp_path)
    with pytest.raises(TrialAdmissibilityError, match="source-digest-drift"):
        extract_outcome_records(job, trial, repo_root=tmp_path)
    with pytest.raises(BenchmarkContractDriftError, match="source-digest-drift"):
        load_trial_bundle(
            trial.path,
            provenance=provenance,
            repo_root=tmp_path,
        )


def test_finalization_refuses_to_overwrite_conflicting_authority(
    tmp_path: Path,
) -> None:
    job, trial, _ = _records(tmp_path)
    finalize_trial_admissibility(job=job, trial=trial, repo_root=tmp_path)
    (trial.path / "analysis/interpretation.json").unlink()
    revision = tmp_path / "analysis-revisions/trial-one/interpretation.json"
    revision.parent.mkdir(parents=True)
    revision.write_text(json.dumps({"disposition": "changed"}), encoding="utf-8")

    with pytest.raises(TrialAdmissibilityError, match="conflicting-existing-artifact"):
        finalize_trial_admissibility(
            job=job,
            trial=trial,
            repo_root=tmp_path,
            interpretation_path=revision,
        )
