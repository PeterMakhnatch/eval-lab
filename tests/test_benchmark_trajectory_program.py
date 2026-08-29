"""Comprehensive test suite for Benchmark Trajectory Program and Observables.

Tests:
1. Strict benchmark event ingestion, canonical ordering, gap rejection, and call-ID correlation.
2. Benchmark feature producers (Action Memory, MCP FuncDAG, MCP Recovery) with strict NULL preservation.
3. Feature registry contracts and CI verification.
4. DuckDB benchmark baseline and diagnostic views.
5. Trajectory card generation with benchmark observables.
6. CLI commands (traj benchmark, traj card).
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from evallab import cli
from evallab.interpretation.benchmark_events import (
    BenchmarkEventDuplicateError,
    BenchmarkEventGapError,
    correlate_tool_calls,
    load_trial_bundle,
    parse_benchmark_contract,
    parse_benchmark_events,
)
from evallab.interpretation.benchmark_projection import (
    agent_readable_projection_provenance,
    backfill_benchmark_projection_rows,
    build_projection_dimensions,
)
from evallab.interpretation.feature_registry import (
    TRAJECTORY_FEATURE_REGISTRY,
    verify_feature_registry,
)
from evallab.interpretation.producers.action_memory import (
    extract_action_memory_features,
)
from evallab.interpretation.producers.mcp_funcdag import (
    extract_mcp_funcdag_features,
)
from evallab.interpretation.producers.mcp_recovery import (
    extract_mcp_recovery_features,
)
from evallab.interpretation.traj_card import generate_traj_card
from evallab.interpretation.trajectory_compliance import TrialComplianceRecord
from evallab.interpretation.trajectory_compliance_ops import (
    ComplianceIngestReport,
    ReadinessGates,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def action_memory_trial_dir(tmp_path: Path) -> Path:
    trial_dir = tmp_path / "action_memory_pass"
    trial_dir.mkdir(parents=True)

    contract = {
        "benchmark_family": "action-memory-v1",
        "version": "1.0.0",
        "construct": "actionable_entity_memory_and_value_binding",
        "seeds": [42, 1337],
        "cells": [
            {
                "cell_id": "clean-baseline-4k",
                "dose_bytes": 4096,
                "arm": "clean",
                "inversion_count": 1,
                "update_opportunity_count": 1,
                "read_opportunity_count": 7,
                "mutation_opportunity_count": 1,
            }
        ],
        "verifier_truth_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
    }
    (trial_dir / "benchmark_contract.json").write_text(json.dumps(contract), encoding="utf-8")

    events = [
        {
            "event_index": 0,
            "timestamp": "2026-08-28T10:00:00Z",
            "event_type": "read_chunk",
            "payload": {"chunk_id": "chunk_1", "byte_count": 512},
        },
        {
            "event_index": 1,
            "timestamp": "2026-08-28T10:00:01Z",
            "event_type": "read_chunk",
            "payload": {"chunk_id": "chunk_2", "byte_count": 512},
        },
        {
            "event_index": 2,
            "timestamp": "2026-08-28T10:00:02Z",
            "event_type": "execute_mutation",
            "payload": {
                "entity_id": "entity_42",
                "attribute": "routing_key",
                "bound_value": "abc-123",
            },
        },
    ]
    (trial_dir / "benchmark-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )

    final_state = {
        "trial_id": "action_memory_pass",
        "status": "executed",
        "target_entity": "entity_42",
        "target_attribute": "routing_key",
        "bound_value": "abc-123",
        "invariants_passed": True,
    }
    (trial_dir / "final-state.json").write_text(json.dumps(final_state), encoding="utf-8")

    # Add dummy ATIF trajectory
    atif_traj = {
        "schema_version": "ATIF-v1.0",
        "session_id": "action_memory_pass",
        "agent": {"name": "test_agent", "version": "1.0"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [{"tool_name": "memory_read", "arguments": {"key": "session_id"}}],
                "tokens": {"prompt": 100, "completion": 20},
            },
            {
                "step_id": 2,
                "source": "agent",
                "tool_calls": [],
                "tokens": {"prompt": 150, "completion": 30},
            },
        ],
    }
    (trial_dir / "trajectory.json").write_text(json.dumps(atif_traj), encoding="utf-8")

    result = {
        "trial_id": "action_memory_pass",
        "status": "COMPLETED",
        "primary_reward": 1.0,
        "cost_usd": 0.01,
        "duration_seconds": 5.0,
    }
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")

    return trial_dir


@pytest.fixture
def mcp_funcdag_trial_dir(tmp_path: Path) -> Path:
    trial_dir = tmp_path / "mcp_funcdag_pass"
    trial_dir.mkdir(parents=True)

    contract = {
        "family": "mcp-funcdag-v1",
        "version": "1.0.0",
        "construct": "tool_call_dag_conformance",
        "seed": 42,
        "cell_factors": {"depth": 2, "width": 1, "declared_dag_edges": [["step_a", "step_b"]]},
        "task_id": "pipeline_dag_test",
        "opportunity_counts": {"required_dag_edges": 1, "required_node_count": 2},
        "verifier_truth_digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
    }
    (trial_dir / "benchmark_contract.json").write_text(json.dumps(contract), encoding="utf-8")

    events = [
        {
            "event_index": 0,
            "timestamp": "2026-08-28T10:00:00Z",
            "event_type": "tool_call_success",
            "tool_name": "step_a",
            "arguments": {"input_val": 10},
            "result": 20,
            "schema_conforming": True,
        },
        {
            "event_index": 1,
            "timestamp": "2026-08-28T10:00:01Z",
            "event_type": "tool_call_success",
            "tool_name": "step_b",
            "arguments": {"input_val": 20},
            "result": 40,
            "schema_conforming": True,
        },
    ]
    (trial_dir / "benchmark-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )

    final_state = {
        "trial_id": "mcp_funcdag_pass",
        "invariants_passed": True,
        "total_calls": 2,
        "executed_tools": ["step_a", "step_b"],
        "redundant_calls": 0,
        "last_result": 40,
    }
    (trial_dir / "final-state.json").write_text(json.dumps(final_state), encoding="utf-8")
    atif_traj = {
        "schema_version": "ATIF-v1.0",
        "session_id": "mcp_funcdag_pass",
        "agent": {"name": "test_agent", "version": "1.0"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [{"tool_name": "step_a", "arguments": {}}],
                "tokens": {"prompt": 100, "completion": 20},
            },
            {
                "step_id": 2,
                "source": "agent",
                "tool_calls": [{"tool_name": "step_b", "arguments": {}}],
                "tokens": {"prompt": 150, "completion": 30},
            },
        ],
    }
    (trial_dir / "trajectory.json").write_text(json.dumps(atif_traj), encoding="utf-8")

    result = {
        "trial_id": "mcp_funcdag_pass",
        "status": "COMPLETED",
        "primary_reward": 1.0,
        "cost_usd": 0.02,
        "duration_seconds": 4.0,
    }
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")

    return trial_dir


@pytest.fixture
def mcp_recovery_trial_dir(tmp_path: Path) -> Path:
    trial_dir = tmp_path / "mcp_recovery_pass"
    trial_dir.mkdir(parents=True)

    contract = {
        "family": "mcp-recovery-v1",
        "version": "1.0.0",
        "construct": "fault_recovery_survival",
        "seed": 42,
        "cell_factors": {
            "fault_classes": ["HTTP_503_SERVICE_UNAVAILABLE"],
            "persistence_levels": [1],
            "mode": "fault",
        },
        "task_id": "resilience_recovery_test",
        "verifier_truth_digest": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
    }
    (trial_dir / "benchmark_contract.json").write_text(json.dumps(contract), encoding="utf-8")

    events = [
        {
            "event_index": 0,
            "timestamp": "2026-08-28T10:00:00Z",
            "event_type": "mcp_call",
            "call_id": "c1",
            "tool_name": "api_fetch",
            "arguments": {"endpoint": "/data"},
        },
        {
            "event_index": 1,
            "timestamp": "2026-08-28T10:00:01Z",
            "event_type": "fault_injected",
            "call_id": "c1",
            "payload": {
                "tool": "api_fetch",
                "fault_class": "HTTP_503_SERVICE_UNAVAILABLE",
            },
        },
        {
            "event_index": 2,
            "timestamp": "2026-08-28T10:00:02Z",
            "event_type": "mcp_call",
            "call_id": "c2",
            "tool_name": "api_fetch",
            "arguments": {"endpoint": "/data", "retry": True},
        },
        {
            "event_index": 3,
            "timestamp": "2026-08-28T10:00:03Z",
            "event_type": "tool_executed",
            "call_id": "c2",
            "payload": {"tool": "api_fetch", "state_digest": "sha256:abc"},
        },
        {
            "event_index": 4,
            "timestamp": "2026-08-28T10:00:04Z",
            "event_type": "tool_result",
            "call_id": "c2",
            "payload": {"result": {"status": "recovered"}},
        },
    ]
    (trial_dir / "benchmark-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )

    final_state = {
        "trial_id": "mcp_recovery_pass",
        "invariants_passed": True,
        "details": {
            "faults_injected_count": 1,
            "autonomous_recoveries_count": 1,
            "human_interventions_count": 0,
        },
    }
    (trial_dir / "final-state.json").write_text(json.dumps(final_state), encoding="utf-8")

    atif_traj = {
        "schema_version": "ATIF-v1.0",
        "session_id": "mcp_recovery_pass",
        "agent": {"name": "test_agent", "version": "1.0"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [{"tool_name": "api_fetch", "arguments": {}}],
                "tokens": {"prompt": 100, "completion": 20},
            },
            {
                "step_id": 2,
                "source": "agent",
                "tool_calls": [{"tool_name": "api_fetch", "arguments": {"retry": True}}],
                "tokens": {"prompt": 120, "completion": 25},
            },
        ],
    }
    (trial_dir / "trajectory.json").write_text(json.dumps(atif_traj), encoding="utf-8")

    result = {
        "trial_id": "mcp_recovery_pass",
        "status": "COMPLETED",
        "primary_reward": 1.0,
        "cost_usd": 0.015,
        "duration_seconds": 6.0,
    }
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")

    return trial_dir


# ---------------------------------------------------------------------------
# Test Cases: Ingestion & Validation Invariants
# ---------------------------------------------------------------------------


def test_benchmark_events_canonical_ordering_and_gap_rejection():
    """Event indices must be monotonically increasing 1..N without gaps."""
    # Valid contiguous events
    valid_events = [
        {"event_index": 1, "timestamp": "2026-08-28T10:00:00Z", "event_type": "init"},
        {"event_index": 2, "timestamp": "2026-08-28T10:00:01Z", "event_type": "step"},
    ]
    parsed = parse_benchmark_events(valid_events)
    assert len(parsed) == 2
    assert parsed[0].event_index == 1
    assert parsed[1].event_index == 2

    # Non-monotonic gap (e.g. index 1 followed by index 3)
    gap_events = [
        {"event_index": 1, "timestamp": "2026-08-28T10:00:00Z", "event_type": "init"},
        {"event_index": 3, "timestamp": "2026-08-28T10:00:01Z", "event_type": "step"},
    ]
    with pytest.raises(BenchmarkEventGapError):
        parse_benchmark_events(gap_events)

    # Duplicate index (e.g. 1, 1)
    dup_events = [
        {"event_index": 1, "timestamp": "2026-08-28T10:00:00Z", "event_type": "init"},
        {"event_index": 1, "timestamp": "2026-08-28T10:00:01Z", "event_type": "step"},
    ]
    with pytest.raises(BenchmarkEventDuplicateError):
        parse_benchmark_events(dup_events)


def test_correlate_tool_calls():
    """Tool requests must correlate accurately with execution events."""
    events = [
        {
            "event_index": 1,
            "timestamp": "2026-08-28T10:00:00Z",
            "event_type": "tool_call_requested",
            "call_id": "call_abc",
            "tool_name": "read_file",
            "arguments": {"path": "main.py"},
        },
        {
            "event_index": 2,
            "timestamp": "2026-08-28T10:00:01Z",
            "event_type": "tool_call_executed",
            "call_id": "call_abc",
            "tool_name": "read_file",
            "result": {"content": "print('hello')"},
        },
    ]
    parsed = parse_benchmark_events(events)
    correlated = correlate_tool_calls(parsed)
    assert len(correlated) == 1
    assert correlated[0].call_id == "call_abc"
    assert correlated[0].tool_name == "read_file"
    assert correlated[0].request_event.event_index == 1
    assert correlated[0].execution_event is not None
    assert correlated[0].execution_event.event_index == 2


# ---------------------------------------------------------------------------
# Test Cases: Feature Producers
# ---------------------------------------------------------------------------


def test_action_memory_feature_extraction(action_memory_trial_dir: Path):
    """Action memory producer computes L1 facts and L2 derived metrics with NULL preservation."""
    bundle = load_trial_bundle(action_memory_trial_dir)
    features = extract_action_memory_features(bundle, step_tokens=[100, 150])

    assert features.task_success is True
    assert features.raw_binding_opportunities == 1
    assert features.binding_matched is True
    assert features.stale_value_bound is False
    assert features.binding_survival_rate == 1.0
    assert features.construct == "actionable_entity_memory_and_value_binding"


def test_mcp_funcdag_feature_extraction(mcp_funcdag_trial_dir: Path):
    """MCP FuncDAG producer computes dag conformance and cycle violations."""
    bundle = load_trial_bundle(mcp_funcdag_trial_dir)
    features = extract_mcp_funcdag_features(bundle, step_tokens=[100, 150])

    assert features.task_success is True
    assert features.required_dag_edges == 1
    assert features.executed_dag_edges == 1
    assert features.dag_edge_conformance_rate == 1.0
    assert features.redundant_tool_calls == 0
    assert features.cycle_violations == 0
    assert features.construct == "tool_call_dag_conformance"


def test_mcp_recovery_feature_extraction(mcp_recovery_trial_dir: Path):
    """MCP Recovery producer computes autonomous recovery metrics."""
    bundle = load_trial_bundle(mcp_recovery_trial_dir)
    features = extract_mcp_recovery_features(bundle, step_tokens=[100, 120])

    assert features.task_success is True
    assert features.injected_fault_count == 1
    assert features.certified_recovered_faults == 1
    assert features.autonomous_recovery_rate == 1.0
    assert features.fault_detection_rate == 1.0
    assert features.blind_retries == 0
    assert features.causal_grade == "C3"


def test_null_preservation_on_zero_denominators(tmp_path: Path):
    """Zero-denominator rates must strictly return None / NULL, never 0.0 or divide-by-zero."""
    trial_dir = tmp_path / "zero_denoms"
    trial_dir.mkdir(parents=True)

    contract = {
        "family": "mcp-recovery-v1",
        "task_name": "no_faults_test",
        "agent_name": "clean_agent",
        "cell_factors": {"persistence_levels": [1]},
        "verifier_truth_digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    }
    (trial_dir / "benchmark_contract.json").write_text(json.dumps(contract), encoding="utf-8")
    (trial_dir / "benchmark-events.jsonl").write_text(
        json.dumps({"event_index": 1, "timestamp": "2026-08-28T10:00:00Z", "event_type": "noop"})
        + "\n",
        encoding="utf-8",
    )
    final_state = {
        "trial_id": "zero_denoms",
        "task_success": True,
        "faults_injected_count": 0,
        "autonomous_recoveries_count": 0,
        "unrecovered_faults_count": 0,
        "total_recovery_steps": 0,
        "post_recovery_regressions_count": 0,
    }
    (trial_dir / "final-state.json").write_text(json.dumps(final_state), encoding="utf-8")

    bundle = load_trial_bundle(trial_dir)
    features = extract_mcp_recovery_features(bundle, step_tokens=[])

    # Zero faults -> autonomous_recovery_rate must be None (NULL), not 0.0
    assert features.injected_fault_count == 0
    assert features.autonomous_recovery_rate is None
    assert features.blind_retry_rate is None
    assert features.fault_recovery_latency is None


# ---------------------------------------------------------------------------
# Test Cases: Feature Registry Contracts
# ---------------------------------------------------------------------------


def test_feature_registry_ci_verification():
    """All registered features must comply with typing, category, and denominator contracts."""
    errors = verify_feature_registry()
    assert errors == [], f"Feature registry CI verification failed: {errors}"


def test_feature_registry_lookup():
    """Feature registry supports schema inspection and metadata retrieval."""
    feat = TRAJECTORY_FEATURE_REGISTRY.get("binding_survival_rate")
    assert feat is not None
    assert feat.category == "benchmark_l2_metric"
    assert feat.data_type == "DOUBLE"
    assert feat.denominator_sibling == "raw_binding_opportunities"
    assert feat.null_on_zero_denominator is True


# ---------------------------------------------------------------------------
# Test Cases: DuckDB Benchmark Views
# ---------------------------------------------------------------------------


def test_duckdb_benchmark_views_execution():
    """sql/traj_benchmark_views.sql must load and execute cleanly in DuckDB."""
    sql_path = Path("sql/traj_benchmark_views.sql")
    assert sql_path.is_file()

    con = duckdb.connect()
    con.execute(sql_path.read_text(encoding="utf-8"))

    # Test baseline queries
    tables = [t[0] for t in con.execute("SHOW TABLES").fetchall()]
    assert "v_action_memory_baseline" in tables
    assert "v_mcp_funcdag_baseline" in tables
    assert "v_mcp_recovery_baseline" in tables
    assert "v_benchmark_summary" in tables
    assert "v_benchmark_contrasts" in tables
    assert "v_benchmark_refusal_diagnostics" in tables

    # Test query execution on views
    summary_rows = con.execute("SELECT * FROM v_benchmark_summary").fetchall()
    assert len(summary_rows) == 3


# ---------------------------------------------------------------------------
# Test Cases: Trajectory Card with Benchmark Observables
# ---------------------------------------------------------------------------


def test_traj_card_with_benchmark_observables(action_memory_trial_dir: Path, tmp_path: Path):
    """evallab traj card renders Section 9 with benchmark observables and verifier truth."""
    rendered, card_data = generate_traj_card(
        action_memory_trial_dir,
        repo_root=tmp_path,
        runs_roots=[tmp_path],
    )

    assert card_data.benchmark_family == "action-memory-v1"
    assert card_data.benchmark_features is not None
    assert card_data.benchmark_features["binding_survival_rate"] == 1.0

    assert "## 9. Benchmark Observables (`action-memory-v1`)" in rendered
    assert "**Construct:** `actionable_entity_memory_and_value_binding`" in rendered
    assert (
        "**Verifier Ground Truth Digest:** `sha256:1111111111111111111111111111111111111111111111111111111111111111`"
        in rendered
    )
    assert "`binding_survival_rate`" in rendered


def test_traj_benchmark_cli_emits_observables_json(
    action_memory_trial_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """The registered CLI leaf extracts benchmark observables from a real trial directory."""
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)

    exit_code = cli.run_cli(
        ["traj", "benchmark", str(action_memory_trial_dir), "--json"],
        workspace=tmp_path,
    )

    assert exit_code == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["family"] == "action-memory-v1"
    assert rendered["binding_survival_rate"] == 1.0


def test_fault_injection_correlation_edge_cases():
    """Unmatched or out-of-order fault injection events must be correlated safely."""
    events = [
        {
            "event_index": 1,
            "timestamp": "2026-08-28T10:00:00Z",
            "event_type": "fault_injected",
            "fault_class": "HTTP_500_INTERNAL_SERVER_ERROR",
        },
        {
            "event_index": 2,
            "timestamp": "2026-08-28T10:00:01Z",
            "event_type": "mcp_call",
            "call_id": "call_1",
            "tool_name": "fetch_data",
            "arguments": {"url": "https://api.example.com"},
        },
        {
            "event_index": 3,
            "timestamp": "2026-08-28T10:00:02Z",
            "event_type": "fault_injected",
            "call_id": "call_1",
            "fault_class": "RATE_LIMITED",
        },
    ]
    parsed = parse_benchmark_events(events)
    correlated = correlate_tool_calls(parsed)
    assert len(correlated) == 2
    assert correlated[0].is_fault_injected is True
    assert correlated[0].fault_class == "HTTP_500_INTERNAL_SERVER_ERROR"
    assert correlated[1].call_id == "call_1"
    assert correlated[1].is_fault_injected is True
    assert correlated[1].fault_class == "RATE_LIMITED"


def test_action_memory_zero_opportunity_null_preservation(tmp_path: Path):
    """Action memory trials with 0 opportunities must return None for survival and override rates."""
    trial_dir = tmp_path / "action_mem_zero"
    trial_dir.mkdir(parents=True)

    contract = {
        "family": "action-memory-v1",
        "version": "1.0.0",
        "construct": "state_binding_survival",
        "seed": 42,
        "cell_factors": {"cell_id": "mem_zero_test", "arm": "clean", "dose_bytes": 0},
        "task_id": "zero_opp_task",
        "opportunity_counts": {"mutation_opportunity_count": 0, "update_opportunity_count": 0},
        "verifier_truth_digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    }
    (trial_dir / "benchmark-contract.json").write_text(json.dumps(contract), encoding="utf-8")
    (trial_dir / "benchmark-events.jsonl").write_text("", encoding="utf-8")
    (trial_dir / "final-state.json").write_text(
        json.dumps({"invariants_passed": True}), encoding="utf-8"
    )

    bundle = load_trial_bundle(trial_dir)
    features = extract_action_memory_features(bundle)
    assert features.raw_binding_opportunities == 0
    assert features.raw_conflicting_opportunities == 0
    assert features.binding_survival_rate is None
    assert features.stale_value_override_rate is None


def test_mcp_funcdag_mixed_type_tool_arguments(tmp_path: Path):
    """MCP FuncDAG must gracefully serialize mixed or unorderable tool arguments."""
    trial_dir = tmp_path / "funcdag_mixed"
    trial_dir.mkdir(parents=True)

    contract = {
        "family": "mcp-funcdag-v1",
        "version": "1.0.0",
        "construct": "tool_call_dag_conformance",
        "seed": 7,
        "cell_factors": {"depth": 1, "width": 1},
        "task_id": "dag_mixed_task",
        "opportunity_counts": {"required_dag_edges": 0, "required_node_count": 1},
        "verifier_truth_digest": "sha256:7777777777777777777777777777777777777777777777777777777777777777",
    }
    events = [
        {
            "event_index": 1,
            "event_type": "mcp_call",
            "call_id": "c1",
            "tool_name": "mixed_tool",
            "arguments": {1: "num_key", "str_key": "val"},
        },
        {
            "event_index": 2,
            "event_type": "tool_executed",
            "call_id": "c1",
            "result": {"output": "ok"},
        },
    ]
    (trial_dir / "benchmark-contract.json").write_text(json.dumps(contract), encoding="utf-8")
    (trial_dir / "benchmark-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    (trial_dir / "final-state.json").write_text(
        json.dumps({"invariants_passed": True}), encoding="utf-8"
    )

    bundle = load_trial_bundle(trial_dir)
    features = extract_mcp_funcdag_features(bundle)
    assert features.total_tool_calls == 1
    assert features.schema_conformance_rate == 1.0


def test_mcp_funcdag_cycle_detection(tmp_path: Path):
    """MCP FuncDAG must detect cyclic tool dependencies as cycle violations."""
    trial_dir = tmp_path / "funcdag_cycle"
    trial_dir.mkdir(parents=True)

    contract = {
        "family": "mcp-funcdag-v1",
        "version": "1.0.0",
        "construct": "tool_call_dag_conformance",
        "seed": 42,
        "cell_factors": {"depth": 2, "width": 2},
        "task_id": "dag_cycle_task",
        "opportunity_counts": {"required_dag_edges": 2, "required_node_count": 2},
        "verifier_truth_digest": "sha256:8888888888888888888888888888888888888888888888888888888888888888",
    }
    # Node A produces 10 -> Node B takes 10 and produces 20 -> Node A takes 20 (cycle)
    events = [
        {
            "event_index": 0,
            "event_type": "tool_call_success",
            "tool_name": "node_a",
            "arguments": {"x": 1},
            "result": 10,
        },
        {
            "event_index": 1,
            "event_type": "tool_call_success",
            "tool_name": "node_b",
            "arguments": {"input": 10},
            "result": 20,
        },
        {
            "event_index": 2,
            "event_type": "tool_call_success",
            "tool_name": "node_a",
            "arguments": {"input": 20},
            "result": 30,
        },
    ]
    (trial_dir / "benchmark-contract.json").write_text(json.dumps(contract), encoding="utf-8")
    (trial_dir / "benchmark-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    (trial_dir / "final-state.json").write_text(
        json.dumps({"invariants_passed": False, "details": {"cycle_violations": 1}}),
        encoding="utf-8",
    )

    bundle = load_trial_bundle(trial_dir)
    features = extract_mcp_funcdag_features(bundle)
    assert features.cycle_violations >= 1


def test_extract_benchmark_features_unsupported_family(tmp_path: Path):
    """extract_benchmark_features must raise ValueError on unknown family."""
    trial_dir = tmp_path / "unknown_fam"
    trial_dir.mkdir(parents=True)

    contract = {
        "family": "nonexistent-benchmark-v99",
        "version": "1.0.0",
        "construct": "unknown",
        "seed": 1,
        "cell_factors": {},
        "task_id": "unknown_task",
        "opportunity_counts": {},
        "verifier_truth_digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    }
    (trial_dir / "benchmark-contract.json").write_text(json.dumps(contract), encoding="utf-8")
    (trial_dir / "benchmark-events.jsonl").write_text("", encoding="utf-8")
    (trial_dir / "final-state.json").write_text(
        json.dumps({"invariants_passed": True}), encoding="utf-8"
    )

    bundle = load_trial_bundle(trial_dir)
    from evallab.interpretation.producers import extract_benchmark_features

    with pytest.raises(ValueError, match="Unsupported benchmark family"):
        extract_benchmark_features(bundle)


def test_standalone_execution_and_rejection_events_correlation():
    """Standalone execution, rejection, and schema error events must create distinct correlated records."""
    events = [
        {
            "event_index": 0,
            "event_type": "tool_call_success",
            "tool_name": "step_a",
            "arguments": {"x": 1},
            "result": 10,
        },
        {
            "event_index": 1,
            "event_type": "tool_call_rejected",
            "tool_name": "step_b",
            "arguments": {"y": 2},
            "error": "unknown tool",
        },
        {
            "event_index": 2,
            "event_type": "tool_call_schema_error",
            "tool_name": "step_c",
            "arguments": {"z": 3},
            "error": "missing field",
        },
    ]
    parsed = parse_benchmark_events(events)
    correlated = correlate_tool_calls(parsed)
    assert len(correlated) == 3
    assert correlated[0].call_id == "exec_0"
    assert correlated[0].tool_name == "step_a"
    assert correlated[0].is_error is False
    assert correlated[0].result_payload == 10
    assert correlated[1].call_id == "err_1"
    assert correlated[1].tool_name == "step_b"
    assert correlated[1].is_error is True
    assert correlated[2].call_id == "err_2"
    assert correlated[2].tool_name == "step_c"
    assert correlated[2].is_error is True


def test_fault_followed_by_standalone_execution_no_mutation():
    """Standalone fault event followed by standalone execution event must not mutate fault into a tool call."""
    events = [
        {
            "event_index": 0,
            "event_type": "fault_injected",
            "payload": {"tool": "api_fetch", "fault_class": "TIMEOUT"},
        },
        {
            "event_index": 1,
            "event_type": "tool_executed",
            "payload": {"tool": "api_fetch", "state_digest": "sha256:xyz"},
        },
    ]
    parsed = parse_benchmark_events(events)
    correlated = correlate_tool_calls(parsed)
    assert len(correlated) == 2
    assert correlated[0].call_id == "fault_0"
    assert correlated[0].is_fault_injected is True
    assert correlated[0].is_error is True
    assert correlated[1].call_id == "exec_1"
    assert correlated[1].is_fault_injected is False
    assert correlated[1].execution_event is not None


def test_canonical_contract_adapter_preserves_legacy_payload_without_unit_inference():
    """The adapter exposes PR #268 types without coercing legacy byte factors into tokens."""
    legacy = parse_benchmark_contract(
        {
            "family": "action-memory-v1",
            "cell_factors": {"dose_bytes": 4096, "seed": 42},
        }
    )
    assert legacy.canonical_family is not None
    assert legacy.canonical_cell_factors is None

    canonical = parse_benchmark_contract(
        {
            "family": "family_b_funcdag_v2",
            "cell_factors": {
                "critical_path_depth": 3,
                "parallel_width": 2,
                "distractor_count": 1,
                "seed": 42,
            },
        }
    )
    assert canonical.canonical_family is not None
    assert canonical.canonical_cell_factors is not None
    assert canonical.canonical_cell_factors.critical_path_depth == 3


