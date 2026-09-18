-- 03_failure_cohorts.sql :: v1.0.0 (2026-09-08) :: DuckDB dialect (>= 1.0)
--
-- Purpose: failure cohorts over the traj_features store by exception class and
-- termination-filter columns, with the capability denominator computed
-- MECHANICALLY: reward-0-with-crash rows (exception_class IS NOT NULL) are
-- excluded from capability rates by predicate, not by analyst judgment.
-- Cohorts:
--   capability_failure = primary_reward = 0 AND exception_class IS NULL
--                        (agent ran clean and the task check failed).
--   crash_with_reward  = exception_class IS NOT NULL (regardless of reward;
--                        includes reward-0-with-crash AND reward-NULL rows).
--   unscored_crash     = primary_reward IS NULL (never verified; never in any
--                        capability denominator).
--   success            = primary_reward = 1 AND exception_class IS NULL.
--
-- STORE READ RULE (trial-statistics-diff.md finding; naive sum(cost_usd) over
-- the store overcounts +87% on the 27-trial corpus): filter status='featured'
-- AND dedupe to one row per (job_name, trial_name). Duplicate featured rows
-- come from reprojection history (same trial, several source_sha256) and from
-- stale checkout paths in source_path. The EXACT rule matches source_sha256
-- against live file bytes, which pure SQL cannot hash; the dedup CTE below
-- therefore keeps one deterministic row per trial (ORDER BY source_sha256,
-- source_path) and exposes n_featured_rows / n_distinct_sha so residual
-- ambiguity is visible, never silently averaged. For byte-exact work, hash
-- the live file (sha256sum runs/<job>/<trial>/agent/trajectory.json) and keep
-- the matching row; see trial-statistics-diff.md section "DuckDB store vs
-- same bytes".
--
-- Termination-filter columns (all from the store; NULL-tolerant grouping):
--   exception_class, max_exit_code_cascade_screening, unrecovered_at_terminal,
--   is_expected_negative. Capability rate denominator =
--   scored AND exception_class IS NULL; NULLIF guards zero denominators.
--
-- Scope (corpus parity with 01/02/04): job_name LIKE 'canary-%' OR
-- job_name = 'funcdag-codex-canary'. The 'canary-' PREFIX matters: LIKE
-- '%canary%' would also match tau_canary* / zai-wave2-* store rows whose runs
-- are outside this pack's runs globs (scope orphans, not real gaps).
--
-- Run (from repo root, read-only; in-memory DB, no pandas needed):
--   uv run --with duckdb python -c "import duckdb; cur=duckdb.connect().execute(open('research/analysis/harbor-native-track/queries/03_failure_cohorts.sql').read()); print([d[0] for d in cur.description]); [print(r) for r in cur.fetchall()]"

-- Cohort counts by exception class + termination-filter columns (single statement).
WITH featured AS (
    SELECT *
    FROM read_parquet('derived/parquet/traj_features', union_by_name = true)
    WHERE status = 'featured'  -- READ RULE part 1: drop accounted_unavailable (correctly zeroed, no trajectory)
),
deduped AS (
    -- READ RULE part 2 (mechanical approximation): one row per trial.
    -- Exact byte-match needs live sha256; ambiguity is exposed, not hidden.
    SELECT
        *,
        count(*) OVER (PARTITION BY job_name, trial_name) AS n_featured_rows,
        count(DISTINCT source_sha256) OVER (PARTITION BY job_name, trial_name) AS n_distinct_sha,
        row_number() OVER (
            PARTITION BY job_name, trial_name
            ORDER BY source_sha256, source_path
        ) AS rn
    FROM featured
    WHERE job_name LIKE 'canary-%' OR job_name = 'funcdag-codex-canary'
)
SELECT
    CASE
        WHEN primary_reward = 1.0 AND exception_class IS NULL THEN 'success'
        WHEN primary_reward = 0.0 AND exception_class IS NULL THEN 'capability_failure'
        WHEN primary_reward IS NULL THEN 'unscored_crash'
        ELSE 'crash_with_reward'
    END AS cohort,
    exception_class,
    max_exit_code_cascade_screening,
    unrecovered_at_terminal,
    is_expected_negative,
    count(*) AS n_trials,
    sum(n_featured_rows - 1) AS n_hidden_duplicate_rows,
    sum(cost_usd) AS cost_usd_sum_deduped
FROM deduped
WHERE rn = 1
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1, 2 NULLS FIRST;
