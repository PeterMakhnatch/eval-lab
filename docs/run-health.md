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
| `run_health.loop` | the agent repeats itself without progress: 8 identical normalized commands with no edit, 10 identical messages, 5 completion claims, a 12-step edit-free window with at most 3 distinct commands, 3 explicit undo commands after an edit, or the same error output 10 times; or the harness stopped it for looping (`LoopBreakStop`, `loop_break`) |
| `run_health.runaway` | 250 agent steps or 2 h of wall time; or a budget, timeout, turn or context limit ended the run (`AgentTimeoutError`, `TrialBudgetExhaustedError`, store `max_turns`/`context_length`/`LimitsExceeded`) |
| `run_health.latency` | median step 60 s, p90 step 300 s, a 45-minute gap between steps, or 15 minutes of setup or verifier time; abstains with fewer than 3 timestamped agent steps and no phase timings |
| `run_health.infra` | an infrastructure exception or run status (sandbox, admission, healthcheck, model API, verifier output; store `refused`/`error`/`provisional_execution_unavailable`), or an infrastructure signature in tool output of the last steps with no agent step after it |

Thresholds (`run-health/3`) sit at or above the pooled p99 of each feature over the
3,809 actor-readable admitted DEV runs, or keep an existing harness constant that
already does. They were set from that unlabelled distribution before any score was
read. `score` is the largest value/threshold ratio, so `>= 1.0` is an alarm and a
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

## Measured (2026-10-28, run-health/3)

- **G6 stuck-loop rows (36 loops, 16 not):** own verdict 35/36 recall, 6/16 false
  alarms, the same false-alarm count as Laminar's `stuck_loop` Signal (34/36, 6/16),
  at $0. Ranked by score: 28/36 at 0/16. Identical with the harness's own stop fields removed.
- **G6 infra rows:** 5/5 at 0/55. These labels come from the same exception field.
- **TB4/MiMo timeouts (129 timed out, 346 clean, our Harbor trials):** with
  `result.json`, runaway or infra catches 129/129 at 47/346 false alarms, but the
  labels are that exception field. From the trajectory alone, 56/107 at 37/346.
- **Admitted DEV alarm rates:** loop 1.9%, runaway 1.7% and latency 2.0% of Goodfire
  runs. Infra catches the 5 Goodfire runs whose Modal sandbox died mid-run, and 49
  MiMo/TB4 journals refused or unexecuted for capacity.
