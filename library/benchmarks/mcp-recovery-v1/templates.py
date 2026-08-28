"""In-process oracle, NOP, and mutant controls for the recovery CI contract."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from faults import FaultClass, FaultSpec
from runtime import McpServerRuntime
from verifier import MUTATION_BY_FAULT


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


def _call(server: McpServerRuntime, name: str, arguments: dict) -> None:
    server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )


def _spec_or_default(fault_spec: FaultSpec | None) -> FaultSpec:
    if fault_spec is not None:
        return fault_spec
    return FaultSpec(
        "write_record",
        FaultClass.PERSISTENT_SIGNATURE_ERROR,
        persistence=1,
        clear_condition=lambda _args, state: state.get("__auth__") == "admin_write",
    )


def run_oracle_repair(task_dir: Path, agent_workspace: Path, fault_spec: FaultSpec | None = None) -> None:
    spec = _spec_or_default(fault_spec)
    evidence = Path(task_dir) / "output" / "benchmark-events.jsonl"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    if evidence.exists():
        evidence.unlink()
    server = McpServerRuntime(
        mode="fault",
        initial_state={"session_status": "initial"},
        fault_specs=[spec],
        evidence_file=evidence,
    )
    write_args = {"key": "canary_key", "value": "verified_value"}
    target = spec.target_tool
    for _ in range(spec.persistence):
        if target == "read_record":
            _call(server, "read_record", {"key": "canary_key"})
        else:
            _call(server, "write_record", write_args)
    mutation = MUTATION_BY_FAULT[spec.fault_class.value]
    if mutation == "refresh_auth":
        _call(server, "refresh_auth", {"scope": "admin_write"})
    elif mutation == "fallback_query":
        _call(server, "fallback_query", {"query": "canary_key"})
    elif mutation == "read_record":
        _call(server, "read_record", {"key": "canary_key"})
    _call(server, "write_record", write_args)
    _call(server, "read_record", {"key": "canary_key"})
    _export(Path(task_dir), server, completed=True)


def run_nop_baseline(task_dir: Path, agent_workspace: Path) -> None:
    out = Path(task_dir) / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark-events.jsonl").write_text("", encoding="utf-8")
    (out / "final-state.json").write_text(json.dumps({"records": {}, "digest": ""}), encoding="utf-8")


def run_blind_retry_control(task_dir: Path, agent_workspace: Path, fault_spec: FaultSpec | None = None) -> None:
    spec = fault_spec or FaultSpec("write_record", FaultClass.PERSISTENT_SIGNATURE_ERROR, persistence=2)
    evidence = Path(task_dir) / "output" / "benchmark-events.jsonl"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    if evidence.exists():
        evidence.unlink()
    server = McpServerRuntime(
        mode="fault",
        initial_state={"session_status": "initial"},
        fault_specs=[spec],
        evidence_file=evidence,
    )
    write_args = {"key": "canary_key", "value": "verified_value"}
    for _ in range(spec.persistence + 1):
        _call(server, spec.target_tool, write_args if spec.target_tool == "write_record" else {"key": "canary_key"})
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
    _call(server, "write_record", {"key": "wrong_key", "value": "corrupted_val"})
    server.state.set("session_status", "corrupted")
    _export(Path(task_dir), server, completed=False)


def mutants() -> dict[str, Callable[[Path, Path], None]]:
    return {"blind_retry": run_blind_retry_control, "wrong_repair": run_wrong_repair_mutant}
