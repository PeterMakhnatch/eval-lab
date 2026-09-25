"""Envelope unwrapping and outline error accounting for agent transports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.traj import outline_trajectory
from evallab.trajectory_error_taxonomy import (
    ErrorCategory,
    classify_step_error,
    split_envelope,
)


def test_split_envelope_unwraps_mini_swe_agent_results() -> None:
    code, text = split_envelope(json.dumps({"returncode": 2, "output": "boom"}))
    assert code == 2
    assert text == "boom"


def test_split_envelope_passes_through_non_envelopes() -> None:
    assert split_envelope("plain output") == (None, "plain output")
    assert split_envelope('{"output": "no code"}') == (
        None,
        '{"output": "no code"}',
    )
    assert split_envelope('{"returncode": "1", "output": "x"}') == (
        None,
        '{"returncode": "1", "output": "x"}',
    )
    assert split_envelope(None) == (None, "")
    assert split_envelope("{not json") == (None, "{not json")


def test_split_envelope_unwraps_truncated_output() -> None:
    content = json.dumps({"returncode": 1, "output_head": "start of log", "output_tail": "boom"})
    code, text = split_envelope(content)
    assert code == 1
    assert text.startswith("start of log") and text.endswith("boom")


def test_envelope_exit_drives_classification() -> None:
    code, text = split_envelope(json.dumps({"returncode": 1, "output": "boom"}))
    classification = classify_step_error(
        tool_name="bash", tool_command="pytest -q", exit_code=code,
        output_content=text,
    )
    assert classification.is_error is True
    assert classification.category == ErrorCategory.COMMAND_NONZERO_EXIT
    assert classification.error_message == "boom"


def test_envelope_probe_miss_stays_a_probe() -> None:
    code, text = split_envelope(json.dumps({"returncode": 1, "output": ""}))
    classification = classify_step_error(
        tool_name="bash", tool_command="grep -r pattern .", exit_code=code,
        output_content=text,
    )
    assert classification.is_error is False
    assert classification.is_expected_probe is True


def test_rejection_words_inside_read_output_are_not_a_rejection() -> None:
    # A successful read of source code that raises "Invalid JSON" deep in its body.
    source_listing = "def load(body):\n    pass\n" * 40 + "    raise HTTPError(400, 'Invalid JSON')\n"
    for exit_code in (None, 0):
        classification = classify_step_error(
            tool_name="exec", tool_command="sed -n '1,200p' bottle.py", exit_code=exit_code,
            output_content=source_listing,
        )
        assert classification.is_error is False


def test_leading_harness_rejection_is_still_classified() -> None:
    classification = classify_step_error(
        tool_name="bash", tool_command=None, exit_code=None,
        output_content="Invalid parameters: missing required argument 'command'",
    )
    assert classification.is_error is True
    assert classification.category == ErrorCategory.HARNESS_SCHEMA_REJECTION


def _trial(path: Path, steps: list[dict[str, Any]]) -> Path:
    agent = path / "agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "session_id": "synthetic-envelope",
                "agent": {"name": "fixture", "model_name": "fixture-model"},
                "steps": steps,
            }
        ),
        encoding="utf-8",
    )
    (path / "result.json").write_text(
        json.dumps(
            {
                "id": "envelope-id",
                "trial_name": path.name,
                "task_name": "fixture-task",
                "config": {"agent": {"name": "fixture"}},
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    return path


def _envelope_step(message: str, command: str, code: int, output: str) -> dict[str, Any]:
    return {
        "source": "agent",
        "message": message,
        "tool_calls": [{"function_name": "bash", "arguments": {"command": command}}],
        "observation": {
            "results": [{"content": json.dumps({"returncode": code, "output": output})}]
        },
    }


def test_outline_unwraps_envelopes_for_error_accounting(tmp_path: Path) -> None:
    trial = _trial(
        tmp_path / "trial",
        [
            _envelope_step("run", "pytest -q", 1, "1 failed"),
            _envelope_step("run again", "pytest -q", 0, "ok"),
        ],
    )
    outline = outline_trajectory(trial, explicit_runs_root=trial)
    assert outline.status == "featured"
    assert outline.total_errors == 1
    assert outline.step_to_first_error == 1
    assert outline.recovery_count == 1
    assert outline.unrecovered_at_terminal is False
    failing = outline.steps[0]
    assert failing.is_error is True
    assert failing.error_category == ErrorCategory.COMMAND_NONZERO_EXIT.value
    # The outline keeps its generic exit-code message (pre-existing contract
    # shared by all transports); what matters is the message is the real
    # output context, never the raw JSON envelope.
    assert failing.error_message == "command exited with code 1"
    assert "returncode" not in (failing.error_message or "")


def test_outline_terminal_envelope_error_is_unrecovered(tmp_path: Path) -> None:
    trial = _trial(
        tmp_path / "trial",
        [
            _envelope_step(
                "run",
                "pytest -q",
                1,
                "Traceback (most recent call last):\n"
                '  File "t.py", line 1\nBoom',
            )
        ],
    )
    outline = outline_trajectory(trial, explicit_runs_root=trial)
    assert outline.total_errors == 1
    assert outline.unrecovered_at_terminal is True
    assert outline.steps[0].error_category == ErrorCategory.RUNTIME_EXCEPTION.value
