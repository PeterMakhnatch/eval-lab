-- HAR-131: transient analytical views, not another evidence store.
-- Native identity is (job_id, trial_id). Counts alone decides countability.
-- JSON is parsed by path/type, never by key substrings or free-text mentions.
-- Missing/invalid evidence is unknown. Rates expose their exact populations.

CREATE OR REPLACE MACRO trace_array(value) AS
    CASE WHEN json_type(value) = 'ARRAY' THEN value ELSE JSON '[]' END;
CREATE OR REPLACE MACRO trace_uint(value, path) AS
    CASE WHEN json_type(value, path) IN ('BIGINT', 'UBIGINT')
          AND TRY_CAST(json_extract_string(value, path) AS BIGINT) >= 0
         THEN TRY_CAST(json_extract_string(value, path) AS BIGINT) END;

-- Common source interpretation, kept at one row per native trial pair.
CREATE OR REPLACE VIEW _trace_analysis_trials AS
WITH parsed AS (
    SELECT t.*,
        TRY_CAST(counts_reasons_json AS JSON) AS reasons,
        TRY_CAST(counts_evidence_json AS JSON) AS counts_evidence,
        TRY_CAST(taint_json AS JSON) AS taint,
        TRY_CAST(decision_facts_json AS JSON) AS facts,
        TRY_CAST(decision_judgments_json AS JSON) AS judgments,
        TRY_CAST(outline_json AS JSON) AS outline,
        TRY_CAST(token_flow_json AS JSON) AS token_flow,
        TRY_CAST(diagnosis_json AS JSON) AS diagnosis,
        TRY_CAST(labels_json AS JSON) AS labels,
        TRY_CAST(shape_counts_json AS JSON) AS shapes,
        TRY_CAST(acceptance_json AS JSON) AS acceptance,
        TRY_CAST(rejection_causes_json AS JSON) AS rejection_causes
    FROM v_trace_trials t
), interpreted AS (
    SELECT p.*,
        COALESCE(scored IS TRUE AND raw_reward IS NOT NULL AND isfinite(raw_reward), false) AS is_scored,
        COALESCE(counts_available IS TRUE AND counts_verdict IN ('counted_pass', 'counted_fail', 'excluded'), false) AS counts_known,
        EXISTS (SELECT 1 FROM json_each(trace_array(reasons)) r WHERE r.type = 'VARCHAR' AND json_extract_string(r.value, '$') = 'infra') AS reason_infra,
        EXISTS (SELECT 1 FROM json_each(trace_array(reasons)) r WHERE r.type = 'VARCHAR' AND json_extract_string(r.value, '$') = 'copied_fix') AS reason_copied_fix,
        EXISTS (SELECT 1 FROM json_each(trace_array(reasons)) r WHERE r.type = 'VARCHAR' AND json_extract_string(r.value, '$') = 'pass_tainted') AS reason_pass_tainted,
        EXISTS (SELECT 1 FROM json_each(trace_array(reasons)) r WHERE r.type = 'VARCHAR' AND json_extract_string(r.value, '$') = 'task_not_usable') AS reason_task_not_usable,
        EXISTS (SELECT 1 FROM json_each(trace_array(taint)) f
                WHERE json_extract_string(f.value, '$.kind') = 'upstream_fetch')
        OR EXISTS (SELECT 1 FROM json_each(trace_array(counts_evidence)) f
                   WHERE json_extract_string(f.value, '$.detector') = 'upstream_fetch')
        OR EXISTS (SELECT 1 FROM json_each(trace_array(json_extract(facts, '$.fetches'))) f
                   WHERE f.type = 'OBJECT'
                     AND (json_type(f.value, '$.command') = 'VARCHAR' AND NULLIF(json_extract_string(f.value, '$.command'), '') IS NOT NULL
                       OR json_type(f.value, '$.step') = 'VARCHAR' AND NULLIF(json_extract_string(f.value, '$.step'), '') IS NOT NULL)) AS fetch_signal,
        COALESCE(json_type(facts, '$.fetches') = 'ARRAY' AND processed_available
                 AND EXISTS (SELECT 1 FROM v_trace_steps s
                             WHERE s.job_id = p.job_id AND s.trial_id = p.trial_id
                               AND NOT s.is_copied_context AND s.source IN ('agent', 'assistant')), false) AS fetch_assessed,
        COALESCE(json_type(labels) = 'ARRAY', false) AS labels_readable,
        CASE WHEN loop_kind IN ('completion-claim', 'repetition', 'none') THEN loop_kind END AS loop_prediction,
        COALESCE(stop_reason LIKE 'ceiling:%'
                 OR stop_reason IN ('trial_budget_exhausted', 'TrialBudgetExhaustedError'), false) AS budget_stop
    FROM parsed p
)
SELECT i.*,
    CASE WHEN NOT is_scored THEN 'unscored'
         WHEN raw_reward >= 1 THEN 'pass' ELSE 'fail' END AS raw_outcome,
    CASE WHEN fetch_signal THEN 'detected'
         WHEN fetch_assessed THEN 'no_detected_signal' ELSE 'unknown' END AS fetch_state
FROM interpreted i;

