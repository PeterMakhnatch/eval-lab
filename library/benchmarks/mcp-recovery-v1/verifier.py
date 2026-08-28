"""Deterministic verifier for MCP error recovery benchmarks."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from state import compute_digest


def verify_recovery_evidence(
    evidence_path: Path | str,
    final_state_path: Path | str,
    expected_invariants: dict[str, Any],
    expected_clean_digest: str | None = None,
    allow_blind_retry_transient: bool = False,
) -> dict[str, Any]:
    evidence_p = Path(evidence_path)
    final_state_p = Path(final_state_path)

    if not evidence_p.exists() or not final_state_p.exists():
        return {
            "reward": 0.0,
            "success": False,
            "reason": "Missing evidence or final-state artifacts",
            "detection_passed": False,
            "adaptation_passed": False,
            "invariant_passed": False,
        }

    events: list[dict[str, Any]] = []
    with open(evidence_p, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                events.append(json.loads(line.strip()))

    with open(final_state_p, "r", encoding="utf-8") as f:
        final_state_data = json.load(f)

    # 1. Monotonic event indexing check
    for idx, ev in enumerate(events):
        if ev.get("event_index") != idx:
            return {
                "reward": 0.0,
                "success": False,
                "reason": f"Non-monotonic event index at {idx}",
                "detection_passed": False,
                "adaptation_passed": False,
                "invariant_passed": False,
            }

    # 2. Extract fault and tool events
    fault_events = [e for e in events if e.get("event_type") == "fault_injected"]
    tool_events = [e for e in events if e.get("event_type") == "tool_executed"]

    detection_passed = len(fault_events) > 0

    # 3. Strategy mutation vs Blind retry detection
    # If fault was injected, check subsequent tool execution
    adaptation_passed = False
    if fault_events:
        # Check if agent altered arguments or invoked recovery/auth tools
        fault_tool = fault_events[0]["payload"].get("tool")
        fault_class = fault_events[0]["payload"].get("fault_class")

        subsequent_tools = tool_events  # tools executed
        if fault_class == "permission_denied":
            # Must have called refresh_auth or mutated auth before subsequent write
            auth_called = any(e["payload"].get("tool") == "refresh_auth" for e in subsequent_tools)
            adaptation_passed = auth_called
        elif fault_class == "not_found":
            # Must have routed to fallback_query or alternate key
            fallback_called = any(e["payload"].get("tool") == "fallback_query" for e in subsequent_tools)
            adaptation_passed = fallback_called
        elif fault_class in ("timeout", "malformed_output", "silent_wrong_result"):
            # If transient, adaptation might be retry or fallback query; for silent_wrong, must re-verify/query
            adaptation_passed = len(subsequent_tools) > 0

    # 4. Final state invariant check
    actual_records = final_state_data.get("records", {})
    invariant_passed = True
    for k, v in expected_invariants.items():
        if actual_records.get(k) != v:
            invariant_passed = False
            break

    if expected_clean_digest:
        actual_digest = compute_digest(actual_records)
        if actual_digest != expected_clean_digest:
            invariant_passed = False

    # Calculate overall reward (1.0 or 0.0)
    success = invariant_passed and (detection_passed and (adaptation_passed or allow_blind_retry_transient))

    return {
        "reward": 1.0 if success else 0.0,
        "success": success,
        "detection_passed": detection_passed,
        "adaptation_passed": adaptation_passed,
        "invariant_passed": invariant_passed,
        "fault_count": len(fault_events),
        "tool_count": len(tool_events),
    }


def verify_harbor_task(task_dir: Path | str, reward_dir: Path | str | None = None) -> dict[str, Any]:
    task_p = Path(task_dir)
    evidence_file = task_p / "evidence" / "benchmark-events.jsonl"
    final_state_file = task_p / "evidence" / "final-state.json"
    manifest_file = task_p / "task_manifest.json"

    expected_invariants = {"session_status": "completed", "canary_key": "verified_value"}
    if manifest_file.exists():
        with open(manifest_file, "r", encoding="utf-8") as f:
            manifest = json.load(f)
            expected_invariants = manifest.get("expected_invariants", expected_invariants)

    res = verify_recovery_evidence(
        evidence_path=evidence_file,
        final_state_path=final_state_file,
        expected_invariants=expected_invariants,
    )

    if reward_dir:
        rew_p = Path(reward_dir)
        rew_p.mkdir(parents=True, exist_ok=True)
        with open(rew_p / "reward.txt", "w", encoding="utf-8") as f:
            f.write(f"{res['reward']:.1f}\n")
        with open(rew_p / "summary.json", "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2)

    return res
