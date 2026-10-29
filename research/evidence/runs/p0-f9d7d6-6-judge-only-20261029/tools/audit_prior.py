"""Count prior runs with unjudged pass-through gate cards, without joining labels."""
import json
from pathlib import Path

path = Path.home() / '.local/state/evallab-prod-live/trace-harness/p0-f9d7d6-3/decomposition-rows.jsonl'
rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
result = {}
for population in ('g150', 'pairs'):
    group = [row for row in rows if row['population'] == population]
    result[population] = {'runs': len(group),
                          'unjudged_gate_runs': sum('grader_tamper' in row['pass_through'] for row in group)}
print(json.dumps(result, sort_keys=True))
