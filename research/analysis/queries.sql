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
