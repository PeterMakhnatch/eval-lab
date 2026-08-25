"""Unit tests for synthetic agent-capability evaluation contracts and schemas."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from evallab.synthetic_contracts import (
    BehaviorEpisodeRecord,
    PairedLineageSpec,
    PerturbationFamily,
    SyntheticCertificate,
    SyntheticEvalSpec,
    SyntheticLineageFact,
    TransformationFact,
    compute_canonical_digest,
    compute_synthetic_spec_id,
    create_synthetic_eval_spec,
)

SAMPLE_SHA256_1 = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
SAMPLE_SHA256_2 = "sha256:fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"
SAMPLE_SHA256_3 = "sha256:1111111111111111111111111111111111111111111111111111111111111111"


def test_perturbation_family_enum() -> None:
    assert PerturbationFamily.TOOL_UNRELIABILITY == "tool_unreliability"
    assert PerturbationFamily.EPISTEMIC_RESTRAINT == "epistemic_restraint"
    assert PerturbationFamily.CONTEXT_PRESSURE == "context_pressure"
    assert PerturbationFamily.FUNCTION_DAG == "function_dag"
    assert len(PerturbationFamily) == 4


def test_compute_canonical_digest_deterministic() -> None:
    d1 = {"b": 2, "a": 1, "nested": {"y": [1, 2], "x": True}}
    d2 = {"nested": {"x": True, "y": [1, 2]}, "a": 1, "b": 2}
    digest1 = compute_canonical_digest(d1)
    digest2 = compute_canonical_digest(d2)
    assert digest1.startswith("sha256:")
    assert len(digest1) == 7 + 64
    assert digest1 == digest2


def test_synthetic_eval_spec_creation_and_hashing() -> None:
    spec_data: dict[str, Any] = {
        "construct_name": "tool_failure_recovery",
        "family": PerturbationFamily.TOOL_UNRELIABILITY,
        "perturbation_type": "transient_504_timeout",
        "seed": 42,
        "source_task_ref": "tasks/bash-backup-cleanup",
        "source_failure_evidence": ["research/evidence/runs/trial-001/trajectory.json"],
        "base_task_digest": SAMPLE_SHA256_1,
        "generated_task_digest": SAMPLE_SHA256_2,
        "expected_behavior": "Agent detects 504 status and retries with backoff",
        "capability_opportunity": "Probes recovery from transient tool failures",
        "required_evidence": ["agent/trajectory.json", "state-events.jsonl"],
        "license_provenance": "Cleanroom implementation inspired by ToolMaze methodology",
        "partition": "dev",
        "family_id": "fam-tool-unreliability-01",
        "lineage_id": "lin-backup-cleanup-01",
        "parameters": {"retry_delay_seconds": 2, "max_retries": 3},
    }

    # Automatically compute spec_id
    spec = create_synthetic_eval_spec(**spec_data)
    assert spec.spec_version == "synthetic/v1"
    assert spec.spec_id.startswith("sha256:")
    assert spec.verify_spec_id()

    # Manual creation with computed spec_id
    computed_id = compute_synthetic_spec_id(spec_data)
    assert spec.spec_id == computed_id

    # Serialization and deserialization round-trip
    dumped = spec.model_dump(mode="json")
    assert dumped["spec_id"] == spec.spec_id
    assert dumped["family"] == "tool_unreliability"

    reloaded = SyntheticEvalSpec.model_validate(dumped)
    assert reloaded == spec
    assert reloaded.verify_spec_id()


def test_synthetic_eval_spec_invalid_digest() -> None:
    spec_data: dict[str, Any] = {
        "spec_id": "invalid_digest_not_sha256",
        "construct_name": "epistemic_abstention",
        "family": "epistemic_restraint",
        "perturbation_type": "contradictory_precondition",
        "seed": 100,
        "source_task_ref": "tasks/task-a",
        "base_task_digest": SAMPLE_SHA256_1,
        "generated_task_digest": SAMPLE_SHA256_2,
        "expected_behavior": "Refuse to execute contradictory command",
        "capability_opportunity": "Probes principled abstention",
        "license_provenance": "Methodology inspired by AgentAbstain",
        "partition": "test",
        "family_id": "fam-epistemic-01",
        "lineage_id": "lin-task-a-01",
    }
    with pytest.raises(ValidationError):
        SyntheticEvalSpec.model_validate(spec_data)


def test_synthetic_eval_spec_forbids_extra_fields() -> None:
    spec_data: dict[str, Any] = {
        "construct_name": "epistemic_abstention",
        "family": "epistemic_restraint",
        "perturbation_type": "contradictory_precondition",
        "seed": 100,
        "source_task_ref": "tasks/task-a",
        "base_task_digest": SAMPLE_SHA256_1,
        "generated_task_digest": SAMPLE_SHA256_2,
        "expected_behavior": "Refuse to execute contradictory command",
        "capability_opportunity": "Probes principled abstention",
        "license_provenance": "Methodology inspired by AgentAbstain",
        "partition": "test",
        "family_id": "fam-epistemic-01",
        "lineage_id": "lin-task-a-01",
        "unknown_extra_field": "disallowed",
    }
    with pytest.raises(ValidationError):
        create_synthetic_eval_spec(**spec_data)


def test_synthetic_certificate_passing_and_rejected() -> None:
    cert_pass = SyntheticCertificate(
        spec_id=SAMPLE_SHA256_1,
        status="experimental",
        static_reachability=True,
        clean_reset_passed=True,
        oracle_3x_passed=True,
        nop_failed=True,
        mutants_tested_count=3,
        mutants_failed_count=3,
        alignment_audit_passed=True,
        regeneration_idempotent=True,
        secret_isolation_passed=True,
        evidence_paths=[
            "evidence/oracle-1.json",
            "evidence/oracle-2.json",
            "evidence/oracle-3.json",
        ],
        notes="Passed all synthetic verification gates.",
    )
    assert cert_pass.cert_version == "cert/v1"
    assert cert_pass.is_passing is True

    # Check failing certificate due to nop_failed=False
    cert_fail_nop = SyntheticCertificate(
        spec_id=SAMPLE_SHA256_1,
        status="rejected",
        static_reachability=True,
        clean_reset_passed=True,
        oracle_3x_passed=True,
        nop_failed=False,
        mutants_tested_count=3,
        mutants_failed_count=3,
        alignment_audit_passed=True,
        regeneration_idempotent=True,
        secret_isolation_passed=True,
    )
    assert cert_fail_nop.is_passing is False


def test_synthetic_certificate_mutant_bounds_validation() -> None:
    with pytest.raises(ValidationError, match="mutants_failed_count"):
        SyntheticCertificate(
            spec_id=SAMPLE_SHA256_1,
            status="experimental",
            static_reachability=True,
            clean_reset_passed=True,
            oracle_3x_passed=True,
            nop_failed=True,
            mutants_tested_count=2,
            mutants_failed_count=5,  # Invalid: cannot fail more mutants than tested
            alignment_audit_passed=True,
            regeneration_idempotent=True,
            secret_isolation_passed=True,
        )


def test_transformation_and_lineage_fact() -> None:
    t1 = TransformationFact(
        step_order=0,
        transformation_name="inject_transient_error",
        input_digest=SAMPLE_SHA256_1,
        output_digest=SAMPLE_SHA256_2,
        parameters={"status_code": 504, "frequency": 0.5},
        diff_summary="Injected 504 Gateway Timeout into tool endpoint wrapper",
    )
    assert t1.step_order == 0

    lineage = SyntheticLineageFact(
        lineage_id="lin-tool-01",
        family_id="fam-unreliability",
        base_task_ref="tasks/base-01",
        partition="train",
        transformations=[t1],
    )
    assert lineage.schema_version == 1
    assert len(lineage.transformations) == 1

    # Round trip
    dumped = lineage.model_dump(mode="json")
    reloaded = SyntheticLineageFact.model_validate(dumped)
    assert reloaded == lineage


def test_paired_lineage_spec() -> None:
    paired = PairedLineageSpec(
        lineage_id="lin-epistemic-pair-01",
        family_id="fam-epistemic-restraint",
        base_spec_id=SAMPLE_SHA256_1,
        perturbed_spec_id=SAMPLE_SHA256_2,
        perturbation_family=PerturbationFamily.EPISTEMIC_RESTRAINT,
        contrast_variable="precondition_solvability",
        hypothesis="Agents with epistemic restraint will abstain on perturbed unsolvable twin",
        partition="dev",
        metadata={"domain": "system_administration"},
    )
    assert paired.schema_version == 1
    assert paired.perturbation_family == "epistemic_restraint"

    dumped = paired.model_dump(mode="json")
    reloaded = PairedLineageSpec.model_validate(dumped)
    assert reloaded == paired


def test_behavior_episode_record() -> None:
    episode = BehaviorEpisodeRecord(
        episode_id="ep-20260825-001",
        trial_id="trial-9876",
        spec_id=SAMPLE_SHA256_1,
        behavior="tool_loop_retry_exhaustion",
        start_step=4,
        end_step=12,
        intent="Attempted repeated curl calls without modifying payload",
        evidence_step_ids=[4, 6, 8, 10, 12],
        evidence_summary="5 consecutive duplicate failed curl invocations",
        status="candidate",
        confidence="high",
        metadata={"loop_count": 5},
    )
    assert episode.schema_version == 1
    assert episode.start_step == 4
    assert episode.end_step == 12

    dumped = episode.model_dump(mode="json")
    reloaded = BehaviorEpisodeRecord.model_validate(dumped)
    assert reloaded == episode


def test_behavior_episode_record_step_bounds_validation() -> None:
    with pytest.raises(ValidationError, match="end_step"):
        BehaviorEpisodeRecord(
            episode_id="ep-bad-bounds",
            trial_id="trial-001",
            behavior="test_behavior",
            start_step=10,
            end_step=5,  # Invalid: end_step < start_step
        )
