-- Statistical lesson aggregation views (WS-D).
--
-- Joins:
--   v_failure_by_facet: craft facets ⋈ trials ⋈ one validated analysis sidecar
--   v_loop_rate_by_env: observation annotations ⋈ exact-digest trial/craft matches
--   v_outcome_by_verifier_type: measured capability rates with explicit exclusions
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
    validation_status VARCHAR,
    source_path VARCHAR,
    source_digest VARCHAR
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
-- 1. v_failure_by_facet: craft facets ⋈ trials ⋈ validated analysis sidecars
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_failure_by_facet AS
WITH validated_sidecars AS (
    SELECT * EXCLUDE (sidecar_rank)
    FROM (
        SELECT
            a.*,
            row_number() OVER (
                PARTITION BY a.source_trial_id
                ORDER BY
                    coalesce(a.source_path, ''),
                    coalesce(a.analysis_id, ''),
                    coalesce(a.job_id, ''),
                    coalesce(a.source_digest, ''),
                    coalesce(a.primary_category, ''),
                    coalesce(a.validity, ''),
                    coalesce(a.summary, ''),
                    coalesce(cast(a.earliest_failure_step_id AS VARCHAR), ''),
                    coalesce(a.confidence, '')
            ) AS sidecar_rank
        FROM analysis_sidecars a
        WHERE a.validation_status = 'valid'
          AND nullif(a.source_trial_id, '') IS NOT NULL
    )
    WHERE sidecar_rank = 1
),
trial_joined AS (
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
        CASE
            WHEN t.exception_class IS NOT NULL THEN NULL
            WHEN t.primary_reward IS NULL THEN NULL
            WHEN t.primary_reward < 1.0 THEN 1
            ELSE 0
        END AS is_failure,
        a.primary_category AS model_failure_category,
        a.validity AS model_validity,
        CASE WHEN a.source_trial_id IS NOT NULL THEN 'validated_analysis_sidecar' END
            AS model_diagnosis_source,
        a.analysis_id AS model_analysis_id,
        a.job_id AS model_analysis_job_id,
        a.source_path AS model_sidecar_path,
        a.source_digest AS model_sidecar_digest,
        CASE
            WHEN t.exception_class IS NOT NULL THEN 'exception'
            WHEN t.primary_reward IS NULL THEN 'unmeasured'
            WHEN t.primary_reward < 1.0 THEN 'unscored_failure'
            ELSE 'none'
        END AS mechanical_failure_category,
        CASE
            WHEN t.exception_class IS NOT NULL THEN 'exception_trial'
            WHEN t.primary_reward IS NULL THEN 'not_measured'
            WHEN t.primary_reward >= 1.0 THEN 'passed'
            ELSE 'measured_agent_attempt'
        END AS mechanical_validity,
        'trial_facts' AS mechanical_diagnosis_source
    FROM craft c
    JOIN trial_facts t
        ON c.task_digest IS NOT NULL
       AND t.task_digest IS NOT NULL
       AND c.task_digest = t.task_digest
    LEFT JOIN validated_sidecars a
        ON a.source_trial_id = t.trial_id
),
faceted_trials AS (
    SELECT source_repo, 'verifier_type' AS facet_name, verifier_type AS facet_value, * EXCLUDE (source_repo, verifier_type, instruction_style, difficulty_mechanism, env_container_mode, base_image_pin, dependency_pinning) FROM trial_joined
    UNION ALL
    SELECT source_repo, 'instruction_style', instruction_style, * EXCLUDE (source_repo, verifier_type, instruction_style, difficulty_mechanism, env_container_mode, base_image_pin, dependency_pinning) FROM trial_joined
    UNION ALL
    SELECT source_repo, 'difficulty_mechanism', difficulty_mechanism, * EXCLUDE (source_repo, verifier_type, instruction_style, difficulty_mechanism, env_container_mode, base_image_pin, dependency_pinning) FROM trial_joined
    UNION ALL
    SELECT source_repo, 'env_container_mode', env_container_mode, * EXCLUDE (source_repo, verifier_type, instruction_style, difficulty_mechanism, env_container_mode, base_image_pin, dependency_pinning) FROM trial_joined
    UNION ALL
    SELECT source_repo, 'base_image_pin', base_image_pin, * EXCLUDE (source_repo, verifier_type, instruction_style, difficulty_mechanism, env_container_mode, base_image_pin, dependency_pinning) FROM trial_joined
    UNION ALL
    SELECT source_repo, 'dependency_pinning', dependency_pinning, * EXCLUDE (source_repo, verifier_type, instruction_style, difficulty_mechanism, env_container_mode, base_image_pin, dependency_pinning) FROM trial_joined
)
SELECT
    source_repo,
    facet_name,
    facet_value,
    model_failure_category,
    model_validity,
    model_diagnosis_source,
    list(DISTINCT model_analysis_id ORDER BY model_analysis_id)
        FILTER (WHERE model_analysis_id IS NOT NULL) AS model_analysis_ids,
    list(DISTINCT model_analysis_job_id ORDER BY model_analysis_job_id)
        FILTER (WHERE model_analysis_job_id IS NOT NULL) AS model_analysis_job_ids,
    list(DISTINCT model_sidecar_path ORDER BY model_sidecar_path)
        FILTER (WHERE model_sidecar_path IS NOT NULL) AS model_sidecar_paths,
    list(DISTINCT model_sidecar_digest ORDER BY model_sidecar_digest)
        FILTER (WHERE model_sidecar_digest IS NOT NULL) AS model_sidecar_digests,
    mechanical_failure_category,
    mechanical_validity,
    mechanical_diagnosis_source,
    count(*) AS total_trials_n,
    count(is_failure) AS n,
    count(*) FILTER (WHERE mechanical_failure_category = 'exception') AS exceptions_n,
    count(*) FILTER (WHERE mechanical_failure_category = 'unmeasured') AS never_measured_n,
    count(*) FILTER (
        WHERE mechanical_failure_category IN ('exception', 'unmeasured')
    ) AS excluded_n,
    count(*) FILTER (WHERE is_failure = 1) AS failures_n,
    round(
        100.0 * count(*) FILTER (WHERE is_failure = 1) / nullif(count(is_failure), 0),
        2
    ) AS failure_rate_pct
