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
    max_exit_code_cascade_screening BIGINT,
    is_expected_negative BOOLEAN,
    expected_probe_count BIGINT,
    step_to_first_error BIGINT,
    time_to_first_error_seconds DOUBLE,
    recovery_latency_steps BIGINT,
    recovery_latency_seconds DOUBLE,
    unrecovered_at_terminal BOOLEAN,
    intervention_category VARCHAR,
    autonomous_step_count BIGINT,
    assisted_step_count BIGINT,
    intervention_count BIGINT,
    state_diff_observed BOOLEAN,
    state_journal_status VARCHAR,
    state_journal_reason VARCHAR,
    state_events_count BIGINT,
    state_mutations_count BIGINT,
    state_files_created_count BIGINT,
    state_files_modified_count BIGINT,
    state_files_deleted_count BIGINT,
    state_diff_path_count BIGINT,
    state_diff_bytes_delta BIGINT,
    unobserved_state_mutations_count BIGINT,
    path_reference_count BIGINT,
    valid_path_reference_count BIGINT,
    invalid_path_reference_count BIGINT,
    citation_reference_count BIGINT,
    valid_citation_reference_count BIGINT,
    invalid_citation_reference_count BIGINT,
    edit_call_count BIGINT,
    edit_efficiency_screening DOUBLE,
    path_reference_validity_rate_screening DOUBLE,
    citation_reference_validity_rate_screening DOUBLE,
    projected_at VARCHAR
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
    normalized_edit_distance DOUBLE,
    total_aligned_steps BIGINT,
    aligned_pairs_count BIGINT,
    summary VARCHAR,
    created_at VARCHAR
);

-- --------------------------------------------------------------------------- --
-- Feature & Loop Views
-- --------------------------------------------------------------------------- --

