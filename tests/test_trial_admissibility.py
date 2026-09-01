from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

import evallab.trial_admissibility as trial_authority
from evallab.evidence.facts import (
    AnalyzerCallResult,
    extract_outcome_records,
    extract_trial_fact,
    run_trial_analysis,
)
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
    _source_authority,
    canonical_trial_admissibility_path,
    finalize_trial_admissibility,
    verify_trial_admissibility,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64

TRIAL_ID = "00000000-0000-0000-0000-000000000001"
JOB_ID = "00000000-0000-0000-0000-000000000002"
ANALYSIS_ID = "00000000-0000-0000-0000-000000000003"


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
    trial_dir = tmp_path / "job-one" / TRIAL_ID
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
    trajectory = {"schema_version": "1.0.0", "session_id": TRIAL_ID, "steps": []}
    (trial_dir / "agent/trajectory.json").write_text(
        json.dumps(trajectory),
        encoding="utf-8",
    )
    (trial_dir / "verifier/result.json").write_text(
        json.dumps({"rewards": {"reward": 1.0}}), encoding="utf-8"
    )
    (trial_dir / "verifier/reward.txt").write_text("1\n", encoding="utf-8")
    result = {
        "id": TRIAL_ID,
        "trial_name": TRIAL_ID,
        "task_name": "task-one",
        "verifier_result": {"rewards": {"reward": 1.0}},
        "finished_at": NOW.isoformat(),
    }
    (trial_dir / "lock.json").write_text("{}", encoding="utf-8")
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    source_digests = {
        "result": f"sha256:{sha256((trial_dir / 'result.json').read_bytes()).hexdigest()}",
        "task": f"sha256:{sha256((trial_dir / 'lock.json').read_bytes()).hexdigest()}",
        "trajectory": (
            f"sha256:{sha256((trial_dir / 'agent/trajectory.json').read_bytes()).hexdigest()}"
        ),
        "files": {
            relative: f"sha256:{sha256((trial_dir / relative).read_bytes()).hexdigest()}"
            for relative in ("lock.json", "result.json")
        },
    }
    interpretation = {
        "schema_version": 1,
        "analysis_id": ANALYSIS_ID,
        "experiment_id": "spec-one",
        "job_id": JOB_ID,
        "source_trial_id": TRIAL_ID,
        "source_trial_path": trial_dir.relative_to(tmp_path).as_posix(),
        "source_digests": source_digests,
        "analysis_provenance": {
            "agent": "test-analyzer",
            "agent_version": "1",
            "model": "test-model",
            "prompt_digest": DIGEST,
            "rubric_digest": DIGEST,
            "output_schema_digest": DIGEST,
            "created_at": NOW.isoformat(),
        },
        "output": {
            "validity": "valid_agent_attempt",
            "primary_category": "unknown",
            "summary": "Complete control interpretation.",
            "evidence": [{"path": "result.json", "supports": "Observed result."}],
            "proposed_discriminator": "No further discriminator.",
            "confidence": "high",
        },
        "validation_status": "valid",
        "validation_errors": [],
        "raw_response_digest": DIGEST,
    }
    (trial_dir / "analysis/interpretation.json").write_text(
        json.dumps(interpretation),
        encoding="utf-8",
    )
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
        result={"id": JOB_ID},
        config={},
        lock={},
        metadata={"experiment": provenance.model_dump(mode="json")},
        trials=(trial,),
    )
    return job, trial, provenance


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _rewrite_trial_result(
    trial: TrialRecord,
    result: dict[str, object],
) -> TrialRecord:
    result_path = trial.path / "result.json"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    digest = f"sha256:{sha256(result_path.read_bytes()).hexdigest()}"
    interpretation_path = trial.path / "analysis/interpretation.json"
    interpretation = json.loads(interpretation_path.read_text())
    interpretation["source_digests"]["result"] = digest
    interpretation["source_digests"]["files"]["result.json"] = digest
    interpretation_path.write_text(json.dumps(interpretation), encoding="utf-8")
    return replace(trial, result=result)


def _completion_case(trial: TrialRecord, case: str) -> TrialRecord:
    result: dict[str, object] = dict(trial.result)
    if case == "missing":
        result.pop("finished_at", None)
        result.pop("started_at", None)
    elif case == "start-only":
        result.pop("finished_at", None)
        result["started_at"] = NOW.isoformat()
    elif case == "malformed":
        result["finished_at"] = "not-a-timestamp"
    elif case == "naive":
        result["finished_at"] = NOW.replace(tzinfo=None).isoformat()
    else:
        raise AssertionError(f"unknown completion case: {case}")
    return _rewrite_trial_result(trial, result)


