-- Canonical trace queries over v_trace_trials and v_trace_steps (HAR-131).
--
-- Reusable DuckDB analytical views over the trace query interface:
--   1. v_trace_cohort_raw: Cohort raw pass rates and counts countability
--   2. v_trace_first_edit_vs_pass: First edit step distribution vs outcome
--   3. v_trace_test_path_access: Test-path access signals in agent commands
--   4. v_trace_post_edit_tokens: Tokens spent after last edit by stop reason
--   5. v_trace_loop_by_arm: Loop kind distribution (claim, repetition, none) by arm
--   6. v_trace_fetch_exclusion_audit: Upstream fetch signals and counts exclusions
--   7. v_trace_parse_rejection_evidence: Parse error shapes and failure modes
--   8. v_trace_frozen_label_agreement: Frozen rater label coverage and loop agreement
--   9. v_trace_candidate_exemplars: Deterministic 2-exemplar extraction per failure pattern
--  10. v_trace_evidence_completeness: Cross-cohort evidence availability and gaps
--
-- Invariants:
--   - Rates always carry N (denominator).
--   - Scored budget stops are never converted to infra (counts is authority).
--   - Trial aggregations MUST use v_trace_trials, not multiply by joined steps.

-- --------------------------------------------------------------------------- --
-- 1. Cohort raw / countability
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_trace_cohort_raw AS
SELECT
    COALESCE(card, 'unknown') AS card,
    COALESCE(arm, 'unknown') AS arm,
    COUNT(*) AS n_total,
    SUM(CASE WHEN scored = true THEN 1 ELSE 0 END) AS n_scored,
    SUM(CASE WHEN raw_reward >= 1.0 THEN 1 ELSE 0 END) AS n_raw_pass,
    ROUND(SUM(CASE WHEN raw_reward >= 1.0 THEN 1.0 ELSE 0.0 END) / COUNT(*), 4) AS raw_pass_rate,
    SUM(CASE WHEN counts_verdict = 'counted_pass' THEN 1 ELSE 0 END) AS n_counted_pass,
    SUM(CASE WHEN counts_verdict = 'counted_fail' THEN 1 ELSE 0 END) AS n_counted_fail,
    SUM(CASE WHEN counts_verdict = 'excluded' THEN 1 ELSE 0 END) AS n_excluded,
    SUM(CASE WHEN counts_verdict = 'excluded' AND counts_reasons_json LIKE '%infra%' THEN 1 ELSE 0 END) AS n_infra_excluded,
    SUM(CASE WHEN stop_reason LIKE '%ceiling%' OR stop_reason LIKE '%budget%' THEN 1 ELSE 0 END) AS n_budget_stops
FROM v_trace_trials
GROUP BY COALESCE(card, 'unknown'), COALESCE(arm, 'unknown')
ORDER BY card, arm;

-- --------------------------------------------------------------------------- --
-- 2. First edit vs pass (explicit step measure)
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_trace_first_edit_vs_pass AS
SELECT
    COALESCE(card, 'unknown') AS card,
    CASE WHEN raw_reward IS NULL THEN 'unscored' WHEN raw_reward >= 1.0 THEN 'pass' ELSE 'fail' END AS outcome,
    COUNT(*) AS n_total,
    SUM(CASE WHEN first_edit_step IS NOT NULL THEN 1 ELSE 0 END) AS n_with_edit,
    SUM(CASE WHEN first_edit_step IS NULL THEN 1 ELSE 0 END) AS n_no_edit,
    ROUND(AVG(first_edit_step), 2) AS avg_first_edit_step,
    MIN(first_edit_step) AS min_first_edit_step,
    MAX(first_edit_step) AS max_first_edit_step,
    ROUND(SUM(CASE WHEN first_edit_step IS NOT NULL THEN 1.0 ELSE 0.0 END) / COUNT(*), 4) AS edit_rate
