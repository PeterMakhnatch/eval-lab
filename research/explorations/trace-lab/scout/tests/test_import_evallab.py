"""Deterministic tests for ``scout/import_evallab.py`` (HAR-109, $0).

Verifies Eval Lab metadata (task, reward, verdict, stop, tokens, trial id)
and tool-call presence on the golden HAR-81 trial (``raw_content``: calls
synthesized from ``extra.step_layers``) and a synthetic stock-shaped trial
(HAR-104 shape: native ``tool_calls`` pass through). No model calls.

Run from the Eval Lab worktree root::

    uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 -- \\
        python -m pytest research/explorations/trace-lab/scout/tests/ \\
        -p no:cacheprovider -o addopts=''
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCOUT = HERE.parent
TRACE_LAB = SCOUT.parent
if str(SCOUT) not in sys.path:
    sys.path.insert(0, str(SCOUT))

import import_evallab as imp  # noqa: E402 -- path set above

R531 = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs")
GOLDEN = R531 / "har81-l-d-a2-arvo-18737" / "har81-l-d-a2-arvo-18737__2kbVhKB"


def test_golden_metadata_from_evallab():
    if not (GOLDEN / "result.json").is_file():
        pytest.skip("golden trial absent")
    meta = imp._report_metadata(GOLDEN)
    assert meta["task"] == "mimo-v2.6-rl/arvo_18737"
    assert meta["trial_name"] == "har81-l-d-a2-arvo-18737__2kbVhKB"
    assert meta["trial_id"] == "cd0833b4-3c1e-43aa-bdd2-078519ae8392"
    assert meta["reward"] == 0.0
    assert meta["verdict"] == "failed"
    assert meta["stop_reason"] == "trial_budget_exhausted"
    assert "input_tokens" in meta["stop_detail"]
    assert meta["input_tokens"] == 2414586
    assert meta["output_tokens"] == 6193
    assert meta["llm_steps"] == 117
    assert meta["source"] == "evallab:build_run_report"


def test_golden_staged_trajectory_has_tool_calls(tmp_path):
    if not (GOLDEN / "result.json").is_file():
        pytest.skip("golden trial absent")
    staged = imp.build_staged_trajectory(GOLDEN, tmp_path)
    trajectory = json.loads(staged.read_text(encoding="utf-8"))
    assert len(trajectory["steps"]) == 118
    n_calls = sum(len(s.get("tool_calls") or []) for s in trajectory["steps"])
    assert n_calls == 117
    refs = [s["extra"]["trace_lab"]["ref"] for s in trajectory["steps"]]
    assert refs[21] == "head#22"
    sources = {
        s["extra"]["trace_lab"]["tool_calls_source"] for s in trajectory["steps"][1:]
    }
    assert "recorded_or_replay" in sources


def _write_stock_trial(root: Path) -> Path:
    trial = root / "har104-smoke-job" / "har104-smoke-job__AbC123"
    agent = trial / "agent"
    agent.mkdir(parents=True)
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "session_id": "11111111-2222-3333-4444-555555555555",
        "agent": {"name": "terminus-2", "version": "2.0.0", "model_name": "m"},
        "steps": [
            {"step_id": 1, "timestamp": "2026-09-30T00:00:00+00:00",
             "source": "user", "message": "Do the thing."},
            {"step_id": 2, "timestamp": "2026-09-30T00:00:05+00:00",
             "source": "agent", "message": "listing",
             "tool_calls": [{"tool_call_id": "call_0_1", "function_name": "bash_command",
                             "arguments": {"keystrokes": "ls\n", "duration": 0.5}}],
             "observation": {"results": [{"content": "New Terminal Output:\n$", "source_call_id": "call_0_1"}]},
             "metrics": {"prompt_tokens": 500, "completion_tokens": 20}},
            {"step_id": 3, "timestamp": "2026-09-30T00:00:10+00:00",
             "source": "agent", "message": "done",
             "tool_calls": [{"tool_call_id": "call_1_1", "function_name": "mark_task_complete",
                             "arguments": {}}],
             "metrics": {"prompt_tokens": 520, "completion_tokens": 10}},
        ],
        "final_metrics": {"total_prompt_tokens": 1020, "total_completion_tokens": 30},
    }
    (agent / "trajectory.json").write_text(json.dumps(trajectory))
    (trial / "result.json").write_text(json.dumps({
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "task_name": "har104-smoke/demo_task",
        "trial_name": "har104-smoke-job__AbC123",
        "config": {},
        "agent_result": {"n_input_tokens": 1020, "n_output_tokens": 30,
                         "metadata": {"n_episodes": 3, "stop_reason": "task_complete"}},
        "verifier_result": {"rewards": {"reward": 1.0}},
        "exception_info": {"exception_type": None, "exception_message": ""},
        "started_at": "2026-09-30T00:00:00+00:00",
        "finished_at": "2026-09-30T00:00:15+00:00",
    }))
    (trial / "config.json").write_text(json.dumps({"trial_name": "har104-smoke-job__AbC123"}))
    return trial


def test_stock_trial_metadata_and_native_calls(tmp_path):
    trial = _write_stock_trial(tmp_path)
    meta = imp._report_metadata(trial)
    assert meta["task"] == "har104-smoke/demo_task"
    assert meta["reward"] == 1.0
    assert meta["verdict"] == "passed"
    staged = imp.build_staged_trajectory(trial, tmp_path / "staging")
    trajectory = json.loads(staged.read_text(encoding="utf-8"))
    calls = [c for s in trajectory["steps"] for c in (s.get("tool_calls") or [])]
    assert [c["function_name"] for c in calls] == ["bash_command", "mark_task_complete"]
    assert trajectory["steps"][1]["extra"]["trace_lab"]["tool_calls_source"] == "native"


def test_limit_names_binding_ceiling():
    assert imp._limit_of("trial_budget_exhausted", "agent metadata stop_reason; binding ceiling: input_tokens") == "input_tokens"
    assert imp._limit_of("agent_timeout", None) == "timeout"
    assert imp._limit_of("ceiling:requests", None) == "requests"
    assert imp._limit_of(None, None) is None
