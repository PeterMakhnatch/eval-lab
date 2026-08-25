"""Tests defending deterministic empirical trajectory sequence analysis and strict invariants."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

import pytest

from evallab.trajectory_sequence import (
    MotifSummary,
    NormalizedAction,
    ObservableMotif,
    TrajectorySequenceError,
    TransitionAggregation,
    TransitionEdge,
    aggregate_transitions,
    detect_observable_motifs,
    extract_transition_edges,
    order_actions,
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
    # 1. Derivation from explicit step_id
    rows_with_step = [
        {"trial_id": "t1", "step_id": 10, "ordinal": 1},
        {"trial_id": "t1", "step_id": 20, "ordinal": 2},
    ]
    actions = order_actions(rows_with_step)
    assert actions[0].action_id == "step_10"
    assert actions[1].action_id == "step_20"

    # 2. Reject when both action_id and step_id are absent
    rows_no_id = [
        {"trial_id": "t1", "ordinal": 1, "action_family": "edit"},
    ]
    with pytest.raises(TrajectorySequenceError, match="lacks action identity"):
        order_actions(rows_no_id)

    # 3. Reject duplicate action_id within same trial
    duplicate_rows = [
        {"trial_id": "t1", "action_id": "duplicate_act", "ordinal": 1},
        {"trial_id": "t1", "action_id": "duplicate_act", "ordinal": 2},
    ]
    with pytest.raises(TrajectorySequenceError, match="Duplicate action_id"):
        order_actions(duplicate_rows)


def test_deterministic_sort_no_input_position_and_reject_duplicate_order_keys() -> None:
    """Sort must not depend on input index and must reject conflicting duplicate explicit order keys."""
    # 1. Rejection of conflicting duplicate explicit order keys (e.g. two actions claiming ordinal 1 in same trial)
    conflicting_rows = [
        {"trial_id": "t1", "action_id": "act_a", "ordinal": 1},
        {"trial_id": "t1", "action_id": "act_b", "ordinal": 1},
    ]
    with pytest.raises(TrajectorySequenceError, match="Conflicting duplicate order key"):
        order_actions(conflicting_rows)

    # 2. Rejection of conflicting duplicate explicit step_id order keys without ordinal
    conflicting_steps = [
        {"trial_id": "t1", "action_id": "act_a", "step_id": 5},
        {"trial_id": "t1", "action_id": "act_b", "step_id": 5},
    ]
    with pytest.raises(TrajectorySequenceError, match="Conflicting duplicate order key"):
        order_actions(conflicting_steps)

    # 3. Valid distinct ordinals sort deterministically regardless of input order
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
    # Cohort has 2 trials:
    # Trial 1: Clean termination at step 2, no post-terminal actions.
    # Trial 2: Terminal action at step 2, but leaked actions at step 3 and 4.
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

    # 1. ObservableMotif for post-terminal action spans all subsequent leaked actions
    post_term_motifs = [m for m in motifs if m.motif_type == "post_terminal_action"]
    assert len(post_term_motifs) == 1
    leakage = post_term_motifs[0]
    assert leakage.trial_id == "t2"
    assert leakage.action_ids == ("t2_a3", "t2_a4")
    assert leakage.step_ids == (3, 4)
    details_dict = dict(leakage.details)
    assert details_dict["terminal_action_id"] == "t2_a2"
    assert details_dict["leaked_action_count"] == "2"

    # 2. Non-tautological opportunity counting:
    # 2 trials reached terminal boundary -> opportunities = 2.
    # 1 trial exhibited post-terminal leakage -> occurrences = 1.
    # Leakage rate = 1 / 2 = 0.50 (50%).
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
