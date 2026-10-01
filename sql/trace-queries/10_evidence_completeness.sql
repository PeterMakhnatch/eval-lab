-- 10: Cross-cohort evidence completeness and gap audit (HAR-131).
SELECT
    COALESCE(card, 'unknown') AS card,
    COUNT(*) AS n_trials,
    SUM(CASE WHEN trajectory_available THEN 1 ELSE 0 END) AS n_trajectory_available,
    SUM(CASE WHEN processed_available THEN 1 ELSE 0 END) AS n_processed_available,
    SUM(CASE WHEN counts_available THEN 1 ELSE 0 END) AS n_counts_available,
    SUM(CASE WHEN labels_json != '[]' AND labels_json IS NOT NULL THEN 1 ELSE 0 END) AS n_labels_available,
    SUM(CASE WHEN step_evidence_source = 'atif_projection' THEN 1 ELSE 0 END) AS n_step_atif,
    SUM(CASE WHEN step_evidence_source = 'parquet_steps' THEN 1 ELSE 0 END) AS n_step_parquet,
    SUM(CASE WHEN step_evidence_source = 'none' THEN 1 ELSE 0 END) AS n_step_none,
    ROUND(SUM(CASE WHEN trajectory_available THEN 1.0 ELSE 0.0 END) / COUNT(*), 4) AS trajectory_coverage_rate,
    ROUND(SUM(CASE WHEN processed_available THEN 1.0 ELSE 0.0 END) / COUNT(*), 4) AS processed_coverage_rate
FROM v_trace_trials
GROUP BY COALESCE(card, 'unknown')
ORDER BY card;
