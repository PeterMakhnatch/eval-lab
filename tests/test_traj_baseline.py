"""Tests for mechanical baseline facts, screening metrics, and v_trace_baseline view."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from evallab.interpretation.traj_baseline import (
    TRACE_BASELINE_PARQUET_SCHEMA,
    TRACE_BASELINE_PROVENANCE,
    _compute_cbv_slope,
    _compute_exit_code_cascade,
    _compute_subagent_overhead,
    compute_trace_baseline,
)
from evallab.traj import (
    LoopSuspicion,
    StepOutline,
    TrajectoryOutline,
    extract_features,
)


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


def test_provenance_registry_completeness() -> None:
    """Every column in schema has documented provenance and screening classification."""
    schema_fields = set(TRACE_BASELINE_PARQUET_SCHEMA.names)
    provenance_keys = set(TRACE_BASELINE_PROVENANCE.keys())

    assert schema_fields == provenance_keys, f"Mismatch: {schema_fields ^ provenance_keys}"

    for col_name, prov in TRACE_BASELINE_PROVENANCE.items():
        assert prov.column_name == col_name
        assert prov.data_type in ("VARCHAR", "BIGINT", "DOUBLE", "BOOLEAN")
        assert prov.category in ("identity", "mechanical_fact", "screening_heuristic")
        if prov.is_screening:
            assert prov.category == "screening_heuristic"
            assert col_name.endswith("_screening") or "screening" in col_name or "score" in col_name or "detected" in col_name or "reasons" in col_name


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


def test_subagent_overhead_ratio(sample_steps: list[StepOutline]) -> None:
    """Subagent overhead ratio is 0.0 when all steps are primary agent/system."""
    dummy_outline = TrajectoryOutline(
        trial_id="test-trial",
        job_id="test-job",
        trial_name="test_trial",
        job_name="test_job",
        task_name="task",
        agent_name="agent",
        agent_version="1.0",
        model_name="gpt-5",
        status="featured",
        unavailable_reason=None,
        source_path="trajectory.json",
        source_sha256="abc",
        duration_seconds=7.5,
        primary_reward=1.0,
        exception_class=None,
        total_steps=len(sample_steps),
        agent_steps=3,
        system_steps=2,
        user_steps=1,
        total_tool_calls=3,
        total_errors=2,
        recovery_count=1,
        step_to_first_tool=3,
        step_to_first_edit=5,
        time_to_first_tool_seconds=3.5,
        time_to_first_edit_seconds=7.0,
        total_prompt_tokens=5100,
        total_completion_tokens=390,
        total_cached_tokens=2000,
        total_cost_usd=0.015,
        phases=(),
        loop_suspicion=LoopSuspicion(0.0, False, (), 0, 0, 0),
        steps=tuple(sample_steps),
        citations=(),
        tool_mix={"bash": 2, "edit": 1},
    )
    overhead = _compute_subagent_overhead(dummy_outline)
    assert overhead == 0.0


def test_compute_trace_baseline_null_preservation() -> None:
    """Empty or tool-free outlines preserve NULLs for LI and TER instead of manufacturing 0.0."""
    empty_outline = TrajectoryOutline(
        trial_id="empty-trial",
        job_id="empty-job",
        trial_name="empty",
        job_name="empty",
        task_name="task",
        agent_name="agent",
        agent_version="1.0",
        model_name="model",
        status="featured",
        unavailable_reason=None,
        source_path="trajectory.json",
        source_sha256="abc",
        duration_seconds=1.0,
        primary_reward=None,
        exception_class=None,
        total_steps=1,
        agent_steps=0,
        system_steps=1,
        user_steps=0,
        total_tool_calls=0,  # 0 tool calls
        total_errors=0,
        recovery_count=0,
        step_to_first_tool=None,
        step_to_first_edit=None,
        time_to_first_tool_seconds=None,
        time_to_first_edit_seconds=None,
        total_prompt_tokens=0,
        total_completion_tokens=0,
        total_cached_tokens=0,
        total_cost_usd=0.0,
        phases=(),
        loop_suspicion=LoopSuspicion(0.0, False, (), 0, 0, 0),
        steps=(),
        citations=(),
        tool_mix={},
    )

    baseline = compute_trace_baseline(empty_outline)

    # Invariant: denominators of 0 MUST produce None / SQL NULL
    assert baseline.linear_innocence_screening is None
    assert baseline.tool_error_rate_screening is None
    assert baseline.context_burn_velocity_screening is None
    assert baseline.cache_hit_rate_screening is None
    assert baseline.total_tokens == 0

def test_compute_trace_baseline_populated(sample_steps: list[StepOutline]) -> None:
    """Populated outline computes accurate baseline facts and screening metrics."""
    outline = TrajectoryOutline(
        trial_id="pop-trial",
        job_id="pop-job",
        trial_name="pop",
        job_name="pop",
        task_name="travel-task",
        agent_name="codex",
        agent_version="1.0",
        model_name="gpt-5.6-luna",
        status="featured",
        unavailable_reason=None,
        source_path="agent/trajectory.json",
        source_sha256="deadbeef",
        duration_seconds=7.5,
        primary_reward=1.0,
        exception_class=None,
        total_steps=len(sample_steps),
        agent_steps=3,
        system_steps=2,
        user_steps=1,
        total_tool_calls=3,
        total_errors=2,
        recovery_count=1,
        step_to_first_tool=3,
        step_to_first_edit=5,
        time_to_first_tool_seconds=3.5,
        time_to_first_edit_seconds=7.0,
        total_prompt_tokens=5100,
        total_completion_tokens=390,
        total_cached_tokens=2000,
        total_cost_usd=0.015,
        phases=(),
        loop_suspicion=LoopSuspicion(0.0, False, (), 0, 0, 0),
        steps=tuple(sample_steps),
        citations=(),
        tool_mix={"bash": 2, "edit": 1},
    )

    baseline = compute_trace_baseline(outline)

    assert baseline.trial_id == "pop-trial"
    assert baseline.step_count == 6
    assert baseline.tool_call_count == 3
    assert baseline.unique_tools_count == 2
    # LI = 2 unique / 3 total = 0.6667
    assert baseline.linear_innocence_screening == pytest.approx(0.6667, rel=1e-3)
    # TER = 2 errors / 3 tool calls = 0.6667
    assert baseline.tool_error_rate_screening == pytest.approx(0.6667, rel=1e-3)
    assert baseline.max_exit_code_cascade_screening == 2
    # Cache hit rate = 2000 / 5100 = 0.3922 (ATIF cached_tokens is a subset of prompt_tokens)
    assert baseline.cache_hit_rate_screening == pytest.approx(2000 / 5100, rel=1e-3)
    # Total tokens = 5100 + 390 = 5490
    assert baseline.total_tokens == 5490


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


def test_python_sql_null_and_cardinality_parity(repo_root: Path, sample_steps: list[StepOutline]) -> None:
    """Verify exact formula parity and null semantics between Python and DuckDB SQL."""
    outline = TrajectoryOutline(
        trial_id="parity-trial",
        job_id="parity-job",
        trial_name="parity_trial",
        job_name="parity_job",
        task_name="task_parity",
        agent_name="codex",
        agent_version="1.0",
        model_name="gpt-5.6-luna",
        status="featured",
        unavailable_reason=None,
        source_path="agent/trajectory.json",
        source_sha256="sha_parity",
        duration_seconds=10.0,
        primary_reward=1.0,
        exception_class=None,
        total_steps=len(sample_steps),
        agent_steps=3,
        system_steps=2,
        user_steps=1,
        total_tool_calls=3,
        total_errors=2,
        recovery_count=1,
        step_to_first_tool=3,
        step_to_first_edit=5,
        time_to_first_tool_seconds=3.5,
        time_to_first_edit_seconds=7.0,
        total_prompt_tokens=5100,
        total_completion_tokens=390,
        total_cached_tokens=2000,
        total_cost_usd=0.015,
        phases=(),
        loop_suspicion=LoopSuspicion(0.0, False, (), 0, 0, 0),
        steps=tuple(sample_steps),
        citations=(),
        tool_mix={"bash": 2, "edit": 1},
    )

    py_baseline = compute_trace_baseline(outline)
    feat = extract_features(outline)

    # Insert into DuckDB
    conn = duckdb.connect(":memory:")
    sql_file = repo_root / "sql" / "traj_views.sql"
    conn.execute(sql_file.read_text())

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
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?
        )
        """,
        [
            feat.trial_id, feat.job_id, feat.trial_name, feat.job_name,
            feat.task_name, feat.agent_name, feat.agent_version, feat.model_name,
            feat.status, feat.unavailable_reason, feat.source_path, feat.source_sha256,
            feat.step_count, feat.agent_step_count, feat.system_step_count, feat.user_step_count,
            feat.tool_call_count, feat.unique_tools_count, feat.tool_mix_json, feat.error_count,
            feat.recovery_count, feat.loop_suspicion_score, feat.loop_suspicion_detected,
            feat.loop_reasons_json, feat.repeated_command_count, feat.step_to_first_tool,
            feat.step_to_first_edit, feat.time_to_first_tool_seconds, feat.time_to_first_edit_seconds,
            feat.prompt_tokens, feat.completion_tokens, feat.cached_tokens, feat.cost_usd,
            feat.primary_reward, feat.exception_class, feat.duration_seconds, feat.created_at,
            feat.context_burn_velocity_screening, feat.max_exit_code_cascade_screening,
        ],
    )
    rows = conn.execute("SELECT * FROM v_trace_baseline WHERE trial_id = 'parity-trial'").fetchall()
    assert len(rows) == 1
    cols = [desc[0] for desc in conn.description]
    sql_row = dict(zip(cols, rows[0], strict=True))

    assert sql_row["linear_innocence_screening"] == pytest.approx(py_baseline.linear_innocence_screening, rel=1e-4)
    assert sql_row["tool_error_rate_screening"] == pytest.approx(py_baseline.tool_error_rate_screening, rel=1e-4)
    assert sql_row["context_burn_velocity_screening"] == pytest.approx(py_baseline.context_burn_velocity_screening, rel=1e-4)
    assert sql_row["max_exit_code_cascade_screening"] == py_baseline.max_exit_code_cascade_screening
    assert sql_row["cache_hit_rate_screening"] == pytest.approx(py_baseline.cache_hit_rate_screening, rel=1e-4)
    assert sql_row["total_tokens"] == py_baseline.total_tokens

    # Test partial missing inputs in SQL: missing completion_tokens must yield NULL total_tokens
    conn.execute("UPDATE traj_features SET completion_tokens = NULL WHERE trial_id = 'parity-trial'")
    null_res = conn.execute("SELECT total_tokens FROM v_trace_baseline WHERE trial_id = 'parity-trial'").fetchone()
    assert null_res[0] is None
