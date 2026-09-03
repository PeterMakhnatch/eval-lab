from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

import pytest

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
    PairedInterventionPlan,
    PairedInterventionRefusal,
    RetryReplacementPolicy,
    plan_paired_intervention,
)
from evallab.queue import PolicyGate
from evallab.schemas import ElicitationSpec, ExperimentSpec, PreregSpec

REPO_ROOT = Path(__file__).resolve().parents[1]


def _canonical_digest(value: object) -> str:
    return f"sha256:{compute_sha256(canonical_json(value))}"


def _redigest(payload: dict[str, object]) -> dict[str, object]:
    body = {key: value for key, value in payload.items() if key != "plan_digest"}
    payload["plan_digest"] = _canonical_digest(body)
    return payload


def _delta(
    repo_root: Path, *, content: bytes = b"Order evidence before conclusions.\n"
) -> ExtraInstructionDelta:
    prompt = repo_root / "interventions" / "ordering.txt"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_bytes(content)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    return ExtraInstructionDelta(
        treatment_path="interventions/ordering.txt",
        treatment_sha256=digest,
    )


def _capture(*artifacts: str) -> CaptureExpectation:
    return CaptureExpectation(
        required_artifacts=artifacts
        or (
            "trajectory/trajectory.json",
            "result.json",
            "environment-integrity.json",
        )
    )


def _analysis_gate(*, minimum_complete_pairs: int = 2) -> PairedAnalysisGate:
    analysis = create_campaign_analysis_spec(
        spec_id="track-e-ordering-paired-sign",
        method=AnalysisMethod.PAIRED_SIGN,
        outcome_feature="task_success",
        predictor_features=("arm",),
        group_by=("block_id",),
        unit=AnalysisUnit.PAIRED_SEED,
        unit_keys=("assignment_unit_id", "arm"),
        pair_keys=("pair_id",),
        denominator_policy=DenominatorPolicy.REQUIRED,
        ci_method="exact",
        minimum_informative_units=minimum_complete_pairs,
    )
    return PairedAnalysisGate(
        analysis_spec=analysis,
        minimum_complete_pairs=minimum_complete_pairs,
    )


def _spec(
    *,
    pair_id: str,
    assignment_unit_id: str,
    seed: int,
    arm: str,
    delta: ExtraInstructionDelta,
) -> ExperimentSpec:
    treatment = arm == "treatment"
    return ExperimentSpec(
        name=f"{pair_id}-{arm}",
        hypothesis="Ordering evidence changes task success without changing other execution inputs.",
        purpose="elicitation",
        question_ref="RQ-track-e-ordering",
        elicitation=ElicitationSpec(
            preamble_hash=delta.treatment_sha256 if treatment else None,
            toolset=["bash", "read"],
            env_overrides={},
        ),
        prereg=PreregSpec(
            expected="Treatment changes paired task success.",
            decision_rule="Apply the preregistered paired sign analysis to complete pairs only.",
        ),
        task="fixtures/paired-intervention-task",
        extra_instruction_path=delta.treatment_path if treatment else None,
        extra_instruction_sha256=delta.treatment_sha256 if treatment else None,
        agent="codex",
        model="gpt-5.6",
        attempts=1,
        concurrency=1,
        timeout_seconds=900,
        submitted_by="track-e-test",
        est_cost_usd=0.25,
        task_family="fixture",
        task_id="paired-intervention-task",
        task_instance_id=assignment_unit_id,
        generator_seed=seed,
        max_requests=20,
        max_input_tokens=20_000,
        max_output_tokens=10_000,
        max_total_tokens=30_000,
        cost_limit_usd=0.25,
    )


def _candidates(
    delta: ExtraInstructionDelta,
    *,
    pairs: int = 4,
) -> list[PairedArmCandidate]:
    capture = _capture()
    result: list[PairedArmCandidate] = []
    for index in range(pairs):
        pair_id = f"pair-{index + 1}"
        assignment = f"task-{index + 1}-seed-{100 + index}"
        block = f"class-{index % 2 + 1}"
        for arm in ("control", "treatment"):
            result.append(
                PairedArmCandidate(
                    pair_id=pair_id,
                    block_id=block,
                    assignment_unit_id=assignment,
                    arm=arm,
                    spec=_spec(
                        pair_id=pair_id,
                        assignment_unit_id=assignment,
                        seed=100 + index,
                        arm=arm,
                        delta=delta,
                    ),
                    capture=capture,
                )
            )
    return result


