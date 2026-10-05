"""Behavioral tests for evallab.interpretation.features (synthetic trials)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.interpretation.features import trial_features


def _call(keystrokes: str, *, name: str = "bash_command") -> dict:
    return {
        "tool_call_id": "c1",
        "function_name": name,
        "arguments": {"keystrokes": keystrokes, "duration": 1.0},
    }


def _step(step_id: int, keystrokes: str, *, content: str = "") -> dict:
    step: dict = {
        "step_id": step_id,
        "source": "agent",
        "message": f"analysis for step {step_id}",
        "tool_calls": [_call(keystrokes)],
        "metrics": {"prompt_tokens": 1000, "completion_tokens": 10},
    }
    if content:
        step["observation"] = {"results": [{"source_call_id": "c1", "content": content}]}
    return step


def _write_trial(
    root: Path, job: str, trial: str, steps: list[dict], *, result: dict | None = None
) -> tuple[Path, Path]:
    job_dir = root / job
    trial_dir = job_dir / trial
    agent = trial_dir / "agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    (job_dir / "lab-metadata.json").write_text(json.dumps({}), encoding="utf-8")
    if result is not None:
        (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return job_dir, trial_dir


def test_first_edit_ignores_quoted_awk_read(tmp_path: Path) -> None:
    """A quoted ``awk 'NR>=..'`` read is exploration, not a repo edit."""
    steps = [
        _step(1, "awk 'NR>=125 && NR<=240' /repo/pkg/data.txt", content="row1\nrow2\n"),
        _step(2, "echo fix > /repo/pkg/mod.py"),
    ]
    job_dir, trial_dir = _write_trial(tmp_path, "job-tuned", "task-tuned__abc", steps)
    row = trial_features(job_dir, trial_dir)
    assert row["first_edit_step"] == 2
    assert any("mod.py" in path for path in row["first_edit_paths"])
    assert "awk" in row["tools_used"]


def test_failed_fetch_counts_as_attempt(tmp_path: Path) -> None:
    """A failed ``pip download`` is still an upstream fetch attempt."""
    steps = [
        _step(
            5,
            "pip download hpccm==22.1.0 --no-deps -d /tmp/hpccm_pkg",
            content="ERROR: Could not find a version that satisfies the requirement hpccm",
        ),
    ]
    job_dir, trial_dir = _write_trial(tmp_path, "job", "task__abc", steps)
    row = trial_features(job_dir, trial_dir)
    assert row["n_fetch_attempts"] == 1
    assert row["first_fetch_step"] == 5
    assert row["n_fetch_confirmed"] == 0


def test_limit_hit_names_binding_ceiling(tmp_path: Path) -> None:
    """TrialBudgetExhausted maps to whichever ceiling is closest to its cap."""
    steps = [_step(1, "ls /repo")]
    result: dict[str, Any] = {
        "task_name": "org/format-code-task-000169",
        "finished_at": "2026-10-01T14:26:16Z",
        "agent_result": {
            "n_input_tokens": 2_400_000,
            "n_output_tokens": 20_000,
            "metadata": {"n_episodes": 50},
        },
        "verifier_result": {"rewards": {"reward": 0.0}},
        "exception_info": {"exception_type": "TrialBudgetExhaustedError"},
    }
    job_dir, trial_dir = _write_trial(tmp_path, "job", "task__abc", steps, result=result)
    (job_dir / "lab-metadata.json").write_text(
        json.dumps(
            {
                "limits": {
                    "max_input_tokens": 2_500_000,
                    "max_output_tokens": 131_072,
                    "max_requests": 120,
                    "max_total_tokens": 2_631_072,
                }
            }
        ),
        encoding="utf-8",
    )
    row = trial_features(job_dir, trial_dir)
    assert row["task_id"] == "format-code-task-000169"
    assert row["stop_reason"] == "trial_budget_exhausted"
    assert row["limit_hit"] == "input_tokens"
