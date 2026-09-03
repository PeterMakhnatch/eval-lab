from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.benchmark_program_contracts import compute_prefixed_sha256
from evallab.trainer_bundle import (
    IncompatibilityCode,
    SpadePlanPayloadV1,
    TrainerArtifactRefV1,
    TrainerAuthorityGateV1,
    TrainerBackendIdentityV1,
    TrainerBackendRequirementsV1,
    TrainerBundleRefusal,
    TrainerBundleV1,
    TrainerDatasetBindingV1,
    TrainerEvaluationSetV1,
    TrainerHyperparametersV1,
    TrainerModelIdentityV1,
    TrainerObjectiveV1,
    TrainerRenderingContractV1,
    TrainerResultContractV1,
    TrainerRevisionIdentityV1,
    TrainerTaskIdentityV1,
    TRLPlanPayloadV1,
    backend_incompatibilities,
    expected_trainer_result_has_parity,
    rehydrate_rendered_trainer_plan,
    render_spade_plan,
    render_trl_plan,
    trainer_evaluation_suite_digest,
    trainer_plan_digest,
    trainer_task_set_digest,
    validate_trainer_bundle,
)


def _digest(value: object) -> str:
    return compute_prefixed_sha256(value)


def _file_digest(path: Path) -> str:
    return _digest(path.read_bytes())


def _dataset_digest(payload: dict[str, object]) -> str:
    return _digest(
        {
            "train_split": payload["train_split"],
            "validation_split": payload["validation_split"],
            "test_split": payload["test_split"],
            "exclusions_digest": payload["exclusions_digest"],
        }
    )


def _manifest_digest(payload: dict[str, object]) -> str:
    body = dict(payload)
    body.pop("manifest_digest", None)
    body.pop("cas_uri", None)
    return _digest(body)


