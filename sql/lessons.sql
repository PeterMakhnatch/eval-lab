-- Statistical lesson aggregation views (WS-D).
--
-- Joins:
--   v_failure_by_facet: craft facets ⋈ trials ⋈ analysis sidecars
--   v_loop_rate_by_env: observation records / trajectories ⋈ craft complexity
--   v_outcome_by_verifier_type: verifier types (pytest/golden/diff/hybrid) ⋈ pass rates and exception rates
--
-- Source inputs:
--   - craft: derived/parquet/craft/craft.parquet (deterministic task corpus scan)
--   - trial_facts: derived/parquet/**/trial_facts.parquet (or catalog trials)
--   - analysis_sidecars: stage-5 analysis sidecars (failure taxonomy & validity)
--   - observation_records: observatory trial observation records
--
-- Run from repository root:
--   duckdb -c ".read sql/lessons.sql" -c "SELECT * FROM v_outcome_by_verifier_type"
--
-- Or with custom variables:
--   SET VARIABLE craft_parquet = '/abs/path/to/craft.parquet';
--   SET VARIABLE trial_facts_parquet = '/abs/path/to/trial_facts.parquet';
--   .read sql/lessons.sql

SET VARIABLE craft_parquet = coalesce(
    getvariable('craft_parquet'),
    'derived/parquet/craft/craft.parquet'
);

SET VARIABLE trial_facts_parquet = coalesce(
    getvariable('trial_facts_parquet'),
    'derived/parquet/**/trial_facts.parquet'
);

-- Schema fallbacks for tables when not pre-registered in memory
CREATE TABLE IF NOT EXISTS craft_schema_fallback (
    task_ref VARCHAR,
    source_repo VARCHAR,
    version VARCHAR,
    task_digest VARCHAR,
    instruction_chars BIGINT,
    instruction_style VARCHAR,
    env_n_files BIGINT,
    env_languages VARCHAR[],
    env_services_n BIGINT,
    env_multi_container BOOLEAN,
    verifier_type VARCHAR,
    anti_cheat VARCHAR[],
    answer_hiding VARCHAR,
    difficulty_mechanism VARCHAR,
    human_minutes BIGINT,
    pinned_deps BOOLEAN,
    facets_schema_version VARCHAR,
    verifier_signals VARCHAR[],
    unresolved_facets VARCHAR[],
    base_image_pin VARCHAR
);

CREATE TABLE IF NOT EXISTS trial_facts_schema_fallback (
    experiment_id VARCHAR,
    job_id VARCHAR,
    trial_id VARCHAR,
    job_name VARCHAR,
    trial_name VARCHAR,
    task_name VARCHAR,
    task_digest VARCHAR,
    verifier_digest VARCHAR,
    environment_digest VARCHAR,
    agent_config_digest VARCHAR,
    agent_name VARCHAR,
    agent_version VARCHAR,
    model_name VARCHAR,
    primary_reward DOUBLE,
    exception_class VARCHAR,
    exception_phase VARCHAR,
    duration_seconds DOUBLE,
    environment_setup_seconds DOUBLE,
    agent_setup_seconds DOUBLE,
    agent_execution_seconds DOUBLE,
    verifier_seconds DOUBLE,
    input_tokens BIGINT,
    cache_tokens BIGINT,
    output_tokens BIGINT,
    cost_usd DOUBLE,
    trajectory_count BIGINT,
    invalid_trajectory_count BIGINT,
    step_count BIGINT,
    llm_call_count BIGINT,
    tool_call_count BIGINT,
    command_failure_count BIGINT,
    repeated_failed_command_count BIGINT,
    artifact_count BIGINT,
    missing_artifact_count BIGINT,
    artifact_set_digest VARCHAR
);

CREATE TABLE IF NOT EXISTS analysis_sidecars (
    analysis_id VARCHAR,
    job_id VARCHAR,
    source_trial_id VARCHAR,
    validity VARCHAR,
    primary_category VARCHAR,
    summary VARCHAR,
    earliest_failure_step_id BIGINT,
    confidence VARCHAR,
    validation_status VARCHAR
);

CREATE TABLE IF NOT EXISTS observation_records (
    trial_id VARCHAR,
    trial_name VARCHAR,
    job VARCHAR,
    agent VARCHAR,
    model VARCHAR,
    task VARCHAR,
    reward DOUBLE,
    steps_taken BIGINT,
    first_failure_step BIGINT,
    loop_detected BOOLEAN,
    loop_step BIGINT,
    verified_before_done BOOLEAN,
    tool_errors BIGINT,
    summary VARCHAR
);