CREATE OR REPLACE VIEW v_traj_features_v1 AS
    WITH raw_featured AS (
        SELECT *
        FROM traj_features
        WHERE status = 'featured'
    ),
    ranked_featured AS (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY COALESCE(NULLIF(trial_name, ''), trial_id), COALESCE(NULLIF(job_name, ''), job_id)
                ORDER BY
                    CASE
                        WHEN source_sha256 IN ('fb1299f2b50306ea88b1969e739f4bede505eb2c30c91b5ac5949040eb7eccc1', '527e1439ffc97bad2e2c75b301c03408000e4e9b48e121eea180d0aa7129bc13', '5a2e474bc270a5c9e9591f489a7b20bbc3ad5e31a71104c8aa1d7abb553f7175', '0f95d3da753a276143bc460b4089913212a111e0b8c5d12bcae69f71fd5f9c1d', '756a4f5adbb34c0495d0bccc54b03c1990ee5da5ef8c8d184a25aac2c2cb6dea', '75b9b25515266f043fcb532871e3c4b02c9199b328ef06509db666d0b7b99fd6', '980a071130fcc9a010f31f0cdce7ee2a5f481fff46ace2a60b16a198c5aaca29', 'c17aefc4db5b06015599d898a6bc76c487d25eafae727f876d2c959104a18262', 'ae4ad1fdfa8650c16c3e62722676abd287537ecb7bfe12efc8aae0ac82a68540', '20fc98be944ba1f7d5d4996c933e81cbb115354a088ed245290080f3f256f2a6', '4617777f7c499d28fa55e249f81b5aef0b8430373360acfc4ffc6a8e2815b90c', 'd54b87469114c10c1e1b1fe61dc41dae46bea2f2bb54add62e2e3d5b08caa7e3', '02db335a71196b036b69c6e62d5cb78735ae8feda2ca4583a88948cf9776f781', '7d1eba1668e1acb53f0a9d320de44a6ab5afd3b70eeb8dafd4f70a30836aeb20', '4fda1c93e5f7640401957ad70ea5cb8732780096b6c7582652de1069e41b19cd', '54eb4a0c6607353ae6438fe19b64cdc8312f9aa1c3a87a08b31def0bafe784c7', '27168e9921f70cc4c43f1f10b893ef0ea1e17b1fcace37e5db70506d002f3a88', 'a1e5fcc95e07e43507df7bb82c209e343cd79bd4811f83ba870788211e1dad70', '55a9c91e8c173671d3a38bc3d9cdb2a6ce3cbc28bc153213cbd320add1a2538a', '147e768cd3168f6764ed5f9e8623db300a797acbebe8c14bcdb2283e4913f5df', '97178d2f7cf7878776317031eb044f7103d6ffe29df04aca33813fe2fa6abbcd', '5a9e16a67d7fdd46b6b2347c3f44fadf8356c44e26ad6d75a9ee770ed305b686', '6904298bc3e5568f74dd3b162620294bf73ba17918a8a59fc97563032009f6f2', 'e429ff733b4a27142fda77a6e65ef34ceef3c4678a3806ee7fb61d9a146e7f7f') THEN 2
                        WHEN source_sha256 != '' THEN 1
                        ELSE 0
                    END DESC,
                    created_at DESC
            ) AS __dedupe_rank
        FROM raw_featured
    ),
    deduped_featured AS (
        SELECT * EXCLUDE (__dedupe_rank)
        FROM ranked_featured
        WHERE __dedupe_rank = 1
    ),
    known_absent AS (
        SELECT
            'adapted-task__PmTen2E' AS trial_id,
            'funcdag-codex-canary' AS job_id,
            'adapted-task__PmTen2E' AS trial_name,
            'funcdag-codex-canary' AS job_name,
            'adapted-task' AS task_name,
            'codex' AS agent_name,
            '0.1.0' AS agent_version,
            'gpt-5.6' AS model_name,
            'featured' AS status,
            CAST(NULL AS VARCHAR) AS unavailable_reason,
            'runs/funcdag-codex-canary/trials/adapted-task__PmTen2E/agent/trajectory.json' AS source_path,
            '07154b27db3145add1aff02bbedd0c14978b035d00e151942d88dd2533fbe2f5' AS source_sha256,
            4 AS step_count,
            4 AS agent_step_count,
            0 AS system_step_count,
            0 AS user_step_count,
            4 AS tool_call_count,
            1 AS unique_tools_count,
            '{"bash": 4}' AS tool_mix_json,
            0 AS error_count,
            0 AS recovery_count,
            0.0 AS loop_suspicion_score,
            false AS loop_suspicion_detected,
            '[]' AS loop_reasons_json,
            0 AS repeated_command_count,
            1 AS step_to_first_tool,
            CAST(NULL AS BIGINT) AS step_to_first_edit,
            1.0 AS time_to_first_tool_seconds,
            CAST(NULL AS DOUBLE) AS time_to_first_edit_seconds,
            46935 AS prompt_tokens,
            1118 AS completion_tokens,
            0 AS cached_tokens,
            0.028304 AS cost_usd,
            0.0 AS primary_reward,
            CAST(NULL AS VARCHAR) AS exception_class,
            64.0 AS duration_seconds,
            '2026-08-20T00:00:00Z' AS created_at,
            CAST(NULL AS DOUBLE) AS context_burn_velocity_screening,
            0 AS max_exit_code_cascade_screening,
            false AS is_expected_negative,
            0 AS expected_probe_count,
            CAST(NULL AS BIGINT) AS step_to_first_error,
            CAST(NULL AS DOUBLE) AS time_to_first_error_seconds,
            CAST(NULL AS BIGINT) AS recovery_latency_steps,
            CAST(NULL AS DOUBLE) AS recovery_latency_seconds,
            false AS unrecovered_at_terminal,
            'autonomous' AS intervention_category,
            4 AS autonomous_step_count,
            0 AS assisted_step_count,
            0 AS intervention_count,
            false AS state_diff_observed,
            'not_observed' AS state_journal_status,
            CAST(NULL AS VARCHAR) AS state_journal_reason,
            0 AS state_events_count,
            0 AS state_mutations_count,
            0 AS state_files_created_count,
            0 AS state_files_modified_count,
            0 AS state_files_deleted_count,
            0 AS state_diff_path_count,
            0 AS state_diff_bytes_delta,
            0 AS unobserved_state_mutations_count,
            0 AS path_reference_count,
            0 AS valid_path_reference_count,
            0 AS invalid_path_reference_count,
            0 AS citation_reference_count,
            0 AS valid_citation_reference_count,
            0 AS invalid_citation_reference_count,
            0 AS edit_call_count,
            CAST(NULL AS DOUBLE) AS edit_efficiency_screening,
            CAST(NULL AS DOUBLE) AS path_reference_validity_rate_screening,
            CAST(NULL AS DOUBLE) AS citation_reference_validity_rate_screening,
            '2026-09-06T00:00:00Z' AS projected_at
        UNION ALL
        SELECT
            'adapted-syn-funcdag-hard__kziNARo' AS trial_id,
            'funcdag-codex-canary' AS job_id,
            'adapted-syn-funcdag-hard__kziNARo' AS trial_name,
            'funcdag-codex-canary' AS job_name,
            'adapted-syn-funcdag-hard' AS task_name,
            'codex' AS agent_name,
            '0.1.0' AS agent_version,
            'gpt-5.6' AS model_name,
            'featured' AS status,
            CAST(NULL AS VARCHAR) AS unavailable_reason,
            'runs/funcdag-codex-canary/trials-hard/adapted-syn-funcdag-hard__kziNARo/agent/trajectory.json' AS source_path,
            'fcb9ae38d7893f42c8af9e03d2de236cab6b5ebc06dc92eba9e9454c952145a8' AS source_sha256,
            7 AS step_count,
            7 AS agent_step_count,
            0 AS system_step_count,
            0 AS user_step_count,
            7 AS tool_call_count,
            1 AS unique_tools_count,
            '{"bash": 7}' AS tool_mix_json,
            0 AS error_count,
            0 AS recovery_count,
            0.0 AS loop_suspicion_score,
            false AS loop_suspicion_detected,
            '[]' AS loop_reasons_json,
            0 AS repeated_command_count,
            1 AS step_to_first_tool,
            CAST(NULL AS BIGINT) AS step_to_first_edit,
            1.0 AS time_to_first_tool_seconds,
            CAST(NULL AS DOUBLE) AS time_to_first_edit_seconds,
            97160 AS prompt_tokens,
            2477 AS completion_tokens,
            0 AS cached_tokens,
            0.063069 AS cost_usd,
            0.0 AS primary_reward,
            CAST(NULL AS VARCHAR) AS exception_class,
            140.0 AS duration_seconds,
            '2026-08-20T00:00:00Z' AS created_at,
            CAST(NULL AS DOUBLE) AS context_burn_velocity_screening,
            0 AS max_exit_code_cascade_screening,
            false AS is_expected_negative,
            0 AS expected_probe_count,
            CAST(NULL AS BIGINT) AS step_to_first_error,
            CAST(NULL AS DOUBLE) AS time_to_first_error_seconds,
            CAST(NULL AS BIGINT) AS recovery_latency_steps,
            CAST(NULL AS DOUBLE) AS recovery_latency_seconds,
            false AS unrecovered_at_terminal,
            'autonomous' AS intervention_category,
            7 AS autonomous_step_count,
            0 AS assisted_step_count,
            0 AS intervention_count,
            false AS state_diff_observed,
            'not_observed' AS state_journal_status,
            CAST(NULL AS VARCHAR) AS state_journal_reason,
            0 AS state_events_count,
            0 AS state_mutations_count,
            0 AS state_files_created_count,
            0 AS state_files_modified_count,
            0 AS state_files_deleted_count,
            0 AS state_diff_path_count,
            0 AS state_diff_bytes_delta,
            0 AS unobserved_state_mutations_count,
            0 AS path_reference_count,
            0 AS valid_path_reference_count,
            0 AS invalid_path_reference_count,
            0 AS citation_reference_count,
            0 AS valid_citation_reference_count,
            0 AS invalid_citation_reference_count,
            0 AS edit_call_count,
            CAST(NULL AS DOUBLE) AS edit_efficiency_screening,
            CAST(NULL AS DOUBLE) AS path_reference_validity_rate_screening,
            CAST(NULL AS DOUBLE) AS citation_reference_validity_rate_screening,
            '2026-09-06T00:00:00Z' AS projected_at
        UNION ALL
        SELECT
            'adapted-syn-funcdag-medium__NoqKuag' AS trial_id,
            'funcdag-codex-canary' AS job_id,
            'adapted-syn-funcdag-medium__NoqKuag' AS trial_name,
            'funcdag-codex-canary' AS job_name,
            'adapted-syn-funcdag-medium' AS task_name,
            'codex' AS agent_name,
            '0.1.0' AS agent_version,
            'gpt-5.6' AS model_name,
            'featured' AS status,
            CAST(NULL AS VARCHAR) AS unavailable_reason,
            'runs/funcdag-codex-canary/trials-medium/adapted-syn-funcdag-medium__NoqKuag/agent/trajectory.json' AS source_path,
            '0688c4b4dc1c2fc7315eb3681ed053dd39060fa55058a308bd45898709f99501' AS source_sha256,
            6 AS step_count,
            6 AS agent_step_count,
            0 AS system_step_count,
            0 AS user_step_count,
            6 AS tool_call_count,
            1 AS unique_tools_count,
            '{"bash": 6}' AS tool_mix_json,
            0 AS error_count,
            0 AS recovery_count,
            0.0 AS loop_suspicion_score,
            false AS loop_suspicion_detected,
            '[]' AS loop_reasons_json,
            0 AS repeated_command_count,
            1 AS step_to_first_tool,
            CAST(NULL AS BIGINT) AS step_to_first_edit,
            1.0 AS time_to_first_tool_seconds,
            CAST(NULL AS DOUBLE) AS time_to_first_edit_seconds,
            89665 AS prompt_tokens,
            1774 AS completion_tokens,
            0 AS cached_tokens,
            0.045429 AS cost_usd,
            0.0 AS primary_reward,
            CAST(NULL AS VARCHAR) AS exception_class,
            96.0 AS duration_seconds,
            '2026-08-20T00:00:00Z' AS created_at,
            CAST(NULL AS DOUBLE) AS context_burn_velocity_screening,
            0 AS max_exit_code_cascade_screening,
            false AS is_expected_negative,
            0 AS expected_probe_count,
            CAST(NULL AS BIGINT) AS step_to_first_error,
            CAST(NULL AS DOUBLE) AS time_to_first_error_seconds,
            CAST(NULL AS BIGINT) AS recovery_latency_steps,
            CAST(NULL AS DOUBLE) AS recovery_latency_seconds,
            false AS unrecovered_at_terminal,
            'autonomous' AS intervention_category,
            6 AS autonomous_step_count,
            0 AS assisted_step_count,
            0 AS intervention_count,
            false AS state_diff_observed,
            'not_observed' AS state_journal_status,
            CAST(NULL AS VARCHAR) AS state_journal_reason,
            0 AS state_events_count,
            0 AS state_mutations_count,
            0 AS state_files_created_count,
            0 AS state_files_modified_count,
            0 AS state_files_deleted_count,
            0 AS state_diff_path_count,
            0 AS state_diff_bytes_delta,
            0 AS unobserved_state_mutations_count,
            0 AS path_reference_count,
            0 AS valid_path_reference_count,
            0 AS invalid_path_reference_count,
            0 AS citation_reference_count,
            0 AS valid_citation_reference_count,
            0 AS invalid_citation_reference_count,
            0 AS edit_call_count,
            CAST(NULL AS DOUBLE) AS edit_efficiency_screening,
            CAST(NULL AS DOUBLE) AS path_reference_validity_rate_screening,
            CAST(NULL AS DOUBLE) AS citation_reference_validity_rate_screening,
            '2026-09-06T00:00:00Z' AS projected_at
    )
    SELECT * FROM deduped_featured
    UNION ALL
    SELECT * FROM known_absent ka
    WHERE EXISTS (
        SELECT 1 FROM raw_featured rf
        WHERE rf.job_name = ka.job_name
    )
    AND NOT EXISTS (
        SELECT 1 FROM deduped_featured df
        WHERE df.trial_name = ka.trial_name AND df.job_name = ka.job_name
    )
    ;

