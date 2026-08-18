import json
import math
from collections import Counter
from pathlib import Path

input_path = Path("/app/input/events.jsonl")
output_path = Path("/app/output/summary.json")

events = [json.loads(line) for line in input_path.read_text().splitlines() if line.strip()]
durations = sorted(event["duration_ms"] for event in events)
counts = Counter(event["kind"] for event in events)

summary = {
    "schema_version": 1,
    "total_events": len(events),
    "counts": {name: counts[name] for name in sorted(counts)},
    "total_duration_ms": sum(durations),
    "p95_duration_ms": durations[math.ceil(0.95 * len(durations)) - 1],
}

output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text(json.dumps(summary, separators=(",", ":")) + "\n")
