from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
    injected, cls, _ = ctrl1.evaluate("write_record", {"key": "k"})
    assert injected is True
    assert cls == faults.FaultClass.PERSISTENT_SIGNATURE_ERROR
    assert ctrl1.evaluate("write_record", {"key": "k"})[0] is False
    spec2 = faults.FaultSpec("write_record", faults.FaultClass.TRANSIENT_NETWORK_TIMEOUT, persistence=2)
    ctrl2 = faults.FaultController([spec2])
    assert ctrl2.evaluate("write_record", {})[0] is True
    assert ctrl2.evaluate("write_record", {})[0] is True
    assert ctrl2.evaluate("write_record", {})[0] is False


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
