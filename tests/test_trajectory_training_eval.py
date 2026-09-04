from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from pydantic import ValidationError

from evallab.benchmark_program_contracts import compute_prefixed_sha256
from evallab.sft_signal import SftCheckpointIdentityV1
from evallab.trainer_bundle import (
    TrainerEvaluationSetV1,
    TrainerTaskIdentityV1,
    trainer_evaluation_suite_digest,
    trainer_task_set_digest,
)
from evallab.training_result import compute_cluster_key_digest
from evallab.trajectory_training_eval import (
    SftEvalPairProjectionV1,
    build_local_eval_pair_projection,
)

BASELINE_CHECKPOINT = "sha256:" + "b" * 64
CANDIDATE_CHECKPOINT = "sha256:" + "c" * 64
MODEL_DIGEST = "sha256:" + "9" * 64
SELECTION_RECIPE_DIGEST = "sha256:" + "a" * 64
HARNESS_IDENTITY_DIGEST = (
    "sha256:11eb0f5544ae92ae3a7a84e6497b720fd2d3d2ef216563ae79f97ed44ee5daac"
)
RUNTIME_IDENTITY_DIGEST = "sha256:" + "8" * 64
HELDOUT_CLUSTER_DIGEST = compute_cluster_key_digest(["funcdag-heldout-key-x"])


def _checkpoint(role: str, digest: str) -> SftCheckpointIdentityV1:
    return SftCheckpointIdentityV1.model_validate(
        {
            "role": role,
            "model_revision": "Qwen/Qwen3-0.6B@fixture",
            "model_digest": MODEL_DIGEST,
            "checkpoint_artifact_digest": digest,
        }
    )


def _evaluation_set() -> TrainerEvaluationSetV1:
    tasks = tuple(
        TrainerTaskIdentityV1.model_validate(task)
        for task in (
            {
                "task_id": "funcdag/conflict-heldout-01",
                "task_digest": "sha256:" + "e" * 64,
                "cluster_key_digest": HELDOUT_CLUSTER_DIGEST,
                "verifier_digest": "sha256:" + "1" * 64,
                "environment_digest": "sha256:" + "2" * 64,
            },
            {
                "task_id": "funcdag/permutation-heldout-02",
                "task_digest": "sha256:" + "f" * 64,
                "cluster_key_digest": HELDOUT_CLUSTER_DIGEST,
                "verifier_digest": "sha256:" + "3" * 64,
                "environment_digest": "sha256:" + "4" * 64,
            },
        )
    )
    task_set_digest = trainer_task_set_digest(tasks)
    return TrainerEvaluationSetV1(
        suite_name="funcdag-heldout-core",
        suite_digest=trainer_evaluation_suite_digest(
            "funcdag-heldout-core", task_set_digest
        ),
        task_set_digest=task_set_digest,
        tasks=tasks,
    )


def _projection() -> SftEvalPairProjectionV1:
    return build_local_eval_pair_projection(
        evaluation_set=_evaluation_set(),
        selection_recipe_digest=SELECTION_RECIPE_DIGEST,
        task_seeds={
            "funcdag/conflict-heldout-01": 17,
            "funcdag/permutation-heldout-02": 29,
        },
        baseline_checkpoint=_checkpoint("baseline", BASELINE_CHECKPOINT),
        candidate_checkpoint=_checkpoint("candidate", CANDIDATE_CHECKPOINT),
        harness_identity_digest=HARNESS_IDENTITY_DIGEST,
        runtime_identity_digest=RUNTIME_IDENTITY_DIGEST,
    )


def _design_witness() -> dict[str, Any]:
    """Regenerate the exact design witnesses recorded before this code lease."""

    evaluation_set = _evaluation_set()
    harness_identity = {
        "name": "local-contract-fixture",
        "version": "spine-6df601b1",
        "execution": "none",
    }
    pairs = []
    for task, seed in zip(evaluation_set.tasks, (17, 29), strict=True):
        pair_identity = {
            "suite_digest": evaluation_set.suite_digest,
            "task_id": task.task_id,
            "task_digest": task.task_digest,
            "seed": seed,
        }
        pairs.append(
            {
                "pair_id": "pair-"
                + str(compute_prefixed_sha256(pair_identity)).removeprefix("sha256:")[:20],
                **pair_identity,
                "cluster_key_digest": task.cluster_key_digest,
                "verifier_digest": task.verifier_digest,
                "environment_digest": task.environment_digest,
                "harness_identity_digest": compute_prefixed_sha256(harness_identity),
                "arms": ("baseline", "candidate"),
            }
        )
    baseline = _checkpoint("baseline", BASELINE_CHECKPOINT).model_dump(mode="json")
    candidate = _checkpoint("candidate", CANDIDATE_CHECKPOINT).model_dump(mode="json")
    decision_rule = {
        "metric_name": "task_success",
        "direction": "higher",
        "minimum_effect": 0.05,
        "confidence_level": 0.95,
        "minimum_eligible_pairs": 2,
        "protected_families": ["funcdag"],
    }
    pair_contract = {
        "schema_status": "nonauthoritative-design-sketch",
        "scope": "local-control-only",
        "submission_permitted": False,
        "training_data_access_permitted": False,
        "outcomes_present": False,
        "baseline_checkpoint": baseline,
        "candidate_checkpoint": candidate,
        "decision_rule": decision_rule,
        "evaluation_set": evaluation_set.model_dump(mode="json"),
        "harness_identity": harness_identity,
        "pairs": pairs,
    }
    pair_contract_digest = str(compute_prefixed_sha256(pair_contract))
    freeze = {
        "source_head": "6df601b1",
        "scope": "local-control-only",
        "evaluation_set_digest": evaluation_set.suite_digest,
        "pair_contract_digest": pair_contract_digest,
        "submission_permitted": False,
        "outcome_claim_permitted": False,
    }
    witness = {
        "heldout_cluster_digest": HELDOUT_CLUSTER_DIGEST,
        "evaluation_set": evaluation_set.model_dump(mode="json"),
        "harness_identity_digest": compute_prefixed_sha256(harness_identity),
        "baseline_checkpoint": baseline,
        "candidate_checkpoint": candidate,
        "decision_rule": decision_rule,
        "pairs": pairs,
        "pair_contract_digest": pair_contract_digest,
        "freeze_digest": str(compute_prefixed_sha256(freeze)),
    }
    output = json.dumps(witness, sort_keys=True, indent=2) + "\n"
    witness["output_sha256"] = hashlib.sha256(output.encode()).hexdigest()
    return witness


