"""Property-based tests for Parquet compaction idempotence, byte stability, and zero row loss."""

from __future__ import annotations

import hashlib
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from evallab.parquet_compaction import (
    COMPACT_DIRNAME,
    PRIMARY_KEYS,
    PROJECTED_TABLE_NAMES,
    TABLE_SCHEMAS,
    compact,
    count_table_rows,
    deduplicate_and_sort,
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_row(table_name: str, job_id: str, trial_id: str, index: int = 1) -> dict[str, Any]:
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
            "timestamp": "2026-08-16T10:00:00Z",
            "model_name": "default",
            "is_copied_context": False,
            "llm_call_count": 1,
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "cached_tokens": 0,
            "cost_usd": 0.002,
            "tool_call_count": 1,
            "observation_count": 1,
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
            "reward_name": f"reward-{index}",
            "reward_value": 1.0,
        }
    if table_name == "artifact_facts":
        return {
            "experiment_id": "exp-001",
            "job_id": job_id,
            "trial_id": trial_id,
            "source": f"/workspace/result-{index}.txt",
            "destination": f"result-{index}.txt",
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
            "function_name": f"fn-{index}",
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
            "event_count": index,
            "first_event_at": "2026-08-10T10:00:00Z",
            "last_event_at": "2026-08-10T10:00:01Z",
            "journal_status": "available",
        }
    if table_name == "state_events":
        return {
            "experiment_id": "exp-001",
            "job_id": job_id,
            "trial_id": trial_id,
            "sequence": index,
            "precedence": index,
            "event_at": "2026-08-10T10:00:00Z",
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
            "timestamp": "2026-08-10T10:00:00Z",
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
            "timestamp": "2026-08-10T10:00:00Z",
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
            "timestamp": "2026-08-10T10:00:00Z",
            "model_name": "default",
            "call_count": 1,
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "cached_tokens": 0,
            "cost_usd": 0.002,
            "projection_status": "projected",
            "source_path": f"/path/to/doc-{index}.json",
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
            "first_event_at": "2026-08-10T10:00:00Z",
            "last_event_at": "2026-08-10T10:00:01Z",
            "link_status": "linked",
            "link_method": "latest_preceding_action",
        }
    raise ValueError(f"Unknown table: {table_name}")


def _write_job(
    derived_root: Path,
    job_id: str,
    dt_iso: str,
    trial_count: int = 1,
    rows_per_table: int = 2,
) -> Path:
    job_dir = derived_root / f"job_id={job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    # jobs.parquet
    job_row = _make_row("jobs", job_id, "t1", 1)
    table = pa.Table.from_pylist([job_row], schema=TABLE_SCHEMAS["jobs"])
    pq.write_table(table, job_dir / "jobs.parquet")

    # trial tables
    for t_idx in range(1, trial_count + 1):
        trial_id = f"trial-{t_idx}"
        trial_dir = job_dir / f"trial_id={trial_id}"
        trial_dir.mkdir(parents=True, exist_ok=True)

        for table_name in PROJECTED_TABLE_NAMES:
            if table_name == "jobs":
                continue
            rows = []
            for r_idx in range(1, rows_per_table + 1):
                row = _make_row(table_name, job_id, trial_id, r_idx)
                if "timestamp" in row:
                    row["timestamp"] = f"{dt_iso}T10:00:00Z"
                if "started_at" in row:
                    row["started_at"] = f"{dt_iso}T10:00:00Z"
                rows.append(row)
            t = pa.Table.from_pylist(rows, schema=TABLE_SCHEMAS[table_name])
            pq.write_table(t, trial_dir / f"{table_name}.parquet")

    return job_dir


# --- Standalone Invariant Properties ---


