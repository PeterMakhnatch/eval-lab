-- Trajectory outline, mechanical features, loop detection, and review queue views (M030 LOOP-TRAJ).
--
-- Exposes:
--   v_traj_features: mechanical feature rows per trial (step/tool/error/token metrics)
--   v_traj_loops: filtered view of loop-suspicious trials ordered by loop score
--   v_traj_tool_mix: tool mix distributions across agents and task families
--   v_traj_error_recovery: error counts, recoveries, and recovery rates by agent/task
--   v_traj_labels: unified human, heuristic, and model behavior labels
--   v_traj_queue: candidate review queue for unlabeled real-agent trials
--   v_traj_summary: headline trajectory coverage and feature summary
--   v_trace_baseline: deterministic mechanical facts & screening metrics per trial
--
-- Run standalone in DuckDB:
--   duckdb -c ".read sql/traj_views.sql" -c "SELECT * FROM v_traj_summary"
--
-- Or via unified attach surface:
--   evallab db attach --query "SELECT * FROM v_traj_loops LIMIT 5"

-- Fallback schema tables for clean in-memory DuckDB sessions
CREATE TABLE IF NOT EXISTS traj_features (
    trial_id VARCHAR,
    job_id VARCHAR,
    trial_name VARCHAR,
    job_name VARCHAR,
    task_name VARCHAR,
    agent_name VARCHAR,
    agent_version VARCHAR,
    model_name VARCHAR,
    status VARCHAR,
    unavailable_reason VARCHAR,
    source_path VARCHAR,
    source_sha256 VARCHAR,
    step_count BIGINT,
    agent_step_count BIGINT,
    system_step_count BIGINT,
    user_step_count BIGINT,
    tool_call_count BIGINT,
    unique_tools_count BIGINT,
    tool_mix_json VARCHAR,
    error_count BIGINT,
    recovery_count BIGINT,
    loop_suspicion_score DOUBLE,
    loop_suspicion_detected BOOLEAN,
    loop_reasons_json VARCHAR,
    repeated_command_count BIGINT,
    step_to_first_tool BIGINT,
    step_to_first_edit BIGINT,
    time_to_first_tool_seconds DOUBLE,
    time_to_first_edit_seconds DOUBLE,
    prompt_tokens BIGINT,
    completion_tokens BIGINT,
    cached_tokens BIGINT,
    cost_usd DOUBLE,
    primary_reward DOUBLE,
    exception_class VARCHAR,
    duration_seconds DOUBLE,
    created_at VARCHAR,
    context_burn_velocity_screening DOUBLE,
    max_exit_code_cascade_screening BIGINT
);

CREATE TABLE IF NOT EXISTS behavior_labels (
    schema_version BIGINT,
    label_id VARCHAR,
    target_type VARCHAR,
    target_id VARCHAR,
    job_id VARCHAR,
    trial_id VARCHAR,
    trial_name VARCHAR,
    task_name VARCHAR,
    taxonomy VARCHAR,
    label VARCHAR,
    rationale VARCHAR,
    provenance VARCHAR,
    author VARCHAR,
    created_at VARCHAR,
    confidence VARCHAR,
    evidence_json VARCHAR,
    source_sha256 VARCHAR,
    analysis_id VARCHAR,
    model_agent VARCHAR,
    model_agent_version VARCHAR,
    model_name VARCHAR,
    prompt_digest VARCHAR,
    rubric_digest VARCHAR,
    output_schema_digest VARCHAR,
    model_created_at VARCHAR,
    input_tokens BIGINT,
    output_tokens BIGINT,
    cost_usd DOUBLE
);

CREATE TABLE IF NOT EXISTS trial_facts (
    experiment_id VARCHAR,
    job_id VARCHAR,
    trial_id VARCHAR,
    job_name VARCHAR,
    trial_name VARCHAR,
    task_name VARCHAR,
    agent_name VARCHAR,
    model_name VARCHAR,
    primary_reward DOUBLE,
    exception_class VARCHAR,
    duration_seconds DOUBLE
);

