"""Opt-in last-N tool-observation masking; no model calls or trajectory rewriting.

Adapted from LastNObservations (polling=1) in:
https://github.com/JetBrains-Research/the-complexity-trap/blob/bf15b5fb7d279679035a007ac9a81084d6b9a89a/sweagent/agent/history_processors.py
Unlike SWE-agent's tagged history, this accepts native chat or Responses tool items.
All user/system/developer messages survive, including the initial task. Only old
text observations are masked; tool IDs, non-text blocks and metadata survive.
There is no always-remove override: recent observations must remain lossless.

Upstream MIT License:
Copyright (c) 2024 John Yang, Carlos E. Jimenez, Alexander Wettig, Shunyu Yao,
Karthik Narasimhan, Ofir Press
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POLICY_VERSION = "last-n-observations-v1"


def context_bytes(messages: list[dict[str, Any]]) -> bytes:
    """Canonical UTF-8 footprint, not a tokenizer or provider cost estimate."""
    return json.dumps(
        messages, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def _observation_field(message: dict[str, Any]) -> str | None:
    if message.get("role") == "tool":
        return "content"
    if message.get("type") in {"function_call_output", "custom_tool_call_output"}:
        return "output"
    return None


def validate_context(messages: list[dict[str, Any]]) -> None:
    """Check explicit call/result linkage without inferring user observations."""
    pending: set[str] = set()
    seen: set[str] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"message {index} must be an object")
        role = message.get("role")
        kind = message.get("type")
        is_call = kind in {"function_call", "custom_tool_call"}
        field = _observation_field(message)
        if role not in {"system", "developer", "user", "assistant", "tool"} and kind not in {
            "reasoning",
            "function_call",
            "custom_tool_call",
            "function_call_output",
            "custom_tool_call_output",
        }:
            raise ValueError(f"message {index} has unsupported native context type")
        if pending and field is None and not is_call and kind != "reasoning":
            raise ValueError(f"message {index} interrupts pending tool results")
        if field is not None:
            call_id = message.get("tool_call_id" if role == "tool" else "call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise ValueError(f"message {index} has an unmatched tool result")
            pending.remove(call_id)
            content = message.get(field)
            if not isinstance(content, (str, list)):
                raise ValueError(f"message {index} has unsupported tool content")
            if isinstance(content, list) and any(not isinstance(block, dict) for block in content):
                raise ValueError(f"message {index} has malformed tool content blocks")
        if role == "assistant" or is_call:
            calls = [{"id": message.get("call_id")}] if is_call else message.get("tool_calls") or []
            if not isinstance(calls, list):
                raise ValueError(f"message {index} has malformed tool calls")
            for call in calls:
                call_id = call.get("id") if isinstance(call, dict) else None
                if not isinstance(call_id, str) or not call_id or call_id in seen:
                    raise ValueError(f"message {index} has a missing or duplicate tool call ID")
                seen.add(call_id)
                pending.add(call_id)
    if pending:
        raise ValueError("context ends before all tool results are present")


def _masked_text(text: str) -> str:
    return f"Old environment output: ({len(text.splitlines())} lines omitted)"


@dataclass(frozen=True)
class LastNObservations:
    """Keep the last N tool-result messages; never mask a user-role message.

    Returns a copy-on-write model view. Does not mutate input messages or their
    content blocks, and must not replace the harness's retained native history.
    """

    keep_last: int

    def __post_init__(self) -> None:
        if type(self.keep_last) is not int or self.keep_last <= 0:
            raise ValueError("keep_last must be a positive integer")

    def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        validate_context(messages)
        observations = [i for i, message in enumerate(messages) if _observation_field(message)]
        output = messages
        for index in observations[: -self.keep_last]:
            message = messages[index]
            if "keep_output" in (message.get("tags") or []):
                continue
            field = _observation_field(message)
            assert field is not None
            content = message[field]
            if isinstance(content, str):
                replacement: Any = _masked_text(content)
            else:
                replacement = [
                    {**block, "text": _masked_text(block["text"])}
                    if block.get("type") in {"text", "input_text", "output_text"}
                    and isinstance(block.get("text"), str)
                    else block
                    for block in content
                ]
            if replacement == content:
                continue
            if output is messages:
                output = list(messages)
            output[index] = {**message, field: replacement}
        return output

    def identity(self) -> dict[str, Any]:
        """Bind configuration and the implementation bytes for offline receipts."""
        return {
            "name": POLICY_VERSION,
            "keep_last": self.keep_last,
            "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--input", type=Path, required=True, help="Native messages list or object with messages"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New private derived-context JSON; never a run file",
    )
    parser.add_argument("--keep-last", type=int, required=True)
    parser.add_argument("--input-format", choices=("messages", "codex-rollout"), default="messages")
    args = parser.parse_args()
    try:
        source_bytes = args.input.read_bytes()
        session_metadata = []
        if args.input_format == "codex-rollout":
            events = [json.loads(line) for line in source_bytes.splitlines() if line.strip()]
            if any(not isinstance(event, dict) for event in events):
                raise ValueError("rollout events must be objects")
            messages = [
                event["payload"] for event in events if event.get("type") == "response_item"
            ]
            session_metadata = [
                event["payload"] for event in events if event.get("type") == "session_meta"
            ]
            if not messages or not session_metadata:
                raise ValueError("rollout requires retained response_item and session_meta records")
        else:
            source = json.loads(source_bytes)
            messages = source.get("messages") if isinstance(source, dict) else source
        if not isinstance(messages, list):
            raise ValueError(
                "input must contain native messages, not ATIF steps or reconstructed history"
            )
        policy = LastNObservations(args.keep_last)
        transformed = policy.apply(messages)
        # Base instructions remain in their original native metadata object, not
        # fabricated system messages. Include these unchanged bytes in both views.
        retained_metadata = (
            [{"native_session_metadata": session_metadata}] if session_metadata else []
        )
        before = context_bytes(retained_metadata + messages)
        after = context_bytes(retained_metadata + transformed)
        identity = policy.identity()
        policy_sha = hashlib.sha256(context_bytes([identity])).hexdigest()
        changed = [
            i for i, (old, new) in enumerate(zip(messages, transformed, strict=True)) if old != new
        ]
        receipt = {
            "artifact_type": "offline-model-context",
            "schema_version": 1,
            "policy": identity,
            "input_format": args.input_format,
            "policy_sha256": policy_sha,
            "source": {
                "path": str(args.input.resolve()),
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
            },
            "metrics": {
                "messages": len(messages),
                "observations": sum(
                    _observation_field(message) is not None for message in messages
                ),
                "masked_observations": len(changed),
                "masked_indices": changed,
                "before_utf8_bytes": len(before),
                "after_utf8_bytes": len(after),
                "saved_utf8_bytes": len(before) - len(after),
                "before_context_sha256": hashlib.sha256(before).hexdigest(),
                "after_context_sha256": hashlib.sha256(after).hexdigest(),
            },
            "limits": "Offline retained context view, not a replay or exact historical provider request; no token, cost, reward or performance claim.",
        }
        artifact = {**receipt, "messages": transformed, "native_session_metadata": session_metadata}
        encoded = (
            json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            + "\n"
        ).encode()
        # Exclusive creation refuses source overwrite and existing/symlink targets.
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
        print(json.dumps(receipt, sort_keys=True, indent=2))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"observation masking: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
