-- 02_cost_tokens.sql :: v1.0.0 (2026-09-08) :: DuckDB dialect (>= 1.0)
--
-- Purpose: cost and token accounting per job plus an overall rollup, sourced
-- from trial-level runs/*/result.json agent_result fields (the payer's view:
-- what the harness recorded per trial). Every mean and per-unit rate uses
-- NULLIF on its denominator, so a zero denominator yields NULL (unknown),
-- never 0 and never a division-by-zero error.
--
-- Evidence read (READ-ONLY): same result.json globs as 01; same
-- `trial_name IS NOT NULL` trial-level predicate (job-aggregate result.json
-- files would otherwise double-count costs: the job root already sums its
-- trials, e.g. canary-event-summary-codex-20260815 aggregates $0.1196).
--
-- Denominators:
--   n_trials_present = trial-level result.json files on disk.
--   n_costed         = agent_result.cost_usd IS NOT NULL (crashed-before-agent
--                      trials record NULL cost and are excluded from means).
--   n_scored         = reward IS NOT NULL (denominator for cost-per-success).
--   mean_*           = sum / NULLIF(n_costed, 0) -> NULL when nothing costed.
--   cost_per_success = cost_sum / NULLIF(n_success, 0) -> NULL on zero success.
--
-- Run (from repo root, read-only; in-memory DB, no pandas needed):
--   uv run --with duckdb python -c "import duckdb; cur=duckdb.connect().execute(open('research/analysis/harbor-native-track/queries/02_cost_tokens.sql').read()); print([d[0] for d in cur.description]); [print(r) for r in cur.fetchall()]"

WITH trial_costs AS (
    SELECT
        regexp_extract(filename, 'runs/([^/]+)/', 1) AS job,
        trial_name,
        verifier_result.rewards.reward AS reward,
        agent_result.n_input_tokens AS prompt_tokens,
        agent_result.n_output_tokens AS completion_tokens,
        agent_result.n_cache_tokens AS cached_tokens,
        agent_result.cost_usd AS cost_usd
    FROM read_json(
        ['runs/canary-*/**/result.json', 'runs/funcdag-codex-canary/**/result.json'],
        union_by_name = true,
        filename = true
    )
    WHERE trial_name IS NOT NULL  -- trial-level only; job aggregates would double-count
)
SELECT
    COALESCE(job, 'ALL_JOBS') AS job,
    count(*) AS n_trials_present,
    count(*) FILTER (WHERE cost_usd IS NOT NULL) AS n_costed,
    count(*) FILTER (WHERE reward IS NOT NULL) AS n_scored,
    count(*) FILTER (WHERE reward = 1.0) AS n_success,
    sum(prompt_tokens) AS prompt_tokens_sum,
    sum(completion_tokens) AS completion_tokens_sum,
    sum(cached_tokens) AS cached_tokens_sum,
    sum(cost_usd) AS cost_usd_sum,
    -- Means over costed trials; NULL (not 0) when no trial reported cost.
    sum(cost_usd) / NULLIF(count(*) FILTER (WHERE cost_usd IS NOT NULL), 0) AS mean_cost_usd_per_costed_trial,
    sum(prompt_tokens)::DOUBLE / NULLIF(count(*) FILTER (WHERE prompt_tokens IS NOT NULL), 0) AS mean_prompt_tokens_per_reporting_trial,
    sum(completion_tokens)::DOUBLE / NULLIF(count(*) FILTER (WHERE completion_tokens IS NOT NULL), 0) AS mean_completion_tokens_per_reporting_trial,
    -- Payer efficiency: spend per success; NULL when the job has zero successes.
    sum(cost_usd) / NULLIF(count(*) FILTER (WHERE reward = 1.0), 0) AS cost_usd_per_success
FROM trial_costs
GROUP BY GROUPING SETS ((job), ())
ORDER BY job;
