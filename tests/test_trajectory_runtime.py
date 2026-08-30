"""Focused contract tests for Track A5 pack-only interpretation runtime.

No live models, no derived/evidence-cas, no required PostgreSQL.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from evallab.cli import parser
from evallab.database import _ingest_interpretation_artifacts
from evallab.evidence_store import archive_evidence
from evallab.interpretation.evidence_pack import OmittedRange, build_evidence_pack
from evallab.interpretation.trajectory_acceptance import (
    AUTO_ACCEPTANCE_ENABLED,
    evaluate_acceptance,
)
from evallab.interpretation.trajectory_data_quality import (
    _add_campaign_projection_joins,
    _cas_record_anti_join,
    _citation_reopen,
)
from evallab.interpretation.trajectory_hydration import (
    CitationHandle,
    RedactionPolicy,
    hydrate_citation,
)
from evallab.interpretation.trajectory_ir import build_trajectory_ir
from evallab.interpretation.trajectory_judgment import (
    TRAJECTORY_ONTOLOGY_V1_CLASSES,
    MachineJudgment,
    canonical_json_digest,
)
from evallab.analysis_capability import (
    AnalysisMethod,
    AnalysisStatus,
    AnalysisUnit,
    CampaignAnalysisConfigV1,
    CampaignAnalysisResultV1,
    CampaignAnalysisSpecV1,
    ContextCitation,
    DenominatorPolicy,
    NextRunAction,
    RefusalCode,
    RetrievalPolicyV1,
    ReviewQueueArtifactV1,
    ReviewQueueRef,
    compute_spec_digest,
    create_campaign_analysis_result,
    create_campaign_analysis_spec,
)
from evallab.interpretation.trajectory_runtime import (
    ArtifactRecord,
    CampaignAnalysisItem,
    CampaignAnalysisManifest,
    _build_review_queue_artifact,
    _data_contract_digest,
    _pack_structure_errors,
    analyze_batch,
    analyze_calibrate,
    analyze_inspect,
    analyze_trial,
    build_acceptance_decision,
    build_calibration_class_gate,
    build_campaign_report,
    build_machine_judgment,
    compute_analysis_snapshot_digest,
    evaluate_deterministic_gates,
    load_campaign_analysis_manifest,
    rebuild_interpretation_projections,
)

REPO = Path(__file__).resolve().parents[1]
REAL_INVENTORY = (
    REPO
    / "research"
    / "experiments"
    / "manifests"
    / "terminal-bench-v3-k1-gemini-low-machine-analysis-inventory.json"
)

FAKE_CITATION = "sha256:" + "ab" * 32


def _report_payload() -> dict:
    row = {
        "acceptance_enabled": False,
        "delta": 0.05,
        "n_gold": 0,
        "n_proposed_accept": 0,
        "prec_acc": None,
        "p_human": None,
        "wilson_lower_one_sided_95": None,
        "beta_lower_one_sided_95": None,
        "ci_width": None,
        "noninferiority_pass": False,
        "hold_reasons": ["acceptance_enabling_disabled"],
    }
    return {
        "schema": "calibration-report-v1",
        "calibration_version": "sha256:" + "1" * 64,
        "acceptance_enabling_allowed": False,
        "thresholds_digest": "sha256:" + "2" * 64,
        "n_items": 0,
        "n_proposed_accept": 0,
        "inter_rater": {
            "n_paired": 0,
            "cohen_kappa": None,
            "gwet_ac1": None,
            "observed_agreement": None,
            "kappa_min": 0.6,
            "alt_test_min": 0.6,
            "floor_pass": False,
        },
        "global_metrics": {
            "raw_judge_accuracy": None,
            "proposed_accept_precision": None,
            "coverage": 0.0,
            "selective_risk": None,
            "ece": None,
            "brier": None,
            "aurc": None,
            "risk_coverage": [],
            "abstention_precision": None,
            "abstention_justified_rate": None,
            "cite_valid_on_proposed_accept": None,
            "cross_judge_agreement": None,
            "cross_judge_is_not_gold": True,
        },
        "classes": {class_id: dict(row) for class_id in TRAJECTORY_ONTOLOGY_V1_CLASSES},
        "hold_summary": ["acceptance_enabling_disabled"],
    }


def _trial_tree(root: Path, *, trial_name: str, unpaired: bool = True, reward: float = 0.0) -> Path:
    trial_dir = root / trial_name
    (trial_dir / "agent").mkdir(parents=True, exist_ok=True)
    step: dict = {
        "step_id": 2,
        "timestamp": "2026-08-26T00:00:01Z",
        "source": "agent",
        "message": "working",
        "tool_calls": [
            {
                "tool_call_id": "call_1",
                "function_name": "exec",
                "arguments": {"cmd": "true"},
            }
        ],
    }
    if not unpaired:
        step["observation"] = {"results": [{"source_call_id": "call_1", "content": "ok"}]}
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "session_id": "session-runtime-test",
        "agent": {"name": "test-agent", "version": "0", "model_name": "none"},
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-08-26T00:00:00Z",
                "source": "user",
                "message": "do the task",
            },
            step,
        ],
    }
    (trial_dir / "agent" / "trajectory.json").write_text(json.dumps(trajectory), encoding="utf-8")
    result = {
        "id": str(uuid4()),
        "task_name": "test/runtime-task",
        "trial_name": trial_name,
        "started_at": "2026-08-26T00:00:00Z",
        "finished_at": "2026-08-26T00:00:02Z",
        "exception_info": None,
        "verifier_result": {"rewards": {"reward": reward}},
    }
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return trial_dir


def _archive_trial(trial_dir: Path, store: Path, record_id: str) -> str:
    archive = archive_evidence(trial_dir, store, record_id=record_id, kind="trial")
    return archive.uri


def _cas_backed_ir_pack(tmp_path: Path, *, trial_name: str):
    trial_dir = _trial_tree(tmp_path, trial_name=trial_name, unpaired=False)
    store = tmp_path / "cas"
    cas_uri = _archive_trial(trial_dir, store, trial_name)
    result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    item = _cohort_row(
        role="primary",
        trial_name=trial_name,
        trial_id=str(result["id"]),
        cas_uri=cas_uri,
        quality="pass",
    )
    ir = build_trajectory_ir(item, repo_root=tmp_path, store_root=store)
    pack = build_evidence_pack(ir, repo_root=tmp_path, store_root=store)
    return ir, pack, store, cas_uri


def _cohort_row(*, role: str, trial_name: str, trial_id: str, cas_uri: str, quality: str) -> dict:
    findings = ["ATIF_UNPAIRED_TOOL_CALL"] if quality == "warn" else []
    return {
        "role": role,
        "spec_name": f"spec-{trial_name}",
        "spec_id": f"spec-{trial_id[:8]}",
        "job_name": f"job-{trial_name}",
        "job_id": str(uuid4()),
        "trial_name": trial_name,
        "trial_id": trial_id,
        "task_name": "test/runtime-task",
        "task_digest": None,
        "verifier_digest": None,
        "quality_status": quality,
        "quality_findings": findings,
        "cas_uri": cas_uri,
        "ingestion_status": "projected_to_postgres_and_parquet",
    }


def test_analyze_commands_registered_and_legacy_worker_plan_remains() -> None:
    root = parser()
    analyze = next(
        action
        for action in root._actions
        if getattr(action, "dest", None) == "command" and hasattr(action, "choices")
    )
    assert "analyze" in analyze.choices
    analyze_parser = analyze.choices["analyze"]
    sub = next(
        action
        for action in analyze_parser._actions
        if getattr(action, "dest", None) == "analyze_command"
    )
    for name in ("trial", "batch", "inspect", "calibrate", "quality", "worker-plan"):
        assert name in sub.choices


def test_real_inventory_adapter_accounts_seven_attempts_five_cohort() -> None:
    manifest = load_campaign_analysis_manifest(REAL_INVENTORY)
    assert len(manifest.items) == 7
    assert len(manifest.cohort_items()) == 5
    assert len(manifest.accounting_items()) == 2
    roles = {item.attempt_role for item in manifest.items}
    assert roles == {"primary", "retry", "control", "quarantined_attempt"}
    assert sum(1 for item in manifest.items if item.attempt_role == "retry") == 1
    assert sum(1 for item in manifest.items if item.attempt_role == "control") == 1
    assert sum(1 for item in manifest.items if item.attempt_role == "quarantined_attempt") == 1
    assert manifest.accounting["analysis_cohort"] == 5
    assert manifest.accounting["executions"] == 7
    assert manifest.accounting["unresolved"] == 0
    names = {item.trial_name for item in manifest.cohort_items()}
    assert names == {
        "bun-sourcemap-leak__vaurWUd",
        "cargo-flight-dispatch__z5vUTct",
        "embedding-drift-monitor__JcUjDcj",
        "foodstuff-beta-activity__GfEgM6V",
        "ico-path-patch__5dkQZr5",
    }


def test_analyze_trial_zero_model_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("model called")

    monkeypatch.setattr("evallab.evidence.facts.run_trial_analysis", boom)
    monkeypatch.setattr("evallab.analysis_worker.default_worker", boom)

    trial_dir = _trial_tree(tmp_path, trial_name="zero-model")
    result = analyze_trial(
        trial_dir,
        repo_root=tmp_path,
        store_root=tmp_path / "cas",
        output_dir=tmp_path / "interpretation",
        derived_root=tmp_path / "derived",
    )
    assert result["decision"] in {"abstained", "rejected"}
    assert result["judgment_id"].startswith("sha256:")
    sidecar = next(
        (tmp_path / "interpretation" / result["trial_id"]).rglob("machine_judgment.json")
    )
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["producer_kind"] == "deterministic_abstention"
    assert payload["validity"] == "insufficient_evidence"


def test_five_tb3_mini_batch_accounting(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="shared-trial")
    store = tmp_path / "cas"
    cas_uri = _archive_trial(trial_dir, store, "shared-trial")
    cohort = [
        _cohort_row(
            role="infrastructure_retry_1",
            trial_name="bun-sourcemap-leak__vaurWUd",
            trial_id=str(uuid4()),
            cas_uri=cas_uri,
            quality="warn",
        ),
        _cohort_row(
            role="spec_2",
            trial_name="cargo-flight-dispatch__z5vUTct",
            trial_id=str(uuid4()),
            cas_uri=cas_uri,
            quality="warn",
        ),
        _cohort_row(
            role="spec_3",
            trial_name="embedding-drift-monitor__JcUjDcj",
            trial_id=str(uuid4()),
            cas_uri=cas_uri,
            quality="warn",
        ),
        _cohort_row(
            role="spec_4",
            trial_name="foodstuff-beta-activity__GfEgM6V",
            trial_id=str(uuid4()),
            cas_uri=cas_uri,
            quality="warn",
        ),
        _cohort_row(
            role="spec_5",
            trial_name="ico-path-patch__5dkQZr5",
            trial_id=str(uuid4()),
            cas_uri=cas_uri,
            quality="pass",
        ),
    ]
    ledger = [
        {
            "role": "free_control",
            "spec_name": None,
            "spec_id": None,
            "job_name": "control-job",
            "job_id": str(uuid4()),
            "trial_name": "agentabstain-act-oracle-control__8wEFfNY",
            "trial_id": str(uuid4()),
            "task_name": "agentabstain/control",
            "task_digest": "n/a",
            "verifier_digest": "n/a",
            "quality_status": "no_atif",
            "quality_findings": [],
            "cas_uri": cas_uri,
        },
        {
            "role": "quarantined_auth_attempt",
            "spec_name": "tb3-k1-bun-sourcemap-leak-gemini-low",
            "spec_id": "spec-q",
            "job_name": "quarantined-job",
            "job_id": str(uuid4()),
            "trial_name": "bun-sourcemap-leak__BKZ7rHT",
            "trial_id": str(uuid4()),
            "task_name": "terminal-bench/bun-sourcemap-leak",
            "quality_status": "quarantine",
            "quality_findings": ["INFRA_EXCEPTION"],
            "cas_uri": cas_uri,
        },
    ]
    inventory = {
        "schema_version": "1.0",
        "inventory_type": "machine_analysis_input_inventory",
        "campaign": "terminal-bench-v3-k1-gemini-low-screen",
        "commit_sha": "test",
        "authorizing_actor": "test",
        "cas_store_root": str(store),
        "accounting": {
            "total_planned_specs": 5,
            "total_executed_trials": 7,
            "valid_analysis_ready_trials": 5,
            "quarantined_infrastructure_attempts": 1,
            "free_local_controls": 1,
            "unresolved_evidence_count": 0,
        },
        "analysis_cohort_5_trials": cohort,
        "controls_and_quarantine_ledger": ledger,
    }
    inv_path = tmp_path / "inventory.json"
    inv_path.write_text(json.dumps(inventory), encoding="utf-8")
    report = analyze_batch(
        inv_path,
        repo_root=tmp_path,
        store_root=store,
        output_dir=tmp_path / "interpretation",
        derived_root=tmp_path / "derived",
    )
    assert report["cohort_accounted"] == 5
    assert report["accepted"] == 0
    assert report["role_counts"]["retry"] == 1
    assert report["role_counts"]["control"] == 1
    assert report["role_counts"]["quarantined_attempt"] == 1
    assert report["reason_counts"]
    assert report["coverage_gap_counts"]["judge_execution_disabled"] == 5
    assert report["report_id"].startswith("sha256:")
    assert report["report_digest"].startswith("sha256:")
    assert report["report_cas_uri"].startswith("cas://sha256/")
    report_sidecar = json.loads(Path(report["report_artifact_path"]).read_text(encoding="utf-8"))
    assert report_sidecar["report_id"] == report["report_id"]
    assert len(report["source_refs"]) == 5
    for ref in report["source_refs"]:
        assert ref["source_cas_uri"] == cas_uri
        assert ref["artifact_cas_uri"].startswith("cas://sha256/")
        assert ref["ir_digest"].startswith("sha256:")
        assert ref["pack_digest"].startswith("sha256:")
        assert ref["judgment_id"].startswith("sha256:")
        assert ref["decision_id"].startswith("sha256:")

    rerun = analyze_batch(
        inv_path,
        repo_root=tmp_path,
        store_root=store,
        output_dir=tmp_path / "interpretation",
        derived_root=tmp_path / "derived",
    )
    assert rerun["report_id"] == report["report_id"]
    assert rerun["report_cas_uri"] == report["report_cas_uri"]


def test_missing_cas_hard_stops(tmp_path: Path) -> None:
    missing = "cas://sha256/" + "00" * 32
    with pytest.raises(RuntimeError, match="missing_cas"):
        analyze_trial(
            {"cas_uri": missing, "trial_id": "missing", "trial_name": "missing"},
            repo_root=tmp_path,
            store_root=tmp_path / "cas",
            output_dir=tmp_path / "interpretation",
        )


def test_mapping_without_cas_hard_stops(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="mapping target requires cas_uri"):
        analyze_trial(
            {"trial_id": "local-only", "trial_name": "local-only"},
            repo_root=tmp_path,
            store_root=tmp_path / "cas",
            output_dir=tmp_path / "interpretation",
        )


def test_invalid_cas_trial_name_is_integrity_error(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="cas-integrity")
    store = tmp_path / "cas"
    cas_uri = _archive_trial(trial_dir, store, "cas-integrity")

    with pytest.raises(RuntimeError, match="cas_integrity_error"):
        analyze_trial(
            {
                "cas_uri": cas_uri,
                "trial_id": "cas-integrity",
                "trial_name": "../outside",
            },
            repo_root=tmp_path,
            store_root=store,
            output_dir=tmp_path / "interpretation",
        )


def test_parquet_rebuild_preserves_identities(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="parquet-trial")
    output = tmp_path / "interpretation"
    derived = tmp_path / "derived"
    result = analyze_trial(
        trial_dir,
        repo_root=tmp_path,
        store_root=tmp_path / "cas",
        output_dir=output,
        derived_root=derived,
    )
    judgment_parquet = derived / "machine_judgments" / "machine_judgments.parquet"
    decision_parquet = derived / "acceptance_decisions" / "acceptance_decisions.parquet"
    assert judgment_parquet.is_file()
    assert decision_parquet.is_file()
    judgment_parquet.unlink()
    decision_parquet.unlink()
    rebuilt = rebuild_interpretation_projections(
        output,
        derived,
        store_root=tmp_path / "cas",
    )
    assert all(path.is_file() for path in rebuilt)
    import pyarrow.parquet as pq

    judgments = pq.read_table(derived / "machine_judgments" / "machine_judgments.parquet")
    decisions = pq.read_table(derived / "acceptance_decisions" / "acceptance_decisions.parquet")
    artifacts = pq.read_table(
        derived / "interpretation_artifacts" / "interpretation_artifacts.parquet"
    )
    assert result["judgment_id"] in judgments.column("judgment_id").to_pylist()
    assert result["decision_id"] in decisions.column("decision_id").to_pylist()
    assert result["pack_digest"] in judgments.column("pack_digest").to_pylist()
    assert artifacts.num_rows == 5
    assert set(artifacts.column("cas_uri").to_pylist()) == {result["artifact_cas_uri"]}


def test_projection_rebuild_skips_partial_sidecar_set(tmp_path: Path) -> None:
    partial = tmp_path / "interpretation" / "trial" / "decision"
    partial.mkdir(parents=True)
    (partial / "acceptance_decision.json").write_text("{}")

    paths = rebuild_interpretation_projections(
        tmp_path / "interpretation",
        tmp_path / "derived",
        store_root=tmp_path / "cas",
    )

    import pyarrow.parquet as pq

    assert all(path.is_file() for path in paths)
    assert pq.read_table(paths[0]).num_rows == 0


def test_projection_rebuild_rejects_tampered_complete_sidecars(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="projection-tamper")
    output = tmp_path / "interpretation"
    store = tmp_path / "cas"
    result = analyze_trial(
        trial_dir,
        repo_root=tmp_path,
        store_root=store,
        output_dir=output,
        derived_root=tmp_path / "derived",
    )
    artifact_dir = output / result["trial_id"] / result["decision_id"].removeprefix("sha256:")
    judgment_path = artifact_dir / "machine_judgment.json"
    judgment = json.loads(judgment_path.read_text(encoding="utf-8"))
    judgment["finding_summary"] = "tampered after archival"
    judgment_path.write_text(json.dumps(judgment), encoding="utf-8")

    paths = rebuild_interpretation_projections(
        output,
        tmp_path / "rebuilt",
        store_root=store,
    )

    import pyarrow.parquet as pq

    assert pq.read_table(paths[0]).num_rows == 0
    assert pq.read_table(paths[1]).num_rows == 0
    assert pq.read_table(paths[2]).num_rows == 0


def test_projection_rebuild_rejects_corrupt_interpretation_blob(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="projection-corrupt-cas")
    output = tmp_path / "interpretation"
    store = tmp_path / "cas"
    result = analyze_trial(
        trial_dir,
        repo_root=tmp_path,
        store_root=store,
        output_dir=output,
        derived_root=tmp_path / "derived",
    )
    record_path = store / "records" / "interpretation" / f"{result['decision_id']}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    blob = store / record["blob_path"]
    content = bytearray(blob.read_bytes())
    content[len(content) // 2] ^= 0x01
    blob.write_bytes(content)

    paths = rebuild_interpretation_projections(
        output,
        tmp_path / "rebuilt",
        store_root=store,
    )

    import pyarrow.parquet as pq

    assert pq.read_table(paths[0]).num_rows == 0
    assert pq.read_table(paths[1]).num_rows == 0
    assert pq.read_table(paths[2]).num_rows == 0


def test_unresolved_citation_rejects(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="cite-fail")
    store = tmp_path / "cas"
    built_ir = build_trajectory_ir(trial_dir, repo_root=tmp_path, store_root=store)
    built_pack = build_evidence_pack(
        built_ir, trial_dir=trial_dir, repo_root=tmp_path, store_root=store
    )
    judgment = build_machine_judgment(built_pack, built_ir, [])
    body = judgment.model_dump(mode="json")
    body["citation_ids"] = [FAKE_CITATION]
    body.pop("produced_at")
    body.pop("judgment_id")
    body.pop("judgment_digest")
    judgment_id = canonical_json_digest(body)
    forged = MachineJudgment(
        judgment_id=judgment_id,
        judgment_digest=canonical_json_digest({**body, "judgment_id": judgment_id}),
        produced_at=judgment.produced_at,
        **body,
    )
    gates = evaluate_deterministic_gates(
        ir=built_ir, pack=built_pack, judgment=forged, cas_store=store
    )
    c1 = next(gate for gate in gates if gate.gate_id == "C1_resolve")
    assert c1.status == "fail"
    assert c1.reason_code == "citation_unresolved"
    baseline = build_acceptance_decision(
        judgment,
        built_pack,
        built_ir,
        calibration_class_gate=build_calibration_class_gate(),
        cas_store=store,
    )
    assert baseline.calibration_class_gate.class_id == ("unlabeled_deterministic_abstention")
    assert "calibration_report_unavailable" in baseline.calibration_class_gate.hold_reasons
    decision = evaluate_acceptance(
        judgment_ids=[forged.judgment_id],
        pack_digest=built_pack.pack_digest,
        deterministic_gates=gates,
        cross_judge=baseline.cross_judge,
        calibration_class_gate=build_calibration_class_gate(),
        policy_digest=canonical_json_digest({"k": "v"}),
        proposed_next_check=None,
    )
    assert decision.decision == "rejected"


def test_tampered_ir_digest_fails_schema_gate(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="tampered-ir")
    store = tmp_path / "cas"
    built_ir = build_trajectory_ir(trial_dir, repo_root=tmp_path, store_root=store)
    built_pack = build_evidence_pack(
        built_ir,
        trial_dir=trial_dir,
        repo_root=tmp_path,
        store_root=store,
    )
    judgment = build_machine_judgment(built_pack, built_ir, [])
    tampered_ir = replace(built_ir, final_verdict="PASS")

    gates = evaluate_deterministic_gates(
        ir=tampered_ir,
        pack=built_pack,
        judgment=judgment,
        cas_store=store,
    )

    schema_gate = next(gate for gate in gates if gate.gate_id == "schema_valid")
    assert schema_gate.status == "fail"
    assert schema_gate.reason_code == "schema_invalid"


def _recompute_pack_digest(pack) -> object:
    payload = pack.to_dict()
    payload.pop("pack_digest", None)
    return replace(pack, pack_digest=_data_contract_digest(payload))


def _schema_gate(ir, pack, judgment, cas_store):
    gates = evaluate_deterministic_gates(ir=ir, pack=pack, judgment=judgment, cas_store=cas_store)
    return next(gate for gate in gates if gate.gate_id == "schema_valid")


def test_rebuilt_ir_pack_schema_valid_passes(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="schema-valid")
    store = tmp_path / "cas"
    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path, store_root=store)
    pack = build_evidence_pack(
        ir,
        trial_dir=trial_dir,
        repo_root=tmp_path,
        store_root=store,
    )
    judgment = build_machine_judgment(pack, ir, [])
    schema_gate = _schema_gate(ir, pack, judgment, store)
    assert schema_gate.status == "pass"
    assert schema_gate.reason_code is None
    assert "ir_digest" in pack.source_digests
    assert pack.source_digests["ir_digest"] == ir.ir_digest
    assert pack.source_digests["redaction_profile_digest"] == pack.redaction_profile_digest
    assert AUTO_ACCEPTANCE_ENABLED is False


def test_pack_specific_redaction_policy_rehydrates_selected_cas_bytes(
    tmp_path: Path,
) -> None:
    ir, _, store, _ = _cas_backed_ir_pack(tmp_path, trial_name="custom-redaction-policy")
    policy = RedactionPolicy(redact_secrets=False, max_display_bytes=5)
    pack = build_evidence_pack(
        ir,
        repo_root=tmp_path,
        store_root=store,
        policy=policy,
    )
    judgment = build_machine_judgment(pack, ir, [])

    assert pack.redaction_policy_config == policy.to_config()
    assert _schema_gate(ir, pack, judgment, store).status == "pass"

    tampered_config = dict(pack.redaction_policy_config)
    tampered_config["max_display_bytes"] = 6
    tampered = _recompute_pack_digest(
        replace(pack, redaction_policy_config=tampered_config)
    )
    tampered_judgment = build_machine_judgment(tampered, ir, [])
    tampered_gate = _schema_gate(ir, tampered, tampered_judgment, store)
    assert (tampered_gate.status, tampered_gate.reason_code) == (
        "fail",
        "schema_invalid",
    )


def test_redaction_policy_digest_binds_regex_flags() -> None:
    case_sensitive = RedactionPolicy(secret_patterns=(re.compile("secret"),))
    case_insensitive = RedactionPolicy(
        secret_patterns=(re.compile("secret", re.IGNORECASE),)
    )

    assert case_sensitive.compute_digest() != case_insensitive.compute_digest()


def test_campaign_quality_reopens_with_pack_redaction_policy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    trajectory_path = source / "agent/trajectory.json"
    trajectory_path.parent.mkdir(parents=True)
    trajectory_path.write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "step_id": 1,
                        "source": "agent",
                        "message": "token=abcdefghijklmnop",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = tmp_path / "cas"
    archive = archive_evidence(source, store, record_id="quality-custom", kind="trial")
    source_digest = f"sha256:{hashlib.sha256(trajectory_path.read_bytes()).hexdigest()}"
    policy = RedactionPolicy(redact_secrets=False, max_display_bytes=5)
    handle = CitationHandle(
        citation_id="quality-custom-citation",
        trial_id="quality-custom",
        source_path="agent/trajectory.json",
        source_sha256=source_digest,
        raw_cas_uri=archive.uri,
        step_id=1,
        target_type="step",
        redaction_profile_digest=policy.compute_digest(),
    )
    hydrated = hydrate_citation(handle, repo_root=store, policy=policy)
    ir = {
        "source_digests": {
            "cas_uri": archive.uri,
            "source_sha256": source_digest,
        }
    }
    pack = {
        "pack_version": "1.0",
        "redaction_profile_digest": policy.compute_digest(),
        "redaction_policy_config": policy.to_config(),
        "selected_windows": [
            {
                "events": [
                    {
                        "source_citation": handle.to_dict(),
                        "hydrated_content": hydrated.redacted_content,
                    }
                ]
            }
        ],
        "omitted_ranges": [],
    }

    quality_reopen = _citation_reopen(
        ir=ir,
        pack=pack,
        store_root=store,
        quarantined=False,
    )

    assert quality_reopen["status"] == "present", quality_reopen["reason_counts"]
    assert quality_reopen["available"] == 1
    assert quality_reopen["integrity_failures"] == 0


def test_wrong_pack_ir_digest_fails_schema_gate(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="wrong-ir-digest")
    store = tmp_path / "cas"
    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path, store_root=store)
    pack = build_evidence_pack(
        ir,
        trial_dir=trial_dir,
        repo_root=tmp_path,
        store_root=store,
    )
    judgment = build_machine_judgment(pack, ir, [])
    mutated = dict(pack.source_digests)
    mutated["ir_digest"] = "sha256:" + "ff" * 32
    tampered = _recompute_pack_digest(replace(pack, source_digests=mutated))
    schema_gate = _schema_gate(ir, tampered, judgment, store)
    assert schema_gate.status == "fail"
    assert schema_gate.reason_code == "schema_invalid"


def test_wrong_pack_source_digest_fails_schema_gate(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="wrong-source")
    store = tmp_path / "cas"
    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path, store_root=store)
    pack = build_evidence_pack(
        ir,
        trial_dir=trial_dir,
        repo_root=tmp_path,
        store_root=store,
    )
    judgment = build_machine_judgment(pack, ir, [])
    mutated = dict(pack.source_digests)
    mutated["source_sha256"] = "sha256:" + "00" * 32
    tampered = _recompute_pack_digest(replace(pack, source_digests=mutated))
    schema_gate = _schema_gate(ir, tampered, judgment, store)
    assert schema_gate.status == "fail"
    assert schema_gate.reason_code == "schema_invalid"


def test_wrong_pack_redaction_digest_fails_schema_gate(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="wrong-redaction")
    store = tmp_path / "cas"
    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path, store_root=store)
    pack = build_evidence_pack(
        ir,
        trial_dir=trial_dir,
        repo_root=tmp_path,
        store_root=store,
    )
    judgment = build_machine_judgment(pack, ir, [])
    mutated = dict(pack.source_digests)
    mutated["redaction_profile_digest"] = "sha256:" + "11" * 32
    tampered = _recompute_pack_digest(replace(pack, source_digests=mutated))
    schema_gate = _schema_gate(ir, tampered, judgment, store)
    assert schema_gate.status == "fail"
    assert schema_gate.reason_code == "schema_invalid"


def test_pack_source_extra_or_missing_key_fails_schema_gate(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="extra-missing")
    store = tmp_path / "cas"
    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path, store_root=store)
    pack = build_evidence_pack(
        ir,
        trial_dir=trial_dir,
        repo_root=tmp_path,
        store_root=store,
    )
    judgment = build_machine_judgment(pack, ir, [])
    extra = dict(pack.source_digests)
    extra["unexpected_digest"] = "sha256:" + "22" * 32
    extra_pack = _recompute_pack_digest(replace(pack, source_digests=extra))
    extra_gate = _schema_gate(ir, extra_pack, judgment, store)
    assert extra_gate.status == "fail"
    assert extra_gate.reason_code == "schema_invalid"

    missing = dict(pack.source_digests)
    missing.pop("ir_digest")
    missing_pack = _recompute_pack_digest(replace(pack, source_digests=missing))
    missing_gate = _schema_gate(ir, missing_pack, judgment, store)
    assert missing_gate.status == "fail"
    assert missing_gate.reason_code == "schema_invalid"


def test_omitted_range_structure_fails_c10_schema_and_pack_complete(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="tampered-omission", unpaired=False)
    trajectory_path = trial_dir / "agent" / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    for step_id in range(3, 12):
        trajectory["steps"].append(
            {
                "step_id": step_id,
                "timestamp": f"2026-08-26T00:00:{step_id:02d}Z",
                "source": "agent",
                "message": f"routine step {step_id}",
            }
        )
    trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")
    store = tmp_path / "cas"
    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path, store_root=store)
    pack = build_evidence_pack(
        ir,
        trial_dir=trial_dir,
        repo_root=tmp_path,
        store_root=store,
    )
    assert pack.omitted_ranges
    assert not _pack_structure_errors(ir, pack)
    judgment = build_machine_judgment(pack, ir, [])
    first = replace(pack.omitted_ranges[0], event_ids=())
    tampered = _recompute_pack_digest(
        replace(pack, omitted_ranges=(first, *pack.omitted_ranges[1:]))
    )

    gates = evaluate_deterministic_gates(
        ir=ir,
        pack=tampered,
        judgment=judgment,
        cas_store=store,
    )
    c10 = next(gate for gate in gates if gate.gate_id == "C10_omitted")
    schema = next(gate for gate in gates if gate.gate_id == "schema_valid")
    complete = next(gate for gate in gates if gate.gate_id == "pack_complete")
    assert (c10.status, c10.reason_code) == ("unknown", "omitted_unreopenable")
    assert (schema.status, schema.reason_code) == ("fail", "schema_invalid")
    assert (complete.status, complete.reason_code) == ("unknown", "pack_incomplete")


def test_selected_window_payload_must_match_canonical_ir_event(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="tampered-selected-event", unpaired=False)
    store = tmp_path / "cas"
    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path, store_root=store)
    pack = build_evidence_pack(
        ir,
        trial_dir=trial_dir,
        repo_root=tmp_path,
        store_root=store,
    )
    assert pack.selected_windows
    window = pack.selected_windows[0]
    event = dict(window.events[0])
    event["event_type"] = "forged"
    tampered_window = replace(window, events=(event, *window.events[1:]))
    tampered = _recompute_pack_digest(
        replace(pack, selected_windows=(tampered_window, *pack.selected_windows[1:]))
    )

    assert "selected_event_payload_mismatch" in _pack_structure_errors(ir, tampered)


def test_selected_and_omitted_step_ranges_cannot_overlap(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="overlapping-pack", unpaired=False)
    trajectory_path = trial_dir / "agent/trajectory.json"
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    trajectory["steps"][1]["tool_calls"].append(
        {
            "tool_call_id": "call_2",
            "function_name": "exec",
            "arguments": {"cmd": "printf second"},
        }
    )
    trajectory["steps"][1]["observation"]["results"].append(
        {"source_call_id": "call_2", "content": "second"}
    )
    trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")
    store = tmp_path / "cas"
    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path, store_root=store)
    pack = build_evidence_pack(
        ir,
        trial_dir=trial_dir,
        repo_root=tmp_path,
        store_root=store,
    )
    window = next(
        window
        for window in pack.selected_windows
        if sum(event["step_index"] == 2 for event in window.events) >= 2
    )
    omitted_payload = next(event for event in reversed(window.events) if event["step_index"] == 2)
    kept = tuple(
        event for event in window.events if event["event_id"] != omitted_payload["event_id"]
    )
    kept_steps = [event["step_index"] for event in kept]
    tampered_window = replace(
        window,
        step_start=min(kept_steps),
        step_end=max(kept_steps),
        event_count=len(kept),
        events=kept,
    )
    source_event = next(
        event for event in ir.events if event.event_id == omitted_payload["event_id"]
    )
    omitted = OmittedRange(
        range_id=1,
        step_start=source_event.step_index,
        step_end=source_event.step_index,
        event_count=1,
        event_ids=(source_event.event_id,),
        action_families=(source_event.action_family,),
        summary="forged overlapping omission",
        omitted_content_digest=canonical_json_digest([source_event.to_dict()]),
        reopening_citation=source_event.source_citation,
    )
    windows = tuple(
        tampered_window if candidate.window_id == window.window_id else candidate
        for candidate in pack.selected_windows
    )
    tampered = _recompute_pack_digest(
        replace(pack, selected_windows=windows, omitted_ranges=(omitted,))
    )

    errors = _pack_structure_errors(ir, tampered)
    assert "selected_omitted_range_overlap" in errors
    judgment = build_machine_judgment(tampered, ir, [])
    schema = _schema_gate(ir, tampered, judgment, store)
    assert (schema.status, schema.reason_code) == ("fail", "schema_invalid")


def test_selected_hydrated_content_is_bound_to_cas_bytes(tmp_path: Path) -> None:
    ir, pack, store, _ = _cas_backed_ir_pack(tmp_path, trial_name="selected-hydration-binding")
    window = pack.selected_windows[0]
    forged_event = dict(window.events[0])
    forged_event["hydrated_content"] = "forged bytes"
    forged_window = replace(window, events=(forged_event, *window.events[1:]))
    tampered = _recompute_pack_digest(
        replace(pack, selected_windows=(forged_window, *pack.selected_windows[1:]))
    )
    judgment = build_machine_judgment(tampered, ir, [])

    schema = _schema_gate(ir, tampered, judgment, store)
    assert (schema.status, schema.reason_code) == ("fail", "schema_invalid")


def test_corrupt_cas_is_an_integrity_failure_in_citation_gates(tmp_path: Path) -> None:
    ir, pack, store, cas_uri = _cas_backed_ir_pack(tmp_path, trial_name="corrupt-citation-cas")
    event = ir.events[0]
    citation_id = canonical_json_digest(event.source_citation.to_dict())
    judgment = build_machine_judgment(pack, ir, []).model_copy(
        update={
            "producer_kind": "model",
            "validity": "supported",
            "citation_ids": [citation_id],
            "earliest_supported_event_id": event.event_id,
        }
    )
    digest = cas_uri.removeprefix("cas://sha256/")
    blob = store / "blobs/sha256" / digest[:2] / f"{digest}.tar.gz"
    content = bytearray(blob.read_bytes())
    content[len(content) // 2] ^= 0x01
    blob.write_bytes(content)

    gates = evaluate_deterministic_gates(
        ir=ir,
        pack=pack,
        judgment=judgment,
        cas_store=store,
    )
    c2 = next(gate for gate in gates if gate.gate_id == "C2_digest")
    c3 = next(gate for gate in gates if gate.gate_id == "C3_source")
    assert (c2.status, c2.reason_code) == ("fail", "cas_integrity_error")
    assert (c3.status, c3.reason_code) == ("fail", "cas_integrity_error")


def _campaign_result(item, *, job_id: str | None = None) -> dict:
    return {
        "job_id": job_id or item.job_id,
        "trial_id": item.trial_id,
        "decision": "abstained",
        "reason_codes": [],
        "coverage_gaps": [],
        "source_cas_uri": item.cas_uri,
        "artifact_cas_uri": "cas://sha256/" + "1" * 64,
        "ir_digest": "sha256:" + "2" * 64,
        "pack_digest": "sha256:" + "3" * 64,
        "judgment_id": "sha256:" + "4" * 64,
        "decision_id": "sha256:" + "5" * 64,
    }


def test_campaign_report_rejects_duplicate_and_foreign_results() -> None:
    manifest = load_campaign_analysis_manifest(REAL_INVENTORY)
    results = [_campaign_result(item) for item in manifest.cohort_items()]

    with pytest.raises(ValueError, match="duplicate identities"):
        build_campaign_report(manifest, [*results, dict(results[0])])

    foreign = [*results]
    foreign[0] = _campaign_result(
        manifest.cohort_items()[0],
        job_id="foreign-job",
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        build_campaign_report(manifest, foreign)


def test_projection_joins_keep_same_trial_id_separate_by_job_and_count_orphans(
    tmp_path: Path,
) -> None:
    derived = tmp_path / "derived"
    trial_id = "shared-trial"
    identities = [
        {
            "job_id": "job-1",
            "pack_digest": "sha256:" + "1" * 64,
            "judgment_id": "sha256:" + "2" * 64,
            "decision_id": "sha256:" + "3" * 64,
        },
        {
            "job_id": "job-2",
            "pack_digest": "sha256:" + "4" * 64,
            "judgment_id": "sha256:" + "5" * 64,
            "decision_id": "sha256:" + "6" * 64,
        },
    ]
    for identity in identities:
        path = (
            derived
            / "parquet"
            / f"job_id={identity['job_id']}"
            / f"trial_id={trial_id}"
            / "trial_facts.parquet"
        )
        path.parent.mkdir(parents=True)
        pq.write_table(
            pa.Table.from_pylist([{"job_id": identity["job_id"], "trial_id": trial_id}]),
            path,
        )

    artifact_rows = [
        {
            "job_id": identity["job_id"],
            "trial_id": trial_id,
            "kind": kind,
            "pack_digest": identity["pack_digest"],
            "judgment_id": identity["judgment_id"],
            "decision_id": identity["decision_id"],
        }
        for identity in identities
        for kind in ("ir", "pack", "judgment", "decision", "interpretation")
    ]
    artifact_rows.append(
        {
            "job_id": "foreign-job",
            "trial_id": trial_id,
            "kind": "ir",
            "pack_digest": "sha256:" + "7" * 64,
            "judgment_id": "sha256:" + "8" * 64,
            "decision_id": "sha256:" + "9" * 64,
        }
    )
    artifact_path = derived / "interpretation_artifacts" / "interpretation_artifacts.parquet"
    artifact_path.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(artifact_rows), artifact_path)

    judgment_path = derived / "machine_judgments" / "machine_judgments.parquet"
    judgment_path.parent.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "judgment_id": identity["judgment_id"],
                    "pack_digest": identity["pack_digest"],
                }
                for identity in identities
            ]
        ),
        judgment_path,
    )
    decision_path = derived / "acceptance_decisions" / "acceptance_decisions.parquet"
    decision_path.parent.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "decision_id": identity["decision_id"],
                    "pack_digest": identity["pack_digest"],
                    "judgment_ids_json": json.dumps([identity["judgment_id"]]),
                }
                for identity in identities
            ]
        ),
        decision_path,
    )
    projections = {
        "trial_facts": {"status": "present"},
        "interpretation_artifacts": {"status": "present"},
        "machine_judgments": {"status": "present"},
        "acceptance_decisions": {"status": "present"},
    }
    trials = [
        {
            "job_id": identity["job_id"],
            "trial_id": trial_id,
            "cohort_included": True,
            "sidecar_identity": identity,
        }
        for identity in identities
    ]

    _add_campaign_projection_joins(
        projections,
        derived_root=derived,
        trials=trials,
    )

    artifacts = projections["interpretation_artifacts"]
    assert artifacts["current_row_count"] == artifacts["expected_current_row_count"] == 10
    assert artifacts["historical_row_count"] == 0
    assert artifacts["orphan_row_count"] == 1
    assert projections["machine_judgments"]["current_row_count"] == 2
    assert projections["acceptance_decisions"]["current_row_count"] == 2


def test_cas_record_anti_join_counts_campaign_scoped_orphan(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "result.json").write_text("{}")
    store = tmp_path / "cas"
    archive = archive_evidence(
        source,
        store,
        record_id="job-current",
        kind="job",
    )
    item = (
        load_campaign_analysis_manifest(REAL_INVENTORY)
        .cohort_items()[0]
        .model_copy(
            update={
                "job_id": "job-current",
                "job_name": "display-name-only",
                "cas_uri": archive.uri,
            }
        )
    )
    record = store / "records/job/job-current.json"
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["uri"] = "cas://sha256/" + "f" * 64
    record.write_text(json.dumps(payload), encoding="utf-8")

    joined = _cas_record_anti_join(store, [item])

    assert joined["matched_uri_count"] == 0
    assert joined["missing_uris"] == [archive.uri]
    assert joined["status"] == "invalid"
    assert joined["orphan_record_count"] == 1


def test_cas_record_anti_join_ignores_foreign_record_with_expected_uri(
    tmp_path: Path,
) -> None:
    expected_uri = "cas://sha256/" + "a" * 64
    item = (
        load_campaign_analysis_manifest(REAL_INVENTORY)
        .cohort_items()[0]
        .model_copy(
            update={
                "job_id": "job-current",
                "job_name": "display-name-only",
                "cas_uri": expected_uri,
            }
        )
    )
    record = tmp_path / "cas/records/job/job-foreign.json"
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps(
            {"kind": "job", "record_id": "job-foreign", "uri": expected_uri}
        ),
        encoding="utf-8",
    )

    joined = _cas_record_anti_join(tmp_path / "cas", [item])

    assert joined["status"] == "missing"
    assert joined["matched_uri_count"] == 0
    assert joined["missing_uris"] == [expected_uri]


def test_cas_record_anti_join_accepts_campaign_bound_legacy_job_name(
    tmp_path: Path,
) -> None:
    expected_uri = "cas://sha256/" + "b" * 64
    item = (
        load_campaign_analysis_manifest(REAL_INVENTORY)
        .cohort_items()[0]
        .model_copy(
            update={
                "job_id": "job-current",
                "job_name": "legacy-job-name",
                "cas_uri": expected_uri,
            }
        )
    )
    record = tmp_path / "cas/records/job/legacy-job-name.json"
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps(
            {"kind": "job", "record_id": "legacy-job-name", "uri": expected_uri}
        ),
        encoding="utf-8",
    )

    joined = _cas_record_anti_join(tmp_path / "cas", [item])

    assert joined["status"] == "present"
    assert joined["matched_uri_count"] == 1
    assert joined["missing_uris"] == []


def test_cas_record_anti_join_binds_each_job_to_its_expected_uri(
    tmp_path: Path,
) -> None:
    uri_a = "cas://sha256/" + "a" * 64
    uri_b = "cas://sha256/" + "b" * 64
    base_items = load_campaign_analysis_manifest(REAL_INVENTORY).cohort_items()[:2]
    items = [
        base_items[0].model_copy(
            update={"job_id": "job-a", "job_name": "legacy-a", "cas_uri": uri_a}
        ),
        base_items[1].model_copy(
            update={"job_id": "job-b", "job_name": "legacy-b", "cas_uri": uri_b}
        ),
    ]
    records = tmp_path / "cas/records/job"
    records.mkdir(parents=True)
    (records / "job-a.json").write_text(
        json.dumps({"kind": "job", "record_id": "job-a", "uri": uri_b}),
        encoding="utf-8",
    )
    (records / "job-b.json").write_text(
        json.dumps({"kind": "job", "record_id": "job-b", "uri": uri_a}),
        encoding="utf-8",
    )

    joined = _cas_record_anti_join(tmp_path / "cas", items)

    assert joined["status"] == "invalid"
    assert joined["matched_record_count"] == 0
    assert joined["missing_record_ids"] == ["job-a", "job-b"]
    assert joined["orphan_record_count"] == 2


def test_cas_record_anti_join_rejects_non_job_namespace_match(tmp_path: Path) -> None:
    expected_uri = "cas://sha256/" + "c" * 64
    item = (
        load_campaign_analysis_manifest(REAL_INVENTORY)
        .cohort_items()[0]
        .model_copy(
            update={
                "job_id": "job-current",
                "job_name": "legacy-current",
                "cas_uri": expected_uri,
            }
        )
    )
    record = tmp_path / "cas/records/interpretation/job-current.json"
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps({"kind": "job", "record_id": "job-current", "uri": expected_uri}),
        encoding="utf-8",
    )

    joined = _cas_record_anti_join(tmp_path / "cas", [item])

    assert joined["status"] == "missing"
    assert joined["missing_record_ids"] == ["job-current"]


def test_cas_record_anti_join_requires_each_shared_uri_job_record(
    tmp_path: Path,
) -> None:
    shared_uri = "cas://sha256/" + "d" * 64
    base_items = load_campaign_analysis_manifest(REAL_INVENTORY).cohort_items()[:2]
    items = [
        base_items[0].model_copy(
            update={"job_id": "job-a", "job_name": "legacy-a", "cas_uri": shared_uri}
        ),
        base_items[1].model_copy(
            update={"job_id": "job-b", "job_name": "legacy-b", "cas_uri": shared_uri}
        ),
    ]
    record = tmp_path / "cas/records/job/job-a.json"
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps({"kind": "job", "record_id": "job-a", "uri": shared_uri}),
        encoding="utf-8",
    )

    joined = _cas_record_anti_join(tmp_path / "cas", items)

    assert joined["status"] == "missing"
    assert joined["expected_record_count"] == 2
    assert joined["matched_record_count"] == 1
    assert joined["missing_record_ids"] == ["job-b"]


def test_cas_record_anti_join_rejects_duplicate_identity_records(
    tmp_path: Path,
) -> None:
    expected_uri = "cas://sha256/" + "e" * 64
    item = (
        load_campaign_analysis_manifest(REAL_INVENTORY)
        .cohort_items()[0]
        .model_copy(
            update={
                "job_id": "job-current",
                "job_name": "legacy-current",
                "cas_uri": expected_uri,
            }
        )
    )
    records = tmp_path / "cas/records/job"
    records.mkdir(parents=True)
    for record_id in ("job-current", "legacy-current"):
        (records / f"{record_id}.json").write_text(
            json.dumps({"kind": "job", "record_id": record_id, "uri": expected_uri}),
            encoding="utf-8",
        )

    joined = _cas_record_anti_join(tmp_path / "cas", [item])

    assert joined["status"] == "invalid"
    assert joined["duplicate_record_uris"] == [
        {
            "job_id": "job-current",
            "uri": expected_uri,
            "record_count": 2,
        }
    ]


def test_cas_record_anti_join_rejects_conflicting_uris_for_one_job(
    tmp_path: Path,
) -> None:
    uri_a = "cas://sha256/" + "a" * 64
    uri_b = "cas://sha256/" + "b" * 64
    base_items = load_campaign_analysis_manifest(REAL_INVENTORY).cohort_items()[:2]
    items = [
        base_items[0].model_copy(
            update={"job_id": "job-same", "job_name": "legacy-a", "cas_uri": uri_a}
        ),
        base_items[1].model_copy(
            update={"job_id": "job-same", "job_name": "legacy-b", "cas_uri": uri_b}
        ),
    ]
    records = tmp_path / "cas/records/job"
    records.mkdir(parents=True)
    (records / "job-same.json").write_text(
        json.dumps({"kind": "job", "record_id": "job-same", "uri": uri_a}),
        encoding="utf-8",
    )
    (records / "legacy-b.json").write_text(
        json.dumps({"kind": "job", "record_id": "legacy-b", "uri": uri_b}),
        encoding="utf-8",
    )

    joined = _cas_record_anti_join(tmp_path / "cas", items)

    assert joined["status"] == "invalid"
    assert joined["conflicting_expected_job_ids"] == ["job-same"]
    assert joined["matched_record_count"] == 2
    assert joined["missing_uris"] == []


def test_quality_warning_coverage_gaps(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="warn-trial", unpaired=True)
    store = tmp_path / "cas"
    cas_uri = _archive_trial(trial_dir, store, "warn-trial")
    item = _cohort_row(
        role="spec-warning",
        trial_name="warn-trial",
        trial_id=str(uuid4()),
        cas_uri=cas_uri,
        quality="warn",
    )
    result = analyze_trial(
        item,
        repo_root=tmp_path,
        store_root=store,
        output_dir=tmp_path / "interpretation",
        derived_root=tmp_path / "derived",
    )
    sidecar = next(
        (tmp_path / "interpretation" / result["trial_id"]).rglob("machine_judgment.json")
    )
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    gaps = payload["coverage_gaps"]
    assert (
        "ATIF_UNPAIRED_TOOL_CALL" in gaps or "quality_warning" in gaps or "unpaired_linkage" in gaps
    )
    decision = json.loads(
        next(
            (tmp_path / "interpretation" / result["trial_id"]).rglob("acceptance_decision.json")
        ).read_text(encoding="utf-8")
    )
    not_quarantined = next(
        gate for gate in decision["deterministic_gates"] if gate["gate_id"] == "not_quarantined"
    )
    assert not_quarantined["status"] == "pass"
    assert decision["decision"] == "abstained"
    assert len(decision["reason_codes"]) == len(set(decision["reason_codes"]))


def test_immutable_idempotency(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="idem")
    kwargs = {
        "repo_root": tmp_path,
        "store_root": tmp_path / "cas",
        "output_dir": tmp_path / "interpretation",
        "derived_root": tmp_path / "derived",
    }
    first = analyze_trial(trial_dir, **kwargs)
    second = analyze_trial(trial_dir, **kwargs)
    for key in ("ir_digest", "pack_digest", "judgment_id", "decision_id"):
        assert first[key] == second[key]
    sidecars = list((tmp_path / "interpretation").rglob("machine_judgment.json"))
    assert len(sidecars) == 1


def test_c5_entail_never_passes(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="entail")
    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path, store_root=tmp_path / "cas")
    pack = build_evidence_pack(ir, trial_dir=trial_dir, repo_root=tmp_path)
    judgment = build_machine_judgment(
        pack, ir, ["quality_warning", "a generated summary is not evidence"]
    )
    gates = evaluate_deterministic_gates(
        ir=ir, pack=pack, judgment=judgment, cas_store=tmp_path / "cas"
    )
    c5 = next(gate for gate in gates if gate.gate_id == "C5_entail")
    assert c5.status == "unknown"
    assert c5.reason_code == "entailment_disabled"


def test_analyze_calibrate_cannot_enable_classes(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_report_payload()), encoding="utf-8")
    result = analyze_calibrate(path)
    assert result["calibration_report_can_enable_acceptance"] is False
    if result.get("per_class_acceptance_enabled"):
        assert not any(result["per_class_acceptance_enabled"].values())


def test_analyze_inspect_reopens_lineage(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="inspect-trial")
    output = tmp_path / "interpretation"
    store = tmp_path / "cas"
    result = analyze_trial(
        trial_dir,
        repo_root=tmp_path,
        store_root=store,
        output_dir=output,
        derived_root=tmp_path / "derived",
    )
    inspected = analyze_inspect(result["decision_id"], output_dir=output, store_root=store)
    assert inspected["artifact_identities"]["decision_id"] == result["decision_id"]
    assert inspected["artifact_identities"]["judgment_id"] == result["judgment_id"]
    assert inspected["reason_codes"]
    assert inspected["gate_results"]


def test_uncallable_pack_still_abstains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="overflow")
    from evallab.interpretation import evidence_pack as ep

    original = ep.build_evidence_pack

    def tiny_budget(ir, **kwargs):
        kwargs["budget_tokens"] = 1
        return original(ir, **kwargs)

    monkeypatch.setattr("evallab.interpretation.trajectory_runtime.build_evidence_pack", tiny_budget)
    result = analyze_trial(
        trial_dir,
        repo_root=tmp_path,
        store_root=tmp_path / "cas",
        output_dir=tmp_path / "interpretation",
        derived_root=tmp_path / "derived",
    )
    sidecar = next(
        (tmp_path / "interpretation" / result["trial_id"]).rglob("machine_judgment.json")
    )
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["producer_kind"] == "deterministic_abstention"
    assert result["decision"] in {"abstained", "rejected"}


def test_ingest_records_identity_sql_without_postgres() -> None:
    captured: list[tuple[object, object]] = []

    class FakeConnection:
        def execute(self, sql, params=None):
            captured.append((sql, params))

    record = ArtifactRecord(
        artifact_digest="sha256:" + "11" * 32,
        kind="judgment",
        trial_id="t1",
        job_id="j1",
        content_digest="sha256:" + "22" * 32,
        artifact_path=Path("machine_judgment.json"),
        cas_uri="cas://sha256/" + "33" * 32,
        pack_digest="sha256:" + "44" * 32,
        judgment_id="sha256:" + "11" * 32,
        decision_id="sha256:" + "55" * 32,
        judgment_digest="sha256:" + "66" * 32,
        producer_kind="deterministic_abstention",
        validity="insufficient_evidence",
    )
    count = _ingest_interpretation_artifacts(FakeConnection(), [record])
    assert count == 1
    assert captured
    assert any("machine_judgments" in str(sql) for sql, _ in captured)


def test_quarantine_mapping_stops_before_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("restore_evidence called for quarantined mapping")

    monkeypatch.setattr("evallab.interpretation.trajectory_runtime.restore_evidence", boom)
    with pytest.raises(RuntimeError, match="quarantined_input"):
        analyze_trial(
            {
                "cas_uri": "cas://sha256/" + "ab" * 32,
                "trial_id": "q-trial",
                "trial_name": "q-trial",
                "quality_status": "quarantine",
            },
            repo_root=tmp_path,
            store_root=tmp_path / "cas",
            output_dir=tmp_path / "interpretation",
        )


def test_warn_mapping_still_restores_missing_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = {"restore": 0}

    def fake_restore(*_args, **_kwargs):
        called["restore"] += 1
        raise FileNotFoundError("evidence blob is missing: cas://sha256/" + "00" * 32)

    monkeypatch.setattr("evallab.interpretation.trajectory_runtime.restore_evidence", fake_restore)
    with pytest.raises(RuntimeError, match="missing_cas"):
        analyze_trial(
            {
                "cas_uri": "cas://sha256/" + "00" * 32,
                "trial_id": "warn-trial",
                "trial_name": "warn-trial",
                "quality_status": "warn",
            },
            repo_root=tmp_path,
            store_root=tmp_path / "cas",
            output_dir=tmp_path / "interpretation",
        )
    assert called["restore"] == 1


def test_corrupt_restore_is_cas_integrity_error(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="corrupt-restore")
    store = tmp_path / "cas"
    cas_uri = _archive_trial(trial_dir, store, "corrupt-restore")
    digest = cas_uri.removeprefix("cas://sha256/")
    blob = store / "blobs" / "sha256" / digest[:2] / f"{digest}.tar.gz"
    payload = bytearray(blob.read_bytes())
    payload[min(32, len(payload) - 1)] ^= 0xFF
    blob.write_bytes(payload)
    with pytest.raises(RuntimeError, match="cas_integrity_error") as excinfo:
        analyze_trial(
            {
                "cas_uri": cas_uri,
                "trial_id": "corrupt-restore",
                "trial_name": "corrupt-restore",
                "quality_status": "pass",
            },
            repo_root=tmp_path,
            store_root=store,
            output_dir=tmp_path / "interpretation",
        )
    assert "missing_cas" not in str(excinfo.value)


def test_restore_digest_mismatch_is_cas_integrity_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_restore(*_args, **_kwargs):
        raise ValueError("restored evidence digest mismatch: expected sha256:aa, got sha256:bb")

    monkeypatch.setattr("evallab.interpretation.trajectory_runtime.restore_evidence", fake_restore)
    with pytest.raises(RuntimeError, match="cas_integrity_error") as excinfo:
        analyze_trial(
            {
                "cas_uri": "cas://sha256/" + "aa" * 32,
                "trial_id": "digest-mismatch",
                "trial_name": "digest-mismatch",
                "quality_status": "pass",
            },
            repo_root=tmp_path,
            store_root=tmp_path / "cas",
            output_dir=tmp_path / "interpretation",
        )
    assert "missing_cas" not in str(excinfo.value)


def test_restore_path_escape_is_cas_integrity_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_restore(*_args, **_kwargs):
        raise ValueError("evidence archive path escapes destination: ../outside")

    monkeypatch.setattr("evallab.interpretation.trajectory_runtime.restore_evidence", fake_restore)
    with pytest.raises(RuntimeError, match="cas_integrity_error"):
        analyze_trial(
            {
                "cas_uri": "cas://sha256/" + "cc" * 32,
                "trial_id": "path-escape",
                "trial_name": "path-escape",
                "quality_status": "pass",
            },
            repo_root=tmp_path,
            store_root=tmp_path / "cas",
            output_dir=tmp_path / "interpretation",
        )


def _make_manifest_with_spec(tmp_path: Path, cas_uri: str) -> Path:
    trial_id = "rerun-trial"
    job_id = "rerun-job"
    spec = create_campaign_analysis_spec(
        spec_id="rate-success",
        method=AnalysisMethod.RATE_WILSON,
        outcome_feature="decision",
        unit=AnalysisUnit.TRIAL,
        unit_keys=("trial_id",),
        denominator_policy=DenominatorPolicy.NOT_APPLICABLE,
        ci_method="wilson",
    )
    from evallab.interpretation.feature_registry import TRAJECTORY_FEATURE_REGISTRY
    feature_reg_digest = canonical_json_digest(
        sorted([f.column_name for f in TRAJECTORY_FEATURE_REGISTRY.all_features().values()])
    )
    cohort_policy_digest = canonical_json_digest({"policy": "tb3_analysis_ready_cohort_v1"})
    redaction_policy_digest = RedactionPolicy().compute_digest()
    producer_digests = {
        "ir_builder": "sha256:" + "1" * 64,
        "pack_builder": "sha256:" + "2" * 64,
        "acceptance_policy": "sha256:" + "3" * 64,
    }

    config = CampaignAnalysisConfigV1(
        feature_registry_digest=feature_reg_digest,
        producer_digests=producer_digests,
        cohort_policy_digest=cohort_policy_digest,
        redaction_policy_digest=redaction_policy_digest,
        specs=(spec,),
    )
    item = CampaignAnalysisItem(
        source_role="analysis",
        cohort_included=True,
        attempt_role="primary",
        job_id=job_id,
        job_name=job_id,
        trial_id=trial_id,
        trial_name=trial_id,
        task_name="runtime-task",
        task_digest="sha256:" + "a" * 64,
        verifier_digest="sha256:" + "b" * 64,
        quality_status="pass",
        cas_uri=cas_uri,
        reward=1.0,
    )
    manifest_body = {
        "schema_version": "campaign-analysis-manifest/v1",
        "campaign_id": "rerun-campaign",
        "source_campaign_manifest_digest": "sha256:" + "4" * 64,
        "source_commit": "c0ffee",
        "authorizing_actor": "test",
        "cas_store_root": str(tmp_path / "cas"),
        "items": [item],
        "accounting": {
            "total_planned_specs": 1,
            "total_executed_trials": 1,
            "valid_analysis_ready_trials": 1,
            "quarantined_infrastructure_attempts": 0,
            "free_local_controls": 0,
            "unresolved_evidence_count": 0,
        },
        "analysis_config": config,
    }
    snapshot_digest = compute_analysis_snapshot_digest(manifest_body, config)
    manifest_body["analysis_snapshot_digest"] = snapshot_digest
    manifest_id = canonical_json_digest(manifest_body)
    manifest_digest = canonical_json_digest({**manifest_body, "manifest_id": manifest_id})
    manifest = CampaignAnalysisManifest(
        manifest_id=manifest_id,
        manifest_digest=manifest_digest,
        produced_at=datetime.now(UTC),
        **manifest_body,
    )
    path = tmp_path / "manifest.json"
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return path


def test_deterministic_batch_rerun_identities(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="rerun-trial")
    store = tmp_path / "cas"
    cas_uri = _archive_trial(trial_dir, store, "rerun-trial")
    manifest_path = _make_manifest_with_spec(tmp_path, cas_uri)

    report1 = analyze_batch(
        manifest_path,
        repo_root=tmp_path,
        store_root=store,
        output_dir=tmp_path / "interpretation",
        derived_root=tmp_path / "derived",
    )
    report2 = analyze_batch(
        manifest_path,
        repo_root=tmp_path,
        store_root=store,
        output_dir=tmp_path / "interpretation",
        derived_root=tmp_path / "derived",
    )
    assert report1["report_id"] == report2["report_id"]
    assert report1["report_digest"] == report2["report_digest"]
    assert report1["analysis_snapshot_digest"] == report2["analysis_snapshot_digest"]
    assert report1["report_cas_uri"] == report2["report_cas_uri"]


def test_stale_snapshot_refuses_before_trial_interpretation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trial_dir = _trial_tree(tmp_path, trial_name="stale-trial")
    store = tmp_path / "cas"
    cas_uri = _archive_trial(trial_dir, store, "stale-trial")
    manifest_path = _make_manifest_with_spec(tmp_path, cas_uri)

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["items"][0]["task_digest"] = "sha256:" + "f" * 64
    manifest_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise AssertionError("_analyze_trial_core must not be called for stale snapshot")

    monkeypatch.setattr("evallab.interpretation.trajectory_runtime._analyze_trial_core", boom)

    with pytest.raises(RuntimeError, match="STALE_SNAPSHOT"):
        analyze_batch(
            manifest_path,
            repo_root=tmp_path,
            store_root=store,
            output_dir=tmp_path / "interpretation",
            derived_root=tmp_path / "derived",
        )


def test_snapshot_digest_binds_cohort_and_config_identities() -> None:
    zero = "sha256:" + "0" * 64
    one = "sha256:" + "1" * 64
    base = {
        "campaign_id": "c1",
        "source_campaign_manifest_digest": zero,
        "source_commit": None,
        "items": [
            {
                "job_id": "j1",
                "trial_id": "t1",
                "attempt_role": "primary",
                "cohort_included": True,
                "task_digest": zero,
                "verifier_digest": zero,
                "quality_report_digest": zero,
                "cas_uri": "cas://sha256/" + "0" * 64,
                "arm": "control",
                "dose": 0,
                "seed": 1,
                "reward": 0.0,
            }
        ],
    }
    config = CampaignAnalysisConfigV1(
        feature_registry_digest=zero,
        producer_digests={"capture_policy": zero},
        cohort_policy_digest=zero,
        redaction_policy_digest=zero,
    )
    d1 = compute_analysis_snapshot_digest(base, config)

    mutations = [
        ("cas_uri", "cas://sha256/" + "1" * 64),
        ("task_digest", one),
        ("verifier_digest", one),
        ("arm", "treatment"),
        ("dose", 1),
        ("seed", 2),
        ("reward", 1.0),
    ]
    for field, value in mutations:
        mutated = json.loads(json.dumps(base))
        mutated["items"][0][field] = value
        d2 = compute_analysis_snapshot_digest(mutated, config)
        assert d2 != d1, f"snapshot digest did not change for item field {field}"


def test_next_run_feedback_in_report(tmp_path: Path) -> None:
    from evallab.analysis_capability import run_campaign_analysis

    rows = []
    for seed in range(1, 5):
        rows.append({"dose": 0, "seed": seed, "outcome": 1, "arm": "control", "capture_complete": True})
        rows.append({"dose": 1, "seed": seed, "outcome": 0, "arm": "treatment", "capture_complete": True})
    for seed in range(5, 10):
        rows.append({"dose": 0, "seed": seed, "outcome": 0, "arm": "control", "capture_complete": True})
        rows.append({"dose": 1, "seed": seed, "outcome": 0, "arm": "treatment", "capture_complete": True})

    spec = create_campaign_analysis_spec(
        spec_id="paired-seed-test",
        method=AnalysisMethod.PAIRED_SIGN,
        outcome_feature="outcome",
        unit=AnalysisUnit.PAIRED_SEED,
        unit_keys=("dose", "seed"),
        pair_keys=("seed",),
        denominator_policy=DenominatorPolicy.NOT_APPLICABLE,
        ci_method="none",
    )
    result = run_campaign_analysis(spec, rows)

    zero = "sha256:" + "0" * 64
    item = CampaignAnalysisItem(
        source_role="analysis",
        cohort_included=True,
        attempt_role="primary",
        job_id="j1",
        job_name="j1",
        trial_id="t1",
        trial_name="t1",
        task_name="task",
        quality_status="pass",
        cas_uri="cas://sha256/" + "0" * 64,
    )
    manifest = CampaignAnalysisManifest(
        schema_version="campaign-analysis-manifest/v1",
        manifest_id="m-1",
        manifest_digest="sha256:" + "1" * 64,
        campaign_id="c1",
        source_campaign_manifest_digest=zero,
        source_commit=None,
        authorizing_actor="test",
        cas_store_root=str(tmp_path / "cas"),
        items=[item],
        accounting={},
        analysis_config=CampaignAnalysisConfigV1(
            feature_registry_digest=zero,
            producer_digests={"ir_builder": zero},
            cohort_policy_digest=zero,
            redaction_policy_digest=zero,
            specs=(spec,),
        ),
        analysis_snapshot_digest=zero,
        produced_at=datetime.now(UTC),
    )
    per_trial = [{
        "job_id": "j1",
        "trial_id": "t1",
        "source_cas_uri": "cas://sha256/" + "0" * 64,
        "artifact_cas_uri": "cas://sha256/" + "1" * 64,
        "ir_digest": zero,
        "pack_digest": zero,
        "judgment_id": zero,
        "decision_id": zero,
        "decision": "abstained",
        "reason_codes": [],
        "coverage_gaps": ["judge_execution_disabled"],
    }]
    report = build_campaign_report(manifest, per_trial, analysis_results=[result])
    assert report["schema_version"] == "campaign-report/v2"
    feedback = report["next_run_feedback"]
    assert feedback["execution_authorized"] is False
    assert feedback["authorizing_actor_required"] is True
    actions = {r["action"] for r in feedback["recommendations"]}
    assert NextRunAction.ADD_INDEPENDENT_SEEDS.value in actions
    assert NextRunAction.HOLD_SEMANTIC_DECISION_ANALYSIS.value in actions


def test_review_queue_ref_is_non_decision(tmp_path: Path) -> None:
    zero = "sha256:" + "0" * 64
    item = CampaignAnalysisItem(
        source_role="analysis",
        cohort_included=True,
        attempt_role="primary",
        job_id="j1",
        job_name="j1",
        trial_id="t1",
        trial_name="t1",
        task_name="task",
        quality_status="pass",
        cas_uri="cas://sha256/" + "0" * 64,
    )
    retrieval = RetrievalPolicyV1(
        embedder_digest=zero,
        redaction_policy_digest=zero,
        enabled=True,
    )
    manifest = CampaignAnalysisManifest(
        schema_version="campaign-analysis-manifest/v1",
        manifest_id="m-1",
        manifest_digest="sha256:" + "1" * 64,
        campaign_id="rq-campaign",
        source_campaign_manifest_digest=zero,
        source_commit=None,
        authorizing_actor="test",
        cas_store_root=str(tmp_path / "cas"),
        items=[item],
        accounting={},
        analysis_config=CampaignAnalysisConfigV1(
            feature_registry_digest=zero,
            producer_digests={"ir_builder": zero},
            cohort_policy_digest=zero,
            redaction_policy_digest=zero,
            retrieval=retrieval,
        ),
        analysis_snapshot_digest=zero,
        produced_at=datetime.now(UTC),
    )
    ref = _build_review_queue_artifact(manifest, retrieval, tmp_path / "cas")
    assert ref.decision_eligible is False
    with pytest.raises(ValidationError):
        CampaignAnalysisResultV1.model_validate(ref.model_dump(mode="json"))


def test_end_to_end_zero_model_batch_spend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("model called")

    monkeypatch.setattr("evallab.evidence.facts.run_trial_analysis", boom)
    monkeypatch.setattr("evallab.analysis_worker.default_worker", boom)

    trial_dir = _trial_tree(tmp_path, trial_name="zero-model-batch")
    store = tmp_path / "cas"
    cas_uri = _archive_trial(trial_dir, store, "zero-model-batch")
    manifest_path = _make_manifest_with_spec(tmp_path, cas_uri)

    report = analyze_batch(
        manifest_path,
        repo_root=tmp_path,
        store_root=store,
        output_dir=tmp_path / "interpretation",
        derived_root=tmp_path / "derived",
    )
    assert report["report_id"].startswith("sha256:")
    assert report["report_digest"].startswith("sha256:")
    assert report["analysis_snapshot_digest"].startswith("sha256:")
    assert report["next_run_feedback"]["execution_authorized"] is False
    assert report["next_run_feedback"]["authorizing_actor_required"] is True
    assert report["analysis_results"]
    for result in report["analysis_results"]:
        assert result["snapshot_digest"] == report["analysis_snapshot_digest"]


def test_phase_a_e0b_paired_batch_orchestration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end proof: 18 repeat trials over 9 (dose, seed) pairs execute zero-model analysis.

    Demonstrates:
    - 18 repeat observations aggregate to exactly 9 independent units
    - Observed 4 worse / 5 ties yields risk diff 4/9, exact p=0.125, attainable p-floor 0.125, UNDERPOWERED
    - NextRunFeedbackV1 generates ADD_INDEPENDENT_SEEDS with requested_units=4 and HOLD_SEMANTIC_DECISION_ANALYSIS
    - execution_authorized is strictly False
    - Rerun produces identical snapshot and report digests.
    """
    def boom(*_args, **_kwargs):
        raise AssertionError("live model invoked")

    monkeypatch.setattr("evallab.evidence.facts.run_trial_analysis", boom)
    monkeypatch.setattr("evallab.analysis_worker.default_worker", boom)

    store = tmp_path / "cas"
    items: list[CampaignAnalysisItem] = []
    
    # 9 pairs (seed 1..9), 2 repeats per cell = 18 trials total
    # Seeds 1..4: control succeeds (reward 1.0), treatment fails (reward 0.0) -> 4 worse
    # Seeds 5..9: both fail (reward 0.0) -> 5 ties
    for seed in range(1, 10):
        for dose in (0, 1):
            arm = "control" if dose == 0 else "treatment"
            ctrl_success = seed <= 4 and arm == "control"
            reward = 1.0 if ctrl_success else 0.0
            
            trial_name = f"phase_a_seed_{seed}_dose_{dose}"
            trial_dir = _trial_tree(tmp_path, trial_name=trial_name, unpaired=False, reward=reward)
            cas_uri = _archive_trial(trial_dir, store, trial_name)

            item = CampaignAnalysisItem(
                source_role="analysis",
                cohort_included=True,
                attempt_role="primary",
                job_id=f"job-{arm}",
                job_name=f"job-{arm}",
                trial_id=trial_name,
                trial_name=trial_name,
                task_name=f"task-seed-{seed}",
                task_digest="sha256:" + f"{seed:02x}" * 32,
                verifier_digest="sha256:" + f"{dose:02x}" * 32,
                quality_status="pass",
                cas_uri=cas_uri,
                arm=arm,
                dose=dose,
                seed=seed,
                reward=reward,
                capture_complete=True,
                capture_authority="concordant",
            )
            items.append(item)

    spec = create_campaign_analysis_spec(
        spec_id="action-memory-arm-contrast",
        method=AnalysisMethod.PAIRED_SIGN,
        outcome_feature="reward",
        unit=AnalysisUnit.PAIRED_SEED,
        unit_keys=("dose", "seed"),
        pair_keys=("seed",),
        denominator_policy=DenominatorPolicy.NOT_APPLICABLE,
        ci_method="none",
    )

    from evallab.interpretation.feature_registry import TRAJECTORY_FEATURE_REGISTRY
    feature_reg_digest = canonical_json_digest(
        sorted([f.column_name for f in TRAJECTORY_FEATURE_REGISTRY.all_features().values()])
    )
    config = CampaignAnalysisConfigV1(
        feature_registry_digest=feature_reg_digest,
        producer_digests={
            "ir_builder": "sha256:" + "a" * 64,
            "pack_builder": "sha256:" + "b" * 64,
            "acceptance_policy": "sha256:" + "c" * 64,
        },
        cohort_policy_digest="sha256:" + "d" * 64,
        redaction_policy_digest=RedactionPolicy().compute_digest(),
        specs=(spec,),
    )

    manifest_body = {
        "schema_version": "campaign-analysis-manifest/v1",
        "campaign_id": "phase-a-arm-contrast",
        "source_campaign_manifest_digest": "sha256:" + "e" * 64,
        "source_commit": "abcdef12",
        "authorizing_actor": "peter",
        "cas_store_root": str(store),
        "items": [item.model_dump(mode="json") for item in items],
        "accounting": {
            "total_planned_specs": 18,
            "total_executed_trials": 18,
            "valid_analysis_ready_trials": 18,
            "quarantined_infrastructure_attempts": 0,
            "free_local_controls": 0,
            "unresolved_evidence_count": 0,
        },
        "analysis_config": config.model_dump(mode="json"),
    }
    snapshot_digest = compute_analysis_snapshot_digest(manifest_body, config)
    manifest_body["analysis_snapshot_digest"] = snapshot_digest
    manifest_id = canonical_json_digest(manifest_body)
    manifest_digest = canonical_json_digest({**manifest_body, "manifest_id": manifest_id})

    manifest = CampaignAnalysisManifest(
        manifest_id=manifest_id,
        manifest_digest=manifest_digest,
        produced_at=datetime.now(UTC),
        items=items,
        accounting=manifest_body["accounting"],
        analysis_config=config,
        analysis_snapshot_digest=snapshot_digest,
        schema_version="campaign-analysis-manifest/v1",
        campaign_id="phase-a-arm-contrast",
        source_campaign_manifest_digest="sha256:" + "e" * 64,
        source_commit="abcdef12",
        authorizing_actor="peter",
        cas_store_root=str(store),
    )
    manifest_path = tmp_path / "phase_a_manifest.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    out_dir = tmp_path / "interpretation"
    derived_dir = tmp_path / "derived"

    report = analyze_batch(
        manifest_path,
        repo_root=tmp_path,
        store_root=store,
        output_dir=out_dir,
        derived_root=derived_dir,
    )

    assert report["schema_version"] == "campaign-report/v2"
    assert report["cohort_accounted"] == 18
    assert report["analysis_snapshot_digest"] == snapshot_digest
    assert len(report["analysis_results"]) == 1

    paired_res = report["analysis_results"][0]
    assert paired_res["spec_id"] == "action-memory-arm-contrast"
    assert paired_res["observed_rows"] == 18
    assert paired_res["analysis_units"] == 9
    assert paired_res["informative_units"] == 4
    assert paired_res["status"] == AnalysisStatus.REFUSAL
    assert RefusalCode.UNDERPOWERED in paired_res["refusals"]
    assert paired_res["estimate"] == pytest.approx(4 / 9)
    assert paired_res["p_value"] is None
    assert paired_res["attainable_p_floor"] == pytest.approx(0.125)

    feedback = report["next_run_feedback"]
    assert feedback["execution_authorized"] is False
    assert feedback["authorizing_actor_required"] is True
    assert feedback["source_snapshot_digest"] == snapshot_digest
    recs = {r["action"]: r for r in feedback["recommendations"]}
    assert NextRunAction.ADD_INDEPENDENT_SEEDS in recs
    assert recs[NextRunAction.ADD_INDEPENDENT_SEEDS]["requested_units"] == 4
    assert NextRunAction.HOLD_SEMANTIC_DECISION_ANALYSIS in recs

    # Idempotent rerun check
    rerun = analyze_batch(
        manifest_path,
        repo_root=tmp_path,
        store_root=store,
        output_dir=out_dir,
        derived_root=derived_dir,
    )
    assert rerun["report_id"] == report["report_id"]
    assert rerun["report_digest"] == report["report_digest"]
    assert rerun["analysis_snapshot_digest"] == report["analysis_snapshot_digest"]
