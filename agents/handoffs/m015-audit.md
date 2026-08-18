Status: complete
Last: completed cycle 6 audit of quota accounting vs events.jsonl truth (CONFIRMED)
Next: Integrator review and PR merge into main
Blockers: none

# M015: LOOP-AUDIT Handoff

Auditing handoff claims across invisible-surface modules against origin/main.

## Ledger

| date | subject | handoff | verdict | evidence path | risk note |
|---|---|---|---|---|---|
| 2026-08-18 | preflight | `agents/handoffs/preflight.md` | CONFIRMED | `research/audits/evidence/preflight/` | Core runtime and contract tests pass (31 tests); operator docs in `docs/operations.md` omit manual command reference |
| 2026-08-18 | storm | `agents/handoffs/storm-status.md` | CONFIRMED | `research/audits/evidence/storm/` | Storm engine, tests (11 tests), models, and banner utilities verified; engine lacks a direct CLI subcommand |
| 2026-08-18 | parquet-compaction | `agents/handoffs/parquet-compaction.md` | CONFIRMED | `research/audits/evidence/parquet-compaction/` | Compaction engine, tests (15 tests), and dry-run across 72 jobs on 9 tables pass; runs via module invocation `python -m evallab.parquet_compaction` |
| 2026-08-18 | status/status_generator | `agents/handoffs/storm-status.md` | CONFIRMED | `research/audits/evidence/status_generator/` | Markdown generator and tests (9 tests) verified; defaults to `research/experiments/STATUS.md` rather than `docs/STATUS.md`, no direct CLI subcommand |
| 2026-08-18 | preflight (correction) | `agents/handoffs/preflight.md` | CONFIRMED | `research/audits/evidence/preflight/` | Sharpened standard: CONFIRMED on execution surface (`uv run evallab preflight` works live as claimed); documentation in `docs/operations.md` remains missing |
| 2026-08-18 | storm (correction) | `agents/handoffs/storm-status.md` | DRIFTED | `research/audits/evidence/storm/` | Supersedes row 2: `storm.py` has no CLI entrypoint and is not imported or called anywhere in `src/` (unwired in production); unit tests pass but module is unreachable |
| 2026-08-18 | parquet-compaction (correction) | `agents/handoffs/parquet-compaction.md` | CONFIRMED | `research/audits/evidence/parquet-compaction/` | Sharpened standard: CONFIRMED on documented module entrypoint (`python -m evallab.parquet_compaction compact --dry-run` works); lacks root `evallab` CLI subcommand |
| 2026-08-18 | status/status_generator (correction) | `agents/handoffs/storm-status.md` | DRIFTED | `research/audits/evidence/status_generator/` | Supersedes row 4: generator shipped without a CLI entrypoint or automated caller in `src/`; target `docs/STATUS.md` did not exist on main |
| 2026-08-18 | spine purpose gate | `agents/handoffs/spine-purpose.md` | CONFIRMED | `research/audits/evidence/spine_purpose_gate/` | `ExperimentSpec.purpose` required across 7 values; `PolicyGate` and `evallab submit` reject purposeless specs with reason_code `purposeless_spec`; 30 tests pass |
| 2026-08-18 | quota accounting vs events.jsonl truth | `agents/handoffs/quota-accounting.md` | CONFIRMED | `research/audits/evidence/quota_accounting/` | Headroom (70.0% used, 67 snapshots) and artifact tokens (1.17M cached, 113K uncached) verified; `events.jsonl` reservation ($2.50) confirmed as static queue bound, not spend; 79 tests pass |

