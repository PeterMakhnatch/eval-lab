"""Behaviour tests for the HAR-116 loop break and output cap."""

from __future__ import annotations

from typing import Any

import pytest

from evallab.loopfix import (
    LOOP_GRACE_CALLS,
    LOOP_NUDGE_MESSAGE,
    OUTPUT_CAP_CHARS,
    cap_output,
    live_loop_action,
    loop_decision,
)
from evallab.probe03 import LOOP_MIN_RUN
from evallab.token_flow import COMMAND_RUN_MIN


def _step(command: str, message: str = "working") -> dict[str, Any]:
    return {
        "source": "agent",
        "message": message,
        "tool_calls": [{"function_name": "bash_command", "arguments": {"keystrokes": command}}],
    }


def _edit() -> dict[str, Any]:
    return _step("python3 - <<'PY'\nopen('/app/x.py','w').write('x')\nPY\n", "editing")


def test_command_run_nudges_once_then_stops_after_grace() -> None:
    steps = [_step("pytest -q\n") for _ in range(COMMAND_RUN_MIN + LOOP_GRACE_CALLS)]
    # One short of the run: nothing fires.
    assert live_loop_action(steps[: COMMAND_RUN_MIN - 1]) is None
    # The call the run reaches its length gets the nudge, exactly once.
    assert live_loop_action(steps[:COMMAND_RUN_MIN]) == "nudge"
    for extra in range(1, LOOP_GRACE_CALLS):
        assert live_loop_action(steps[: COMMAND_RUN_MIN + extra]) is None
    # Still repeating five calls later: stop.
    assert live_loop_action(steps) == "stop"
    decision = loop_decision(steps)
    assert decision["nudge_call"] == COMMAND_RUN_MIN
    assert decision["detector"] == "normalized_command_run"
    assert decision["stop_call"] == COMMAND_RUN_MIN + LOOP_GRACE_CALLS
    assert decision["broke_at_call"] is None
    assert "repeating" in LOOP_NUDGE_MESSAGE


def test_loop_that_ends_does_not_stop() -> None:
    steps = [_step("pytest -q\n") for _ in range(COMMAND_RUN_MIN)]
    steps.append(_step("ls /app\n", "trying something else"))
    steps.extend(_step("pytest -q\n") for _ in range(LOOP_GRACE_CALLS))
    decision = loop_decision(steps)
    assert decision["nudge_call"] == COMMAND_RUN_MIN
    assert decision["broke_at_call"] == COMMAND_RUN_MIN + 1
    assert decision["stop_call"] is None
    # The live agent stops intervening the moment the repetition breaks,
    # including on the calls that would otherwise have been the grace window.
    assert live_loop_action(steps[: COMMAND_RUN_MIN + 1]) is None
    assert live_loop_action(steps) is None


def test_an_edit_inside_the_run_is_not_a_loop() -> None:
    steps = [_step("cat /app/x.py\n") for _ in range(COMMAND_RUN_MIN - 1)]
    steps.append(_edit())
    steps.extend(_step("cat /app/x.py\n") for _ in range(COMMAND_RUN_MIN))
    # The edit-bearing call breaks the no-progress run, so the onset is the
    # later run, not the earlier one.
    decision = loop_decision(steps)
    assert decision["nudge_call"] == len(steps)
    assert decision["stop_call"] is None


def test_identical_messages_fire_at_the_message_threshold() -> None:
    # Distinct commands (normalization collapses digits, so a counter would
    # itself be a command loop) sharing one message: the message run fires.
    steps = [
        _step(f"probe-{chr(97 + index)} /app\n", "still checking") for index in range(LOOP_MIN_RUN)
    ]
    assert live_loop_action(steps[: LOOP_MIN_RUN - 1]) is None
    assert live_loop_action(steps) == "nudge"
    decision = loop_decision(steps)
    assert decision["detector"] == "identical_message_run"
    assert decision["nudge_call"] == LOOP_MIN_RUN


def test_number_changes_do_not_break_a_command_loop() -> None:
    steps = [_step(f"sed -n {index}p /app/log\n") for index in range(COMMAND_RUN_MIN)]
    # Different line numbers, same normalized command: still one run.
    assert live_loop_action(steps) == "nudge"


def test_output_cap_keeps_head_and_tail_and_names_the_file() -> None:
    body = "H" * 1500 + "M" * 9000 + "T" * 1500
    spill = "/logs/agent/evallab-output/step-0007.txt"
    capped = cap_output(body, spill)
    assert capped.startswith("H" * (OUTPUT_CAP_CHARS // 2))
    assert capped.endswith("T" * (OUTPUT_CAP_CHARS - OUTPUT_CAP_CHARS // 2))
    assert spill in capped
    assert "10000 characters omitted" in capped
    # The full body is not what goes back, but both ends survive intact.
    assert "M" * 100 not in capped


def test_output_under_the_cap_is_unchanged() -> None:
    short = "all of it\n"
    assert cap_output(short, "/logs/agent/evallab-output/step-0001.txt") == short


def test_output_cap_rejects_a_limit_without_two_ends() -> None:
    with pytest.raises(ValueError):
        cap_output("abcdef", "/tmp/out.txt", limit=1)
