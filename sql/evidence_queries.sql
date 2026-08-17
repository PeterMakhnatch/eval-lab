-- Evidence queries over the trial corpus (WS-E).
--
-- Reusable DuckDB views over the unified attach surface (`trial_facts`):
--   - outcome by task and agent (n, passes, pass rate, harness vs scored failures)
--   - task summary (total n, never_measured, measured, passes, scored failures)
--   - failure classification (harness exceptions vs genuine scored failures)
--   - exception taxonomy (exception class, phase, frequency, tasks affected)
--   - outcome by date bucket (temporal cluster breakdown)
--
-- Each view carries n alongside every rate.
-- Run via `evallab db attach` or in DuckDB with the attach surface.
--
-- Usage:
--   evallab db attach --query "SELECT * FROM v_outcome_by_task_agent"
-- Or in DuckDB with attached surface:
--   .read sql/evidence_queries.sql
--   SELECT * FROM v_outcome_by_task_agent;

-- Schema fallback for trial_facts when no parquet pre-registered / clean DuckDB
CREATE TABLE IF NOT EXISTS trial_facts (
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
    cost_usd DOUBLE
);

-- --------------------------------------------------------------------------- --
-- v_outcome_by_task_agent: n, passes, pass rate, Wilson-ready counts, split failures
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_outcome_by_task_agent AS
SELECT
    coalesce(task_name, 'unknown') AS task_name,
    coalesce(agent_name, 'unknown') AS agent_name,
    count(*) AS n,
    sum(CASE WHEN exception_class IS NULL THEN 1 ELSE 0 END) AS measured_n,
    sum(CASE WHEN exception_class IS NOT NULL THEN 1 ELSE 0 END) AS never_measured_n,
    sum(CASE WHEN primary_reward >= 1.0 AND exception_class IS NULL THEN 1 ELSE 0 END) AS passes,
    round(100.0 * sum(CASE WHEN primary_reward >= 1.0 AND exception_class IS NULL THEN 1 ELSE 0 END) / NULLIF(sum(CASE WHEN exception_class IS NULL THEN 1 ELSE 0 END), 0), 2) AS pass_rate_pct,
    sum(CASE WHEN exception_class IS NOT NULL THEN 1 ELSE 0 END) AS harness_exceptions_n,
    sum(CASE WHEN coalesce(primary_reward, 0.0) < 1.0 AND exception_class IS NULL THEN 1 ELSE 0 END) AS scored_failures_n,
    sum(CASE WHEN primary_reward >= 1.0 AND exception_class IS NULL THEN 1 ELSE 0 END) AS k_for_wilson,
    sum(CASE WHEN exception_class IS NULL THEN 1 ELSE 0 END) AS n_for_wilson
FROM trial_facts
GROUP BY task_name, agent_name
ORDER BY n DESC, task_name, agent_name;

-- --------------------------------------------------------------------------- --
-- v_task_summary: task-level overview (n, never_measured, measured, passes, fails)
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_task_summary AS
SELECT
    coalesce(task_name, 'unknown') AS task_name,
    count(*) AS n,
    sum(CASE WHEN exception_class IS NOT NULL THEN 1 ELSE 0 END) AS never_measured,
    sum(CASE WHEN exception_class IS NULL THEN 1 ELSE 0 END) AS measured,
    sum(CASE WHEN primary_reward >= 1.0 AND exception_class IS NULL THEN 1 ELSE 0 END) AS passes,
    sum(CASE WHEN coalesce(primary_reward, 0.0) < 1.0 AND exception_class IS NULL THEN 1 ELSE 0 END) AS scored_failures,
    round(100.0 * sum(CASE WHEN primary_reward >= 1.0 AND exception_class IS NULL THEN 1 ELSE 0 END) / NULLIF(sum(CASE WHEN exception_class IS NULL THEN 1 ELSE 0 END), 0), 2) AS pass_rate_pct
FROM trial_facts
GROUP BY task_name
ORDER BY task_name;

-- --------------------------------------------------------------------------- --
-- v_failure_classification: harness exceptions vs genuine scored failures
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_failure_classification AS
WITH classified AS (
    SELECT
        trial_id,
        task_name,
        agent_name,
        primary_reward,
        exception_class,
        CASE
            WHEN exception_class IS NOT NULL THEN 'harness_exception'
            WHEN coalesce(primary_reward, 0.0) < 1.0 THEN 'scored_failure'
            ELSE 'passed'
        END AS failure_type
    FROM trial_facts
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
-- v_exception_taxonomy: exception classes, phases, frequency, tasks affected
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_exception_taxonomy AS
SELECT
    coalesce(exception_class, 'none') AS exception_class,
    coalesce(exception_phase, 'unknown') AS exception_phase,
    count(*) AS n,
    count(DISTINCT task_name) AS tasks_affected
FROM trial_facts
WHERE exception_class IS NOT NULL
GROUP BY exception_class, exception_phase
ORDER BY n DESC, exception_class;

-- --------------------------------------------------------------------------- --
-- v_outcome_by_date_bucket: temporal cluster breakdown derived from job_name
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_outcome_by_date_bucket AS
SELECT
    CASE
        WHEN job_name LIKE '%20260814%' THEN '2026-08-14'
        WHEN job_name LIKE '%20260815%' THEN '2026-08-15'
        WHEN job_name LIKE '%20260816%' THEN '2026-08-16'
        ELSE 'control/smoke'
    END AS date_bucket,
    count(*) AS n,
    sum(CASE WHEN exception_class IS NOT NULL THEN 1 ELSE 0 END) AS exceptions_n,
    sum(CASE WHEN exception_class IS NULL THEN 1 ELSE 0 END) AS measured_n,
    sum(CASE WHEN primary_reward >= 1.0 AND exception_class IS NULL THEN 1 ELSE 0 END) AS passes_n
FROM trial_facts
GROUP BY date_bucket
ORDER BY date_bucket;