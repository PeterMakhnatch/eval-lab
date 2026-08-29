"""Verifier-only in-process controls for every C3 fault and clean twin cell."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from envelope import encrypt_envelope, write_atomic_envelope


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
    payload = {
        "sequence": len(events),
        "initial_digest": _digest({}),
        "final_digest": _digest(records),
        "records": dict(records),
        "events": list(events),
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

    events: list[dict[str, Any]] = []
    records: dict[str, Any] = {}

    if is_clean:
        records["operational-record"] = "verified-payload"
        events.append({"event_index": 0, "event_type": "tool_executed", "tool": "write_record", "outcome": "ok", "fault_injected": False, "written_key": "operational-record", "written_value": "verified-payload"})
        events.append({"event_index": 1, "event_type": "tool_executed", "tool": "read_record", "outcome": "ok", "fault_injected": False, "read_key": "operational-record", "read_value": "verified-payload"})
    else:
        # Injections
        for idx in range(persistence):
            tool = "read_record" if record.get("fault_class") == "silent_wrong_payload" else "write_record"
            outcome = "silent_corruption" if record.get("fault_class") == "silent_wrong_payload" else "error"
            events.append({"event_index": idx, "event_type": "tool_executed", "tool": tool, "outcome": outcome, "fault_injected": True, "fault_id": fault_id})

        # Causal strategy mutation strictly before post-fault target success
        events.append({"event_index": len(events), "event_type": "tool_executed", "tool": "fallback_query", "outcome": "ok", "fault_injected": False})

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
            tool = "read_record" if record.get("fault_class") == "silent_wrong_payload" else "write_record"
            outcome = "silent_corruption" if record.get("fault_class") == "silent_wrong_payload" else "error"
            events.append({"event_index": idx, "event_type": "tool_executed", "tool": tool, "outcome": outcome, "fault_injected": True, "fault_id": fault_id})

        # Post-window write and read without any causal mutation -> auto_clear=True, reward=0.0
        records["operational-record"] = "verified-payload"
        events.append({"event_index": len(events), "event_type": "tool_executed", "tool": "write_record", "outcome": "ok", "fault_injected": False, "written_key": "operational-record", "written_value": "verified-payload"})
        events.append({"event_index": len(events), "event_type": "tool_executed", "tool": "read_record", "outcome": "ok", "fault_injected": False, "read_key": "operational-record", "read_value": "verified-payload"})

    _seal_and_write(task, record, key, events, records)


def run_wrong_repair_mutant(task_dir: Path, agent_workspace: Path) -> None:
    task = Path(task_dir)
    record, key = _load_cell_meta(task)
    fault_id = str(record["fault_id"])

    events: list[dict[str, Any]] = []
    records: dict[str, Any] = {}

    # Ineffective mutation + invalid empty write
    events.append({"event_index": 0, "event_type": "tool_executed", "tool": "refresh_auth", "outcome": "ok", "fault_injected": False})
    events.append({"event_index": 1, "event_type": "tool_executed", "tool": "write_record", "outcome": "invalid_args", "fault_injected": False, "written_key": "", "written_value": ""})

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
    return {"blind_retry": run_blind_retry_control, "wrong_repair": run_wrong_repair_mutant}
