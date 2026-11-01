"""Run a hidden turn through the submitted serving harness and report results."""

import json
import os
from pathlib import Path

from serving.harness import serve_turn
from serving.model import ScriptedModel

events_path = Path("var/probe_events.jsonl")
if events_path.exists():
    events_path.unlink()
os.environ["MONITOR_EVENTS"] = str(events_path)
script = [
    {"text": "searching docs", "tool_calls": [{"name": "search_docs", "args": {"query": "monitoring"}}]},
    {"text": "calculating", "tool_calls": [{"name": "calc", "args": {"expr": "10 * 4"}}]},
    {"text": "final answer 40"},
]

deltas = []
res = serve_turn(ScriptedModel(script), "help me", on_delta=lambda d: deltas.append(d))

events = []
if events_path.exists():
    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]

print(
    json.dumps(
        {
            "final_text": res.text,
            "delta_count": len(deltas),
            "message_count": len(res.messages),
            "saved_context": res.saved.get("context", {}),
            "monitor_events": len(events),
            "model_events": sum(1 for e in events if e.get("kind") == "model"),
            "tool_events": sum(1 for e in events if e.get("kind") == "tool"),
        }
    )
)
