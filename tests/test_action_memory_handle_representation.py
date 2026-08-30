"""Behavioral tests for the E0b Action Memory handle-representation intervention.

Validates deterministic twins, equal content/target truth, declared single delta,
stable representation digests, fail-closed rejection of undeclared/mixed modes,
and invariant verifier truth across opaque, indexed, and range/batch reference modes.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1] / "library" / "benchmarks" / "action-memory-v1"


def load(name: str, filename: str | None = None):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{filename or name}.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_event(
    ordinal: int, tool_name: str, arguments: dict[str, Any], result_val: Any = None
) -> dict[str, Any]:
    return {
        "schema_version": "mcp-tool-event-v1",
        "event_ordinal": ordinal,
        "event_type": "tool_call_success",
        "tool_name": tool_name,
        "arguments": arguments,
        "result": {"status": "ok", "value": result_val if result_val is not None else {}},
        "is_error": False,
        "is_distractor": False,
    }


def _setup_task_and_evidence(
    tmp_path: Path,
    spec: Any,
    representation: str,
    events: list[dict[str, Any]],
    final_state: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    task_dir = tmp_path / "task"
    fixtures = task_dir / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)

    target_spec = {
        "spec_version": "1.1",
        "target_entity": spec.target_entity,
        "target_attribute": spec.target_attribute,
        "expected_bound_value": spec.latest_value,
        "required_chunk_ids": [c["chunk_id"] for c in spec.chunks],
        "dose_bytes": spec.dose_bytes,
        "representation": representation,
    }
    (fixtures / "target_spec.json").write_text(
        json.dumps(target_spec, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "benchmark-events.jsonl").write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in events), encoding="utf-8"
    )

    state = final_state or {
        "status": "executed",
        "target_entity": spec.target_entity,
        "target_attribute": spec.target_attribute,
        "bound_value": spec.latest_value,
    }
    (evidence_dir / "final-state.json").write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return task_dir, evidence_dir


def test_deterministic_twins():
    """Generating the same representation cell twice must yield identical ScenarioSpec instances."""
    rep_mod = load("am_handle_rep_det", "handle_representation")
    for seed in rep_mod.HANDLE_SEEDS:
        for dose in rep_mod.HANDLE_DOSE_BYTES:
            for arm in rep_mod.HANDLE_ARMS:
                for rep in rep_mod.HANDLE_REPRESENTATIONS:
                    spec_a = rep_mod.generate_matched_handle_arm(seed, dose, arm, rep)
                    spec_b = rep_mod.generate_matched_handle_arm(seed, dose, arm, rep)
                    assert spec_a.cell_id == spec_b.cell_id
                    assert spec_a.target_entity == spec_b.target_entity
                    assert spec_a.initial_value == spec_b.initial_value
                    assert spec_a.latest_value == spec_b.latest_value
                    assert spec_a.dose_bytes == spec_b.dose_bytes
                    assert [c["chunk_id"] for c in spec_a.chunks] == [
                        c["chunk_id"] for c in spec_b.chunks
                    ]
                    assert [c["content"] for c in spec_a.chunks] == [
                        c["content"] for c in spec_b.chunks
                    ]


def test_equal_content_and_target_truth_across_representations():
    """Matched twins must hold chunk content, dose, target truth, and read set size invariant."""
    rep_mod = load("am_handle_rep_equal", "handle_representation")
    for seed in rep_mod.HANDLE_SEEDS:
        for dose in rep_mod.HANDLE_DOSE_BYTES:
            for arm in rep_mod.HANDLE_ARMS:
                opaque = rep_mod.generate_matched_handle_arm(seed, dose, arm, "opaque")
                indexed = rep_mod.generate_matched_handle_arm(seed, dose, arm, "indexed")
                range_batch = rep_mod.generate_matched_handle_arm(seed, dose, arm, "range_batch")

                # Invariant targets and doses
                assert opaque.target_entity == indexed.target_entity == range_batch.target_entity
                assert opaque.target_attribute == indexed.target_attribute == range_batch.target_attribute
                assert opaque.initial_value == indexed.initial_value == range_batch.initial_value
                assert opaque.latest_value == indexed.latest_value == range_batch.latest_value
                assert opaque.dose_bytes == indexed.dose_bytes == range_batch.dose_bytes == dose

                # Content must be byte-for-byte identical across all 3 representations
                opaque_contents = [c["content"] for c in opaque.chunks]
                indexed_contents = [c["content"] for c in indexed.chunks]
                range_contents = [c["content"] for c in range_batch.chunks]
                assert opaque_contents == indexed_contents == range_contents

                # Required read set size must be identical
                assert len(opaque.chunks) == len(indexed.chunks) == len(range_batch.chunks)

                # Handles must follow representation rules
                assert all(c["chunk_id"].startswith("ctx_") for c in opaque.chunks)
                assert all(c["chunk_id"] == f"chunk_{i:03d}" for i, c in enumerate(indexed.chunks))
                assert all(c["chunk_id"] == f"chunk_{i:03d}" for i, c in enumerate(range_batch.chunks))


def test_declared_single_delta_and_rejection_of_undeclared_modes():
    """Manipulation must declare a single delta, provide stable digests, and reject invalid modes."""
    rep_mod = load("am_handle_rep_delta", "handle_representation")
    assert rep_mod.DECLARED_DELTA == "handle_reference_representation"

    # Stable digests for each declared representation
    opaque_digest = rep_mod.representation_digest("opaque")
    indexed_digest = rep_mod.representation_digest("indexed")
    range_digest = rep_mod.representation_digest("range_batch")

    assert opaque_digest.startswith("sha256:")
    assert indexed_digest.startswith("sha256:")
    assert range_digest.startswith("sha256:")
    assert len({opaque_digest, indexed_digest, range_digest}) == 3

    # Digest stability
    assert opaque_digest == rep_mod.representation_digest("opaque")

    # Reject undeclared representations fail-closed
    with pytest.raises(ValueError, match="undeclared handle representation"):
        rep_mod.normalize_representation("mixed_mode")

    with pytest.raises(ValueError, match="undeclared handle representation"):
        rep_mod.normalize_representation("hierarchical_tree")

    with pytest.raises(ValueError, match="undeclared handle representation"):
        rep_mod.generate_matched_handle_arm(42, 4096, "neutral_padding", "invalid_rep")


def test_enumeration_and_contract_structure():
    """Enumerated cells must populate all seed/dose/arm/representation combinations."""
    rep_mod = load("am_handle_rep_enum", "handle_representation")
    cells = rep_mod.enumerate_handle_rep_cells()

    expected_count = (
        len(rep_mod.HANDLE_SEEDS)
        * len(rep_mod.HANDLE_DOSE_BYTES)
        * len(rep_mod.HANDLE_ARMS)
        * len(rep_mod.HANDLE_REPRESENTATIONS)
    )
    assert len(cells) == expected_count

    # Every triplet of representations must share a base_task_pair_id
    pair_ids = {c["base_task_pair_id"] for c in cells}
    assert len(pair_ids) == len(rep_mod.HANDLE_SEEDS) * len(rep_mod.HANDLE_DOSE_BYTES) * len(rep_mod.HANDLE_ARMS)

    for cell in cells:
        assert cell["declared_delta"] == "handle_reference_representation"
        assert cell["representation"] in rep_mod.HANDLE_REPRESENTATIONS
        assert cell["representation_digest"] == rep_mod.representation_digest(cell["representation"])
        assert cell["tool_schema"] == list(rep_mod.REPRESENTATION_TOOL_SURFACES[cell["representation"]])


def test_verifier_truth_invariance_and_representation_binding_success(tmp_path):
    """Verifier must award 1.0 when canonical retrieval path matches the declared representation."""
    rep_mod = load("am_handle_rep_vtruth", "handle_representation")
    verifier = load("am_handle_rep_ver", "verifier")

    seed, dose, arm = 42, 4096, "neutral_padding"

    for rep in ("opaque", "indexed"):
        spec = rep_mod.generate_matched_handle_arm(seed, dose, arm, rep)
        chunk_ids = [c["chunk_id"] for c in spec.chunks]

        events = [
            _runtime_event(
                1,
                "list_context_chunks",
                {},
                {"chunk_ids": chunk_ids, "representation": rep},
            )
        ]
        for idx, cid in enumerate(chunk_ids, start=2):
            events.append(
                _runtime_event(
                    idx,
                    "get_context_chunk",
                    {"chunk_id": cid},
                    {"chunk_id": cid, "content": "context chunk"},
                )
            )
        events.append(
            _runtime_event(
                len(events) + 1,
                "execute_mutation",
                {
                    "entity_id": spec.target_entity,
                    "attribute": spec.target_attribute,
                    "bound_value": spec.latest_value,
                },
                {"status": "executed"},
            )
        )

        task_dir, evidence_dir = _setup_task_and_evidence(
            tmp_path / rep, spec, rep, events
        )
        result = verifier.verify(task_dir, evidence_dir, reward_dir=tmp_path / rep / "reward")
        assert result["reward"] == 1.0
        assert result["reason"] == "exact_latest_value_bound_after_complete_retrieval"


def test_verifier_range_batch_retrieval_modes(tmp_path):
    """Verifier must accept range/batch reads via range descriptor or batch chunk_ids list."""
    rep_mod = load("am_handle_rep_rb", "handle_representation")
    verifier = load("am_handle_rep_ver_rb", "verifier")

    seed, dose, arm = 42, 4096, "neutral_padding"
    spec = rep_mod.generate_matched_handle_arm(seed, dose, arm, "range_batch")
    chunk_ids = [c["chunk_id"] for c in spec.chunks]
    total_chunks = len(chunk_ids)

    # Sub-case A: Single range read covering the entire chunk span
    events_range = [
        _runtime_event(
            1,
            "list_context_chunks",
            {},
            {
                "chunk_ids": chunk_ids,
                "representation": "range_batch",
                "range": {"start": 0, "end": total_chunks - 1, "unit": "chunk"},
            },
        ),
        _runtime_event(
            2,
            "get_context_chunks",
            {"range": {"start": 0, "end": total_chunks - 1}},
            {"chunks": [{"chunk_id": cid, "content": "data"} for cid in chunk_ids]},
        ),
        _runtime_event(
            3,
            "execute_mutation",
            {
                "entity_id": spec.target_entity,
                "attribute": spec.target_attribute,
                "bound_value": spec.latest_value,
            },
            {"status": "executed"},
        ),
    ]

    task_dir_a, evidence_dir_a = _setup_task_and_evidence(
        tmp_path / "rb_range", spec, "range_batch", events_range
    )
    result_a = verifier.verify(task_dir_a, evidence_dir_a, reward_dir=tmp_path / "rb_range" / "reward")
    assert result_a["reward"] == 1.0

    # Sub-case B: Batch read using chunk_ids list
    events_batch = [
        _runtime_event(
            1,
            "list_context_chunks",
            {},
            {
                "chunk_ids": chunk_ids,
                "representation": "range_batch",
                "range": {"start": 0, "end": total_chunks - 1, "unit": "chunk"},
            },
        ),
        _runtime_event(
            2,
            "get_context_chunks",
            {"chunk_ids": chunk_ids},
            {"chunks": [{"chunk_id": cid, "content": "data"} for cid in chunk_ids]},
        ),
        _runtime_event(
            3,
            "execute_mutation",
            {
                "entity_id": spec.target_entity,
                "attribute": spec.target_attribute,
                "bound_value": spec.latest_value,
            },
            {"status": "executed"},
        ),
    ]

    task_dir_b, evidence_dir_b = _setup_task_and_evidence(
        tmp_path / "rb_batch", spec, "range_batch", events_batch
    )
    result_b = verifier.verify(task_dir_b, evidence_dir_b, reward_dir=tmp_path / "rb_batch" / "reward")
    assert result_b["reward"] == 1.0


def test_verifier_rejects_representation_mismatch(tmp_path):
    """Verifier must fail closed when runtime events declare a different representation than target_spec."""
    rep_mod = load("am_handle_rep_mismatch", "handle_representation")
    verifier = load("am_handle_rep_ver_mismatch", "verifier")

    spec = rep_mod.generate_matched_handle_arm(42, 4096, "neutral_padding", "opaque")
    chunk_ids = [c["chunk_id"] for c in spec.chunks]

    # Events declare 'indexed' while target_spec declared 'opaque'
    events = [
        _runtime_event(
            1,
            "list_context_chunks",
            {},
            {"chunk_ids": chunk_ids, "representation": "indexed"},
        ),
        _runtime_event(
            2,
            "execute_mutation",
            {
                "entity_id": spec.target_entity,
                "attribute": spec.target_attribute,
                "bound_value": spec.latest_value,
            },
            {"status": "executed"},
        ),
    ]

    task_dir, evidence_dir = _setup_task_and_evidence(
        tmp_path / "mismatch", spec, "opaque", events
    )
    result = verifier.verify(task_dir, evidence_dir, reward_dir=tmp_path / "mismatch" / "reward")
    assert result["reward"] == 0.0
    assert result["reason"] == "representation_mismatch_in_runtime_events"


def test_verifier_rejects_mixed_mode_tool_calls(tmp_path):
    """Verifier must reject range_batch tool calls in opaque or indexed mode."""
    rep_mod = load("am_handle_rep_mixed", "handle_representation")
    verifier = load("am_handle_rep_ver_mixed", "verifier")

    spec = rep_mod.generate_matched_handle_arm(42, 4096, "neutral_padding", "opaque")
    chunk_ids = [c["chunk_id"] for c in spec.chunks]

    events = [
        _runtime_event(
            1,
            "list_context_chunks",
            {},
            {"chunk_ids": chunk_ids, "representation": "opaque"},
        ),
        _runtime_event(
            2,
            "get_context_chunks",
            {"chunk_ids": chunk_ids},
            {"chunks": []},
        ),
        _runtime_event(
            3,
            "execute_mutation",
            {
                "entity_id": spec.target_entity,
                "attribute": spec.target_attribute,
                "bound_value": spec.latest_value,
            },
            {"status": "executed"},
        ),
    ]

    task_dir, evidence_dir = _setup_task_and_evidence(
        tmp_path / "mixed", spec, "opaque", events
    )
    result = verifier.verify(task_dir, evidence_dir, reward_dir=tmp_path / "mixed" / "reward")
    assert result["reward"] == 0.0
    assert result["reason"] == "undeclared_or_mixed_handle_reference_mode"


def test_ops_registry_and_representation_surface():
    """ops.py must expose representation-aware listings and batch retrieval."""
    ops = load("am_ops_handle_rep", "ops")
    assert "get_context_chunks" in ops.OP_REGISTRY
    assert callable(ops.OP_REGISTRY["get_context_chunks"])
    assert "list_context_chunks" in ops.OP_REGISTRY


def test_materialize_handle_representation_cell_structure(tmp_path):
    """Materializer must bind handle representation metadata and tools."""
    materializer = load("am_mat_handle_rep", "materializer")

    # Without wheelhouse, materialization will fail-closed cleanly
    with pytest.raises(ValueError, match="production FastMCP materialization requires both"):
        materializer.materialize_handle_representation_cell(
            seed=42,
            dose_bytes=4096,
            arm="neutral_padding",
            representation="opaque",
            output_dir=tmp_path / "cell",
        )