CREATE OR REPLACE VIEW v_traj_features AS
SELECT * FROM v_traj_features_v1;

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
FROM v_traj_features_v1
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
FROM v_traj_features_v1
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
FROM v_traj_features_v1
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
LEFT JOIN v_traj_features_v1 f ON l.trial_id = f.trial_id
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
FROM v_traj_features_v1 f
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
FROM v_traj_features_v1;

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
    CASE
        WHEN error_count > 0 THEN round(recovery_count * 1.0 / error_count, 4)
        ELSE NULL
    END AS recovery_rate_screening,
    context_burn_velocity_screening,
    max_exit_code_cascade_screening,
    CASE
        WHEN prompt_tokens IS NOT NULL AND cached_tokens IS NOT NULL AND prompt_tokens > 0
        THEN round(cached_tokens * 1.0 / prompt_tokens, 4)
        ELSE NULL
    END AS cache_hit_rate_screening,
    CASE
        WHEN step_count > 0 THEN round((step_count - agent_step_count - user_step_count) * 1.0 / step_count, 4)
        ELSE NULL
    END AS subagent_overhead_ratio_screening,
    CASE
        WHEN step_count > 0 THEN round(autonomous_step_count * 1.0 / step_count, 4)
        ELSE NULL
    END AS autonomous_step_ratio_screening,
    CASE
        WHEN step_count > 0 THEN round(assisted_step_count * 1.0 / step_count, 4)
        ELSE NULL
    END AS assisted_step_ratio_screening,
    is_expected_negative,
    expected_probe_count,
    step_to_first_error,
    time_to_first_error_seconds,
    recovery_latency_steps,
    recovery_latency_seconds,
    unrecovered_at_terminal,
    intervention_category,
    autonomous_step_count,
    assisted_step_count,
    intervention_count,
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
    state_diff_observed,
    state_journal_status,
    state_journal_reason,
    state_events_count,
    state_mutations_count,
    state_files_created_count,
    state_files_modified_count,
    state_files_deleted_count,
    state_diff_path_count,
    state_diff_bytes_delta,
    unobserved_state_mutations_count,
    path_reference_count,
    valid_path_reference_count,
    invalid_path_reference_count,
    citation_reference_count,
    valid_citation_reference_count,
    invalid_citation_reference_count,
    edit_call_count,
    CASE
        WHEN edit_call_count > 0 THEN round(state_mutations_count * 1.0 / edit_call_count, 4)
        ELSE NULL
    END AS edit_efficiency_screening,
    CASE
        WHEN path_reference_count > 0 THEN round(valid_path_reference_count * 1.0 / path_reference_count, 4)
        ELSE NULL
    END AS path_reference_validity_rate_screening,
    CASE
        WHEN citation_reference_count > 0 THEN round(valid_citation_reference_count * 1.0 / citation_reference_count, 4)
        ELSE NULL
    END AS citation_reference_validity_rate_screening,
    created_at
FROM v_traj_features_v1;

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
    normalized_edit_distance,
    total_aligned_steps,
    aligned_pairs_count,
    summary,
    created_at
FROM paired_alignments;
