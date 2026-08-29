from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from evallab.benchmark_program_contracts import CellFactorsC, FaultClass
from evallab.task_workbench import CandidateSource, inspect_candidate

ROOT = Path(__file__).resolve().parents[1] / "library" / "benchmarks" / "mcp-recovery-v1"


def load(name: str):
    module_name = f"mcp_recovery_v1_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    sys.path.insert(0, str(ROOT)) if str(ROOT) not in sys.path else None
    spec = importlib.util.spec_from_file_location(module_name, ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _fault_id(fault: FaultClass, persistence: int) -> str:
    contracts = load("contract")
    materializer = load("materializer")
    task_id = "mcp-recovery-" + contracts.compute_sha256(
        {"seed": 42, "fault": fault.value, "dose": persistence}
    )[:16]
    cell = CellFactorsC(fault_class=fault, fault_injection_count=persistence, seed=42)
    return materializer._fault_record(cell, task_id).fault_id


def test_contract_schema():
    contract = load("contract").get_benchmark_contract()
    assert contract["family"] == "mcp-recovery-v1"
    assert contract["synthetic_family"] == "family_c_fault_recovery"
    assert contract["cell_factors"]["persistence_levels"] == [1, 2]
    assert CellFactorsC(fault_class=FaultClass.TRANSIENT_HTTP_5XX, fault_injection_count=2, seed=42)


def test_fault_controller_persistent_clear_and_transient_window():
    faults = load("faults")
    persistent = faults.FaultSpec(
        "write_record",
        FaultClass.PERSISTENT_SIGNATURE_ERROR,
        persistence=2,
        clear_condition=lambda _args, state: bool(state) and state.get("__fallback_synced__") is True,
    )
    ctrl = faults.FaultController([persistent])
    assert ctrl.evaluate("write_record", {}, state={})[0] is True
    assert ctrl.evaluate("write_record", {}, state={})[0] is True
    assert ctrl.evaluate("write_record", {}, state={})[0] is True
    assert ctrl.evaluate("write_record", {}, state={"__fallback_synced__": True})[0] is False
    transient = faults.FaultController(
        [faults.FaultSpec("write_record", FaultClass.TRANSIENT_NETWORK_TIMEOUT, persistence=2)]
    )
    assert [transient.evaluate("write_record", {})[0] for _ in range(3)] == [True, True, False]


def test_fastmcp_iserror_sse_parser():
    client = load("client")
    raw = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":'
        '"operation rejected by the current session"}],"isError":true}}\n\n'
    )
    assert client.classify_tool_response(200, raw)[0] == "error"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_port(port: int, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"FastMCP server did not bind 127.0.0.1:{port}")


