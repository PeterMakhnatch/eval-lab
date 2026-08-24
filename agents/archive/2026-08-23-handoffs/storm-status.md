Status: done
Last: merged as PR #81 (`fcdbf4f`)
Next: none
Blockers: none

# WS-E Items 5 & 6 Handoff: Storm Alarms Engine & STATUS.md Generator

## 1. Summary of Changes

Implemented the Storm Alarms Engine and STATUS.md Generator in accordance with WS-E (Items 5 & 6):

1. **Storm Alarms Engine (`src/evallab/storm.py`)**:
   - Implemented sliding 1-hour window detection for repeated identical `reason_code` events in `queue/events.jsonl`.
   - Formulates structured `StormAlarm` and `StormReport` models with alarm levels (`info`, `warning`, `critical`), event counts, affected job/spec IDs, timestamps, and actionable operator guidance.
   - Includes a comprehensive catalog mapping reason codes and dynamic prefixes (`subscription_quota_exhausted`, `headless_doctor_failed:*`, `missing_credential:*`, `quiet_failure_rule`, `daily_cost_ceiling`, `paid_run_unauthorized`, etc.) to specific severity levels and remediation steps.
   - Provides utilities for banner rendering (`render_storm_banner`), digest section generation (`digest_storm_section`), and status snapshot conversion (`status_items_from_alarms`).

2. **STATUS.md Generator (`src/evallab/status_generator.py`)**:
   - Implemented deterministic and idempotent status report generation projecting live catalog and queue state into `research/experiments/STATUS.md`.
   - Answers "what happened yesterday and what is running now" deterministically without requiring interactive terminal navigation.
   - Sections:
     - `RECENT (Yesterday: YYYY-MM-DD)`: Summarizes completed trials, pass rates (`reward == 1.0`), models, and exceptions.
     - `RUNNING NOW`: Lists active jobs/specs in `queue/running/` and `queue/approved/`.
     - `NEXT`: Details queued specs in `queue/waiting/`, `queue/pending/`, `queue/proposed/` with reason codes and blockers.
     - `STORM ALARMS`: Emits high-visibility alert banner if active storm alarms are detected.
     - `PROGRAM EXPERIMENTS & TASK DECISIONS`: Extracts next actions and human-owned unresolved decisions from `PROGRAM.json`.
     - `SYSTEM HEALTH & OPERATIONAL SMOKE`: Reports catalog accessibility and control run counts.

3. **Documentation (`docs/storm-alarms.md`)**:
   - Living documentation covering storm detection rules, structured models, reason codes catalog, and STATUS.md generator contract.

4. **Test Suites (`tests/test_storm.py`, `tests/test_status_generator.py`)**:
   - 10 tests in `test_storm.py` validating quiet event streams, storm triggers, sliding window boundaries, dynamic prefix matching, file loading resilience, and rendering functions.
   - 7 tests in `test_status_generator.py` validating idempotence, trial aggregation, queue state representations, storm alarm banner embedding, and PROGRAM.json task decisions.

## 2. Verification Evidence

- `uv run ruff check .` passed with 0 errors across the codebase.
- `uv run pytest tests/test_storm.py tests/test_status_generator.py` passed with 17/17 tests passing in 0.22s.
- `uv run pytest` (full suite) passed with 972 passed, 1 xfailed (0 regressions).

## 3. Leased Paths

- `src/evallab/storm.py`
- `tests/test_storm.py`
- `src/evallab/status_generator.py`
- `tests/test_status_generator.py`
- `docs/storm-alarms.md`
- `agents/handoffs/storm-status.md`

Non-goals respected: No edits made to `schemas.py`, `queue.py`, `cli.py`, or `policy/`.
