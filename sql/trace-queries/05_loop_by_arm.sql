-- 05: Loop kind distribution by arm (HAR-131).
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
