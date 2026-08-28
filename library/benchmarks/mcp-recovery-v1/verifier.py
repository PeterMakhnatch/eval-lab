"""Deterministic verifier for MCP recovery evidence and state certificates."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MUTATION_BY_FAULT = {
    "persistent_signature_error": "refresh_auth",
    "persistent_schema_mismatch": "fallback_query",
    "transient_network_timeout": "fallback_query",
    "transient_http_5xx": "fallback_query",
    "silent_wrong_payload": "read_record",
}


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


def _tool_name(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("tool") or payload.get("tool_name") or "")


def score_recovery_events(
    events: list[dict[str, Any]],
    records: dict[str, Any],
    expected_fault_mode: str | None,
    expected_persistence: int,
    expected_invariants: dict[str, Any],
) -> dict[str, Any]:
    monotonic = all(event.get("event_index") == idx for idx, event in enumerate(events))
    invariant_passed = all(records.get(key) == value for key, value in expected_invariants.items())
    faults = [event for event in events if event.get("event_type") == "fault_injected"]
    tools = [event for event in events if event.get("event_type") == "tool_executed"]
    fault_mode = expected_fault_mode or (
        (faults[0].get("payload") or {}).get("fault_class") if faults else ""
    )
    required = MUTATION_BY_FAULT.get(str(fault_mode), "")
    mutation_passed = bool(required) and any(_tool_name(item) == required for item in tools)
    fault_ledger_ok = len(faults) >= expected_persistence
    auto_clear = bool(invariant_passed and fault_ledger_ok and not mutation_passed)
    success = bool(monotonic and invariant_passed and mutation_passed and fault_ledger_ok)
    return {
        "reward": 1.0 if success else 0.0,
        "success": success,
        "detection_passed": len(faults) > 0,
        "adaptation_passed": mutation_passed,
        "invariant_passed": invariant_passed,
        "auto_clear": auto_clear,
        "fault_count": len(faults),
        "tool_count": len(tools),
        "fault_mode": fault_mode,
        "required_mutation": required,
        "monotonic": monotonic,
    }


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
            "auto_clear": False,
            "fault_count": 0,
            "tool_count": 0,
        }
    events = [
        json.loads(line)
        for line in evidence_p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    final_state = json.loads(final_state_p.read_text(encoding="utf-8"))
    records = final_state.get("records", {})
    if not isinstance(records, dict):
        records = {}
    return score_recovery_events(
        events,
        records,
        expected_fault_mode,
        expected_persistence,
        expected_invariants,
    )


def verify_harbor_task(task_dir: Path | str, reward_dir: Path | str | None = None) -> dict[str, Any]:
    task_p = Path(task_dir)
    events_path, final_state_path = _evidence_paths(task_p)
    expected_mode = None
    expected_persistence = 1
    fixture = task_p / "tests" / "fixtures" / "fault_record.json"
    if fixture.is_file():
        record = json.loads(fixture.read_text(encoding="utf-8"))
        expected_mode = record.get("fault_class")
        payload = record.get("injection_payload") or {}
        if isinstance(payload, dict) and payload.get("persistence") is not None:
            expected_persistence = int(payload["persistence"])
    result = verify_recovery_evidence(
        events_path,
        final_state_path,
        expected_fault_mode=expected_mode,
        expected_persistence=expected_persistence,
    )
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
