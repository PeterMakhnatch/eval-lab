-- 04: Post-edit tokens spent by stop reason (HAR-131).
-- Canonical definition lives once in sql/trace_queries.sql
-- (v_trace_post_edit_tokens). This file is the runnable saved-query handle.
-- Shares use complete ordinal-matched native prompt_series populations,
-- independently per direction. Proxy totals are never a share denominator.
SELECT * FROM v_trace_post_edit_tokens;
