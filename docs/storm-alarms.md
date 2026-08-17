---
status: living
audience:
  - operator
  - runner
---
# Storm Alarms & STATUS.md Generator

## 1. Overview and Motivation

During long-running automated evaluations, automated ticks and nightly loops can encounter repetitive errors, capacity bottlenecks, quota exhaustions, or policy refusals. When an automated loop defers or fails silently without a visible operator signal, runs stall unnoticed.

The **Storm Alarms Engine** (`src/evallab/storm.py`) and **STATUS.md Generator** (`src/evallab/status_generator.py`) address this:

1. **Storm Alarms**: Detects bursts of repeated events sharing the same `reason_code` (>N events within a 1-hour sliding window) in `queue/events.jsonl`.
2. **Actionable Recommendations**: Emits structured `StormAlarm` models with severity levels and domain-specific recommended operator actions.
3. **Legible Surfaces**: Automatically projects storm banners into `research/experiments/STATUS.md`, nightly digests, and `evallab status` snapshots.
4. **Deterministic STATUS.md**: Generates an idempotent, human-readable overview of what ran yesterday and what is currently running without requiring interactive terminal navigation.

---

## 2. Storm Detection Contract

### Sliding Window Rule

- **Input**: Stream of `QueueEvent` records read from `queue/events.jsonl` (including rotated archive segments `.events.jsonl.*`).
- **Condition**: An alarm is triggered if strictly more than $N$ events with the identical `reason_code` occur within any 1-hour window (`timedelta(hours=1)`):
  $$\text{Count}(\text{reason\_code}, [t_{\text{start}}, t_{\text{start}} + 1\text{h}]) > N$$
- **Default Threshold**: $N = 5$ (configurable per scan).
- **Quiet State**: If $\le N$ events occur in any 1-hour window, the engine remains quiet (`has_alarms == False`).

### Structured Models

```python
class StormAlarm(BaseModel):
    reason_code: str
    alarm_level: Literal["info", "warning", "critical"]
    count: int
    threshold: int
    window_seconds: int
    window_start: datetime
    window_end: datetime
    first_occurred_at: datetime
    last_occurred_at: datetime
    recommended_action: str
    job_names: list[str]
    spec_ids: list[str]


class StormReport(BaseModel):
    checked_at: datetime
    threshold: int
    window_seconds: int
    total_events_evaluated: int
    alarms: list[StormAlarm]
```

---

## 3. Catalog of Reason Codes and Recommended Actions

| Reason Code / Pattern | Severity | Recommended Operator Action |
|---|---|---|
| `subscription_quota_exhausted` | `CRITICAL` | Provider reports subscription allowance exhausted. Suspend dispatch or switch to approved provider/tier. |
| `subscription_quota_ceiling` | `WARNING` | Approaching provider daily spend or run quota ceiling. Review active queue and approve overrides if necessary. |
| `daily_cost_ceiling` | `CRITICAL` | Daily cost ceiling exceeded. Adjust `StandingApprovalsPolicy.daily_cost_ceiling_usd` or hold dispatch until next UTC day. |
| `per_job_cost_ceiling` | `WARNING` | Job estimated cost exceeds per-job ceiling. Review spec `est_cost_usd` or adjust policy limit. |
| `quiet_failure_rule` | `CRITICAL` | Consecutive harness failures triggered quiet failure rule. Inspect harness error logs in `~/Library/Logs/evallab/` and quarantine bad task specs. |
| `paid_run_unauthorized` | `WARNING` | Multiple specs waiting for paid run authorization. Operator review needed: approve with `--actor` or verify standing policy. |
| `paid_run_authorization_mismatch` | `WARNING` | Authorization spec mismatch storm. Check spec generator submission IDs against recorded authorizations. |
| `paid_run_authorization_stale` | `WARNING` | Recorded authorization predates spec submission. Re-approve with fresh authorization. |
| `purposeless_spec` | `WARNING` | Specs missing required `purpose` field. Fix upstream spec generation to declare valid purpose. |
| `unregistered_task` | `WARNING` | Unregistered task specs in queue. Run task registration or verify `library/tasks` inventory. |
| `task_not_registered` | `WARNING` | Task not registered in library. Run task registration before submitting specs. |
| `task_path_redirection` | `CRITICAL` | Task path redirection error detected. Check task package registry metadata. |
| `task_version_mismatch` | `WARNING` | Task package version mismatch. Rebuild task artifacts or re-register package. |
| `task_digest_mismatch` | `CRITICAL` | Task package digest mismatch. Rebuild task artifacts or inspect package tamper state. |
| `invalid_control_evidence` | `CRITICAL` | Task control evidence invalid. Inspect oracle/nop evidence for task package. |
| `missing_package_component` | `CRITICAL` | Task package component missing. Inspect task directory contents. |
| `headless_doctor_failed:*` | `CRITICAL` | Headless doctor infrastructure checks failing repeatedly. Inspect Docker daemon, PostgreSQL connection, disk headroom, and keychain credentials. |
| `missing_credential:*` | `CRITICAL` | Required credentials missing. Restore credentials in keychain or environment. |
| `transient_harness:provider_http_429`| `WARNING` | Provider rate limit HTTP 429 storm. Back off dispatch cadence or adjust concurrent workers. |
| `no_approved_specs` | `INFO` | Repeated tick deferrals with no approved specs. Queue is empty or waiting for human approval. |

---

## 4. Integration Surfaces

### 1. Alert Banner Rendering
`render_storm_banner(alarms)` generates a high-visibility Markdown banner:
```markdown
> ⚠️ **STORM ALARM ACTIVE** — Multiple event storms detected in queue log (>N/hour):
>
> 🚨 **CRITICAL**: `subscription_quota_exhausted` — **8** events within 1h window (threshold > 5).
>   *Recommended Action:* Provider reports subscription allowance exhausted. Suspend dispatch or switch to approved provider/tier.
```

### 2. Nightly Digest Embedding
`digest_storm_section(alarms)` produces a clean Markdown section and table for nightly digests.

### 3. Status Snapshot
`status_items_from_alarms(alarms)` produces `StatusItem` records with `availability="review-needed"` for critical alarms and `availability="draft"` for warnings.

---

## 5. STATUS.md Generator

The module `src/evallab/status_generator.py` compiles a deterministic snapshot into `research/experiments/STATUS.md`:

- **Deterministic & Idempotent**: Produces the identical output on repeated runs over the same repository and date state.
- **Answers**:
  1. **RECENT (Yesterday)**: Completed trials, pass rates (`reward == 1.0`), models, and exception breakdowns.
  2. **RUNNING NOW**: Live specs in `queue/running/` and `queue/approved/`.
  3. **NEXT**: Pending, waiting, and proposed queue items with reasons and blockers.
  4. **STORM ALARMS**: Active alarms if any burst was detected in the recent window.
  5. **PROGRAM EXPERIMENTS & TASK DECISIONS**: Human-owned unresolved decisions and next actions from `research/experiments/PROGRAM.json`.
  6. **SYSTEM HEALTH**: Catalog accessibility, operational smoke counts, and overall status.

### Programmatic Usage

```python
from evallab.status_generator import generate_status_markdown, update_status_file

# Generate markdown string
markdown_text = generate_status_markdown(repo_root)

# Update research/experiments/STATUS.md on disk
updated_path = update_status_file(repo_root)
```
