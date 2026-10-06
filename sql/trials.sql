-- Transient trial census view (HAR-178).
--
-- `trials_base` is a transient Arrow registration built by
-- `evallab.storage.trials.connect_trials` (one row per deduped local trial:
-- finished, infra, and unfinished). This file documents the view contract;
-- the module generates the identical statement via `trials_view_sql()`.
--
-- `date` is typed via TRY_CAST so the recorded timestamp stays
-- query-usable while malformed values read as NULL, never an error.
-- `legit` holds exactly six clauses: unknown inputs coalesce to false,
-- and reward positivity, integrity, and copy verdicts never participate.

CREATE OR REPLACE VIEW trials AS
SELECT
  "job_id",
  "trial_id",
  "campaign",
  "job",
  "trial",
  "task",
  TRY_CAST("date" AS TIMESTAMPTZ) AS "date",
  "model",
  "harness",
  "egress_lock",
  "reference_profile",
  "reference_profile_match",
  "reference_profile_diffs",
  "stop_reason",
  "cut_short_by_our_limits",
  "infra",
  "infra_exception",
  "reward",
  "integrity",
  "reward_gated",
  "copy_verdict",
  "laminar_trace_id",
  "source_job_dir",
  "source_trial_dir",
  "projection_error",
  COALESCE(
    "model" = 'XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B'
    AND "harness" = 'evallab.harbor_mimoagent:NativeMimoAgent'
    AND "egress_lock"
    AND "reference_profile_match"
    AND NOT "infra"
    AND NOT "cut_short_by_our_limits",
    FALSE
  ) AS "legit"
FROM "trials_base";
