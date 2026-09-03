"""Offline behavioral coverage for the SFT signal gate (no execution, no I/O beyond tmp)."""

from __future__ import annotations

import hashlib
import json
import typing
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evallab.artifact_authority import (
    VERIFIER_IMPLEMENTATION_DIGEST,
    ArtifactRef,
    AuthorityRefusal,
    verify_artifact,
)
from evallab.sft_signal import (
    FamilyStatus,
    ReadinessStatus,
    SftCheckpointIdentityV1,
    SftExclusionCode,
    SftSignalDecisionV1,
    SftSignalInputV1,
    SftSignalObservationV1,
    SftSignalRefusalCode,
    SignalStatus,
    analyze_sft_signal,
    assess_sft_readiness,
    create_sft_signal_freeze,
    publish_sft_artifact,
)
from evallab.trainer_bundle import (
    IncompatibilityCode,
    TrainerEvaluationSetV1,
    TrainerTaskIdentityV1,
    trainer_evaluation_suite_digest,
    trainer_task_set_digest,
)
from evallab.training_result import (
    FrozenHeldOutEvaluationPlan,
    compute_cluster_key_digest,
    compute_frozen_plan_digest,
    compute_split_integrity_binding_digest,
    create_trainer_result_manifest,
)


def _digest(character: str) -> str:
    return f"sha256:{character * 64}"


COMMIT = "0123456789abcdef0123456789abcdef01234567"
BASELINE_CHECKPOINT = _digest("b")
CANDIDATE_CHECKPOINT = _digest("c")
HELDOUT_CLUSTER_KEYS = ["funcdag-heldout-key-x"]
HELDOUT_KEY_DIGEST = compute_cluster_key_digest(HELDOUT_CLUSTER_KEYS)
TRAIN_KEY_DIGEST = compute_cluster_key_digest(["funcdag-train-key-a"])
HELDOUT_SPLIT_DIGEST = _digest("6")
TRAIN_SPLIT_DIGEST = _digest("4")
MODEL_DIGEST = _digest("9")


def _evaluation_tasks() -> list[dict[str, str]]:
    return [
        {
            "task_id": "funcdag/conflict-heldout-01",
            "task_digest": _digest("e"),
            "cluster_key_digest": HELDOUT_KEY_DIGEST,
            "verifier_digest": _digest("1"),
            "environment_digest": _digest("2"),
        },
        {
            "task_id": "funcdag/permutation-heldout-02",
            "task_digest": _digest("f"),
            "cluster_key_digest": HELDOUT_KEY_DIGEST,
            "verifier_digest": _digest("3"),
            "environment_digest": _digest("4"),
        },
    ]


def _evaluation_set() -> dict[str, Any]:
    tasks = tuple(TrainerTaskIdentityV1.model_validate(task) for task in _evaluation_tasks())
    task_set_digest = trainer_task_set_digest(tasks)
    suite_name = "funcdag-heldout-core"
    return {
        "suite_name": suite_name,
        "suite_digest": trainer_evaluation_suite_digest(suite_name, task_set_digest),
        "task_set_digest": task_set_digest,
        "tasks": _evaluation_tasks(),
    }


def _evaluation_set_model() -> TrainerEvaluationSetV1:
    return TrainerEvaluationSetV1.model_validate(_evaluation_set())


def _held_out_plan() -> FrozenHeldOutEvaluationPlan:
    evaluation_set = _evaluation_set_model()
    seeded = FrozenHeldOutEvaluationPlan(
        source_result_manifest_digest=_digest("d"),
        trainer_bundle_digest=_digest("a"),
        trainer_plan_digest=_digest("2"),
        evaluation_set=evaluation_set,
        produced_checkpoint_artifact_digest=CANDIDATE_CHECKPOINT,
        heldout_split_digest=HELDOUT_SPLIT_DIGEST,
        heldout_cluster_key_digest=HELDOUT_KEY_DIGEST,
        plan_digest=_digest("0"),
    )
    payload = seeded.model_dump(mode="json")
    payload["plan_digest"] = compute_frozen_plan_digest(seeded)
    return FrozenHeldOutEvaluationPlan.model_validate(payload)


