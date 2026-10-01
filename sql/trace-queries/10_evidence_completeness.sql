-- 10: Cross-cohort evidence completeness and gap audit (HAR-131).
-- Canonical definition lives once in sql/trace_queries.sql
-- (v_trace_evidence_completeness). This file is the runnable saved-query handle.
-- Unknown stays explicit and is never folded into a passing rate.
SELECT * FROM v_trace_evidence_completeness;
