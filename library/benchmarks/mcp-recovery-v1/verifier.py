"""Deterministic verifier for MCP recovery evidence and state certificates."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _evidence_paths(task_dir: Path) -> tuple[Path, Path]:
    candidates = [
        (Path("/app/output/benchmark-events.jsonl"), Path("/app/output/final-state.json")),
        (task_dir / "output" / "benchmark-events.jsonl", task_dir / "output" / "final-state.json"),
        (task_dir / "evidence" / "benchmark-events.jsonl", task_dir / "evidence" / "final-state.json"),
    ]
    for events, final_state in candidates:
        if events.is_file() or final_state.is_file():
            return events, final_state
    return candidates[1]


def verify_recovery_evidence(
    evidence_path: Path | str,
    final_state_path: Path | str,
    expected_invariants: dict[str, Any] | None = None,
    expected_fault_mode: str | None = None,
    expected_persistence: int = 1,
) -> dict[str, Any]:
    expected_invariants = expected_invariants or {
        "session_status": "completed",
        "canary_key": "verified_value",
    }
    evidence_p = Path(evidence_path)
    final_state_p = Path(final_state_path)
    if not evidence_p.is_file() or not final_state_p.is_file():
        return {
            "reward": 0.0,
            "success": False,
            "detection_passed": False,
            "adaptation_passed": False,
            "invariant_passed": False,
        }
    events = [
        json.loads(line)
        for line in evidence_p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    final_state = json.loads(final_state_p.read_text(encoding="utf-8"))
    monotonic = all(event.get("event_index") == idx for idx, event in enumerate(events))
    records = final_state.get("records", {})
    invariant_passed = all(records.get(key) == value for key, value in expected_invariants.items())
    faults = [event for event in events if event.get("event_type") == "fault_injected"]
    tools = [event for event in events if event.get("event_type") == "tool_executed"]
    fault_mode = expected_fault_mode or (faults[0]["payload"].get("fault_class") if faults else "")
    adaptation = False
    if fault_mode == "persistent_signature_error":
        adaptation = any(item.get("payload", {}).get("tool") == "refresh_auth" for item in tools)
    elif fault_mode == "persistent_schema_mismatch":
        adaptation = any(item.get("payload", {}).get("tool") == "fallback_query" for item in tools)
    elif fault_mode == "silent_wrong_payload":
        adaptation = any(item.get("payload", {}).get("tool") == "read_record" for item in tools)
    else:
        adaptation = len(faults) >= expected_persistence and len(tools) >= 1
    success = bool(
        monotonic and invariant_passed and adaptation and len(faults) >= expected_persistence
    )
    return {
        "reward": 1.0 if success else 0.0,
        "success": success,
        "detection_passed": len(faults) > 0,
        "adaptation_passed": adaptation,
        "invariant_passed": invariant_passed,
        "fault_count": len(faults),
        "tool_count": len(tools),
    }


def verify_harbor_task(task_dir: Path | str, reward_dir: Path | str | None = None) -> dict[str, Any]:
    task_p = Path(task_dir)
    events_path, final_state_path = _evidence_paths(task_p)
    result = verify_recovery_evidence(events_path, final_state_path)
    if reward_dir:
        reward_p = Path(reward_dir)
        reward_p.mkdir(parents=True, exist_ok=True)
        (reward_p / "reward.txt").write_text(f"{result['reward']:.1f}\n", encoding="utf-8")
        (reward_p / "reward.json").write_text(
            json.dumps({"reward": result["reward"], "passed": float(result["success"])}, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    return result
