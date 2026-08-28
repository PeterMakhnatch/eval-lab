from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "library" / "benchmarks" / "mcp-recovery-v1"
sys.path.insert(0, str(ROOT))

from contract import get_benchmark_contract  # noqa: E402
from faults import FaultClass, FaultController, FaultSpec  # noqa: E402
from materializer import materialize, output_path  # noqa: E402
from runtime import McpServerRuntime  # noqa: E402
from state import DatabaseState, StateCertificate  # noqa: E402
from templates import (  # noqa: E402
    run_blind_retry_control,
    run_nop_baseline,
    run_oracle_repair,
    run_wrong_repair_mutant,
)
from verifier import verify_harbor_task  # noqa: E402

from evallab.task_workbench import CandidateSource, inspect_candidate  # noqa: E402


def test_contract_schema():
    contract = get_benchmark_contract()
    assert contract["family"] == "mcp-recovery-v1"
    assert len(contract["verifier_truth_digest"]) == 64
    assert "permission_denied" in contract["cell_factors"]["fault_classes"]
    assert 1 in contract["cell_factors"]["persistence_levels"]
    assert 2 in contract["cell_factors"]["persistence_levels"]


def test_database_state_and_certificate():
    db = DatabaseState({"a": 1})
    d1 = db.digest()
    db.set("b", 2)
    assert db.get("b") == 2
    assert db.digest() != d1
    cert = StateCertificate(initial_digest=d1, final_digest=db.digest(), step_count=1, mutations=db.history, invariants_passed=True)
    assert cert.invariants_passed is True


def test_fault_controller_transient_and_recurrent():
    # Transient fault (persistence=1)
    spec1 = FaultSpec("write_record", FaultClass.PERMISSION_DENIED, persistence=1)
    ctrl1 = FaultController([spec1])

    # Hit 1 -> faults
    f1, cls1, _ = ctrl1.evaluate("write_record", {"key": "k"})
    assert f1 is True
    assert cls1 == FaultClass.PERMISSION_DENIED

    # Hit 2 -> cleared
    f2, _, _ = ctrl1.evaluate("write_record", {"key": "k"})
    assert f2 is False

    # Recurrent fault (persistence=2)
    spec2 = FaultSpec("write_record", FaultClass.TIMEOUT, persistence=2)
    ctrl2 = FaultController([spec2])
    assert ctrl2.evaluate("write_record", {})[0] is True
    assert ctrl2.evaluate("write_record", {})[0] is True
    assert ctrl2.evaluate("write_record", {})[0] is False


def test_runtime_tools_and_events(tmp_path):
    ev_file = tmp_path / "events.jsonl"
    runtime = McpServerRuntime(mode="clean", initial_state={"count": 0}, evidence_file=ev_file)

    # Call tools/list
    res = runtime.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "tools" in res["result"]

    # Call write_record
    res = runtime.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "write_record", "arguments": {"key": "foo", "value": "bar"}}})
    assert "result" in res
    assert runtime.state.get("foo") == "bar"
    assert ev_file.exists()


def test_verifier_oracle_nop_and_mutants(tmp_path):
    task_dir = tmp_path / "task"
    materialize(task_dir, seed=42)

    # 1. Oracle scores 1.0
    run_oracle_repair(task_dir, task_dir / "agent_workspace")
    res_oracle = verify_harbor_task(task_dir)
    assert res_oracle["reward"] == 1.0
    assert res_oracle["success"] is True

    # 2. NOP scores 0.0
    run_nop_baseline(task_dir, task_dir / "agent_workspace")
    res_nop = verify_harbor_task(task_dir)
    assert res_nop["reward"] == 0.0
    assert res_nop["success"] is False

    # 3. Blind retry on recurrent fault scores 0.0
    run_blind_retry_control(task_dir, task_dir / "agent_workspace")
    res_blind = verify_harbor_task(task_dir)
    assert res_blind["reward"] == 0.0

    # 4. Wrong repair mutant scores 0.0
    run_wrong_repair_mutant(task_dir, task_dir / "agent_workspace")
    res_wrong = verify_harbor_task(task_dir)
    assert res_wrong["reward"] == 0.0


def test_task_workbench_static_inspection():
    repo_root = Path(__file__).resolve().parents[1]
    task_dir = output_path(seed=42, fault_mode="permission_denied", persistence=1)
    materialize(task_dir, seed=42, fault_mode="permission_denied", persistence=1)
    
    source = CandidateSource(
        source_uri="https://github.com/PeterMakhnatch/eval-lab",
        source_ref="local/mcp-recovery@1.0",
        license="MIT",
        provenance_zone="03-synthetic",
    )
    
    inspection = inspect_candidate(repo_root=repo_root, task_path=task_dir, source=source)
    assert inspection.static_passed is True, f"Inspection failed: {inspection.diagnostics}"
    assert inspection.candidate["candidate_id"] is not None
