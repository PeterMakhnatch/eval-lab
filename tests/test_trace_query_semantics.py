"""Behavioral regressions for the ten HAR-131 analytical query populations.

Synthetic Arrow rows exercise the declared transient interface, not raw runs.
The attachment/manifest verifier has its own tests; no fixture is run evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest

from evallab.trace_query import STEPS_ARROW_SCHEMA, TRIALS_ARROW_SCHEMA

SQL = Path(__file__).resolve().parents[1] / "sql" / "trace_queries.sql"


def _trial(trial_id: str, *, job_id: str = "job", **changes: Any) -> dict[str, Any]:
    row = {
        "job_id": job_id,
        "trial_id": trial_id,
        "job_name": f"job-{job_id}",
        "trial_name": f"trial-{trial_id}",
        "task_name": "synthetic-task",
        "card": "HAR-116",
        "arm": "original",
        "model_name": "synthetic-model",
        "scored": True,
        "raw_reward": 0.0,
        "counts_available": False,
        "counts_verdict": None,
        "counts_reasons_json": "[]",
        "counts_evidence_json": "[]",
        "labels_json": "[]",
        "source_job_dir": f"/fixture/{job_id}",
        "source_trial_dir": f"/fixture/{job_id}/{trial_id}",
        "report_path": None,
        "trajectory_available": False,
        "processed_available": False,
        "step_evidence_source": "synthetic",
        "first_edit_measure_available": False,
        "first_edit_evidence_source": None,
    }
    row.update(changes)
    return row


def _step(trial_id: str, *, job_id: str = "job", step_id: int = 1, **changes: Any) -> dict[str, Any]:
    row = {
        "job_id": job_id,
        "trial_id": trial_id,
        "document_id": "head",
        "step_id": step_id,
        "source_path": "agent/trajectory.json",
        "source_sha256": "sha256:synthetic-unit-fixture",
        "source": "agent",
        "is_copied_context": False,
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "tool_call_count": 1,
        "command_text": "pwd",
        "command_provenance": "recorded",
        "step_ref": f"head#{step_id}",
    }
    row.update(changes)
    return row


@pytest.fixture
def query_surface():
    connections = []

    def attach(trials, steps=()):
        conn = duckdb.connect(":memory:")
        connections.append(conn)
        conn.register("_fixture_trials", pa.Table.from_pylist(trials, schema=TRIALS_ARROW_SCHEMA))
        conn.register("_fixture_steps", pa.Table.from_pylist(list(steps), schema=STEPS_ARROW_SCHEMA))
        conn.execute("CREATE VIEW v_trace_trials AS SELECT * FROM _fixture_trials")
        conn.execute("CREATE VIEW v_trace_steps AS SELECT * FROM _fixture_steps")
        conn.execute(SQL.read_text(encoding="utf-8"))
        return conn

    yield attach
    for connection in connections:
        connection.close()


def _rows(conn, view: str):
    cursor = conn.execute(f"SELECT * FROM {view}")
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _one(conn, view: str):
    rows = _rows(conn, view)
    assert len(rows) == 1
    return rows[0]


def test_raw_scored_yield_and_counts_unknown_have_distinct_populations(query_surface):
    conn = query_surface([
        _trial("pass", raw_reward=1.0, counts_available=True, counts_verdict="counted_pass"),
        _trial("fail", counts_available=True, counts_verdict="counted_fail"),
        _trial("missing-reward", raw_reward=None, counts_available=True, counts_verdict="excluded"),
        _trial("unscored-reward", scored=False, raw_reward=1.0),
        _trial("unknown-scored", scored=None, raw_reward=1.0),
        _trial("unavailable-counts", raw_reward=1.0, counts_verdict="counted_pass"),
    ])
    row = _one(conn, "v_trace_cohort_raw")
    assert (row["n_total"], row["n_scored"], row["n_unscored"]) == (6, 3, 3)
    assert (row["n_raw_pass"], row["n_raw_fail"], row["n_reward_without_scored_status"]) == (2, 1, 2)
    assert row["raw_pass_rate_all_attempts"] == pytest.approx(0.3333)
    assert row["raw_pass_rate_scored"] == pytest.approx(0.6667)
    assert (row["n_counted_pass"], row["n_counted_fail"], row["n_excluded"], row["n_counts_unknown"]) == (1, 1, 1, 3)
    assert row["counted_pass_rate"] == pytest.approx(0.5)


def test_first_edit_null_requires_an_available_measurement_for_negative_signal(query_surface):
    measured = {"first_edit_measure_available": True, "first_edit_evidence_source": "traj_features.parquet"}
    conn = query_surface([
        _trial("edit", first_edit_step=8, **measured),
        _trial("no-signal", first_edit_step=None, **measured),
        _trial("outline-only", processed_available=True, outline_json=json.dumps({"agent_steps": 30, "status": "ok"})),
        _trial("invalid-edit", first_edit_step=-1, **measured),
    ])
    row = _one(conn, "v_trace_first_edit_vs_pass")
    assert (row["n_with_edit"], row["n_no_detected_signal"], row["n_unknown_evidence"]) == (1, 1, 2)
    assert row["n_edit_measure_available"] == 2
    assert row["avg_first_edit_step"] == 8


def test_test_access_denominator_includes_no_steps_and_keeps_native_pair_identity(query_surface):
    # These two distinct native pairs collide if identity is concatenated by '|'.
    trials = [
        _trial("c", job_id="a|b", raw_reward=1.0, counts_available=True, counts_verdict="excluded"),
        _trial("b|c", job_id="a", raw_reward=1.0),
        _trial("copied"),
        _trial("reconstructed"),
        _trial("missing"),
    ]
    conn = query_surface(trials, [
        _step("c", job_id="a|b", command_text="pytest tests/test_example.py"),
        _step("c", job_id="a|b", step_id=2, command_text="cat tests/test_example.py"),
        _step("copied", command_text="pytest", is_copied_context=True),
        _step("reconstructed", command_text="pytest", command_provenance="reconstructed"),
        _step("b|c", job_id="a", source="system", command_text="example: pytest"),
    ])
    row = _one(conn, "v_trace_test_path_access")
    assert (row["n_total_trials"], row["n_trials_with_steps"], row["n_trials_without_steps"]) == (5, 4, 1)
    assert (row["n_trials_with_test_access"], row["n_steps_with_test_access"]) == (1, 2)
    assert row["n_excluded_passes_with_test_access"] == 1
    assert row["observed_step_coverage_rate"] == pytest.approx(0.8)
    assert row["n_trials_with_recorded_commands"] == 1
    assert row["n_trials_with_reconstructed_commands"] == 1


def _token_flow(*, prompts=(100, 300), completions=(10, 30), edit_step=1, n_agent_steps=2):
    series = [
        {"step_id": index, "call_index": index, "prompt_tokens": prompt, "completion_tokens": completion}
        for index, (prompt, completion) in enumerate(zip(prompts, completions, strict=True), 1)
    ]
    return json.dumps({
        "schema": "token_flow/v1",
        "trajectory": {"n_agent_steps": n_agent_steps},
        "last_useful_edit": {"step_id": edit_step, "call_index": 1, "persists_in_final_diff": False},
        "prompt_series": series,
        # Producer shares can use agent_result totals. Neither these nor proxy
        # totals may replace the matching native metric population.
        "tokens_after_last_edit": {"input_tokens": 300, "output_tokens": 30, "share_input": 0.01, "totals_source": "agent_result"},
    })


def test_post_edit_shares_use_complete_same_native_population_not_proxy_totals(query_surface):
    conn = query_surface([
        _trial("matched", token_flow_json=_token_flow(), input_tokens=10000, output_tokens=1000),
        _trial("input-only", token_flow_json=_token_flow(prompts=(200, 200), completions=(None, 20)), input_tokens=20000),
        _trial("partial", token_flow_json=_token_flow(prompts=(500,), completions=(50,)), input_tokens=30000),
        _trial("no-edit", token_flow_json=_token_flow(edit_step=None), input_tokens=40000),
    ])
    row = _one(conn, "v_trace_post_edit_tokens")
    assert (row["n_input_matched"], row["n_output_matched"]) == (2, 1)
    assert (row["n_input_unmatched_or_missing"], row["n_output_unmatched_or_missing"]) == (2, 3)
    assert (row["matched_native_input_tokens"], row["matched_native_post_edit_input_tokens"]) == (800, 500)
    assert row["post_edit_input_share"] == pytest.approx(0.625)
    assert row["post_edit_output_share"] == pytest.approx(0.75)
    assert row["total_proxy_input_tokens"] == 100000


def test_repeated_call_indices_cannot_fake_complete_native_token_coverage(query_surface):
    flow = json.loads(_token_flow())
    flow["prompt_series"][1]["call_index"] = 1
    conn = query_surface([_trial("duplicate-index", token_flow_json=json.dumps(flow))])
    row = _one(conn, "v_trace_post_edit_tokens")
    assert row["n_input_matched"] == 0
    assert row["post_edit_input_share"] is None


def test_loop_prediction_unknown_is_not_none_or_a_rate_denominator(query_surface):
    conn = query_surface([
        _trial("claim", loop_kind="completion-claim"),
        _trial("repetition", loop_kind="repetition"),
        _trial("none", loop_kind="none"),
        _trial("absent", loop_kind=None),
        _trial("invalid", loop_kind="loop_kind: none"),
    ])
    row = _one(conn, "v_trace_loop_by_arm")
    assert (row["n_total"], row["n_classified"], row["n_unclassified"], row["n_invalid_prediction"]) == (5, 3, 2, 1)
    assert row["n_no_loop"] == 1
    assert row["prediction_coverage_rate"] == pytest.approx(0.6)
    assert row["claim_rate_classified"] == pytest.approx(0.3333)


def test_fetch_detection_reads_real_array_entries_and_exact_counts_reasons(query_surface):
    conn = query_surface([
        _trial("fetch", raw_reward=1.0, taint_json=json.dumps([{"kind": "upstream_fetch", "names_task_repo": False}]),
               counts_available=True, counts_verdict="excluded", counts_reasons_json='["copied_fix"]'),
        _trial("mention", taint_json=json.dumps([{"kind": "guard_reject", "upstream_fetch": False}]),
               counts_available=True, counts_verdict="excluded", counts_reasons_json='["not_pass_tainted"]'),
        _trial("assessed-empty", processed_available=True, decision_facts_json='{"fetches":[]}'),
        _trial("unobserved-empty", processed_available=True, decision_facts_json='{"fetches":[]}'),
        _trial("actual-fetch", decision_facts_json='{"fetches":[{"step":"head#3","command":"pip download remote-package"}]}'),
        _trial("false-fetch", decision_facts_json='{"fetches":[{"step":false,"command":false}]}'),
        _trial("tainted", raw_reward=1.0, counts_available=True, counts_verdict="excluded", counts_reasons_json='["pass_tainted"]'),
        _trial("unusable", raw_reward=1.0, counts_available=True, counts_verdict="excluded", counts_reasons_json='["task_not_usable"]'),
    ], [_step("assessed-empty")])
    row = _one(conn, "v_trace_fetch_exclusion_audit")
    assert (row["n_fetched"], row["n_no_detected_fetch_signal"], row["n_fetch_unknown"]) == (2, 1, 5)
    assert row["n_fetched_passed"] == 1
    assert (row["n_copied_fix_excluded"], row["n_pass_tainted_excluded"], row["n_task_not_usable_excluded"]) == (1, 1, 1)


def test_parse_counters_require_numbers_not_keys_and_keep_recorded_intersection_unknown(query_surface):
    acceptance = {
        "counts": {"false": 2, "true": 3, "unknown": 0},
        "provenance": {"recorded": 3, "inferred": 2, "reconstructed": 0},
        "agreement": {"agree_reject": 0, "agree_accept": 3},
    }
    conn = query_surface([
        _trial("shape-and-inferred", shape_counts_json='{"unparseable":2,"prose":3}',
               acceptance_json=json.dumps(acceptance), rejection_causes_json='{"head":{"bad_arguments":{"count":2,"first_step":4}}}'),
        _trial("zero-shape", shape_counts_json='{"unparseable":0,"prose":1}'),
        _trial("false-key", shape_counts_json='{"unparseable":false,"prose":1}',
               taint_json='[{"kind":"guard_reject","parse_error":false}]'),
        _trial("string-count", shape_counts_json='{"unparseable":"4"}'),
        _trial("absent", diagnosis_json='{"modes":[{"mode":"malformed_artifact"}],"parse_error":true}'),
    ])
    row = _one(conn, "v_trace_parse_rejection_evidence")
    assert (row["n_shape_observed"], row["n_shape_unknown"], row["n_unparseable_shape_runs"]) == (2, 3, 1)
    assert row["n_unparseable_shape_steps"] == 2
    assert row["unparseable_shape_run_rate_observed"] == pytest.approx(0.5)
    assert row["n_acceptance_false_steps"] == 2
    assert row["n_recorded_provenance_steps"] == 3
    assert row["n_inferred_provenance_steps"] == 2
    assert row["n_runs_with_recorded_agree_reject"] == 0
    assert row["n_complete_recorded_false_steps"] is None
    assert row["n_classified_rejection_cause_steps"] == 2


def _vote(trial_id: str, rater: str, kind: Any, *, cohort="har119"):
    return {
        "cohort": cohort, "rater": rater, "provenance": "agent_rater",
        "trial_name": f"trial-{trial_id}", "loop_kind": kind,
        "source_file": f"synthetic/{rater}/{trial_id}.json", "source_sha256": "synthetic-unit-fixture",
    }


def test_frozen_agreement_excludes_disagreement_abstention_duplicate_raters_and_wrong_scopes(query_surface):
    def labels(*entries):
        return json.dumps(entries)

    conn = query_surface([
        _trial("match", loop_kind="completion-claim", labels_json=labels(_vote("match", "a", "completion-claim"), _vote("match", "b", "completion-claim"))),
        _trial("mismatch", loop_kind="none", labels_json=labels(_vote("mismatch", "a", "repetition"), _vote("mismatch", "b", "repetition"))),
        _trial("disagree", loop_kind="none", labels_json=labels(_vote("disagree", "a", "none"), _vote("disagree", "b", "repetition"))),
        _trial("abstain", labels_json=labels(_vote("abstain", "a", "none"), _vote("abstain", "b", "none"))),
        _trial("one-rater", loop_kind="none", labels_json=labels(_vote("one-rater", "a", "none"), _vote("one-rater", "a", "none"))),
        _trial("wrong-scope", loop_kind="none", labels_json=labels({"cohort": "har128-sft-pass", "rater": None, "provenance": "frozen_adjudication", "label_scope": "sft_pass_cleanliness", "label": {"loop_kind": "none", "notes": "rater_a rater_b completion-claim repetition"}})),
        _trial("hand", loop_kind="none", labels_json=labels({"cohort": "har109", "rater": "hand", "provenance": "frozen_hand", "loop": {"present": False}})),
        _trial("absent", loop_kind="none"),
        _trial("ambiguous", loop_kind="none", labels_json=labels(_vote("ambiguous", "a", "none"), _vote("ambiguous", "b", "none"), _vote("ambiguous", "c", "none", cohort="different-freeze"))),
    ])
    row = _one(conn, "v_trace_frozen_label_agreement")
    assert (row["n_agreed"], row["n_disagreement"], row["n_insufficient_or_invalid_labels"], row["n_ambiguous_cohort"]) == (3, 1, 1, 1)
    assert (row["n_prediction_abstention"], row["eligibleN"], row["n_match"]) == (1, 2, 1)
    assert row["accuracy"] == pytest.approx(0.5)
    assert row["n_missing_loop_labels"] == 3
    assert (row["n_with_sft_labels"], row["n_with_hand_labels"]) == (1, 1)


def test_conflicting_duplicate_rater_or_incidental_nested_kind_never_forms_consensus(query_surface):
    conn = query_surface([
        _trial("duplicate-conflict", loop_kind="none", labels_json=json.dumps([
            _vote("duplicate-conflict", "a", "none"),
            _vote("duplicate-conflict", "a", "repetition"),
            _vote("duplicate-conflict", "b", "none"),
        ])),
        _trial("nested-only", loop_kind="none", labels_json=json.dumps([
            {"cohort": "har119", "rater": r, "provenance": "agent_rater", "trial_name": "trial-nested-only", "first_failure": {"loop_kind": "none"}}
            for r in ("a", "b")
        ])),
    ])
    row = _one(conn, "v_trace_frozen_label_agreement")
    assert row["n_disagreement"] == 1
    assert row["n_insufficient_or_invalid_labels"] == 1
    assert row["eligibleN"] == 0
    assert row["accuracy"] is None



def test_two_valid_cohorts_reported_separately_without_pooling(query_surface):
    """Two admitted cohorts on one native trial report distinct study results.

    Conflicting internally agreed kinds must never pool into cross-cohort
    disagreement or ambiguous_cohort, and each cohort must retain unknown and
    duplicate-rater protections independently.
    """
    conn = query_surface([
        _trial(
            "shared-conflict",
            loop_kind="none",
            labels_json=json.dumps([
                _vote("shared-conflict", "rater_a", "none", cohort="har119"),
                _vote("shared-conflict", "rater_b", "none", cohort="har119"),
                _vote("shared-conflict", "rater_a", "repetition", cohort="har128-har116"),
                _vote("shared-conflict", "rater_b", "repetition", cohort="har128-har116"),
            ]),
        ),
        _trial(
            "har128-dup-rater",
            loop_kind="none",
            labels_json=json.dumps([
                _vote("har128-dup-rater", "rater_a", "none", cohort="har128-har116"),
                _vote("har128-dup-rater", "rater_a", "none", cohort="har128-har116"),
            ]),
        ),
        _trial(
            "har128-dup-conflict",
            loop_kind="none",
            labels_json=json.dumps([
                _vote("har128-dup-conflict", "rater_a", "none", cohort="har128-har116"),
                _vote("har128-dup-conflict", "rater_a", "repetition", cohort="har128-har116"),
                _vote("har128-dup-conflict", "rater_b", "none", cohort="har128-har116"),
            ]),
        ),
        _trial(
            "har128-unknown-rater",
            loop_kind="none",
            labels_json=json.dumps([
                _vote("har128-unknown-rater", "", "none", cohort="har128-har116"),
                _vote("har128-unknown-rater", "rater_b", "none", cohort="har128-har116"),
            ]),
        ),
    ])

    rows = _rows(conn, "v_trace_frozen_label_agreement")
    assert len(rows) == 2
    by_cohort = {row["cohort"]: row for row in rows}
    assert sorted(by_cohort.keys()) == ["har119", "har128-har116"]

    assert by_cohort["har119"]["n_total"] == 4
    assert by_cohort["har128-har116"]["n_total"] == 4

    h119 = by_cohort["har119"]
    assert h119["n_agreed"] == 1
    assert h119["n_agreed_none"] == 1
    assert h119["n_agreed_repetition"] == 0
    assert h119["n_disagreement"] == 0
    assert h119["n_ambiguous_cohort"] == 0
    assert h119["n_missing_loop_labels"] == 3
    assert h119["eligibleN"] == 1
    assert h119["n_match"] == 1
    assert h119["accuracy"] == pytest.approx(1.0)

    h128 = by_cohort["har128-har116"]
    assert h128["n_agreed"] == 1
    assert h128["n_agreed_repetition"] == 1
    assert h128["n_agreed_none"] == 0
    assert h128["n_disagreement"] == 1
    assert h128["n_insufficient_or_invalid_labels"] == 2
    assert h128["n_ambiguous_cohort"] == 0
    assert h128["n_missing_loop_labels"] == 0
    assert h128["eligibleN"] == 1
    assert h128["n_match"] == 0
    assert h128["accuracy"] == pytest.approx(0.0)

def test_exemplars_do_not_call_passes_failures_and_only_link_resolved_native_anchors(query_surface):
    conn = query_surface([
        _trial("ordinary", raw_reward=1.0, counts_available=True, counts_verdict="counted_pass", loop_kind="repetition"),
        _trial("unknown-pass", raw_reward=1.0),
        _trial("task-unusable", raw_reward=1.0, counts_available=True, counts_verdict="excluded", counts_reasons_json='["task_not_usable"]'),
        _trial("tainted-pass", raw_reward=1.0, counts_available=True, counts_verdict="excluded", counts_reasons_json='["pass_tainted"]'),
        _trial("resolved", first_failure_ref="head#7", report_path="/fixture/processed/resolved.json"),
        _trial("unresolved", first_failure_ref="head#7", job_id="other-job"),
    ], [_step("resolved", step_id=7)])
    by_id = {row["trial_id"]: row for row in _rows(conn, "v_trace_candidate_exemplars")}
    assert by_id["ordinary"]["category"] == "counted_pass"
    assert by_id["unknown-pass"]["category"] == "raw_pass_counts_unknown"
    assert by_id["task-unusable"]["category"] == "task_not_usable"
    assert by_id["tainted-pass"]["category"] == "tainted_pass"
    assert by_id["resolved"]["step_ref"] == "head#7"
    assert by_id["resolved"]["trajectory_path"] == "/fixture/job/resolved/agent/trajectory.json"
    assert by_id["resolved"]["report_path"] == "/fixture/processed/resolved.json"
    assert by_id["unresolved"]["step_ref"] is None
    assert by_id["unresolved"]["trajectory_path"] is None
    assert by_id["unresolved"]["first_failure_opinion_ref"] == "head#7"


def test_completeness_distinguishes_file_presence_steps_labels_and_counts_known(query_surface):
    conn = query_surface([
        _trial("observed", trajectory_available=True, processed_available=True, counts_available=True, counts_verdict="counted_fail"),
        _trial("present-unreadable", trajectory_available=True, labels_json='[{"cohort":"har109","rater":"hand"}]'),
        _trial("absent", scored=False, raw_reward=None),
        _trial("invalid-counts", counts_available=True, counts_verdict="mystery", labels_json="not JSON"),
    ], [_step("observed")])
    row = _one(conn, "v_trace_evidence_completeness")
    assert (row["n_trials"], row["n_trajectory_available"], row["n_trials_with_observed_steps"]) == (4, 2, 1)
    assert row["n_trajectory_present_without_observed_steps"] == 1
    assert (row["n_counts_available"], row["n_counts_unknown"]) == (1, 3)
    assert (row["n_labels_available"], row["n_no_frozen_label_entries"], row["n_label_input_unknown"]) == (1, 3, 1)
    assert row["trajectory_file_coverage_rate"] == pytest.approx(0.5)
    assert row["observed_step_coverage_rate"] == pytest.approx(0.25)