def _persist_dataset(root: Path, payload: dict[str, object]) -> TrainerDatasetBindingV1:
    payload = dict(payload)
    payload["dataset_digest"] = _dataset_digest(payload)
    payload["manifest_digest"] = _manifest_digest(payload)
    dataset = TrainerDatasetBindingV1.model_validate(payload)
    if dataset.manifest_path is not None:
        (root / dataset.manifest_path).write_text(
            json.dumps(dataset.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return dataset


def _backend(name: str) -> TrainerBackendIdentityV1:
    external_name = {
        "trl": "generic-trl",
        "spade": "spade-external-consumer",
    }[name]
    return TrainerBackendIdentityV1(
        name=external_name,
        version="0.1.0",
        source_commit="a" * 40,
        image_digest=_digest(f"{name}-image"),
    )


def _evaluation_set() -> TrainerEvaluationSetV1:
    tasks = (
        TrainerTaskIdentityV1(
            task_id="task-a",
            task_digest=_digest("task-a"),
            cluster_key_digest=_digest("task-a-cluster"),
            verifier_digest=_digest("task-a-verifier"),
            environment_digest=_digest("task-a-environment"),
        ),
        TrainerTaskIdentityV1(
            task_id="task-b",
            task_digest=_digest("task-b"),
            cluster_key_digest=_digest("task-b-cluster"),
            verifier_digest=_digest("task-b-verifier"),
            environment_digest=_digest("task-b-environment"),
        ),
    )
    task_set_digest = trainer_task_set_digest(tasks)
    return TrainerEvaluationSetV1(
        suite_name="frozen-canary-v1",
        suite_digest=trainer_evaluation_suite_digest("frozen-canary-v1", task_set_digest),
        task_set_digest=task_set_digest,
        tasks=tasks,
    )


def _make_bundle(root: Path, *, objective: str = "sft") -> TrainerBundleV1:
    root.mkdir()
    train = root / "data/train.jsonl"
    validation = root / "data/validation.jsonl"
    test = root / "data/test.jsonl"
    train.parent.mkdir()
    if objective == "sft":
        train.write_text('{"prompt":"Question","completion":"Answer"}\n', encoding="utf-8")
        counts = {"prompt_response_sft": 3, "episode_steps": 0}
        rendering = TrainerRenderingContractV1(
            representation="prompt_response_sft",
            sft_format="prompt_completion",
            prompt_field="prompt",
            completion_field="completion",
        )
        trainer_objective = TrainerObjectiveV1(kind="sft")
    else:
        train.write_text('{"episode":[{"role":"assistant","content":"ok"}]}\n', encoding="utf-8")
        counts = {"prompt_response_sft": 0, "episode_steps": 3}
        rendering = TrainerRenderingContractV1(
            representation="episode_steps", episode_field="episode"
        )
        trainer_objective = TrainerObjectiveV1(
            kind="verifier_reward_episode",
            verifier_contract_digest=_digest("verifier-contract"),
        )
    validation.write_text('{"heldout":"validation"}\n', encoding="utf-8")
    test.write_text('{"heldout":"test"}\n', encoding="utf-8")
    exclusions = root / "exclusions.jsonl"
    exclusions.write_text("", encoding="utf-8")

    def split(path: Path) -> dict[str, object]:
        return {
            "path": path.relative_to(root).as_posix(),
            "digest": _file_digest(path),
            "cluster_key_digest": _digest(f"clusters:{path.name}"),
            "record_count": 1,
        }

    payload: dict[str, object] = {
        "schema_version": "training-dataset-manifest/v1",
        "manifest_path": "manifest.json",
        "cas_uri": None,
        "manifest_digest": _digest("pending"),
        "dataset_digest": _digest("pending"),
        "train_split": split(train),
        "validation_split": split(validation),
        "test_split": split(test),
        "source_refs": [
            {
                "job_id": "job-1",
                "trial_id": "trial-1",
                "source_digest": _digest("source"),
                "registry_allowed_use": "training",
                "task_registry_record_digest": _digest("registry"),
                "trial_admissibility_digest": _digest("admissibility"),
                "trial_admissibility_decision": "admissible",
                "trial_analysis_eligibility": "causal-eligible",
                "trial_admissibility_allowed_use": "causal",
            }
        ],
        "exporter": {
            "name": "evallab.training_export",
            "version": "1",
            "digest": _digest("exporter"),
        },
        "benchmark_families": ["mcp-funcdag"],
        "task_families": ["mcp-funcdag-v2"],
        "environment_integrity": "passed",
        "capture_complete": True,
        "redaction_status": "redacted",
        "registry_allowed_use": "training",
        "exclusions_path": "exclusions.jsonl",
        "exclusions_digest": _file_digest(exclusions),
        "exclusion_count": 0,
        "representation_counts": counts,
    }
    dataset = _persist_dataset(root, payload)
    checkpoint = root / "model/checkpoint.safetensors"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"fixture-checkpoint")
    checkpoint_digest = _file_digest(checkpoint)

    def identity(name: str) -> TrainerRevisionIdentityV1:
        return TrainerRevisionIdentityV1(
            name=name,
            revision=f"{name}-revision",
            digest=_digest(f"{name}-identity"),
        )

    return TrainerBundleV1(
        model_identity=TrainerModelIdentityV1(
            provider="local",
            model=identity("model"),
            tokenizer=identity("tokenizer"),
            chat_template=identity("template"),
            access_mode="checkpoint",
            checkpoint=TrainerArtifactRefV1(
                path="model/checkpoint.safetensors",
                content_digest=checkpoint_digest,
                cas_uri=f"cas://sha256/{checkpoint_digest.removeprefix('sha256:')}",
            ),
        ),
        dataset=dataset,
        dataset_manifest_artifact_digest=_file_digest(root / "manifest.json"),
        selected_split="train",
        heldout_split="validation",
        selected_representation=("prompt_response_sft" if objective == "sft" else "episode_steps"),
        objective=trainer_objective,
        rendering=rendering,
        seed=17,
        hyperparameters=TrainerHyperparametersV1(
            epochs=1,
            learning_rate=0.0001,
            batch_size=2,
            gradient_accumulation_steps=4,
            max_sequence_length=4096,
        ),
        backend_requirements=TrainerBackendRequirementsV1(),
        result_contract=TrainerResultContractV1(manifest_path="outputs/trainer-result.json"),
        evaluation_set=_evaluation_set(),
        authority_gate=TrainerAuthorityGateV1(),
    )


def _replace_dataset(root: Path, bundle: TrainerBundleV1, **updates: object) -> TrainerBundleV1:
    payload = bundle.dataset.model_dump(mode="json")
    payload.update(updates)
    dataset = _persist_dataset(root, payload)
    return bundle.model_copy(
        update={
            "dataset": dataset,
            "dataset_manifest_artifact_digest": _file_digest(root / "manifest.json"),
        }
    )


def test_trl_plan_is_deterministic_and_binds_strict_projection(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    bundle = _make_bundle(root)
    backend = _backend("trl")
    first = render_trl_plan(bundle, root, backend)
    second = render_trl_plan(bundle, root, backend)

    assert first == second
    assert trainer_plan_digest(first) == first.expected_result.trainer_plan_digest
    assert expected_trainer_result_has_parity(bundle, first, backend)
    assert isinstance(first.payload, TRLPlanPayloadV1)
    assert first.payload.adoption_stage == "adopted_s0"
    assert first.payload.truncation == "error"
    assert first.payload.assistant_only_loss is False
    expected = first.expected_result
    assert expected.required_result_fields == (
        "trainer_bundle_digest",
        "trainer_plan_digest",
        "checkpoint_artifact_digest",
    )
    assert expected.backend_version == backend.version
    assert expected.backend_source_commit == backend.source_commit
    assert expected.dataset_digest == bundle.dataset.dataset_digest
    assert expected.model_revision == bundle.model_identity.model.revision
    assert expected.input_model_checkpoint_digest == bundle.model_identity.checkpoint.content_digest
    assert expected.tokenizer_revision == bundle.model_identity.tokenizer.revision
    assert expected.chat_template_revision == bundle.model_identity.chat_template.revision
    assert expected.evaluation_set == bundle.evaluation_set
    rendered = first.model_dump_json()
    assert bundle.dataset.validation_split.path not in rendered
    assert '"command"' not in rendered


def test_spade_shape_is_deterministic_adapt_only_and_nonrenderable(
    tmp_path: Path,
) -> None:
    payload = SpadePlanPayloadV1(
        pair_id=_digest("pair"),
        arms=(
            {"arm": "hinted", "hint_available": True},
            {"arm": "unhinted", "hint_available": False},
        ),
        episode_path="data/train.jsonl",
        checkpoint_path="model/checkpoint.safetensors",
        verifier_contract_digest=_digest("verifier"),
        result_manifest_path="outputs/trainer-result.json",
    )
    assert payload == SpadePlanPayloadV1.model_validate(payload.model_dump(mode="json"))
    assert payload.adoption_stage == "adapt_only"

    root = tmp_path / "bundle"
    bundle = _make_bundle(root, objective="verifier_reward_episode")
    with pytest.raises(TrainerBundleRefusal) as raised:
        render_spade_plan(bundle, root, _backend("spade"))
    assert (
        raised.value.reason_code
        == "blocked:trainer_backend_incompatible:spade:sft_signal_not_established"
    )


def test_spade_api_only_model_refuses_with_exact_reason(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    bundle = _make_bundle(root, objective="verifier_reward_episode")
    model = bundle.model_identity.model_copy(update={"access_mode": "api_only", "checkpoint": None})
    bundle = bundle.model_copy(update={"model_identity": model})

    with pytest.raises(TrainerBundleRefusal) as raised:
        render_spade_plan(bundle, root, _backend("spade"))
    assert raised.value.reason_code == "blocked:trainer_backend_incompatible:spade:api_only_model"


def test_unsupported_backend_requirements_are_typed(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "bundle")
    bundle = bundle.model_copy(
        update={
            "backend_requirements": TrainerBackendRequirementsV1(
                requires_on_policy_tokens=True,
                requires_token_logprobs=True,
                requires_gpu=True,
                requires_network=True,
                required_runtime="cuda-12",
            )
        }
    )
    assert [item.code for item in backend_incompatibilities(bundle, "trl")] == [
        IncompatibilityCode.ON_POLICY_TOKENS_REQUIRED,
        IncompatibilityCode.TOKEN_LOGPROBS_REQUIRED,
        IncompatibilityCode.GPU_REQUIRED,
        IncompatibilityCode.NETWORK_REQUIRED,
        IncompatibilityCode.TRAINER_RUNTIME_REQUIRED,
    ]


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("data/train.jsonl", "digest_mismatch:training_split"),
        ("data/validation.jsonl", "digest_mismatch:validation_split"),
        ("data/test.jsonl", "digest_mismatch:test_split"),
        ("model/checkpoint.safetensors", "digest_mismatch:checkpoint"),
    ],
)
def test_bound_artifact_mutation_refuses(tmp_path: Path, path: str, reason: str) -> None:
    root = tmp_path / "bundle"
    bundle = _make_bundle(root)
    (root / path).write_bytes(b"mutated")
    with pytest.raises(TrainerBundleRefusal, match=reason):
        validate_trainer_bundle(bundle, root)


def test_hidden_split_output_traversal_and_symlink_refuse(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    bundle = _make_bundle(root)
    with pytest.raises(TrainerBundleRefusal, match="hidden_or_nontraining_split"):
        validate_trainer_bundle(bundle.model_copy(update={"selected_split": "validation"}), root)

    contract = bundle.result_contract.model_copy(update={"manifest_path": "../trainer-result.json"})
    with pytest.raises(TrainerBundleRefusal, match="path_invalid:result_manifest"):
        validate_trainer_bundle(bundle.model_copy(update={"result_contract": contract}), root)

    checkpoint = root / "model/checkpoint.safetensors"
    external = tmp_path / "external-checkpoint"
    external.write_bytes(checkpoint.read_bytes())
    checkpoint.unlink()
    checkpoint.symlink_to(external)
    with pytest.raises(TrainerBundleRefusal, match="symlink:checkpoint"):
        validate_trainer_bundle(bundle, root)


@pytest.mark.parametrize("field", ["labels", "token_ids", "logprobs", "reward", "attention_mask"])
def test_precomputed_training_fields_refuse(tmp_path: Path, field: str) -> None:
    root = tmp_path / "bundle"
    bundle = _make_bundle(root)
    train = root / bundle.dataset.train_split.path
    train.write_text(
        json.dumps({"prompt": "Question", "completion": "Answer", field: [1]}) + "\n",
        encoding="utf-8",
    )
    split = bundle.dataset.train_split.model_copy(update={"digest": _file_digest(train)})
    rebound = _replace_dataset(root, bundle, train_split=split.model_dump(mode="json"))
    with pytest.raises(TrainerBundleRefusal, match=f"prohibited_training_field:{field}"):
        validate_trainer_bundle(rebound, root)


def test_digest_authority_and_prohibited_corpus_refuse(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    bundle = _make_bundle(root)
    bad_dataset = bundle.dataset.model_copy(update={"dataset_digest": _digest("wrong")})
    with pytest.raises(TrainerBundleRefusal, match="dataset_digest_mismatch"):
        validate_trainer_bundle(bundle.model_copy(update={"dataset": bad_dataset}), root)

    source = bundle.dataset.source_refs[0].model_copy(
        update={"trial_admissibility_decision": "rejected"}
    )
    nonadmissible = _replace_dataset(root, bundle, source_refs=[source.model_dump(mode="json")])
    with pytest.raises(TrainerBundleRefusal, match="source_authority_not_training_admissible"):
        validate_trainer_bundle(nonadmissible, root)

    unauthorized_source = bundle.dataset.source_refs[0].model_copy(
        update={"registry_allowed_use": "measurement"}
    )
    unauthorized = _replace_dataset(
        root,
        bundle,
        source_refs=[unauthorized_source.model_dump(mode="json")],
    )
    with pytest.raises(
        TrainerBundleRefusal, match="source_registry_not_training_authorized"
    ):
        validate_trainer_bundle(unauthorized, root)

    prohibited = _replace_dataset(root, bundle, task_families=["syn-funcdag-easy"])
    with pytest.raises(TrainerBundleRefusal, match="prohibited_corpus:syn-funcdag-easy"):
        validate_trainer_bundle(prohibited, root)


def test_dataset_manifest_requires_bytes_verified_authority(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    bundle = _make_bundle(root)
    validated = validate_trainer_bundle(bundle, root)
    assert validated.dataset_manifest_authority.level == "bytes-verified"

    manifest = root / "manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    with pytest.raises(
        TrainerBundleRefusal,
        match="dataset_manifest_authority:ref_digest_parity_failed",
    ):
        validate_trainer_bundle(bundle, root)


def test_mutable_checkpoint_reference_refuses(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    bundle = _make_bundle(root)
    checkpoint = bundle.model_identity.checkpoint
    assert checkpoint is not None
    mutable = checkpoint.model_copy(update={"cas_uri": "file:///tmp/checkpoint"})
    model = bundle.model_identity.model_copy(update={"checkpoint": mutable})

    with pytest.raises(TrainerBundleRefusal, match="mutable_source:checkpoint"):
        validate_trainer_bundle(bundle.model_copy(update={"model_identity": model}), root)


def test_plan_digest_and_projection_parity_reject_substitution(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    bundle = _make_bundle(root)
    backend = _backend("trl")
    plan = render_trl_plan(bundle, root, backend)

    payload_mutation = plan.model_dump(mode="json")
    payload_mutation["payload"]["learning_rate"] = 0.5
    with pytest.raises(ValidationError, match="trainer plan digest mismatch"):
        type(plan).model_validate(payload_mutation)

    authority_substitution = plan.model_dump(mode="json")
    authority_substitution["expected_result"]["dataset_digest"] = _digest("substitute")
    authority_substitution["expected_result"]["trainer_plan_digest"] = trainer_plan_digest(
        authority_substitution
    )
    internally_consistent = type(plan).model_validate(authority_substitution)
    with pytest.raises(
        TrainerBundleRefusal, match="expected_result_parity_mismatch"
    ):
        rehydrate_rendered_trainer_plan(
            internally_consistent,
            bundle=bundle,
            backend_identity=backend,
        )
    assert not expected_trainer_result_has_parity(bundle, internally_consistent, backend)

def test_render_rehydrate_is_byte_identical(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    bundle = _make_bundle(root)
    backend = _backend("trl")
    rendered = render_trl_plan(bundle, root, backend)
    rendered_bytes = rendered.model_dump_json().encode()

    rehydrated = rehydrate_rendered_trainer_plan(
        json.loads(rendered_bytes),
        bundle=bundle,
        backend_identity=backend,
    )

    assert rehydrated.model_dump_json().encode() == rendered_bytes


def test_arbitrary_sft_signal_digest_cannot_unlock_spade(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        TrainerObjectiveV1(
            kind="verifier_reward_episode",
            verifier_contract_digest=_digest("verifier-contract"),
            training_signal_status="established",
            training_signal_digest=_digest("untrusted-sft-result"),
        )

    root = tmp_path / "bundle"
    bundle = _make_bundle(root, objective="verifier_reward_episode")
    bypassed = bundle.objective.model_copy(
        update={
            "training_signal_status": "established",
            "training_signal_digest": _digest("untrusted-sft-result"),
        }
    )
    with pytest.raises(TrainerBundleRefusal) as raised:
        render_spade_plan(
            bundle.model_copy(update={"objective": bypassed}),
            root,
            _backend("spade"),
        )
    assert (
        raised.value.reason_code
        == "blocked:trainer_backend_incompatible:spade:sft_signal_not_established"
    )


def test_train_heldout_overlap_and_exclusion_leak_refuse(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    bundle = _make_bundle(root)
    overlapping = _replace_dataset(
        root,
        bundle,
        validation_split=bundle.dataset.train_split.model_dump(mode="json"),
    )
    with pytest.raises(TrainerBundleRefusal, match="split_overlap"):
        validate_trainer_bundle(overlapping, root)

    leaky_root = tmp_path / "leaky"
    bundle = _make_bundle(leaky_root)
    exclusions = leaky_root / "exclusions.jsonl"
    exclusions.write_text('{"labels":[1]}\n', encoding="utf-8")
    leaky = _replace_dataset(
        leaky_root,
        bundle,
        exclusions_digest=_file_digest(exclusions),
        exclusion_count=1,
    )
    with pytest.raises(TrainerBundleRefusal, match="prohibited_exclusion_field:labels"):
        validate_trainer_bundle(leaky, leaky_root)


def test_existing_and_symlinked_result_output_refuse(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    bundle = _make_bundle(root)
    output = root / bundle.result_contract.manifest_path
    output.parent.mkdir()
    output.write_text("{}\n", encoding="utf-8")
    with pytest.raises(TrainerBundleRefusal, match="result_manifest_exists"):
        validate_trainer_bundle(bundle, root)

    output.unlink()
    external = tmp_path / "external-result"
    external.write_text("{}\n", encoding="utf-8")
    output.symlink_to(external)
    with pytest.raises(TrainerBundleRefusal, match="symlink:result_manifest"):
        validate_trainer_bundle(bundle, root)


def test_suite_digest_and_finite_hyperparameters_fail_closed() -> None:
    evaluation = _evaluation_set()
    payload = evaluation.model_dump(mode="json")
    payload["tasks"][0]["environment_digest"] = _digest("substitution")
    with pytest.raises(ValidationError, match="task_set_digest mismatch"):
        TrainerEvaluationSetV1.model_validate(payload)

    with pytest.raises(ValidationError, match="learning_rate must be finite"):
        TrainerHyperparametersV1(
            epochs=1,
            learning_rate=float("inf"),
            batch_size=1,
            gradient_accumulation_steps=1,
            max_sequence_length=1,
        )


def test_thinking_loss_and_unsorted_task_set_cannot_be_enabled() -> None:
    identity = {"name": "model", "revision": "revision", "digest": _digest("identity")}
    with pytest.raises(ValidationError):
        TrainerModelIdentityV1(
            provider="api",
            model=identity,
            tokenizer=identity,
            chat_template=identity,
            access_mode="api_only",
            enable_thinking=True,
        )
    with pytest.raises(ValidationError):
        TrainerRenderingContractV1(
            representation="prompt_response_sft",
            sft_format="messages",
            messages_field="messages",
            assistant_only_loss=True,
        )
    evaluation = _evaluation_set()
    with pytest.raises(ValidationError):
        TrainerEvaluationSetV1(
            suite_name=evaluation.suite_name,
            suite_digest=evaluation.suite_digest,
            task_set_digest=evaluation.task_set_digest,
            tasks=tuple(reversed(evaluation.tasks)),
        )
    aliased_tasks = (
        evaluation.tasks[0],
        evaluation.tasks[1].model_copy(update={"task_digest": evaluation.tasks[0].task_digest}),
    )
    aliased_task_set_digest = trainer_task_set_digest(aliased_tasks)
    with pytest.raises(ValidationError):
        TrainerEvaluationSetV1(
            suite_name=evaluation.suite_name,
            suite_digest=trainer_evaluation_suite_digest(
                evaluation.suite_name, aliased_task_set_digest
            ),
            task_set_digest=aliased_task_set_digest,
            tasks=aliased_tasks,
        )
    with pytest.raises(ValidationError):
        TrainerAuthorityGateV1(real_corpus_training_allowed=True)
