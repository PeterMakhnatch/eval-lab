"""Focused tests for digest assertion hardening across interpretation, retrieval, authoring, and autonomous research."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from evallab.autonomous_research import (
    ResearchIterationV1,
    ResearchRunTraceV1,
    ScoreScaleBindingV1,
    ScoreScaleVerificationResult,
    extract_autonomous_research_features,
)
from evallab.interpretation.feature_registry import TRAJECTORY_FEATURE_REGISTRY
from evallab.interpretation.trajectory_runtime import (
    DETERMINISTIC_GATE_ORDER,
    RedactionPolicy,
    build_evidence_pack,
    build_trajectory_ir,
    canonical_json_digest,
    load_campaign_analysis_manifest,
)
from evallab.lance import LanceIndexManifest
from evallab.registry import compute_task_digests, task_directory_digest

# ---------------------------------------------------------------------------
# F1: Analysis Configuration Digests in trajectory_runtime
# ---------------------------------------------------------------------------


def _compute_canonical_producer_digests() -> dict[str, str]:
    from evallab.interpretation.trajectory_runtime import _sha256_file

    return {
        "ir_builder": _sha256_file(Path(build_trajectory_ir.__code__.co_filename)),
        "pack_builder": _sha256_file(Path(build_evidence_pack.__code__.co_filename)),
        "acceptance_policy": canonical_json_digest(
            {
                "auto_acceptance_enabled": False,
                "gate_order": list(DETERMINISTIC_GATE_ORDER),
            }
        ),
    }


def _compute_canonical_feature_registry_digest() -> str:
    return canonical_json_digest(
        [
            asdict(f)
            for f in sorted(
                TRAJECTORY_FEATURE_REGISTRY.all_features().values(), key=lambda x: x.column_name
            )
        ]
    )


def test_trajectory_runtime_analysis_config_matching_digests_pass(tmp_path: Path) -> None:
    """Declared analysis config digests that match canonical values must pass validation."""
    canonical_frd = _compute_canonical_feature_registry_digest()
    canonical_producers = _compute_canonical_producer_digests()
    canonical_cohort = canonical_json_digest({"policy": "tb3_analysis_ready_cohort_v1"})
    canonical_redaction = RedactionPolicy().compute_digest()

    manifest_file = tmp_path / "manifest.json"
    manifest_data = {
        "campaign": "test-campaign",
        "commit_sha": "abc1234",
        "authorizing_actor": "analyst",
        "cas_store_root": "derived/evidence-cas",
        "items": [],
        "analysis_config": {
            "feature_registry_digest": canonical_frd,
            "producer_digests": canonical_producers,
            "cohort_policy_digest": canonical_cohort,
            "redaction_policy_digest": canonical_redaction,
        },
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    manifest = load_campaign_analysis_manifest(manifest_file)
    assert manifest.analysis_config.feature_registry_digest == canonical_frd
    assert manifest.analysis_config.producer_digests == canonical_producers


def test_trajectory_runtime_analysis_config_absent_synthesizes_canonical(tmp_path: Path) -> None:
    """Absent or None analysis_config synthesizes canonical digests for backwards compatibility."""
    manifest_file = tmp_path / "manifest.json"
    manifest_data = {
        "campaign": "test-campaign",
        "commit_sha": "abc1234",
        "authorizing_actor": "analyst",
        "cas_store_root": "derived/evidence-cas",
        "items": [],
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    manifest = load_campaign_analysis_manifest(manifest_file)
    assert (
        manifest.analysis_config.feature_registry_digest
        == _compute_canonical_feature_registry_digest()
    )
    assert manifest.analysis_config.producer_digests == _compute_canonical_producer_digests()


@pytest.mark.parametrize("invalid_config", [[], "invalid-string", 123, True])
def test_trajectory_runtime_analysis_config_non_dict_raises(
    tmp_path: Path, invalid_config: Any
) -> None:
    """Non-dict analysis_config declaration must fail closed with a clear ValueError."""
    manifest_file = tmp_path / "manifest.json"
    manifest_data = {
        "campaign": "test-campaign",
        "items": [],
        "analysis_config": invalid_config,
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    with pytest.raises(ValueError, match="analysis_config must be a dictionary when provided"):
        load_campaign_analysis_manifest(manifest_file)


def test_trajectory_runtime_stale_feature_registry_digest_raises(tmp_path: Path) -> None:
    """Declared feature registry digest that differs from in-tree registry must fail closed."""
    manifest_file = tmp_path / "manifest.json"
    manifest_data = {
        "campaign": "test-campaign",
        "items": [],
        "analysis_config": {
            "feature_registry_digest": "sha256:" + "0" * 64,
        },
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    with pytest.raises(ValueError, match="declared feature_registry_digest mismatch"):
        load_campaign_analysis_manifest(manifest_file)


def test_trajectory_runtime_producer_digests_missing_key_raises(tmp_path: Path) -> None:
    """Declared producer digests missing required producer keys must fail closed."""
    manifest_file = tmp_path / "manifest.json"
    manifest_data = {
        "campaign": "test-campaign",
        "items": [],
        "analysis_config": {
            "producer_digests": {"ir_builder": "sha256:" + "1" * 64},
        },
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    with pytest.raises(ValueError, match="missing="):
        load_campaign_analysis_manifest(manifest_file)


def test_trajectory_runtime_producer_digests_extra_key_raises(tmp_path: Path) -> None:
    """Declared producer digests with extra unknown keys must fail closed."""
    canonical_producers = _compute_canonical_producer_digests()
    tampered_producers = dict(canonical_producers)
    tampered_producers["unauthorized_producer"] = "sha256:" + "2" * 64

    manifest_file = tmp_path / "manifest.json"
    manifest_data = {
        "campaign": "test-campaign",
        "items": [],
        "analysis_config": {
            "producer_digests": tampered_producers,
        },
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    with pytest.raises(ValueError, match="extra="):
        load_campaign_analysis_manifest(manifest_file)


def test_trajectory_runtime_producer_digests_value_mismatch_raises(tmp_path: Path) -> None:
    """Declared producer digest with wrong hash value must fail closed."""
    canonical_producers = _compute_canonical_producer_digests()
    tampered_producers = dict(canonical_producers)
    tampered_producers["ir_builder"] = "sha256:" + "f" * 64

    manifest_file = tmp_path / "manifest.json"
    manifest_data = {
        "campaign": "test-campaign",
        "items": [],
        "analysis_config": {
            "producer_digests": tampered_producers,
        },
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    with pytest.raises(ValueError, match="declared producer digest for 'ir_builder' mismatch"):
        load_campaign_analysis_manifest(manifest_file)


# ---------------------------------------------------------------------------
# F2: Lance Manifest Digest Verification in lance.py
# ---------------------------------------------------------------------------


def test_lance_index_manifest_valid_roundtrip() -> None:
    """Valid LanceIndexManifest with matching index_digest must deserialize cleanly."""
    expected_digest = LanceIndexManifest.compute_index_digest(
        table_name="trajectories",
        snapshot_digest="sha256:" + "a" * 64,
        candidate_pool_digest="sha256:" + "b" * 64,
        embedder_digest="sha256:" + "c" * 64,
        redaction_policy_digest="sha256:" + "d" * 64,
        row_count=100,
    )
    raw = {
        "table_name": "trajectories",
        "snapshot_digest": "sha256:" + "a" * 64,
        "candidate_pool_digest": "sha256:" + "b" * 64,
        "embedder_id": "fast-embed",
        "embedder_version": "1.0",
        "embedder_digest": "sha256:" + "c" * 64,
        "redaction_policy_digest": "sha256:" + "d" * 64,
        "row_count": 100,
        "index_digest": expected_digest,
        "decision_eligible": False,
    }

    manifest = LanceIndexManifest.from_dict(raw)
    assert manifest.index_digest == expected_digest
    assert manifest.table_name == "trajectories"

    # JSON roundtrip
    json_str = manifest.to_json()
    manifest_from_json = LanceIndexManifest.from_json(json_str)
    assert manifest_from_json.index_digest == expected_digest


def test_lance_index_manifest_tampered_index_digest_raises() -> None:
    """Deserializing LanceIndexManifest with tampered index_digest must fail closed."""
    raw = {
        "table_name": "trajectories",
        "snapshot_digest": "sha256:" + "a" * 64,
        "candidate_pool_digest": "sha256:" + "b" * 64,
        "embedder_id": "fast-embed",
        "embedder_version": "1.0",
        "embedder_digest": "sha256:" + "c" * 64,
        "redaction_policy_digest": "sha256:" + "d" * 64,
        "row_count": 100,
        "index_digest": "sha256:" + "9" * 64,  # tampered
        "decision_eligible": False,
    }

    with pytest.raises(ValueError, match="LanceIndexManifest index_digest mismatch"):
        LanceIndexManifest.from_dict(raw)


def test_lance_index_manifest_tampered_field_raises() -> None:
    """Modifying a payload field (e.g. row_count) without re-signing index_digest must fail."""
    expected_digest = LanceIndexManifest.compute_index_digest(
        table_name="trajectories",
        snapshot_digest="sha256:" + "a" * 64,
        candidate_pool_digest="sha256:" + "b" * 64,
        embedder_digest="sha256:" + "c" * 64,
        redaction_policy_digest="sha256:" + "d" * 64,
        row_count=100,
    )
    raw = {
        "table_name": "trajectories",
        "snapshot_digest": "sha256:" + "a" * 64,
        "candidate_pool_digest": "sha256:" + "b" * 64,
        "embedder_id": "fast-embed",
        "embedder_version": "1.0",
        "embedder_digest": "sha256:" + "c" * 64,
        "redaction_policy_digest": "sha256:" + "d" * 64,
        "row_count": 999,  # tampered from 100
        "index_digest": expected_digest,
    }

    with pytest.raises(ValueError, match="LanceIndexManifest index_digest mismatch"):
        LanceIndexManifest.from_dict(raw)


# ---------------------------------------------------------------------------
# F3: Authoring vs Registry Task Directory Digest
# ---------------------------------------------------------------------------


def test_authoring_tree_digest_ignores_transient_files(tmp_path: Path) -> None:
    """Canonical task directory digest in authoring must ignore .DS_Store, __pycache__, and .pytest_cache."""
    task_dir = tmp_path / "task-pkg"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'test'\n", encoding="utf-8")
    (task_dir / "instruction.md").write_text("# Test\n", encoding="utf-8")

    digest_clean = task_directory_digest(task_dir)

    # Inject transient files
    (task_dir / ".DS_Store").write_bytes(b"\x00\x00\x01\x00")
    pycache = task_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "module.cpython-312.pyc").write_bytes(b"\x00" * 32)
    pytest_cache = task_dir / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / "README.md").write_text("pytest\n", encoding="utf-8")

    digest_with_transient = task_directory_digest(task_dir)
    assert digest_with_transient == digest_clean, (
        "transient files must not alter task package digest"
    )


def test_authoring_tree_digest_detects_real_source_change(tmp_path: Path) -> None:
    """Modifying an authoritative task file must change the computed digest."""
    task_dir = tmp_path / "task-pkg"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'test'\n", encoding="utf-8")
    inst = task_dir / "instruction.md"
    inst.write_text("# Test v1\n", encoding="utf-8")

    digest_v1 = task_directory_digest(task_dir)

    inst.write_text("# Test v2 mutated\n", encoding="utf-8")
    digest_v2 = task_directory_digest(task_dir)

    assert digest_v1 != digest_v2, "real source modification must change package digest"


# ---------------------------------------------------------------------------
# F4: Score Scale Binding Artifact Verification & Transfer Refusal
# ---------------------------------------------------------------------------


def _sha256_json(obj: Any) -> str:
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def test_score_scale_binding_verify_against_artifacts_exact_match(tmp_path: Path) -> None:
    """ScoreScaleBindingV1.verify_against_artifacts must return verified status when all artifacts match."""
    task_dir = tmp_path / "eval-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'eval-task'\n", encoding="utf-8")
    (task_dir / "instruction.md").write_text("# Eval Task\n", encoding="utf-8")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_eval.py").write_text("def test_it(): pass\n", encoding="utf-8")

    task_digests = compute_task_digests(task_dir)
    metric_cfg = {"metric": "precision", "k": 5, "threshold": 0.8}
    metric_digest = _sha256_json(metric_cfg)
    vis_outcome = {"split": "visible", "score": 0.85}
    hid_outcome = {"split": "hidden", "score": 0.82}

    binding = ScoreScaleBindingV1.create(
        authority_kind="benchmark_contract",
        metric_name="precision_at_5",
        direction="higher",
        task_digest=task_digests.package,
        verifier_digest=task_digests.verifier,
        metric_config_digest=metric_digest,
        visible_split_id="split_v1",
        hidden_split_id="split_h1",
        visible_outcome_binding_digest=_sha256_json(vis_outcome),
        hidden_outcome_binding_digest=_sha256_json(hid_outcome),
    )

    res = binding.verify_against_artifacts(
        task_dir=task_dir,
        metric_config=metric_cfg,
        visible_outcome=vis_outcome,
        hidden_outcome=hid_outcome,
    )
    assert isinstance(res, ScoreScaleVerificationResult)
    assert res.verified is True
    assert res.status == "verified"
    assert res.binding_digest == binding.binding_digest
    assert res.task_status == "verified"
    assert res.verifier_status == "verified"
    assert res.metric_config_status == "verified"
    assert res.visible_outcome_status == "verified"
    assert res.hidden_outcome_status == "verified"


def test_score_scale_binding_partial_artifacts_returns_unresolved() -> None:
    """Missing required artifact sources must return status='unresolved' with verified=False."""
    metric_cfg = {"metric": "accuracy", "k": 5}
    metric_digest = _sha256_json(metric_cfg)
    binding = ScoreScaleBindingV1.create(
        authority_kind="benchmark_contract",
        metric_name="accuracy",
        direction="higher",
        task_digest="sha256:" + "1" * 64,
        verifier_digest="sha256:" + "2" * 64,
        metric_config_digest=metric_digest,
        visible_split_id="split_v1",
        hidden_split_id="split_h1",
        visible_outcome_binding_digest="sha256:" + "4" * 64,
        hidden_outcome_binding_digest="sha256:" + "5" * 64,
    )

    # Only matching metric config supplied, task and outcomes missing
    res = binding.verify_against_artifacts(
        metric_config=metric_cfg,
        fail_closed=False,
    )
    assert res.verified is False
    assert res.status == "unresolved"
    assert res.metric_config_status == "verified"
    assert res.task_status == "unresolved"
    assert res.visible_outcome_status == "unresolved"
    assert res.hidden_outcome_status == "unresolved"
    assert "unresolved_components" in (res.reason or "")


def test_score_scale_binding_verify_mismatched_task_digest_raises(tmp_path: Path) -> None:
    """verify_against_artifacts must raise ValueError when task directory bytes differ."""
    task_dir = tmp_path / "eval-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'eval-task'\n", encoding="utf-8")
    (task_dir / "instruction.md").write_text("# Eval Task\n", encoding="utf-8")

    binding = ScoreScaleBindingV1.create(
        authority_kind="benchmark_contract",
        metric_name="precision_at_5",
        direction="higher",
        task_digest="sha256:" + "a" * 64,  # wrong digest
        verifier_digest="sha256:" + "b" * 64,
        metric_config_digest="sha256:" + "c" * 64,
        visible_split_id="split_v1",
        hidden_split_id="split_h1",
        visible_outcome_binding_digest="sha256:" + "d" * 64,
        hidden_outcome_binding_digest="sha256:" + "e" * 64,
    )

    with pytest.raises(ValueError, match="task_digest mismatch"):
        binding.verify_against_artifacts(task_dir=task_dir)


def test_score_scale_binding_verify_mismatched_metric_config_raises() -> None:
    """verify_against_artifacts must raise ValueError when metric configuration differs."""
    binding = ScoreScaleBindingV1.create(
        authority_kind="benchmark_contract",
        metric_name="accuracy",
        direction="higher",
        task_digest="sha256:" + "a" * 64,
        verifier_digest="sha256:" + "b" * 64,
        metric_config_digest="sha256:" + "c" * 64,
        visible_split_id="split_v1",
        hidden_split_id="split_h1",
        visible_outcome_binding_digest="sha256:" + "d" * 64,
        hidden_outcome_binding_digest="sha256:" + "e" * 64,
    )

    tampered_cfg = {"metric": "accuracy", "k": 999}
    with pytest.raises(ValueError, match="metric_config_digest mismatch"):
        binding.verify_against_artifacts(metric_config=tampered_cfg)


def test_score_scale_binding_unverified_trace_refuses_transfer_arithmetic() -> None:
    """A trace with an unverified score scale binding must deserialize cleanly but emit None transfer gap."""
    binding = ScoreScaleBindingV1.create(
        authority_kind="benchmark_contract",
        metric_name="score",
        direction="higher",
        task_digest="sha256:" + "1" * 64,
        verifier_digest="sha256:" + "2" * 64,
        metric_config_digest="sha256:" + "3" * 64,
        visible_split_id="val",
        hidden_split_id="test",
        visible_outcome_binding_digest="sha256:" + "4" * 64,
        hidden_outcome_binding_digest="sha256:" + "5" * 64,
    )
    trace = ResearchRunTraceV1(
        run_id="unverified-rsi-run",
        benchmark_family="paperbench",
        source_digest="sha256:" + "3" * 64,
        task_digest=binding.task_digest,
        verifier_digest=binding.verifier_digest,
        metric_config_digest=binding.metric_config_digest,
        visible_outcome_binding_digest=binding.visible_outcome_binding_digest,
        hidden_outcome_binding_digest=binding.hidden_outcome_binding_digest,
        score_direction="higher",
        score_scale_binding=binding,
        hidden_score=12.0,
        selected_iteration_id="v1",
        iterations=(ResearchIterationV1(iteration_id="v1", visible_score=10.0),),
    )

    # Extraction with no artifacts provided
    features = extract_autonomous_research_features(trace)
    assert features.score_scale_compatible is False
    assert features.scale_binding_status == "unresolved"
    assert features.scale_binding_unresolved_reason is not None
    assert features.visible_hidden_transfer_gap is None
    assert features.scale_binding_digest == binding.binding_digest


def test_score_scale_binding_artifact_resolver_emits_transfer(tmp_path: Path) -> None:
    """Providing a complete artifact resolver authorizes transfer gap calculation in the production path."""
    task_dir = tmp_path / "eval-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'eval-task'\n", encoding="utf-8")
    (task_dir / "instruction.md").write_text("# Eval Task\n", encoding="utf-8")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_eval.py").write_text("def test_it(): pass\n", encoding="utf-8")

    task_digests = compute_task_digests(task_dir)
    metric_cfg = {"metric": "score", "threshold": 0.5}
    vis_outcome = {"score": 10.0}
    hid_outcome = {"score": 12.0}

    binding = ScoreScaleBindingV1.create(
        authority_kind="benchmark_contract",
        metric_name="score",
        direction="higher",
        task_digest=task_digests.package,
        verifier_digest=task_digests.verifier,
        metric_config_digest=_sha256_json(metric_cfg),
        visible_split_id="val",
        hidden_split_id="test",
        visible_outcome_binding_digest=_sha256_json(vis_outcome),
        hidden_outcome_binding_digest=_sha256_json(hid_outcome),
    )
    trace = ResearchRunTraceV1(
        run_id="verified-rsi-run",
        benchmark_family="paperbench",
        source_digest="sha256:" + "3" * 64,
        task_digest=binding.task_digest,
        verifier_digest=binding.verifier_digest,
        metric_config_digest=binding.metric_config_digest,
        visible_outcome_binding_digest=binding.visible_outcome_binding_digest,
        hidden_outcome_binding_digest=binding.hidden_outcome_binding_digest,
        score_direction="higher",
        score_scale_binding=binding,
        hidden_score=12.0,
        selected_iteration_id="v1",
        iterations=(ResearchIterationV1(iteration_id="v1", visible_score=10.0),),
    )

    def resolver(t: ResearchRunTraceV1) -> dict[str, Any]:
        return {
            "task_dir": task_dir,
            "metric_config": metric_cfg,
            "visible_outcome": vis_outcome,
            "hidden_outcome": hid_outcome,
        }

    features = extract_autonomous_research_features(trace, artifact_resolver=resolver)
    assert features.score_scale_compatible is True
    assert features.scale_binding_status == "verified"
    assert features.scale_binding_task_status == "verified"
    assert features.scale_binding_verifier_status == "verified"
    assert features.scale_binding_metric_config_status == "verified"
    assert features.scale_binding_visible_outcome_status == "verified"
    assert features.scale_binding_hidden_outcome_status == "verified"
    assert features.visible_hidden_transfer_gap == 2.0


def test_score_scale_binding_another_binding_artifacts_refuses_transfer(tmp_path: Path) -> None:
    """Supplying artifacts that match binding A to a trace containing binding B must refuse transfer."""
    task_dir = tmp_path / "eval-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'eval-task'\n", encoding="utf-8")
    (task_dir / "instruction.md").write_text("# Eval Task\n", encoding="utf-8")

    task_digests = compute_task_digests(task_dir)
    metric_cfg_a = {"metric": "score_a"}
    metric_cfg_b = {"metric": "score_b"}
    vis_outcome = {"score": 10.0}
    hid_outcome = {"score": 12.0}

    binding_b = ScoreScaleBindingV1.create(
        authority_kind="benchmark_contract",
        metric_name="score_b",
        direction="higher",
        task_digest=task_digests.package,
        verifier_digest=task_digests.verifier,
        metric_config_digest=_sha256_json(metric_cfg_b),
        visible_split_id="val",
        hidden_split_id="test",
        visible_outcome_binding_digest=_sha256_json(vis_outcome),
        hidden_outcome_binding_digest=_sha256_json(hid_outcome),
    )
    trace = ResearchRunTraceV1(
        run_id="mismatch-binding-run",
        benchmark_family="paperbench",
        source_digest="sha256:" + "3" * 64,
        task_digest=binding_b.task_digest,
        verifier_digest=binding_b.verifier_digest,
        metric_config_digest=binding_b.metric_config_digest,
        visible_outcome_binding_digest=binding_b.visible_outcome_binding_digest,
        hidden_outcome_binding_digest=binding_b.hidden_outcome_binding_digest,
        score_direction="higher",
        score_scale_binding=binding_b,
        hidden_score=12.0,
        selected_iteration_id="v1",
        iterations=(ResearchIterationV1(iteration_id="v1", visible_score=10.0),),
    )

    # Pass metric_cfg_a which does not match binding_b
    features = extract_autonomous_research_features(
        trace,
        task_dir=task_dir,
        metric_config=metric_cfg_a,
        visible_outcome=vis_outcome,
        hidden_outcome=hid_outcome,
    )
    assert features.score_scale_compatible is False
    assert features.scale_binding_status == "mismatch"
    assert features.visible_hidden_transfer_gap is None
