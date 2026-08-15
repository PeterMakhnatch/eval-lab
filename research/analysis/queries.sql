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

-- ============================================================================
-- DS: trajectory-intelligence queries (DATA-STRATEGY, 2026-08-15)
-- Definitions and interpretation boundaries: docs/research/trajectory-intelligence.md
-- Run from the repository root with DuckDB >= 1.5 over the LOCAL Zone 02 root.
-- For Zone 01 corpora substitute derived/parquet/external/<item>/ and keep the
-- zones separate unless the query names both deliberately.
-- Missing token/cost/reward/exit values stay NULL — never coerced.
-- ============================================================================

-- DS-1. Cross-model win/loss matrix per task x (agent, model): mean, sigma,
-- attempts. Interpret nothing under n_trials < 5 (literature-survey T1).
SELECT
    task_name,
    agent_name,
    COALESCE(model_name, 'adhoc') AS model_name,
    count(*)                      AS n_trials,
    round(avg(primary_reward), 4) AS mean_reward,
    round(coalesce(stddev_samp(primary_reward), 0), 4) AS reward_sigma,
    sum(CASE WHEN primary_reward >= 1.0 THEN 1 ELSE 0 END) AS wins,
    sum(CASE WHEN primary_reward = 0.0 THEN 1 ELSE 0 END)  AS losses
FROM read_parquet('derived/parquet/job_id=*/trial_id=*/trial_facts.parquet',
                  hive_partitioning = true, union_by_name = true)
WHERE primary_reward IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY task_name, mean_reward DESC, agent_name, model_name;

-- DS-2. Loop Index: LI = (N - D) / N over (function_name, arguments_sha256)
-- signatures per trial. Triage rank, not a failure label.
WITH per_signature AS (
    SELECT trial_id, function_name, arguments_sha256, count(*) AS occurrences
    FROM read_parquet('derived/parquet/job_id=*/trial_id=*/tool_calls.parquet',
                      hive_partitioning = true, union_by_name = true)
    GROUP BY 1, 2, 3
)
SELECT
    trial_id,
    sum(occurrences)                        AS n_calls,
    count(*)                                AS d_signatures,
    round((sum(occurrences) - count(*))::DOUBLE
          / nullif(sum(occurrences), 0), 4) AS loop_index,
    max_by(function_name, occurrences)      AS most_repeated_function,
    max(occurrences)                        AS worst_signature_repeats
FROM per_signature
GROUP BY trial_id
ORDER BY loop_index DESC NULLS LAST;

-- DS-3. Tool Efficiency Ratio: linked observations with NULL-or-zero exit code
-- over tool calls; unlinked calls reported separately (instrumentation vs
-- execution-quality signal — see interpretation boundary in the doc).
WITH linkage AS (
    SELECT
        tc.trial_id,
        tc.tool_call_id,
        o.source_call_id IS NOT NULL                                   AS linked,
        (o.source_call_id IS NOT NULL
         AND (o.command_exit_code IS NULL OR o.command_exit_code = 0)) AS linked_ok
    FROM read_parquet('derived/parquet/job_id=*/trial_id=*/tool_calls.parquet',
                      hive_partitioning = true, union_by_name = true) tc
    LEFT JOIN read_parquet('derived/parquet/job_id=*/trial_id=*/observations.parquet',
                           hive_partitioning = true, union_by_name = true) o
      ON o.trial_id = tc.trial_id AND o.step_id = tc.step_id
     AND o.source_call_id = tc.tool_call_id
)
SELECT
    trial_id,
    count(*)                                   AS tool_calls,
    count(*) FILTER (linked_ok)                AS linked_ok_observations,
    count(*) FILTER (NOT linked)               AS unlinked_calls,
    round(count(*) FILTER (linked_ok)::DOUBLE
          / nullif(count(*), 0), 4)            AS tool_efficiency_ratio
FROM linkage
GROUP BY trial_id
ORDER BY tool_efficiency_ratio ASC NULLS LAST;