CREATE TABLE IF NOT EXISTS trajectory_ir (
    ir_digest VARCHAR,
    trial_id VARCHAR,
    job_id VARCHAR,
    trial_name VARCHAR,
    job_name VARCHAR,
    task_name VARCHAR,
    task_digest VARCHAR,
    verifier_digest VARCHAR,
    agent_scaffold VARCHAR,
    agent_version VARCHAR,
    model_name VARCHAR,
    status VARCHAR,
    unavailable_reason VARCHAR,
    final_verdict VARCHAR,
    primary_reward DOUBLE,
    exception_class VARCHAR,
    duration_seconds DOUBLE,
    total_tokens BIGINT,
    cost_usd DOUBLE,
    quality_status VARCHAR,
    quality_findings_json VARCHAR,
    unpaired_tool_calls_count BIGINT,
    linkage_coverage VARCHAR,
    is_production_cas BOOLEAN,
    total_events BIGINT,
    total_episodes BIGINT,
    total_opportunities BIGINT,
    created_at VARCHAR
);

CREATE TABLE IF NOT EXISTS evidence_packs (
    pack_digest VARCHAR,
    ir_digest VARCHAR,
    trial_id VARCHAR,
    job_id VARCHAR,
    trial_name VARCHAR,
    job_name VARCHAR,
    task_name VARCHAR,
    agent_name VARCHAR,
    model_name VARCHAR,
    final_verdict VARCHAR,
    primary_reward DOUBLE,
    quality_status VARCHAR,
    quality_findings_json VARCHAR,
    budget_tokens BIGINT,
    consumed_tokens_est BIGINT,
    is_model_callable BOOLEAN,
    tiered_pack_required BOOLEAN,
    abstain_required BOOLEAN,
    overflow_reason VARCHAR,
    redaction_profile_digest VARCHAR,
    selected_windows_count BIGINT,
    omitted_ranges_count BIGINT,
    is_bounded BOOLEAN,
    created_at VARCHAR
);

CREATE TABLE IF NOT EXISTS paired_alignments (
    alignment_id VARCHAR,
    alignment_version VARCHAR,
    trial_id_a VARCHAR,
    trial_id_b VARCHAR,
    ir_digest_a VARCHAR,
    ir_digest_b VARCHAR,
    trial_name_a VARCHAR,
    trial_name_b VARCHAR,
    task_name VARCHAR,
    config_delta VARCHAR,
    outcome_delta VARCHAR,
    divergence_step_a BIGINT,
    divergence_step_b BIGINT,
    citation_a_json VARCHAR,
    citation_b_json VARCHAR,
    has_local_divergences BOOLEAN,
    local_divergences_json VARCHAR,
    unmatched_ranges_a_json VARCHAR,
    unmatched_ranges_b_json VARCHAR,
    alignment_score DOUBLE,
    total_aligned_steps BIGINT,
    aligned_pairs_count BIGINT,
    summary VARCHAR,
    created_at VARCHAR
);

-- --------------------------------------------------------------------------- --
-- Feature & Loop Views
-- --------------------------------------------------------------------------- --

CREATE OR REPLACE VIEW v_traj_features AS
SELECT
    trial_id,
    job_id,
    trial_name,
    job_name,
    task_name,
    agent_name,
    agent_version,
    model_name,
    status,
    unavailable_reason,
    source_path,
    source_sha256,
    step_count,
    agent_step_count,
    system_step_count,
    user_step_count,
    tool_call_count,
    unique_tools_count,
    tool_mix_json,
    error_count,
    recovery_count,
    loop_suspicion_score,
    loop_suspicion_detected,
    loop_reasons_json,
    repeated_command_count,
    step_to_first_tool,
    step_to_first_edit,
    time_to_first_tool_seconds,
    time_to_first_edit_seconds,
    prompt_tokens,
    completion_tokens,
    cached_tokens,
    cost_usd,
    primary_reward,
    exception_class,
    duration_seconds,
    created_at,
    context_burn_velocity_screening,
    max_exit_code_cascade_screening
FROM traj_features;

CREATE OR REPLACE VIEW v_traj_loops AS
SELECT
    trial_id,
    trial_name,
    task_name,
    agent_name,
    model_name,
    primary_reward,
    step_count,
    tool_call_count,
    error_count,
    loop_suspicion_score,
    loop_suspicion_detected,
    loop_reasons_json,
    repeated_command_count,
    source_path
