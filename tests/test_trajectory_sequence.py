"""Tests defending deterministic empirical trajectory sequence analysis."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

import pytest

from evallab.trajectory_sequence import (
    MotifSummary,
    NormalizedAction,
    ObservableMotif,
    TransitionAggregation,
    TransitionEdge,
    aggregate_transitions,
    detect_observable_motifs,
    extract_transition_edges,
    order_actions,
)


def test_ordering_by_ordinal_sequence_and_timestamp() -> None:
    """Actions within a trial must be ordered by ordinal, sequence, step_id, or timestamp."""
    raw_rows = [
        {"trial_id": "trial-1", "action_id": "a3", "ordinal": 3, "step_id": 30},
        {"trial_id": "trial-1", "action_id": "a1", "ordinal": 1, "step_id": 10},
        {"trial_id": "trial-1", "action_id": "a2", "ordinal": 2, "step_id": 20},
    ]
    ordered = order_actions(raw_rows)
    assert [a.action_id for a in ordered] == ["a1", "a2", "a3"]
    assert [a.ordinal for a in ordered] == [1, 2, 3]

    # Test ordering by timestamp when ordinal is missing
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

    # Verify no edge connects A2 -> B1
    for edge in edges:
        assert not (edge.source_action_id.startswith("A") and edge.target_action_id.startswith("B"))
        assert edge.source_action_id[0] == edge.target_action_id[0]


def test_eligible_opportunity_denominators_and_rates() -> None:
    """Aggregation must compute rate = count / opportunities, and return None when opportunities == 0."""
    edges = [
        TransitionEdge(
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

    # edit has 2 total outgoing opportunities: 1 to test (50%) and 1 to search (50%)
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
            "outcome": None,  # unexposed
            "action_family": "execute",
        },
    ]

    actions = order_actions(rows)
    assert actions[0].outcome == "unknown"
    assert actions[1].outcome == "unknown"

    motifs, summaries = detect_observable_motifs(actions)
    # Zero opportunities for error-based motifs because outcome is unknown
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
    # Opportunity 1: action 1 (error on bash) followed by action 2 (bash).
    # Opportunity 2: action 2 (error on bash) followed by action 3 (bash).
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
    # Errors followed by an action: step 1 (followed by 2), step 3 (followed by 4). Total opportunities = 2.
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
    # Mutation opportunities followed by next action: step 1, step 3. Total opportunities = 2.
    assert summary_map["verification_after_action"].opportunities == 2
    assert summary_map["verification_after_action"].occurrences == 1
    assert summary_map["verification_after_action"].rate == pytest.approx(0.5)


def test_post_terminal_action_motif() -> None:
    """Actions occurring strictly after a terminal action in the trial must be flagged."""
    rows = [
        {"trial_id": "t1", "action_id": "1", "ordinal": 1, "function_name": "edit"},
        {"trial_id": "t1", "action_id": "2", "ordinal": 2, "function_name": "submit", "is_terminal": True},
        {"trial_id": "t1", "action_id": "3", "ordinal": 3, "function_name": "search"},  # Leaked
        {"trial_id": "t1", "action_id": "4", "ordinal": 4, "function_name": "edit"},    # Leaked
    ]

    motifs, summaries = detect_observable_motifs(rows)
    post_term = [m for m in motifs if m.motif_type == "post_terminal_action"]
    assert len(post_term) == 2
    assert post_term[0].action_ids == ("3",)
    assert post_term[1].action_ids == ("4",)

    summary_map = {s.motif_type: s for s in summaries}
    assert summary_map["post_terminal_action"].occurrences == 2
    assert summary_map["post_terminal_action"].opportunities == 2


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

    # Shuffle and verify identity
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
        """Emulates a mapping returned by DuckDB/PyArrow."""
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
