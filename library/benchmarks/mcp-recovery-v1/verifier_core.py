"""Verifier-only scoring for sealed MCP recovery evidence envelopes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from contract import (
    DESIGNATED_REPAIR_MOVES,
    get_designated_repair,
    resolve_fault_class,
)
from envelope import compute_mutation_digest, decrypt_envelope

MUTATION_TOOLS = frozenset({"refresh_auth", "fallback_query"})

CANONICAL_EVENT_SCHEMA = "mcp-tool-event-v1"
CANONICAL_TOOLS = frozenset({"write_record", "read_record", "refresh_auth", "fallback_query"})


def _load_canonical_events(task_path: Path) -> list[dict[str, Any]]:
    event_path = task_path / "output" / "benchmark-events.jsonl"
    if not event_path.is_file():
        raise ValueError("missing canonical mcp-tool-event-v1 artifact")
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for ordinal, event in enumerate(events, start=1):
        if event.get("schema_version") != CANONICAL_EVENT_SCHEMA or event.get("event_ordinal") != ordinal:
            raise ValueError("canonical event schema or ordinal mismatch")
        tool = event.get("tool_name")
        if tool not in CANONICAL_TOOLS or not isinstance(event.get("arguments"), dict):
            raise ValueError("canonical event has invalid tool alphabet or arguments")
        typ = event.get("event_type")
        if typ == "tool_call_success":
            if event.get("is_error") is not False or "result" not in event or "error" in event:
                raise ValueError("canonical success event is malformed")
        elif typ == "tool_call_error":
            if event.get("is_error") is not True or not isinstance(event.get("error"), dict) or "result" in event:
                raise ValueError("canonical error event is malformed")
        else:
            raise ValueError("canonical event type is invalid")
    return events


def _crosscheck_transport(sealed: list[dict[str, Any]], canonical: list[dict[str, Any]]) -> None:
    if len(sealed) != len(canonical):
        raise ValueError("sealed and canonical event count mismatch")
    for handler, transport in zip(sealed, canonical, strict=True):
        if handler.get("tool") != transport.get("tool_name"):
            raise ValueError("sealed/canonical tool order mismatch")
        handler_error = handler.get("outcome") not in {"ok"}
        if bool(transport.get("is_error")) != handler_error and handler.get("outcome") != "silent_corruption":
            raise ValueError("sealed/canonical outcome mismatch")


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


def _base_failure_result(
    reason: str,
    *,
    expected_persistence: int = 1,
    is_clean_twin: bool = False,
    designated_repair: str = "none",
) -> dict[str, Any]:
    return {
        "reward": 0.0,
        "success": False,
        "reason": reason,
        "causal_mutation": False,
        "designated_repair_matched": False,
        "designated_repair": designated_repair,
        "mutation_digest_verified": False,
        "exact_injections": False,
        "write_ok": False,
        "read_ok": False,
        "read_confirmed": False,
        "state_changed": False,
        "auto_clear": False,
        "monotonic": False,
        "zero_faults": False,
        "fault_count": 0,
        "required_fault_count": expected_persistence,
        "is_clean_twin": is_clean_twin,
    }


def score_recovery_envelope(
    key: bytes,
    raw_envelope: dict[str, Any],
    *,
    task_id: str,
    fault_id: str,
    expected_persistence: int,
    is_clean_twin: bool = False,
    target_tool: str = "write_record",
    fault_class: str | None = None,
    seed: int | None = None,
    twin_task_id: str | None = None,
    designated_repair_move: str | None = None,
    expected_mutation_digest: str | None = None,
) -> dict[str, Any]:
    # Resolve designated repair move
    resolved_repair = designated_repair_move
    if not resolved_repair and fault_class:
        try:
            resolved_repair = get_designated_repair(fault_class)
        except Exception:
            resolved_repair = None

    try:
        payload = decrypt_envelope(
            key,
            raw_envelope,
            task_id=task_id,
            fault_id=fault_id,
            persistence=expected_persistence,
        )
    except Exception as exc:
        return _base_failure_result(
            f"envelope decryption failed: {exc}",
            expected_persistence=expected_persistence,
            is_clean_twin=is_clean_twin,
            designated_repair=resolved_repair or "none",
        )

    events = payload.get("events", [])
    monotonic = all(ev.get("event_index") == idx for idx, ev in enumerate(events))
    injections = [ev for ev in events if ev.get("fault_injected") is True and ev.get("fault_id") == fault_id]
    exact_injections = len(injections) == expected_persistence

    state_changed = payload.get("initial_digest") != payload.get("final_digest") and bool(payload.get("records"))

    # Verify sealed mutation digest if present
    sealed_digest = payload.get("mutation_digest")
    mutation_digest_verified = True
    if sealed_digest and fault_class and seed is not None and twin_task_id:
        if is_clean_twin:
            expected_dig = compute_mutation_digest(
                fault_class=fault_class,
                persistence=0,
                seed=seed,
                is_clean_twin=True,
                twin_task_id=twin_task_id,
                mutation_tool=None,
            )
            mutation_digest_verified = (sealed_digest == expected_dig)
        elif resolved_repair:
            expected_dig = compute_mutation_digest(
                fault_class=fault_class,
                persistence=expected_persistence,
                seed=seed,
                is_clean_twin=False,
                twin_task_id=twin_task_id,
                mutation_tool=resolved_repair,
            )
            mutation_digest_verified = (sealed_digest == expected_dig)

    all_reads = [ev for ev in events if ev.get("tool") == "read_record" and ev.get("outcome") == "ok" and ev.get("read_value")]
    all_writes = [ev for ev in events if ev.get("tool") == "write_record" and ev.get("outcome") == "ok"]

    if is_clean_twin:
        writes = all_writes
        reads = all_reads
        confirmed = any(
            w.get("written_value") and r.get("read_value") == w.get("written_value") and int(r.get("event_index", 0)) > int(w.get("event_index", 0))
            for w in writes for r in reads
        ) if writes and reads else False
        success = bool(monotonic and exact_injections and writes and reads and confirmed and state_changed and mutation_digest_verified)
        return {
            "reward": 1.0 if success else 0.0,
            "success": success,
            "zero_faults": exact_injections,
            "write_ok": bool(writes),
            "read_ok": bool(reads),
            "read_confirmed": confirmed,
            "state_changed": state_changed,
            "auto_clear": False,
            "monotonic": monotonic,
            "fault_count": len(injections),
            "required_fault_count": expected_persistence,
            "mutation_digest_verified": mutation_digest_verified,
            "causal_mutation": False,
            "designated_repair_matched": True,
            "designated_repair": resolved_repair or "none",
            "is_clean_twin": True,
        }

    if not injections:
        res = _base_failure_result(
            "no fault injections observed",
            expected_persistence=expected_persistence,
            is_clean_twin=False,
            designated_repair=resolved_repair or "none",
        )
        res["write_ok"] = bool(all_writes)
        res["read_ok"] = bool(all_reads)
        res["monotonic"] = monotonic
        return res

    first_fault_idx = min(int(ev["event_index"]) for ev in injections)

    # First non-injected target tool success (write_record or read_record ok) after first fault
    post_fault_target_successes = [
        int(ev["event_index"])
        for ev in events
        if ev.get("tool") in ("write_record", "read_record")
        and ev.get("outcome") == "ok"
        and int(ev.get("event_index", 0)) > first_fault_idx
    ]

    if not post_fault_target_successes:
        res = _base_failure_result(
            "no post-fault target operation succeeded",
            expected_persistence=expected_persistence,
            is_clean_twin=False,
            designated_repair=resolved_repair or "none",
        )
        res["fault_count"] = len(injections)
        res["exact_injections"] = exact_injections
        res["monotonic"] = monotonic
        res["write_ok"] = bool(all_writes)
        res["read_ok"] = bool(all_reads)
        return res

    first_post_fault_success = min(post_fault_target_successes)

    # Exact Designated Causal Mutation:
    # 1. Mutation must occur strictly after first fault and strictly before first post-fault target success
    # 2. If a designated repair move is specified, the mutation MUST be the designated tool.
    target_mutation_tools = frozenset({resolved_repair}) if resolved_repair else MUTATION_TOOLS
    mutations_before_success = [
        int(ev["event_index"])
        for ev in events
        if ev.get("tool") in target_mutation_tools
        and first_fault_idx < int(ev.get("event_index", -1)) < first_post_fault_success
    ]
    causal_mutation = bool(mutations_before_success)
    designated_repair_matched = causal_mutation

    # Check if a non-designated tool was executed in the mutation window (wrong repair check)
    any_mutation_in_window = [
        ev.get("tool")
        for ev in events
        if ev.get("tool") in MUTATION_TOOLS
        and first_fault_idx < int(ev.get("event_index", -1)) < first_post_fault_success
    ]
    wrong_repair_executed = bool(
        resolved_repair and any_mutation_in_window and resolved_repair not in any_mutation_in_window
    )
    if wrong_repair_executed:
        causal_mutation = False
        designated_repair_matched = False

    confirmed_read = False
    post_mutation_writes: list[dict[str, Any]] = []

    if target_tool == "read_record":
        # Silent read-targeted fault: initial write before fault -> corrupt reads -> mutation -> clean read matching initial write
        initial_writes = [ev for ev in events if ev.get("tool") == "write_record" and ev.get("outcome") == "ok" and int(ev.get("event_index", 0)) < first_fault_idx]
        clean_reads = [ev for ev in events if ev.get("tool") == "read_record" and ev.get("outcome") == "ok" and int(ev.get("event_index", 0)) >= first_post_fault_success]
        for w in initial_writes:
            w_val = w.get("written_value") or payload.get("records", {}).get(w.get("written_key", ""))
            if any(r.get("read_value") == w_val or (w_val and str(w_val) in str(r.get("read_value", ""))) for r in clean_reads):
                confirmed_read = True
                break
        if not confirmed_read:
            post_mut_writes = [ev for ev in events if ev.get("tool") == "write_record" and ev.get("outcome") == "ok" and int(ev.get("event_index", 0)) >= first_post_fault_success]
            for w in post_mut_writes:
                w_val = w.get("written_value") or payload.get("records", {}).get(w.get("written_key", ""))
                w_idx = int(w.get("event_index", 0))
                if any((r.get("read_value") == w_val or (w_val and str(w_val) in str(r.get("read_value", "")))) and int(r.get("event_index", 0)) > w_idx for r in clean_reads):
                    confirmed_read = True
                    break
        success = bool(monotonic and exact_injections and causal_mutation and designated_repair_matched and confirmed_read and state_changed and mutation_digest_verified)
    else:
        # Write-targeted faults: fault writes -> designated mutation -> post-mutation successful write -> matching read confirmation
        post_mutation_writes = [
            ev for ev in events
            if ev.get("tool") == "write_record"
            and ev.get("outcome") == "ok"
            and int(ev.get("event_index", 0)) >= first_post_fault_success
        ]
        for write_ev in post_mutation_writes:
            w_idx = int(write_ev.get("event_index", 0))
            written_val = write_ev.get("written_value") or payload.get("records", {}).get(write_ev.get("written_key", ""))
            matching_reads = [
                ev for ev in events
                if ev.get("tool") == "read_record"
                and ev.get("outcome") == "ok"
                and int(ev.get("event_index", 0)) > w_idx
                and (ev.get("read_value") == written_val or (written_val and str(written_val) in str(ev.get("read_value"))))
            ]
            if matching_reads:
                confirmed_read = True
                break
        success = bool(monotonic and exact_injections and causal_mutation and designated_repair_matched and post_mutation_writes and confirmed_read and state_changed and mutation_digest_verified)

    auto_clear = bool(state_changed and exact_injections and not causal_mutation)

    return {
        "reward": 1.0 if success else 0.0,
        "success": success,
        "causal_mutation": causal_mutation,
        "designated_repair_matched": designated_repair_matched,
        "designated_repair": resolved_repair or "any",
        "mutation_digest_verified": mutation_digest_verified,
        "exact_injections": exact_injections,
        "write_ok": bool(post_mutation_writes or all_writes),
        "read_ok": bool(all_reads),
        "read_confirmed": confirmed_read,
        "state_changed": state_changed,
        "auto_clear": auto_clear,
        "monotonic": monotonic,
        "zero_faults": False,
        "fault_count": len(injections),
        "required_fault_count": expected_persistence,
        "is_clean_twin": False,
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
    seed = payload_cfg.get("seed", 42)
    designated_repair = payload_cfg.get("designated_repair_move")
    fault_class_str = record.get("fault_class")

    envelope_file = _evidence_path(task_path)
    if not envelope_file.is_file():
        result = _base_failure_result(
            f"missing sealed evidence envelope: {envelope_file}",
            expected_persistence=expected_persistence,
            is_clean_twin=is_clean_twin,
            designated_repair=designated_repair or "none",
        )
    else:
        try:
            raw_env = json.loads(envelope_file.read_text(encoding="utf-8"))
        except Exception as exc:
            result = _base_failure_result(
                f"envelope JSON parse error: {exc}",
                expected_persistence=expected_persistence,
                is_clean_twin=is_clean_twin,
                designated_repair=designated_repair or "none",
            )
        else:
            try:
                payload = decrypt_envelope(
                    key,
                    raw_env,
                    task_id=str(record["task_id"]),
                    fault_id=str(record["fault_id"]),
                    persistence=expected_persistence,
                )
                _crosscheck_transport(payload.get("events", []), _load_canonical_events(task_path))
            except Exception as exc:
                result = _base_failure_result(
                    f"canonical transport validation failed: {exc}",
                    expected_persistence=expected_persistence,
                    is_clean_twin=is_clean_twin,
                    designated_repair=designated_repair or "none",
                )
            else:
                result = score_recovery_envelope(
                    key,
                    raw_env,
                    task_id=str(record["task_id"]),
                    fault_id=str(record["fault_id"]),
                    expected_persistence=expected_persistence,
                    is_clean_twin=is_clean_twin,
                    target_tool=str(record.get("target_tool", "write_record")),
                    fault_class=fault_class_str,
                    seed=seed,
                    twin_task_id=str(record.get("twin_task_id", "")),
                    designated_repair_move=designated_repair,
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
