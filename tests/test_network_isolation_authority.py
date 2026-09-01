from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.interpretation.benchmark_events import (
    BenchmarkEventSchemaError,
    BenchmarkIngestionError,
    load_trial_bundle,
    parse_benchmark_contract,
)
from evallab.interpretation.producers import extract_benchmark_features
from evallab.profiles import compute_qualification_digest
from evallab.schemas import (
    DARWIN_ISOLATION_UNAVAILABLE_REASON,
    NETWORK_ESCAPE_CLASSES,
    AgentReadinessRecord,
    NetworkEscapeProbeResultV1,
    NetworkIsolationEvidenceV1,
    NetworkIsolationProbeIdentityV1,
    NetworkIsolationRuntimeIdentityV1,
    NetworkPolicyEvidenceV1,
    TaskRuntimeIdentityV1,
    TrialSourceDigestsV1,
    TrialSourcePathsV1,
    build_network_isolation_evidence,
    build_trial_admissibility,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
REPO_ROOT = Path(__file__).resolve().parents[1]


def _evidence(
    *,
    platform_system: str = "Linux",
    requested_mode: str = "no-network",
    effective_mode: str = "no-network",
    outcomes: tuple[str, ...] = ("blocked",) * 5,
    requested_verifier_phase_mode: str | None = "no-network",
    effective_verifier_phase_mode: str | None = "no-network",
    classes: tuple[str, ...] = NETWORK_ESCAPE_CLASSES,
) -> NetworkIsolationEvidenceV1:
    requested = NetworkPolicyEvidenceV1(mode=requested_mode)
    effective = NetworkPolicyEvidenceV1(mode=effective_mode)
    return build_network_isolation_evidence(
        requested_agent_policy=requested,
        effective_agent_policy=effective,
        requested_verifier_policy=requested,
        effective_verifier_policy=effective,
        requested_verifier_phase_policy=(
            NetworkPolicyEvidenceV1(mode=requested_verifier_phase_mode)
            if requested_verifier_phase_mode is not None
            else None
        ),
        effective_verifier_phase_policy=(
            NetworkPolicyEvidenceV1(mode=effective_verifier_phase_mode)
            if effective_verifier_phase_mode is not None
            else None
        ),
        runtime_identity=NetworkIsolationRuntimeIdentityV1(
            platform_system=platform_system,
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
                outcome=outcome,
                detail=f"test-{outcome}",
            )
            for escape_class, outcome in zip(classes, outcomes, strict=True)
        ),
        observed_at=NOW,
        valid_until=NOW + timedelta(days=7),
        evaluated_at=NOW,
    )


def _task_identity(state: str = "registered") -> TaskRuntimeIdentityV1:
    return TaskRuntimeIdentityV1(
        task_id="task-one",
        task_version="1.0.0",
        registry_record_digest="sha256:" + "e" * 64,
        certified_runtime_package_digest="sha256:" + "f" * 64,
        registry_admission_state=state,
    )


def test_complete_blocked_probe_contract_is_causal_eligible() -> None:
    evidence = _evidence()

    assert evidence.status == "enforced"
    assert evidence.reason is None
    assert evidence.analysis_eligibility == "causal-eligible"
    assert evidence.project(as_of=NOW + timedelta(days=1)).status == "enforced"


def test_partial_or_stale_probe_contract_is_unknown() -> None:
    partial = _evidence(
        outcomes=("blocked",) * 4,
        classes=NETWORK_ESCAPE_CLASSES[:-1],
    )

    assert partial.status == "unknown"
    assert partial.reason == "network_isolation_unknown:partial-probe-evidence"
    assert partial.analysis_eligibility == "calibration-only"
    stale = _evidence().project(as_of=NOW + timedelta(days=8))
    assert stale.status == "unknown"
    assert stale.reason == "network_isolation_unknown:stale-evidence"


def test_darwin_policy_mismatch_and_all_five_escapes_are_unavailable() -> None:
    evidence = _evidence(
        platform_system="Darwin",
        effective_mode="public",
        outcomes=("escaped",) * 5,
    )

    assert evidence.status == "unavailable"
    assert evidence.reason == DARWIN_ISOLATION_UNAVAILABLE_REASON
    assert evidence.analysis_eligibility == "calibration-only"


@pytest.mark.parametrize(
    ("requested_phase", "effective_phase"),
    ((None, "no-network"), ("no-network", None)),
)
def test_missing_either_verifier_phase_policy_is_unavailable(
    requested_phase: str | None,
    effective_phase: str | None,
) -> None:
    evidence = _evidence(
        requested_verifier_phase_mode=requested_phase,
        effective_verifier_phase_mode=effective_phase,
    )

    assert evidence.status == "unavailable"
    assert evidence.reason == "network_isolation_unavailable:missing-verifier-phase-policy-evidence"
    assert evidence.analysis_eligibility == "calibration-only"


def test_matching_public_policies_never_establish_isolation() -> None:
    evidence = _evidence(
        requested_mode="public",
        effective_mode="public",
        requested_verifier_phase_mode="public",
        effective_verifier_phase_mode="public",
    )

    assert evidence.status == "unavailable"
    assert evidence.reason == "network_isolation_unavailable:non-isolating-policy-mode"
    assert evidence.analysis_eligibility == "calibration-only"


