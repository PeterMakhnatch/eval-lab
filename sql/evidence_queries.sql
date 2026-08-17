-- Evidence queries over the promoted trial corpus (WS-E).
--
-- Reusable DuckDB views for:
--   - outcome by task and agent (n, passes, pass rate, harness vs scored failures)
--   - exception taxonomy with date ranges
--
-- Each view carries n alongside every rate.
-- Run in clean DuckDB with zero pre-created tables (uses schema fallbacks).
--
-- Usage:
--   duckdb -c ".read sql/evidence_queries.sql" -c "SELECT * FROM v_outcome_by_task_agent"
-- Or with variables:
--   SET VARIABLE trial_evidence_parquet = '/path/to/trial_facts.parquet';
--   .read sql/evidence_queries.sql

SET VARIABLE trial_evidence_parquet = coalesce(
    getvariable('trial_evidence_parquet'),
    'derived/parquet/**/trial_facts.parquet'
);

-- Schema fallback for trial evidence when no parquet pre-registered
CREATE TABLE IF NOT EXISTS trial_evidence_schema_fallback (
    experiment_id VARCHAR,
    job_id VARCHAR,
    trial_id VARCHAR,
    job_name VARCHAR,
    trial_name VARCHAR,
    task_name VARCHAR,
    task_digest VARCHAR,
    agent_name VARCHAR,
    agent_version VARCHAR,
    model_name VARCHAR,
    primary_reward DOUBLE,
    reward DOUBLE,
    exception_class VARCHAR,
    exception_phase VARCHAR,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    duration_seconds DOUBLE,
    cost_usd DOUBLE
);

CREATE TABLE IF NOT EXISTS trial_evidence AS SELECT * FROM trial_evidence_schema_fallback;

-- --------------------------------------------------------------------------- --
-- v_outcome_by_task_agent: n, passes, pass rate, Wilson-ready counts, split failures
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_outcome_by_task_agent AS
SELECT
    coalesce(task_name, 'unknown') AS task_name,
    coalesce(agent_name, 'unknown') AS agent_name,
    count(*) AS n,
    sum(CASE WHEN coalesce(reward, primary_reward, 0.0) >= 1.0 AND exception_class IS NULL THEN 1 ELSE 0 END) AS passes,
    round(100.0 * sum(CASE WHEN coalesce(reward, primary_reward, 0.0) >= 1.0 AND exception_class IS NULL THEN 1 ELSE 0 END) / NULLIF(count(*), 0), 2) AS pass_rate_pct,
    sum(CASE WHEN exception_class IS NOT NULL THEN 1 ELSE 0 END) AS harness_exceptions_n,
    sum(CASE WHEN coalesce(reward, primary_reward, 0.0) < 1.0 AND exception_class IS NULL THEN 1 ELSE 0 END) AS scored_failures_n,
    sum(CASE WHEN coalesce(reward, primary_reward, 0.0) >= 1.0 AND exception_class IS NULL THEN 1 ELSE 0 END) AS k_for_wilson
FROM trial_evidence
GROUP BY task_name, agent_name
ORDER BY n DESC, task_name, agent_name;

-- --------------------------------------------------------------------------- --
-- v_failure_classification: harness exceptions vs genuine scored failures
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_failure_classification AS
WITH classified AS (
    SELECT
        trial_id,
        task_name,
        agent_name,
        coalesce(reward, primary_reward, 0.0) AS reward,
        exception_class,
        CASE
            WHEN exception_class IS NOT NULL THEN 'harness_exception'
            WHEN coalesce(reward, primary_reward, 0.0) < 1.0 THEN 'scored_failure'
            ELSE 'passed'
        END AS failure_type
    FROM trial_evidence
)
SELECT
    failure_type,
    count(*) AS n,
    count(DISTINCT task_name) AS distinct_tasks,
    count(DISTINCT agent_name) AS distinct_agents
FROM classified
GROUP BY failure_type
ORDER BY n DESC;

-- --------------------------------------------------------------------------- --
-- v_exception_taxonomy: exception classes, frequency, date range
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_exception_taxonomy AS
SELECT
    coalesce(exception_class, 'none') AS exception_class,
    count(*) AS n,
    min(started_at) AS first_seen,
    max(started_at) AS last_seen,
    count(DISTINCT task_name) AS tasks_affected
FROM trial_evidence
GROUP BY exception_class
ORDER BY n DESC, exception_class;

-- --------------------------------------------------------------------------- --
-- v_outcome_by_date_bucket: to check temporal comparability (08-14 cluster)
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_outcome_by_date_bucket AS
SELECT
    date_trunc('day', started_at) AS date_bucket,
    count(*) AS n,
    sum(CASE WHEN exception_class IS NOT NULL THEN 1 ELSE 0 END) AS exceptions_n,
    sum(CASE WHEN coalesce(reward, primary_reward, 0.0) >= 1.0 AND exception_class IS NULL THEN 1 ELSE 0 END) AS passes_n
FROM trial_evidence
GROUP BY date_bucket
ORDER BY date_bucket;