@given(
    st.integers(min_value=1, max_value=3),
    st.integers(min_value=1, max_value=2),
    st.integers(min_value=1, max_value=4),
)
@settings(max_examples=30, deadline=None)
def test_property_compaction_idempotence_and_byte_stability(
    num_jobs: int,
    num_trials: int,
    rows_per_table: int,
) -> None:
    """Recompacting an already-compacted day produces identical row counts and byte-identical
    parquet files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        derived_root = Path(tmpdir)
        target_dt = "2026-08-15"

        for j in range(1, num_jobs + 1):
            _write_job(
                derived_root,
                job_id=f"job{j:03d}",
                dt_iso=target_dt,
                trial_count=num_trials,
                rows_per_table=rows_per_table,
            )

        # First compaction
        res1 = compact(derived_root, prune=False, target_date=target_dt)
        assert res1.ok
        day_dir = derived_root / COMPACT_DIRNAME / f"dt={target_dt}"
        assert day_dir.is_dir()

        # Capture file hashes and row counts
        hashes1: dict[str, str] = {}
        counts1: dict[str, int] = {}
        for table_name in PROJECTED_TABLE_NAMES:
            p = day_dir / f"{table_name}.parquet"
            assert p.is_file(), f"Missing compacted table {table_name}"
            hashes1[table_name] = _sha256_file(p)
            counts1[table_name] = count_table_rows(p)

        # Second compaction (idempotent re-run)
        res2 = compact(derived_root, prune=False, target_date=target_dt)
        assert res2.ok

        # Assert byte stability (exact same SHA256 digest)
        hashes2: dict[str, str] = {}
        counts2: dict[str, int] = {}
        for table_name in PROJECTED_TABLE_NAMES:
            p = day_dir / f"{table_name}.parquet"
            hashes2[table_name] = _sha256_file(p)
            counts2[table_name] = count_table_rows(p)

        assert counts2 == counts1, f"Row count changed on recompaction: {counts2} != {counts1}"
        assert hashes2 == hashes1, f"File bytes changed on recompaction: {hashes2} != {hashes1}"


@given(
    st.integers(min_value=1, max_value=4),
    st.integers(min_value=1, max_value=3),
)
@settings(max_examples=30, deadline=None)
def test_property_zero_row_loss_and_pk_deduplication(
    num_jobs: int,
    rows_per_table: int,
) -> None:
    """Compacted table has exactly the count of distinct primary keys across source jobs,
    with zero row loss."""
    with tempfile.TemporaryDirectory() as tmpdir:
        derived_root = Path(tmpdir)
        target_dt = "2026-08-14"

        # Write jobs with deterministic rows
        for j in range(1, num_jobs + 1):
            _write_job(
                derived_root,
                job_id=f"job{j:02d}",
                dt_iso=target_dt,
                trial_count=2,
                rows_per_table=rows_per_table,
            )

        res = compact(derived_root, prune=False, target_date=target_dt)
        assert res.ok

        day_dir = derived_root / COMPACT_DIRNAME / f"dt={target_dt}"

        # Verify zero row loss per table
        for table_name in PROJECTED_TABLE_NAMES:
            compact_file = day_dir / f"{table_name}.parquet"
            compacted_table = pq.read_table(compact_file)

            # Query source row count via DuckDB
            con = duckdb.connect(database=":memory:")
            if table_name == "jobs":
                src_glob = str(derived_root / "job_id=*" / "jobs.parquet")
            else:
                src_glob = str(derived_root / "job_id=*" / "trial_id=*" / f"{table_name}.parquet")

            pk_cols = ", ".join(PRIMARY_KEYS[table_name])
            expected_distinct_count = con.execute(
                f"SELECT count(DISTINCT ({pk_cols})) FROM read_parquet('{src_glob}')"
            ).fetchone()[0]

            assert compacted_table.num_rows == expected_distinct_count, (
                f"Row loss in {table_name}: compacted has {compacted_table.num_rows}, "
                f"expected {expected_distinct_count}"
            )


@given(
    st.sampled_from(list(PROJECTED_TABLE_NAMES)),
    st.lists(
        st.tuples(
            st.text(alphabet="abcdef123456", min_size=2, max_size=6),
            st.text(alphabet="abcdef123456", min_size=2, max_size=6),
            st.integers(min_value=1, max_value=5),
        ),
        min_size=1,
        max_size=20,
    ),
)
@settings(max_examples=50, deadline=None)
def test_property_deduplicate_and_sort_invariants(
    table_name: str,
    key_tuples: list[tuple[str, str, int]],
) -> None:
    """deduplicate_and_sort strictly eliminates duplicate primary keys and sorts
    deterministically."""
    rows = [_make_row(table_name, j_id, t_id, idx) for j_id, t_id, idx in key_tuples]
    table = pa.Table.from_pylist(rows, schema=TABLE_SCHEMAS[table_name])

    deduped = deduplicate_and_sort(table, table_name)

    # Invariant 1: No duplicate primary keys
    con = duckdb.connect(database=":memory:")
    con.register("deduped", deduped)
    pk_cols = ", ".join(PRIMARY_KEYS[table_name])
    dups = con.execute(
        f"SELECT {pk_cols}, count(*) FROM deduped GROUP BY {pk_cols} HAVING count(*) > 1"
    ).fetchall()
    assert dups == [], f"Duplicates found after deduplication in {table_name}: {dups}"

    # Invariant 2: Idempotence: dedup(dedup(t)) == dedup(t)
    deduped_again = deduplicate_and_sort(deduped, table_name)
    assert deduped_again.equals(deduped)


def test_deduplication_retains_the_same_conflicting_row_for_any_input_order() -> None:
    first = _make_row("agent_actions", "job-1", "trial-1")
    first["source_path"] = "z-source.json"
    second = {**first, "source_path": "a-source.json"}

    forward = pa.Table.from_pylist([first, second], schema=TABLE_SCHEMAS["agent_actions"])
    reverse = pa.Table.from_pylist([second, first], schema=TABLE_SCHEMAS["agent_actions"])

    forward_result = deduplicate_and_sort(forward, "agent_actions")
    reverse_result = deduplicate_and_sort(reverse, "agent_actions")

    assert forward_result.equals(reverse_result)
    assert forward_result.to_pylist()[0]["source_path"] == "a-source.json"


# --- Stateful Retention & Pruning Fuzz ---


class CompactionRetentionStateMachine(RuleBasedStateMachine):
    """Fuzzes dynamic job additions, compaction across dates, pruning older than retention days,
    and row conservation."""

    def __init__(self) -> None:
        super().__init__()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.clock_today = date(2026, 8, 20)
        self.retention_days = 7
        self.next_job_id = 0
        self.created_jobs: dict[str, str] = {}  # job_id -> dt_iso

    def teardown(self) -> None:
        self.tempdir.cleanup()

    @rule(
        day_offset=st.integers(min_value=-15, max_value=0),
        rows=st.integers(min_value=1, max_value=2),
    )
    def add_job(self, day_offset: int, rows: int) -> None:
        self.next_job_id += 1
        job_id = f"job{self.next_job_id:04d}"
        job_date = self.clock_today + timedelta(days=day_offset)
        dt_iso = job_date.isoformat()

        _write_job(
            self.root,
            job_id=job_id,
            dt_iso=dt_iso,
            trial_count=1,
            rows_per_table=rows,
        )
        self.created_jobs[job_id] = dt_iso

    @rule(prune=st.booleans())
    def run_compact(self, prune: bool) -> None:
        res = compact(
            self.root,
            retention_days=self.retention_days,
            clock_today=self.clock_today,
            prune=prune,
        )
        assert res.ok

    @invariant()
    def pruned_jobs_are_strictly_older_than_cutoff(self) -> None:
        for job_dir in self.root.glob("job_id=*"):
            assert job_dir.is_dir()

    @invariant()
    def compact_tables_exist_and_non_empty_for_all_compacted_dates(self) -> None:
        compact_root = self.root / COMPACT_DIRNAME
        if not compact_root.is_dir():
            return
        for day_dir in compact_root.glob("dt=*"):
            for table_name in PROJECTED_TABLE_NAMES:
                table_file = day_dir / f"{table_name}.parquet"
                if table_file.is_file():
                    rows = count_table_rows(table_file)
                    assert rows >= 0


TestCompactionProperties = CompactionRetentionStateMachine.TestCase
TestCompactionProperties.settings = settings(
    max_examples=50, stateful_step_count=20, deadline=None
)
