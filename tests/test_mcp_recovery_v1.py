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
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(module_name, ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_contract_schema():
    contract = load("contract").get_benchmark_contract()
    assert contract["family"] == "mcp-recovery-v1"
    assert contract["synthetic_family"] == "family_c_fault_recovery"
    assert len(contract["verifier_truth_digest"]) == 64
    assert FaultClass.SILENT_WRONG_PAYLOAD.value in contract["cell_factors"]["fault_classes"]
    assert contract["cell_factors"]["persistence_levels"] == [1, 2]
    cell = CellFactorsC(fault_class=FaultClass.TRANSIENT_HTTP_5XX, fault_injection_count=2, seed=42)
    assert cell.fault_injection_count == 2


def test_database_state_and_certificate():
    state = load("state")
    db = state.DatabaseState({"a": 1})
    digest = db.digest()
    db.set("b", 2)
    assert db.get("b") == 2
    assert db.digest() != digest
    cert = state.StateCertificate(
        initial_digest=digest,
        final_digest=db.digest(),
        step_count=1,
        mutations=db.history,
        invariants_passed=True,
    )
    assert cert.invariants_passed is True


def test_fault_controller_transient_and_recurrent():
    faults = load("faults")
    spec1 = faults.FaultSpec("write_record", faults.FaultClass.PERSISTENT_SIGNATURE_ERROR, persistence=1)
    ctrl1 = faults.FaultController([spec1])
    injected, cls, payload = ctrl1.evaluate("write_record", {"key": "k"})
    assert injected is True
    assert cls == faults.FaultClass.PERSISTENT_SIGNATURE_ERROR
    assert payload.get("isError") is True
    assert ctrl1.evaluate("write_record", {"key": "k"})[0] is True
    cleared = faults.FaultSpec(
        "write_record",
        faults.FaultClass.PERSISTENT_SIGNATURE_ERROR,
        persistence=1,
        clear_condition=lambda _args, state: bool(state) and state.get("__auth__") == "admin_write",
    )
    ctrl_clear = faults.FaultController([cleared])
    assert ctrl_clear.evaluate("write_record", {}, state={})[0] is True
    assert ctrl_clear.evaluate("write_record", {}, state={"__auth__": "admin_write"})[0] is False
    spec2 = faults.FaultSpec("write_record", faults.FaultClass.TRANSIENT_NETWORK_TIMEOUT, persistence=2)
    ctrl2 = faults.FaultController([spec2])
    assert ctrl2.evaluate("write_record", {})[0] is True
    assert ctrl2.evaluate("write_record", {})[0] is True
    assert ctrl2.evaluate("write_record", {})[0] is False


def test_classify_fastmcp_iserror_sse():
    client = load("client")
    raw = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":'
        '"Error calling tool \'write_record\': Permission denied: write requires admin_write scope"}],'
        '"isError":true}}\n\n'
    )
    kind, _blob = client.classify_tool_response(200, raw)
    assert kind == "permission"
    timeout_raw = (
        'data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":'
        '"Error calling tool \'write_record\': Timeout"}],"isError":true}}\n'
    )
    assert client.classify_tool_response(200, timeout_raw)[0] == "timeout"


def test_runtime_tools_and_events(tmp_path):
    runtime_mod = load("runtime")
    ev_file = tmp_path / "events.jsonl"
    runtime = runtime_mod.McpServerRuntime(mode="clean", initial_state={"count": 0}, evidence_file=ev_file)
    listed = runtime.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "tools" in listed["result"]
    runtime.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "write_record", "arguments": {"key": "foo", "value": "bar"}},
        }
    )
    assert runtime.state.get("foo") == "bar"
    assert ev_file.exists()


def test_verifier_oracle_nop_and_mutants(tmp_path):
    materializer = load("materializer")
    templates = load("templates")
    verifier = load("verifier")
    task_dir = tmp_path / "task"
    materializer.materialize(task_dir, seed=42)
    templates.run_oracle_repair(task_dir, task_dir / "agent_workspace")
    assert verifier.verify_harbor_task(task_dir)["reward"] == 1.0
    templates.run_nop_baseline(task_dir, task_dir / "agent_workspace")
    assert verifier.verify_harbor_task(task_dir)["reward"] == 0.0
    templates.run_blind_retry_control(task_dir, task_dir / "agent_workspace")
    assert verifier.verify_harbor_task(task_dir)["reward"] == 0.0
    templates.run_wrong_repair_mutant(task_dir, task_dir / "agent_workspace")
    assert verifier.verify_harbor_task(task_dir)["reward"] == 0.0


def test_generated_reward_json_is_harbor_numeric(tmp_path):
    materializer = load("materializer")
    task_dir = tmp_path / "task"
    materializer.materialize(task_dir, seed=42)
    source = (task_dir / "tests" / "verify.py").read_text(encoding="utf-8")
    assert 'rewards = {"reward": reward_val, "passed": float(passed)}' in source
    assert 'LOG_DIR / "reward.json"' in source
    assert "NEED_TOOL" in source
    assert "auto_clear" in source


def test_task_workbench_static_inspection():
    materializer = load("materializer")
    repo_root = Path(__file__).resolve().parents[1]
    task_dir = materializer.output_path(seed=42)
    materializer.materialize(task_dir, seed=42)
    source = CandidateSource(
        source_uri="https://github.com/PeterMakhnatch/eval-lab",
        source_ref="local/mcp-recovery@1.0.0",
        license="MIT",
        provenance_zone="03-synthetic",
    )
    inspection = inspect_candidate(repo_root=repo_root, task_path=task_dir, source=source)
    assert inspection.static_passed is True, f"Inspection failed: {inspection.diagnostics}"
    assert inspection.candidate["candidate_id"]


