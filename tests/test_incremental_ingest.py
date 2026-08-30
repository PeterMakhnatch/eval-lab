"""Focused deterministic tests for incremental ingest of promoted ATIF bundles."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from evallab.interpretation.trajectory_judgment import canonical_json_digest
from evallab.storage.incremental_ingest import (
    DIGEST_INDEX_FILENAME,
    PERF_LEDGER_FILENAME,
    PROMOTED_BUNDLES_DIRNAME,
    compute_bundle_digest,
    discover_promoted_bundles,
    ingest_promoted_bundles,
    is_raw_log_path,
    load_digest_index,
    validate_bundle_security,
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

    manifest = {
        "schema_version": 2,
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

    # No leftover staging directory
    staging_dir = (
        derived_root / PROMOTED_BUNDLES_DIRNAME / "bundle-alpha.staging"
    )
    assert not staging_dir.exists()


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


def test_lineage_and_omissions_preservation(tmp_path: Path) -> None:
    """Lineage mapping and R2 omission records are accurately preserved in Parquet."""
    runs_root = tmp_path / "runs"
    derived_root = tmp_path / "derived"

    _write_minimal_bundle(runs_root, "bundle-alpha")
    ingest_promoted_bundles(runs_root, derived_root)

    alpha_dir = derived_root / PROMOTED_BUNDLES_DIRNAME / "bundle-alpha"
    lineage_table = pq.read_table(alpha_dir / "promotion_lineage.parquet")
    omissions_table = pq.read_table(alpha_dir / "promotion_omissions.parquet")

    assert lineage_table.num_rows == 2
    assert omissions_table.num_rows == 1

    omissions_data = omissions_table.to_pylist()
    assert omissions_data[0]["rule"] == "R2"
    assert omissions_data[0]["entry_type"] == "file"
    assert "sessions/session.json" in omissions_data[0]["source_path"]