def _checkpoint(role: str, artifact_digest: str) -> SftCheckpointIdentityV1:
    return SftCheckpointIdentityV1(
        role=role,  # type: ignore[arg-type]
        model_revision="acme/solver-7b@main",
        model_digest=MODEL_DIGEST,
        checkpoint_artifact_digest=artifact_digest,
    )


def _freeze(**changes: Any) -> Any:
    kwargs: dict[str, Any] = {
        "freeze_id": "solver-sft-signal-001",
        "held_out_plan": _held_out_plan(),
        "baseline_checkpoint": _checkpoint("baseline", BASELINE_CHECKPOINT),
        "candidate_checkpoint": _checkpoint("candidate", CANDIDATE_CHECKPOINT),
        "pairing_cluster_digest": HELDOUT_KEY_DIGEST,
        "environment_identity_digest": _digest("7"),
        "runtime_identity_digest": _digest("8"),
        "metric_name": "task_reward",
        "direction": "higher",
        "minimum_effect": 0.1,
        "confidence_level": 0.9,
        "minimum_eligible_pairs": 2,
        "bootstrap_resamples": 400,
        "protected_families": ("funcdag",),
    }
    kwargs.update(changes)
    return create_sft_signal_freeze(**kwargs)


def _dataset_binding() -> dict[str, Any]:
    return {
        "dataset_manifest_digest": _digest("2"),
        "dataset_manifest_authority_digest": _digest("5"),
        "dataset_manifest_verifier_digest": _digest("6"),
        "dataset_manifest_authority_level": "bytes-verified",
        "dataset_digest": _digest("3"),
        "train_split_digest": TRAIN_SPLIT_DIGEST,
        "heldout_split_digest": HELDOUT_SPLIT_DIGEST,
        "train_cluster_key_digest": TRAIN_KEY_DIGEST,
        "heldout_cluster_key_digest": HELDOUT_KEY_DIGEST,
    }


def _non_contamination(dataset: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_ref": "evidence/cluster-separation.json",
        "evidence_digest": _digest("3"),
        "enforcement_status": "verified",
        "observed_train_split_digest": dataset["train_split_digest"],
        "observed_heldout_split_digest": dataset["heldout_split_digest"],
        "observed_train_cluster_key_digest": dataset["train_cluster_key_digest"],
        "observed_heldout_cluster_key_digest": dataset["heldout_cluster_key_digest"],
        "split_integrity_binding_digest": compute_split_integrity_binding_digest(
            train_split_digest=dataset["train_split_digest"],
            heldout_split_digest=dataset["heldout_split_digest"],
            train_cluster_key_digest=dataset["train_cluster_key_digest"],
            heldout_cluster_key_digest=dataset["heldout_cluster_key_digest"],
        ),
    }


