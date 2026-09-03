from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.analysis_capability import (
    AnalysisMethod,
    AnalysisUnit,
    DenominatorPolicy,
    create_campaign_analysis_spec,
)
from evallab.benchmark_program_contracts import canonical_json, compute_sha256
from evallab.execution_contracts import load_policy
from evallab.paired_intervention import (
    CaptureExpectation,
    ExtraInstructionDelta,
    PairedAnalysisGate,
    PairedArmCandidate,
    RetryReplacementPolicy,
    plan_paired_intervention,
)
from evallab.paired_outcome import (
    MeasurementBasis,
    OutcomeStatus,
    PairedInterventionOutcomeV1,
    PairedOutcomeDecisionRuleV1,
    PairedOutcomeRefusal,
    PairedTrialObservationV1,
    PairExclusionCode,
    analyze_paired_outcomes,
)
from evallab.queue import PolicyGate
from evallab.schemas import (
    NETWORK_ESCAPE_CLASSES,
    ElicitationSpec,
    ExperimentSpec,
    NetworkEscapeProbeResultV1,
    NetworkIsolationProbeIdentityV1,
    NetworkIsolationRuntimeIdentityV1,
    NetworkPolicyEvidenceV1,
    PreregSpec,
    TaskRuntimeIdentityV1,
    TrialSourceDigestsV1,
    TrialSourcePathsV1,
    build_network_isolation_evidence,
    build_trial_admissibility,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)


def _digest(value: object) -> str:
    return f"sha256:{compute_sha256(canonical_json(value))}"


def _task_runtime(
    *,
    registry_state: str = "registered",
    suffix: str = "base",
    package_suffix: str = "paired-task:1",
):
    return TaskRuntimeIdentityV1(
        task_id="paired-task",
        task_version="1",
        registry_record_digest=_digest(f"registry:{suffix}"),
        certified_runtime_package_digest=_digest(f"package:{package_suffix}"),
        registry_admission_state=registry_state,
    )


def _isolation_evidence(*, suffix: str = "base"):
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
            platform_release="fixture",
            platform_machine="arm64",
            container_runtime="docker",
            container_runtime_version="29",
            container_image_digest=_digest(f"container:{suffix}"),
            adapter="fixture-adapter",
            adapter_version="1",
            adapter_digest=_digest("adapter"),
        ),
        probe_identity=NetworkIsolationProbeIdentityV1(
            implementation="fixture-probe",
            implementation_version="1",
            implementation_digest=_digest("probe"),
            config_digest=_digest(f"probe-config:{suffix}"),
        ),
        probe_results=tuple(
            NetworkEscapeProbeResultV1(
                escape_class=escape_class,
                target=f"http://blocked.invalid/{escape_class}",
                outcome="blocked",
                detail="blocked",
            )
            for escape_class in NETWORK_ESCAPE_CLASSES
        ),
        observed_at=NOW,
        valid_until=NOW + timedelta(days=1),
        evaluated_at=NOW,
    )


def _admissibility(
    *,
    trial_id: str,
    outcome_digest: str,
    runtime: TaskRuntimeIdentityV1,
    environment_suffix: str = "base",
):
    source_digests = TrialSourceDigestsV1(
        contract=_digest(f"{trial_id}:contract"),
        trajectory=_digest(f"{trial_id}:trajectory"),
        final_state=_digest(f"{trial_id}:final-state"),
        verifier=_digest(f"{trial_id}:verifier"),
        outcome=outcome_digest,
        interpretation=_digest(f"{trial_id}:interpretation"),
    )
    source_paths = TrialSourcePathsV1(
        contract=(f"fixtures/{trial_id}/contract.json",),
        trajectory=(f"fixtures/{trial_id}/trajectory.json",),
        final_state=(f"fixtures/{trial_id}/final-state.json",),
        verifier=(f"fixtures/{trial_id}/verifier.json",),
        outcome=(f"fixtures/{trial_id}/outcome.json",),
        interpretation=(f"fixtures/{trial_id}/interpretation.json",),
    )
    return build_trial_admissibility(
        trial_id=trial_id,
        task_runtime_identity=runtime,
        source_digests=source_digests,
        source_paths=source_paths,
        network_isolation_evidence=_isolation_evidence(suffix=environment_suffix),
        evaluated_at=NOW,
    )


