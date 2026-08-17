---
status: living
audience:
  - operator
  - analyst
---
# Operating Surfaces: Digest, Storm Alarms, and STATUS.md

This document defines the contracts, rendering order, and lifecycle of the operator surfaces in Eval Lab: the nightly digest (`digests/YYYY-MM-DD.md`), storm alarm banners, and the deterministic status projection (`research/experiments/STATUS.md`).

## 1. Nightly Digest Section Ordering

The nightly digest (`src/evallab/digest.py`) renders a deterministic snapshot for each UTC catalog day. The section ordering is fixed per platform architecture §9:

1. **Header & Period**: Title `# Eval lab digest — <YYYY-MM-DD>` and reporting period (`<YYYY-MM-DD - 1d>`).
2. **`## Automation status`**: Readiness health, quarantine status, zero dispatch enforcement, catalog accessibility.
3. **`## Preflight`**: Provider quota headroom, queue breakdown by declared purpose, power warnings for queued comparisons, and admission verdict.
4. **`## Completed trials`**: Completed trials for the local reporting period grouped by job, task, agent, rewards, and exception spread.
5. **`## Early-morning automation`** *(conditional)*: Trials completed after the reporting-period cutoff.
6. **`## Canary drift`**: Canary baseline observations and drift assessments per catalog day.
7. **`## Cost and failures`**: Recorded spend vs daily ceiling and exception taxonomy breakdown (`harness_failure`, `transient_harness`).
8. **`## Queue`**: State depths (`proposed`, `pending`, `approved`, `waiting`, `running`, `done`, `failed`) and waiting proposal reasons.
9. **`## Evidence and calibration`**: Run directory corpus bytes, judge calibration state, canary observation counts.
10. **`## Queue events`**: Chronological run log with consecutive duplicate folding (`×N`).
11. **`## Storm alarms`**: Reason code burst detection across queue events.
12. **`<!-- run-bytes: N -->`**: Machine-readable digest footer tracking corpus size.

Additional enrichments (such as Fleet status or GC reclaim plans) may be appended by `ResearcherLoop` or `append_gc_plan_to_digest` at the end of the nightly cycle.

## 2. Storm Alarms: Quiet vs Unavailable vs Active

Storm alarms (`src/evallab/storm.py`) detect bursts of repeated `reason_code` events (>5 identical reasons within any 1-hour window). The digest and status projections distinguish three states:

### (a) Quiet (Healthy)

When zero event bursts exceed the threshold, the section renders an explicit quiet affirmation rather than a blank or omitted block:

```markdown
## Storm alarms

- Status: quiet (no reason_code storm detected in 1h window)
```

### (b) Unavailable (Degraded)

When the event source or alarm loader cannot be evaluated (e.g. storage error, missing file permissions), the failure reason is explicitly named. An unavailable reading is never rendered as quiet or empty:

```markdown
## Storm alarms

- Unavailable: storm alarms could not be evaluated (RuntimeError: disk error). That is not a statement that no event storm occurred.
```

### (c) Active Alarm

When an event storm is detected, a table is rendered with severity levels, reason codes, repeat counts, time window, and actionable operator guidance:

```markdown
## Storm alarms

| level | reason_code | count in 1h | window | recommended action |
|---|---|---:|---|---|
| CRITICAL | `subscription_quota_exhausted` | 8 (threshold > 5) | 12:00:00 – 12:45:00 UTC | Provider reports subscription allowance exhausted. Suspend dispatch or switch to approved provider/tier. |
```

Active alarms also project a visible blockquote banner into `research/experiments/STATUS.md` and surface `review-needed` status items in `evallab status`.

## 3. STATUS.md Lifecycle and Regeneration

`research/experiments/STATUS.md` (`src/evallab/status_generator.py`) provides an idempotent, human-readable answer to "what happened yesterday and what is running now" without requiring terminal access.

### When STATUS.md is Generated:

1. **Nightly Cycle**: `NightlyCycle.run` in `src/evallab/automation.py` executes status file generation as an ordered step after ingest, dispatch, researcher passes, and digest rendering.
2. **Direct CLI / Module Invocations**: Via `update_status_file(repo_root)` or `generate_status_markdown(repo_root)`.

### Idempotency Guarantee:

Running the generator multiple times against the same repository and date state produces byte-identical output. Trials, queue states, program items, and storm alarms are sorted deterministically.