def _publish_forged_authority(
    repo_root: Path,
    trial: TrialRecord,
    provenance: RunProvenance,
    *,
    evaluated_at: datetime,
) -> None:
    source_paths, source_digests = _source_authority(
        trial.path.resolve(),
        repo_root=repo_root,
        trial_id=trial.id,
    )
    record = build_trial_admissibility(
        trial_id=trial.id,
        task_runtime_identity=provenance.task_runtime_identity,
        source_digests=source_digests,
        source_paths=source_paths,
        network_isolation_evidence=provenance.network_isolation_evidence,
        evaluated_at=evaluated_at,
    )
    authority = canonical_trial_admissibility_path(repo_root, trial.id)
    authority.parent.mkdir(parents=True, exist_ok=True)
    authority.write_bytes(_canonical_bytes(record.model_dump(mode="json")))


def _replace_result_completion(trial: TrialRecord, finished_at: datetime) -> None:
    result = json.loads((trial.path / "result.json").read_text(encoding="utf-8"))
    result["finished_at"] = finished_at.isoformat()
    (trial.path / "result.json").write_text(json.dumps(result), encoding="utf-8")


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
    assert list(artifact.parent.glob(f"{TRIAL_ID}.json")) == [artifact]
    assert first.record == second.record
    assert first.record.decision == "admissible"
    assert first.record.evaluated_at == NOW


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("missing", "missing-finished-at"),
        ("start-only", "missing-finished-at"),
        ("malformed", "malformed-finished-at"),
        ("naive", "naive-finished-at"),
    ),
)
def test_finalizer_requires_exact_aware_completion_time(
    tmp_path: Path,
    case: str,
    reason: str,
) -> None:
    job, trial, _ = _records(tmp_path)
    changed_trial = _completion_case(trial, case)

    with pytest.raises(TrialAdmissibilityError, match=reason):
        finalize_trial_admissibility(
            job=job,
            trial=changed_trial,
            repo_root=tmp_path,
        )
    assert not canonical_trial_admissibility_path(tmp_path, trial.id).exists()


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("missing", "missing-finished-at"),
        ("start-only", "missing-finished-at"),
        ("malformed", "malformed-finished-at"),
        ("naive", "naive-finished-at"),
    ),
)
def test_strict_loader_requires_exact_aware_completion_time(
    tmp_path: Path,
    case: str,
    reason: str,
) -> None:
    _, trial, provenance = _records(tmp_path)
    changed_trial = _completion_case(trial, case)
    _publish_forged_authority(
        tmp_path,
        changed_trial,
        provenance,
        evaluated_at=NOW,
    )

    with pytest.raises(TrialAdmissibilityError, match=reason):
        verify_trial_admissibility(
            trial_dir=changed_trial.path,
            trial_id=changed_trial.id,
            provenance=provenance,
            repo_root=tmp_path,
        )


def test_strict_loader_rejects_preexpiry_time_for_postexpiry_completion(
    tmp_path: Path,
) -> None:
    _, trial, provenance = _records(tmp_path)
    result: dict[str, object] = dict(trial.result)
    result["finished_at"] = (NOW + timedelta(days=8)).isoformat()
    changed_trial = _rewrite_trial_result(trial, result)
    _publish_forged_authority(
        tmp_path,
        changed_trial,
        provenance,
        evaluated_at=NOW,
    )

    with pytest.raises(TrialAdmissibilityError, match="completion-time-drift"):
        verify_trial_admissibility(
            trial_dir=changed_trial.path,
            trial_id=changed_trial.id,
            provenance=provenance,
            repo_root=tmp_path,
        )


def test_strict_loader_rejects_result_replacement_between_digest_and_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, trial, provenance = _records(tmp_path)
    later = NOW + timedelta(days=1)
    _publish_forged_authority(
        tmp_path,
        trial,
        provenance,
        evaluated_at=later,
    )
    original_source_authority = trial_authority._source_authority

    def hash_then_replace_result(*args: object, **kwargs: object):
        authority = original_source_authority(*args, **kwargs)
        _replace_result_completion(trial, later)
        return authority

    monkeypatch.setattr(
        trial_authority,
        "_source_authority",
        hash_then_replace_result,
    )
    with pytest.raises(
        TrialAdmissibilityError,
        match="completion-time-drift|result-snapshot-drift",
    ):
        verify_trial_admissibility(
            trial_dir=trial.path,
            trial_id=trial.id,
            provenance=provenance,
            repo_root=tmp_path,
        )


