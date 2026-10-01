-- 01: Cohort raw pass rates and counts countability (HAR-131).
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
