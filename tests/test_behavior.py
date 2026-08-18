"""Tests for behavioral analysis views and programmatic reporting."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab import cli
from evallab.behavior import (
    DEFAULT_POWER_THRESHOLD,
    generate_behavior_report,
    render_behavior_report,
)


def test_behavior_sql_in_clean_duckdb() -> None:
    """Test sql/behavior.sql in clean in-memory DuckDB with zero pre-created tables."""
    sql = Path("sql/behavior.sql").read_text()
    with duckdb.connect(":memory:") as con:
        con.execute(sql)
        for view_name in [
            "v_behavior_trial_summary",
            "v_behavior_effort_by_outcome",
            "v_behavior_efficiency",
            "v_behavior_struggle_signals",
            "v_behavior_step_shape",
            "v_behavior_token_economics",
        ]:
            rows = con.execute(f"SELECT * FROM {view_name}").fetchall()
            assert rows == []


def _create_fixture_corpus(root: Path) -> Path:
    """Create a structured fixture corpus with passed, scored-zero, and never-measured trials."""
    derived_root = root / "derived" / "parquet"
    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "INDEX.md").write_text("---\nstatus: living\naudience:\n  - builder\n---\n")

    # Fixture trials:
    # Task A:
    #   - 2 passed trials (reward 1.0, steps 10, tools 4, exec 20.0s, cost $0.04)
    #   - 2 scored-zero trials (reward 0.0, steps 25, tools 12, exec 80.0s, cost $0.20)
    #   - 2 never-measured trials (exception ValueError, reward None, steps 3, exec 5.0s, cost None)
    # Task B:
    #   - 1 passed trial (reward 1.0, steps 0, tools 0, exec 0.5s, cost None)
    fixture_trials = [
        # Task A - passed
        {
            "job_id": "job-1",
            "trial_id": "trial-a1",
            "task_name": "task-a",
            "agent_name": "codex",
            "model_name": "gpt-5.6-terra",
            "primary_reward": 1.0,
            "exception_class": None,
            "exception_phase": None,
            "duration_seconds": 30.0,
            "agent_execution_seconds": 20.0,
            "step_count": 10,
            "tool_call_count": 4,
            "llm_call_count": 5,
            "repeated_failed_command_count": 0,
            "command_failure_count": 0,
            "invalid_trajectory_count": 0,
            "input_tokens": 50000,
            "cache_tokens": 40000,
            "output_tokens": 1000,
            "cost_usd": 0.04,
        },
        {
            "job_id": "job-1",
            "trial_id": "trial-a2",
            "task_name": "task-a",
            "agent_name": "codex",
            "model_name": "gpt-5.6-terra",
            "primary_reward": 1.0,
            "exception_class": None,
            "exception_phase": None,
            "duration_seconds": 30.0,
            "agent_execution_seconds": 20.0,
            "step_count": 10,
            "tool_call_count": 4,
            "llm_call_count": 5,
            "repeated_failed_command_count": 0,
            "command_failure_count": 0,
            "invalid_trajectory_count": 0,
            "input_tokens": 50000,
            "cache_tokens": 40000,
            "output_tokens": 1000,
            "cost_usd": 0.04,
        },
        # Task A - scored_zero
        {
            "job_id": "job-2",
            "trial_id": "trial-a3",
            "task_name": "task-a",
            "agent_name": "codex",
            "model_name": "gpt-5.6-terra",
            "primary_reward": 0.0,
            "exception_class": None,
            "exception_phase": None,
            "duration_seconds": 100.0,
            "agent_execution_seconds": 80.0,
            "step_count": 25,
            "tool_call_count": 12,
            "llm_call_count": 14,
            "repeated_failed_command_count": 0,
            "command_failure_count": 0,
            "invalid_trajectory_count": 0,
            "input_tokens": 200000,
            "cache_tokens": 180000,
            "output_tokens": 8000,
            "cost_usd": 0.20,
        },
        {
            "job_id": "job-2",
            "trial_id": "trial-a4",
            "task_name": "task-a",
            "agent_name": "codex",
            "model_name": "gpt-5.6-terra",
            "primary_reward": 0.0,
            "exception_class": None,
            "exception_phase": None,
            "duration_seconds": 100.0,
            "agent_execution_seconds": 80.0,
            "step_count": 25,
            "tool_call_count": 12,
            "llm_call_count": 14,
            "repeated_failed_command_count": 0,
            "command_failure_count": 0,
            "invalid_trajectory_count": 0,
            "input_tokens": 200000,
            "cache_tokens": 180000,
            "output_tokens": 8000,
            "cost_usd": 0.20,
        },
        # Task A - never_measured
        {
            "job_id": "job-3",
            "trial_id": "trial-a5",
            "task_name": "task-a",
            "agent_name": "codex",
            "model_name": "gpt-5.6-terra",
            "primary_reward": None,
            "exception_class": "ValueError",
            "exception_phase": "unknown",
            "duration_seconds": 10.0,
            "agent_execution_seconds": 5.0,
            "step_count": 3,
            "tool_call_count": 0,
            "llm_call_count": 0,
            "repeated_failed_command_count": 0,
            "command_failure_count": 0,
            "invalid_trajectory_count": 0,
            "input_tokens": None,
            "cache_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
        },
        {
            "job_id": "job-3",
            "trial_id": "trial-a6",
            "task_name": "task-a",
            "agent_name": "codex",
            "model_name": "gpt-5.6-terra",
            "primary_reward": None,
            "exception_class": "ValueError",
            "exception_phase": "unknown",
            "duration_seconds": 10.0,
            "agent_execution_seconds": 5.0,
            "step_count": 3,
            "tool_call_count": 0,
            "llm_call_count": 0,
            "repeated_failed_command_count": 0,
            "command_failure_count": 0,
            "invalid_trajectory_count": 0,
            "input_tokens": None,
            "cache_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
        },
        # Task B - passed
        {
            "job_id": "job-4",
            "trial_id": "trial-b1",
            "task_name": "task-b",
            "agent_name": "oracle",
            "model_name": None,
            "primary_reward": 1.0,
            "exception_class": None,
            "exception_phase": None,
            "duration_seconds": 1.0,
            "agent_execution_seconds": 0.5,
            "step_count": 0,
            "tool_call_count": 0,
            "llm_call_count": 0,
            "repeated_failed_command_count": 0,
            "command_failure_count": 0,
            "invalid_trajectory_count": 0,
            "input_tokens": None,
            "cache_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
        },
    ]

    for t in fixture_trials:
        trial_dir = derived_root / f"job_id={t['job_id']}" / f"trial_id={t['trial_id']}"
        trial_dir.mkdir(parents=True, exist_ok=True)

        fact_table = pa.table(
            {
                "trial_id": pa.array([t["trial_id"]], type=pa.string()),
                "job_id": pa.array([t["job_id"]], type=pa.string()),
                "task_name": pa.array([t["task_name"]], type=pa.string()),
                "agent_name": pa.array([t["agent_name"]], type=pa.string()),
                "model_name": pa.array([t["model_name"]], type=pa.string()),
                "primary_reward": pa.array([t["primary_reward"]], type=pa.float64()),
                "exception_class": pa.array([t["exception_class"]], type=pa.string()),
                "exception_phase": pa.array([t["exception_phase"]], type=pa.string()),
                "duration_seconds": pa.array([t["duration_seconds"]], type=pa.float64()),
                "agent_execution_seconds": pa.array(
                    [t["agent_execution_seconds"]], type=pa.float64()
                ),
                "step_count": pa.array([t["step_count"]], type=pa.int64()),
                "tool_call_count": pa.array([t["tool_call_count"]], type=pa.int64()),
                "llm_call_count": pa.array([t["llm_call_count"]], type=pa.int64()),
                "repeated_failed_command_count": pa.array(
                    [t["repeated_failed_command_count"]], type=pa.int64()
                ),
                "command_failure_count": pa.array([t["command_failure_count"]], type=pa.int64()),
                "invalid_trajectory_count": pa.array(
                    [t["invalid_trajectory_count"]], type=pa.int64()
                ),
                "input_tokens": pa.array([t["input_tokens"]], type=pa.int64()),
                "cache_tokens": pa.array([t["cache_tokens"]], type=pa.int64()),
                "output_tokens": pa.array([t["output_tokens"]], type=pa.int64()),
                "cost_usd": pa.array([t["cost_usd"]], type=pa.float64()),
            }
        )
        pq.write_table(fact_table, trial_dir / "trial_facts.parquet")

        # Steps fixture for trial-a1 (passed)
        if t["trial_id"] == "trial-a1":
            steps_table = pa.table(
                {
                    "job_id": pa.array(["job-1"] * 3, type=pa.string()),
                    "trial_id": pa.array(["trial-a1"] * 3, type=pa.string()),
                    "step_id": pa.array([1, 2, 3], type=pa.int64()),
                    "source": pa.array(["system", "agent", "user"], type=pa.string()),
                }
            )
            pq.write_table(steps_table, trial_dir / "steps.parquet")

    return derived_root


def test_behavior_aggregates_against_fixture_corpus(tmp_path: Path) -> None:
    """Test that fixture corpus produces exact three-way split aggregates."""
    derived_root = _create_fixture_corpus(tmp_path)

    report = generate_behavior_report(repo_root=tmp_path, explicit_derived=derived_root)

    assert report.total_trials == 7
    assert report.total_measured == 5
    assert report.total_never_measured == 2
    assert report.total_passed == 3
    assert report.total_scored_zero == 2

    # Verify three-way split for task-a (passed, scored_zero, never_measured)
    task_a_rows = {r.outcome: r for r in report.effort_by_outcome if r.task_name == "task-a"}
    assert "passed" in task_a_rows
    assert "scored_zero" in task_a_rows
    assert "never_measured" in task_a_rows

    passed = task_a_rows["passed"]
    assert passed.n == 2
    assert passed.avg_steps == 10.0
    assert passed.avg_tool_calls == 4.0
    assert passed.avg_llm_calls == 5.0
    assert passed.avg_execution_seconds == 20.0
    assert passed.avg_reward == 1.0

    scored_zero = task_a_rows["scored_zero"]
    assert scored_zero.n == 2
    assert scored_zero.avg_steps == 25.0
    assert scored_zero.avg_tool_calls == 12.0
    assert scored_zero.avg_llm_calls == 14.0
    assert scored_zero.avg_execution_seconds == 80.0
    assert scored_zero.avg_reward == 0.0

    never_measured = task_a_rows["never_measured"]
    assert never_measured.n == 2
    assert never_measured.avg_steps == 3.0
    assert never_measured.avg_tool_calls == 0.0
    assert never_measured.avg_reward is None


def test_undefined_efficiency_ratios(tmp_path: Path) -> None:
    """Test that reward 0 and unmeasured trials yield undefined (None) efficiency ratios."""
    derived_root = _create_fixture_corpus(tmp_path)

    report = generate_behavior_report(repo_root=tmp_path, explicit_derived=derived_root)

    eff_by_outcome = {
        (r.task_name, r.outcome): r for r in report.efficiency if r.task_name == "task-a"
    }

    # Passed: steps_per_reward_point is 10.0 / 1.0 = 10.0
    assert eff_by_outcome[("task-a", "passed")].steps_per_reward_point == 10.0
    assert eff_by_outcome[("task-a", "passed")].seconds_per_step == 2.0  # 40s / 20 steps

    # Scored zero: reward is 0.0 -> steps_per_reward_point MUST be None (undefined, not inf)
    assert eff_by_outcome[("task-a", "scored_zero")].steps_per_reward_point is None
    assert eff_by_outcome[("task-a", "scored_zero")].seconds_per_step == 3.2  # 160s / 50 steps

    # Never measured: unmeasured -> steps_per_reward_point MUST be None
    assert eff_by_outcome[("task-a", "never_measured")].steps_per_reward_point is None

    # Task B: steps is 0 -> seconds_per_step is None (undefined)
    task_b = next(r for r in report.efficiency if r.task_name == "task-b")
    assert task_b.seconds_per_step is None


def test_sparse_token_economics_coverage(tmp_path: Path) -> None:
    """Test that sparse token columns report populated count and total coverage."""
    derived_root = _create_fixture_corpus(tmp_path)

    report = generate_behavior_report(repo_root=tmp_path, explicit_derived=derived_root)

    # 4 of 7 trials have cost_usd populated
    assert report.token_coverage_summary == "4 of 7 trials"

    tok_by_outcome = {
        (r.task_name, r.outcome): r for r in report.token_economics if r.task_name == "task-a"
    }

    passed_tok = tok_by_outcome[("task-a", "passed")]
    assert passed_tok.n_total == 2
    assert passed_tok.n_populated == 2
    assert passed_tok.coverage_summary == "2 of 2 trials"
    assert passed_tok.populated_pct == 100.0
    assert passed_tok.avg_cost_usd == 0.04

    never_tok = tok_by_outcome[("task-a", "never_measured")]
    assert never_tok.n_total == 2
    assert never_tok.n_populated == 0
    assert never_tok.coverage_summary == "0 of 2 trials"
    assert never_tok.populated_pct == 0.0
    assert never_tok.avg_cost_usd is None


def test_underpowered_comparisons_render_not_distinguishable(tmp_path: Path) -> None:
    """Test that underpowered comparisons render 'not distinguishable'."""
    derived_root = _create_fixture_corpus(tmp_path)
    # With n=2 per group, power_threshold=5 labels comparisons underpowered
    report = generate_behavior_report(
        repo_root=tmp_path,
        explicit_derived=derived_root,
        power_threshold=DEFAULT_POWER_THRESHOLD,
    )

    rendered = render_behavior_report(report)
    assert "not distinguishable" in rendered


def test_cli_behavior_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test `evallab behavior` and `evallab behavior --json` CLI wiring."""
    derived_root = _create_fixture_corpus(tmp_path)

    # 1. Plain text markdown report
    exit_code = cli.run_cli(
        ["behavior", "--derived-root", str(derived_root)],
        workspace=tmp_path,
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "# Behavioral Analysis Report" in out
    assert "Effort vs Outcome" in out
    assert "undefined" in out

    # 2. JSON mode
    exit_code_json = cli.run_cli(
        ["behavior", "--json", "--derived-root", str(derived_root)],
        workspace=tmp_path,
    )
    assert exit_code_json == 0
    json_out = capsys.readouterr().out
    parsed = json.loads(json_out)
    assert parsed["total_trials"] == 7
    assert parsed["total_measured"] == 5
    assert parsed["token_coverage_summary"] == "4 of 7 trials"