def _manifest_data(
    *,
    produced: str,
    consumed_input: str,
    source_job: str,
    plan_digest: str,
) -> dict[str, Any]:
    dataset = _dataset_binding()
    return {
        "trainer_bundle_digest": _digest("a"),
        "source_authority_status": "copied_digest_refs_only",
        "result_manifest_path": "result-manifest.json",
        "trainer_plan_digest": plan_digest,
        "adapter_contract": "trl-sft-plan/v1",
        "backend_identity": {
            "backend_name": "generic-trl",
            "backend_version": "0.9.10",
            "backend_source_commit": COMMIT,
            "backend_image_digest": _digest("1"),
        },
        "model": {
            "model_revision": "acme/solver-7b@main",
            "model_digest": MODEL_DIGEST,
            "tokenizer_revision": "acme/solver-tokenizer@v1",
            "tokenizer_digest": _digest("a"),
            "chat_template_revision": "solver-chat@v2",
            "chat_template_digest": _digest("b"),
        },
        "input_model_checkpoint_digest": consumed_input,
        "produced_checkpoint": {"produced_checkpoint_artifact_digest": produced},
        "dataset": dataset,
        "run_identity": {
            "seed": 20260903,
            "terminal_status": "completed",
            "terminal_status_reason": "trainer emitted final checkpoint and exited cleanly",
        },
        "effective_config_digest": _digest("c"),
        "reported_metrics": [
            {
                "metric_name": "token_accuracy",
                "scope": "train",
                "estimate": 0.81,
                "uncertainty": {
                    "method": "bootstrap-95ci",
                    "lower_bound": 0.77,
                    "upper_bound": 0.85,
                },
                "sample_size": 2_400,
            }
        ],
        "training_log_artifacts": [
            {
                "artifact_ref": "artifacts/trainer-log.jsonl",
                "artifact_digest": _digest("4"),
                "media_type": "application/x-ndjson",
            }
        ],
        "checkpoint_artifacts": [
            {
                "artifact_ref": "artifacts/produced-checkpoint.safetensors",
                "artifact_digest": produced,
                "media_type": "application/x-safetensors",
            }
        ],
        "result_artifacts": [
            {
                "artifact_ref": "artifacts/checkpoint-report.json",
                "artifact_digest": _digest("5"),
                "media_type": "application/json",
            }
        ],
        "provenance": {
            "source_job_identity": source_job,
            "source_trial_identity": f"{source_job}-trial",
            "source_artifact_digest": _digest("6"),
            "runtime_receipts": {
                "platform_receipt_digest": _digest("7"),
                "isolation_receipt_digest": _digest("8"),
                "allowlist_receipt_digest": _digest("9"),
                "hardware_receipt_digest": _digest("a"),
            },
            "result_adapter_identity": "external-trainer-result-adapter/v1",
            "benchmark_family": "funcdag",
        },
        "non_contamination_evidence": [_non_contamination(dataset)],
        "exclusion_notes": ["Held-out Harbor split excluded from every training input."],
        "provenance_notes": ["External trainer result bound to its frozen expectation."],
    }


def _bytes_authority(tmp_path: Path, ref: str, content: bytes) -> Any:
    path = tmp_path / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    authority = verify_artifact(
        ArtifactRef(ref=ref, digest=f"sha256:{hashlib.sha256(content).hexdigest()}"),
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        repo_root=tmp_path,
    )
    assert not isinstance(authority, AuthorityRefusal)
    return authority

def _arm_result(
    tmp_path: Path,
    subdir: str,
    data: dict[str, Any],
) -> Any:
    from evallab.sft_signal import SftArmResultV1

    manifest = create_trainer_result_manifest(**data)
    payload = manifest.model_dump_json().encode()
    authority = _bytes_authority(tmp_path, f"{subdir}/result-manifest.json", payload)
    return SftArmResultV1(manifest=manifest, manifest_authority=authority)


def _observation(
    tmp_path: Path,
    freeze: Any,
    *,
    pair_id: str,
    arm: str,
    family: str = "funcdag",
    task_id: str | None = None,
    trial_suffix: str | None = None,
    baseline_value: float = 0.2,
    candidate_value: float = 0.6,
    capture_status: str = "complete",
    metric_name: str = "task_reward",
    freeze_digest: str | None = None,
) -> Any:
    value = baseline_value if arm == "baseline" else candidate_value
    trial_id = f"trial-{pair_id}-{trial_suffix or arm}"
    content = json.dumps({"reward": value, "trial": trial_id}).encode()
    authority = _bytes_authority(
        tmp_path, f"observations/{pair_id}-{arm}.json", content
    )
    return SftSignalObservationV1(
        freeze_digest=freeze_digest or freeze.freeze_digest,
        arm=arm,  # type: ignore[arg-type]
        pair_id=pair_id,
        task_family=family,
        task_id=task_id or f"funcdag/conflict-heldout-{pair_id}",
        task_version="v1",
        trial_id=trial_id,
        outcome_artifact_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        metric_name=metric_name,
        metric_value=value,
        capture_status=capture_status,  # type: ignore[arg-type]
        authority=authority,
    )


