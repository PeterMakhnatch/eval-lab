from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_curriculum_candidates import real_track_b_receipt
from test_paired_intervention import _analysis_gate, _capture, _delta, _spec
from test_paired_outcome import _observation, _rule, _task_runtime
from test_trainer_bundle import _backend, _file_digest, _make_bundle
from test_training_export import _messages, _source
from test_training_result_manifest import _manifest_data, _result_manifest_authority

import evallab.improvement_plan as improvement_plan_module
from evallab.artifact_authority import (
    VERIFIER_IMPLEMENTATION_DIGEST,
    ArtifactAuthority,
    ArtifactRef,
    verify_artifact,
)
from evallab.curriculum_candidates import synthesize_curriculum_candidates
from evallab.improvement_plan import (
    ImprovementPlanInputManifestV1,
    ImprovementPlanRefusal,
    ImprovementPlanRefusalCode,
    ImprovementStage,
    PairedOutcomeInputV1,
    StageStatus,
    TrainerResultAttemptV1,
    gate_table,
    run_improvement_plan,
)
from evallab.paired_intervention import PairedArmCandidate, RetryReplacementPolicy
from evallab.queue import load_policy
from evallab.trainer_bundle import ExpectedTrainerResultV1, TrainerObjectiveV1
from evallab.training_export import export_training_dataset
from evallab.training_result import (
    compute_split_integrity_binding_digest,
    create_trainer_result_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _checkpoint_authority(workspace: Path, bundle_root: Path) -> ArtifactAuthority:
    checkpoint = bundle_root / "model/checkpoint.safetensors"
    result = verify_artifact(
        ArtifactRef(
            ref=checkpoint.relative_to(workspace).as_posix(),
            digest=_file_digest(checkpoint),
        ),
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        repo_root=workspace,
    )
    assert isinstance(result, ArtifactAuthority)
    return result


def _input_manifest(workspace: Path) -> ImprovementPlanInputManifestV1:
    deficit_root = workspace / "deficit"
    receipt, expectation = real_track_b_receipt(deficit_root)
    trusted = {receipt.artifact.content_digest: expectation}
    curriculum = synthesize_curriculum_candidates(
        (receipt,),
        trusted_parent_outputs=trusted,
        seed=41,
        authority_store_root=deficit_root / "store",
    )
    assert curriculum.contrast_pairs

    delta = _delta(workspace)
    paired_candidates: list[PairedArmCandidate] = []
    for index, pair in enumerate(curriculum.contrast_pairs, start=1):
        runtime = _task_runtime().model_copy(
            update={
                "task_id": "paired-intervention-task",
                "task_version": "fixture-v1",
            }
        )
        for arm in ("control", "treatment"):
            paired_candidates.append(
                PairedArmCandidate(
                    pair_id=pair.twin_pair_id,
                    block_id="fixture-block",
                    assignment_unit_id=f"fixture-unit-{index}",
                    arm=arm,
                    spec=_spec(
                        pair_id=f"fixture-pair-{index}",
                        assignment_unit_id=f"fixture-unit-{index}",
                        seed=100 + index,
                        arm=arm,
                        delta=delta,
                    ).model_copy(
                        update={
                            "task_version": runtime.task_version,
                            "task_package_digest": (
                                runtime.certified_runtime_package_digest
                            ),
                            "task_runtime_identity": runtime,
                        }
                    ),
                    capture=_capture(),
                )
            )

    source_root = workspace / "training-sources"
    sources = tuple(
        _source(
            source_root,
            index,
            split=split,
            messages=_messages(response=f"Stable improvement response {index}."),
        )
        for index, split in enumerate(("train", "validation", "test"), start=1)
    )
    preview = export_training_dataset(
        sources,
        workspace / "training-preview",
        representations=("prompt_response_sft",),
    )
    trainer_root = workspace / "trainer-source"
    trainer = _make_bundle(trainer_root).model_copy(
        update={
            "dataset": preview.manifest,
            "dataset_manifest_artifact_digest": _file_digest(preview.manifest_path),
        }
    )
    return ImprovementPlanInputManifestV1(
        output_path="improvement-output",
        training_sources=sources,
        deficit_receipts=(receipt,),
        trusted_parent_outputs=trusted,
        curriculum_seed=41,
        curriculum_budget=1,
        paired_plan_id="fixture-improvement-plan",
        paired_randomization_seed=7349,
        paired_candidates=tuple(paired_candidates),
        paired_delta=delta,
        paired_retry_policy=RetryReplacementPolicy(),
        paired_analysis_gate=_analysis_gate(minimum_complete_pairs=1),
        standing_approvals=load_policy(REPO_ROOT / "policy/standing-approvals.yaml"),
        spent_today_usd=0.0,
        trainer_bundle=trainer,
        trainer_backend_identity=_backend("trl"),
        checkpoint_authority=_checkpoint_authority(workspace, trainer_root),
        authority_store_root=deficit_root / "store",
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _completed_result_attempt(
    workspace: Path,
    manifest: ImprovementPlanInputManifestV1,
    expected: ExpectedTrainerResultV1,
) -> TrainerResultAttemptV1:
    data = _manifest_data()
    expected_payload = expected.model_dump(mode="json")
    for name in (
        "trainer_bundle_digest",
        "source_authority_status",
        "result_manifest_path",
        "trainer_plan_digest",
        "adapter_contract",
        "input_model_checkpoint_digest",
        "effective_config_digest",
    ):
        data[name] = expected_payload[name]
    data["backend_identity"] = {
        "backend_name": expected_payload["backend_name"],
        "backend_version": expected_payload["backend_version"],
        "backend_source_commit": expected_payload["backend_source_commit"],
        "backend_image_digest": expected_payload["backend_image_digest"],
    }
    data["model"] = {
        "model_revision": expected_payload["model_revision"],
        "model_digest": expected_payload["model_digest"],
        "tokenizer_revision": expected_payload["tokenizer_revision"],
        "tokenizer_digest": expected_payload["tokenizer_digest"],
        "chat_template_revision": expected_payload["chat_template_revision"],
        "chat_template_digest": expected_payload["chat_template_digest"],
    }
    dataset_names = (
        "dataset_manifest_digest",
        "dataset_manifest_authority_digest",
        "dataset_manifest_verifier_digest",
        "dataset_manifest_authority_level",
        "dataset_digest",
        "train_split_digest",
        "heldout_split_digest",
        "train_cluster_key_digest",
        "heldout_cluster_key_digest",
    )
    data["dataset"] = {name: expected_payload[name] for name in dataset_names}
    evidence = data["non_contamination_evidence"][0]
    evidence.update(
        {
            "observed_train_split_digest": expected_payload["train_split_digest"],
            "observed_heldout_split_digest": expected_payload[
                "heldout_split_digest"
            ],
            "observed_train_cluster_key_digest": expected_payload[
                "train_cluster_key_digest"
            ],
            "observed_heldout_cluster_key_digest": expected_payload[
                "heldout_cluster_key_digest"
            ],
            "split_integrity_binding_digest": compute_split_integrity_binding_digest(
                train_split_digest=expected_payload["train_split_digest"],
                heldout_split_digest=expected_payload["heldout_split_digest"],
                train_cluster_key_digest=expected_payload[
                    "train_cluster_key_digest"
                ],
                heldout_cluster_key_digest=expected_payload[
                    "heldout_cluster_key_digest"
                ],
            ),
        }
    )
    result_manifest = create_trainer_result_manifest(**data)
    result_authority = _result_manifest_authority(workspace, result_manifest)
    heldout_keys = tuple(
        sorted(
            {
                source.cluster_key
                for source in manifest.training_sources
                if source.split == manifest.trainer_bundle.heldout_split
            }
        )
    )
    return TrainerResultAttemptV1(
        manifest=result_manifest,
        result_manifest_authority=result_authority,
        frozen_tasks=expected.evaluation_set.tasks,
        trusted_heldout_cluster_keys=heldout_keys,
    )

def _assert_refusal(
    manifest: ImprovementPlanInputManifestV1,
    workspace: Path,
    reason_code: ImprovementPlanRefusalCode,
) -> None:
    with pytest.raises(ImprovementPlanRefusal) as exc:
        run_improvement_plan(manifest, workspace_root=workspace)
    assert exc.value.reason_code is reason_code
    assert not (workspace / manifest.output_path).exists()


def test_offline_plan_publishes_once_and_rehydrates_byte_identically(tmp_path: Path) -> None:
    manifest = _input_manifest(tmp_path)
    assert improvement_plan_module._input_digest(
        manifest.model_copy(update={"output_path": "different-output"})
    ) != improvement_plan_module._input_digest(manifest)
    first = run_improvement_plan(manifest, workspace_root=tmp_path)
    output = tmp_path / manifest.output_path
    before = _tree_bytes(output)

    second = run_improvement_plan(manifest, workspace_root=tmp_path)

    assert second == first
    assert _tree_bytes(output) == before
    assert tuple(stage.stage for stage in first.stages) == tuple(ImprovementStage)
    assert first.ready_for_external_sft is True
    assert first.ready_for_rl is False
    assert first.curriculum.status == "quarantined"
    assert first.curriculum.training_eligible is False
    assert all(
        stage.status is StageStatus.UNAVAILABLE for stage in first.stages[-3:]
    )
    assert all(stage.input_digest.startswith("sha256:") for stage in first.stages)
    assert all(stage.numerator <= stage.denominator for stage in first.stages)
    assert {entry.pair_id for entry in first.paired_plan.schedule} == {
        pair.twin_pair_id for pair in first.curriculum.contrast_pairs
    }
    assert "external_sft_plan_ready" in gate_table(first)


def test_existing_output_refuses_changed_input_and_artifact_tampering(tmp_path: Path) -> None:
    manifest = _input_manifest(tmp_path)
    run_improvement_plan(manifest, workspace_root=tmp_path)
    output = tmp_path / manifest.output_path

    changed = manifest.model_copy(update={"curriculum_seed": manifest.curriculum_seed + 1})
    with pytest.raises(ImprovementPlanRefusal) as mismatch:
        run_improvement_plan(changed, workspace_root=tmp_path)
    assert mismatch.value.reason_code is ImprovementPlanRefusalCode.INPUT_MANIFEST_MISMATCH

    trainer_plan = output / "trainer-plan.json"
    payload = json.loads(trainer_plan.read_text(encoding="utf-8"))
    payload["seed"] += 1
    trainer_plan.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ImprovementPlanRefusal) as tampered:
        run_improvement_plan(manifest, workspace_root=tmp_path)
    assert tampered.value.reason_code is ImprovementPlanRefusalCode.OUTPUT_ARTIFACT_TAMPERED


def test_rl_and_curriculum_pair_substitution_refuse_without_publication(tmp_path: Path) -> None:
    manifest = _input_manifest(tmp_path)
    rl_objective = TrainerObjectiveV1(
        kind="verifier_reward_episode",
        verifier_contract_digest="sha256:" + "a" * 64,
    )
    rl_bundle = manifest.trainer_bundle.model_copy(update={"objective": rl_objective})
    with pytest.raises(ImprovementPlanRefusal) as unsupported:
        run_improvement_plan(
            manifest.model_copy(update={"trainer_bundle": rl_bundle}),
            workspace_root=tmp_path,
        )
    assert unsupported.value.reason_code is ImprovementPlanRefusalCode.TRAINING_MODE_UNSUPPORTED
    assert not (tmp_path / manifest.output_path).exists()

    substituted = tuple(
        candidate.model_copy(update={"pair_id": "substituted-pair"})
        for candidate in manifest.paired_candidates
    )
    with pytest.raises(ImprovementPlanRefusal) as mismatch:
        run_improvement_plan(
            manifest.model_copy(update={"paired_candidates": substituted}),
            workspace_root=tmp_path,
        )
    assert mismatch.value.reason_code is ImprovementPlanRefusalCode.CURRICULUM_PAIR_MISMATCH
    assert not (tmp_path / manifest.output_path).exists()


def test_optional_paired_outcome_is_composed_and_persisted(tmp_path: Path) -> None:
    preview_manifest = _input_manifest(tmp_path)
    preview = run_improvement_plan(preview_manifest, workspace_root=tmp_path)
    observations = tuple(
        _observation(
            preview.paired_plan,
            scheduled,
            runtime=scheduled.spec.task_runtime_identity,
        )
        for scheduled in preview.paired_plan.schedule
    )
    manifest = preview_manifest.model_copy(
        update={
            "output_path": "improvement-with-outcome",
            "paired_outcome": PairedOutcomeInputV1(
                artifact_id="track-h-paired-outcome",
                observations=observations,
                decision_rule=_rule(minimum_pairs=1),
            ),
        }
    )

    result = run_improvement_plan(manifest, workspace_root=tmp_path)

    assert result.paired_outcome is not None
    assert result.paired_outcome.plan == result.paired_plan
    outcome_stage = next(
        stage
        for stage in result.stages
        if stage.stage is ImprovementStage.PAIRED_OUTCOME
    )
    assert outcome_stage.status is StageStatus.PASSED
    assert (tmp_path / manifest.output_path / "paired-outcome.json").is_file()


def test_completed_result_reaches_frozen_heldout_handoff(tmp_path: Path) -> None:
    preview_manifest = _input_manifest(tmp_path)
    preview = run_improvement_plan(preview_manifest, workspace_root=tmp_path)
    attempt = _completed_result_attempt(
        tmp_path,
        preview_manifest,
        preview.trainer_plan.expected_result,
    )
    manifest = preview_manifest.model_copy(
        update={
            "output_path": "improvement-with-completed-result",
            "trainer_result": attempt,
        }
    )

    result = run_improvement_plan(manifest, workspace_root=tmp_path)
    rehydrated = run_improvement_plan(manifest, workspace_root=tmp_path)

    assert result.result_validation is not None
    assert rehydrated == result
    assert result.result_validation.eligible_for_held_out_handoff is True
    assert result.held_out_handoff is not None
    assert result.held_out_handoff.submission_permitted is False
    assert all(stage.status is StageStatus.PASSED for stage in result.stages[-2:])
    output = tmp_path / manifest.output_path
    assert (output / "result-validation.json").is_file()
    assert (output / "held-out-handoff.json").is_file()


def test_structural_only_checkpoint_authority_refuses_closed(tmp_path: Path) -> None:
    manifest = _input_manifest(tmp_path)
    structural = manifest.checkpoint_authority.model_copy(
        update={"level": "structural-self-consistent"}
    )
    _assert_refusal(
        manifest.model_copy(update={"checkpoint_authority": structural}),
        tmp_path,
        ImprovementPlanRefusalCode.CHECKPOINT_AUTHORITY_UNVERIFIED,
    )


def test_empty_training_set_refuses_closed(tmp_path: Path) -> None:
    manifest = _input_manifest(tmp_path).model_copy(update={"training_sources": ()})
    _assert_refusal(
        manifest,
        tmp_path,
        ImprovementPlanRefusalCode.TRAINING_SET_EMPTY,
    )


def test_unsupported_deficit_refuses_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _input_manifest(tmp_path)
    curriculum = synthesize_curriculum_candidates(
        manifest.deficit_receipts,
        trusted_parent_outputs=manifest.trusted_parent_outputs,
        seed=manifest.curriculum_seed,
        authority_store_root=manifest.authority_store_root,
    ).model_copy(update={"candidates": (), "contrast_pairs": ()})
    monkeypatch.setattr(
        improvement_plan_module,
        "synthesize_curriculum_candidates",
        lambda *args, **kwargs: curriculum,
    )
    _assert_refusal(
        manifest,
        tmp_path,
        ImprovementPlanRefusalCode.CURRICULUM_EMPTY,
    )


def test_candidate_quarantine_corruption_refuses_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _input_manifest(tmp_path)
    curriculum = synthesize_curriculum_candidates(
        manifest.deficit_receipts,
        trusted_parent_outputs=manifest.trusted_parent_outputs,
        seed=manifest.curriculum_seed,
        authority_store_root=manifest.authority_store_root,
    ).model_copy(update={"status": "published", "training_eligible": True})
    monkeypatch.setattr(
        improvement_plan_module,
        "synthesize_curriculum_candidates",
        lambda *args, **kwargs: curriculum,
    )
    _assert_refusal(
        manifest,
        tmp_path,
        ImprovementPlanRefusalCode.CURRICULUM_QUARANTINE_BROKEN,
    )


def test_split_overlap_substitution_refuses_closed(tmp_path: Path) -> None:
    manifest = _input_manifest(tmp_path)
    dataset = manifest.trainer_bundle.dataset
    overlap = dataset.model_copy(update={"validation_split": dataset.train_split})
    trainer = manifest.trainer_bundle.model_copy(update={"dataset": overlap})
    _assert_refusal(
        manifest.model_copy(update={"trainer_bundle": trainer}),
        tmp_path,
        ImprovementPlanRefusalCode.DATASET_BINDING_MISMATCH,
    )


def test_stale_paired_plan_digest_refuses_existing_output(tmp_path: Path) -> None:
    manifest = _input_manifest(tmp_path)
    run_improvement_plan(manifest, workspace_root=tmp_path)
    bundle_path = tmp_path / manifest.output_path / "improvement-plan.json"
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    payload["paired_plan"]["plan_digest"] = "sha256:" + "0" * 64
    bundle_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ImprovementPlanRefusal) as exc:
        run_improvement_plan(manifest, workspace_root=tmp_path)
    assert exc.value.reason_code is ImprovementPlanRefusalCode.EXISTING_OUTPUT_INVALID


