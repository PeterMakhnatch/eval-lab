from __future__ import annotations

import copy
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from evallab.observation_masking import LastNObservations, validate_context


def history(count: int) -> list[dict]:
    messages = [
        {"role": "system", "content": "Keep the task safe."},
        {"role": "user", "content": "Find the real failure."},
    ]
    for index in range(count):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "Inspect evidence",
                    "tool_calls": [
                        {
                            "id": f"call-{index}",
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": json.dumps({"command": f"cat log-{index}"}),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"call-{index}",
                    "content": f"evidence-{index}\n" * 20,
                    "extra": {"returncode": index},
                },
            ]
        )
    return messages


def test_only_old_observation_bodies_change_without_mutating_history() -> None:
    messages = history(4)
    messages.insert(4, {"role": "user", "content": "Additional instruction: preserve me."})
    before = copy.deepcopy(messages)
    masked = LastNObservations(2).apply(messages)
    assert messages == before
    observations = [i for i, message in enumerate(messages) if message["role"] == "tool"]
    for index, message in enumerate(messages):
        if index in observations[:-2]:
            assert message["content"] not in masked[index]["content"]
            assert {k: v for k, v in masked[index].items() if k != "content"} == {
                k: v for k, v in message.items() if k != "content"
            }
        else:
            assert masked[index] == message
    validate_context(masked)


@pytest.mark.parametrize("count", [0, 1, 3])
def test_history_at_or_below_window_is_unchanged(count: int) -> None:
    messages = history(count)
    assert LastNObservations(3).apply(messages) == messages


def test_parallel_calls_preserve_ids_order_and_recent_result() -> None:
    messages = history(2)
    messages[2]["tool_calls"].extend(messages[4]["tool_calls"])
    del messages[4]
    masked = LastNObservations(1).apply(messages)
    assert masked[2] == messages[2]
    assert [m.get("tool_call_id") for m in masked] == [m.get("tool_call_id") for m in messages]
    assert masked[-1] == messages[-1]
    assert masked[3]["content"] != messages[3]["content"]
    validate_context(masked)


def test_old_multimodal_observation_preserves_nontext_blocks_and_metadata() -> None:
    messages = history(2)
    messages[3]["content"] = [
        {
            "type": "text",
            "text": "Old verbose output\n" * 30,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,fixture"}},
    ]
    before = copy.deepcopy(messages)
    masked = LastNObservations(1).apply(messages)
    assert masked[3]["content"][0]["text"] != messages[3]["content"][0]["text"]
    assert masked[3]["content"][0]["cache_control"] == messages[3]["content"][0]["cache_control"]
    assert masked[3]["content"][1] == messages[3]["content"][1]
    assert masked[-1] == messages[-1]
    assert messages == before


def test_keep_tag_preserves_old_evidence_without_sacrificing_recent_window() -> None:
    messages = history(3)
    messages[3]["tags"] = ["keep_output"]
    masked = LastNObservations(1).apply(messages)
    assert masked[3] == messages[3]
    assert masked[5]["content"] != messages[5]["content"]
    assert masked[-1] == messages[-1]


@pytest.mark.parametrize("keep", [0, -1, True, 1.5])
def test_invalid_window_is_rejected(keep: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        LastNObservations(keep)


@pytest.mark.parametrize("defect", ["orphan", "duplicate", "missing", "interrupted"])
def test_malformed_linkage_is_not_hidden_by_masking(defect: str) -> None:
    messages = history(2)
    if defect == "orphan":
        messages[3]["tool_call_id"] = "absent"
    elif defect == "duplicate":
        messages[4]["tool_calls"][0]["id"] = "call-0"
    elif defect == "missing":
        messages.pop()
    else:
        messages.insert(3, {"role": "user", "content": "Interrupt"})
    with pytest.raises(ValueError):
        LastNObservations(1).apply(messages)


def test_cli_emits_deterministic_private_context_and_refuses_source_overwrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "native.json"
    source.write_text(json.dumps({"messages": history(3)}))
    before = source.read_bytes()
    outputs = [tmp_path / "first.json", tmp_path / "second.json"]
    command = [
        sys.executable,
        "-m",
        "evallab.observation_masking",
        "--input",
        str(source),
        "--keep-last",
        "1",
        "--output",
    ]
    for output in outputs:
        result = subprocess.run([*command, str(output)], check=True, capture_output=True, text=True)
        report = json.loads(result.stdout)
        assert report["metrics"]["saved_utf8_bytes"] > 0
        assert "messages" not in report
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        validate_context(json.loads(output.read_text())["messages"])
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    result = subprocess.run([*command, str(source)], capture_output=True, text=True)
    assert result.returncode != 0
    assert source.read_bytes() == before


def test_native_responses_items_keep_reasoning_and_call_result_identity() -> None:
    messages = [
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "Instructions"}],
        },
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Task"}]},
        {"type": "reasoning", "encrypted_content": "opaque-retained-content"},
        {"type": "custom_tool_call", "call_id": "custom-1", "name": "shell", "input": "pwd"},
        {
            "type": "custom_tool_call_output",
            "call_id": "custom-1",
            "output": [{"type": "input_text", "text": "old output\n" * 30}],
        },
        {"type": "function_call", "call_id": "function-2", "name": "bash", "arguments": "{}"},
        {
            "type": "function_call_output",
            "call_id": "function-2",
            "output": "Latest exact evidence",
        },
    ]
    before = copy.deepcopy(messages)
    masked = LastNObservations(1).apply(messages)
    assert masked[:4] == messages[:4]
    assert masked[4]["output"] != messages[4]["output"]
    assert masked[4]["call_id"] == "custom-1"
    assert masked[5:] == messages[5:]
    assert messages == before
    validate_context(masked)


def test_rollout_cli_preserves_native_base_instructions_and_payloads(tmp_path: Path) -> None:
    metadata = {
        "base_instructions": {"text": "Exact system instructions"},
        "cli_version": "fixture",
    }
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Task"}]},
        {"type": "custom_tool_call", "call_id": "a", "name": "shell", "input": "ls"},
        {"type": "custom_tool_call_output", "call_id": "a", "output": "Old output\n" * 30},
        {"type": "custom_tool_call", "call_id": "b", "name": "shell", "input": "pwd"},
        {"type": "custom_tool_call_output", "call_id": "b", "output": "Current directory"},
    ]
    events = [{"type": "session_meta", "payload": metadata}]
    events.extend({"type": "response_item", "payload": item} for item in items)
    source = tmp_path / "rollout.jsonl"
    source.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    output = tmp_path / "context.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "evallab.observation_masking",
            "--input",
            str(source),
            "--input-format",
            "codex-rollout",
            "--keep-last",
            "1",
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    artifact = json.loads(output.read_text())
    assert artifact["native_session_metadata"] == [metadata]
    assert artifact["messages"][0] == items[0]
    assert artifact["messages"][-2:] == items[-2:]
    assert artifact["messages"][2]["output"] != items[2]["output"]
    validate_context(artifact["messages"])