def _input(
    tmp_path: Path,
    freeze: Any | None = None,
    *,
    observations: tuple[Any, ...] | None = None,
    baseline_changes: dict[str, Any] | None = None,
    candidate_changes: dict[str, Any] | None = None,
) -> Any:
    freeze = freeze or _freeze()
    baseline_data = _manifest_data(
        produced=BASELINE_CHECKPOINT,
        consumed_input=_digest("a"),
        source_job="solver-baseline-run",
        plan_digest=_digest("2"),
    )
    candidate_data = _manifest_data(
        produced=CANDIDATE_CHECKPOINT,
        consumed_input=BASELINE_CHECKPOINT,
        source_job="solver-sft-run",
        plan_digest=_digest("e"),
    )
    for data, changes in ((baseline_data, baseline_changes), (candidate_data, candidate_changes)):
        if changes:
            for key, value in changes.items():
                if isinstance(value, dict) and isinstance(data.get(key), dict):
                    data[key].update(value)
                else:
                    data[key] = value
    return SftSignalInputV1(
        freeze=freeze,
        baseline_result=_arm_result(tmp_path, "baseline", baseline_data),
        candidate_result=_arm_result(tmp_path, "candidate", candidate_data),
        observations=observations if observations is not None else _default_observations(tmp_path, freeze),
        baseline_authority_repo_root=tmp_path,
        candidate_authority_repo_root=tmp_path,
        observation_authority_repo_root=tmp_path,
    )


def _default_observations(tmp_path: Path, freeze: Any) -> tuple[Any, ...]:
    """Two improving pairs in one family plus two improving pairs in a second."""

    return (
        _observation(tmp_path, freeze, pair_id="p01", arm="baseline"),
        _observation(tmp_path, freeze, pair_id="p01", arm="candidate"),
        _observation(tmp_path, freeze, pair_id="p02", arm="baseline"),
        _observation(tmp_path, freeze, pair_id="p02", arm="candidate"),
    )


def test_freeze_binds_content_and_refuses_identical_checkpoints() -> None:
    freeze = _freeze()
    assert freeze.freeze_digest
    with pytest.raises(ValidationError, match="identical_checkpoint_identities"):
        _freeze(candidate_checkpoint=_checkpoint("candidate", BASELINE_CHECKPOINT))
    tampered = freeze.model_dump(mode="json")
    tampered["minimum_effect"] = 0.01
    with pytest.raises(ValidationError, match="freeze_digest"):
        create_sft_signal_freeze(**tampered)
    pair_mismatch = freeze.model_dump(mode="json")
    pair_mismatch["pairing_cluster_digest"] = _digest("1")
    with pytest.raises(ValidationError, match="pairing cluster"):
        create_sft_signal_freeze(**pair_mismatch)


def test_readiness_ready_and_refused(tmp_path: Path) -> None:
    ready = assess_sft_readiness(_input(tmp_path))
    assert ready.status is ReadinessStatus.READY
    assert ready.reason_codes == ()
    assert ready.heldout_task_count == 2
    assert ready.ready_for_rl is False
    assert ready.readiness_digest

    baseline_data = _manifest_data(
        produced=_digest("9"),
        consumed_input=_digest("a"),
        source_job="solver-baseline-run",
        plan_digest=_digest("2"),
    )
    wrong_produced_input = _input(tmp_path)
    broken = wrong_produced_input.model_copy(
        update={
            "baseline_result": _arm_result(
                tmp_path,
                "baseline2",
                baseline_data,
            )
        }
    )
    refused = assess_sft_readiness(broken)
    assert refused.status is ReadinessStatus.REFUSED
    assert SftSignalRefusalCode.CHECKPOINT_CHAIN_MISMATCH in refused.reason_codes


def test_supported_happy_path_reports_per_family_and_no_authority(tmp_path: Path) -> None:
    decision = analyze_sft_signal(_input(tmp_path))
    assert decision.status is SignalStatus.SUPPORTED
    assert decision.reason_codes == ()
    assert decision.pair_total == 2
    assert decision.denominator_eligible_pairs == 2
    assert decision.excluded_pair_count == 0
    assert decision.ready_for_rl is False
    assert decision.authorization_scope == "none"
    assert decision.families[0].status is FamilyStatus.SUPPORTED


def test_identical_checkpoints_refuse_at_input() -> None:
    with pytest.raises(ValidationError, match="identical_checkpoint_identities"):
        _freeze(candidate_checkpoint=_checkpoint("candidate", BASELINE_CHECKPOINT))


