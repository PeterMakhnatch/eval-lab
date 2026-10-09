"""Focused behavioral tests for the CheatBench converter (no network, $0)."""

from __future__ import annotations

import json

import pytest

import evallab.cheatbench as mod
from evallab.cheatbench import (
    REVISION,
    SHA256,
    CheatbenchError,
    _anchor_line,
    _split_rendered_log,
    build_annotation,
    convert_row,
    convert_trace,
    counts_table,
    fetch,
    is_subset_row,
    locate_cheat,
    locate_row,
    message_number,
    parse_function_markup,
    parse_openai_tool_calls,
    parse_raw_trace,
    resolve_field_path,
    select_subset,
    text_of,
    validate_staged,
)


def _row(**overrides):
    base = {
        "trace_id": "cb-test-1",
        "benchmark": "swe-bench-verified",
        "harness": "SWE-agent",
        "model": "m",
        "label": "cheating",
        "cheating_type": "task",
        "cheating_mechanism": "secret_discovery_in_environment",
        "rationale": "peeked at future history",
        "note": None,
        "trace": "",
        "loc_kind": "localized_snippet",
        "loc_field_path": "messages[1].content",
        "loc_line_start": 1,
        "loc_line_end": 1,
        "loc_snippet": "git log",
        "loc_snippet_context": "messages[1].content L1-L1",
        "loc_evidence_source": "regex",
        "loc_confidence": 0.9,
    }
    base.update(overrides)
    return base


def test_resolve_field_path_nested_and_failure():
    obj = {"messages": [{"content": "hi"}, {"content": "there"}]}
    assert resolve_field_path(obj, "messages[1].content") == "there"
    with pytest.raises(LookupError):
        resolve_field_path(obj, "messages[7].content")
    with pytest.raises(LookupError):
        resolve_field_path(obj, "")


def test_parse_raw_trace_schema_policy():
    assert parse_raw_trace('{"id": "x", "messages": []}')["id"] == "x"
    assert parse_raw_trace('{"schema_version": "cheatbench.raw_trace.v1"}') == {
        "schema_version": "cheatbench.raw_trace.v1"
    }
    with pytest.raises(CheatbenchError):
        parse_raw_trace('{"schema_version": "something.else.v9"}')
    with pytest.raises(CheatbenchError):
        parse_raw_trace("not json")


def test_text_of_heterogeneous_content():
    assert text_of("abc") == "abc"
    assert text_of([{"text": "a", "type": "text"}, {"text": "b", "type": "text"}]) == "ab"
    assert text_of(None) == ""


def test_messages_mapping_preserves_order_roles_and_markup():
    trace = {
        "id": "t",
        "messages": [
            {"role": "system", "content": "sys"},
            {
                "role": "assistant",
                "content": "hi <function=execute_bash>\n<parameter=command>ls</parameter>\n</function>",
            },
            {"role": "user", "content": "OBSERVATION:\nok"},
        ],
    }
    steps, warnings = convert_trace(trace)
    assert [s["source"] for s in steps] == ["system", "agent", "user"]
    assert [s["step_id"] for s in steps] == [1, 2, 3]
    assert steps[1]["tool_calls"][0]["function_name"] == "execute_bash"
    assert steps[1]["tool_calls"][0]["arguments"] == {"command": "ls"}
    assert warnings == []


def test_function_markup_multiple_params():
    calls = parse_function_markup(
        "<function=f>\n<parameter=a>1</parameter>\n<parameter=b>2</parameter>\n</function>"
    )
    assert calls[0]["arguments"] == {"a": "1", "b": "2"}


def test_openai_tool_calls_string_arguments():
    calls = parse_openai_tool_calls(
        [{"id": "1", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}]
    )
    assert calls[0] == {
        "tool_call_id": "1",
        "function_name": "bash",
        "arguments": {"command": "ls"},
    }


def test_tool_event_attaches_to_previous_agent_with_call_id():
    trace = {
        "events": [
            {"role": "assistant", "content": "run it"},
            {"role": "tool", "content": "output", "tool_call_ids": ["c1"]},
        ]
    }
    steps, _ = convert_trace(trace)
    assert len(steps) == 1
    assert steps[0]["observation"]["results"] == [{"content": "output", "source_call_id": "c1"}]


def test_trace_role_maps_to_agent_and_action_inlined():
    trace = {
        "events": [
            {
                "role": "assistant",
                "content": "thinking",
                "message_type": "action",
                "action": "rm -rf /tmp/x",
                "tool_calls": [],
            }
        ]
    }
    steps, _ = convert_trace(trace)
    assert steps[0]["source"] == "agent"
    assert "rm -rf /tmp/x" in steps[0]["message"]
    assert steps[0]["extra"]["cheatbench"]["note"] == "action-inlined"
    assert steps[0]["tool_calls"][0]["arguments"] == {"command": "rm -rf /tmp/x"}


