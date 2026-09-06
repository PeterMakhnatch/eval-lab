"""Focused tests for the versioned trajectory store read rule (A4).

Specifications tested:
1. status='featured' filter: accounts only for successfully featured trials.
2. source_sha256-to-bytes deduplication: deduplicates reprojection duplicates
   to the row matching current file bytes, preventing double/triple counting.
3. Documented known-absent accounting: includes on-disk trials omitted from
   historical projections due to nested 'trials*/' subdirectories.
4. Deduped cost arithmetic: an inflated store sums higher than the truth, and the
   read rule recovers the exact per-trial sums (the real-corpus instance of this,
   $3.77 inflated vs $2.013527 true over 27 files, is recorded with its
   reproduction command in
   research/analysis/harbor-native-track/offline-distributions.md).
5. projected_at field availability.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from evallab.traj import (
    VERIFIED_ON_DISK_TRAJECTORY_HASHES,
    load_versioned_traj_features,
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def memory_db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    sql = Path("sql/traj_views.sql").read_text()
    con.execute(sql)
    yield con
    con.close()


def test_dedupe_on_fixture_rows(memory_db: duckdb.DuckDBPyConnection) -> None:
    """Reprojection duplicates for the same trial are deduped to the verified hash row."""
    # Insert two rows for the same trial:
    # Row 1: Stale projection hash
    # Row 2: Verified current hash
    verified_hash = VERIFIED_ON_DISK_TRAJECTORY_HASHES[0]
    stale_hash = "stale_hash_from_older_projection_1234567890"

    memory_db.execute(
        f"""
        INSERT INTO traj_features (
            trial_id, job_id, trial_name, job_name, task_name,
            agent_name, agent_version, model_name, status, unavailable_reason,
            source_path, source_sha256, step_count, agent_step_count, system_step_count, user_step_count,
            tool_call_count, unique_tools_count, tool_mix_json, error_count, recovery_count,
            loop_suspicion_score, loop_suspicion_detected, loop_reasons_json, repeated_command_count,
            step_to_first_tool, step_to_first_edit, time_to_first_tool_seconds, time_to_first_edit_seconds,
            prompt_tokens, completion_tokens, cached_tokens, cost_usd, primary_reward, exception_class,
            duration_seconds, created_at, context_burn_velocity_screening, max_exit_code_cascade_screening,
            projected_at
        ) VALUES
        (
            't-stale', 'j-001', 'trial-001', 'job-001', 'task-a',
            'codex', '0.1.0', 'gpt-5.6', 'featured', NULL,
            'path/to/traj', '{stale_hash}', 10, 8, 1, 1,
            6, 2, '{{"exec":6}}', 2, 2, 0.75, true, '["repeated_cmd"]', 2,
            2, 4, 1.2, 5.4, 5000, 500, 4000, 0.05, 1.0, NULL, 35.0, '2026-08-15',
            450.0, 2, '2026-08-15T00:00:00Z'
        ),
        (
            't-verified', 'j-001', 'trial-001', 'job-001', 'task-a',
            'codex', '0.1.0', 'gpt-5.6', 'featured', NULL,
            'path/to/traj', '{verified_hash}', 10, 8, 1, 1,
            6, 2, '{{"exec":6}}', 2, 2, 0.75, true, '["repeated_cmd"]', 2,
            2, 4, 1.2, 5.4, 5000, 500, 4000, 0.05, 1.0, NULL, 35.0, '2026-08-19',
            450.0, 2, '2026-08-19T00:00:00Z'
        )
        """
    )

    # Raw store has 2 rows
    raw_count = memory_db.execute("SELECT count(*) FROM traj_features").fetchone()[0]
    assert raw_count == 2

    # Versioned view dedupes to exactly 1 row matching the verified hash
    v1_rows = memory_db.execute(
        "SELECT trial_id, source_sha256, cost_usd FROM v_traj_features_v1"
    ).fetchall()
    assert len(v1_rows) == 1
    assert v1_rows[0][0] == "t-verified"
    assert v1_rows[0][1] == verified_hash
    assert v1_rows[0][2] == 0.05


def test_status_filtering_excludes_unavailable(memory_db: duckdb.DuckDBPyConnection) -> None:
    """status='featured' filter excludes accounted_unavailable trials."""
    memory_db.execute(
        """
        INSERT INTO traj_features (
            trial_id, job_id, trial_name, job_name, task_name,
            agent_name, agent_version, model_name, status, unavailable_reason,
            source_path, source_sha256, step_count, agent_step_count, system_step_count, user_step_count,
            tool_call_count, unique_tools_count, tool_mix_json, error_count, recovery_count,
            loop_suspicion_score, loop_suspicion_detected, loop_reasons_json, repeated_command_count,
            step_to_first_tool, step_to_first_edit, time_to_first_tool_seconds, time_to_first_edit_seconds,
            prompt_tokens, completion_tokens, cached_tokens, cost_usd, primary_reward, exception_class,
            duration_seconds, created_at, context_burn_velocity_screening, max_exit_code_cascade_screening
        ) VALUES
        (
            't-unavail', 'j-002', 'trial-unavail', 'job-002', 'task-b',
            'codex', '0.1.0', 'gpt-5.6', 'accounted_unavailable', 'missing_trajectory_file',
            'path/to/missing', '', 0, 0, 0, 0,
            0, 0, '{}', 0, 0, 0.0, false, '[]', 0,
            NULL, NULL, NULL, NULL, 0, 0, 0, 0.0, NULL, NULL, NULL, '2026-08-19',
            NULL, 0
        )
        """
    )
    v1_rows = memory_db.execute(
        "SELECT * FROM v_traj_features_v1 WHERE job_name = 'job-002'"
    ).fetchall()
    assert len(v1_rows) == 0


def test_known_absent_accounting(memory_db: duckdb.DuckDBPyConnection) -> None:
    """Known-absent trials are unioned when their job is present, but never duplicated."""
    # When funcdag-codex-canary is present with one trial
    memory_db.execute(
        """
        INSERT INTO traj_features (
            trial_id, job_id, trial_name, job_name, task_name,
            agent_name, agent_version, model_name, status, unavailable_reason,
            source_path, source_sha256, step_count, agent_step_count, system_step_count, user_step_count,
            tool_call_count, unique_tools_count, tool_mix_json, error_count, recovery_count,
            loop_suspicion_score, loop_suspicion_detected, loop_reasons_json, repeated_command_count,
            step_to_first_tool, step_to_first_edit, time_to_first_tool_seconds, time_to_first_edit_seconds,
            prompt_tokens, completion_tokens, cached_tokens, cost_usd, primary_reward, exception_class,
            duration_seconds, created_at, context_burn_velocity_screening, max_exit_code_cascade_screening
        ) VALUES
        (
            'syn-funcdag-easy__Az2rApj', 'funcdag-codex-canary',
            'syn-funcdag-easy__Az2rApj', 'funcdag-codex-canary',
            'syn-funcdag-easy', 'codex', '0.1.0', 'gpt-5.6', 'featured', NULL,
            'runs/funcdag-codex-canary/syn-funcdag-easy__Az2rApj/agent/trajectory.json',
            'e429ff733b4a27142fda77a6e65ef34ceef3c4678a3806ee7fb61d9a146e7f7f',
            5, 5, 0, 0, 5, 1, '{"bash": 5}', 0, 0, 0.0, false, '[]', 0,
            1, NULL, 1.0, NULL, 79759, 1500, 0, 0.038923, 0.0, NULL, 80.0, '2026-08-20',
            NULL, 0
        )
        """
    )

    # In v_traj_features_v1, the 3 known absent trials are included
    funcdag_rows = memory_db.execute(
        "SELECT trial_name, cost_usd FROM v_traj_features_v1 WHERE job_name = 'funcdag-codex-canary'"
    ).fetchall()
    names = {r[0] for r in funcdag_rows}
    assert len(funcdag_rows) == 4
    assert "syn-funcdag-easy__Az2rApj" in names
    assert "adapted-task__PmTen2E" in names
    assert "adapted-syn-funcdag-hard__kziNARo" in names
    assert "adapted-syn-funcdag-medium__NoqKuag" in names

    # Total cost of the 4 funcdag trials is exact
    total_cost = sum(r[1] for r in funcdag_rows)
    assert round(total_cost, 6) == round(0.038923 + 0.028304 + 0.063069 + 0.045429, 6)


def test_read_rule_recovers_true_cost_from_an_inflated_store(
    memory_db: duckdb.DuckDBPyConnection,
) -> None:
    """Duplicate projections inflate a naive sum; the read rule recovers the truth.

    This is the mechanism behind the real-corpus finding (naive $3.77 against a true
    $2.013527 over the same 27 files). It is asserted on fixture rows rather than the
    live shared store, which mutates as new trials are ingested.
    """
    verified_hash = VERIFIED_ON_DISK_TRAJECTORY_HASHES[0]
    columns = (
        "trial_id, job_id, trial_name, job_name, task_name, agent_name, agent_version, "
        "model_name, status, unavailable_reason, source_path, source_sha256, step_count, "
        "agent_step_count, system_step_count, user_step_count, tool_call_count, "
        "unique_tools_count, tool_mix_json, error_count, recovery_count, loop_suspicion_score, "
        "loop_suspicion_detected, loop_reasons_json, repeated_command_count, step_to_first_tool, "
        "step_to_first_edit, time_to_first_tool_seconds, time_to_first_edit_seconds, "
        "prompt_tokens, completion_tokens, cached_tokens, cost_usd, primary_reward, "
        "exception_class, duration_seconds, created_at, context_burn_velocity_screening, "
        "max_exit_code_cascade_screening, projected_at"
    )

    def row(trial_id: str, trial_name: str, sha: str, cost: float, prompt: int, day: str) -> str:
        return (
            f"('{trial_id}', 'j-001', '{trial_name}', 'canary-fixture', 'task-a', "
            f"'codex', '0.1.0', 'gpt-5.6', 'featured', NULL, 'path/{trial_name}', '{sha}', "
            f"10, 8, 1, 1, 6, 2, '{{\"exec\":6}}', 2, 2, 0.75, true, '[]', 2, 2, 4, 1.2, 5.4, "
            f"{prompt}, 500, 4000, {cost}, 1.0, NULL, 450.0, '{day}', 35.0, 2, '{day}T00:00:00Z')"
        )

    # One trial projected three times (two stale hashes), one projected once.
    memory_db.execute(
        f"INSERT INTO traj_features ({columns}) VALUES "
        + ", ".join(
            [
                row("t-a-stale-1", "trial-a", "stale-a-1", 0.25, 1000, "2026-08-15"),
                row("t-a-stale-2", "trial-a", "stale-a-2", 0.25, 1000, "2026-08-16"),
                row("t-a-current", "trial-a", verified_hash, 0.25, 1000, "2026-08-19"),
                row("t-b-current", "trial-b", verified_hash, 0.40, 2000, "2026-08-19"),
            ]
        )
    )

    naive = memory_db.execute(
        "SELECT count(*), round(sum(cost_usd), 6), sum(prompt_tokens) FROM traj_features"
    ).fetchone()
    assert naive == (4, 1.15, 5000)

    deduped = memory_db.execute(
        "SELECT count(*), round(sum(cost_usd), 6), sum(prompt_tokens) FROM v_traj_features_v1"
    ).fetchone()
    assert deduped == (2, 0.65, 3000)

    # The Python helper reports the same deduped rows as the SQL view.
    py_rows = load_versioned_traj_features(memory_db)
    assert len(py_rows) == 2
    assert round(sum(r["cost_usd"] for r in py_rows), 6) == 0.65
    assert sum(r["prompt_tokens"] for r in py_rows) == 3000