def test_family_regression_blocks_signal(tmp_path: Path) -> None:
    freeze = _freeze()
    observations = (
        _observation(tmp_path, freeze, pair_id="p01", arm="baseline", family="funcdag"),
        _observation(
            tmp_path, freeze, pair_id="p01", arm="candidate", family="funcdag",
            candidate_value=0.7,
        ),
        _observation(tmp_path, freeze, pair_id="p02", arm="baseline", family="funcdag"),
        _observation(
            tmp_path, freeze, pair_id="p02", arm="candidate", family="funcdag",
            candidate_value=0.7,
        ),
        _observation(
            tmp_path, freeze, pair_id="p03", arm="baseline", family="otherfamily",
        ),
        _observation(
            tmp_path, freeze, pair_id="p03", arm="candidate", family="otherfamily",
            candidate_value=0.05,
        ),
        _observation(
            tmp_path, freeze, pair_id="p04", arm="baseline", family="otherfamily",
        ),
        _observation(
            tmp_path, freeze, pair_id="p04", arm="candidate", family="otherfamily",
            candidate_value=0.05,
        ),
    )
    decision = analyze_sft_signal(_input(tmp_path, observations=observations))
    assert decision.status is SignalStatus.NOT_ESTABLISHED
    assert SftSignalRefusalCode.FAMILY_REGRESSION in decision.reason_codes
    by_family = {family.family: family for family in decision.families}
    assert by_family["otherfamily"].status is FamilyStatus.REFUTED


def test_protected_family_must_be_supported(tmp_path: Path) -> None:
    freeze = _freeze(protected_families=("reviewpair",))
    observations = _default_observations(tmp_path, freeze) + (
        _observation(tmp_path, freeze, pair_id="p04", arm="baseline", family="reviewpair"),
        _observation(
            tmp_path, freeze, pair_id="p04", arm="candidate", family="reviewpair",
            candidate_value=0.25,
        ),
        _observation(tmp_path, freeze, pair_id="p05", arm="baseline", family="reviewpair"),
        _observation(
            tmp_path, freeze, pair_id="p05", arm="candidate", family="reviewpair",
            candidate_value=0.3,
        ),
    )
    decision = analyze_sft_signal(_input(tmp_path, freeze, observations=observations))
    assert decision.status is SignalStatus.NOT_ESTABLISHED
    assert (
        SftSignalRefusalCode.PROTECTED_FAMILY_NOT_SUPPORTED in decision.reason_codes
    )


def test_underpowered_family_is_not_established(tmp_path: Path) -> None:
    protected_decision = analyze_sft_signal(
        _input(tmp_path, observations=_default_observations(tmp_path, _freeze())[:2])
    )
    assert protected_decision.status is SignalStatus.NOT_ESTABLISHED
    assert (
        SftSignalRefusalCode.PROTECTED_FAMILY_UNINFORMATIVE
        in protected_decision.reason_codes
    )
    assert protected_decision.families[0].status is FamilyStatus.UNDERPOWERED

    freeze = _freeze(protected_families=())
    unprotected = analyze_sft_signal(
        _input(tmp_path, freeze=freeze, observations=_default_observations(tmp_path, freeze)[:2])
    )
    assert unprotected.status is SignalStatus.NOT_ESTABLISHED
    assert SftSignalRefusalCode.FAMILY_UNINFORMATIVE in unprotected.reason_codes


def test_missing_pair_arm_refuses(tmp_path: Path) -> None:
    observations = _default_observations(tmp_path, _freeze())[:3]
    decision = analyze_sft_signal(_input(tmp_path, observations=observations))
    assert decision.status is SignalStatus.REFUSED
    assert SftSignalRefusalCode.MISSING_PAIR_ARM in decision.reason_codes


