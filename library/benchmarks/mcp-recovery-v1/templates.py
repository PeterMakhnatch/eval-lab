"""In-process oracle, NOP, and mutant controls for the recovery CI contract."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from faults import FaultClass, FaultSpec
from runtime import McpServerRuntime


def _export(task_dir: Path, server: McpServerRuntime, completed: bool = True) -> None:
    out = task_dir / "output"
    out.mkdir(parents=True, exist_ok=True)
    if completed:
        server.state.set("canary_key", "verified_value")
        server.state.set("session_status", "completed")
    (out / "benchmark-events.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "event_index": event["event_index"],
                    "event_type": event["event_type"],
                    "payload": event["payload"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for event in server.recorded_events
        ),
        encoding="utf-8",
    )
    (out / "final-state.json").write_text(
        json.dumps({"records": dict(server.state.records), "digest": server.state.digest()}, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def run_oracle_repair(task_dir: Path, agent_workspace: Path, fault_spec: FaultSpec | None = None) -> None:
    specs = [
        fault_spec
        if fault_spec
        else FaultSpec("write_record", FaultClass.PERSISTENT_SIGNATURE_ERROR, persistence=1)
    ]
    evidence = Path(task_dir) / "output" / "benchmark-events.jsonl"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    if evidence.exists():
        evidence.unlink()
    server = McpServerRuntime(
        mode="fault",
        initial_state={"session_status": "initial"},
        fault_specs=specs,
        evidence_file=evidence,
    )
    write = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "write_record", "arguments": {"key": "canary_key", "value": "verified_value"}},
    }
    server.handle_request(write)
    server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "refresh_auth", "arguments": {"scope": "admin_write"}},
        }
    )
    server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "fallback_query", "arguments": {"query": "canary_key"}},
        }
    )
    server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "read_record", "arguments": {"key": "canary_key"}},
        }
    )
    server.handle_request(write)
    _export(Path(task_dir), server, completed=True)


def run_nop_baseline(task_dir: Path, agent_workspace: Path) -> None:
    out = Path(task_dir) / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark-events.jsonl").write_text("", encoding="utf-8")
    (out / "final-state.json").write_text(json.dumps({"records": {}, "digest": ""}), encoding="utf-8")


def run_blind_retry_control(task_dir: Path, agent_workspace: Path, fault_spec: FaultSpec | None = None) -> None:
    specs = [
        fault_spec
        if fault_spec
        else FaultSpec("write_record", FaultClass.PERSISTENT_SIGNATURE_ERROR, persistence=2)
    ]
    evidence = Path(task_dir) / "output" / "benchmark-events.jsonl"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    if evidence.exists():
        evidence.unlink()
    server = McpServerRuntime(
        mode="fault",
        initial_state={"session_status": "initial"},
        fault_specs=specs,
        evidence_file=evidence,
    )
    server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "write_record", "arguments": {"key": "canary_key", "value": "verified_value"}},
        }
    )
    _export(Path(task_dir), server, completed=False)


def run_wrong_repair_mutant(task_dir: Path, agent_workspace: Path) -> None:
    evidence = Path(task_dir) / "output" / "benchmark-events.jsonl"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    if evidence.exists():
        evidence.unlink()
    server = McpServerRuntime(
        mode="fault",
        initial_state={"session_status": "initial"},
        fault_specs=[FaultSpec("write_record", FaultClass.PERSISTENT_SIGNATURE_ERROR, persistence=1)],
        evidence_file=evidence,
    )
    server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "write_record", "arguments": {"key": "wrong_key", "value": "corrupted_val"}},
        }
    )
    server.state.set("session_status", "corrupted")
    _export(Path(task_dir), server, completed=False)


def mutants() -> dict[str, Callable[[Path, Path], None]]:
    return {"blind_retry": run_blind_retry_control, "wrong_repair": run_wrong_repair_mutant}