def _plan(tmp_path: Path, *, pairs: int = 6):
    prompt_content = b"Order evidence before conclusions.\n"
    prompt = tmp_path / "interventions" / "ordering.txt"
    prompt.parent.mkdir(parents=True)
    prompt.write_bytes(prompt_content)
    delta = ExtraInstructionDelta(
        treatment_path="interventions/ordering.txt",
        treatment_sha256=f"sha256:{hashlib.sha256(prompt_content).hexdigest()}",
    )
    capture = CaptureExpectation(
        required_artifacts=(
            "environment-integrity.json",
            "result.json",
            "trajectory/trajectory.json",
        )
    )
    candidates: list[PairedArmCandidate] = []
    for index in range(pairs):
        pair_id = f"pair-{index + 1}"
        assignment = f"task-{index + 1}-seed-{100 + index}"
        block_id = f"cluster-{index % 2 + 1}"
        for arm in ("control", "treatment"):
            treatment = arm == "treatment"
            spec = ExperimentSpec(
                name=f"{pair_id}-{arm}",
                hypothesis="The ordering prompt changes evaluator-backed task success.",
                purpose="elicitation",
                question_ref="RQ-track-g-paired-outcome",
                elicitation=ElicitationSpec(
                    preamble_hash=delta.treatment_sha256 if treatment else None,
                    toolset=["bash", "read"],
                ),
                prereg=PreregSpec(
                    expected="Treatment improves paired task success.",
                    decision_rule="Use complete evaluator-backed pairs and the exact paired contrast.",
                ),
                task="fixtures/paired-task",
                extra_instruction_path=delta.treatment_path if treatment else None,
                extra_instruction_sha256=delta.treatment_sha256 if treatment else None,
                agent="codex",
                model="gpt-5.6",
                submitted_by="track-g-test",
                est_cost_usd=0.25,
                task_version="1",
                verifier_digest=_digest("verifier-code"),
                task_package_digest=_digest("package:paired-task:1"),
                task_family="fixture",
                task_id="paired-task",
                task_instance_id=assignment,
                generator_seed=100 + index,
                max_requests=20,
                max_input_tokens=20_000,
                max_output_tokens=10_000,
                max_total_tokens=30_000,
                cost_limit_usd=0.25,
            )
            candidates.append(
                PairedArmCandidate(
                    pair_id=pair_id,
                    block_id=block_id,
                    assignment_unit_id=assignment,
                    arm=arm,
                    spec=spec,
                    capture=capture,
                )
            )
    analysis = create_campaign_analysis_spec(
        spec_id="track-g-task-success",
        method=AnalysisMethod.PAIRED_SIGN,
        outcome_feature="task_success",
        predictor_features=("arm",),
        group_by=("block_id",),
        unit=AnalysisUnit.PAIRED_SEED,
        unit_keys=("assignment_unit_id", "arm"),
        pair_keys=("pair_id",),
        denominator_policy=DenominatorPolicy.REQUIRED,
        ci_method="exact",
        minimum_informative_units=2,
    )
    return plan_paired_intervention(
        plan_id="track-g-ordering",
        randomization_seed=9917,
        candidates=candidates,
        delta=delta,
        retry_policy=RetryReplacementPolicy(),
        analysis_gate=PairedAnalysisGate(
            analysis_spec=analysis,
            minimum_complete_pairs=2,
        ),
        policy_gate=PolicyGate(
            load_policy(REPO_ROOT / "policy" / "standing-approvals.yaml"),
            repo_root=tmp_path,
        ),
        repo_root=tmp_path,
        spent_today_usd=0.0,
    )


def _rule(*, minimum_pairs: int = 6):
    return PairedOutcomeDecisionRuleV1(
        metric_name="task_success",
        direction="higher",
        minimum_effect=0.2,
        minimum_eligible_pairs=minimum_pairs,
    )


