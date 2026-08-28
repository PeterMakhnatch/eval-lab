"""Oracle solver and solution runner for action-memory-v1."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def solve(task_dir: Path, evidence_dir: Path) -> dict[str, Any]:
    scenario_file = task_dir / "scenario.json"
    scenario = json.loads(scenario_file.read_text(encoding="utf-8"))

    target_entity = scenario["target_entity"]
    target_attribute = scenario["target_attribute"]
    
    # Read chunks in order, tracking latest state for the target entity
    current_value = None
    evidence_events = []
    
    for idx, chunk in enumerate(scenario["chunks"], start=1):
        content = chunk["content"]
        evidence_events.append({
            "event_index": idx,
            "event_type": "read_chunk",
            "payload": {"chunk_id": chunk["chunk_id"], "byte_count": chunk["byte_count"]}
        })
        
        # Match initial binding or override
        if target_entity in content:
            match = re.search(r"'(?P<val>[^']+)'", content)
            if match:
                current_value = match.group("val")

    # Record final mutation
    final_mutation_event = {
        "event_index": len(evidence_events) + 1,
        "event_type": "execute_mutation",
        "payload": {
            "entity_id": target_entity,
            "attribute": target_attribute,
            "bound_value": current_value,
        }
    }
    evidence_events.append(final_mutation_event)

    evidence_dir.mkdir(parents=True, exist_ok=True)
    with (evidence_dir / "benchmark-events.jsonl").open("w", encoding="utf-8") as f:
        for ev in evidence_events:
            f.write(json.dumps(ev, sort_keys=True) + "\n")

    final_state = {
        "status": "executed",
        "target_entity": target_entity,
        "target_attribute": target_attribute,
        "bound_value": current_value,
    }
    with (evidence_dir / "final-state.json").open("w", encoding="utf-8") as f:
        json.dump(final_state, f, indent=2, sort_keys=True)

    return final_state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-dir", type=Path, default=Path("/app/task_state"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("/app/evidence"))
    args = parser.parse_args()

    solve(args.task_dir, args.evidence_dir)
