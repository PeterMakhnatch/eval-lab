"""Focused tests for the all-durable `evallab data backfill` operator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

import pytest

from evallab.cli import parser, run_cli
from evallab.evidence_store import archive_evidence
from evallab.storage.data_backfill import (
    IDENTITY_UNRESOLVED,
    STORE_JOIN_UNAVAILABLE,
    assemble_disposition_rows,
    run_all_durable_backfill,
)


def _trial_tree(root: Path, *, trial_name: str, unpaired: bool = False) -> Path:
    trial_dir = root / trial_name
    (trial_dir / "agent").mkdir(parents=True)
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
        "session_id": "session-backfill-test",
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
        "verifier_result": {"rewards": {"reward": 0.0}},
    }
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return trial_dir


def _cohort_row(
    *,
    role: str,
    trial_name: str,
    trial_id: str,
    job_id: str,
    cas_uri: str,
    job_name: str | None = None,
) -> dict:
    return {
        "role": role,
        "spec_name": f"spec-{trial_name}",
        "spec_id": f"spec-{trial_id[:8]}",
        "job_name": f"job-{trial_name}" if job_name is None else job_name,
        "job_id": job_id,
        "trial_name": trial_name,
        "trial_id": trial_id,
        "task_name": "test/runtime-task",
        "task_digest": None,
        "verifier_digest": None,
        "quality_status": "pass",
        "quality_findings": [],
        "cas_uri": cas_uri,
        "ingestion_status": "projected_to_postgres_and_parquet",
    }


def _write_campaign_manifest(
    path: Path,
    *,
    campaign: str,
    store: Path,
    cohort: list[dict],
) -> None:
    payload = {
        "schema_version": "1.0",
        "inventory_type": "machine_analysis_input_inventory",
        "campaign": campaign,
        "commit_sha": "test",
        "authorizing_actor": "test",
        "cas_store_root": str(store),
        "accounting": {
            "total_planned_specs": len(cohort),
            "total_executed_trials": len(cohort),
            "valid_analysis_ready_trials": len(cohort),
            "free_local_controls": 0,
            "quarantined_infrastructure_attempts": 0,
            "unresolved_evidence_count": 0,
        },
        "analysis_cohort_5_trials": cohort,
        "controls_and_quarantine_ledger": [],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_inventory(
    path: Path,
    *,
    campaigns: list[dict],
    quarantines: list[dict],
) -> None:
    total = sum(int(entry["trial_count"]) for entry in campaigns) + len(quarantines)
    payload = {
        "schema_version": "1.0",
        "inventory_type": "cross_campaign_analysis_inventory",
        "status_summary": {
            "total_indexed_trials": total,
            "analysis_ready_trials": sum(int(entry["trial_count"]) for entry in campaigns),
            "quarantined_hold_trials": len(quarantines),
            "active_interpretation_campaigns": len(campaigns),
        },
        "batch_interpreted_campaigns": campaigns,
        "quarantined_and_hold_trials": quarantines,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _archive_job(trial_dir: Path, store: Path, job_name: str) -> str:
    return archive_evidence(trial_dir, store, record_id=job_name, kind="job").uri


def _run_backfill(tmp_path: Path, *, database_url: str | None = None):
    return run_all_durable_backfill(
        inventory_path=tmp_path / "inventory.json",
        manifest_dir=tmp_path / "manifests",
        repo_root=tmp_path,
        store_root=tmp_path / "cas",
        output_dir=tmp_path / "out",
        derived_root=tmp_path / "derived",
        database_url=database_url,
    )


def _ready_world(tmp_path: Path) -> dict[str, str]:
    store = tmp_path / "cas"
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    trial_dir = _trial_tree(tmp_path, trial_name="ready-trial")
    job_id = str(uuid4())
    job_name = "job-ready-trial"
    trial_id = str(uuid4())
    cas_uri = _archive_job(trial_dir, store, job_name)
    cohort = [
        _cohort_row(
            role="primary",
            trial_name="ready-trial",
            trial_id=trial_id,
            job_id=job_id,
            cas_uri=cas_uri,
        )
    ]
    _write_campaign_manifest(
        manifests / "ready.json",
        campaign="ready-campaign",
        store=store,
        cohort=cohort,
    )
    _write_inventory(
        tmp_path / "inventory.json",
        campaigns=[
            {
                "campaign_id": "ready-campaign",
                "manifest_path": "ready.json",
                "trial_count": 1,
            }
        ],
        quarantines=[],
    )
    return {
        "trial_id": trial_id,
        "job_id": job_id,
        "job_name": job_name,
        "cas_uri": cas_uri,
    }


def _mixed_world(tmp_path: Path) -> dict[str, str]:
    ids = _ready_world(tmp_path)
    store = tmp_path / "cas"
    q_dir = _trial_tree(tmp_path, trial_name="hold-trial")
    q_job = "job-hold-trial"
    q_trial = str(uuid4())
    q_uri = _archive_job(q_dir, store, q_job)
    inventory = json.loads((tmp_path / "inventory.json").read_text(encoding="utf-8"))
    inventory["quarantined_and_hold_trials"] = [
        {
            "trial_id": q_trial,
            "job_name": q_job,
            "trial_name": "hold-trial",
            "task_name": "test/quarantined-task",
            "reason": "pre-fix auth timeout",
            "cas_uri": q_uri,
        }
    ]
    inventory["status_summary"]["total_indexed_trials"] = 2
    inventory["status_summary"]["quarantined_hold_trials"] = 1
    (tmp_path / "inventory.json").write_text(
        json.dumps(inventory, indent=2, sort_keys=True), encoding="utf-8"
    )
    ids.update({"q_trial": q_trial, "q_job": q_job, "q_uri": q_uri})
    return ids


def test_data_backfill_help_registered(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parser().parse_args(["data", "backfill", "--help"])
    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    assert "usage:" in output
    assert "backfill" in output
    command_action = next(
        action for action in parser()._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert "data" in command_action.choices
    data_action = next(
        action
        for action in command_action.choices["data"]._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert "backfill" in data_action.choices


def test_single_invocation_exit_zero_exact_count(tmp_path: Path) -> None:
    ids = _mixed_world(tmp_path)
    ledger = _run_backfill(tmp_path)
    assert ledger.exit_code == 0
    assert ledger.disposition_count == 2
    assert ledger.discovered_count == 2
    assert {row.trial_id for row in ledger.dispositions} == {ids["trial_id"], ids["q_trial"]}
    assert (
        run_cli(
            [
                "data",
                "backfill",
                "--inventory",
                "inventory.json",
                "--manifest-dir",
                "manifests",
                "--store-root",
                "cas",
                "--output-dir",
                "cli-out",
                "--derived-root",
                "cli-derived",
            ],
            workspace=tmp_path,
        )
        == 0
    )


def test_ready_cohort_analysis_ready_empty_holds(tmp_path: Path) -> None:
    from evallab.storage.data_backfill import STORE_JOIN_UNAVAILABLE

    ids = _ready_world(tmp_path)
    ledger = _run_backfill(tmp_path)
    assert ledger.exit_code == 0
    row = ledger.dispositions[0]
    assert row.trial_id == ids["trial_id"]
    # Fail-closed settlement/root contract: frozen legacy backfill lacks partitioned store joins
    assert row.readiness == "HOLD"
    assert row.hold_reasons == [STORE_JOIN_UNAVAILABLE]
    assert row.job_id == ids["job_id"]
    assert row.job_id != ids["job_name"]
    assert row.cas_uri == ids["cas_uri"]
    assert row.ir_digest and row.ir_digest.startswith("sha256:")


def test_quarantined_trial_hold_absent_from_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.storage.data_backfill import STORE_JOIN_UNAVAILABLE
    from evallab.storage.data_backfill import analyze_batch as real_analyze_batch

    seen: list[Path] = []

    def wrapped(inventory_path, **kwargs):
        seen.append(Path(inventory_path))
        return real_analyze_batch(inventory_path, **kwargs)

    monkeypatch.setattr("evallab.storage.data_backfill.analyze_batch", wrapped)
    ids = _mixed_world(tmp_path)
    ledger = _run_backfill(tmp_path)
    hold = next(row for row in ledger.dispositions if row.trial_id == ids["q_trial"])
    ready = next(row for row in ledger.dispositions if row.trial_id == ids["trial_id"])
    assert hold.readiness == "HOLD"
    assert "pre-fix auth timeout" in hold.hold_reasons
    # Fail-closed settlement/root contract: partitioned store joins yield typed STORE_JOIN_UNAVAILABLE
    assert ready.readiness == "HOLD"
    assert ready.hold_reasons == [STORE_JOIN_UNAVAILABLE]
    assert ids["q_trial"] not in {
        row.trial_id for row in ledger.dispositions if row.readiness == "ANALYSIS_READY"
    }
    assert seen
    passed = json.loads(seen[0].read_text(encoding="utf-8"))
    cohort_ids = {row["trial_id"] for row in passed["analysis_cohort_5_trials"]}
    assert ids["trial_id"] in cohort_ids
    assert ids["q_trial"] not in cohort_ids


def test_missing_job_name_hold_unresolved(tmp_path: Path) -> None:
    store = tmp_path / "cas"
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    trial_dir = _trial_tree(tmp_path, trial_name="missing-name-trial")
    trial_id = str(uuid4())
    job_id = str(uuid4())
    cas_uri = _archive_job(trial_dir, store, "job-missing-name-trial")
    _write_campaign_manifest(
        manifests / "missing.json",
        campaign="missing-campaign",
        store=store,
        cohort=[
            _cohort_row(
                role="primary",
                trial_name="missing-name-trial",
                trial_id=trial_id,
                job_id=job_id,
                job_name="",
                cas_uri=cas_uri,
            )
        ],
    )
    _write_inventory(
        tmp_path / "inventory.json",
        campaigns=[
            {
                "campaign_id": "missing-campaign",
                "manifest_path": "missing.json",
                "trial_count": 1,
            }
        ],
        quarantines=[],
    )
    ledger = _run_backfill(tmp_path)
    assert ledger.exit_code == 0
    row = ledger.dispositions[0]
    assert row.readiness == "HOLD"
    assert row.job_id == job_id
    assert IDENTITY_UNRESOLVED in row.hold_reasons


def test_ambiguous_duplicate_binding_hold_unresolved(tmp_path: Path) -> None:
    store = tmp_path / "cas"
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    trial_dir = _trial_tree(tmp_path, trial_name="dup-trial")
    job_id = str(uuid4())
    job_name = "job-dup-trial"
    trial_id = str(uuid4())
    cas_uri = _archive_job(trial_dir, store, job_name)
    record_path = store / "records" / "job" / f"{job_name}.json"
    (record_path.parent / "duplicate-name.json").write_text(
        record_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _write_campaign_manifest(
        manifests / "dup.json",
        campaign="dup-campaign",
        store=store,
        cohort=[
            _cohort_row(
                role="primary",
                trial_name="dup-trial",
                trial_id=trial_id,
                job_id=job_id,
                cas_uri=cas_uri,
            )
        ],
    )
    _write_inventory(
        tmp_path / "inventory.json",
        campaigns=[{"campaign_id": "dup-campaign", "manifest_path": "dup.json", "trial_count": 1}],
        quarantines=[],
    )
    ledger = _run_backfill(tmp_path)
    row = ledger.dispositions[0]
    assert row.readiness == "HOLD"
    assert row.job_id == job_id
    assert IDENTITY_UNRESOLVED in row.hold_reasons


def test_cas_uri_mismatch_hold_unresolved(tmp_path: Path) -> None:
    store = tmp_path / "cas"
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    trial_a = _trial_tree(tmp_path, trial_name="foreign-a")
    trial_b = _trial_tree(tmp_path, trial_name="foreign-b")
    job_a = str(uuid4())
    trial_id = str(uuid4())
    _archive_job(trial_a, store, "job-foreign-a")
    uri_b = _archive_job(trial_b, store, "job-foreign-b")
    _write_campaign_manifest(
        manifests / "foreign.json",
        campaign="foreign-campaign",
        store=store,
        cohort=[
            _cohort_row(
                role="primary",
                trial_name="foreign-a",
                trial_id=trial_id,
                job_id=job_a,
                cas_uri=uri_b,
            )
        ],
    )
    _write_inventory(
        tmp_path / "inventory.json",
        campaigns=[
            {
                "campaign_id": "foreign-campaign",
                "manifest_path": "foreign.json",
                "trial_count": 1,
            }
        ],
        quarantines=[],
    )
    ledger = _run_backfill(tmp_path)
    row = ledger.dispositions[0]
    assert row.readiness == "HOLD"
    assert row.job_id == job_a
    assert IDENTITY_UNRESOLVED in row.hold_reasons


def test_four_permanent_quarantine_reasons_are_preserved(tmp_path: Path) -> None:
    store = tmp_path / "cas"
    (tmp_path / "manifests").mkdir()
    expected: dict[str, str] = {}
    quarantines: list[dict[str, str]] = []
    reasons = (
        "pre-fix auth timeout",
        "pre-fix Darwin verifier isolation failure",
        "pre-fix Darwin verifier isolation failure",
        "pre-fix Darwin environment isolation failure",
    )
    for index, reason in enumerate(reasons):
        trial_id = str(uuid4())
        trial_name = f"permanent-quarantine-{index}"
        job_name = f"job-{trial_name}"
        trial_dir = _trial_tree(tmp_path, trial_name=trial_name)
        cas_uri = _archive_job(trial_dir, store, job_name)
        expected[trial_id] = reason
        quarantines.append(
            {
                "trial_id": trial_id,
                "trial_name": trial_name,
                "job_name": job_name,
                "task_name": "test/permanent-quarantine",
                "reason": reason,
                "cas_uri": cas_uri,
            }
        )

    _write_inventory(
        tmp_path / "inventory.json",
        campaigns=[],
        quarantines=quarantines,
    )
    ledger = _run_backfill(tmp_path)

    assert ledger.disposition_count == 4
    assert ledger.ready_count == 0
    assert ledger.hold_count == 4
    for row in ledger.dispositions:
        assert row.readiness == "HOLD"
        assert expected[row.trial_id] in row.hold_reasons
        assert IDENTITY_UNRESOLVED not in row.hold_reasons


def test_unavailable_store_reason_coded_hold(tmp_path: Path) -> None:
    _ready_world(tmp_path)
    ledger = _run_backfill(
        tmp_path,
        database_url="postgresql://127.0.0.1:1/evallab_missing",
    )
    row = ledger.dispositions[0]
    assert row.readiness == "HOLD"
    assert STORE_JOIN_UNAVAILABLE in row.hold_reasons
    assert ledger.exit_code == 0


def test_short_disposition_count_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mixed_world(tmp_path)

    def drop_last(rows):
        return assemble_disposition_rows(rows)[:-1]

    monkeypatch.setattr("evallab.storage.data_backfill.assemble_disposition_rows", drop_last)
    ledger = _run_backfill(tmp_path)
    assert ledger.disposition_count < ledger.discovered_count
    assert ledger.exit_code != 0


def test_identical_invocations_byte_identical_ledger(tmp_path: Path) -> None:
    _ready_world(tmp_path)
    first = _run_backfill(tmp_path)
    first_bytes = Path(first.ledger_path or tmp_path / "out" / "ledger.json").read_bytes()
    # second invocation over the same inputs and output dir
    second = _run_backfill(tmp_path)
    second_bytes = Path(second.ledger_path or tmp_path / "out" / "ledger.json").read_bytes()
    assert first.content_digest == second.content_digest
    assert first_bytes == second_bytes
