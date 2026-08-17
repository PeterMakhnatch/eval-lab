---
status: living
audience:
  - builder
  - analyst
---

# Join Spine (E05)

The structural invariant of the platform: every downstream number depends on
`spec_id → job_id → trial_id → {trajectory, analysis, observation}`.

- `task_ref@version` joins trials to CraftRecord and Suite.
- `agent_name` joins to AgentProfile.

Any component breaking a spine join fails CI (`tests/test_join_spine.py`).

## v_spine contract

`sql/views.sql` defines the canonical view `v_spine`:

- Walks experiments (spec) → jobs → trials.
- LEFT JOINs trajectory_documents, analysis_invocations, observation_records.
- A trial with no analysis still appears (analysis_id NULL).
- Columns: spec_id, job_id, trial_id, task_ref, task_version, agent_name, ...
- Runs in clean DuckDB via schema fallbacks (lessons.sql pattern).

## Edges validated by checker

`src/evallab/spine.py` (CLI: `python -m evallab.spine check`):

- trial → job
- job → spec
- trajectory → trial
- analysis → trial
- observation → trial

Reports: `N orphans, e.g. id1, id2` per edge + total. Non-zero exit on any orphan.

## What an orphan means

An orphan trial (no job) means every aggregate, leaderboard, or lesson
describes a different population than claimed. The number is silently wrong.

## How to run

```bash
uv run python -m evallab.spine check
uv run pytest tests/test_join_spine.py -q
```

See `docs/platform-architecture.md` §2.1, §2.2, §10 for full context.

## v_quota_today / v_suite_leaderboard

Omitted from `sql/views.sql`: quota_consumption and suites tables absent in
current schema; no rows to aggregate. See handoff Blockers.