-- 1. An all-attempt yield is not a failure rate: unscored stays separate.
-- Only scored/finite rewards enter the scored pass denominator.
CREATE OR REPLACE VIEW v_trace_cohort_raw AS
SELECT COALESCE(card, 'unknown') AS card, COALESCE(arm, 'unknown') AS arm,
    COUNT(*) AS n_total,
    COUNT(*) FILTER (WHERE is_scored) AS n_scored,
    COUNT(*) FILTER (WHERE NOT is_scored) AS n_unscored,
    COUNT(*) FILTER (WHERE NOT is_scored AND raw_reward IS NOT NULL) AS n_reward_without_scored_status,
    COUNT(*) FILTER (WHERE raw_outcome = 'pass') AS n_raw_pass,
    COUNT(*) FILTER (WHERE raw_outcome = 'fail') AS n_raw_fail,
    ROUND(COUNT(*) FILTER (WHERE raw_outcome = 'pass') * 1.0 / COUNT(*), 4) AS raw_pass_rate_all_attempts,
    ROUND(COUNT(*) FILTER (WHERE raw_outcome = 'pass') * 1.0 / NULLIF(COUNT(*) FILTER (WHERE is_scored), 0), 4) AS raw_pass_rate_scored,
    COUNT(*) FILTER (WHERE counts_known AND counts_verdict = 'counted_pass') AS n_counted_pass,
    COUNT(*) FILTER (WHERE counts_known AND counts_verdict = 'counted_fail') AS n_counted_fail,
    COUNT(*) FILTER (WHERE counts_known AND counts_verdict = 'excluded') AS n_excluded,
    COUNT(*) FILTER (WHERE NOT counts_known) AS n_counts_unknown,
    COUNT(*) FILTER (WHERE counts_known AND counts_verdict IN ('counted_pass', 'counted_fail')) AS n_countable,
    ROUND(COUNT(*) FILTER (WHERE counts_known AND counts_verdict = 'counted_pass') * 1.0
          / NULLIF(COUNT(*) FILTER (WHERE counts_known AND counts_verdict IN ('counted_pass', 'counted_fail')), 0), 4) AS counted_pass_rate,
    COUNT(*) FILTER (WHERE counts_known AND counts_verdict = 'excluded' AND reason_infra) AS n_infra_excluded,
    COUNT(*) FILTER (WHERE budget_stop) AS n_budget_stops
FROM _trace_analysis_trials
GROUP BY COALESCE(card, 'unknown'), COALESCE(arm, 'unknown')
ORDER BY card, arm;

-- 2. Existing traj_features Parquet supplies the first-edit detector metric.
-- The loader resolves native IDs and measurement availability. Current
-- process_job outline omits this metric and cannot establish a negative.
CREATE OR REPLACE VIEW v_trace_first_edit_vs_pass AS
WITH measured AS (
    SELECT *,
        COALESCE(first_edit_measure_available
                 AND NULLIF(first_edit_evidence_source, '') IS NOT NULL
                 AND (first_edit_step IS NULL OR first_edit_step >= 0), false) AS edit_measure_available
    FROM _trace_analysis_trials
)
SELECT COALESCE(card, 'unknown') AS card, raw_outcome AS outcome,
    COUNT(*) AS n_total,
    COUNT(*) FILTER (WHERE edit_measure_available AND first_edit_step >= 0) AS n_with_edit,
    COUNT(*) FILTER (WHERE first_edit_step IS NULL AND edit_measure_available) AS n_no_detected_signal,
    COUNT(*) FILTER (WHERE NOT edit_measure_available) AS n_unknown_evidence,
    COUNT(*) FILTER (WHERE edit_measure_available) AS n_edit_measure_available,
    ROUND(AVG(first_edit_step) FILTER (WHERE edit_measure_available AND first_edit_step >= 0), 2) AS avg_first_edit_step,
    MIN(first_edit_step) FILTER (WHERE edit_measure_available AND first_edit_step >= 0) AS min_first_edit_step,
    MAX(first_edit_step) FILTER (WHERE edit_measure_available AND first_edit_step >= 0) AS max_first_edit_step,
    ROUND(COUNT(*) FILTER (WHERE edit_measure_available AND first_edit_step >= 0) * 1.0 / COUNT(*), 4) AS detected_edit_rate_all_attempts,
    'existing Parquet step_to_first_edit detector with unambiguous native-pair provenance, missing measurement is unknown, no signal is not proof of absence or persistence, and not a causal pass explanation' AS edit_limitation
FROM measured
GROUP BY COALESCE(card, 'unknown'), raw_outcome
ORDER BY card, outcome;

-- 3. Recorded command text is a signal, not proof that a test file was read.
-- Exclude copied context and reconstructed proposals. Aggregate steps BEFORE
-- the trial join, so all-trial denominators cannot grow with the step count.
CREATE OR REPLACE VIEW _trace_step_coverage AS
SELECT job_id, trial_id, COUNT(*) AS n_observed_steps,
    COUNT(*) FILTER (WHERE NOT is_copied_context AND source IN ('agent', 'assistant')) AS n_live_agent_steps,
    COUNT(*) FILTER (WHERE NOT is_copied_context AND source IN ('agent', 'assistant')
                           AND command_provenance = 'recorded' AND command_text IS NOT NULL) AS n_recorded_command_steps,
    COUNT(*) FILTER (WHERE NOT is_copied_context AND source IN ('agent', 'assistant')
                           AND command_provenance = 'reconstructed' AND command_text IS NOT NULL) AS n_reconstructed_command_steps,
    COUNT(*) FILTER (WHERE NOT is_copied_context AND source IN ('agent', 'assistant')
                           AND command_provenance = 'recorded'
                           AND regexp_matches(command_text, '(^|[^[:alnum:]_])(pytest|([^[:space:]]*/)?tests/|([^[:space:]]*/)?test_[^[:space:]]*[.]py)')) AS n_test_signal_steps
FROM v_trace_steps
GROUP BY job_id, trial_id;

