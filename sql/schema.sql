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
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class r ON r.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = r.relnamespace
        WHERE c.conname = 'jobs_experiment_id_fkey'
          AND n.nspname = current_schema()
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

-- `session_id` is the bridge between this catalog and a Phoenix trace:
-- `harbor-atif2otel` copies it verbatim onto the converted root span as
-- `session.id`, so a span resolves to experiment -> job -> trial through this
-- column alone. It is indexed, and deliberately **not** unique.
--
-- Uniqueness holds in today's data (23/23 distinct) but not by construction.
-- ATIF v1.7 embedded subagent trajectories share their parent's `session_id`
-- and are disambiguated by `trajectory_id` (harbor_atif2otel/ids.py:45-55),
-- and `evallab.evidence.atif._flatten_payloads` writes one row per embedded payload,
-- so one multi-agent trial legitimately yields several rows with the same
-- `session_id`. A UNIQUE constraint here would abort that ingest. Callers
-- must therefore treat the lookup as one-to-many and refuse ambiguity
-- themselves; `evallab.tracing.resolve_session` does.
CREATE INDEX IF NOT EXISTS trajectory_documents_session_idx
    ON trajectory_documents (session_id);

CREATE TABLE IF NOT EXISTS deterministic_trial_facts (
    trial_id uuid PRIMARY KEY REFERENCES trials(id) ON DELETE CASCADE,
    verifier_digest text NOT NULL,
    environment_digest text NOT NULL,
    agent_config_digest text NOT NULL,
    grid_id text,
    point_id text,
    arm_id text,
    factor_values_json text,
    factor_values_digest text,
    factor_bindings_json text,
    factor_bindings_digest text,
    bound_execution_values_json text,
    bound_execution_values_digest text,
    preamble_path text,
    preamble_content_sha256 text,
    task_family text,
    task_id text,
    task_instance_id text,
    generator_seed_json text,
    task_block_inputs_json text,
    task_block_id text,
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

-- Additive migration for catalogs created before factor provenance existed.
-- Columns remain nullable because legacy rows have no source-grounded coordinates.
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS grid_id text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS point_id text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS arm_id text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS factor_values_json text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS factor_values_digest text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS factor_bindings_json text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS factor_bindings_digest text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS bound_execution_values_json text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS bound_execution_values_digest text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS preamble_path text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS preamble_content_sha256 text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS task_family text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS task_id text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS task_instance_id text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS generator_seed_json text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS task_block_inputs_json text;
ALTER TABLE deterministic_trial_facts ADD COLUMN IF NOT EXISTS task_block_id text;

CREATE TABLE IF NOT EXISTS analysis_invocations (
    id uuid PRIMARY KEY,
    source_trial_id uuid NOT NULL REFERENCES trials(id) ON DELETE CASCADE,
    sidecar_path text NOT NULL,
    sidecar_sha256 text NOT NULL,
    validation_status text NOT NULL,
    agent_name text NOT NULL,
    agent_version text NOT NULL,
    model_name text NOT NULL,
    prompt_digest text NOT NULL,
    rubric_digest text NOT NULL,
    output_schema_digest text NOT NULL,
    source_digests jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    input_tokens bigint,
    output_tokens bigint,
    cost_usd double precision,
    raw_sidecar jsonb NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE analysis_invocations ADD COLUMN IF NOT EXISTS sidecar_sha256 text;

CREATE INDEX IF NOT EXISTS analysis_invocations_trial_idx
    ON analysis_invocations (source_trial_id);
CREATE INDEX IF NOT EXISTS analysis_invocations_validation_idx
    ON analysis_invocations (validation_status);

CREATE TABLE IF NOT EXISTS analysis_findings (
    analysis_id uuid PRIMARY KEY REFERENCES analysis_invocations(id) ON DELETE CASCADE,
    validity text NOT NULL,
    primary_category text NOT NULL,
    summary text NOT NULL,
    earliest_failure_step_id integer,
    confidence text NOT NULL,
    proposed_discriminator text NOT NULL,
    alternative_explanations jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS analysis_evidence_citations (
    analysis_id uuid NOT NULL REFERENCES analysis_invocations(id) ON DELETE CASCADE,
    citation_index integer NOT NULL,
    source_path text NOT NULL,
    step_id integer,
    tool_call_id text,
    supports text NOT NULL,
    PRIMARY KEY (analysis_id, citation_index)
);

CREATE TABLE IF NOT EXISTS analysis_reviews (
    id uuid PRIMARY KEY,
    analysis_id uuid NOT NULL REFERENCES analysis_invocations(id) ON DELETE CASCADE,
    disposition text NOT NULL,
    rationale text NOT NULL,
    reviewer text NOT NULL,
    reviewed_at timestamptz NOT NULL,
    superseded_by uuid,
    review_path text NOT NULL
);

CREATE INDEX IF NOT EXISTS analysis_reviews_analysis_idx
    ON analysis_reviews (analysis_id);

CREATE OR REPLACE VIEW experiment_trial_analysis_path AS
SELECT
    e.id AS experiment_id,
    j.id AS job_id,
    t.id AS trial_id,
    td.id AS trajectory_document_id,
    ai.id AS analysis_id,
    ai.validation_status AS analysis_validation_status
FROM experiments e
JOIN jobs j ON j.experiment_id = e.id
JOIN trials t ON t.job_id = j.id
LEFT JOIN trajectory_documents td ON td.trial_id = t.id
LEFT JOIN analysis_invocations ai ON ai.source_trial_id = t.id;

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

CREATE TABLE IF NOT EXISTS verdicts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    discovery_id text NOT NULL,
    status text NOT NULL,
    "by" text NOT NULL,
    "at" timestamptz NOT NULL,
    note text,
    ingested_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS verdicts_discovery_idx ON verdicts (discovery_id);
CREATE INDEX IF NOT EXISTS verdicts_at_idx ON verdicts ("at" DESC);
CREATE INDEX IF NOT EXISTS verdicts_status_idx ON verdicts (status);

CREATE OR REPLACE VIEW v_verdicts_history AS
SELECT
    discovery_id,
    status,
    "by",
    "at",
    note
FROM verdicts
ORDER BY discovery_id, "at" ASC;

CREATE OR REPLACE VIEW v_current_verdicts AS
WITH ranked AS (
    SELECT
        discovery_id,
        status,
        "by",
        "at",
        note,
        row_number() OVER (
            PARTITION BY discovery_id
            ORDER BY "at" DESC
        ) AS ranking
    FROM verdicts
)
SELECT
    discovery_id,
    status,
    "by",
    "at",
    note
FROM ranked
WHERE ranking = 1
ORDER BY "at" DESC, discovery_id;

CREATE TABLE IF NOT EXISTS suites (
    name text NOT NULL,
    version text NOT NULL,
    frozen_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS suite_members (
    suite_name text NOT NULL,
    suite_version text NOT NULL,
    task_ref text NOT NULL,
    task_version text NOT NULL,
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (suite_name, suite_version, task_ref, task_version),
    FOREIGN KEY (suite_name, suite_version) REFERENCES suites(name, version) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS suite_members_task_idx ON suite_members (task_ref, task_version);
CREATE INDEX IF NOT EXISTS suite_members_suite_idx ON suite_members (suite_name, suite_version);

CREATE OR REPLACE FUNCTION check_suite_members_immutability()
RETURNS trigger AS $$
DECLARE
    v_frozen_at timestamptz;
    v_suite_name text;
    v_suite_version text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_suite_name := OLD.suite_name;
        v_suite_version := OLD.suite_version;
    ELSE
        v_suite_name := NEW.suite_name;
        v_suite_version := NEW.suite_version;
    END IF;

    SELECT frozen_at INTO v_frozen_at
    FROM suites
    WHERE name = v_suite_name AND version = v_suite_version;

    IF v_frozen_at IS NOT NULL THEN
        RAISE EXCEPTION 'Cannot modify membership of frozen suite %@% (frozen at %)',
            v_suite_name, v_suite_version, v_frozen_at;
    END IF;

    IF TG_OP = 'UPDATE' AND (OLD.suite_name <> NEW.suite_name OR OLD.suite_version <> NEW.suite_version) THEN
        SELECT frozen_at INTO v_frozen_at
        FROM suites
        WHERE name = OLD.suite_name AND version = OLD.suite_version;

        IF v_frozen_at IS NOT NULL THEN
            RAISE EXCEPTION 'Cannot modify membership of frozen suite %@% (frozen at %)',
                OLD.suite_name, OLD.suite_version, v_frozen_at;
        END IF;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    ELSE
        RETURN NEW;
    END IF;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_suite_members_immutability ON suite_members;
CREATE TRIGGER trg_suite_members_immutability
    BEFORE INSERT OR UPDATE OR DELETE ON suite_members
    FOR EACH ROW
    EXECUTE FUNCTION check_suite_members_immutability();

CREATE OR REPLACE FUNCTION check_suite_immutability()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.frozen_at IS NOT NULL THEN
            RAISE EXCEPTION 'Cannot delete frozen suite %@% (frozen at %)',
                OLD.name, OLD.version, OLD.frozen_at;
        END IF;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD.frozen_at IS NOT NULL THEN
            IF NEW.name <> OLD.name OR NEW.version <> OLD.version OR NEW.frozen_at IS DISTINCT FROM OLD.frozen_at THEN
                RAISE EXCEPTION 'Cannot modify frozen suite %@% (frozen at %)',
                    OLD.name, OLD.version, OLD.frozen_at;
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_suite_immutability ON suites;
CREATE TRIGGER trg_suite_immutability
    BEFORE UPDATE OR DELETE ON suites
    FOR EACH ROW
    EXECUTE FUNCTION check_suite_immutability();

CREATE OR REPLACE VIEW v_quota_today AS
SELECT
    agent_name AS provider,
    count(*) AS runs,
    sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)) AS tokens
FROM trials
WHERE started_at IS NOT NULL
  AND (started_at::timestamptz AT TIME ZONE 'UTC')::date = (now() AT TIME ZONE 'UTC')::date
GROUP BY agent_name
ORDER BY provider;

-- Track A5 interpretation artifacts: identity/index only, no raw JSON blobs.
CREATE TABLE IF NOT EXISTS interpretation_artifacts (
    artifact_digest text PRIMARY KEY,
    kind text NOT NULL,
    trial_id text NOT NULL,
    job_id text NOT NULL,
    content_digest text NOT NULL,
    artifact_path text NOT NULL,
    cas_uri text,
    pack_digest text,
    judgment_id text,
    decision_id text,
    ingested_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS interpretation_artifacts_trial_idx ON interpretation_artifacts (trial_id);
CREATE INDEX IF NOT EXISTS interpretation_artifacts_pack_idx ON interpretation_artifacts (pack_digest);
CREATE INDEX IF NOT EXISTS interpretation_artifacts_decision_idx ON interpretation_artifacts (decision_id);

CREATE TABLE IF NOT EXISTS machine_judgments (
    judgment_id text PRIMARY KEY,
    judgment_digest text NOT NULL,
    pack_digest text NOT NULL,
    producer_kind text NOT NULL,
    validity text NOT NULL,
    citation_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    coverage_gaps jsonb NOT NULL DEFAULT '[]'::jsonb,
    artifact_path text NOT NULL,
    cas_uri text,
    produced_at timestamptz NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS machine_judgments_pack_idx ON machine_judgments (pack_digest);

CREATE TABLE IF NOT EXISTS acceptance_decisions (
    decision_id text PRIMARY KEY,
    decision_digest text NOT NULL,
    decision text NOT NULL,
    judgment_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    pack_digest text NOT NULL,
    reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
    calibration_version text,
    calibration_schema text,
    status text NOT NULL,
    supersedes_decision_id text,
    artifact_path text NOT NULL,
    cas_uri text,
    produced_at timestamptz NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS acceptance_decisions_pack_idx ON acceptance_decisions (pack_digest);

CREATE OR REPLACE VIEW v_machine_judgment_decisions AS
SELECT
    d.decision_id,
    d.decision,
    d.pack_digest,
    d.reason_codes,
    m.judgment_id,
    m.producer_kind,
    m.validity,
    m.citation_ids,
    m.coverage_gaps
FROM acceptance_decisions d
LEFT JOIN machine_judgments m ON m.judgment_id = (d.judgment_ids->>0);

CREATE OR REPLACE VIEW v_current_acceptance_decisions AS
SELECT DISTINCT ON (pack_digest)
    decision_id,
    pack_digest,
    decision,
    reason_codes,
    produced_at,
    supersedes_decision_id
FROM acceptance_decisions
ORDER BY pack_digest, produced_at DESC;
