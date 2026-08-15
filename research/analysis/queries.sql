-- Reward summary by fixed task + adapter + model. Use multiple attempts before
-- interpreting stochastic differences.
SELECT *
FROM reward_summary
ORDER BY task_name, reward_name, mean DESC, agent_name, model_name;

-- Trial-level analysis surface, including failures that have no reward.
SELECT
    job_name,
    trial_name,
    task_name,
    agent_name,
    COALESCE(model_name, 'adhoc') AS model_name,
    primary_reward,
    exception_type,
    duration_seconds,
    input_tokens,
    output_tokens,
    cost_usd,
    evidence_path
FROM trial_observations
ORDER BY ingested_at DESC, job_name, trial_name;

-- Separate infrastructure/adapter exceptions from scored failures.
SELECT
    task_name,
    agent_name,
    COALESCE(model_name, 'adhoc') AS model_name,
    exception_type,
    count(*) AS occurrences
FROM trial_observations
WHERE exception_type IS NOT NULL
GROUP BY task_name, agent_name, COALESCE(model_name, 'adhoc'), exception_type
ORDER BY occurrences DESC;

-- Artifact-boundary failures: the computation may have run, but required
-- evidence did not cross out of the agent environment.
SELECT
    j.job_name,
    t.trial_name,
    t.agent_name,
    a.source,
    a.status,
    a.host_relative_path
FROM artifacts a
JOIN trials t ON t.id = a.trial_id
JOIN jobs j ON j.id = t.job_id
WHERE a.status NOT IN ('ok', 'empty')
ORDER BY j.job_name, t.trial_name, a.source;

-- Slowest trials for capacity and timeout analysis.
SELECT
    job_name,
    task_name,
    agent_name,
    model_name,
    duration_seconds,
    primary_reward,
    exception_type
FROM trial_observations
WHERE duration_seconds IS NOT NULL
ORDER BY duration_seconds DESC
LIMIT 50;

-- Cost and token rollup. Oracle/no-op controls correctly remain null/zero.
SELECT
    task_name,
    agent_name,
    COALESCE(model_name, 'adhoc') AS model_name,
    count(*) AS trials,
    sum(input_tokens) AS input_tokens,
    sum(cache_tokens) AS cache_tokens,
    sum(output_tokens) AS output_tokens,
    sum(cost_usd) AS cost_usd
FROM trial_observations
GROUP BY task_name, agent_name, COALESCE(model_name, 'adhoc')
ORDER BY cost_usd DESC NULLS LAST;

-- Find duplicate artifacts by content even if jobs use different names/paths.
SELECT sha256, count(*) AS copies, sum(size_bytes) AS total_bytes
FROM run_files
WHERE kind = 'artifact' AND size_bytes > 0
GROUP BY sha256
HAVING count(*) > 1
ORDER BY total_bytes DESC;

-- DuckDB: join trial-level facts to ATIF documents without loading raw content.
SELECT
    f.experiment_id,
    f.job_name,
    f.trial_name,
    f.agent_name,
    f.primary_reward,
    count(t.document_id) AS trajectory_documents,
    sum(t.step_count) AS trajectory_steps
FROM read_parquet('derived/parquet/**/trial_facts.parquet') AS f
LEFT JOIN read_parquet('derived/parquet/**/trajectories.parquet') AS t
    USING (job_id, trial_id)
GROUP BY ALL
ORDER BY f.job_name, f.trial_name;

-- DuckDB: per-function tool use and structured command failures by trial.
SELECT
    s.job_id,
    s.trial_id,
    tc.function_name,
    count(*) AS calls,
    count(*) FILTER (WHERE o.command_exit_code <> 0) AS failed_calls
FROM read_parquet('derived/parquet/**/steps.parquet') AS s
JOIN read_parquet('derived/parquet/**/tool_calls.parquet') AS tc
    USING (job_id, trial_id, document_id, source_path, source_sha256, step_id)
LEFT JOIN read_parquet('derived/parquet/**/observations.parquet') AS o
    ON o.job_id = tc.job_id
   AND o.trial_id = tc.trial_id
   AND o.document_id = tc.document_id
   AND o.step_id = tc.step_id
   AND o.source_call_id = tc.tool_call_id