CREATE OR REPLACE VIEW v_trace_test_path_access AS
SELECT COALESCE(t.card, 'unknown') AS card, COALESCE(t.arm, 'unknown') AS arm,
    COUNT(*) AS n_total_trials,
    COUNT(*) FILTER (WHERE s.n_observed_steps > 0) AS n_trials_with_steps,
    COUNT(*) FILTER (WHERE s.n_observed_steps IS NULL) AS n_trials_without_steps,
    ROUND(COUNT(*) FILTER (WHERE s.n_observed_steps > 0) * 1.0 / COUNT(*), 4) AS observed_step_coverage_rate,
    COUNT(*) FILTER (WHERE s.n_live_agent_steps > 0) AS n_trials_with_live_agent_steps,
    COUNT(*) FILTER (WHERE s.n_recorded_command_steps > 0) AS n_trials_with_recorded_commands,
    COUNT(*) FILTER (WHERE s.n_reconstructed_command_steps > 0) AS n_trials_with_reconstructed_commands,
    COUNT(*) FILTER (WHERE s.n_test_signal_steps > 0) AS n_trials_with_test_access,
    COALESCE(SUM(s.n_test_signal_steps), 0) AS n_steps_with_test_access,
    COUNT(*) FILTER (WHERE t.raw_outcome = 'pass' AND s.n_test_signal_steps > 0) AS n_passes_with_test_access,
    COUNT(*) FILTER (WHERE t.raw_outcome = 'pass' AND t.counts_known AND t.counts_verdict = 'excluded'
                           AND s.n_test_signal_steps > 0) AS n_excluded_passes_with_test_access,
    'recorded agent-command text mentioning a test path or pytest, not proof of file access, success, taint, or causal effect' AS signal_limitation
FROM _trace_analysis_trials t
LEFT JOIN _trace_step_coverage s ON s.job_id = t.job_id AND s.trial_id = t.trial_id
GROUP BY COALESCE(t.card, 'unknown'), COALESCE(t.arm, 'unknown')
ORDER BY card, arm;

-- 4. Recompute BOTH numerator and denominator from token_flow.prompt_series
-- native step metrics. Complete, ordinal-matched agent-call coverage is required
-- separately for input/output. Never divide by settled proxy totals or reuse
-- token_flow's mixed-source share_input. No detected edit is NOT post-edit waste.
CREATE OR REPLACE VIEW _trace_native_token_coverage AS
SELECT t.job_id, t.trial_id,
    COUNT(p.key) AS n_series,
    COUNT(DISTINCT trace_uint(p.value, '$.call_index')) AS n_call_indices,
    MIN(trace_uint(p.value, '$.call_index')) AS first_call_index,
    MAX(trace_uint(p.value, '$.call_index')) AS last_call_index,
    COUNT(trace_uint(p.value, '$.prompt_tokens')) AS n_input_metrics,
    COUNT(trace_uint(p.value, '$.completion_tokens')) AS n_output_metrics,
    SUM(trace_uint(p.value, '$.prompt_tokens')) AS native_input_total,
    SUM(trace_uint(p.value, '$.completion_tokens')) AS native_output_total,
    SUM(CASE WHEN trace_uint(p.value, '$.call_index') > trace_uint(t.token_flow, '$.last_useful_edit.call_index')
             THEN trace_uint(p.value, '$.prompt_tokens') ELSE 0 END) AS native_input_after_edit,
    SUM(CASE WHEN trace_uint(p.value, '$.call_index') > trace_uint(t.token_flow, '$.last_useful_edit.call_index')
             THEN trace_uint(p.value, '$.completion_tokens') ELSE 0 END) AS native_output_after_edit,
    MAX(CASE WHEN trace_uint(p.value, '$.call_index') = trace_uint(t.token_flow, '$.last_useful_edit.call_index')
             THEN trace_uint(p.value, '$.step_id') END) AS located_edit_step
FROM _trace_analysis_trials t
LEFT JOIN LATERAL json_each(trace_array(json_extract(t.token_flow, '$.prompt_series'))) p ON true
GROUP BY t.job_id, t.trial_id;

CREATE OR REPLACE VIEW v_trace_post_edit_tokens AS
WITH matched AS (
    SELECT t.*, n.* EXCLUDE (job_id, trial_id),
        COALESCE(json_extract_string(t.token_flow, '$.schema') = 'token_flow/v1'
                 AND n.n_series > 0 AND n.n_series = trace_uint(t.token_flow, '$.trajectory.n_agent_steps')
                 AND n.n_call_indices = n.n_series AND n.first_call_index = 1 AND n.last_call_index = n.n_series
                 AND n.located_edit_step = trace_uint(t.token_flow, '$.last_useful_edit.step_id'), false) AS population_matched
    FROM _trace_analysis_trials t
    LEFT JOIN _trace_native_token_coverage n ON n.job_id = t.job_id AND n.trial_id = t.trial_id
), eligible AS (
    SELECT *, population_matched AND n_input_metrics = n_series AS input_matched,
              population_matched AND n_output_metrics = n_series AS output_matched
    FROM matched
)
SELECT COALESCE(card, 'unknown') AS card, COALESCE(stop_reason, 'unknown') AS stop_reason,
    COUNT(*) AS n_total,
    COUNT(*) FILTER (WHERE json_type(token_flow) = 'OBJECT') AS n_with_token_flow,
    COUNT(*) FILTER (WHERE trace_uint(token_flow, '$.last_useful_edit.step_id') IS NOT NULL) AS n_with_detected_last_edit,
    COUNT(*) FILTER (WHERE trace_uint(token_flow, '$.last_useful_edit.step_id') IS NULL) AS n_last_edit_unknown_or_no_signal,
    COUNT(*) FILTER (WHERE input_matched) AS n_input_matched,
    COUNT(*) FILTER (WHERE NOT input_matched) AS n_input_unmatched_or_missing,
    COUNT(*) FILTER (WHERE output_matched) AS n_output_matched,
    COUNT(*) FILTER (WHERE NOT output_matched) AS n_output_unmatched_or_missing,
    SUM(native_input_total) FILTER (WHERE input_matched) AS matched_native_input_tokens,
    SUM(native_input_after_edit) FILTER (WHERE input_matched) AS matched_native_post_edit_input_tokens,
    ROUND(SUM(native_input_after_edit) FILTER (WHERE input_matched) * 1.0
          / NULLIF(SUM(native_input_total) FILTER (WHERE input_matched), 0), 4) AS post_edit_input_share,
    SUM(native_output_total) FILTER (WHERE output_matched) AS matched_native_output_tokens,
    SUM(native_output_after_edit) FILTER (WHERE output_matched) AS matched_native_post_edit_output_tokens,
    ROUND(SUM(native_output_after_edit) FILTER (WHERE output_matched) * 1.0
          / NULLIF(SUM(native_output_total) FILTER (WHERE output_matched), 0), 4) AS post_edit_output_share,
    COUNT(*) FILTER (WHERE input_tokens IS NOT NULL) AS n_with_proxy_input_totals,
    SUM(input_tokens) AS total_proxy_input_tokens,
    COUNT(*) FILTER (WHERE output_tokens IS NOT NULL) AS n_with_proxy_output_totals,
    SUM(output_tokens) AS total_proxy_output_tokens,
    'shares use complete ordinal-matched native prompt_series only, independently per direction, never settled proxy totals or agent_result totals, and the detected edit is a heuristic with unverified persistence' AS token_source_note
