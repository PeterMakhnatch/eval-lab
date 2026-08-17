-- Verdict aggregation views and fallbacks (§2.1, §2.2).
--
-- Exposes:
--   v_verdicts_history: full append-only verdict history, oldest first
--   v_current_verdicts: current (latest by timestamp) verdict per discovery
--
-- Run from repository root in clean DuckDB session:
--   duckdb -c ".read sql/verdicts.sql" -c "SELECT * FROM v_current_verdicts"

-- Schema fallback for verdicts table when not pre-registered in memory
CREATE TABLE IF NOT EXISTS verdicts_schema_fallback (
    discovery_id VARCHAR,
    status VARCHAR,
    "by" VARCHAR,
    "at" VARCHAR,
    note VARCHAR
);

CREATE TABLE IF NOT EXISTS verdicts AS SELECT * FROM verdicts_schema_fallback;

-- 1. v_verdicts_history: full history per discovery, oldest first
CREATE OR REPLACE VIEW v_verdicts_history AS
SELECT
    discovery_id,
    status,
    "by",
    "at",
    note
FROM verdicts
ORDER BY discovery_id, "at" ASC;

-- 2. v_current_verdicts: latest verdict per discovery by timestamp
CREATE OR REPLACE VIEW v_current_verdicts AS
WITH ranked AS (
    SELECT
        discovery_id,
        status,
        "by",
        "at",
        note,
        row_number() OVER (
            PARTITION BY discovery_id
            ORDER BY "at" DESC
        ) AS ranking
    FROM verdicts
)
SELECT
    discovery_id,
    status,
    "by",
    "at",
    note
FROM ranked
WHERE ranking = 1
ORDER BY "at" DESC, discovery_id;
