-- Behavioral analysis views over the unified attach surface (trial_facts, steps, tool_calls).
--
-- Reusable DuckDB views over agent execution telemetry:
--   - v_behavior_effort_by_outcome: steps, tools, LLMs, seconds split by passed / scored_zero / never_measured
--   - v_behavior_efficiency: seconds-per-step and steps-per-reward (degenerate cases handled honestly as NULL)
--   - v_behavior_struggle_signals: repeated failed commands, command failures, invalid trajectories
--   - v_behavior_step_shape: trajectory source mix (system vs agent vs user) joined to outcome
--   - v_behavior_token_economics: token and cost metrics with explicit populated / total coverage
--   - v_behavior_trial_summary: per-trial unified behavioral breakdown
--
-- Every view carries `n` (and populated counts) beside every aggregate.
--
-- Run via `evallab db attach` or in DuckDB with the attach surface:
--   evallab db attach --query "SELECT * FROM v_behavior_effort_by_outcome"
-- Or standalone:
--   duckdb -c ".read sql/behavior.sql" -c "SELECT * FROM v_behavior_effort_by_outcome"

-- Schema fallbacks for tables when not pre-registered in memory / clean session
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
    environment_setup_seconds DOUBLE,
    agent_setup_seconds DOUBLE,
    agent_execution_seconds DOUBLE,
    verifier_seconds DOUBLE,
    input_tokens BIGINT,
    cache_tokens BIGINT,
    output_tokens BIGINT,
    cost_usd DOUBLE,
    trajectory_count BIGINT,
    invalid_trajectory_count BIGINT,
    step_count BIGINT,
    llm_call_count BIGINT,
    tool_call_count BIGINT,
    command_failure_count BIGINT,
    repeated_failed_command_count BIGINT,
    artifact_count BIGINT,
    missing_artifact_count BIGINT,
    artifact_set_digest VARCHAR
);

CREATE TABLE IF NOT EXISTS steps (
    job_id VARCHAR,
    trial_id VARCHAR,
    document_id VARCHAR,
    source_path VARCHAR,
    source_sha256 VARCHAR,
    step_id BIGINT,
    source VARCHAR,
    timestamp VARCHAR,
    model_name VARCHAR,
    is_copied_context BOOLEAN,
    llm_call_count BIGINT,
    prompt_tokens BIGINT,
    completion_tokens BIGINT,
    cached_tokens BIGINT,
    cost_usd DOUBLE,
    tool_call_count BIGINT,
    observation_count BIGINT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    job_id VARCHAR,
    trial_id VARCHAR,
    document_id VARCHAR,
    source_path VARCHAR,
    source_sha256 VARCHAR,
    step_id BIGINT,
    tool_call_id VARCHAR,
    function_name VARCHAR,
    arguments_sha256 VARCHAR
);

-- --------------------------------------------------------------------------- --
-- 1. v_behavior_trial_summary: per-trial behavioral classifications
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_behavior_trial_summary AS
SELECT
    trial_id,
    job_id,
    coalesce(task_name, 'unknown') AS task_name,
    coalesce(agent_name, 'unknown') AS agent_name,
    coalesce(model_name, 'adhoc') AS model_name,
    primary_reward,
    exception_class,
    exception_phase,
    CASE
        WHEN exception_class IS NOT NULL OR primary_reward IS NULL THEN 'never_measured'
        WHEN primary_reward >= 1.0 THEN 'passed'
        WHEN primary_reward = 0.0 THEN 'scored_zero'
        ELSE 'partial'
    END AS outcome,
    coalesce(step_count, 0) AS step_count,
    coalesce(tool_call_count, 0) AS tool_call_count,
    coalesce(llm_call_count, 0) AS llm_call_count,
    agent_execution_seconds,
    duration_seconds,
    CASE
        WHEN coalesce(step_count, 0) > 0 AND agent_execution_seconds IS NOT NULL
            THEN round(agent_execution_seconds / step_count, 2)
        ELSE NULL
    END AS seconds_per_step,
    CASE
        WHEN exception_class IS NULL AND primary_reward IS NOT NULL AND primary_reward > 0.0
            THEN round(coalesce(step_count, 0) / primary_reward, 2)
        ELSE NULL
    END AS steps_per_reward_point,
    coalesce(repeated_failed_command_count, 0) AS repeated_failed_command_count,
    coalesce(command_failure_count, 0) AS command_failure_count,
    coalesce(invalid_trajectory_count, 0) AS invalid_trajectory_count,
    input_tokens,
    cache_tokens,
    output_tokens,
    cost_usd
FROM trial_facts;

-- --------------------------------------------------------------------------- --
-- 2. v_behavior_effort_by_outcome: effort vs outcome breakdown
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_behavior_effort_by_outcome AS
SELECT
    task_name,
    agent_name,
    outcome,
    count(*) AS n,
    round(avg(step_count), 2) AS avg_steps,
    round(avg(tool_call_count), 2) AS avg_tool_calls,
    round(avg(llm_call_count), 2) AS avg_llm_calls,
    round(avg(agent_execution_seconds), 2) AS avg_execution_seconds,
    round(avg(duration_seconds), 2) AS avg_duration_seconds,
    round(avg(primary_reward), 4) AS avg_reward
