"""Focused deterministic tests for incremental ingest of promoted ATIF bundles."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest

from evallab.storage.incremental_ingest import (
    DIGEST_INDEX_FILENAME,
    PROMOTED_BUNDLES_DIRNAME,
    compute_bundle_digest,
    ingest_promoted_bundles,
    load_digest_index,
)


def _write_minimal_bundle(
    runs_root: Path,
    bundle_name: str,
    *,
    task_name: str = "test-task",
    trial_name: str = "trial-01",
    reward: float = 1.0,
    steps_count: int = 2,
    include_raw_log: bool = False,
    include_symlink: bool = False,
    include_r4_sidecar: bool = False,
    schema_version: int | str = 2,
) -> Path:
    """Helper to create a self-contained, valid promoted Harbor job tree with PROMOTION.json."""
    bundle_dir = runs_root / bundle_name
    trial_dir = bundle_dir / trial_name
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    # 1. Job level result/config/lock
    job_result = {
        "id": f"job-{bundle_name}",
        "started_at": "2026-08-30T00:00:00Z",
        "finished_at": "2026-08-30T00:01:00Z",
        "n_total_trials": 1,
        "stats": {"passed": 1},
    }
    (bundle_dir / "result.json").write_text(json.dumps(job_result), encoding="utf-8")
    (bundle_dir / "config.json").write_text("{}", encoding="utf-8")
    (bundle_dir / "lock.json").write_text("{}", encoding="utf-8")

    # 2. Trial level result/config/lock
    trial_result = {
        "id": f"trial-{bundle_name}-{trial_name}",
        "trial_name": trial_name,
        "task_name": task_name,
        "task_checksum": "sha256:1234567890abcdef",
        "started_at": "2026-08-30T00:00:00Z",
        "finished_at": "2026-08-30T00:01:00Z",
        "verifier_result": {"rewards": {"reward": reward}},
    }
    (trial_dir / "result.json").write_text(json.dumps(trial_result), encoding="utf-8")
    (trial_dir / "config.json").write_text("{}", encoding="utf-8")
    (trial_dir / "lock.json").write_text("{}", encoding="utf-8")

    # 3. ATIF v1.7 trajectory
    steps = []
    for step_id in range(1, steps_count + 1):
        steps.append(
            {
                "step_id": step_id,
                "source": "agent",
                "message": f"Step {step_id} action",
                "tool_calls": [
                    {
                        "tool_call_id": f"call_{step_id}",
                        "function_name": "bash",
                        "arguments": {"command": "echo ok"},
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": f"call_{step_id}",
                            "content": "ok\n",
                        }
                    ]
                },
                "metrics": {"prompt_tokens": 100, "completion_tokens": 50},
            }
        )

    trajectory_payload = {
        "schema_version": "ATIF-v1.7",
        "session_id": f"sess-{bundle_name}",
        "trajectory_id": f"traj-{bundle_name}",
        "agent": {"name": "test-agent", "version": "1.0"},
        "steps": steps,
        "final_metrics": {
            "total_prompt_tokens": 100 * steps_count,
            "total_completion_tokens": 50 * steps_count,
        },
    }
    (agent_dir / "trajectory.json").write_text(json.dumps(trajectory_payload), encoding="utf-8")

    # Optional R4 quota sidecar
    if include_r4_sidecar:
        quota_dir = agent_dir / "quota"
        quota_dir.mkdir(parents=True, exist_ok=True)
        quota_payload = {
            "schema_version": 1,
            "rule": "R4",
            "source_path": f"{trial_name}/agent/sessions/rollout-01.jsonl",
            "snapshots": [{"timestamp": "2026-08-30T00:00:00Z", "used_percent": 10}],
        }
        (quota_dir / "rollout-01.rate-limits.json").write_text(
            json.dumps(quota_payload), encoding="utf-8"
        )

    # Optional security defect: raw log or symlink
    if include_raw_log:
        (bundle_dir / "job.log").write_text("unredacted raw log", encoding="utf-8")
    if include_symlink:
        link_target = agent_dir / "trajectory.json"
        link_path = bundle_dir / "leak_symlink"
        link_path.symlink_to(link_target)

    # 4. PROMOTION.json manifest
    manifest_files = [
        {
            "source_path": f"{trial_name}/agent/trajectory.json",
            "promoted_path": f"{trial_name}/agent/trajectory.json",
            "action": "redacted",
            "rule": "R1",
            "source_bytes": 500,
            "source_sha256": "sha256:origtraj123",
            "promoted_bytes": len(json.dumps(trajectory_payload)),
            "promoted_sha256": "sha256:promotedtraj123",
        },
        {
            "source_path": f"{trial_name}/agent/sessions/session.json",
            "promoted_path": None,
            "action": "omitted",
            "rule": "R2",
            "entry_type": "file",
            "source_bytes": 1024,
            "source_sha256": "sha256:omittedsession123",
        },
    ]

    if include_r4_sidecar:
        # Both the omitted rollout and its derived R4 sidecar share source_path
        manifest_files.append(
            {
                "source_path": f"{trial_name}/agent/sessions/rollout-01.jsonl",
                "promoted_path": None,
                "action": "omitted",
                "rule": "R2",
                "entry_type": "file",
                "source_bytes": 2048,
                "source_sha256": "sha256:rawrollout123",
            }
        )
        manifest_files.append(
            {
                "source_path": f"{trial_name}/agent/sessions/rollout-01.jsonl",
                "promoted_path": f"{trial_name}/agent/quota/rollout-01.rate-limits.json",
                "action": "redacted",
                "rule": "R4",
                "derived_from": f"{trial_name}/agent/sessions/rollout-01.jsonl",
                "source_bytes": 2048,
                "source_sha256": "sha256:rawrollout123",
                "promoted_bytes": 150,
                "promoted_sha256": "sha256:quotasidecar123",
            }
        )

    manifest = {
        "schema_version": schema_version,
        "bundle": bundle_name,
        "source_job_runtime_path": f"runs/{bundle_name}",
        "source_job_result_sha256": "sha256:resultsha123",
        "files": manifest_files,
    }
    (bundle_dir / "PROMOTION.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return bundle_dir


# --------------------------------------------------------------------------- #
# Test Scenarios
# --------------------------------------------------------------------------- #


def test_cold_ingest_processes_all_bundles(tmp_path: Path) -> None:
    """Cold ingest on an empty index must project every candidate bundle."""
    runs_root = tmp_path / "runs"
    derived_root = tmp_path / "derived"

    _write_minimal_bundle(runs_root, "bundle-alpha")
    _write_minimal_bundle(runs_root, "bundle-beta")

    result = ingest_promoted_bundles(runs_root, derived_root)

    assert result.performance.scanned_bundles == 2
    assert result.performance.changed_bundles == 2
    assert result.performance.skipped_bundles == 0
    assert result.performance.rejected_bundles == 0
    assert len(result.dispositions) == 2
    assert all(d.outcome == "changed" for d in result.dispositions)

    # Verify Parquet outputs exist
    alpha_dir = derived_root / PROMOTED_BUNDLES_DIRNAME / "bundle-alpha"
    assert (alpha_dir / "trajectories.parquet").is_file()
    assert (alpha_dir / "steps.parquet").is_file()
    assert (alpha_dir / "promotion_lineage.parquet").is_file()
    assert (alpha_dir / "promotion_omissions.parquet").is_file()

    # Verify digest index was saved
    index = load_digest_index(result.index_path)
    assert "bundle-alpha" in index.entries
    assert "bundle-beta" in index.entries


def test_warm_ingest_skips_unchanged_bundles(tmp_path: Path) -> None:
    """Warm ingest when no artifacts changed must skip all bundles."""
    runs_root = tmp_path / "runs"
    derived_root = tmp_path / "derived"

    _write_minimal_bundle(runs_root, "bundle-alpha")
    _write_minimal_bundle(runs_root, "bundle-beta")

    # Initial cold run
    first_result = ingest_promoted_bundles(runs_root, derived_root)
    assert first_result.performance.changed_bundles == 2

    # Second warm run
    second_result = ingest_promoted_bundles(runs_root, derived_root)
    assert second_result.performance.scanned_bundles == 2
    assert second_result.performance.changed_bundles == 0
    assert second_result.performance.skipped_bundles == 2
    assert second_result.performance.promoted_files_skipped > 0
    assert all(d.outcome == "skipped" for d in second_result.dispositions)


def test_changed_one_bundle_reingests_only_modified(tmp_path: Path) -> None:
    """When one bundle changes, only that bundle is re-ingested while others are skipped."""
    runs_root = tmp_path / "runs"
    derived_root = tmp_path / "derived"

    _write_minimal_bundle(runs_root, "bundle-alpha", steps_count=2)
    _write_minimal_bundle(runs_root, "bundle-beta", steps_count=2)

    # Cold run
    ingest_promoted_bundles(runs_root, derived_root)

    # Modify bundle-beta (add steps and update manifest)
    _write_minimal_bundle(runs_root, "bundle-beta", steps_count=5)

    result = ingest_promoted_bundles(runs_root, derived_root)
    assert result.performance.scanned_bundles == 2
    assert result.performance.changed_bundles == 1
    assert result.performance.skipped_bundles == 1

    disposition_map = {d.bundle_name: d.outcome for d in result.dispositions}
    assert disposition_map["bundle-alpha"] == "skipped"
    assert disposition_map["bundle-beta"] == "changed"

    # Check updated steps parquet for bundle-beta
    beta_steps = pq.read_table(
        derived_root / PROMOTED_BUNDLES_DIRNAME / "bundle-beta" / "steps.parquet"
    )
    assert beta_steps.num_rows == 5


def test_rollback_on_projection_failure_preserves_prior_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If bundle projection fails midway, prior partitions and index are intact."""
    runs_root = tmp_path / "runs"
    derived_root = tmp_path / "derived"

    _write_minimal_bundle(runs_root, "bundle-alpha", steps_count=2)
    ingest_promoted_bundles(runs_root, derived_root)

    initial_index = load_digest_index(derived_root / DIGEST_INDEX_FILENAME)
    initial_digest = initial_index.entries["bundle-alpha"]
    initial_steps = pq.read_table(
        derived_root / PROMOTED_BUNDLES_DIRNAME / "bundle-alpha" / "steps.parquet"
    )
    assert initial_steps.num_rows == 2

    # Modify bundle-alpha
    _write_minimal_bundle(runs_root, "bundle-alpha", steps_count=4)

    # Inject failure during projection
    from evallab.storage import incremental_ingest

    def failing_project_trial(*args, **kwargs):
        raise RuntimeError("simulated atomic crash")

    monkeypatch.setattr(incremental_ingest, "project_trial", failing_project_trial)

    failed_result = ingest_promoted_bundles(runs_root, derived_root)
    assert failed_result.performance.failed_bundles == 1
    assert failed_result.performance.changed_bundles == 0

    # Digest index was NOT updated with the new digest
    retained_index = load_digest_index(derived_root / DIGEST_INDEX_FILENAME)
    assert retained_index.entries["bundle-alpha"] == initial_digest

    # Previous partition is completely preserved by adjacent rollback swap
    retained_steps = pq.read_table(
        derived_root / PROMOTED_BUNDLES_DIRNAME / "bundle-alpha" / "steps.parquet"
    )
    assert retained_steps.num_rows == 2

    # No leftover staging or backup directories
    staging_dir = derived_root / PROMOTED_BUNDLES_DIRNAME / "bundle-alpha.staging"
    backup_dir = derived_root / PROMOTED_BUNDLES_DIRNAME / "bundle-alpha.backup"
    assert not staging_dir.exists()
    assert not backup_dir.exists()


