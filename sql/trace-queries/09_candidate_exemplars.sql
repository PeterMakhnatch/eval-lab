-- 09: Deterministic candidate exemplars (top 2 per category) (HAR-131).
-- step_ref is NULL when no recorded first-failure ref exists (never fabricated).
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
            WHEN raw_reward >= 1.0 AND counts_reasons_json LIKE '%pass_tainted%' THEN 'tainted_pass'
            WHEN counts_verdict = 'excluded' AND counts_reasons_json LIKE '%task_not_usable%' THEN 'task_not_usable'
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
