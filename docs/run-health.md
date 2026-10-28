---
status: living
audience:
  - analyst
  - operator
---

# Run Health

Logic: `src/evallab/production/run_health.py`. Tests: `tests/test_run_health.py`.
Contract: `trace-findings/v1` (Wave 6 bridge contract §3b), one record per run and kind.

Four cheap, deterministic detectors for run problems that are not cheating but still
waste compute or corrupt QA results. No model is called and no label, reward or
publisher flag is read, so a record can be scored against any label set later.

| Detector | Alarms when |
|---|---|
| `run_health.loop` | the agent repeats itself without progress: 8 steps with the same normalized command, the same tool output (per-call ids and numbers masked) and no edit; 10 identical messages; 5 completion claims; or a 12-step edit-free window with at most 3 distinct command-and-output pairs. Also when the harness stopped it for looping (`LoopBreakStop`, `loop_break`). Undo commands and repeated errors are reported as features only |
| `run_health.runaway` | 250 agent steps or 2 h of wall time; or a budget, timeout, turn or context limit ended the run (`AgentTimeoutError`, `TrialBudgetExhaustedError`, store `max_turns`/`context_length`/`LimitsExceeded`) |
| `run_health.latency` | median step 60 s, p90 step 300 s, a 45-minute gap between steps, or 15 minutes of setup or verifier time; abstains with fewer than 3 timestamped agent steps and no phase timings |
| `run_health.infra` | an infrastructure exception or run status (sandbox, admission, healthcheck, model API, verifier output; store `refused`/`error`/`provisional_execution_unavailable`), or an infrastructure signature in tool output of the last steps with no agent step after it |

Thresholds sit at or above the pooled p99 of each feature over the 3,809
actor-readable admitted DEV runs, or keep an existing harness constant that already
does; they were set from that unlabelled distribution before any score was read
(`run-health/2`). `run-health/4` then narrowed the loop rule after a blind DEV audit
(below). `score` is the largest value/threshold ratio, so `>= 1.0` is an alarm and a
scorer can sweep cuts.

A run without an actor transcript (`actor_inputs_available` false) is `unknown` for
every kind, never `clear`. It can still alarm on a run-level exception or status,
and infra on a signature that ends the run.

```bash
# Per-run features (JSONL): step counts, timing, tokens, repetition, markers.
uv run evallab run-health features --db <store-copy.sqlite> --dataset tb4-audit-v1 --out /tmp/f.jsonl
uv run evallab run-health features --trial-dir <harbor-trial-dir>

# trace-findings/v1 records for the four detectors.
uv run evallab run-health detect --db <store-copy.sqlite> --out /tmp/findings.jsonl
uv run evallab prod import-trace-findings --cloud-database <store-copy.sqlite> --jsonl /tmp/findings.jsonl

# Precision and recall against run-level labels ({"run_id": ..., "label": bool} per line).
uv run evallab run-health score --findings /tmp/findings.jsonl --labels labels.jsonl

# Beside the other detectors on the labelled G6 rows.
uv run evallab detectors score --detector run_health_loop --detector laminar_stuck_loop --check stuck_loop

# Live triggers for a run that may still be going (the Live Supervisor's cheap input).
uv run evallab run-health triggers --trial-dir <running-harbor-trial-dir> [--now 2026-10-28T21:00:00Z]

# Per-source p50/p90/p99 of every numeric feature ("far above the usual for this job type").
uv run evallab run-health baseline --features /tmp/f.jsonl
```

`--db` opens the store read-only and keeps only admitted DEV runs. Point it at a
`.backup` copy, not the live file. `--trial-dir` reads a Harbor trial: the stitched
`agent/trajectory.json` plus `result.json` (phase timings, `exception_info`, stop
reason, token totals).

## What each source provides

| Source | Timestamps | Tokens | Stop reason |
|---|---|---|---|
| Harbor trial dir | ATIF step `timestamp`, phase start and end | step `metrics`, `agent_result` totals | `exception_info`, `agent_result.metadata.stop_reason` |
| Store, Goodfire | `native_node_residual.timestamp` (2,459 of 3,674 runs) | none | receipt `native_status.stop_condition` |
| Store, TB4 | `source_timestamp` or `raw_message.timestamp` (55 of 61 actor runs) | `source_metrics` or `raw_message.usage` | run status in `outcome` |
| Store, MiMo and qa-trace-sft | none for actor runs | none | run status in `outcome` (MiMo journals) |

## Measured (2026-10-28)

- **DEV loop audit (blind, 136 windows):** two independent raters judged a 16-step
  window at each of the 68 `run-health/3` loop alarms plus 68 all-clear controls,
  without knowing which was which (135 of 136 verdicts agreed). Only 6 of 68 alarms
  were loops, against 4 of 68 controls: the old rule matched file paging
  (`sed -n '1,300p'`, `'300,600p'`) and log polling, because the command normalizer
  masks numbers. `run-health/4` also requires the same output: 5 DEV alarms, all 5
  judged loops (in-sample: tuned on this audit).
- **G6 stuck-loop rows (36 loops, 16 not), out of sample for `run-health/4`:** 24/36
  recall at 0/16 false alarms; ranked by score, 28/36 at 1/16. `run-health/3` had
  35/36 at 6/16 (Laminar `stuck_loop`: 34/36 at 6/16). The 12 misses are MiMo
  terminal-screen loops whose output changes on every repeat.
- **G6 infra rows:** 5/5 at 0/55. These labels come from the same exception field.
- **TB4/MiMo timeouts (129 timed out, 346 clean, our Harbor trials):** with
  `result.json`, runaway or infra catches 129/129 at 47/346 false alarms, but the
  labels are that exception field. From the trajectory alone, 56/107 at 37/346.
- **Admitted DEV alarm rates:** runaway 1.7% and latency 2.0% of Goodfire runs; loop 5
  runs, which are the same 5 runs whose Modal sandbox died mid-run (infra catches
  them too). Infra also catches 49 MiMo/TB4 journals refused or unexecuted for
  capacity. The other DEV loops the audit found are semantic (re-checking or
  resubmitting the same conclusion) and need a reader.