FROM v_trace_trials
GROUP BY COALESCE(card, 'unknown'), CASE WHEN raw_reward IS NULL THEN 'unscored' WHEN raw_reward >= 1.0 THEN 'pass' ELSE 'fail' END
ORDER BY card, outcome;

-- --------------------------------------------------------------------------- --
-- 3. Test-path access signals
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_trace_test_path_access AS
SELECT
    COALESCE(card, 'unknown') AS card,
    COALESCE(arm, 'unknown') AS arm,
    COUNT(DISTINCT trial_id) AS n_total_trials,
    COUNT(DISTINCT CASE
        WHEN command_provenance = 'recorded'
         AND (command_text LIKE '%testbed/tests/%'
           OR command_text LIKE '%/tests/%'
           OR command_text LIKE '%pytest%'
           OR command_text LIKE '%test_%.py%'
           OR command_text LIKE '%/test_%')
        THEN trial_id
    END) AS n_trials_with_test_access,
    COUNT(CASE
        WHEN command_provenance = 'recorded'
         AND (command_text LIKE '%testbed/tests/%'
           OR command_text LIKE '%/tests/%'
           OR command_text LIKE '%pytest%'
           OR command_text LIKE '%test_%.py%'
           OR command_text LIKE '%/test_%')
        THEN 1
    END) AS n_steps_with_test_access,
    COUNT(DISTINCT CASE
        WHEN raw_reward >= 1.0
         AND command_provenance = 'recorded'
         AND (command_text LIKE '%testbed/tests/%'
           OR command_text LIKE '%/tests/%'
           OR command_text LIKE '%pytest%'
           OR command_text LIKE '%test_%.py%'
           OR command_text LIKE '%/test_%')
        THEN trial_id
    END) AS n_passes_with_test_access,
    COUNT(DISTINCT CASE
        WHEN raw_reward >= 1.0
         AND counts_verdict = 'excluded'
         AND command_provenance = 'recorded'
         AND (command_text LIKE '%testbed/tests/%'
           OR command_text LIKE '%/tests/%'
           OR command_text LIKE '%pytest%'
           OR command_text LIKE '%test_%.py%'
           OR command_text LIKE '%/test_%')
        THEN trial_id
    END) AS n_excluded_passes_with_test_access
FROM v_trace_steps
GROUP BY COALESCE(card, 'unknown'), COALESCE(arm, 'unknown')
ORDER BY card, arm;

-- --------------------------------------------------------------------------- --
-- 4. Post-edit tokens
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_trace_post_edit_tokens AS
SELECT
    COALESCE(card, 'unknown') AS card,
    COALESCE(stop_reason, 'unknown') AS stop_reason,
    COUNT(*) AS n_total,
    SUM(CASE WHEN tokens_after_last_edit_input IS NOT NULL THEN 1 ELSE 0 END) AS n_with_post_edit,
    SUM(input_tokens) AS total_input_tokens,
    SUM(tokens_after_last_edit_input) AS total_post_edit_input_tokens,
    ROUND(SUM(tokens_after_last_edit_input)::DOUBLE / NULLIF(SUM(input_tokens), 0), 4) AS post_edit_input_share,
    ROUND(AVG(tokens_after_last_edit_input), 0) AS avg_post_edit_input,
    ROUND(AVG(tokens_after_last_edit_output), 0) AS avg_post_edit_output
FROM v_trace_trials
GROUP BY COALESCE(card, 'unknown'), COALESCE(stop_reason, 'unknown')
ORDER BY card, n_total DESC;

-- --------------------------------------------------------------------------- --
-- 5. Loop kind by arm
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_trace_loop_by_arm AS
SELECT
    COALESCE(card, 'unknown') AS card,
    COALESCE(arm, 'unknown') AS arm,
    COUNT(*) AS n_total,
    SUM(CASE WHEN loop_kind = 'completion-claim' THEN 1 ELSE 0 END) AS n_completion_claim,
    SUM(CASE WHEN loop_kind = 'repetition' THEN 1 ELSE 0 END) AS n_repetition,
    SUM(CASE WHEN loop_kind = 'none' THEN 1 ELSE 0 END) AS n_no_loop,
    SUM(CASE WHEN loop_kind IS NULL THEN 1 ELSE 0 END) AS n_unclassified,
    ROUND(SUM(CASE WHEN loop_kind = 'completion-claim' THEN 1.0 ELSE 0.0 END) / COUNT(*), 4) AS claim_rate,
    ROUND(SUM(CASE WHEN loop_kind = 'repetition' THEN 1.0 ELSE 0.0 END) / COUNT(*), 4) AS repetition_rate
