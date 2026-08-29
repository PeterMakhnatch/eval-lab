"""Verifier-only scoring for opaque MCP recovery evidence."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# This accepted set is verifier-only. The public task asks agents to diagnose
# and change strategy; it never publishes a fault-to-repair lookup table.
MUTATION_TOOLS = frozenset({"refresh_auth", "fallback_query"})


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


def score_recovery_events(
    events: list[dict[str, Any]],
    certificate: dict[str, Any],
    expected_fault_id: str,
    expected_persistence: int,
) -> dict[str, Any]:
    monotonic = all(event.get("event_index") == index for index, event in enumerate(events))
    injections = [event for event in events if event.get("fault_id") == expected_fault_id]
    first_injection = min((int(event["event_index"]) for event in injections), default=len(events))
    strategy_mutation = any(
        event.get("tool") in MUTATION_TOOLS and int(event.get("event_index", -1)) > first_injection
        for event in events
    )
    writes = [event for event in events if event.get("tool") == "write_record" and event.get("outcome") == "ok"]
    reads = [event for event in events if event.get("tool") == "read_record" and event.get("outcome") == "ok"]
    state_changed = certificate.get("initial_digest") != certificate.get("final_digest")
    exact_injection_count = len(injections) == expected_persistence
    auto_clear = bool(state_changed and exact_injection_count and not strategy_mutation)
    success = bool(
        monotonic
        and exact_injection_count
        and strategy_mutation
        and writes
        and reads
        and state_changed
    )
    return {
        "reward": 1.0 if success else 0.0,
        "success": success,
        "detection_passed": bool(injections),
        "adaptation_passed": strategy_mutation,
        "state_changed": state_changed,
        "auto_clear": auto_clear,
        "fault_count": len(injections),
        "required_fault_count": expected_persistence,
        "monotonic": monotonic,
    }


def verify_recovery_evidence(
    evidence_path: Path | str,
    final_state_path: Path | str,
    *,
    expected_fault_id: str,
    expected_persistence: int,
) -> dict[str, Any]:
    events_path = Path(evidence_path)
    certificate_path = Path(final_state_path)
    if not events_path.is_file() or not certificate_path.is_file():
        return {
            "reward": 0.0,
            "success": False,
            "detection_passed": False,
            "adaptation_passed": False,
            "state_changed": False,
            "auto_clear": False,
            "fault_count": 0,
            "required_fault_count": expected_persistence,
            "monotonic": False,
        }
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
    return score_recovery_events(events, certificate, expected_fault_id, expected_persistence)


def verify_harbor_task(task_dir: Path | str, reward_dir: Path | str | None = None) -> dict[str, Any]:
    task_path = Path(task_dir)
    record_path = task_path / "tests" / "fixtures" / "fault_record.json"
    if not record_path.is_file():
        raise ValueError("verifier-only fault record is missing")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    payload = record.get("injection_payload") or {}
    expected_persistence = int(payload["persistence"])
    events_path, certificate_path = _evidence_paths(task_path)
    result = verify_recovery_evidence(
        events_path,
        certificate_path,
        expected_fault_id=str(record["fault_id"]),
        expected_persistence=expected_persistence,
    )
    if reward_dir:
        reward_path = Path(reward_dir)
        reward_path.mkdir(parents=True, exist_ok=True)
        (reward_path / "reward.txt").write_text(f"{result['reward']:.1f}\n", encoding="utf-8")
        (reward_path / "reward.json").write_text(
            json.dumps({"reward": result["reward"], "passed": float(result["success"])}, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    return result
