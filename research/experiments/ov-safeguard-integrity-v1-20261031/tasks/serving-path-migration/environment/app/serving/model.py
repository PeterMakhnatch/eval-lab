"""Scripted model used by the serving tests (no network, no weights).

``complete`` returns a whole reply; ``stream`` yields the same reply as deltas, with tool
calls split into a start chunk and argument fragments, as a real streaming API does.
"""

from __future__ import annotations

import json
from collections.abc import Iterator


class ScriptedModel:
    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.complete_calls = 0
        self.stream_calls = 0

    def complete(self, messages: list[dict]) -> dict:
        reply = self.script[self.complete_calls]
        self.complete_calls += 1
        return {"text": reply.get("text", ""), "tool_calls": list(reply.get("tool_calls", []))}

    def stream(self, messages: list[dict]) -> Iterator[dict]:
        reply = self.script[self.stream_calls]
        self.stream_calls += 1
        for word in reply.get("text", "").split(" "):
            if word:
                yield {"type": "text", "delta": word + " "}
        for index, call in enumerate(reply.get("tool_calls", [])):
            payload = json.dumps(call["args"])
            half = len(payload) // 2
            yield {"type": "tool_call_start", "index": index, "name": call["name"]}
            yield {"type": "tool_call_args", "index": index, "delta": payload[:half]}
            yield {"type": "tool_call_args", "index": index, "delta": payload[half:]}
        yield {"type": "end"}