def test_r4_quota_sidecar_accepted_and_version_compatibility(tmp_path: Path) -> None:
    """R4 quota sidecars with sessions in source_path and supported versions are accepted."""
    runs_root = tmp_path / "runs"
    derived_root = tmp_path / "derived"

    # Bundle with R4 quota sidecars and schema version 1 (supported)
    _write_minimal_bundle(runs_root, "bundle-quota", include_r4_sidecar=True, schema_version=1)

    result = ingest_promoted_bundles(runs_root, derived_root)
    assert result.performance.rejected_bundles == 0
    assert result.performance.changed_bundles == 1
    assert result.dispositions[0].outcome == "changed"

    # Check lineage contains the R4 sidecar
    partition_dir = derived_root / PROMOTED_BUNDLES_DIRNAME / "bundle-quota"
    lineage = pq.read_table(partition_dir / "promotion_lineage.parquet").to_pylist()
    r4_entries = [e for e in lineage if e.get("rule") == "R4"]
    assert len(r4_entries) == 1
    assert r4_entries[0]["action"] == "redacted"
    assert "quota/rollout-01.rate-limits.json" in r4_entries[0]["promoted_path"]


def test_security_rejection_symlink(tmp_path: Path) -> None:
    """A bundle containing physical symlinks must be rejected fail-closed."""
    runs_root = tmp_path / "runs"
    derived_root = tmp_path / "derived"

    _write_minimal_bundle(runs_root, "bundle-hostile", include_symlink=True)

    result = ingest_promoted_bundles(runs_root, derived_root)
    assert result.performance.rejected_bundles == 1
    assert result.performance.changed_bundles == 0

    disposition = result.dispositions[0]
    assert disposition.outcome == "rejected"
    assert "security_symlink_detected" in (disposition.reason or "")

    # Partition was not written
    partition_dir = derived_root / PROMOTED_BUNDLES_DIRNAME / "bundle-hostile"
    assert not partition_dir.exists()


