---
status: living
audience:
  - operator
  - analyst
---

# Eval Lab Dashboard (E13)

The local Streamlit research overview (`uv run evallab dashboard`) providing live visibility into operator status, catalog trials, ATIF-derived analytics, spend, canaries, calibrations, and discoveries.

## Architecture and Data Access

Per `docs/platform-architecture.md` v2 §2.5 and §9, dashboard panes access data exclusively through the **unified attach surface** (`evallab.storage.attach.attach`). The attach surface provides a single DuckDB session registering:

- **Zone Z2 (`z2`)**: PostgreSQL catalog tables attached via `postgres_scanner` (`z2.public.trials`, `z2.public.jobs`, `z2.public.canary_drift_observations`, `z2.public.judge_calibrations`).
- **Zone Z3 (`z3`)**: Parquet analytics views (`trial_facts`, `reward_facts`, `artifact_facts`, `trajectories`, `steps`, `tool_calls`, `tool_usage`, `observations`, `jobs`) unioning hot partitions (`job_id=*/trial_id=*/`) and compacted cold storage (`compact/<table>/dt=*/`).
- **Zone Z4 (`z4`)**: Knowledge front-matter table (`z4.front_matter`).

Direct Parquet globbing and direct database driver connections are prohibited under `dashboard/`.

## Pane to View Mapping

All dashboard panes declare their attach-surface view in code (`dashboard.queries.PANES`):

```python
PANES = {
    "leaderboard": "z2.trials",
    "canaries": "z2.canary_drift_observations",
    "spend": "z2.trials",
    "calibrations": "z2.judge_calibrations",
    "atif": "trial_facts",
    "discoveries": "z4.front_matter",
}
```

### Pane Details and Degradation Behavior

| Pane | Surface View / Source | Required Zone | Unavailable Zone Behavior | Empty Data Behavior |
|---|---|---|---|---|
| **Operator status** | `StatusSnapshot` | Z2 (optional) + filesystem | Sections marked `unavailable` with specific probe failure reasons (e.g. Postgres unreachable) | Renders empty section info |
| **Leaderboard by cohort** | `z2.trials` JOIN `z2.jobs` | `z2` | Raises `ZoneUnavailableError("z2")`, rendered as warning with DSN and error details | Shows info: "No catalog trials are indexed yet" (distinct from unscorable trials) |
| **Canary trend** | `z2.canary_drift_observations` | `z2` | Raises `ZoneUnavailableError("z2")`, rendered as warning with DSN and error details | Shows info: "No canary observations are indexed yet" |
| **Spend vs daily ceiling** | `z2.trials` (grouped by UTC date) | `z2` | Raises `ZoneUnavailableError("z2")`, rendered as warning with DSN and error details | Shows 0 spend against daily ceiling |
| **Queue funnel** | `queue/{pending,approved,running,done,failed}` | Filesystem | Rendered as warning if queue directory is missing | Shows 0 counts per state |
| **Calibration history** | `z2.judge_calibrations` + file fallback | `z2` (or files) | If Z2 is unavailable and no file records exist, raises `ZoneUnavailableError("z2")` | Shows info: "No measured calibration records are available" |
| **ATIF-derived activity** | `trial_facts`, `tool_usage` | `z3` | Raises `ZoneUnavailableError("z3")`, rendered as warning stating derived root missing | Shows info: "No ATIF-derived Parquet is available" |
| **DISCOVERIES** | `z4.front_matter` / `digests/DISCOVERIES.md` | `z4` / Filesystem | Rendered as warning if journal is missing | Shows info: "No discovery entries are recorded" |

## Honest Degradation

When a storage zone cannot attach (e.g., PostgreSQL is offline, or the derived Parquet root has not been generated), panes report the zone unavailability with the exact underlying reason rather than silently rendering an empty table that looks like "no data yet". Unscorable runs (trials with exceptions or unrecorded rewards) remain clearly distinguished from cohorts with zero trials.

## Running the Dashboard

```bash
# Start the Streamlit research overview
uv run evallab dashboard

# Run the query and attach-surface test suite
uv run pytest dashboard/tests
```
