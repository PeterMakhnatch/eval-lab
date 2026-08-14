CREATE TABLE IF NOT EXISTS jobs (
    id uuid PRIMARY KEY,
    job_name text NOT NULL,
    evidence_path text NOT NULL,
    harbor_version text,
    started_at text,
    finished_at text,
    duration_seconds double precision,
    n_total_trials integer,
    n_completed_trials integer,
    n_errored_trials integer,
    raw_config jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_lock jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    lab_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS jobs_evidence_path_idx ON jobs (evidence_path);
CREATE INDEX IF NOT EXISTS jobs_started_at_idx ON jobs (started_at);

CREATE TABLE IF NOT EXISTS experiments (
    id text PRIMARY KEY,
    source_kind text NOT NULL,
    raw_provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    ingested_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS experiment_id text;
CREATE INDEX IF NOT EXISTS jobs_experiment_idx ON jobs (experiment_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'jobs_experiment_id_fkey'
    ) THEN
        ALTER TABLE jobs
            ADD CONSTRAINT jobs_experiment_id_fkey
            FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS trials (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    trial_name text NOT NULL,
    evidence_path text NOT NULL,
    task_name text,
    task_checksum text,
    agent_name text,
    agent_version text,
    model_name text,
    primary_reward double precision,
    exception_type text,
    started_at text,
    finished_at text,
    duration_seconds double precision,
    input_tokens bigint,
    cache_tokens bigint,
    output_tokens bigint,
    cost_usd double precision,
    raw_config jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_lock jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, trial_name)
);

CREATE INDEX IF NOT EXISTS trials_task_idx ON trials (task_name);
CREATE INDEX IF NOT EXISTS trials_agent_model_idx ON trials (agent_name, model_name);
CREATE INDEX IF NOT EXISTS trials_reward_idx ON trials (primary_reward);
CREATE INDEX IF NOT EXISTS trials_exception_idx ON trials (exception_type);

CREATE TABLE IF NOT EXISTS rewards (
    trial_id uuid NOT NULL REFERENCES trials(id) ON DELETE CASCADE,
    name text NOT NULL,
    value double precision NOT NULL,
    PRIMARY KEY (trial_id, name)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trial_id uuid NOT NULL REFERENCES trials(id) ON DELETE CASCADE,
    source text NOT NULL,
    destination text,
    artifact_type text,
    status text,
    service text,
    host_relative_path text,
    exists_on_disk boolean NOT NULL,
    size_bytes bigint,
    sha256 text
);

CREATE INDEX IF NOT EXISTS artifacts_trial_idx ON artifacts (trial_id);
CREATE INDEX IF NOT EXISTS artifacts_status_idx ON artifacts (status);
CREATE INDEX IF NOT EXISTS artifacts_sha256_idx ON artifacts (sha256);

CREATE TABLE IF NOT EXISTS run_files (
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    relative_path text NOT NULL,
    kind text NOT NULL,
    size_bytes bigint NOT NULL,
    sha256 text NOT NULL,
    PRIMARY KEY (job_id, relative_path)
);

CREATE INDEX IF NOT EXISTS run_files_kind_idx ON run_files (kind);
CREATE INDEX IF NOT EXISTS run_files_sha256_idx ON run_files (sha256);

CREATE TABLE IF NOT EXISTS trajectory_documents (
    id text PRIMARY KEY,
    trial_id uuid NOT NULL REFERENCES trials(id) ON DELETE CASCADE,
    source_path text NOT NULL,
    source_sha256 text NOT NULL,
    embedded_path text,
    schema_version text,
    session_id text,
    trajectory_id text,
    validation_status text NOT NULL,
    validator text NOT NULL,
    validation_error text,
    step_count integer NOT NULL,
    llm_call_count integer NOT NULL,
    parquet_path text
);

CREATE INDEX IF NOT EXISTS trajectory_documents_trial_idx
    ON trajectory_documents (trial_id);
CREATE INDEX IF NOT EXISTS trajectory_documents_status_idx
    ON trajectory_documents (validation_status);

CREATE TABLE IF NOT EXISTS deterministic_trial_facts (
    trial_id uuid PRIMARY KEY REFERENCES trials(id) ON DELETE CASCADE,
    verifier_digest text NOT NULL,
    environment_digest text NOT NULL,
    agent_config_digest text NOT NULL,
    exception_phase text,
    environment_setup_seconds double precision,
    agent_setup_seconds double precision,
    agent_execution_seconds double precision,
    verifier_seconds double precision,
    trajectory_count integer NOT NULL,
    invalid_trajectory_count integer NOT NULL,
    step_count integer NOT NULL,
    llm_call_count integer NOT NULL,
    tool_call_count integer NOT NULL,
    command_failure_count integer NOT NULL,
    repeated_failed_command_count integer NOT NULL,
    artifact_count integer NOT NULL,
    missing_artifact_count integer NOT NULL,
    artifact_set_digest text NOT NULL,
    raw_facts jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE VIEW trial_observations AS
SELECT
    j.job_name,
    j.harbor_version,
    j.ingested_at,
    t.id AS trial_id,
    t.trial_name,
    t.task_name,
    t.task_checksum,
    t.agent_name,
    t.agent_version,
    t.model_name,
    t.primary_reward,
    t.exception_type,
    t.duration_seconds,
    t.input_tokens,
    t.cache_tokens,
    t.output_tokens,
    t.cost_usd,
    t.evidence_path,
    j.experiment_id
FROM trials t
JOIN jobs j ON j.id = t.job_id;

CREATE OR REPLACE VIEW reward_summary AS
SELECT
    t.task_name,
    t.agent_name,
    COALESCE(t.model_name, 'adhoc') AS model_name,
    r.name AS reward_name,
    count(*) AS n,
    avg(r.value) AS mean,
    min(r.value) AS minimum,
    max(r.value) AS maximum
FROM trials t
JOIN rewards r ON r.trial_id = t.id
WHERE t.exception_type IS NULL
GROUP BY t.task_name, t.agent_name, COALESCE(t.model_name, 'adhoc'), r.name;

DROP VIEW IF EXISTS canary_trailing_7d;
DROP VIEW IF EXISTS canary_drift_observations;
DROP VIEW IF EXISTS canary_daily_outcomes;
DROP VIEW IF EXISTS canary_trial_observations;

CREATE VIEW canary_trial_observations AS
SELECT
    (
        t.finished_at::timestamptz
        AT TIME ZONE current_setting('TIMEZONE')
    )::date AS observation_date,
    j.lab_metadata #>> '{experiment,task}' AS task_name,
    j.lab_metadata #>> '{experiment,task_version}' AS task_version,
    t.agent_name,
    t.primary_reward,
    t.exception_type
FROM trials t
JOIN jobs j ON j.id = t.job_id
WHERE t.finished_at IS NOT NULL
  AND j.lab_metadata #>> '{experiment,policy_rule}' = 'canary';

CREATE VIEW canary_daily_outcomes AS
SELECT
    observation_date,
    task_name,
    task_version,
    agent_name,
    count(*) AS attempt_count,
    count(*) FILTER (WHERE exception_type IS NOT NULL) AS exception_count,
    avg(primary_reward) FILTER (WHERE exception_type IS NULL) AS reward
FROM canary_trial_observations
GROUP BY observation_date, task_name, task_version, agent_name;

CREATE VIEW canary_drift_observations AS
SELECT
    current.observation_date,
    current.task_name,
    current.task_version,
    current.agent_name,
    current.attempt_count,
    current.exception_count,
    current.reward,
    COALESCE(baseline.baseline_n, 0) AS baseline_n,
    baseline.baseline_mean,
    baseline.baseline_stddev,
    previous.task_version AS previous_task_version,
    previous.task_version IS NOT NULL
        AND previous.task_version IS DISTINCT FROM current.task_version
        AS task_version_changed,
    CASE
        WHEN previous.task_version IS NOT NULL
             AND previous.task_version IS DISTINCT FROM current.task_version THEN true
        WHEN current.exception_count > 0 THEN true
        WHEN baseline.baseline_n < 3 OR current.reward IS NULL
             OR baseline.baseline_mean IS NULL THEN false
        ELSE abs(current.reward - baseline.baseline_mean)
            > COALESCE(baseline.baseline_stddev, 0)
    END AS is_harness_drift_suspect,
    CASE
        WHEN previous.task_version IS NOT NULL
             AND previous.task_version IS DISTINCT FROM current.task_version
            THEN 'task_version_changed'
        WHEN current.exception_count > 0 THEN 'canary_exception'
        WHEN baseline.baseline_n >= 3
             AND current.reward IS NOT NULL
             AND baseline.baseline_mean IS NOT NULL
             AND abs(current.reward - baseline.baseline_mean)
                > COALESCE(baseline.baseline_stddev, 0)
            THEN 'reward_excursion'
        ELSE NULL
    END AS drift_reason
FROM canary_daily_outcomes current
LEFT JOIN LATERAL (
    SELECT
        count(*) AS baseline_n,
        avg(history.primary_reward) AS baseline_mean,
        stddev_samp(history.primary_reward) AS baseline_stddev
    FROM canary_trial_observations history
    WHERE history.task_name = current.task_name
      AND history.agent_name = current.agent_name
      AND history.task_version = current.task_version
      AND history.exception_type IS NULL
      AND history.observation_date >= current.observation_date - interval '7 days'
      AND history.observation_date < current.observation_date
) baseline ON true
LEFT JOIN LATERAL (
    SELECT prior.task_version
    FROM canary_daily_outcomes prior
    WHERE prior.task_name = current.task_name
      AND prior.agent_name = current.agent_name
      AND prior.observation_date < current.observation_date
    ORDER BY prior.observation_date DESC
    LIMIT 1
) previous ON true;