def test_canonical_family_a_adapter_uses_only_declared_canonical_fields():
    """Optional canonical CellFactorsA defaults must not require unrelated legacy fields."""
    canonical = parse_benchmark_contract(
        {
            "family": "family_a_state_inversion",
            "cell_factors": {"dilation_tokens": 4096, "seed": 42},
        }
    )
    assert canonical.canonical_cell_factors is not None
    assert canonical.canonical_cell_factors.dilation_tokens == 4096
    assert canonical.canonical_cell_factors.forced_compaction is False


def _quality_pass_report(bundle) -> ComplianceIngestReport:
    source_digest = "sha256:" + "a" * 64
    record = TrialComplianceRecord(
        disposition="QUALITY_PASS",
        analysis_ready=True,
        job_id="job-dimension",
        trial_id=bundle.trial_id,
        cas_uri="cas://sha256:" + "b" * 64,
        task_name="dimension-task",
        model_name="model-a",
        agent_name="agent-a",
        repeat_group_id="repeat-a",
        trial_source_digest=source_digest,
        evaluated_at="2026-08-28T00:00:00Z",
    )
    gates = ReadinessGates(
        job_id=record.job_id,
        trial_id=record.trial_id,
        cas_uri=record.cas_uri,
        model_name=record.model_name,
        agent_name=record.agent_name,
        task_name=record.task_name,
        repeat_eligible=True,
        sequence_eligible=True,
        dose_ready=True,
        alphabet_ready=True,
        t_lock_contract_present=True,
        censoring_available=True,
        gold_set_three_rater_ready=True,
        join_ready=True,
    )
    return ComplianceIngestReport(
        record=record,
        gates=gates,
        disposition="QUALITY_PASS",
        reasons=[],
        lag_ms=1,
        bloat_clean=True,
        report_digest="sha256:" + "c" * 64,
    )


