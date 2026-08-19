-- Ingest reconciliation and completeness views (LOOP-INGEST).
--
-- Exposes:
--   v_ingest_reconciliation: per-trial reconciliation across catalog, parquet facts, and ATIF
--   v_ingest_summary: aggregate trial counts and projection rates by task and agent
--   v_ingest_gaps: filtered view of any cataloged trials missing parquet facts or trajectories
--   v_ingest_completeness: headline completeness invariant check
--
-- Run in clean DuckDB (zero pre-created tables) via fallback schema tables:
--   duckdb -c ".read sql/ingest_views.sql" -c "SELECT * FROM v_ingest_summary LIMIT 5"
--
-- Source: PostgreSQL catalog (jobs, trials) + derived parquet (trial_facts, trajectory_documents).

-- Fallback schema tables (DuckDB in-memory session; Postgres uses real tables)
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    source_kind TEXT,
    raw_provenance JSON
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    experiment_id TEXT,
    job_name TEXT,
    evidence_path TEXT,
    harbor_version TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_seconds DOUBLE,
    n_total_trials INTEGER,
    n_completed_trials INTEGER,
    n_errored_trials INTEGER
);

CREATE TABLE IF NOT EXISTS trials (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    trial_name TEXT,
    evidence_path TEXT,
    task_name TEXT,
    task_checksum TEXT,
    agent_name TEXT,
    agent_version TEXT,
    model_name TEXT,
    primary_reward DOUBLE,
    exception_type TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_seconds DOUBLE,
    input_tokens BIGINT,
    cache_tokens BIGINT,
    output_tokens BIGINT,
    cost_usd DOUBLE
);

CREATE TABLE IF NOT EXISTS trajectory_documents (
    id TEXT PRIMARY KEY,
    trial_id TEXT,
    validation_status TEXT,
    validator TEXT,
    validation_error TEXT
);

CREATE TABLE IF NOT EXISTS trial_facts (
    trial_id TEXT PRIMARY KEY,
    task_name TEXT,
    task_family TEXT,
    agent_name TEXT,
    model_name TEXT,
    is_success BOOLEAN,
    has_repeated_failure BOOLEAN,
    step_count INTEGER,
    llm_call_count INTEGER,
    tool_call_count INTEGER,
    command_failure_count INTEGER
);

CREATE TABLE IF NOT EXISTS trial_usage (
    trial_id TEXT PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    input_tokens BIGINT,
    cache_tokens BIGINT,
    output_tokens BIGINT
);

-- 1. v_ingest_reconciliation: trial-level join across all four stores
CREATE OR REPLACE VIEW v_ingest_reconciliation AS
SELECT
    j.id AS job_id,
    j.job_name,
    t.id AS trial_id,
    t.trial_name,
    t.task_name,
    coalesce(t.agent_name, 'unknown') AS agent_name,
    coalesce(t.model_name, 'unknown') AS model_name,
    t.primary_reward,
    t.exception_type,
    tf.trial_id IS NOT NULL AS has_facts,
    td.id IS NOT NULL AS has_trajectory,
    coalesce(td.validation_status, 'none') AS trajectory_status,
    CASE
        WHEN tf.trial_id IS NOT NULL AND (t.agent_name IN ('oracle', 'nop') OR td.id IS NOT NULL) THEN 'fully_projected'
        WHEN tf.trial_id IS NOT NULL THEN 'facts_only'
        ELSE 'unprojected'
    END AS projection_status
FROM jobs j
JOIN trials t ON t.job_id = j.id
LEFT JOIN trial_facts tf ON tf.trial_id = t.id
LEFT JOIN trajectory_documents td ON td.trial_id = t.id;

-- 2. v_ingest_summary: aggregate projection completeness by task and agent
CREATE OR REPLACE VIEW v_ingest_summary AS
SELECT
    coalesce(task_name, 'unknown') AS task_name,
    coalesce(agent_name, 'unknown') AS agent_name,
    count(*) AS total_trials,
    sum(CASE WHEN has_facts THEN 1 ELSE 0 END) AS projected_facts,
    sum(CASE WHEN has_trajectory THEN 1 ELSE 0 END) AS projected_trajectories,
    sum(CASE WHEN projection_status = 'fully_projected' THEN 1 ELSE 0 END) AS fully_projected_trials,
    round(100.0 * sum(CASE WHEN projection_status = 'fully_projected' THEN 1 ELSE 0 END) / count(*), 1) AS completeness_pct
FROM v_ingest_reconciliation
GROUP BY task_name, agent_name
ORDER BY task_name, agent_name;

-- 3. v_ingest_gaps: trials with missing parquet facts or trajectory documents
CREATE OR REPLACE VIEW v_ingest_gaps AS
SELECT
    job_id,
    job_name,
    trial_id,
    trial_name,
    task_name,
    agent_name,
    model_name,
    has_facts,
    has_trajectory,
    projection_status,
    CASE
        WHEN NOT has_facts AND NOT has_trajectory THEN 'missing_facts_and_trajectory'
        WHEN NOT has_facts THEN 'missing_parquet_facts'
        WHEN NOT has_trajectory AND agent_name NOT IN ('oracle', 'nop') THEN 'missing_trajectory_document'
        ELSE 'valid'
    END AS gap_reason
FROM v_ingest_reconciliation
WHERE projection_status != 'fully_projected';

-- 4. v_ingest_completeness: headline completeness invariant check
CREATE OR REPLACE VIEW v_ingest_completeness AS
SELECT
    count(*) AS total_catalog_trials,
    sum(CASE WHEN projection_status = 'fully_projected' THEN 1 ELSE 0 END) AS fully_projected_trials,
    sum(CASE WHEN projection_status != 'fully_projected' THEN 1 ELSE 0 END) AS gap_trials,
    count(DISTINCT job_id) AS total_catalog_jobs,
    CASE
        WHEN count(*) = sum(CASE WHEN projection_status = 'fully_projected' THEN 1 ELSE 0 END) THEN TRUE
        ELSE FALSE
    END AS is_complete
FROM v_ingest_reconciliation;