def test_step_event_tools_and_obs():
    trace = {
        "events": [
            {
                "msg": "do",
                "obs": "done",
                "src": "agent",
                "step": 3,
                "tools": [{"fn": "shell", "cmd": "ls"}],
            }
        ]
    }
    steps, _ = convert_trace(trace)
    assert steps[0]["tool_calls"][0]["tool_call_id"] == "call_3_0"
    assert steps[0]["observation"]["results"] == [{"content": "done"}]
    ref = steps[0]["extra"]["cheatbench"]
    assert ref["source_ref"] == {"field": "events[0].msg", "line_start": None, "line_end": None}
    assert ref["obs_refs"] == [{"field": "events[0].obs", "line_start": 1, "line_end": 1}]


def test_forgecode_stdout_segments_and_duplicate_text():
    stdout = "banner\n⏺ first\nline a\n⏺ second\nline b"
    trace = {
        "events": [
            {"command": "run", "stdout": stdout, "type": "x"},
            {"id": "t", "text": "⏺ first\nline a", "type": "y"},
        ]
    }
    steps, warnings = convert_trace(trace)
    fields = [s["extra"]["cheatbench"]["source_ref"]["field"] for s in steps]
    assert fields == ["events[0].stdout"] * 2
    assert any("contained in events[0].stdout" in w for w in warnings)
    assert steps[0]["extra"]["cheatbench"]["shell_command"] == "run"
    ranges = [
        (
            s["extra"]["cheatbench"]["source_ref"]["line_start"],
            s["extra"]["cheatbench"]["source_ref"]["line_end"],
        )
        for s in steps
    ]
    assert ranges == [(1, 3), (4, 5)]


def test_message_number_counts_tool_calls_not_observations():
    steps, _ = convert_trace(
        {
            "events": [
                {"role": "assistant", "content": "a"},
                {"role": "tool", "content": "out"},
                {
                    "msg": "do",
                    "src": "agent",
                    "step": 0,
                    "tools": [{"fn": "shell", "cmd": "x"}, {"fn": "shell", "cmd": "y"}],
                },
            ]
        }
    )
    # Step 1 (agent) + attached tool output (no badge) + step 2 (agent, 2 calls).
    assert message_number(steps, 2, None) == 2
    assert len(steps) == 2


def test_split_rendered_log_without_markers():
    assert _split_rendered_log("a\nb") == [("a\nb", 1, 2)]
    assert _split_rendered_log("") == []


def test_prefixed_log_turns_and_tool_calls():
    content = "\n".join(
        [
            "[0001] role=assistant name=-",
            "[0001] look at history",
            "[0001] TOOL_CALL[1] execute_bash: git log --all",
            "[0002] role=tool name=execute_bash",
            "[0002] abc123 first commit",
        ]
    )
    steps, _ = convert_trace({"events": [{"content": content, "kind": "x", "role": "trace"}]})
    assert [s["source"] for s in steps] == ["agent"]
    assert steps[0]["tool_calls"][0]["function_name"] == "execute_bash"
    assert "abc123" in steps[0]["observation"]["results"][0]["content"]


def test_locate_exact_stdout_line_and_message_number():
    steps, _ = convert_trace(
        {"events": [{"command": "r", "stdout": "banner\n⏺ a\nx\n⏺ b\ny", "type": "x"}]}
    )
    step_id, result, correction = locate_cheat(steps, "events[0].stdout", 5)
    assert (step_id, result, correction) == (2, None, None)
    assert message_number(steps, 2, None) == 2
    step_id, _, _ = locate_cheat(steps, "events[0].stdout", 2)
    assert step_id == 1


def test_locate_tools_cmd_falls_back_to_owning_event():
    trace = {
        "events": [
            {
                "msg": "m",
                "obs": "o",
                "src": "agent",
                "step": 0,
                "tools": [{"fn": "shell", "cmd": "evil"}],
            }
        ]
    }
    steps, _ = convert_trace(trace)
    step_id, result, correction = locate_cheat(steps, "events[0].tools[0].cmd", 1)
    assert (step_id, result, correction) == (1, None, "event-index-fallback")
    # The obs subfield resolves exactly to the tool output, which Scout
    # renders without a badge number: step only, no message number.
    step_id, result, correction = locate_cheat(steps, "events[0].obs", 1)
    assert (step_id, result, correction) == (1, 0, None)
    assert message_number(steps, 1, 0) is None


def test_locate_single_event_fallback_and_empty_path():
    trace = {
        "events": [{"content": "[0001] role=assistant name=-\nhi", "kind": "x", "role": "trace"}]
    }
    steps, _ = convert_trace(trace)
    step_id, _, correction = locate_cheat(steps, "events[1].content", 1)
    assert step_id == 1 and correction == "single-event-fallback"
    assert locate_cheat(steps, "", 1) == (None, None, None)
    assert locate_cheat(steps, "messages[0].content", 1) == (None, None, None)


