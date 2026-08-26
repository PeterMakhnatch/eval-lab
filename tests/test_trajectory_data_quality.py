"""Observable contracts for the Platform campaign data-quality operator."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from evallab.cli import parser
from evallab.trajectory_data_quality import (
    campaign_data_quality_report,
    load_cross_campaign_inventory,
)
from evallab.trajectory_runtime import load_campaign_analysis_manifest

REPO = Path(__file__).resolve().parents[1]
TB3_INVENTORY = (
    REPO
    / "research"
    / "experiments"
    / "manifests"
    / "terminal-bench-v3-k1-gemini-low-machine-analysis-inventory.json"
)
CROSS_INVENTORY = (
    REPO / "research" / "experiments" / "manifests" / "cross-campaign-analysis-inventory.json"
)
CANARY_EVENT_SUMMARY = (
    REPO
    / "research"
    / "experiments"
    / "manifests"
    / "canary-event-summary-codex-20260815-analysis-manifest.json"
)


def _mini_inventory(path: Path, *, cas_uri: str, verifier_digest: str | None) -> Path:
    trial_id = str(uuid4())
    payload = {
        "schema_version": "1.0",
        "inventory_type": "machine_analysis_input_inventory",
        "campaign": "data-quality-mini",
        "commit_sha": "test",
        "authorizing_actor": "test",
        "cas_store_root": "derived/evidence-cas",
        "accounting": {
            "total_planned_specs": 1,
            "total_executed_trials": 1,
            "valid_analysis_ready_trials": 1,
            "quarantined_infrastructure_attempts": 0,
            "free_local_controls": 0,
            "unresolved_evidence_count": 0,
        },
        "analysis_cohort_5_trials": [
            {
                "role": "primary",
                "spec_name": "spec-mini",
                "spec_id": "spec-mini",
                "job_name": "job-mini",
                "job_id": str(uuid4()),
                "trial_name": "mini-trial",
                "trial_id": trial_id,
                "task_name": "test/mini",
                "task_digest": None,
                "verifier_digest": verifier_digest,
                "quality_status": "pass",
                "quality_findings": [],
                "cas_uri": cas_uri,
            }
        ],
        "controls_and_quarantine_ledger": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_analyze_quality_command_registered() -> None:
    root = parser()
    analyze = next(
        action
        for action in root._actions
        if getattr(action, "dest", None) == "command" and hasattr(action, "choices")
    )
    sub = next(
        action
        for action in analyze.choices["analyze"]._actions
        if getattr(action, "dest", None) == "analyze_command"
    )
    assert "quality" in sub.choices


def test_quality_report_unknown_vs_zero(tmp_path: Path) -> None:
    inventory = _mini_inventory(
        tmp_path / "inventory.json",
        cas_uri="cas://sha256/" + "11" * 32,
        verifier_digest=None,
    )
    report = campaign_data_quality_report(
        inventory,
        repo_root=tmp_path,
        store_root=tmp_path / "cas",
        output_dir=tmp_path / "interpretation",
        derived_root=tmp_path / "derived",
        database_url="postgresql://evallab@127.0.0.1:1/missing",
    )
    postgres = report["projections"]["postgres"]
    jobs = report["projections"]["jobs_parquet"]
    assert report["readiness"] == "HOLD"
    assert postgres["status"] == "unavailable"
    assert postgres["row_count"] is None
    assert jobs["status"] == "missing"
    assert jobs["row_count"] is None
    assert report["projections"]["trial_facts"]["row_count"] is None
    trial = report["trials"][0]
    assert trial["verifier_digest"] is None
    assert trial["source_gaps"]["verifier_digest"] is None
    assert trial["source_gaps"]["verifier_digest_presence"] == "unknown"
    assert trial["pack"]["selected_events"] is None
    assert trial["citation_reopen"]["available"] is None
    assert "postgres_unavailable" in report["hold_reasons"]
    assert "jobs_parquet_missing" in report["hold_reasons"]


def test_current_manifest_accounting_tb3_and_cross_campaign() -> None:
    manifest = load_campaign_analysis_manifest(TB3_INVENTORY)
    assert manifest.accounting["planned_specs"] == 5
    assert manifest.accounting["executions"] == 7
    assert manifest.accounting["analysis_cohort"] == 5
    assert manifest.accounting["quarantine"] == 1
    assert manifest.accounting["controls"] == 1
    assert manifest.accounting["unresolved"] == 0
    assert len(manifest.cohort_items()) == 5
    assert len(manifest.accounting_items()) == 2

    cross = load_cross_campaign_inventory(CROSS_INVENTORY)
    summary = cross["status_summary"]
    assert summary["total_indexed_trials"] == 21
    assert summary["analysis_ready_trials"] == 17
    assert summary["batch_interpreted_trials"] == 17
    assert summary["quarantined_hold_trials"] == 4
    assert summary["active_interpretation_campaigns"] == 5
    assert len(cross["batch_interpreted_campaigns"]) == 5


def test_canary_verifier_digest_stays_unknown(tmp_path: Path) -> None:
    report = campaign_data_quality_report(
        CANARY_EVENT_SUMMARY,
        repo_root=REPO,
        store_root=tmp_path / "cas",
        output_dir=tmp_path / "interpretation",
        derived_root=tmp_path / "derived",
        database_url=None,
    )
    assert report["campaign_id"] == "canary-event-summary-codex-20260815"
    assert report["accounting"]["analysis_cohort"] == 3
    assert report["accounting"]["executions"] == 3
    assert report["readiness"] == "HOLD"
    assert len(report["cas_identity"]["shared_source_cas_uris"]) == 1
    assert report["cas_identity"]["shared_source_cas_uris"][0]["trial_count"] == 3
    for trial in report["trials"]:
        assert trial["verifier_digest"] is None
        assert trial["source_gaps"]["verifier_digest"] is None
        assert trial["source_gaps"]["verifier_digest_presence"] == "unknown"
        assert trial["verifier_digest"] != 0
