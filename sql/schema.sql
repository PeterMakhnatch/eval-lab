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
    t.evidence_path
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

CREATE OR REPLACE VIEW canary_drift_observations AS
WITH canary_trials AS (
    SELECT
        j.job_name,
        t.id AS trial_id,
        t.task_name,
        t.task_checksum,
        t.agent_name,
        t.primary_reward,
        t.exception_type,
        t.finished_at::timestamptz AS finished_at,
        (t.finished_at::timestamptz AT TIME ZONE current_setting('TIMEZONE'))::date
            AS observation_date
    FROM trials t
    JOIN jobs j ON j.id = t.job_id
    WHERE j.job_name LIKE 'canary-%'
      AND t.finished_at IS NOT NULL
), with_baseline AS (
    SELECT
        current_trial.*,
        baseline.n AS baseline_n,
        baseline.mean AS baseline_mean,
        baseline.stddev AS baseline_stddev,
        baseline.task_checksum AS baseline_task_checksum
    FROM canary_trials current_trial
    LEFT JOIN LATERAL (
        SELECT
            count(*) AS n,
            avg(prior.primary_reward) AS mean,
            stddev_samp(prior.primary_reward) AS stddev,
            mode() WITHIN GROUP (ORDER BY prior.task_checksum) AS task_checksum
        FROM canary_trials prior
        WHERE prior.task_name = current_trial.task_name
          AND prior.agent_name = current_trial.agent_name
          AND prior.exception_type IS NULL
          AND prior.finished_at >= date_trunc('day', current_trial.finished_at) - interval '7 days'
          AND prior.finished_at < date_trunc('day', current_trial.finished_at)
    ) baseline ON true
)
SELECT
    *,
    baseline_n > 0
        AND task_checksum IS DISTINCT FROM baseline_task_checksum AS task_version_changed,
    CASE
        WHEN baseline_n = 0 THEN false
        WHEN task_checksum IS DISTINCT FROM baseline_task_checksum THEN true
        WHEN primary_reward IS NULL OR baseline_mean IS NULL THEN false
        ELSE abs(primary_reward - baseline_mean) > COALESCE(baseline_stddev, 0)
    END AS is_harness_drift_suspect,
    CASE
        WHEN baseline_n = 0 THEN NULL
        WHEN task_checksum IS DISTINCT FROM baseline_task_checksum THEN 'task_version_changed'
        WHEN primary_reward IS NOT NULL
             AND baseline_mean IS NOT NULL
             AND abs(primary_reward - baseline_mean) > COALESCE(baseline_stddev, 0)
            THEN 'reward_excursion'
        ELSE NULL
    END AS drift_reason
FROM with_baseline;