CREATE TABLE IF NOT EXISTS craft AS SELECT * FROM craft_schema_fallback;
CREATE TABLE IF NOT EXISTS trial_facts AS SELECT * FROM trial_facts_schema_fallback;

-- --------------------------------------------------------------------------- --
-- 1. v_failure_by_facet: craft facets ⋈ trials ⋈ analysis sidecars
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_failure_by_facet AS
WITH trial_joined AS (
    SELECT
        c.source_repo,
        c.task_ref,
        c.task_digest,
        coalesce(c.verifier_type, 'unclassified') AS verifier_type,
        coalesce(c.instruction_style, 'unclassified') AS instruction_style,
        coalesce(c.difficulty_mechanism, 'unclassified') AS difficulty_mechanism,
        CASE
            WHEN c.env_multi_container IS TRUE THEN 'multi_container'
            WHEN c.env_multi_container IS FALSE THEN 'single_container'
            ELSE 'unspecified'
        END AS env_container_mode,
        coalesce(c.base_image_pin, 'unpinned') AS base_image_pin,
        CASE
            WHEN c.pinned_deps IS TRUE THEN 'pinned'
            WHEN c.pinned_deps IS FALSE THEN 'unpinned'
            ELSE 'unstated'
        END AS dependency_pinning,
        t.trial_id,
        t.job_id,
        t.primary_reward,
        t.exception_class,
        CASE
            WHEN coalesce(t.primary_reward, 0.0) < 1.0 OR t.exception_class IS NOT NULL THEN 1
            ELSE 0
        END AS is_failure,
        coalesce(
            a.primary_category,
            CASE
                WHEN t.exception_class IS NOT NULL THEN 'exception'
                WHEN coalesce(t.primary_reward, 0.0) < 1.0 THEN 'unscored_failure'
                ELSE 'none'
            END
        ) AS failure_category,
        coalesce(
            a.validity,
            CASE
                WHEN coalesce(t.primary_reward, 0.0) >= 1.0 THEN 'passed'
                WHEN t.exception_class IS NOT NULL THEN 'harness_failure'
                ELSE 'valid_agent_attempt'
            END
        ) AS validity
    FROM craft c
    JOIN trial_facts t
        ON (c.task_digest IS NOT NULL AND c.task_digest = t.task_digest)
        OR (c.task_ref IS NOT NULL AND t.task_name IS NOT NULL AND (
            c.task_ref = t.task_name
            OR c.task_ref LIKE '%' || t.task_name
            OR t.task_name LIKE '%' || c.task_ref
        ))
    LEFT JOIN analysis_sidecars a
        ON a.source_trial_id = t.trial_id
),
faceted_trials AS (
    SELECT source_repo, 'verifier_type' AS facet_name, verifier_type AS facet_value, failure_category, validity, is_failure, trial_id FROM trial_joined
    UNION ALL
    SELECT source_repo, 'instruction_style' AS facet_name, instruction_style AS facet_value, failure_category, validity, is_failure, trial_id FROM trial_joined
    UNION ALL
    SELECT source_repo, 'difficulty_mechanism' AS facet_name, difficulty_mechanism AS facet_value, failure_category, validity, is_failure, trial_id FROM trial_joined
    UNION ALL
    SELECT source_repo, 'env_container_mode' AS facet_name, env_container_mode AS facet_value, failure_category, validity, is_failure, trial_id FROM trial_joined
    UNION ALL
    SELECT source_repo, 'base_image_pin' AS facet_name, base_image_pin AS facet_value, failure_category, validity, is_failure, trial_id FROM trial_joined
    UNION ALL
    SELECT source_repo, 'dependency_pinning' AS facet_name, dependency_pinning AS facet_value, failure_category, validity, is_failure, trial_id FROM trial_joined
)
SELECT
    source_repo,
    facet_name,
    facet_value,
    failure_category,
    validity,
    count(*) AS n,
    sum(is_failure) AS failures_n,
    round(100.0 * sum(is_failure) / count(*), 2) AS failure_rate_pct
