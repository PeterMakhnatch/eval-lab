"""Offline fixture coverage for the v3 external trainer-result manifest."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from evallab.artifact_authority import (
    VERIFIER_IMPLEMENTATION_DIGEST,
    ArtifactAuthority,
    ArtifactRef,
    AuthorityRefusal,
    verify_artifact,
)
from evallab.trainer_bundle import (
    ExpectedTrainerResultV1,
    TrainerEvaluationSetV1,
    TrainerTaskIdentityV1,
    trainer_evaluation_suite_digest,
    trainer_task_set_digest,
)
from evallab.training_result import (
    TrainerResultManifestRefused,
    TrainerResultRefusalCode,
    compute_cluster_key_digest,
    compute_frozen_plan_digest,
    compute_split_integrity_binding_digest,
    create_trainer_result_manifest,
    rehydrate_frozen_held_out_evaluation_plan,
    render_frozen_held_out_evaluation_plan,
    validate_trainer_result_manifest,
)


def _digest(character: str) -> str:
    return f"sha256:{character * 64}"


COMMIT = "0123456789abcdef0123456789abcdef01234567"
TRAIN_CLUSTER_KEYS = ["funcdag-train-key-a", "funcdag-train-key-b"]
HELDOUT_CLUSTER_KEYS = ["funcdag-heldout-key-x"]
HELDOUT_KEY_DIGEST = compute_cluster_key_digest(HELDOUT_CLUSTER_KEYS)


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


def _expected_data() -> dict[str, Any]:
    return {
        "schema_version": "expected-trainer-result/v1",
        "result_schema": "trainer-result-manifest-v1",
        "source_authority_status": "copied_digest_refs_only",
        "result_manifest_path": "research/evidence/runs/external-001/result-manifest.json",
        "trainer_bundle_digest": _digest("a"),
        "trainer_plan_digest": _digest("b"),
        "backend_name": "generic-trl",
        "backend_version": "0.9.10",
        "backend_source_commit": COMMIT,
        "backend_image_digest": _digest("1"),
        "adapter_contract": "trl-sft-plan/v1",
        "dataset_manifest_digest": _digest("2"),
        "dataset_manifest_authority_digest": _digest("5"),
        "dataset_manifest_verifier_digest": _digest("6"),
        "dataset_manifest_authority_level": "bytes-verified",
        "dataset_digest": _digest("3"),
        "train_split_digest": _digest("4"),
        "train_cluster_key_digest": compute_cluster_key_digest(TRAIN_CLUSTER_KEYS),
        "heldout_split_digest": _digest("6"),
        "heldout_cluster_key_digest": compute_cluster_key_digest(HELDOUT_CLUSTER_KEYS),
        "input_model_checkpoint_digest": _digest("8"),
        "model_revision": "acme/solver-7b@main",
        "model_digest": _digest("9"),
        "tokenizer_revision": "acme/solver-tokenizer@v1",
        "tokenizer_digest": _digest("a"),
        "chat_template_revision": "solver-chat@v2",
        "chat_template_digest": _digest("b"),
        "effective_config_digest": _digest("c"),
        "evaluation_set": _evaluation_set(),
    }


def _expected() -> ExpectedTrainerResultV1:
    return ExpectedTrainerResultV1.model_validate(_expected_data())


def _manifest_data() -> dict[str, Any]:
    dataset = {
        "dataset_manifest_digest": _digest("2"),
        "dataset_manifest_authority_digest": _digest("5"),
        "dataset_manifest_verifier_digest": _digest("6"),
        "dataset_manifest_authority_level": "bytes-verified",
        "dataset_digest": _digest("3"),
        "train_split_digest": _digest("4"),
        "heldout_split_digest": _digest("6"),
        "train_cluster_key_digest": compute_cluster_key_digest(TRAIN_CLUSTER_KEYS),
        "heldout_cluster_key_digest": compute_cluster_key_digest(HELDOUT_CLUSTER_KEYS),
    }
    evidence = {
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
    return {
        "trainer_bundle_digest": _digest("a"),
        "source_authority_status": "copied_digest_refs_only",
        "result_manifest_path": "research/evidence/runs/external-001/result-manifest.json",
        "trainer_plan_digest": _digest("b"),
        "adapter_contract": "trl-sft-plan/v1",
        "backend_identity": {
            "backend_name": "generic-trl",
            "backend_version": "0.9.10",
            "backend_source_commit": COMMIT,
            "backend_image_digest": _digest("1"),
        },
        "model": {
            "model_revision": "acme/solver-7b@main",
            "model_digest": _digest("9"),
            "tokenizer_revision": "acme/solver-tokenizer@v1",
            "tokenizer_digest": _digest("a"),
            "chat_template_revision": "solver-chat@v2",
            "chat_template_digest": _digest("b"),
        },
        "input_model_checkpoint_digest": _digest("8"),
        "produced_checkpoint": {"produced_checkpoint_artifact_digest": _digest("0")},
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
                "artifact_digest": _digest("0"),
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
            "source_job_identity": "external-trainer-job-20260903-001",
            "source_trial_identity": "external-trainer-trial-20260903-001",
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
        "non_contamination_evidence": [evidence],
        "exclusion_notes": ["Held-out Harbor split excluded from every training input."],
        "provenance_notes": [
            "External trainer result bound to the immutable Track D expectation."
        ],
    }


def _manifest_payload() -> dict[str, Any]:
    return create_trainer_result_manifest(**_manifest_data()).model_dump(mode="json")


def _validate(
    payload: dict[str, Any],
    expected: ExpectedTrainerResultV1 | None = None,
    *,
    dataset_manifest_authority: ArtifactAuthority | None = None,
    result_manifest_authority: ArtifactAuthority | None = None,
    dataset_authority_repo_root: Any = None,
    result_authority_repo_root: Any = None,
) -> Any:
    return validate_trainer_result_manifest(
        payload,
        expected=expected or _expected(),
        dataset_manifest_authority=dataset_manifest_authority,
        result_manifest_authority=result_manifest_authority,
        dataset_authority_repo_root=dataset_authority_repo_root,
        result_authority_repo_root=result_authority_repo_root,
    )


def _frozen_tasks() -> list[TrainerTaskIdentityV1]:
    return [TrainerTaskIdentityV1.model_validate(task) for task in _evaluation_set()["tasks"]]


def _render(
    manifest: Any,
    expected: ExpectedTrainerResultV1,
    authority: ArtifactAuthority | None,
    result_authority: ArtifactAuthority | None,
    repo_root: Any,
) -> Any:
    return render_frozen_held_out_evaluation_plan(
        manifest,
        expected=expected,
        frozen_tasks=_frozen_tasks(),
        trusted_heldout_cluster_keys=HELDOUT_CLUSTER_KEYS,
        dataset_manifest_authority=authority,
        result_manifest_authority=result_authority,
        dataset_authority_repo_root=repo_root,
        result_authority_repo_root=repo_root,
    )


def _authorizing_context(tmp_path: Any) -> tuple[ExpectedTrainerResultV1, dict[str, Any], ArtifactAuthority]:
    raw_manifest = b'{"dataset":"authoritative"}'
    dataset_path = tmp_path / "dataset-manifest.json"
    dataset_path.write_bytes(raw_manifest)
    digest = f"sha256:{hashlib.sha256(raw_manifest).hexdigest()}"
    authority = verify_artifact(
        ArtifactRef(ref="dataset-manifest.json", digest=digest),
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        repo_root=tmp_path,
    )
    assert not isinstance(authority, AuthorityRefusal)
    expected_data = _expected_data()
    expected_data.update(
        {
            "dataset_manifest_digest": digest,
            "dataset_manifest_authority_digest": authority.authority_digest,
            "dataset_manifest_verifier_digest": VERIFIER_IMPLEMENTATION_DIGEST,
        }
    )
    manifest_data = _manifest_data()
    manifest_data["dataset"].update(
        {
            "dataset_manifest_digest": digest,
            "dataset_manifest_authority_digest": authority.authority_digest,
            "dataset_manifest_verifier_digest": VERIFIER_IMPLEMENTATION_DIGEST,
        }
    )
    return ExpectedTrainerResultV1.model_validate(expected_data), manifest_data, authority


def _result_manifest_authority(
    tmp_path: Any,
    manifest: Any,
    *,
    artifact_ref: str | None = None,
    raw_bytes: bytes | None = None,
) -> ArtifactAuthority:
    ref = artifact_ref or manifest.result_manifest_path
    path = tmp_path / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    content = raw_bytes if raw_bytes is not None else manifest.model_dump_json().encode()
    path.write_bytes(content)
    authority = verify_artifact(
        ArtifactRef(ref=ref, digest=f"sha256:{hashlib.sha256(content).hexdigest()}"),
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        repo_root=tmp_path,
    )
    assert not isinstance(authority, AuthorityRefusal)
    return authority


def test_track_d_hardened_schema_parity_and_instance() -> None:
    expected = _expected()
    assert isinstance(expected, ExpectedTrainerResultV1)
    assert expected.source_authority_status == "copied_digest_refs_only"
    assert expected.dataset_manifest_authority_level == "bytes-verified"
    assert expected.input_model_checkpoint_digest == _digest("8")
    assert [task.task_id for task in expected.evaluation_set.tasks] == sorted(
        task["task_id"] for task in _evaluation_set()["tasks"]
    )


def test_real_result_requires_reverified_authority_before_handoff(tmp_path: Any) -> None:
    structural_manifest = create_trainer_result_manifest(**_manifest_data())
    structural_validation = _validate(structural_manifest.model_dump(mode="json"))
    assert structural_validation.status == "valid"
    assert structural_validation.eligible_for_held_out_handoff is False
    with pytest.raises(TrainerResultManifestRefused) as structural_exc:
        _render(structural_manifest, _expected(), None, None, None)
    assert TrainerResultRefusalCode.AUTHORITY_NOT_REVERIFIED in structural_exc.value.reason_codes

    expected, manifest_data, authority = _authorizing_context(tmp_path)
    manifest = create_trainer_result_manifest(**manifest_data)
    result_authority = _result_manifest_authority(tmp_path, manifest)
    validation = _validate(
        manifest.model_dump(mode="json"),
        expected,
        dataset_manifest_authority=authority,
        result_manifest_authority=result_authority,
        dataset_authority_repo_root=tmp_path,
        result_authority_repo_root=tmp_path,
    )
    assert validation.status == "valid"
    assert validation.eligible_for_held_out_handoff is True

    plan = _render(manifest, expected, authority, result_authority, tmp_path)
    assert plan.execution_mode == "proposed-handoff"
    assert plan.submission_permitted is False
    assert plan.verify_plan_digest()
    assert plan.evaluation_set.task_set_digest == _evaluation_set()["task_set_digest"]

    view = rehydrate_frozen_held_out_evaluation_plan(
        plan,
        manifest=manifest,
        expected=expected,
        dataset_manifest_authority=authority,
        result_manifest_authority=result_authority,
        dataset_authority_repo_root=tmp_path,
        result_authority_repo_root=tmp_path,
    )
    assert view.verify_view_digest()
    assert view.produced_checkpoint_artifact_digest == _digest("0")
    assert view.model_revision == "acme/solver-7b@main"




def test_result_manifest_authority_requires_exact_current_bytes(tmp_path: Any) -> None:
    expected, manifest_data, dataset_authority = _authorizing_context(tmp_path)
    manifest = create_trainer_result_manifest(**manifest_data)

    structural = _validate(
        manifest.model_dump(mode="json"),
        expected,
        dataset_manifest_authority=dataset_authority,
        dataset_authority_repo_root=tmp_path,
        result_authority_repo_root=tmp_path,
    )
    assert structural.status == "valid"
    assert structural.eligible_for_held_out_handoff is False

    stale_authority = _result_manifest_authority(tmp_path, manifest)
    (tmp_path / manifest.result_manifest_path).write_bytes(b'{"stale":true}')
    stale = _validate(
        manifest.model_dump(mode="json"),
        expected,
        dataset_manifest_authority=dataset_authority,
        result_manifest_authority=stale_authority,
        dataset_authority_repo_root=tmp_path,
        result_authority_repo_root=tmp_path,
    )
    assert TrainerResultRefusalCode.AUTHORITY_NOT_REVERIFIED in stale.reason_codes

    ref_mismatch_authority = _result_manifest_authority(
        tmp_path, manifest, artifact_ref="other/result-manifest.json"
    )
    ref_mismatch = _validate(
        manifest.model_dump(mode="json"),
        expected,
        dataset_manifest_authority=dataset_authority,
        result_manifest_authority=ref_mismatch_authority,
        dataset_authority_repo_root=tmp_path,
        result_authority_repo_root=tmp_path,
    )
    assert TrainerResultRefusalCode.AUTHORITY_NOT_REVERIFIED in ref_mismatch.reason_codes

    different = create_trainer_result_manifest(
        **{**manifest_data, "provenance_notes": ["A different but valid result record."]}
    )
    different_authority = _result_manifest_authority(tmp_path, different)
    semantic_mismatch = _validate(
        manifest.model_dump(mode="json"),
        expected,
        dataset_manifest_authority=dataset_authority,
        result_manifest_authority=different_authority,
        dataset_authority_repo_root=tmp_path,
        result_authority_repo_root=tmp_path,
    )
    assert TrainerResultRefusalCode.AUTHORITY_NOT_REVERIFIED in semantic_mismatch.reason_codes

def test_conditional_failed_run_records_pre_checkpoint_failure(tmp_path: Any) -> None:
    expected, data, authority = _authorizing_context(tmp_path)
    data["run_identity"] = {
        "seed": 20260903,
        "terminal_status": "failed",
        "terminal_status_reason": "trainer process exited non-zero before first checkpoint",
    }
    for conditional in (
        "reported_metrics",
        "checkpoint_artifacts",
        "result_artifacts",
        "training_log_artifacts",
        "non_contamination_evidence",
    ):
        data[conditional] = []
    data["produced_checkpoint"] = None
    data["provenance"]["runtime_receipts"] = None
    failed = create_trainer_result_manifest(**data)
    result_authority = _result_manifest_authority(tmp_path, failed)

    validation = _validate(
        failed.model_dump(mode="json"),
        expected,
        dataset_manifest_authority=authority,
        result_manifest_authority=result_authority,
        dataset_authority_repo_root=tmp_path,
        result_authority_repo_root=tmp_path,
    )
    assert validation.status == "valid"
    assert validation.eligible_for_held_out_handoff is False
    with pytest.raises(TrainerResultManifestRefused) as exc:
        _render(failed, expected, authority, result_authority, tmp_path)
    assert TrainerResultRefusalCode.TERMINAL_STATUS_NOT_COMPLETED in exc.value.reason_codes


def test_completed_run_conditional_requirements_refuse() -> None:
    cases = (
        ({"reported_metrics": []}, TrainerResultRefusalCode.COMPLETED_RUN_REQUIRES_METRICS),
        (
            {"produced_checkpoint": None, "checkpoint_artifacts": []},
            TrainerResultRefusalCode.COMPLETED_RUN_REQUIRES_CHECKPOINT,
        ),
        (
            {"provenance": {**_manifest_payload()["provenance"], "runtime_receipts": None}},
            TrainerResultRefusalCode.COMPLETED_RUN_REQUIRES_RECEIPTS,
        ),
        (
            {"result_artifacts": [], "training_log_artifacts": []},
            TrainerResultRefusalCode.COMPLETED_RUN_REQUIRES_ARTIFACTS,
        ),
        (
            {"non_contamination_evidence": []},
            TrainerResultRefusalCode.MISSING_NON_CONTAMINATION_EVIDENCE,
        ),
    )
    for changes, code in cases:
        payload = _manifest_payload()
        payload.update(changes)
        validation = _validate(payload)
        assert validation.status == "refused", code
        assert code in validation.reason_codes, code


def test_input_checkpoint_binds_exactly_and_is_never_output() -> None:
    payload = _manifest_payload()
    payload["input_model_checkpoint_digest"] = _digest("1")

    validation = _validate(payload)

    assert validation.status == "refused"
    assert TrainerResultRefusalCode.INPUT_CHECKPOINT_MISMATCH in validation.reason_codes


def test_produced_checkpoint_must_be_declared_artifact() -> None:
    payload = _manifest_payload()
    payload["produced_checkpoint"]["produced_checkpoint_artifact_digest"] = _digest("9")

    validation = _validate(payload)

    assert validation.status == "refused"
    assert TrainerResultRefusalCode.CHECKPOINT_BINDING_MISMATCH in validation.reason_codes


def test_strict_projection_compares_every_identity_field() -> None:
    expected = _expected()

    backend_data = _manifest_data()
    backend_data["backend_identity"]["backend_version"] = "0.9.11"
    backend_mismatch = _validate(
        create_trainer_result_manifest(**backend_data).model_dump(mode="json"), expected
    )

    tokenizer_data = _manifest_data()
    tokenizer_data["model"]["tokenizer_revision"] = "acme/solver-tokenizer@v2"
    tokenizer_mismatch = _validate(
        create_trainer_result_manifest(**tokenizer_data).model_dump(mode="json"), expected
    )

    config_data = _manifest_data()
    config_data["effective_config_digest"] = _digest("d")
    config_mismatch = _validate(
        create_trainer_result_manifest(**config_data).model_dump(mode="json"), expected
    )

    for validation in (backend_mismatch, tokenizer_mismatch, config_mismatch):
        assert validation.status == "refused"
        assert TrainerResultRefusalCode.EXPECTED_PROJECTION_MISMATCH in validation.reason_codes


def test_unsupported_backends_and_adapter_contracts_refuse() -> None:
    for backend in ("agent-lightning", "verl"):
        payload = _manifest_payload()
        payload["backend_identity"]["backend_name"] = backend
        validation = _validate(payload)
        assert TrainerResultRefusalCode.UNSUPPORTED_TRAINING_BACKEND in validation.reason_codes

    bad_adapter = _manifest_payload()
    bad_adapter["adapter_contract"] = "agent-lightning-plan/v1"
    validation = _validate(bad_adapter)
    assert TrainerResultRefusalCode.UNSUPPORTED_ADAPTER_CONTRACT in validation.reason_codes


def test_split_and_cluster_parity_against_trusted_projection() -> None:
    split_expected = {**_expected_data(), "heldout_split_digest": _digest("7")}
    validation = _validate(
        _manifest_payload(), ExpectedTrainerResultV1.model_validate(split_expected)
    )
    assert TrainerResultRefusalCode.SPLIT_PARITY_MISMATCH in validation.reason_codes

    cluster_expected = {**_expected_data(), "heldout_cluster_key_digest": _digest("7")}
    cluster_validation = _validate(
        _manifest_payload(), ExpectedTrainerResultV1.model_validate(cluster_expected)
    )
    assert TrainerResultRefusalCode.SPLIT_PARITY_MISMATCH in cluster_validation.reason_codes


def test_raw_split_tuple_and_raw_cluster_keys_are_unknown_fields() -> None:
    payload = _manifest_payload()
    payload["dataset"]["train_input_split_digests"] = [_digest("4")]
    validation = _validate(payload)
    assert TrainerResultRefusalCode.UNKNOWN_FIELD in validation.reason_codes

    keys_payload = _manifest_payload()
    keys_payload["dataset"]["train_cluster_keys"] = ["funcdag-train-key-a"]
    keys_validation = _validate(keys_payload)
    assert TrainerResultRefusalCode.UNKNOWN_FIELD in keys_validation.reason_codes


def test_split_integrity_and_cluster_overlap_refuse() -> None:
    same_split = _manifest_payload()
    same_split["dataset"]["heldout_split_digest"] = _digest("4")
    assert (
        TrainerResultRefusalCode.HELD_OUT_SPLIT_IN_TRAIN_INPUTS
        in _validate(same_split).reason_codes
    )

    same_cluster = _manifest_payload()
    same_cluster["dataset"]["heldout_cluster_key_digest"] = compute_cluster_key_digest(
        TRAIN_CLUSTER_KEYS
    )
    assert TrainerResultRefusalCode.CLUSTER_KEY_OVERLAP in _validate(same_cluster).reason_codes


def test_evaluation_set_is_self_bound() -> None:
    tampered = _evaluation_set()
    tampered["task_set_digest"] = _digest("9")
    with pytest.raises(Exception, match="task_set_digest mismatch"):
        TrainerEvaluationSetV1.model_validate(tampered)


def test_forged_plan_mutations_refuse_even_with_recomputed_digest(tmp_path: Any) -> None:
    expected, manifest_data, authority = _authorizing_context(tmp_path)
    manifest = create_trainer_result_manifest(**manifest_data)
    result_authority = _result_manifest_authority(tmp_path, manifest)
    plan = _render(manifest, expected, authority, result_authority, tmp_path)

    tasks = list(plan.evaluation_set.tasks)
    evil = tasks[0].model_copy(update={"task_digest": _digest("9")})
    task_tampered = plan.model_copy(
        update={"evaluation_set": plan.evaluation_set.model_copy(update={"tasks": [evil, *tasks[1:]]})}
    )
    recomputed = task_tampered.model_copy(
        update={"plan_digest": compute_frozen_plan_digest(task_tampered)}
    )
    with pytest.raises(TrainerResultManifestRefused) as exc:
        rehydrate_frozen_held_out_evaluation_plan(
            recomputed,
            manifest=manifest,
            expected=expected,
            dataset_manifest_authority=authority,
            result_manifest_authority=result_authority,
            dataset_authority_repo_root=tmp_path,
            result_authority_repo_root=tmp_path,
        )
    assert TrainerResultRefusalCode.FROZEN_TASK_SET_MISMATCH in exc.value.reason_codes
    assert recomputed.verify_plan_digest()


def test_rehydrate_cross_checks_manifest_and_typed_expectation(tmp_path: Any) -> None:
    expected, manifest_data, authority = _authorizing_context(tmp_path)
    manifest = create_trainer_result_manifest(**manifest_data)
    result_authority = _result_manifest_authority(tmp_path, manifest)
    plan = _render(manifest, expected, authority, result_authority, tmp_path)

    other = create_trainer_result_manifest(
        **{**manifest_data, "trainer_bundle_digest": _digest("1")}
    )
    with pytest.raises(TrainerResultManifestRefused) as bundle_exc:
        rehydrate_frozen_held_out_evaluation_plan(
            plan,
            manifest=other,
            expected=expected,
            dataset_manifest_authority=authority,
            result_manifest_authority=result_authority,
            dataset_authority_repo_root=tmp_path,
            result_authority_repo_root=tmp_path,
        )
    assert TrainerResultRefusalCode.TRAINER_BUNDLE_DIGEST_MISMATCH in bundle_exc.value.reason_codes

    mutated_expected_data = expected.model_dump(mode="json")
    mutated_set = _evaluation_set()
    mutated_set["tasks"][0]["task_digest"] = _digest("9")
    mutated_tasks = tuple(
        TrainerTaskIdentityV1.model_validate(task) for task in mutated_set["tasks"]
    )
    mutated_set["task_set_digest"] = trainer_task_set_digest(mutated_tasks)
    mutated_set["suite_digest"] = trainer_evaluation_suite_digest(
        mutated_set["suite_name"], mutated_set["task_set_digest"]
    )
    mutated_expected_data["evaluation_set"] = mutated_set
    with pytest.raises(TrainerResultManifestRefused) as set_exc:
        rehydrate_frozen_held_out_evaluation_plan(
            plan,
            manifest=manifest,
            expected=ExpectedTrainerResultV1.model_validate(mutated_expected_data),
            dataset_manifest_authority=authority,
            result_manifest_authority=result_authority,
            dataset_authority_repo_root=tmp_path,
            result_authority_repo_root=tmp_path,
        )
    assert TrainerResultRefusalCode.FROZEN_TASK_SET_MISMATCH in set_exc.value.reason_codes


def test_renderer_gates_arbitrary_tasks_and_cluster_keys(tmp_path: Any) -> None:
    expected, manifest_data, authority = _authorizing_context(tmp_path)
    manifest = create_trainer_result_manifest(**manifest_data)
    result_authority = _result_manifest_authority(tmp_path, manifest)

    outsider = TrainerTaskIdentityV1(
        task_id="funcdag/not-in-set-99",
        task_digest=_digest("9"),
        cluster_key_digest=HELDOUT_KEY_DIGEST,
        verifier_digest=_digest("1"),
        environment_digest=_digest("2"),
    )
    with pytest.raises(TrainerResultManifestRefused) as outsider_exc:
        render_frozen_held_out_evaluation_plan(
            manifest,
            expected=expected,
            frozen_tasks=[outsider],
            trusted_heldout_cluster_keys=HELDOUT_CLUSTER_KEYS,
            dataset_manifest_authority=authority,
            result_manifest_authority=result_authority,
            dataset_authority_repo_root=tmp_path,
            result_authority_repo_root=tmp_path,
        )
    assert TrainerResultRefusalCode.FROZEN_TASK_SET_MISMATCH in outsider_exc.value.reason_codes

    with pytest.raises(TrainerResultManifestRefused) as trusted_exc:
        render_frozen_held_out_evaluation_plan(
            manifest,
            expected=expected,
            frozen_tasks=_frozen_tasks(),
            trusted_heldout_cluster_keys=["funcdag-heldout-key-wrong"],
            dataset_manifest_authority=authority,
            result_manifest_authority=result_authority,
            dataset_authority_repo_root=tmp_path,
            result_authority_repo_root=tmp_path,
        )
    assert TrainerResultRefusalCode.FROZEN_TASK_CLUSTER_MISMATCH in trusted_exc.value.reason_codes



def test_raw_manifest_refusals_are_complete_and_canonically_ordered() -> None:
    payload = _manifest_payload()
    payload["untrusted_trainer_field"] = True
    payload["trainer_bundle_digest"] = "sha256:x"
    payload["backend_identity"]["backend_name"] = "agent-lightning"
    payload["backend_identity"]["backend_source_commit"] = "zz123"
    payload["adapter_contract"] = "agent-lightning-plan/v1"
    payload["reported_metrics"] = []

    validation = _validate(payload)

    assert validation.status == "refused"
    assert validation.reason_codes == (
        TrainerResultRefusalCode.UNKNOWN_FIELD,
        TrainerResultRefusalCode.INVALID_DIGEST,
        TrainerResultRefusalCode.INVALID_COMMIT,
        TrainerResultRefusalCode.UNSUPPORTED_TRAINING_BACKEND,
        TrainerResultRefusalCode.UNSUPPORTED_ADAPTER_CONTRACT,
        TrainerResultRefusalCode.COMPLETED_RUN_REQUIRES_METRICS,
    )

def test_evidence_and_artifact_ref_path_traversal_refuse() -> None:
    evidence_payload = _manifest_payload()
    evidence_payload["non_contamination_evidence"][0]["evidence_ref"] = "../outside/evidence.json"
    evidence_validation = _validate(evidence_payload)
    assert TrainerResultRefusalCode.UNSAFE_ARTIFACT_REF in evidence_validation.reason_codes

    artifact_payload = _manifest_payload()
    artifact_payload["result_artifacts"][0]["artifact_ref"] = "../outside/report.json"
    artifact_validation = _validate(artifact_payload)
    assert TrainerResultRefusalCode.UNSAFE_ARTIFACT_REF in artifact_validation.reason_codes


def test_canonical_sets_sort_and_dedupe() -> None:
    data = _manifest_data()
    data["exclusion_notes"] = ["zebra note", "alpha note", "zebra note"]
    data["reported_metrics"] = [
        {
            "metric_name": "zeta_loss",
            "estimate": 0.5,
            "uncertainty": {"method": "bootstrap-95ci", "lower_bound": 0.4, "upper_bound": 0.6},
            "sample_size": 100,
        },
        {
            "metric_name": "token_accuracy",
            "estimate": 0.81,
            "uncertainty": {"method": "bootstrap-95ci", "lower_bound": 0.77, "upper_bound": 0.85},
            "sample_size": 2_400,
        },
    ]
    data["result_artifacts"] = [
        {
            "artifact_ref": "artifacts/z-report.json",
            "artifact_digest": _digest("5"),
            "media_type": "application/json",
        },
        {
            "artifact_ref": "artifacts/a-report.json",
            "artifact_digest": _digest("6"),
            "media_type": "application/json",
        },
    ]
    manifest = create_trainer_result_manifest(**data)
    payload = manifest.model_dump(mode="json")

    assert payload["exclusion_notes"] == ["alpha note", "zebra note"]
    assert [m["metric_name"] for m in payload["reported_metrics"]] == [
        "token_accuracy",
        "zeta_loss",
    ]
    assert [a["artifact_ref"] for a in payload["result_artifacts"]] == [
        "artifacts/a-report.json",
        "artifacts/z-report.json",
    ]
    assert _validate(payload).status == "valid"


def test_remaining_typed_refusals() -> None:
    no_uncertainty = _manifest_payload()
    del no_uncertainty["reported_metrics"][0]["uncertainty"]
    assert TrainerResultRefusalCode.MISSING_UNCERTAINTY in _validate(no_uncertainty).reason_codes

    unknown = _manifest_payload()
    unknown["untrusted_trainer_field"] = "not admitted"
    assert TrainerResultRefusalCode.UNKNOWN_FIELD in _validate(unknown).reason_codes

    missing = _manifest_payload()
    del missing["effective_config_digest"]
    assert TrainerResultRefusalCode.MISSING_REQUIRED_FIELD in _validate(missing).reason_codes

    bad_digest = _manifest_payload()
    bad_digest["trainer_bundle_digest"] = "sha256:x"
    assert TrainerResultRefusalCode.INVALID_DIGEST in _validate(bad_digest).reason_codes

    bad_commit = _manifest_payload()
    bad_commit["backend_identity"]["backend_source_commit"] = "zz123"
    assert TrainerResultRefusalCode.INVALID_COMMIT in _validate(bad_commit).reason_codes

    contradictory = _manifest_payload()
    contradictory["non_contamination_evidence"][0]["observed_heldout_cluster_key_digest"] = (
        _digest("7")
    )
    assert (
        TrainerResultRefusalCode.NON_CONTAMINATION_EVIDENCE_MISMATCH
        in _validate(contradictory).reason_codes
    )

    forged_receipts = _manifest_payload()
    del forged_receipts["provenance"]["runtime_receipts"]
    forged_receipts["provenance"]["platform_system"] = "Linux"
    forged_receipts["provenance"]["lockfile"] = "/tmp/trainer.lock"
    forged_receipts["provenance"]["extra_allowed_hosts"] = ["harbor.example.test"]
    forged_validation = _validate(forged_receipts)
    assert TrainerResultRefusalCode.UNKNOWN_FIELD in forged_validation.reason_codes

    override = _manifest_payload()
    override["non_contamination_evidence"][0]["evidence_class"] = "verified"
    assert TrainerResultRefusalCode.UNKNOWN_FIELD in _validate(override).reason_codes

    default_overwrite = _manifest_payload()
    default_overwrite["default"] = True
    assert TrainerResultRefusalCode.UNKNOWN_FIELD in _validate(default_overwrite).reason_codes

    held_out_scope = _manifest_payload()
    held_out_scope["reported_metrics"][0]["scope"] = "held-out"
    assert (
        TrainerResultRefusalCode.HELD_OUT_RESULT_IN_MANIFEST
        in _validate(held_out_scope).reason_codes
    )

    harbor = _manifest_payload()
    harbor["harbor_result"] = {"score": 0.9}
    assert TrainerResultRefusalCode.UNKNOWN_FIELD in _validate(harbor).reason_codes