def test_local_pair_projection_is_deterministic_exact_and_non_submittable() -> None:
    first = _projection()
    second = _projection()

    assert first == second
    assert first.scope == "local-control-only"
    assert first.submission_permitted is False
    assert first.scientific_claim_permitted is False
    assert first.outcomes_present is False
    assert tuple(pair.pair_ordinal for pair in first.pairs) == (1, 2)
    assert tuple(pair.task for pair in first.pairs) == first.evaluation_set.tasks
    assert tuple(pair.generator_seed for pair in first.pairs) == (17, 29)
    assert all(pair.arms == ("baseline", "candidate") for pair in first.pairs)
    assert all(
        pair.selection_recipe_digest == SELECTION_RECIPE_DIGEST for pair in first.pairs
    )
    assert first.pair_set_digest
    assert first.projection_digest


def test_design_digest_witnesses_regenerate_exactly() -> None:
    first = _design_witness()
    second = _design_witness()

    assert first == second
    assert (
        first["pair_contract_digest"]
        == "sha256:37db3a34f154b2fc5e58aba1e61db0d21f39d189e7be3f460f7e4b86cef35e69"
    )
    assert (
        first["freeze_digest"]
        == "sha256:f44f26529ebdb248925fcde7017a64d2b716420211efcee2c79b6234354b3c65"
    )
    assert first["output_sha256"] == (
        "83903a33449459a2aa17a7b2097f90a1391e90f0f9b873fbca6a81b41d63d4b7"
    )


def test_projection_refuses_missing_or_aliased_pair_membership() -> None:
    evaluation_set = _evaluation_set()
    common = {
        "evaluation_set": evaluation_set,
        "selection_recipe_digest": SELECTION_RECIPE_DIGEST,
        "baseline_checkpoint": _checkpoint("baseline", BASELINE_CHECKPOINT),
        "candidate_checkpoint": _checkpoint("candidate", CANDIDATE_CHECKPOINT),
        "harness_identity_digest": HARNESS_IDENTITY_DIGEST,
        "runtime_identity_digest": RUNTIME_IDENTITY_DIGEST,
    }
    with pytest.raises(ValueError, match="exactly cover"):
        build_local_eval_pair_projection(
            **common,
            task_seeds={"funcdag/conflict-heldout-01": 17},
        )
    with pytest.raises(ValueError, match="exactly cover"):
        build_local_eval_pair_projection(
            **common,
            task_seeds={
                "funcdag/conflict-heldout-01": 17,
                "funcdag/permutation-heldout-02": 29,
                "funcdag/unfrozen": 31,
            },
        )


def test_projection_refuses_checkpoint_or_identity_drift() -> None:
    with pytest.raises(ValueError, match="checkpoints must differ"):
        build_local_eval_pair_projection(
            evaluation_set=_evaluation_set(),
            selection_recipe_digest=SELECTION_RECIPE_DIGEST,
            task_seeds={
                "funcdag/conflict-heldout-01": 17,
                "funcdag/permutation-heldout-02": 29,
            },
            baseline_checkpoint=_checkpoint("baseline", BASELINE_CHECKPOINT),
            candidate_checkpoint=_checkpoint("candidate", BASELINE_CHECKPOINT),
            harness_identity_digest=HARNESS_IDENTITY_DIGEST,
            runtime_identity_digest=RUNTIME_IDENTITY_DIGEST,
        )

    payload = _projection().model_dump(mode="json")
    payload["pairs"][0]["generator_seed"] = 31
    with pytest.raises(ValidationError, match="pair_id does not match"):
        SftEvalPairProjectionV1.model_validate(payload)


def test_projection_shape_cannot_carry_outcomes_or_permission() -> None:
    permission_payload = _projection().model_dump(mode="json")
    permission_payload["submission_permitted"] = True
    with pytest.raises(ValidationError):
        SftEvalPairProjectionV1.model_validate(permission_payload)

    outcome_payload = _projection().model_dump(mode="json")
    outcome_payload["outcomes"] = [{"metric_value": 1.0}]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SftEvalPairProjectionV1.model_validate(outcome_payload)