def _plan(
    repo_root: Path,
    candidates: list[PairedArmCandidate],
    delta: ExtraInstructionDelta,
):
    gate = PolicyGate(
        load_policy(REPO_ROOT / "policy" / "standing-approvals.yaml"),
        repo_root=repo_root,
    )
    return plan_paired_intervention(
        plan_id="track-e-ordering",
        randomization_seed=7349,
        candidates=candidates,
        delta=delta,
        retry_policy=RetryReplacementPolicy(),
        analysis_gate=_analysis_gate(),
        policy_gate=gate,
        repo_root=repo_root,
        spent_today_usd=0.0,
    )


def _replace_candidate(
    candidates: list[PairedArmCandidate],
    replacement: PairedArmCandidate,
) -> list[PairedArmCandidate]:
    return [
        replacement
        if candidate.pair_id == replacement.pair_id and candidate.arm == replacement.arm
        else candidate
        for candidate in candidates
    ]


def test_plan_is_deterministic_balanced_interleaved_and_approval_preserving(
    tmp_path: Path,
) -> None:
    delta = _delta(tmp_path)
    candidates = _candidates(delta)

    first = _plan(tmp_path, candidates, delta)
    second = _plan(tmp_path, list(reversed(candidates)), delta)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.plan_digest.startswith("sha256:")
    assert len(first.schedule) == 8
    assert PairedInterventionPlan.model_validate_json(first.model_dump_json()) == first
    assert Counter(entry.arm for entry in first.schedule) == {
        "control": 4,
        "treatment": 4,
    }
    pairs = [first.schedule[index : index + 2] for index in range(0, 8, 2)]
    assert all(pair[0].pair_id == pair[1].pair_id for pair in pairs)
    assert all({entry.arm for entry in pair} == {"control", "treatment"} for pair in pairs)
    assert Counter(pair[0].arm for pair in pairs) == {"control": 2, "treatment": 2}
    assert all(pairs[index][0].block_id != pairs[index + 1][0].block_id for index in range(3))

    assert first.specs == tuple(entry.spec for entry in first.schedule)
    for entry in first.schedule:
        assert isinstance(entry.spec, ExperimentSpec)
        assert entry.spec.spec_id is None
        assert entry.spec.submitted_at is None
        assert entry.spec.grid_id == first.plan_id
        assert entry.spec.grid_point is not None
        assert entry.spec.grid_point["pair_id"] == entry.pair_id
        assert entry.spec.grid_point["assignment_unit_id"] == entry.assignment_unit_id
        assert entry.policy_decision.admitted is False
        assert entry.policy_decision.reason_code == "paid_run_unauthorized"
        if entry.arm == "treatment":
            assert entry.spec.extra_instruction_path == delta.treatment_path
            assert entry.spec.extra_instruction_sha256 == delta.treatment_sha256
        else:
            assert entry.spec.extra_instruction_path is None
            assert entry.spec.extra_instruction_sha256 is None
    assert not (tmp_path / "queue").exists()


def test_missing_twin_is_refused(tmp_path: Path) -> None:
    delta = _delta(tmp_path)
    candidates = _candidates(delta)
    candidates.pop()

    with pytest.raises(PairedInterventionRefusal, match="missing_twin"):
        _plan(tmp_path, candidates, delta)


def test_duplicate_assignment_across_pairs_is_refused(tmp_path: Path) -> None:
    delta = _delta(tmp_path)
    candidates = _candidates(delta)
    duplicate_assignment = candidates[0].assignment_unit_id
    for index in (2, 3):
        candidate = candidates[index]
        duplicated_spec = candidate.spec.model_copy(
            update={"task_instance_id": duplicate_assignment}
        )
        candidates[index] = candidate.model_copy(
            update={
                "assignment_unit_id": duplicate_assignment,
                "spec": duplicated_spec,
            }
        )

    with pytest.raises(PairedInterventionRefusal, match="duplicate_assignment"):
        _plan(tmp_path, candidates, delta)


def test_simultaneous_execution_variable_change_is_refused(tmp_path: Path) -> None:
    delta = _delta(tmp_path)
    candidates = _candidates(delta)
    treatment = candidates[1]
    changed_spec = treatment.spec.model_copy(
        update={"timeout_seconds": treatment.spec.timeout_seconds + 1}
    )
    candidates = _replace_candidate(
        candidates,
        treatment.model_copy(update={"spec": changed_spec}),
    )

    with pytest.raises(PairedInterventionRefusal, match="simultaneous_variable_change"):
        _plan(tmp_path, candidates, delta)


def test_capture_asymmetry_is_refused(tmp_path: Path) -> None:
    delta = _delta(tmp_path)
    candidates = _candidates(delta)
    treatment = candidates[1]
    candidates = _replace_candidate(
        candidates,
        treatment.model_copy(
            update={"capture": _capture("trajectory/trajectory.json", "result.json")}
        ),
    )

    with pytest.raises(PairedInterventionRefusal, match="capture_asymmetry"):
        _plan(tmp_path, candidates, delta)


