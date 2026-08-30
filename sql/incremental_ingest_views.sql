-- Incremental Ingest and Promoted ATIF Reconciliation Views.
--
-- Exposes:
--   v_incremental_ingest_reconciliation: per-bundle reconciliation across digest index, lineage, and ATIF
--   v_incremental_ingest_efficiency: skip rates, changed bundle ratios, and work saved
--   v_incremental_ingest_security: security audit of R2 omissions, symlink targets, and redaction rules
--   v_incremental_ingest_summary: headline run-level efficiency and completeness
--
-- Run in clean DuckDB via fallback schema tables:
--   duckdb -c ".read sql/incremental_ingest_views.sql" -c "SELECT * FROM v_incremental_ingest_summary LIMIT 5"

-- Fallback schema tables (DuckDB in-memory session; Postgres uses real tables)
CREATE TABLE IF NOT EXISTS promoted_bundles_index (
    bundle_name TEXT PRIMARY KEY,
    bundle_digest TEXT,
    source_job_result_sha256 TEXT
);

CREATE TABLE IF NOT EXISTS incremental_ingest_perf (
    run_id TEXT PRIMARY KEY,
    scanned_bundles BIGINT,
    changed_bundles BIGINT,
    skipped_bundles BIGINT,
    rejected_bundles BIGINT,
    failed_bundles BIGINT,
    promoted_files_scanned BIGINT,
    promoted_files_ingested BIGINT,
    promoted_files_skipped BIGINT,
    content_digest TEXT
);

CREATE TABLE IF NOT EXISTS promotion_lineage (
    bundle_name TEXT,
    source_path TEXT,
    promoted_path TEXT,
    action TEXT,
    rule TEXT,
    source_bytes BIGINT,
    source_sha256 TEXT,
    promoted_bytes BIGINT,
    promoted_sha256 TEXT
);

CREATE TABLE IF NOT EXISTS promotion_omissions (
    bundle_name TEXT,
    source_path TEXT,
    rule TEXT,
    entry_type TEXT,
    link_target TEXT,
    source_bytes BIGINT,
    source_sha256 TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_name TEXT,
    trial_count BIGINT
);

CREATE TABLE IF NOT EXISTS trajectories (
    job_id TEXT,
    trial_id TEXT,
    document_id TEXT PRIMARY KEY,
    validation_status TEXT,
    step_count BIGINT,
    llm_call_count BIGINT
);

-- 1. v_incremental_ingest_reconciliation: pre-aggregated join across index, lineage, omissions, and projections
CREATE OR REPLACE VIEW v_incremental_ingest_reconciliation AS
WITH agg_lineage AS (
    SELECT
        bundle_name,
        count(DISTINCT source_path) AS total_source_files,
        sum(CASE WHEN action = 'verbatim' THEN 1 ELSE 0 END) AS verbatim_files,
        sum(CASE WHEN action = 'redacted' THEN 1 ELSE 0 END) AS redacted_files
    FROM promotion_lineage
    GROUP BY bundle_name
),
agg_omissions AS (
    SELECT
        bundle_name,
        count(DISTINCT source_path) AS omitted_files
    FROM promotion_omissions
    GROUP BY bundle_name
),
agg_trajectories AS (
    SELECT
        job_id,
        count(DISTINCT document_id) AS projected_trajectories
    FROM trajectories
    GROUP BY job_id
)
SELECT
    idx.bundle_name,
    idx.bundle_digest,
    j.job_id,
    j.trial_count,
    coalesce(l.total_source_files, 0) AS total_source_files,
    coalesce(l.verbatim_files, 0) AS verbatim_files,
    coalesce(l.redacted_files, 0) AS redacted_files,
    coalesce(o.omitted_files, 0) AS omitted_files,
    coalesce(t.projected_trajectories, 0) AS projected_trajectories,
    CASE
        WHEN j.job_id IS NOT NULL AND coalesce(t.projected_trajectories, 0) > 0 THEN 'fully_projected'
        WHEN j.job_id IS NOT NULL THEN 'partial_projection'
        ELSE 'unprojected'
    END AS projection_status
FROM promoted_bundles_index idx
LEFT JOIN jobs j ON j.job_name = idx.bundle_name OR j.job_id = idx.bundle_name
LEFT JOIN agg_trajectories t ON t.job_id = j.job_id OR t.job_id = idx.bundle_name
LEFT JOIN agg_lineage l ON l.bundle_name = idx.bundle_name
LEFT JOIN agg_omissions o ON o.bundle_name = idx.bundle_name;

-- 2. v_incremental_ingest_efficiency: skip rates and work saved
CREATE OR REPLACE VIEW v_incremental_ingest_efficiency AS
SELECT
    run_id,
    scanned_bundles,
    changed_bundles,
    skipped_bundles,
    rejected_bundles,
    failed_bundles,
    promoted_files_scanned,
    promoted_files_ingested,
    promoted_files_skipped,
    CASE
        WHEN scanned_bundles > 0 THEN round(100.0 * skipped_bundles / scanned_bundles, 1)
        ELSE 0.0
    END AS bundle_skip_rate_pct,
    CASE
        WHEN promoted_files_scanned > 0 THEN round(100.0 * promoted_files_skipped / promoted_files_scanned, 1)
        ELSE 0.0
    END AS file_skip_rate_pct,
    content_digest
FROM incremental_ingest_perf;

-- 3. v_incremental_ingest_security: R2 omission integrity and symlink isolation audit
CREATE OR REPLACE VIEW v_incremental_ingest_security AS
SELECT
    bundle_name,
    count(*) AS total_omissions,
    sum(CASE WHEN entry_type = 'symlink' THEN 1 ELSE 0 END) AS symlink_omissions,
    sum(CASE WHEN entry_type = 'file' THEN 1 ELSE 0 END) AS file_omissions,
    sum(CASE WHEN rule = 'R2' THEN 1 ELSE 0 END) AS r2_rule_omissions,
    sum(source_bytes) AS omitted_bytes_total
FROM promotion_omissions
GROUP BY bundle_name;

-- 4. v_incremental_ingest_summary: headline run metrics
CREATE OR REPLACE VIEW v_incremental_ingest_summary AS
SELECT
    count(DISTINCT bundle_name) AS total_indexed_bundles,
    sum(total_source_files) AS total_manifest_files,
    sum(omitted_files) AS total_omitted_files,
    sum(projected_trajectories) AS total_projected_trajectories,
    sum(CASE WHEN projection_status = 'fully_projected' THEN 1 ELSE 0 END) AS fully_projected_bundles
FROM v_incremental_ingest_reconciliation;