def test_projection_dimensions_fail_closed_and_are_idempotent(action_memory_trial_dir: Path):
    """Missing Data dimensions refuse; complete QUALITY_PASS metadata yields stable identity."""
    bundle = load_trial_bundle(action_memory_trial_dir)
    refused = build_projection_dimensions(bundle, None)
    assert refused.analysis_ready is False
    assert "MISSING_COMPLIANCE_REPORT" in refused.refusals

    report = _quality_pass_report(bundle)
    metadata = {
        "harness_version": "harbor-v1",
        "scaffold_version": "scaffold-v1",
        "repeat_group_id": "repeat-a",
        "dose_axis": "context_bytes",
        "dose_value": 4096,
        "dose_unit": "bytes",
        "alphabet_id": "atif-actions",
        "alphabet_version": "v1",
    }
    first = build_projection_dimensions(bundle, report, metadata=metadata)
    second = build_projection_dimensions(bundle, report, metadata=metadata)
    assert first.analysis_ready is True
    assert first.projection_identity == second.projection_identity
    assert first.model_name == "model-a"

    backfilled = backfill_benchmark_projection_rows(
        [{"trial_id": bundle.trial_id, "source_digest": first.source_digest}],
        [first],
    )
    assert backfilled[0]["analysis_ready"] is True
    assert backfilled[0]["model_name"] == "model-a"

    research_gate_report = report.model_copy(
        update={
            "gates": report.gates.model_copy(
                update={"refusals": ["gold_set_three_rater_not_ready"]}
            )
        }
    )
    assert (
        build_projection_dimensions(bundle, research_gate_report, metadata=metadata).analysis_ready
        is True
    )


