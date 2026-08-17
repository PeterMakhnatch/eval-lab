# Evidence Review 2026-08-17

Promoted evidence bundles under `research/evidence/runs/` contain 11 trials across 5 bundles (3 canary tasks + 2 controls). The referenced 92-trial corpus is not present in committed promoted evidence; only these 11 trials are available on disk for analysis. No parquet projections or additional run directories are committed.

## terminal-bench-html-js-filter record
- 3 trials (all on 2026-08-15 with codex__gpt-5.6-terra__adhoc).
- All 3 scored reward=0.0 via verifier_result.rewards.reward; exception_info=null in every case.
- n=3, passes=0, harness_exceptions=0, scored_failures=3.
- Wilson 95% interval on 0/3: [0.0, 0.708] (computed via cohort.wilson_interval).
- This result is underpowered (n<5). Evidence supports "not yet distinguishable" between a genuinely hard task and a broken task. The specific check that would separate them is: execute the identical task with the oracle agent; if oracle scores 0.0 the task definition/verifier is broken; if oracle scores 1.0 the codex agent is the limiting factor. Current data cannot assert either cause.

## oracle/nop control record
- event-summary-oracle-evidence (2026-08-14): 1 trial, reward=1.0, exception=null. Correct.
- event-summary-nop-evidence (2026-08-14): 1 trial, reward=0.0, exception=null. Correct.
- Both controls match expected behavior (oracle=1.0, nop=0.0). No harness indictment from these runs.

## corpus temporal comparability
- All 11 promoted trials (08-13 to 08-15) have zero harness exceptions (exception_info remains null across oracle, nop, and all three canary tasks).
- The 08-14 exception cluster referenced in task context is absent from the promoted bundles.
- The 08-15 canary runs and 08-14 controls are comparable on the observed dimension (no NonZeroAgentExitCodeError / ValueError / harness failures).
- Canaries on 08-15 show event-summary and transaction-reconciliation both at 3/3 passes (n=3 each, underpowered), terminal-bench at 0/3.

## Summary counts from v_outcome_by_task_agent (via evidence_queries.sql on promoted fixtures)
- event-summary + codex: n=3, passes=3, pass_rate=100%, harness_exceptions=0
- terminal-bench-html-js-filter + codex: n=3, passes=0, pass_rate=0%, harness_exceptions=0, scored_failures=3 (underpowered)
- transaction-reconciliation + codex: n=3, passes=3, pass_rate=100%, harness_exceptions=0
- event-summary + oracle: n=1, passes=1
- event-summary + nop: n=1, passes=0

All underpowered cohorts (n<5) are explicitly marked underpowered rather than reported as findings. No exception taxonomy rows in current promoted evidence.