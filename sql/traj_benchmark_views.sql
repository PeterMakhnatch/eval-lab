-- Benchmark-Specific Trajectory Baseline Views & Matched Contrasts
-- Exposes:
--   v_action_memory_baseline: Context & Actionable Memory (action-memory-v1) L1 facts and L2 metrics
--   v_mcp_funcdag_baseline: Tool Selection, Composition & Value Propagation (mcp-funcdag-v1)
--   v_mcp_recovery_baseline: Error Detection & Autonomous Recovery (mcp-recovery-v1)
--   v_benchmark_contrasts: Matched-pair contrasts within benchmark families
--   v_benchmark_refusal_diagnostics: Underpowered/unsupported cross-cell refusal diagnostics
--   v_benchmark_summary: Unified headline coverage and metric summary

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

-- 1. Action Memory Baseline View
CREATE OR REPLACE VIEW v_action_memory_baseline AS
SELECT
    trial_id,
    family,
    task_id,
    seed,
    cell_id,
    arm,
    dose_bytes,
    construct,
    causal_grade,
    task_success,
    total_tool_calls,
    model_call_count,
    prompt_tokens_per_step,
    prompt_cache_hit_rate,
    raw_binding_opportunities,
    raw_conflicting_opportunities,
    bound_target_entity,
    bound_target_attribute,
    bound_target_value,
    binding_matched,
    stale_value_bound,
    schema_conformance_rate,
    binding_survival_rate,
    stale_value_override_rate,
    context_burn_velocity,
    occupancy_first_failure,
    citation,
    verifier_truth_digest
FROM action_memory_features;

-- 2. MCP-FuncDAG Baseline View
CREATE OR REPLACE VIEW v_mcp_funcdag_baseline AS
SELECT
    trial_id,
    family,
    task_id,
    seed,
    depth,
    width,
    distractor_count,
    name_similarity,
    schema_drift,
    task_success,
    total_tool_calls,
    model_call_count,
    prompt_tokens_per_step,
    prompt_cache_hit_rate,
    required_dag_edges,
    required_value_bindings,
    executed_dag_edges,
    correct_value_bindings,
    redundant_tool_calls,
    satisfied_edge_opportunities,
    first_edge_step,
    schema_conformance_rate,
    value_propagation_accuracy,
    dag_edge_conformance_rate,
    redundant_call_ratio,
    first_edge_latency,
    citation,
    verifier_truth_digest
FROM mcp_funcdag_features;

-- 3. MCP-Recovery Baseline View
CREATE OR REPLACE VIEW v_mcp_recovery_baseline AS
SELECT
    trial_id,
    family,
    task_id,
    seed,
    fault_class,
    persistence_level,
    mode,
    task_success,
    total_tool_calls,
    model_call_count,
    prompt_tokens_per_step,
    prompt_cache_hit_rate,
    injected_fault_record,
    injected_fault_count,
    fault_detected_count,
    post_fault_retries,
    blind_retries,
    certified_recovered_faults,
    step_to_first_fault,
    step_to_recovery,
    schema_conformance_rate,
    autonomous_recovery_rate,
    fault_detection_rate,
    blind_retry_rate,
    fault_recovery_latency,
    citation,
    verifier_truth_digest
FROM mcp_recovery_features;

-- 4. Matched-Pair Contrasts View
CREATE OR REPLACE VIEW v_benchmark_contrasts AS
WITH mem_pairs AS (
    SELECT
        c.seed,
        c.cell_id AS entity_id,
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
     AND c.arm = 'clean'
     AND t.arm != 'clean'
),
rec_pairs AS (
    SELECT
        c.seed,
        COALESCE(t.fault_class, 'unknown') AS entity_id,
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
    'action-memory-v1' AS benchmark_vertical,
    raw_binding_opportunities > 0 AS has_binding_eligibility,
    raw_conflicting_opportunities > 0 AS has_conflict_eligibility,
    (raw_binding_opportunities = 0 OR total_tool_calls = 0) AS is_refused_underpowered,
    CASE
        WHEN total_tool_calls = 0 THEN 'NO_TOOL_CALLS_INITIATED'
        WHEN raw_binding_opportunities = 0 THEN 'ZERO_OPPORTUNITY_UNDERPOWERED'
        ELSE NULL
    END AS refusal_reason
FROM action_memory_features
UNION ALL
SELECT
    trial_id,
    family,
    task_id,
    'mcp-funcdag-v1' AS benchmark_vertical,
    required_dag_edges > 0 AS has_binding_eligibility,
    required_value_bindings > 0 AS has_conflict_eligibility,
    (required_dag_edges = 0 OR total_tool_calls = 0) AS is_refused_underpowered,
    CASE
        WHEN total_tool_calls = 0 THEN 'NO_TOOL_CALLS_INITIATED'
        WHEN required_dag_edges = 0 THEN 'ZERO_REQUIRED_EDGES_UNDERPOWERED'
        ELSE NULL
    END AS refusal_reason
FROM mcp_funcdag_features
UNION ALL
SELECT
    trial_id,
    family,
    task_id,
    'mcp-recovery-v1' AS benchmark_vertical,
    injected_fault_count > 0 AS has_binding_eligibility,
    post_fault_retries > 0 AS has_conflict_eligibility,
    ((injected_fault_count = 0 AND mode = 'fault') OR total_tool_calls = 0) AS is_refused_underpowered,
    CASE
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
UNION ALL
SELECT
    'mcp-recovery-v1' AS family,
    count(*) AS total_trials,
    sum(CASE WHEN task_success THEN 1 ELSE 0 END) AS success_trials,
    round(avg(CASE WHEN task_success THEN 1.0 ELSE 0.0 END), 4) AS success_rate,
    round(avg(schema_conformance_rate), 4) AS avg_schema_conformance_rate,
    round(avg(autonomous_recovery_rate), 4) AS primary_l2_rate,
    'autonomous_recovery_rate' AS primary_l2_name
FROM mcp_recovery_features;
