-- 06: Upstream fetch signals and counts exclusions (HAR-131).
-- Canonical definition lives once in sql/trace_queries.sql
-- (v_trace_fetch_exclusion_audit). This file is the runnable saved-query handle.
-- Fetch signals use typed producer JSON paths/array entries, not fragments
-- or incidental mentions. Only exact counts reasons identify exclusions.
SELECT * FROM v_trace_fetch_exclusion_audit;