FROM eligible
GROUP BY COALESCE(card, 'unknown'), COALESCE(stop_reason, 'unknown')
ORDER BY card, stop_reason;

-- 5. Decision loop_kind is an opinion, never frozen ground truth or counts.
CREATE OR REPLACE VIEW v_trace_loop_by_arm AS
SELECT COALESCE(card, 'unknown') AS card, COALESCE(arm, 'unknown') AS arm,
    COUNT(*) AS n_total,
    COUNT(*) FILTER (WHERE loop_prediction = 'completion-claim') AS n_completion_claim,
    COUNT(*) FILTER (WHERE loop_prediction = 'repetition') AS n_repetition,
    COUNT(*) FILTER (WHERE loop_prediction = 'none') AS n_no_loop,
    COUNT(*) FILTER (WHERE loop_prediction IS NOT NULL) AS n_classified,
    COUNT(*) FILTER (WHERE loop_prediction IS NULL) AS n_unclassified,
    COUNT(*) FILTER (WHERE loop_kind IS NOT NULL AND loop_prediction IS NULL) AS n_invalid_prediction,
    ROUND(COUNT(*) FILTER (WHERE loop_prediction IS NOT NULL) * 1.0 / COUNT(*), 4) AS prediction_coverage_rate,
    ROUND(COUNT(*) FILTER (WHERE loop_prediction = 'completion-claim') * 1.0 / NULLIF(COUNT(*) FILTER (WHERE loop_prediction IS NOT NULL), 0), 4) AS claim_rate_classified,
    ROUND(COUNT(*) FILTER (WHERE loop_prediction = 'repetition') * 1.0 / NULLIF(COUNT(*) FILTER (WHERE loop_prediction IS NOT NULL), 0), 4) AS repetition_rate_classified,
    'recognized decision loop predictions are opinions, including none, unclassified means absent or invalid prediction, observed trajectory coverage is reported separately and no classifier opinion proves no loop' AS prediction_limitation
FROM _trace_analysis_trials
GROUP BY COALESCE(card, 'unknown'), COALESCE(arm, 'unknown')
ORDER BY card, arm;

-- 6. Detector flags are fetch signals, not proof a copied fix was used.
-- facts.fetches=[] proves only that the processor detected no fetch signal.
-- Missing taint (loader elides empty lists) alone is not negative evidence.
CREATE OR REPLACE VIEW v_trace_fetch_exclusion_audit AS
SELECT COALESCE(card, 'unknown') AS card, COALESCE(arm, 'unknown') AS arm,
    COUNT(*) AS n_total,
    COUNT(*) FILTER (WHERE fetch_state = 'detected') AS n_fetched,
    COUNT(*) FILTER (WHERE fetch_state = 'no_detected_signal') AS n_no_detected_fetch_signal,
    COUNT(*) FILTER (WHERE fetch_state = 'unknown') AS n_fetch_unknown,
    COUNT(*) FILTER (WHERE fetch_signal AND raw_outcome = 'pass') AS n_fetched_passed,
    COUNT(*) FILTER (WHERE counts_known AND counts_verdict = 'excluded' AND reason_copied_fix) AS n_copied_fix_excluded,
    COUNT(*) FILTER (WHERE counts_known AND counts_verdict = 'excluded' AND reason_pass_tainted) AS n_pass_tainted_excluded,
    COUNT(*) FILTER (WHERE counts_known AND counts_verdict = 'excluded' AND reason_task_not_usable) AS n_task_not_usable_excluded,
    COUNT(*) FILTER (WHERE counts_known AND counts_verdict = 'counted_fail' AND fetch_signal) AS n_counted_fail_with_fetch,
    COUNT(*) FILTER (WHERE NOT counts_known) AS n_counts_unknown
FROM _trace_analysis_trials
GROUP BY COALESCE(card, 'unknown'), COALESCE(arm, 'unknown')
ORDER BY card, arm;