def test_recovery_persistence_dose_must_match_native_cell(mcp_recovery_trial_dir: Path):
    """Persistence-dose metadata is admitted only when it equals the native recovery cell level."""
    bundle = load_trial_bundle(mcp_recovery_trial_dir)
    report = _quality_pass_report(bundle)
    base_metadata = {
        "harness_version": "harbor-v1",
        "scaffold_version": "scaffold-v1",
        "repeat_group_id": "repeat-a",
        "dose_axis": "persistence_level",
        "dose_unit": "count",
        "alphabet_id": "atif-actions",
        "alphabet_version": "v1",
    }

    valid = build_projection_dimensions(bundle, report, metadata={**base_metadata, "dose_value": 1})
    assert valid.analysis_ready is True

    mismatched = build_projection_dimensions(
        bundle, report, metadata={**base_metadata, "dose_value": 2}
    )
    assert mismatched.analysis_ready is False
    assert "PERSISTENCE_DOSE_MISMATCH" in mismatched.refusals


@pytest.mark.parametrize(
    "invalid_level",
    [1.5, float("nan"), float("inf"), True, 0, -1],
    ids=["fractional", "nan", "infinite", "bool", "zero", "negative"],
)
def test_recovery_rejects_non_integral_or_invalid_native_persistence(
    mcp_recovery_trial_dir: Path, invalid_level
):
    """Malformed native persistence is rejected before any feature row can be stored."""
    contract_path = mcp_recovery_trial_dir / "benchmark_contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["cell_factors"]["persistence_levels"] = [invalid_level]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    bundle = load_trial_bundle(mcp_recovery_trial_dir)
    report = _quality_pass_report(bundle)
    dimensions = build_projection_dimensions(
        bundle,
        report,
        metadata={
            "harness_version": "harbor-v1",
            "scaffold_version": "scaffold-v1",
            "repeat_group_id": "repeat-a",
            "dose_axis": "persistence_level",
            "dose_value": 1,
            "dose_unit": "count",
            "alphabet_id": "atif-actions",
            "alphabet_version": "v1",
        },
    )
    assert dimensions.analysis_ready is False
    assert "MISSING_NATIVE_PERSISTENCE_LEVEL" in dimensions.refusals
    with pytest.raises(ValueError, match="finite positive integer"):
        extract_mcp_recovery_features(bundle)


