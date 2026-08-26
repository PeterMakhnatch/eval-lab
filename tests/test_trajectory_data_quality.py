"""Observable contracts for the Platform campaign data-quality operator."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.cli import _select_json_fields, parser
from evallab.evidence_store import archive_evidence, load_archive
from evallab.trajectory_data_quality import (
    _add_campaign_projection_joins,
    _cas_availability,
    _citation_reopen,
    _jobs_parquet_projection,
    _pack_selection,
    _select_sidecar_generation,
    campaign_data_quality_report,
    load_cross_campaign_inventory,
)
from evallab.trajectory_judgment import canonical_json_digest
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


def test_quality_cli_field_selection_is_explicit_and_nested() -> None:
    payload = {
        "readiness": "HOLD",
        "coverage_totals": {"analysis_cohort": {"included": 5}},
    }
    assert _select_json_fields(
        payload,
        "{status:.readiness,included:.coverage_totals.analysis_cohort.included}",
    ) == {"status": "HOLD", "included": 5}


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
    assert Path(report["report_path"]).is_file()
    assert load_archive(tmp_path / "cas", report["report_cas_uri"]).is_file()
    rerun = campaign_data_quality_report(
        inventory,
        repo_root=tmp_path,
        store_root=tmp_path / "cas",
        output_dir=tmp_path / "out",
        derived_root=tmp_path / "derived",
        database_url="postgresql://evallab@127.0.0.1:1/missing",
    )
    assert rerun["report_id"] == report["report_id"]
    assert rerun["report_cas_uri"] == report["report_cas_uri"]


def test_jobs_parquet_projection_present_for_job_level_hive(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    job_dir = derived / "parquet" / "job_id=abc123"
    job_dir.mkdir(parents=True)
    rows = [
        {"job_id": "abc123", "status": "done"},
        {"job_id": "abc123", "status": "running"},
    ]
    hive_path = job_dir / "jobs.parquet"
    pq.write_table(pa.Table.from_pylist(rows), hive_path)

    projection = _jobs_parquet_projection(derived)
    assert projection["status"] == "present"
    assert projection["reason"] is None
    assert projection["row_count"] == 2
    assert projection["stray_jobs_parquet_paths"] == []
    assert str(hive_path) not in projection["stray_jobs_parquet_paths"]


def test_jobs_parquet_projection_treats_trial_nested_jobs_as_stray(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    nested = derived / "parquet" / "job_id=abc123" / "trial_id=t1"
    nested.mkdir(parents=True)
    stray_path = nested / "jobs.parquet"
    pq.write_table(
        pa.Table.from_pylist([{"job_id": "abc123", "status": "done"}]),
        stray_path,
    )

    projection = _jobs_parquet_projection(derived)
    assert projection["status"] == "missing"
    assert projection["reason"] == "jobs_parquet_hive_absent"
    assert projection["row_count"] is None
    assert projection["stray_jobs_parquet_paths"] == [
        "parquet/job_id=abc123/trial_id=t1/jobs.parquet"
    ]


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


def test_pack_selection_rejects_missing_counts_and_omitted_digest_mismatch() -> None:
    source_citation = {
        "citation_id": "cit-1",
        "source_path": "agent/trajectory.json",
        "source_sha256": "sha256:" + "1" * 64,
        "raw_cas_uri": "cas://sha256/" + "2" * 64,
        "step_id": 1,
        "target_type": "step",
    }
    event = {
        "event_id": "evt-1",
        "step_index": 1,
        "source_citation": source_citation,
    }
    ir = {"events": [event]}
    missing = _pack_selection(
        {"selected_windows": [{}], "omitted_ranges": []},
        ir,
    )
    assert missing["status"] == "invalid"
    assert "invalid_selected_events" in missing["reason"]
    assert missing["selected_events"] == 0

    omitted_range = {
        "event_count": 1,
        "event_ids": ["evt-1"],
        "step_start": 1,
        "step_end": 1,
        "reopening_citation": source_citation,
        "omitted_content_digest": "sha256:" + "0" * 64,
    }
    bad_omission = {"selected_windows": [], "omitted_ranges": [omitted_range]}
    invalid = _pack_selection(bad_omission, ir)
    assert invalid["status"] == "invalid"
    assert "omitted_content_digest_mismatch" in invalid["reason"]

    omitted_range["omitted_content_digest"] = canonical_json_digest([event])
    complete = _pack_selection(bad_omission, ir)
    assert complete["status"] == "present"
    assert complete["accounted_events"] == complete["ir_events"] == 1
    assert complete["omitted_ranges_verified"] == 1

    duplicate = {
        "selected_windows": [],
        "omitted_ranges": [dict(omitted_range), dict(omitted_range)],
    }
    duplicated = _pack_selection(duplicate, ir)
    assert duplicated["status"] == "invalid"
    assert "duplicate_omitted_event" in duplicated["reason"]


def test_multiple_sidecar_candidates_never_select_a_generation() -> None:
    status, selected = _select_sidecar_generation(
        [
            {"status": "valid", "path": "output/current"},
            {"status": "partial", "path": "output/interrupted"},
        ]
    )
    assert status == "multiple"
    assert selected is None


def test_source_cas_availability_rejects_corrupt_archive(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "result.json").write_text("{}", encoding="utf-8")
    store = tmp_path / "cas"
    archived = archive_evidence(source, store, record_id="corrupt-source")
    blob = load_archive(store, archived.uri)
    content = bytearray(blob.read_bytes())
    content[len(content) // 2] ^= 0x01
    blob.write_bytes(content)

    availability = _cas_availability(archived.uri, store)

    assert availability["status"] == "invalid"
    assert availability["reason"].startswith("cas_restore_failed:")


def test_citation_reopen_classifies_malformed_source_digest_map(tmp_path: Path) -> None:
    handle = {
        "citation_id": "citation",
        "source_path": "agent/trajectory.json",
        "source_sha256": "sha256:" + "1" * 64,
        "target_type": "step",
    }
    result = _citation_reopen(
        ir={"source_digests": [], "events": [{"source_citation": handle}]},
        pack={"selected_windows": [], "omitted_ranges": []},
        store_root=tmp_path / "cas",
        quarantined=False,
    )

    assert result["status"] == "invalid"
    assert result["reason"] == "invalid_ir_source_digests"
    assert result["integrity_failures"] == 1


def test_citation_reopen_rejects_content_digest_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    trajectory = source / "agent" / "trajectory.json"
    trajectory.parent.mkdir(parents=True)
    trajectory.write_text(
        json.dumps({"steps": [{"step_id": 1, "source": "agent", "message": "observed"}]}),
        encoding="utf-8",
    )
    store = tmp_path / "cas"
    archive = archive_evidence(source, store, record_id="citation", kind="trial")
    source_digest = f"sha256:{hashlib.sha256(trajectory.read_bytes()).hexdigest()}"
    handle = {
        "citation_id": "cit-content",
        "trial_id": "trial-content",
        "source_document_id": "main",
        "source_path": "agent/trajectory.json",
        "source_sha256": source_digest,
        "raw_cas_uri": archive.uri,
        "step_id": 1,
        "target_type": "step",
        "content_sha256": "sha256:" + "0" * 64,
        "availability": "available",
    }
    ir = {
        "source_digests": {
            "cas_uri": archive.uri,
            "source_sha256": source_digest,
        },
        "events": [{"event_id": "evt-content", "source_citation": handle}],
    }
    pack = {"selected_windows": [], "omitted_ranges": []}

    reopened = _citation_reopen(
        ir=ir,
        pack=pack,
        store_root=store,
        quarantined=False,
    )
    assert reopened["status"] == "invalid"
    assert reopened["integrity_failures"] == 1
    assert reopened["reason_counts"] == {"content_digest_mismatch": 1}


def test_corrupt_named_projection_is_unknown_not_zero(tmp_path: Path) -> None:
    inventory = _mini_inventory(
        tmp_path / "inventory.json",
        cas_uri="cas://sha256/" + "22" * 32,
        verifier_digest=None,
    )
    corrupt = tmp_path / "derived" / "interpretation_artifacts" / "interpretation_artifacts.parquet"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not parquet")

    report = campaign_data_quality_report(
        inventory,
        repo_root=tmp_path,
        store_root=tmp_path / "cas",
        output_dir=tmp_path / "interpretation",
        derived_root=tmp_path / "derived",
    )
    projection = report["projections"]["interpretation_artifacts"]
    assert projection["status"] == "invalid"
    assert projection["row_count"] is None
    assert "interpretation_artifacts_invalid" in report["hold_reasons"]


def test_null_manifest_cas_stays_unknown(tmp_path: Path) -> None:
    inventory = _mini_inventory(
        tmp_path / "inventory.json",
        cas_uri="cas://sha256/" + "33" * 32,
        verifier_digest=None,
    )
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["analysis_cohort_5_trials"][0]["cas_uri"] = None
    inventory.write_text(json.dumps(payload), encoding="utf-8")

    manifest = load_campaign_analysis_manifest(inventory)
    assert manifest.items[0].cas_uri is None
    report = campaign_data_quality_report(
        inventory,
        repo_root=tmp_path,
        store_root=tmp_path / "cas",
        output_dir=tmp_path / "interpretation",
        derived_root=tmp_path / "derived",
    )
    trial = report["trials"][0]
    assert trial["cas_uri"] is None
    assert trial["cas_availability"]["status"] == "unknown"
    assert "source_cas_unknown" in trial["coverage_gaps"]


def test_projection_joins_separate_current_and_historical_rows(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    derived = tmp_path / "derived"
    trial_id = "trial-current"
    current = {
        "job_id": "job",
        "pack_digest": "sha256:" + "1" * 64,
        "judgment_id": "sha256:" + "2" * 64,
        "decision_id": "sha256:" + "3" * 64,
    }
    historical = {
        "job_id": "job",
        "pack_digest": "sha256:" + "4" * 64,
        "judgment_id": "sha256:" + "5" * 64,
        "decision_id": "sha256:" + "6" * 64,
    }
    facts = derived / "parquet" / "job_id=job" / f"trial_id={trial_id}" / "trial_facts.parquet"
    facts.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([{"job_id": "job", "trial_id": trial_id}]), facts)

    artifact_rows = []
    for identity in (current, historical):
        for kind in ("ir", "pack", "judgment", "decision", "interpretation"):
            artifact_rows.append(
                {
                    "trial_id": trial_id,
                    "job_id": identity["job_id"],
                    "kind": kind,
                    "pack_digest": identity["pack_digest"],
                    "judgment_id": identity["judgment_id"],
                    "decision_id": identity["decision_id"],
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
                    "judgment_id": current["judgment_id"],
                    "pack_digest": current["pack_digest"],
                },
                {
                    "judgment_id": historical["judgment_id"],
                    "pack_digest": historical["pack_digest"],
                },
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
                    "decision_id": current["decision_id"],
                    "pack_digest": current["pack_digest"],
                    "judgment_ids_json": json.dumps([current["judgment_id"]]),
                },
                {
                    "decision_id": historical["decision_id"],
                    "pack_digest": historical["pack_digest"],
                    "judgment_ids_json": json.dumps([historical["judgment_id"]]),
                },
            ]
        ),
        decision_path,
    )
    projections = {
        name: {"status": "present", "reason": None, "row_count": count}
        for name, count in {
            "trial_facts": 1,
            "interpretation_artifacts": 10,
            "machine_judgments": 2,
            "acceptance_decisions": 2,
        }.items()
    }
    trials = [
        {
            "job_id": "job",
            "trial_id": trial_id,
            "cohort_included": True,
            "sidecar_identity": current,
        }
    ]

    _add_campaign_projection_joins(
        projections,
        derived_root=derived,
        trials=trials,
    )

    artifacts = projections["interpretation_artifacts"]
    assert artifacts["current_row_count"] == artifacts["expected_current_row_count"] == 5
    assert artifacts["historical_row_count"] == 5
    assert artifacts["duplicate_current_rows"] == 0
    assert projections["machine_judgments"]["current_row_count"] == 1
    assert projections["acceptance_decisions"]["current_row_count"] == 1
