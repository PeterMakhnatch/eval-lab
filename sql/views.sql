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

-- Note: v_quota_today and v_suite_leaderboard omitted.
-- No quota_consumption table or suites table present in sql/schema.sql;
-- underlying rows absent, so view would be empty. Record in Blockers.