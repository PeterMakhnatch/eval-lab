-- Benchmark-Specific Trajectory Baseline Views & Matched Contrasts
-- Exposes:
--   v_action_memory_baseline: Context & Actionable Memory (action-memory-v1) L1 facts and L2 metrics
--   v_mcp_funcdag_baseline: Tool Selection, Composition & Value Propagation (mcp-funcdag-v1)
--   v_mcp_recovery_baseline: Error Detection & Autonomous Recovery (mcp-recovery-v1)
--   v_benchmark_contrasts: Matched-pair contrasts within benchmark families
--   v_benchmark_refusal_diagnostics: Underpowered/unsupported cross-cell refusal diagnostics
--   v_benchmark_summary: Unified headline coverage and metric summary
--   v_predictor_eligibility: Predictor admissibility and exact refusal reason codes
-- Fallback DDL for clean in-memory DuckDB sessions
CREATE TABLE IF NOT EXISTS action_memory_features (
    trial_id VARCHAR PRIMARY KEY,
    family VARCHAR NOT NULL,
    task_id VARCHAR NOT NULL,
    seed BIGINT NOT NULL,
    cell_id VARCHAR NOT NULL,
    arm VARCHAR NOT NULL,
    dose_bytes BIGINT NOT NULL,
    construct VARCHAR NOT NULL,
    causal_grade VARCHAR NOT NULL,
    task_success BOOLEAN NOT NULL,
    total_tool_calls BIGINT NOT NULL,
    model_call_count BIGINT NOT NULL,
    prompt_tokens_per_step DOUBLE,
    prompt_cache_hit_rate DOUBLE,
    raw_binding_opportunities BIGINT NOT NULL,
    raw_conflicting_opportunities BIGINT NOT NULL,
    bound_target_entity VARCHAR,
    bound_target_attribute VARCHAR,
    bound_target_value VARCHAR,
    binding_matched BOOLEAN NOT NULL,
    stale_value_bound BOOLEAN NOT NULL,
    expected_handle_count BIGINT NOT NULL,
    valid_handle_count BIGINT NOT NULL,
    unknown_handle_count BIGINT NOT NULL,
    duplicate_handle_count BIGINT NOT NULL,
    issued_handle_count BIGINT,
    handle_set_match BOOLEAN NOT NULL,
    handle_order_match BOOLEAN NOT NULL,
    handle_coverage_rate DOUBLE,
    handle_issuance_ratio DOUBLE,
    handle_order_concordance BOOLEAN,
    retrieval_authority VARCHAR,
    capture_concordance_status VARCHAR,
    schema_conformance_rate DOUBLE,
    binding_survival_rate DOUBLE,
    stale_value_override_rate DOUBLE,
    context_burn_velocity DOUBLE,
    occupancy_first_failure DOUBLE,
    citation VARCHAR NOT NULL,
    verifier_truth_digest VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_funcdag_features (
    trial_id VARCHAR PRIMARY KEY,
    family VARCHAR NOT NULL,
    task_id VARCHAR NOT NULL,
    seed BIGINT NOT NULL,
    depth BIGINT NOT NULL,
    width BIGINT NOT NULL,
    distractor_count BIGINT NOT NULL,
    name_similarity VARCHAR NOT NULL,
    schema_drift BOOLEAN NOT NULL,
    task_success BOOLEAN NOT NULL,
    total_tool_calls BIGINT NOT NULL,
    model_call_count BIGINT NOT NULL,
    prompt_tokens_per_step DOUBLE,
    prompt_cache_hit_rate DOUBLE,
    required_dag_edges BIGINT NOT NULL,
    required_value_bindings BIGINT NOT NULL,
    executed_dag_edges BIGINT NOT NULL,
    correct_value_bindings BIGINT NOT NULL,
    redundant_tool_calls BIGINT NOT NULL,
    cycle_violations BIGINT NOT NULL,
    satisfied_edge_opportunities BIGINT NOT NULL,
    first_edge_step BIGINT,
    schema_conformance_rate DOUBLE,
    value_propagation_accuracy DOUBLE,
    dag_edge_conformance_rate DOUBLE,
    redundant_call_ratio DOUBLE,
    first_edge_latency DOUBLE,
    citation VARCHAR NOT NULL,
    verifier_truth_digest VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_recovery_features (
    trial_id VARCHAR PRIMARY KEY,
    family VARCHAR NOT NULL,
    task_id VARCHAR NOT NULL,
    seed BIGINT NOT NULL,
    fault_class VARCHAR,
    persistence_level BIGINT NOT NULL,
    mode VARCHAR NOT NULL,
    task_success BOOLEAN NOT NULL,
    total_tool_calls BIGINT NOT NULL,
    model_call_count BIGINT NOT NULL,
    prompt_tokens_per_step DOUBLE,
    prompt_cache_hit_rate DOUBLE,
    injected_fault_record VARCHAR,
    injected_fault_count BIGINT NOT NULL,
    fault_detected_count BIGINT NOT NULL,
    post_fault_retries BIGINT NOT NULL,
    blind_retries BIGINT NOT NULL,
    certified_recovered_faults BIGINT NOT NULL,
    step_to_first_fault BIGINT,
    step_to_recovery BIGINT,
    schema_conformance_rate DOUBLE,
    autonomous_recovery_rate DOUBLE,
    fault_detection_rate DOUBLE,
    blind_retry_rate DOUBLE,
    fault_recovery_latency DOUBLE,
    citation VARCHAR NOT NULL,
    verifier_truth_digest VARCHAR NOT NULL
);

-- Idempotent dimension migration for existing benchmark fact tables.  Rows without
-- these fields remain present for audit but cannot enter analysis-ready views.
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS job_id VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS cas_uri VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS model_name VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS agent_name VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS task_name VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS harness_version VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS scaffold_version VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS repeat_group_id VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS dose_axis VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS dose_value DOUBLE;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS dose_unit VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS alphabet_id VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS alphabet_version VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS quality_status VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS report_digest VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS source_digest VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS producer_version VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS projection_identity VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS dimension_digest VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS projection_status VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS analysis_ready BOOLEAN;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS projection_refusals VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS expected_handle_count BIGINT;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS valid_handle_count BIGINT;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS unknown_handle_count BIGINT;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS duplicate_handle_count BIGINT;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS handle_set_match BOOLEAN;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS handle_order_match BOOLEAN;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS handle_coverage_rate DOUBLE;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS issued_handle_count BIGINT;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS handle_issuance_ratio DOUBLE;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS handle_order_concordance BOOLEAN;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS retrieval_authority VARCHAR;
ALTER TABLE action_memory_features ADD COLUMN IF NOT EXISTS capture_concordance_status VARCHAR;

ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS job_id VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS cas_uri VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS model_name VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS agent_name VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS task_name VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS harness_version VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS scaffold_version VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS repeat_group_id VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS dose_axis VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS dose_value DOUBLE;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS dose_unit VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS alphabet_id VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS alphabet_version VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS quality_status VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS report_digest VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS source_digest VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS producer_version VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS projection_identity VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS dimension_digest VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS projection_status VARCHAR;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS analysis_ready BOOLEAN;
ALTER TABLE mcp_funcdag_features ADD COLUMN IF NOT EXISTS projection_refusals VARCHAR;

ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS job_id VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS cas_uri VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS model_name VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS agent_name VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS task_name VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS harness_version VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS scaffold_version VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS repeat_group_id VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS dose_axis VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS dose_value DOUBLE;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS dose_unit VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS alphabet_id VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS alphabet_version VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS quality_status VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS report_digest VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS source_digest VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS producer_version VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS projection_identity VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS dimension_digest VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS projection_status VARCHAR;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS analysis_ready BOOLEAN;
ALTER TABLE mcp_recovery_features ADD COLUMN IF NOT EXISTS projection_refusals VARCHAR;

-- 1. Action Memory Baseline View
CREATE OR REPLACE VIEW v_action_memory_baseline AS
SELECT *
FROM action_memory_features
WHERE analysis_ready IS TRUE;

-- 2. MCP-FuncDAG Baseline View
CREATE OR REPLACE VIEW v_mcp_funcdag_baseline AS
SELECT *
FROM mcp_funcdag_features
WHERE analysis_ready IS TRUE;

-- 3. MCP-Recovery Baseline View
CREATE OR REPLACE VIEW v_mcp_recovery_baseline AS
SELECT *
FROM mcp_recovery_features
WHERE analysis_ready IS TRUE;

-- 4. Matched-Pair Contrasts View
CREATE OR REPLACE VIEW v_benchmark_contrasts AS
WITH mem_pairs AS (
    SELECT
        c.seed,
        c.cell_id AS entity_id,
        c.model_name,
        c.agent_name,
        c.task_name,
        c.harness_version,
        c.scaffold_version,
        c.repeat_group_id,
        c.dose_axis,
        c.dose_unit,
        c.alphabet_id,
        c.alphabet_version,
        'action-memory-v1' AS family,
        'clean_vs_inversion' AS contrast_type,
        c.trial_id AS control_trial_id,
        t.trial_id AS treatment_trial_id,
        c.task_success AS control_success,
        t.task_success AS treatment_success,
        c.binding_survival_rate AS control_metric,
        t.binding_survival_rate AS treatment_metric,
        CASE
            WHEN c.binding_survival_rate IS NOT NULL AND t.binding_survival_rate IS NOT NULL
            THEN round(t.binding_survival_rate - c.binding_survival_rate, 4)
            ELSE NULL
        END AS delta_metric
    FROM action_memory_features c
    JOIN action_memory_features t
      ON c.seed = t.seed
     AND c.cell_id = t.cell_id
     AND c.model_name = t.model_name
     AND c.agent_name = t.agent_name
     AND c.task_name = t.task_name
     AND c.harness_version = t.harness_version
     AND c.scaffold_version = t.scaffold_version
     AND c.repeat_group_id = t.repeat_group_id
     AND c.dose_axis = t.dose_axis
     AND c.dose_value = t.dose_value
     AND c.dose_unit = t.dose_unit
     AND c.alphabet_id = t.alphabet_id
     AND c.alphabet_version = t.alphabet_version
     AND c.analysis_ready IS TRUE
     AND t.analysis_ready IS TRUE
     AND c.arm = 'clean'
     AND t.arm != 'clean'
),
rec_pairs AS (
    SELECT
        c.seed,
        COALESCE(t.fault_class, 'unknown') AS entity_id,
        c.model_name,
        c.agent_name,
        c.task_name,
        c.harness_version,
        c.scaffold_version,
        c.repeat_group_id,
        c.dose_axis,
        c.dose_unit,
        c.alphabet_id,
        c.alphabet_version,
        'mcp-recovery-v1' AS family,
        'clean_vs_fault' AS contrast_type,
        c.trial_id AS control_trial_id,
        t.trial_id AS treatment_trial_id,
        c.task_success AS control_success,
        t.task_success AS treatment_success,
        (CASE WHEN c.task_success THEN 1.0 ELSE 0.0 END) AS control_metric,
        (CASE WHEN t.task_success THEN 1.0 ELSE 0.0 END) AS treatment_metric,
        round((CASE WHEN t.task_success THEN 1.0 ELSE 0.0 END) - (CASE WHEN c.task_success THEN 1.0 ELSE 0.0 END), 4) AS delta_metric
    FROM mcp_recovery_features c
    JOIN mcp_recovery_features t
      ON c.seed = t.seed
     AND c.task_id = t.task_id
     AND c.model_name = t.model_name
     AND c.agent_name = t.agent_name
     AND c.task_name = t.task_name
     AND c.harness_version = t.harness_version
     AND c.scaffold_version = t.scaffold_version
     AND c.repeat_group_id = t.repeat_group_id
     AND c.persistence_level = t.persistence_level
     AND c.dose_axis = t.dose_axis
     AND c.dose_value = t.dose_value
     AND c.dose_unit = t.dose_unit
     AND c.alphabet_id = t.alphabet_id
     AND c.alphabet_version = t.alphabet_version
     AND c.analysis_ready IS TRUE
     AND t.analysis_ready IS TRUE
     AND c.mode = 'clean'
     AND t.mode = 'fault'
)
SELECT * FROM mem_pairs
UNION ALL
SELECT * FROM rec_pairs;

-- 5. Refusal & Power Diagnostics View
CREATE OR REPLACE VIEW v_benchmark_refusal_diagnostics AS
SELECT
    trial_id,
    family,
    task_id,
    task_name,
    model_name,
    agent_name,
    harness_version,
    scaffold_version,
    repeat_group_id,
    quality_status,
    report_digest,
    analysis_ready,
    'action-memory-v1' AS benchmark_vertical,
    raw_binding_opportunities > 0 AS has_binding_eligibility,
    raw_conflicting_opportunities > 0 AS has_conflict_eligibility,
    (analysis_ready IS NOT TRUE OR raw_binding_opportunities = 0 OR total_tool_calls = 0) AS is_refused_underpowered,
    CASE
        WHEN analysis_ready IS NOT TRUE THEN coalesce(nullif(projection_refusals, ''), 'DIMENSION_OR_QUALITY_REFUSED')
        WHEN total_tool_calls = 0 THEN 'NO_TOOL_CALLS_INITIATED'
        WHEN raw_binding_opportunities = 0 THEN 'ZERO_OPPORTUNITY_UNDERPOWERED'
        ELSE NULL
    END AS refusal_reason
FROM action_memory_features
UNION ALL
SELECT
    trial_id, family, task_id, task_name, model_name, agent_name, harness_version,
    scaffold_version, repeat_group_id, quality_status, report_digest, analysis_ready,
    'mcp-funcdag-v1' AS benchmark_vertical,
    required_dag_edges > 0 AS has_binding_eligibility,
    required_value_bindings > 0 AS has_conflict_eligibility,
    (analysis_ready IS NOT TRUE OR required_dag_edges = 0 OR total_tool_calls = 0) AS is_refused_underpowered,
    CASE
        WHEN analysis_ready IS NOT TRUE THEN coalesce(nullif(projection_refusals, ''), 'DIMENSION_OR_QUALITY_REFUSED')
        WHEN total_tool_calls = 0 THEN 'NO_TOOL_CALLS_INITIATED'
        WHEN required_dag_edges = 0 THEN 'ZERO_REQUIRED_EDGES_UNDERPOWERED'
        ELSE NULL
    END AS refusal_reason
FROM mcp_funcdag_features
UNION ALL
SELECT
    trial_id, family, task_id, task_name, model_name, agent_name, harness_version,
    scaffold_version, repeat_group_id, quality_status, report_digest, analysis_ready,
    'mcp-recovery-v1' AS benchmark_vertical,
    injected_fault_count > 0 AS has_binding_eligibility,
    post_fault_retries > 0 AS has_conflict_eligibility,
    (analysis_ready IS NOT TRUE OR (injected_fault_count = 0 AND mode = 'fault') OR total_tool_calls = 0) AS is_refused_underpowered,
    CASE
        WHEN analysis_ready IS NOT TRUE THEN coalesce(nullif(projection_refusals, ''), 'DIMENSION_OR_QUALITY_REFUSED')
        WHEN total_tool_calls = 0 THEN 'NO_TOOL_CALLS_INITIATED'
        WHEN injected_fault_count = 0 AND mode = 'fault' THEN 'FAULT_ARM_MISSING_INJECTION'
        ELSE NULL
    END AS refusal_reason
FROM mcp_recovery_features;
-- 6. Benchmark Summary View
CREATE OR REPLACE VIEW v_benchmark_summary AS
SELECT
    'action-memory-v1' AS family,
    count(*) AS total_trials,
    sum(CASE WHEN task_success THEN 1 ELSE 0 END) AS success_trials,
    round(avg(CASE WHEN task_success THEN 1.0 ELSE 0.0 END), 4) AS success_rate,
    round(avg(schema_conformance_rate), 4) AS avg_schema_conformance_rate,
    round(avg(binding_survival_rate), 4) AS primary_l2_rate,
    'binding_survival_rate' AS primary_l2_name
FROM action_memory_features
WHERE analysis_ready IS TRUE
UNION ALL
SELECT
    'mcp-funcdag-v1' AS family,
    count(*) AS total_trials,
    sum(CASE WHEN task_success THEN 1 ELSE 0 END) AS success_trials,
    round(avg(CASE WHEN task_success THEN 1.0 ELSE 0.0 END), 4) AS success_rate,
    round(avg(schema_conformance_rate), 4) AS avg_schema_conformance_rate,
    round(avg(dag_edge_conformance_rate), 4) AS primary_l2_rate,
    'dag_edge_conformance_rate' AS primary_l2_name
FROM mcp_funcdag_features
WHERE analysis_ready IS TRUE
UNION ALL
SELECT
    'mcp-recovery-v1' AS family,
    count(*) AS total_trials,
    sum(CASE WHEN task_success THEN 1 ELSE 0 END) AS success_trials,
    round(avg(CASE WHEN task_success THEN 1.0 ELSE 0.0 END), 4) AS success_rate,
    round(avg(schema_conformance_rate), 4) AS avg_schema_conformance_rate,
    round(avg(autonomous_recovery_rate), 4) AS primary_l2_rate,
    'autonomous_recovery_rate' AS primary_l2_name
FROM mcp_recovery_features
WHERE analysis_ready IS TRUE;

-- 7. Predictor Eligibility View
CREATE TABLE IF NOT EXISTS trajectory_feature_catalog (
    column_name VARCHAR PRIMARY KEY,
    data_type VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    family VARCHAR,
    construct VARCHAR,
    producer_module VARCHAR NOT NULL,
    available_before_verdict BOOLEAN,
    verdict_coupling VARCHAR,
    coupling_basis VARCHAR,
    denominator_policy VARCHAR,
    denominator_sibling VARCHAR,
    null_on_zero_denominator BOOLEAN NOT NULL,
    causal_grade VARCHAR,
    is_screening BOOLEAN NOT NULL
);

CREATE OR REPLACE VIEW v_predictor_eligibility AS
SELECT
    column_name AS feature_name,
    data_type,
    category,
    family,
    construct,
    producer_module,
    available_before_verdict,
    verdict_coupling,
    coupling_basis,
    denominator_policy,
    denominator_sibling,
    causal_grade,
    is_screening,
    (available_before_verdict = TRUE AND verdict_coupling IN ('independent', 'correlates') AND (verdict_coupling = 'independent' OR (coupling_basis IS NOT NULL AND coupling_basis != '')) AND (denominator_policy IS NOT NULL AND denominator_policy != '')) AS predictor_eligible,
    CASE
        WHEN available_before_verdict IS NULL THEN 'MISSING_TEMPORAL_AVAILABILITY'
        WHEN available_before_verdict = FALSE THEN 'POST_VERDICT_TEMPORAL_VIOLATION'
        WHEN verdict_coupling IS NULL OR verdict_coupling = '' THEN 'UNDECLARED_VERDICT_COUPLING'
        WHEN verdict_coupling = 'defines' THEN 'REWARD_DEFINITION_LEAKAGE'
        WHEN verdict_coupling = 'not_applicable' THEN 'NOT_APPLICABLE_FOR_PREDICTION'
        WHEN verdict_coupling = 'correlates' AND (coupling_basis IS NULL OR coupling_basis = '') THEN 'MISSING_COUPLING_EVIDENCE_BASIS'
        WHEN denominator_policy IS NULL OR denominator_policy = '' THEN 'MISSING_DENOMINATOR_APPLICABILITY_DECLARATION'
        ELSE 'ELIGIBLE'
    END AS predictor_refusal_code
FROM trajectory_feature_catalog;