def test_security_rejection_raw_log_path(tmp_path: Path) -> None:
    """A bundle containing physical raw logs must be rejected fail-closed."""
    runs_root = tmp_path / "runs"
    derived_root = tmp_path / "derived"

    _write_minimal_bundle(runs_root, "bundle-raw-log", include_raw_log=True)

    result = ingest_promoted_bundles(runs_root, derived_root)
    assert result.performance.rejected_bundles == 1
    assert result.performance.changed_bundles == 0

    disposition = result.dispositions[0]
    assert disposition.outcome == "rejected"
    assert "security_raw_log_file_present" in (disposition.reason or "")


def test_deterministic_digest_tie_break(tmp_path: Path) -> None:
    """Duplicate source_paths in manifests produce identical digests regardless of list order."""
    entry_a = {
        "source_path": "trial/agent/sessions/rollout.jsonl",
        "promoted_path": None,
        "action": "omitted",
        "rule": "R2",
        "entry_type": "file",
        "source_bytes": 100,
        "source_sha256": "sha256:111",
    }
    entry_b = {
        "source_path": "trial/agent/sessions/rollout.jsonl",
        "promoted_path": "trial/agent/quota/rollout.rate-limits.json",
        "action": "redacted",
        "rule": "R4",
        "source_bytes": 100,
        "source_sha256": "sha256:111",
        "promoted_bytes": 50,
        "promoted_sha256": "sha256:222",
    }

    manifest_1 = {
        "schema_version": 2,
        "bundle": "b1",
        "source_job_result_sha256": "sha256:333",
        "files": [entry_a, entry_b],
    }
    manifest_2 = {
        "schema_version": 2,
        "bundle": "b1",
        "source_job_result_sha256": "sha256:333",
        "files": [entry_b, entry_a],
    }

    assert compute_bundle_digest(manifest_1) == compute_bundle_digest(manifest_2)