def test_finalizer_rejects_result_replacement_after_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, trial, _ = _records(tmp_path)
    later = NOW + timedelta(days=1)
    original_source_authority = trial_authority._source_authority

    def hash_then_replace_result(*args: object, **kwargs: object):
        authority = original_source_authority(*args, **kwargs)
        _replace_result_completion(trial, later)
        return authority

    monkeypatch.setattr(
        trial_authority,
        "_source_authority",
        hash_then_replace_result,
    )
    with pytest.raises(TrialAdmissibilityError, match="result-snapshot-drift"):
        finalize_trial_admissibility(
            job=job,
            trial=trial,
            repo_root=tmp_path,
        )
    assert not canonical_trial_admissibility_path(tmp_path, trial.id).exists()


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
    revision = tmp_path / f"analysis-revisions/{TRIAL_ID}/interpretation.json"
    revision.parent.mkdir(parents=True)
    revision_payload = json.loads((trial.path / "analysis/interpretation.json").read_text())
    revision_payload["analysis_id"] = "00000000-0000-0000-0000-000000000004"
    (trial.path / "analysis/interpretation.json").unlink()
    revision.write_text(json.dumps(revision_payload), encoding="utf-8")

    with pytest.raises(TrialAdmissibilityError, match="conflicting-existing-artifact"):
        finalize_trial_admissibility(
            job=job,
            trial=trial,
            repo_root=tmp_path,
            interpretation_path=revision,
        )


def test_repository_authority_rejects_alternate_artifact_path(
    tmp_path: Path,
) -> None:
    job, trial, provenance = _records(tmp_path)
    alternate = tmp_path / "alternate/trial-admissibility.json"

    with pytest.raises(TrialAdmissibilityError, match="alternate-authority-path"):
        finalize_trial_admissibility(
            job=job,
            trial=trial,
            repo_root=tmp_path,
            artifact_path=alternate,
        )
    with pytest.raises(TrialAdmissibilityError, match="alternate-authority-path"):
        verify_trial_admissibility(
            trial_dir=trial.path,
            trial_id=trial.id,
            provenance=provenance,
            repo_root=tmp_path,
            artifact_path=alternate,
        )
    assert not alternate.exists()


def test_strict_loader_rejects_digest_bound_invalid_interpretation(
    tmp_path: Path,
) -> None:
    job, trial, provenance = _records(tmp_path)
    finalized = finalize_trial_admissibility(
        job=job,
        trial=trial,
        repo_root=tmp_path,
    )
    assert finalized is not None
    interpretation = trial.path / "analysis/interpretation.json"
    payload = json.loads(interpretation.read_text())
    payload["validation_status"] = "invalid"
    payload["validation_errors"] = ["missing source evidence"]
    interpretation.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TrialAdmissibilityError, match="invalid-interpretation"):
        verify_trial_admissibility(
            trial_dir=trial.path,
            trial_id=trial.id,
            provenance=provenance,
            repo_root=tmp_path,
        )


def test_analysis_producer_cannot_publish_invalid_interpretation(
    tmp_path: Path,
) -> None:
    job, trial, _ = _records(tmp_path)
    (trial.path / "analysis/interpretation.json").unlink()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("{source_trial_path}\n{rubric}\n{output_schema}")
    rubric = tmp_path / "rubric.json"
    rubric.write_text("{}")
    invalid_output = {
        "validity": "valid_agent_attempt",
        "primary_category": "unknown",
        "summary": "Invalid citation.",
        "evidence": [{"path": "missing.txt", "supports": "Missing source."}],
        "proposed_discriminator": "Inspect the missing source.",
        "confidence": "low",
    }

    with pytest.raises(TrialAdmissibilityError, match="invalid-interpretation"):
        run_trial_analysis(
            job,
            trial,
            analyzer=lambda _prompt, _schema: AnalyzerCallResult(
                raw_output=json.dumps(invalid_output)
            ),
            repo_root=tmp_path,
            destination_root=tmp_path / "analysis",
            prompt_path=prompt,
            rubric_path=rubric,
            agent="test-analyzer",
            agent_version="1",
            model="test-model",
            created_at=NOW,
        )
    assert not canonical_trial_admissibility_path(tmp_path, trial.id).exists()