def test_all_campaign0_cells_materialize_and_pass_static():
    materializer = load("materializer")
    repo_root = Path(__file__).resolve().parents[1]
    paths = materializer.materialize_all_campaign0(seed=42)
    assert len(paths) == 10
    source = CandidateSource(
        source_uri="https://github.com/PeterMakhnatch/eval-lab",
        source_ref="local/mcp-recovery@1.0.0",
        license="MIT",
        provenance_zone="03-synthetic",
    )
    for path in paths:
        inspection = inspect_candidate(repo_root=repo_root, task_path=path, source=source)
        assert inspection.static_passed is True, f"{path.name}: {inspection.diagnostics}"
        assert "from fastmcp import FastMCP" in (path / "environment/mcp-server/server.py").read_text()
        assert (path / "tests/fixtures/fault_record.json").is_file()
        assert "fault_mode" not in (path / "solution/solve.sh").read_text()
        assert (path / "solution/solve.sh").read_bytes() != (path / "workbench/fair-alternative.sh").read_bytes()


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


def _start_fastmcp(tmp_path: Path, fault: FaultClass, persistence: int) -> tuple[subprocess.Popen, Path, int]:
    materializer = load("materializer")
    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    server_path = tmp_path / "server.py"
    server_path.write_text(
        materializer.generate_recovery_server_script(fault, persistence),
        encoding="utf-8",
    )
    port = _free_port()
    env = {
        **os.environ,
        "MCP_RECOVERY_OUTPUT": str(out_dir),
        "MCP_RECOVERY_HOST": "127.0.0.1",
        "MCP_RECOVERY_PORT": str(port),
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
    }
    proc = subprocess.Popen(
        [sys.executable, str(server_path)],
        env=env,
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_port(port)
    except Exception:
        proc.kill()
        raise
    return proc, out_dir, port


def _run_oracle(port: int) -> None:
    client = load("client")
    os.environ["MCP_HOST"] = "127.0.0.1"
    os.environ["MCP_PORT"] = str(port)
    session = client.McpHttpSession(host="127.0.0.1", port=port)
    for _ in range(30):
        status, _raw = session.initialize()
        if status == 200:
            break
        time.sleep(0.1)
    session.request({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    for _attempt in range(12):
        _status, kind, _blob = session.call_tool(
            "write_record", {"key": "canary_key", "value": "verified_value"}
        )
        if kind == "permission":
            session.call_tool("refresh_auth", {"scope": "admin_write"})
            continue
        if kind in ("not_found", "timeout", "malformed", "error"):
            session.call_tool("fallback_query", {"query": "canary_key"})
            continue
        _rs, _rk, rblob = session.call_tool("read_record", {"key": "canary_key"})
        if "verified_value" in rblob:
            break


def _run_blind(port: int) -> None:
    client = load("client")
    session = client.McpHttpSession(host="127.0.0.1", port=port)
    session.initialize()
    for _ in range(6):
        session.call_tool("write_record", {"key": "canary_key", "value": "verified_value"})


def _events(out_dir: Path) -> list[dict]:
    path = out_dir / "benchmark-events.jsonl"
    assert path.is_file(), f"missing evidence {path}"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("fault", list(FaultClass))
@pytest.mark.parametrize("persistence", [1, 2])
def test_fastmcp_oracle_exact_ledger_and_blind_retry_gate(tmp_path, fault, persistence):
    pytest.importorskip("fastmcp")
    verifier = load("verifier")
    proc, out_dir, port = _start_fastmcp(tmp_path / "oracle", fault, persistence)
    try:
        _run_oracle(port)
        events = _events(out_dir)
        state = json.loads((out_dir / "final-state.json").read_text(encoding="utf-8"))
        scored = verifier.score_recovery_events(
            events,
            state.get("records", {}),
            fault.value,
            persistence,
            {"session_status": "completed", "canary_key": "verified_value"},
        )
        faults = [item for item in events if item.get("event_type") == "fault_injected"]
        assert len(faults) == persistence, (fault.value, persistence, faults, scored)
        assert scored["reward"] == 1.0, scored
        assert scored["adaptation_passed"] is True
        assert scored["auto_clear"] is False
    finally:
        proc.kill()
        proc.wait(timeout=5)

    proc2, out_dir2, port2 = _start_fastmcp(tmp_path / "blind", fault, persistence)
    try:
        _run_blind(port2)
        events2 = _events(out_dir2)
        state2 = json.loads((out_dir2 / "final-state.json").read_text(encoding="utf-8"))
        scored2 = verifier.score_recovery_events(
            events2,
            state2.get("records", {}),
            fault.value,
            persistence,
            {"session_status": "completed", "canary_key": "verified_value"},
        )
        assert scored2["reward"] == 0.0, scored2
        assert scored2["adaptation_passed"] is False
        if fault in (FaultClass.TRANSIENT_HTTP_5XX, FaultClass.TRANSIENT_NETWORK_TIMEOUT) and persistence == 1:
            assert scored2["auto_clear"] is True, scored2
    finally:
        proc2.kill()
        proc2.wait(timeout=5)
