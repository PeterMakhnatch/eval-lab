"""Verifier-only in-process controls for every C3 class/persistence cell."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from evallab.benchmark_program_contracts import FaultClass

from faults import FaultSpec
from runtime import McpServerRuntime


def _fault_meta(task_dir: Path) -> tuple[FaultClass, int, str]:
    record = json.loads((task_dir / "tests" / "fixtures" / "fault_record.json").read_text(encoding="utf-8"))
    return FaultClass(record["fault_class"]), int(record["injection_payload"]["persistence"]), str(record["fault_id"])


def _spec(task_dir: Path) -> FaultSpec:
    fault, persistence, _ = _fault_meta(task_dir)
    target = "read_record" if fault == FaultClass.SILENT_WRONG_PAYLOAD else "write_record"
    return FaultSpec(
        target,
        fault,
        persistence=persistence,
        clear_condition=lambda _args, state: bool(state) and state.get("__fallback_synced__") is True,
    )


def _call(server: McpServerRuntime, tool: str, arguments: dict[str, str]) -> None:
    server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )


def _export(task_dir: Path, server: McpServerRuntime) -> None:
    _, _, fault_id = _fault_meta(task_dir)
    out = task_dir / "output"
    out.mkdir(parents=True, exist_ok=True)
    public_events = []
    for event in server.recorded_events:
        payload = event.get("payload") or {}
        if event["event_type"] == "fault_injected":
            public_events.append(
                {"event_index": event["event_index"], "event_type": "tool_result", "tool": payload.get("tool"), "outcome": "error", "fault_id": fault_id}
            )
        elif event["event_type"] == "tool_executed":
            public_events.append(
                {"event_index": event["event_index"], "event_type": "tool_result", "tool": payload.get("tool"), "outcome": "ok"}
            )
    (out / "benchmark-events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n" for event in public_events),
        encoding="utf-8",
    )
    (out / "final-state.json").write_text(
        json.dumps(
            {
                "initial_digest": server.initial_digest,
                "final_digest": server.state.digest(),
                "event_count": len(public_events),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def run_oracle_repair(task_dir: Path, agent_workspace: Path, fault_spec: FaultSpec | None = None) -> None:
    task = Path(task_dir)
    spec = fault_spec or _spec(task)
    server = McpServerRuntime(mode="fault", initial_state={}, fault_specs=[spec])
    write = {"key": "control-record", "value": "control-value"}
    if spec.target_tool == "read_record":
        _call(server, "write_record", write)
        for _ in range(spec.persistence):
            _call(server, "read_record", {"key": "control-record"})
        _call(server, "fallback_query", {"query": "control-record"})
        _call(server, "read_record", {"key": "control-record"})
    else:
        for _ in range(spec.persistence):
            _call(server, "write_record", write)
        _call(server, "fallback_query", {"query": "control-record"})
        _call(server, "write_record", write)
        _call(server, "read_record", {"key": "control-record"})
    _export(task, server)


def run_nop_baseline(task_dir: Path, agent_workspace: Path) -> None:
    task = Path(task_dir)
    out = task / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark-events.jsonl").write_text("", encoding="utf-8")
    (out / "final-state.json").write_text(
        json.dumps({"initial_digest": "", "final_digest": "", "event_count": 0}) + "\n",
        encoding="utf-8",
    )


def run_blind_retry_control(task_dir: Path, agent_workspace: Path, fault_spec: FaultSpec | None = None) -> None:
    task = Path(task_dir)
    spec = fault_spec or _spec(task)
    server = McpServerRuntime(mode="fault", initial_state={}, fault_specs=[spec])
    write = {"key": "control-record", "value": "control-value"}
    if spec.target_tool == "read_record":
        _call(server, "write_record", write)
        for _ in range(spec.persistence + 1):
            _call(server, "read_record", {"key": "control-record"})
    else:
        for _ in range(spec.persistence + 1):
            _call(server, "write_record", write)
        _call(server, "read_record", {"key": "control-record"})
    _export(task, server)


def run_wrong_repair_mutant(task_dir: Path, agent_workspace: Path) -> None:
    task = Path(task_dir)
    spec = _spec(task)
    server = McpServerRuntime(mode="fault", initial_state={}, fault_specs=[spec])
    _call(server, "refresh_auth", {"scope": "retry"})
    _call(server, "write_record", {"key": "", "value": ""})
    _export(task, server)


def mutants() -> dict[str, Callable[[Path, Path], None]]:
    return {"blind_retry": run_blind_retry_control, "wrong_repair": run_wrong_repair_mutant}