def _observation(
    plan,
    scheduled,
    *,
    value: float | None = None,
    trial_id: str | None = None,
    outcome_digest: str | None = None,
    runtime: TaskRuntimeIdentityV1 | None = None,
    environment_suffix: str = "base",
):
    pair_ordinal = (scheduled.ordinal + 1) // 2
    trial_id = trial_id or f"trial-{scheduled.ordinal}"
    outcome_digest = outcome_digest or _digest(f"outcome:{trial_id}")
    runtime = runtime or _task_runtime()
    authority = _admissibility(
        trial_id=trial_id,
        outcome_digest=outcome_digest,
        runtime=runtime,
        environment_suffix=environment_suffix,
    )
    environment_digest = authority.network_isolation_evidence_digest
    assert environment_digest is not None
    metric_value = value if value is not None else (1.0 if scheduled.arm == "treatment" else 0.0)
    return PairedTrialObservationV1(
        plan_digest=plan.plan_digest,
        spec_digest=scheduled.spec_digest,
        intervention_delta_digest=_digest(plan.delta.model_dump(mode="json")),
        schedule_ordinal=scheduled.ordinal,
        pair_ordinal=pair_ordinal,
        pair_id=scheduled.pair_id,
        block_id=scheduled.block_id,
        assignment_unit_id=scheduled.assignment_unit_id,
        arm=scheduled.arm,
        randomization_seed=plan.randomization_seed,
        carryover_status="isolated",
        trial_id=trial_id,
        task_ref=scheduled.spec.task,
        task_id=scheduled.spec.task_id,
        task_version=scheduled.spec.task_version,
        task_instance_id=scheduled.spec.task_instance_id,
        generator_seed=scheduled.spec.generator_seed,
        task_cluster_id=scheduled.block_id,
        environment_name=scheduled.spec.environment,
        environment_identity_digest=environment_digest,
        runtime_identity_digest=_digest(runtime.model_dump(mode="json")),
        outcome_artifact_digest=outcome_digest,
        trial_admissibility_digest=authority.admissibility_digest,
        trial_admissibility_decision=authority.decision,
        analysis_eligibility=authority.analysis_eligibility,
        allowed_use=authority.allowed_use,
        admissibility=authority,
        capture_status="complete",
        measurement_basis=MeasurementBasis.EVALUATOR_BACKED,
        metric_name="task_success",
        metric_value=metric_value,
        uncertainty_basis="not_available",
    )


def _observations(plan):
    return [_observation(plan, scheduled) for scheduled in plan.schedule]


def _analyze(plan, observations, *, minimum_pairs: int = 6):
    return analyze_paired_outcomes(
        artifact_id="track-g-fixture-outcome",
        plan=plan,
        observations=observations,
        decision_rule=_rule(minimum_pairs=minimum_pairs),
    )


def _replace(observations, replacement):
    return [
        replacement if item.schedule_ordinal == replacement.schedule_ordinal else item
        for item in observations
    ]


def _redigest_outcome(payload: dict[str, object]) -> dict[str, object]:
    body = {key: value for key, value in payload.items() if key != "outcome_digest"}
    payload["outcome_digest"] = _digest(body)
    return payload