-- 7. Shape classifier counts, acceptance counts, recorded-agreement cross-tab,
-- and rejection-cause classifications are distinct signals. False acceptance
-- and recorded provenance marginals do NOT identify their intersection.
CREATE OR REPLACE VIEW _trace_parse_signals AS
WITH shape_totals AS (
    SELECT t.job_id, t.trial_id, COUNT(s.key) AS n_shape_keys,
        COUNT(s.key) FILTER (WHERE trace_uint(s.value, '$') IS NULL) AS n_invalid_shape_keys,
        SUM(trace_uint(s.value, '$')) AS n_shape_steps
    FROM _trace_analysis_trials t
    LEFT JOIN LATERAL json_each(CASE WHEN json_type(t.shapes) = 'OBJECT' THEN t.shapes ELSE JSON '{}' END) s ON true
    GROUP BY t.job_id, t.trial_id
), causes AS (
    SELECT t.job_id, t.trial_id,
        SUM(trace_uint(c.value, '$.count')) FILTER (WHERE c.key <> 'harness_standin') AS n_rejection_cause_steps,
        COUNT(c.key) FILTER (WHERE trace_uint(c.value, '$.count') IS NULL) AS n_invalid_cause_entries
    FROM _trace_analysis_trials t
    LEFT JOIN LATERAL json_each(CASE WHEN json_type(t.rejection_causes) = 'OBJECT' THEN t.rejection_causes ELSE JSON '{}' END) d ON true
    LEFT JOIN LATERAL json_each(CASE WHEN d.type = 'OBJECT' THEN d.value ELSE JSON '{}' END) c ON true
    GROUP BY t.job_id, t.trial_id
)
SELECT t.*, s.n_shape_steps,
    COALESCE(json_type(t.shapes) = 'OBJECT' AND s.n_shape_keys > 0
             AND s.n_invalid_shape_keys = 0 AND s.n_shape_steps > 0, false) AS shape_observed,
    CASE WHEN json_exists(t.shapes, '$.unparseable') = false THEN 0
         ELSE trace_uint(t.shapes, '$.unparseable') END AS n_unparseable,
    trace_uint(t.acceptance, '$.counts.false') AS n_acceptance_false,
    trace_uint(t.acceptance, '$.counts.true') AS n_acceptance_true,
    trace_uint(t.acceptance, '$.counts.unknown') AS n_acceptance_unknown,
    trace_uint(t.acceptance, '$.provenance.recorded') AS n_acceptance_recorded,
    trace_uint(t.acceptance, '$.provenance.inferred') AS n_acceptance_inferred,
    trace_uint(t.acceptance, '$.provenance.reconstructed') AS n_acceptance_reconstructed,
    trace_uint(t.acceptance, '$.agreement.agree_reject') AS n_recorded_agree_reject,
    c.n_rejection_cause_steps, c.n_invalid_cause_entries
FROM _trace_analysis_trials t
LEFT JOIN shape_totals s ON s.job_id = t.job_id AND s.trial_id = t.trial_id
LEFT JOIN causes c ON c.job_id = t.job_id AND c.trial_id = t.trial_id;

CREATE OR REPLACE VIEW v_trace_parse_rejection_evidence AS
SELECT COALESCE(model_name, 'unknown') AS model_name, COALESCE(card, 'unknown') AS card,
    COUNT(*) AS n_total,
    COUNT(*) FILTER (WHERE shape_observed) AS n_shape_observed,
    COUNT(*) FILTER (WHERE NOT shape_observed) AS n_shape_unknown,
    COUNT(*) FILTER (WHERE shape_observed AND n_unparseable > 0) AS n_unparseable_shape_runs,
    SUM(n_unparseable) FILTER (WHERE shape_observed) AS n_unparseable_shape_steps,
    ROUND(COUNT(*) FILTER (WHERE shape_observed AND n_unparseable > 0) * 1.0
          / NULLIF(COUNT(*) FILTER (WHERE shape_observed), 0), 4) AS unparseable_shape_run_rate_observed,
    COUNT(*) FILTER (WHERE n_acceptance_false + n_acceptance_true + n_acceptance_unknown > 0) AS n_acceptance_observed,
    COUNT(*) FILTER (WHERE COALESCE(n_acceptance_false + n_acceptance_true + n_acceptance_unknown, 0) = 0) AS n_acceptance_unknown,
    SUM(n_acceptance_false) FILTER (WHERE n_acceptance_false + n_acceptance_true + n_acceptance_unknown > 0) AS n_acceptance_false_steps,
    SUM(n_acceptance_recorded) AS n_recorded_provenance_steps,
    SUM(n_acceptance_inferred) AS n_inferred_provenance_steps,
    SUM(n_acceptance_reconstructed) AS n_reconstructed_provenance_steps,
    COUNT(*) FILTER (WHERE n_recorded_agree_reject > 0 AND n_recorded_agree_reject <= n_acceptance_recorded) AS n_runs_with_recorded_agree_reject,
    COUNT(*) FILTER (WHERE n_recorded_agree_reject IS NOT NULL AND n_acceptance_recorded > 0
                           AND n_recorded_agree_reject <= n_acceptance_recorded) AS n_recorded_agreement_observed,
    SUM(n_recorded_agree_reject) FILTER (WHERE n_acceptance_recorded > 0
                                             AND n_recorded_agree_reject <= n_acceptance_recorded) AS n_recorded_agree_reject_steps,
    CAST(NULL AS BIGINT) AS n_complete_recorded_false_steps,
    SUM(n_rejection_cause_steps) FILTER (WHERE shape_observed AND n_invalid_cause_entries = 0) AS n_classified_rejection_cause_steps,
    COUNT(*) FILTER (WHERE budget_stop) AS n_budget_stops,
    COUNT(*) FILTER (WHERE stop_reason IN ('agent_timeout', 'AgentTimeoutError')) AS n_timeout_stops,
    'unparseable is a model-shape classifier, not harness rejection, false acceptance mixes recorded and inferred evidence, agree_reject directly observes recorded rejection plus agreeing observation (agent population may include harness stand-ins), no complete recorded-false cross-tab is supplied, causes classify rejections and are not recorded parse-error proofs' AS parse_limitation
