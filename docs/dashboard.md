---
status: living
audience:
  - operator
  - analyst
---

# Eval Lab Dashboard (E13)

The local Streamlit research overview (`uv run evallab dashboard`) providing live visibility into operator status, catalog trials, ATIF-derived analytics, spend, canaries, calibrations, and discoveries.

## Pages

`dashboard/app.py` is a multipage app (`st.navigation`): **Integrity** (default) and
**Operations** (the E13 panes documented below, unchanged behaviour).

## Integrity page

Read-only front end over existing projections for studying bad tasks and cheating —
no new detector, judge, trace viewer, or parser. Query logic lives in
`dashboard/integrity.py` (importable functions returning JSON-native row lists);
rendering in `dashboard/integrity_app.py`. A pool selector (MiMo Python ledger pool,
default, or all MiMo tasks) and a domain filter scope every tab. Charts first, tables second:

| Tab | Contents | Sources |
|---|---|---|
| **Overview** | task cards (keep / fix / discard / oracle-proven / broken grader); agent-run cards with controls excluded (runs / passes / counted / excluded / cheat rate = copied_fix or pass_tainted passes ÷ passes); exclusion reasons among passes; finding rules as % of pool, rules on every task in one caption | `v_task_audit`, ledger CSV, oracle sweep labels, results-home processed reports, `task_findings` |
| **Tasks** | findings by rule, domain × rule matrix, filterable one-row-per-task table (verdict, rules, exploit, nop, grader, runs); task detail with dossier summary and runs with 8100 links | `v_task_audit` + `v_task_outcomes`, `task_findings`, `task_versions`, `task_qualification`, `evallab task` dossier builder, `evallab trials` census entry point |
| **Verifier** | oracle evidence (proven / fail / conflict / not attempted), nop>0 graders, nop/oracle control runs, stability-probe verdicts, exploit cracks, LLM-judge graders, isolation findings, held-out regrade, sealed-corpus detector scores | oracle sweep labels, `task_qualification`, `task_versions`, `task_stability`, `task_exploits`, `task_findings`, HAR-197/198 stored outputs when present |
| **Runs** | daily agent passes counted vs excluded, by-model cheat rate, excluded passes with reason, evidence command and 8100 link, integrity-gate failures, action mix for counted-pass vs excluded-pass vs fail | processed reports (pre-counts `process_job/v1` reports load as verdict-less legacy rows), `trial_facts`, `reward_facts`, `agent_actions`, `connect_trials` |
| **New** | last 24h/7d: new excluded passes, exploit cracks, trajectory findings; current totals where sources carry no timestamp | report mtimes, `produced_at`, `evaluated_at` |

Missing data renders as `not available: <reason>` (or `not measured` for outside-test
outputs with no stored file), never empty-as-zero. The page needs no Postgres.
Streamlit never opens a browser: the CLI launcher passes `--server.headless=true`.

### Always on

`scripts/ops/launchd/install-dashboard.sh --load` installs the LaunchAgent
`com.petermakhnatch.evallab.dashboard` serving <http://127.0.0.1:8501>. Like the nightly
refresh, it runs a `git archive` snapshot of the installing commit with its own locked venv
(`~/.local/state/evallab-dashboard/runtime/<commit>`), so worktree pruning cannot break it;
derived Parquet (`<data root>/derived/parquet`) and the results home are read live.
Re-run the installer from a clean checkout to upgrade; `--uninstall` removes it. Logs:
`~/Library/Logs/evallab/dashboard{,.error}.log`.

## Architecture and Data Access

Per `docs/archive/platform-architecture.md` v2 §2.5 and §9, dashboard panes access data exclusively through the **unified attach surface** (`evallab.storage.attach.attach`). The attach surface provides a single DuckDB session registering:

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
