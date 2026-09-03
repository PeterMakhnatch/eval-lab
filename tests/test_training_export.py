from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import evallab.training_export as training_export
from evallab.artifact_authority import (
    VERIFIER_IMPLEMENTATION_DIGEST,
    AuthorityRefusal,
    reverify_authority,
)
from evallab.evidence_store import EvidenceLocator, archive_evidence, evidence_locator
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
    TrainingFunctionCall,
    TrainingFunctionDefinition,
    TrainingMessage,
    TrainingReceiptSourceV1,
    TrainingSourceReceiptV1,
    TrainingSplitRefV1,
    TrainingTool,
    TrainingToolCall,
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

def _authority_digest(payload: dict[str, object]) -> str:
    body = {key: value for key, value in payload.items() if key != "authority_digest"}
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
    provenance_root: Path,
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
    normalized_messages = messages or _messages()
    source_bytes = (
        json.dumps(
            {
                "messages": [message.model_dump(mode="json") for message in normalized_messages],
                "trial_id": trial_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    source_digest = _sha_bytes(source_bytes)
    admissibility = _admissibility(
        trial_id=trial_id,
        source_digest=source_digest,
        registry_record=registry_record,
    )
    authority_bytes = (
        json.dumps(
            admissibility.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    lineage_bytes = (
        json.dumps(
            {"source_digest": source_digest, "trial_id": trial_id},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    identity = hashlib.sha256(source_bytes).hexdigest()[:12]
    source_root = provenance_root / f"receipt-input-{index}-{identity}"
    (source_root / "agent").mkdir(parents=True)
    (source_root / "analysis").mkdir()
    (source_root / "agent/trajectory.json").write_bytes(source_bytes)
    (source_root / "analysis/lineage.json").write_bytes(lineage_bytes)
    (source_root / "trial-admissibility.json").write_bytes(authority_bytes)
    store_root = provenance_root / "cas"
    archive = archive_evidence(
        source_root,
        store_root,
        record_id=f"receipt-{index}-{identity}",
        kind="source-receipt",
    )
    locator = evidence_locator(store_root.resolve(), archive)
    receipt = TrainingSourceReceiptV1(
        cas_record_id=archive.record_id,
        cas_record_digest=archive.record_digest,
        cas_content_digest=archive.content_digest,
        evidence_locator=locator,
        admissibility_record_path="trial-admissibility.json",
        admissibility_record_digest=_sha_bytes(authority_bytes),
        source_digests=(
            TrainingReceiptSourceV1(
                source_kind="lineage",
                path="analysis/lineage.json",
                digest=_sha_bytes(lineage_bytes),
            ),
            TrainingReceiptSourceV1(
                source_kind="trajectory",
                path="agent/trajectory.json",
                digest=source_digest,
            ),
        ),
        consumer_digest=_sha_bytes((REPO_ROOT / "src/evallab/training_export.py").read_bytes()),
        created_at=NOW,
    )
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
        source_cas_uri=archive.uri,
        lineage_path="analysis/lineage.json",
        lineage_digest=_sha_bytes(lineage_bytes),
        source_receipt=receipt,
        registry_record=registry_record,
        admissibility=admissibility,
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
        messages=normalized_messages,
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(root.iterdir())}


def _reason_set(result) -> set[str]:
    return {reason for exclusion in result.exclusions for reason in exclusion.reasons}


def test_fixture_export_is_byte_identical_and_authority_bound(tmp_path: Path) -> None:
    source = _source(tmp_path)
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
    assert source_ref.source_authority.level == "bytes-verified"
    assert (
        source_ref.source_authority.verifier_implementation_digest
        == VERIFIER_IMPLEMENTATION_DIGEST
    )
    assert source_ref.source_authority.admissibility_binding is not None
    assert (
        source_ref.source_authority.admissibility_binding.artifact_kind
        == "trajectory"
    )
    assert source_ref.lineage_authority.level == "bytes-verified"
    assert source_ref.trial_admissibility_record_authority.level == "bytes-verified"
    assert source.source_receipt is not None
    for authority in (
        source_ref.source_authority,
        source_ref.lineage_authority,
        source_ref.trial_admissibility_record_authority,
    ):
        reopened = reverify_authority(
            authority,
            expected_verifier_digest=VERIFIER_IMPLEMENTATION_DIGEST,
            store_root=source.source_receipt.evidence_locator.store_root,
        )
        assert not isinstance(reopened, AuthorityRefusal)
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
    base = _source(tmp_path)
    measurement_only_payload = base.registry_record.model_dump(mode="json")
    measurement_only_payload["allowed_uses"] = ["measurement"]
    measurement_only = TaskRegistryRecord.model_validate(measurement_only_payload)
    assert base.source_receipt is not None
    forged_digest = _sha("forged-trajectory")
    forged_content_digest = _sha("forged-cas-content")
    source_locator = base.source_receipt.evidence_locator
    forged_locator = EvidenceLocator(
        store_root=source_locator.store_root,
        kind=source_locator.kind,
        record_id=source_locator.record_id,
        expected_record_digest=source_locator.expected_record_digest,
        expected_content_digest=forged_content_digest,
    )
    forged_receipt = base.source_receipt.model_copy(
        update={
            "cas_content_digest": forged_content_digest,
            "evidence_locator": forged_locator,
            "source_digests": tuple(
                item.model_copy(update={"digest": forged_digest})
                if item.source_kind == "trajectory"
                else item
                for item in base.source_receipt.source_digests
            ),
        }
    )
    missing_authority_receipt = base.source_receipt.model_copy(
        update={"admissibility_record_path": "missing-admissibility.json"}
    )
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
        base.model_copy(
            update={
                "trial_id": "missing-receipt",
                "history_key": "h21",
                "source_receipt": None,
            }
        ),
        base.model_copy(
            update={
                "trial_id": "none-digest",
                "history_key": "h22",
                "source_artifact_digest": None,
            }
        ),
        base.model_copy(
            update={
                "trial_id": "malformed-digest",
                "history_key": "h23",
                "source_artifact_digest": "not-a-digest",
            }
        ),
        base.model_copy(
            update={
                "trial_id": "forged-receipt",
                "history_key": "h24",
                "source_artifact_digest": forged_digest,
                "source_cas_uri": ("cas://sha256/" + forged_content_digest.removeprefix("sha256:")),
                "source_receipt": forged_receipt,
            }
        ),
        base.model_copy(
            update={
                "trial_id": "missing-authority-record",
                "history_key": "h25",
                "source_receipt": missing_authority_receipt,
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
        "invalid_lineage_digest",
        "invalid_source_digest",
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
        "missing_trusted_provenance",
        "receipt_admissibility_unavailable",
        "receipt_digest_mismatch",
    }
    serialized = result.exclusions_path.read_text(encoding="utf-8")
    assert "reference_answer" not in serialized
    assert "abcdefghijklmnopqrstuvwxyz012345" not in serialized
    assert "sk-proj-metadata" not in serialized
    assert "../../solution/answer.txt" not in serialized


def test_latest_history_content_dedup_and_cluster_split_are_fail_closed(tmp_path: Path) -> None:
    old = _source(
        tmp_path,
        1,
        history_key="history-latest",
        history_revision=1,
        messages=_messages("old response"),
    )
    latest = _source(
        tmp_path,
        2,
        history_key="history-latest",
        history_revision=2,
        messages=_messages("latest response"),
    )
    duplicate = _source(
        tmp_path,
        3,
        history_key="history-duplicate",
        cluster_key="different-cluster",
        messages=_messages("latest response"),
    )
    conflict_train = _source(
        tmp_path,
        4,
        history_key="history-conflict-train",
        cluster_key="shared-assignment",
        split="train",
        messages=_messages("train conflict"),
    )
    conflict_test = _source(
        tmp_path,
        5,
        history_key="history-conflict-test",
        cluster_key="shared-assignment",
        split="test",
        messages=_messages("test conflict"),
    )
    ambiguous_a = _source(
        tmp_path,
        6,
        history_key="history-ambiguous",
        history_revision=3,
        messages=_messages("candidate a"),
    )
    ambiguous_b = _source(
        tmp_path,
        7,
        history_key="history-ambiguous",
        history_revision=3,
        messages=_messages("candidate b"),
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


def test_contiguous_sequences_and_exact_tool_linkage_are_required(
    tmp_path: Path,
) -> None:
    call = TrainingToolCall(
        id="call-1",
        function=TrainingFunctionCall(name="read_public_fixture", arguments='{"name":"fixture"}'),
    )
    valid_messages = (
        TrainingMessage(sequence=0, role="user", content="Read the fixture."),
        TrainingMessage(sequence=1, role="assistant", content="", tool_calls=(call,)),
        TrainingMessage(
            sequence=2,
            role="tool",
            content="fixture body",
            name="read_public_fixture",
            tool_call_id="call-1",
        ),
        TrainingMessage(sequence=3, role="assistant", content="Fixture read."),
    )
    valid = _source(tmp_path, 30, messages=valid_messages)
    accepted = export_training_dataset([valid], tmp_path / "valid-tools")
    assert len(accepted.records) == 3

    duplicate_call = call.model_copy(update={"id": "duplicate"})
    negatives = (
        valid.model_copy(
            update={
                "history_key": "gap",
                "messages": tuple(
                    message.model_copy(update={"sequence": 4}) if message.sequence == 3 else message
                    for message in valid_messages
                ),
            }
        ),
        valid.model_copy(
            update={
                "history_key": "dangling",
                "messages": valid_messages[:2],
            }
        ),
        valid.model_copy(
            update={
                "history_key": "duplicate-call",
                "messages": (
                    TrainingMessage(sequence=0, role="user", content="Read."),
                    TrainingMessage(
                        sequence=1,
                        role="assistant",
                        content="",
                        tool_calls=(duplicate_call, duplicate_call),
                    ),
                ),
            }
        ),
        valid.model_copy(
            update={
                "history_key": "orphan",
                "messages": (
                    TrainingMessage(sequence=0, role="user", content="Read."),
                    TrainingMessage(
                        sequence=1,
                        role="tool",
                        content="orphan",
                        tool_call_id="ghost",
                    ),
                    TrainingMessage(sequence=2, role="assistant", content="Done."),
                ),
            }
        ),
    )
    excluded = export_training_dataset(negatives, tmp_path / "invalid-tools")
    assert excluded.records == ()
    assert {"invalid_message_sequence", "invalid_tool_linkage"} <= _reason_set(excluded)


def test_publication_is_immutable_and_whole_directory_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path, 31)
    existing_file = tmp_path / "existing-file"
    existing_file.write_bytes(b"keep")
    with pytest.raises(FileExistsError, match="destination already exists"):
        export_training_dataset([source], existing_file)
    assert existing_file.read_bytes() == b"keep"

    existing_directory = tmp_path / "existing-directory"
    existing_directory.mkdir()
    with pytest.raises(FileExistsError, match="destination already exists"):
        export_training_dataset([source], existing_directory)
    assert list(existing_directory.iterdir()) == []

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    symlink_parent = tmp_path / "symlink-parent"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink destination chain"):
        export_training_dataset([source], symlink_parent / "output")
    assert not (real_parent / "output").exists()

    original_write = training_export._write_staged_file
    calls = 0

    def fail_second_write(path: Path, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected staged write failure")
        original_write(path, payload)

    monkeypatch.setattr(training_export, "_write_staged_file", fail_second_write)
    destination = tmp_path / "partial"
    with pytest.raises(OSError, match="injected staged write failure"):
        export_training_dataset([source], destination)
    assert not os.path.lexists(destination)
    staged = list(tmp_path.glob(".partial.staging-*"))
    assert len(staged) == 1
    assert list(staged[0].iterdir())


def test_manifest_tampering_paths_and_symlinks_fail_closed(tmp_path: Path) -> None:
    source = _source(tmp_path)
    result = export_training_dataset([source], tmp_path / "output")

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

    reordered_payload = result.manifest.model_dump(mode="json")
    reordered_payload["representation_counts"] = {
        "episode_steps": 1,
        "prompt_response_sft": 1,
    }
    reordered_payload["manifest_digest"] = _manifest_digest(reordered_payload)
    with pytest.raises(ValidationError, match="canonical_set_mismatch"):
        TrainingDatasetManifestV1.model_validate(reordered_payload)

    alias_payload = result.manifest.model_dump(mode="json")

    authority_binding_payload = result.manifest.model_dump(mode="json")
    source_authority = authority_binding_payload["source_refs"][0]["source_authority"]
    source_authority["artifact"]["digest"] = _sha("forged-source")
    source_authority["authority_digest"] = _authority_digest(source_authority)
    authority_binding_payload["manifest_digest"] = _manifest_digest(
        authority_binding_payload
    )
    with pytest.raises(ValidationError, match="source authority binding mismatch"):
        TrainingDatasetManifestV1.model_validate(authority_binding_payload)
    alias_payload["exclusions_path"] = "./exclusions.jsonl"
    alias_payload["manifest_digest"] = _manifest_digest(alias_payload)
    with pytest.raises(ValidationError, match="exact canonical path"):
        TrainingDatasetManifestV1.model_validate(alias_payload)

    with pytest.raises(ValidationError, match="canonical and relative"):
        TrainingSplitRefV1(
            path="../train.jsonl", digest=DIGEST, cluster_key_digest=DIGEST, record_count=1
        )

    symlink_target = tmp_path / "symlink-target"
    symlink_target.mkdir()
    symlink_destination = tmp_path / "symlink-output"
    symlink_destination.symlink_to(symlink_target, target_is_directory=True)
    with pytest.raises(FileExistsError, match="destination already exists"):
        export_training_dataset([source], symlink_destination)

    file_target = tmp_path / "outside.jsonl"
    file_target.write_text("outside", encoding="utf-8")
    split_link = result.root / "train.jsonl"
    split_link.unlink()
    split_link.symlink_to(file_target)
    with pytest.raises(FileExistsError, match="destination already exists"):
        export_training_dataset([source], result.root)
    assert file_target.read_text(encoding="utf-8") == "outside"
