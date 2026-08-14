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
