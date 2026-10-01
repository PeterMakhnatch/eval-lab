-- 07: Parse rejection evidence and error shapes (HAR-131).
-- Canonical definition lives once in sql/trace_queries.sql
-- (v_trace_parse_rejection_evidence). This file is the runnable saved-query handle.
-- Numeric shape counts, acceptance counts/provenance and recorded agreement
-- are distinct. Recorded-false intersection is unknown from marginal counts.
SELECT * FROM v_trace_parse_rejection_evidence;