def test_anchor_line_snippet_calibration_and_pipe_variant():
    content = "line one\nline two cheat here\nline three"
    line, adjusted = _anchor_line(content, 1, "two cheat")
    assert (line, adjusted) == (2, True)
    line, adjusted = _anchor_line(content, 2, "two cheat")
    assert (line, adjusted) == (2, False)
    line, _ = _anchor_line("a\nb", 9, "a|b")
    assert line == 1  # pipe variant maps onto real newlines
    line, adjusted = _anchor_line(content, 1, "absent needle")
    assert (line, adjusted) == (1, False)


def test_locate_row_single_event_path_rewrite():
    trace = {
        "events": [
            {"content": "[0001] role=assistant name=-\ngit log --all", "kind": "x", "role": "trace"}
        ]
    }
    steps, _ = convert_trace(trace)
    row = _row(
        trace=json.dumps(trace),
        loc_field_path="events[1].content",
        loc_line_start=2,
        loc_snippet="git log",
    )
    step_id, _, correction, adjusted = locate_row(trace, steps, row)
    assert step_id == 1 and correction == "single-event-fallback" and adjusted is False


def test_convert_row_end_to_end_annotation_and_meta():
    trace = {
        "id": "t",
        "messages": [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "git log --all shows the fix"},
        ],
    }
    row = _row(trace=json.dumps(trace))
    trajectory, meta, warnings = convert_row(row)
    assert validate_staged(trajectory) == []
    assert trajectory["session_id"] == "cheatbench-cb-test-1"
    assert trajectory["steps"][-1]["message"].startswith("[CheatBench annotation")
    assert "transcript message M2" in trajectory["steps"][-1]["message"]
    assert "secret_discovery_in_environment" in trajectory["steps"][-1]["message"]
    assert meta["cheat_step_id"] == 2
    assert meta["reward"] == 1.0 and meta["verdict"] == "passed"
    assert meta["loc_corrected"] is False and warnings == []


def test_convert_row_unlocalized_and_attempt_reward():
    trace = {"events": [{"msg": "m", "src": "agent", "step": 0}]}
    row = _row(
        trace=json.dumps(trace),
        label="attempt",
        loc_field_path="",
        loc_line_start=None,
        loc_kind="unlocalized",
        cheating_mechanism="verifier_exploitation",
    )
    trajectory, meta, _ = convert_row(row)
    assert meta["cheat_step_id"] is None and meta["reward"] == 0.0
    assert "unlocalized" in trajectory["steps"][-1]["message"]


def test_validate_staged_catches_bad_source_and_ids():
    row = _row(trace=json.dumps({"messages": [{"role": "user", "content": "x"}]}))
    trajectory, _, _ = convert_row(row)
    trajectory["steps"][0]["source"] = "tool"
    assert any("invalid source" in issue for issue in validate_staged(trajectory))
    trajectory["steps"][0]["source"] = "user"
    trajectory["steps"][0]["step_id"] = 7
    assert any("step_id must be 1" in issue for issue in validate_staged(trajectory))


def test_subset_predicate_and_counts():
    rows = [
        _row(
            trace_id="a",
            benchmark="b1",
            label="cheating",
            cheating_mechanism="secret_discovery_in_environment",
        ),
        _row(
            trace_id="b",
            benchmark="b1",
            label="attempt",
            cheating_mechanism="verifier_exploitation",
        ),
        _row(
            trace_id="c",
            benchmark="b2",
            label="cheating",
            cheating_mechanism="harness_leaks_answer",
        ),
        _row(trace_id="d", benchmark="b2", label="benign", cheating_mechanism="none"),
    ]
    assert [r["trace_id"] for r in select_subset(rows)] == ["a", "b"]
    assert counts_table(rows) == [
        ("b1", "secret_discovery_in_environment", "cheating", 1),
        ("b1", "verifier_exploitation", "attempt", 1),
    ]
    assert is_subset_row(rows[0]) and not is_subset_row(rows[2])


def test_fetch_verifies_digest_and_short_circuits(tmp_path):
    dest = tmp_path / "full.parquet"
    with pytest.raises(CheatbenchError):
        fetch(dest, downloader=lambda url: b"parquet-bytes")
    assert not dest.exists()
    real = mod.SHA256
    try:
        mod.SHA256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
        assert fetch(dest, downloader=lambda url: b"test").read_bytes() == b"test"
        calls = []
        assert fetch(dest, downloader=lambda url: calls.append(url) or b"other") == dest
        assert calls == []
    finally:
        mod.SHA256 = real


def test_annotation_correction_notes():
    row = _row()
    text = build_annotation(
        row,
        cheat_step_id=3,
        cheat_message_no=5,
        correction="event-index-fallback",
        line_adjusted=True,
        warnings=["w1"],
        n_steps=9,
    )
    assert "ATIF step 3 of 9, transcript message M5" in text
    assert "no own text span" in text
    assert "snippet search" in text
    assert "converter warning: w1" in text


def test_pinned_constants_match_record():
    assert REVISION == "461360b34e93644955563dcc9243c6cc1a53bd0f"
    assert SHA256 == "b4d2a1496e5f8de7b4c0e4791bd9f8fcbd0a3567f95d2e759b5da20ebb160e63"
