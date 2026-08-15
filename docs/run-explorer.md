# Run & analysis explorer

M005 (Platform). Logic: `src/evallab/explorer.py`. Page:
`dashboard/explorer.py`. Tests: `tests/test_explorer.py` (16, fixture-driven,
zero host state). Fixtures: `tests/fixtures/explorer/`.

## What it answers

Select a task, job, trial, trajectory, or analysis and see: what ran
(agent/model/config), what happened (reward, exception, timing, cost,
artifacts), why it was classified (analysis sidecar with citations resolved
to the actual step and tool call), and the exact safe command to act next.

```bash
uv run --with streamlit==1.61.1 streamlit run dashboard/explorer.py   # repo evidence
EVALLAB_EXPLORER_ROOT=tests/fixtures/explorer \
  uv run --with streamlit==1.61.1 streamlit run dashboard/explorer.py # fixture demo
```

## Contracts (tested)

- **Read-only.** Building the index twice leaves the evidence byte-identical
  (`test_index_build_performs_zero_writes`). No page control mutates state.
- **Provenance on every field**: `observed` (read from evidence), `derived`
  (computed here, with the rule named), `draft` (unreviewed model output —
  all analysis conclusions render this way until reviewed), `unavailable`
  (missing/malformed, with the reason).
- **Infrastructure exceptions ≠ reward failures.** Exception trials render in
  their own section; their reward is `unavailable`, never 0.
- **Citations are verified, not trusted.** Each analysis citation resolves
  against the trial: file exists inside the jail, step exists in the
  trajectory, tool call exists in that step — or it renders ⛔ with the reason.
- **Path jail.** `..`, absolute paths, and anything under task `tests/` or
  `solution/` resolve to refusal; artifact links are trial-relative only.
- **No secrets.** Key-shaped names in any rendered mapping are `[redacted]`.
- **Cold start navigable.** Missing roots, malformed sidecars, absent
  trajectories, and duplicate trial keys degrade into labeled notes.
- **No ranking of incomparable cohorts.** The explorer renders one entity at
  a time and never aggregates across agents/models; comparisons stay in
  `evallab compare`, which enforces cohort declarations.

## Next Action (emits, never executes)

Task → oracle/nop control commands. Trial → `harbor view <trial-dir>` and
`evallab analyze plan <trial-dir>`; infra exceptions add a status re-check.
Queue → `evallab submit` / `evallab approve` (policy ceilings still apply).
Every command is a string in a copy box; the explorer holds no executor.