-- DS-4. Context Bloat Velocity: regr_slope(prompt_tokens, llm_step_ordinal)
-- per trial; measured points and endpoints reported; <2 points -> NULL row
-- excluded by HAVING. Cross-adapter comparison requires matching metric
-- semantics first (doc caveat).
WITH llm_steps AS (
    SELECT
        trial_id,
        prompt_tokens,
        row_number() OVER (PARTITION BY trial_id ORDER BY step_id) AS llm_seq
    FROM read_parquet('derived/parquet/job_id=*/trial_id=*/steps.parquet',
                      hive_partitioning = true, union_by_name = true)
    WHERE llm_call_count > 0 AND prompt_tokens IS NOT NULL
)
SELECT
    trial_id,
    count(*)                                          AS measured_points,
    min(prompt_tokens)                                AS first_prompt_tokens,
    max(prompt_tokens)                                AS last_prompt_tokens,
    round(regr_slope(prompt_tokens, llm_seq), 2)      AS cbv_tokens_per_llm_step
FROM llm_steps
GROUP BY trial_id
HAVING count(*) >= 2
ORDER BY cbv_tokens_per_llm_step DESC NULLS LAST;

-- DS-5. Context spikes: step-to-step prompt-token deltas. Negative deltas
-- expose compaction resets; large positive jumps expose bulk tool-output
-- ingestion.
WITH ordered AS (
    SELECT
        trial_id, step_id, prompt_tokens,
        prompt_tokens - lag(prompt_tokens) OVER (
            PARTITION BY trial_id ORDER BY step_id) AS delta
    FROM read_parquet('derived/parquet/job_id=*/trial_id=*/steps.parquet',
                      hive_partitioning = true, union_by_name = true)
    WHERE prompt_tokens IS NOT NULL
)
SELECT
    trial_id,
    max(delta)                       AS max_jump_tokens,
    min(delta)                       AS min_delta_tokens,
    count(*) FILTER (delta < 0)      AS compaction_resets,
    arg_max(step_id, delta)          AS step_of_max_jump
FROM ordered
WHERE delta IS NOT NULL
GROUP BY trial_id
ORDER BY max_jump_tokens DESC NULLS LAST;

-- DS-6. Failure flags: mutually NON-exclusive per-trial flags. These populate
-- review queues; they never overwrite reviewed labels or rewards.
SELECT
    trial_id,
    task_name,
    agent_name,
    primary_reward,
    (exception_class ILIKE '%timeout%'
     OR exception_phase ILIKE '%timeout%')                      AS flag_timeout,
    (exception_class IS NULL
     AND (primary_reward IS NULL OR primary_reward < 1)
     AND step_count <= 3 AND tool_call_count = 0)               AS flag_surrender,
    (repeated_failed_command_count > 0)                          AS flag_repeated_failed_commands,
    (exception_class IS NOT NULL)                                AS flag_infra_exception
FROM read_parquet('derived/parquet/job_id=*/trial_id=*/trial_facts.parquet',
                  hive_partitioning = true, union_by_name = true)
ORDER BY flag_timeout DESC, flag_surrender DESC, trial_id;

-- DS-7. Flaky-verifier candidates: same task_digest + verifier_digest shows
-- both reward 1 and reward < 1 across exception-free trials. Candidate only —
-- agent/model differences and stochasticity may explain the split.
SELECT
    task_name,
    task_digest,
    verifier_digest,
    count(*)                                   AS exception_free_trials,
    count(DISTINCT agent_name)                 AS agents_involved,
    min(primary_reward)                        AS min_reward,
    max(primary_reward)                        AS max_reward
FROM read_parquet('derived/parquet/job_id=*/trial_id=*/trial_facts.parquet',
                  hive_partitioning = true, union_by_name = true)
WHERE exception_class IS NULL AND primary_reward IS NOT NULL
GROUP BY 1, 2, 3
HAVING max(primary_reward) >= 1 AND min(primary_reward) < 1
ORDER BY exception_free_trials DESC;

-- DS-8. Tool-hallucination candidates: calls with no linked observation.
-- Adapter loss, cancellation, or invalid tool selection — raw trace decides.
SELECT
    tc.trial_id,
    tc.function_name,
    count(*) AS unlinked_calls
FROM read_parquet('derived/parquet/job_id=*/trial_id=*/tool_calls.parquet',
                  hive_partitioning = true, union_by_name = true) tc
LEFT JOIN read_parquet('derived/parquet/job_id=*/trial_id=*/observations.parquet',
                       hive_partitioning = true, union_by_name = true) o
  ON o.trial_id = tc.trial_id AND o.step_id = tc.step_id
 AND o.source_call_id = tc.tool_call_id