FROM _trace_parse_signals
GROUP BY COALESCE(model_name, 'unknown'), COALESCE(card, 'unknown')
ORDER BY card, model_name;

-- 8. Freeze verification belongs to connect_trace_query. Agreement is partitioned
-- per native trial and admitted cohort (har119, har128-har116, har128-g2-a1,
-- har128-g2-r2, har128-g2-tail), requiring >= 2 distinct raters in that cohort to agree.
-- Conflicting duplicate votes are disagreement, and same-rater duplicate votes
-- do not create another rater. Rows are non-additive per-cohort study results:
-- cross-cohort disagreements are reported separately, never pooled into
-- cross-cohort votes. SFT-pass and hand labels remain coverage-only, strictly
-- excluded from loop voting. n_total is the selected card's native population,
-- not the frozen study size; n_agreed/eligibleN are the alignment denominators.
CREATE OR REPLACE VIEW _trace_label_entries AS
SELECT t.job_id, t.trial_id, e.key AS entry_index,
    json_extract_string(e.value, '$.cohort') AS cohort,
    json_extract_string(e.value, '$.rater') AS rater,
    json_extract_string(e.value, '$.provenance') AS provenance,
    json_extract_string(e.value, '$.label_scope') AS label_scope,
    json_extract_string(e.value, '$.trial_name') AS label_trial_name,
    CASE WHEN json_type(e.value, '$.loop_kind') = 'VARCHAR'
         THEN json_extract_string(e.value, '$.loop_kind') END AS label_loop_kind,
    json_exists(e.value, '$.loop_kind') AS has_loop_field
FROM _trace_analysis_trials t,
LATERAL json_each(trace_array(t.labels)) e
WHERE e.type = 'OBJECT';

CREATE OR REPLACE VIEW _trace_label_consensus AS
WITH admitted_cohorts AS (
    SELECT DISTINCT cohort
    FROM _trace_label_entries
    WHERE cohort IN ('har119', 'har128-har116', 'har128-g2-a1', 'har128-g2-r2', 'har128-g2-tail')
    UNION
    SELECT 'har119'
    WHERE NOT EXISTS (
        SELECT 1 FROM _trace_label_entries WHERE cohort IN ('har119', 'har128-har116', 'har128-g2-a1', 'har128-g2-r2', 'har128-g2-tail')
    )
), trials_x_cohorts AS (
    SELECT t.*, ac.cohort
    FROM _trace_analysis_trials t
    CROSS JOIN admitted_cohorts ac
), loop_entries AS (
    SELECT e.*, t.trial_name,
        COALESCE(e.provenance = 'agent_rater'
                 AND NULLIF(e.rater, '') IS NOT NULL
                 AND e.label_trial_name = t.trial_name
                 AND e.label_loop_kind IN ('completion-claim', 'repetition', 'none'), false) AS vote_valid
    FROM _trace_label_entries e
    JOIN _trace_analysis_trials t ON t.job_id = e.job_id AND t.trial_id = e.trial_id
    WHERE COALESCE(e.cohort, '') <> 'har128-sft-pass'
      AND COALESCE(e.label_scope, '') <> 'sft_pass_cleanliness'
      AND e.cohort IN ('har119', 'har128-har116', 'har128-g2-a1', 'har128-g2-r2', 'har128-g2-tail')
), loop_summary AS (
    SELECT job_id, trial_id, cohort,
        COUNT(*) AS n_cohort_votes,
        COUNT(DISTINCT rater) FILTER (WHERE vote_valid) AS n_valid_raters,
        COUNT(*) FILTER (WHERE NOT vote_valid) AS n_invalid_votes,
        COUNT(DISTINCT label_loop_kind) FILTER (WHERE vote_valid) AS n_distinct_kinds,
        MIN(label_loop_kind) FILTER (WHERE vote_valid) AS agreed_candidate
    FROM loop_entries
    GROUP BY job_id, trial_id, cohort
), foreign_loop_votes AS (
    SELECT job_id, trial_id,
        COUNT(*) AS n_foreign_loop_entries
    FROM _trace_label_entries
    WHERE COALESCE(cohort, '') NOT IN ('har119', 'har128-har116', 'har128-g2-a1', 'har128-g2-r2', 'har128-g2-tail', 'har128-sft-pass', 'har109')
      AND (has_loop_field OR provenance = 'agent_rater')
    GROUP BY job_id, trial_id
), coverage AS (
    SELECT job_id, trial_id, COUNT(*) AS n_label_entries,
        COUNT(*) FILTER (WHERE cohort = 'har109') AS n_hand_entries,
        COUNT(*) FILTER (WHERE cohort = 'har128-sft-pass' AND label_scope = 'sft_pass_cleanliness') AS n_sft_entries
    FROM _trace_label_entries
    GROUP BY job_id, trial_id
)
SELECT tc.job_id, tc.trial_id, tc.card, tc.cohort, tc.loop_prediction,
    COALESCE(c.n_label_entries, 0) AS n_label_entries,
    COALESCE(c.n_hand_entries, 0) AS n_hand_entries,
    COALESCE(c.n_sft_entries, 0) AS n_sft_entries,
    CASE WHEN NOT tc.labels_readable THEN 'unknown_label_input'
         WHEN COALESCE(f.n_foreign_loop_entries, 0) > 0 THEN 'ambiguous_cohort'
         WHEN s.n_cohort_votes IS NULL OR s.n_cohort_votes = 0 THEN 'missing_loop_labels'
         WHEN s.n_distinct_kinds > 1 THEN 'disagreement'
         WHEN s.n_valid_raters >= 2 AND s.n_invalid_votes = 0 AND s.n_distinct_kinds = 1 THEN 'agreed'
         ELSE 'insufficient_or_invalid' END AS label_state,
    CASE WHEN s.n_valid_raters >= 2 AND s.n_invalid_votes = 0 AND s.n_distinct_kinds = 1
         THEN s.agreed_candidate END AS agreed_kind