def test_duplicate_trial_and_outcome_refuse(tmp_path: Path) -> None:
    freeze = _freeze()
    baseline_observation = _observation(tmp_path, freeze, pair_id="p01", arm="baseline")
    duplicate_trial = baseline_observation.model_copy(
        update={"arm": "candidate", "trial_id": baseline_observation.trial_id}
    )
    decision = analyze_sft_signal(
        _input(tmp_path, observations=(baseline_observation, duplicate_trial))
    )
    assert SftSignalRefusalCode.DUPLICATE_TRIAL in decision.reason_codes

    second = _observation(tmp_path, freeze, pair_id="p01", arm="baseline")
    duplicate_outcome = second.model_copy(
        update={
            "arm": "candidate",
            "outcome_artifact_digest": baseline_observation.outcome_artifact_digest,
        }
    )
    decision = analyze_sft_signal(
        _input(tmp_path, observations=(baseline_observation, duplicate_outcome))
    )
    assert SftSignalRefusalCode.DUPLICATE_OUTCOME in decision.reason_codes


def test_task_identity_mismatch_refuses(tmp_path: Path) -> None:
    freeze = _freeze()
    observations = (
        _observation(tmp_path, freeze, pair_id="p01", arm="baseline"),
        _observation(
            tmp_path,
            freeze,
            pair_id="p01",
            arm="candidate",
            task_id="funcdag/permutation-heldout-02",
        ),
    )
    decision = analyze_sft_signal(_input(tmp_path, observations=observations))
    assert decision.status is SignalStatus.REFUSED
    assert SftSignalRefusalCode.TASK_IDENTITY_MISMATCH in decision.reason_codes


def test_stale_outcome_bytes_refuse(tmp_path: Path) -> None:
    freeze = _freeze()
    observation = _observation(tmp_path, freeze, pair_id="p01", arm="baseline")
    stale_path = tmp_path / observation.authority.artifact.ref
    stale_path.write_bytes(b'{"reward": 9.9}')
    decision = analyze_sft_signal(
        _input(tmp_path, observations=(observation, observation.model_copy(
            update={"arm": "candidate", "trial_id": "trial-p01-c2"}
        )))
    )
    assert decision.status is SignalStatus.REFUSED
    assert (
        SftSignalRefusalCode.OBSERVATION_AUTHORITY_UNVERIFIED in decision.reason_codes
    )


def test_observation_digest_mismatch_refuses(tmp_path: Path) -> None:
    freeze = _freeze()
    observation = _observation(tmp_path, freeze, pair_id="p01", arm="baseline")
    mismatched = observation.model_copy(
        update={"outcome_artifact_digest": _digest("d")}
    )
    decision = analyze_sft_signal(_input(tmp_path, observations=(mismatched,)))
    assert SftSignalRefusalCode.OBSERVATION_DIGEST_MISMATCH in decision.reason_codes


def test_metric_and_freeze_mismatch_refuse(tmp_path: Path) -> None:
    freeze = _freeze()
    wrong_metric = _observation(
        tmp_path, freeze, pair_id="p01", arm="baseline", metric_name="other_metric"
    )
    decision = analyze_sft_signal(_input(tmp_path, observations=(wrong_metric,)))
    assert SftSignalRefusalCode.METRIC_MISMATCH in decision.reason_codes

    other_freeze = _freeze(freeze_id="other-freeze")
    wrong_freeze = _observation(
        tmp_path, freeze, pair_id="p01", arm="baseline", freeze_digest=other_freeze.freeze_digest
    )
    decision = analyze_sft_signal(_input(tmp_path, observations=(wrong_freeze,)))
    assert (
        SftSignalRefusalCode.OBSERVATION_FREEZE_MISMATCH in decision.reason_codes
    )


def test_no_observations_is_unavailable(tmp_path: Path) -> None:
    decision = analyze_sft_signal(_input(tmp_path, observations=()))
    assert decision.status is SignalStatus.UNAVAILABLE
    assert SftSignalRefusalCode.NO_ELIGIBLE_PAIRS in decision.reason_codes