FROM traj_features
WHERE loop_suspicion_detected
ORDER BY loop_suspicion_score DESC, error_count DESC, trial_id ASC;

CREATE OR REPLACE VIEW v_traj_tool_mix AS
SELECT
    task_name,
    agent_name,
    model_name,
    count(*) AS trial_count,
    sum(step_count) AS total_steps,
    sum(tool_call_count) AS total_tool_calls,
    round(avg(tool_call_count), 1) AS avg_tools_per_trial,
    sum(error_count) AS total_errors,
    sum(recovery_count) AS total_recoveries,
    sum(CASE WHEN loop_suspicion_detected THEN 1 ELSE 0 END) AS loop_trials_count
FROM traj_features
WHERE status = 'featured'
GROUP BY task_name, agent_name, model_name
ORDER BY task_name, agent_name;

CREATE OR REPLACE VIEW v_traj_error_recovery AS
SELECT
    task_name,
    agent_name,
    count(*) AS trials,
    sum(error_count) AS total_errors,
    sum(recovery_count) AS total_recoveries,
    round(
        CASE
            WHEN sum(error_count) > 0
            THEN CAST(sum(recovery_count) AS DOUBLE) / CAST(sum(error_count) AS DOUBLE)
            ELSE NULL
        END,
        3
    ) AS recovery_rate,
    sum(CASE WHEN primary_reward = 1.0 THEN 1 ELSE 0 END) AS passed_trials
FROM traj_features
WHERE status = 'featured'
GROUP BY task_name, agent_name
ORDER BY total_errors DESC, recovery_rate DESC;

CREATE OR REPLACE VIEW v_traj_labels AS
SELECT
    l.label_id,
    l.trial_id,
    l.trial_name,
    l.task_name,
    l.label,
    l.rationale,
    l.provenance,
    l.author,
    l.created_at,
    l.taxonomy,
    l.confidence,
    l.analysis_id,
    l.model_name AS label_model_name,
    f.agent_name,
    f.model_name AS trajectory_model_name,
    f.primary_reward,
    f.step_count,
    f.loop_suspicion_score,
    f.source_path
FROM behavior_labels l
LEFT JOIN traj_features f ON l.trial_id = f.trial_id
WHERE l.target_type IN ('trajectory', 'trial');

CREATE OR REPLACE VIEW v_traj_queue AS
SELECT
    f.trial_id,
    f.trial_name,
    f.job_id,
    f.job_name,
    f.task_name,
    f.agent_name,
    f.model_name,
    f.primary_reward,
    f.step_count,
    f.tool_call_count,
    f.error_count,
    f.loop_suspicion_score,
    f.loop_suspicion_detected,
    f.source_path
FROM traj_features f
WHERE f.status = 'featured'
  AND lower(f.agent_name) NOT IN ('oracle', 'nop')
  AND f.trial_id NOT IN (
      SELECT trial_id
      FROM behavior_labels
      WHERE provenance = 'human' AND target_type = 'trajectory'
  )
ORDER BY f.loop_suspicion_score DESC, f.error_count DESC, f.task_name ASC, f.trial_id ASC;

CREATE OR REPLACE VIEW v_traj_summary AS
SELECT
    count(*) AS total_trials,
    sum(CASE WHEN status = 'featured' THEN 1 ELSE 0 END) AS featured_trials,
    sum(CASE WHEN status = 'accounted_unavailable' THEN 1 ELSE 0 END) AS unavailable_trials,
    sum(CASE WHEN loop_suspicion_detected THEN 1 ELSE 0 END) AS loop_detected_trials,
    sum(error_count) AS total_errors,
    sum(recovery_count) AS total_recoveries,
    sum(prompt_tokens) AS total_prompt_tokens,
    sum(completion_tokens) AS total_completion_tokens,
    round(sum(cost_usd), 4) AS total_cost_usd,
    (SELECT count(*) FROM behavior_labels WHERE provenance = 'human') AS human_labels_count,
    (SELECT count(*) FROM behavior_labels WHERE provenance = 'heuristic') AS heuristic_labels_count