FROM trials_x_cohorts tc
LEFT JOIN loop_summary s ON s.job_id = tc.job_id AND s.trial_id = tc.trial_id AND s.cohort = tc.cohort
LEFT JOIN foreign_loop_votes f ON f.job_id = tc.job_id AND f.trial_id = tc.trial_id
LEFT JOIN coverage c ON c.job_id = tc.job_id AND c.trial_id = tc.trial_id;

CREATE OR REPLACE VIEW v_trace_frozen_label_agreement AS
SELECT COALESCE(card, 'unknown') AS card,
    cohort,
    COUNT(*) AS n_total,
    COUNT(*) FILTER (WHERE n_label_entries > 0) AS n_with_frozen_labels,
    COUNT(*) FILTER (WHERE n_hand_entries > 0) AS n_with_hand_labels,
    COUNT(*) FILTER (WHERE n_sft_entries > 0) AS n_with_sft_labels,
    COUNT(*) FILTER (WHERE label_state = 'missing_loop_labels') AS n_missing_loop_labels,
    COUNT(*) FILTER (WHERE label_state = 'unknown_label_input') AS n_unknown_label_input,
    COUNT(*) FILTER (WHERE label_state = 'ambiguous_cohort') AS n_ambiguous_cohort,
    COUNT(*) FILTER (WHERE label_state = 'insufficient_or_invalid') AS n_insufficient_or_invalid_labels,
    COUNT(*) FILTER (WHERE label_state = 'disagreement') AS n_disagreement,
    COUNT(*) FILTER (WHERE label_state = 'agreed') AS n_agreed,
    COUNT(*) FILTER (WHERE label_state = 'agreed' AND agreed_kind = 'completion-claim') AS n_agreed_claim,
    COUNT(*) FILTER (WHERE label_state = 'agreed' AND agreed_kind = 'repetition') AS n_agreed_repetition,
    COUNT(*) FILTER (WHERE label_state = 'agreed' AND agreed_kind = 'none') AS n_agreed_none,
    COUNT(*) FILTER (WHERE label_state = 'agreed' AND loop_prediction IS NULL) AS n_prediction_abstention,
    COUNT(*) FILTER (WHERE label_state = 'agreed' AND loop_prediction IS NOT NULL) AS eligibleN,
    COUNT(*) FILTER (WHERE label_state = 'agreed' AND loop_prediction = agreed_kind) AS n_match,
    ROUND(COUNT(*) FILTER (WHERE label_state = 'agreed' AND loop_prediction = agreed_kind) * 1.0
          / NULLIF(COUNT(*) FILTER (WHERE label_state = 'agreed' AND loop_prediction IS NOT NULL), 0), 4) AS accuracy,
    'agreement uses valid top-level loop_kind votes from at least two distinct agent raters in an admitted cohort (har119, har128-har116, har128-g2-a1, har128-g2-r2, har128-g2-tail), evaluated separately per cohort with non-additive rows; n_total is card-wide coverage, not the study denominator; excludes within-cohort disagreement and missing labels, prediction abstentions are outside eligibleN, HAR-109 hand and HAR-128 cleanliness labels are coverage only, descriptive frozen-cohort alignment is not general calibration' AS agreement_limitation
