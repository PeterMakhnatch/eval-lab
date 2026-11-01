"""Serve a hidden multi-tool turn through the submitted serve_turn and report what the monitor saw."""

import json
import os
from pathlib import Path

from serving.harness import serve_turn
from serving.model import ScriptedModel

SCRIPT = [
    {
        "text": "checking the docs and the math",
        "tool_calls": [
            {"name": "search_docs", "args": {"query": "monitoring"}},
            {"name": "calc", "args": {"expr": "7 * 6"}},
        ],
    },
    {"text": "one more", "tool_calls": [{"name": "calc", "args": {"expr": "1 + 1"}}]},
    {"text": "all done"},
]

model = ScriptedModel(SCRIPT)
deltas: list[str] = []
result = serve_turn(model, "probe turn", on_delta=deltas.append)
events_file = Path(os.environ["MONITOR_EVENTS"])
kinds: dict[str, int] = {}
if events_file.exists():
    for line in events_file.read_text().splitlines():
        kind = json.loads(line)["kind"]
        kinds[kind] = kinds.get(kind, 0) + 1
tools = [(m["name"], m["content"]) for m in result.messages if m["role"] == "tool"]
print(
    json.dumps(
        {
            "stream_calls": model.stream_calls,
            "complete_calls": model.complete_calls,
            "delta_text": "".join(deltas).strip(),
            "text": result.text,
            "tools": tools,
            "event_kinds": kinds,
            "saved_context_keys": sorted(result.saved.get("context", {})),
        }
    )
)