def test_failed_trainer_attempt_refuses_closed(tmp_path: Path) -> None:
    data = _manifest_data()
    data["run_identity"] = {
        "seed": 20260903,
        "terminal_status": "failed",
        "terminal_status_reason": "trainer exited before producing a checkpoint",
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
    manifest = _input_manifest(tmp_path)
    attempt = TrainerResultAttemptV1(
        manifest=failed,
        result_manifest_authority=result_authority,
        frozen_tasks=manifest.trainer_bundle.evaluation_set.tasks,
        trusted_heldout_cluster_keys=("heldout-fixture",),
    )
    _assert_refusal(
        manifest.model_copy(update={"trainer_result": attempt}),
        tmp_path,
        ImprovementPlanRefusalCode.RESULT_REFUSED,
    )


def test_heldout_split_mutation_refuses_resume(tmp_path: Path) -> None:
    manifest = _input_manifest(tmp_path)
    bundle = run_improvement_plan(manifest, workspace_root=tmp_path)
    heldout = next(
        artifact
        for artifact in bundle.artifacts
        if artifact.path.endswith("validation.jsonl")
    )
    path = tmp_path / manifest.output_path / heldout.path
    path.write_bytes(path.read_bytes() + b'{"mutated":true}\n')

    with pytest.raises(ImprovementPlanRefusal) as exc:
        run_improvement_plan(manifest, workspace_root=tmp_path)
    assert exc.value.reason_code is ImprovementPlanRefusalCode.OUTPUT_ARTIFACT_TAMPERED


def test_symlink_parent_refuses_without_publication(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    external_output = real_parent / "improvement-output"
    external_output.mkdir(parents=True)
    sentinel = external_output / "improvement-plan.json"
    sentinel.write_text("must-not-be-read", encoding="utf-8")
    (tmp_path / "linked-parent").symlink_to(real_parent, target_is_directory=True)
    manifest = _input_manifest(tmp_path).model_copy(
        update={"output_path": "linked-parent/improvement-output"}
    )
    with pytest.raises(ImprovementPlanRefusal) as exc:
        run_improvement_plan(manifest, workspace_root=tmp_path)
    assert exc.value.reason_code is ImprovementPlanRefusalCode.EXISTING_OUTPUT_INVALID
    assert "traverses a symlink" in str(exc.value)
    assert sentinel.read_text(encoding="utf-8") == "must-not-be-read"


def test_interrupted_staging_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _input_manifest(tmp_path)
    original = improvement_plan_module._write_new
    calls = 0

    def interrupt(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interruption")
        original(path, content)

    monkeypatch.setattr(improvement_plan_module, "_write_new", interrupt)
    _assert_refusal(
        manifest,
        tmp_path,
        ImprovementPlanRefusalCode.OUTPUT_PUBLICATION_REFUSED,
    )
    assert list(tmp_path.glob(".improvement-output.staging-*")) == []