FROM v_behavior_trial_summary
GROUP BY task_name, agent_name, outcome
ORDER BY task_name, agent_name, outcome;

-- --------------------------------------------------------------------------- --
-- 3. v_behavior_efficiency: seconds-per-step and steps-per-reward
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_behavior_efficiency AS
SELECT
    task_name,
    agent_name,
    outcome,
    count(*) AS n,
    round(avg(step_count), 2) AS avg_steps,
    round(avg(agent_execution_seconds), 2) AS avg_execution_seconds,
    round(avg(primary_reward), 4) AS avg_reward,
    CASE
        WHEN sum(step_count) > 0 AND sum(agent_execution_seconds) IS NOT NULL
            THEN round(sum(agent_execution_seconds) / sum(step_count), 2)
        ELSE NULL
    END AS seconds_per_step,
    CASE
        WHEN outcome = 'passed' AND sum(primary_reward) > 0
            THEN round(sum(step_count) / sum(primary_reward), 2)
        ELSE NULL
    END AS steps_per_reward_point
FROM v_behavior_trial_summary
GROUP BY task_name, agent_name, outcome
ORDER BY task_name, agent_name, outcome;

-- --------------------------------------------------------------------------- --
-- 4. v_behavior_struggle_signals: struggle signal distributions
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_behavior_struggle_signals AS
SELECT
    task_name,
    agent_name,
    outcome,
    count(*) AS n,
    round(avg(repeated_failed_command_count), 2) AS avg_repeated_failed_commands,
    max(repeated_failed_command_count) AS max_repeated_failed_commands,
    sum(CASE WHEN repeated_failed_command_count > 0 THEN 1 ELSE 0 END) AS trials_with_repeated_failures_n,
    round(avg(command_failure_count), 2) AS avg_command_failures,
    max(command_failure_count) AS max_command_failures,
    sum(CASE WHEN command_failure_count > 0 THEN 1 ELSE 0 END) AS trials_with_command_failures_n,
    round(avg(invalid_trajectory_count), 2) AS avg_invalid_trajectories,
    sum(CASE WHEN invalid_trajectory_count > 0 THEN 1 ELSE 0 END) AS trials_with_invalid_trajectories_n
FROM v_behavior_trial_summary
GROUP BY task_name, agent_name, outcome
ORDER BY task_name, agent_name, outcome;

-- --------------------------------------------------------------------------- --
-- 5. v_behavior_step_shape: step counts joined to source mix (system/agent/user)
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_behavior_step_shape AS
WITH step_counts AS (
    SELECT
        trial_id,
        count(*) AS total_steps,
        sum(CASE WHEN source = 'system' THEN 1 ELSE 0 END) AS system_steps,
        sum(CASE WHEN source = 'agent' THEN 1 ELSE 0 END) AS agent_steps,
        sum(CASE WHEN source = 'user' THEN 1 ELSE 0 END) AS user_steps
    FROM steps
    GROUP BY trial_id
)
SELECT
    t.task_name,
    t.agent_name,
    t.outcome,
    count(t.trial_id) AS n_trials,
    count(sc.trial_id) AS n_trials_with_steps,
    coalesce(sum(sc.total_steps), 0) AS total_steps_n,
    round(avg(t.step_count), 2) AS avg_steps_per_trial,
    coalesce(sum(sc.system_steps), 0) AS system_steps_n,
    coalesce(sum(sc.agent_steps), 0) AS agent_steps_n,
    coalesce(sum(sc.user_steps), 0) AS user_steps_n,
    round(100.0 * coalesce(sum(sc.system_steps), 0) / NULLIF(sum(sc.total_steps), 0), 1) AS system_pct,
    round(100.0 * coalesce(sum(sc.agent_steps), 0) / NULLIF(sum(sc.total_steps), 0), 1) AS agent_pct,
    round(100.0 * coalesce(sum(sc.user_steps), 0) / NULLIF(sum(sc.total_steps), 0), 1) AS user_pct
FROM v_behavior_trial_summary t
LEFT JOIN step_counts sc ON t.trial_id = sc.trial_id
GROUP BY t.task_name, t.agent_name, t.outcome
ORDER BY t.task_name, t.agent_name, t.outcome;

-- --------------------------------------------------------------------------- --
-- 6. v_behavior_token_economics: token and cost metrics with coverage counts
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_behavior_token_economics AS
SELECT
    task_name,
    agent_name,
    outcome,
    count(*) AS n_total,
    count(cost_usd) AS n_populated,
    count(cost_usd) || ' of ' || count(*) || ' trials' AS coverage_summary,
    round(100.0 * count(cost_usd) / count(*), 1) AS populated_pct,
    round(avg(input_tokens), 0) AS avg_input_tokens,
    round(avg(cache_tokens), 0) AS avg_cache_tokens,
    round(avg(output_tokens), 0) AS avg_output_tokens,
    round(avg(cost_usd), 4) AS avg_cost_usd,
    round(sum(cost_usd), 4) AS total_cost_usd
FROM v_behavior_trial_summary
GROUP BY task_name, agent_name, outcome
ORDER BY task_name, agent_name, outcome;
