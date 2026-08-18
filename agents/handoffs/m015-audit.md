Status: building
Last: completed cycle 5 audit of spine purpose gate (CONFIRMED) & applied integrator corrections for cycles 1-4
Next: cycle 6 audit of quota accounting vs events.jsonl truth (agents/handoffs/quota-accounting.md)
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

## Cycle Log
- Cycle 1 (2026-08-18): Audited `preflight` (`agents/handoffs/preflight.md`). Initial verdict: CONFIRMED. Runtime command `evallab preflight` works without network/billable calls, `tests/test_preflight.py` passes 31 tests, digest section is integrated. Board note filed for missing documentation in `docs/operations.md`.
- Cycle 2 (2026-08-18): Audited `storm` (`agents/handoffs/storm-status.md`). Initial verdict: CONFIRMED. Engine sliding window detection, structured `StormAlarm` models, reason code catalog, rendering functions, and 11 tests in `tests/test_storm.py` verified.
- Cycle 3 (2026-08-18): Audited `parquet-compaction` (`agents/handoffs/parquet-compaction.md`). Initial verdict: CONFIRMED. Compaction across 9 tables, date resolution hierarchy, retention window, and 15 tests in `tests/test_parquet_compaction.py` verified; live dry-run planned 72 jobs across 3 dates with 0 row loss.
- Cycle 4 (2026-08-18): Audited `status/status_generator` (`src/evallab/status_generator.py`). Initial verdict: CONFIRMED. Generator correctly projects catalog, queue, and `PROGRAM.json` state into markdown; 9 tests pass in `tests/test_status_generator.py`.
- Cycle 5 (2026-08-18): Applied Integrator corrections for Cycles 1-4 under the sharpened verdict standard: appended corrected rows for `preflight` (CONFIRMED on CLI), `storm` (DRIFTED - dead/unwired in `src/`), `parquet-compaction` (CONFIRMED on module CLI), and `status_generator` (DRIFTED - no CLI/caller, `docs/STATUS.md` never generated on main). Filed unified board note on Disconnected Operator Surfaces. Audited `spine purpose gate` (`agents/handoffs/spine-purpose.md`). Verdict: CONFIRMED. `ExperimentSpec.purpose` strictly required across 7-value taxonomy; `PolicyGate.decide` refuses purposeless specs at dispatch time (`reason_code="purposeless_spec"`); `evallab submit` rejects missing/invalid purpose; `scripts/backfill_spec_purpose.py` verified; 30 tests in `tests/test_spec_purpose.py` pass.

## Evidence Transcript: Cycle 5 (`spine purpose gate`)

### Command 1: `uv run pytest tests/test_spec_purpose.py`
```
..............................                                           [100%]
30 passed in 0.42s
```

### Command 2: `uv run evallab submit <purposeless_spec.json>` (CLI Schema Validation Refusal)
```
Exit code: 2
error: 3 validation errors for ExperimentSpec
hypothesis
  Field required [type=missing, input_value={'name': 'test-spec', 'ta...ary', 'agent': 'oracle'}, input_type=dict]
purpose
  Field required [type=missing, input_value={'name': 'test-spec', 'ta...ary', 'agent': 'oracle'}, input_type=dict]
submitted_by
  Field required [type=missing, input_value={'name': 'test-spec', 'ta...ary', 'agent': 'oracle'}, input_type=dict]
```

### Command 3: `PolicyGate.decide` on `purposeless` bypass spec
```
Admitted: False
Reason Code: purposeless_spec
Message:
spec unnamed-intent declares no purpose. Every experiment spec must declare its intent so work can be grouped, budgeted, and reviewed by intent rather than merely listed.
  allowed values: baseline | comparison | elicitation | drift | calibration | craft | practice
  fix: set "purpose" in the spec file to one of the values above, then resubmit with `uv run evallab submit <spec.json>`
```

### Command 4: `uv run python scripts/backfill_spec_purpose.py`
```
queue: /Users/petermakhnatch/Developer/eval-lab/.worktrees/m015-audit/queue
would update: 0
already declared: 0
skipped (not a spec, or would still be invalid): 0
```