FROM faceted_trials
GROUP BY source_repo, facet_name, facet_value, failure_category, validity
ORDER BY source_repo, facet_name, n DESC, failures_n DESC, facet_value;

-- --------------------------------------------------------------------------- --
-- 2. v_loop_rate_by_env: observation records / trajectories ⋈ craft complexity
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_loop_rate_by_env AS
WITH env_joined AS (
    SELECT
        c.source_repo,
        coalesce(c.env_services_n, 1) AS env_services_n,
        coalesce(c.env_multi_container, false) AS env_multi_container,
        CASE
            WHEN c.env_n_files IS NULL THEN 'unknown'
            WHEN c.env_n_files = 0 THEN '0_files'
            WHEN c.env_n_files <= 5 THEN '1_to_5_files'
            WHEN c.env_n_files <= 20 THEN '6_to_20_files'
            ELSE 'over_20_files'
        END AS env_files_bucket,
        coalesce(c.difficulty_mechanism, 'unclassified') AS difficulty_mechanism,
        coalesce(obs.loop_detected, CASE WHEN coalesce(t.repeated_failed_command_count, 0) > 0 THEN true ELSE false END) AS loop_detected,
        coalesce(obs.steps_taken, coalesce(t.step_count, 0)) AS steps_taken,
        coalesce(obs.tool_errors, coalesce(t.command_failure_count, 0)) AS tool_errors,
        t.trial_id
    FROM craft c
    JOIN trial_facts t
        ON (c.task_digest IS NOT NULL AND c.task_digest = t.task_digest)
        OR (c.task_ref IS NOT NULL AND t.task_name IS NOT NULL AND (
            c.task_ref = t.task_name
            OR c.task_ref LIKE '%' || t.task_name
            OR t.task_name LIKE '%' || c.task_ref
        ))
    LEFT JOIN observation_records obs
        ON obs.trial_id = t.trial_id
)
SELECT
    source_repo,
    env_services_n,
    env_multi_container,
    env_files_bucket,
    count(*) AS n,
    sum(CASE WHEN loop_detected THEN 1 ELSE 0 END) AS loops_n,
    round(100.0 * sum(CASE WHEN loop_detected THEN 1 ELSE 0 END) / count(*), 2) AS loop_rate_pct,
    round(avg(steps_taken), 1) AS avg_steps,
    round(avg(tool_errors), 1) AS avg_tool_errors
FROM env_joined
GROUP BY source_repo, env_services_n, env_multi_container, env_files_bucket
ORDER BY source_repo, n DESC, loop_rate_pct DESC;

-- --------------------------------------------------------------------------- --
-- 3. v_outcome_by_verifier_type: verifier types ⋈ pass rates and exception rates
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_outcome_by_verifier_type AS
SELECT
    c.source_repo,
    coalesce(c.verifier_type, 'unclassified') AS verifier_type,
    count(*) AS n,
    sum(CASE WHEN coalesce(t.primary_reward, 0.0) >= 1.0 AND t.exception_class IS NULL THEN 1 ELSE 0 END) AS passed_n,
    round(100.0 * sum(CASE WHEN coalesce(t.primary_reward, 0.0) >= 1.0 AND t.exception_class IS NULL THEN 1 ELSE 0 END) / count(*), 2) AS pass_rate_pct,
    sum(CASE WHEN t.exception_class IS NOT NULL THEN 1 ELSE 0 END) AS exceptions_n,
    round(100.0 * sum(CASE WHEN t.exception_class IS NOT NULL THEN 1 ELSE 0 END) / count(*), 2) AS exception_rate_pct,
    sum(CASE WHEN coalesce(t.primary_reward, 0.0) < 1.0 AND t.exception_class IS NULL THEN 1 ELSE 0 END) AS failed_unexcepted_n,
    round(avg(t.duration_seconds), 2) AS avg_duration_seconds,
    round(avg(t.cost_usd), 4) AS avg_cost_usd
FROM craft c
JOIN trial_facts t
    ON (c.task_digest IS NOT NULL AND c.task_digest = t.task_digest)
    OR (c.task_ref IS NOT NULL AND t.task_name IS NOT NULL AND (
        c.task_ref = t.task_name
        OR c.task_ref LIKE '%' || t.task_name
        OR t.task_name LIKE '%' || c.task_ref
    ))
GROUP BY c.source_repo, c.verifier_type
ORDER BY c.source_repo, n DESC, verifier_type;
