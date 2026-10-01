-- 04: Post-edit tokens spent by stop reason (HAR-131).
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