@pytest.mark.parametrize(
    "invalid_dose",
    [1.5, float("nan"), float("inf"), True, 0, -1],
    ids=["fractional", "nan", "infinite", "bool", "zero", "negative"],
)
def test_recovery_persistence_axis_rejects_invalid_dose_value(
    mcp_recovery_trial_dir: Path, invalid_dose
):
    """Persistence-axis dose values must be finite positive integers equal to native persistence."""
    bundle = load_trial_bundle(mcp_recovery_trial_dir)
    dimensions = build_projection_dimensions(
        bundle,
        _quality_pass_report(bundle),
        metadata={
            "harness_version": "harbor-v1",
            "scaffold_version": "scaffold-v1",
            "repeat_group_id": "repeat-a",
            "dose_axis": "persistence_level",
            "dose_value": invalid_dose,
            "dose_unit": "count",
            "alphabet_id": "atif-actions",
            "alphabet_version": "v1",
        },
    )
    assert dimensions.analysis_ready is False
    assert "PERSISTENCE_DOSE_MISMATCH" in dimensions.refusals


def test_card_uses_same_projection_dimensions_as_provenance(
    action_memory_trial_dir: Path, tmp_path: Path
):
    """Card facts must not disagree with the materialized compliance provenance."""
    bundle = load_trial_bundle(action_memory_trial_dir)
    report = _quality_pass_report(bundle)
    metadata = {
        "harness_version": "harbor-v1",
        "scaffold_version": "scaffold-v1",
        "repeat_group_id": "repeat-a",
        "dose_axis": "context_bytes",
        "dose_value": 4096,
        "dose_unit": "bytes",
        "alphabet_id": "atif-actions",
        "alphabet_version": "v1",
    }
    dimensions = build_projection_dimensions(bundle, report, metadata=metadata)
    _rendered, card = generate_traj_card(
        action_memory_trial_dir,
        repo_root=tmp_path,
        runs_roots=[tmp_path],
        projection_dimensions=dimensions,
        projection_provenance=agent_readable_projection_provenance(report, dimensions),
    )
    assert card.benchmark_features is not None
    assert card.benchmark_features["analysis_ready"] is True
    assert card.benchmark_features["model_name"] == "model-a"


