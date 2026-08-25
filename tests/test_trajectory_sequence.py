"""Tests defending deterministic empirical trajectory sequence analysis, schemas, and Parquet projections."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from evallab.trajectory_sequence import (
    TRAJECTORY_SEQUENCE_SCHEMAS,
    MotifSummary,
    NormalizedAction,
    ObservableMotif,
    TrajectorySequenceError,
    TransitionAggregation,
    TransitionEdge,
    aggregate_transitions,
    deterministic_edge_id,
    deterministic_motif_id,
    deterministic_summary_id,
    detect_observable_motifs,
    extract_transition_edges,
    load_trajectory_sequence_table,
    order_actions,
    project_trajectory_sequence_tables,
)


def test_reject_missing_trial_identity() -> None:
    """Rows lacking trial identity must be rejected immediately; never merged into unknown_trial."""
    bad_rows = [
        {"action_id": "a1", "step_id": 1, "action_family": "edit"},
    ]
    with pytest.raises(TrajectorySequenceError, match="missing required trial identity"):
        order_actions(bad_rows)

    bad_empty_trial = [
        {"trial_id": "   ", "action_id": "a1", "step_id": 1},
    ]
    with pytest.raises(TrajectorySequenceError, match="missing required trial identity"):
        order_actions(bad_empty_trial)


def test_require_unique_action_identity_and_derive_from_step_id() -> None:
    """Action identity must derive from explicit step_id when available, reject when absent, and reject duplicates."""
    rows_with_step = [
        {"trial_id": "t1", "step_id": 10, "ordinal": 1},
        {"trial_id": "t1", "step_id": 20, "ordinal": 2},
    ]
    actions = order_actions(rows_with_step)
    assert actions[0].action_id == "step_10"
    assert actions[1].action_id == "step_20"

    rows_no_id = [
        {"trial_id": "t1", "ordinal": 1, "action_family": "edit"},
    ]
    with pytest.raises(TrajectorySequenceError, match="lacks action identity"):
        order_actions(rows_no_id)

    duplicate_rows = [
        {"trial_id": "t1", "action_id": "duplicate_act", "ordinal": 1},
        {"trial_id": "t1", "action_id": "duplicate_act", "ordinal": 2},
    ]
    with pytest.raises(TrajectorySequenceError, match="Duplicate action_id"):
        order_actions(duplicate_rows)


def test_deterministic_sort_no_input_position_and_reject_duplicate_order_keys() -> None:
    """Sort must not depend on input index and must reject conflicting duplicate explicit order keys."""
    conflicting_rows = [
        {"trial_id": "t1", "action_id": "act_a", "ordinal": 1},
        {"trial_id": "t1", "action_id": "act_b", "ordinal": 1},
    ]
    with pytest.raises(TrajectorySequenceError, match="Conflicting duplicate order key"):
        order_actions(conflicting_rows)

    conflicting_steps = [
        {"trial_id": "t1", "action_id": "act_a", "step_id": 5},
        {"trial_id": "t1", "action_id": "act_b", "step_id": 5},
    ]
    with pytest.raises(TrajectorySequenceError, match="Conflicting duplicate order key"):
        order_actions(conflicting_steps)

    valid_rows = [
        {"trial_id": "t1", "action_id": "act_3", "ordinal": 3},
        {"trial_id": "t1", "action_id": "act_1", "ordinal": 1},
        {"trial_id": "t1", "action_id": "act_2", "ordinal": 2},
    ]
    res1 = [a.action_id for a in order_actions(valid_rows)]
    res2 = [a.action_id for a in order_actions(list(reversed(valid_rows)))]
    assert res1 == ["act_1", "act_2", "act_3"]
    assert res1 == res2


def test_require_consistent_cohort_keys_per_trial() -> None:
    """Actions within the same trial must carry identical cohort_keys; conflicting keys must be rejected."""
    inconsistent_cohort_rows = [
        {"trial_id": "t1", "action_id": "a1", "ordinal": 1, "model": "gpt-4"},
        {"trial_id": "t1", "action_id": "a2", "ordinal": 2, "model": "claude-3"},
    ]
    with pytest.raises(TrajectorySequenceError, match="Inconsistent cohort_keys within trial"):
        order_actions(inconsistent_cohort_rows, cohort_fields=["model"])

    consistent_rows = [
        {"trial_id": "t1", "action_id": "a1", "ordinal": 1, "model": "gpt-4"},
        {"trial_id": "t1", "action_id": "a2", "ordinal": 2, "model": "gpt-4"},
    ]
    ordered = order_actions(consistent_rows, cohort_fields=["model"])
    assert ordered[0].cohort_keys == (("model", "gpt-4"),)
    assert ordered[1].cohort_keys == (("model", "gpt-4"),)


def test_post_terminal_leakage_episode_non_tautological_opportunity() -> None:
    """Post-terminal motif represents 1 leakage episode per terminal boundary, non-tautological opportunity."""
    rows = [
        # Trial 1 (clean)
        {"trial_id": "t1", "action_id": "t1_a1", "step_id": 1, "ordinal": 1, "function_name": "edit", "model": "m1"},
        {"trial_id": "t1", "action_id": "t1_a2", "step_id": 2, "ordinal": 2, "function_name": "submit", "is_terminal": True, "model": "m1"},
        # Trial 2 (leaky)
        {"trial_id": "t2", "action_id": "t2_a1", "step_id": 1, "ordinal": 1, "function_name": "edit", "model": "m1"},
        {"trial_id": "t2", "action_id": "t2_a2", "step_id": 2, "ordinal": 2, "function_name": "submit", "is_terminal": True, "model": "m1"},
        {"trial_id": "t2", "action_id": "t2_a3", "step_id": 3, "ordinal": 3, "function_name": "search", "model": "m1"},
        {"trial_id": "t2", "action_id": "t2_a4", "step_id": 4, "ordinal": 4, "function_name": "edit", "model": "m1"},
    ]

    motifs, summaries = detect_observable_motifs(rows, cohort_fields=["model"])

    post_term_motifs = [m for m in motifs if m.motif_type == "post_terminal_action"]
    assert len(post_term_motifs) == 1
    leakage = post_term_motifs[0]
    assert leakage.trial_id == "t2"
    assert leakage.action_ids == ("t2_a3", "t2_a4")
    assert leakage.step_ids == (3, 4)
    details_dict = dict(leakage.details)
    assert details_dict["terminal_action_id"] == "t2_a2"
    assert details_dict["leaked_action_count"] == "2"

    summary_map = {s.motif_type: s for s in summaries}
    assert summary_map["post_terminal_action"].opportunities == 2
    assert summary_map["post_terminal_action"].occurrences == 1
    assert summary_map["post_terminal_action"].rate == pytest.approx(0.5)


def test_ordering_by_timestamp_when_ordinal_missing() -> None:
    """Actions with distinct timestamps must order deterministically."""
    ts_rows = [
        {"trial_id": "trial-1", "action_id": "late", "timestamp": "2026-08-25T12:05:00Z"},
        {"trial_id": "trial-1", "action_id": "early", "timestamp": "2026-08-25T12:01:00Z"},
        {"trial_id": "trial-1", "action_id": "mid", "timestamp": "2026-08-25T12:03:00Z"},
    ]
    ordered_ts = order_actions(ts_rows)
    assert [a.action_id for a in ordered_ts] == ["early", "mid", "late"]


def test_trial_boundary_isolation() -> None:
    """Transitions and ordering must strictly isolate trials; no edge may cross trial boundaries."""
    rows = [
        {"trial_id": "trial-A", "action_id": "A1", "ordinal": 1, "action_family": "edit"},
        {"trial_id": "trial-A", "action_id": "A2", "ordinal": 2, "action_family": "test"},
        {"trial_id": "trial-B", "action_id": "B1", "ordinal": 1, "action_family": "search"},
        {"trial_id": "trial-B", "action_id": "B2", "ordinal": 2, "action_family": "edit"},
    ]

    edges = extract_transition_edges(rows)
    assert len(edges) == 2

    edge_trials = [e.trial_id for e in edges]
    assert edge_trials == ["trial-A", "trial-B"]

    for edge in edges:
        assert not (edge.source_action_id.startswith("A") and edge.target_action_id.startswith("B"))
        assert edge.source_action_id[0] == edge.target_action_id[0]


def test_eligible_opportunity_denominators_and_rates() -> None:
    """Aggregation must compute rate = count / opportunities, and return None when opportunities == 0."""
    edges = [
        TransitionEdge(
            edge_id="e1",
            trial_id="trial-1",
            source_action_id="a1",
            source_step_id=1,
            target_action_id="a2",
            target_step_id=2,
            from_type="edit",
            to_type="test",
            transition_type="edit->test",
            source_outcome="success",
            target_outcome="success",
            cohort_keys=(("model", "gpt-4"),),
        ),
        TransitionEdge(
            edge_id="e2",
            trial_id="trial-1",
            source_action_id="a2",
            source_step_id=2,
            target_action_id="a3",
            target_step_id=3,
            from_type="edit",
            to_type="search",
            transition_type="edit->search",
            source_outcome="success",
            target_outcome="success",
            cohort_keys=(("model", "gpt-4"),),
        ),
    ]

    aggs = aggregate_transitions(edges)
    assert len(aggs) == 2

    agg_dict = {a.transition_type: a for a in aggs}
    assert agg_dict["edit->test"].count == 1
    assert agg_dict["edit->test"].opportunities == 2
    assert agg_dict["edit->test"].rate == pytest.approx(0.5)

    assert agg_dict["edit->search"].count == 1
    assert agg_dict["edit->search"].opportunities == 2
    assert agg_dict["edit->search"].rate == pytest.approx(0.5)


def test_missing_evidence_and_unknown_preservation() -> None:
    """Missing or unknown evidence must be preserved and not conflated with zero or success."""
    rows = [
        {
            "trial_id": "trial-1",
            "action_id": "a1",
            "ordinal": 1,
            "outcome": "unknown",
            "action_family": "execute",
        },
        {
            "trial_id": "trial-1",
            "action_id": "a2",
            "ordinal": 2,
            "outcome": None,
            "action_family": "execute",
        },
    ]

    actions = order_actions(rows)
    assert actions[0].outcome == "unknown"
    assert actions[1].outcome == "unknown"

    motifs, summaries = detect_observable_motifs(actions)
    summary_map = {s.motif_type: s for s in summaries}
    assert summary_map["recovery_after_failure"].opportunities == 0
    assert summary_map["recovery_after_failure"].rate is None
    assert summary_map["recovery_after_failure"].unknown_evidence_count > 0


def test_repeated_tool_failure_motif() -> None:
    """Consecutive errors on the same tool must be detected with exact opportunities."""
    rows = [
        {"trial_id": "t1", "action_id": "1", "ordinal": 1, "function_name": "bash", "outcome": "error"},
        {"trial_id": "t1", "action_id": "2", "ordinal": 2, "function_name": "bash", "outcome": "error"},
        {"trial_id": "t1", "action_id": "3", "ordinal": 3, "function_name": "bash", "outcome": "success"},
    ]

    motifs, summaries = detect_observable_motifs(rows)
    repeat_motifs = [m for m in motifs if m.motif_type == "repeated_tool_failure"]
    assert len(repeat_motifs) == 1
    assert repeat_motifs[0].step_ids == (None, None)
    assert repeat_motifs[0].action_ids == ("1", "2")

    summary_map = {s.motif_type: s for s in summaries}
    assert summary_map["repeated_tool_failure"].opportunities == 2
    assert summary_map["repeated_tool_failure"].occurrences == 1
    assert summary_map["repeated_tool_failure"].rate == pytest.approx(0.5)


def test_recovery_after_failure_motif() -> None:
    """Error followed immediately by success must be detected."""
    rows = [
        {"trial_id": "t1", "action_id": "1", "ordinal": 1, "function_name": "edit", "outcome": "error"},
        {"trial_id": "t1", "action_id": "2", "ordinal": 2, "function_name": "test", "outcome": "success"},
        {"trial_id": "t1", "action_id": "3", "ordinal": 3, "function_name": "edit", "outcome": "error"},
        {"trial_id": "t1", "action_id": "4", "ordinal": 4, "function_name": "edit", "outcome": "error"},
    ]

    motifs, summaries = detect_observable_motifs(rows)
    recovery_motifs = [m for m in motifs if m.motif_type == "recovery_after_failure"]
    assert len(recovery_motifs) == 1
    assert recovery_motifs[0].action_ids == ("1", "2")

    summary_map = {s.motif_type: s for s in summaries}
    assert summary_map["recovery_after_failure"].opportunities == 2
    assert summary_map["recovery_after_failure"].occurrences == 1
    assert summary_map["recovery_after_failure"].rate == pytest.approx(0.5)


def test_verification_after_action_motif() -> None:
    """Mutation action followed by verification action must be detected."""
    rows = [
        {"trial_id": "t1", "action_id": "1", "ordinal": 1, "action_family": "edit", "intent": "mutation"},
        {"trial_id": "t1", "action_id": "2", "ordinal": 2, "action_family": "test", "intent": "verification"},
        {"trial_id": "t1", "action_id": "3", "ordinal": 3, "action_family": "edit", "intent": "mutation"},
        {"trial_id": "t1", "action_id": "4", "ordinal": 4, "action_family": "edit", "intent": "mutation"},
    ]

    motifs, summaries = detect_observable_motifs(rows)
    verify_motifs = [m for m in motifs if m.motif_type == "verification_after_action"]
    assert len(verify_motifs) == 1
    assert verify_motifs[0].action_ids == ("1", "2")

    summary_map = {s.motif_type: s for s in summaries}
    assert summary_map["verification_after_action"].opportunities == 2
    assert summary_map["verification_after_action"].occurrences == 1
    assert summary_map["verification_after_action"].rate == pytest.approx(0.5)


def test_deterministic_output_under_shuffling() -> None:
    """Output ordering and aggregations must be identical regardless of input order."""
    base_rows = [
        {"trial_id": "t1", "action_id": f"a{i}", "ordinal": i, "step_id": i, "action_family": "edit" if i % 2 == 0 else "test"}
        for i in range(1, 11)
    ] + [
        {"trial_id": "t2", "action_id": f"b{i}", "ordinal": i, "step_id": i, "action_family": "search" if i % 2 == 0 else "execute"}
        for i in range(1, 11)
    ]

    res_edges_1 = extract_transition_edges(base_rows)
    res_motifs_1, res_summ_1 = detect_observable_motifs(base_rows)

    shuffled = list(base_rows)
    rng = random.Random(42)
    rng.shuffle(shuffled)

    res_edges_2 = extract_transition_edges(shuffled)
    res_motifs_2, res_summ_2 = detect_observable_motifs(shuffled)

    assert res_edges_1 == res_edges_2
    assert res_motifs_1 == res_motifs_2
    assert res_summ_1 == res_summ_2


def test_provenance_and_step_ids_retention() -> None:
    """Step IDs, document IDs, and source paths must be preserved in edges and motifs."""
    rows = [
        {
            "trial_id": "trial-x",
            "action_id": "act-10",
            "step_id": 10,
            "ordinal": 1,
            "document_id": "doc-abc",
            "source_path": "/data/atif.json",
            "function_name": "bash",
            "outcome": "error",
        },
        {
            "trial_id": "trial-x",
            "action_id": "act-11",
            "step_id": 11,
            "ordinal": 2,
            "document_id": "doc-abc",
            "source_path": "/data/atif.json",
            "function_name": "bash",
            "outcome": "success",
        },
    ]

    edges = extract_transition_edges(rows, type_field="function_name")
    assert len(edges) == 1
    edge = edges[0]
    assert edge.source_step_id == 10
    assert edge.target_step_id == 11
    prov_dict = dict(edge.provenance)
    assert prov_dict.get("document_id") == "doc-abc"
    assert prov_dict.get("source_path") == "/data/atif.json"

    motifs, _ = detect_observable_motifs(rows)
    recovery = [m for m in motifs if m.motif_type == "recovery_after_failure"][0]
    assert recovery.step_ids == (10, 11)
    assert dict(recovery.provenance).get("document_id") == "doc-abc"


def test_duckdb_and_parquet_row_compatibility() -> None:
    """Mappings directly derived from DuckDB or PyArrow dict rows must process without issue."""
    class DuckDBRow(dict):
        pass

    row1 = DuckDBRow({
        "trial_id": "t-duck",
        "action_id": "act-1",
        "sequence": 1,
        "step_id": 100,
        "tool_name": "file_editor",
        "family": "edit",
        "exit_code": 0,
        "outcome": "success",
        "is_terminal": False,
    })
    row2 = DuckDBRow({
        "trial_id": "t-duck",
        "action_id": "act-2",
        "sequence": 2,
        "step_id": 101,
        "tool_name": "pytest",
        "family": "test",
        "exit_code": 0,
        "outcome": "success",
        "is_terminal": False,
    })

    actions = order_actions([row1, row2])
    assert len(actions) == 2
    assert actions[0].action_family == "edit"
    assert actions[1].action_family == "test"

    edges = extract_transition_edges([row1, row2])
    assert len(edges) == 1
    assert edges[0].transition_type == "edit->test"


# --- PyArrow Schema and Atomic Parquet Projection Tests -----------------------


def test_pyarrow_schemas_structure() -> None:
    """Schemas for action edges, observable motifs, and summaries must match required definitions."""
    edge_schema = TRAJECTORY_SEQUENCE_SCHEMAS["action_transition_edges"]
    assert edge_schema.field("edge_id").type == pa.string()
    assert not edge_schema.field("edge_id").nullable
    assert edge_schema.field("trial_id").type == pa.string()
    assert not edge_schema.field("trial_id").nullable
    assert edge_schema.field("source_step_id").nullable
    assert edge_schema.field("source_step_id").type == pa.int64()

    motif_schema = TRAJECTORY_SEQUENCE_SCHEMAS["observable_motifs"]
    assert motif_schema.field("motif_id").type == pa.string()
    assert not motif_schema.field("motif_id").nullable
    assert motif_schema.field("motif_type").type == pa.string()
    assert not motif_schema.field("motif_type").nullable

    summary_schema = TRAJECTORY_SEQUENCE_SCHEMAS["motif_summaries"]
    assert summary_schema.field("summary_id").type == pa.string()
    assert not summary_schema.field("summary_id").nullable
    assert summary_schema.field("rate").type == pa.float64()
    assert summary_schema.field("rate").nullable  # Must remain nullable for unexposed rates


def test_deterministic_primary_keys_and_collision_resistance() -> None:
    """Primary keys must be deterministic and identical for identical inputs."""
    k1 = deterministic_edge_id("t1", "a1", "a2", "edit->test")
    k2 = deterministic_edge_id("t1", "a1", "a2", "edit->test")
    k3 = deterministic_edge_id("t1", "a1", "a2", "edit->search")
    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 64

    m1 = deterministic_motif_id("t1", "recovery_after_failure", ["a1", "a2"])
    m2 = deterministic_motif_id("t1", "recovery_after_failure", ["a1", "a2"])
    m3 = deterministic_motif_id("t1", "repeated_tool_failure", ["a1", "a2"])
    assert m1 == m2
    assert m1 != m3

    s1 = deterministic_summary_id('{"model": "gpt-4"}', "recovery_after_failure")
    s2 = deterministic_summary_id('{"model": "gpt-4"}', "recovery_after_failure")
    s3 = deterministic_summary_id('{"model": "gpt-4"}', "repeated_tool_failure")
    assert s1 == s2
    assert s1 != s3


def test_parquet_projection_round_trip_and_atomic_replacement(tmp_path: Path) -> None:
    """Atomic Parquet projections must write valid tables, replace atomically, and round-trip cleanly."""
    rows = [
        {
            "trial_id": "trial-1",
            "action_id": "act-1",
            "step_id": 1,
            "ordinal": 1,
            "function_name": "bash",
            "action_family": "execute",
            "outcome": "error",
            "exit_code": 1,
            "model": "model-v1",
            "document_id": "doc-1",
            "source_path": "/path/atif.json",
        },
        {
            "trial_id": "trial-1",
            "action_id": "act-2",
            "step_id": 2,
            "ordinal": 2,
            "function_name": "bash",
            "action_family": "execute",
            "outcome": "success",
            "exit_code": 0,
            "model": "model-v1",
            "document_id": "doc-1",
            "source_path": "/path/atif.json",
        },
        {
            "trial_id": "trial-1",
            "action_id": "act-3",
            "step_id": 3,
            "ordinal": 3,
            "function_name": "submit",
            "action_family": "other",
            "is_terminal": True,
            "outcome": "success",
            "model": "model-v1",
            "document_id": "doc-1",
            "source_path": "/path/atif.json",
        },
    ]

    out_tables = project_trajectory_sequence_tables(
        rows,
        output_dir=tmp_path,
        cohort_fields=["model"],
    )

    assert out_tables["action_transition_edges"].is_file()
    assert out_tables["observable_motifs"].is_file()
    assert out_tables["motif_summaries"].is_file()

    # Load back with load_trajectory_sequence_table
    edges_table = load_trajectory_sequence_table(out_tables["action_transition_edges"])
    assert edges_table.schema == TRAJECTORY_SEQUENCE_SCHEMAS["action_transition_edges"]
    assert len(edges_table) == 2  # act-1 -> act-2, act-2 -> act-3

    motifs_table = load_trajectory_sequence_table(out_tables["observable_motifs"])
    assert motifs_table.schema == TRAJECTORY_SEQUENCE_SCHEMAS["observable_motifs"]
    motif_rows = motifs_table.to_pylist()
    assert any(m["motif_type"] == "recovery_after_failure" for m in motif_rows)

    summaries_table = load_trajectory_sequence_table(out_tables["motif_summaries"])
    assert summaries_table.schema == TRAJECTORY_SEQUENCE_SCHEMAS["motif_summaries"]
    summary_rows = summaries_table.to_pylist()
    assert len(summary_rows) == 4

    # Check that rates are correctly typed and nullable when opportunities == 0
    rec_summary = next(s for s in summary_rows if s["motif_type"] == "recovery_after_failure")
    assert rec_summary["opportunities"] == 1
    assert rec_summary["occurrences"] == 1
    assert rec_summary["rate"] == pytest.approx(1.0)

    repeat_summary = next(s for s in summary_rows if s["motif_type"] == "repeated_tool_failure")
    assert repeat_summary["opportunities"] == 1
    assert repeat_summary["occurrences"] == 0
    assert repeat_summary["rate"] == pytest.approx(0.0)

    # Re-project to verify atomic idempotency and byte-stability
    bytes_before = out_tables["action_transition_edges"].read_bytes()
    out_tables_2 = project_trajectory_sequence_tables(
        rows,
        output_dir=tmp_path,
        cohort_fields=["model"],
    )
    bytes_after = out_tables_2["action_transition_edges"].read_bytes()
    assert bytes_before == bytes_after
