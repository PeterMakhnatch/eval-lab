"""Comprehensive test suite for Benchmark Trajectory Program and Capability Observables.

Tests:
1. Strict benchmark event ingestion, canonical ordering, gap rejection, and call-ID correlation.
2. Benchmark capability feature producers (Action Memory, MCP FuncDAG, MCP Recovery) with strict NULL preservation.
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

from evallab.interpretation.benchmark_events import (
    BenchmarkEventDuplicateError,
    BenchmarkEventGapError,
    correlate_tool_calls,
    load_trial_bundle,
    parse_benchmark_events,
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def action_memory_trial_dir(tmp_path: Path) -> Path:
    trial_dir = tmp_path / "action_memory_pass"
    trial_dir.mkdir(parents=True)

    contract = {
        "family": "action-memory-v1",
        "task_name": "state_retention_test",
        "agent_name": "test_agent",
        "verifier_truth_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "construct": "state_binding_survival",
        "causal_grade": "L2_derived",
        "invariants": ["monotonic_events", "valid_bindings"],
    }
    (trial_dir / "benchmark_contract.json").write_text(json.dumps(contract), encoding="utf-8")

    events = [
        {
            "event_index": 1,
            "timestamp": "2026-08-28T10:00:00Z",
            "event_type": "tool_call_requested",
            "call_id": "call_1",
            "tool_name": "memory_read",
            "arguments": {"key": "session_id"},
        },
        {
            "event_index": 2,
            "timestamp": "2026-08-28T10:00:01Z",
            "event_type": "tool_call_executed",
            "call_id": "call_1",
            "tool_name": "memory_read",
            "result": {"value": "abc-123"},
        },
        {
            "event_index": 3,
            "timestamp": "2026-08-28T10:00:02Z",
            "event_type": "state_binding_matched",
            "call_id": "call_1",
            "binding_key": "session_id",
            "binding_value": "abc-123",
            "is_matched": True,
        },
    ]
    (trial_dir / "benchmark-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )

    final_state = {
        "trial_id": "action_memory_pass",
        "task_success": True,
        "primary_reward": 1.0,
        "state_bindings": {"session_id": "abc-123"},
        "unmatched_bindings": [],
        "stale_bindings": [],
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
        "task_name": "pipeline_dag_test",
        "agent_name": "test_agent",
        "verifier_truth_digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
        "construct": "tool_call_dag_conformance",
        "causal_grade": "L2_derived",
        "declared_dag_edges": [["step_a", "step_b"]],
    }
    (trial_dir / "benchmark_contract.json").write_text(json.dumps(contract), encoding="utf-8")

    events = [
        {
            "event_index": 1,
            "timestamp": "2026-08-28T10:00:00Z",
            "event_type": "tool_call_requested",
            "call_id": "call_1",
            "tool_name": "step_a",
        },
        {
            "event_index": 2,
            "timestamp": "2026-08-28T10:00:01Z",
            "event_type": "tool_call_executed",
            "call_id": "call_1",
            "tool_name": "step_a",
            "result": {"status": "done"},
        },
        {
            "event_index": 3,
            "timestamp": "2026-08-28T10:00:02Z",
            "event_type": "tool_call_requested",
            "call_id": "call_2",
            "tool_name": "step_b",
        },
        {
            "event_index": 4,
            "timestamp": "2026-08-28T10:00:03Z",
            "event_type": "tool_call_executed",
            "call_id": "call_2",
            "tool_name": "step_b",
            "result": {"status": "completed"},
        },
    ]
    (trial_dir / "benchmark-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )

    final_state = {
        "trial_id": "mcp_funcdag_pass",
        "task_success": True,
        "primary_reward": 1.0,
        "conformed_edges": [["step_a", "step_b"]],
        "cycle_violations": [],
        "undeclared_calls": [],
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
        "task_name": "resilience_recovery_test",
        "agent_name": "test_agent",
        "verifier_truth_digest": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
        "construct": "fault_recovery_rate",
        "causal_grade": "L2_derived",
    }
    (trial_dir / "benchmark_contract.json").write_text(json.dumps(contract), encoding="utf-8")

    events = [
        {
            "event_index": 1,
            "timestamp": "2026-08-28T10:00:00Z",
            "event_type": "tool_call_requested",
            "call_id": "call_1",
            "tool_name": "api_fetch",
        },
        {
            "event_index": 2,
            "timestamp": "2026-08-28T10:00:01Z",
            "event_type": "fault_injected",
            "call_id": "call_1",
            "tool_name": "api_fetch",
            "fault_type": "HTTP_503_SERVICE_UNAVAILABLE",
        },
        {
            "event_index": 3,
            "timestamp": "2026-08-28T10:00:02Z",
            "event_type": "tool_call_requested",
            "call_id": "call_2",
            "tool_name": "api_fetch",
            "arguments": {"retry": True},
        },
        {
            "event_index": 4,
            "timestamp": "2026-08-28T10:00:03Z",
            "event_type": "tool_call_executed",
            "call_id": "call_2",
            "tool_name": "api_fetch",
            "result": {"status": 200, "data": "recovered"},
        },
        {
            "event_index": 5,
            "timestamp": "2026-08-28T10:00:04Z",
            "event_type": "autonomous_recovery_observed",
            "fault_event_index": 2,
            "recovery_call_id": "call_2",
            "steps_to_recovery": 1,
        },
    ]
    (trial_dir / "benchmark-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )

    final_state = {
        "trial_id": "mcp_recovery_pass",
        "task_success": True,
        "primary_reward": 1.0,
        "faults_injected_count": 1,
        "autonomous_recoveries_count": 1,
        "unrecovered_faults_count": 0,
        "total_recovery_steps": 1,
        "post_recovery_regressions_count": 0,
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
    assert features.causal_grade == "L2_derived"
    assert features.construct == "state_binding_survival"


def test_mcp_funcdag_feature_extraction(mcp_funcdag_trial_dir: Path):
    """MCP FuncDAG producer computes dag conformance and cycle violations."""
    bundle = load_trial_bundle(mcp_funcdag_trial_dir)
    features = extract_mcp_funcdag_features(bundle, step_tokens=[100, 150])

    assert features.task_success is True
    assert features.required_dag_edges == 1
    assert features.executed_dag_edges == 1
    assert features.dag_edge_conformance_rate == 1.0
    assert features.redundant_tool_calls == 0
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


def test_null_preservation_on_zero_denominators(tmp_path: Path):
    """Zero-denominator rates must strictly return None / NULL, never 0.0 or divide-by-zero."""
    trial_dir = tmp_path / "zero_denoms"
    trial_dir.mkdir(parents=True)

    contract = {
        "family": "mcp-recovery-v1",
        "task_name": "no_faults_test",
        "agent_name": "clean_agent",
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

    assert "## 9. Benchmark Capability Observables (`action-memory-v1`)" in rendered
    assert "**Construct:** `state_binding_survival`" in rendered
    assert (
        "**Verifier Ground Truth Digest:** `sha256:1111111111111111111111111111111111111111111111111111111111111111`"
        in rendered
    )
    assert "`binding_survival_rate`" in rendered
