"""Incomplete or tool-using proposer output must not become an accepted candidate."""

import json

import pytest

from evallab.gepa_optimizer.opencode_transport import OpenCodeTransportError, parse_response


def test_incomplete_or_tool_turn_cannot_supply_candidate(tmp_path):
    path = tmp_path / "events.jsonl"
    text = {"type": "text", "part": {"text": "def example():\n    return 1\n"}}
    path.write_text(json.dumps(text) + "\n")
    with pytest.raises(OpenCodeTransportError):
        parse_response(path)
    stop = {"type": "step_finish", "part": {"reason": "stop"}}
    path.write_text("\n".join(map(json.dumps, [text, stop])))
    assert parse_response(path) == text["part"]["text"]
    tool = {"type": "tool_use", "part": {"tool": "bash", "state": {"status": "error"}}}
    path.write_text("\n".join(map(json.dumps, [text, tool, stop])))
    with pytest.raises(OpenCodeTransportError):
        parse_response(path)


def test_truncated_proposal_is_not_a_completed_module(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            map(
                json.dumps,
                [
                    {"type": "text", "part": {"text": "def example():"}},
                    {"type": "step_finish", "part": {"reason": "length"}},
                ],
            )
        )
    )
    with pytest.raises(OpenCodeTransportError):
        parse_response(path)


def test_live_event_monitor_preserves_partial_utf8_and_rejects_tool(tmp_path):
    from evallab.gepa_optimizer.opencode_transport import _poll_events

    path = tmp_path / "events.jsonl"
    event = json.dumps({"type": "text", "part": {"text": "café"}}, ensure_ascii=False).encode()
    split = event.index("é".encode()) + 1
    path.write_bytes(event[:split])
    offset, pending = _poll_events(path, 0, b"")
    with path.open("ab") as stream:
        stream.write(event[split:] + b"\n")
    offset, pending = _poll_events(path, offset, pending)
    assert pending == b""
    with path.open("ab") as stream:
        stream.write(b'{"type":"tool_use"}\n')
    with pytest.raises(OpenCodeTransportError):
        _poll_events(path, offset, pending)
