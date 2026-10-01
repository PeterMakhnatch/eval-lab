-- 02: First edit vs pass (step measure explicit) (HAR-131).
SELECT
    COALESCE(card, 'unknown') AS card,
    CASE WHEN raw_reward >= 1.0 THEN 'pass' ELSE 'fail' END AS outcome,
    COUNT(*) AS n_total,
    SUM(CASE WHEN first_edit_step IS NOT NULL THEN 1 ELSE 0 END) AS n_with_edit,
    SUM(CASE WHEN first_edit_step IS NULL THEN 1 ELSE 0 END) AS n_no_edit,
    ROUND(AVG(first_edit_step), 2) AS avg_first_edit_step,
    MIN(first_edit_step) AS min_first_edit_step,
    MAX(first_edit_step) AS max_first_edit_step,
    ROUND(SUM(CASE WHEN first_edit_step IS NOT NULL THEN 1.0 ELSE 0.0 END) / COUNT(*), 4) AS edit_rate
FROM v_trace_trials
GROUP BY COALESCE(card, 'unknown'), CASE WHEN raw_reward >= 1.0 THEN 'pass' ELSE 'fail' END
ORDER BY card, outcome;