FROM v_trace_trials
GROUP BY COALESCE(card, 'unknown'), COALESCE(arm, 'unknown')
ORDER BY card, arm;

-- --------------------------------------------------------------------------- --
-- 6. Fetch / exclusion audit
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_trace_fetch_exclusion_audit AS
SELECT
    COALESCE(card, 'unknown') AS card,
    COALESCE(arm, 'unknown') AS arm,
    COUNT(*) AS n_total,
    SUM(CASE WHEN counts_evidence_json LIKE '%upstream_fetch%' OR taint_json LIKE '%upstream_fetch%' THEN 1 ELSE 0 END) AS n_fetched,
    SUM(CASE WHEN (counts_evidence_json LIKE '%upstream_fetch%' OR taint_json LIKE '%upstream_fetch%') AND raw_reward >= 1.0 THEN 1 ELSE 0 END) AS n_fetched_passed,
    SUM(CASE WHEN counts_reasons_json LIKE '%copied_fix%' THEN 1 ELSE 0 END) AS n_copied_fix_excluded,
    SUM(CASE WHEN counts_reasons_json LIKE '%pass_tainted%' THEN 1 ELSE 0 END) AS n_pass_tainted_excluded,
    SUM(CASE WHEN counts_verdict = 'counted_fail' AND (counts_evidence_json LIKE '%upstream_fetch%' OR taint_json LIKE '%upstream_fetch%') THEN 1 ELSE 0 END) AS n_counted_fail_with_fetch
FROM v_trace_trials
GROUP BY COALESCE(card, 'unknown'), COALESCE(arm, 'unknown')
ORDER BY card, arm;

-- --------------------------------------------------------------------------- --
-- 7. Parse rejection evidence
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_trace_parse_rejection_evidence AS
SELECT
    COALESCE(model_name, 'unknown') AS model_name,
    COALESCE(card, 'unknown') AS card,
    COUNT(*) AS n_total,
    SUM(CASE WHEN taint_json LIKE '%parse_error%' OR decision_facts_json LIKE '%unparseable%' OR diagnosis_json LIKE '%unparseable%' THEN 1 ELSE 0 END) AS n_parse_error_runs,
    SUM(CASE WHEN stop_reason LIKE '%ceiling%' OR stop_reason LIKE '%budget%' THEN 1 ELSE 0 END) AS n_budget_stops,
    SUM(CASE WHEN stop_reason = 'agent_timeout' THEN 1 ELSE 0 END) AS n_timeout_stops,
    ROUND(SUM(CASE WHEN taint_json LIKE '%parse_error%' OR decision_facts_json LIKE '%unparseable%' OR diagnosis_json LIKE '%unparseable%' THEN 1.0 ELSE 0.0 END) / COUNT(*), 4) AS parse_error_rate
FROM v_trace_trials
GROUP BY COALESCE(model_name, 'unknown'), COALESCE(card, 'unknown')
ORDER BY card, model_name;

