-- 07: Parse rejection evidence and error shapes (HAR-131).
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
