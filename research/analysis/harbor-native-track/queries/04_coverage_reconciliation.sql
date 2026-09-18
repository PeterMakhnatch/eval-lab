-- 04_coverage_reconciliation.sql :: v1.0.0 (2026-09-08) :: DuckDB dialect (>= 1.0)
--
-- Purpose: reconcile four pipeline stages per (job, trial) and name the EXACT
-- mechanical gap reason for every off-diagonal row. Stages:
--   runs_present  = trial-level result.json on disk (trial_name IS NOT NULL;
--                   drops job aggregates + verifier/artifact payloads).
--   traj_present  = agent/trajectory.json file on disk (glob presence only;
--                   file contents are never parsed here, so this stays cheap).
--   ingested      = path listed in derived/harbor-traces/atif-coverage.json
--                   trajectories[] (the ATIF coverage export).
--   projected     = row present in derived/parquet/traj_features store
--                   (any status; status shown so accounted_unavailable reads
--                   as correctly-zeroed, not missing).
--
-- Gap reasons (first match wins, top to bottom):
--   complete                                     all four stages present.
--   accounted_unavailable_no_trajectory          in store as
--     unavailable_reason='missing_trajectory_file', no trajectory on disk:
--     correctly zeroed crash row, not a gap.
--   missing_trajectory_file                      runs_present but no
--     trajectory.json (harness crash before agent output).
--   not_in_atif_coverage                         trajectory on disk but absent
--     from atif-coverage.json (unsurveyed export window; or oracle agent,
--     which has no ATIF format -- see export-summary.json skipped entries).
--   not_projected_to_store                       ingested but no store row
--     (stale projection; e.g. trials*/ subdir shape was missed before).
--   store_only_featured_orphan                   featured store row with no
--     disk trial (job-dir junk name like 'trials'/'trials-hard', or a trial
--     from a stale checkout path in source_path).
--   store_only_accounted_unavailable             accounted_unavailable row with
--     no disk trial (same junk family, already zeroed).
--
-- Denominators: each stage's n is over the union of (job, trial) keys; the
-- second statement reports stage totals. The store side applies the read rule
-- (status='featured' + one row per trial) only for the duplicate diagnostic;
-- stage presence uses any-status DISTINCT keys.
--
-- Run (from repo root, read-only; in-memory DB, no pandas needed):
--   uv run --with duckdb python -c "import duckdb; cur=duckdb.connect().execute(open('research/analysis/harbor-native-track/queries/04_coverage_reconciliation.sql').read()); print([d[0] for d in cur.description]); [print(r) for r in cur.fetchall()]"

WITH runs_present AS (
    SELECT DISTINCT
        regexp_extract(filename, 'runs/([^/]+)/', 1) AS job,
        trial_name AS trial
    FROM read_json(
        ['runs/canary-*/**/result.json', 'runs/funcdag-codex-canary/**/result.json'],
        union_by_name = true,
        filename = true
    )
    WHERE trial_name IS NOT NULL
),
traj_present AS (
    SELECT DISTINCT
        regexp_extract(file, '^runs/([^/]+)/', 1) AS job,
        regexp_extract(file, '/([^/]+)/agent/trajectory\.json$', 1) AS trial
    FROM glob('runs/canary-*/**/agent/trajectory.json')
    UNION
    SELECT DISTINCT
        regexp_extract(file, '^runs/([^/]+)/', 1) AS job,
        regexp_extract(file, '/([^/]+)/agent/trajectory\.json$', 1) AS trial
    FROM glob('runs/funcdag-codex-canary/**/agent/trajectory.json')
),
ingested AS (
    SELECT DISTINCT job, trial FROM (
        SELECT
            regexp_extract(t.path, '^runs/([^/]+)/', 1) AS job,
            regexp_extract(t.path, '/([^/]+)/agent/trajectory\.json$', 1) AS trial
        FROM (
            SELECT UNNEST(trajectories) AS t
            FROM read_json('derived/harbor-traces/atif-coverage.json')
        )
    )
    -- Corpus parity: atif-coverage.json also lists jobs outside this pack's
    -- runs globs (minimal-luna-*, session-window-debug); those are out of
    -- scope here, not gaps in this corpus.
    WHERE job LIKE 'canary-%' OR job = 'funcdag-codex-canary'
),
projected AS (
    SELECT DISTINCT job_name AS job, trial_name AS trial, status
    FROM read_parquet('derived/parquet/traj_features', union_by_name = true)
    -- Corpus parity: the 'canary-' PREFIX matters; LIKE '%canary%' would pull
    -- in tau_canary* / zai-wave2-* rows whose runs are outside the globs above.
    WHERE job_name LIKE 'canary-%' OR job_name = 'funcdag-codex-canary'
),
keys AS (
    SELECT job, trial FROM runs_present
    UNION SELECT job, trial FROM traj_present
    UNION SELECT job, trial FROM ingested
    UNION SELECT job, trial FROM projected
)
SELECT
    k.job,
    k.trial,
    (r.trial IS NOT NULL) AS runs_present,
    (t.trial IS NOT NULL) AS traj_present,
    (i.trial IS NOT NULL) AS ingested,
    (p.trial IS NOT NULL) AS projected,
    p.status AS store_status,
    CASE
        WHEN r.trial IS NOT NULL AND t.trial IS NOT NULL
             AND i.trial IS NOT NULL AND p.trial IS NOT NULL THEN 'complete'
        WHEN p.trial IS NOT NULL AND p.status = 'accounted_unavailable'
             AND t.trial IS NULL THEN 'accounted_unavailable_no_trajectory'
        WHEN r.trial IS NOT NULL AND t.trial IS NULL THEN 'missing_trajectory_file'
        WHEN t.trial IS NOT NULL AND i.trial IS NULL THEN 'not_in_atif_coverage'
        WHEN i.trial IS NOT NULL AND p.trial IS NULL THEN 'not_projected_to_store'
        WHEN r.trial IS NULL AND p.trial IS NOT NULL
             AND p.status = 'featured' THEN 'store_only_featured_orphan'
        WHEN r.trial IS NULL AND p.trial IS NOT NULL THEN 'store_only_accounted_unavailable'
        ELSE 'other_off_diagonal'
    END AS gap_reason
FROM keys k
LEFT JOIN runs_present r USING (job, trial)
LEFT JOIN traj_present t USING (job, trial)
LEFT JOIN ingested i USING (job, trial)
LEFT JOIN projected p USING (job, trial)
ORDER BY gap_reason, job, trial;
