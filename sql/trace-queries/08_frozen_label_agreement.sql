-- 08: Frozen label agreement and coverage (HAR-131).
-- Canonical definition lives once in sql/trace_queries.sql
-- (v_trace_frozen_label_agreement). This file is the runnable saved-query handle.
-- Agreement is partitioned per native trial and admitted cohort (har119,
-- har128-har116, har128-g2-a1, har128-g2-r2), requiring >= 2 distinct raters
-- in that cohort to agree. Rows are non-additive per-cohort study results:
-- cross-cohort disagreements are reported separately, never pooled into
-- cross-cohort votes. HAR-109 hand and HAR-128 SFT-pass labels are coverage
-- only and excluded from loop voting.
SELECT * FROM v_trace_frozen_label_agreement;