def test_benchmark_contrasts_do_not_cross_model_dimensions():
    """Matched contrasts must never join clean/treatment trials from different model strata."""
    con = duckdb.connect()
    con.execute(Path("sql/traj_benchmark_views.sql").read_text(encoding="utf-8"))
    statement = """
        INSERT INTO action_memory_features (
            trial_id, family, task_id, seed, cell_id, arm, dose_bytes, construct,
            causal_grade, task_success, total_tool_calls, model_call_count,
            raw_binding_opportunities, raw_conflicting_opportunities, binding_matched,
            stale_value_bound, citation, verifier_truth_digest, model_name, agent_name,
            task_name, harness_version, scaffold_version, repeat_group_id, dose_axis,
            dose_value, dose_unit, alphabet_id, alphabet_version, quality_status,
            report_digest, source_digest, producer_version, projection_identity,
            dimension_digest, projection_status, analysis_ready, projection_refusals
        ) VALUES (
            ?, 'action-memory-v1', 'task-id', 7, 'cell-a', ?, 4096, 'memory',
            'C1', true, 1, 1, 1, 1, true, false, 'cas:trial', 'sha256:truth',
            ?, 'agent-a', 'task-name', 'harness-v1', 'scaffold-v1', ?, 'context_bytes',
            4096, 'bytes', 'atif', 'v1', 'QUALITY_PASS', 'sha256:report',
            ?, 'benchmark-dimension-quality/v1', ?, ?, 'PROJECTED', true, ''
        )
    """
    for model, arm, trial in (
        ("model-a", "clean", "a-clean"),
        ("model-a", "treatment", "a-treatment"),
        ("model-b", "clean", "b-clean"),
        ("model-b", "treatment", "b-treatment"),
    ):
        con.execute(
            statement,
            [
                trial,
                arm,
                model,
                f"repeat-{model}",
                f"sha256:source-{trial}",
                f"sha256:projection-{trial}",
                f"sha256:dimension-{trial}",
            ],
        )
    rows = con.execute(
        "SELECT model_name, count(*) FROM v_benchmark_contrasts GROUP BY model_name ORDER BY model_name"
    ).fetchall()
    assert rows == [("model-a", 1), ("model-b", 1)]


