-- 03: Test-path access signals in agent commands (HAR-131).
SELECT
    COALESCE(card, 'unknown') AS card,
    COALESCE(arm, 'unknown') AS arm,
    COUNT(DISTINCT trial_id) AS n_total_trials,
    COUNT(DISTINCT CASE
        WHEN command_text LIKE '%test%'
          OR command_text LIKE '%testbed/tests%'
          OR observation_excerpt LIKE '%test%'
        THEN trial_id
    END) AS n_trials_with_test_access,
    COUNT(CASE
        WHEN command_text LIKE '%test%'
          OR command_text LIKE '%testbed/tests%'
          OR observation_excerpt LIKE '%test%'
        THEN 1
    END) AS n_steps_with_test_access,
    COUNT(DISTINCT CASE
        WHEN raw_reward >= 1.0
         AND (command_text LIKE '%test%' OR observation_excerpt LIKE '%test%')
        THEN trial_id
    END) AS n_passes_with_test_access,
    COUNT(DISTINCT CASE
        WHEN raw_reward >= 1.0
         AND counts_verdict = 'excluded'
         AND (command_text LIKE '%test%' OR observation_excerpt LIKE '%test%')
        THEN trial_id
    END) AS n_excluded_passes_with_test_access
FROM v_trace_steps
GROUP BY COALESCE(card, 'unknown'), COALESCE(arm, 'unknown')
ORDER BY card, arm;