GROUP BY ALL
ORDER BY s.job_id, s.trial_id, calls DESC, tc.function_name;

-- PostgreSQL: the durable experiment -> job -> trial -> trajectory -> analysis path.
SELECT *
FROM experiment_trial_analysis_path
ORDER BY experiment_id, job_id, trial_id, trajectory_document_id, analysis_id;

-- Trajectory intelligence (DuckDB). Tests execute every statement between the
-- begin/end markers against typed Parquet fixtures.
-- BEGIN TRAJECTORY_INTELLIGENCE_DUCKDB

-- name: loop-index
WITH signature_counts AS (
    SELECT
        job_id,
        trial_id,
        count(*) AS tool_calls,
        count(DISTINCT concat(function_name, ':', arguments_sha256)) AS distinct_signatures
    FROM read_parquet('derived/parquet/**/tool_calls.parquet')
    GROUP BY job_id, trial_id
)
SELECT
    job_id,
    trial_id,
    tool_calls,
    distinct_signatures,
    (tool_calls - distinct_signatures)::DOUBLE / nullif(tool_calls, 0) AS loop_index
FROM signature_counts
ORDER BY loop_index DESC NULLS LAST, job_id, trial_id;

-- name: tool-efficiency-ratio
WITH call_outcomes AS (
    SELECT
        tc.job_id,
        tc.trial_id,
        tc.document_id,
        tc.tool_call_id,
        count(o.observation_index) AS linked_observations,
        max(CASE WHEN o.command_exit_code <> 0 THEN 1 ELSE 0 END) AS had_nonzero_exit
    FROM read_parquet('derived/parquet/**/tool_calls.parquet') AS tc
    LEFT JOIN read_parquet('derived/parquet/**/observations.parquet') AS o
        ON o.job_id = tc.job_id
       AND o.trial_id = tc.trial_id
       AND o.document_id = tc.document_id
       AND o.source_call_id = tc.tool_call_id
    GROUP BY tc.job_id, tc.trial_id, tc.document_id, tc.tool_call_id
)
SELECT
    job_id,
    trial_id,
    count(*) AS tool_calls,
    count(*) FILTER (WHERE linked_observations = 0) AS unlinked_calls,
    count(*) FILTER (
        WHERE linked_observations > 0 AND had_nonzero_exit = 0
    )::DOUBLE / nullif(count(*), 0) AS tool_efficiency_ratio
FROM call_outcomes
GROUP BY job_id, trial_id
ORDER BY tool_efficiency_ratio, job_id, trial_id;

-- name: context-bloat-velocity
WITH measured AS (
    SELECT
        job_id,
        trial_id,
        document_id,
        step_id,
        prompt_tokens,
        row_number() OVER (
            PARTITION BY job_id, trial_id, document_id ORDER BY step_id
        ) AS llm_step_ordinal
    FROM read_parquet('derived/parquet/**/steps.parquet')
    WHERE prompt_tokens IS NOT NULL
)
SELECT
    job_id,
    trial_id,
    document_id,
    count(*) AS measured_steps,
    first(prompt_tokens ORDER BY step_id) AS first_prompt_tokens,
    last(prompt_tokens ORDER BY step_id) AS last_prompt_tokens,
    regr_slope(prompt_tokens::DOUBLE, llm_step_ordinal::DOUBLE) AS context_bloat_velocity
FROM measured
GROUP BY job_id, trial_id, document_id
ORDER BY context_bloat_velocity DESC NULLS LAST, job_id, trial_id;

-- name: context-growth-spikes
WITH deltas AS (
    SELECT
        job_id,
        trial_id,
        document_id,
        step_id,
        prompt_tokens,
        prompt_tokens - lag(prompt_tokens) OVER (
            PARTITION BY job_id, trial_id, document_id ORDER BY step_id
        ) AS prompt_token_delta
    FROM read_parquet('derived/parquet/**/steps.parquet')
    WHERE prompt_tokens IS NOT NULL
)
SELECT *
FROM deltas
WHERE prompt_token_delta IS NOT NULL
ORDER BY abs(prompt_token_delta) DESC, job_id, trial_id, step_id;

