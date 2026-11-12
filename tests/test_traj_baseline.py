"""Tests for mechanical baseline facts, screening metrics, and v_trace_baseline view."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from evallab.traj import StepOutline, _compute_cbv_slope, _compute_exit_code_cascade


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def sample_steps() -> list[StepOutline]:
    return [
        StepOutline(
            step_id=1,
            source="system",
            timestamp="2026-08-25T12:00:00Z",
            model_name=None,
            tool_name=None,
            tool_command=None,
            exit_code=0,
            is_error=False,
            error_message=None,
            prompt_tokens=100,
            completion_tokens=20,
            cached_tokens=0,
            cost_usd=0.0001,
            thought_snippet="Setup OK",
        ),
        StepOutline(
            step_id=2,
            source="user",
            timestamp="2026-08-25T12:00:01Z",
            model_name=None,
            tool_name=None,
            tool_command=None,
            exit_code=0,
            is_error=False,
            error_message=None,
            prompt_tokens=500,
            completion_tokens=50,
            cached_tokens=0,
            cost_usd=0.0005,
            thought_snippet="Task prompt",
        ),
        StepOutline(
            step_id=3,
            source="agent",
            timestamp="2026-08-25T12:00:03Z",
            model_name="gpt-5.6-luna",
            tool_name="bash",
            tool_command="pytest tests/",
            exit_code=1,
            is_error=True,
            error_message="1 failed",
            prompt_tokens=1000,
            completion_tokens=100,
            cached_tokens=500,
            cost_usd=0.002,
            thought_snippet="Running tests",
        ),
        StepOutline(
            step_id=4,
            source="agent",
            timestamp="2026-08-25T12:00:05Z",
            model_name="gpt-5.6-luna",
            tool_name="bash",
            tool_command="pytest tests/test_single.py",
            exit_code=1,
            is_error=True,
            error_message="1 failed again",
            prompt_tokens=1500,
            completion_tokens=120,
            cached_tokens=1000,
            cost_usd=0.003,
            thought_snippet="Retrying test",
        ),
        StepOutline(
            step_id=5,
            source="agent",
            timestamp="2026-08-25T12:00:07Z",
            model_name="gpt-5.6-luna",
            tool_name="edit",
            tool_command="edit src/app.py",
            exit_code=0,
            is_error=False,
            error_message=None,
            prompt_tokens=2000,
            completion_tokens=150,
            cached_tokens=1500,
            cost_usd=0.004,
            thought_snippet="Editing app",
        ),
        StepOutline(
            step_id=6,
            source="system",
            timestamp="2026-08-25T12:00:08Z",
            model_name=None,
            tool_name=None,
            tool_command=None,
            exit_code=0,
            is_error=False,
            error_message=None,
            prompt_tokens=None,
            completion_tokens=None,
            cached_tokens=None,
            cost_usd=None,
            thought_snippet="Verifier pass",
        ),
    ]


def test_cbv_slope_calculation(sample_steps: list[StepOutline]) -> None:
    """Context Burn Velocity (slope of prompt tokens over steps) is computed deterministically."""
    slope = _compute_cbv_slope(sample_steps)
    assert slope is not None
    # Steps 1 to 5: (1, 100), (2, 500), (3, 1000), (4, 1500), (5, 2000)
    # Average slope is around +470 tokens per step
    assert 400.0 < slope < 500.0


def test_cbv_slope_null_when_insufficient_points() -> None:
    """CBV slope is null when fewer than 2 token observations exist."""
    step_none = [
        StepOutline(
            step_id=1,
            source="agent",
            timestamp=None,
            model_name=None,
            tool_name=None,
            tool_command=None,
            exit_code=None,
            is_error=False,
            error_message=None,
            prompt_tokens=None,
            completion_tokens=None,
            cached_tokens=None,
            cost_usd=None,
            thought_snippet=None,
        )
    ]
    assert _compute_cbv_slope(step_none) is None


def test_exit_code_cascade_streak(sample_steps: list[StepOutline]) -> None:
    """Max exit-code cascade streak identifies contiguous non-zero exit code steps."""
    # Steps 3 and 4 have exit code 1 -> streak of 2
    cascade = _compute_exit_code_cascade(sample_steps)
    assert cascade == 2


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
