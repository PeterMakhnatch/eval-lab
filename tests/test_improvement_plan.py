from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_curriculum_candidates import real_track_b_receipt
from test_paired_intervention import _analysis_gate, _capture, _delta, _spec
from test_trainer_bundle import _backend, _file_digest, _make_bundle
from test_training_export import _messages, _source

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
    StageDisposition,
    gate_table,
    run_improvement_plan,
)
from evallab.paired_intervention import PairedArmCandidate, RetryReplacementPolicy
from evallab.queue import load_policy
from evallab.trainer_bundle import TrainerObjectiveV1
from evallab.training_export import export_training_dataset

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


def test_offline_plan_publishes_once_and_rehydrates_byte_identically(tmp_path: Path) -> None:
    manifest = _input_manifest(tmp_path)
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
    assert first.stages[-2].disposition is StageDisposition.WAITING
    assert first.stages[-1].disposition is StageDisposition.WAITING
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
