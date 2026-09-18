-- 01_trial_stats_per_job.sql :: v1.0.0 (2026-09-08) :: DuckDB dialect (>= 1.0)
--
-- Purpose: per-job trial statistics over Harbor runs with EXPLICIT scored /
-- unscored denominators. A trial is SCORED iff verifier_result.rewards.reward
-- IS NOT NULL. Unscored trials (harness crash before the verifier ran,
-- e.g. ValueError "Model name is required") MUST NOT enter success-rate
-- denominators. Success = reward = 1.0 (binary Harbor reward on this corpus).
--
-- Evidence read (READ-ONLY, no writes, no projections):
--   runs/canary-*/**/result.json and runs/funcdag-codex-canary/**/result.json
--   via read_json. Trial-level rows only: job-aggregate result.json files at
--   job roots and verifier/artifacts result.json files carry no trial_name and
--   are excluded by the `trial_name IS NOT NULL` predicate (do not remove it:
--   funcdag jobs nest verifier/result.json and artifacts/app/output/result.json
--   files that would otherwise pollute counts).
--
-- Denominators:
--   n_trials_present = trial-level result.json files on disk (job, trial).
--   n_scored         = reward IS NOT NULL (capability denominator).
--   n_unscored       = reward IS NULL (crash / never-verified; excluded).
--   success_rate     = n_success / NULLIF(n_scored, 0) -> NULL on zero scored.
--   Wilson 95% bounds are over n_scored; NULL when n_scored = 0.
--
-- Run (from repo root, read-only; in-memory DB, no pandas needed):
--   uv run --with duckdb python -c "import duckdb; cur=duckdb.connect().execute(open('research/analysis/harbor-native-track/queries/01_trial_stats_per_job.sql').read()); print([d[0] for d in cur.description]); [print(r) for r in cur.fetchall()]"
--
-- Corpus note: canary jobs hold 3 trials each; funcdag-codex-canary holds 5
-- trial-level result.json files (3 under trials*/ subdirs + 2 top-level easy).
-- New run dirs appear over time; extend the GLOB list below to re-scope.

WITH trial_results AS (
    SELECT
        regexp_extract(filename, 'runs/([^/]+)/', 1) AS job,
        trial_name,
        verifier_result.rewards.reward AS reward,
        CAST(exception_info.exception_type AS VARCHAR) AS exception_type
    FROM read_json(
        ['runs/canary-*/**/result.json', 'runs/funcdag-codex-canary/**/result.json'],
        union_by_name = true,
        filename = true
    )
    WHERE trial_name IS NOT NULL  -- trial-level only; drops job aggregates + verifier/artifact payloads
),
per_job AS (
    SELECT
        job,
        count(*) AS n_trials_present,
        count(*) FILTER (WHERE reward IS NOT NULL) AS n_scored,
        count(*) FILTER (WHERE reward IS NULL) AS n_unscored,
        count(*) FILTER (WHERE reward = 1.0) AS n_success,
        count(*) FILTER (WHERE reward = 0.0) AS n_failed,
        count(*) FILTER (WHERE exception_type IS NOT NULL) AS n_with_exception,
        -- Success rate over SCORED trials only; NULL (not 0) when nothing scored.
        count(*) FILTER (WHERE reward = 1.0)::DOUBLE / NULLIF(count(*) FILTER (WHERE reward IS NOT NULL), 0) AS success_rate_scored
    FROM trial_results
    GROUP BY job
)
SELECT
    job,
    n_trials_present,
    n_scored,
    n_unscored,
    n_success,
    n_failed,
    n_with_exception,
    success_rate_scored,
    -- Wilson 95% interval over the scored denominator (z = 1.96); NULL on zero scored.
    CASE WHEN n_scored = 0 THEN NULL ELSE
        ((success_rate_scored + 1.96 * 1.96 / (2 * n_scored))
            - 1.96 * sqrt(success_rate_scored * (1 - success_rate_scored) / n_scored
                + 1.96 * 1.96 / (4 * n_scored * n_scored)))
        / (1 + 1.96 * 1.96 / n_scored)
    END AS wilson95_low_scored,
    CASE WHEN n_scored = 0 THEN NULL ELSE
        ((success_rate_scored + 1.96 * 1.96 / (2 * n_scored))
            + 1.96 * sqrt(success_rate_scored * (1 - success_rate_scored) / n_scored
                + 1.96 * 1.96 / (4 * n_scored * n_scored)))
        / (1 + 1.96 * 1.96 / n_scored)
    END AS wilson95_high_scored
FROM per_job
ORDER BY job;