def test_sql_views_pre_aggregation_and_no_fan_out(tmp_path: Path) -> None:
    """SQL views in sql/incremental_ingest_views.sql execute cleanly in DuckDB with no Cartesian fanout."""
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "incremental_ingest_views.sql"
    sql_ddl = sql_path.read_text(encoding="utf-8")

    conn = duckdb.connect(":memory:")
    conn.execute(sql_ddl)

    # Insert test rows into fallback schema tables
    conn.execute("INSERT INTO promoted_bundles_index VALUES ('b1', 'sha256:dig1', 'sha256:res1')")
    conn.execute("INSERT INTO jobs VALUES ('job-b1', 'b1', 1)")
    conn.execute("INSERT INTO trajectories VALUES ('job-b1', 't1', 'doc1', 'valid', 5, 2)")
    # 2 lineage records
    conn.execute(
        "INSERT INTO promotion_lineage VALUES ('b1', 'f1', 'f1', 'verbatim', 'R0', 10, 'sha256:a', 10, 'sha256:a')"
    )
    conn.execute(
        "INSERT INTO promotion_lineage VALUES ('b1', 'f2', 'f2', 'redacted', 'R1', 20, 'sha256:b', 15, 'sha256:c')"
    )
    # 2 omissions records
    conn.execute(
        "INSERT INTO promotion_omissions VALUES ('b1', 'f3', 'R2', 'file', NULL, 30, 'sha256:d')"
    )
    conn.execute(
        "INSERT INTO promotion_omissions VALUES ('b1', 'f4', 'R2', 'file', NULL, 40, 'sha256:e')"
    )

    rows = conn.execute("SELECT * FROM v_incremental_ingest_reconciliation").fetchall()
    assert len(rows) == 1
    row = rows[0]
    # columns: bundle_name, bundle_digest, job_id, trial_count, total_source_files, verbatim_files, redacted_files, omitted_files, projected_trajectories, projection_status
    assert row[0] == "b1"
    assert row[2] == "job-b1"
    assert row[4] == 2  # total_source_files exactly 2 (not 2 * 2 = 4)
    assert row[5] == 1  # verbatim_files exactly 1
    assert row[6] == 1  # redacted_files exactly 1
    assert row[7] == 2  # omitted_files exactly 2 (not 2 * 2 = 4)
    assert row[8] == 1  # projected_trajectories exactly 1
    assert row[9] == "fully_projected"

    summary_rows = conn.execute("SELECT * FROM v_incremental_ingest_summary").fetchall()
    assert len(summary_rows) == 1
    assert summary_rows[0][0] == 1  # total_indexed_bundles
    assert summary_rows[0][4] == 1  # fully_projected_bundles
