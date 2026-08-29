"""Verifier-only scoring for sealed MCP recovery evidence envelopes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from envelope import decrypt_envelope

MUTATION_TOOLS = frozenset({"refresh_auth", "fallback_query"})


def _evidence_path(task_dir: Path) -> Path:
    candidates = [
        Path("/app/output/sealed-evidence.json"),
        task_dir / "output" / "sealed-evidence.json",
        task_dir / "evidence" / "sealed-evidence.json",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return candidates[1]


def score_recovery_envelope(
    key: bytes,
    raw_envelope: dict[str, Any],
    *,
    task_id: str,
    fault_id: str,
    expected_persistence: int,
    is_clean_twin: bool = False,
) -> dict[str, Any]:
    try:
        payload = decrypt_envelope(
            key,
            raw_envelope,
            task_id=task_id,
            fault_id=fault_id,
            persistence=expected_persistence,
        )
    except Exception as exc:
        return {
            "reward": 0.0,
            "success": False,
            "reason": f"envelope decryption failed: {exc}",
            "auto_clear": False,
            "monotonic": False,
            "fault_count": 0,
        }

    events = payload.get("events", [])
    monotonic = all(ev.get("event_index") == idx for idx, ev in enumerate(events))
    injections = [ev for ev in events if ev.get("fault_injected") is True and ev.get("fault_id") == fault_id]
    exact_injections = len(injections) == expected_persistence

    writes = [ev for ev in events if ev.get("tool") == "write_record" and ev.get("outcome") == "ok"]
    reads = [ev for ev in events if ev.get("tool") == "read_record" and ev.get("outcome") == "ok" and ev.get("read_value")]
    state_changed = payload.get("initial_digest") != payload.get("final_digest") and bool(payload.get("records"))

    if is_clean_twin:
        success = bool(monotonic and exact_injections and writes and reads and state_changed)
        return {
            "reward": 1.0 if success else 0.0,
            "success": success,
            "zero_faults": exact_injections,
            "write_ok": bool(writes),
            "read_ok": bool(reads),
            "state_changed": state_changed,
            "auto_clear": False,
            "monotonic": monotonic,
            "fault_count": len(injections),
        }

    first_fault_idx = min((int(ev["event_index"]) for ev in injections), default=len(events))
    first_recovery_write_idx = min(
        (int(ev["event_index"]) for ev in writes if int(ev.get("event_index", 0)) > first_fault_idx),
        default=len(events),
    )
    causal_mutation = any(
        ev.get("tool") in MUTATION_TOOLS and first_fault_idx < int(ev.get("event_index", -1)) < first_recovery_write_idx
        for ev in events
    )
    auto_clear = bool(state_changed and exact_injections and not causal_mutation)
    success = bool(monotonic and exact_injections and causal_mutation and writes and reads and state_changed)

    return {
        "reward": 1.0 if success else 0.0,
        "success": success,
        "causal_mutation": causal_mutation,
        "exact_injections": exact_injections,
        "write_ok": bool(writes),
        "read_ok": bool(reads),
        "state_changed": state_changed,
        "auto_clear": auto_clear,
        "monotonic": monotonic,
        "fault_count": len(injections),
        "required_fault_count": expected_persistence,
    }


def verify_harbor_task(task_dir: Path | str, reward_dir: Path | str | None = None) -> dict[str, Any]:
    task_path = Path(task_dir)
    fixtures = task_path / "tests" / "fixtures"
    record_path = fixtures / "fault_record.json"
    key_path = fixtures / "secret_key.txt"

    if not record_path.is_file() or not key_path.is_file():
        raise ValueError(f"verifier fixtures missing in {fixtures}")

    record = json.loads(record_path.read_text(encoding="utf-8"))
    key = bytes.fromhex(key_path.read_text(encoding="utf-8").strip())
    payload_cfg = record.get("injection_payload") or {}
    expected_persistence = int(payload_cfg.get("persistence", 1))
    is_clean_twin = bool(payload_cfg.get("is_clean_twin", False))

    envelope_file = _evidence_path(task_path)
    if not envelope_file.is_file():
        result = {
            "reward": 0.0,
            "success": False,
            "reason": f"missing sealed evidence envelope: {envelope_file}",
            "auto_clear": False,
        }
    else:
        try:
            raw_env = json.loads(envelope_file.read_text(encoding="utf-8"))
        except Exception as exc:
            result = {
                "reward": 0.0,
                "success": False,
                "reason": f"envelope JSON parse error: {exc}",
                "auto_clear": False,
            }
        else:
            result = score_recovery_envelope(
                key,
                raw_env,
                task_id=str(record["task_id"]),
                fault_id=str(record["fault_id"]),
                expected_persistence=expected_persistence,
                is_clean_twin=is_clean_twin,
            )

    if reward_dir:
        r_path = Path(reward_dir)
        r_path.mkdir(parents=True, exist_ok=True)
        (r_path / "reward.txt").write_text(f"{result['reward']:.1f}\n", encoding="utf-8")
        (r_path / "reward.json").write_text(
            json.dumps({"reward": result["reward"], "passed": float(result["success"])}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (r_path / "checks.json").write_text(
            json.dumps(result, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return result
