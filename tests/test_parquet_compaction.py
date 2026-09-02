"""Contracts and tests for deterministic Parquet compaction engine (WS-E item 4).

Tests cover:
- Date resolution hierarchy (steps.parquet timestamp -> result.json -> mtime).
- End-to-end compaction across all 15 Parquet tables.
- Zero row loss and exact Arrow schema preservation.
- Idempotent re-runs and deduplication by primary key.
- Granular partition retention (retaining trailing 7 days, pruning > 7 days).
- Command-line interface and JSON/human output.
- DuckDB Hive partitioning query compatibility over compact/dt=*.
- Robust error handling and validation failure rollbacks.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.storage.parquet_compaction import (
    COMPACT_DIRNAME,
    PROJECTED_TABLE_NAMES,
    TABLE_SCHEMAS,
    CompactionValidationError,
    _read_table_or_empty,
    compact,
    count_table_rows,
    deduplicate_and_sort,
    discover_compacted_row_counts,
    discover_uncompacted_jobs,
    main,
    parse_iso_date,
    plan_compaction,
    resolve_job_date,
    write_compact_table,
)


def _make_table_row(table_name: str, job_id: str, trial_id: str, index: int = 1) -> dict[str, Any]:
    """Generate a valid dummy row matching TABLE_SCHEMAS[table_name]."""
    if table_name == "jobs":
        return {
            "job_id": job_id,
            "job_name": f"job-{job_id}",
            "trial_count": 1,
        }
    if table_name == "trajectories":
        return {
            "job_id": job_id,
            "trial_id": trial_id,
            "document_id": f"doc-{index}",
            "source_path": f"/path/to/doc-{index}.json",
            "source_sha256": "sha256:abc123",
            "embedded_path": None,
            "schema_version": "ATIF-v1.7",
            "session_id": f"sess-{job_id}",
            "trajectory_id": f"traj-{index}",
            "validation_status": "valid",
            "validator": "internal-atif-v1",
            "validation_error": None,
            "agent_name": "oracle",
            "agent_version": "1.0.0",
            "model_name": "default",
            "continued_trajectory_ref": None,
            "step_count": 5,
            "llm_call_count": 5,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cached_tokens": 0,
            "cost_usd": 0.01,
        }
    if table_name == "steps":
        return {
            "job_id": job_id,
            "trial_id": trial_id,
            "document_id": f"doc-{index}",
            "source_path": f"/path/to/doc-{index}.json",
            "source_sha256": "sha256:abc123",
            "step_id": index,
            "source": "agent",
            "timestamp": "2026-08-10T14:30:00Z",
            "model_name": "default",
            "is_copied_context": False,
            "llm_call_count": 1,
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "cached_tokens": 0,
            "cost_usd": 0.002,
            "tool_call_count": 1,
            "observation_count": 1,
            "llm_metadata_available": False,
        }
    if table_name == "tool_calls":
        return {
            "job_id": job_id,
            "trial_id": trial_id,
            "document_id": f"doc-{index}",
            "source_path": f"/path/to/doc-{index}.json",
            "source_sha256": "sha256:abc123",
            "step_id": index,
            "tool_call_id": f"call-{index}",
            "function_name": "bash",
            "arguments_sha256": "sha256:callargs",
        }
    if table_name == "observations":
        return {
            "job_id": job_id,
            "trial_id": trial_id,
            "document_id": f"doc-{index}",
            "source_path": f"/path/to/doc-{index}.json",
            "source_sha256": "sha256:abc123",
            "step_id": index,
            "observation_index": 0,
            "source_call_id": f"call-{index}",
            "content_size_bytes": 42,
            "content_sha256": "sha256:obscontent",
            "subagent_ref_count": 0,
            "subagent_refs_sha256": None,
            "command_exit_code": 0,
        }
    if table_name == "trial_facts":
        return {
            "experiment_id": "exp-001",
            "job_id": job_id,
            "trial_id": trial_id,
            "job_name": f"job-{job_id}",
            "trial_name": f"trial-{trial_id}",
            "task_name": "local-lab/sample",
            "task_digest": "sha256:task",
            "verifier_digest": "sha256:verifier",
            "environment_digest": "sha256:env",
            "agent_config_digest": "sha256:config",
            "agent_name": "oracle",
            "agent_version": "1.0.0",
            "model_name": "default",
            "primary_reward": 1.0,
            "exception_class": None,
            "exception_phase": None,
            "duration_seconds": 12.5,
            "environment_setup_seconds": 1.0,
            "agent_setup_seconds": 0.5,
            "agent_execution_seconds": 10.0,
            "verifier_seconds": 1.0,
            "input_tokens": 100,
            "cache_tokens": 0,
            "output_tokens": 50,
            "cost_usd": 0.01,
            "trajectory_count": 1,
            "invalid_trajectory_count": 0,
            "step_count": 5,
            "llm_call_count": 5,
            "tool_call_count": 1,
            "command_failure_count": 0,
            "repeated_failed_command_count": 0,
            "artifact_count": 1,
            "missing_artifact_count": 0,
            "artifact_set_digest": "sha256:artifacts",
            "state_journal_status": "available",
            "state_journal_reason": None,
            "state_change_count": 1,
        }
    if table_name == "reward_facts":
        return {
            "experiment_id": "exp-001",
            "job_id": job_id,
            "trial_id": trial_id,
            "reward_name": "reward",
            "reward_value": 1.0,
        }
    if table_name == "artifact_facts":
        return {
            "experiment_id": "exp-001",
            "job_id": job_id,
            "trial_id": trial_id,
            "source": "/workspace/result.txt",
            "destination": "result.txt",
            "status": "present",
            "exists_on_disk": True,
            "size_bytes": 120,
            "sha256": "sha256:art123",
        }
    if table_name == "tool_usage":
        return {
            "experiment_id": "exp-001",
            "job_id": job_id,
            "trial_id": trial_id,
            "function_name": "bash",
            "call_count": 1,
        }
    if table_name == "state_changes":
        return {
            "experiment_id": "exp-001",
            "job_id": job_id,
            "trial_id": trial_id,
            "path": f"output/result-{index}.txt",
            "change_type": "added",
            "before_sha256": None,
            "after_sha256": "sha256:state123",
            "before_size_bytes": None,
            "after_size_bytes": 42,
            "event_count": 2,
            "first_event_at": "2026-08-10T14:30:01Z",
            "last_event_at": "2026-08-10T14:30:02Z",
            "journal_status": "available",
        }
    if table_name == "state_events":
        return {
            "experiment_id": "exp-001",
            "job_id": job_id,
            "trial_id": trial_id,
            "sequence": index,
            "precedence": index,
            "event_at": "2026-08-10T14:30:01Z",
            "predecessor_sequence": index - 1 if index > 1 else None,
            "operations": ["close_write"],
            "path": f"output/result-{index}.txt",
            "is_directory": False,
            "cookie": None,
            "before_state_digest": None,
            "after_state_digest": "sha256:state",
            "before_content_sha256": None,
            "after_content_sha256": "sha256:content",
            "before_size_bytes": None,
            "after_size_bytes": 42,
            "before_evidence_status": "known_absent",
            "producer": "evallab-state-journal",
            "producer_schema_version": 1,
            "fact_schema_version": "state-event-fact-v1",
            "source_digest": "sha256:source",
            "source_record_digest": "sha256:record",
            "temporal_semantics": "sequence_precedence_non_causal",
            "evidence_status": "valid",
            "invalid_reason": None,
            "invalid_error_digest": None,
        }
    if table_name == "trajectory_events":
        return {
            "job_id": job_id,
            "trial_id": trial_id,
            "document_id": f"doc-{index}",
            "event_id": f"event-{index}",
            "parent_event_id": None,
            "sequence": index,
            "step_id": index,
            "event_type": "tool_call",
            "source": "agent",
            "timestamp": "2026-08-10T14:30:00Z",
            "model_name": None,
            "tool_call_id": f"call-{index}",
            "content_sha256": "sha256:event",
            "content_size_bytes": 42,
            "outcome": "success",
            "exit_code": 0,
            "source_path": f"/path/to/doc-{index}.json",
        }
    if table_name == "agent_actions":
        return {
            "job_id": job_id,
            "trial_id": trial_id,
            "document_id": f"doc-{index}",
            "action_id": f"action-{index}",
            "step_id": index,
            "tool_call_id": f"call-{index}",
            "timestamp": "2026-08-10T14:30:00Z",
            "function_name": "bash",
            "action_family": "execute",
            "arguments_sha256": "sha256:input",
            "observation_sha256": "sha256:output",
            "observation_size_bytes": 42,
            "exit_code": 0,
            "outcome": "success",
            "effect_count": 1,
            "source_path": f"/path/to/doc-{index}.json",
        }
    if table_name == "llm_calls":
        return {
            "job_id": job_id,
            "trial_id": trial_id,
            "document_id": f"doc-{index}",
            "call_id": f"llm-{index}",
            "step_id": index,
            "timestamp": "2026-08-10T14:30:00Z",
            "model_name": "default",
            "call_count": 1,
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "cached_tokens": 0,
            "cost_usd": 0.002,
            "projection_status": "projected",
            "source_path": f"/path/to/doc-{index}.json",
            "metadata_available": False,
        }
    if table_name == "trajectory_phases":
        return {
            "job_id": job_id,
            "trial_id": trial_id,
            "phase_id": index,
            "phase_type": "execution",
            "name": "Execution",
            "step_start": index,
            "step_end": index,
            "step_count": 1,
            "tool_calls": 1,
            "errors": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "cost_usd": 0.0,
            "algorithm_version": "phase-v1",
            "source_path": f"/path/to/doc-{index}.json",
        }
    if table_name == "action_effects":
        return {
            "job_id": job_id,
            "trial_id": trial_id,
            "effect_id": f"effect-{index}",
            "action_id": f"action-{index}",
            "path": f"output/result-{index}.txt",
            "change_type": "added",
            "before_sha256": None,
            "after_sha256": "sha256:state123",
            "before_size_bytes": None,
            "after_size_bytes": 42,
            "first_event_at": "2026-08-10T14:30:01Z",
            "last_event_at": "2026-08-10T14:30:02Z",
            "link_status": "linked",
            "link_method": "latest_preceding_action",
        }
    if table_name == "capability_opportunities":
        return {
            "source_ref": f"runs/{trial_id}/trajectory.json",
            "source_digest": "sha256:" + "a" * 64,
            "provenance_kind": "mechanical",
            "opportunity_id": f"opp-{index}",
            "trial_id": trial_id,
            "benchmark": "swebench",
            "construct": "retrieval",
            "start_step": 0,
            "end_step": 1,
            "eligible": True,
            "required_evidence": ["e1"],
            "missing_evidence": [],
        }
    if table_name == "process_step_facts":
        return {
            "source_ref": f"runs/{trial_id}/trajectory.json",
            "source_digest": "sha256:" + "b" * 64,
            "provenance_kind": "mechanical",
            "trial_id": trial_id,
            "source_trajectory_id": f"traj-{index}",
            "source_step_id": f"step-{index}",
            "label": "correct",
            "original_label": None,
            "propagated_from_step": None,
            "first_error": None,
        }
    if table_name == "constraint_facts":
        return {
            "source_ref": f"runs/{trial_id}/trajectory.json",
            "source_digest": "sha256:" + "c" * 64,
            "provenance_kind": "benchmark_verifier",
            "trial_id": trial_id,
            "plan_id": f"plan-{index}",
            "action_id": None,
            "constraint_id": f"const-{index}",
            "constraint_scope": "local",
            "required": True,
            "verdict": "satisfied",
            "verifier_evidence": "verified",
        }
    if table_name == "context_operation_facts":
        return {
            "source_ref": f"runs/{trial_id}/trajectory.json",
            "source_digest": "sha256:" + "d" * 64,
            "provenance_kind": "mechanical",
            "trial_id": trial_id,
            "operation_id": f"op-{index}",
            "operation": "compaction",
            "configured_size": 100,
            "realized_size": 80,
            "prompt_tokens": 50,
            "before_token_count": 200,
            "after_token_count": 120,
            "content_digest": None,
        }
    if table_name == "paired_condition_facts":
        return {
            "source_ref": f"runs/{trial_id}/trajectory.json",
            "source_digest": "sha256:" + "e" * 64,
            "provenance_kind": "mechanical",
            "trial_id": trial_id,
            "pair_id": f"pair-{index}",
            "session_id": f"sess-{job_id}",
            "task_id": "task-001",
            "variant": "v1",
            "condition": "control",
            "trigger": "prompt",
            "critical_action": None,
            "state_diff": None,
            "primary_verdict": "satisfied",
            "secondary_verdict": "unknown",
        }
    if table_name == "session_dependency_facts":
        return {
            "source_ref": f"runs/{trial_id}/trajectory.json",
            "source_digest": "sha256:" + "f" * 64,
            "provenance_kind": "mechanical",
            "trial_id": trial_id,
            "episode_id": f"ep-{index}",
            "session_id": f"sess-{job_id}",
            "subtask_id": "subtask-1",
            "dependency_edge": "depends_on",
            "required_prior_fact": "fact-1",
            "observed_memory_reference": None,
            "progress": "done",
            "outcome": "success",
        }
    if table_name == "evidence_coverage":
        return {
            "source_ref": f"runs/{trial_id}/coverage.json",
            "source_digest": "sha256:" + "1" * 64,
            "provenance_kind": "derived",
            "trial_id": trial_id,
            "benchmark": "swebench",
            "construct": "retrieval",
            "exposed": True,
            "eligible": True,
            "required_evidence": ["e1"],
            "observed_evidence": ["e1"],
            "missing_evidence": [],
            "analysis_ready": True,
        }
    raise ValueError(f"Unknown table name: {table_name}")


def create_uncompacted_job(
    derived_root: Path,
    *,
    job_id: str,
    trial_ids: Sequence[str],
    timestamp: str | None = "2026-08-10T14:30:00Z",
) -> Path:
    """Create a fully populated uncompacted job partition under derived_root."""
    job_dir = derived_root / f"job_id={job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    # Write jobs.parquet
    job_rows = [{"job_id": job_id, "job_name": f"job-{job_id}", "trial_count": len(trial_ids)}]
    job_table = pa.Table.from_pylist(job_rows, schema=TABLE_SCHEMAS["jobs"])
    pq.write_table(job_table, job_dir / "jobs.parquet")

    # Write trial partitions
    for trial_id in trial_ids:
        trial_dir = job_dir / f"trial_id={trial_id}"
        trial_dir.mkdir(parents=True, exist_ok=True)

        for table_name in PROJECTED_TABLE_NAMES:
            if table_name == "jobs":
                continue
            rows = [_make_table_row(table_name, job_id, trial_id, index=1)]
            if table_name == "steps" and timestamp is not None:
                rows[0]["timestamp"] = timestamp
            elif table_name == "steps" and timestamp is None:
                rows[0]["timestamp"] = None
            table = pa.Table.from_pylist(rows, schema=TABLE_SCHEMAS[table_name])
            pq.write_table(table, trial_dir / f"{table_name}.parquet")

    return job_dir


# --------------------------------------------------------------------------- #
# Unit Tests: Date Resolution & Parsing
# --------------------------------------------------------------------------- #


def test_parse_iso_date() -> None:
    assert parse_iso_date("2026-08-15T12:00:00Z") == date(2026, 8, 15)
    assert parse_iso_date("2026-08-15T12:00:00+00:00") == date(2026, 8, 15)
    assert parse_iso_date("2026-08-15") == date(2026, 8, 15)
    assert parse_iso_date(None) is None
    assert parse_iso_date("not-a-date") is None


def test_resolve_job_date_from_steps(tmp_path: Path) -> None:
    job_dir = create_uncompacted_job(
        tmp_path,
        job_id="job-1",
        trial_ids=["trial-1"],
        timestamp="2026-08-12T09:15:00Z",
    )
    resolved = resolve_job_date(job_dir)
    assert resolved == date(2026, 8, 12)


def test_resolve_job_date_from_runs_dir(tmp_path: Path) -> None:
    # steps without timestamp
    job_dir = create_uncompacted_job(
        tmp_path / "derived",
        job_id="job-2",
        trial_ids=["trial-2"],
        timestamp=None,
    )
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)
    job_run_dir = runs_dir / "canary-job-2"
    job_run_dir.mkdir()
    (job_run_dir / "result.json").write_text(
        json.dumps(
            {
                "id": "job-2",
                "started_at": "2026-08-11T10:00:00Z",
                "finished_at": "2026-08-11T10:05:00Z",
            }
        )
    )

    resolved = resolve_job_date(job_dir, runs_dir=runs_dir)
    assert resolved == date(2026, 8, 11)


# --------------------------------------------------------------------------- #
# Unit Tests: Deduplication, Sorting & Writing
# --------------------------------------------------------------------------- #


def test_deduplicate_and_sort_jobs() -> None:
    schema = TABLE_SCHEMAS["jobs"]
    rows = [
        {"job_id": "job-b", "job_name": "b", "trial_count": 1},
        {"job_id": "job-a", "job_name": "a", "trial_count": 2},
        {"job_id": "job-a", "job_name": "a-dup", "trial_count": 2},
    ]
    table = pa.Table.from_pylist(rows, schema=schema)
    deduped = deduplicate_and_sort(table, "jobs")
    assert deduped.num_rows == 2
    assert deduped.column("job_id").to_pylist() == ["job-a", "job-b"]
    assert deduped.schema.equals(schema)


def test_write_compact_table_validation(tmp_path: Path) -> None:
    schema = TABLE_SCHEMAS["jobs"]
    rows = [{"job_id": "j1", "job_name": "n1", "trial_count": 1}]
    table = pa.Table.from_pylist(rows, schema=schema)
    target = tmp_path / "jobs.parquet"
    written_count = write_compact_table(table, target, "jobs")
    assert written_count == 1
    assert target.is_file()

    read_back = pq.read_table(target)
    assert read_back.schema.equals(schema)
    assert read_back.num_rows == 1


def test_write_compact_table_schema_error(tmp_path: Path) -> None:
    # Intentionally invalid schema for "jobs" (missing columns)
    bad_schema = pa.schema([pa.field("wrong_col", pa.string())])
    bad_table = pa.Table.from_pylist([{"wrong_col": "val"}], schema=bad_schema)
    target = tmp_path / "jobs.parquet"

    with pytest.raises(CompactionValidationError, match="Schema integrity mismatch"):
        write_compact_table(bad_table, target, "jobs")

    assert not target.exists()


@pytest.mark.parametrize(
    ("table_name", "removed", "expected"),
    [
        (
            "trajectories",
            {
                "capture_source",
                "retained_request_count",
                "inferred_total_call_lower_bound",
                "assistant_turn_lower_bound",
                "ring_buffer_truncated",
                "unknown_prefix",
                "per_call_metadata_complete",
                "unavailable_call_metadata",
                "retained_request_paths",
                "retained_request_sha256",
                "tools_offered",
                "tools_offered_sha256",
                "harness_fault_signature",
            },
            {"capture_source": None, "tools_offered": None},
        ),
        (
            "steps",
            {
                "llm_source_path",
                "llm_source_sha256",
                "llm_metadata_available",
                "usage_status",
            },
            {"llm_metadata_available": False, "usage_status": None},
        ),
        (
            "llm_calls",
            {"source_sha256", "metadata_available", "usage_status"},
            {"metadata_available": False, "usage_status": None},
        ),
    ],
)
def test_historical_request_metadata_columns_are_defaulted_before_compaction(
    tmp_path: Path,
    table_name: str,
    removed: set[str],
    expected: dict[str, object],
) -> None:
    current_schema = TABLE_SCHEMAS[table_name]
    legacy_schema = pa.schema([field for field in current_schema if field.name not in removed])
    row = _make_table_row(table_name, "legacy-job", "legacy-trial")
    legacy_path = tmp_path / f"{table_name}.parquet"
    pq.write_table(pa.Table.from_pylist([row], schema=legacy_schema), legacy_path)

    migrated = _read_table_or_empty(legacy_path, table_name)

    assert migrated.schema.equals(current_schema)
    migrated_row = migrated.to_pylist()[0]
    assert {name: migrated_row[name] for name in expected} == expected


# --------------------------------------------------------------------------- #
# Integration Tests: Planning & Discovery
# --------------------------------------------------------------------------- #


def test_plan_compaction_and_discovery(tmp_path: Path) -> None:
    derived_root = tmp_path / "derived" / "parquet"
    create_uncompacted_job(
        derived_root,
        job_id="job-1",
        trial_ids=["t1", "t2"],
        timestamp="2026-08-05T10:00:00Z",
    )
    create_uncompacted_job(
        derived_root,
        job_id="job-2",
        trial_ids=["t3"],
        timestamp="2026-08-15T10:00:00Z",
    )

    uncompacted = discover_uncompacted_jobs(derived_root)
    assert len(uncompacted) == 2
    assert {u.job_id for u in uncompacted} == {"job-1", "job-2"}

    today = date(2026, 8, 16)
    plan = plan_compaction(derived_root, clock_today=today, retention_days=7)
    assert len(plan.days) == 2

    day_05 = next(d for d in plan.days if d.dt == "2026-08-05")
    assert day_05.is_closed is True
    assert day_05.is_prunable is True  # > 7 days old
    assert day_05.uncompacted_row_counts["jobs"] == 1
    assert day_05.uncompacted_row_counts["trial_facts"] == 2

    day_15 = next(d for d in plan.days if d.dt == "2026-08-15")
    assert day_15.is_closed is True
    assert day_15.is_prunable is False  # <= 7 days old

    # Check discover_compacted_row_counts before compaction
    pre_counts = discover_compacted_row_counts(derived_root, "2026-08-05")
    assert pre_counts["jobs"] == 0


# --------------------------------------------------------------------------- #
# Integration Tests: End-to-End Compaction
# --------------------------------------------------------------------------- #


def test_compaction_end_to_end(tmp_path: Path) -> None:
    derived_root = tmp_path / "derived" / "parquet"
    # Create 2 jobs for 2026-08-08 (older than 7 days relative to 2026-08-16)
    create_uncompacted_job(
        derived_root,
        job_id="job-old-1",
        trial_ids=["t1", "t2"],
        timestamp="2026-08-08T12:00:00Z",
    )
    create_uncompacted_job(
        derived_root,
        job_id="job-old-2",
        trial_ids=["t3"],
        timestamp="2026-08-08T14:00:00Z",
    )
    # Create 1 job for 2026-08-14 (recent, within 7 days relative to 2026-08-16)
    create_uncompacted_job(
        derived_root,
        job_id="job-recent-1",
        trial_ids=["t4"],
        timestamp="2026-08-14T10:00:00Z",
    )

    today = date(2026, 8, 16)
    result = compact(derived_root, clock_today=today, retention_days=7)

    assert result.ok
    assert len(result.compacted_days) == 2

    # Check 2026-08-08
    day_08 = next(d for d in result.compacted_days if d.dt == "2026-08-08")
    assert day_08.table_row_counts["jobs"] == 2
    assert day_08.table_row_counts["trial_facts"] == 3  # 2 + 1 trials
    assert day_08.table_row_counts["steps"] == 3
    assert set(day_08.pruned_job_ids) == {"job-old-1", "job-old-2"}
    assert day_08.retained_job_ids == ()

    # Verify old granular partitions were pruned
    assert not (derived_root / "job_id=job-old-1").exists()
    assert not (derived_root / "job_id=job-old-2").exists()

    # Check 2026-08-14
    day_14 = next(d for d in result.compacted_days if d.dt == "2026-08-14")
    assert day_14.table_row_counts["jobs"] == 1
    assert day_14.table_row_counts["trial_facts"] == 1
    assert day_14.pruned_job_ids == ()
    assert set(day_14.retained_job_ids) == {"job-recent-1"}

    # Verify recent granular partitions were retained
    assert (derived_root / "job_id=job-recent-1").is_dir()

    # Verify compacted parquet files exist and have exact schemas
    for dt_str in ["2026-08-08", "2026-08-14"]:
        day_dir = derived_root / COMPACT_DIRNAME / f"dt={dt_str}"
        assert day_dir.is_dir()
        for table_name in PROJECTED_TABLE_NAMES:
            file_path = day_dir / f"{table_name}.parquet"
            assert file_path.is_file()
            t = pq.read_table(file_path)
            assert t.schema.equals(TABLE_SCHEMAS[table_name])


def test_idempotent_rerun(tmp_path: Path) -> None:
    derived_root = tmp_path / "derived" / "parquet"
    create_uncompacted_job(
        derived_root,
        job_id="job-1",
        trial_ids=["t1", "t2"],
        timestamp="2026-08-14T10:00:00Z",
    )

    today = date(2026, 8, 16)
    # First run (retains granular partition because dt is within 7 days)
    res1 = compact(derived_root, clock_today=today, retention_days=7)
    assert res1.ok
    assert res1.total_compacted_rows["jobs"] == 1
    assert res1.total_compacted_rows["trial_facts"] == 2

    # Second run over same retained data
    res2 = compact(derived_root, clock_today=today, retention_days=7)
    assert res2.ok
    assert res2.total_compacted_rows["jobs"] == 1
    assert res2.total_compacted_rows["trial_facts"] == 2

    # Verify files on disk match expected counts exactly
    compact_dir = derived_root / COMPACT_DIRNAME / "dt=2026-08-14"
    assert count_table_rows(compact_dir / "jobs.parquet") == 1
    assert count_table_rows(compact_dir / "trial_facts.parquet") == 2


def test_target_date_filtering(tmp_path: Path) -> None:
    derived_root = tmp_path / "derived" / "parquet"
    create_uncompacted_job(
        derived_root,
        job_id="job-1",
        trial_ids=["t1"],
        timestamp="2026-08-13T10:00:00Z",
    )
    create_uncompacted_job(
        derived_root,
        job_id="job-2",
        trial_ids=["t2"],
        timestamp="2026-08-14T10:00:00Z",
    )

    # Compact only 2026-08-13
    res = compact(derived_root, target_date="2026-08-13")
    assert res.ok
    assert len(res.compacted_days) == 1
    assert res.compacted_days[0].dt == "2026-08-13"

    assert (derived_root / COMPACT_DIRNAME / "dt=2026-08-13").is_dir()
    assert not (derived_root / COMPACT_DIRNAME / "dt=2026-08-14").exists()


def test_dry_run_leaves_disk_untouched(tmp_path: Path) -> None:
    derived_root = tmp_path / "derived" / "parquet"
    create_uncompacted_job(
        derived_root,
        job_id="job-old",
        trial_ids=["t1"],
        timestamp="2026-08-01T10:00:00Z",
    )

    today = date(2026, 8, 16)
    res = compact(derived_root, clock_today=today, retention_days=7, dry_run=True)
    assert res.ok
    assert len(res.compacted_days) == 1
    assert res.compacted_days[0].dt == "2026-08-01"

    # Compact dir not created, uncompacted job not pruned
    assert not (derived_root / COMPACT_DIRNAME).exists()
    assert (derived_root / "job_id=job-old").is_dir()


def test_no_prune_flag_retains_old_jobs(tmp_path: Path) -> None:
    derived_root = tmp_path / "derived" / "parquet"
    create_uncompacted_job(
        derived_root,
        job_id="job-old",
        trial_ids=["t1"],
        timestamp="2026-08-01T10:00:00Z",
    )

    today = date(2026, 8, 16)
    res = compact(derived_root, clock_today=today, retention_days=7, prune=False)
    assert res.ok
    assert (derived_root / COMPACT_DIRNAME / "dt=2026-08-01").is_dir()
    assert (derived_root / "job_id=job-old").is_dir()
    assert res.retained_jobs == ("job-old",)
    assert res.pruned_jobs == ()


# --------------------------------------------------------------------------- #
# Integration Tests: DuckDB Hive Partitioning Queries
# --------------------------------------------------------------------------- #


def test_duckdb_hive_partitioning_query(tmp_path: Path) -> None:
    derived_root = tmp_path / "derived" / "parquet"
    create_uncompacted_job(
        derived_root,
        job_id="job-1",
        trial_ids=["t1", "t2"],
        timestamp="2026-08-10T10:00:00Z",
    )
    create_uncompacted_job(
        derived_root,
        job_id="job-2",
        trial_ids=["t3"],
        timestamp="2026-08-11T10:00:00Z",
    )

    compact(derived_root, clock_today=date(2026, 8, 16), retention_days=7)

    con = duckdb.connect()
    glob_pattern = (derived_root / COMPACT_DIRNAME / "dt=*" / "trial_facts.parquet").as_posix()
    query = (
        f"SELECT dt, count(*) as cnt "
        f"FROM read_parquet('{glob_pattern}', hive_partitioning = true) "
        f"GROUP BY dt ORDER BY dt"
    )
    result = con.execute(query).fetchall()

    assert [(str(r[0]), r[1]) for r in result] == [("2026-08-10", 2), ("2026-08-11", 1)]


# --------------------------------------------------------------------------- #
# CLI Tests
# --------------------------------------------------------------------------- #


def test_cli_compact_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    derived_root = tmp_path / "derived" / "parquet"
    create_uncompacted_job(
        derived_root,
        job_id="job-cli-1",
        trial_ids=["t1"],
        timestamp="2026-08-12T10:00:00Z",
    )

    exit_code = main(
        [
            "compact",
            "--derived-dir",
            str(derived_root),
            "--json",
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert len(payload["compacted_days"]) == 1
    assert payload["compacted_days"][0]["dt"] == "2026-08-12"


def test_cli_compact_human_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    derived_root = tmp_path / "derived" / "parquet"
    create_uncompacted_job(
        derived_root,
        job_id="job-cli-2",
        trial_ids=["t1"],
        timestamp="2026-08-12T10:00:00Z",
    )

    exit_code = main(
        [
            "compact",
            "--derived-dir",
            str(derived_root),
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "parquet compaction" in captured.out
    assert "dt=2026-08-12" in captured.out
    assert "total compacted rows" in captured.out


def test_deduplicate_and_sort_semantic_facts() -> None:
    # 1. capability_opportunities
    opp_schema = TABLE_SCHEMAS["capability_opportunities"]
    opp_rows = [
        _make_table_row("capability_opportunities", "j1", "t1", index=2),
        _make_table_row("capability_opportunities", "j1", "t1", index=1),
        _make_table_row("capability_opportunities", "j1", "t1", index=1),  # duplicate
    ]
    opp_table = pa.Table.from_pylist(opp_rows, schema=opp_schema)
    opp_deduped = deduplicate_and_sort(opp_table, "capability_opportunities")
    assert opp_deduped.num_rows == 2
    assert opp_deduped.column("opportunity_id").to_pylist() == ["opp-1", "opp-2"]
    assert opp_deduped.schema.equals(opp_schema)

    # 2. process_step_facts
    step_schema = TABLE_SCHEMAS["process_step_facts"]
    step_rows = [
        _make_table_row("process_step_facts", "j1", "t1", index=2),
        _make_table_row("process_step_facts", "j1", "t1", index=1),
        _make_table_row("process_step_facts", "j1", "t1", index=1),
    ]
    step_table = pa.Table.from_pylist(step_rows, schema=step_schema)
    step_deduped = deduplicate_and_sort(step_table, "process_step_facts")
    assert step_deduped.num_rows == 2
    assert step_deduped.column("source_step_id").to_pylist() == ["step-1", "step-2"]

    # 3. constraint_facts
    const_schema = TABLE_SCHEMAS["constraint_facts"]
    const_rows = [
        _make_table_row("constraint_facts", "j1", "t1", index=2),
        _make_table_row("constraint_facts", "j1", "t1", index=1),
        _make_table_row("constraint_facts", "j1", "t1", index=1),
    ]
    const_table = pa.Table.from_pylist(const_rows, schema=const_schema)
    const_deduped = deduplicate_and_sort(const_table, "constraint_facts")
    assert const_deduped.num_rows == 2
    assert const_deduped.column("constraint_id").to_pylist() == ["const-1", "const-2"]

    # 4. context_operation_facts
    ctx_schema = TABLE_SCHEMAS["context_operation_facts"]
    ctx_rows = [
        _make_table_row("context_operation_facts", "j1", "t1", index=2),
        _make_table_row("context_operation_facts", "j1", "t1", index=1),
        _make_table_row("context_operation_facts", "j1", "t1", index=1),
    ]
    ctx_table = pa.Table.from_pylist(ctx_rows, schema=ctx_schema)
    ctx_deduped = deduplicate_and_sort(ctx_table, "context_operation_facts")
    assert ctx_deduped.num_rows == 2
    assert ctx_deduped.column("operation_id").to_pylist() == ["op-1", "op-2"]

    # 5. paired_condition_facts
    pair_schema = TABLE_SCHEMAS["paired_condition_facts"]
    pair_rows = [
        _make_table_row("paired_condition_facts", "j1", "t1", index=2),
        _make_table_row("paired_condition_facts", "j1", "t1", index=1),
        _make_table_row("paired_condition_facts", "j1", "t1", index=1),
    ]
    pair_table = pa.Table.from_pylist(pair_rows, schema=pair_schema)
    pair_deduped = deduplicate_and_sort(pair_table, "paired_condition_facts")
    assert pair_deduped.num_rows == 2
    assert pair_deduped.column("pair_id").to_pylist() == ["pair-1", "pair-2"]

    # 6. session_dependency_facts
    sess_schema = TABLE_SCHEMAS["session_dependency_facts"]
    sess_rows = [
        _make_table_row("session_dependency_facts", "j1", "t1", index=2),
        _make_table_row("session_dependency_facts", "j1", "t1", index=1),
        _make_table_row("session_dependency_facts", "j1", "t1", index=1),
    ]
    sess_table = pa.Table.from_pylist(sess_rows, schema=sess_schema)
    sess_deduped = deduplicate_and_sort(sess_table, "session_dependency_facts")
    assert sess_deduped.num_rows == 2
    assert sess_deduped.column("episode_id").to_pylist() == ["ep-1", "ep-2"]

    # 7. evidence_coverage
    cov_schema = TABLE_SCHEMAS["evidence_coverage"]
    cov_row1 = _make_table_row("evidence_coverage", "j1", "t1", index=1)
    cov_row1["construct"] = "retrieval"
    cov_row2 = _make_table_row("evidence_coverage", "j1", "t1", index=2)
    cov_row2["construct"] = "planning"
    cov_row3 = _make_table_row("evidence_coverage", "j1", "t1", index=3)
    cov_row3["construct"] = "retrieval"  # duplicate key (trial_id, benchmark, construct)
    cov_table = pa.Table.from_pylist([cov_row1, cov_row2, cov_row3], schema=cov_schema)
    cov_deduped = deduplicate_and_sort(cov_table, "evidence_coverage")
    assert cov_deduped.num_rows == 2
    assert cov_deduped.column("construct").to_pylist() == ["planning", "retrieval"]


def test_behavior_episodes_not_compacted() -> None:
    from evallab.storage.parquet_compaction import (
        PRIMARY_KEYS,
        PROJECTED_TABLE_NAMES,
        TRIAL_TABLE_NAMES,
    )

    assert "behavior_episodes" not in PROJECTED_TABLE_NAMES
    assert "behavior_episodes" not in TRIAL_TABLE_NAMES
    assert "behavior_episodes" not in PRIMARY_KEYS


def test_retrieval_facts_not_compacted_without_immutable_identity() -> None:
    from evallab.semantic_facts import SEMANTIC_FACT_SCHEMAS
    from evallab.storage.parquet_compaction import (
        PRIMARY_KEYS,
        PROJECTED_TABLE_NAMES,
        TRIAL_TABLE_NAMES,
    )

    assert "retrieval_facts" not in PROJECTED_TABLE_NAMES
    assert "retrieval_facts" not in TRIAL_TABLE_NAMES
    assert "retrieval_facts" not in PRIMARY_KEYS
    retrieval_schema = SEMANTIC_FACT_SCHEMAS["retrieval_facts"]
    assert retrieval_schema.get_field_index("retrieval_id") == -1
    assert all(
        retrieval_schema.field(name).nullable
        for name in (
            "query_id",
            "call_id",
            "result_id",
            "document_id",
            "file_id",
            "block_id",
            "line_id",
        )
    )