def test_capture_incomplete_excludes_pair(tmp_path: Path) -> None:
    freeze = _freeze()
    observations = (
        _observation(tmp_path, freeze, pair_id="p01", arm="baseline"),
        _observation(
            tmp_path,
            freeze,
            pair_id="p01",
            arm="candidate",
            capture_status="corrupt",
        ),
        _observation(tmp_path, freeze, pair_id="p02", arm="baseline"),
        _observation(tmp_path, freeze, pair_id="p02", arm="candidate"),
    )
    decision = analyze_sft_signal(_input(tmp_path, observations=observations))
    assert decision.pair_total == 2
    assert decision.excluded_pair_count == 1
    assert decision.denominator_eligible_pairs == 1
    assert decision.exclusions[0].pair_id == "p01"
    assert decision.exclusions[0].reasons == (
        SftExclusionCode.CANDIDATE_CAPTURE_INCOMPLETE,
    )


def test_checkpoint_chain_mismatch_refuses(tmp_path: Path) -> None:
    swapped = _input(tmp_path)
    decision = analyze_sft_signal(swapped)
    assert decision.status is SignalStatus.SUPPORTED

    tampered_freeze = _freeze(
        baseline_checkpoint=_checkpoint("baseline", _digest("d"))
    )
    decision = analyze_sft_signal(
        _input(tmp_path, freeze=tampered_freeze)
    )
    assert decision.status is SignalStatus.REFUSED
    assert SftSignalRefusalCode.CHECKPOINT_CHAIN_MISMATCH in decision.reason_codes


def test_authority_over_different_bytes_refuses(tmp_path: Path) -> None:
    payload = _input(tmp_path)
    forged_bytes = b'{"forged": true}'
    forged = _bytes_authority(tmp_path, "baseline/result-manifest.json", forged_bytes)
    from evallab.sft_signal import SftArmResultV1

    tampered = payload.model_copy(
        update={
            "baseline_result": SftArmResultV1(
                manifest=payload.baseline_result.manifest,
                manifest_authority=forged,
            )
        }
    )
    decision = analyze_sft_signal(tampered)
    assert decision.status is SignalStatus.REFUSED
    assert SftSignalRefusalCode.RESULT_STRUCTURE_REFUSED in decision.reason_codes


def test_established_decision_cannot_unlock_rl(tmp_path: Path) -> None:
    decision = analyze_sft_signal(_input(tmp_path))
    assert decision.status is SignalStatus.SUPPORTED
    assert decision.ready_for_rl is False
    assert decision.authorization_scope == "none"
    assert typing.get_args(
        SftSignalDecisionV1.model_fields["ready_for_rl"].annotation
    ) == (False,)
    assert typing.get_args(
        SftSignalDecisionV1.model_fields["authorization_scope"].annotation
    ) == ("none",)
    assert IncompatibilityCode.SFT_SIGNAL_NOT_ESTABLISHED.value == (
        "sft_signal_not_established"
    )


def test_publish_is_no_replace(tmp_path: Path) -> None:
    decision = analyze_sft_signal(_input(tmp_path))
    destination = tmp_path / "artifact-out"
    published = publish_sft_artifact(destination, decision)
    assert (published / "artifact.json").is_file()
    restored = json.loads((published / "artifact.json").read_text())
    assert SftSignalDecisionV1.model_validate(restored) == decision
    with pytest.raises(FileExistsError):
        publish_sft_artifact(destination, decision)


def test_decision_digest_binds_content(tmp_path: Path) -> None:
    decision = analyze_sft_signal(_input(tmp_path))
    payload = decision.model_dump(mode="json")
    payload["freeze_id"] = "tampered-freeze"
    with pytest.raises(ValidationError, match="decision_digest"):
        SftSignalDecisionV1.model_validate(payload)


def test_cli_readiness_and_decide(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from evallab import cli

    manifest_path = tmp_path / "input.json"
    manifest_path.write_text(_input(tmp_path).model_dump_json())
    out_dir = tmp_path / "cli-out"

    args = cli.parser().parse_args(
        [
            "sft-signal",
            "readiness",
            "--input",
            str(manifest_path),
            "--output-dir",
            str(out_dir),
        ]
    )
    exit_code = args.func(args, tmp_path)
    assert exit_code == 0
    first_line = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert first_line["status"] == "ready"
    assert (out_dir / "artifact.json").is_file()

    args = cli.parser().parse_args(
        ["sft-signal", "decide", "--input", str(manifest_path)]
    )
    exit_code = args.func(args, tmp_path)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert payload["status"] == "supported"
