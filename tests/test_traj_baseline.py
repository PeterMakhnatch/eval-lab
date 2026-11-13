"""Stored mechanical facts and the retained v_trace_baseline view."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_duckdb_v_trace_baseline_view(repo_root: Path) -> None:
    """Test v_trace_baseline view inside DuckDB with populated traj_features table."""
    conn = duckdb.connect(":memory:")
    sql_file = repo_root / "sql" / "traj_views.sql"
    conn.execute(sql_file.read_text())

    # Insert a sample row into traj_features
    conn.execute(
        """
        INSERT INTO traj_features (
            trial_id, job_id, trial_name, job_name, task_name, agent_name, agent_version,
            model_name, status, unavailable_reason, source_path, source_sha256,
            step_count, agent_step_count, system_step_count, user_step_count,
            tool_call_count, unique_tools_count, tool_mix_json, error_count, recovery_count,
            loop_suspicion_score, loop_suspicion_detected, loop_reasons_json, repeated_command_count,
            step_to_first_tool, step_to_first_edit, time_to_first_tool_seconds, time_to_first_edit_seconds,
            prompt_tokens, completion_tokens, cached_tokens, cost_usd, primary_reward,
            exception_class, duration_seconds, created_at,
            context_burn_velocity_screening, max_exit_code_cascade_screening
        ) VALUES (
            'trial-1', 'job-1', 'trial_one', 'job_one', 'task_a', 'codex', '1.0',
            'gpt-5', 'featured', NULL, 'trajectory.json', 'sha256_mock',
            10, 8, 1, 1,
            5, 3, '{"bash": 3, "edit": 2}', 1, 1,
            0.0, FALSE, '[]', 0,
            2, 4, 1.5, 3.0,
            8000, 500, 2000, 0.02, 1.0,
            NULL, 25.0, '2026-08-25T12:00:00Z',
            450.25, 2
        )
        """
    )

    # Query v_trace_baseline
    result = conn.execute("SELECT * FROM v_trace_baseline WHERE trial_id = 'trial-1'").fetchall()
    assert len(result) == 1  # Exactly one deterministic row per trial

    cols = [desc[0] for desc in conn.description]
    row = dict(zip(cols, result[0], strict=True))

    assert row["trial_id"] == "trial-1"
    assert row["primary_reward"] == 1.0
    assert row["linear_innocence_screening"] == pytest.approx(3 / 5, rel=1e-3)
    assert row["tool_error_rate_screening"] == pytest.approx(1 / 5, rel=1e-3)
    assert row["context_burn_velocity_screening"] == pytest.approx(450.25, rel=1e-3)
    assert row["max_exit_code_cascade_screening"] == 2
    assert row["cache_hit_rate_screening"] == pytest.approx(2000 / 8000, rel=1e-3)
    assert row["total_tokens"] == 8500
    assert row["subagent_overhead_ratio_screening"] == pytest.approx((10 - 8 - 1) / 10, rel=1e-3)