-- name: flaky-verifier-candidates
SELECT
    task_digest,
    verifier_digest,
    count(*) AS trials,
    count(*) FILTER (WHERE primary_reward >= 1.0) AS passing_trials,
    count(*) FILTER (WHERE primary_reward < 1.0) AS failing_trials,
    count(DISTINCT coalesce(agent_config_digest, '')) AS agent_configs
FROM read_parquet('derived/parquet/**/trial_facts.parquet')
WHERE exception_class IS NULL AND primary_reward IS NOT NULL
GROUP BY task_digest, verifier_digest
HAVING passing_trials > 0 AND failing_trials > 0
ORDER BY trials DESC, task_digest;

-- name: tool-hallucination-candidates
SELECT
    tc.job_id,
    tc.trial_id,
    tc.document_id,
    tc.step_id,
    tc.tool_call_id,
    tc.function_name
FROM read_parquet('derived/parquet/**/tool_calls.parquet') AS tc
LEFT JOIN read_parquet('derived/parquet/**/observations.parquet') AS o
    ON o.job_id = tc.job_id
   AND o.trial_id = tc.trial_id
   AND o.document_id = tc.document_id
   AND o.source_call_id = tc.tool_call_id
GROUP BY ALL
HAVING count(o.observation_index) = 0
ORDER BY tc.job_id, tc.trial_id, tc.step_id, tc.tool_call_id;

-- name: timeout-failures
SELECT
    job_id,
    trial_id,
    task_name,
    agent_name,
    model_name,
    exception_class,
    exception_phase,
    duration_seconds
FROM read_parquet('derived/parquet/**/trial_facts.parquet')
WHERE lower(coalesce(exception_class, '')) LIKE '%timeout%'
   OR lower(coalesce(exception_phase, '')) LIKE '%timeout%'
ORDER BY duration_seconds DESC NULLS LAST, job_id, trial_id;

-- name: surrender-candidates
SELECT
    job_id,
    trial_id,
    task_name,
    agent_name,
    model_name,
    primary_reward,
    step_count,
    tool_call_count
FROM read_parquet('derived/parquet/**/trial_facts.parquet')
WHERE exception_class IS NULL
  AND (primary_reward IS NULL OR primary_reward < 1.0)
  AND step_count <= 3
  AND tool_call_count = 0
ORDER BY step_count, job_id, trial_id;

-- name: repeated-failed-commands
SELECT
    tc.job_id,
    tc.trial_id,
    tc.function_name,
    tc.arguments_sha256,
    count(*) AS failed_attempts,
    min(tc.step_id) AS first_failed_step,
    max(tc.step_id) AS last_failed_step
FROM read_parquet('derived/parquet/**/tool_calls.parquet') AS tc
JOIN read_parquet('derived/parquet/**/observations.parquet') AS o
    ON o.job_id = tc.job_id
   AND o.trial_id = tc.trial_id
   AND o.document_id = tc.document_id
   AND o.source_call_id = tc.tool_call_id
WHERE o.command_exit_code <> 0
GROUP BY tc.job_id, tc.trial_id, tc.function_name, tc.arguments_sha256
HAVING failed_attempts >= 2
ORDER BY failed_attempts DESC, tc.job_id, tc.trial_id;

-- name: token-cost-coverage
SELECT
    coalesce(model_name, 'unknown') AS model_name,
    count(*) AS trajectories,
    count(prompt_tokens) AS with_prompt_tokens,
    count(completion_tokens) AS with_completion_tokens,
    count(cost_usd) AS with_cost,
    sum(prompt_tokens) AS prompt_tokens,
    sum(completion_tokens) AS completion_tokens,
    sum(cost_usd) AS cost_usd
FROM read_parquet('derived/parquet/**/trajectories.parquet')
GROUP BY coalesce(model_name, 'unknown')
ORDER BY trajectories DESC, model_name;

-- END TRAJECTORY_INTELLIGENCE_DUCKDB
