-- 03: Recorded agent-command test-path signals (HAR-131).
-- Canonical definition lives once in sql/trace_queries.sql
-- (v_trace_test_path_access). This file is the runnable saved-query handle.
-- Only noncopied, recorded agent-command text contributes test-path signals;
-- reconstructed proposals and stdout mentions are not recorded access proof.
-- Denominators cover all trials via the native (job_id, trial_id) pair.
SELECT * FROM v_trace_test_path_access;