FROM traj_features;

-- --------------------------------------------------------------------------- --
-- Deterministic Trace Baseline View (Phase 1 Baseline)
-- --------------------------------------------------------------------------- --

CREATE OR REPLACE VIEW v_trace_baseline AS
SELECT
    trial_id,
    job_id,
    trial_name,
    job_name,
    task_name,
    agent_name,
    agent_version,
    model_name,
    status,
    unavailable_reason,
    source_path,
    source_sha256,
    primary_reward,
    exception_class,
    duration_seconds,
    step_count,
    agent_step_count,
    system_step_count,
    user_step_count,
    tool_call_count,
    unique_tools_count,
    error_count,
    recovery_count,
    CASE
        WHEN tool_call_count > 0 THEN round(unique_tools_count * 1.0 / tool_call_count, 4)
        ELSE NULL
    END AS linear_innocence_screening,
    CASE
        WHEN tool_call_count > 0 THEN round(error_count * 1.0 / tool_call_count, 4)
        ELSE NULL
    END AS tool_error_rate_screening,
    context_burn_velocity_screening,
    max_exit_code_cascade_screening,
    CASE
        WHEN prompt_tokens IS NOT NULL AND cached_tokens IS NOT NULL AND (prompt_tokens + cached_tokens) > 0
        THEN round(cached_tokens * 1.0 / (prompt_tokens + cached_tokens), 4)
        ELSE NULL
    END AS cache_hit_rate_screening,
    CASE
        WHEN step_count > 0 THEN round((step_count - agent_step_count - user_step_count) * 1.0 / step_count, 4)
        ELSE NULL
    END AS subagent_overhead_ratio_screening,
    prompt_tokens,
    completion_tokens,
    cached_tokens,
    CASE
        WHEN prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL
        THEN prompt_tokens + completion_tokens
        ELSE NULL
    END AS total_tokens,
    cost_usd,
    loop_suspicion_score,
    loop_suspicion_detected,
    loop_reasons_json,
    repeated_command_count,
    created_at
FROM traj_features;

-- --------------------------------------------------------------------------- --
-- TrajectoryIR, EvidencePack, and Paired Alignment Views
-- --------------------------------------------------------------------------- --

CREATE OR REPLACE VIEW v_trajectory_ir_summary AS
SELECT
    ir_digest,
    trial_id,
    job_id,
    trial_name,
    job_name,
    task_name,
    task_digest,
    verifier_digest,
    agent_scaffold,
    agent_version,
    model_name,
    status,
    unavailable_reason,
    final_verdict,
    primary_reward,
    exception_class,
    duration_seconds,
    total_tokens,
    cost_usd,
    quality_status,
    quality_findings_json,
    unpaired_tool_calls_count,
    linkage_coverage,
    is_production_cas,
    total_events,
    total_episodes,
    total_opportunities,
    created_at
FROM trajectory_ir;

CREATE OR REPLACE VIEW v_evidence_packs AS
SELECT
    pack_digest,
    ir_digest,
    trial_id,
    job_id,
    trial_name,
    job_name,
    task_name,
    agent_name,
    model_name,
    final_verdict,
    primary_reward,
    quality_status,
    quality_findings_json,
    budget_tokens,
    consumed_tokens_est,
    is_model_callable,
    tiered_pack_required,
    abstain_required,
    overflow_reason,
    redaction_profile_digest,
    selected_windows_count,
    omitted_ranges_count,
    is_bounded,
    created_at
FROM evidence_packs;

CREATE OR REPLACE VIEW v_paired_alignments AS
SELECT
    alignment_id,
    alignment_version,
    trial_id_a,
    trial_id_b,
    ir_digest_a,
    ir_digest_b,
    trial_name_a,
    trial_name_b,
    task_name,
    config_delta,
    outcome_delta,
    divergence_step_a,
    divergence_step_b,
    citation_a_json,
    citation_b_json,
    has_local_divergences,
    local_divergences_json,
    unmatched_ranges_a_json,
    unmatched_ranges_b_json,
    alignment_score,
    total_aligned_steps,
    aligned_pairs_count,
    summary,
    created_at
FROM paired_alignments;
