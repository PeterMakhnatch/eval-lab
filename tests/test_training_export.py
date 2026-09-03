from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.registry import task_runtime_identity
from evallab.schemas import (
    NETWORK_ESCAPE_CLASSES,
    NetworkEscapeProbeResultV1,
    NetworkIsolationProbeIdentityV1,
    NetworkIsolationRuntimeIdentityV1,
    NetworkPolicyEvidenceV1,
    TaskRegistryRecord,
    TrialSourceDigestsV1,
    TrialSourcePathsV1,
    build_network_isolation_evidence,
    build_trial_admissibility,
)
from evallab.training_export import (
    NormalizedTrainingEvidence,
    TrainingDatasetManifestV1,
    TrainingFunctionDefinition,
    TrainingMessage,
    TrainingSplitRefV1,
    TrainingTool,
    export_training_dataset,
)

NOW = datetime(2026, 9, 3, 1, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _sha_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _manifest_digest(payload: dict[str, object]) -> str:
    body = {
        key: value for key, value in payload.items() if key not in {"manifest_digest", "cas_uri"}
    }
    return _sha_bytes(
        json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    )


def _registry_record() -> TaskRegistryRecord:
    return TaskRegistryRecord.model_validate_json(
        (REPO_ROOT / "library/registry/event-summary.json").read_text(encoding="utf-8")
    )


def _isolation_evidence():
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
            platform_machine="x86_64",
            container_runtime="docker",
            container_runtime_version="29.4.1",
            container_image_digest=DIGEST,
            adapter="fixture-adapter",
            adapter_version="1",
            adapter_digest="sha256:" + "b" * 64,
        ),
        probe_identity=NetworkIsolationProbeIdentityV1(
            implementation="fixture-probe",
            implementation_version="1",
            implementation_digest="sha256:" + "c" * 64,
            config_digest="sha256:" + "d" * 64,
        ),
        probe_results=tuple(
            NetworkEscapeProbeResultV1(
                escape_class=escape_class,
                target=f"http://blocked.invalid/{escape_class}",
                outcome="blocked",
                detail="blocked by fixture policy",
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
    source_digest: str,
    source_path: str = "agent/trajectory.json",
    registry_record: TaskRegistryRecord | None = None,
):
    record = registry_record or _registry_record()
    return build_trial_admissibility(
        trial_id=trial_id,
        task_runtime_identity=task_runtime_identity(record),
        source_digests=TrialSourceDigestsV1(
            contract=_sha("contract"),
            trajectory=source_digest,
            final_state=_sha("final-state"),
            verifier=_sha("verifier"),
            outcome=_sha("outcome"),
            interpretation=_sha("interpretation"),
        ),
        source_paths=TrialSourcePathsV1(
            contract=("benchmark_contract.json",),
            trajectory=(source_path,),
            final_state=("final-state.json",),
            verifier=("verifier/result.json",),
            outcome=("artifacts/app/output/result.json",),
            interpretation=("analysis/interpretation.json",),
        ),
        network_isolation_evidence=_isolation_evidence(),
        evaluated_at=NOW,
    )


def _messages(response: str = "Use the stable public interface.") -> tuple[TrainingMessage, ...]:
    return (
        TrainingMessage(sequence=0, role="system", content="Follow the public task contract."),
        TrainingMessage(sequence=1, role="user", content="Repair the public fixture."),
        TrainingMessage(sequence=2, role="assistant", content=response),
    )


def _source(
    index: int = 1,
    *,
    split: str = "train",
    cluster_key: str | None = None,
    history_key: str | None = None,
    history_revision: int = 1,
    messages: tuple[TrainingMessage, ...] | None = None,
) -> NormalizedTrainingEvidence:
    registry_record = _registry_record()
    trial_id = f"trial-{index}"
    source_digest = _sha(f"trajectory-{index}")
    return NormalizedTrainingEvidence(
        job_id=f"job-{index}",
        trial_id=trial_id,
        task_id=registry_record.task_id,
        benchmark_family="event-summary-v1",
        task_family=registry_record.task_family,
        corpus_id="fixture-public",
        split=split,
        cluster_key=cluster_key or f"cluster-{index}",
        history_key=history_key or f"history-{index}",
        history_revision=history_revision,
        source_path="agent/trajectory.json",
        source_artifact_digest=source_digest,
        source_cas_uri=f"cas://sha256/{source_digest.removeprefix('sha256:')}",
        lineage_digest=_sha(f"lineage-{index}"),
        lineage_status="immutable",
        registry_record=registry_record,
        admissibility=_admissibility(
            trial_id=trial_id, source_digest=source_digest, registry_record=registry_record
        ),
        capture_status="complete",
        environment_integrity="passed",
        evaluator_status="present",
        semantic_evidence_status="complete",
        redaction_status="redacted",
        feature_names=("status",),
        tools=(
            TrainingTool(
                function=TrainingFunctionDefinition(
                    name="read_public_fixture",
                    description="Read a named public fixture.",
                    parameters={
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                )
            ),
        ),
        messages=messages or _messages(),
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(root.iterdir())}


def _reason_set(result) -> set[str]:
    return {reason for exclusion in result.exclusions for reason in exclusion.reasons}


def test_fixture_export_is_byte_identical_and_authority_bound(tmp_path: Path) -> None:
    source = _source()
    first = export_training_dataset([source], tmp_path / "first")
    second = export_training_dataset([source], tmp_path / "second")

    assert _tree_bytes(first.root) == _tree_bytes(second.root)
    assert len(first.records) == 2
    assert first.manifest.train_split.record_count == 2
    assert first.manifest.validation_split.record_count == 0
    assert first.manifest.test_split.record_count == 0
    assert first.manifest.registry_allowed_use == "training"
    source_ref = first.manifest.source_refs[0]
    assert source_ref.source_digest == source.source_artifact_digest
    assert source_ref.registry_allowed_use == "training"
    assert source_ref.task_registry_record_digest
    assert source_ref.trial_admissibility_digest == source.admissibility.admissibility_digest
    assert source_ref.trial_admissibility_decision == "admissible"
    assert source_ref.trial_analysis_eligibility == "causal-eligible"
    assert source_ref.trial_admissibility_allowed_use == "causal"
    assert {record.representation for record in first.records} == {
        "prompt_response_sft",
        "episode_steps",
    }
    assert all(record.source.trial_admissibility_digest for record in first.records)
    assert all(record.source.task_registry_record_digest for record in first.records)
    assert all("tools" in record.payload for record in first.records)
    serialized = b"".join(_tree_bytes(first.root).values())
    for forbidden in (b"token_ids", b"labels", b"logprobs", b"trainer_config"):
        assert forbidden not in serialized
    for split, reference in (
        ("train", first.manifest.train_split),
        ("validation", first.manifest.validation_split),
        ("test", first.manifest.test_split),
    ):
        assert _sha_bytes(first.split_paths[split].read_bytes()) == reference.digest


def test_incomplete_malicious_and_prohibited_inputs_are_typed_exclusions(tmp_path: Path) -> None:
    base = _source()
    measurement_only_payload = base.registry_record.model_dump(mode="json")
    measurement_only_payload["allowed_uses"] = ["measurement"]
    measurement_only = TaskRegistryRecord.model_validate(measurement_only_payload)
    cases = [
        base.model_copy(
            update={"trial_id": "missing-admiss", "history_key": "h1", "admissibility": None}
        ),
        base.model_copy(
            update={"trial_id": "capture", "history_key": "h2", "capture_status": "gapped"}
        ),
        base.model_copy(
            update={"trial_id": "integrity", "history_key": "h3", "environment_integrity": "failed"}
        ),
        base.model_copy(
            update={"trial_id": "evaluator", "history_key": "h4", "evaluator_status": "missing"}
        ),
        base.model_copy(
            update={
                "trial_id": "reward",
                "history_key": "h5",
                "semantic_evidence_status": "reward_only",
            }
        ),
        base.model_copy(
            update={
                "trial_id": "lineage",
                "history_key": "h6",
                "lineage_status": "missing",
                "lineage_digest": None,
            }
        ),
        base.model_copy(
            update={"trial_id": "unredacted", "history_key": "h7", "redaction_status": "unredacted"}
        ),
        base.model_copy(
            update={
                "trial_id": "truncated",
                "history_key": "h8",
                "terminal_span_status": "truncated",
            }
        ),
        base.model_copy(
            update={"trial_id": "prohibited", "history_key": "h9", "corpus_id": "syn-funcdag-easy"}
        ),
        base.model_copy(
            update={
                "trial_id": "path",
                "history_key": "h10",
                "source_path": "../../solution/answer.txt",
            }
        ),
        base.model_copy(
            update={
                "trial_id": "quarantine",
                "history_key": "h11",
                "feature_names": ("unavailable_reason",),
            }
        ),
        base.model_copy(
            update={
                "trial_id": "feature",
                "history_key": "h12",
                "feature_names": ("invented_feature",),
            }
        ),
        base.model_copy(
            update={"trial_id": "registry", "history_key": "h13", "registry_record": None}
        ),
        base.model_copy(
            update={"trial_id": "use", "history_key": "h14", "registry_record": measurement_only}
        ),
        base.model_copy(
            update={
                "trial_id": "cas",
                "history_key": "h15",
                "source_cas_uri": "cas://sha256/" + "f" * 64,
            }
        ),
        base.model_copy(
            update={
                "trial_id": "hidden",
                "history_key": "h16",
                "messages": (
                    TrainingMessage(sequence=0, role="user", content="public"),
                    TrainingMessage(
                        sequence=1,
                        role="assistant",
                        content="reference_answer from hidden verifier",
                        visibility="hidden_verifier",
                    ),
                ),
            }
        ),
        base.model_copy(
            update={
                "trial_id": "secret",
                "history_key": "h17",
                "messages": _messages("sk-proj-abcdefghijklmnopqrstuvwxyz012345"),
            }
        ),
        base.model_copy(
            update={
                "trial_id": "digest",
                "history_key": "h18",
                "source_artifact_digest": _sha("mutated-after-admission"),
            }
        ),
        base.model_copy(
            update={
                "job_id": "sk-proj-metadataabcdefghijklmnopqrstuvwxyz",
                "trial_id": "metadata-secret",
                "history_key": "h19",
            }
        ),
        base.model_copy(
            update={
                "trial_id": "trainer-tool",
                "history_key": "h20",
                "tools": (
                    TrainingTool(
                        function=TrainingFunctionDefinition(
                            name="bad_schema",
                            parameters={
                                "type": "object",
                                "properties": {"token_ids": {"type": "array"}},
                            },
                        )
                    ),
                ),
            }
        ),
    ]

    result = export_training_dataset(cases, tmp_path / "excluded")

    assert result.records == ()
    assert _reason_set(result) >= {
        "missing_admissibility",
        "capture_incomplete",
        "environment_integrity_failed",
        "evaluator_missing",
        "reward_only_without_semantic_evidence",
        "lineage_not_immutable",
        "invalid_lineage_digest",
        "unredacted_prompt",
        "truncated_terminal_span",
        "prohibited_corpus",
        "unsafe_source_path",
        "quarantined_feature",
        "unregistered_feature",
        "missing_registry_record",
        "training_use_not_allowed",
        "source_cas_digest_mismatch",
        "hidden_verifier_leakage",
        "secret_detected",
        "source_digest_mismatch",
        "trainer_only_material",
    }
    serialized = result.exclusions_path.read_text(encoding="utf-8")
    assert "reference_answer" not in serialized
    assert "abcdefghijklmnopqrstuvwxyz012345" not in serialized
    assert "sk-proj-metadata" not in serialized
    assert "../../solution/answer.txt" not in serialized


def test_latest_history_content_dedup_and_cluster_split_are_fail_closed(tmp_path: Path) -> None:
    old = _source(
        1, history_key="history-latest", history_revision=1, messages=_messages("old response")
    )
    latest = _source(
        2, history_key="history-latest", history_revision=2, messages=_messages("latest response")
    )
    duplicate = _source(
        3,
        history_key="history-duplicate",
        cluster_key="different-cluster",
        messages=_messages("latest response"),
    )
    conflict_train = _source(
        4,
        history_key="history-conflict-train",
        cluster_key="shared-assignment",
        split="train",
        messages=_messages("train conflict"),
    )
    conflict_test = _source(
        5,
        history_key="history-conflict-test",
        cluster_key="shared-assignment",
        split="test",
        messages=_messages("test conflict"),
    )
    ambiguous_a = _source(
        6, history_key="history-ambiguous", history_revision=3, messages=_messages("candidate a")
    )
    ambiguous_b = _source(
        7, history_key="history-ambiguous", history_revision=3, messages=_messages("candidate b")
    )

    result = export_training_dataset(
        [conflict_test, ambiguous_b, duplicate, old, latest, conflict_train, ambiguous_a],
        tmp_path / "dedup",
    )

    assert len(result.records) == 2
    assert {record.source.trial_id for record in result.records} == {latest.trial_id}
    assert {
        "superseded_history",
        "duplicate_content",
        "cluster_split_conflict",
        "ambiguous_latest_history",
    } <= _reason_set(result)
    assert "shared-assignment" not in {record.source.cluster_key for record in result.records}
    assert result.manifest.test_split.record_count == 0


def test_manifest_tampering_paths_and_symlinks_fail_closed(tmp_path: Path) -> None:
    result = export_training_dataset([_source()], tmp_path / "output")

    payload = result.manifest.model_dump(mode="json")
    payload["exclusion_count"] += 1
    with pytest.raises(ValidationError, match="manifest digest mismatch"):
        TrainingDatasetManifestV1.model_validate(payload)

    dataset_payload = result.manifest.model_dump(mode="json")
    dataset_payload["dataset_digest"] = _sha("tampered")
    dataset_payload["manifest_digest"] = _manifest_digest(dataset_payload)
    with pytest.raises(ValidationError, match="dataset digest mismatch"):
        TrainingDatasetManifestV1.model_validate(dataset_payload)

    authority_payload = result.manifest.model_dump(mode="json")
    authority_payload["source_refs"][0]["trial_admissibility_decision"] = "rejected"
    authority_payload["manifest_digest"] = _manifest_digest(authority_payload)
    with pytest.raises(ValidationError, match="accepted causal authority"):
        TrainingDatasetManifestV1.model_validate(authority_payload)

    cas_payload = result.manifest.model_dump(mode="json")
    cas_payload["manifest_path"] = None
    cas_payload["cas_uri"] = "cas://sha256/" + "f" * 64
    cas_payload["manifest_digest"] = _manifest_digest(cas_payload)
    with pytest.raises(ValidationError, match="CAS URI must bind manifest_digest"):
        TrainingDatasetManifestV1.model_validate(cas_payload)

    with pytest.raises(ValidationError, match="canonical and relative"):
        TrainingSplitRefV1(
            path="../train.jsonl", digest=DIGEST, cluster_key_digest=DIGEST, record_count=1
        )

    symlink_target = tmp_path / "symlink-target"
    symlink_target.mkdir()
    symlink_destination = tmp_path / "symlink-output"
    symlink_destination.symlink_to(symlink_target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink destination"):
        export_training_dataset([_source()], symlink_destination)

    file_target = tmp_path / "outside.jsonl"
    file_target.write_text("outside", encoding="utf-8")
    split_link = result.root / "train.jsonl"
    split_link.unlink()
    split_link.symlink_to(file_target)
    with pytest.raises(ValueError, match="symlink output path"):
        export_training_dataset([_source()], result.root)
    assert file_target.read_text(encoding="utf-8") == "outside"