-- --------------------------------------------------------------------------- --
-- 8. Frozen label agreement / coverage
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_trace_frozen_label_agreement AS
SELECT
    COALESCE(card, 'unknown') AS card,
    COUNT(*) AS n_total,
    SUM(CASE WHEN labels_json != '[]' AND labels_json IS NOT NULL THEN 1 ELSE 0 END) AS n_with_frozen_labels,
    SUM(CASE WHEN labels_json LIKE '%completion-claim%' THEN 1 ELSE 0 END) AS n_labeled_loop_claim,
    SUM(CASE WHEN labels_json LIKE '%"repetition"%' THEN 1 ELSE 0 END) AS n_labeled_loop_repetition,
    SUM(CASE WHEN labels_json LIKE '%"none"%' THEN 1 ELSE 0 END) AS n_labeled_loop_none,
    SUM(CASE WHEN loop_kind IS NOT NULL AND labels_json LIKE '%' || loop_kind || '%' THEN 1 ELSE 0 END) AS n_decision_matches_label,
    ROUND(SUM(CASE WHEN labels_json != '[]' AND labels_json IS NOT NULL THEN 1.0 ELSE 0.0 END) / COUNT(*), 4) AS label_coverage_rate
FROM v_trace_trials
GROUP BY COALESCE(card, 'unknown')
ORDER BY card;

-- --------------------------------------------------------------------------- --
-- 9. Candidate exemplars (deterministic top-2 per category)
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_trace_candidate_exemplars AS
WITH categorized AS (
    SELECT
        trial_id,
        trial_name,
        card,
        arm,
        first_failure_ref AS step_ref,
        report_path,
        CASE
            WHEN raw_reward >= 1.0 AND counts_reasons_json LIKE '%copied_fix%' THEN 'copied_pass'
            WHEN raw_reward >= 1.0 AND (counts_reasons_json LIKE '%pass_tainted%' OR counts_reasons_json LIKE '%task_not_usable%') THEN 'tainted_pass'
            WHEN loop_kind = 'completion-claim' THEN 'completion_claim_loop'
            WHEN loop_kind = 'repetition' THEN 'repetition_loop'
            WHEN (stop_reason LIKE '%ceiling%' OR stop_reason LIKE '%budget%') AND first_edit_step IS NULL THEN 'budget_stop_no_edit'
            WHEN (stop_reason LIKE '%ceiling%' OR stop_reason LIKE '%budget%') AND first_edit_step IS NOT NULL THEN 'budget_stop_with_edit'
            WHEN counts_verdict = 'excluded' AND counts_reasons_json LIKE '%infra%' THEN 'infra_excluded'
            WHEN raw_reward IS NULL THEN 'unscored_unknown'
            ELSE 'other_failure'
        END AS category
    FROM v_trace_trials
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (PARTITION BY category ORDER BY trial_id) AS rank_in_category
    FROM categorized
)
SELECT
    category,
    rank_in_category,
    trial_id,
    trial_name,
    card,
    arm,
    step_ref,
    report_path
FROM ranked
WHERE rank_in_category <= 2
ORDER BY category, rank_in_category;

-- --------------------------------------------------------------------------- --
-- 10. Evidence completeness
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE VIEW v_trace_evidence_completeness AS
SELECT
    COALESCE(card, 'unknown') AS card,
    COUNT(*) AS n_trials,
    SUM(CASE WHEN trajectory_available THEN 1 ELSE 0 END) AS n_trajectory_available,
    SUM(CASE WHEN processed_available THEN 1 ELSE 0 END) AS n_processed_available,
    SUM(CASE WHEN counts_available THEN 1 ELSE 0 END) AS n_counts_available,
    SUM(CASE WHEN labels_json != '[]' AND labels_json IS NOT NULL THEN 1 ELSE 0 END) AS n_labels_available,
    SUM(CASE WHEN step_evidence_source = 'stitched' THEN 1 ELSE 0 END) AS n_step_stitched,
    SUM(CASE WHEN step_evidence_source = 'none' THEN 1 ELSE 0 END) AS n_step_none,
    ROUND(SUM(CASE WHEN trajectory_available THEN 1.0 ELSE 0.0 END) / COUNT(*), 4) AS trajectory_coverage_rate,
    ROUND(SUM(CASE WHEN processed_available THEN 1.0 ELSE 0.0 END) / COUNT(*), 4) AS processed_coverage_rate
FROM v_trace_trials
GROUP BY COALESCE(card, 'unknown')
ORDER BY card;