def test_evidence_projection_and_digest_cannot_be_forged() -> None:
    evidence = _evidence()
    payload = evidence.model_dump(mode="json")
    payload["status"] = "unavailable"
    payload["analysis_eligibility"] = "calibration-only"

    with pytest.raises(ValidationError, match="status/reason/eligibility parity"):
        NetworkIsolationEvidenceV1.model_validate(payload)

    payload = evidence.model_dump(mode="json")
    payload["runtime_identity"]["adapter"] = "substituted-adapter"
    with pytest.raises(ValidationError, match="evidence digest mismatch"):
        NetworkIsolationEvidenceV1.model_validate(payload)


def test_trial_admissibility_requires_registered_runtime_complete_sources_and_isolation() -> None:
    sources = TrialSourceDigestsV1(
        contract=DIGEST,
        trajectory=DIGEST,
        final_state=DIGEST,
        verifier=DIGEST,
        outcome=DIGEST,
        interpretation=DIGEST,
    )
    source_paths = TrialSourcePathsV1(
        contract=("contract.json",),
        trajectory=("agent/trajectory.json",),
        final_state=("final-state.json",),
        verifier=("verifier/result.json", "verifier/reward.txt"),
        outcome=("result.json",),
        interpretation=("analysis/interpretation.json",),
    )
    admitted = build_trial_admissibility(
        trial_id="trial-one",
        task_runtime_identity=_task_identity(),
        source_digests=sources,
        source_paths=source_paths,
        network_isolation_evidence=_evidence(),
        evaluated_at=NOW,
    )
    assert admitted.causal_eligible is True
    assert admitted.allowed_use == "causal"

    candidate = build_trial_admissibility(
        trial_id="trial-one",
        task_runtime_identity=_task_identity("candidate"),
        source_digests=sources,
        network_isolation_evidence=_evidence(),
        source_paths=source_paths,
        evaluated_at=NOW,
    )
    assert candidate.decision == "rejected"
    assert candidate.allowed_use == "descriptive-only"

    incomplete = build_trial_admissibility(
        trial_id="trial-one",
        task_runtime_identity=_task_identity(),
        source_digests=sources.model_copy(update={"interpretation": None}),
        network_isolation_evidence=_evidence(),
        source_paths=source_paths,
        evaluated_at=NOW,
    )
    assert incomplete.decision == "unavailable"
    assert incomplete.allowed_use == "descriptive-only"


def test_reviewed_darwin_evidence_is_separate_and_transport_digest_is_unchanged() -> None:
    readiness_path = REPO_ROOT / "research/evidence/readiness/zai-opencode-glm-5.3.json"
    separate_path = readiness_path.with_name("zai-opencode-glm-5.3.network-isolation.json")
    readiness = AgentReadinessRecord.model_validate_json(readiness_path.read_text())
    evidence = NetworkIsolationEvidenceV1.model_validate_json(separate_path.read_text())

    assert readiness.network_isolation_evidence == evidence
    assert readiness.network_isolation_evidence_digest == evidence.evidence_digest
    assert readiness.network_isolation_status == "unavailable"
    assert readiness.analysis_eligibility == "calibration-only"
    assert evidence.reason == DARWIN_ISOLATION_UNAVAILABLE_REASON
    assert (
        evidence.evidence_digest
        == "sha256:0dea81047ac365ea89e1e3d4be5f10aacfac114b36c6550e0e721dc61a93f792"
    )
    assert tuple(result.escape_class for result in evidence.probe_results) == (
        NETWORK_ESCAPE_CLASSES
    )
    assert all(result.outcome == "escaped" for result in evidence.probe_results)
    assert readiness.qualification is not None
    assert compute_qualification_digest(readiness.qualification.smoke_records) == (
        "sha256:a6102664a924a6466799015f8cfcb5864bcc2d78a31dab7ca7ae5ac84d61ba98"
    )
    assert readiness.qualification.qualification_digest == (
        "sha256:a6102664a924a6466799015f8cfcb5864bcc2d78a31dab7ca7ae5ac84d61ba98"
    )


def test_historical_trial_labels_and_cell_factors_never_grant_authority(
    tmp_path: Path,
) -> None:
    trial_dir = tmp_path / "darwin-registered-causal-looking-label"
    trial_dir.mkdir()
    (trial_dir / "benchmark_contract.json").write_text(
        json.dumps(
            {
                "family": "action-memory-v1",
                "task_name": "registered-looking-task",
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

    bundle = load_trial_bundle(trial_dir)
    assert bundle.contract.task_id_explicit is False
    assert bundle.admissibility.decision == "unavailable"
    assert bundle.admissibility.allowed_use == "descriptive-only"
    assert bundle.registry_binding_verified is False
    with pytest.raises(BenchmarkIngestionError, match="descriptive-only"):
        extract_benchmark_features(bundle, governed=True)

    with pytest.raises(BenchmarkEventSchemaError, match="cannot be stored"):
        parse_benchmark_contract(
            {
                "family": "action-memory-v1",
                "seed": 42,
                "task_id": "task-one",
                "cell_factors": {"analysis_eligibility": "causal-eligible"},
            }
        )
