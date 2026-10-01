-- 06: Upstream fetch signals and counts exclusions (HAR-131).
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
