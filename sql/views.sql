-- Canonical join spine (E05) and related views.
--
-- Per §2.1: spec_id (experiments.id) → job_id (jobs.id) → trial_id (trials.id)
-- → {trajectory_documents, analysis_invocations, observation_records}
-- Left joins so trials without analysis/trajectory still appear (nulls).
-- Carries task_ref (task_name), task_version (task_checksum), agent_name.
--
-- Run in clean DuckDB (zero pre-created tables) via fallback schema tables:
--   duckdb -c ".read sql/views.sql" -c "SELECT * FROM v_spine LIMIT 5"
--
-- Or with variables for Parquet-backed facts if needed for full analytics.
-- Source: sql/schema.sql (Postgres catalog) + derived parquet for facts.

-- Fallback schema tables (DuckDB only; Postgres uses real tables)
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    source_kind TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    experiment_id TEXT,
    job_name TEXT
);

CREATE TABLE IF NOT EXISTS trials (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    trial_name TEXT,
    task_name TEXT,
    agent_name TEXT
);

CREATE TABLE IF NOT EXISTS trial_usage (
    trial_id TEXT PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    input_tokens BIGINT,
    cache_tokens BIGINT,
    output_tokens BIGINT
);

CREATE TABLE IF NOT EXISTS trajectory_documents (
    id TEXT PRIMARY KEY,
    trial_id TEXT
);

CREATE TABLE IF NOT EXISTS analysis_invocations (
    id TEXT PRIMARY KEY,
    source_trial_id TEXT
);