def test_supported_outcome_is_deterministic_bounded_and_rehydratable(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    observations = _observations(plan)

    first = _analyze(plan, observations)
    second = _analyze(plan, list(reversed(observations)))

    assert first.model_dump_json() == second.model_dump_json()
    assert PairedInterventionOutcomeV1.model_validate_json(first.model_dump_json()) == first
    assert first.status == OutcomeStatus.SUPPORTED
    assert first.claim_scope == "priority_only_never_general"
    assert first.planned_pair_count == 6
    assert first.denominator_eligible_pairs == 6
    assert first.excluded_pair_count == 0
    assert first.numerator_directional_improvement_pairs == 6
    assert first.directional_regression_pairs == 0
    assert first.tied_pairs == 0
    assert first.mean_paired_difference == 1.0
    assert first.exact_binary_contrast is not None
    assert first.exact_binary_contrast.exact_p_value == pytest.approx(0.03125)


def test_significant_opposite_effect_is_refuted(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    observations = [
        _observation(
            plan,
            scheduled,
            value=0.0 if scheduled.arm == "treatment" else 1.0,
        )
        for scheduled in plan.schedule
    ]

    outcome = _analyze(plan, observations)

    assert outcome.status == OutcomeStatus.REFUTED
    assert outcome.numerator_directional_improvement_pairs == 0
    assert outcome.directional_regression_pairs == 6


def test_missing_pair_arm_is_refused(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    observations = _observations(plan)

    with pytest.raises(PairedOutcomeRefusal, match="missing_pair_arm"):
        _analyze(plan, observations[:-1])


def test_extra_pair_is_refused(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    observations = _observations(plan)
    extra = observations[0].model_copy(update={"pair_id": "extra-pair"})

    with pytest.raises(PairedOutcomeRefusal, match="extra_pair"):
        _analyze(plan, _replace(observations, extra))


def test_duplicate_arm_is_refused(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    observations = _observations(plan)

    with pytest.raises(PairedOutcomeRefusal, match="duplicate_arm"):
        _analyze(plan, [*observations, observations[0]])


def test_control_treatment_spec_swap_is_refused(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    observations = _observations(plan)
    swapped = observations[0].model_copy(update={"spec_digest": observations[1].spec_digest})

    with pytest.raises(PairedOutcomeRefusal, match="spec_digest_substitution"):
        _analyze(plan, _replace(observations, swapped))


def test_plan_and_delta_digest_substitution_are_refused(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    observations = _observations(plan)
    forged = observations[0].model_copy(update={"plan_digest": _digest("other-plan")})
    with pytest.raises(PairedOutcomeRefusal, match="plan_digest_mismatch"):
        _analyze(plan, _replace(observations, forged))

    forged = observations[0].model_copy(
        update={"intervention_delta_digest": _digest("other-delta")}
    )
    with pytest.raises(PairedOutcomeRefusal, match="intervention_delta_mismatch"):
        _analyze(plan, _replace(observations, forged))


def test_randomization_mismatch_and_self_report_are_refused(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    observations = _observations(plan)
    mismatched = observations[0].model_copy(
        update={"randomization_seed": plan.randomization_seed + 1}
    )
    with pytest.raises(PairedOutcomeRefusal, match="assignment_mismatch"):
        _analyze(plan, _replace(observations, mismatched))

    payload = observations[0].model_dump(mode="json")
    payload["randomization_source"] = "self_reported"
    with pytest.raises(ValidationError, match="plan"):
        PairedTrialObservationV1.model_validate(payload)


def test_recomputed_artifact_digest_cannot_hide_semantic_mutation(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    outcome = _analyze(plan, _observations(plan))
    payload = outcome.model_dump(mode="json")
    payload["status"] = "refuted"

    with pytest.raises(ValueError, match="derived outcome field 'status'"):
        PairedInterventionOutcomeV1.model_validate(_redigest_outcome(payload))


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            {"measurement_basis": MeasurementBasis.REWARD_ONLY},
            PairExclusionCode.NOT_EVALUATOR_BACKED,
        ),
        ({"capture_status": "missing"}, PairExclusionCode.CAPTURE_INCOMPLETE),
    ],
)
def test_ineligible_arm_is_retained_as_typed_excluded_pair(
    tmp_path: Path,
    mutation: dict[str, object],
    reason: PairExclusionCode,
) -> None:
    plan = _plan(tmp_path)
    observations = _observations(plan)
    changed = observations[0].model_copy(update=mutation)

    outcome = _analyze(plan, _replace(observations, changed))

    assert outcome.status == OutcomeStatus.INCONCLUSIVE
    assert outcome.denominator_eligible_pairs == 5
    assert outcome.excluded_pair_count == 1
    assert outcome.pairs[0].eligible is False
    assert reason in outcome.pairs[0].exclusion_reasons


def test_environment_mismatch_is_retained_as_typed_exclusion(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    observations = _observations(plan)
    changed = _observation(
        plan,
        plan.schedule[0],
        environment_suffix="other",
    )

    outcome = _analyze(plan, _replace(observations, changed))

    assert outcome.denominator_eligible_pairs == 5
    assert PairExclusionCode.ENVIRONMENT_MISMATCH in outcome.pairs[0].exclusion_reasons


def test_inadmissible_arm_is_retained_with_closed_reasons(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    observations = _observations(plan)
    changed = _observation(
        plan,
        plan.schedule[0],
        runtime=_task_runtime(registry_state="candidate", suffix="candidate"),
    )

    outcome = _analyze(plan, _replace(observations, changed))

    reasons = outcome.pairs[0].exclusion_reasons
    assert PairExclusionCode.ARM_NOT_ADMISSIBLE in reasons
    assert PairExclusionCode.NOT_CAUSAL_ALLOWED_USE in reasons
    assert PairExclusionCode.RUNTIME_MISMATCH in reasons
    assert outcome.denominator_eligible_pairs == 5


def test_runtime_package_substitution_is_refused(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    observations = _observations(plan)
    changed = _observation(
        plan,
        plan.schedule[0],
        runtime=_task_runtime(package_suffix="substituted"),
    )

    with pytest.raises(PairedOutcomeRefusal, match="runtime_identity_substitution"):
        _analyze(plan, _replace(observations, changed))


def test_denominator_zero_is_explicitly_unavailable(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    observations = [
        observation.model_copy(update={"capture_status": "missing"})
        for observation in _observations(plan)
    ]

    outcome = _analyze(plan, observations)

    assert outcome.status == OutcomeStatus.UNAVAILABLE
    assert outcome.denominator_eligible_pairs == 0
    assert outcome.excluded_pair_count == 6
    assert outcome.mean_paired_difference is None
    assert outcome.exact_binary_contrast is None


def test_nonbinary_scalar_is_descriptive_and_inconclusive(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    observations = [
        _observation(
            plan,
            scheduled,
            value=float(scheduled.ordinal) / 10.0,
        )
        for scheduled in plan.schedule
    ]

    outcome = _analyze(plan, observations)

    assert outcome.denominator_eligible_pairs == 6
    assert outcome.exact_binary_contrast is None
    assert outcome.status == OutcomeStatus.INCONCLUSIVE


def test_cross_cluster_carryover_and_ordering_violations_are_refused(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    observations = _observations(plan)
    changed = observations[0].model_copy(update={"task_cluster_id": "other-cluster"})
    with pytest.raises(PairedOutcomeRefusal, match="cross_cluster_leakage"):
        _analyze(plan, _replace(observations, changed))

    changed = observations[0].model_copy(update={"carryover_status": "violation"})
    with pytest.raises(PairedOutcomeRefusal, match="carryover_violation"):
        _analyze(plan, _replace(observations, changed))

    changed = observations[0].model_copy(update={"schedule_ordinal": 2})
    with pytest.raises(PairedOutcomeRefusal, match="assignment_mismatch|duplicate_arm"):
        _analyze(plan, _replace(observations, changed))


def test_repeated_trial_and_outcome_identities_are_refused(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    observations = _observations(plan)
    duplicate_trial = _observation(
        plan,
        plan.schedule[1],
        trial_id=observations[0].trial_id,
    )
    with pytest.raises(PairedOutcomeRefusal, match="duplicate_trial"):
        _analyze(plan, _replace(observations, duplicate_trial))

    duplicate_outcome = _observation(
        plan,
        plan.schedule[1],
        outcome_digest=observations[0].outcome_artifact_digest,
    )
    with pytest.raises(PairedOutcomeRefusal, match="duplicate_outcome"):
        _analyze(plan, _replace(observations, duplicate_outcome))


def test_unknown_field_unknown_arm_and_nonfinite_metric_are_rejected(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    observation = _observations(plan)[0]
    payload = observation.model_dump(mode="json")
    payload["forged"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PairedTrialObservationV1.model_validate(payload)

    payload = observation.model_dump(mode="json")
    payload["arm"] = "baseline"
    with pytest.raises(ValidationError, match="control.*treatment"):
        PairedTrialObservationV1.model_validate(payload)

    payload = observation.model_dump(mode="json")
    payload["metric_value"] = float("inf")
    with pytest.raises(ValidationError, match="metric_value must be finite"):
        PairedTrialObservationV1.model_validate(payload)
