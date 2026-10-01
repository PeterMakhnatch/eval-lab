-- 02: First edit vs pass (HAR-131).
-- Canonical definition lives once in sql/trace_queries.sql
-- (v_trace_first_edit_vs_pass). This file is the runnable saved-query handle.
-- Only an available, native-pair Parquet measurement distinguishes no signal
-- from unknown. The edit detector does not prove persistence or usefulness.
SELECT * FROM v_trace_first_edit_vs_pass;
