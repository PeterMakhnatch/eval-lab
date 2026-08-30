"""Verifier-only in-process controls for every C3 fault and clean twin cell."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from contract import (
    get_alternative_repair,
    get_designated_repair,
)
from envelope import compute_mutation_digest, encrypt_envelope, write_atomic_envelope


def _load_cell_meta(task_dir: Path) -> tuple[dict[str, Any], bytes]:
    fixtures = task_dir / "tests" / "fixtures"
    record = json.loads((fixtures / "fault_record.json").read_text(encoding="utf-8"))
    key = bytes.fromhex((fixtures / "secret_key.txt").read_text(encoding="utf-8").strip())
    return record, key


def _digest(val: Any) -> str:
    return hashlib.sha256(json.dumps(val, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _seal_and_write(task_dir: Path, record: dict[str, Any], key: bytes, events: list[dict[str, Any]], records: dict[str, Any]) -> None:
    out = task_dir / "output"
    out.mkdir(parents=True, exist_ok=True)
    payload_cfg = record.get("injection_payload") or {}
    persistence = int(payload_cfg.get("persistence", 1))
    is_clean = bool(payload_cfg.get("is_clean_twin", False))
    seed = int(payload_cfg.get("seed", 42))
    twin_task_id = str(record.get("twin_task_id", ""))
    fault_class = str(record.get("fault_class", "persistent_signature_error"))
    designated_repair = payload_cfg.get("designated_repair_move") or get_designated_repair(fault_class)

    # Determine executed designated mutation tool
    first_fault = next((int(ev["event_index"]) for ev in events if ev.get("fault_injected")), -1)
    designated_mut = next(
        (
            ev["tool"] for ev in events
            if ev.get("tool") == designated_repair
            and not ev.get("fault_injected")
            and (first_fault < 0 or int(ev.get("event_index", 0)) > first_fault)
        ),
        None,
    )
    mut_tool = None if is_clean else designated_mut

    mutation_digest = compute_mutation_digest(
        fault_class=fault_class,
        persistence=persistence,
        seed=seed,
        is_clean_twin=is_clean,
        twin_task_id=twin_task_id,
        mutation_tool=mut_tool,
    )

    payload = {
        "sequence": len(events),
        "initial_digest": _digest({}),
        "final_digest": _digest(records),
        "records": dict(records),
        "events": list(events),
        "fault_class": fault_class,
        "persistence": persistence,
        "seed": seed,
        "is_clean_twin": is_clean,
        "twin_task_id": twin_task_id,
        "mutation_digest": mutation_digest,
        "designated_repair": designated_repair,
    }
    env = encrypt_envelope(
        key,
        payload,
        task_id=str(record["task_id"]),
        fault_id=str(record["fault_id"]),
        persistence=persistence,
        sequence=len(events),
    )
    write_atomic_envelope(out / "sealed-evidence.json", env)
    canonical = []
    for ordinal, event in enumerate(events, start=1):
        is_error = event.get("outcome") not in {"ok", "silent_corruption"}
        record_ev = {
            "schema_version": "mcp-tool-event-v1",
            "event_ordinal": ordinal,
            "tool_name": event.get("tool"),
            "arguments": {},
            "event_type": "tool_call_error" if is_error else "tool_call_success",
            "is_error": is_error,
        }
        if is_error:
            record_ev["error"] = {"type": "tool_error", "message": str(event.get("outcome"))}
        else:
            record_ev["result"] = {"value": event.get("read_value") or event.get("written_value")}
        canonical.append(record_ev)
    (out / "benchmark-events.jsonl").write_text(
        "".join(json.dumps(ev, sort_keys=True, separators=(",", ":")) + "\n" for ev in canonical),
        encoding="utf-8",
    )


def run_oracle_repair(task_dir: Path, agent_workspace: Path) -> None:
    task = Path(task_dir)
    record, key = _load_cell_meta(task)
    payload_cfg = record.get("injection_payload") or {}
    persistence = int(payload_cfg.get("persistence", 1))
    is_clean = bool(payload_cfg.get("is_clean_twin", False))
    fault_id = str(record["fault_id"])
    fault_class = str(record.get("fault_class", "persistent_signature_error"))
    designated_repair = payload_cfg.get("designated_repair_move") or get_designated_repair(fault_class)

    events: list[dict[str, Any]] = []
    records: dict[str, Any] = {}

    if is_clean:
        records["operational-record"] = "verified-payload"
        events.append({"event_index": 0, "event_type": "tool_executed", "tool": "write_record", "outcome": "ok", "fault_injected": False, "written_key": "operational-record", "written_value": "verified-payload"})
        events.append({"event_index": 1, "event_type": "tool_executed", "tool": "read_record", "outcome": "ok", "fault_injected": False, "read_key": "operational-record", "read_value": "verified-payload"})
    else:
        # Injections
        for idx in range(persistence):
            tool = "read_record" if fault_class == "silent_wrong_payload" else "write_record"
            outcome = "silent_corruption" if fault_class == "silent_wrong_payload" else "error"
            events.append({"event_index": idx, "event_type": "tool_executed", "tool": tool, "outcome": outcome, "fault_injected": True, "fault_id": fault_id})

        # Exact designated causal strategy mutation strictly before post-fault target success
        events.append({
            "event_index": len(events),
            "event_type": "tool_executed",
            "tool": designated_repair,
            "outcome": "ok",
            "fault_injected": False,
            "is_designated_repair": True,
        })

        # Post-mutation recovery write and matching read confirmation
        records["operational-record"] = "verified-payload"
        events.append({"event_index": len(events), "event_type": "tool_executed", "tool": "write_record", "outcome": "ok", "fault_injected": False, "written_key": "operational-record", "written_value": "verified-payload"})
        events.append({"event_index": len(events), "event_type": "tool_executed", "tool": "read_record", "outcome": "ok", "fault_injected": False, "read_key": "operational-record", "read_value": "verified-payload"})

    _seal_and_write(task, record, key, events, records)


def run_nop_baseline(task_dir: Path, agent_workspace: Path) -> None:
    task = Path(task_dir)
    out = task / "output"
    out.mkdir(parents=True, exist_ok=True)
    env_file = out / "sealed-evidence.json"
    if env_file.exists():
        env_file.unlink()


def run_blind_retry_control(task_dir: Path, agent_workspace: Path) -> None:
    """Fixed-policy blind retry: identical operations executed in both fault and clean twin arms."""
    task = Path(task_dir)
    record, key = _load_cell_meta(task)
    payload_cfg = record.get("injection_payload") or {}
    persistence = int(payload_cfg.get("persistence", 1))
    is_clean = bool(payload_cfg.get("is_clean_twin", False))
    fault_id = str(record["fault_id"])
    fault_class = str(record.get("fault_class", "persistent_signature_error"))

    events: list[dict[str, Any]] = []
    records: dict[str, Any] = {}

    if is_clean:
        # On clean twin, identical fixed policy succeeds immediately with zero faults (baseline)
        records["operational-record"] = "verified-payload"
        events.append({"event_index": 0, "event_type": "tool_executed", "tool": "write_record", "outcome": "ok", "fault_injected": False, "written_key": "operational-record", "written_value": "verified-payload"})
        events.append({"event_index": 1, "event_type": "tool_executed", "tool": "read_record", "outcome": "ok", "fault_injected": False, "read_key": "operational-record", "read_value": "verified-payload"})
    else:
        # On fault cell, blind retry without mutation: injections happen, writes continue with no mutation
        for idx in range(persistence):
            tool = "read_record" if fault_class == "silent_wrong_payload" else "write_record"
            outcome = "silent_corruption" if fault_class == "silent_wrong_payload" else "error"
            events.append({"event_index": idx, "event_type": "tool_executed", "tool": tool, "outcome": outcome, "fault_injected": True, "fault_id": fault_id})

        # Post-window write and read without any causal mutation -> auto_clear=True, reward=0.0
        records["operational-record"] = "verified-payload"
        events.append({"event_index": len(events), "event_type": "tool_executed", "tool": "write_record", "outcome": "ok", "fault_injected": False, "written_key": "operational-record", "written_value": "verified-payload"})
        events.append({"event_index": len(events), "event_type": "tool_executed", "tool": "read_record", "outcome": "ok", "fault_injected": False, "read_key": "operational-record", "read_value": "verified-payload"})

    _seal_and_write(task, record, key, events, records)


def run_wrong_repair_mutant(task_dir: Path, agent_workspace: Path) -> None:
    task = Path(task_dir)
    record, key = _load_cell_meta(task)
    payload_cfg = record.get("injection_payload") or {}
    persistence = int(payload_cfg.get("persistence", 1))
    is_clean = bool(payload_cfg.get("is_clean_twin", False))
    fault_id = str(record["fault_id"])
    fault_class = str(record.get("fault_class", "persistent_signature_error"))
    wrong_tool = get_alternative_repair(fault_class)

    events: list[dict[str, Any]] = []
    records: dict[str, Any] = {}

    if is_clean:
        # Ineffective mutation + invalid empty write on clean arm
        events.append({"event_index": 0, "event_type": "tool_executed", "tool": wrong_tool, "outcome": "ok", "fault_injected": False})
        events.append({"event_index": 1, "event_type": "tool_executed", "tool": "write_record", "outcome": "invalid_args", "fault_injected": False, "written_key": "", "written_value": ""})
    else:
        # Injections followed by WRONG repair tool (non-designated)
        for idx in range(persistence):
            tool = "read_record" if fault_class == "silent_wrong_payload" else "write_record"
            outcome = "silent_corruption" if fault_class == "silent_wrong_payload" else "error"
            events.append({"event_index": idx, "event_type": "tool_executed", "tool": tool, "outcome": outcome, "fault_injected": True, "fault_id": fault_id})

        # Executes wrong repair tool
        events.append({"event_index": len(events), "event_type": "tool_executed", "tool": wrong_tool, "outcome": "ok", "fault_injected": False, "is_designated_repair": False})

        # Writes and reads back but wrong mutation means causal_mutation=False -> reward=0.0
        records["operational-record"] = "verified-payload"
        events.append({"event_index": len(events), "event_type": "tool_executed", "tool": "write_record", "outcome": "ok", "fault_injected": False, "written_key": "operational-record", "written_value": "verified-payload"})
        events.append({"event_index": len(events), "event_type": "tool_executed", "tool": "read_record", "outcome": "ok", "fault_injected": False, "read_key": "operational-record", "read_value": "verified-payload"})

    _seal_and_write(task, record, key, events, records)


def run_unconditional_fallback_mutant(task_dir: Path, agent_workspace: Path) -> None:
    """Unconditional fallback: calls fallback_query before fault injection, omits post-fault diagnosis."""
    task = Path(task_dir)
    record, key = _load_cell_meta(task)
    payload_cfg = record.get("injection_payload") or {}
    persistence = int(payload_cfg.get("persistence", 1))
    is_clean = bool(payload_cfg.get("is_clean_twin", False))
    fault_id = str(record["fault_id"])
    fault_class = str(record.get("fault_class", "persistent_signature_error"))

    events: list[dict[str, Any]] = []
    records: dict[str, Any] = {}

    if is_clean:
        # Calls fallback unconditionally before write on clean arm
        events.append({"event_index": 0, "event_type": "tool_executed", "tool": "fallback_query", "outcome": "ok", "fault_injected": False})
        records["operational-record"] = "verified-payload"
        events.append({"event_index": 1, "event_type": "tool_executed", "tool": "write_record", "outcome": "ok", "fault_injected": False, "written_key": "operational-record", "written_value": "verified-payload"})
        events.append({"event_index": 2, "event_type": "tool_executed", "tool": "read_record", "outcome": "ok", "fault_injected": False, "read_key": "operational-record", "read_value": "verified-payload"})
    else:
        # Pre-probe fallback unconditionally before first fault
        events.append({"event_index": 0, "event_type": "tool_executed", "tool": "fallback_query", "outcome": "ok", "fault_injected": False})

        # Injections occur after the unconditional probe
        for idx in range(persistence):
            tool = "read_record" if fault_class == "silent_wrong_payload" else "write_record"
            outcome = "silent_corruption" if fault_class == "silent_wrong_payload" else "error"
            events.append({"event_index": len(events), "event_type": "tool_executed", "tool": tool, "outcome": outcome, "fault_injected": True, "fault_id": fault_id})

        # Retries write with no causal diagnosis post-fault -> reward=0.0
        records["operational-record"] = "verified-payload"
        events.append({"event_index": len(events), "event_type": "tool_executed", "tool": "write_record", "outcome": "ok", "fault_injected": False, "written_key": "operational-record", "written_value": "verified-payload"})
        events.append({"event_index": len(events), "event_type": "tool_executed", "tool": "read_record", "outcome": "ok", "fault_injected": False, "read_key": "operational-record", "read_value": "verified-payload"})

    _seal_and_write(task, record, key, events, records)


def run_unconfirmed_write_mutant(task_dir: Path, agent_workspace: Path) -> None:
    task = Path(task_dir)
    record, key = _load_cell_meta(task)
    events = [
        {"event_index": 0, "event_type": "tool_executed", "tool": "write_record", "outcome": "ok", "fault_injected": False, "written_key": "operational-record", "written_value": "verified-payload"}
    ]
    records = {"operational-record": "verified-payload"}
    _seal_and_write(task, record, key, events, records)


def mutants(is_clean_twin: bool = False) -> dict[str, Callable[[Path, Path], None]]:
    if is_clean_twin:
        return {"unconfirmed_write": run_unconfirmed_write_mutant, "wrong_repair": run_wrong_repair_mutant}
    return {
        "blind_retry": run_blind_retry_control,
        "wrong_repair": run_wrong_repair_mutant,
        "unconditional_fallback": run_unconditional_fallback_mutant,
    }