FROM faceted_trials
GROUP BY
    source_repo,
    facet_name,
    facet_value,
    model_failure_category,
    model_validity,
    model_diagnosis_source,
    mechanical_failure_category,
    mechanical_validity,
    mechanical_diagnosis_source
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
        obs.loop_detected AS observation_loop_detected,
        obs.steps_taken AS observation_steps_taken,
        obs.tool_errors AS observation_tool_errors,
        'observation_markdown' AS observation_source,
        obs.trial_id IS NOT NULL AS is_annotated,
        t.trial_id
    FROM craft c
    JOIN trial_facts t
        ON c.task_digest IS NOT NULL
       AND t.task_digest IS NOT NULL
       AND c.task_digest = t.task_digest
    LEFT JOIN observation_records obs
        ON obs.trial_id = t.trial_id
)
SELECT
    source_repo,
    env_services_n,
    env_multi_container,
    env_files_bucket,
    observation_source,
    count(*) AS total_trials_n,
    count(*) FILTER (WHERE is_annotated) AS annotated_n,
    count(*) FILTER (WHERE NOT is_annotated) AS unannotated_n,
    count(observation_loop_detected) AS n,
    count(*) FILTER (WHERE observation_loop_detected IS TRUE) AS loops_n,
    round(
        100.0 * count(*) FILTER (WHERE observation_loop_detected IS TRUE)
        / nullif(count(observation_loop_detected), 0),
        2
    ) AS loop_rate_pct,
    round(avg(observation_steps_taken), 1) AS avg_observation_steps,
    round(avg(observation_tool_errors), 1) AS avg_observation_tool_errors
FROM env_joined
GROUP BY source_repo, env_services_n, env_multi_container, env_files_bucket, observation_source
ORDER BY source_repo, n DESC, loop_rate_pct DESC;

-- --------------------------------------------------------------------------- --
-- 3. v_outcome_by_verifier_type: verifier types ⋈ pass rates and exception rates
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_outcome_by_verifier_type AS
SELECT
    c.source_repo,
    coalesce(c.verifier_type, 'unclassified') AS verifier_type,
    count(*) AS total_trials_n,
    count(*) FILTER (
        WHERE t.primary_reward IS NOT NULL AND t.exception_class IS NULL
    ) AS n,
    count(*) FILTER (
        WHERE t.primary_reward >= 1.0 AND t.exception_class IS NULL
    ) AS passed_n,
    round(
        100.0 * count(*) FILTER (
            WHERE t.primary_reward >= 1.0 AND t.exception_class IS NULL
        ) / nullif(
            count(*) FILTER (
                WHERE t.primary_reward IS NOT NULL AND t.exception_class IS NULL
            ),
            0
        ),
        2
    ) AS pass_rate_pct,
    count(*) FILTER (WHERE t.exception_class IS NOT NULL) AS exceptions_n,
    count(*) FILTER (
        WHERE t.primary_reward IS NULL AND t.exception_class IS NULL
    ) AS never_measured_n,
    count(*) FILTER (
        WHERE t.exception_class IS NOT NULL
           OR (t.primary_reward IS NULL AND t.exception_class IS NULL)
    ) AS excluded_n,
    count(*) FILTER (
        WHERE t.primary_reward < 1.0 AND t.exception_class IS NULL
    ) AS failed_unexcepted_n,
    round(
        avg(t.duration_seconds) FILTER (
            WHERE t.primary_reward IS NOT NULL AND t.exception_class IS NULL
        ),
        2
    ) AS avg_duration_seconds,
    round(
        avg(t.cost_usd) FILTER (
            WHERE t.primary_reward IS NOT NULL AND t.exception_class IS NULL
        ),
        4
    ) AS avg_cost_usd
FROM craft c
JOIN trial_facts t
    ON c.task_digest IS NOT NULL
   AND t.task_digest IS NOT NULL
   AND c.task_digest = t.task_digest
GROUP BY c.source_repo, c.verifier_type
ORDER BY c.source_repo, n DESC, verifier_type;