def test_unbound_typed_intervention_payload_is_refused(tmp_path: Path) -> None:
    delta = _delta(tmp_path)
    candidates = _candidates(delta)
    treatment = candidates[1]
    changed_spec = treatment.spec.model_copy(update={"extra_instruction_path": None})
    candidates = _replace_candidate(
        candidates,
        treatment.model_copy(update={"spec": changed_spec}),
    )

    with pytest.raises(PairedInterventionRefusal, match="unbound_intervention_payload"):
        _plan(tmp_path, candidates, delta)


def test_stale_intervention_digest_is_refused(tmp_path: Path) -> None:
    delta = _delta(tmp_path)
    candidates = _candidates(delta)
    (tmp_path / delta.treatment_path).write_bytes(b"Changed after planning.\n")

    with pytest.raises(PairedInterventionRefusal, match="intervention_digest_mismatch"):
        _plan(tmp_path, candidates, delta)


def test_symlinked_intervention_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"Outside the bound intervention path.\n")
    target = tmp_path / "interventions" / "ordering.txt"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)
    digest = f"sha256:{hashlib.sha256(outside.read_bytes()).hexdigest()}"
    delta = ExtraInstructionDelta(
        treatment_path="interventions/ordering.txt",
        treatment_sha256=digest,
    )

    with pytest.raises(PairedInterventionRefusal, match="invalid_intervention_path"):
        _plan(tmp_path, _candidates(delta), delta)


def test_absolute_intervention_path_is_refused_before_open(tmp_path: Path) -> None:
    valid_delta = _delta(tmp_path)
    with pytest.raises(ValueError, match="canonical repo-relative"):
        ExtraInstructionDelta(
            treatment_path="/etc/passwd",
            treatment_sha256=valid_delta.treatment_sha256,
        )
    bypassed_schema = ExtraInstructionDelta.model_construct(
        variable="extra_instruction",
        control_path=None,
        control_sha256=None,
        treatment_path="/etc/passwd",
        treatment_sha256=valid_delta.treatment_sha256,
    )

    with pytest.raises(PairedInterventionRefusal, match="invalid_intervention_path"):
        _plan(tmp_path, _candidates(valid_delta), bypassed_schema)


def test_rehydration_refuses_mismatched_spec_digest(tmp_path: Path) -> None:
    delta = _delta(tmp_path)
    payload = _plan(tmp_path, _candidates(delta), delta).model_dump(mode="json")
    payload["schedule"][0]["spec_digest"] = "sha256:" + "1" * 64

    with pytest.raises(ValueError, match="spec_digest_mismatch"):
        PairedInterventionPlan.model_validate(_redigest(payload))


def test_rehydration_refuses_admitted_policy_decision(tmp_path: Path) -> None:
    delta = _delta(tmp_path)
    payload = _plan(tmp_path, _candidates(delta), delta).model_dump(mode="json")
    payload["schedule"][0]["policy_decision"]["admitted"] = True
    payload["schedule"][0]["policy_decision"]["reason_code"] = None

    with pytest.raises(ValueError, match="approval_gate_not_preserved"):
        PairedInterventionPlan.model_validate(_redigest(payload))


def test_rehydration_refuses_schedule_missing_a_twin(tmp_path: Path) -> None:
    delta = _delta(tmp_path)
    payload = _plan(tmp_path, _candidates(delta), delta).model_dump(mode="json")
    del payload["schedule"][1]
    for ordinal, entry in enumerate(payload["schedule"], start=1):
        entry["ordinal"] = ordinal

    with pytest.raises(ValueError, match="missing_twin"):
        PairedInterventionPlan.model_validate(_redigest(payload))


def test_rehydration_refuses_grid_metadata_disagreement(tmp_path: Path) -> None:
    delta = _delta(tmp_path)
    payload = _plan(tmp_path, _candidates(delta), delta).model_dump(mode="json")
    payload["schedule"][0]["spec"]["grid_point"]["pair_id"] = "forged-pair"

    with pytest.raises(ValueError, match="schedule_metadata_mismatch"):
        PairedInterventionPlan.model_validate(_redigest(payload))


def test_rehydration_refuses_treatment_payload_disagreement(tmp_path: Path) -> None:
    delta = _delta(tmp_path)
    payload = _plan(tmp_path, _candidates(delta), delta).model_dump(mode="json")
    payload["delta"]["treatment_sha256"] = "sha256:" + "2" * 64

    with pytest.raises(ValueError, match="unbound_intervention_payload"):
        PairedInterventionPlan.model_validate(_redigest(payload))