def _start_fastmcp(tmp_path: Path, fault: FaultClass, persistence: int) -> tuple[subprocess.Popen, Path, int, str]:
    materializer = load("materializer")
    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    fault_id = _fault_id(fault, persistence)
    server_path = tmp_path / "server.py"
    server_path.write_text(
        materializer.generate_recovery_server_script(fault, persistence, fault_id), encoding="utf-8"
    )
    port = _free_port()
    env = {
        **os.environ,
        "MCP_RECOVERY_OUTPUT": str(out_dir),
        "MCP_RECOVERY_PRIVATE_STATE": str(tmp_path / "private-state.json"),
        "MCP_RECOVERY_HOST": "127.0.0.1",
        "MCP_RECOVERY_PORT": str(port),
    }
    proc = subprocess.Popen([sys.executable, str(server_path)], env=env, cwd=str(tmp_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_port(port)
    except Exception:
        proc.kill()
        raise
    return proc, out_dir, port, fault_id


def _client(port: int):
    client = load("client")
    session = client.McpHttpSession(host="127.0.0.1", port=port)
    assert session.initialize()[0] == 200
    return session


def _run_strategy(port: int, strategy: str) -> None:
    session = _client(port)
    key, value = "agent-record", "agent-value"
    for _ in range(12):
        _status, kind, _raw = session.call_tool("write_record", {"key": key, "value": value})
        if kind != "ok":
            session.call_tool(strategy, {"scope": "retry"} if strategy == "refresh_auth" else {"query": key})
            continue
        _status, _kind, raw = session.call_tool("read_record", {"key": key})
        if value in raw:
            return
        session.call_tool(strategy, {"scope": "reconcile"} if strategy == "refresh_auth" else {"query": key})
    raise AssertionError("strategy did not restore a readable record")


def _run_blind(port: int) -> None:
    session = _client(port)
    for _ in range(8):
        session.call_tool("write_record", {"key": "agent-record", "value": "agent-value"})
        session.call_tool("read_record", {"key": "agent-record"})


def _public_evidence(out_dir: Path) -> tuple[list[dict], dict]:
    events_path = out_dir / "agent-events.jsonl"
    state_path = out_dir / "agent-state.json"
    assert events_path.is_file() and state_path.is_file()
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return events, json.loads(state_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("fault", list(FaultClass))
@pytest.mark.parametrize("persistence", [1, 2])
def test_fastmcp_all_cells_have_exact_opaque_ledger_and_blind_gate(tmp_path, fault, persistence):
    pytest.importorskip("fastmcp")
    verifier = load("verifier")
    proc, out_dir, port, fault_id = _start_fastmcp(tmp_path / "oracle", fault, persistence)
    try:
        _run_strategy(port, "fallback_query")
        events, certificate = _public_evidence(out_dir)
        scored = verifier.score_recovery_events(events, certificate, fault_id, persistence)
        assert scored["reward"] == 1.0, scored
        assert scored["fault_count"] == persistence, scored
        assert scored["auto_clear"] is False
        assert all("fault_class" not in event and "hit" not in event for event in events)
    finally:
        proc.kill()
        proc.wait(timeout=5)

    proc, out_dir, port, fault_id = _start_fastmcp(tmp_path / "blind", fault, persistence)
    try:
        _run_blind(port)
        events, certificate = _public_evidence(out_dir)
        scored = verifier.score_recovery_events(events, certificate, fault_id, persistence)
        assert scored["reward"] == 0.0, scored
        if fault in {FaultClass.TRANSIENT_HTTP_5XX, FaultClass.TRANSIENT_NETWORK_TIMEOUT}:
            assert scored["auto_clear"] is True, scored
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_multiple_repair_strategies_pass(tmp_path):
    pytest.importorskip("fastmcp")
    verifier = load("verifier")
    for strategy in ("refresh_auth", "fallback_query"):
        proc, out_dir, port, fault_id = _start_fastmcp(tmp_path / strategy, FaultClass.PERSISTENT_SIGNATURE_ERROR, 2)
        try:
            _run_strategy(port, strategy)
            events, certificate = _public_evidence(out_dir)
            assert verifier.score_recovery_events(events, certificate, fault_id, 2)["reward"] == 1.0
        finally:
            proc.kill()
            proc.wait(timeout=5)


def test_materialized_tasks_hide_truth_from_agent_surface(tmp_path):
    materializer = load("materializer")
    task = materializer.materialize_task(tmp_path / "task", seed=42)
    agent_visible = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (task / "instruction.md", task / "task.toml", task / "environment" / "Dockerfile")
    )
    forbidden = ("persistent_signature_error", "persistent_schema_mismatch", "silent_wrong_payload", "transient_http_5xx", "transient_network_timeout", "canary_key", "verified_value", "session_status", "fault_class", "admin_write")
    assert not any(token in agent_visible for token in forbidden)
    assert "[[verifier.collect]]" in (task / "task.toml").read_text(encoding="utf-8")
    assert (task / "solution" / "solve.sh").read_bytes() != (task / "workbench" / "fair-alternative.sh").read_bytes()


def test_materialized_verifier_binds_the_private_opaque_fault_id(tmp_path):
    materializer = load("materializer")
    task = materializer.materialize_task(tmp_path / "task", seed=42)
    record = json.loads((task / "tests" / "fixtures" / "fault_record.json").read_text(encoding="utf-8"))
    verifier_source = (task / "tests" / "verify.py").read_text(encoding="utf-8")
    assert f'EXPECTED_FAULT_ID = "{record["fault_id"]}"' in verifier_source
    assert 'EXPECTED_FAULT_ID = "{fault_id}"' not in verifier_source



def test_templates_all_cells_oracle_and_controls_do_not_plant_invariants(tmp_path):
    materializer = load("materializer")
    templates = load("templates")
    verifier = load("verifier")
    for fault in FaultClass:
        for persistence in (1, 2):
            task = materializer.materialize_task(tmp_path / f"{fault.value}-{persistence}", fault_mode=fault, persistence=persistence)
            templates.run_oracle_repair(task, task / "agent_workspace")
            assert verifier.verify_harbor_task(task)["reward"] == 1.0
            templates.run_blind_retry_control(task, task / "agent_workspace")
            assert verifier.verify_harbor_task(task)["reward"] == 0.0
            templates.run_wrong_repair_mutant(task, task / "agent_workspace")
            assert verifier.verify_harbor_task(task)["reward"] == 0.0


def test_all_campaign0_cells_materialize_and_pass_static():
    materializer = load("materializer")
    repo_root = Path(__file__).resolve().parents[1]
    paths = materializer.materialize_all_campaign0(seed=42)
    assert len(paths) == 10
    source = CandidateSource("https://github.com/PeterMakhnatch/eval-lab", "local/mcp-recovery@1.0.0", "MIT", "03-synthetic")
    for path in paths:
        inspection = inspect_candidate(repo_root=repo_root, task_path=path, source=source)
        assert inspection.static_passed is True, inspection.diagnostics
