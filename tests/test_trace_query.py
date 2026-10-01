"""Behavioral boundary tests for evallab.trace_query (HAR-131).

Tests:
    - Deduplication of duplicate published/source trial identities.
    - Missingness transparency: missing ATIF, processed, counts visible as coverage.
    - Trial vs step grain separation (no denominator multiplication).
    - Label freeze integrity and empty list representation for unlabeled trials.
    - Execution of all 10 canonical trace queries.
    - CLI db attach trace mode flags.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from evallab.cli import run_cli
from evallab.trace_query import connect_trace_query


def _create_minimal_trial(
    job_dir: Path,
    trial_name: str,
    *,
    job_id: str = "job-uuid-1",
    trial_id: str = "trial-uuid-1",
    task_name: str = "format-code-task-001",
    reward: float | None = 1.0,
    with_trajectory: bool = True,
    with_processed: bool = True,
    with_counts: bool = True,
    counts_verdict: str = "counted_pass",
    n_steps: int = 3,
) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    trial_dir = job_dir / trial_name
    trial_dir.mkdir(parents=True, exist_ok=True)

    # Job result.json
    job_result = {
        "id": job_id,
        "n_total_trials": 1,
        "finished_at": "2026-10-01T00:00:00Z",
        "stats": {"n_completed_trials": 1},
    }
    (job_dir / "result.json").write_text(json.dumps(job_result), encoding="utf-8")

    # Provenance
    prov = {
        "card": "HAR-116",
        "job_name": job_dir.name,
        "source_path": str(job_dir),
        "model": "test-model",
    }
    (job_dir / "provenance.json").write_text(json.dumps(prov), encoding="utf-8")

    # Spec
    spec = {
        "hypothesis": "arm=original: test hypothesis",
        "model": "test-model",
        "task_package_digest": "sha256:pkgdigest123",
    }
    (job_dir / "experiment-spec.json").write_text(json.dumps(spec), encoding="utf-8")

    # Trial result.json
    trial_result = {
        "id": trial_id,
        "job_id": job_id,
        "trial_name": trial_name,
        "task_name": task_name,
        "verifier_result": {"rewards": {"reward": reward}} if reward is not None else {},
        "agent_result": {"n_input_tokens": 1000, "n_output_tokens": 100},
    }
    (trial_dir / "result.json").write_text(json.dumps(trial_result), encoding="utf-8")

    # Trajectory
    if with_trajectory:
        agent_dir = trial_dir / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        steps = []
        for i in range(1, n_steps + 1):
            steps.append(
                {
                    "step_id": i,
                    "source": "agent",
                    "timestamp": f"2026-10-01T00:0{i}:00Z",
                    "is_copied_context": False,
                    "metrics": {"prompt_tokens": 100 * i, "completion_tokens": 10 * i},
                    "tool_calls": [
                        {
                            "function_name": "bash_command",
                            "arguments": {"keystrokes": f"echo step {i} > /tmp/out"},
                        }
                    ],
                    "observation": {"results": [{"content": f"output from step {i}"}]},
                }
            )
        (agent_dir / "trajectory.json").write_text(
            json.dumps({"steps": steps}), encoding="utf-8"
        )

    # Processed
    if with_processed:
        proc_dir = job_dir / "processed"
        proc_dir.mkdir(parents=True, exist_ok=True)
        proc_data = {
            "trial_name": trial_name,
            "task_name": task_name,
            "reward": reward,
            "scored": reward is not None,
            "stop_reason": "completed" if reward is not None else "agent_timeout",
            "agent_steps": n_steps,
            "counts": (
                {"verdict": counts_verdict, "reasons": [], "evidence": []}
                if with_counts
                else None
            ),
            "tokens_proxy": {
                "source": "proxy_settled_ledger",
                "attribution": "single_trial",
                "input_tokens": 2500,
                "output_tokens": 150,
            },
            "decision": {
                "schema": "trial_decision/v2",
                "judgments": {"loop_kind": {"kind": "none"}},
            },
            "token_flow": {
                "last_useful_edit": {"step_id": 1},
                "tokens_after_last_edit": {"input_tokens": 200, "output_tokens": 20},
            },
        }
        (proc_dir / f"trial-{trial_name}.json").write_text(
            json.dumps(proc_data), encoding="utf-8"
        )

    return trial_dir


def test_duplicate_identities_deduplicated(tmp_path: Path) -> None:
    """Duplicate published/source identities deduplicate to exactly one canonical row."""
    job1 = tmp_path / "job1"
    job2 = tmp_path / "job2"

    # Same native job_id and trial_id in both jobs
    # job1 has processed data; job2 does not
    _create_minimal_trial(
        job1,
        "trial_alpha",
        job_id="uuid-job-shared",
        trial_id="uuid-trial-shared",
        with_processed=True,
    )
    _create_minimal_trial(
        job2,
        "trial_alpha_copy",
        job_id="uuid-job-shared",
        trial_id="uuid-trial-shared",
        with_processed=False,
    )

    con, coverage = connect_trace_query(
        repo_root=tmp_path,
        job_dirs=[job1, job2],
    )
    try:
        rows = con.execute(
            "SELECT job_id, trial_id, processed_available FROM v_trace_trials"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "uuid-job-shared"
        assert rows[0][1] == "uuid-trial-shared"
        assert rows[0][2] is True  # The processed-available version was preserved
        assert coverage["discovered_trials"] == 1
    finally:
        con.close()


def test_missing_inputs_visible_as_coverage(tmp_path: Path) -> None:
    """Missing ATIF, processed, and counts remain visible without dropping the trial."""
    job = tmp_path / "job_sparse"
    _create_minimal_trial(
        job,
        "trial_sparse",
        job_id="uuid-sparse-job",
        trial_id="uuid-sparse-trial",
        reward=None,
        with_trajectory=False,
        with_processed=False,
        with_counts=False,
    )

    con, coverage = connect_trace_query(
        repo_root=tmp_path,
        job_dirs=[job],
    )
    try:
        # Trial is NOT dropped
        t_row = con.execute("SELECT * FROM v_trace_trials").fetchone()
        assert t_row is not None

        cols = [d[0] for d in con.execute("DESCRIBE v_trace_trials").fetchall()]
        row_dict = dict(zip(cols, t_row, strict=True))

        assert row_dict["trajectory_available"] is False
        assert row_dict["processed_available"] is False
        assert row_dict["counts_available"] is False
        assert row_dict["step_evidence_source"] == "none"
        assert row_dict["counts_verdict"] is None
        assert row_dict["counts_reasons_json"] == "[]"

        # Explicit sentinel marker row exists in v_trace_steps
        s_rows = con.execute(
            "SELECT step_id, source, step_ref FROM v_trace_steps WHERE trial_id = 'uuid-sparse-trial'"
        ).fetchall()
        assert len(s_rows) == 1
        assert s_rows[0][0] == -1
        assert s_rows[0][1] == "trial_no_steps"
        assert s_rows[0][2] == "no_steps"

        # Coverage dict reflects explicit missingness
        assert coverage["missing_atif"] == 1
        assert coverage["missing_processed"] == 1
        assert coverage["missing_counts"] == 1
    finally:
        con.close()


def test_trial_vs_step_grain_isolation(tmp_path: Path) -> None:
    """Trial aggregations are not corrupted by joined steps."""
    job = tmp_path / "job_multistep"
    _create_minimal_trial(
        job,
        "trial_multistep",
        job_id="uuid-multi-job",
        trial_id="uuid-multi-trial",
        reward=1.0,
        n_steps=5,
    )

    con, _ = connect_trace_query(
        repo_root=tmp_path,
        job_dirs=[job],
    )
    try:
        trial_count = con.execute("SELECT COUNT(*) FROM v_trace_trials").fetchone()[0]
        step_count = con.execute("SELECT COUNT(*) FROM v_trace_steps").fetchone()[0]
        assert trial_count == 1
        assert step_count == 5

        # Joined fields on v_trace_steps
        step_row = con.execute(
            "SELECT trial_name, raw_reward, step_id, command_text, step_ref FROM v_trace_steps WHERE step_id = 1"
        ).fetchone()
        assert step_row[0] == "trial_multistep"
        assert step_row[1] == 1.0
        assert step_row[2] == 1
        assert "echo step 1" in step_row[3]
        assert step_row[4] == "head#1"
    finally:
        con.close()


def test_unlabeled_is_empty_list_not_clean(tmp_path: Path) -> None:
    """Unlabeled trials carry labels_json='[]', never a fake empty-clean assumption."""
    job = tmp_path / "job_unlabeled"
    _create_minimal_trial(job, "trial_unlabeled")

    con, _ = connect_trace_query(
        repo_root=tmp_path,
        job_dirs=[job],
    )
    try:
        labels_json = con.execute(
            "SELECT labels_json FROM v_trace_trials"
        ).fetchone()[0]
        assert labels_json == "[]"
    finally:
        con.close()


def test_all_ten_canonical_queries_execute(tmp_path: Path) -> None:
    """All 10 canonical views resolve and execute cleanly."""
    job = tmp_path / "job_queries"
    _create_minimal_trial(job, "trial_q", reward=1.0, n_steps=3)

    con, _ = connect_trace_query(
        repo_root=tmp_path,
        job_dirs=[job],
    )
    try:
        views = [
            "v_trace_cohort_raw",
            "v_trace_first_edit_vs_pass",
            "v_trace_test_path_access",
            "v_trace_post_edit_tokens",
            "v_trace_loop_by_arm",
            "v_trace_fetch_exclusion_audit",
            "v_trace_parse_rejection_evidence",
            "v_trace_frozen_label_agreement",
            "v_trace_candidate_exemplars",
            "v_trace_evidence_completeness",
        ]
        for v in views:
            res = con.execute(f"SELECT * FROM {v}").fetchall()
            assert isinstance(res, list)
    finally:
        con.close()


def test_cli_attach_trace_mode(tmp_path: Path) -> None:
    """CLI evallab db attach supports --trace and --trace-job-dir."""
    job = tmp_path / "job_cli"
    _create_minimal_trial(job, "trial_cli", reward=1.0, n_steps=2)

    code_zones = run_cli(
        ["db", "attach", "--trace-job-dir", str(job), "--zones"],
        workspace=tmp_path,
    )
    assert code_zones == 0

    code_query = run_cli(
        [
            "db",
            "attach",
            "--trace-job-dir",
            str(job),
            "--query",
            "SELECT trial_name, raw_reward FROM v_trace_trials",
        ],
        workspace=tmp_path,
    )
    assert code_query == 0
