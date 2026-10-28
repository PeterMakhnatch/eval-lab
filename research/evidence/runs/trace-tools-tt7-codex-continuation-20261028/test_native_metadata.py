from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(scope="module")
def audit() -> Any:
    path = Path(__file__).with_name("audit_native_metadata.py")
    spec = importlib.util.spec_from_file_location("tt7_native_metadata_audit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assistant() -> dict[str, Any]:
    return {"role": "assistant", "provider": "openai-codex", "model": "gpt-6-luna", "stopReason": "stop",
            "content": [{"type": "text", "text": "untrusted-body-canary"},
                        {"type": "thinking", "thinking": "untrusted-body-canary"},
                        {"type": "toolCall", "name": "manage_skill", "arguments": {"body": "untrusted-body-canary"}}]}


def test_only_turn_name_status_metadata_is_retained(audit: Any) -> None:
    events = [
        {"type": "message_end", "message": assistant()},
        {"type": "tool_execution_start", "toolName": "manage_skill", "args": {"body": "untrusted-body-canary"}},
        {"type": "tool_execution_end", "toolName": "manage_skill", "isError": False,
         "result": {"content": [{"text": "untrusted-body-canary"}], "details": {"name": "untrusted-body-canary"}}},
        {"type": "agent_end", "messages": [assistant()]},
    ]
    selected = [audit.event_metadata(event) for event in events]
    assert "untrusted-body-canary" not in json.dumps(selected)
    assert selected[0]["tool_call_blocks"] == 1
    assert selected[1] == {"event": "tool_execution_start", "tool": "manage_skill"}
    assert selected[2] == {"event": "tool_execution_end", "tool": "manage_skill", "isError": False}
    assert audit.event_metadata({"type": "message_update", "raw": "untrusted-body-canary"}) is None


def test_unknown_identifiers_are_opaque(audit: Any) -> None:
    selected = audit.event_metadata({"type": "tool_execution_start", "toolName": "untrusted-body-canary"})
    assert selected["tool"].startswith("other-sha256:")
    assert "untrusted-body-canary" not in json.dumps(selected)


def test_two_assistant_turns_and_real_tool_end_are_not_collapsed(audit: Any, tmp_path: Path) -> None:
    first, second = assistant(), assistant()
    first["stopReason"] = "toolUse"
    events = [
        {"type": "message_end", "message": first},
        {"type": "tool_execution_start", "toolName": "manage_skill", "args": {"body": "untrusted-body-canary"}},
        {"type": "tool_execution_end", "toolName": "manage_skill", "isError": False},
        {"type": "message_end", "message": second},
        {"type": "agent_end", "messages": [first, {"role": "toolResult", "content": "untrusted-body-canary"}, second]},
    ]
    path = tmp_path / "form-attempt-0-events.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    stream = audit.audit_file(path, "codex-continuation-storage")
    report = audit.summarize([stream])
    assert report["streams"] == 1 and report["assistant_turns"] == 2
    assert report["multi_assistant_turn_streams"] == 1
    assert report["missing_final_agent_end_streams"] == 0
    assert report["assistant_end_vs_final_agent_end_count_mismatches"] == 0
    assert report["tools"]["manage_skill"] == {"starts": 1, "ends": 1, "successful_ends": 1, "error_ends": 0}
    assert "untrusted-body-canary" not in json.dumps(report)
    assert report["capability_boundary"]["zero_advertised_tools_attested"] is False
    assert report["capability_boundary"]["explicit_tool_handler_deny_attested"] is False
    assert report["capability_boundary"]["future_inference_authorized"] is False


def test_incomplete_or_unparseable_capture_is_not_called_complete(audit: Any, tmp_path: Path) -> None:
    path = tmp_path / "partial-events.jsonl"
    path.write_text('{"type":"message_update","raw":"untrusted-body-canary"}\nnot-json\n')
    report = audit.summarize([audit.audit_file(path, "codex-continuation-storage")])
    assert report["malformed_json_lines"] == 1
    assert report["missing_final_agent_end_streams"] == 1
    assert report["all_retained_tool_events_are_known_single_anomaly"] is False
    assert "untrusted-body-canary" not in json.dumps(report)


def test_existing_audit_output_is_never_rewritten(audit: Any, tmp_path: Path) -> None:
    path = tmp_path / "audit.json"
    audit.write_once(path, b"first\n")
    with pytest.raises(FileExistsError):
        audit.write_once(path, b"second\n")
    assert path.read_bytes() == b"first\n"