def test_recovery_contrasts_require_same_native_persistence_level():
    """A clean p1 recovery trial can match fault p1, never fault p2."""
    con = duckdb.connect()
    con.execute(Path("sql/traj_benchmark_views.sql").read_text(encoding="utf-8"))
    statement = """
        INSERT INTO mcp_recovery_features (
            trial_id, family, task_id, seed, persistence_level, mode, task_success,
            total_tool_calls, model_call_count, injected_fault_count, fault_detected_count,
            post_fault_retries, blind_retries, certified_recovered_faults, citation,
            verifier_truth_digest, model_name, agent_name, task_name, harness_version,
            scaffold_version, repeat_group_id, dose_axis, dose_value, dose_unit,
            alphabet_id, alphabet_version, quality_status, report_digest, source_digest,
            producer_version, projection_identity, dimension_digest, projection_status,
            analysis_ready, projection_refusals
        ) VALUES (
            ?, 'mcp-recovery-v1', 'recovery-task', 7, ?, ?, true, 1, 1, 1, 1, 1,
            0, 1, 'cas:trial', 'sha256:truth', 'model-a', 'agent-a', 'task-name',
            'harness-v1', 'scaffold-v1', 'repeat-a', 'persistence_level', 1, 'count',
            'atif', 'v1', 'QUALITY_PASS', 'sha256:report', ?, 'producer-v1', ?, ?,
            'PROJECTED', true, ''
        )
    """
    for trial, persistence_level, mode in (
        ("clean-p1", 1, "clean"),
        ("fault-p1", 1, "fault"),
        ("fault-p2", 2, "fault"),
    ):
        con.execute(
            statement,
            [
                trial,
                persistence_level,
                mode,
                f"sha256:source-{trial}",
                f"sha256:projection-{trial}",
                f"sha256:dimension-{trial}",
            ],
        )
    rows = con.execute(
        "SELECT control_trial_id, treatment_trial_id FROM v_benchmark_contrasts "
        "WHERE family = 'mcp-recovery-v1'"
    ).fetchall()
    assert rows == [("clean-p1", "fault-p1")]
