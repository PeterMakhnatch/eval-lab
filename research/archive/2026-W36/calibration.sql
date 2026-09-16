-- Calibration views over judge and verifier evidence.
--
-- Provides relational queries for:
--   - judge calibration history (sealed corpora, E09)
--   - verifier calibration history (SG-4, chance-corrected agreement against execution ground truth)
--   - verifier task-level candidate distributions for selection lift
--
-- Works in DuckDB (with attach surface) and PostgreSQL.

-- Fallback schema for clean DuckDB execution
CREATE TABLE IF NOT EXISTS judge_calibrations (
    record_id TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    status TEXT NOT NULL,
    judge_backend TEXT NOT NULL,
    judge_model TEXT NOT NULL,
    judge_engine_version TEXT,
    rubric_digest TEXT NOT NULL,
    corpus_digest TEXT NOT NULL,
    per_criterion_agreement JSON,
    mean_agreement DOUBLE PRECISION NOT NULL,
    agreement_floor DOUBLE PRECISION NOT NULL,
    meets_floor BOOLEAN NOT NULL,
    reportable BOOLEAN NOT NULL,
    document_count INTEGER NOT NULL,
    evaluated_on DATE NOT NULL,
    prediction_artifact TEXT NOT NULL,
    record_path TEXT NOT NULL,
    raw_record JSON,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS calibration_records (
    calib_id TEXT PRIMARY KEY,
    judge_model TEXT NOT NULL,
    rubric_digest TEXT NOT NULL,
    corpus_digest TEXT NOT NULL,
    per_criterion_agreement JSON,
    evaluated_at TIMESTAMPTZ NOT NULL,
    raw_record JSON,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- --------------------------------------------------------------------------- --
-- v_judge_calibration_history: sealed-corpus judge records
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_judge_calibration_history AS
SELECT
    record_id,
    family,
    status,
    judge_backend,
    judge_model,
    round(mean_agreement::numeric, 4) AS mean_agreement,
    meets_floor,
    reportable,
    document_count,
    evaluated_on
FROM judge_calibrations
ORDER BY evaluated_on DESC, record_id;

-- --------------------------------------------------------------------------- --
-- v_verifier_calibration_history: LLM-as-a-verifier calibration records
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_verifier_calibration_history AS
SELECT
    calib_id,
    judge_model,
    rubric_digest,
    corpus_digest,
    evaluated_at
FROM calibration_records
ORDER BY evaluated_at DESC, calib_id;

-- --------------------------------------------------------------------------- --
-- v_selection_lift_candidates: tasks with trials >= 3 suitable for best-of-k
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_selection_lift_candidates AS
SELECT
    coalesce(task_name, 'unknown') AS task_name,
    count(*) AS total_trials,
    sum(CASE WHEN exception_class IS NULL AND primary_reward IS NOT NULL THEN 1 ELSE 0 END) AS valid_attempts,
    sum(CASE WHEN primary_reward >= 1.0 AND exception_class IS NULL THEN 1 ELSE 0 END) AS pass_count,
    round(avg(CASE WHEN exception_class IS NULL AND primary_reward IS NOT NULL THEN primary_reward ELSE NULL END)::numeric, 4) AS pass_at_1,
    max(CASE WHEN exception_class IS NULL AND primary_reward IS NOT NULL THEN primary_reward ELSE NULL END) AS oracle_ceiling
FROM trial_facts
GROUP BY coalesce(task_name, 'unknown')
HAVING count(*) >= 3
ORDER BY total_trials DESC, task_name;