CREATE TABLE IF NOT EXISTS observation_records (
    trial_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS trial_outcomes (
    outcome_id TEXT PRIMARY KEY,
    trial_id TEXT NOT NULL,
    source_trial_id TEXT,
    outcome_kind TEXT NOT NULL,
    outcome_namespace TEXT NOT NULL DEFAULT 'harbor_verifier',
    outcome_name TEXT NOT NULL DEFAULT 'reward',
    reward_value DOUBLE,
    is_valid_reward BOOLEAN NOT NULL DEFAULT false,
    valid_fraction DOUBLE,
    agent_status TEXT NOT NULL,
    agent_exception TEXT,
    verifier_status TEXT NOT NULL,
    artifact_status TEXT NOT NULL,
    artifact_digest TEXT,
    source_digest TEXT NOT NULL,
    verifier_digest TEXT NOT NULL,
    evidence_digest TEXT,
    authority_state TEXT NOT NULL,
    superseded_by_outcome_id TEXT,
    supersession_reason TEXT,
    is_summable BOOLEAN NOT NULL DEFAULT false,
    cas_uri TEXT,
    evidence_path TEXT,
    recorded_at TEXT
);

CREATE OR REPLACE VIEW v_composite_outcome_validity AS
WITH normalized AS (
    SELECT
        outcomes.*,
        COALESCE(source_trial_id, trial_id) AS authority_trial_id
    FROM trial_outcomes AS outcomes
),
anchors AS (
    SELECT
        authority_trial_id,
        MAX(source_digest) FILTER (
            WHERE outcome_kind IN ('original_verifier', 'synthetic_fallback', 'manual_audit')
        ) AS source_digest,
        MAX(verifier_digest) FILTER (
            WHERE outcome_kind IN ('original_verifier', 'synthetic_fallback', 'manual_audit')
        ) AS verifier_digest,
        MAX(artifact_digest) FILTER (
            WHERE outcome_kind IN ('original_verifier', 'synthetic_fallback', 'manual_audit')
              AND artifact_status = 'preserved'
        ) AS artifact_digest
    FROM normalized
    GROUP BY authority_trial_id
),
classified AS (
    SELECT
        outcomes.*,
        (
            outcomes.outcome_kind = 'verifier_regrade'
            AND outcomes.source_trial_id = outcomes.authority_trial_id
            AND outcomes.source_digest = anchors.source_digest
            AND outcomes.verifier_digest = anchors.verifier_digest
            AND outcomes.artifact_digest = anchors.artifact_digest
            AND outcomes.artifact_status = 'preserved'
            AND outcomes.verifier_status = 'regrade_valid'
            AND outcomes.is_valid_reward
            AND outcomes.reward_value IS NOT NULL
        ) AS is_valid_regrade,
        (
            outcomes.outcome_kind = 'original_verifier'
            AND outcomes.verifier_status = 'completed'
            AND outcomes.artifact_status = 'preserved'
            AND outcomes.is_valid_reward
            AND outcomes.reward_value IS NOT NULL
        ) AS is_valid_original
    FROM normalized AS outcomes
    JOIN anchors USING (authority_trial_id)
),
summary AS (
    SELECT
        authority_trial_id,
        COUNT(*) FILTER (WHERE outcome_kind = 'verifier_regrade') AS regrade_count,
        COUNT(*) FILTER (WHERE is_valid_regrade) AS valid_regrade_count,
        COUNT(DISTINCT reward_value) FILTER (WHERE is_valid_regrade) AS regrade_reward_count,
        COUNT(DISTINCT artifact_digest) FILTER (WHERE is_valid_regrade) AS regrade_artifact_count,
        COUNT(*) FILTER (WHERE is_valid_original) AS valid_original_count,
        COUNT(*) FILTER (WHERE outcome_kind = 'inspect_scorer') AS inspect_count
    FROM classified
    GROUP BY authority_trial_id
),
decision AS (
    SELECT
        authority_trial_id,
        CASE
            WHEN regrade_count > valid_regrade_count THEN 'disputed'
            WHEN regrade_reward_count > 1 OR regrade_artifact_count > 1 THEN 'disputed'
            WHEN valid_regrade_count > 0 THEN 'regrade_authoritative'
            WHEN valid_original_count > 0 THEN 'original_verifier_authoritative'
            WHEN inspect_count > 0 AND regrade_count = 0 THEN 'non_decision'
            ELSE 'unresolved_verifier_timeout'
        END AS authority_axis,
        CASE
            WHEN regrade_count > valid_regrade_count THEN 'invalid_regrade_lineage'
            WHEN regrade_reward_count > 1 OR regrade_artifact_count > 1
                THEN 'conflicting_regrades'
            ELSE NULL
        END AS refusal_reason
    FROM summary
),
ranked AS (
    SELECT
        classified.*,
        decision.authority_axis,
        decision.refusal_reason,
        ROW_NUMBER() OVER (
            PARTITION BY classified.authority_trial_id
            ORDER BY
                CASE
                    WHEN decision.authority_axis = 'regrade_authoritative'
                         AND classified.is_valid_regrade THEN 0
                    WHEN decision.authority_axis = 'original_verifier_authoritative'
                         AND classified.is_valid_original THEN 0
                    WHEN classified.outcome_kind = 'original_verifier' THEN 1
                    WHEN classified.outcome_kind = 'synthetic_fallback' THEN 2
                    WHEN classified.outcome_kind = 'verifier_regrade' THEN 3
                    ELSE 4
                END,
                classified.outcome_id
        ) AS ranking
    FROM classified
    JOIN decision USING (authority_trial_id)
)
SELECT
    authority_trial_id AS trial_id,
    agent_status AS agent_axis,
    agent_exception,
    verifier_status AS verifier_axis,
    artifact_status AS artifact_axis,
    authority_axis,
    CASE
        WHEN authority_axis IN ('regrade_authoritative', 'original_verifier_authoritative')
            THEN reward_value
        ELSE NULL
    END AS resolved_reward,
    (
        authority_axis IN ('regrade_authoritative', 'original_verifier_authoritative')
        AND is_summable
        AND artifact_status = 'preserved'
        AND is_valid_reward
    ) AS is_admissible_for_aggregation,
    (
        authority_axis IN ('regrade_authoritative', 'original_verifier_authoritative')
        AND artifact_status = 'preserved'
        AND is_valid_reward
    ) AS is_valid_result,
    CASE
        WHEN authority_axis IN ('regrade_authoritative', 'original_verifier_authoritative')
            THEN outcome_id
        ELSE NULL
    END AS authoritative_outcome_id,
    refusal_reason
FROM ranked
WHERE ranking = 1;

CREATE OR REPLACE VIEW v_reward_authority AS
SELECT
    composite.trial_id,
    composite.resolved_reward AS authoritative_reward,
    composite.is_admissible_for_aggregation AS is_authoritative_summable,
    COUNT(outcomes.outcome_id) FILTER (
        WHERE (
            composite.authoritative_outcome_id IS NOT NULL
            AND outcomes.outcome_id <> composite.authoritative_outcome_id
            AND outcomes.outcome_kind <> 'inspect_scorer'
        ) OR (
            composite.authoritative_outcome_id IS NULL
            AND outcomes.outcome_kind = 'synthetic_fallback'
        )
    ) AS superseded_count,
    COUNT(outcomes.outcome_id) FILTER (
        WHERE outcomes.outcome_kind = 'synthetic_fallback'
          AND outcomes.outcome_id IS DISTINCT FROM composite.authoritative_outcome_id
    ) AS superseded_synthetic_count,
    composite.authority_axis = 'disputed' AS is_disputed,
    composite.refusal_reason
FROM v_composite_outcome_validity AS composite
LEFT JOIN trial_outcomes AS outcomes
    ON COALESCE(outcomes.source_trial_id, outcomes.trial_id) = composite.trial_id
GROUP BY
    composite.trial_id,
    composite.resolved_reward,
    composite.is_admissible_for_aggregation,
    composite.authoritative_outcome_id,
    composite.authority_axis,
    composite.refusal_reason;

CREATE OR REPLACE VIEW v_spine AS
SELECT
    e.id AS spec_id,
    j.id AS job_id,
    t.id AS trial_id,
    t.trial_name,
    t.task_name AS task_ref,
    t.agent_name,
    td.id AS trajectory_document_id,
    ai.id AS analysis_id,
    obs.trial_id IS NOT NULL AS has_observation
FROM experiments e
JOIN jobs j ON j.experiment_id = e.id
JOIN trials t ON t.job_id = j.id
LEFT JOIN trajectory_documents td ON td.trial_id = t.id
LEFT JOIN analysis_invocations ai ON ai.source_trial_id = t.id
LEFT JOIN observation_records obs ON obs.trial_id = t.id
ORDER BY e.id, j.id, t.id;

-- 2. v_quota_today: per-provider consumption for current UTC day (§2.3, §3.1)
CREATE OR REPLACE VIEW v_quota_today AS
SELECT
    t.agent_name AS provider,
    count(*) AS runs,
    sum(coalesce(u.input_tokens, 0) + coalesce(u.output_tokens, 0)) AS tokens
FROM trials t
JOIN trial_usage u ON u.trial_id = t.id
WHERE u.started_at IS NOT NULL
  AND (u.started_at::timestamptz AT TIME ZONE 'UTC')::date = (current_timestamp AT TIME ZONE 'UTC')::date
GROUP BY t.agent_name
ORDER BY provider;