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

# Cheap triggers: clock checks require an actively published trajectory.
uv run evallab run-health triggers --trial-dir <harbor-trial-dir> --now 2026-10-28T21:00:00Z

# Per-source p50/p90/p99 of every numeric feature ("far above the usual for this job type").
uv run evallab run-health baseline --features /tmp/f.jsonl
```

`--db` opens the store read-only and keeps only admitted DEV runs. Point it at a
`.backup` copy, not the live file. `--trial-dir` reads a Harbor trial: the stitched
`agent/trajectory.json` plus `result.json` (phase timings, `exception_info`, stop
reason, token totals).

## Live Supervisor handoff

The stable Python entry point is
`triggers(run: HealthRun, *, now: float | None = None, feats=None)`.
It returns a list of `{"trigger", "kind", "value", "threshold", "op", "step"}` objects.
`step` is an original ATIF step id or null; `op` is `>=`, `<=`, or null for a
categorical error/status. No model, database writes, process control or network
calls occur. These are **triage inputs, not stop decisions or trace findings**.
Use `detect(run, feats)` for the four `trace-findings/v1` records.

```python
import time
from evallab.production.run_health import HealthRun, features, triggers

# The supervisor's live adapter supplies observed ATIF steps and lifecycle facts.
run = HealthRun(run_id, source, observed_steps, observed_meta)
feats = features(run)
pending = triggers(run, now=time.time(), feats=feats)
```

`observed_meta` may include `started_at` and `finished_at` (epoch or ISO); unknown
facts must stay missing. Pass `now` only while actively observing a live run.
Silence is measured from the latest step's timestamp, or from `started_at` before
the first step. If the latest step has no timestamp, silence is unknown, not
elapsed time since an older step. Recorded finish suppresses clock triggers.
Historical store rows have no finish time: `triggers --db` intentionally disables
clock checks, and Python callers must omit `now` for that source.

**Visibility boundary:** Harbor usually writes `result.json` only at finalize;
4,038 of 4,756 clean live-native trajectories inspected also had finalization-time
mtimes. Do not treat `trial_run()` as a universal live stream. Consume native
incremental snapshots or adapter-owned observed events, where available; otherwise
use controller state and live logs. An absent trajectory is not proof of silence.
Setup/verifier duration triggers need observed phase clocks; finalized phase times
cannot be used to claim an early warning.

The full threshold set remains in the detector table above. Extra clock triggers
are `silence >= 2700 s` and `live_wall_seconds >= 7200 s`; `baseline(rows, by="source")`
supplies per-source numeric p50/p90/p99 and sample counts, not new tuned thresholds.
Missing observations are omitted rather than counted as zero. An empty trigger
list means no observed trigger, **not** a healthy/clear run. Loop features cover
history and can remain above threshold after recovery: re-read recent evidence
and deduplicate episodes before asking a reader to decide.

### Measured trigger quality, not authorization to act

Thresholds below were unchanged during scoring. DEV audits used the admitted
cohort already described below; G6 is a frozen cross-check. Additional fleet and
archived setup studies are exploratory and did not set task thresholds.

| Signal | Threshold | Observed quality | Supervisor use |
|---|---|---|---|
| Loop | 8 repeated command/output pairs; 12-step cycle with at most 3 pairs; 10 identical messages; 5 completion claims | G6 prefix replay: 22/36 loops caught, 0/16 false alarms; median 189 s lead over 22 true positives. Finalized runs: 24/36, 0/16. DEV: 5/5 alarms confirmed, in-sample | Escalate to a reader; no automatic stop |
| Excess steps / wall time | 250 agent steps / 2 h | Blind DEV audit of 30 alarms: 5 wasting, 22 progressing, 3 rater disagreements; 30 controls all progressing. All 8 wall-time alarms were progressing | Budget/attention trigger, not evidence that stopping is safe |
| Slow steps | median 60 s; p90 300 s; gap 2700 s | 49 flagged DEV windows: 29 agreed unexplained stalls, 1 agent-command wait, 14 unjudgeable timings, 5 disagreements | Inspect command and timestamp semantics; unknown cases stay unknown |
| Infra tail | Frozen error signatures in the current tail | G6 prefix replay: 0/5 infra labels caught, 2/55 non-infra runs flagged transient errors. Finalized 5/5 at 0/55 is not live precision | Diagnose/recheck recovery, never stop from a log regex alone |
| Fleet mid-turn silence | 45 min; exclude outstanding `wait` | Day-3 fleet study: 5/6 fires stuck, one deliberate sleep, covering 5 distinct episodes. Small sample, one adjudicator, partly time-defined labels | Report only; never restart persistent chats |
| Token burn | DEV p99: 6509 output tok/min, 4643 output tok/step, 7776 context-growth tok/step | Timeout-prediction precision 6/43, 16/79, 9/62 versus 18–19% base rates; no MiMo DEV token baseline | Rejected; no token-burn trigger added |

Prefix replay hides the finish, exception and stop reason and polls at up to 20
step-arrival positions per trial plus exact clock crossings. It models already
available snapshots, not actual Harbor file-publication latency. The complete G6
subsets finished; the larger timeout replay was stopped under host memory pressure
and contributes no precision claim.

The 900 s setup-overrun candidate caught only 2/97 setup failures at 0/5,039 clean
false alarms in the live-native archive subset. This is insufficient for an action
gate. The six seeker `_TrialAbort` cases previously grouped with infra were
post-setup candidate `solve.sh` failures, not six setup outages; `exit-code.txt`
exposed them only a median 2.5 s before finalization. Fallback-route events exposed
21/21 route mismatches at 0/27 clean fires, median 120 s lead, but that comparison
is partly circular with the route-mismatch label.

No trigger here certifies the Live Supervisor's 90%-precision **decision/action**
gate. That gate needs separately adjudicated shadow decisions, a representative
base rate, and the supervisor's existing ownership and stop-cap checks.


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
