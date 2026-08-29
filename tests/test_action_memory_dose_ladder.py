from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1] / "library" / "benchmarks" / "action-memory-v1"


def load(name: str, filename: str | None = None):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{filename or name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _require_production_sidecar() -> None:
    if not (os.environ.get("ACTION_MEMORY_WHEELHOUSE") and os.environ.get("ACTION_MEMORY_RESOLVER_PROVENANCE")):
        pytest.skip("target-specific FastMCP wheelhouse/provenance not populated on this host")


def _runtime_event(ordinal: int, tool_name: str, arguments: dict[str, str]) -> dict[str, object]:
    return {"schema_version": "mcp-tool-event-v1", "event_ordinal": ordinal, "event_type": "tool_call_success", "tool_name": tool_name, "arguments": arguments, "result": {"status": "ok", "value": {}}, "is_error": False, "is_distractor": False}


def _write_truth_and_evidence(tmp_path: Path, chunk_ids: list[str], *, complete: bool) -> tuple[Path, Path]:
    task_dir, evidence_dir = tmp_path / "task", tmp_path / "evidence"
    fixtures = task_dir / "fixtures"
    fixtures.mkdir(parents=True)
    truth = {"target_entity": "entity_555", "target_attribute": "routing_key", "expected_bound_value": "v2", "required_chunk_ids": chunk_ids}
    (fixtures / "target_spec.json").write_text(json.dumps(truth), encoding="utf-8")
    evidence_dir.mkdir()
    events = [_runtime_event(1, "list_context_chunks", {})]
    events.extend(_runtime_event(index, "get_context_chunk", {"chunk_id": chunk_id}) for index, chunk_id in enumerate(chunk_ids if complete else chunk_ids[:-1], start=2))
    events.append(_runtime_event(len(events) + 1, "execute_mutation", {"entity_id": "entity_555", "attribute": "routing_key", "bound_value": "v2"}))
    events[0]["result"]["value"] = {"chunk_ids": chunk_ids}
    for event in events[1:-1]:
        event["result"]["value"] = {"chunk_id": event["arguments"]["chunk_id"], "content": "context"}
    events[-1]["result"]["value"] = {"status": "executed"}
    (evidence_dir / "benchmark-events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    (evidence_dir / "final-state.json").write_text(json.dumps({"status": "executed", "target_entity": "entity_555", "target_attribute": "routing_key", "bound_value": "v2"}), encoding="utf-8")
    return task_dir, evidence_dir


def test_v1_identities_are_frozen():
    state = load("am_state_frozen", "action_memory_state")
    spec = state.generate_scenario(seed=42, cell_id="clean-baseline-4k", arm="clean")
    assert (spec.target_entity, spec.initial_value, spec.latest_value) == ("entity_891", "1e9c5dd9_v1", "1e9c5dd9_v2")
    contract = json.loads((ROOT / "benchmark_contract.json").read_text(encoding="utf-8"))
    assert contract["version"] == "1.0.0"
    assert [cell["cell_id"] for cell in contract["cells"]] == ["clean-baseline-4k", "neutral-padding-16k-prefix", "neutral-padding-16k-middle", "semantic-distractor-16k", "multi-inversion-semantic-distractor-64k"]


def test_matched_pairs_use_opaque_handles_and_equal_public_response_bytes():
    ladder, ops = load("am_dose_ladder", "dose_ladder"), load("am_ops_surface", "ops")
    for seed in ladder.DOSE_LADDER_SEEDS:
        for dose in ladder.DOSE_LADDER_BYTES:
            neutral = ladder.generate_matched_dose_arm(seed, dose, "neutral_padding")
            semantic = ladder.generate_matched_dose_arm(seed, dose, "semantic_distractor")
            assert (neutral.target_entity, neutral.initial_value, neutral.latest_value, neutral.dose_bytes) == (semantic.target_entity, semantic.initial_value, semantic.latest_value, dose)
            assert [chunk["chunk_id"] for chunk in neutral.chunks] == [chunk["chunk_id"] for chunk in semantic.chunks]
            assert all(chunk["chunk_id"].startswith("ctx_") and all(label not in chunk["chunk_id"] for label in ("init", "inv", "fill", "padding", "distractor")) for chunk in neutral.chunks)
            for neutral_chunk, semantic_chunk in zip(neutral.chunks, semantic.chunks, strict=True):
                assert set(ops.agent_visible_chunk(neutral_chunk)) == {"chunk_id", "content"}
                assert len(ops.canonical_agent_chunk_bytes(neutral_chunk)) == len(ops.canonical_agent_chunk_bytes(semantic_chunk))
            assert all(neutral_chunk["content"] != semantic_chunk["content"] for neutral_chunk, semantic_chunk in zip(neutral.chunks[2:], semantic.chunks[2:], strict=True))


def test_dose_ladder_four_levels_three_seeds_and_12_pairs():
    ladder = load("am_dose_ladder_enum", "dose_ladder")
    cells = ladder.enumerate_dose_ladder_cells()
    assert len(cells) == 24
    assert {cell["dose_bytes"] for cell in cells} == {4096, 16384, 65536, 131072}
    assert {cell["seed"] for cell in cells} == {42, 1337, 2026}
    assert {cell["dose_axis_version"] for cell in cells} == {"am-dose-ladder-v1"}
    assert len({cell["base_task_pair_id"] for cell in cells}) == 12


class _MockMcpSession:
    def __init__(self, chunks: list[dict[str, object]]) -> None:
        self.chunks = chunks
        self.mutation_called: dict[str, str] | None = None

    def initialize(self) -> tuple[int, str]:
        return 200, "ok"

    def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
        args = arguments or {}
        if name == "list_context_chunks":
            return {"chunk_ids": [chunk["chunk_id"] for chunk in self.chunks]}
        if name == "get_context_chunk":
            target = next(chunk for chunk in self.chunks if chunk["chunk_id"] == args["chunk_id"])
            return {"chunk_id": target["chunk_id"], "content": target["content"]}
        if name == "execute_mutation":
            self.mutation_called = dict(args)
            return {"status": "ok"}
        raise ValueError(f"unknown tool {name}")


def test_oracle_solve_via_mcp_and_fair_resolve_prefix_collision_semantic_cells(monkeypatch):
    ladder = load("am_dl_prefix", "dose_ladder")
    oracle = load("am_oracle_prefix", "oracle")
    layout = load("am_layout_prefix", "package_layout")
    cases = [(42, 65536, "entity_858"), (42, 131072, "entity_525"), (2026, 131072, "entity_548")]
    for seed, dose, expected_ent in cases:
        spec = ladder.generate_matched_dose_arm(seed, dose, "semantic_distractor")
        assert spec.target_entity == expected_ent
        mock = _MockMcpSession(spec.chunks)
        monkeypatch.setattr(oracle, "_get_client", lambda m=mock: lambda url=None: m)
        oracle.solve_via_mcp(mcp_url="http://mock:8080/mcp")
        assert mock.mutation_called is not None
        assert mock.mutation_called["entity_id"] == spec.target_entity
        assert mock.mutation_called["bound_value"] == spec.latest_value

        oracle_code = layout.oracle_solve_py()
        fair_code = layout.fair_alternative_mcp_snippet()
        assert r're.search(rf"\b{re.escape(target_entity)}\b", text)' in oracle_code
        assert r're.search(rf"\b{re.escape(entity)}\b", text)' in fair_code


def test_verifier_requires_complete_canonical_retrieval_path(tmp_path):
    verifier = load("am_ver_retrieval", "verifier")
    task_dir, evidence_dir = _write_truth_and_evidence(tmp_path / "complete", ["ctx_a", "ctx_b"], complete=True)
    assert verifier.verify(task_dir, evidence_dir, reward_dir=tmp_path / "complete-reward")["reward"] == 1.0
    task_dir, evidence_dir = _write_truth_and_evidence(tmp_path / "incomplete", ["ctx_a", "ctx_b"], complete=False)
    result = verifier.verify(task_dir, evidence_dir, reward_dir=tmp_path / "incomplete-reward")
    assert (result["reward"], result["reason"]) == (0.0, "incomplete_or_reordered_context_retrieval")


def test_verifier_rejects_noncanonical_evidence(tmp_path):
    verifier = load("am_ver_canonical", "verifier")
    task_dir, evidence_dir = _write_truth_and_evidence(tmp_path, ["ctx_a"], complete=True)
    (evidence_dir / "benchmark-events.jsonl").write_text(json.dumps({"event_ordinal": 1}) + "\n", encoding="utf-8")
    result = verifier.verify(task_dir, evidence_dir, reward_dir=tmp_path / "reward")
    assert (result["reward"], result["reason"]) == (0.0, "noncanonical_runtime_evidence")


def test_instruction_does_not_disclose_private_truth_or_output_location():
    layout = load("am_layout_instruction", "package_layout")
    private = SimpleNamespace(target_entity="entity_711", target_attribute="routing_key", latest_value="secret_v2")
    instruction = layout.instruction_md(private)
    for leaked in (private.target_entity, private.target_attribute, private.latest_value, "/app/output", "entity_id", "bound_value"):
        assert leaked not in instruction
    assert "execute_mutation" in instruction


def test_materialize_fails_closed_without_trusted_provenance(tmp_path, monkeypatch):
    materializer = load("am_mat_fail_closed", "materializer")
    monkeypatch.delenv(materializer.WHEELHOUSE_ENV, raising=False)
    monkeypatch.delenv(materializer.RESOLVER_PROVENANCE_ENV, raising=False)
    with pytest.raises(ValueError, match="requires both"):
        materializer.materialize(output_dir=tmp_path / "task")


def test_production_package_uses_trusted_wheelhouse(tmp_path):
    _require_production_sidecar()
    materializer = load("am_mat_prod", "materializer")
    sidecar = tmp_path / "prod" / "environment" / "mcp-server"
    assert not sidecar.exists()
    result = materializer.materialize_dose_ladder_cell(42, 4096, "neutral_padding", output_dir=tmp_path / "prod")
    assert list((sidecar / "wheelhouse").glob("*.whl"))
    assert "--require-hashes" in (sidecar / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY ops.py /app/ops.py" in (sidecar / "Dockerfile").read_text(encoding="utf-8")
    proof = json.loads((sidecar / "offline-build-proof.json").read_text(encoding="utf-8"))
    assert {asset["path"] for asset in proof["runtime_assets"]} == {"ops.py", "scenario.json"}
    assert result["plan_only"] is False
