-- Analyst conclusions and reasoning trajectory views (WS-A).
--
-- Views:
--   v_analysis_records: analyst conclusions with confidence, category, evidence count
--   v_analyst_trajectories: ordered reasoning steps taken by analysts
--   v_analysis_with_trajectory: joins conclusions to reasoning steps on analysis_id
--
-- Run from repository root:
--   duckdb -c ".read sql/analyst.sql" -c "SELECT * FROM v_analysis_records"
--
-- Or with custom variables:
--   SET VARIABLE analyses_parquet = '/abs/path/to/analyses.parquet';
--   SET VARIABLE analyst_trajectories_parquet = '/abs/path/to/analyst_trajectories.parquet';
--   .read sql/analyst.sql

SET VARIABLE analyses_parquet = coalesce(
    getvariable('analyses_parquet'),
    'derived/parquet/analyses/analyses.parquet'
);

SET VARIABLE analyst_trajectories_parquet = coalesce(
    getvariable('analyst_trajectories_parquet'),
    'derived/parquet/analyst_trajectories/analyst_trajectories.parquet'
);

-- Schema fallbacks for tables when not pre-registered in memory
CREATE TABLE IF NOT EXISTS analysis_records_schema_fallback (
    analysis_id VARCHAR,
    trial_id VARCHAR,
    rubric_digest VARCHAR,
    model VARCHAR,
    category VARCHAR,
    evidence_count BIGINT,
    confidence_level VARCHAR,
    confidence_n BIGINT,
    confidence_interval_low DOUBLE,
    confidence_interval_high DOUBLE,
    confidence_provenance VARCHAR,
    created_at VARCHAR
);

CREATE TABLE IF NOT EXISTS analyst_trajectories_schema_fallback (
    analysis_id VARCHAR,
    step_id BIGINT,
    source VARCHAR,
    timestamp VARCHAR,
    message VARCHAR
);

CREATE TABLE IF NOT EXISTS analysis_records AS SELECT * FROM analysis_records_schema_fallback;
CREATE TABLE IF NOT EXISTS analyst_trajectories AS SELECT * FROM analyst_trajectories_schema_fallback;

-- --------------------------------------------------------------------------- --
-- 1. v_analysis_records: stored analyst conclusions
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_analysis_records AS
SELECT
    analysis_id,
    trial_id,
    rubric_digest,
    model,
    category,
    evidence_count,
    confidence_level,
    confidence_n,
    confidence_interval_low,
    confidence_interval_high,
    confidence_provenance,
    created_at
FROM analysis_records
ORDER BY created_at DESC, analysis_id;

-- --------------------------------------------------------------------------- --
-- 2. v_analyst_trajectories: ordered reasoning steps
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_analyst_trajectories AS
SELECT
    analysis_id,
    step_id,
    source,
    timestamp,
    message
FROM analyst_trajectories
ORDER BY analysis_id, step_id;

-- --------------------------------------------------------------------------- --
-- 3. v_analysis_with_trajectory: conclusions joined with reasoning steps
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_analysis_with_trajectory AS
SELECT
    a.analysis_id,
    a.trial_id,
    a.model,
    a.category,
    a.evidence_count,
    a.confidence_level,
    a.created_at AS analysis_created_at,
    t.step_id,
    t.source AS step_source,
    t.timestamp AS step_timestamp,
    t.message AS step_message
FROM analysis_records a
JOIN analyst_trajectories t ON a.analysis_id = t.analysis_id
ORDER BY a.created_at DESC, a.analysis_id, t.step_id;
