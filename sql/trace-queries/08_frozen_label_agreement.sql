-- 08: Frozen label agreement and coverage (HAR-131).
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
