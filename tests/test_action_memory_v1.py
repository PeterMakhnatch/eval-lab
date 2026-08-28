from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parents[1] / "library" / "benchmarks" / "action-memory-v1"
sys.path.insert(0, str(ROOT))


def load(name: str, filename: str | None = None):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{filename or name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_contract_metadata_and_cell_factors():
    contract_file = ROOT / "benchmark_contract.json"
    assert contract_file.exists()
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    assert contract["benchmark_family"] == "action-memory-v1"
    assert contract["construct"] == "actionable_entity_memory_and_value_binding"
    assert len(contract["cells"]) >= 4
    cell_arms = {c["arm"] for c in contract["cells"]}
    assert "clean" in cell_arms
    assert "neutral_padding" in cell_arms
    assert "semantic_distractor" in cell_arms


def test_contract_opportunity_counts_match_generator_output():
    state_mod = load("state_contract_match", "state")
    contract_file = ROOT / "benchmark_contract.json"
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    for cell in contract["cells"]:
        spec = state_mod.generate_scenario(
            seed=42,
            cell_id=cell["cell_id"],
            arm=cell["arm"],
            dose_bytes=cell["dose_bytes"],
            inversion_count=cell["inversion_count"],
            padding_position=cell.get("padding_position"),
            distractor_count=cell.get("distractor_count", 4),
        )
        assert cell["read_opportunity_count"] == spec.read_opportunity_count, (
            f"Cell {cell['cell_id']} read_opportunity_count mismatch: "
            f"declared {cell['read_opportunity_count']} != generated {spec.read_opportunity_count}"
        )
        assert cell["update_opportunity_count"] == spec.update_opportunity_count
        assert cell["mutation_opportunity_count"] == spec.mutation_opportunity_count
        assert cell["dose_bytes"] == spec.dose_bytes


def test_state_generation_deterministic_and_dosed():
    state_mod = load("state_test", "state")
    spec1 = state_mod.generate_scenario(seed=42, cell_id="clean_baseline_4k", arm="clean")
    spec2 = state_mod.generate_scenario(seed=42, cell_id="clean_baseline_4k", arm="clean")
    assert spec1 == spec2
    assert spec1.target_entity.startswith("entity_")
    assert spec1.latest_value != spec1.initial_value
    assert spec1.dose_bytes == 4096
    assert spec1.update_opportunity_count >= 1
    assert spec1.mutation_opportunity_count == 1


def test_materializer_generates_valid_harbor_and_compose_structure(tmp_path):
    mat_mod = load("mat_test", "materializer")
    target = tmp_path / "action_mem_task"
    mat_mod.materialize(output_dir=target, cell_id="clean_baseline_4k", seed=42)
    assert (target / "task.toml").exists()
    assert (target / "instruction.md").exists()
    assert (target / "environment" / "docker-compose.yaml").exists()
    assert (target / "environment" / "mcp-server" / "Dockerfile").exists()
    assert (target / "verifier" / "verify.py").exists()
    assert (target / "tests" / "verify.py").exists()
    assert (target / "solution" / "solve.sh").exists()
    assert (target / "workbench" / "fair-alternative.sh").exists()
    assert (target / "workbench" / "please-hack.sh").exists()
    assert (target / "workbench" / "adversarial" / "stale-value.sh").exists()
    assert (target / "workbench" / "adversarial" / "wrong-entity.sh").exists()
    assert (target / "workbench" / "adversarial" / "empty-output.sh").exists()


def test_mcp_server_client_protocol_interaction(tmp_path):
    mat_mod = load("mat_mcp_test", "materializer")
    runtime_mod = load("runtime_mcp_test", "runtime")
    oracle_mod = load("oracle_mcp_test", "oracle")

    task_dir = tmp_path / "mcp_task"
    mat_mod.materialize(output_dir=task_dir, cell_id="clean_baseline_4k", seed=42)

    # Start live runtime server on local free port
    port = 18080
    server_thread = threading.Thread(
        target=runtime_mod.start_server,
        args=(task_dir / "task_state", task_dir / "evidence", port),
        daemon=True,
    )
    server_thread.start()
    time.sleep(0.2)

    # Exercise standard client session: initialize, list_tools, call_tools
    client = runtime_mod.MCPClient(f"http://127.0.0.1:{port}/mcp")
    assert client.wait_until_ready(timeout_sec=5.0)
    init_res = client.initialize()
    assert init_res["serverInfo"]["name"] == "action-memory-mcp"

    tools = client.list_tools()
    tool_names = {t["name"] for t in tools}
    assert tool_names == {"list_context_chunks", "get_context_chunk", "execute_mutation"}

    # Test fastmcp.Client integration when package is installed in environment
    try:
        import asyncio

        import fastmcp

        async def _test_fastmcp():
            async with fastmcp.Client(f"http://127.0.0.1:{port}/mcp") as fm_client:
                fm_tools = await fm_client.list_tools()
                fm_tool_names = {t.name for t in fm_tools}
                assert fm_tool_names == {"list_context_chunks", "get_context_chunk", "execute_mutation"}

        asyncio.run(_test_fastmcp())
    except ImportError:
        pass

    # Run oracle solver over the live MCP protocol
    oracle_mod.solve_via_mcp(mcp_url=f"http://127.0.0.1:{port}/mcp")

    # Verify produced evidence in evidence/ and simulate collect transfer to output/
    evidence_dir = task_dir / "evidence"
    output_dir = task_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    assert (evidence_dir / "benchmark-events.jsonl").exists()
    assert (evidence_dir / "final-state.json").exists()

    shutil.copy2(evidence_dir / "benchmark-events.jsonl", output_dir / "benchmark-events.jsonl")
    shutil.copy2(evidence_dir / "final-state.json", output_dir / "final-state.json")

    final_state = json.loads((output_dir / "final-state.json").read_text(encoding="utf-8"))
    assert final_state["status"] == "executed"
    assert final_state["bound_value"] != ""


def test_verifier_discriminates_oracle_nop_and_mutants(tmp_path):
    mat_mod = load("mat_discrim", "materializer")
    ver_mod = load("ver_discrim", "verifier")
    tmpl_mod = load("tmpl_discrim", "templates")

    target = tmp_path / "task_for_discrim"
    mat_mod.materialize(output_dir=target, cell_id="clean_baseline_4k", seed=42)
    task_dir = target / "task_state"
    evidence_dir = target / "evidence"
    rewards = target / "tests" / "rewards"

    # NOP should score 0.0
    tmpl_mod.nop(task_dir, evidence_dir)
    res_nop = ver_mod.verify(task_dir, evidence_dir, reward_dir=rewards / "nop")
    assert res_nop["reward"] == 0.0

    # Oracle should score 1.0
    mat_mod.materialize(output_dir=target, cell_id="clean_baseline_4k", seed=42)
    tmpl_mod.oracle(task_dir, evidence_dir)
    res_oracle = ver_mod.verify(task_dir, evidence_dir, reward_dir=rewards / "oracle")
    assert res_oracle["reward"] == 1.0

    # Mutants should score 0.0
    for name, mutant in tmpl_mod.mutants().items():
        mat_mod.materialize(output_dir=target, cell_id="clean_baseline_4k", seed=42)
        mutant(task_dir, evidence_dir)
        res_mutant = ver_mod.verify(task_dir, evidence_dir, reward_dir=rewards / name)
        assert res_mutant["reward"] == 0.0, f"Mutant {name} must yield reward 0.0"


def test_verifier_rejects_corrupted_event_order_or_invalid_truth(tmp_path):
    mat_mod = load("mat_corrupt", "materializer")
    ver_mod = load("ver_corrupt", "verifier")

    target = tmp_path / "task_for_corrupt"
    mat_mod.materialize(output_dir=target, cell_id="clean_baseline_4k", seed=42)
    output_dir = target / "output"
    rewards = target / "tests" / "rewards"

    # Setup valid final state in output directory (post-collect destination)
    scenario = json.loads((target / "task_state" / "scenario.json").read_text(encoding="utf-8"))
    valid_final_state = {
        "status": "executed",
        "target_entity": scenario["target_entity"],
        "target_attribute": scenario["target_attribute"],
        "bound_value": scenario["latest_value"],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "final-state.json").write_text(json.dumps(valid_final_state), encoding="utf-8")

    # Corrupt event log with non-monotone descending event indices: [2, 1]
    corrupt_events = [
        {"event_index": 2, "event_type": "read_chunk", "payload": {}},
        {"event_index": 1, "event_type": "execute_mutation", "payload": {}},
    ]
    with (output_dir / "benchmark-events.jsonl").open("w", encoding="utf-8") as f:
        for ev in corrupt_events:
            f.write(json.dumps(ev) + "\n")

    # Materialized verifier targeting output_dir must reject and award 0.0
    res_corrupt = ver_mod.verify(target / "tests", output_dir, reward_dir=rewards / "corrupt_events")
    assert res_corrupt["reward"] == 0.0
    assert res_corrupt["reason"] == "non_monotone_event_indices"
