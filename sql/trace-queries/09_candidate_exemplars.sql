-- 09: Deterministic candidate exemplars (top 2 per category) (HAR-131).
-- Canonical definition lives once in sql/trace_queries.sql
-- (v_trace_candidate_exemplars). This file is the runnable saved-query handle.
-- first_failure is an opinion anchor, not a recorded cause. step_ref/path/hash
-- are supplied only for a unique exact match to a real native-pair ATIF step.
SELECT * FROM v_trace_candidate_exemplars;
