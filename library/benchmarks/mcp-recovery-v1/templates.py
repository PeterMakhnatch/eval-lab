"""Oracle solutions, NOP baselines, blind retry, and mutant scripts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from faults import FaultClass, FaultSpec
from runtime import McpServerRuntime
from state import DatabaseState


def run_oracle_repair(
    task_dir: Path,
    agent_workspace: Path,
    fault_spec: FaultSpec | None = None,
) -> None:
    """Oracle agent that detects errors and takes correct adaptive actions."""
    evidence_dir = task_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_file = evidence_dir / "benchmark-events.jsonl"
    final_state_file = evidence_dir / "final-state.json"

    if evidence_file.exists():
        evidence_file.unlink()

    # Initial server runtime with fault spec
    specs = [fault_spec] if fault_spec else [FaultSpec("write_record", FaultClass.PERMISSION_DENIED, persistence=1)]
    server = McpServerRuntime(mode="fault", initial_state={"session_status": "initial"}, fault_specs=specs, evidence_file=evidence_file)

    # 1. First attempt: write_record (will trigger fault)
    req1 = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "write_record", "arguments": {"key": "canary_key", "value": "verified_value"}}}
    res1 = server.handle_request(req1)

    # 2. Adaptation logic based on fault
    fault_cls = specs[0].fault_class
    if fault_cls == FaultClass.PERMISSION_DENIED:
        # Step 2a: Call refresh_auth
        req_auth = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "refresh_auth", "arguments": {"scope": "admin_write"}}}
        server.handle_request(req_auth)
        # Step 2b: Retry write_record
        req_retry = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "write_record", "arguments": {"key": "canary_key", "value": "verified_value"}}}
        server.handle_request(req_retry)
    elif fault_cls == FaultClass.NOT_FOUND:
        # Step 2: Fallback query
        req_fb = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "fallback_query", "arguments": {"query": "canary_key"}}}
        server.handle_request(req_fb)
        # Finalize write
        req_final = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "write_record", "arguments": {"key": "canary_key", "value": "verified_value"}}}
        server.handle_request(req_final)
    elif fault_cls in (FaultClass.TIMEOUT, FaultClass.MALFORMED_OUTPUT, FaultClass.SILENT_WRONG_RESULT):
        # Retry write_record (transient fault will now clear if persistence=1)
        req_retry = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "write_record", "arguments": {"key": "canary_key", "value": "verified_value"}}}
        server.handle_request(req_retry)

    # Set final session status invariant
    server.state.set("session_status", "completed")

    # Export final state
    with open(final_state_file, "w", encoding="utf-8") as f:
        json.dump({"records": server.state.records, "digest": server.state.digest()}, f, indent=2)


def run_nop_baseline(task_dir: Path, agent_workspace: Path) -> None:
    """NOP agent that does nothing."""
    evidence_dir = task_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    with open(evidence_dir / "benchmark-events.jsonl", "w", encoding="utf-8") as f:
        pass
    with open(evidence_dir / "final-state.json", "w", encoding="utf-8") as f:
        json.dump({"records": {}, "digest": ""}, f)


def run_blind_retry_control(
    task_dir: Path,
    agent_workspace: Path,
    fault_spec: FaultSpec | None = None,
) -> None:
    """Blind retry agent that repeatedly attempts identical failed calls without adapting."""
    evidence_dir = task_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_file = evidence_dir / "benchmark-events.jsonl"
    final_state_file = evidence_dir / "final-state.json"

    if evidence_file.exists():
        evidence_file.unlink()

    # Fault with persistence=2 (blind retry 1 time won't clear, or permission denied which requires auth)
    specs = [fault_spec] if fault_spec else [FaultSpec("write_record", FaultClass.PERMISSION_DENIED, persistence=2)]
    server = McpServerRuntime(mode="fault", initial_state={"session_status": "initial"}, fault_specs=specs, evidence_file=evidence_file)

    # Repeated identical calls without auth/mutation
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "write_record", "arguments": {"key": "canary_key", "value": "verified_value"}}}
    server.handle_request(req)
    req["id"] = 2
    server.handle_request(req)

    # Export final state (invariants NOT restored)
    with open(final_state_file, "w", encoding="utf-8") as f:
        json.dump({"records": server.state.records, "digest": server.state.digest()}, f, indent=2)


def run_wrong_repair_mutant(
    task_dir: Path,
    agent_workspace: Path,
) -> None:
    """Mutant agent that performs incorrect mutation and leaves corrupted invariant state."""
    evidence_dir = task_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_file = evidence_dir / "benchmark-events.jsonl"
    final_state_file = evidence_dir / "final-state.json"

    if evidence_file.exists():
        evidence_file.unlink()

    specs = [FaultSpec("write_record", FaultClass.PERMISSION_DENIED, persistence=1)]
    server = McpServerRuntime(mode="fault", initial_state={"session_status": "initial"}, fault_specs=specs, evidence_file=evidence_file)

    req1 = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "write_record", "arguments": {"key": "canary_key", "value": "wrong_value"}}}
    server.handle_request(req1)
    req2 = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "refresh_auth", "arguments": {"scope": "wrong_scope"}}}
    server.handle_request(req2)
    # Fails to write canary key with verified_value, sets corrupt session status
    server.state.set("session_status", "corrupted")

    with open(final_state_file, "w", encoding="utf-8") as f:
        json.dump({"records": server.state.records, "digest": server.state.digest()}, f, indent=2)


def mutants() -> dict[str, Callable[[Path, Path], None]]:
    return {
        "blind_retry": run_blind_retry_control,
        "wrong_repair": run_wrong_repair_mutant,
    }
