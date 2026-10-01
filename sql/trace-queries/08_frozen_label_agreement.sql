-- 08: Frozen label agreement and coverage (HAR-131).
-- Canonical definition lives once in sql/trace_queries.sql
-- (v_trace_frozen_label_agreement). This file is the runnable saved-query handle.
-- Agreement needs an agreed valid loop_kind from >= 2 distinct raters in one
-- unambiguous har119 cohort. HAR-128 SFT-pass labels are coverage only.
SELECT * FROM v_trace_frozen_label_agreement;