## Cycle Log
- Cycle 1 (2026-08-18): Audited `preflight` (`agents/handoffs/preflight.md`). Initial verdict: CONFIRMED. Runtime command `evallab preflight` works without network/billable calls, `tests/test_preflight.py` passes 31 tests, digest section is integrated. Board note filed for missing documentation in `docs/operations.md`.
- Cycle 2 (2026-08-18): Audited `storm` (`agents/handoffs/storm-status.md`). Initial verdict: CONFIRMED. Engine sliding window detection, structured `StormAlarm` models, reason code catalog, rendering functions, and 11 tests in `tests/test_storm.py` verified.
- Cycle 3 (2026-08-18): Audited `parquet-compaction` (`agents/handoffs/parquet-compaction.md`). Initial verdict: CONFIRMED. Compaction across 9 tables, date resolution hierarchy, retention window, and 15 tests in `tests/test_parquet_compaction.py` verified; live dry-run planned 72 jobs across 3 dates with 0 row loss.
- Cycle 4 (2026-08-18): Audited `status/status_generator` (`src/evallab/status_generator.py`). Initial verdict: CONFIRMED. Generator correctly projects catalog, queue, and `PROGRAM.json` state into markdown; 9 tests pass in `tests/test_status_generator.py`.
- Cycle 5 (2026-08-18): Applied Integrator corrections for Cycles 1-4 under the sharpened verdict standard: appended corrected rows for `preflight` (CONFIRMED on CLI), `storm` (DRIFTED - dead/unwired in `src/`), `parquet-compaction` (CONFIRMED on module CLI), and `status_generator` (DRIFTED - no CLI/caller, `docs/STATUS.md` never generated on main). Filed unified board note on Disconnected Operator Surfaces. Audited `spine purpose gate` (`agents/handoffs/spine-purpose.md`). Verdict: CONFIRMED. `ExperimentSpec.purpose` strictly required across 7-value taxonomy; `PolicyGate.decide` refuses purposeless specs at dispatch time (`reason_code="purposeless_spec"`); `evallab submit` rejects missing/invalid purpose; `scripts/backfill_spec_purpose.py` verified; 30 tests in `tests/test_spec_purpose.py` pass.
- Cycle 6 (2026-08-18): Audited `quota accounting vs events.jsonl truth` (`agents/handoffs/quota-accounting.md`, `agents/handoffs/quota-gate.md`, `agents/handoffs/quota-fallback.md`). Verdict: CONFIRMED. Verified `quota.py` sidecar fallback across 67 snapshots in `research/evidence/runs/` reporting 70.0% `[observed]` used, 30.0% remaining, weekly rolling window (`10080` min), and `hard_stop=True`. Verified artifact token aggregation: 1,176,832 cached input tokens (91.2%), 113,176 uncached input tokens, 40,375 output tokens across 9 trials ($0.9462 list-price equivalent). Reconciled against `queue/events.jsonl` truth: `events.jsonl` tracks lifecycle state-machine events and gate reason codes, while `dispatch_attempt_reserved` carries a static estimate heuristic (`estimated_cost_usd: 2.5`) rather than measured spend. 79 tests in `tests/test_quota.py` and `tests/test_quota_gate.py` pass.

## Evidence Transcript: Cycle 6 (`quota accounting vs events.jsonl truth`)

### Command 1: `uv run pytest tests/test_quota.py tests/test_quota_gate.py`
```
........................................................................ [ 91%]
.......                                                                  [100%]
79 passed in 0.36s
```

### Command 2: `uv run python -m evallab.quota research/evidence/runs`
```
Subscription quota accounting
generated_at: 2026-08-18T06:56:55.978073+00:00
paid agents:  claude-code, codex
roots:        research/evidence/runs

REMAINING on the subscription (scope: account, NOT the lab)
  used_percent                         70.0 [observed]
  remaining_percent                    30.0 [observed]
  limit_id / plan_type                 codex / prolite
  window                               10080 minutes (168h00m)
  resets_at                            2026-08-20T18:32:49+00:00
  observed_at                          2026-08-15T07:02:25.846000+00:00
  staleness                            71h54m
  credits_balance                      0
  hard stop                            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  rate_limit_reached_type              [unavailable]
  counter resolution                   1.0 percentage point
  source                               event-summary__5E3btLv/agent/quota/rollout-2026-08-15T07-02-04-01a0043a-4b83-7252-a594-fa289617124f.rate-limits.json
  lab's share of that percentage       [unavailable]

CONSUMED by the lab (scope: this lab only)
  paid jobs                            3 [observed]
  paid trials dispatched               9 [observed]
  with observed usage                  9 [observed]
  without usage evidence               0 [observed]
  model turns                          0 [observed]
  uncached input tokens                113,176 [observed]
  cached input tokens                  1,176,832 [observed]
  input tokens (incl. cached)          1,290,008 [observed]
  output tokens                        40,375 [observed]
  cached share of input                91.2% [observed]
  job wall clock                       32m15s [observed]
  longest single trial                 8m35s [observed]
  attempt slots declared (per job)     9 [observed]
  reported_cost_usd                    0.9462 [observed] -- API list-price equivalent, NOT subscription spend
```

### Command 3: Events vs Artifact Truth Comparison
```
--- EVENTS.JSONL TRUTH ---
Total events in queue log: 144
Codex canary events: 53
Dispatch attempt reservations: 6
  Reserved attempt 1 for canary-transaction-reconciliation-codex-20260815: estimated_cost_usd=2.5
  Reserved attempt 1 for canary-terminal-bench-html-js-filter-codex-20260815: estimated_cost_usd=2.5
  Reserved attempt 1 for canary-event-summary-codex-20260815: estimated_cost_usd=2.5
  Reserved attempt 1 for canary-transaction-reconciliation-codex-20260816: estimated_cost_usd=2.5
Dispatch refusals in events: 1
  Refused canary-event-summary-codex-20260814-r2: reason_code=quiet_failure_rule

--- QUOTA.PY ARTIFACT TRUTH ---
Headroom: used_percent=70.0, hard_stop=True, resets_at=2026-08-20 18:32:49+00:00, window_minutes=10080, total_snapshots=67
Ledger Totals: paid_jobs=3, paid_trials=9, uncached_in=113176, cached_in=1176832, out=40375
Reported cost USD (list-price equivalent): 0.9462
```
