"""Control templates and mutant behaviors for action-memory-v1."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from oracle import solve_direct


def oracle(task_dir: Path, evidence_dir: Path) -> dict[str, Any]:
    return solve_direct(task_dir, evidence_dir)


def nop(task_dir: Path, evidence_dir: Path) -> None:
    pass


def stale_value_mutant(task_dir: Path, evidence_dir: Path) -> dict[str, Any]:
    scenario_file = task_dir / "scenario.json"
    if not scenario_file.exists() and (task_dir / "task_state" / "scenario.json").exists():
        scenario_file = task_dir / "task_state" / "scenario.json"
    scenario = json.loads(scenario_file.read_text(encoding="utf-8"))
    
    target_entity = scenario["target_entity"]
    target_attribute = scenario["target_attribute"]
    stale_val = scenario["initial_value"]

    evidence_dir.mkdir(parents=True, exist_ok=True)
    final_state = {
        "status": "executed",
        "target_entity": target_entity,
        "target_attribute": target_attribute,
        "bound_value": stale_val,
    }
    with (evidence_dir / "final-state.json").open("w", encoding="utf-8") as f:
        json.dump(final_state, f, indent=2, sort_keys=True)
    return final_state


def wrong_entity_mutant(task_dir: Path, evidence_dir: Path) -> dict[str, Any]:
    scenario_file = task_dir / "scenario.json"
    if not scenario_file.exists() and (task_dir / "task_state" / "scenario.json").exists():
        scenario_file = task_dir / "task_state" / "scenario.json"
    scenario = json.loads(scenario_file.read_text(encoding="utf-8"))
    
    wrong_entity = "entity_99999"
    target_attribute = scenario["target_attribute"]
    latest_val = scenario["latest_value"]

    evidence_dir.mkdir(parents=True, exist_ok=True)
    final_state = {
        "status": "executed",
        "target_entity": wrong_entity,
        "target_attribute": target_attribute,
        "bound_value": latest_val,
    }
    with (evidence_dir / "final-state.json").open("w", encoding="utf-8") as f:
        json.dump(final_state, f, indent=2, sort_keys=True)
    return final_state


def recall_only_mutant(task_dir: Path, evidence_dir: Path) -> dict[str, Any]:
    scenario_file = task_dir / "scenario.json"
    if not scenario_file.exists() and (task_dir / "task_state" / "scenario.json").exists():
        scenario_file = task_dir / "task_state" / "scenario.json"
    scenario = json.loads(scenario_file.read_text(encoding="utf-8"))

    evidence_dir.mkdir(parents=True, exist_ok=True)
    with (evidence_dir / "benchmark-events.jsonl").open("w", encoding="utf-8") as f:
        for idx, chunk in enumerate(scenario["chunks"], start=1):
            f.write(json.dumps({
                "event_index": idx,
                "event_type": "read_chunk",
                "payload": {"chunk_id": chunk["chunk_id"]}
            }) + "\n")
    return {}


def mutants() -> dict[str, Callable[[Path, Path], Any]]:
    return {
        "stale_value_mutant": stale_value_mutant,
        "wrong_entity_mutant": wrong_entity_mutant,
        "recall_only_mutant": recall_only_mutant,
    }