WHERE o.source_call_id IS NULL
GROUP BY 1, 2
ORDER BY unlinked_calls DESC;

-- DS-9. Repeated failed commands: exact call signatures joined to non-zero
-- exits — the strongest deterministic loop evidence.
SELECT
    tc.trial_id,
    tc.function_name,
    tc.arguments_sha256,
    count(*)                             AS failed_repeats,
    sum(o.content_size_bytes)            AS failed_output_bytes
FROM read_parquet('derived/parquet/job_id=*/trial_id=*/tool_calls.parquet',
                  hive_partitioning = true, union_by_name = true) tc
JOIN read_parquet('derived/parquet/job_id=*/trial_id=*/observations.parquet',
                  hive_partitioning = true, union_by_name = true) o
  ON o.trial_id = tc.trial_id AND o.step_id = tc.step_id
 AND o.source_call_id = tc.tool_call_id
WHERE o.command_exit_code IS NOT NULL AND o.command_exit_code <> 0
GROUP BY 1, 2, 3
HAVING count(*) >= 2
ORDER BY failed_repeats DESC, failed_output_bytes DESC;

-- DS-10. Harness sensitivity: reward spread across scaffolds for a fixed
-- (task, model) — the local instrument for arXiv:2602.07150. Requires
-- matched attempts per the comparison protocol.
SELECT
    task_name,
    COALESCE(model_name, 'adhoc')     AS model_name,
    count(DISTINCT agent_name || '@' || COALESCE(agent_version, '?'))
                                      AS scaffolds_tested,
    round(max(mean_by_scaffold) - min(mean_by_scaffold), 4)
                                      AS scaffold_reward_spread,
    round(avg(sigma_by_scaffold), 4)  AS avg_within_scaffold_sigma
FROM (
    SELECT
        task_name, model_name, agent_name, agent_version,
        avg(primary_reward)                      AS mean_by_scaffold,
        coalesce(stddev_samp(primary_reward), 0) AS sigma_by_scaffold
    FROM read_parquet('derived/parquet/job_id=*/trial_id=*/trial_facts.parquet',
                      hive_partitioning = true, union_by_name = true)
    WHERE primary_reward IS NOT NULL
    GROUP BY 1, 2, 3, 4
)
GROUP BY 1, 2
HAVING count(DISTINCT agent_name || '@' || COALESCE(agent_version, '?')) >= 2
ORDER BY scaffold_reward_spread DESC;

-- DS-11. Token/cost coverage by model: how much of the corpus can support
-- token-efficiency claims at all. Run BEFORE any cost comparison.
SELECT
    COALESCE(model_name, 'unknown')             AS model_name,
    count(*)                                    AS trajectories,
    count(prompt_tokens)                        AS with_prompt_tokens,
    count(cost_usd)                             AS with_cost,
    round(count(prompt_tokens)::DOUBLE / count(*), 3) AS token_coverage,
    round(count(cost_usd)::DOUBLE / count(*), 3)      AS cost_coverage
FROM read_parquet('derived/parquet/job_id=*/trial_id=*/trajectories.parquet',
                  hive_partitioning = true, union_by_name = true)
GROUP BY 1
ORDER BY trajectories DESC;

-- DS-12. Token-waste hotspots: observation bytes attached to failed commands,
-- attributed to the calling function and task — where error-loop output
-- actually accumulates.
SELECT
    tf.task_name,
    tc.function_name,
    count(*)                         AS failed_observations,
    sum(o.content_size_bytes)        AS wasted_bytes,
    round(avg(o.content_size_bytes)) AS avg_bytes_per_failure
FROM read_parquet('derived/parquet/job_id=*/trial_id=*/observations.parquet',
                  hive_partitioning = true, union_by_name = true) o
JOIN read_parquet('derived/parquet/job_id=*/trial_id=*/tool_calls.parquet',
                  hive_partitioning = true, union_by_name = true) tc
  ON o.trial_id = tc.trial_id AND o.step_id = tc.step_id
 AND o.source_call_id = tc.tool_call_id
JOIN read_parquet('derived/parquet/job_id=*/trial_id=*/trial_facts.parquet',
                  hive_partitioning = true, union_by_name = true) tf
  ON o.trial_id = tf.trial_id
WHERE o.command_exit_code IS NOT NULL AND o.command_exit_code <> 0
GROUP BY 1, 2
ORDER BY wasted_bytes DESC;