FROM _trace_label_consensus
GROUP BY COALESCE(card, 'unknown'), cohort
ORDER BY card, cohort;
-- 9. Category precedence is explicit, overlapping counts reasons are retained.
-- Ordinary raw passes are never other_failure. task_not_usable is separate
-- from pass_tainted. A first_failure judgment is an OPINION anchor. A link is
-- supplied only if that exact ref resolves uniquely to a real hashed ATIF row.
CREATE OR REPLACE VIEW v_trace_candidate_exemplars AS
WITH resolved AS (
    SELECT t.job_id, t.trial_id,
        COUNT(s.step_ref) AS n_anchor_matches,
        MIN(s.step_ref) AS matched_ref,
        MIN(s.source_path) AS matched_source_path,
        MIN(s.source_sha256) AS matched_source_sha256,
        BOOL_OR(s.is_copied_context) AS copied_anchor,
        MIN(s.source) AS matched_step_source,
        MIN(s.command_provenance) AS matched_command_provenance
    FROM _trace_analysis_trials t
    LEFT JOIN v_trace_steps s ON s.job_id = t.job_id AND s.trial_id = t.trial_id
                            AND s.step_ref = t.first_failure_ref
                            AND NULLIF(s.source_sha256, '') IS NOT NULL
    GROUP BY t.job_id, t.trial_id
), categorized AS (
    SELECT t.*, r.* EXCLUDE (job_id, trial_id),
        CASE
            WHEN counts_known AND counts_verdict = 'excluded' AND reason_task_not_usable THEN 'task_not_usable'
            WHEN counts_known AND counts_verdict = 'excluded' AND raw_outcome = 'pass' AND reason_copied_fix THEN 'copied_pass'
            WHEN counts_known AND counts_verdict = 'excluded' AND raw_outcome = 'pass' AND reason_pass_tainted THEN 'tainted_pass'
            WHEN raw_outcome = 'pass' AND counts_known AND counts_verdict = 'counted_pass' THEN 'counted_pass'
            WHEN raw_outcome = 'pass' AND NOT counts_known THEN 'raw_pass_counts_unknown'
            WHEN raw_outcome = 'pass' THEN 'raw_pass_other_counts_state'
            WHEN counts_known AND counts_verdict = 'excluded' AND reason_infra THEN 'infra_excluded'
            WHEN raw_outcome = 'unscored' THEN 'unscored_unknown'
            WHEN loop_prediction = 'completion-claim' THEN 'completion_claim_loop_opinion'
            WHEN loop_prediction = 'repetition' THEN 'repetition_loop_opinion'
            WHEN budget_stop AND first_edit_measure_available AND first_edit_step >= 0 THEN 'budget_stop_detected_edit'
            WHEN budget_stop THEN 'budget_stop_edit_unknown_or_no_signal'
            ELSE 'other_scored_failure'
        END AS category
    FROM _trace_analysis_trials t
    LEFT JOIN resolved r ON r.job_id = t.job_id AND r.trial_id = t.trial_id
), ranked AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY category ORDER BY job_id, trial_id) AS rank_in_category
    FROM categorized
)
SELECT category, rank_in_category, job_id, trial_id, trial_name, card, arm,
    raw_reward, raw_outcome, counts_verdict, counts_known, counts_reasons_json,
    reason_task_not_usable, reason_copied_fix, reason_pass_tainted,
    first_failure_ref AS first_failure_opinion_ref,
    CASE WHEN n_anchor_matches = 1 THEN matched_ref END AS step_ref,
    CASE WHEN n_anchor_matches = 1 THEN source_trial_dir || '/' || matched_source_path END AS trajectory_path,
    CASE WHEN n_anchor_matches = 1 THEN matched_source_sha256 END AS trajectory_sha256,
    CASE WHEN n_anchor_matches = 1 THEN matched_step_source END AS anchor_step_source,
    CASE WHEN n_anchor_matches = 1 THEN matched_command_provenance END AS anchor_command_provenance,
    CASE WHEN first_failure_ref IS NULL THEN 'absent'
         WHEN n_anchor_matches = 0 THEN 'unresolved_opinion_anchor'
         WHEN n_anchor_matches > 1 THEN 'ambiguous_opinion_anchor'
         WHEN copied_anchor THEN 'opinion_anchor_on_copied_context'
         WHEN matched_step_source NOT IN ('agent', 'assistant') THEN 'opinion_context_anchor_not_agent_action'
         ELSE 'opinion_anchor_on_native_agent_step_not_causal_proof' END AS step_ref_provenance,
    report_path, source_trial_dir,
    'deterministic candidates, not representative or causal exemplars, counts reasons can overlap, detector/first_failure opinions are not recorded actions, path plus exact ref and source hash identifies the actual ATIF evidence' AS exemplar_limitation
FROM ranked
WHERE rank_in_category <= 2
ORDER BY category, rank_in_category;

-- 10. Physical trajectory existence is not observed-step availability. Empty
-- labels are no frozen coverage, not a clean label. Counts remains unknown
-- unless a valid authoritative counts verdict is present.
CREATE OR REPLACE VIEW v_trace_evidence_completeness AS
SELECT COALESCE(t.card, 'unknown') AS card, COUNT(*) AS n_trials,
    COUNT(*) FILTER (WHERE t.trajectory_available) AS n_trajectory_available,
    COUNT(*) FILTER (WHERE NOT t.trajectory_available) AS n_trajectory_missing,
    COUNT(*) FILTER (WHERE t.processed_available) AS n_processed_available,
    COUNT(*) FILTER (WHERE NOT t.processed_available) AS n_processed_missing,
    COUNT(*) FILTER (WHERE t.counts_known) AS n_counts_available,
    COUNT(*) FILTER (WHERE NOT t.counts_known) AS n_counts_unknown,
    COUNT(*) FILTER (WHERE NOT t.is_scored) AS n_unscored,
    COUNT(*) FILTER (WHERE s.n_observed_steps > 0) AS n_trials_with_observed_steps,
    COUNT(*) FILTER (WHERE s.n_observed_steps IS NULL) AS n_trials_without_observed_steps,
    COUNT(*) FILTER (WHERE t.trajectory_available AND s.n_observed_steps IS NULL) AS n_trajectory_present_without_observed_steps,
    COUNT(*) FILTER (WHERE l.n_label_entries > 0) AS n_labels_available,
    COUNT(*) FILTER (WHERE l.n_label_entries = 0) AS n_no_frozen_label_entries,
    COUNT(*) FILTER (WHERE NOT t.labels_readable) AS n_label_input_unknown,
    COUNT(*) FILTER (WHERE l.n_sft_entries > 0) AS n_sft_labels_available,
    COUNT(*) FILTER (WHERE t.loop_prediction IS NULL) AS n_loop_prediction_unknown,
    COUNT(*) FILTER (WHERE t.input_tokens IS NULL) AS n_proxy_input_unknown,
    COUNT(*) FILTER (WHERE t.output_tokens IS NULL) AS n_proxy_output_unknown,
    ROUND(COUNT(*) FILTER (WHERE t.trajectory_available) * 1.0 / COUNT(*), 4) AS trajectory_file_coverage_rate,
    ROUND(COUNT(*) FILTER (WHERE s.n_observed_steps > 0) * 1.0 / COUNT(*), 4) AS observed_step_coverage_rate,
    ROUND(COUNT(*) FILTER (WHERE t.processed_available) * 1.0 / COUNT(*), 4) AS processed_coverage_rate
FROM _trace_analysis_trials t
LEFT JOIN _trace_step_coverage s ON s.job_id = t.job_id AND s.trial_id = t.trial_id
LEFT JOIN (
    SELECT job_id, trial_id,
        MAX(n_label_entries) AS n_label_entries,
        MAX(n_sft_entries) AS n_sft_entries
    FROM _trace_label_consensus
    GROUP BY job_id, trial_id
) l ON l.job_id = t.job_id AND l.trial_id = t.trial_id
GROUP BY COALESCE(t.card, 'unknown')
ORDER BY card;
