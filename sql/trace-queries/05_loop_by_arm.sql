-- 05: Loop kind distribution by arm (HAR-131).
-- Canonical definition lives once in sql/trace_queries.sql
-- (v_trace_loop_by_arm). This file is the runnable saved-query handle.
-- NULL loop_kind means no decision prediction (unknown), never 'none'.
SELECT * FROM v_trace_loop_by_arm;
