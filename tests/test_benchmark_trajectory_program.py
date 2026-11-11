"""Stored benchmark contrasts preserve model, harness, and dose strata."""

from pathlib import Path

import duckdb


def test_benchmark_contrasts_do_not_cross_model_dimensions():
    """Matched contrasts must never join clean/treatment trials from different model strata."""
    con = duckdb.connect()
    con.execute(Path("sql/traj_benchmark_views.sql").read_text(encoding="utf-8"))
    statement = """
        INSERT INTO action_memory_features (
            trial_id, family, task_id, seed, cell_id, arm, dose_bytes, construct,
            causal_grade, task_success, total_tool_calls, model_call_count,
            raw_binding_opportunities, raw_conflicting_opportunities, binding_matched,
            stale_value_bound, expected_handle_count, valid_handle_count, unknown_handle_count,
            duplicate_handle_count, handle_set_match, handle_order_match, handle_coverage_rate,
            citation, verifier_truth_digest, model_name, agent_name,
            task_name, harness_version, scaffold_version, repeat_group_id, dose_axis,
            dose_value, dose_unit, alphabet_id, alphabet_version, quality_status,
            report_digest, source_digest, producer_version, projection_identity,
            dimension_digest, projection_status, analysis_ready, projection_refusals
        ) VALUES (
            ?, 'action-memory-v1', 'task-id', 7, 'cell-a', ?, 4096, 'memory',
            'C1', true, 1, 1, 1, 1, true, false, 3, 3, 0, 0, true, true, 1.0, 'cas:trial', 'sha256:truth',
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


def test_c1_matched_contrasts_reject_mismatched_harness_or_dose_strata():
    """C1 matched contrasts must reject pairs with mismatched harness_version or mismatched dose_value."""
    con = duckdb.connect()
    con.execute(Path("sql/traj_benchmark_views.sql").read_text(encoding="utf-8"))
    statement = """
        INSERT INTO action_memory_features (
            trial_id, family, task_id, seed, cell_id, arm, dose_bytes, construct,
            causal_grade, task_success, total_tool_calls, model_call_count,
            raw_binding_opportunities, raw_conflicting_opportunities, binding_matched,
            stale_value_bound, expected_handle_count, valid_handle_count, unknown_handle_count,
            duplicate_handle_count, handle_set_match, handle_order_match, handle_coverage_rate,
            citation, verifier_truth_digest, model_name, agent_name,
            task_name, harness_version, scaffold_version, repeat_group_id, dose_axis,
            dose_value, dose_unit, alphabet_id, alphabet_version, quality_status,
            report_digest, source_digest, producer_version, projection_identity,
            dimension_digest, projection_status, analysis_ready, projection_refusals
        ) VALUES (
            ?, 'action-memory-v1', 'task-id', 7, 'cell-a', ?, 4096, 'memory',
            'C1', true, 1, 1, 1, 1, true, false, 3, 3, 0, 0, true, true, 1.0, 'cas:trial', 'sha256:truth',
            'model-a', 'agent-a', 'task-name', ?, 'scaffold-v1', 'repeat-a', 'context_bytes',
            ?, 'bytes', 'atif', 'v1', 'QUALITY_PASS', 'sha256:report',
            ?, 'benchmark-dimension-quality/v1', ?, ?, 'PROJECTED', true, ''
        )
    """
    # Insert control clean: harness v1, dose 4096
    con.execute(
        statement,
        [
            "clean-ctrl",
            "clean",
            "harness-v1",
            4096,
            "sha256:ctrl-src",
            "sha256:ctrl-proj",
            "sha256:ctrl-dim",
        ],
    )
    # Insert treatment 1: harness v2 (mismatched harness -> must not pair)
    con.execute(
        statement,
        [
            "treat-diff-harness",
            "treatment",
            "harness-v2",
            4096,
            "sha256:t1-src",
            "sha256:t1-proj",
            "sha256:t1-dim",
        ],
    )
    # Insert treatment 2: dose 16384 (mismatched dose_value on non-varying arm -> must not pair)
    con.execute(
        statement,
        [
            "treat-diff-dose",
            "treatment",
            "harness-v1",
            16384,
            "sha256:t2-src",
            "sha256:t2-proj",
            "sha256:t2-dim",
        ],
    )
    # Insert treatment 3: harness v1, dose 4096 (exact match -> must pair)
    con.execute(
        statement,
        [
            "treat-valid",
            "treatment",
            "harness-v1",
            4096,
            "sha256:t3-src",
            "sha256:t3-proj",
            "sha256:t3-dim",
        ],
    )

    rows = con.execute(
        "SELECT control_trial_id, treatment_trial_id FROM v_benchmark_contrasts WHERE family = 'action-memory-v1'"
    ).fetchall()
    assert rows == [("clean-ctrl", "treat-valid")]
